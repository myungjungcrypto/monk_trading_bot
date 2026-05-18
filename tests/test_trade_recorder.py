"""TradeRecorder DB 기록 테스트."""

import asyncio
import time

import pytest
from sqlalchemy import select

pytest.importorskip("greenlet")

from backend.app.models import PnlSnapshot, Trade, create_async_session_factory, init_db
from backend.bot.exchanges.base import PositionSide
from backend.bot.position_manager import LegInfo, PairDirection, PairTrade
from backend.bot.trade_recorder import TradeRecorder


def _make_closed_trade() -> PairTrade:
    opened_at = time.time() - 600
    trade = PairTrade(
        trade_id="virtual_1",
        exchange_name="virtual",
        direction=PairDirection.LONG_BTC_SHORT_ETH,
        btc_leg=LegInfo(
            asset="BTC",
            side=PositionSide.LONG,
            size_usd=500,
            entry_price=100000,
            current_price=101000,
            unrealized_pnl=5.0,
        ),
        eth_leg=LegInfo(
            asset="ETH",
            side=PositionSide.SHORT,
            size_usd=500,
            entry_price=2000,
            current_price=1980,
            unrealized_pnl=5.0,
        ),
        opened_at=opened_at,
        zscore_at_entry=2.1,
        spread_at_entry=1.4,
    )
    trade.closed_at = opened_at + 300
    trade.is_open = False
    return trade


def test_record_close_creates_trade_and_pnl_snapshot(tmp_path):
    async def run():
        session_factory, engine = create_async_session_factory(
            f"sqlite+aiosqlite:///{tmp_path}/recorder.db"
        )
        try:
            await init_db(engine)
            recorder = TradeRecorder(session_factory)
            trade = _make_closed_trade()

            db_id = await recorder.record_open(trade, signal_mode="swing")
            assert db_id is not None
            assert await recorder.record_close(db_id, trade, "ZSCORE", open_positions=0)

            async with session_factory() as db:
                trades = (await db.execute(select(Trade))).scalars().all()
                snapshots = (await db.execute(select(PnlSnapshot))).scalars().all()

            assert len(trades) == 1
            assert trades[0].exchange == "virtual"
            assert trades[0].net_pnl_usd == pytest.approx(10.0)
            assert trades[0].exit_reason == "ZSCORE"
            assert len(snapshots) == 1
            assert snapshots[0].cumulative_pnl == pytest.approx(10.0)
            assert snapshots[0].open_positions == 0
        finally:
            await engine.dispose()

    asyncio.run(run())
