"""
Tick Generator — 1분봉 OHLCV → 합성 틱 변환.

1분봉당 4개 틱을 생성하고, BTC/ETH 틱을 시간순으로 인터리빙합니다.
"""

from dataclasses import dataclass
from typing import Iterator

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


def generate_interleaved_ticks(
    btc_df: pd.DataFrame,
    eth_df: pd.DataFrame,
) -> Iterator[SyntheticTick]:
    """
    BTC/ETH 1분봉을 합성 틱으로 변환하고 시간순 인터리빙.

    Yields:
        SyntheticTick (시간순 정렬)
    """
    btc_ticks: list[SyntheticTick] = []
    eth_ticks: list[SyntheticTick] = []

    for _, row in btc_df.iterrows():
        btc_ticks.extend(candle_to_ticks(row, "BTC"))
    for _, row in eth_df.iterrows():
        eth_ticks.extend(candle_to_ticks(row, "ETH"))

    # 두 리스트를 시간순 머지 (merge sort)
    i, j = 0, 0
    while i < len(btc_ticks) and j < len(eth_ticks):
        if btc_ticks[i].timestamp_ms <= eth_ticks[j].timestamp_ms:
            yield btc_ticks[i]
            i += 1
        else:
            yield eth_ticks[j]
            j += 1

    while i < len(btc_ticks):
        yield btc_ticks[i]
        i += 1
    while j < len(eth_ticks):
        yield eth_ticks[j]
        j += 1
