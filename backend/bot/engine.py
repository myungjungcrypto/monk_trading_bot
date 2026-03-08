"""
Bot Engine — BTC/ETH 페어 트레이딩 메인 루프.

1분 주기로 가격 데이터를 수집하고, 시그널을 계산하며,
진입/청산/리스크 관리를 수행합니다.
"""

import asyncio
import logging
import os
import time
from typing import Optional

from backend.bot.exchanges.backpack import BackpackExchange
from backend.bot.exchanges.base import BaseExchange
from backend.bot.position_manager import PairDirection, PositionManager
from backend.bot.risk_manager import (
    ExitReason,
    RiskAction,
    RiskConfig,
    RiskManager,
)
from backend.bot.signal import SignalConfig, SignalDirection, SignalEngine

logger = logging.getLogger(__name__)


class BotConfig:
    """봇 전체 설정."""

    def __init__(
        self,
        # 거래소 설정
        position_size_usd: float = 500.0,
        leverage: int = 3,
        # 봇 동작
        loop_interval_sec: int = 60,
        paper_trading: bool = False,
        # 시그널 설정
        signal_config: Optional[SignalConfig] = None,
        # 리스크 설정
        risk_config: Optional[RiskConfig] = None,
    ):
        self.position_size_usd = position_size_usd
        self.leverage = leverage
        self.loop_interval_sec = loop_interval_sec
        self.paper_trading = paper_trading
        self.signal_config = signal_config or SignalConfig()
        self.risk_config = risk_config or RiskConfig()


class BotEngine:
    """
    페어 트레이딩 봇 메인 엔진.

    메인 루프:
    1. 가격 데이터 수집 (1분 캔들)
    2. 수익률 계산
    3. Z-score 시그널 판단
    4. 진입 조건 → 페어 오픈
    5. 청산 조건 → 페어 클로즈
    6. 리스크 관리 (Averaging / Size Reduction)
    """

    def __init__(self, exchange: BaseExchange, config: Optional[BotConfig] = None):
        self.exchange = exchange
        self.config = config or BotConfig()
        self.signal_engine = SignalEngine(self.config.signal_config)
        self.position_manager = PositionManager()
        self.risk_manager = RiskManager(self.config.risk_config)

        self._running = False
        self._btc_prices: list[float] = []
        self._eth_prices: list[float] = []

        # 상태
        self._last_tick_time: float = 0
        self._tick_count: int = 0
        self._errors: list[str] = []

    @property
    def is_running(self) -> bool:
        return self._running

    # ── 메인 루프 ─────────────────────────────────────────────

    async def start(self) -> None:
        """봇 메인 루프를 시작합니다."""
        logger.info("Bot engine starting... exchange=%s", self.exchange.name)
        logger.info(
            "Config: size=$%.0f, leverage=%dx, interval=%ds, paper=%s",
            self.config.position_size_usd,
            self.config.leverage,
            self.config.loop_interval_sec,
            self.config.paper_trading,
        )

        self._running = True

        # 초기 가격 히스토리 로드
        await self._load_initial_prices()

        while self._running:
            try:
                await self._tick()
            except Exception as e:
                error_msg = f"Tick error: {e}"
                logger.error(error_msg, exc_info=True)
                self._errors.append(error_msg)
                if len(self._errors) > 100:
                    self._errors = self._errors[-50:]

            await asyncio.sleep(self.config.loop_interval_sec)

        logger.info("Bot engine stopped.")

    async def stop(self) -> None:
        """봇을 정지합니다."""
        logger.info("Stopping bot engine...")
        self._running = False

    # ── 단일 틱 ───────────────────────────────────────────────

    async def _tick(self) -> None:
        """1회 주기 실행."""
        self._tick_count += 1
        self._last_tick_time = time.time()

        # 1. 가격 수집
        btc_price, eth_price = await self._fetch_prices()
        if btc_price is None or eth_price is None:
            return

        self._btc_prices.append(btc_price)
        self._eth_prices.append(eth_price)

        # 메모리 관리: 최대 500개 유지
        if len(self._btc_prices) > 500:
            self._btc_prices = self._btc_prices[-300:]
            self._eth_prices = self._eth_prices[-300:]

        lookback = self.config.signal_config.lookback_minutes

        # 2. 수익률 계산
        btc_ret = SignalEngine.calculate_return(self._btc_prices, lookback)
        eth_ret = SignalEngine.calculate_return(self._eth_prices, lookback)

        # 3. 시그널 판단
        signal = self.signal_engine.check_entry(btc_ret, eth_ret)

        logger.info(
            "Tick #%d | BTC=$%.2f ETH=$%.2f | spread=%.2f%% Z=%.2f prob=%.1f%% | data=%d/%d",
            self._tick_count, btc_price, eth_price,
            signal.spread_pct, signal.zscore, signal.probability_pct,
            self.signal_engine.spread_history_len, self.signal_engine.window,
        )

        # 4. 포지션이 없으면 → 진입 체크
        if not self.position_manager.has_open_position:
            if signal.should_enter and self.risk_manager.can_open_trade(0):
                await self._handle_entry(signal)
            return

        # 5. 포지션이 있으면 → 업데이트 & 청산/리스크 체크
        await self.position_manager.update_positions(self.exchange)

        for trade_id, trade in list(self.position_manager.open_trades.items()):
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

    # ── 가격 수집 ─────────────────────────────────────────────

    async def _fetch_prices(self) -> tuple[Optional[float], Optional[float]]:
        """BTC, ETH 현재가를 조회합니다."""
        try:
            btc_symbol = self.exchange.perp_symbol("BTC")
            eth_symbol = self.exchange.perp_symbol("ETH")

            tickers = await self.exchange.get_tickers([btc_symbol, eth_symbol])
            btc_ticker = tickers.get(btc_symbol)
            eth_ticker = tickers.get(eth_symbol)

            if btc_ticker is None or eth_ticker is None:
                logger.warning("Missing ticker data: BTC=%s ETH=%s (keys: %s)", btc_ticker, eth_ticker, list(tickers.keys()))
                return None, None

            return btc_ticker.last_price, eth_ticker.last_price
        except Exception as e:
            logger.error("Price fetch failed: %s", e)
            return None, None

    async def _load_initial_prices(self) -> None:
        """초기 가격 히스토리를 K-line에서 로드합니다."""
        try:
            btc_symbol = self.exchange.perp_symbol("BTC")
            eth_symbol = self.exchange.perp_symbol("ETH")

            # lookback + sigma_window 만큼의 히스토리 필요
            need = self.config.signal_config.sigma_window + self.config.signal_config.lookback_minutes + 10
            limit = min(need, 200)

            btc_klines = await self.exchange.get_klines(btc_symbol, "1m", limit)
            eth_klines = await self.exchange.get_klines(eth_symbol, "1m", limit)

            self._btc_prices = [k["close"] for k in btc_klines]
            self._eth_prices = [k["close"] for k in eth_klines]

            # 시그널 엔진에 히스토리 채우기
            lookback = self.config.signal_config.lookback_minutes
            for i in range(lookback, len(self._btc_prices)):
                btc_ret = SignalEngine.calculate_return(self._btc_prices[:i + 1], lookback)
                eth_ret = SignalEngine.calculate_return(self._eth_prices[:i + 1], lookback)
                spread = SignalEngine.calculate_spread(btc_ret, eth_ret)
                self.signal_engine.add_spread(spread)

            logger.info(
                "Initial prices loaded: BTC=%d candles, ETH=%d candles, spreads=%d",
                len(self._btc_prices), len(self._eth_prices),
                self.signal_engine.spread_history_len,
            )
        except Exception as e:
            logger.warning("Failed to load initial prices (will warm up): %s", e)

    # ── 진입 처리 ─────────────────────────────────────────────

    async def _handle_entry(self, signal) -> None:
        """시그널에 따라 페어 포지션을 엽니다."""
        direction = (
            PairDirection.LONG_BTC_SHORT_ETH
            if signal.direction == SignalDirection.LONG_BTC_SHORT_ETH
            else PairDirection.SHORT_BTC_LONG_ETH
        )

        logger.info(
            "ENTRY SIGNAL: %s | Z=%.2f spread=%.2f%% prob=%.1f%%",
            direction.value, signal.zscore, signal.spread_pct, signal.probability_pct,
        )

        if self.config.paper_trading:
            logger.info("[PAPER] Would open pair: %s $%.0f", direction.value, self.config.position_size_usd)
            return

        trade = await self.position_manager.open_pair(
            exchange=self.exchange,
            direction=direction,
            size_usd=self.config.position_size_usd,
            leverage=self.config.leverage,
            zscore=signal.zscore,
            spread_pct=signal.spread_pct,
        )

        if trade:
            logger.info("Trade opened: %s | PNL tracking started", trade.trade_id)
        else:
            logger.error("Failed to open pair trade")

    # ── 청산 처리 ─────────────────────────────────────────────

    async def _handle_exit(self, trade_id: str, reason: ExitReason, message: str) -> None:
        """포지션을 청산합니다."""
        logger.info("EXIT: %s | reason=%s | %s", trade_id, reason.value, message)

        if self.config.paper_trading:
            trade = self.position_manager.open_trades.get(trade_id)
            if trade:
                logger.info("[PAPER] Would close pair: PNL=$%.2f (%.2f%%)", trade.net_pnl_usd, trade.pnl_pct)
            return

        trade = await self.position_manager.close_pair(trade_id, self.exchange, reason.value)
        if trade:
            self.risk_manager.on_trade_closed(trade_id, trade.net_pnl_usd)
            logger.info(
                "Trade closed: %s | PNL=$%.2f (%.2f%%) | reason=%s",
                trade_id, trade.net_pnl_usd, trade.pnl_pct, reason.value,
            )

    # ── 리스크 액션 처리 ──────────────────────────────────────

    async def _handle_averaging(self, trade_id: str, message: str) -> None:
        """Averaging down을 실행합니다."""
        logger.info("AVERAGING: %s | %s", trade_id, message)

        if self.config.paper_trading:
            logger.info("[PAPER] Would average down: %s", trade_id)
            return

        success = await self.position_manager.averaging_down(
            trade_id, self.exchange, self.config.risk_config.averaging_multiplier,
        )
        if success:
            logger.info("Averaging completed: %s", trade_id)

    async def _handle_size_reduction(self, trade_id: str, message: str) -> None:
        """Size reduction을 실행합니다."""
        logger.info("SIZE REDUCTION: %s | %s", trade_id, message)

        if self.config.paper_trading:
            logger.info("[PAPER] Would reduce size: %s", trade_id)
            return

        success = await self.position_manager.size_reduction(
            trade_id, self.exchange, self.config.risk_config.size_reduction_ratio,
        )
        if success:
            logger.info("Size reduction completed: %s", trade_id)

    # ── 상태 조회 (대시보드용) ────────────────────────────────

    def get_status(self) -> dict:
        """봇 전체 상태 요약."""
        return {
            "running": self._running,
            "exchange": self.exchange.name,
            "tick_count": self._tick_count,
            "last_tick": self._last_tick_time,
            "paper_trading": self.config.paper_trading,
            "signal": self.signal_engine.get_status(),
            "positions": self.position_manager.get_summary(),
            "risk": self.risk_manager.get_status(),
            "price_history_len": len(self._btc_prices),
            "recent_errors": self._errors[-5:],
        }


# ── 스탠드얼론 실행 ──────────────────────────────────────────


async def run_bot():
    """환경변수에서 설정을 읽어 봇을 실행합니다."""
    from dotenv import load_dotenv
    load_dotenv()

    # 로깅 설정
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    api_key = os.getenv("BACKPACK_API_KEY")
    secret_key = os.getenv("BACKPACK_SECRET_KEY")

    if not api_key or not secret_key:
        logger.error("BACKPACK_API_KEY and BACKPACK_SECRET_KEY must be set in .env")
        return

    exchange = BackpackExchange(api_key=api_key, secret_key=secret_key)

    # 설정 (환경변수 또는 기본값)
    config = BotConfig(
        position_size_usd=float(os.getenv("POSITION_SIZE_USD", "500")),
        leverage=int(os.getenv("LEVERAGE", "3")),
        loop_interval_sec=int(os.getenv("LOOP_INTERVAL_SEC", "60")),
        paper_trading=os.getenv("PAPER_TRADING", "true").lower() == "true",
        signal_config=SignalConfig(
            divergence_threshold_pct=float(os.getenv("DIVERGENCE_THRESHOLD", "2.5")),
            lookback_minutes=int(os.getenv("LOOKBACK_MINUTES", "15")),
            confirmation_candles=int(os.getenv("CONFIRMATION_CANDLES", "2")),
            sigma_window=int(os.getenv("SIGMA_WINDOW", "100")),
            entry_zscore=float(os.getenv("ENTRY_ZSCORE", "2.0")),
            max_zscore=float(os.getenv("MAX_ZSCORE", "3.5")),
            probability_threshold_pct=float(os.getenv("PROBABILITY_THRESHOLD", "95")),
        ),
        risk_config=RiskConfig(
            take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.8")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-3.0")),
            max_hold_hours=float(os.getenv("MAX_HOLD_HOURS", "24")),
            max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
            daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT", "-200")),
        ),
    )

    bot = BotEngine(exchange=exchange, config=config)

    try:
        await bot.start()
    except KeyboardInterrupt:
        logger.info("Keyboard interrupt received")
    finally:
        await bot.stop()
        await exchange.close()


if __name__ == "__main__":
    asyncio.run(run_bot())
