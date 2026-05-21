"""Check external fair prices for Variational execution.

Example:
    python -m backend.scripts.check_fair_price --json
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict
from typing import Dict

from backend.bot.fair_price import FairPrice, FairPriceConfig, FairPriceOracle


async def main() -> None:
    args = parse_args()
    config = FairPriceConfig.from_env()
    config = FairPriceConfig(
        symbols=tuple(args.symbols),
        min_sources=args.min_sources,
        request_timeout_sec=args.timeout,
        source_max_age_ms=int(args.max_age_sec * 1000),
        max_deviation_bps=args.max_deviation_bps,
        binance_base_url=config.binance_base_url,
        hyperliquid_info_url=config.hyperliquid_info_url,
        lighter_ws_url=config.lighter_ws_url,
        lighter_market_ids=config.lighter_market_ids,
        enabled_sources=tuple(args.sources.split(",")),
    )

    oracle = FairPriceOracle(config)
    fair_prices = await oracle.fetch()
    if args.json:
        print(json.dumps(to_json(fair_prices), indent=2, ensure_ascii=False))
    else:
        print_table(fair_prices)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", nargs="+", default=["BTC", "ETH"])
    parser.add_argument("--sources", default="binance,lighter,hyperliquid")
    parser.add_argument("--min-sources", type=int, default=2)
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--max-age-sec", type=float, default=15.0)
    parser.add_argument("--max-deviation-bps", type=float, default=500.0)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def to_json(fair_prices: Dict[str, FairPrice]) -> dict:
    return {
        symbol: {
            "fair_price": fair.price,
            "sources": [asdict(sample) for sample in fair.sources],
            "ignored": [asdict(sample) for sample in fair.ignored],
            "deviation_bps": fair.deviations_bps(),
        }
        for symbol, fair in fair_prices.items()
    }


def print_table(fair_prices: Dict[str, FairPrice]) -> None:
    if not fair_prices:
        print("No fair prices available. Need at least the configured minimum source count.")
        return
    for symbol, fair in fair_prices.items():
        print(f"{symbol} fair={fair.price:.6f} sources={','.join(fair.source_names)}")
        for sample in fair.sources:
            dev = fair.deviations_bps().get(sample.source, 0.0)
            print(
                f"  - {sample.source:12s} price={sample.price:.6f} "
                f"dev={dev:+.2f}bps age={sample.age_ms / 1000:.1f}s"
            )
        for sample in fair.ignored:
            print(f"  - ignored {sample.source:8s} price={sample.price:.6f}")


if __name__ == "__main__":
    asyncio.run(main())
