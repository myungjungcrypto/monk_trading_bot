"""
Tick Generator — 1분봉 OHLCV → 합성 틱 변환.

1분봉당 4개 틱을 생성하고, BTC/ETH 틱을 시간순으로 인터리빙합니다.
"""

from dataclasses import dataclass
from typing import Iterator, Optional

import pandas as pd


@dataclass
class SyntheticTick:
    """합성 틱."""
    symbol: str         # "BTC" or "ETH"
    price: float
    timestamp_ms: int


def candle_to_ticks(
    row: pd.Series,
    symbol: str,
) -> list[SyntheticTick]:
    """
    1분봉 → 4개 합성 틱.

    틱 순서는 캔들 방향에 따라 결정:
    - 양봉 (close >= open): O → L → H → C
    - 음봉 (close < open):  O → H → L → C
    """
    ts = int(row["timestamp_ms"])
    o, h, l, c = row["open"], row["high"], row["low"], row["close"]

    if c >= o:  # 양봉
        return [
            SyntheticTick(symbol, o, ts),
            SyntheticTick(symbol, l, ts + 15_000),
            SyntheticTick(symbol, h, ts + 30_000),
            SyntheticTick(symbol, c, ts + 55_000),
        ]
    else:  # 음봉
        return [
            SyntheticTick(symbol, o, ts),
            SyntheticTick(symbol, h, ts + 15_000),
            SyntheticTick(symbol, l, ts + 30_000),
            SyntheticTick(symbol, c, ts + 55_000),
        ]


def _iter_candle_ticks(df: pd.DataFrame, symbol: str) -> Iterator[SyntheticTick]:
    """DataFrame을 전체 tick list로 만들지 않고 candle별 synthetic tick을 스트리밍합니다."""
    for row in df.itertuples(index=False):
        ts = int(row.timestamp_ms)
        o, h, l, c = row.open, row.high, row.low, row.close

        if c >= o:
            yield SyntheticTick(symbol, o, ts)
            yield SyntheticTick(symbol, l, ts + 15_000)
            yield SyntheticTick(symbol, h, ts + 30_000)
            yield SyntheticTick(symbol, c, ts + 55_000)
        else:
            yield SyntheticTick(symbol, o, ts)
            yield SyntheticTick(symbol, h, ts + 15_000)
            yield SyntheticTick(symbol, l, ts + 30_000)
            yield SyntheticTick(symbol, c, ts + 55_000)


def _next_or_none(iterator: Iterator[SyntheticTick]) -> Optional[SyntheticTick]:
    try:
        return next(iterator)
    except StopIteration:
        return None


def generate_interleaved_ticks(
    btc_df: pd.DataFrame,
    eth_df: pd.DataFrame,
) -> Iterator[SyntheticTick]:
    """
    BTC/ETH 1분봉을 합성 틱으로 변환하고 시간순 인터리빙.

    Yields:
        SyntheticTick (시간순 정렬)
    """
    btc_iter = _iter_candle_ticks(btc_df, "BTC")
    eth_iter = _iter_candle_ticks(eth_df, "ETH")
    btc_tick = _next_or_none(btc_iter)
    eth_tick = _next_or_none(eth_iter)

    while btc_tick is not None or eth_tick is not None:
        if eth_tick is None or (
            btc_tick is not None and btc_tick.timestamp_ms <= eth_tick.timestamp_ms
        ):
            yield btc_tick
            btc_tick = _next_or_none(btc_iter)
        else:
            yield eth_tick
            eth_tick = _next_or_none(eth_iter)
