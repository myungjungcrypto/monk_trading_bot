"""
Lighter Exchange 커넥터.

인증: Lighter 공식 SDK SignerClient 기반
Base URL: https://mainnet.zklighter.elliot.ai
WS URL:   wss://mainnet.zklighter.elliot.ai/stream

특이사항: 수수료가 ~0%로 스캘핑에 최적화된 거래소.
NOTE: live 주문은 SDK/계정 설정과 LIGHTER_LIVE_TRADING_ENABLED=true가 모두 필요합니다.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
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

_PERP_SYMBOLS = {"BTC": "BTC", "ETH": "ETH"}


class LighterExchange(BaseExchange):
    """
    Lighter Exchange API 커넥터.

    수수료 ~0%로 스캘핑 전략에 적합합니다.
    공개 ticker WebSocket은 키 없이 사용하고, live 주문은 공식 SDK signer를 사용합니다.
    """

    name = "lighter"
    BASE_URL = "https://mainnet.zklighter.elliot.ai"
    WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        private_key: Optional[str] = None,
        account_index: Optional[int] = None,
        api_key_index: Optional[int] = None,
        btc_market_id: int = 1,
        eth_market_id: int = 0,
        base_url: Optional[str] = None,
        ws_url: Optional[str] = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.private_key = private_key or secret_key
        self.account_index = account_index
        self.api_key_index = api_key_index
        self.market_ids = {"BTC": btc_market_id, "ETH": eth_market_id}
        self.market_symbols = {btc_market_id: "BTC", eth_market_id: "ETH"}
        self.base_decimals = {
            "BTC": int(os.getenv("LIGHTER_BTC_BASE_DECIMALS", "5")),
            "ETH": int(os.getenv("LIGHTER_ETH_BASE_DECIMALS", "4")),
        }
        self.price_decimals = int(os.getenv("LIGHTER_PRICE_DECIMALS", "2"))
        self.live_enabled = os.getenv("LIGHTER_LIVE_TRADING_ENABLED", "false").lower() == "true"
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.ws_url = ws_url or self.WS_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_running: bool = False
        self._ws_task: Optional[Any] = None
        self._last_tickers: Dict[str, Ticker] = {}
        self._signer = None

    @property
    def live_trading_ready(self) -> bool:
        return bool(
            self.live_enabled
            and self.private_key
            and self.account_index is not None
            and self.api_key_index is not None
        )

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
            logger.error("Lighter API error: %s %s - %s", method, path, e)
            raise

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Any:
        if resp.status == 204:
            return None
        data = await resp.json()
        if 200 <= resp.status < 300:
            return data
        error_msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
        raise LighterAPIError(resp.status, error_msg)

    def _market_id(self, symbol: str) -> int:
        asset = symbol.split("-")[0].split("/")[0].upper()
        if asset not in self.market_ids:
            raise LighterAPIError(400, f"Unknown Lighter symbol: {symbol}")
        return self.market_ids[asset]

    def _asset_from_symbol(self, symbol: str) -> str:
        return symbol.split("-")[0].split("/")[0].upper()

    def _to_base_amount(self, symbol: str, quantity: float) -> int:
        asset = self._asset_from_symbol(symbol)
        return int(round(quantity * (10 ** self.base_decimals[asset])))

    def _to_base_price(self, price: float) -> int:
        return int(round(price * (10 ** self.price_decimals)))

    def _client_order_index(self) -> int:
        return int(time.time() * 1000) % (2 ** 48 - 1)

    def _get_signer(self):
        if self._signer is not None:
            return self._signer
        if not self.live_trading_ready:
            raise LighterAPIError(
                403,
                "Lighter live trading is disabled. Set LIGHTER_LIVE_TRADING_ENABLED=true, "
                "LIGHTER_PRIVATE_KEY, LIGHTER_ACCOUNT_INDEX, and LIGHTER_API_KEY_INDEX.",
            )
        try:
            import lighter  # type: ignore
        except ImportError as exc:
            raise LighterAPIError(500, "Install the official lighter-sdk package for live trading") from exc

        try:
            signer = lighter.SignerClient(
                url=self.base_url,
                private_key=self.private_key,
                account_index=self.account_index,
                api_key_index=self.api_key_index,
            )
        except TypeError:
            signer = lighter.SignerClient(
                url=self.base_url,
                api_private_keys={self.api_key_index: self.private_key},
                account_index=self.account_index,
            )

        check = getattr(signer, "check_client", None)
        if check is not None:
            err = check()
            if err is not None:
                raise LighterAPIError(500, f"Lighter signer check failed: {err}")

        self._signer = signer
        return signer

    # ── 시세 데이터 ──────────────────────────────────────

    async def get_ticker(self, symbol: str) -> Ticker:
        cached = self._last_tickers.get(symbol)
        if cached is not None:
            return cached
        raise LighterAPIError(
            503,
            f"No cached Lighter ticker for {symbol}. Start WebSocket before trading.",
        )

    async def get_tickers(self, symbols: List[str]) -> Dict[str, Ticker]:
        return {sym: self._last_tickers[sym] for sym in symbols if sym in self._last_tickers}

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
        signer = self._get_signer()
        ticker = await self.get_ticker(symbol)
        mark = ticker.last_price
        if price is None:
            # Market orders still need a worst acceptable price in the SDK.
            price = mark * (1.01 if side == OrderSide.BUY else 0.99)

        order_type_value = (
            signer.ORDER_TYPE_MARKET
            if order_type == OrderType.MARKET
            else signer.ORDER_TYPE_LIMIT
        )
        tif = (
            signer.ORDER_TIME_IN_FORCE_IMMEDIATE_OR_CANCEL
            if order_type == OrderType.MARKET
            else signer.ORDER_TIME_IN_FORCE_GOOD_TILL_TIME
        )
        expiry = (
            signer.DEFAULT_IOC_EXPIRY
            if order_type == OrderType.MARKET
            else signer.DEFAULT_28_DAY_ORDER_EXPIRY
        )
        client_order_index = self._client_order_index()
        base_amount = self._to_base_amount(symbol, quantity)
        base_price = self._to_base_price(price)

        tx, tx_hash, err = await signer.create_order(
            market_index=self._market_id(symbol),
            client_order_index=client_order_index,
            base_amount=base_amount,
            price=base_price,
            is_ask=side == OrderSide.SELL,
            order_type=order_type_value,
            time_in_force=tif,
            reduce_only=reduce_only,
            order_expiry=expiry,
        )
        if err is not None:
            raise LighterAPIError(500, f"Lighter create_order failed: {err}")

        return OrderResult(
            order_id=str(client_order_index),
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=price,
            filled_quantity=quantity,
            avg_fill_price=mark,
            status="SUBMITTED",
            raw={"tx": tx, "tx_hash": tx_hash},
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        try:
            await self._request("DELETE", "/api/v1/order", {"symbol": symbol, "orderId": order_id})
            return True
        except LighterAPIError:
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
        if self.account_index is None:
            return []

        data = await self._request(
            "GET",
            "/api/v1/account",
            {"account_index": self.account_index},
            authenticated=False,
        )
        positions = data.get("positions", data.get("account", {}).get("positions", {}))
        if isinstance(positions, dict):
            raw_positions = list(positions.values())
        else:
            raw_positions = positions or []

        result = []
        for item in raw_positions:
            market_id = int(item.get("market_id", item.get("market_index", -1)))
            symbol = self.market_symbols.get(market_id, item.get("symbol", ""))
            size = abs(float(item.get("position", item.get("size", 0)) or 0))
            if size == 0:
                continue
            sign = int(item.get("sign", 1))
            ticker = self._last_tickers.get(symbol)
            mark = ticker.last_price if ticker else float(item.get("mark_price", item.get("avg_entry_price", 0)) or 0)
            result.append(Position(
                symbol=symbol,
                side=PositionSide.LONG if sign >= 0 else PositionSide.SHORT,
                size=size,
                entry_price=float(item.get("avg_entry_price", 0) or 0),
                mark_price=mark,
                unrealized_pnl=float(item.get("unrealized_pnl", 0) or 0),
                leverage=1.0,
                liquidation_price=_safe_float(item.get("liquidation_price")),
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
        logger.info("Lighter leverage is controlled by margin/account settings; requested %s=%dx", symbol, leverage)
        return True

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
        self._ws_running = True

        async def _run():
            while self._ws_running:
                try:
                    session = await self._get_session()
                    self._ws = await session.ws_connect(self.ws_url)
                    logger.info("Lighter WS connected")

                    for sym in symbols:
                        market_id = self._market_id(sym)
                        await self._ws.send_json({"type": "subscribe", "channel": f"ticker/{market_id}"})
                        logger.info("Lighter WS subscribed: ticker/%s (%s)", market_id, sym)

                    async for msg in self._ws:
                        if not self._ws_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_ws_tick(data, on_tick)
                            except Exception as e:
                                logger.warning("Lighter WS parse error: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            break
                except Exception as e:
                    if self._ws_running:
                        logger.error("Lighter WS error: %s, reconnecting in 3s...", e)
                        await asyncio.sleep(3)

        self._ws_task = asyncio.create_task(_run())

    async def _handle_ws_tick(self, data: dict, on_tick: TickCallback) -> None:
        channel = data.get("channel", "")
        msg_type = data.get("type", "")
        if not (channel.startswith("ticker:") and msg_type in {"update/ticker", "subscribed/ticker"}):
            return

        payload = data.get("ticker", {})
        try:
            market_id = int(channel.split(":", 1)[1])
        except (IndexError, ValueError):
            return

        symbol = self.market_symbols.get(market_id) or payload.get("s", "")
        ask = payload.get("a") or {}
        bid = payload.get("b") or {}
        ask_price = _safe_float(ask.get("price"))
        bid_price = _safe_float(bid.get("price"))
        if ask_price is None and bid_price is None:
            return
        if ask_price is None:
            price = bid_price or 0.0
        elif bid_price is None:
            price = ask_price
        else:
            price = (ask_price + bid_price) / 2.0
        ts = int(data.get("timestamp", time.time() * 1000))
        ticker = Ticker(
            symbol=symbol,
            last_price=price,
            bid_price=bid_price or price,
            ask_price=ask_price or price,
            volume_24h=0.0,
            high_24h=price,
            low_24h=price,
            timestamp=ts,
        )
        self._last_tickers[symbol] = ticker
        await on_tick(self.name, symbol, price, ts)

    async def disconnect_ws(self) -> None:
        self._ws_running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        logger.info("Lighter WS disconnected")

    @property
    def ws_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ── 유틸리티 ─────────────────────────────────────────

    def perp_symbol(self, base: str) -> str:
        sym = _PERP_SYMBOLS.get(base.upper())
        if sym is None:
            raise ValueError(f"Unknown base asset for Lighter perp: {base}")
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


class LighterAPIError(Exception):
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
