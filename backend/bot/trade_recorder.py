"""
Trade Recorder — 거래 기록을 DB에 자동 저장.

엔진에서 거래 진입/청산 시 호출하여 trades 테이블에 기록합니다.
"""

import logging
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.models import Trade
from backend.bot.position_manager import PairTrade

logger = logging.getLogger(__name__)


class TradeRecorder:
    """DB에 거래를 기록하는 유틸리티."""

    def __init__(self, session_factory):
        self._session_factory = session_factory

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
                await db.commit()
                await db.refresh(db_trade)
                return db_trade.id
        except Exception as e:
            logger.error("Failed to record full trade: %s", e)
            return None
