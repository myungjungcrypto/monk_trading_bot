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
import logging
import os
from contextlib import asynccontextmanager
from typing import Optional

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
    yield

    # 종료 시 봇 정지
    global _bot_engine, _bot_task
    if _bot_engine:
        await _bot_engine.stop()
    if _bot_task:
        _bot_task.cancel()

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
    trading_mode: str = "swing"
    paper_trading: bool = True


@app.get("/api/bot/status")
async def bot_status(_user: TokenData = Depends(get_current_user)):
    """봇 상태를 조회합니다."""
    if _bot_engine is None:
        return {"running": False, "message": "Bot not initialized"}
    return _bot_engine.get_status()


@app.post("/api/bot/start")
async def bot_start(
    req: BotStartRequest,
    _user: TokenData = Depends(get_current_user),
):
    """봇을 시작합니다."""
    global _bot_engine, _bot_task

    if _bot_engine and _bot_engine.is_running:
        raise HTTPException(400, "Bot is already running")

    from backend.bot.engine import BotConfig, BotEngine
    from backend.bot.exchanges.backpack import BackpackExchange
    from backend.bot.signal import MultiTFConfig
    from backend.bot.risk_manager import RiskConfig
    from backend.app.models import BotConfig as BotConfigModel

    # DB에서 저장된 설정 로드
    db_signal = None
    db_exit = None
    db_risk_cfg = None
    db_exchanges_cfg = None
    try:
        async for db in get_db():
            result = await db.execute(select(BotConfigModel))
            configs = {c.config_key: c.config_val for c in result.scalars().all()}
            db_signal = configs.get("signal")
            db_exit = configs.get("exit")
            db_risk_cfg = configs.get("risk")
            db_exchanges_cfg = configs.get("exchanges")
            db_mode = configs.get("mode")
            if db_mode and db_mode.get("value"):
                req.trading_mode = db_mode["value"]
            break
    except Exception as e:
        logger.warning("Failed to load config from DB, using defaults: %s", e)

    # 거래소 초기화
    exchanges = {}

    # Backpack
    bp_key = os.getenv("BACKPACK_API_KEY")
    bp_secret = os.getenv("BACKPACK_SECRET_KEY")
    if bp_key and bp_secret:
        exchanges["backpack"] = BackpackExchange(api_key=bp_key, secret_key=bp_secret)

    # Pacifica
    pac_key = os.getenv("PACIFICA_API_KEY")
    pac_secret = os.getenv("PACIFICA_SECRET_KEY")
    if pac_key and pac_secret:
        from backend.bot.exchanges.pacifica import PacificaExchange
        exchanges["pacifica"] = PacificaExchange(api_key=pac_key, secret_key=pac_secret)

    # Extended
    ext_key = os.getenv("EXTENDED_API_KEY")
    ext_secret = os.getenv("EXTENDED_SECRET_KEY")
    if ext_key and ext_secret:
        from backend.bot.exchanges.extended import ExtendedExchange
        exchanges["extended"] = ExtendedExchange(api_key=ext_key, secret_key=ext_secret)

    # Lighter
    lt_key = os.getenv("LIGHTER_API_KEY")
    lt_secret = os.getenv("LIGHTER_SECRET_KEY")
    if lt_key and lt_secret:
        from backend.bot.exchanges.lighter import LighterExchange
        exchanges["lighter"] = LighterExchange(api_key=lt_key, secret_key=lt_secret)

    if not exchanges:
        raise HTTPException(400, "No exchanges configured. Set API keys in .env")

    # 시그널 설정: DB 값 우선, 없으면 모드별 프리셋
    signal_config = MultiTFConfig.from_mode(req.trading_mode)
    if db_signal:
        for key in ["z_window_5m", "entry_zscore", "max_zscore",
                     "divergence_threshold_pct", "divergence_lookback",
                     "peak_revert_ratio"]:
            if key in db_signal:
                val = db_signal[key]
                if key == "z_window_5m" or key == "divergence_lookback":
                    setattr(signal_config, key, int(val))
                else:
                    setattr(signal_config, key, float(val))
        logger.info("Signal config loaded from DB: %s", db_signal)

    # 리스크/청산 설정: DB 값 우선
    risk_config = RiskConfig(
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.8")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-3.0")),
        max_hold_hours=float(os.getenv("MAX_HOLD_HOURS", "24")),
        max_open_trades=int(os.getenv("MAX_OPEN_TRADES", "3")),
        daily_loss_limit_usd=float(os.getenv("DAILY_LOSS_LIMIT", "-200")),
    )
    if db_exit:
        if "take_profit_pct" in db_exit:
            risk_config.take_profit_pct = float(db_exit["take_profit_pct"])
        if "stop_loss_pct" in db_exit:
            risk_config.stop_loss_pct = float(db_exit["stop_loss_pct"])
        if "max_hold_hours" in db_exit:
            risk_config.max_hold_hours = float(db_exit["max_hold_hours"])
        if "zscore_revert_threshold" in db_exit:
            signal_config.zscore_revert_threshold = float(db_exit["zscore_revert_threshold"])
        logger.info("Exit config loaded from DB: %s", db_exit)
    if db_risk_cfg:
        if "max_open_trades" in db_risk_cfg:
            risk_config.max_open_trades = int(db_risk_cfg["max_open_trades"])
        if "daily_loss_limit_usd" in db_risk_cfg:
            risk_config.daily_loss_limit_usd = float(db_risk_cfg["daily_loss_limit_usd"])
        logger.info("Risk config loaded from DB: %s", db_risk_cfg)

    config = BotConfig(
        position_size_usd=float(os.getenv("POSITION_SIZE_USD", "500")),
        leverage=int(os.getenv("LEVERAGE", "3")),
        paper_trading=req.paper_trading,
        trading_mode=req.trading_mode,
        signal_config=signal_config,
        risk_config=risk_config,
    )

    _bot_engine = BotEngine(exchanges=exchanges, config=config)
    broadcaster.set_bot_engine(_bot_engine)
    _bot_task = asyncio.create_task(_bot_engine.start())

    return {"status": "started", "mode": req.trading_mode, "paper": req.paper_trading}


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
