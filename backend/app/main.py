"""
FastAPI 엔트리포인트.

- /api/auth  — JWT 로그인
- /api/config — 봇 설정 CRUD
- /api/trades — 거래 기록
- /api/pnl   — PNL 히스토리
- /api/bot   — 봇 상태/제어
- /ws/dashboard — 실시간 대시보드 WebSocket
"""

import asyncio
import json
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import OAuth2PasswordRequestForm
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.auth import (
    TokenData,
    TokenResponse,
    create_access_token,
    get_current_user,
    hash_password,
    verify_password,
)
from backend.app.config import (
    pnl_router,
    router as config_router,
    set_session_factory,
    trades_router,
    get_db,
)
from backend.app.models import (
    Base,
    BotConfig as DbBotConfig,
    Trade as DbTrade,
    User,
    create_async_session_factory,
    init_db,
)
from backend.app.ws_broadcast import broadcaster

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ── 전역 상태 ────────────────────────────────────────────

_bot_engine = None
_bot_task: Optional[asyncio.Task] = None
_broadcast_task: Optional[asyncio.Task] = None
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _kill_switch_path() -> Path:
    configured = os.getenv("VARIATIONAL_BROWSER_KILL_SWITCH_PATH")
    if configured:
        path = Path(configured).expanduser()
        if path.is_absolute():
            return path
        return PROJECT_ROOT / path
    return PROJECT_ROOT / "tools" / "variational-browser" / "runtime" / "kill_switch.json"


def _read_kill_switch() -> Dict[str, Any]:
    path = _kill_switch_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {
                "active": bool(data.get("active")),
                "reason": data.get("reason") or "",
                "updated_at": data.get("updated_at") or "",
                "updated_by": data.get("updated_by") or "",
                "path": str(path),
            }
    except FileNotFoundError:
        pass
    except Exception as exc:
        logger.warning("Failed to read kill switch file %s: %s", path, exc)
    return {"active": False, "reason": "", "updated_at": "", "updated_by": "", "path": str(path)}


def _write_kill_switch(*, active: bool, reason: str = "", updated_by: str = "") -> Dict[str, Any]:
    path = _kill_switch_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "active": bool(active),
        "reason": reason or ("Emergency kill switch" if active else "Kill switch cleared"),
        "updated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "updated_by": updated_by,
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return {**payload, "path": str(path)}


# ── Lifespan ─────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """앱 시작/종료 시 DB 초기화 및 봇 관리."""
    # DB 초기화
    from backend.app.models import get_database_url
    db_url = get_database_url(async_mode=True)
    logger.info("Using database: %s", db_url.split("@")[-1] if "@" in db_url else db_url)
    session_factory, engine = create_async_session_factory(db_url)
    set_session_factory(session_factory)
    app.state.session_factory = session_factory  # 봇 엔진에서 사용
    await init_db(engine)

    # 기본 admin 사용자 생성 (없으면)
    async with session_factory() as db:
        result = await db.execute(select(User).where(User.username == "admin"))
        if result.scalar_one_or_none() is None:
            admin_pw = os.getenv("ADMIN_PASSWORD", "admin")
            admin = User(username="admin", hashed_password=hash_password(admin_pw))
            db.add(admin)
            await db.commit()
            logger.info("Default admin user created")

    logger.info("FastAPI backend started")
    global _broadcast_task
    _broadcast_task = asyncio.create_task(broadcaster.start_broadcast_loop())
    await _auto_resume_open_trades(session_factory)
    yield

    # 종료 시 봇 정지
    global _bot_engine, _bot_task
    if _bot_engine:
        await _bot_engine.stop()
    if _bot_task:
        _bot_task.cancel()
    if _broadcast_task:
        _broadcast_task.cancel()

    await engine.dispose()
    logger.info("FastAPI backend stopped")


# ── FastAPI 앱 ────────────────────────────────────────────

app = FastAPI(
    title="Monk Pair Trading Bot",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # 프로덕션에서는 도메인 제한
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 라우터 등록
app.include_router(config_router)
app.include_router(trades_router)
app.include_router(pnl_router)


# ── 인증 API ─────────────────────────────────────────────

@app.post("/api/auth/login", response_model=TokenResponse)
async def login(
    form: OAuth2PasswordRequestForm = Depends(),
    db: AsyncSession = Depends(get_db),
):
    """JWT 로그인."""
    result = await db.execute(select(User).where(User.username == form.username))
    user = result.scalar_one_or_none()

    if user is None or not verify_password(form.password, user.hashed_password):
        raise HTTPException(401, "Invalid username or password")

    token = create_access_token(user.username)
    return TokenResponse(access_token=token)


@app.get("/api/auth/me")
async def get_me(user: TokenData = Depends(get_current_user)):
    """현재 로그인된 사용자 정보."""
    return {"username": user.username}


# ── 봇 제어 API ──────────────────────────────────────────

class BotStartRequest(BaseModel):
    trading_mode: Optional[str] = None
    paper_trading: bool = True
    execution_mode: Optional[str] = None
    primary_exchange: Optional[str] = None


class KillSwitchRequest(BaseModel):
    active: bool = True
    reason: Optional[str] = None


async def _auto_resume_open_trades(session_factory) -> None:
    """백엔드 재시작 시 열린 DB 거래가 있으면 봇을 자동 재시작합니다."""
    enabled = os.getenv("AUTO_RESUME_OPEN_TRADES", "true").lower() not in {"0", "false", "no"}
    if not enabled:
        return

    if _read_kill_switch().get("active"):
        logger.warning("Auto-resume skipped because Variational kill switch is active")
        return

    global _bot_engine, _bot_task
    if _bot_engine and _bot_engine.is_running:
        return

    async with session_factory() as db:
        result = await db.execute(select(DbTrade).where(DbTrade.closed_at.is_(None)))
        open_trades = result.scalars().all()
        if not open_trades:
            return
        configs = await _load_config_map(db)

    from backend.bot.engine import BotEngine

    exchanges = _build_exchanges(configs)
    if not exchanges:
        logger.warning("Open DB trades exist, but no exchanges are configured; cannot auto-resume bot")
        return

    config = _build_runtime_config(BotStartRequest(), configs)
    _bot_engine = BotEngine(exchanges=exchanges, config=config)
    _bot_engine.set_session_factory(session_factory)
    _bot_engine.set_config_loader(_build_runtime_config_loader(session_factory))
    broadcaster.set_bot_engine(_bot_engine)
    _bot_task = asyncio.create_task(_bot_engine.start())
    logger.info("Auto-resuming bot with %d open DB trade(s)", len(open_trades))


async def _load_config_map(db: AsyncSession) -> Dict[str, Dict[str, Any]]:
    result = await db.execute(select(DbBotConfig))
    return {c.config_key: c.config_val or {} for c in result.scalars().all()}


def _mode_from_request(req: BotStartRequest, configs: Dict[str, Dict[str, Any]]) -> str:
    if req.trading_mode:
        return req.trading_mode
    mode_cfg = configs.get("mode", {})
    return str(mode_cfg.get("value") or os.getenv("TRADING_MODE", "swing"))


def _saved_mode(configs: Dict[str, Dict[str, Any]]) -> str:
    mode_cfg = configs.get("mode", {})
    return str(mode_cfg.get("value") or "").strip()


def _apply_attrs(obj, values: Dict[str, Any], allowed: set[str]) -> None:
    for key, value in values.items():
        if key in allowed and value is not None:
            setattr(obj, key, value)


def _exchange_cost_defaults(name: str) -> Dict[str, float]:
    defaults = {
        "lighter": {"taker_fee_bps": 0.0, "slippage_bps": 1.0},
        "pacifica": {"taker_fee_bps": 2.0, "slippage_bps": 1.0},
        "extended": {"taker_fee_bps": 2.0, "slippage_bps": 1.0},
        "backpack": {"taker_fee_bps": 6.0, "slippage_bps": 1.0},
    }
    return defaults.get(name, {"taker_fee_bps": 0.0, "slippage_bps": 1.0})


def _build_runtime_config(req: BotStartRequest, configs: Dict[str, Dict[str, Any]]):
    from backend.bot.engine import (
        BotConfig,
        EXECUTION_ALERT_ONLY,
        EXECUTION_LIVE,
    )
    from backend.bot.signal import MultiTFConfig
    from backend.bot.risk_manager import RiskConfig

    trading_mode = _mode_from_request(req, configs)
    saved_mode = _saved_mode(configs)
    use_saved_mode_params = not (req.trading_mode and saved_mode and saved_mode != trading_mode)
    if not use_saved_mode_params:
        logger.info(
            "Ignoring saved signal/exit config for requested mode=%s because saved mode=%s",
            trading_mode,
            saved_mode,
        )

    primary_exchange = (
        req.primary_exchange
        or configs.get("execution", {}).get("primary_exchange")
        or os.getenv("PRIMARY_EXCHANGE", "lighter")
    )
    execution_mode = (
        req.execution_mode
        or configs.get("execution", {}).get("mode")
        or os.getenv("EXECUTION_MODE", EXECUTION_ALERT_ONLY)
    )

    signal_db = configs.get("signal", {}) if use_saved_mode_params else {}
    exit_cfg = configs.get("exit", {}) if use_saved_mode_params else {}

    signal_cfg = MultiTFConfig.from_mode(trading_mode)
    _apply_attrs(
        signal_cfg,
        signal_db,
        {"z_window_5m", "entry_zscore", "max_zscore", "peak_revert_ratio"},
    )
    if "divergence_threshold_pct" in signal_db:
        db_div = float(signal_db["divergence_threshold_pct"])
        if db_div > 0:
            signal_cfg.divergence_threshold_pct = db_div
    if "divergence_lookback" in signal_db:
        db_lb = int(signal_db["divergence_lookback"])
        if db_lb >= 6:
            signal_cfg.divergence_lookback = db_lb

    if "zscore_revert_threshold" in exit_cfg:
        signal_cfg.zscore_revert_threshold = exit_cfg["zscore_revert_threshold"]

    risk_base = RiskConfig(
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.8")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-3.0")),
        max_hold_hours=float(os.getenv("MAX_HOLD_HOURS", "12")),
        max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
        daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT", "-200")),
        min_hold_minutes=float(os.getenv("MIN_HOLD_MINUTES", "120")),
        zscore_exit_min_pnl_pct=float(os.getenv("ZSCORE_EXIT_MIN_PNL_PCT", "0.0")),
    )
    _apply_attrs(
        risk_base,
        exit_cfg,
        {"take_profit_pct", "stop_loss_pct", "max_hold_hours", "zscore_exit_min_pnl_pct", "min_hold_minutes"},
    )
    _apply_attrs(
        risk_base,
        configs.get("risk", {}),
        {
            "max_open_trades",
            "daily_loss_limit_usd",
            "averaging_enabled",
            "averaging_trigger_pct",
            "averaging_multiplier",
            "size_reduction_enabled",
            "size_reduction_trigger_pct",
            "size_reduction_ratio",
        },
    )

    exchange_cfg = configs.get("exchanges", {}).get(primary_exchange, {})
    cost_defaults = _exchange_cost_defaults(primary_exchange)
    position_size = float(exchange_cfg.get("position_size_usd", os.getenv("POSITION_SIZE_USD", "500")))
    leverage = int(exchange_cfg.get("leverage", os.getenv("LEVERAGE", "3")))
    taker_fee_bps = float(
        exchange_cfg.get(
            "taker_fee_bps",
            os.getenv("TAKER_FEE_BPS", str(cost_defaults["taker_fee_bps"])),
        )
    )
    slippage_bps = float(
        exchange_cfg.get(
            "slippage_bps",
            os.getenv("SLIPPAGE_BPS", str(cost_defaults["slippage_bps"])),
        )
    )

    return BotConfig(
        position_size_usd=position_size,
        leverage=leverage,
        paper_trading=execution_mode != EXECUTION_LIVE,
        execution_mode=execution_mode,
        primary_exchange=primary_exchange,
        trading_mode=trading_mode,
        signal_config=signal_cfg,
        risk_config=risk_base,
        taker_fee_bps=taker_fee_bps,
        slippage_bps=slippage_bps,
    )


def _build_runtime_config_loader(session_factory):
    async def load_runtime_config():
        async with session_factory() as db:
            configs = await _load_config_map(db)
        return _build_runtime_config(BotStartRequest(), configs)

    return load_runtime_config


def _exchange_enabled(configs: Dict[str, Dict[str, Any]], name: str) -> bool:
    exchanges_cfg = configs.get("exchanges", {})
    if name not in exchanges_cfg:
        return True
    return bool(exchanges_cfg.get(name, {}).get("enabled", True))


def _build_exchanges(configs: Dict[str, Dict[str, Any]]):
    from backend.bot.exchanges.backpack import BackpackExchange
    from backend.bot.exchanges.extended import ExtendedExchange
    from backend.bot.exchanges.lighter import LighterExchange
    from backend.bot.exchanges.pacifica import PacificaExchange

    exchanges = {}

    if _exchange_enabled(configs, "lighter"):
        lt_key = os.getenv("LIGHTER_API_KEY", "")
        lt_secret = os.getenv("LIGHTER_SECRET_KEY", "")
        lt_private = os.getenv("LIGHTER_PRIVATE_KEY")
        lt_account = os.getenv("LIGHTER_ACCOUNT_INDEX")
        lt_key_index = os.getenv("LIGHTER_API_KEY_INDEX")
        # alert_only / paper 모드는 공개 ticker WebSocket만 필요하므로 키 없이도 초기화합니다.
        exchanges["lighter"] = LighterExchange(
            api_key=lt_key,
            secret_key=lt_secret,
            private_key=lt_private,
            account_index=int(lt_account) if lt_account else None,
            api_key_index=int(lt_key_index) if lt_key_index else None,
            btc_market_id=int(os.getenv("LIGHTER_BTC_MARKET_ID", "1")),
            eth_market_id=int(os.getenv("LIGHTER_ETH_MARKET_ID", "0")),
        )

    if _exchange_enabled(configs, "backpack"):
        bp_key = os.getenv("BACKPACK_API_KEY")
        bp_secret = os.getenv("BACKPACK_SECRET_KEY")
        if bp_key and bp_secret:
            exchanges["backpack"] = BackpackExchange(api_key=bp_key, secret_key=bp_secret)

    if _exchange_enabled(configs, "pacifica"):
        pac_key = os.getenv("PACIFICA_API_KEY")
        pac_secret = os.getenv("PACIFICA_SECRET_KEY")
        if pac_key and pac_secret:
            exchanges["pacifica"] = PacificaExchange(api_key=pac_key, secret_key=pac_secret)

    if _exchange_enabled(configs, "extended"):
        ext_key = os.getenv("EXTENDED_API_KEY")
        ext_secret = os.getenv("EXTENDED_SECRET_KEY")
        if ext_key and ext_secret:
            exchanges["extended"] = ExtendedExchange(api_key=ext_key, secret_key=ext_secret)

    return exchanges


@app.get("/api/bot/status")
async def bot_status(_user: TokenData = Depends(get_current_user)):
    """봇 상태를 조회합니다."""
    if _bot_engine is None:
        return {
            "running": False,
            "message": "Bot not initialized",
            "kill_switch": _read_kill_switch(),
        }
    status = _bot_engine.get_status()
    status["kill_switch"] = _read_kill_switch()
    return status


@app.post("/api/bot/start")
async def bot_start(
    req: BotStartRequest,
    db: AsyncSession = Depends(get_db),
    _user: TokenData = Depends(get_current_user),
):
    """봇을 시작합니다."""
    global _bot_engine, _bot_task

    if _bot_engine and _bot_engine.is_running:
        raise HTTPException(400, "Bot is already running")

    kill_switch = _read_kill_switch()
    if kill_switch.get("active"):
        raise HTTPException(423, "Emergency kill switch is active. Clear it before starting the bot.")

    from backend.bot.engine import BotEngine

    configs = await _load_config_map(db)
    exchanges = _build_exchanges(configs)
    if not exchanges:
        raise HTTPException(400, "No exchanges configured. Set API keys in .env")
    config = _build_runtime_config(req, configs)

    _bot_engine = BotEngine(exchanges=exchanges, config=config)
    # DB 세션 팩토리 연결 → 거래 기록 자동 저장
    sf = getattr(app, 'state', None) and getattr(app.state, 'session_factory', None)
    if sf is not None:
        _bot_engine.set_session_factory(sf)
        _bot_engine.set_config_loader(_build_runtime_config_loader(sf))
    broadcaster.set_bot_engine(_bot_engine)
    _bot_task = asyncio.create_task(_bot_engine.start())

    return {
        "status": "started",
        "mode": config.trading_mode,
        "execution_mode": config.execution_mode,
        "primary_exchange": config.primary_exchange,
        "paper": config.paper_trading,
        "costs": {
            "taker_fee_bps": config.taker_fee_bps,
            "slippage_bps": config.slippage_bps,
        },
    }


@app.post("/api/bot/test-telegram")
async def bot_test_telegram(_user: TokenData = Depends(get_current_user)):
    """텔레그램 알림 설정을 테스트합니다."""
    from backend.bot.telegram_notifier import TelegramNotifier

    notifier = TelegramNotifier.from_env()
    if notifier is None:
        raise HTTPException(400, "Telegram is not configured")
    try:
        await notifier.status("Telegram alert test")
    finally:
        await notifier.close()
    return {"status": "sent"}


@app.post("/api/bot/stop")
async def bot_stop(_user: TokenData = Depends(get_current_user)):
    """봇을 정지합니다."""
    global _bot_engine, _bot_task

    if _bot_engine is None or not _bot_engine.is_running:
        raise HTTPException(400, "Bot is not running")

    await _bot_engine.stop()
    if _bot_task:
        _bot_task.cancel()
        _bot_task = None

    return {"status": "stopped"}


@app.post("/api/bot/kill-switch")
async def bot_kill_switch(
    req: KillSwitchRequest,
    user: TokenData = Depends(get_current_user),
):
    """Emergency switch for stopping the bot and blocking browser clicks."""
    global _bot_engine, _bot_task

    state = _write_kill_switch(
        active=req.active,
        reason=req.reason or "",
        updated_by=user.username,
    )

    if req.active:
        logger.warning("Emergency kill switch activated by %s: %s", user.username, state["reason"])
        if _bot_engine and _bot_engine.is_running:
            await _bot_engine.stop()
        if _bot_task:
            _bot_task.cancel()
            _bot_task = None
        return {"status": "activated", "kill_switch": state}

    logger.info("Emergency kill switch cleared by %s", user.username)
    return {"status": "cleared", "kill_switch": state}


# ── 대시보드 WebSocket ───────────────────────────────────

@app.websocket("/ws/dashboard")
async def dashboard_ws(websocket: WebSocket):
    """대시보드 실시간 데이터 스트림."""
    await broadcaster.connect(websocket)
    try:
        while True:
            # 클라이언트로부터 ping/명령 수신 (연결 유지)
            data = await websocket.receive_text()
            if data == "ping":
                await websocket.send_text('{"type":"pong"}')
    except WebSocketDisconnect:
        broadcaster.disconnect(websocket)


# ── 헬스체크 ─────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "bot_running": _bot_engine.is_running if _bot_engine else False,
        "dashboard_clients": broadcaster.client_count,
    }
