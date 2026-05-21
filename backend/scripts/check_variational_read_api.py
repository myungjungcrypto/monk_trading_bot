"""Measure Variational Omni read API freshness against Binance Futures.

Usage:
    python -m backend.scripts.check_variational_read_api --samples 300 --interval 1

The script writes a CSV in backend/data by default and prints a compact summary.
It does not place orders or require credentials.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import time
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from backend.bot.variational.market_data import (
    VARIATIONAL_OMNI_BASE_URL,
    FreshnessSample,
    parse_stats,
    summarize_samples,
)

BINANCE_FAPI_URL = "https://fapi.binance.com"
DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "data"


async def main() -> None:
    args = parse_args()
    output_path = args.output or _default_output_path(args.samples)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    samples: list[FreshnessSample] = []
    timeout = aiohttp.ClientTimeout(total=args.timeout)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        with output_path.open("w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=CSV_FIELDS)
            writer.writeheader()

            for idx in range(args.samples):
                started = time.perf_counter()
                sample = await collect_sample(session, args.variational_base_url, args.binance_base_url)
                samples.append(sample)
                writer.writerow(sample_to_row(sample))
                f.flush()

                print_sample(idx + 1, sample)
                elapsed = time.perf_counter() - started
                await asyncio.sleep(max(args.interval - elapsed, 0.0))

    summary = summarize_samples(samples)
    print("\nSummary")
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    print(f"\nCSV: {output_path}")


async def collect_sample(
    session: aiohttp.ClientSession,
    variational_base_url: str,
    binance_base_url: str,
) -> FreshnessSample:
    received_at = datetime.now(timezone.utc)
    started = time.perf_counter()
    error = ""
    stats: Optional[Dict[str, Any]] = None
    binance_prices: Dict[str, Decimal] = {}

    try:
        stats, binance_prices = await asyncio.gather(
            fetch_variational_stats(session, variational_base_url),
            fetch_binance_prices(session, binance_base_url),
        )
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"

    latency_ms = (time.perf_counter() - started) * 1000.0
    if stats is None:
        return FreshnessSample(
            received_at=received_at,
            latency_ms=latency_ms,
            btc=None,
            eth=None,
            error=error or "empty Variational response",
        )

    listings = parse_stats(stats)
    return FreshnessSample(
        received_at=received_at,
        latency_ms=latency_ms,
        btc=listings.get("BTC"),
        eth=listings.get("ETH"),
        binance_btc=binance_prices.get("BTCUSDT"),
        binance_eth=binance_prices.get("ETHUSDT"),
        error=error,
    )


async def fetch_variational_stats(
    session: aiohttp.ClientSession,
    base_url: str,
) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}/metadata/stats"
    async with session.get(url) as resp:
        resp.raise_for_status()
        return await resp.json()


async def fetch_binance_prices(
    session: aiohttp.ClientSession,
    base_url: str,
) -> Dict[str, Decimal]:
    symbols = '["BTCUSDT","ETHUSDT"]'
    url = f"{base_url.rstrip('/')}/fapi/v1/ticker/price?symbols={symbols}"
    async with session.get(url) as resp:
        resp.raise_for_status()
        rows = await resp.json()
    prices: Dict[str, Decimal] = {}
    for row in rows:
        prices[str(row["symbol"])] = Decimal(str(row["price"]))
    return prices


CSV_FIELDS = [
    "received_at",
    "latency_ms",
    "error",
    "btc_mark",
    "eth_mark",
    "btc_binance",
    "eth_binance",
    "btc_mark_diff_bps",
    "eth_mark_diff_bps",
    "btc_quote_updated_at",
    "eth_quote_updated_at",
    "btc_quote_age_sec",
    "eth_quote_age_sec",
    "btc_1k_bid",
    "btc_1k_ask",
    "eth_1k_bid",
    "eth_1k_ask",
    "btc_100k_bid",
    "btc_100k_ask",
    "eth_100k_bid",
    "eth_100k_ask",
]


def sample_to_row(sample: FreshnessSample) -> Dict[str, Any]:
    btc = sample.btc
    eth = sample.eth
    return {
        "received_at": sample.received_at.isoformat(),
        "latency_ms": f"{sample.latency_ms:.1f}",
        "error": sample.error,
        "btc_mark": _fmt(btc.mark_price if btc else None),
        "eth_mark": _fmt(eth.mark_price if eth else None),
        "btc_binance": _fmt(sample.binance_btc),
        "eth_binance": _fmt(sample.binance_eth),
        "btc_mark_diff_bps": _fmt(sample.btc_mark_diff_bps),
        "eth_mark_diff_bps": _fmt(sample.eth_mark_diff_bps),
        "btc_quote_updated_at": btc.quote_updated_at.isoformat() if btc and btc.quote_updated_at else "",
        "eth_quote_updated_at": eth.quote_updated_at.isoformat() if eth and eth.quote_updated_at else "",
        "btc_quote_age_sec": _fmt_float(sample.btc_quote_age_sec),
        "eth_quote_age_sec": _fmt_float(sample.eth_quote_age_sec),
        "btc_1k_bid": _fmt(btc.size_1k.bid if btc else None),
        "btc_1k_ask": _fmt(btc.size_1k.ask if btc else None),
        "eth_1k_bid": _fmt(eth.size_1k.bid if eth else None),
        "eth_1k_ask": _fmt(eth.size_1k.ask if eth else None),
        "btc_100k_bid": _fmt(btc.size_100k.bid if btc else None),
        "btc_100k_ask": _fmt(btc.size_100k.ask if btc else None),
        "eth_100k_bid": _fmt(eth.size_100k.bid if eth else None),
        "eth_100k_ask": _fmt(eth.size_100k.ask if eth else None),
    }


def print_sample(idx: int, sample: FreshnessSample) -> None:
    if sample.error:
        print(f"{idx:04d} ERROR {sample.error} latency={sample.latency_ms:.0f}ms")
        return
    print(
        f"{idx:04d} "
        f"lat={sample.latency_ms:.0f}ms "
        f"BTC age={_fmt_float(sample.btc_quote_age_sec)}s diff={_fmt(sample.btc_mark_diff_bps)}bp "
        f"ETH age={_fmt_float(sample.eth_quote_age_sec)}s diff={_fmt(sample.eth_mark_diff_bps)}bp"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=300)
    parser.add_argument("--interval", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=8.0)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--variational-base-url", default=VARIATIONAL_OMNI_BASE_URL)
    parser.add_argument("--binance-base-url", default=BINANCE_FAPI_URL)
    return parser.parse_args()


def _default_output_path(samples: int) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    return DEFAULT_OUTPUT_DIR / f"variational_read_api_freshness_{stamp}_{samples}.csv"


def _fmt(value: Optional[Decimal]) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _fmt_float(value: Optional[float]) -> str:
    if value is None:
        return ""
    return f"{value:.3f}"


if __name__ == "__main__":
    asyncio.run(main())

