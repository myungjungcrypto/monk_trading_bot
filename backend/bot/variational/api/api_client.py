"""Variational Omni direct-API connector.

Why this exists
---------------
The bot previously placed Variational orders by driving Chrome and clicking like
a human. That is slow: each click round-trips through the DOM, so the two legs of
a pair trade (BTC and ETH) open seconds apart and the intended market-neutral
entry is already skewed by the time both fills land. This connector talks to the
same JSON backend the web client uses, so both legs can be fired concurrently
with millisecond-level skew instead of seconds.

The actual API (verified against a browser HAR capture, 2026-07-03)
-------------------------------------------------------------------
Base URL: https://omni.variational.io (the app host; all paths under /api).

Auth — wallet signature, session token afterwards:
  1. POST /api/auth/generate_signing_data  {"address": "0x..."}
     -> text/plain SIWE message (server builds it, incl. nonce + 60s expiry).
  2. personal_sign that exact text with the trading wallet key.
  3. POST /api/auth/login  {"address": "0x...", "signed_message": "<hex, NO 0x>"}
     -> {"token": "<JWT>", ...}
  The web client also sends a `vr-connected-address: <address>` header on every
  request. The HAR was cookie/authorization-sanitized by Chrome, so whether the
  JWT travels as `Authorization: Bearer` or a cookie is not directly visible; we
  send the bearer header, keep any cookies httpx receives, and re-authenticate
  automatically on a 401.

Trading — RFQ then market order, NO per-order wallet signature:
  1. POST /api/quotes/indicative
       {"instrument": {"underlying": "ETH", "instrument_type": "perpetual_future",
                       "settlement_asset": "USDC", "funding_interval_s": 3600},
        "qty": "<base-asset qty, string>"}
     -> {"quote_id": "...", "bid": "...", "ask": "...", "mark_price": "...", ...}
  2. POST /api/orders/new/market
       {"quote_id": "...", "side": "buy"|"sell", "max_slippage": 0.0002,
        "is_reduce_only": false}
     -> {"rfq_id": "...", ...}
  Quotes expire quickly (the web client re-quotes every ~1s), so we submit
  immediately after quoting. Order qty is in the BASE ASSET; the connector
  converts from USD notional using the mark price of a discovery quote.

Positions / account:
  GET /api/positions                      -> list with position_info{instrument,
                                             qty (signed), avg_entry_price}, upnl
  GET /api/portfolio?compute_margin=true  -> {"balance": "...", "upnl": "..."}
  GET /api/funding/v2?underlying=X&instrument_type=perpetual_future

Safety
------
``VARIATIONAL_DRY_RUN=true`` (the default) runs the full auth + quote flow but
logs the final order request instead of POSTing it. Flip it off only after the
dry-run output looks right.

Operational note: the host sits behind Cloudflare. If server-side requests get
challenged (403 from cdn-cgi), the fallback is the documented client-API host —
see VARIATIONAL_API_BASE in .env.
"""

from __future__ import annotations

import json
import logging
from decimal import ROUND_DOWN, Decimal
from typing import Any, Optional

from backend.bot.variational.api.api_config import (
    VariationalSettings,
    get_variational_settings,
)
from backend.bot.variational.api.api_http import build_transport
from backend.bot.variational.api.api_types import (
    BaseExchange,
    ExchangeError,
    Order,
    OrderResult,
    Position,
    Side,
)

logger = logging.getLogger(__name__)


# Verified against HAR capture 2026-07-03. An endpoint map produced by
# tools/har_extractor.py can still override these (e.g. after a frontend update).
_DEFAULT_PATHS: dict[str, tuple[str, str]] = {
    "auth_signing_data": ("POST", "/api/auth/generate_signing_data"),
    "auth_login": ("POST", "/api/auth/login"),
    "rfq": ("POST", "/api/quotes/indicative"),
    "order_submit": ("POST", "/api/orders/new/market"),
    "position": ("GET", "/api/positions"),
    "portfolio": ("GET", "/api/portfolio"),
    "funding": ("GET", "/api/funding/v2"),
}

# Small base-asset qty used for price-discovery quotes (indicative only, no order).
_DISCOVERY_QTY = Decimal("0.001")

# Fallback qty tick if a quote omits qty_limits (BTC perp tick observed = 1e-6).
_DEFAULT_QTY_TICK = Decimal("0.000001")


def _fmt_qty(qty: Decimal) -> str:
    """Format a Decimal qty without exponent or trailing zeros ("0.25", not "2.5E-1")."""
    s = format(qty, "f")
    if "." in s:
        s = s.rstrip("0").rstrip(".")
    return s or "0"


class VariationalConnector(BaseExchange):
    name = "variational"

    def __init__(self, settings: Optional[VariationalSettings] = None):
        self.settings = settings or get_variational_settings()
        self._client = None  # transport (curl_cffi or httpx), set in connect()
        self._endpoint_map: dict[str, Any] = {}
        self._session_token: Optional[str] = None
        self._address: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        if self._client is None:
            self._client = build_transport(
                transport=self.settings.transport,
                impersonate=self.settings.impersonate,
                origin=self.settings.api_base.rstrip("/"),
                user_agent=self.settings.user_agent,
                cf_clearance=self.settings.cf_clearance,
                headless=self.settings.browser_headless,
                executable_path=self.settings.browser_executable,
                user_data_dir=self.settings.browser_user_data_dir,
            )
            # Browser transport needs async startup (launch + Cloudflare clear).
            if hasattr(self._client, "astart"):
                await self._client.astart()
        self._load_endpoint_map()
        self._address = self._derive_address()
        # The web client stamps every request with the connected address.
        self._client.headers["vr-connected-address"] = self._address
        await self.authenticate()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None
        self._session_token = None

    def _url(self, path: str) -> str:
        """Absolute URL for an endpoint path (transports don't share a base_url)."""
        if path.startswith("http"):
            return path
        return self.settings.api_base.rstrip("/") + path

    # -- endpoint map -------------------------------------------------------
    def _load_endpoint_map(self) -> None:
        path = self.settings.endpoint_map_path
        if not path.exists():
            self._endpoint_map = {}
            return
        with open(path, "r", encoding="utf-8") as fh:
            self._endpoint_map = json.load(fh)
        logger.info("Loaded Variational endpoint map from %s", path)

    def _endpoint(self, category: str) -> tuple[str, str]:
        """Resolve (method, url_path) for a logical action from the map or default."""
        entries = (self._endpoint_map.get("endpoints") or {}).get(category)
        if entries:
            first = entries[0]
            return first.get("method", "POST"), first.get("path") or first.get("url")
        if category in _DEFAULT_PATHS:
            return _DEFAULT_PATHS[category]
        raise ExchangeError(f"No endpoint known for '{category}'.", venue=self.name)

    # -- signing ------------------------------------------------------------
    def _account(self):
        if not self.settings.private_key or self.settings.private_key.strip("0x") == "":
            raise ExchangeError("VARIATIONAL_PRIVATE_KEY is not set.", venue=self.name)
        try:
            from eth_account import Account
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ExchangeError(
                "eth-account is required for signing. `pip install eth-account`.",
                venue=self.name,
            ) from exc
        return Account.from_key(self.settings.private_key)

    def _derive_address(self) -> str:
        return self._account().address

    def _personal_sign(self, message: str) -> str:
        """personal_sign the message; return hex WITHOUT the 0x prefix.

        The captured login request carries a 130-char unprefixed hex signature.
        """
        from eth_account.messages import encode_defunct

        acct = self._account()
        signed = acct.sign_message(encode_defunct(text=message))
        sig = signed.signature.hex()
        return sig[2:] if sig.startswith("0x") else sig

    # -- auth ----------------------------------------------------------------
    async def authenticate(self) -> None:
        """Sign the server-provided SIWE message and exchange it for a session JWT.

        The message has a ~60s expiry and embeds a nonce, so we fetch it fresh and
        sign immediately. It also embeds Variational's ToS-acceptance wording —
        the same message a human signs in the wallet popup on first login.
        """
        method, path = self._endpoint("auth_signing_data")
        resp = await self._request(
            method, path, json={"address": self._address}, _retry_auth=False
        )
        # The endpoint returns text/plain; tolerate a JSON wrapper too.
        message = resp.get("message") if isinstance(resp, dict) else resp
        if not isinstance(message, str) or "sign in with your Ethereum account" not in message:
            raise ExchangeError(
                "Unexpected signing-data response; frontend API may have changed.",
                venue=self.name,
                payload=resp,
            )

        signature = self._personal_sign(message)

        method, path = self._endpoint("auth_login")
        login = await self._request(
            method,
            path,
            json={"address": self._address, "signed_message": signature},
            _retry_auth=False,
        )
        token = self._extract(login, ("token", "access_token"))
        if not token:
            raise ExchangeError("Login returned no session token.", venue=self.name, payload=login)
        self._session_token = token
        # Chrome sanitizes cookies/authorization out of HAR exports, so the exact
        # transport of the JWT is unverified. Bearer is the standard guess; any
        # Set-Cookie the server sends is kept by httpx automatically.
        assert self._client is not None
        self._client.headers["authorization"] = f"Bearer {token}"
        logger.info("Variational session established for %s", self._address)

    # -- market data ----------------------------------------------------------
    def _instrument(self, symbol: str) -> dict[str, Any]:
        # funding_interval_s=3600 confirmed for BTC and ETH perps in the capture.
        return {
            "underlying": symbol.upper(),
            "instrument_type": "perpetual_future",
            "settlement_asset": "USDC",
            "funding_interval_s": 3600,
        }

    async def request_quote(self, symbol: str, qty: Decimal) -> dict[str, Any]:
        """POST an indicative RFQ; the response carries quote_id/bid/ask/mark."""
        method, path = self._endpoint("rfq")
        body = {"instrument": self._instrument(symbol), "qty": _fmt_qty(qty)}
        resp = await self._request(method, path, json=body)
        if not isinstance(resp, dict) or "quote_id" not in resp:
            raise ExchangeError("Unexpected RFQ response shape.", venue=self.name, payload=resp)
        return resp

    async def get_mark_price(self, symbol: str) -> Decimal:
        quote = await self.request_quote(symbol, _DISCOVERY_QTY)
        price = self._as_decimal(quote.get("mark_price"))
        if price is None:
            raise ExchangeError(f"No mark price for {symbol}.", venue=self.name, payload=quote)
        return price

    @staticmethod
    def _qty_limits(quote: dict[str, Any], side: Side) -> tuple[Decimal, Decimal]:
        """(min_qty_tick, min_qty) for the relevant book side from a quote.

        A BUY crosses the ask, a SELL hits the bid. The venue rejects an order
        whose qty isn't a multiple of min_qty_tick, or below min_qty.
        """
        book = "ask" if side is Side.BUY else "bid"
        limits = ((quote.get("qty_limits") or {}).get(book)) or {}
        tick = VariationalConnector._as_decimal(limits.get("min_qty_tick")) or _DEFAULT_QTY_TICK
        min_qty = VariationalConnector._as_decimal(limits.get("min_qty")) or Decimal(0)
        return tick, min_qty

    @staticmethod
    def _floor_to_tick(qty: Decimal, tick: Decimal) -> Decimal:
        if tick <= 0:
            return qty
        steps = (qty / tick).to_integral_value(rounding=ROUND_DOWN)
        return steps * tick

    # -- trading --------------------------------------------------------------
    async def place_order(self, order: Order) -> OrderResult:
        """Discovery quote (USD -> base qty) -> real quote -> market order.

        Quotes expire in seconds, so the submit follows the quote immediately.
        """
        # One discovery quote gives us both the mark price and the qty limits.
        disc = await self.request_quote(order.symbol, _DISCOVERY_QTY)
        mark = self._as_decimal(disc.get("mark_price"))
        if mark is None or mark <= 0:
            raise ExchangeError(f"No mark price for {order.symbol}.", venue=self.name, payload=disc)

        tick, min_qty = self._qty_limits(disc, order.side)
        # Closes pass the exact base-asset qty; entries convert USD via the mark.
        raw_qty = order.size_base if order.size_base is not None else (order.size_usd / mark)
        qty = self._floor_to_tick(raw_qty, tick)
        if qty < min_qty or qty <= 0:
            raise ExchangeError(
                f"size_usd {order.size_usd} -> qty {qty} for {order.symbol} is below the "
                f"venue minimum ({min_qty}, tick {tick}) at mark {mark}. Increase size_usd.",
                venue=self.name,
            )
        quote = await self.request_quote(order.symbol, qty)
        result = await self._submit_market(order, quote)
        result.filled_qty = qty
        return result

    async def _submit_market(self, order: Order, quote: dict[str, Any]) -> OrderResult:
        # Buys cross the ask, sells hit the bid — best estimate of the fill.
        est_price = self._as_decimal(
            quote.get("ask") if order.side is Side.BUY else quote.get("bid")
        )
        body = {
            "quote_id": quote["quote_id"],
            "side": order.side.value,
            "max_slippage": float(self.settings.max_slippage),
            "is_reduce_only": order.reduce_only,
        }
        method, path = self._endpoint("order_submit")

        if self.settings.dry_run:
            logger.info("[DRY-RUN] %s %s body=%s", method, path, json.dumps(body))
            return OrderResult(
                accepted=True, venue=self.name, symbol=order.symbol, side=order.side,
                size_usd=order.size_usd, filled_price=est_price, dry_run=True,
                raw={"submit_body": body, "quote": quote},
            )

        resp = await self._request(method, path, json=body)
        rfq_id = self._extract(resp, ("rfq_id", "order_id", "id"))
        return OrderResult(
            accepted=rfq_id is not None,
            venue=self.name,
            symbol=order.symbol,
            side=order.side,
            size_usd=order.size_usd,
            filled_price=est_price,
            order_id=rfq_id,
            raw=resp if isinstance(resp, dict) else {"raw": resp},
        )

    async def get_position(self, symbol: str) -> Optional[Position]:
        method, path = self._endpoint("position")
        resp = await self._request(method, path)
        if not isinstance(resp, list):
            raise ExchangeError("Unexpected positions response.", venue=self.name, payload=resp)
        for row in resp:
            info = row.get("position_info") or {}
            instrument = info.get("instrument") or {}
            if instrument.get("underlying") != symbol.upper():
                continue
            qty = self._as_decimal(info.get("qty")) or Decimal(0)
            if qty == 0:
                return None
            return Position(
                symbol=symbol.upper(),
                side=Side.BUY if qty > 0 else Side.SELL,
                size=abs(qty),
                entry_price=self._as_decimal(info.get("avg_entry_price")) or Decimal(0),
                unrealized_pnl=self._as_decimal(row.get("upnl")) or Decimal(0),
                raw=row,
            )
        return None

    async def close_position(self, symbol: str) -> Optional[OrderResult]:
        """Flatten using the exact base-asset qty from the venue (no USD roundtrip)."""
        pos = await self.get_position(symbol)
        if pos is None:
            return None
        quote = await self.request_quote(symbol, pos.size)
        notional = pos.size * (self._as_decimal(quote.get("mark_price")) or pos.entry_price)
        order = Order(
            symbol=symbol,
            side=pos.side.opposite,
            size_usd=notional,
            reduce_only=True,
        )
        return await self._submit_market(order, quote)

    async def get_balance(self) -> dict[str, Decimal]:
        """USDC balance and account-level uPnL from the portfolio endpoint."""
        method, path = self._endpoint("portfolio")
        resp = await self._request(method, path, params={"compute_margin": "true"})
        return {
            "balance": self._as_decimal(self._extract(resp, ("balance",))) or Decimal(0),
            "upnl": self._as_decimal(self._extract(resp, ("upnl",))) or Decimal(0),
        }

    # -- HTTP plumbing ------------------------------------------------------
    async def _request(
        self, method: str, path: str, *, _retry_auth: bool = True, **kwargs: Any
    ) -> Any:
        if self._client is None:
            raise ExchangeError("Connector not connected; call connect().", venue=self.name)
        try:
            resp = await self._client.request(method, self._url(path), **kwargs)
        except Exception as exc:  # httpx.HTTPError or curl_cffi.CurlError
            raise ExchangeError(f"HTTP error on {method} {path}: {exc}", venue=self.name) from exc

        # Cloudflare interstitial — surface a clear, actionable message.
        if resp.status_code in (403, 503) and self._is_cloudflare_challenge(resp):
            raise ExchangeError(
                f"{method} {path} -> {resp.status_code}: blocked by Cloudflare challenge. "
                "Use curl_cffi impersonation (VARIATIONAL_IMPERSONATE=chrome, "
                "`pip install curl_cffi`) or inject a cf_clearance cookie "
                "(see tools/cf_bootstrap.py).",
                venue=self.name,
            )

        # Session JWTs expire; transparently re-login once and retry.
        if resp.status_code == 401 and _retry_auth:
            logger.info("401 on %s %s — re-authenticating.", method, path)
            await self.authenticate()
            return await self._request(method, path, _retry_auth=False, **kwargs)

        if resp.status_code >= 400:
            raise ExchangeError(
                f"{method} {path} -> {resp.status_code}: {resp.text[:300]}",
                venue=self.name,
                payload=resp.text,
            )
        if not resp.content:
            return {}
        try:
            return resp.json()
        except ValueError:
            # e.g. generate_signing_data returns text/plain.
            return resp.text

    # -- helpers ------------------------------------------------------------
    @staticmethod
    def _is_cloudflare_challenge(resp: Any) -> bool:
        try:
            text = resp.text
        except Exception:
            return False
        markers = ("Just a moment", "cf-challenge", "cdn-cgi/challenge", "Attention Required")
        return any(m in text for m in markers)

    @staticmethod
    def _as_decimal(value: Any) -> Optional[Decimal]:
        if value is None or value == "":
            return None
        try:
            return Decimal(str(value))
        except (ValueError, ArithmeticError):
            return None

    @staticmethod
    def _extract(obj: Any, keys: tuple[str, ...]) -> Any:
        """Return the first present value among ``keys`` (dotted paths supported)."""
        if not isinstance(obj, dict):
            return None
        for key in keys:
            cur: Any = obj
            for part in key.split("."):
                if isinstance(cur, dict) and part in cur:
                    cur = cur[part]
                else:
                    cur = None
                    break
            if cur is not None:
                return cur
        return None
