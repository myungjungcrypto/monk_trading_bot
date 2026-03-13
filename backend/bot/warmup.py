"""
Warm-up — 시작 시 거래소 REST API에서 과거 캔들을 로드하여
PriceBuffer와 SignalEngine을 즉시 사용 가능 상태로 만듭니다.

재시작해도 10시간 대기 없이 바로 시그널 생성 가능.
"""

import logging
from typing import Dict, List, Any

from backend.bot.exchanges.base import BaseExchange
from backend.bot.price_buffer import Candle, PriceBuffer
from backend.bot.signal import MultiTimeframeSignalEngine

logger = logging.getLogger(__name__)


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
        btc_5m, eth_5m, btc_1h, eth_1h = await _fetch_klines(
            exchange, btc_sym, eth_sym,
        )

        if not btc_5m or not eth_5m:
            logger.warning("Warmup: failed to fetch 5m klines")
            return False

        # PriceBuffer에 캔들 주입
        _inject_candles(price_buffer.btc.candles_5m, btc_5m)
        _inject_candles(price_buffer.eth.candles_5m, eth_5m)
        _inject_candles(price_buffer.btc.candles_1h, btc_1h)
        _inject_candles(price_buffer.eth.candles_1h, eth_1h)

        # SignalEngine 스프레드 히스토리 pre-fill
        _prefill_spreads(signal_engine, price_buffer)

        logger.info(
            "Warmup complete: 5m=%d/%d candles, 1h=%d/%d candles, "
            "spread_5m=%d, spread_1h=%d",
            len(price_buffer.btc.candles_5m), len(price_buffer.eth.candles_5m),
            len(price_buffer.btc.candles_1h), len(price_buffer.eth.candles_1h),
            len(signal_engine._spread_5m), len(signal_engine._spread_1h),
        )
        return True

    except Exception as e:
        logger.error("Warmup failed: %s", e, exc_info=True)
        return False


async def _fetch_klines(
    exchange: BaseExchange,
    btc_sym: str,
    eth_sym: str,
) -> tuple[list[Candle], list[Candle], list[Candle], list[Candle]]:
    """거래소에서 5분봉/1시간봉 klines를 가져와 Candle로 변환."""
    import asyncio

    results = await asyncio.gather(
        exchange.get_klines(btc_sym, "5m", limit=200),
        exchange.get_klines(eth_sym, "5m", limit=200),
        exchange.get_klines(btc_sym, "1h", limit=100),
        exchange.get_klines(eth_sym, "1h", limit=100),
        return_exceptions=True,
    )

    out: list[list[Candle]] = []
    labels = ["BTC 5m", "ETH 5m", "BTC 1h", "ETH 1h"]
    for i, (result, label) in enumerate(zip(results, labels)):
        if isinstance(result, Exception):
            logger.warning("Warmup: %s kline fetch failed: %s", label, result)
            out.append([])
        else:
            candles = _klines_to_candles(result)
            logger.info("Warmup: loaded %d %s candles", len(candles), label)
            out.append(candles)

    return out[0], out[1], out[2], out[3]


def _klines_to_candles(klines: List[Dict[str, Any]]) -> list[Candle]:
    """거래소 kline 응답을 Candle 리스트로 변환."""
    candles = []
    for k in klines:
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
    return candles


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
        # 양쪽 길이 맞추기 (뒤에서부터)
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
