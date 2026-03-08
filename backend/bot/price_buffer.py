"""
Price Buffer — 틱 → 캔들 집계 모듈.

각 거래소에서 수신한 실시간 틱을 1초/1분/5분/1시간 OHLC 캔들로 집계합니다.
멀티 타임프레임 시그널 엔진(signal.py)에 데이터를 공급합니다.
"""

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Deque, Dict, Optional

logger = logging.getLogger(__name__)


@dataclass
class Candle:
    """OHLC 캔들."""
    open_time: int     # ms timestamp
    close_time: int    # ms timestamp
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    volume: int = 0    # 틱 수
    is_closed: bool = False

    @property
    def mid(self) -> float:
        return (self.high + self.low) / 2.0

    def update(self, price: float) -> None:
        """새 가격으로 캔들 업데이트."""
        if self.volume == 0:
            self.open = price
            self.high = price
            self.low = price
        else:
            self.high = max(self.high, price)
            self.low = min(self.low, price)
        self.close = price
        self.volume += 1


@dataclass
class Tick:
    """원시 틱 데이터."""
    exchange: str
    symbol: str
    price: float
    timestamp: int  # ms


class SymbolBuffer:
    """단일 심볼(BTC 또는 ETH)의 멀티 타임프레임 버퍼."""

    def __init__(self, max_candles: int = 500):
        self.max_candles = max_candles

        # 원시 틱 (최근 10000개)
        self.ticks: Deque[Tick] = deque(maxlen=10000)

        # 캔들 버퍼
        self.candles_1s: Deque[Candle] = deque(maxlen=max_candles)
        self.candles_1m: Deque[Candle] = deque(maxlen=max_candles)
        self.candles_5m: Deque[Candle] = deque(maxlen=200)
        self.candles_1h: Deque[Candle] = deque(maxlen=100)

        # 현재 열린 캔들
        self._current_1s: Optional[Candle] = None
        self._current_1m: Optional[Candle] = None
        self._current_5m: Optional[Candle] = None
        self._current_1h: Optional[Candle] = None

    @property
    def last_price(self) -> Optional[float]:
        if self.ticks:
            return self.ticks[-1].price
        return None

    @property
    def last_tick_time(self) -> Optional[int]:
        if self.ticks:
            return self.ticks[-1].timestamp
        return None

    def update(self, tick: Tick) -> None:
        """새 틱으로 모든 타임프레임 캔들을 업데이트합니다."""
        self.ticks.append(tick)
        ts = tick.timestamp
        price = tick.price

        # 1초 캔들
        self._update_candle(
            price, ts,
            interval_ms=1_000,
            current_attr="_current_1s",
            buffer=self.candles_1s,
        )

        # 1분 캔들
        self._update_candle(
            price, ts,
            interval_ms=60_000,
            current_attr="_current_1m",
            buffer=self.candles_1m,
        )

        # 5분 캔들
        self._update_candle(
            price, ts,
            interval_ms=300_000,
            current_attr="_current_5m",
            buffer=self.candles_5m,
        )

        # 1시간 캔들
        self._update_candle(
            price, ts,
            interval_ms=3_600_000,
            current_attr="_current_1h",
            buffer=self.candles_1h,
        )

    def _update_candle(
        self,
        price: float,
        timestamp_ms: int,
        interval_ms: int,
        current_attr: str,
        buffer: Deque[Candle],
    ) -> None:
        """특정 타임프레임의 캔들을 업데이트합니다."""
        # 현재 캔들의 시간 구간 계산
        candle_open_time = (timestamp_ms // interval_ms) * interval_ms
        candle_close_time = candle_open_time + interval_ms

        current: Optional[Candle] = getattr(self, current_attr)

        if current is None or current.open_time != candle_open_time:
            # 이전 캔들 마감
            if current is not None:
                current.is_closed = True
                buffer.append(current)

            # 새 캔들 시작
            new_candle = Candle(
                open_time=candle_open_time,
                close_time=candle_close_time,
            )
            new_candle.update(price)
            setattr(self, current_attr, new_candle)
        else:
            current.update(price)

    def get_close_prices(self, timeframe: str, count: int) -> list[float]:
        """
        지정 타임프레임의 최근 N개 종가를 반환합니다.

        Args:
            timeframe: "1s", "1m", "5m", "1h"
            count: 가져올 캔들 수

        Returns:
            종가 리스트 (오래된 순서)
        """
        buf = self._get_buffer(timeframe)
        current = self._get_current(timeframe)

        # 닫힌 캔들들 + 현재 열린 캔들의 close(=최신가)
        prices = [c.close for c in buf]
        if current is not None and current.volume > 0:
            prices.append(current.close)

        return prices[-count:] if len(prices) > count else prices

    def get_candles(self, timeframe: str, count: int) -> list[Candle]:
        """지정 타임프레임의 최근 N개 캔들을 반환합니다."""
        buf = self._get_buffer(timeframe)
        candles = list(buf)
        current = self._get_current(timeframe)
        if current is not None and current.volume > 0:
            candles.append(current)
        return candles[-count:] if len(candles) > count else candles

    def get_recent_ticks(self, count: int = 100) -> list[Tick]:
        """최근 N개 틱을 반환합니다."""
        ticks = list(self.ticks)
        return ticks[-count:] if len(ticks) > count else ticks

    def _get_buffer(self, timeframe: str) -> Deque[Candle]:
        mapping = {
            "1s": self.candles_1s,
            "1m": self.candles_1m,
            "5m": self.candles_5m,
            "1h": self.candles_1h,
        }
        buf = mapping.get(timeframe)
        if buf is None:
            raise ValueError(f"Unknown timeframe: {timeframe}")
        return buf

    def _get_current(self, timeframe: str) -> Optional[Candle]:
        mapping = {
            "1s": self._current_1s,
            "1m": self._current_1m,
            "5m": self._current_5m,
            "1h": self._current_1h,
        }
        return mapping.get(timeframe)

    def get_status(self) -> dict:
        return {
            "tick_count": len(self.ticks),
            "candles_1s": len(self.candles_1s),
            "candles_1m": len(self.candles_1m),
            "candles_5m": len(self.candles_5m),
            "candles_1h": len(self.candles_1h),
            "last_price": self.last_price,
            "last_tick_time": self.last_tick_time,
        }


class PriceBuffer:
    """
    전체 가격 버퍼 — BTC, ETH 각각의 SymbolBuffer를 관리합니다.

    on_tick 콜백에서 호출되며, 거래소별/심볼별 틱을
    통합 BTC/ETH 버퍼에 집계합니다.
    """

    def __init__(self, max_candles: int = 500):
        self.btc = SymbolBuffer(max_candles=max_candles)
        self.eth = SymbolBuffer(max_candles=max_candles)

        # 심볼 매핑 (거래소별 심볼 → BTC/ETH)
        self._symbol_map: Dict[str, str] = {}

    def register_symbols(self, exchange: str, btc_symbol: str, eth_symbol: str) -> None:
        """거래소별 심볼을 BTC/ETH로 매핑합니다."""
        self._symbol_map[f"{exchange}:{btc_symbol}"] = "BTC"
        self._symbol_map[f"{exchange}:{eth_symbol}"] = "ETH"

    def update(self, exchange: str, symbol: str, price: float, timestamp_ms: int) -> Optional[str]:
        """
        새 틱을 버퍼에 추가합니다.

        Returns:
            "BTC" or "ETH" (업데이트된 심볼), None if unknown symbol
        """
        key = f"{exchange}:{symbol}"
        asset = self._symbol_map.get(key)
        if asset is None:
            return None

        tick = Tick(exchange=exchange, symbol=symbol, price=price, timestamp=timestamp_ms)

        if asset == "BTC":
            self.btc.update(tick)
        else:
            self.eth.update(tick)

        return asset

    @property
    def has_data(self) -> bool:
        """양쪽 모두 틱이 수신되었는지."""
        return self.btc.last_price is not None and self.eth.last_price is not None

    def get_status(self) -> dict:
        return {
            "btc": self.btc.get_status(),
            "eth": self.eth.get_status(),
            "has_data": self.has_data,
        }
