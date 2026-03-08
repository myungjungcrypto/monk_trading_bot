"""
Config CRUD — 봇 설정 DB CRUD API 라우터.

웹 대시보드 Settings 페이지에서 파라미터를 저장/조회합니다.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import get_current_user, TokenData
from backend.app.models import BotConfig, Trade, PnlSnapshot

router = APIRouter(prefix="/api/config", tags=["config"])


# ── Pydantic 스키마 ──────────────────────────────────────

class ConfigUpdate(BaseModel):
    config_key: str
    config_val: Dict[str, Any]


class ConfigResponse(BaseModel):
    config_key: str
    config_val: Dict[str, Any]
    updated_at: Optional[str] = None


# ── DB 세션 의존성 (main.py에서 주입) ─────────────────────

_session_factory = None

def set_session_factory(factory):
    global _session_factory
    _session_factory = factory

async def get_db() -> AsyncSession:
    if _session_factory is None:
        raise HTTPException(500, "Database not configured")
    async with _session_factory() as session:
        yield session


# ── 설정 CRUD ────────────────────────────────────────────

@router.get("/", response_model=List[ConfigResponse])
async def get_all_configs(
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """모든 봇 설정을 조회합니다."""
    result = await db.execute(select(BotConfig))
    configs = result.scalars().all()
    return [
        ConfigResponse(
            config_key=c.config_key,
            config_val=c.config_val,
            updated_at=c.updated_at.isoformat() if c.updated_at else None,
        )
        for c in configs
    ]


@router.get("/{key}", response_model=ConfigResponse)
async def get_config(
    key: str,
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """특정 설정을 조회합니다."""
    result = await db.execute(select(BotConfig).where(BotConfig.config_key == key))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(404, f"Config key not found: {key}")
    return ConfigResponse(
        config_key=config.config_key,
        config_val=config.config_val,
        updated_at=config.updated_at.isoformat() if config.updated_at else None,
    )


@router.put("/")
async def upsert_config(
    data: ConfigUpdate,
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """설정을 생성하거나 업데이트합니다."""
    result = await db.execute(select(BotConfig).where(BotConfig.config_key == data.config_key))
    config = result.scalar_one_or_none()

    if config is None:
        config = BotConfig(
            config_key=data.config_key,
            config_val=data.config_val,
            updated_at=datetime.now(timezone.utc),
        )
        db.add(config)
    else:
        config.config_val = data.config_val
        config.updated_at = datetime.now(timezone.utc)

    await db.commit()
    return {"status": "ok", "config_key": data.config_key}


@router.delete("/{key}")
async def delete_config(
    key: str,
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """설정을 삭제합니다."""
    result = await db.execute(select(BotConfig).where(BotConfig.config_key == key))
    config = result.scalar_one_or_none()
    if config is None:
        raise HTTPException(404, f"Config key not found: {key}")
    await db.delete(config)
    await db.commit()
    return {"status": "deleted", "config_key": key}


# ── 거래 기록 조회 ───────────────────────────────────────

trades_router = APIRouter(prefix="/api/trades", tags=["trades"])


@trades_router.get("/")
async def get_trades(
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """거래 기록을 조회합니다."""
    result = await db.execute(
        select(Trade).order_by(Trade.id.desc()).offset(offset).limit(limit)
    )
    trades = result.scalars().all()
    return [t.to_dict() for t in trades]


@trades_router.get("/summary")
async def get_trade_summary(
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """거래 요약 통계를 반환합니다."""
    result = await db.execute(select(Trade).where(Trade.closed_at.isnot(None)))
    closed_trades = result.scalars().all()

    total_pnl = sum(t.net_pnl_usd or 0 for t in closed_trades)
    total_trades = len(closed_trades)
    wins = sum(1 for t in closed_trades if (t.net_pnl_usd or 0) > 0)
    losses = sum(1 for t in closed_trades if (t.net_pnl_usd or 0) < 0)

    return {
        "total_trades": total_trades,
        "total_pnl_usd": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / total_trades * 100, 1) if total_trades > 0 else 0,
    }


# ── PNL 스냅샷 ──────────────────────────────────────────

pnl_router = APIRouter(prefix="/api/pnl", tags=["pnl"])


@pnl_router.get("/")
async def get_pnl_history(
    limit: int = 100,
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """PNL 히스토리를 조회합니다."""
    result = await db.execute(
        select(PnlSnapshot).order_by(PnlSnapshot.id.desc()).limit(limit)
    )
    snapshots = result.scalars().all()
    return [s.to_dict() for s in reversed(list(snapshots))]
