"""
Backpack Exchange 커넥터.

인증: Ed25519 서명 기반 (API Key + Secret Key)
Base URL: https://api.backpack.exchange/
Perp 심볼: BTC_USDC_PERP, ETH_USDC_PERP
"""

import base64
import json
import logging
import time
from typing import Any, Dict, List, Optional

import aiohttp
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

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

# Backpack API의 side 매핑
_SIDE_MAP = {
    OrderSide.BUY: "Bid",
    OrderSide.SELL: "Ask",
}

_SIDE_REVERSE = {
    "Bid": OrderSide.BUY,
    "Ask": OrderSide.SELL,
}

# Perp 심볼 매핑
_PERP_SYMBOLS = {
    "BTC": "BTC_USDC_PERP",
    "ETH": "ETH_USDC_PERP",
}


class BackpackExchange(BaseExchange):
    """
    Backpack Exchange API 커넥터.

    Ed25519 서명 기반 인증을 사용합니다.
    .env에서 BACKPACK_API_KEY (public key)와 BACKPACK_SECRET_KEY (base64 encoded private key)를 로드합니다.
    """

    name = "backpack"
    BASE_URL = "https://api.backpack.exchange/"
    WS_URL = "wss://ws.backpack.exchange"
    REQUEST_WINDOW = 5000  # ms

    def __init__(
        self,
        api_key: str,
        secret_key: str,
        request_window: int = 5000,
        base_url: Optional[str] = None,
        ws_url: Optional[str] = None,
    ):
        """
        Args:
            api_key: Base64 인코딩된 public key (X-API-Key 헤더에 사용)
            secret_key: Base64 인코딩된 private key (Ed25519 서명에 사용)
            request_window: 요청 유효 시간 (ms), 기본 5000
            base_url: API base URL 오버라이드 (테스트용)
            ws_url: WebSocket URL 오버라이드 (테스트용)
        """
        self.api_key = api_key
        self.private_key = Ed25519PrivateKey.from_private_bytes(
            base64.b64decode(secret_key)
        )
        self.request_window = request_window
        self.base_url = (base_url or self.BASE_URL).rstrip("/")
        self.ws_url = ws_url or self.WS_URL
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._ws_running: bool = False
        self._ws_task: Optional[Any] = None

    # ── HTTP 세션 관리 ──────────────────────────────────────

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        """HTTP 세션을 닫습니다."""
        if self._session and not self._session.closed:
            await self._session.close()

    # ── Ed25519 서명 ────────────────────────────────────────

    def _sign(self, instruction: str, params: Optional[Dict[str, Any]], timestamp: int) -> str:
        """
        Backpack API 서명 문자열을 생성하고 Ed25519로 서명합니다.

        서명 문자열 형식:
            instruction={instruction}&{sorted_params}&timestamp={ts}&window={window}
        """
        parts = [f"instruction={instruction}"]

        if params:
            normalized = {}
            for k, v in params.items():
                if isinstance(v, bool):
                    normalized[k] = str(v).lower()
                else:
                    normalized[k] = v
            sorted_params = "&".join(
                f"{k}={v}" for k, v in sorted(normalized.items())
            )
            parts.append(sorted_params)

        parts.append(f"timestamp={timestamp}")
        parts.append(f"window={self.request_window}")
        sign_str = "&".join(parts)

        signature = self.private_key.sign(sign_str.encode())
        return base64.b64encode(signature).decode()

    def _auth_headers(
        self, instruction: str, params: Optional[Dict[str, Any]] = None
    ) -> Dict[str, str]:
        """인증 헤더를 생성합니다."""
        timestamp = int(time.time() * 1000)
        signature = self._sign(instruction, params, timestamp)
        return {
            "X-API-Key": self.api_key,
            "X-Signature": signature,
            "X-Timestamp": str(timestamp),
            "X-Window": str(self.request_window),
            "Content-Type": "application/json; charset=utf-8",
        }

    # ── HTTP 요청 헬퍼 ──────────────────────────────────────

    async def _request(
        self,
        method: str,
        endpoint: str,
        instruction: Optional[str] = None,
        params: Optional[Dict[str, Any]] = None,
        authenticated: bool = True,
    ) -> Any:
        """
        API 요청을 보냅니다.

        Args:
            method: HTTP 메서드
            endpoint: API 경로 (e.g. "api/v1/ticker")
            instruction: 서명에 사용할 instruction (인증 요청 시 필수)
            params: 요청 파라미터
            authenticated: 인증 헤더 포함 여부
        """
        url = f"{self.base_url}/{endpoint}"
        session = await self._get_session()

        headers = {}
        if authenticated and instruction:
            headers = self._auth_headers(instruction, params)

        try:
            if method == "GET":
                async with session.get(url, headers=headers, params=params) as resp:
                    return await self._handle_response(resp)
            elif method == "POST":
                async with session.post(
                    url, headers=headers, data=json.dumps(params) if params else None
                ) as resp:
                    return await self._handle_response(resp)
            elif method == "DELETE":
                async with session.delete(
                    url, headers=headers, data=json.dumps(params) if params else None
                ) as resp:
                    return await self._handle_response(resp)
            elif method == "PATCH":
                async with session.patch(
                    url, headers=headers, data=json.dumps(params) if params else None
                ) as resp:
                    return await self._handle_response(resp)
            else:
                raise ValueError(f"Unsupported HTTP method: {method}")
        except aiohttp.ClientError as e:
            logger.error("Backpack API request failed: %s %s - %s", method, endpoint, e)
            raise

    async def _handle_response(self, resp: aiohttp.ClientResponse) -> Any:
        """응답을 처리합니다. 오류 시 예외를 발생시킵니다."""
        if resp.status == 204:
            return None

        try:
            data = await resp.json()
        except Exception:
            text = await resp.text()
            if 200 <= resp.status < 300:
                return text
            raise BackpackAPIError(resp.status, text)

        if 200 <= resp.status < 300:
            return data

        error_msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
        error_code = data.get("code") if isinstance(data, dict) else None
        raise BackpackAPIError(resp.status, error_msg, error_code)

    # ── 시세 데이터 (Public) ────────────────────────────────

    async def get_ticker(self, symbol: str) -> Ticker:
        data = await self._request(
            "GET", "api/v1/ticker",
            params={"symbol": symbol},
            authenticated=False,
        )
        return self._parse_ticker(symbol, data)

    async def get_tickers(self, symbols: List[str]) -> Dict[str, Ticker]:
        data = await self._request(
            "GET", "api/v1/tickers", authenticated=False
        )
        result = {}
        for item in data:
            sym = item.get("symbol", "")
            if sym in symbols:
                result[sym] = self._parse_ticker(sym, item)
        return result

    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        end_time = int(time.time())
        # interval을 초 단위로 변환하여 start_time 계산
        interval_seconds = self._interval_to_seconds(interval)
        start_time = end_time - (interval_seconds * limit)

        data = await self._request(
            "GET", "api/v1/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "startTime": start_time,
                "endTime": end_time,
            },
            authenticated=False,
        )
        result = []
        for candle in (data or []):
            if isinstance(candle, dict):
                # 딕셔너리 형식: {"open": ..., "close": ..., ...}
                result.append({
                    "open_time": candle.get("startTime", candle.get("openTime", candle.get("t", 0))),
                    "open": float(candle.get("open", candle.get("o", 0))),
                    "high": float(candle.get("high", candle.get("h", 0))),
                    "low": float(candle.get("low", candle.get("l", 0))),
                    "close": float(candle.get("close", candle.get("c", 0))),
                    "volume": float(candle.get("volume", candle.get("v", 0))),
                    "close_time": candle.get("endTime", candle.get("closeTime", candle.get("T", 0))),
                })
            elif isinstance(candle, (list, tuple)):
                # 배열 형식: [open_time, open, high, low, close, volume, close_time]
                result.append({
                    "open_time": candle[0],
                    "open": float(candle[1]),
                    "high": float(candle[2]),
                    "low": float(candle[3]),
                    "close": float(candle[4]),
                    "volume": float(candle[5]),
                    "close_time": candle[6] if len(candle) > 6 else 0,
                })
        return result

    async def get_orderbook(self, symbol: str) -> Dict[str, List[List[float]]]:
        data = await self._request(
            "GET", "api/v1/depth",
            params={"symbol": symbol},
            authenticated=False,
        )
        return {
            "bids": [[float(p), float(q)] for p, q in data.get("bids", [])],
            "asks": [[float(p), float(q)] for p, q in data.get("asks", [])],
        }

    # ── 주문 (Authenticated) ────────────────────────────────

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
            "side": _SIDE_MAP[side],
            "orderType": order_type.value,
            "quantity": str(quantity),
        }
        if order_type == OrderType.LIMIT and price is not None:
            params["price"] = str(price)
            params["timeInForce"] = "GTC"
        if reduce_only:
            params["reduceOnly"] = True

        data = await self._request("POST", "api/v1/order", "orderExecute", params)
        return self._parse_order(data)

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        params = {"symbol": symbol, "orderId": order_id}
        try:
            await self._request("DELETE", "api/v1/order", "orderCancel", params)
            return True
        except BackpackAPIError:
            return False

    async def cancel_all_orders(self, symbol: str) -> int:
        params = {"symbol": symbol}
        result = await self._request("DELETE", "api/v1/orders", "orderCancelAll", params)
        if isinstance(result, list):
            return len(result)
        return 0

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderResult]:
        params: Dict[str, Any] = {}
        if symbol:
            params["symbol"] = symbol
        data = await self._request("GET", "api/v1/orders", "orderQueryAll", params)
        return [self._parse_order(o) for o in (data or [])]

    # ── 포지션 (Authenticated) ──────────────────────────────

    async def get_position(self, symbol: str) -> Optional[Position]:
        positions = await self.get_positions()
        for pos in positions:
            if pos.symbol == symbol:
                return pos
        return None

    async def get_positions(self) -> List[Position]:
        data = await self._request("GET", "api/v1/position", "positionQuery")
        if not data:
            return []
        # API가 단일 객체 또는 리스트를 반환할 수 있음
        if isinstance(data, dict):
            data = [data]
        result = []
        for item in data:
            size = float(item.get("netQuantity", 0))
            if size == 0:
                continue
            # IMF(Initial Margin Fraction)에서 레버리지 역산: leverage = 1 / imf
            imf = float(item.get("imf", 0))
            leverage = round(1 / imf) if imf > 0 else 1
            result.append(Position(
                symbol=item.get("symbol", ""),
                side=PositionSide.LONG if size > 0 else PositionSide.SHORT,
                size=abs(size),
                entry_price=float(item.get("entryPrice", 0)),
                mark_price=float(item.get("markPrice", 0)),
                unrealized_pnl=float(item.get("pnlUnrealized", 0)),
                leverage=leverage,
                liquidation_price=_safe_float(item.get("estLiquidationPrice")),
                raw=item,
            ))
        return result

    async def close_position(self, symbol: str) -> Optional[OrderResult]:
        pos = await self.get_position(symbol)
        if pos is None:
            return None
        # 포지션 반대 방향으로 시장가 주문
        close_side = OrderSide.SELL if pos.side == PositionSide.LONG else OrderSide.BUY
        return await self.place_order(
            symbol=symbol,
            side=close_side,
            quantity=pos.size,
            order_type=OrderType.MARKET,
            reduce_only=True,
        )

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """
        Backpack는 계정 단위 레버리지 설정 (leverageLimit).
        심볼별 레버리지는 지원하지 않으므로 계정 전체에 적용됩니다.
        """
        try:
            await self._request(
                "PATCH", "api/v1/account", "accountUpdate",
                params={"leverageLimit": str(leverage)},
            )
            return True
        except BackpackAPIError as e:
            logger.warning("Failed to set leverage: %s", e)
            return False

    # ── 계정 잔고 (Authenticated) ───────────────────────────

    async def get_balances(self) -> List[Balance]:
        data = await self._request("GET", "api/v1/capital", "balanceQuery")
        if not data:
            return []
        result = []
        # API 응답: {asset: {available: str, locked: str}, ...}
        if isinstance(data, dict):
            for asset, info in data.items():
                available = float(info.get("available", 0))
                locked = float(info.get("locked", 0))
                if available > 0 or locked > 0:
                    result.append(Balance(asset=asset, available=available, locked=locked))
        return result

    async def get_balance(self, asset: str) -> Optional[Balance]:
        balances = await self.get_balances()
        for b in balances:
            if b.asset == asset:
                return b
        return None

    # ── WebSocket ─────────────────────────────────────────

    async def connect_ws(
        self,
        symbols: List[str],
        on_tick: "TickCallback",
    ) -> None:
        """Backpack WebSocket에 연결하여 실시간 틱을 수신합니다."""
        import asyncio

        self._ws_running = True

        async def _run():
            while self._ws_running:
                try:
                    session = await self._get_session()
                    self._ws = await session.ws_connect(self.ws_url)
                    logger.info("Backpack WS connected")

                    # 각 심볼의 ticker 스트림 구독
                    for sym in symbols:
                        subscribe_msg = {
                            "method": "SUBSCRIBE",
                            "params": [f"ticker.{sym}"],
                        }
                        await self._ws.send_json(subscribe_msg)
                        logger.info("Backpack WS subscribed: ticker.%s", sym)

                    async for msg in self._ws:
                        if not self._ws_running:
                            break
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            try:
                                data = json.loads(msg.data)
                                await self._handle_ws_message(data, on_tick)
                            except Exception as e:
                                logger.warning("Backpack WS message parse error: %s", e)
                        elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                            logger.warning("Backpack WS closed/error: %s", msg.type)
                            break

                except Exception as e:
                    if self._ws_running:
                        logger.error("Backpack WS error: %s, reconnecting in 3s...", e)
                        await asyncio.sleep(3)

            logger.info("Backpack WS loop ended")

        self._ws_task = asyncio.create_task(_run())

    async def _handle_ws_message(
        self, data: dict, on_tick: "TickCallback"
    ) -> None:
        """WebSocket 메시지를 파싱하여 on_tick 콜백을 호출합니다."""
        stream = data.get("stream", "")
        if not stream.startswith("ticker."):
            return

        payload = data.get("data", {})
        symbol = payload.get("s", stream.replace("ticker.", ""))
        last_price_str = payload.get("c") or payload.get("lastPrice")
        if last_price_str is None:
            return

        price = float(last_price_str)
        ts = int(payload.get("E", payload.get("t", time.time() * 1000)))
        await on_tick(self.name, symbol, price, ts)

    async def disconnect_ws(self) -> None:
        """WebSocket 연결을 종료합니다."""
        self._ws_running = False
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._ws_task:
            self._ws_task.cancel()
            self._ws_task = None
        logger.info("Backpack WS disconnected")

    @property
    def ws_connected(self) -> bool:
        return self._ws is not None and not self._ws.closed

    # ── 유틸리티 ────────────────────────────────────────────

    def perp_symbol(self, base: str) -> str:
        """BTC → BTC_USDC_PERP, ETH → ETH_USDC_PERP"""
        sym = _PERP_SYMBOLS.get(base.upper())
        if sym is None:
            raise ValueError(f"Unknown base asset for Backpack perp: {base}")
        return sym

    async def ping(self) -> bool:
        try:
            data = await self._request(
                "GET", "api/v1/ping", authenticated=False
            )
            return True
        except Exception:
            return False

    async def get_mark_price(self, symbol: str) -> Dict[str, float]:
        """마크 프라이스, 인덱스 프라이스, 펀딩레이트를 조회합니다."""
        data = await self._request(
            "GET", "api/v1/markPrices",
            params={"symbol": symbol},
            authenticated=False,
        )
        return {
            "mark_price": float(data.get("markPrice", 0)),
            "index_price": float(data.get("indexPrice", 0)),
            "funding_rate": float(data.get("lastFundingRate", 0)),
        }

    async def get_fill_history(
        self,
        symbol: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """체결 내역을 조회합니다."""
        params: Dict[str, Any] = {"limit": limit}
        if symbol:
            params["symbol"] = symbol
        return await self._request(
            "GET", "wapi/v1/history/fills", "fillHistoryQueryAll", params
        )

    # ── 내부 파서 ───────────────────────────────────────────

    def _parse_ticker(self, symbol: str, data: Dict[str, Any]) -> Ticker:
        return Ticker(
            symbol=symbol,
            last_price=float(data.get("lastPrice", 0)),
            bid_price=float(data.get("bestBidPrice", 0)),
            ask_price=float(data.get("bestAskPrice", 0)),
            volume_24h=float(data.get("volume", 0)),
            high_24h=float(data.get("high", 0)),
            low_24h=float(data.get("low", 0)),
            timestamp=int(data.get("timestamp", 0)),
        )

    def _parse_order(self, data: Dict[str, Any]) -> OrderResult:
        side_str = data.get("side", "")
        return OrderResult(
            order_id=data.get("id", data.get("orderId", "")),
            symbol=data.get("symbol", ""),
            side=_SIDE_REVERSE.get(side_str, OrderSide.BUY),
            order_type=OrderType.LIMIT if data.get("orderType") == "Limit" else OrderType.MARKET,
            quantity=float(data.get("quantity", 0)),
            price=_safe_float(data.get("price")),
            filled_quantity=float(data.get("executedQuantity", 0)),
            avg_fill_price=float(data.get("executedQuoteQuantity", 0)),
            status=data.get("status", ""),
            raw=data,
        )

    @staticmethod
    def _interval_to_seconds(interval: str) -> int:
        """'1m' → 60, '5m' → 300, '1h' → 3600, '1d' → 86400"""
        units = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
        unit = interval[-1]
        num = int(interval[:-1])
        return num * units.get(unit, 60)


class BackpackAPIError(Exception):
    """Backpack API 오류."""

    def __init__(self, status_code: int, message: str, code: Optional[str] = None):
        self.status_code = status_code
        self.message = message
        self.code = code
        super().__init__(f"[{status_code}] {code or ''}: {message}")


def _safe_float(value: Any) -> Optional[float]:
    """None이나 빈 문자열을 안전하게 처리합니다."""
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (ValueError, TypeError):
        return None
