"""Variational API executor — drop-in replacement for the browser click bridge.

The engine used to place Variational orders by writing request files that a
headless-Chrome daemon clicked through (with Telegram approval). That path is
slow: the two legs of a pair open seconds apart, skewing the intended
market-neutral entry.

This executor exposes the SAME surface the engine calls on ``variational_bridge``
(``create_entry_requests`` / ``create_close_requests`` /
``wait_for_batch_completion`` / ``open_request_statuses_for_trade``) but executes
via the direct JSON API (``backend.bot.variational.api``). Both legs fire
concurrently with millisecond-level skew, and there is no clicking or approval.

Return types are structurally compatible with the browser bridge's, so the
engine's helpers (``request_quantity`` / ``completions_all_clicked`` /
``format_completions``) work unchanged: a batch has ``action`` / ``paths`` /
``requests`` and each request carries ``variationalOrder`` with the filled
quantity; a completion has ``path`` (for ``.name``) and ``status`` (``"clicked"``
on success, ``"failed"`` otherwise, ``"external_closed"`` when a close finds the
venue already flat).

Safety: if one entry leg fills and the other fails, the filled leg is unwound
immediately (opposite reduce-only order) so the bot never carries a one-legged
position, and the batch reports failure so the engine skips recording it.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Callable, List, Optional

from backend.bot.position_manager import PairDirection, PairTrade
from backend.bot.variational.api.api_client import VariationalConnector
from backend.bot.variational.api.api_config import get_variational_settings
from backend.bot.variational.api.api_types import ExchangeError, Order, OrderResult, Side

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ApiExecutionCompletion:
    """Compatible with VariationalBrowserRequestCompletion (has .path, .status)."""

    path: Path
    status: str
    done_path: Optional[Path] = None


@dataclass(frozen=True)
class ApiExecutionBatch:
    """Compatible with VariationalBrowserRequestBatch (.action/.paths/.requests),
    plus the already-computed completions the API produced synchronously."""

    action: str
    paths: List[Path]
    requests: List[dict]
    completions: List[ApiExecutionCompletion] = field(default_factory=list)


# Legs of a pair, per direction: (symbol, entry_side).
def _entry_legs(direction: PairDirection) -> list[tuple[str, Side]]:
    if direction == PairDirection.LONG_BTC_SHORT_ETH:
        return [("BTC", Side.BUY), ("ETH", Side.SELL)]
    return [("BTC", Side.SELL), ("ETH", Side.BUY)]


class VariationalApiExecutor:
    """Executes Variational pair entries/exits via the direct API."""

    def __init__(
        self,
        settings=None,
        connector_factory: Optional[Callable[[], VariationalConnector]] = None,
        notifier=None,
    ):
        self._settings = settings or get_variational_settings()
        self._connector_factory = connector_factory or (
            lambda: VariationalConnector(self._settings)
        )
        self._connector: Optional[VariationalConnector] = None
        self._connect_lock = asyncio.Lock()
        # Session keepalive: the bot only touches the API on a (rare) signal, so
        # an idle JWT/Cloudflare expiry would only surface at the worst moment.
        # A periodic probe keeps the session warm and detects breakage early.
        self._notifier = notifier
        self._health_task: Optional[asyncio.Task] = None
        self._session_healthy = True
        self._in_flight = 0  # order batches currently executing (probe defers to them)

    # -- lifecycle ----------------------------------------------------------
    async def _ensure_connected(self) -> VariationalConnector:
        """Connect once and reuse (the browser transport keeps a session open)."""
        if self._connector is not None:
            return self._connector
        async with self._connect_lock:
            if self._connector is None:
                connector = self._connector_factory()
                await connector.connect()
                self._connector = connector
        return self._connector

    async def aclose(self) -> None:
        await self.stop_healthcheck()
        if self._connector is not None:
            try:
                await self._connector.close()
            finally:
                self._connector = None

    # -- session keepalive --------------------------------------------------
    def start_healthcheck(self) -> None:
        """Begin the periodic session probe (idempotent). Call after the bot starts."""
        interval = float(getattr(self._settings, "api_healthcheck_sec", 120) or 0)
        if interval <= 0 or self._health_task is not None:
            return
        self._health_task = asyncio.ensure_future(self._health_loop(interval))
        logger.info("Variational API session keepalive started (every %.0fs)", interval)

    async def stop_healthcheck(self) -> None:
        task, self._health_task = self._health_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    async def _health_loop(self, interval: float) -> None:
        while True:
            try:
                await asyncio.sleep(interval)
                await self._run_health_check()
            except asyncio.CancelledError:
                break
            except Exception:  # noqa: BLE001 - loop must never die
                logger.exception("Variational API keepalive loop error")

    async def _run_health_check(self) -> None:
        # Don't probe/reconnect while an order batch is executing — it shares the
        # connector/browser, and a reconnect mid-order would disrupt it.
        if self._in_flight > 0:
            return
        try:
            connector = await self._ensure_connected()
            await connector.get_position("BTC")  # cheap authenticated call
            if not self._session_healthy:
                self._session_healthy = True
                await self._notify_status(
                    "Variational API session recovered — probe succeeded again.")
        except Exception as exc:  # noqa: BLE001
            await self._handle_unhealthy(exc)

    async def _handle_unhealthy(self, exc: Exception) -> None:
        logger.warning("Variational API session probe failed: %s", exc)
        if self._in_flight > 0:
            return  # an order is running; let it own the connector
        try:
            async with self._connect_lock:
                if self._connector is not None:
                    try:
                        await self._connector.close()
                    except Exception:  # noqa: BLE001
                        pass
                    self._connector = None
                connector = self._connector_factory()
                await connector.connect()
                self._connector = connector
            self._session_healthy = True
            # One status ping per reconnect — infrequent (sessions last hours),
            # so this is useful observability, not spam.
            await self._notify_status(
                "Variational API session auto-reconnected after a failed probe.")
        except Exception as rexc:  # noqa: BLE001
            self._session_healthy = False
            logger.error("Variational API auto-reconnect failed: %s", rexc, exc_info=True)
            await self._notify_error(
                "Variational API session DOWN",
                f"probe error: {exc}\nreconnect failed: {rexc}\n"
                "New signals will fail to execute. Consider switching Execution "
                "Mode to Variational Browser until the session is restored.",
            )

    async def _notify_status(self, text: str) -> None:
        logger.info(text)
        if self._notifier is not None:
            try:
                await self._notifier.status(text)
            except Exception:  # noqa: BLE001
                logger.warning("Telegram status send failed", exc_info=True)

    async def _notify_error(self, title: str, detail: str) -> None:
        if self._notifier is not None:
            try:
                await self._notifier.error(title, detail)
            except Exception:  # noqa: BLE001
                logger.warning("Telegram error send failed", exc_info=True)

    # -- entry --------------------------------------------------------------
    async def create_entry_requests(
        self,
        *,
        direction: PairDirection,
        size_usd: float,
        zscore: float = 0.0,
        divergence_pct: float = 0.0,
    ) -> ApiExecutionBatch:
        connector = await self._ensure_connected()
        legs = _entry_legs(direction)
        orders = [
            Order(symbol=sym, side=side, size_usd=Decimal(str(size_usd)))
            for sym, side in legs
        ]
        self._in_flight += 1
        try:
            results = await asyncio.gather(
                *(connector.place_order(o) for o in orders),
                return_exceptions=True,
            )

            accepted = [
                isinstance(r, OrderResult) and r.accepted for r in results
            ]
            if all(accepted):
                return self._batch("open", legs, orders, results, status="clicked")

            # Partial or total failure: unwind any filled leg so we stay flat.
            await self._unwind_partial(connector, legs, orders, results)
            self._log_failures("entry", legs, results)
            return self._batch("open", legs, orders, results, status="failed")
        finally:
            self._in_flight -= 1

    async def _unwind_partial(
        self,
        connector: VariationalConnector,
        legs: list[tuple[str, Side]],
        orders: list[Order],
        results: list,
    ) -> None:
        for (sym, side), res in zip(legs, results):
            if isinstance(res, OrderResult) and res.accepted and res.filled_qty:
                try:
                    logger.warning(
                        "Unwinding filled %s leg (%s %s) after pair entry failed",
                        sym, side.value, res.filled_qty,
                    )
                    await connector.place_order(
                        Order(
                            symbol=sym,
                            side=side.opposite,
                            size_usd=Decimal(0),
                            size_base=res.filled_qty,
                            reduce_only=True,
                        )
                    )
                except Exception as exc:  # noqa: BLE001 - must not raise here
                    logger.error(
                        "CRITICAL: failed to unwind %s leg; position may be one-legged: %s",
                        sym, exc, exc_info=True,
                    )

    # -- close --------------------------------------------------------------
    async def create_close_requests(
        self,
        *,
        trade: PairTrade,
        reason: str = "",
    ) -> ApiExecutionBatch:
        connector = await self._ensure_connected()
        # Close = opposite side of each open leg, using the exact filled base qty.
        legs: list[tuple[str, Side]] = []
        orders: list[Order] = []
        for symbol, leg in (("BTC", trade.btc_leg), ("ETH", trade.eth_leg)):
            close_side = Side.SELL if leg.side.value.lower().startswith("long") else Side.BUY
            legs.append((symbol, close_side))
            orders.append(
                Order(
                    symbol=symbol,
                    side=close_side,
                    size_usd=Decimal(0),
                    size_base=Decimal(str(leg.quantity)),
                    reduce_only=True,
                )
            )

        self._in_flight += 1
        try:
            results = await asyncio.gather(
                *(connector.place_order(o) for o in orders),
                return_exceptions=True,
            )

            statuses: list[str] = []
            for (sym, _side), res in zip(legs, results):
                if isinstance(res, OrderResult) and res.accepted:
                    statuses.append("clicked")
                elif await self._leg_is_flat(connector, sym):
                    # Already closed on the venue (manual/liquidation) — treat as done.
                    statuses.append("external_closed")
                else:
                    statuses.append("failed")

            self._log_failures("close", legs, results)
            return self._batch("close", legs, orders, results, statuses=statuses)
        finally:
            self._in_flight -= 1

    async def _leg_is_flat(self, connector: VariationalConnector, symbol: str) -> bool:
        try:
            return await connector.get_position(symbol) is None
        except Exception:  # noqa: BLE001
            return False

    # -- wait (synchronous already) -----------------------------------------
    async def wait_for_batch_completion(
        self, batch: ApiExecutionBatch, *, timeout_sec: Optional[int] = None
    ) -> List[ApiExecutionCompletion]:
        # Orders executed synchronously in create_*; just report the outcome.
        return list(batch.completions)

    # -- reconciliation (browser-only concept) ------------------------------
    def open_request_statuses_for_trade(self, **_kwargs) -> dict:
        # API orders confirm synchronously, so there are no unconfirmed clicks to
        # reconcile. Restart reconciliation should use live get_position instead.
        return {}

    # -- helpers ------------------------------------------------------------
    def _batch(
        self,
        action: str,
        legs: list[tuple[str, Side]],
        orders: list[Order],
        results: list,
        *,
        status: Optional[str] = None,
        statuses: Optional[list[str]] = None,
    ) -> ApiExecutionBatch:
        requests: List[dict] = []
        completions: List[ApiExecutionCompletion] = []
        for i, ((sym, side), order, res) in enumerate(zip(legs, orders, results)):
            filled_qty = (
                float(res.filled_qty)
                if isinstance(res, OrderResult) and res.filled_qty is not None
                else (float(order.size_base) if order.size_base is not None else None)
            )
            order_id = res.order_id if isinstance(res, OrderResult) else None
            requests.append(
                {
                    "id": f"api-{action}-{sym}",
                    "variationalOrder": {
                        "symbol": sym,
                        "side": side.value,
                        "quantity": filled_qty,
                        "reduceOnly": order.reduce_only,
                        "orderId": order_id,
                        "action": action,
                    },
                }
            )
            leg_status = statuses[i] if statuses is not None else status
            completions.append(
                ApiExecutionCompletion(path=Path(f"api-{action}-{sym}"), status=leg_status)
            )
        paths = [c.path for c in completions]
        return ApiExecutionBatch(action=action, paths=paths, requests=requests, completions=completions)

    @staticmethod
    def _log_failures(label: str, legs: list[tuple[str, Side]], results: list) -> None:
        for (sym, side), res in zip(legs, results):
            if isinstance(res, Exception):
                logger.error("Variational API %s %s %s failed: %s", label, side.value, sym, res)
            elif isinstance(res, OrderResult) and not res.accepted:
                logger.error("Variational API %s %s %s not accepted: %s", label, side.value, sym, res.raw)
