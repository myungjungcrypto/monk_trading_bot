"""
Extended Exchange 커넥터.

인증: API Key + HMAC-SHA256 서명
Base URL: TBD (API 문서 확인 필요)
Perp 심볼: BTC-PERP, ETH-PERP (예상)

NOTE: API 문서 확인 후 엔드포인트/파라미터를 업데이트해야 합니다.
"""

import hashlib
import hmac
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp

from backend.bot.exchanges.base import (
    Balance,
    BaseExchange,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    PositionSide,
    TickCallback,
    Ticker,
)

logger = logging.getLogger(__name__)

_PERP_SYMBOLS = {
    "BTC": "BTC-PERP",
    "ETH": "ETH-PERP",
}


class ExtendedExchange(BaseExchange):
    """
    Extended Exchange API 커넥터.

    HMAC-SHA256 서명 기반 인증을 사용합니다.
    """

    name = "extended"
    BASE_URL = "https://api.extended.exchange"  # TBD
    WS_URL = "wss://ws.extended.exchange"  # TBD

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        base_url: Optional[str] = None,
        ws_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.ws_url = ws_url or self.WS_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_running: bool = False
        self._ws_task: Optional[Any] = None

    # ── HTTP 세션 ────────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    # ── 인증 ─────────────────────────────────────────────

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        message = f"{timestamp}{method}{path}{body}"
        return hmac.new(
            self.secret_key.encode(),
            message.encode(),
            hashlib.sha256,
        ).hexdigest()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict[str, str]:
        timestamp = str(int(time.time() * 1000))
        signature = self._sign(timestamp, method, path, body)
        return {
            "X-API-Key": self.api_key,
            "X-Signature": signature,
            "X-Timestamp": timestamp,
            "Content-Type": "application/json",
        }

    # ── HTTP 요청 헬퍼 ───────────────────────────────────

    async def _request(
        self,
        method: str,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Any:
        url = f"{self.base_url}{path}"
        session = await self._get_session()

        body = json.dumps(params) if params and method != "GET" else ""
        headers = self._auth_headers(method, path, body) if authenticated else {}

        try:
            if method == "GET":
                async with session.get(url, headers=headers, params=params) as resp:
                    return await self._handle_response(resp)
            elif method == "POST":
                async with session.post(url, headers=headers, data=body) as resp:
                    return await self._handle_response(resp)
            elif method == "DELETE":
                async with session.delete(url, headers=headers, data=body) as resp:
                    return await self._handle_response(resp)
        except aiohttp.ClientError as e:
            logger.error("Extended API error: %s %s - %s", method, path, e)
            raise

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Any:
        if resp.status == 204:
            return None
        data = await resp.json()
        if 200 <= resp.status < 300:
            return data
        error_msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
        raise ExtendedAPIError(resp.status, error_msg)

    # ── 시세 데이터 ──────────────────────────────────────

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._request("GET", "/api/v1/ticker", {"symbol": symbol}, authenticated=False)
        return self._parse_ticker(symbol, data)

    async def get_tickers(self, symbols: List[str]) -> Dict[str, Ticker]:
        data = await self._request("GET", "/api/v1/tickers", authenticated=False)
        result = {}
        for item in (data or []):
            sym = item.get("symbol", "")
            if sym in symbols:
                result[sym] = self._parse_ticker(sym, item)
        return result

    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> List[Dict[str, Any]]:
        data = await self._request(
            "GET", "/api/v1/klines",
            {"symbol": symbol, "interval": interval, "limit": limit},
            authenticated=False,
        )
        return [
            {
                "open_time": c.get("openTime", c.get("t", 0)),
                "open": float(c.get("open", c.get("o", 0))),
                "high": float(c.get("high", c.get("h", 0))),
                "low": float(c.get("low", c.get("l", 0))),
                "close": float(c.get("close", c.get("c", 0))),
                "volume": float(c.get("volume", c.get("v", 0))),
                "close_time": c.get("closeTime", c.get("T", 0)),
            }
            for c in (data or [])
        ]

    async def get_orderbook(self, symbol: str) -> Dict[str, List[List[float]]]:
        data = await self._request("GET", "/api/v1/depth", {"symbol": symbol}, authenticated=False)
        return {
            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
        }

    # ── 주문 ─────────────────────────────────────────────

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        params: Dict[str, Any] = {
            "symbol": symbol,
            "side": side.value,
            "type": order_type.value,
            "quantity": str(quantity),
        }
        if price is not None:
            params["price"] = str(price)
        if reduce_only:
            params["reduceOnly"] = True

        data = await self._request("POST", "/api/v1/order", params)
        return self._parse_order(data)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._request("DELETE", "/api/v1/order", {"symbol": symbol, "orderId": order_id})
            return True
        except ExtendedAPIError:
            return False

    async def cancel_all_orders(self, symbol: str) -> int:
        result = await self._request("DELETE", "/api/v1/orders", {"symbol": symbol})
        return len(result) if isinstance(result, list) else 0

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderResult]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "/api/v1/orders", params)
        return [self._parse_order(o) for o in (data or [])]

    # ── 포지션 ───────────────────────────────────────────

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions()
        for pos in positions:
            if pos.symbol == symbol:
                return pos
        return None

    async def get_positions(self) -> List[Position]:
        data = await self._request("GET", "/api/v1/positions")
        if not data:
            return []
        if isinstance(data, dict):
            data = [data]
        result = []
        for item in data:
            size = float(item.get("size", item.get("quantity", 0)))
            if size == 0:
                continue
            result.append(Position(
                symbol=item.get("symbol", ""),
                side=PositionSide.LONG if item.get("side", "").lower() == "long" else PositionSide.SHORT,
                size=abs(size),
                entry_price=float(item.get("entryPrice", 0)),
                mark_price=float(item.get("markPrice", 0)),
                unrealized_pnl=float(item.get("unrealizedPnl", 0)),
                leverage=float(item.get("leverage", 1)),
                raw=item,
            ))
        return result

    async def close_position(self, symbol: str) -> Optional[OrderResult]:
        pos = await self.get_position(symbol)
        if pos is None:
            return None
        close_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
        return await self.place_order(symbol, close_side, pos.size, OrderType.MARKET, reduce_only=True)

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        try:
            await self._request("POST", "/api/v1/leverage", {"symbol": symbol, "leverage": leverage})
            return True
        except ExtendedAPIError as e:
            logger.warning("Extended set_leverage failed: %s", e)
            return False

    # ── 계정 ─────────────────────────────────────────────

    async def get_balances(self) -> List[Balance]:
        data = await self._request("GET", "/api/v1/balances")
        if not data:
            return []
        result = []
        for item in (data if isinstance(data, list) else [data]):
            available = float(item.get("available", 0))
            locked = float(item.get("locked", 0))
            if available > 0 or locked > 0:
                result.append(Balance(asset=item.get("asset", ""), available=available, locked=locked))
        return result

    async def get_balance(self, asset: str) -> Optional[Balance]:
        balances = await self.get_balances()
        for b in balances:
            if b.asset == asset:
                return b
        return None

    # ── WebSocket ────────────────────────────────────────

    async def connect_ws(self, symbols: List[str], on_tick: TickCallback) -> None:
        import asyncio

        self._ws_running = True

        async def _run():
            while self._ws_running:
                try:
                    session = await self._get_session()
                    self._ws = await session.ws_connect(self.ws_url)
                    logger.info("Extended WS connected")

                    for sym in symbols:
                        await self._ws.send_json({"op": "subscribe", "channel": "ticker", "symbol": sym})
                        logger.info("Extended WS subscribed: %s", sym)

                    async for msg in self._ws:
                        if not self._ws_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_ws_tick(data, on_tick)
                            except Exception as e:
                                logger.warning("Extended WS parse error: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                except Exception as e:
                    if self._ws_running:
                        logger.error("Extended WS error: %s, reconnecting in 3s...", e)
                        await asyncio.sleep(3)

        self._ws_task = asyncio.create_task(_run())

    async def _handle_ws_tick(self, data: dict, on_tick: TickCallback) -> None:
        channel = data.get("channel", "")
        if channel != "ticker":
            return
        payload = data.get("data", data)
        symbol = payload.get("symbol", "")
        price_str = payload.get("lastPrice") or payload.get("last") or payload.get("c")
        if not price_str:
            return
        price = float(price_str)
        ts = int(payload.get("timestamp", time.time() * 1000))
        await on_tick(self.name, symbol, price, ts)

    async def disconnect_ws(self) -> None:
        self._ws_running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        logger.info("Extended WS disconnected")

    @property
    def ws_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ── 유틸리티 ─────────────────────────────────────────

    def perp_symbol(self, base: str) -> str:
        sym = _PERP_SYMBOLS.get(base.upper())
        if sym is None:
            raise ValueError(f"Unknown base asset for Extended perp: {base}")
        return sym

    async def ping(self) -> bool:
        try:
            await self._request("GET", "/api/v1/ping", authenticated=False)
            return True
        except Exception:
            return False

    # ── 내부 파서 ────────────────────────────────────────

    def _parse_ticker(self, symbol: str, data: Dict[str, Any]) -> Ticker:
        return Ticker(
            symbol=symbol,
            last_price=float(data.get("lastPrice", data.get("last", 0))),
            bid_price=float(data.get("bestBid", data.get("bid", 0))),
            ask_price=float(data.get("bestAsk", data.get("ask", 0))),
            volume_24h=float(data.get("volume24h", data.get("volume", 0))),
            high_24h=float(data.get("high24h", data.get("high", 0))),
            low_24h=float(data.get("low24h", data.get("low", 0))),
            timestamp=int(data.get("timestamp", 0)),
        )

    def _parse_order(self, data: Dict[str, Any]) -> OrderResult:
        return OrderResult(
            order_id=str(data.get("orderId", data.get("id", ""))),
            symbol=data.get("symbol", ""),
            side=OrderSide.BUY if data.get("side", "").lower() in ("buy", "bid") else OrderSide.SELL,
            order_type=OrderType.LIMIT if data.get("type", "").lower() == "limit" else OrderType.MARKET,
            quantity=float(data.get("quantity", data.get("size", 0))),
            price=_safe_float(data.get("price")),
            filled_quantity=float(data.get("filledQuantity", data.get("executedQty", 0))),
            avg_fill_price=float(data.get("avgPrice", data.get("avgFillPrice", 0))),
            status=data.get("status", ""),
            raw=data,
        )


class ExtendedAPIError(Exception):
    def __init__(self, status_code: int, message: str):
        self.status_code = status_code
        self.message = message
        super().__init__(f"[{status_code}] {message}")


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
