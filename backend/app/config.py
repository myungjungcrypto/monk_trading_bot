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
    result = await db.execute(select(Trade))
    trades = result.scalars().all()
    closed_trades = [t for t in trades if t.closed_at is not None]

    total_pnl = sum(t.net_pnl_usd or 0 for t in closed_trades)
    total_trades = len(trades)
    closed_count = len(closed_trades)
    open_count = total_trades - closed_count
    wins = sum(1 for t in closed_trades if (t.net_pnl_usd or 0) > 0)
    losses = sum(1 for t in closed_trades if (t.net_pnl_usd or 0) < 0)

    return {
        "total_trades": total_trades,
        "closed_trades": closed_count,
        "open_trades": open_count,
        "total_pnl_usd": round(total_pnl, 2),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / closed_count * 100, 1) if closed_count > 0 else 0,
    }


@trades_router.get("/analytics")
async def get_trade_analytics(
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """상세 거래 분석을 반환합니다 (승률, 평균 PNL, 모드별 분석 등)."""
    result = await db.execute(select(Trade).where(Trade.closed_at.isnot(None)))
    closed_trades = result.scalars().all()

    if not closed_trades:
        return {"message": "No closed trades found", "total_trades": 0}

    pnls = [t.net_pnl_usd or 0 for t in closed_trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    # 모드별 분석
    mode_stats = {}
    for t in closed_trades:
        mode = t.signal_mode or "unknown"
        if mode not in mode_stats:
            mode_stats[mode] = {"trades": 0, "wins": 0, "total_pnl": 0.0, "pnls": []}
        mode_stats[mode]["trades"] += 1
        pnl = t.net_pnl_usd or 0
        mode_stats[mode]["total_pnl"] += pnl
        mode_stats[mode]["pnls"].append(pnl)
        if pnl > 0:
            mode_stats[mode]["wins"] += 1

    mode_analysis = {}
    for mode, s in mode_stats.items():
        mode_pnls = s["pnls"]
        mode_analysis[mode] = {
            "trades": s["trades"],
            "wins": s["wins"],
            "losses": s["trades"] - s["wins"],
            "win_rate": round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0,
            "total_pnl": round(s["total_pnl"], 2),
            "avg_pnl": round(s["total_pnl"] / s["trades"], 2) if s["trades"] > 0 else 0,
            "best_trade": round(max(mode_pnls), 2) if mode_pnls else 0,
            "worst_trade": round(min(mode_pnls), 2) if mode_pnls else 0,
        }

    # 방향별 분석
    dir_stats = {}
    for t in closed_trades:
        d = t.direction or "unknown"
        if d not in dir_stats:
            dir_stats[d] = {"trades": 0, "wins": 0, "total_pnl": 0.0}
        dir_stats[d]["trades"] += 1
        pnl = t.net_pnl_usd or 0
        dir_stats[d]["total_pnl"] += pnl
        if pnl > 0:
            dir_stats[d]["wins"] += 1

    direction_analysis = {}
    for d, s in dir_stats.items():
        direction_analysis[d] = {
            "trades": s["trades"],
            "win_rate": round(s["wins"] / s["trades"] * 100, 1) if s["trades"] > 0 else 0,
            "total_pnl": round(s["total_pnl"], 2),
        }

    # 청산 사유별 분석
    reason_stats = {}
    for t in closed_trades:
        r = t.exit_reason or "unknown"
        if r not in reason_stats:
            reason_stats[r] = {"count": 0, "total_pnl": 0.0}
        reason_stats[r]["count"] += 1
        reason_stats[r]["total_pnl"] += (t.net_pnl_usd or 0)

    exit_analysis = {
        r: {"count": s["count"], "total_pnl": round(s["total_pnl"], 2)}
        for r, s in reason_stats.items()
    }

    # Z-score 구간별 승률
    zscore_buckets = {"1.0-1.5": [], "1.5-2.0": [], "2.0-2.5": [], "2.5-3.0": [], "3.0+": []}
    for t in closed_trades:
        z = abs(t.zscore_entry or 0)
        pnl = t.net_pnl_usd or 0
        if z < 1.5:
            zscore_buckets["1.0-1.5"].append(pnl)
        elif z < 2.0:
            zscore_buckets["1.5-2.0"].append(pnl)
        elif z < 2.5:
            zscore_buckets["2.0-2.5"].append(pnl)
        elif z < 3.0:
            zscore_buckets["2.5-3.0"].append(pnl)
        else:
            zscore_buckets["3.0+"].append(pnl)

    zscore_analysis = {}
    for bucket, bucket_pnls in zscore_buckets.items():
        if bucket_pnls:
            w = sum(1 for p in bucket_pnls if p > 0)
            zscore_analysis[bucket] = {
                "trades": len(bucket_pnls),
                "win_rate": round(w / len(bucket_pnls) * 100, 1),
                "avg_pnl": round(sum(bucket_pnls) / len(bucket_pnls), 2),
            }

    # Profit Factor
    total_wins_sum = sum(wins) if wins else 0
    total_losses_sum = abs(sum(losses)) if losses else 0
    profit_factor = round(total_wins_sum / total_losses_sum, 2) if total_losses_sum > 0 else float('inf')

    return {
        "total_trades": len(closed_trades),
        "win_rate": round(len(wins) / len(closed_trades) * 100, 1),
        "total_pnl": round(sum(pnls), 2),
        "avg_pnl_per_trade": round(sum(pnls) / len(pnls), 2),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else 0,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else 0,
        "best_trade": round(max(pnls), 2),
        "worst_trade": round(min(pnls), 2),
        "profit_factor": profit_factor,
        "by_mode": mode_analysis,
        "by_direction": direction_analysis,
        "by_exit_reason": exit_analysis,
        "by_zscore": zscore_analysis,
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
