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
import time
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


def _brief(exc: BaseException, limit: int = 300) -> str:
    """First line of an exception, capped — for Telegram (the log keeps it all).

    Playwright errors append a multi-KB "Call log:" (the full Chromium command
    line and launch output). Two of them in one alert broke Telegram's 4096-char
    limit, so none of the 2026-10-16 "session DOWN" alerts were delivered.
    """
    lines = str(exc).strip().splitlines()
    text = lines[0] if lines else type(exc).__name__
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _fmt_wait(seconds: float) -> str:
    return f"{seconds / 60:.0f} min" if seconds >= 120 else f"{seconds:.0f}s"


class VariationalApiExecutor:
    """Executes Variational pair entries/exits via the direct API."""

    def __init__(
        self,
        settings=None,
        connector_factory: Optional[Callable[[], VariationalConnector]] = None,
        notifier=None,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._settings = settings or get_variational_settings()
        self._connector_factory = connector_factory or (
            lambda: VariationalConnector(self._settings)
        )
        self._connector: Optional[VariationalConnector] = None
        self._connect_lock = asyncio.Lock()
        self._clock = clock
        # Session keepalive: the bot only touches the API on a (rare) signal, so
        # an idle JWT/Cloudflare expiry would only surface at the worst moment.
        # A periodic probe keeps the session warm and detects breakage early.
        self._notifier = notifier
        self._health_task: Optional[asyncio.Task] = None
        self._session_healthy = True
        self._in_flight = 0  # order batches currently executing (probe defers to them)
        # Browser hygiene: a Chromium page left open for days grows its JS heap.
        # Recycling the session on a schedule keeps that bounded. 0 disables.
        self._connected_at = 0.0
        # Reconnect backoff: each consecutive failed health check doubles the wait
        # before the keepalive launches another browser (api_reconnect_base_sec,
        # capped at api_reconnect_max_sec); a passing probe resets it. Without it
        # a persistent failure, like a changed login API, relaunched Chromium on
        # every tick.
        self._consecutive_failures = 0
        self._retry_not_before = 0.0

    # -- lifecycle ----------------------------------------------------------
    async def _ensure_connected(self) -> VariationalConnector:
        """Connect once and reuse (the browser transport keeps a session open).

        Orders call this directly and ignore the keepalive's reconnect backoff:
        signals are rare, each is worth one connect attempt, and the engine has
        its own retry cooldown.
        """
        if self._connector is not None:
            return self._connector
        async with self._connect_lock:
            if self._connector is None:
                self._connector = await self._connect_new()
                self._connected_at = self._clock()
        return self._connector

    async def _connect_new(self) -> VariationalConnector:
        """Build and connect a fresh connector, closing it again if connect() fails.

        A connector only reaches ``self._connector`` once connected, so after a
        failed connect() nothing else would ever close it — with the browser
        transport that is a live Chromium on the persistent profile plus its
        Playwright driver.
        """
        connector = self._connector_factory()
        try:
            await connector.connect()
        except BaseException:
            await self._close_quietly(connector)
            raise
        return connector

    @staticmethod
    async def _close_quietly(connector: VariationalConnector) -> None:
        try:
            await connector.close()
        except Exception:  # noqa: BLE001
            logger.warning("Closing a Variational connector failed", exc_info=True)

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
        # Pre-warm immediately: build the browser session + SIWE login now, while
        # nothing is waiting on it. Otherwise the first order to arrive pays the
        # cold-start cost (browser launch + login, ~5s) inline on the order path —
        # exactly the latency this executor exists to remove.
        try:
            await self._run_health_check()
            if self._session_healthy and self._connector is not None:
                logger.info("Variational API session pre-warmed — ready for orders")
        except asyncio.CancelledError:
            return
        except Exception:  # noqa: BLE001
            logger.exception("Variational API session pre-warm failed")
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
        # No session and a reconnect failed recently: wait out the backoff instead
        # of launching another browser into the same error.
        if self._connector is None and self._backing_off():
            return
        try:
            connector = await self._ensure_connected()
        except Exception as exc:  # noqa: BLE001
            await self._reconnect_failed("connect failed", exc)
            return
        try:
            await connector.get_position("BTC")  # cheap authenticated call
        except Exception as exc:  # noqa: BLE001
            await self._handle_unhealthy(exc)
            return
        self._consecutive_failures = 0
        self._retry_not_before = 0.0
        if not self._session_healthy:
            self._session_healthy = True
            await self._notify_status(
                "Variational API session recovered — probe succeeded again.")
        await self._recycle_if_stale()

    async def _rebuild_connector(self) -> None:
        """Tear down the browser session and build a fresh one (login included).

        The old session goes first — two browsers can't share the persistent
        profile — so a failed rebuild leaves no session at all; the keepalive
        then reconnects under the backoff.
        """
        async with self._connect_lock:
            old, self._connector = self._connector, None
            if old is not None:
                await self._close_quietly(old)
            self._connector = await self._connect_new()
            self._connected_at = self._clock()

    async def _recycle_if_stale(self) -> None:
        """Recycle a long-lived browser session before its heap becomes a problem.

        A Chromium page held open for days keeps growing; on a shared box that
        eventually starves everything else. Rebuilding on a schedule bounds it.
        Runs only when idle. A failed recycle leaves no session (see
        ``_rebuild_connector``), so it is alerted and retried like any failed
        reconnect.
        """
        hours = float(getattr(self._settings, "api_recycle_hours", 6) or 0)
        if hours <= 0 or self._connector is None or self._in_flight > 0:
            return
        age = self._clock() - self._connected_at
        if age < hours * 3600:
            return
        logger.info(
            "Recycling Variational API browser session after %.1fh to bound memory", age / 3600
        )
        try:
            await self._rebuild_connector()
        except Exception as exc:  # noqa: BLE001
            await self._reconnect_failed("session recycle failed", exc)

    async def _handle_unhealthy(self, exc: Exception) -> None:
        """The probe failed on a live session: rebuild it (backoff permitting)."""
        logger.warning("Variational API session probe failed: %s", exc)
        if self._in_flight > 0:
            return  # an order is running; let it own the connector
        if self._backing_off():
            return  # rebuilt recently; the backoff decides when to try again
        try:
            await self._rebuild_connector()
        except Exception as rexc:  # noqa: BLE001
            await self._reconnect_failed("reconnect failed", rexc, probe_exc=exc)
            return
        # The failed probe still counts: if probes keep failing on fresh sessions,
        # rebuilds back off as well instead of relaunching Chromium every tick.
        self._schedule_retry()
        self._session_healthy = True
        # One status ping per reconnect — infrequent (sessions last hours),
        # so this is useful observability, not spam.
        await self._notify_status(
            "Variational API session auto-reconnected after a failed probe.")

    async def _reconnect_failed(
        self, what: str, exc: Exception, *, probe_exc: Optional[Exception] = None
    ) -> None:
        delay = self._schedule_retry()
        self._session_healthy = False
        failures = self._consecutive_failures
        # The next attempt runs on the first keepalive tick past the backoff.
        interval = float(getattr(self._settings, "api_healthcheck_sec", 120) or 0)
        wait = max(delay, interval)
        logger.error(
            "Variational API %s (%d in a row, next attempt in ~%.0fs): %s",
            what, failures, wait, exc, exc_info=exc,
        )
        lines = []
        if probe_exc is not None:
            lines.append(f"probe error: {_brief(probe_exc)}")
        lines += [
            f"{what} (consecutive failures: {failures}): {_brief(exc)}",
            f"next attempt in ~{_fmt_wait(wait)} (backoff doubles per failure)",
            "New signals will fail to execute. Consider switching Execution "
            "Mode to Variational Browser until the session is restored.",
        ]
        await self._notify_error("Variational API session DOWN", "\n".join(lines))

    def _schedule_retry(self) -> float:
        """Count one more failed check and push the next reconnect out."""
        self._consecutive_failures += 1
        delay = self._retry_delay(self._consecutive_failures)
        self._retry_not_before = self._clock() + delay
        return delay

    def _retry_delay(self, failures: int) -> float:
        """api_reconnect_base_sec, doubled per further failure, capped at api_reconnect_max_sec."""
        base = float(getattr(self._settings, "api_reconnect_base_sec", 120) or 0)
        cap = float(getattr(self._settings, "api_reconnect_max_sec", 1800) or 0)
        if base <= 0:
            return 0.0  # backoff disabled: retry on every keepalive tick
        delay = base * 2 ** min(failures - 1, 30)
        return min(delay, cap) if cap > 0 else delay

    def _backing_off(self) -> bool:
        return self._clock() < self._retry_not_before

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
