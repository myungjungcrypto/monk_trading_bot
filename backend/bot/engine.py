"""
Bot Engine — BTC/ETH 페어 트레이딩 메인 이벤트 루프.

v2: REST 폴링 제거 → WebSocket 이벤트 드리븐 구조.
    PriceHub에서 틱 수신 → MultiTF 시그널 평가 → 진입/청산 실행.
"""

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backend.bot.exchanges.backpack import BackpackExchange
from backend.bot.exchanges.base import BaseExchange
from backend.bot.position_manager import PairDirection, PositionManager
from backend.bot.price_buffer import PriceBuffer
from backend.bot.price_hub import PriceHub
from backend.bot.risk_manager import (
    ExitReason,
    RiskAction,
    RiskConfig,
    RiskManager,
)
from backend.bot.signal import (
    MultiTFConfig,
    MultiTimeframeSignalEngine,
    Signal,
    SignalDirection,
    TradingMode,
    # v1 호환
    SignalConfig,
    SignalEngine,
)
from backend.bot.telegram_notifier import TelegramNotifier
from backend.bot.trade_recorder import TradeRecorder
from backend.bot.warmup import warmup

logger = logging.getLogger(__name__)

EXECUTION_ALERT_ONLY = "alert_only"
EXECUTION_PAPER = "paper"
EXECUTION_LIVE = "live"
VIRTUAL_EXCHANGE_NAME = "virtual"


@dataclass
class BotConfig:
    """봇 전체 설정."""
    # 거래소 설정
    position_size_usd: float = 500.0
    leverage: int = 3
    paper_trading: bool = False
    execution_mode: str = EXECUTION_ALERT_ONLY
    primary_exchange: str = "lighter"

    # 운영 모드
    trading_mode: str = "swing"

    # 시그널 설정 (v2: MultiTF)
    signal_config: Optional[MultiTFConfig] = None
    # 시그널 설정 (v1: 단일 Z-score, 하위 호환)
    legacy_signal_config: Optional[SignalConfig] = None

    # 리스크 설정
    risk_config: Optional[RiskConfig] = None

    # Alert/Paper 가상 체결 비용 추정 (1bp = 0.01%)
    taker_fee_bps: float = 0.0
    slippage_bps: float = 1.0

    # 포지션 모니터링 간격 (초)
    position_check_interval: int = 10


class BotEngine:
    """
    페어 트레이딩 봇 메인 엔진 (v2 — 이벤트 드리븐).

    구조:
    - PriceHub가 모든 거래소 WS에 연결 → 틱 수신
    - 틱 수신 시 on_tick 콜백에서 시그널 평가
    - 시그널 발생 시 즉시 진입/청산 실행
    - 별도 태스크로 포지션 PNL 모니터링
    """

    def __init__(
        self,
        exchanges: Dict[str, BaseExchange],
        config: Optional[BotConfig] = None,
    ):
        self.exchanges = exchanges
        self.config = config or BotConfig()

        # 데이터 엔진
        self.price_buffer = PriceBuffer()
        self.price_hub = PriceHub(exchanges, self.price_buffer)

        # 시그널 엔진 (v2)
        signal_cfg = self.config.signal_config or MultiTFConfig.from_mode(self.config.trading_mode)
        self.signal_engine = MultiTimeframeSignalEngine(signal_cfg)

        # 거래 엔진
        self.position_manager = PositionManager()
        self.risk_manager = RiskManager(self.config.risk_config or RiskConfig())
        self.execution_mode = self._normalize_execution_mode(
            self.config.execution_mode,
            self.config.paper_trading,
        )
        self.telegram = TelegramNotifier.from_env()

        if self._uses_virtual_positions:
            # Repeated averaging/reduction alerts are not useful before live orders.
            self.risk_manager.config.averaging_enabled = False
            self.risk_manager.config.size_reduction_enabled = False

        # 거래 기록기 (DB persistence)
        self.trade_recorder: Optional[TradeRecorder] = None
        self._trade_db_ids: Dict[str, int] = {}  # trade_id → DB id 매핑

        # 상태
        self._running = False
        self._tick_count = 0
        self._signal_count = 0
        self._last_tick_time: float = 0
        self._errors: list[str] = []

        # 첫 번째 활성 거래소 (주문 실행용)
        self._primary_exchange: Optional[BaseExchange] = None

    def set_session_factory(self, session_factory) -> None:
        """DB 세션 팩토리를 설정하여 거래 기록을 활성화합니다."""
        self.trade_recorder = TradeRecorder(session_factory)
        logger.info("Trade recorder initialized — trades will be persisted to DB")

    @staticmethod
    def _normalize_execution_mode(mode: str, paper_trading: bool) -> str:
        normalized = (mode or "").lower().strip()
        if normalized in {EXECUTION_ALERT_ONLY, EXECUTION_PAPER, EXECUTION_LIVE}:
            return normalized
        return EXECUTION_PAPER if paper_trading else EXECUTION_LIVE

    @property
    def _uses_virtual_positions(self) -> bool:
        return self.execution_mode in {EXECUTION_ALERT_ONLY, EXECUTION_PAPER}

    @property
    def is_running(self) -> bool:
        return self._running

    # ── 메인 실행 ───────────────────────────────────────────

    async def start(self) -> None:
        """봇을 시작합니다. PriceHub + 포지션 모니터링을 비동기 병렬 실행."""
        logger.info("Bot engine starting... mode=%s exchanges=%s",
                     self.config.trading_mode, list(self.exchanges.keys()))
        logger.info("Config: size=$%.0f, leverage=%dx, execution=%s, telegram=%s",
                     self.config.position_size_usd, self.config.leverage,
                     self.execution_mode, bool(self.telegram))

        # 첫 번째 활성 거래소를 기본 주문 실행 거래소로 설정
        self._primary_exchange = (
            self.exchanges.get(self.config.primary_exchange)
            or next(iter(self.exchanges.values()), None)
        )
        await self._validate_live_execution()
        self._running = True
        if self.telegram:
            await self.telegram.status(
                f"Bot started: mode={self.config.trading_mode}, "
                f"execution={self.execution_mode}, primary={self.config.primary_exchange}"
            )

        # 틱 이벤트 리스너 등록
        self.price_hub.add_listener(self._on_tick)

        # 과거 캔들 로드 (warm-up) — 재시작 시 대기 시간 제거
        warmup_ok = await warmup(self.exchanges, self.price_buffer, self.signal_engine)
        if warmup_ok:
            logger.info("Warmup succeeded — signal engine ready immediately")
        else:
            logger.warning("Warmup failed — will wait for live data to accumulate")

        # 텔레그램 봇 시작 알림
        if self.telegram:
            await self.telegram.notify_bot_started(
                self.config.trading_mode, list(self.exchanges.keys()),
                self.config.position_size_usd, self.config.leverage,
            )

        # 병렬 태스크 실행
        try:
            await asyncio.gather(
                self.price_hub.start(),                    # WS 연결 유지
                self._position_monitor_loop(),             # 포지션 PNL 감시
                self._status_log_loop(),                   # 주기적 상태 로그
            )
        except asyncio.CancelledError:
            logger.info("Bot engine tasks cancelled")
        finally:
            await self.stop()

    async def stop(self) -> None:
        """봇을 정지합니다."""
        if not self._running:
            return
        self._running = False
        logger.info("Stopping bot engine...")
        await self.price_hub.stop()

        # 텔레그램 봇 정지 알림
        if self.telegram:
            await self.telegram.notify_bot_stopped()
            await self.telegram.close()

        # 거래소 세션 정리
        for exchange in self.exchanges.values():
            if hasattr(exchange, 'close'):
                await exchange.close()

        logger.info("Bot engine stopped.")

    async def _validate_live_execution(self) -> None:
        if self.execution_mode != EXECUTION_LIVE:
            return
        if self._primary_exchange is None:
            raise RuntimeError("Live execution requested but no primary exchange is configured")
        live_ready = getattr(self._primary_exchange, "live_trading_ready", True)
        if live_ready is False:
            message = (
                f"{self._primary_exchange.name} is not ready for live trading. "
                "Use alert_only/paper first or configure the exchange live flags."
            )
            if self.telegram:
                await self.telegram.error("Live trading blocked", message)
            raise RuntimeError(message)

    # ── 틱 이벤트 핸들러 ─────────────────────────────────────

    async def _on_tick(
        self,
        exchange: str,
        symbol: str,
        price: float,
        timestamp_ms: int,
    ) -> None:
        """
        PriceHub에서 틱 수신 시 호출됩니다.

        1. 시그널 엔진 평가
        2. 진입/청산 결정
        """
        self._tick_count += 1
        self._last_tick_time = time.time()

        try:
            # 시그널 평가
            signal = self.signal_engine.evaluate(self.price_buffer)

            # 포지션 없으면 → 진입 체크
            if not self.position_manager.has_open_position:
                if signal.should_enter and self.risk_manager.can_open_trade(0):
                    await self._handle_entry(signal)
            else:
                # 포지션 있으면 → Z-score 수렴 청산 체크
                if signal.should_exit_zscore:
                    for trade_id, trade in list(self.position_manager.open_trades.items()):
                        decision = self.risk_manager.evaluate(trade, zscore_reverted=True)
                        if (
                            decision.action == RiskAction.EXIT
                            and decision.reason == ExitReason.ZSCORE_REVERT
                        ):
                            await self._handle_exit(trade_id, decision.reason, decision.message)

        except Exception as e:
            error_msg = f"Tick handler error: {e}"
            logger.error(error_msg, exc_info=True)
            self._errors.append(error_msg)
            if len(self._errors) > 100:
                self._errors = self._errors[-50:]

    # ── 포지션 모니터링 루프 ──────────────────────────────────

    async def _position_monitor_loop(self) -> None:
        """포지션 PNL을 주기적으로 체크하여 TP/SL/Trailing/Timeout 처리."""
        while self._running:
            try:
                if self.position_manager.has_open_position:
                    if self._uses_virtual_positions:
                        btc_price = self.price_buffer.btc.last_price
                        eth_price = self.price_buffer.eth.last_price
                        if btc_price is None or eth_price is None:
                            await asyncio.sleep(self.config.position_check_interval)
                            continue
                        self.position_manager.update_virtual_positions(
                            VIRTUAL_EXCHANGE_NAME,
                            btc_price,
                            eth_price,
                        )
                    elif self._primary_exchange:
                        await self.position_manager.update_positions(self._primary_exchange)
                    else:
                        await asyncio.sleep(self.config.position_check_interval)
                        continue

                    for trade_id, trade in list(self.position_manager.open_trades.items()):
                        signal = self.signal_engine.evaluate(self.price_buffer)
                        decision = self.risk_manager.evaluate(
                            trade,
                            zscore_reverted=signal.should_exit_zscore,
                        )

                        if decision.action == RiskAction.EXIT:
                            await self._handle_exit(trade_id, decision.reason, decision.message)
                        elif decision.action == RiskAction.AVERAGING_DOWN:
                            await self._handle_averaging(trade_id, decision.message)
                        elif decision.action == RiskAction.SIZE_REDUCTION:
                            await self._handle_size_reduction(trade_id, decision.message)

            except Exception as e:
                logger.error("Position monitor error: %s", e)

            await asyncio.sleep(self.config.position_check_interval)

    # ── 주기적 상태 로그 ─────────────────────────────────────

    async def _status_log_loop(self) -> None:
        """60초마다 상태를 로깅합니다."""
        while self._running:
            await asyncio.sleep(60)

            if not self.price_buffer.has_data:
                logger.info("Waiting for price data... ticks=%d", self._tick_count)
                continue

            status = self.signal_engine.get_status()
            btc_price = self.price_buffer.btc.last_price or 0
            eth_price = self.price_buffer.eth.last_price or 0
            positions = len(self.position_manager.open_trades)

            logger.info(
                "Status | BTC=$%.2f ETH=$%.2f | mode=%s Z=%.2f trend=%s | "
                "ticks=%d signals=%d positions=%d | data=%d/%d",
                btc_price, eth_price,
                status["mode"], status["zscore_5m"], status["trend_1h"],
                self._tick_count, self._signal_count, positions,
                status["spread_5m_history_len"], status["window"],
            )

    # ── 진입 처리 ───────────────────────────────────────────

    async def _handle_entry(self, signal: Signal) -> None:
        """시그널에 따라 페어 포지션을 엽니다."""
        self._signal_count += 1

        direction = (
            PairDirection.LONG_BTC_SHORT_ETH
            if signal.direction == SignalDirection.LONG_BTC_SHORT_ETH
            else PairDirection.SHORT_BTC_LONG_ETH
        )

        logger.info(
            "ENTRY SIGNAL #%d: %s | Z5m=%.2f div=%.2f%% prob=%.1f%% trend=%s",
            self._signal_count, direction.value,
            signal.zscore_5m, signal.divergence_pct,
            signal.probability_pct, signal.trend.value,
        )

        btc_price = self.price_buffer.btc.last_price
        eth_price = self.price_buffer.eth.last_price
        if btc_price is None or eth_price is None:
            logger.error("Cannot enter without BTC/ETH prices")
            return

        if self.telegram:
            await self.telegram.entry_signal(
                signal=signal,
                direction=direction.value,
                execution_mode=self.execution_mode,
                size_usd=self.config.position_size_usd,
                btc_price=btc_price,
                eth_price=eth_price,
            )

        if self._uses_virtual_positions:
            trade = self.position_manager.open_virtual_pair(
                exchange_name=VIRTUAL_EXCHANGE_NAME,
                direction=direction,
                size_usd=self.config.position_size_usd,
                btc_price=btc_price,
                eth_price=eth_price,
                zscore=signal.zscore_5m,
                spread_pct=signal.divergence_pct,
                taker_fee_bps=self.config.taker_fee_bps,
                slippage_bps=self.config.slippage_bps,
            )
            if trade:
                if self.trade_recorder:
                    db_id = await self.trade_recorder.record_open(
                        trade,
                        signal_mode=self.config.trading_mode,
                    )
                    if db_id:
                        self._trade_db_ids[trade.trade_id] = db_id
                if self.telegram:
                    await self.telegram.trade_opened(trade, self.execution_mode)
            return

        if self._primary_exchange is None:
            logger.error("No exchange available for order execution")
            if self.telegram:
                await self.telegram.error("No exchange available for live order execution")
            return

        trade = await self.position_manager.open_pair(
            exchange=self._primary_exchange,
            direction=direction,
            size_usd=self.config.position_size_usd,
            leverage=self.config.leverage,
            zscore=signal.zscore_5m,
            spread_pct=signal.divergence_pct,
        )

        if trade:
            logger.info("Trade opened: %s | PNL tracking started", trade.trade_id)
            # DB에 기록
            if self.trade_recorder:
                db_id = await self.trade_recorder.record_open(trade, signal_mode=self.config.trading_mode)
                if db_id:
                    self._trade_db_ids[trade.trade_id] = db_id
            if self.telegram:
                await self.telegram.notify_entry(
                    direction=direction.value,
                    exchange=self._primary_exchange.name if self._primary_exchange else "unknown",
                    size_usd=self.config.position_size_usd,
                    leverage=self.config.leverage,
                    zscore=signal.zscore_5m,
                    divergence=signal.divergence_pct,
                    probability=signal.probability_pct,
                    trend=signal.trend.value,
                )
        else:
            logger.error("Failed to open pair trade")
            if self.telegram:
                await self.telegram.error("Failed to open pair trade", direction.value)

    # ── 청산 처리 ───────────────────────────────────────────

    async def _handle_exit(self, trade_id: str, reason: ExitReason, message: str) -> None:
        """포지션을 청산합니다."""
        logger.info("EXIT: %s | reason=%s | %s", trade_id, reason.value, message)

        if self._uses_virtual_positions:
            trade = self.position_manager.close_virtual_pair(trade_id, reason.value)
            if trade:
                self.risk_manager.on_trade_closed(trade_id, trade.net_pnl_usd)
                db_id = self._trade_db_ids.pop(trade_id, None)
                if db_id and self.trade_recorder:
                    await self.trade_recorder.record_close(
                        db_id,
                        trade,
                        reason.value,
                        open_positions=len(self.position_manager.open_trades),
                    )
                elif self.trade_recorder:
                    await self.trade_recorder.record_full(
                        trade,
                        exit_reason=reason.value,
                        signal_mode=self.config.trading_mode,
                    )
                if self.telegram:
                    await self.telegram.trade_closed(trade, reason.value, message)
            return

        if self._primary_exchange is None:
            return

        trade = await self.position_manager.close_pair(trade_id, self._primary_exchange, reason.value)
        if trade:
            self.risk_manager.on_trade_closed(trade_id, trade.net_pnl_usd)
            logger.info(
                "Trade closed: %s | PNL=$%.2f (%.2f%%) | reason=%s",
                trade_id, trade.net_pnl_usd, trade.pnl_pct, reason.value,
            )
            # DB에 청산 기록
            db_id = self._trade_db_ids.pop(trade_id, None)
            if db_id and self.trade_recorder:
                await self.trade_recorder.record_close(
                    db_id,
                    trade,
                    reason.value,
                    open_positions=len(self.position_manager.open_trades),
                )
            if self.telegram:
                await self.telegram.notify_exit(
                    trade_id=trade_id,
                    reason=reason.value,
                    pnl_usd=trade.net_pnl_usd,
                    pnl_pct=trade.pnl_pct,
                    direction=trade.direction.value if hasattr(trade, 'direction') else "",
                )

    # ── 리스크 액션 처리 ─────────────────────────────────────

    async def _handle_averaging(self, trade_id: str, message: str) -> None:
        logger.info("AVERAGING: %s | %s", trade_id, message)
        if self._uses_virtual_positions:
            logger.info("[%s] Averaging ignored for virtual trade: %s", self.execution_mode.upper(), trade_id)
            return
        if self._primary_exchange is None:
            return
        success = await self.position_manager.averaging_down(
            trade_id, self._primary_exchange, self.config.risk_config.averaging_multiplier
            if self.config.risk_config else 0.5,
        )
        if success:
            logger.info("Averaging completed: %s", trade_id)
            if self.telegram:
                await self.telegram.notify_averaging(trade_id, message)

    async def _handle_size_reduction(self, trade_id: str, message: str) -> None:
        logger.info("SIZE REDUCTION: %s | %s", trade_id, message)
        if self._uses_virtual_positions:
            logger.info("[%s] Size reduction ignored for virtual trade: %s", self.execution_mode.upper(), trade_id)
            return
        if self._primary_exchange is None:
            return
        success = await self.position_manager.size_reduction(
            trade_id, self._primary_exchange, self.config.risk_config.size_reduction_ratio
            if self.config.risk_config else 0.5,
        )
        if success:
            logger.info("Size reduction completed: %s", trade_id)
            if self.telegram:
                await self.telegram.notify_size_reduction(trade_id, message)

    # ── 상태 조회 (대시보드용) ───────────────────────────────

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "mode": self.config.trading_mode,
            "exchanges": list(self.exchanges.keys()),
            "tick_count": self._tick_count,
            "signal_count": self._signal_count,
            "last_tick": self._last_tick_time,
            "paper_trading": self.config.paper_trading,
            "execution_mode": self.execution_mode,
            "primary_exchange": self._primary_exchange.name if self._primary_exchange else None,
            "telegram_enabled": bool(self.telegram and self.telegram.enabled),
            "costs": {
                "taker_fee_bps": self.config.taker_fee_bps,
                "slippage_bps": self.config.slippage_bps,
            },
            "signal": self.signal_engine.get_status(),
            "positions": self.position_manager.get_summary(),
            "risk": self.risk_manager.get_status(),
            "price_hub": self.price_hub.get_status(),
            "recent_errors": self._errors[-5:],
        }


# ── 스탠드얼론 실행 ──────────────────────────────────────────


async def run_bot():
    """환경변수에서 설정을 읽어 봇을 실행합니다."""
    from dotenv import load_dotenv
    load_dotenv()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # 거래소 초기화
    exchanges: Dict[str, BaseExchange] = {}

    # Backpack
    bp_key = os.getenv("BACKPACK_API_KEY")
    bp_secret = os.getenv("BACKPACK_SECRET_KEY")
    if bp_key and bp_secret:
        exchanges["backpack"] = BackpackExchange(api_key=bp_key, secret_key=bp_secret)
        logger.info("Backpack exchange initialized")

    # Pacifica (API 문서 확인 후 활성화)
    pac_key = os.getenv("PACIFICA_API_KEY")
    pac_secret = os.getenv("PACIFICA_SECRET_KEY")
    if pac_key and pac_secret:
        from backend.bot.exchanges.pacifica import PacificaExchange
        exchanges["pacifica"] = PacificaExchange(api_key=pac_key, secret_key=pac_secret)
        logger.info("Pacifica exchange initialized")

    # Extended (API 문서 확인 후 활성화)
    ext_key = os.getenv("EXTENDED_API_KEY")
    ext_secret = os.getenv("EXTENDED_SECRET_KEY")
    if ext_key and ext_secret:
        from backend.bot.exchanges.extended import ExtendedExchange
        exchanges["extended"] = ExtendedExchange(api_key=ext_key, secret_key=ext_secret)
        logger.info("Extended exchange initialized")

    # Lighter (API 문서 확인 후 활성화)
    lt_key = os.getenv("LIGHTER_API_KEY")
    lt_secret = os.getenv("LIGHTER_SECRET_KEY")
    lt_private = os.getenv("LIGHTER_PRIVATE_KEY")
    lt_account = os.getenv("LIGHTER_ACCOUNT_INDEX")
    lt_key_index = os.getenv("LIGHTER_API_KEY_INDEX")
    if lt_key or lt_secret or lt_private:
        from backend.bot.exchanges.lighter import LighterExchange
        exchanges["lighter"] = LighterExchange(
            api_key=lt_key or "",
            secret_key=lt_secret or "",
            private_key=lt_private,
            account_index=int(lt_account) if lt_account else None,
            api_key_index=int(lt_key_index) if lt_key_index else None,
            btc_market_id=int(os.getenv("LIGHTER_BTC_MARKET_ID", "1")),
            eth_market_id=int(os.getenv("LIGHTER_ETH_MARKET_ID", "0")),
        )
        logger.info("Lighter exchange initialized")

    if not exchanges:
        logger.error("No exchanges configured. Set API keys in .env")
        return

    # 설정
    trading_mode = os.getenv("TRADING_MODE", "swing")
    paper_trading = os.getenv("PAPER_TRADING", "true").lower() == "true"
    execution_mode = os.getenv(
        "EXECUTION_MODE",
        EXECUTION_PAPER if paper_trading else EXECUTION_LIVE,
    )
    signal_cfg = MultiTFConfig.from_mode(trading_mode)
    signal_cfg.zscore_revert_threshold = float(
        os.getenv("ZSCORE_REVERT_THRESHOLD", str(signal_cfg.zscore_revert_threshold))
    )
    config = BotConfig(
        position_size_usd=float(os.getenv("POSITION_SIZE_USD", "500")),
        leverage=int(os.getenv("LEVERAGE", "3")),
        paper_trading=paper_trading,
        execution_mode=execution_mode,
        primary_exchange=os.getenv("PRIMARY_EXCHANGE", "lighter"),
        trading_mode=trading_mode,
        signal_config=signal_cfg,
        risk_config=RiskConfig(
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.8")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-3.0")),
            max_hold_hours=float(os.getenv("MAX_HOLD_HOURS", "12")),
            max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
            daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT", "-200")),
            min_hold_minutes=float(os.getenv("MIN_HOLD_MINUTES", "120")),
            zscore_exit_min_pnl_pct=float(os.getenv("ZSCORE_EXIT_MIN_PNL_PCT", "0.0")),
        ),
    )

    bot = BotEngine(exchanges=exchanges, config=config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        await bot.stop()


if __name__ == "__main__":
    asyncio.run(run_bot())
