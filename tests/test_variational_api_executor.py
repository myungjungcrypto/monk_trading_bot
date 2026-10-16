"""Unit tests for VariationalApiExecutor (drop-in for the browser bridge).

Uses a fake connector — no network, no funds. Mirrors the repo's asyncio.run()
test style so it needs no pytest-asyncio config.
"""

import asyncio
from decimal import Decimal
from types import SimpleNamespace

from backend.bot.exchanges.base import PositionSide
from backend.bot.position_manager import PairDirection
from backend.bot.variational.api_executor import VariationalApiExecutor
from backend.bot.variational.api.api_types import ExchangeError, Order, OrderResult, Side
from backend.bot.variational.browser_requests import (
    completions_all_clicked,
    request_quantity,
)


class FakeConnector:
    """Records orders; returns canned results or raises per-symbol."""

    def __init__(self, *, fail_symbols=(), flat_symbols=()):
        self.orders: list[Order] = []
        self.connected = False
        self.closed = False
        self._fail = set(fail_symbols)
        self._flat = set(flat_symbols)

    async def connect(self):
        self.connected = True

    async def close(self):
        self.closed = True

    async def place_order(self, order: Order) -> OrderResult:
        self.orders.append(order)
        if order.symbol in self._fail:
            raise ExchangeError(f"boom {order.symbol}", venue="variational")
        qty = order.size_base if order.size_base is not None else Decimal("0.001")
        return OrderResult(
            accepted=True, venue="variational", symbol=order.symbol, side=order.side,
            size_usd=order.size_usd, filled_price=Decimal("100"), filled_qty=qty,
            order_id=f"rfq-{order.symbol}-{order.side.value}",
        )

    async def get_position(self, symbol):
        return None  # treated as flat


def _executor(connector) -> VariationalApiExecutor:
    return VariationalApiExecutor(
        settings=SimpleNamespace(),  # unused; factory injected below
        connector_factory=lambda: connector,
    )


def test_entry_both_legs_fire_with_correct_sides():
    conn = FakeConnector()
    ex = _executor(conn)

    async def run():
        batch = await ex.create_entry_requests(
            direction=PairDirection.LONG_BTC_SHORT_ETH, size_usd=500,
        )
        completions = await ex.wait_for_batch_completion(batch)
        return batch, completions

    batch, completions = asyncio.run(run())

    # LONG_BTC_SHORT_ETH => BTC buy, ETH sell.
    sides = {o.symbol: o.side for o in conn.orders}
    assert sides == {"BTC": Side.BUY, "ETH": Side.SELL}
    assert all(not o.reduce_only for o in conn.orders)
    assert completions_all_clicked(completions)
    # Filled quantities are exposed via the engine's request_quantity helper.
    assert request_quantity(batch, "BTC") == 0.001
    assert request_quantity(batch, "ETH") == 0.001


def test_entry_short_direction_flips_sides():
    conn = FakeConnector()
    ex = _executor(conn)
    asyncio.run(ex.create_entry_requests(direction=PairDirection.SHORT_BTC_LONG_ETH, size_usd=100))
    sides = {o.symbol: o.side for o in conn.orders}
    assert sides == {"BTC": Side.SELL, "ETH": Side.BUY}


def test_entry_partial_fill_unwinds_and_reports_failed():
    # ETH leg fails; BTC leg fills and must be unwound.
    conn = FakeConnector(fail_symbols=("ETH",))
    ex = _executor(conn)

    async def run():
        return await ex.create_entry_requests(direction=PairDirection.LONG_BTC_SHORT_ETH, size_usd=500)

    batch = asyncio.run(run())

    # Batch reports failure so the engine won't record a virtual position.
    assert not completions_all_clicked(batch.completions)
    assert all(c.status == "failed" for c in batch.completions)

    # An unwind order was placed: BTC SELL, reduce_only, using the filled qty.
    unwinds = [o for o in conn.orders if o.reduce_only]
    assert len(unwinds) == 1
    u = unwinds[0]
    assert u.symbol == "BTC" and u.side == Side.SELL
    assert u.size_base == Decimal("0.001")


def test_close_uses_opposite_side_reduce_only_and_exact_qty():
    conn = FakeConnector()
    ex = _executor(conn)
    trade = SimpleNamespace(
        direction=PairDirection.LONG_BTC_SHORT_ETH,
        btc_leg=SimpleNamespace(side=PositionSide.LONG, quantity=0.00008),
        eth_leg=SimpleNamespace(side=PositionSide.SHORT, quantity=0.0099),
    )

    async def run():
        batch = await ex.create_close_requests(trade=trade, reason="TAKE_PROFIT")
        return batch, await ex.wait_for_batch_completion(batch)

    batch, completions = asyncio.run(run())

    by_symbol = {o.symbol: o for o in conn.orders}
    # LONG BTC closes with SELL; SHORT ETH closes with BUY. All reduce-only.
    assert by_symbol["BTC"].side == Side.SELL and by_symbol["BTC"].reduce_only
    assert by_symbol["ETH"].side == Side.BUY and by_symbol["ETH"].reduce_only
    # Exact base-asset quantities from the trade legs (no USD roundtrip).
    assert by_symbol["BTC"].size_base == Decimal("0.00008")
    assert by_symbol["ETH"].size_base == Decimal("0.0099")
    assert completions_all_clicked(completions)


def test_close_marks_external_closed_when_already_flat():
    # A leg fails to submit but the venue shows flat -> external_closed, not failed.
    conn = FakeConnector(fail_symbols=("BTC", "ETH"))
    ex = _executor(conn)
    trade = SimpleNamespace(
        direction=PairDirection.LONG_BTC_SHORT_ETH,
        btc_leg=SimpleNamespace(side=PositionSide.LONG, quantity=0.00008),
        eth_leg=SimpleNamespace(side=PositionSide.SHORT, quantity=0.0099),
    )
    batch = asyncio.run(ex.create_close_requests(trade=trade, reason="STOP_LOSS"))
    assert all(c.status == "external_closed" for c in batch.completions)


class RecordingNotifier:
    def __init__(self):
        self.statuses = []
        self.errors = []

    async def status(self, text):
        self.statuses.append(text)

    async def error(self, title, detail=""):
        self.errors.append((title, detail))


def test_healthcheck_probe_healthy_is_silent():
    conn = FakeConnector()
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=1),
        connector_factory=lambda: conn,
        notifier=notifier,
    )

    async def run():
        await ex._run_health_check()  # probe succeeds

    asyncio.run(run())
    assert notifier.statuses == [] and notifier.errors == []


def test_healthcheck_reconnects_and_alerts_on_probe_failure():
    # First connector's probe fails; factory hands out a fresh healthy one.
    bad = FakeConnector(fail_symbols=("BTC",))   # get_position ok, but make probe fail below
    good = FakeConnector()

    # Make the bad connector's probe raise.
    async def boom(symbol):
        raise ExchangeError("session expired", venue="variational")
    bad.get_position = boom  # type: ignore

    conns = [bad, good]
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=1),
        connector_factory=lambda: conns.pop(0),
        notifier=notifier,
    )

    async def run():
        await ex._ensure_connected()      # connects `bad`
        await ex._run_health_check()      # probe fails -> reconnect to `good`

    asyncio.run(run())
    assert ex._connector is good
    assert bad.closed  # old session torn down
    assert any("auto-reconnected" in s for s in notifier.statuses)
    assert notifier.errors == []


def test_healthcheck_alerts_when_reconnect_fails():
    bad = FakeConnector()

    async def boom(symbol):
        raise ExchangeError("session expired", venue="variational")
    bad.get_position = boom  # type: ignore

    class DeadConnector(FakeConnector):
        async def connect(self):
            raise RuntimeError("cloudflare challenge")

    conns = [bad, DeadConnector()]
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=1),
        connector_factory=lambda: conns.pop(0),
        notifier=notifier,
    )

    async def run():
        await ex._ensure_connected()
        await ex._run_health_check()

    asyncio.run(run())
    assert ex._session_healthy is False
    assert any("session DOWN" in t for t, _ in notifier.errors)


def test_healthcheck_prewarms_before_first_interval():
    """The session must be built at start, not lazily on the first order."""
    conn = FakeConnector()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=3600),  # long interval
        connector_factory=lambda: conn,
    )

    async def run():
        ex.start_healthcheck()
        await asyncio.sleep(0.05)   # far less than the 3600s interval
        connected = conn.connected
        await ex.stop_healthcheck()
        return connected

    # Connected immediately despite the long probe interval -> pre-warm happened.
    assert asyncio.run(run()) is True


def test_session_recycles_when_stale():
    """A long-lived browser session is rebuilt so its heap stays bounded."""
    first, second = FakeConnector(), FakeConnector()
    conns = [first, second]
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=60, api_recycle_hours=6),
        connector_factory=lambda: conns.pop(0),
    )

    async def run():
        await ex._ensure_connected()
        ex._connected_at -= 7 * 3600      # pretend the session is 7h old
        await ex._run_health_check()      # healthy probe -> should still recycle

    asyncio.run(run())
    assert ex._connector is second, "stale session should have been rebuilt"
    assert first.closed, "old browser must be torn down, not leaked"


def test_session_not_recycled_before_deadline():
    first = FakeConnector()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=60, api_recycle_hours=6),
        connector_factory=lambda: first,
    )

    async def run():
        await ex._ensure_connected()
        ex._connected_at -= 1 * 3600      # only 1h old
        await ex._run_health_check()

    asyncio.run(run())
    assert ex._connector is first and not first.closed


def test_recycle_disabled_by_zero_hours():
    first = FakeConnector()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=60, api_recycle_hours=0),
        connector_factory=lambda: first,
    )

    async def run():
        await ex._ensure_connected()
        ex._connected_at -= 100 * 3600
        await ex._run_health_check()

    asyncio.run(run())
    assert ex._connector is first and not first.closed


def test_recycle_skipped_while_order_in_flight():
    """Never rebuild the browser out from under a live entry/close."""
    first = FakeConnector()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=60, api_recycle_hours=6),
        connector_factory=lambda: first,
    )

    async def run():
        await ex._ensure_connected()
        ex._connected_at -= 99 * 3600
        ex._in_flight = 1
        await ex._recycle_if_stale()

    asyncio.run(run())
    assert ex._connector is first and not first.closed


def test_healthcheck_defers_while_order_in_flight():
    conn = FakeConnector()

    async def boom(symbol):
        raise ExchangeError("should not be called", venue="variational")
    conn.get_position = boom  # type: ignore

    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=1),
        connector_factory=lambda: conn,
    )

    async def run():
        ex._in_flight = 1  # pretend an order batch is running
        await ex._run_health_check()  # must skip probe entirely

    asyncio.run(run())
    # No reconnect attempted; connector never even created.
    assert ex._connector is None


def test_connect_is_lazy_and_reused():
    conn = FakeConnector()
    ex = _executor(conn)

    async def run():
        assert not conn.connected  # not connected until first use
        await ex.create_entry_requests(direction=PairDirection.LONG_BTC_SHORT_ETH, size_usd=10)
        assert conn.connected
        await ex.create_entry_requests(direction=PairDirection.LONG_BTC_SHORT_ETH, size_usd=10)
        await ex.aclose()
        assert conn.closed

    asyncio.run(run())


# -- failed connects must not leak; reconnects back off (2026-10-16 outage) ----

LOGIN_400 = 'POST /api/auth/login -> 400: {"message":"Invalid body"}'


class FakeClock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class LoginBrokenConnector(FakeConnector):
    """connect() gets as far as the browser, then the login is rejected."""

    async def connect(self):
        self.connected = True
        raise ExchangeError(LOGIN_400, venue="variational")


class ProbeBrokenConnector(FakeConnector):
    """Connects fine, but the keepalive probe always fails."""

    async def get_position(self, symbol):
        raise ExchangeError("GET /api/positions -> 500: upstream error", venue="variational")


def _recording_factory(make, clock=None):
    """Factory that remembers every connector it built (and when)."""
    created, times = [], []

    def factory():
        conn = make()
        created.append(conn)
        if clock is not None:
            times.append(clock.now - 1000.0)
        return conn

    return factory, created, times


def test_failed_recycle_closes_the_new_connector():
    """The outage path: the 6h recycle hit login 400 and its new browser leaked."""
    first, broken = FakeConnector(), LoginBrokenConnector()
    conns = [first, broken]
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=120, api_recycle_hours=6),
        connector_factory=lambda: conns.pop(0),
        notifier=notifier,
    )

    async def run():
        await ex._ensure_connected()
        ex._connected_at -= 7 * 3600
        await ex._run_health_check()   # probe ok -> recycle -> login 400

    asyncio.run(run())
    assert first.closed, "old session is torn down before the rebuild"
    assert broken.closed, "the half-connected new session must be closed, not leaked"
    assert ex._connector is None
    assert ex._session_healthy is False
    [(title, detail)] = notifier.errors
    assert "session DOWN" in title
    assert "recycle failed" in detail and "Invalid body" in detail


def test_failed_connects_back_off_exponentially_and_never_leak():
    clock = FakeClock()
    factory, created, attempt_times = _recording_factory(LoginBrokenConnector, clock)
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(
            api_healthcheck_sec=120, api_reconnect_base_sec=120, api_reconnect_max_sec=600,
        ),
        connector_factory=factory,
        notifier=notifier,
        clock=clock,
    )

    async def run():
        for _ in range(20):            # 20 keepalive ticks, 120s apart (40 min)
            await ex._run_health_check()
            clock.advance(120)

    asyncio.run(run())
    # 120 -> 240 -> 480 -> 600 (cap) -> 600: 6 attempts instead of 20 (or 40 before).
    assert attempt_times == [0, 120, 360, 840, 1440, 2040]
    assert all(c.closed for c in created), "every failed connector must be closed"
    assert ex._connector is None
    assert ex._consecutive_failures == 6
    assert len(notifier.errors) == 6
    assert all("connect failed" in d and "Invalid body" in d for _, d in notifier.errors)


def test_backoff_resets_once_the_probe_passes_again():
    clock = FakeClock()
    good = FakeConnector()
    conns = [LoginBrokenConnector(), LoginBrokenConnector(), good]
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=120, api_reconnect_base_sec=120),
        connector_factory=lambda: conns.pop(0),
        notifier=notifier,
        clock=clock,
    )

    async def run():
        await ex._run_health_check()   # fail 1 -> wait 120
        clock.advance(120)
        await ex._run_health_check()   # fail 2 -> wait 240
        clock.advance(120)
        await ex._run_health_check()   # still backing off: no attempt
        assert conns[0] is good
        clock.advance(120)
        await ex._run_health_check()   # connects + probe passes

    asyncio.run(run())
    assert ex._connector is good
    assert ex._consecutive_failures == 0 and ex._retry_not_before == 0.0
    assert ex._session_healthy is True
    assert any("recovered" in s for s in notifier.statuses)


def test_rebuilds_back_off_when_the_probe_keeps_failing_on_fresh_sessions():
    clock = FakeClock()
    factory, created, build_times = _recording_factory(ProbeBrokenConnector, clock)
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(
            api_healthcheck_sec=120, api_reconnect_base_sec=120, api_reconnect_max_sec=600,
        ),
        connector_factory=factory,
        notifier=RecordingNotifier(),
        clock=clock,
    )

    async def run():
        for _ in range(8):
            await ex._run_health_check()
            clock.advance(120)

    asyncio.run(run())
    # t=0 connect + rebuild, then rebuilds at 120, 360, 840 — not on every tick.
    assert build_times == [0, 0, 120, 360, 840]
    assert all(c.closed for c in created[:-1]), "replaced sessions are closed"
    assert ex._connector is created[-1] and not created[-1].closed


def test_down_alert_stays_short_with_playwright_call_logs():
    """Two multi-KB Playwright errors in one alert broke Telegram's 4096 limit."""
    call_log = "\nCall log:\n" + "\n".join(f"  - <launching> chrome --flag-{i}" for i in range(200))
    probe_err = RuntimeError("Page.evaluate: Target crashed" + call_log)
    launch_err = RuntimeError(
        "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton "
        "for your profile directory." + call_log
    )
    assert len(str(launch_err)) > 4096

    bad = FakeConnector()

    async def boom(symbol):
        raise probe_err
    bad.get_position = boom  # type: ignore

    class LaunchFails(FakeConnector):
        async def connect(self):
            raise launch_err

    conns = [bad, LaunchFails()]
    notifier = RecordingNotifier()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=120),
        connector_factory=lambda: conns.pop(0),
        notifier=notifier,
    )

    async def run():
        await ex._ensure_connected()
        await ex._run_health_check()

    asyncio.run(run())
    [(_, detail)] = notifier.errors
    assert len(detail) < 1000
    assert "probe error: Page.evaluate: Target crashed" in detail
    assert "Failed to create a ProcessSingleton" in detail
    assert "Call log" not in detail


def test_cancel_during_connect_closes_the_connector():
    """Stopping the bot mid-launch must not orphan the half-started browser."""

    class HangingConnector(FakeConnector):
        async def connect(self):
            self.connected = True
            await asyncio.Event().wait()   # e.g. a slow browser launch / page load

    conn = HangingConnector()
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_healthcheck_sec=3600),
        connector_factory=lambda: conn,
    )

    async def run():
        ex.start_healthcheck()           # pre-warm starts connecting right away
        await asyncio.sleep(0.01)
        assert conn.connected and not conn.closed
        await ex.stop_healthcheck()      # cancels the connect

    asyncio.run(run())
    assert conn.closed
    assert ex._connector is None


def test_order_path_connect_failure_closes_the_connector():
    broken = LoginBrokenConnector()
    ex = _executor(broken)

    async def run():
        try:
            await ex.create_entry_requests(direction=PairDirection.LONG_BTC_SHORT_ETH, size_usd=10)
        except ExchangeError as exc:
            return exc
        return None

    exc = asyncio.run(run())
    assert exc is not None and "Invalid body" in str(exc)
    assert broken.closed and ex._connector is None
    assert broken.orders == [], "no order may be sent without a session"
    # Orders are outside the keepalive backoff: no failure was counted.
    assert ex._consecutive_failures == 0


def test_backoff_delay_doubles_and_caps():
    ex = VariationalApiExecutor(
        settings=SimpleNamespace(api_reconnect_base_sec=120, api_reconnect_max_sec=1800),
        connector_factory=FakeConnector,
    )
    assert [ex._retry_delay(n) for n in range(1, 7)] == [120, 240, 480, 960, 1800, 1800]
    assert ex._retry_delay(10_000) == 1800   # no overflow after days of failures

    disabled = VariationalApiExecutor(
        settings=SimpleNamespace(api_reconnect_base_sec=0), connector_factory=FakeConnector,
    )
    assert disabled._retry_delay(5) == 0.0
