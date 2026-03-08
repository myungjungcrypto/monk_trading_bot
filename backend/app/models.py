"""
DB 모델 — SQLAlchemy ORM.

trades, pnl_snapshots, bot_config 테이블 정의.
"""

from datetime import datetime, timezone
from typing import Any, Dict, Optional

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker


class Base(DeclarativeBase):
    pass


class Trade(Base):
    """거래 기록."""
    __tablename__ = "trades"

    id = Column(Integer, primary_key=True, autoincrement=True)
    exchange = Column(String(20), nullable=False)
    direction = Column(String(30), nullable=False)  # LONG_BTC_SHORT_ETH
    size_usd = Column(Float, nullable=False)
    btc_entry = Column(Float)
    eth_entry = Column(Float)
    zscore_entry = Column(Float)
    spread_entry = Column(Float)
    signal_mode = Column(String(20))  # scalp / swing / position
    opened_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    closed_at = Column(DateTime(timezone=True), nullable=True)
    pnl_usd = Column(Float, nullable=True)
    fees_usd = Column(Float, default=0.0)
    net_pnl_usd = Column(Float, nullable=True)
    exit_reason = Column(String(50), nullable=True)  # TP/SL/ZSCORE/MANUAL/TIMEOUT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "exchange": self.exchange,
            "direction": self.direction,
            "size_usd": self.size_usd,
            "btc_entry": self.btc_entry,
            "eth_entry": self.eth_entry,
            "zscore_entry": self.zscore_entry,
            "spread_entry": self.spread_entry,
            "signal_mode": self.signal_mode,
            "opened_at": self.opened_at.isoformat() if self.opened_at else None,
            "closed_at": self.closed_at.isoformat() if self.closed_at else None,
            "pnl_usd": self.pnl_usd,
            "fees_usd": self.fees_usd,
            "net_pnl_usd": self.net_pnl_usd,
            "exit_reason": self.exit_reason,
        }


class PnlSnapshot(Base):
    """누적 PNL 스냅샷."""
    __tablename__ = "pnl_snapshots"

    id = Column(Integer, primary_key=True, autoincrement=True)
    snapshot_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))
    cumulative_pnl = Column(Float, default=0.0)
    daily_pnl = Column(Float, default=0.0)
    open_positions = Column(Integer, default=0)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "snapshot_at": self.snapshot_at.isoformat() if self.snapshot_at else None,
            "cumulative_pnl": self.cumulative_pnl,
            "daily_pnl": self.daily_pnl,
            "open_positions": self.open_positions,
        }


class BotConfig(Base):
    """봇 설정 (JSONB)."""
    __tablename__ = "bot_config"

    id = Column(Integer, primary_key=True, autoincrement=True)
    config_key = Column(String(100), unique=True, nullable=False)
    config_val = Column(JSON, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc),
                        onupdate=lambda: datetime.now(timezone.utc))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "config_key": self.config_key,
            "config_val": self.config_val,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


class User(Base):
    """대시보드 사용자."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(50), unique=True, nullable=False)
    hashed_password = Column(String(255), nullable=False)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


# ── DB 연결 ──────────────────────────────────────────────

def get_database_url(async_mode: bool = True) -> str:
    """환경변수에서 DB URL을 생성합니다. DB_URL이 없으면 SQLite 폴백."""
    import os

    # 명시적 DB_URL 환경변수 우선
    explicit_url = os.getenv("DATABASE_URL")
    if explicit_url:
        if async_mode and explicit_url.startswith("postgresql://"):
            return explicit_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        return explicit_url

    # PostgreSQL 설정이 있으면 사용
    host = os.getenv("DB_HOST")
    if host:
        port = os.getenv("DB_PORT", "5432")
        name = os.getenv("DB_NAME", "monk_bot")
        user = os.getenv("DB_USER", "monk")
        password = os.getenv("DB_PASSWORD", "monk")
        if async_mode:
            return f"postgresql+asyncpg://{user}:{password}@{host}:{port}/{name}"
        return f"postgresql://{user}:{password}@{host}:{port}/{name}"

    # 폴백: SQLite (로컬 개발용)
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "monk_bot.db")
    if async_mode:
        return f"sqlite+aiosqlite:///{db_path}"
    return f"sqlite:///{db_path}"


def create_async_session_factory(database_url: Optional[str] = None):
    """비동기 DB 세션 팩토리를 생성합니다."""
    url = database_url or get_database_url(async_mode=True)
    engine = create_async_engine(url, echo=False)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False), engine


async def init_db(engine) -> None:
    """테이블을 생성합니다."""
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
