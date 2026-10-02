"""Bot auto-restart / auto-resume tests (no network, no exchanges)."""

import asyncio

import pytest

from backend.app import main as app_main
from backend.app.bot_runtime import (
    BOT_RUNTIME_CONFIG_KEY,
    BotSupervisor,
    load_bot_runtime_state,
    save_bot_runtime_state,
)
from backend.app.models import create_async_session_factory, init_db


class FakeEngine:
    def __init__(self, name, fail=False):
        self.name = name
        self.fail = fail
        self.started = 0
        self.stopped = 0

    async def start(self):
        self.started += 1
        if self.fail:
            raise RuntimeError("loop blew up")

    async def stop(self):
        self.stopped += 1


def _supervisor(*, should_run, factory, sleeps, published, notes=None):
    async def sleep(delay):
        sleeps.append(delay)

    async def notify(text):
        if notes is not None:
            notes.append(text)

    return BotSupervisor(
        engine_factory=factory,
        should_run=should_run,
        on_engine=published.append,
        notify=notify,
        base_delay_sec=30,
        max_delay_sec=600,
        stable_reset_sec=3600,
        sleep=sleep,
        clock=lambda: 0.0,
    )


class TestBotSupervisor:
    def test_restarts_dead_engine_while_desired(self):
        runs = iter([True, True, True, True, False])
        built = []

        async def factory():
            engine = FakeEngine(f"e{len(built) + 1}")
            built.append(engine)
            return engine

        sleeps, published, notes = [], [], []
        sup = _supervisor(should_run=lambda: next(runs), factory=factory,
                          sleeps=sleeps, published=published, notes=notes)
        first = FakeEngine("e0", fail=True)
        asyncio.run(sup.run(first))

        assert first.started == 1 and first.stopped == 1
        assert [e.name for e in published] == ["e1", "e2"]
        assert all(e.started == 1 for e in built)
        assert sleeps == [30, 60]  # exponential backoff
        assert sup.restart_count == 2
        assert "auto-restarting" in notes[0]

    def test_no_restart_after_user_stop(self):
        async def factory():
            raise AssertionError("must not rebuild after an intentional stop")

        sleeps, published = [], []
        sup = _supervisor(should_run=lambda: False, factory=factory,
                          sleeps=sleeps, published=published)
        engine = FakeEngine("e0")
        asyncio.run(sup.run(engine))
        assert engine.started == 1 and sleeps == [] and published == []

    def test_stop_during_backoff_cancels_restart(self):
        runs = iter([True, False])  # desired at exit, cleared while sleeping

        async def factory():
            raise AssertionError("restart should have been cancelled")

        sleeps, published = [], []
        sup = _supervisor(should_run=lambda: next(runs), factory=factory,
                          sleeps=sleeps, published=published)
        asyncio.run(sup.run(FakeEngine("e0")))
        assert sleeps == [30] and published == []

    def test_factory_failure_retries_with_backoff(self):
        runs = iter([True, True, True, True, False])
        attempts = []

        async def factory():
            attempts.append(1)
            if len(attempts) == 1:
                raise RuntimeError("config load failed")
            return FakeEngine("ok")

        sleeps, published = [], []
        sup = _supervisor(should_run=lambda: next(runs), factory=factory,
                          sleeps=sleeps, published=published)
        asyncio.run(sup.run(FakeEngine("e0")))
        assert len(attempts) == 2
        assert sleeps == [30, 60]
        assert [e.name for e in published] == ["ok"]

    def test_backoff_caps_at_max(self):
        sup = _supervisor(should_run=lambda: False, factory=None, sleeps=[], published=[])
        assert [sup._delay(n) for n in range(1, 8)] == [30, 60, 120, 240, 480, 600, 600]


@pytest.fixture
def session_factory(tmp_path):
    sf, engine = create_async_session_factory(f"sqlite+aiosqlite:///{tmp_path / 'bot.db'}")
    asyncio.run(init_db(engine))
    yield sf
    asyncio.run(engine.dispose())


class TestRuntimeStatePersistence:
    def test_save_and_load(self, session_factory):
        req = {"trading_mode": "swing", "paper_trading": False}
        asyncio.run(save_bot_runtime_state(
            session_factory, desired_running=True, start_request=req, reason="user_start"))
        state = asyncio.run(load_bot_runtime_state(session_factory))
        assert state["desired_running"] is True
        assert state["start_request"] == req

        # Stopping keeps the last start request but flips the intent.
        asyncio.run(save_bot_runtime_state(session_factory, desired_running=False, reason="user_stop"))
        state = asyncio.run(load_bot_runtime_state(session_factory))
        assert state["desired_running"] is False
        assert state["start_request"] == req
        assert state["reason"] == "user_stop"


class TestAutoResumeOnStartup:
    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch):
        launched = []
        monkeypatch.setattr(app_main, "_read_kill_switch", lambda: {"active": False})
        monkeypatch.setattr(app_main, "_bot_engine", None)
        monkeypatch.setattr(app_main, "_bot_desired_running", False)
        monkeypatch.setattr(app_main, "_create_engine", lambda req, configs, sf: ("engine", req))
        monkeypatch.setattr(app_main, "_launch_supervised",
                            lambda engine, req, sf: launched.append((engine, req)))

        async def no_notify(_text):
            return None

        monkeypatch.setattr(app_main, "_notify_status", no_notify)
        self.launched = launched

    def test_resumes_last_running_bot_even_when_flat(self, session_factory):
        asyncio.run(save_bot_runtime_state(
            session_factory, desired_running=True,
            start_request={"trading_mode": "swing", "paper_trading": False}))
        asyncio.run(app_main._auto_resume_bot(session_factory))

        assert len(self.launched) == 1
        _engine, req = self.launched[0]
        assert req.trading_mode == "swing" and req.paper_trading is False
        assert app_main._bot_desired_running is True

    def test_stays_off_after_user_stop(self, session_factory):
        asyncio.run(save_bot_runtime_state(session_factory, desired_running=False))
        asyncio.run(app_main._auto_resume_bot(session_factory))
        assert self.launched == []

    def test_respects_disable_flag(self, session_factory, monkeypatch):
        monkeypatch.setenv("AUTO_RESUME_LAST_RUNNING", "false")
        asyncio.run(save_bot_runtime_state(session_factory, desired_running=True))
        asyncio.run(app_main._auto_resume_bot(session_factory))
        assert self.launched == []

    def test_kill_switch_blocks_resume(self, session_factory, monkeypatch):
        monkeypatch.setattr(app_main, "_read_kill_switch", lambda: {"active": True})
        asyncio.run(save_bot_runtime_state(session_factory, desired_running=True))
        asyncio.run(app_main._auto_resume_bot(session_factory))
        assert self.launched == []

    def test_runtime_key_is_ignored_by_runtime_config(self, session_factory):
        # bot_runtime lives in bot_config next to real settings; it must not
        # break building the engine config from that same map.
        asyncio.run(save_bot_runtime_state(session_factory, desired_running=True))

        async def load():
            async with session_factory() as db:
                return await app_main._load_config_map(db)

        configs = asyncio.run(load())
        assert BOT_RUNTIME_CONFIG_KEY in configs
        app_main._build_runtime_config(app_main.BotStartRequest(), configs)
