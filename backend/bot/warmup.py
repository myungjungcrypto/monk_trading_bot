"""
Warm-up — 시작 시 거래소 REST API에서 과거 캔들을 로드하여
PriceBuffer와 SignalEngine을 즉시 사용 가능 상태로 만듭니다.

재시작해도 10시간 대기 없이 바로 시그널 생성 가능.
"""

import asyncio
import logging
import os
from typing import Any, Dict, List

import aiohttp

from backend.bot.exchanges.base import BaseExchange
from backend.bot.price_buffer import Candle, PriceBuffer
from backend.bot.signal import MultiTimeframeSignalEngine

logger = logging.getLogger(__name__)

BINANCE_FAPI_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
BINANCE_WARMUP_SYMBOLS = {"BTC": "BTCUSDT", "ETH": "ETHUSDT"}


async def warmup(
    exchanges: Dict[str, BaseExchange],
    price_buffer: PriceBuffer,
    signal_engine: MultiTimeframeSignalEngine,
) -> bool:
    """
    첫 번째 활성 거래소에서 과거 5분봉/1시간봉을 로드합니다.

    Returns:
        True if warmup succeeded, False otherwise
    """
    if not exchanges:
        logger.warning("Warmup: no exchanges available")
        return False

    exchange = next(iter(exchanges.values()))
    exchange_name = next(iter(exchanges.keys()))
    btc_sym = exchange.perp_symbol("BTC")
    eth_sym = exchange.perp_symbol("ETH")

    # 심볼 등록 (price_hub.start 전에 필요)
    price_buffer.register_symbols(exchange_name, btc_sym, eth_sym)

    try:
        # 5분봉 200개 (≈16시간), 1시간봉 100개 (≈4일)
        source, btc_5m, eth_5m, btc_1h, eth_1h = await _fetch_warmup_klines(
            exchange, btc_sym, eth_sym, exchange_name,
        )

        if not btc_5m or not eth_5m:
            logger.warning("Warmup: no 5m klines loaded — will wait for live data")
            return False

        # PriceBuffer에 캔들 주입
        _inject_candles(price_buffer.btc.candles_5m, btc_5m)
        _inject_candles(price_buffer.eth.candles_5m, eth_5m)
        _inject_candles(price_buffer.btc.candles_1h, btc_1h)
        _inject_candles(price_buffer.eth.candles_1h, eth_1h)

        # SignalEngine 스프레드 히스토리 pre-fill
        _prefill_spreads(signal_engine, price_buffer)

        logger.info(
            "Warmup complete from %s: 5m=%d/%d candles, 1h=%d/%d candles, "
            "spread_5m=%d, spread_1h=%d",
            source,
            len(price_buffer.btc.candles_5m), len(price_buffer.eth.candles_5m),
            len(price_buffer.btc.candles_1h), len(price_buffer.eth.candles_1h),
            len(signal_engine._spread_5m), len(signal_engine._spread_1h),
        )
        return True

    except Exception as e:
        logger.error("Warmup failed: %s (%s)", e, type(e).__name__, exc_info=True)
        return False


async def _fetch_warmup_klines(
    exchange: BaseExchange,
    btc_sym: str,
    eth_sym: str,
    exchange_name: str,
) -> tuple[str, list[Candle], list[Candle], list[Candle], list[Candle]]:
    """Binance Futures를 우선 사용하고, 실패 시 활성 거래소로 fallback."""
    attempts = []
    binance_enabled = _env_bool("WARMUP_BINANCE_ENABLED", True)
    binance_first = _env_bool("WARMUP_BINANCE_FIRST", True)

    if binance_enabled and binance_first:
        attempts.append(("binance", _fetch_binance_klines))

    attempts.append((exchange_name, lambda: _fetch_klines(exchange, btc_sym, eth_sym)))

    if binance_enabled and not binance_first:
        attempts.append(("binance", _fetch_binance_klines))

    for source, fetcher in attempts:
        btc_5m, eth_5m, btc_1h, eth_1h = await fetcher()
        if btc_5m and eth_5m:
            logger.info("Warmup: using %s klines", source)
            return source, btc_5m, eth_5m, btc_1h, eth_1h

        logger.warning(
            "Warmup: %s 5m klines incomplete (BTC=%d, ETH=%d)",
            source, len(btc_5m), len(eth_5m),
        )

    return "", [], [], [], []


async def _fetch_one(
    exchange: BaseExchange,
    symbol: str,
    interval: str,
    limit: int,
    label: str,
) -> list[Candle]:
    """단일 kline 요청 + 에러 핸들링."""
    try:
        raw = await exchange.get_klines(symbol, interval, limit=limit)
        logger.info("Warmup: %s raw response type=%s len=%s",
                     label, type(raw).__name__, len(raw) if isinstance(raw, list) else "N/A")

        if not isinstance(raw, list):
            logger.warning("Warmup: %s unexpected response: %s", label, repr(raw)[:200])
            return []

        if not raw:
            logger.warning("Warmup: %s returned empty list", label)
            return []

        candles = _klines_to_candles(raw)
        logger.info("Warmup: loaded %d %s candles (first close=%.2f, last close=%.2f)",
                     len(candles), label,
                     candles[0].close if candles else 0,
                     candles[-1].close if candles else 0)
        return candles

    except Exception as e:
        logger.warning("Warmup: %s fetch failed: %s (%s)", label, e, type(e).__name__)
        return []


async def _fetch_klines(
    exchange: BaseExchange,
    btc_sym: str,
    eth_sym: str,
) -> tuple[list[Candle], list[Candle], list[Candle], list[Candle]]:
    """거래소에서 5분봉/1시간봉 klines를 가져와 Candle로 변환."""
    btc_5m, eth_5m, btc_1h, eth_1h = await asyncio.gather(
        _fetch_one(exchange, btc_sym, "5m", 200, "BTC 5m"),
        _fetch_one(exchange, eth_sym, "5m", 200, "ETH 5m"),
        _fetch_one(exchange, btc_sym, "1h", 100, "BTC 1h"),
        _fetch_one(exchange, eth_sym, "1h", 100, "ETH 1h"),
    )
    return btc_5m, eth_5m, btc_1h, eth_1h


async def _fetch_binance_klines() -> tuple[list[Candle], list[Candle], list[Candle], list[Candle]]:
    """Binance USD-M Futures에서 warmup용 5분봉/1시간봉을 가져옵니다."""
    timeout = aiohttp.ClientTimeout(total=float(os.getenv("WARMUP_BINANCE_TIMEOUT_SEC", "10")))
    async with aiohttp.ClientSession(timeout=timeout) as session:
        btc_5m, eth_5m, btc_1h, eth_1h = await asyncio.gather(
            _fetch_binance_one(session, BINANCE_WARMUP_SYMBOLS["BTC"], "5m", 200, "Binance BTC 5m"),
            _fetch_binance_one(session, BINANCE_WARMUP_SYMBOLS["ETH"], "5m", 200, "Binance ETH 5m"),
            _fetch_binance_one(session, BINANCE_WARMUP_SYMBOLS["BTC"], "1h", 100, "Binance BTC 1h"),
            _fetch_binance_one(session, BINANCE_WARMUP_SYMBOLS["ETH"], "1h", 100, "Binance ETH 1h"),
        )
    return btc_5m, eth_5m, btc_1h, eth_1h


async def _fetch_binance_one(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    limit: int,
    label: str,
) -> list[Candle]:
    """단일 Binance kline 요청 + 에러 핸들링."""
    try:
        url = os.getenv("WARMUP_BINANCE_KLINES_URL", BINANCE_FAPI_KLINES_URL)
        async with session.get(url, params={"symbol": symbol, "interval": interval, "limit": limit}) as resp:
            if resp.status != 200:
                body = await resp.text()
                raise RuntimeError(f"HTTP {resp.status}: {body[:200]}")
            raw = await resp.json()

        if not isinstance(raw, list):
            logger.warning("Warmup: %s unexpected response: %s", label, repr(raw)[:200])
            return []

        candles = _binance_klines_to_candles(raw)
        logger.info(
            "Warmup: loaded %d %s candles (first close=%.2f, last close=%.2f)",
            len(candles), label,
            candles[0].close if candles else 0,
            candles[-1].close if candles else 0,
        )
        return candles

    except Exception as e:
        logger.warning("Warmup: %s fetch failed: %s (%s)", label, e, type(e).__name__)
        return []


def _klines_to_candles(klines: List[Dict[str, Any]]) -> list[Candle]:
    """거래소 kline 응답을 Candle 리스트로 변환."""
    candles = []
    for k in klines:
        try:
            candles.append(Candle(
                open_time=int(k.get("open_time", 0)),
                close_time=int(k.get("close_time", 0)),
                open=float(k.get("open", 0)),
                high=float(k.get("high", 0)),
                low=float(k.get("low", 0)),
                close=float(k.get("close", 0)),
                volume=int(float(k.get("volume", 0))),
                is_closed=True,
            ))
        except (TypeError, ValueError, AttributeError) as e:
            logger.warning("Warmup: skipping malformed kline: %s (error: %s)", repr(k)[:100], e)
            continue
    return candles


def _binance_klines_to_candles(klines: List[List[Any]]) -> list[Candle]:
    """Binance USD-M Futures kline 배열 응답을 Candle 리스트로 변환."""
    candles = []
    for k in klines:
        try:
            candles.append(Candle(
                open_time=int(k[0]),
                close_time=int(k[6]),
                open=float(k[1]),
                high=float(k[2]),
                low=float(k[3]),
                close=float(k[4]),
                volume=int(float(k[5])),
                is_closed=True,
            ))
        except (TypeError, ValueError, IndexError) as e:
            logger.warning("Warmup: skipping malformed Binance kline: %s (error: %s)", repr(k)[:100], e)
            continue
    return candles


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _inject_candles(buffer, candles: list[Candle]) -> None:
    """Candle 리스트를 deque에 주입 (기존 데이터 클리어 후)."""
    buffer.clear()
    for c in candles:
        buffer.append(c)


def _prefill_spreads(
    signal_engine: MultiTimeframeSignalEngine,
    price_buffer: PriceBuffer,
) -> None:
    """
    로드된 캔들에서 스프레드 히스토리를 계산하여
    signal_engine._spread_5m, _spread_1h를 채웁니다.
    """
    # 5분봉 스프레드: 연속 2개 캔들의 수익률 차이
    btc_5m_closes = [c.close for c in price_buffer.btc.candles_5m]
    eth_5m_closes = [c.close for c in price_buffer.eth.candles_5m]

    min_len = min(len(btc_5m_closes), len(eth_5m_closes))
    if min_len >= 2:
        btc_prices = btc_5m_closes[-min_len:]
        eth_prices = eth_5m_closes[-min_len:]

        signal_engine._spread_5m.clear()
        for i in range(1, len(btc_prices)):
            if btc_prices[i - 1] == 0 or eth_prices[i - 1] == 0:
                continue
            btc_ret = (btc_prices[i] - btc_prices[i - 1]) / btc_prices[i - 1] * 100
            eth_ret = (eth_prices[i] - eth_prices[i - 1]) / eth_prices[i - 1] * 100
            signal_engine._spread_5m.append(eth_ret - btc_ret)

    # 1시간봉 스프레드
    btc_1h_closes = [c.close for c in price_buffer.btc.candles_1h]
    eth_1h_closes = [c.close for c in price_buffer.eth.candles_1h]

    min_len = min(len(btc_1h_closes), len(eth_1h_closes))
    if min_len >= 2:
        btc_prices = btc_1h_closes[-min_len:]
        eth_prices = eth_1h_closes[-min_len:]

        signal_engine._spread_1h.clear()
        for i in range(1, len(btc_prices)):
            if btc_prices[i - 1] == 0 or eth_prices[i - 1] == 0:
                continue
            btc_ret = (btc_prices[i] - btc_prices[i - 1]) / btc_prices[i - 1] * 100
            eth_ret = (eth_prices[i] - eth_prices[i - 1]) / eth_prices[i - 1] * 100
            signal_engine._spread_1h.append(eth_ret - btc_ret)
