"""Tests for the Variational connector.

Uses respx to mock the HTTP backend and a well-known throwaway test key for
signing. No real network or funds are touched. Requires httpx, respx,
eth-account, pydantic-settings (see requirements.txt).
"""

import json
from decimal import Decimal

import httpx
import pytest
import respx

from bot.config import VariationalSettings
from bot.exchanges.base import Order, Side
from bot.exchanges.variational import VariationalConnector

# Public hardhat/anvil test account #0 — throwaway, never used with real funds.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

API_BASE = "https://omni-client-api.prod.ap-northeast-1.variational.io"


def _settings(**overrides) -> VariationalSettings:
    base = dict(
        private_key=TEST_KEY,
        api_base=API_BASE,
        chain_id=42161,
        endpoint_map="does-not-exist.json",  # force documented fallbacks
        dry_run=True,
    )
    base.update(overrides)
    return VariationalSettings(**base)


def test_derives_address_from_key():
    c = VariationalConnector(_settings())
    assert c._derive_address().lower() == TEST_ADDR.lower()


def test_symbol_mapping():
    c = VariationalConnector(_settings())
    assert c._listing("BTC") == "BTC_USDC_PERP"
    assert c._listing("eth") == "ETH_USDC_PERP"
    assert c._listing("SOL") == "SOL"  # passthrough for unknown


def test_extract_supports_dotted_paths():
    c = VariationalConnector
    obj = {"data": {"token": "jwt"}, "nonce": "n1"}
    assert c._extract(obj, ("token", "data.token")) == "jwt"
    assert c._extract(obj, ("nonce",)) == "n1"
    assert c._extract(obj, ("missing",)) is None


def test_endpoint_map_overrides_fallback(tmp_path):
    map_file = tmp_path / "endpoints.json"
    map_file.write_text(json.dumps({
        "endpoints": {
            "rfq": [{"method": "POST", "path": "/v2/quote"}],
        }
    }))
    c = VariationalConnector(_settings(endpoint_map=str(map_file)))
    c._load_endpoint_map()
    assert c._endpoint("rfq") == ("POST", "/v2/quote")
    # Falls back to documented default when the map lacks the category.
    assert c._endpoint("order_submit") == ("POST", "/order")


@pytest.mark.asyncio
@respx.mock
async def test_place_order_dry_run_signs_but_does_not_submit():
    nonce = respx.get(f"{API_BASE}/auth/nonce").mock(
        return_value=httpx.Response(200, json={"nonce": "abc123"})
    )
    login = respx.post(f"{API_BASE}/auth/login").mock(
        return_value=httpx.Response(200, json={"token": "session-jwt"})
    )
    rfq = respx.post(f"{API_BASE}/rfq").mock(
        return_value=httpx.Response(200, json={"quote_id": "q1", "price": "95000.5"})
    )
    submit = respx.post(f"{API_BASE}/order").mock(
        return_value=httpx.Response(200, json={"order_id": "should-not-be-called"})
    )

    c = VariationalConnector(_settings(dry_run=True))
    await c.connect()
    try:
        result = await c.place_order(
            Order(symbol="BTC", side=Side.BUY, size_usd=Decimal("500"))
        )
    finally:
        await c.close()

    # Auth handshake happened, and the bearer token was attached.
    assert nonce.called and login.called
    # RFQ was requested (needed to build the order)...
    assert rfq.called
    # ...but in dry-run the order was NOT submitted.
    assert not submit.called
    assert result.dry_run is True
    assert result.accepted is True
    assert result.venue == "variational"
    assert result.raw["submit_body"]["listing"] == "BTC_USDC_PERP"
    assert result.raw["submit_body"]["quote_id"] == "q1"


@pytest.mark.asyncio
@respx.mock
async def test_place_order_live_submits_order():
    respx.get(f"{API_BASE}/auth/nonce").mock(
        return_value=httpx.Response(200, json={"nonce": "abc123"})
    )
    respx.post(f"{API_BASE}/auth/login").mock(
        return_value=httpx.Response(200, json={"token": "session-jwt"})
    )
    respx.post(f"{API_BASE}/rfq").mock(
        return_value=httpx.Response(200, json={"quote_id": "q1"})
    )
    submit = respx.post(f"{API_BASE}/order").mock(
        return_value=httpx.Response(200, json={"order_id": "o-42", "fill_price": "95001.0", "accepted": True})
    )

    c = VariationalConnector(_settings(dry_run=False))
    await c.connect()
    try:
        result = await c.place_order(
            Order(symbol="ETH", side=Side.SELL, size_usd=Decimal("500"))
        )
    finally:
        await c.close()

    assert submit.called
    assert result.dry_run is False
    assert result.order_id == "o-42"
    assert result.filled_price == Decimal("95001.0")
    # Authorization header propagated to the submit call.
    sent = submit.calls.last.request
    assert sent.headers["authorization"] == "Bearer session-jwt"


@pytest.mark.asyncio
@respx.mock
async def test_get_position_returns_none_when_flat():
    respx.get(f"{API_BASE}/auth/nonce").mock(return_value=httpx.Response(200, json={"nonce": "n"}))
    respx.post(f"{API_BASE}/auth/login").mock(return_value=httpx.Response(200, json={"token": "t"}))
    respx.get(f"{API_BASE}/positions").mock(
        return_value=httpx.Response(200, json={"positions": [
            {"listing": "ETH_USDC_PERP", "size": "0", "entry_price": "0"},
        ]})
    )
    c = VariationalConnector(_settings())
    await c.connect()
    try:
        assert await c.get_position("BTC") is None  # no BTC row
        assert await c.get_position("ETH") is None  # size 0
    finally:
        await c.close()
