"""PlaywrightTransport must never outlive a failed start (2026-10-16 outage).

A fake ``playwright.async_api`` module stands in for the real driver, so these
run without Playwright or a browser installed.
"""

import asyncio
import sys
import types

import pytest

from backend.bot.variational.api.api_http import PlaywrightTransport

PROFILE_LOCKED = (
    "BrowserType.launch_persistent_context: Failed to create a ProcessSingleton for "
    "your profile directory. This usually means that the profile is already in use "
    "by another instance of Chromium."
)


class FakePage:
    def __init__(self, log, goto):
        self.log = log
        self._goto = goto

    async def evaluate(self, script, arg=None):
        return "Mozilla/5.0 HeadlessChrome/148.0.0.0"

    async def goto(self, url, wait_until=None):
        self.log.append("goto")
        await self._goto()

    async def content(self):
        return "<html>omni</html>"

    async def wait_for_timeout(self, ms):
        pass


class FakeCdp:
    async def send(self, method, params=None):
        pass


class FakeContext:
    def __init__(self, log, goto, close_error=None):
        self.log = log
        self.pages = []
        self._goto = goto
        self._close_error = close_error

    async def add_init_script(self, script):
        pass

    async def new_page(self):
        return FakePage(self.log, self._goto)

    async def new_cdp_session(self, page):
        return FakeCdp()

    async def close(self):
        self.log.append("context.close")
        if self._close_error:
            raise self._close_error


class FakeChromium:
    def __init__(self, log, context, launch_error=None):
        self.log = log
        self._context = context
        self._launch_error = launch_error

    async def launch_persistent_context(self, user_data_dir, **kwargs):
        self.log.append("launch")
        if self._launch_error:
            raise self._launch_error
        return self._context


class FakePlaywright:
    def __init__(self, log, chromium):
        self.log = log
        self.chromium = chromium

    async def stop(self):
        self.log.append("pw.stop")


async def _ok():
    pass


def _install(monkeypatch, *, launch_error=None, goto=_ok, close_error=None):
    log: list = []
    context = FakeContext(log, goto, close_error)
    pw = FakePlaywright(log, FakeChromium(log, context, launch_error))

    class Starter:
        async def start(self):
            log.append("pw.start")
            return pw

    module = types.ModuleType("playwright.async_api")
    module.async_playwright = lambda: Starter()
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    return log


def _transport():
    return PlaywrightTransport(
        origin="https://omni.variational.io",
        user_agent="",
        headless=True,
        user_data_dir="/tmp/monk-test-profile",
    )


def _assert_released(t):
    assert t._pw is None and t._context is None and t._page is None and t._browser is None


def test_profile_lock_error_stops_the_driver(monkeypatch):
    log = _install(monkeypatch, launch_error=RuntimeError(PROFILE_LOCKED))
    t = _transport()
    with pytest.raises(RuntimeError, match="ProcessSingleton"):
        asyncio.run(t.astart())
    assert log == ["pw.start", "launch", "pw.stop"]
    _assert_released(t)


def test_page_load_failure_closes_browser_and_driver(monkeypatch):
    async def timeout():
        raise TimeoutError("Page.goto: Timeout 30000ms exceeded.")

    log = _install(monkeypatch, goto=timeout)
    t = _transport()
    with pytest.raises(TimeoutError):
        asyncio.run(t.astart())
    assert log == ["pw.start", "launch", "goto", "context.close", "pw.stop"]
    _assert_released(t)


def test_cancelled_start_still_cleans_up(monkeypatch):
    async def hang():
        await asyncio.Event().wait()

    log = _install(monkeypatch, goto=hang)
    t = _transport()

    async def run():
        task = asyncio.ensure_future(t.astart())
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(run())
    assert log[-2:] == ["context.close", "pw.stop"]
    _assert_released(t)


def test_aclose_stops_the_driver_even_if_the_browser_close_fails(monkeypatch):
    log = _install(monkeypatch, close_error=RuntimeError("Target page, context or browser has been closed"))
    t = _transport()

    async def run():
        await t.astart()
        assert "pw.stop" not in log            # a good start keeps the browser
        with pytest.raises(RuntimeError):
            await t.aclose()
        await t.aclose()                       # second close is a no-op

    asyncio.run(run())
    assert log.count("context.close") == 1
    assert log.count("pw.stop") == 1 and log[-1] == "pw.stop"
    _assert_released(t)
