"""PositionManager 단위 테스트 (거래소 호출 없는 로직 테스트)."""

import time

import pytest

from backend.bot.exchanges.base import PositionSide
from backend.bot.position_manager import (
    LegInfo,
    PairDirection,
    PairTrade,
    PositionManager,
)


class TestPairTrade:
    def test_total_pnl(self):
        trade = PairTrade(
            trade_id="t1",
            exchange_name="backpack",
            direction=PairDirection.LONG_BTC_SHORT_ETH,
            btc_leg=LegInfo(asset="BTC", side=PositionSide.LONG, size_usd=500, unrealized_pnl=10.0),
            eth_leg=LegInfo(asset="ETH", side=PositionSide.SHORT, size_usd=500, unrealized_pnl=-3.0),
            opened_at=time.time(),
        )
        assert trade.total_pnl_usd == pytest.approx(7.0)

    def test_net_pnl_with_fees(self):
        trade = PairTrade(
            trade_id="t1",
            exchange_name="backpack",
            direction=PairDirection.LONG_BTC_SHORT_ETH,
            btc_leg=LegInfo(asset="BTC", side=PositionSide.LONG, size_usd=500, unrealized_pnl=10.0),
            eth_leg=LegInfo(asset="ETH", side=PositionSide.SHORT, size_usd=500, unrealized_pnl=-3.0),
            opened_at=time.time(),
            total_fees_usd=2.0,
        )
        assert trade.net_pnl_usd == pytest.approx(5.0)

    def test_pnl_pct(self):
        trade = PairTrade(
            trade_id="t1",
            exchange_name="backpack",
            direction=PairDirection.LONG_BTC_SHORT_ETH,
            btc_leg=LegInfo(asset="BTC", side=PositionSide.LONG, size_usd=500, unrealized_pnl=5.0),
            eth_leg=LegInfo(asset="ETH", side=PositionSide.SHORT, size_usd=500, unrealized_pnl=5.0),
            opened_at=time.time(),
        )
        # PNL = $10, size = $1000, pct = 1.0%
        assert trade.pnl_pct == pytest.approx(1.0)

    def test_zero_size_pnl_pct(self):
        trade = PairTrade(
            trade_id="t1",
            exchange_name="backpack",
            direction=PairDirection.LONG_BTC_SHORT_ETH,
            btc_leg=LegInfo(asset="BTC", side=PositionSide.LONG, size_usd=0),
            eth_leg=LegInfo(asset="ETH", side=PositionSide.SHORT, size_usd=0),
            opened_at=time.time(),
        )
        assert trade.pnl_pct == 0.0


class TestPositionManagerState:
    def test_initial_state(self):
        pm = PositionManager()
        assert not pm.has_open_position
        assert pm.open_trade_count == 0

    def test_calculate_quantity_btc(self):
        qty = PositionManager._calculate_quantity(500.0, 67000.0, "BTC")
        assert qty == pytest.approx(0.00746, abs=0.001)

    def test_calculate_quantity_eth(self):
        qty = PositionManager._calculate_quantity(500.0, 1970.0, "ETH")
        assert qty == pytest.approx(0.2538, abs=0.001)

    def test_calculate_quantity_zero_price(self):
        qty = PositionManager._calculate_quantity(500.0, 0.0, "BTC")
        assert qty == 0.0

    def test_get_summary_empty(self):
        pm = PositionManager()
        summary = pm.get_summary()
        assert summary["open_trades"] == 0
        assert summary["total_pnl_usd"] == 0
        assert summary["trades"] == []

    def test_virtual_pair_updates_pnl(self):
        pm = PositionManager()
        trade = pm.open_virtual_pair(
            exchange_name="virtual",
            direction=PairDirection.LONG_BTC_SHORT_ETH,
            size_usd=500,
            btc_price=100000,
            eth_price=2000,
            zscore=2.1,
            spread_pct=1.7,
        )

        assert trade is not None
        pm.update_virtual_positions("virtual", btc_price=101000, eth_price=1980)

        updated = pm.open_trades[trade.trade_id]
        assert updated.total_pnl_usd > 0
        assert updated.btc_leg.unrealized_pnl > 0
        assert updated.eth_leg.unrealized_pnl > 0

    def test_virtual_pair_applies_round_trip_costs(self):
        pm = PositionManager()
        trade = pm.open_virtual_pair(
            exchange_name="virtual",
            direction=PairDirection.SHORT_BTC_LONG_ETH,
            size_usd=500,
            btc_price=100000,
            eth_price=2000,
            taker_fee_bps=0,
            slippage_bps=1,
        )

        assert trade.total_fees_usd == pytest.approx(0.20)
        assert trade.net_pnl_usd == pytest.approx(-0.20)

        pm.update_virtual_positions("virtual", btc_price=99000, eth_price=2020)
        updated = pm.open_trades[trade.trade_id]
        assert updated.total_pnl_usd == pytest.approx(10.0)
        assert updated.net_pnl_usd == pytest.approx(9.80)

    def test_close_virtual_pair_moves_trade_to_closed_state(self):
        pm = PositionManager()
        trade = pm.open_virtual_pair(
            exchange_name="virtual",
            direction=PairDirection.SHORT_BTC_LONG_ETH,
            size_usd=500,
            btc_price=100000,
            eth_price=2000,
        )

        closed = pm.close_virtual_pair(trade.trade_id, reason="ZSCORE")
        assert closed is not None
        assert not closed.is_open
        assert pm.open_trade_count == 0
