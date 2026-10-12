"""Bot run-state persistence and in-process supervisor.

Two gaps this closes:

1. Backend restart while flat. ``_auto_resume_open_trades`` only restarted the
   bot when an open DB trade existed, so a restart while flat left the bot off
   with nothing but a ``BOT STOPPED`` message. The user's intent ("the bot should
   be running") is now persisted in ``bot_config`` under ``bot_runtime`` and
   honoured on startup.

2. Engine dying inside a live backend. ``BotEngine.start()`` returns (after
   calling ``stop()``) whenever one of its loops raises, and nothing restarted
   it. ``BotSupervisor`` wraps the engine task: if the engine ends while the
   user still wants it running, it rebuilds a fresh engine from the DB config
   and starts it again with exponential backoff.

The user-facing stop paths (``/api/bot/stop``, kill switch) clear the desired
state first, so the supervisor never fights an intentional stop. Backend
shutdown sets ``shutting_down`` but keeps the persisted desired state, so the
next process start resumes.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable, Dict, Optional

from sqlalchemy import select

from backend.app.models import BotConfig as DbBotConfig

logger = logging.getLogger(__name__)

BOT_RUNTIME_CONFIG_KEY = "bot_runtime"


def env_flag(name: str, default: bool = True) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() not in {"0", "false", "no", "off"}


def env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


# ── persisted desired state ─────────────────────────────────


async def load_bot_runtime_state(session_factory) -> Dict[str, Any]:
    async with session_factory() as db:
        result = await db.execute(
            select(DbBotConfig).where(DbBotConfig.config_key == BOT_RUNTIME_CONFIG_KEY)
        )
        row = result.scalar_one_or_none()
        return dict(row.config_val or {}) if row is not None else {}


async def save_bot_runtime_state(
    session_factory,
    *,
    desired_running: bool,
    start_request: Optional[Dict[str, Any]] = None,
    reason: str = "",
) -> None:
    """Persist whether the bot should be running (and how it was started).

    Never raises: losing this record only degrades auto-resume, it must not
    break the start/stop endpoints that call it.
    """
    try:
        async with session_factory() as db:
            result = await db.execute(
                select(DbBotConfig).where(DbBotConfig.config_key == BOT_RUNTIME_CONFIG_KEY)
            )
            row = result.scalar_one_or_none()
            value = dict(row.config_val or {}) if row is not None else {}
            value["desired_running"] = bool(desired_running)
            if start_request is not None:
                value["start_request"] = start_request
            value["reason"] = reason
            value["updated_at"] = datetime.now(timezone.utc).isoformat()
            if row is None:
                db.add(DbBotConfig(config_key=BOT_RUNTIME_CONFIG_KEY, config_val=value))
            else:
                # Reassign a new dict so SQLAlchemy sees the JSON column change.
                row.config_val = value
                row.updated_at = datetime.now(timezone.utc)
            await db.commit()
    except Exception:  # noqa: BLE001
        logger.warning("Failed to persist bot runtime state", exc_info=True)


# ── supervisor ──────────────────────────────────────────────


class BotSupervisor:
    """Runs a BotEngine and restarts it if it stops while it should be running.

    ``engine_factory`` builds a fresh engine (re-reading DB config) for each
    restart. ``should_run`` is checked after the engine ends and again after the
    backoff sleep, so a user stop during the backoff cancels the restart.
    ``on_engine`` lets the caller publish the current engine (globals,
    dashboard broadcaster). ``notify`` sends a Telegram status line.
    """

    def __init__(
        self,
        *,
        engine_factory: Callable[[], Awaitable[Any]],
        should_run: Callable[[], bool],
        on_engine: Callable[[Any], None],
        notify: Optional[Callable[[str], Awaitable[None]]] = None,
        base_delay_sec: Optional[float] = None,
        max_delay_sec: Optional[float] = None,
        stable_reset_sec: Optional[float] = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._engine_factory = engine_factory
        self._should_run = should_run
        self._on_engine = on_engine
        self._notify = notify
        self._base_delay = base_delay_sec if base_delay_sec is not None else env_float(
            "BOT_AUTO_RESTART_BASE_DELAY_SEC", 30.0)
        self._max_delay = max_delay_sec if max_delay_sec is not None else env_float(
            "BOT_AUTO_RESTART_MAX_DELAY_SEC", 600.0)
        # An engine that ran this long counts as healthy; backoff starts over.
        self._stable_reset = stable_reset_sec if stable_reset_sec is not None else env_float(
            "BOT_AUTO_RESTART_STABLE_RESET_SEC", 3600.0)
        self._sleep = sleep
        self._clock = clock
        self.restart_count = 0

    def _delay(self, attempt: int) -> float:
        return min(self._base_delay * (2 ** max(attempt - 1, 0)), self._max_delay)

    async def _send(self, text: str) -> None:
        logger.warning(text)
        if self._notify is None:
            return
        try:
            await self._notify(text)
        except Exception:  # noqa: BLE001
            logger.warning("Supervisor notify failed", exc_info=True)

    async def run(self, engine: Any) -> None:
        attempt = 0
        while True:
            started_at = self._clock()
            if engine is not None:
                try:
                    await engine.start()
                except asyncio.CancelledError:
                    raise
                except Exception:  # noqa: BLE001
                    logger.exception("Bot engine crashed")
                try:
                    await engine.stop()  # idempotent; makes sure loops/sessions are closed
                except Exception:  # noqa: BLE001
                    logger.warning("Engine stop after exit failed", exc_info=True)

            if not self._should_run():
                return
            if engine is not None and self._clock() - started_at >= self._stable_reset:
                attempt = 0
            attempt += 1
            delay = self._delay(attempt)
            await self._send(
                f"Bot engine stopped unexpectedly — auto-restarting in {delay:.0f}s "
                f"(attempt {attempt}). Press Stop on the dashboard to cancel."
            )
            await self._sleep(delay)
            if not self._should_run():
                return

            try:
                engine = await self._engine_factory()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.exception("Failed to build bot engine for auto-restart")
                await self._send(f"Bot auto-restart failed to build engine: {exc}")
                engine = None
                continue
            self.restart_count += 1
            self._on_engine(engine)
            logger.info("Bot engine auto-restarted (attempt %d)", attempt)
