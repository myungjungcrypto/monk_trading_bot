"""VariationalConnector login flow and connect() cleanup (2026-10-16 outage).

That morning the server started rejecting logins without the signing-data
nonce (400 {"message":"Invalid body"}). connect() then raised with the browser
already up and left it running, holding the persistent profile. The fake
transport below answers the way the live server did when probed that day.
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.bot.variational.api import api_client
from backend.bot.variational.api.api_client import VariationalConnector
from backend.bot.variational.api.api_http import _ShimResponse
from backend.bot.variational.api.api_types import ExchangeError

NONCE = "3f9c2a7d1b"
SIWE_TEXT = (
    "omni.variational.io wants you to sign in with your Ethereum account:\n"
    "0x0000000000000000000000000000000000000000\n\n"
    "Sign in to Variational.\n\n"
    "URI: https://omni.variational.io\nVersion: 1\nChain ID: 42161\n"
    f"Nonce: {NONCE}\nIssued At: 2026-10-16T03:48:56Z"
)


class FakeTransport:
    """Speaks the 2026-10-16 auth API: v2 signing data, login needs the nonce."""

    def __init__(self, *, fail_start=False, accept_login=True):
        self.headers = {}
        self.fail_start = fail_start
        self.accept_login = accept_login
        self.closed = 0
        self.calls = []
        self.login_bodies = []

    async def astart(self):
        if self.fail_start:
            raise RuntimeError("BrowserType.launch_persistent_context: ProcessSingleton")

    async def request(self, method, url, **kwargs):
        self.calls.append(url)
        if url.endswith("/api/auth/v2/generate_signing_data"):
            return _ShimResponse(200, json.dumps({"message": SIWE_TEXT, "nonce": NONCE}))
        if url.endswith("/api/auth/generate_signing_data"):
            return _ShimResponse(200, SIWE_TEXT)  # v1: bare text
        if url.endswith("/api/auth/login"):
            body = kwargs.get("json") or {}
            self.login_bodies.append(body)
            if "nonce" not in body:
                return _ShimResponse(400, '{"message":"Invalid body"}')
            if not self.accept_login:
                return _ShimResponse(401, '{"message":"Unknown Error"}')
            return _ShimResponse(200, '{"token": "jwt", "intercomUserJwt": "x"}')
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


def test_login_echoes_the_v2_nonce(monkeypatch, throwaway_key):
    transport = FakeTransport()
    _patch_transport(monkeypatch, transport)
    conn = VariationalConnector(_settings(throwaway_key))

    async def run():
        await conn.connect()
        await conn.close()

    asyncio.run(run())
    assert transport.calls[0].endswith("/api/auth/v2/generate_signing_data")
    [body] = transport.login_bodies
    assert body["nonce"] == NONCE
    assert body["address"] == conn._address
    sig = body["signed_message"]
    assert len(sig) == 130 and not sig.startswith("0x")
    assert transport.headers["authorization"] == "Bearer jwt"


def test_login_signature_recovers_to_the_wallet(monkeypatch, throwaway_key):
    eth_account = pytest.importorskip("eth_account")
    from eth_account.messages import encode_defunct

    transport = FakeTransport()
    _patch_transport(monkeypatch, transport)
    conn = VariationalConnector(_settings(throwaway_key))
    asyncio.run(conn.connect())

    [body] = transport.login_bodies
    signer = eth_account.Account.recover_message(
        encode_defunct(text=SIWE_TEXT), signature="0x" + body["signed_message"]
    )
    assert signer == conn._address


def test_bare_text_signing_data_uses_the_siwe_nonce_line(throwaway_key):
    conn = VariationalConnector(_settings(throwaway_key))
    assert conn._signing_data(SIWE_TEXT) == (SIWE_TEXT, NONCE)
    assert conn._signing_data({"message": SIWE_TEXT, "nonce": "n-1"}) == (SIWE_TEXT, "n-1")


def test_signing_data_without_a_nonce_fails_clearly(throwaway_key):
    conn = VariationalConnector(_settings(throwaway_key))
    no_nonce = SIWE_TEXT.replace(f"Nonce: {NONCE}\n", "")
    with pytest.raises(ExchangeError, match="no nonce"):
        conn._signing_data({"message": no_nonce})
    with pytest.raises(ExchangeError, match="Unexpected signing-data"):
        conn._signing_data({"nonce": NONCE})


def test_login_rejection_closes_the_transport(monkeypatch, throwaway_key):
    transport = FakeTransport(accept_login=False)
    _patch_transport(monkeypatch, transport)
    conn = VariationalConnector(_settings(throwaway_key))

    with pytest.raises(ExchangeError, match="401"):
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
