"""VariationalConnector.connect() must close its transport when it fails.

On 2026-10-16 the login started returning 400 after the browser was already up;
connect() raised and the browser stayed open, holding the persistent profile.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.bot.variational.api import api_client
from backend.bot.variational.api.api_client import VariationalConnector
from backend.bot.variational.api.api_http import _ShimResponse
from backend.bot.variational.api.api_types import ExchangeError

SIWE_TEXT = (
    "omni.variational.io wants you to sign in with your Ethereum account:\n"
    "0x0000000000000000000000000000000000000000\n\nNonce: 1"
)


class FakeTransport:
    def __init__(self, *, fail_start=False, login_status=400):
        self.headers = {}
        self.fail_start = fail_start
        self.login_status = login_status
        self.closed = 0
        self.calls = []

    async def astart(self):
        if self.fail_start:
            raise RuntimeError("BrowserType.launch_persistent_context: ProcessSingleton")

    async def request(self, method, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/api/auth/generate_signing_data"):
            return _ShimResponse(200, SIWE_TEXT)
        if url.endswith("/api/auth/login"):
            if self.login_status == 200:
                return _ShimResponse(200, '{"token": "jwt"}')
            return _ShimResponse(self.login_status, '{"message":"Invalid body"}')
        return _ShimResponse(404, "")

    async def aclose(self):
        self.closed += 1


def _settings(private_key):
    return SimpleNamespace(
        transport="browser",
        impersonate="",
        api_base="https://omni.variational.io",
        user_agent="",
        cf_clearance="",
        browser_headless=True,
        browser_executable="",
        browser_user_data_dir="/tmp/monk-test-profile",
        endpoint_map_path=Path("/nonexistent/variational_endpoints.json"),
        private_key=private_key,
    )


@pytest.fixture
def throwaway_key():
    eth_account = pytest.importorskip("eth_account")
    return eth_account.Account.create().key.hex()


def _patch_transport(monkeypatch, transport):
    built = []

    def build(**kwargs):
        built.append(kwargs)
        return transport

    monkeypatch.setattr(api_client, "build_transport", build)
    return built


def test_login_rejection_closes_the_transport(monkeypatch, throwaway_key):
    transport = FakeTransport(login_status=400)
    _patch_transport(monkeypatch, transport)
    conn = VariationalConnector(_settings(throwaway_key))

    with pytest.raises(ExchangeError, match="Invalid body"):
        asyncio.run(conn.connect())
    assert transport.calls[-1].endswith("/api/auth/login")
    assert transport.closed == 1
    assert conn._client is None


def test_failed_transport_start_leaves_no_client(monkeypatch, throwaway_key):
    transport = FakeTransport(fail_start=True)
    _patch_transport(monkeypatch, transport)
    conn = VariationalConnector(_settings(throwaway_key))

    with pytest.raises(RuntimeError, match="ProcessSingleton"):
        asyncio.run(conn.connect())
    assert transport.closed == 1
    assert conn._client is None


def test_missing_key_fails_before_launching_a_browser(monkeypatch):
    built = _patch_transport(monkeypatch, FakeTransport())
    conn = VariationalConnector(_settings(""))

    with pytest.raises(ExchangeError, match="PRIVATE_KEY"):
        asyncio.run(conn.connect())
    assert built == []


def test_successful_connect_keeps_the_transport(monkeypatch, throwaway_key):
    transport = FakeTransport(login_status=200)
    _patch_transport(monkeypatch, transport)
    conn = VariationalConnector(_settings(throwaway_key))

    async def run():
        await conn.connect()
        assert transport.closed == 0
        assert transport.headers["authorization"] == "Bearer jwt"
        await conn.close()

    asyncio.run(run())
    assert transport.closed == 1 and conn._client is None
