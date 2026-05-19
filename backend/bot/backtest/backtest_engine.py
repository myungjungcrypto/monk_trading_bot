"""
Backtest Engine — 메인 백테스트 루프.

기존 PriceBuffer, SignalEngine, PositionManager, RiskManager를
SimulatedExchange와 조합하여 과거 데이터로 전략을 검증합니다.
"""

import asyncio
import logging
import time as _time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    import pandas as pd

from backend.bot.backtest.backtest_result import BacktestResult
from backend.bot.backtest.data_fetcher import load_pair_data
from backend.bot.backtest.simulation_clock import SimulationClock
from backend.bot.backtest.simulated_exchange import FeeConfig, SimulatedExchange
from backend.bot.backtest.tick_generator import generate_interleaved_ticks
from backend.bot.position_manager import PairDirection, PairTrade, PositionManager
from backend.bot.price_buffer import PriceBuffer
from backend.bot.risk_manager import ExitReason, RiskAction, RiskConfig, RiskManager
from backend.bot.signal import MultiTFConfig, MultiTimeframeSignalEngine, SignalDirection

logger = logging.getLogger(__name__)

EXCHANGE_NAME = "simulated"
WARMUP_HOURS = 48


@dataclass
class BacktestConfig:
    """백테스트 설정."""
    mode: str = "swing"
    start_date: str = "2025-03-17"
    end_date: str = "2026-03-17"
    position_size_usd: float = 500.0
    leverage: int = 3
    fee_config: Optional[FeeConfig] = None
    fee_preset: str = "backpack"
    risk_config: Optional[RiskConfig] = None
    signal_config: Optional[MultiTFConfig] = None
    force_download: bool = False
    # 미리 로드된 데이터 (실험용 — 반복 다운로드 방지)
    preloaded_btc_df: Optional["pd.DataFrame"] = None
    preloaded_eth_df: Optional["pd.DataFrame"] = None


class BacktestEngine:
    """백테스트 엔진."""

    def __init__(self, config: BacktestConfig):
        self.config = config
        self.clock = SimulationClock()

        # 수수료 설정
        fee = config.fee_config or FeeConfig.from_preset(config.fee_preset)

        # 시그널 설정
        signal_cfg = config.signal_config or MultiTFConfig.from_mode(config.mode)

        # 리스크 설정
        risk_cfg = config.risk_config or self._default_risk_config(config.mode)

        # 컴포넌트 초기화
        self.exchange = SimulatedExchange(fee_config=fee)
        self.price_buffer = PriceBuffer()
        self.signal_engine = MultiTimeframeSignalEngine(config=signal_cfg)
        self.position_manager = PositionManager()
        self.risk_manager = RiskManager(config=risk_cfg)

        # 심볼 등록
        self.price_buffer.register_symbols(
            EXCHANGE_NAME,
            self.exchange.perp_symbol("BTC"),
            self.exchange.perp_symbol("ETH"),
        )

        # 결과
        self.result = BacktestResult(
            mode=config.mode,
            start_date=config.start_date,
            end_date=config.end_date,
            fee_preset=config.fee_preset,
            position_size_usd=config.position_size_usd,
            leverage=config.leverage,
        )

    async def run(self) -> BacktestResult:
        """백테스트를 실행합니다."""
        t0 = _time.time()

        # 미리 로드된 데이터가 있으면 재사용, 없으면 다운로드
        if self.config.preloaded_btc_df is not None and self.config.preloaded_eth_df is not None:
            btc_df = self.config.preloaded_btc_df
            eth_df = self.config.preloaded_eth_df
            logger.info("Using preloaded data: BTC=%d, ETH=%d candles", len(btc_df), len(eth_df))
        else:
            warmup_start = self._subtract_hours(self.config.start_date, WARMUP_HOURS)
            logger.info(
                "Loading data: %s ~ %s (warmup from %s)",
                warmup_start, self.config.end_date, self.config.start_date,
            )
            btc_df, eth_df = await load_pair_data(
                warmup_start, self.config.end_date,
                force=self.config.force_download,
            )

        if btc_df.empty or eth_df.empty:
            logger.error("No data available")
            return self.result

        # 백테스트 실제 시작 시각 (워밍업 후)
        actual_start_ms = int(
            datetime.strptime(self.config.start_date, "%Y-%m-%d")
            .replace(tzinfo=timezone.utc)
            .timestamp() * 1000
        )

        # 일별 PnL 추적
        current_day = ""
        day_pnl = 0.0
        cumulative_pnl = 0.0

        # 틱 생성 & 루프
        tick_count = 0
        total_ticks = len(btc_df) * 4 + len(eth_df) * 4
        log_interval = max(total_ticks // 20, 1)

        for tick in generate_interleaved_ticks(btc_df, eth_df):
            tick_count += 1

            # 시뮬레이션 시간 업데이트
            self.clock.set_ms(tick.timestamp_ms)

            # 거래소 가격 업데이트
            symbol = self.exchange.perp_symbol(tick.symbol)
            self.exchange.set_price(symbol, tick.price)

            # PriceBuffer 업데이트
            self.price_buffer.update(
                EXCHANGE_NAME, symbol, tick.price, tick.timestamp_ms,
            )

            # 워밍업 기간에는 진입 차단
            if tick.timestamp_ms < actual_start_ms:
                continue

            # 일별 PnL 추적
            day = self.clock.strftime("%Y-%m-%d")
            if day != current_day:
                if current_day:
                    self.result.daily_pnl.append(day_pnl)
                    day_pnl = 0.0
                current_day = day

            # 시그널 평가
            signal = self.signal_engine.evaluate(self.price_buffer)

            # 진입 판단
            if not self.position_manager.has_open_position and signal.should_enter:
                if self.risk_manager.can_open_trade(
                    self.position_manager.open_trade_count,
                    current_time=self.clock.now,
                ):
                    direction = (
                        PairDirection.LONG_BTC_SHORT_ETH
                        if signal.direction == SignalDirection.LONG_BTC_SHORT_ETH
                        else PairDirection.SHORT_BTC_LONG_ETH
                    )
                    trade = await self.position_manager.open_pair(
                        exchange=self.exchange,
                        direction=direction,
                        size_usd=self.config.position_size_usd,
                        leverage=self.config.leverage,
                        zscore=signal.zscore_5m,
                        spread_pct=signal.divergence_pct,
                    )
                    if trade:
                        trade.opened_at = self.clock.now

            # 포지션 모니터링 (오픈 포지션이 있을 때)
            if self.position_manager.has_open_position:
                await self.position_manager.update_positions(self.exchange)

                for trade_id, trade in list(self.position_manager.open_trades.items()):
                    decision = self.risk_manager.evaluate(
                        trade,
                        zscore_reverted=signal.should_exit_zscore,
                        current_time=self.clock.now,
                    )

                    if decision.action == RiskAction.EXIT:
                        closed = await self.position_manager.close_pair(
                            trade_id, self.exchange, reason=decision.reason.value,
                        )
                        if closed:
                            closed.closed_at = self.clock.now
                            # 수수료 계산: 4-leg fee + 청산 2-leg slippage.
                            # 진입 slippage는 SimulatedExchange의 entry fill에 이미 반영됩니다.
                            fee_per_leg = (
                                self.config.position_size_usd
                                * self.exchange.fee_config.taker_fee_pct / 100.0
                            )
                            close_slippage_per_leg = (
                                self.config.position_size_usd
                                * self.exchange.fee_config.slippage_pct / 100.0
                            )
                            closed.total_fees_usd = fee_per_leg * 4 + close_slippage_per_leg * 2
                            self.result.add_trade(closed, decision.reason.value)
                            self.risk_manager.on_trade_closed(
                                trade_id, closed.net_pnl_usd,
                                current_time=self.clock.now,
                            )
                            day_pnl += closed.net_pnl_usd
                            cumulative_pnl += closed.net_pnl_usd
                            self.result.equity_curve.append(cumulative_pnl)

                    elif decision.action == RiskAction.AVERAGING_DOWN:
                        await self.position_manager.averaging_down(
                            trade_id, self.exchange,
                            multiplier=self.risk_manager.config.averaging_multiplier,
                        )

                    elif decision.action == RiskAction.SIZE_REDUCTION:
                        await self.position_manager.size_reduction(
                            trade_id, self.exchange,
                            ratio=self.risk_manager.config.size_reduction_ratio,
                        )

            # 진행 로그
            if tick_count % log_interval == 0:
                pct = tick_count / total_ticks * 100
                logger.info(
                    "Progress: %.0f%% | Trades: %d | PnL: $%.2f",
                    pct, self.result.total_trades, cumulative_pnl,
                )

        # 잔여 포지션 강제 청산
        for trade_id, trade in list(self.position_manager.open_trades.items()):
            closed = await self.position_manager.close_pair(
                trade_id, self.exchange, reason="END_OF_DATA",
            )
            if closed:
                closed.closed_at = self.clock.now
                fee_per_leg = (
                    self.config.position_size_usd
                    * self.exchange.fee_config.taker_fee_pct / 100.0
                )
                closed.total_fees_usd = fee_per_leg * 4
                self.result.add_trade(closed, "END_OF_DATA")
                cumulative_pnl += closed.net_pnl_usd
                self.result.equity_curve.append(cumulative_pnl)

        # 마지막 날 PnL 기록
        if day_pnl != 0:
            self.result.daily_pnl.append(day_pnl)

        elapsed = _time.time() - t0
        logger.info(
            "Backtest complete in %.1fs | %d ticks | %d trades",
            elapsed, tick_count, self.result.total_trades,
        )

        return self.result

    # ── 유틸리티 ──────────────────────────────────────────────

    @staticmethod
    def _subtract_hours(date_str: str, hours: int) -> str:
        from datetime import timedelta
        dt = datetime.strptime(date_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        dt -= timedelta(hours=hours)
        return dt.strftime("%Y-%m-%d")

    @staticmethod
    def _default_risk_config(mode: str) -> RiskConfig:
        if mode == "scalp":
            return RiskConfig(
                take_profit_pct=0.4,
                stop_loss_pct=-1.5,
                max_hold_hours=2.0,
                trailing_activate_at_pct=0.25,
                trailing_trail_pct=0.15,
            )
        elif mode == "position":
            return RiskConfig(
                take_profit_pct=2.0,
                stop_loss_pct=-5.0,
                max_hold_hours=48.0,
                trailing_activate_at_pct=1.2,
                trailing_trail_pct=0.6,
            )
        else:  # swing (default)
            return RiskConfig(
                take_profit_pct=0.8,
                stop_loss_pct=-3.0,
                max_hold_hours=12.0,
                trailing_activate_at_pct=0.5,
                trailing_trail_pct=0.3,
            )
