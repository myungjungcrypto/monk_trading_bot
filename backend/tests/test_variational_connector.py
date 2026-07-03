"""Tests for the Variational connector.

The mocked flow mirrors the real one captured from the Omni web client
(HAR, 2026-07-03): server-provided SIWE text -> personal_sign (no 0x prefix)
-> JWT -> indicative quote (base-asset qty) -> market order with quote_id.

Uses respx to mock the HTTP backend and a well-known throwaway test key for
signing. No real network or funds are touched.
"""

import json
from decimal import Decimal

import httpx
import pytest
import respx

from bot.config import VariationalSettings
from bot.exchanges.base import Order, Side
from bot.exchanges.variational import VariationalConnector, _fmt_qty

# Public hardhat/anvil test account #0 — throwaway, never used with real funds.
TEST_KEY = "0xac0974bec39a17e36ba4a6b4d238ff944bacb478cbed5efcae784d7bf4f2ff80"
TEST_ADDR = "0xf39Fd6e51aad88F6F4ce6aB8827279cffFb92266"

API = "https://omni.variational.io"

SIGNING_MESSAGE = (
    f"omni.variational.io wants you to sign in with your Ethereum account:\n"
    f"{TEST_ADDR}\n\n"
    "I accept Variational's Terms of Service: https://docs.variational.io/legal/terms-of-service.\n\n"
    "URI: https://omni.variational.io/api/auth/login\n"
    "Version: 1\n"
    "Chain ID: 42161\n"
    "Nonce: testnonce123\n"
    "Issued At: 2026-07-03T10:00:00+00:00"
)


def _settings(**overrides) -> VariationalSettings:
    base = dict(
        private_key=TEST_KEY,
        api_base=API,
        chain_id=42161,
        endpoint_map="does-not-exist.json",  # use verified defaults
        dry_run=True,
        max_slippage=0.0005,
    )
    base.update(overrides)
    return VariationalSettings(**base)


def _quote(qty: str, quote_id: str, mark="2000", bid="1999", ask="2001") -> dict:
    return {
        "instrument": {
            "instrument_type": "perpetual_future",
            "underlying": "ETH",
            "funding_interval_s": 3600,
            "settlement_asset": "USDC",
        },
        "qty": qty,
        "bid": bid,
        "ask": ask,
        "mark_price": mark,
        "index_price": mark,
        "quote_id": quote_id,
    }


def _mock_auth():
    respx.post(f"{API}/api/auth/generate_signing_data").mock(
        return_value=httpx.Response(
            200, text=SIGNING_MESSAGE, headers={"content-type": "text/plain"}
        )
    )
    return respx.post(f"{API}/api/auth/login").mock(
        return_value=httpx.Response(200, json={"token": "session-jwt"})
    )


def test_derives_address_from_key():
    c = VariationalConnector(_settings())
    assert c._derive_address().lower() == TEST_ADDR.lower()


def test_personal_sign_has_no_0x_prefix():
    c = VariationalConnector(_settings())
    sig = c._personal_sign("hello")
    assert not sig.startswith("0x")
    assert len(sig) == 130  # 65-byte signature as unprefixed hex, as captured


def test_instrument_shape_matches_capture():
    c = VariationalConnector(_settings())
    assert c._instrument("eth") == {
        "underlying": "ETH",
        "instrument_type": "perpetual_future",
        "settlement_asset": "USDC",
        "funding_interval_s": 3600,
    }


def test_fmt_qty_never_uses_exponent():
    assert _fmt_qty(Decimal("0.250000000")) == "0.25"
    assert _fmt_qty(Decimal("1E+1")) == "10"
    assert _fmt_qty(Decimal("0.000000001")) == "0.000000001"


@pytest.mark.asyncio
@respx.mock
async def test_auth_flow_signs_server_message():
    login = _mock_auth()
    c = VariationalConnector(_settings())
    await c.connect()
    try:
        body = json.loads(login.calls.last.request.content)
        assert body["address"].lower() == TEST_ADDR.lower()
        assert not body["signed_message"].startswith("0x")
        assert len(body["signed_message"]) == 130
        # Session + identity headers installed for subsequent calls.
        assert c._client.headers["authorization"] == "Bearer session-jwt"
        assert c._client.headers["vr-connected-address"].lower() == TEST_ADDR.lower()
    finally:
        await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_place_order_dry_run_quotes_but_does_not_submit():
    _mock_auth()
    quote_calls = []

    def quote_side_effect(request):
        body = json.loads(request.content)
        quote_calls.append(body)
        return httpx.Response(200, json=_quote(body["qty"], f"q{len(quote_calls)}"))

    respx.post(f"{API}/api/quotes/indicative").mock(side_effect=quote_side_effect)
    submit = respx.post(f"{API}/api/orders/new/market").mock(
        return_value=httpx.Response(200, json={"rfq_id": "should-not-be-called"})
    )

    c = VariationalConnector(_settings(dry_run=True))
    await c.connect()
    try:
        result = await c.place_order(Order(symbol="ETH", side=Side.BUY, size_usd=Decimal("500")))
    finally:
        await c.close()

    # Discovery quote then real quote; qty converted at the mark price (500/2000).
    assert len(quote_calls) == 2
    assert quote_calls[0]["instrument"]["underlying"] == "ETH"
    assert quote_calls[1]["qty"] == "0.25"
    # Dry-run: order NOT submitted.
    assert not submit.called
    assert result.dry_run and result.accepted
    assert result.raw["submit_body"]["quote_id"] == "q2"
    assert result.raw["submit_body"]["side"] == "buy"
    assert result.raw["submit_body"]["max_slippage"] == 0.0005
    assert result.raw["submit_body"]["is_reduce_only"] is False
    assert result.filled_price == Decimal("2001")  # buy crosses the ask


@pytest.mark.asyncio
@respx.mock
async def test_place_order_live_submits_market_order():
    _mock_auth()
    n = 0

    def quote_side_effect(request):
        nonlocal n
        n += 1
        return httpx.Response(200, json=_quote(json.loads(request.content)["qty"], f"q{n}"))

    respx.post(f"{API}/api/quotes/indicative").mock(side_effect=quote_side_effect)
    submit = respx.post(f"{API}/api/orders/new/market").mock(
        return_value=httpx.Response(200, json={"rfq_id": "rfq-42", "take_profit_rfq_id": None})
    )

    c = VariationalConnector(_settings(dry_run=False))
    await c.connect()
    try:
        result = await c.place_order(Order(symbol="ETH", side=Side.SELL, size_usd=Decimal("500")))
    finally:
        await c.close()

    assert submit.called
    sent = json.loads(submit.calls.last.request.content)
    assert sent == {
        "quote_id": "q2",
        "side": "sell",
        "max_slippage": 0.0005,
        "is_reduce_only": False,
    }
    assert submit.calls.last.request.headers["authorization"] == "Bearer session-jwt"
    assert result.order_id == "rfq-42"
    assert result.filled_price == Decimal("1999")  # sell hits the bid


@pytest.mark.asyncio
@respx.mock
async def test_get_position_parses_capture_shape():
    _mock_auth()
    respx.get(f"{API}/api/positions").mock(
        return_value=httpx.Response(200, json=[
            {
                "position_info": {
                    "instrument": {
                        "instrument_type": "perpetual_future",
                        "underlying": "BTC",
                        "funding_interval_s": 3600,
                        "settlement_asset": "USDC",
                    },
                    "qty": "-0.5",
                    "avg_entry_price": "61889.85",
                },
                "upnl": "-0.0002372",
            },
            {
                "position_info": {
                    "instrument": {"underlying": "ETH"},
                    "qty": "0",
                    "avg_entry_price": "0",
                },
                "upnl": "0",
            },
        ])
    )
    c = VariationalConnector(_settings())
    await c.connect()
    try:
        btc = await c.get_position("BTC")
        assert btc is not None
        assert btc.side is Side.SELL          # negative qty = short
        assert btc.size == Decimal("0.5")
        assert btc.entry_price == Decimal("61889.85")
        assert await c.get_position("ETH") is None   # qty 0 = flat
        assert await c.get_position("SOL") is None   # not present
    finally:
        await c.close()


@pytest.mark.asyncio
@respx.mock
async def test_reauthenticates_on_401():
    login = _mock_auth()
    respx.get(f"{API}/api/positions").mock(
        side_effect=[
            httpx.Response(401, text="expired"),
            httpx.Response(200, json=[]),
        ]
    )
    c = VariationalConnector(_settings())
    await c.connect()
    try:
        assert await c.get_position("BTC") is None
    finally:
        await c.close()
    # Once at connect, once after the 401.
    assert login.call_count == 2


@pytest.mark.asyncio
@respx.mock
async def test_get_balance():
    _mock_auth()
    respx.get(f"{API}/api/portfolio").mock(
        return_value=httpx.Response(200, json={
            "margin_usage": {"initial_margin": "0.004688"},
            "balance": "1128.3268468",
            "upnl": "-0.0021762",
        })
    )
    c = VariationalConnector(_settings())
    await c.connect()
    try:
        bal = await c.get_balance()
    finally:
        await c.close()
    assert bal["balance"] == Decimal("1128.3268468")
    assert bal["upnl"] == Decimal("-0.0021762")
