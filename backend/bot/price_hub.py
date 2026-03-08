"""
Price Hub — WebSocket 가격 스트림 허브.

모든 활성화된 거래소의 WebSocket에 동시 연결하여
BTC/ETH 실시간 틱을 수신하고 PriceBuffer에 전달합니다.
"""

import asyncio
import logging
from typing import Callable, Coroutine, Any, Dict, List, Optional

from backend.bot.exchanges.base import BaseExchange, TickCallback
from backend.bot.price_buffer import PriceBuffer

logger = logging.getLogger(__name__)

# 틱 이벤트 리스너 타입
OnTickListener = Callable[[str, str, float, int], Coroutine[Any, Any, None]]


class PriceHub:
    """
    WebSocket 가격 허브.

    각 거래소의 WS에 연결하여 실시간 BTC/ETH 틱을 수신합니다.
    틱이 수신되면:
    1. PriceBuffer에 업데이트
    2. 등록된 리스너들에게 전파 (signal engine, dashboard 등)
    """

    def __init__(
        self,
        exchanges: Dict[str, BaseExchange],
        price_buffer: PriceBuffer,
    ):
        """
        Args:
            exchanges: {name: exchange_instance} 딕셔너리
            price_buffer: 틱→캔들 집계 버퍼
        """
        self.exchanges = exchanges
        self.price_buffer = price_buffer
        self._listeners: List[OnTickListener] = []
        self._running = False
        self._tick_count = 0

    def add_listener(self, listener: OnTickListener) -> None:
        """틱 이벤트 리스너를 등록합니다."""
        self._listeners.append(listener)

    async def start(self) -> None:
        """모든 거래소 WebSocket에 연결합니다."""
        self._running = True
        logger.info("PriceHub starting... exchanges=%s", list(self.exchanges.keys()))

        # 각 거래소의 심볼 등록
        for name, exchange in self.exchanges.items():
            btc_sym = exchange.perp_symbol("BTC")
            eth_sym = exchange.perp_symbol("ETH")
            self.price_buffer.register_symbols(name, btc_sym, eth_sym)

        # 모든 거래소 WS 동시 연결
        connect_tasks = []
        for name, exchange in self.exchanges.items():
            btc_sym = exchange.perp_symbol("BTC")
            eth_sym = exchange.perp_symbol("ETH")
            symbols = [btc_sym, eth_sym]
            connect_tasks.append(exchange.connect_ws(symbols, self._on_tick))

        await asyncio.gather(*connect_tasks, return_exceptions=True)
        logger.info("PriceHub started — all exchanges connected")

    async def stop(self) -> None:
        """모든 WebSocket 연결을 종료합니다."""
        self._running = False
        logger.info("PriceHub stopping...")

        disconnect_tasks = []
        for exchange in self.exchanges.values():
            disconnect_tasks.append(exchange.disconnect_ws())

        await asyncio.gather(*disconnect_tasks, return_exceptions=True)
        logger.info("PriceHub stopped. Total ticks received: %d", self._tick_count)

    async def _on_tick(
        self,
        exchange: str,
        symbol: str,
        price: float,
        timestamp_ms: int,
    ) -> None:
        """
        거래소에서 틱 수신 시 호출되는 콜백.

        1. PriceBuffer 업데이트
        2. 등록된 리스너에게 전파
        """
        self._tick_count += 1

        # PriceBuffer 업데이트
        asset = self.price_buffer.update(exchange, symbol, price, timestamp_ms)
        if asset is None:
            return

        # 리스너에게 전파
        for listener in self._listeners:
            try:
                await listener(exchange, symbol, price, timestamp_ms)
            except Exception as e:
                logger.error("Tick listener error: %s", e)

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def tick_count(self) -> int:
        return self._tick_count

    def get_status(self) -> dict:
        ws_status = {}
        for name, exchange in self.exchanges.items():
            ws_status[name] = exchange.ws_connected

        return {
            "running": self._running,
            "tick_count": self._tick_count,
            "exchanges": ws_status,
            "price_buffer": self.price_buffer.get_status(),
        }
