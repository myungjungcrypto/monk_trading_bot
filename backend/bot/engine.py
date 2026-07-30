"""
Bot Engine — BTC/ETH 페어 트레이딩 메인 이벤트 루프.

v2: REST 폴링 제거 → WebSocket 이벤트 드리븐 구조.
    PriceHub에서 틱 수신 → MultiTF 시그널 평가 → 진입/청산 실행.
"""

import asyncio
import json
import logging
import os
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, List, Optional

from backend.bot.exchanges.backpack import BackpackExchange
from backend.bot.exchanges.base import BaseExchange, PositionSide
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
from backend.bot.variational.browser_requests import (
    VariationalBrowserRequestBatch,
    VariationalBrowserRequestBridge,
    completions_all_clicked,
    completions_browser_unavailable,
    completions_close_resolved,
    completions_external_closed,
    completions_wallet_unavailable,
    format_completions,
    request_quantity,
)
from backend.bot.variational.api_executor import VariationalApiExecutor
from backend.bot.warmup import warmup

logger = logging.getLogger(__name__)

EXECUTION_ALERT_ONLY = "alert_only"
EXECUTION_PAPER = "paper"
EXECUTION_LIVE = "live"
EXECUTION_VARIATIONAL_BROWSER = "variational_browser"
EXECUTION_VARIATIONAL_API = "variational_api"
VIRTUAL_EXCHANGE_NAME = "virtual"
VARIATIONAL_BROWSER_EXCHANGE_NAME = "variational_browser"
VARIATIONAL_API_EXCHANGE_NAME = "variational_api"


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
        # Variational execution backend. The API executor is a drop-in for the
        # browser bridge (same create/close/wait surface) but places orders via
        # the direct JSON API instead of clicking Chrome — both legs fire
        # concurrently with no approval step.
        self.variational_bridge = None
        if self.execution_mode == EXECUTION_VARIATIONAL_BROWSER:
            self.variational_bridge = VariationalBrowserRequestBridge()
        elif self.execution_mode == EXECUTION_VARIATIONAL_API:
            self.variational_bridge = VariationalApiExecutor(notifier=self.telegram)

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
        self._config_loader: Optional[Callable[[], Awaitable[BotConfig]]] = None
        self._config_reload_interval = float(os.getenv("BOT_CONFIG_RELOAD_INTERVAL_SEC", "15"))
        self._config_fingerprint = self._runtime_config_fingerprint(self.config)
        self._ignored_reload_warning: Optional[str] = None
        self._closing_trade_ids: set[str] = set()
        self._exit_retry_after: Dict[str, float] = {}
        self._exit_retry_cooldown_sec = float(os.getenv("VARIATIONAL_BROWSER_CLOSE_RETRY_COOLDOWN_SEC", "120"))
        self._browser_unavailable_retry_cooldown_sec = float(os.getenv("VARIATIONAL_BROWSER_INFRA_RETRY_COOLDOWN_SEC", "900"))
        self._entry_retry_after: float = 0.0
        self._entry_retry_cooldown_sec = float(os.getenv("VARIATIONAL_BROWSER_ENTRY_RETRY_COOLDOWN_SEC", "120"))

        # 첫 번째 활성 거래소 (주문 실행용)
        self._primary_exchange: Optional[BaseExchange] = None

    def set_session_factory(self, session_factory) -> None:
        """DB 세션 팩토리를 설정하여 거래 기록을 활성화합니다."""
        self.trade_recorder = TradeRecorder(session_factory)
        logger.info("Trade recorder initialized — trades will be persisted to DB")

    def set_config_loader(self, loader: Callable[[], Awaitable[BotConfig]]) -> None:
        """실행 중 DB 설정을 다시 읽기 위한 비동기 로더를 설정합니다."""
        self._config_loader = loader

    @staticmethod
    def _normalize_execution_mode(mode: str, paper_trading: bool) -> str:
        normalized = (mode or "").lower().strip()
        if normalized in {
            EXECUTION_ALERT_ONLY, EXECUTION_PAPER, EXECUTION_LIVE,
            EXECUTION_VARIATIONAL_BROWSER, EXECUTION_VARIATIONAL_API,
        }:
            return normalized
        return EXECUTION_PAPER if paper_trading else EXECUTION_LIVE

    @property
    def _uses_virtual_positions(self) -> bool:
        return self.execution_mode in {
            EXECUTION_ALERT_ONLY, EXECUTION_PAPER,
            EXECUTION_VARIATIONAL_BROWSER, EXECUTION_VARIATIONAL_API,
        }

    @property
    def _virtual_exchange_name(self) -> str:
        if self.execution_mode == EXECUTION_VARIATIONAL_BROWSER:
            return VARIATIONAL_BROWSER_EXCHANGE_NAME
        if self.execution_mode == EXECUTION_VARIATIONAL_API:
            return VARIATIONAL_API_EXCHANGE_NAME
        return VIRTUAL_EXCHANGE_NAME

    @property
    def _variational_label(self) -> str:
        """Log/telegram prefix that matches how Variational orders are executed."""
        return (
            "Variational API"
            if self.execution_mode == EXECUTION_VARIATIONAL_API
            else "Variational Browser"
        )

    @property
    def is_running(self) -> bool:
        return self._running

    # ── 재시작 복구 ───────────────────────────────────────────

    async def _restore_open_trades(self) -> None:
        """DB/거래소에 남아 있는 열린 포지션을 메모리 상태로 복구합니다."""
        if self.trade_recorder is None:
            return

        exchange_name = self._virtual_exchange_name if self._uses_virtual_positions else (
            self._primary_exchange.name if self._primary_exchange else None
        )
        if exchange_name is None:
            return

        db_trades = await self.trade_recorder.fetch_open_trades(exchange=exchange_name)
        if not db_trades:
            return

        if self.execution_mode == EXECUTION_VARIATIONAL_BROWSER:
            db_trades = await self._reconcile_variational_browser_trades(db_trades)
            if not db_trades:
                return

        if self._uses_virtual_positions:
            restored = self._restore_virtual_trades(db_trades)
        else:
            restored = await self._restore_live_trades(db_trades)

        if restored and self.telegram:
            await self.telegram.status(f"Restored {restored} open trade(s) after restart")

    async def _reconcile_variational_browser_trades(self, db_trades: List[object]) -> List[object]:
        """Do not restore browser trades whose UI clicks were never confirmed."""
        if self.variational_bridge is None or self.trade_recorder is None:
            return db_trades

        window_sec = int(os.getenv("VARIATIONAL_BROWSER_RECONCILE_WINDOW_SEC", "600"))
        confirmed: List[object] = []
        for db_trade in db_trades:
            try:
                direction = PairDirection(db_trade.direction)
            except ValueError:
                confirmed.append(db_trade)
                continue

            statuses = self.variational_bridge.open_request_statuses_for_trade(
                direction=direction,
                opened_at=db_trade.opened_at,
                window_sec=window_sec,
            )
            if not statuses:
                confirmed.append(db_trade)
                continue

            if statuses.get("BTC") == "clicked" and statuses.get("ETH") == "clicked":
                confirmed.append(db_trade)
                continue

            detail = ", ".join(f"{symbol}={status}" for symbol, status in sorted(statuses.items()))
            logger.warning(
                "Closing unconfirmed Variational browser DB trade id=%s: %s",
                db_trade.id, detail,
            )
            await self.trade_recorder.mark_open_trade_unconfirmed(
                db_trade.id,
                "UNCONFIRMED_BROWSER_REQUEST",
            )
            if self.telegram:
                await self.telegram.status(
                    "\n".join([
                        "Cleared unconfirmed Variational browser position",
                        f"db_trade_id: {db_trade.id}",
                        f"statuses: {detail}",
                    ])
                )

        return confirmed

    def _restore_virtual_trades(self, db_trades: List[object]) -> int:
        restored = 0
        for db_trade in db_trades:
            trade = self._restore_trade_from_db(db_trade)
            if trade:
                self._trade_db_ids[trade.trade_id] = db_trade.id
                restored += 1
        return restored

    async def _restore_live_trades(self, db_trades: List[object]) -> int:
        if self._primary_exchange is None:
            return 0
        if len(db_trades) > 1:
            logger.warning(
                "Multiple open live DB trades found (%d); exchange position is aggregate, restoring oldest only",
                len(db_trades),
            )

        try:
            positions = await self._primary_exchange.get_positions()
        except Exception as e:
            logger.error("Failed to fetch live positions for restore: %s", e)
            if self.telegram:
                await self.telegram.error(f"Failed to restore live position: {e}")
            return 0

        btc_symbol = self._primary_exchange.perp_symbol("BTC")
        eth_symbol = self._primary_exchange.perp_symbol("ETH")
        btc_pos = next((p for p in positions if p.symbol == btc_symbol), None)
        eth_pos = next((p for p in positions if p.symbol == eth_symbol), None)
        if btc_pos is None or eth_pos is None:
            logger.warning("Open DB trade exists but live BTC/ETH positions were not found")
            if self.telegram:
                await self.telegram.error("Open DB trade exists, but live BTC/ETH positions were not found")
            return 0

        db_trade = db_trades[0]
        direction = PairDirection(db_trade.direction)
        expected_btc = PositionSide.LONG if direction == PairDirection.LONG_BTC_SHORT_ETH else PositionSide.SHORT
        expected_eth = PositionSide.SHORT if direction == PairDirection.LONG_BTC_SHORT_ETH else PositionSide.LONG
        if btc_pos.side != expected_btc or eth_pos.side != expected_eth:
            logger.warning(
                "Live position side mismatch on restore: db=%s btc=%s eth=%s",
                direction.value, btc_pos.side.value, eth_pos.side.value,
            )
            if self.telegram:
                await self.telegram.error("Live position side mismatch on restore; not attaching position")
            return 0

        trade = self._restore_trade_from_db(
            db_trade,
            btc_quantity=btc_pos.size,
            eth_quantity=eth_pos.size,
            btc_entry=btc_pos.entry_price or db_trade.btc_entry,
            eth_entry=eth_pos.entry_price or db_trade.eth_entry,
            btc_current=btc_pos.mark_price or btc_pos.entry_price,
            eth_current=eth_pos.mark_price or eth_pos.entry_price,
        )
        if not trade:
            return 0
        self._trade_db_ids[trade.trade_id] = db_trade.id
        await self.position_manager.update_positions(self._primary_exchange)
        return 1

    def _restore_trade_from_db(
        self,
        db_trade: object,
        btc_quantity: Optional[float] = None,
        eth_quantity: Optional[float] = None,
        btc_entry: Optional[float] = None,
        eth_entry: Optional[float] = None,
        btc_current: Optional[float] = None,
        eth_current: Optional[float] = None,
    ):
        try:
            direction = PairDirection(db_trade.direction)
        except ValueError:
            logger.warning("Cannot restore unknown trade direction: %s", db_trade.direction)
            return None

        size_usd_per_leg = float(db_trade.size_usd or self.config.position_size_usd * 2) / 2.0
        btc_entry_price = float(btc_entry if btc_entry is not None else (db_trade.btc_entry or 0.0))
        eth_entry_price = float(eth_entry if eth_entry is not None else (db_trade.eth_entry or 0.0))
        if btc_entry_price <= 0 or eth_entry_price <= 0:
            logger.warning("Cannot restore DB trade %s without valid entry prices", db_trade.id)
            return None

        return self.position_manager.restore_pair(
            trade_id=f"db_{db_trade.id}",
            exchange_name=db_trade.exchange,
            direction=direction,
            size_usd_per_leg=size_usd_per_leg,
            btc_entry=btc_entry_price,
            eth_entry=eth_entry_price,
            opened_at=self._datetime_to_timestamp(db_trade.opened_at),
            zscore=float(db_trade.zscore_entry or 0.0),
            spread_pct=float(db_trade.spread_entry or 0.0),
            total_fees_usd=float(db_trade.fees_usd or 0.0),
            btc_quantity=btc_quantity,
            eth_quantity=eth_quantity,
            btc_current=btc_current,
            eth_current=eth_current,
        )

    async def reconcile_external_close(
        self,
        db_trade_id: Optional[int] = None,
        reason: str = "EXTERNAL_MANUAL_CLOSE",
        pnl_usd: float = 0.0,
    ) -> Dict[str, Any]:
        """실제 거래소 포지션이 외부에서 닫힌 경우 가상/DB 상태만 닫습니다."""
        if self.trade_recorder is None:
            return {"closed": [], "message": "Trade recorder is not configured"}

        db_trades = await self.trade_recorder.fetch_open_trades(exchange=self._virtual_exchange_name)
        if db_trade_id is not None:
            db_trades = [trade for trade in db_trades if trade.id == db_trade_id]
        closed = []

        for db_trade in db_trades:
            trade_id = next(
                (tid for tid, mapped_db_id in self._trade_db_ids.items() if mapped_db_id == db_trade.id),
                None,
            )
            if trade_id:
                self.position_manager.mark_virtual_pair_externally_closed(trade_id, reason)
                self._trade_db_ids.pop(trade_id, None)
                self._closing_trade_ids.discard(trade_id)
                self._exit_retry_after.pop(trade_id, None)
                self.risk_manager.on_trade_closed(trade_id, float(pnl_usd or 0.0))

            ok = await self.trade_recorder.mark_open_trade_external_closed(
                db_trade.id,
                reason=reason,
                pnl_usd=float(pnl_usd or 0.0),
                open_positions=len(self.position_manager.open_trades),
            )
            if ok:
                closed.append({"db_trade_id": db_trade.id, "trade_id": trade_id, "reason": reason})

        if closed and self.telegram:
            await self.telegram.status(
                "\n".join([
                    "Reconciled externally closed Variational position(s)",
                    f"closed_db_trade_ids: {', '.join(str(item['db_trade_id']) for item in closed)}",
                    f"reason: {reason}",
                ])
            )
        return {"closed": closed}

    async def request_manual_close(
        self,
        db_trade_id: Optional[int] = None,
        engine_trade_id: Optional[str] = None,
        reason: str = "MANUAL_CLOSE",
        force: bool = False,
    ) -> Dict[str, Any]:
        """Queue the normal close path for an open Variational/browser trade."""
        open_trades = list(self.position_manager.open_trades.items())
        if not open_trades:
            raise ValueError("No open in-memory bot position is available to close")

        selected_trade_id: Optional[str] = None
        if engine_trade_id:
            if engine_trade_id not in self.position_manager.open_trades:
                raise ValueError(f"Open bot position not found: {engine_trade_id}")
            selected_trade_id = engine_trade_id
        elif db_trade_id is not None:
            selected_trade_id = next(
                (tid for tid, mapped_db_id in self._trade_db_ids.items() if mapped_db_id == db_trade_id),
                None,
            )
            if selected_trade_id is None:
                if len(open_trades) == 1:
                    selected_trade_id = open_trades[0][0]
                    logger.warning(
                        "DB trade %s is not mapped to a bot trade; using sole open trade %s for manual close",
                        db_trade_id,
                        selected_trade_id,
                    )
                else:
                    raise ValueError(f"Open Variational DB trade is not attached to the bot: {db_trade_id}")
        elif len(open_trades) == 1:
            selected_trade_id = open_trades[0][0]
        else:
            choices = ", ".join(
                f"db={self._trade_db_ids.get(tid, '?')} engine={tid}"
                for tid, _ in open_trades
            )
            raise ValueError(f"Multiple open bot positions exist; specify trade_id. Choices: {choices}")

        if selected_trade_id in self._closing_trade_ids:
            return {
                "status": "already_closing",
                "trade_id": selected_trade_id,
                "db_trade_id": self._trade_db_ids.get(selected_trade_id),
            }

        now = time.time()
        retry_after = self._exit_retry_after.get(selected_trade_id, 0.0)
        if retry_after > now and not force:
            return {
                "status": "retry_suppressed",
                "trade_id": selected_trade_id,
                "db_trade_id": self._trade_db_ids.get(selected_trade_id),
                "retry_after_sec": round(retry_after - now, 1),
            }
        if force:
            self._exit_retry_after.pop(selected_trade_id, None)

        message = (reason or "MANUAL_CLOSE").strip()[:120] or "MANUAL_CLOSE"
        task = asyncio.create_task(
            self._handle_exit(selected_trade_id, ExitReason.MANUAL, message)
        )

        def _log_manual_close_task(done_task: asyncio.Task) -> None:
            try:
                done_task.result()
            except Exception:
                logger.exception("Manual Variational close task failed for %s", selected_trade_id)

        task.add_done_callback(_log_manual_close_task)
        return {
            "status": "queued",
            "trade_id": selected_trade_id,
            "db_trade_id": self._trade_db_ids.get(selected_trade_id),
            "reason": message,
            "force": force,
        }

    @staticmethod
    def _datetime_to_timestamp(value) -> float:
        if isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.timestamp()
        return time.time()

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
        await self._restore_open_trades()
        self._running = True

        # Keep the Variational API session warm so idle JWT/Cloudflare expiry is
        # caught (and auto-reconnected) before a signal needs it.
        if self.variational_bridge is not None and hasattr(self.variational_bridge, "start_healthcheck"):
            self.variational_bridge.start_healthcheck()
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
                self._config_reload_loop(),                # DB 설정 hot reload
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

        # Variational API executor (closes the persistent browser/HTTP session).
        if self.variational_bridge is not None and hasattr(self.variational_bridge, "aclose"):
            try:
                await self.variational_bridge.aclose()
            except Exception:  # noqa: BLE001
                logger.warning("Failed to close Variational API executor", exc_info=True)

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
                            self._virtual_exchange_name,
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

    # ── 설정 hot reload ─────────────────────────────────────

    async def _config_reload_loop(self) -> None:
        """DB에 저장된 설정을 주기적으로 반영합니다."""
        if self._config_loader is None:
            return

        while self._running:
            await asyncio.sleep(self._config_reload_interval)
            try:
                loaded_config = await self._config_loader()
                loaded_config = await self._coerce_hot_reload_config(loaded_config)
                fingerprint = self._runtime_config_fingerprint(loaded_config)
                if fingerprint == self._config_fingerprint:
                    continue

                await self._apply_runtime_config(loaded_config)
                self._config_fingerprint = fingerprint
            except asyncio.CancelledError:
                raise
            except Exception as e:
                error_msg = f"Config reload error: {e}"
                logger.warning(error_msg, exc_info=True)
                self._errors.append(error_msg)
                if len(self._errors) > 100:
                    self._errors = self._errors[-50:]

    async def _coerce_hot_reload_config(self, loaded_config: BotConfig) -> BotConfig:
        """런타임에 안전하게 바꿀 수 없는 필드는 현재 값으로 고정합니다."""
        requested_execution = self._normalize_execution_mode(
            loaded_config.execution_mode,
            loaded_config.paper_trading,
        )
        requested_primary = loaded_config.primary_exchange
        ignored = []
        if requested_execution != self.execution_mode:
            ignored.append(
                f"execution_mode={requested_execution} requires bot restart "
                f"(running={self.execution_mode})"
            )
        if requested_primary != self.config.primary_exchange:
            ignored.append(
                f"primary_exchange={requested_primary} requires bot restart "
                f"(running={self.config.primary_exchange})"
            )

        if ignored:
            warning = "; ".join(ignored)
            if warning != self._ignored_reload_warning:
                self._ignored_reload_warning = warning
                logger.warning("Ignored runtime config change: %s", warning)
                if self.telegram:
                    await self.telegram.status(
                        "\n".join([
                            "CONFIG RELOAD NOTICE",
                            warning,
                            "Stop and start the bot to apply execution venue changes.",
                        ])
                    )

        loaded_config.execution_mode = self.config.execution_mode
        loaded_config.paper_trading = self.config.paper_trading
        loaded_config.primary_exchange = self.config.primary_exchange
        return loaded_config

    async def _apply_runtime_config(self, loaded_config: BotConfig) -> None:
        """시그널/청산/사이즈 설정을 실행 중인 엔진에 반영합니다."""
        old_config = self.config
        self.config = loaded_config
        self.config.execution_mode = old_config.execution_mode
        self.config.paper_trading = old_config.paper_trading
        self.config.primary_exchange = old_config.primary_exchange

        signal_cfg = self.config.signal_config or MultiTFConfig.from_mode(self.config.trading_mode)
        self.signal_engine.update_config(signal_cfg)

        risk_cfg = self.config.risk_config or RiskConfig()
        self.risk_manager.update_config(risk_cfg)
        self._apply_virtual_risk_guards()

        logger.info(
            "Runtime config reloaded: mode=%s size=$%.0f leverage=%dx "
            "entry_z=%.2f div=%.2f%% tp=%.2f%% sl=%.2f%% z_exit_min=%.2f%%",
            self.config.trading_mode,
            self.config.position_size_usd,
            self.config.leverage,
            signal_cfg.entry_zscore,
            signal_cfg.divergence_threshold_pct,
            risk_cfg.take_profit_pct,
            risk_cfg.stop_loss_pct,
            risk_cfg.zscore_exit_min_pnl_pct,
        )
        if self.telegram:
            await self.telegram.send(
                "\n".join([
                    "[Monk] CONFIG RELOADED",
                    f"mode: {self.config.trading_mode}",
                    f"size: ${self.config.position_size_usd:.2f} per leg",
                    f"leverage: {self.config.leverage}x",
                    f"entry_zscore: {signal_cfg.entry_zscore:.3f}",
                    f"divergence_threshold: {signal_cfg.divergence_threshold_pct:.4f}%",
                    f"take_profit: {risk_cfg.take_profit_pct:.3f}%",
                    f"stop_loss: {risk_cfg.stop_loss_pct:.3f}%",
                    f"zscore_exit_min_pnl: {risk_cfg.zscore_exit_min_pnl_pct:.3f}%",
                    f"min_hold_minutes: {risk_cfg.min_hold_minutes:.1f}",
                    f"max_hold_hours: {risk_cfg.max_hold_hours:.1f}",
                ])
            )

    def _apply_virtual_risk_guards(self) -> None:
        if not self._uses_virtual_positions:
            return
        self.risk_manager.config.averaging_enabled = False
        self.risk_manager.config.size_reduction_enabled = False

    @staticmethod
    def _runtime_config_fingerprint(config: BotConfig) -> str:
        signal_cfg = config.signal_config or MultiTFConfig.from_mode(config.trading_mode)
        risk_cfg = config.risk_config or RiskConfig()
        payload = {
            "position_size_usd": config.position_size_usd,
            "leverage": config.leverage,
            "trading_mode": config.trading_mode,
            "signal_config": asdict(signal_cfg),
            "risk_config": asdict(risk_cfg),
            "taker_fee_bps": config.taker_fee_bps,
            "slippage_bps": config.slippage_bps,
            "position_check_interval": config.position_check_interval,
        }
        return json.dumps(payload, sort_keys=True, default=str)

    # ── 진입 처리 ───────────────────────────────────────────

    async def _handle_entry(self, signal: Signal) -> None:
        """시그널에 따라 페어 포지션을 엽니다."""
        now = time.time()
        if self._entry_retry_after > now:
            logger.warning(
                "Entry suppressed for %.1fs after failed Variational browser entry",
                self._entry_retry_after - now,
            )
            return

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

        variational_entry_batch: Optional[VariationalBrowserRequestBatch] = None
        if self.variational_bridge:
            try:
                variational_entry_batch = await self.variational_bridge.create_entry_requests(
                    direction=direction,
                    size_usd=self.config.position_size_usd,
                    zscore=signal.zscore_5m,
                    divergence_pct=signal.divergence_pct,
                )
                await self._notify_variational_requests("entry", variational_entry_batch)
                execution_status, _ = await self._wait_variational_browser_execution_result("entry", variational_entry_batch)
                if execution_status != "clicked":
                    cooldown = (
                        self._browser_unavailable_retry_cooldown_sec
                        if execution_status in {"browser_unavailable", "wallet_unavailable"}
                        else self._entry_retry_cooldown_sec
                    )
                    self._entry_retry_after = time.time() + cooldown
                    return
            except Exception as e:
                self._entry_retry_after = time.time() + self._entry_retry_cooldown_sec
                logger.error("Failed to create Variational entry requests: %s", e, exc_info=True)
                if self.telegram:
                    await self.telegram.error("Variational entry request failed", str(e))
                return

        if self._uses_virtual_positions:
            trade = self.position_manager.open_virtual_pair(
                exchange_name=self._virtual_exchange_name,
                direction=direction,
                size_usd=self.config.position_size_usd,
                btc_price=btc_price,
                eth_price=eth_price,
                zscore=signal.zscore_5m,
                spread_pct=signal.divergence_pct,
                taker_fee_bps=self.config.taker_fee_bps,
                slippage_bps=self.config.slippage_bps,
                btc_quantity=request_quantity(variational_entry_batch, "BTC"),
                eth_quantity=request_quantity(variational_entry_batch, "ETH"),
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
        if trade_id in self._closing_trade_ids:
            logger.info("Exit already in progress for %s; skipping duplicate close request", trade_id)
            return

        retry_after = self._exit_retry_after.get(trade_id, 0.0)
        now = time.time()
        if retry_after > now:
            logger.warning(
                "Exit retry suppressed for %s for %.1fs after a failed Variational close",
                trade_id,
                retry_after - now,
            )
            return

        self._closing_trade_ids.add(trade_id)
        logger.info("EXIT: %s | reason=%s | %s", trade_id, reason.value, message)

        try:
            if self._uses_virtual_positions:
                if self.variational_bridge:
                    open_trade = self.position_manager.open_trades.get(trade_id)
                    if open_trade is None:
                        logger.warning("Virtual trade not found for Variational close request: %s", trade_id)
                        return
                    try:
                        close_batch = await self.variational_bridge.create_close_requests(
                            trade=open_trade,
                            reason=reason.value,
                        )
                        await self._notify_variational_requests("close", close_batch)
                        execution_status, _ = await self._wait_variational_browser_execution_result("close", close_batch)
                        if execution_status == "external_closed":
                            db_id = self._trade_db_ids.get(trade_id)
                            if db_id is None:
                                logger.warning(
                                    "Variational close resolved externally but DB trade id is unknown for %s",
                                    trade_id,
                                )
                                self._exit_retry_after[trade_id] = time.time() + self._exit_retry_cooldown_sec
                                return
                            result = await self.reconcile_external_close(
                                db_trade_id=db_id,
                                reason="EXTERNAL_MANUAL_CLOSE",
                                pnl_usd=0.0,
                            )
                            if not result.get("closed"):
                                self._exit_retry_after[trade_id] = time.time() + self._exit_retry_cooldown_sec
                            return
                        if execution_status != "clicked":
                            cooldown = (
                                self._browser_unavailable_retry_cooldown_sec
                                if execution_status in {"browser_unavailable", "wallet_unavailable"}
                                else self._exit_retry_cooldown_sec
                            )
                            self._exit_retry_after[trade_id] = time.time() + cooldown
                            return
                    except Exception as e:
                        self._exit_retry_after[trade_id] = time.time() + self._exit_retry_cooldown_sec
                        logger.error("Failed to create Variational close requests: %s", e, exc_info=True)
                        if self.telegram:
                            await self.telegram.error("Variational close request failed", str(e))
                        return

                trade = self.position_manager.close_virtual_pair(trade_id, reason.value)
                if trade:
                    self._exit_retry_after.pop(trade_id, None)
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
                self._exit_retry_after.pop(trade_id, None)
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
        finally:
            self._closing_trade_ids.discard(trade_id)

    async def _notify_variational_requests(
        self,
        label: str,
        batch: VariationalBrowserRequestBatch,
    ) -> None:
        vlabel = self._variational_label
        logger.info(
            "%s %s requests queued: %s",
            vlabel,
            label,
            ", ".join(str(path) for path in batch.paths),
        )
        if not self.telegram:
            return
        lines = [
            f"{vlabel} {label} requests queued",
            f"action: {batch.action}",
            "files:",
            *[str(path) for path in batch.paths],
        ]
        if self.execution_mode == EXECUTION_VARIATIONAL_BROWSER:
            lines.append("Run tools/variational-browser daemon to process them.")
        await self.telegram.status("\n".join(lines))

    async def _await_variational_browser_execution(
        self,
        label: str,
        batch: VariationalBrowserRequestBatch,
    ) -> bool:
        status, _ = await self._wait_variational_browser_execution_result(label, batch)
        return status == "clicked"

    async def _wait_variational_browser_execution_result(
        self,
        label: str,
        batch: VariationalBrowserRequestBatch,
    ) -> tuple[str, str]:
        if self.variational_bridge is None:
            return "clicked", ""

        vlabel = self._variational_label
        completions = await self.variational_bridge.wait_for_batch_completion(batch)
        summary = format_completions(completions)
        if completions_all_clicked(completions):
            logger.info("%s %s execution confirmed:\n%s", vlabel, label, summary)
            if self.telegram:
                await self.telegram.status(
                    "\n".join([
                        f"{vlabel} {label} execution confirmed",
                        summary,
                    ])
                )
            return "clicked", summary

        if label == "close" and completions_close_resolved(completions):
            logger.warning("%s close resolved by external flat state:\n%s", vlabel, summary)
            if self.telegram:
                await self.telegram.status(
                    "\n".join([
                        f"{vlabel} close resolved externally",
                        summary,
                        "No live close was needed because Variational appeared flat.",
                    ])
                )
            if completions_external_closed(completions):
                return "external_closed", summary
            return "clicked", summary

        if completions_browser_unavailable(completions):
            logger.error("Variational Browser %s unavailable:\n%s", label, summary)
            if self.telegram:
                await self.telegram.error(
                    "Variational Browser unavailable",
                    "\n".join([
                        summary,
                        "Chrome/CDP page was closed while processing the request.",
                        "Restart the Chrome process opened with --remote-debugging-port=9222 and then restart variational-browser.",
                    ]),
                )
            return "browser_unavailable", summary

        if completions_wallet_unavailable(completions):
            logger.error("Variational Browser wallet unavailable during %s:\n%s", label, summary)
            if self.telegram:
                await self.telegram.error(
                    "Variational Browser wallet unavailable",
                    "\n".join([
                        summary,
                        "Variational page is disconnected/auth-required, so no live click was sent.",
                        "Reconnect the wallet with tools/variational-browser --connect-wallet and keep variational-wallet running.",
                    ]),
                )
            return "wallet_unavailable", summary

        logger.warning("%s %s not executed:\n%s", vlabel, label, summary)
        if self.telegram:
            await self.telegram.error(
                f"{vlabel} {label} not executed",
                "\n".join([
                    summary,
                    "Virtual/DB position was not changed.",
                ]),
            )
        return "failed", summary

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
