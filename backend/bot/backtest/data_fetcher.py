"""
Data Fetcher — Binance Futures 1분봉 OHLCV 다운로드 & CSV 캐싱.

API 키 불필요. 월별 CSV 파일로 캐싱하여 재다운로드 방지.
"""

import asyncio
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import aiohttp
import pandas as pd

logger = logging.getLogger(__name__)

BINANCE_FAPI_URL = "https://fapi.binance.com/fapi/v1/klines"
DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
MAX_CANDLES_PER_REQUEST = 1500
RATE_LIMIT_DELAY = 0.3  # seconds between requests


async def fetch_klines(
    session: aiohttp.ClientSession,
    symbol: str,
    interval: str,
    start_ms: int,
    end_ms: int,
    limit: int = MAX_CANDLES_PER_REQUEST,
    max_retries: int = 5,
) -> list[list]:
    """Binance Futures klines 단일 요청 (429 시 자동 재시도)."""
    params = {
        "symbol": symbol,
        "interval": interval,
        "startTime": start_ms,
        "endTime": end_ms,
        "limit": limit,
    }
    for attempt in range(max_retries):
        async with session.get(BINANCE_FAPI_URL, params=params) as resp:
            if resp.status == 200:
                return await resp.json()
            if resp.status == 429:
                wait = 2 ** attempt * 5  # 5s, 10s, 20s, 40s, 80s
                logger.warning("Rate limited (429), waiting %ds before retry...", wait)
                await asyncio.sleep(wait)
                continue
            text = await resp.text()
            raise RuntimeError(f"Binance API error {resp.status}: {text}")
    raise RuntimeError(f"Rate limited after {max_retries} retries")


async def download_symbol(
    symbol: str,
    start_date: str,
    end_date: str,
    interval: str = "1m",
) -> pd.DataFrame:
    """
    심볼의 OHLCV 데이터를 다운로드합니다.

    Args:
        symbol: "BTCUSDT" or "ETHUSDT"
        start_date: "2025-03-17" 형식
        end_date: "2026-03-17" 형식
        interval: "1m" (기본값)

    Returns:
        DataFrame with columns: timestamp_ms, open, high, low, close, volume
    """
    start_dt = datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    start_ms = int(start_dt.timestamp() * 1000)
    end_ms = int(end_dt.timestamp() * 1000)

    all_rows: list[list] = []
    current_ms = start_ms

    async with aiohttp.ClientSession() as session:
        while current_ms < end_ms:
            raw = await fetch_klines(
                session, symbol, interval,
                start_ms=current_ms,
                end_ms=end_ms,
                limit=MAX_CANDLES_PER_REQUEST,
            )
            if not raw:
                break

            all_rows.extend(raw)
            # Next batch starts after last candle's open_time
            last_open_time = raw[-1][0]
            if last_open_time <= current_ms:
                break
            current_ms = last_open_time + 1

            await asyncio.sleep(RATE_LIMIT_DELAY)

            if len(all_rows) % 10000 == 0:
                logger.info("%s: %d candles downloaded...", symbol, len(all_rows))

    if not all_rows:
        return pd.DataFrame()

    df = pd.DataFrame(all_rows, columns=[
        "timestamp_ms", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    ])
    df = df[["timestamp_ms", "open", "high", "low", "close", "volume"]]
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    df["timestamp_ms"] = df["timestamp_ms"].astype(int)
    df = df.drop_duplicates(subset=["timestamp_ms"]).sort_values("timestamp_ms").reset_index(drop=True)

    logger.info("%s: %d candles total (%s ~ %s)", symbol, len(df), start_date, end_date)
    return df


def _cache_path(symbol: str, start_date: str, end_date: str) -> Path:
    """캐시 CSV 경로."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{symbol}_1m_{start_date}_{end_date}.csv"


async def load_or_download(
    symbol: str,
    start_date: str,
    end_date: str,
    force: bool = False,
) -> pd.DataFrame:
    """
    캐시된 CSV가 있으면 로드, 없으면 다운로드 후 캐싱.

    Args:
        symbol: "BTCUSDT" or "ETHUSDT"
        start_date: 시작일
        end_date: 종료일
        force: True면 캐시 무시하고 재다운로드
    """
    path = _cache_path(symbol, start_date, end_date)

    if path.exists() and not force:
        logger.info("Loading cached: %s", path.name)
        df = pd.read_csv(path)
        logger.info("Loaded %d candles from cache", len(df))
        return df

    logger.info("Downloading %s from %s to %s...", symbol, start_date, end_date)
    df = await download_symbol(symbol, start_date, end_date)

    if not df.empty:
        df.to_csv(path, index=False)
        logger.info("Cached to: %s", path.name)

    return df


async def load_pair_data(
    start_date: str,
    end_date: str,
    force: bool = False,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    BTC/ETH 양쪽 데이터를 동시에 로드/다운로드합니다.

    Returns:
        (btc_df, eth_df) tuple
    """
    # 순차 다운로드 — 동시 요청 시 Binance rate limit 회피
    btc_df = await load_or_download("BTCUSDT", start_date, end_date, force=force)
    eth_df = await load_or_download("ETHUSDT", start_date, end_date, force=force)
    return btc_df, eth_df
