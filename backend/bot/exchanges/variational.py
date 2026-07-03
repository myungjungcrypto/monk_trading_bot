"""Variational Omni direct-API connector.

Why this exists
---------------
The bot previously placed Variational orders by driving Chrome and clicking like
a human. That is slow: each click round-trips through the DOM, so the two legs of
a pair trade (BTC and ETH) open seconds apart and the intended market-neutral
entry is already skewed by the time both fills land. This connector talks to the
same JSON backend the web client uses, so both legs can be fired concurrently
with millisecond-level skew instead of seconds.

What Variational actually is
----------------------------
Omni is an on-chain (Arbitrum) RFQ derivatives protocol, not a CEX. There is no
API key/secret. Two consequences shape this connector:

1. **Auth is a wallet signature.** We sign a SIWE (Sign-In-With-Ethereum)
   message with the trading wallet's private key to obtain a session token.

2. **A trade is a signed message, not just an HTTP body.** You request a quote
   (RFQ), the backend returns the exact terms to sign, you sign them with EIP-712
   typed data, and submit the signature. The backend/OLP settles it.

Undocumented surface
--------------------
Variational's trading API is not published. The exact paths, headers, and JSON
shapes are therefore *not hard-coded here* — they are loaded from an endpoint map
produced by ``tools/har_extractor.py`` against a real browser capture. Methods
below use that map (with documented fallbacks) and raise a clear error when a
required endpoint has not been captured yet. This keeps the connector honest:
it never silently guesses a payload.

Safety
------
``VARIATIONAL_DRY_RUN=true`` (the default) signs and logs every request but does
not POST orders. Flip it off only after verifying payloads against a capture.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

import httpx

from bot.config import VariationalSettings, get_variational_settings
from bot.exchanges.base import (
    BaseExchange,
    ExchangeError,
    Order,
    OrderResult,
    Position,
    Side,
)

logger = logging.getLogger(__name__)


def _http2_available() -> bool:
    """True if httpx can negotiate HTTP/2 (the optional `h2` package is installed)."""
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False

# Documented/known fallbacks used when the endpoint map lacks an entry. These are
# best-effort defaults; the HAR capture is the source of truth.
_DEFAULT_PATHS: dict[str, tuple[str, str]] = {
    # category -> (method, path)
    "auth_nonce": ("GET", "/auth/nonce"),
    "auth_login": ("POST", "/auth/login"),
    "rfq": ("POST", "/rfq"),
    "order_submit": ("POST", "/order"),
    "position": ("GET", "/positions"),
    "market_data": ("GET", "/market/statistics"),
}

# Canonical symbol -> Variational perp listing symbol. Confirm against a capture.
_SYMBOL_MAP = {
    "BTC": "BTC_USDC_PERP",
    "ETH": "ETH_USDC_PERP",
}


class VariationalConnector(BaseExchange):
    name = "variational"

    def __init__(self, settings: Optional[VariationalSettings] = None):
        self.settings = settings or get_variational_settings()
        self._client: Optional[httpx.AsyncClient] = None
        self._endpoint_map: dict[str, Any] = {}
        self._session_token: Optional[str] = None
        self._address: Optional[str] = None

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.settings.api_base,
                timeout=httpx.Timeout(10.0, connect=5.0),
                # HTTP/2 multiplexes both pair legs over one connection, shaving
                # latency — but only if the optional `h2` package is present.
                http2=_http2_available(),
                headers={"content-type": "application/json"},
            )
        self._load_endpoint_map()
        self._address = self._derive_address()
        await self.authenticate()

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # -- endpoint map -------------------------------------------------------
    def _load_endpoint_map(self) -> None:
        path = self.settings.endpoint_map_path
        if not path.exists():
            logger.warning(
                "Endpoint map %s not found; using documented fallbacks. Capture a "
                "real order flow with tools/har_extractor.py for accurate payloads.",
                path,
            )
            self._endpoint_map = {}
            return
        with open(path, "r", encoding="utf-8") as fh:
            self._endpoint_map = json.load(fh)
        logger.info("Loaded Variational endpoint map from %s", path)

    def _endpoint(self, category: str) -> tuple[str, str]:
        """Resolve (method, url_path) for a logical action from the map or fallback."""
        entries = (self._endpoint_map.get("endpoints") or {}).get(category)
        if entries:
            first = entries[0]
            return first.get("method", "POST"), first.get("path") or first.get("url")
        if category in _DEFAULT_PATHS:
            return _DEFAULT_PATHS[category]
        raise ExchangeError(
            f"No endpoint known for '{category}'. Capture it with har_extractor "
            f"and set VARIATIONAL_ENDPOINT_MAP.",
            venue=self.name,
        )

    # -- signing ------------------------------------------------------------
    def _account(self):
        """Return an eth_account LocalAccount, importing lazily.

        eth-account is only needed when actually signing, so the module (and the
        HAR tooling) import fine without it installed.
        """
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
        from eth_account.messages import encode_defunct

        acct = self._account()
        signed = acct.sign_message(encode_defunct(text=message))
        return signed.signature.hex()

    def _sign_typed_data(self, typed_data: dict[str, Any]) -> str:
        """EIP-712 sign a full typed-data document (domain/types/message).

        The RFQ response is expected to include the exact typed data to sign;
        that avoids us reconstructing the domain/types by hand from guesses.
        """
        from eth_account import Account

        acct = self._account()
        # eth-account >=0.13 exposes sign_typed_data(full_message=...).
        signed = acct.sign_typed_data(full_message=typed_data)
        return signed.signature.hex()

    # -- auth (SIWE) --------------------------------------------------------
    async def authenticate(self) -> None:
        """Obtain a session token via Sign-In-With-Ethereum.

        Flow (confirm exact shapes against a capture):
          1. GET auth_nonce -> {"nonce": "..."}
          2. Build a SIWE message, personal_sign it.
          3. POST auth_login {message, signature} -> {"token": "..."}  (or cookie)
        """
        assert self._client is not None
        method, path = self._endpoint("auth_nonce")
        try:
            resp = await self._request(method, path)
        except ExchangeError:
            logger.warning("Nonce endpoint unavailable; skipping SIWE (public-only mode).")
            return
        nonce = self._extract(resp, ("nonce", "data.nonce")) or ""
        if not nonce:
            logger.warning("No nonce in response; auth response shape may differ from expectation.")
            return

        message = self._build_siwe_message(nonce)
        signature = self._personal_sign(message)

        method, path = self._endpoint("auth_login")
        login = await self._request(
            method, path, json={"message": message, "signature": signature}
        )
        token = self._extract(login, ("token", "accessToken", "data.token", "session_token"))
        if token:
            self._session_token = token
            self._client.headers["authorization"] = f"Bearer {token}"
            logger.info("Variational session established for %s", self._address)
        else:
            # Some backends set an httpOnly cookie instead of returning a token;
            # httpx keeps cookies on the client automatically.
            logger.info("No bearer token returned; relying on session cookie.")

    def _build_siwe_message(self, nonce: str) -> str:
        host = httpx.URL(self.settings.api_base).host
        issued_at = self._utcnow_iso()
        return (
            f"{host} wants you to sign in with your Ethereum account:\n"
            f"{self._address}\n\n"
            f"Sign in to Variational Omni.\n\n"
            f"URI: {self.settings.api_base}\n"
            f"Version: 1\n"
            f"Chain ID: {self.settings.chain_id}\n"
            f"Nonce: {nonce}\n"
            f"Issued At: {issued_at}"
        )

    @staticmethod
    def _utcnow_iso() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    # -- trading ------------------------------------------------------------
    async def place_order(self, order: Order) -> OrderResult:
        """RFQ -> sign -> submit for a single leg.

        Returns a normalized :class:`OrderResult`. In dry-run mode the request is
        signed and logged but not POSTed.
        """
        listing = self._listing(order.symbol)

        # 1. Request a quote for this side/size.
        quote = await self.request_quote(listing, order.side, order.size_usd)

        # 2. The quote carries the EIP-712 terms to sign (typical RFQ design).
        typed_data = self._extract(quote, ("typed_data", "eip712", "data.typed_data"))
        signature = self._sign_typed_data(typed_data) if typed_data else None

        submit_body: dict[str, Any] = {
            "listing": listing,
            "side": order.side.value,
            "size_usd": str(order.size_usd),
            "reduce_only": order.reduce_only,
            "quote_id": self._extract(quote, ("quote_id", "id", "data.quote_id")),
        }
        if signature is not None:
            submit_body["signature"] = signature
        if order.client_id:
            submit_body["client_id"] = order.client_id

        method, path = self._endpoint("order_submit")

        if self.settings.dry_run:
            logger.info(
                "[DRY-RUN] %s %s body=%s", method, path, json.dumps(submit_body)[:400]
            )
            return OrderResult(
                accepted=True, venue=self.name, symbol=order.symbol, side=order.side,
                size_usd=order.size_usd, dry_run=True, raw={"submit_body": submit_body},
            )

        resp = await self._request(method, path, json=submit_body)
        return OrderResult(
            accepted=bool(self._extract(resp, ("accepted", "success")) is not False),
            venue=self.name,
            symbol=order.symbol,
            side=order.side,
            size_usd=order.size_usd,
            filled_price=self._as_decimal(self._extract(resp, ("fill_price", "price"))),
            order_id=self._extract(resp, ("order_id", "id", "trade_id")),
            raw=resp if isinstance(resp, dict) else {"raw": resp},
        )

    async def request_quote(self, listing: str, side: Side, size_usd: Decimal) -> dict[str, Any]:
        method, path = self._endpoint("rfq")
        body = {"listing": listing, "side": side.value, "size_usd": str(size_usd)}
        resp = await self._request(method, path, json=body)
        if not isinstance(resp, dict):
            raise ExchangeError("Unexpected RFQ response shape.", venue=self.name, payload=resp)
        return resp

    async def get_position(self, symbol: str) -> Optional[Position]:
        listing = self._listing(symbol)
        method, path = self._endpoint("position")
        resp = await self._request(method, path)
        rows = resp.get("positions", resp) if isinstance(resp, dict) else resp
        if not isinstance(rows, list):
            return None
        for row in rows:
            if row.get("listing") in (listing, symbol):
                size = self._as_decimal(row.get("size")) or Decimal(0)
                if size == 0:
                    return None
                return Position(
                    symbol=symbol,
                    side=Side.BUY if size > 0 else Side.SELL,
                    size=abs(size),
                    entry_price=self._as_decimal(row.get("entry_price")) or Decimal(0),
                    unrealized_pnl=self._as_decimal(row.get("unrealized_pnl")) or Decimal(0),
                    raw=row,
                )
        return None

    async def get_mark_price(self, symbol: str) -> Decimal:
        listing = self._listing(symbol)
        method, path = self._endpoint("market_data")
        resp = await self._request(method, path)
        rows = resp.get("markets", resp) if isinstance(resp, dict) else resp
        if isinstance(rows, list):
            for row in rows:
                if row.get("listing") in (listing, symbol):
                    price = self._as_decimal(row.get("mark_price") or row.get("price"))
                    if price is not None:
                        return price
        raise ExchangeError(f"No mark price for {symbol}.", venue=self.name, payload=resp)

    # -- HTTP plumbing ------------------------------------------------------
    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        if self._client is None:
            raise ExchangeError("Connector not connected; call connect().", venue=self.name)
        try:
            resp = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise ExchangeError(f"HTTP error on {method} {path}: {exc}", venue=self.name) from exc
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
            return resp.text

    # -- helpers ------------------------------------------------------------
    def _listing(self, symbol: str) -> str:
        return _SYMBOL_MAP.get(symbol.upper(), symbol)

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
