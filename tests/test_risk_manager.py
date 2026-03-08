"""RiskManager 단위 테스트."""

import time

import pytest

from backend.bot.exchanges.base import PositionSide
from backend.bot.position_manager import LegInfo, PairDirection, PairTrade
from backend.bot.risk_manager import (
    ExitReason,
    RiskAction,
    RiskConfig,
    RiskManager,
)


def _make_trade(
    pnl_btc: float = 0.0,
    pnl_eth: float = 0.0,
    size_usd: float = 500.0,
    fees: float = 0.0,
    opened_at: float = None,
    trade_id: str = "test_1",
) -> PairTrade:
    """테스트용 PairTrade 생성."""
    return PairTrade(
        trade_id=trade_id,
        exchange_name="backpack",
        direction=PairDirection.LONG_BTC_SHORT_ETH,
        btc_leg=LegInfo(
            asset="BTC", side=PositionSide.LONG, size_usd=size_usd,
            unrealized_pnl=pnl_btc,
        ),
        eth_leg=LegInfo(
            asset="ETH", side=PositionSide.SHORT, size_usd=size_usd,
            unrealized_pnl=pnl_eth,
        ),
        opened_at=opened_at or time.time(),
        total_fees_usd=fees,
    )


class TestTakeProfit:
    def test_tp_hit(self):
        rm = RiskManager(RiskConfig(take_profit_pct=0.8))
        # PNL = $4+$5 = $9, size = $1000, pnl% = 0.9% > 0.8%
        trade = _make_trade(pnl_btc=4.0, pnl_eth=5.0)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.EXIT
        assert decision.reason == ExitReason.TAKE_PROFIT

    def test_tp_not_hit(self):
        rm = RiskManager(RiskConfig(take_profit_pct=0.8))
        # PNL = $1+$1 = $2, size = $1000, pnl% = 0.2% < 0.8%
        trade = _make_trade(pnl_btc=1.0, pnl_eth=1.0)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.HOLD


class TestStopLoss:
    def test_sl_hit(self):
        rm = RiskManager(RiskConfig(stop_loss_pct=-3.0))
        # PNL = -$20+-$15 = -$35, size = $1000, pnl% = -3.5% < -3.0%
        trade = _make_trade(pnl_btc=-20.0, pnl_eth=-15.0)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.EXIT
        assert decision.reason == ExitReason.STOP_LOSS


class TestTrailingStop:
    def test_trailing_triggers(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,  # TP를 높게 설정하여 안 걸리게
            trailing_stop_enabled=True,
            trailing_activate_at_pct=0.5,
            trailing_trail_pct=0.3,
        ))
        trade_id = "trail_1"
        # size_usd=100 each → total=200, so PNL/200*100 = pnl_pct

        # pnl=$2 → 1.0% → 활성화, peak = 1.0
        trade = _make_trade(pnl_btc=1.0, pnl_eth=1.0, size_usd=100, trade_id=trade_id)
        d1 = rm.evaluate(trade)
        assert d1.action == RiskAction.HOLD

        # pnl=$3 → 1.5% → peak 갱신 = 1.5
        trade2 = _make_trade(pnl_btc=1.5, pnl_eth=1.5, size_usd=100, trade_id=trade_id)
        d2 = rm.evaluate(trade2)
        assert d2.action == RiskAction.HOLD

        # pnl=$2 → 1.0% → drawdown = 1.5 - 1.0 = 0.5 >= trail 0.3 → 청산
        trade3 = _make_trade(pnl_btc=1.0, pnl_eth=1.0, size_usd=100, trade_id=trade_id)
        d3 = rm.evaluate(trade3)
        assert d3.action == RiskAction.EXIT
        assert d3.reason == ExitReason.TRAILING_STOP

    def test_trailing_not_activated(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,
            trailing_stop_enabled=True,
            trailing_activate_at_pct=0.5,
            trailing_trail_pct=0.3,
        ))
        # 0.2% < activate 0.5% → 트레일링 미활성화
        trade = _make_trade(pnl_btc=1.0, pnl_eth=1.0)
        d = rm.evaluate(trade)
        assert d.action == RiskAction.HOLD


class TestTimeout:
    def test_max_hold_time(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,
            stop_loss_pct=-5.0,
            max_hold_hours=24.0,
        ))
        # 25시간 전에 열림
        trade = _make_trade(opened_at=time.time() - 25 * 3600)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.EXIT
        assert decision.reason == ExitReason.TIMEOUT

    def test_within_hold_time(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,
            stop_loss_pct=-5.0,
            max_hold_hours=24.0,
        ))
        trade = _make_trade(opened_at=time.time() - 1 * 3600)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.HOLD


class TestZscoreRevert:
    def test_exit_on_revert_with_profit(self):
        rm = RiskManager(RiskConfig(take_profit_pct=5.0))
        trade = _make_trade(pnl_btc=2.0, pnl_eth=1.0)  # +0.3%
        decision = rm.evaluate(trade, zscore_reverted=True)
        assert decision.action == RiskAction.EXIT
        assert decision.reason == ExitReason.ZSCORE_REVERT

    def test_no_exit_on_revert_with_loss(self):
        rm = RiskManager(RiskConfig(take_profit_pct=5.0, stop_loss_pct=-5.0))
        trade = _make_trade(pnl_btc=-2.0, pnl_eth=-1.0)  # -0.3%
        decision = rm.evaluate(trade, zscore_reverted=True)
        # 손실 상태에서 z-score revert은 청산 안 함
        assert decision.action != RiskAction.EXIT or decision.reason != ExitReason.ZSCORE_REVERT


class TestDailyLossLimit:
    def test_daily_limit_blocks_entry(self):
        rm = RiskManager(RiskConfig(daily_loss_limit_usd=-200.0))
        rm.record_realized_pnl(-250.0)
        assert not rm.can_open_trade(0)

    def test_daily_limit_triggers_exit(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,
            stop_loss_pct=-5.0,
            daily_loss_limit_usd=-200.0,
        ))
        rm.record_realized_pnl(-250.0)
        trade = _make_trade()
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.EXIT
        assert decision.reason == ExitReason.DAILY_LIMIT


class TestMaxOpenTrades:
    def test_blocks_when_at_max(self):
        rm = RiskManager(RiskConfig(max_open_trades=3))
        assert rm.can_open_trade(3) is False
        assert rm.can_open_trade(2) is True


class TestAveragingAndReduction:
    def test_averaging_trigger(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,
            stop_loss_pct=-5.0,
            averaging_enabled=True,
            averaging_trigger_pct=-1.5,
        ))
        # PNL = -$10+-$8 = -$18, size=$1000, pnl% = -1.8% <= -1.5%
        trade = _make_trade(pnl_btc=-10.0, pnl_eth=-8.0)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.AVERAGING_DOWN

    def test_size_reduction_trigger(self):
        rm = RiskManager(RiskConfig(
            take_profit_pct=5.0,
            stop_loss_pct=-5.0,
            averaging_enabled=False,
            size_reduction_enabled=True,
            size_reduction_trigger_pct=-2.0,
        ))
        # PNL = -$12+-$10 = -$22, size=$1000, pnl% = -2.2% <= -2.0%
        trade = _make_trade(pnl_btc=-12.0, pnl_eth=-10.0)
        decision = rm.evaluate(trade)
        assert decision.action == RiskAction.SIZE_REDUCTION


class TestOnTradeClosed:
    def test_records_pnl_and_cleans_trailing(self):
        rm = RiskManager()
        rm._peak_pnl_pct["test_1"] = 1.5
        rm.on_trade_closed("test_1", -10.0)
        assert rm.daily_realized_pnl == pytest.approx(-10.0)
        assert "test_1" not in rm._peak_pnl_pct
