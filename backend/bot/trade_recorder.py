"""
Trade Recorder — 거래 기록을 DB에 자동 저장.

엔진에서 거래 진입/청산 시 호출하여 trades 테이블에 기록합니다.
"""

import logging
from datetime import datetime, timezone
from typing import List, Optional

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import PnlSnapshot, Trade
from backend.bot.position_manager import PairTrade

logger = logging.getLogger(__name__)


class TradeRecorder:
    """DB에 거래를 기록하는 유틸리티."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

    async def fetch_open_trades(self, exchange: Optional[str] = None) -> List[Trade]:
        """청산되지 않은 DB 거래를 오래된 순서대로 가져옵니다."""
        try:
            async with self._session_factory() as db:
                query = select(Trade).where(Trade.closed_at.is_(None))
                if exchange:
                    query = query.where(Trade.exchange == exchange)
                query = query.order_by(Trade.opened_at.asc(), Trade.id.asc())
                result = await db.execute(query)
                return list(result.scalars().all())
        except Exception as e:
            logger.error("Failed to fetch open trades: %s", e)
            return []

    async def record_open(self, trade: PairTrade, signal_mode: str = "scalp") -> Optional[int]:
        """거래 진입 시 DB에 기록합니다. 반환: DB trade ID."""
        try:
            async with self._session_factory() as db:
                db_trade = Trade(
                    exchange=trade.exchange_name,
                    direction=trade.direction.value,
                    size_usd=trade.total_size_usd,
                    btc_entry=trade.btc_leg.entry_price,
                    eth_entry=trade.eth_leg.entry_price,
                    zscore_entry=trade.zscore_at_entry,
                    spread_entry=trade.spread_at_entry,
                    signal_mode=signal_mode,
                    opened_at=datetime.fromtimestamp(trade.opened_at, tz=timezone.utc),
                    fees_usd=trade.total_fees_usd,
                )
                db.add(db_trade)
                await db.commit()
                await db.refresh(db_trade)
                logger.info("Trade recorded to DB: id=%d trade_id=%s", db_trade.id, trade.trade_id)
                return db_trade.id
        except Exception as e:
            logger.error("Failed to record trade open: %s", e)
            return None

    async def record_close(
        self,
        db_trade_id: int,
        trade: PairTrade,
        exit_reason: str,
        open_positions: int = 0,
    ) -> bool:
        """거래 청산 시 DB 레코드를 업데이트합니다."""
        try:
            async with self._session_factory() as db:
                db_trade = await db.get(Trade, db_trade_id)
                if db_trade is None:
                    logger.warning("DB trade not found: id=%d", db_trade_id)
                    return False

                db_trade.closed_at = datetime.fromtimestamp(trade.closed_at, tz=timezone.utc)
                db_trade.pnl_usd = trade.total_pnl_usd
                db_trade.fees_usd = trade.total_fees_usd
                db_trade.net_pnl_usd = trade.net_pnl_usd
                db_trade.exit_reason = exit_reason
                await db.flush()
                await self._record_pnl_snapshot(db, open_positions=open_positions)
                await db.commit()
                logger.info(
                    "Trade close recorded: id=%d pnl=$%.2f reason=%s",
                    db_trade_id, trade.net_pnl_usd, exit_reason,
                )
                return True
        except Exception as e:
            logger.error("Failed to record trade close: %s", e)
            return False

    async def record_full(self, trade: PairTrade, exit_reason: str, signal_mode: str = "scalp") -> Optional[int]:
        """이미 닫힌 거래를 한 번에 기록합니다 (로그 복구용)."""
        try:
            async with self._session_factory() as db:
                db_trade = Trade(
                    exchange=trade.exchange_name,
                    direction=trade.direction.value,
                    size_usd=trade.total_size_usd,
                    btc_entry=trade.btc_leg.entry_price,
                    eth_entry=trade.eth_leg.entry_price,
                    zscore_entry=trade.zscore_at_entry,
                    spread_entry=trade.spread_at_entry,
                    signal_mode=signal_mode,
                    opened_at=datetime.fromtimestamp(trade.opened_at, tz=timezone.utc),
                    closed_at=datetime.fromtimestamp(trade.closed_at, tz=timezone.utc) if trade.closed_at else None,
                    pnl_usd=trade.total_pnl_usd,
                    fees_usd=trade.total_fees_usd,
                    net_pnl_usd=trade.net_pnl_usd,
                    exit_reason=exit_reason,
                )
                db.add(db_trade)
                await db.flush()
                if trade.closed_at:
                    await self._record_pnl_snapshot(db, open_positions=0)
                await db.commit()
                await db.refresh(db_trade)
                return db_trade.id
        except Exception as e:
            logger.error("Failed to record full trade: %s", e)
            return None

    async def record_pnl_snapshot(self, open_positions: int = 0) -> bool:
        """현재 DB 거래 기록 기준으로 PNL 스냅샷을 저장합니다."""
        try:
            async with self._session_factory() as db:
                await self._record_pnl_snapshot(db, open_positions=open_positions)
                await db.commit()
                return True
        except Exception as e:
            logger.error("Failed to record PNL snapshot: %s", e)
            return False

    async def _record_pnl_snapshot(self, db: AsyncSession, open_positions: int = 0) -> None:
        result = await db.execute(select(Trade).where(Trade.closed_at.isnot(None)))
        closed_trades = result.scalars().all()
        now = datetime.now(timezone.utc)
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

        cumulative_pnl = sum(t.net_pnl_usd or 0.0 for t in closed_trades)
        daily_pnl = sum(
            t.net_pnl_usd or 0.0
            for t in closed_trades
            if t.closed_at and self._as_utc(t.closed_at) >= day_start
        )
        db.add(PnlSnapshot(
            snapshot_at=now,
            cumulative_pnl=cumulative_pnl,
            daily_pnl=daily_pnl,
            open_positions=open_positions,
        ))

    @staticmethod
    def _as_utc(dt: datetime) -> datetime:
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
