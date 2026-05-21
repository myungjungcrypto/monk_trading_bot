"""Create a Variational browser-gate request with external fair prices.

This script does not place an order. It writes a request JSON that
tools/variational-browser can screenshot and optionally click after Telegram
approval. Keep dry-run enabled until selectors and screen state are verified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from backend.bot.fair_price import FairPriceOracle

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_REQUEST_DIR = ROOT / "tools" / "variational-browser" / "runtime" / "requests"


async def main() -> None:
    args = parse_args()
    oracle = FairPriceOracle()
    fair_prices = await oracle.fetch(["BTC", "ETH"])
    missing = [symbol for symbol in ("BTC", "ETH") if symbol not in fair_prices]
    if missing:
        raise SystemExit(f"Missing fair price for: {', '.join(missing)}")

    request = build_request(args, fair_prices)
    request_dir = Path(args.request_dir).expanduser()
    request_dir.mkdir(parents=True, exist_ok=True)
    path = request_dir / f"{request['id']}.json"
    path.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
    print(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--direction",
        choices=["LONG_BTC_SHORT_ETH", "SHORT_BTC_LONG_ETH"],
        required=True,
    )
    parser.add_argument("--size-usd", type=float, default=float(os.getenv("POSITION_SIZE_USD", "50")))
    parser.add_argument("--zscore", type=float, default=None)
    parser.add_argument("--divergence-pct", type=float, default=None)
    parser.add_argument("--url", default=os.getenv("VARIATIONAL_BROWSER_URL", "https://omni.variational.io/perpetual/BTC"))
    parser.add_argument("--confirm-selector", default=os.getenv("VARIATIONAL_BROWSER_CONFIRM_SELECTOR", "body"))
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--request-dir", default=str(DEFAULT_REQUEST_DIR))
    parser.add_argument("--steps-json", default="")
    return parser.parse_args()


def build_request(args: argparse.Namespace, fair_prices: dict) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    request_id = f"variational-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    steps = json.loads(args.steps_json) if args.steps_json else []
    if not isinstance(steps, list):
        raise SystemExit("--steps-json must decode to a JSON array")

    btc = fair_prices["BTC"]
    eth = fair_prices["ETH"]
    summary = "\n".join([
        f"Variational browser request: {args.direction}",
        f"size_usd_per_leg: {args.size_usd:.2f}",
        f"fair_btc: {btc.price:.2f} ({','.join(btc.source_names)})",
        f"fair_eth: {eth.price:.4f} ({','.join(eth.source_names)})",
        "decision_price: external median fair price, not Variational screen price",
    ])

    return {
        "id": request_id,
        "createdAt": now,
        "url": args.url,
        "summary": summary,
        "confirmSelector": args.confirm_selector,
        "dryRun": args.dry_run,
        "steps": steps,
        "signal": {
            "direction": args.direction,
            "size_usd_per_leg": args.size_usd,
            "zscore": args.zscore,
            "divergence_pct": args.divergence_pct,
            "fair_price": {
                "BTC": serialize_fair_price(btc),
                "ETH": serialize_fair_price(eth),
            },
        },
    }


def serialize_fair_price(fair) -> dict:
    return {
        "price": fair.price,
        "sources": [
            {
                "source": sample.source,
                "price": sample.price,
                "age_ms": sample.age_ms,
            }
            for sample in fair.sources
        ],
        "deviation_bps": fair.deviations_bps(),
    }


if __name__ == "__main__":
    asyncio.run(main())
