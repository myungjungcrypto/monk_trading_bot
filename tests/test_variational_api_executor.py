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
