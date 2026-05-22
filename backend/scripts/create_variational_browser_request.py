"""Create Variational browser-gate leg requests with external fair prices.

This script does not place an order. It writes request JSON files that
tools/variational-browser can use to prepare the Variational order panel,
screenshot, and optionally click after Telegram approval. Keep dry-run enabled
until selectors and screen state are verified.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv

from backend.bot.fair_price import FairPriceOracle

ROOT = Path(__file__).resolve().parents[2]
VARIATIONAL_BROWSER_DIR = ROOT / "tools" / "variational-browser"
DEFAULT_REQUEST_DIR = VARIATIONAL_BROWSER_DIR / "runtime" / "requests"

async def main() -> None:
    load_dotenv(ROOT / "backend" / ".env")
    load_dotenv(VARIATIONAL_BROWSER_DIR / ".env", override=True)
    args = parse_args()
    oracle = FairPriceOracle()
    fair_prices = await oracle.fetch(["BTC", "ETH"])
    missing = [symbol for symbol in ("BTC", "ETH") if symbol not in fair_prices]
    if missing:
        raise SystemExit(f"Missing fair price for: {', '.join(missing)}")

    requests = build_requests(args, fair_prices)
    request_dir = Path(args.request_dir).expanduser()
    request_dir.mkdir(parents=True, exist_ok=True)
    for request in requests:
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
    parser.add_argument("--action", choices=["open", "close"], default="open")
    parser.add_argument("--zscore", type=float, default=None)
    parser.add_argument("--divergence-pct", type=float, default=None)
    parser.add_argument("--base-url", default=os.getenv("VARIATIONAL_BROWSER_BASE_URL", os.getenv("VARIATIONAL_BROWSER_URL", "https://omni.variational.io")))
    parser.add_argument("--legs", choices=["both", "BTC", "ETH"], default="both")
    parser.add_argument("--confirm-selector", default=os.getenv("VARIATIONAL_BROWSER_CONFIRM_SELECTOR", "auto"))
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=env_bool("VARIATIONAL_BROWSER_DRY_RUN", True))
    parser.add_argument("--request-dir", default=os.getenv("VARIATIONAL_BROWSER_REQUEST_DIR", str(DEFAULT_REQUEST_DIR)))
    parser.add_argument("--steps-json", default="")
    parser.add_argument("--max-age-sec", type=int, default=int(os.getenv("VARIATIONAL_REQUEST_MAX_AGE_SEC", "300")))
    parser.add_argument("--approval-timeout-sec", type=int, default=int(os.getenv("VARIATIONAL_BROWSER_APPROVAL_TIMEOUT_SEC", "120")))
    parser.add_argument("--quantity", default="")
    parser.add_argument("--btc-quantity", default="")
    parser.add_argument("--eth-quantity", default="")
    parser.add_argument("--reduce-only", action=argparse.BooleanOptionalAction, default=None)
    parser.add_argument("--btc-qty-decimals", type=int, default=6)
    parser.add_argument("--eth-qty-decimals", type=int, default=4)
    return parser.parse_args()


def build_requests(args: argparse.Namespace, fair_prices: dict) -> list[dict]:
    now = datetime.now(timezone.utc).isoformat()
    steps = json.loads(args.steps_json) if args.steps_json else []
    if not isinstance(steps, list):
        raise SystemExit("--steps-json must decode to a JSON array")

    btc = fair_prices["BTC"]
    eth = fair_prices["ETH"]
    if args.quantity and args.legs == "both":
        raise SystemExit("--quantity requires --legs BTC or --legs ETH. Use --btc-quantity/--eth-quantity for both legs.")

    legs = selected_legs(args.direction, args.legs, args.action)
    base_url = normalize_base_url(args.base_url)
    request_group = f"variational-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}-{uuid4().hex[:8]}"
    requests = []

    for symbol, side in legs:
        fair = fair_prices[symbol]
        decimals = args.btc_qty_decimals if symbol == "BTC" else args.eth_qty_decimals
        quantity = requested_quantity(args, symbol) or format_quantity(args.size_usd / fair.price, decimals)
        reduce_only = args.reduce_only if args.reduce_only is not None else args.action == "close"
        summary = "\n".join([
            f"Variational browser request: {args.direction}",
            f"action: {args.action}",
            f"leg: {symbol} {side}",
            f"quantity: {quantity} {symbol}",
            f"size_usd: {args.size_usd:.2f}",
            f"reduce_only: {reduce_only}",
            f"fair_{symbol.lower()}: {fair.price:.6f} ({','.join(fair.source_names)})",
            f"fair_btc: {btc.price:.2f} ({','.join(btc.source_names)})",
            f"fair_eth: {eth.price:.4f} ({','.join(eth.source_names)})",
            "decision_price: external median fair price, not Variational screen price",
        ])

        requests.append({
            "id": f"{request_group}-{symbol.lower()}",
            "createdAt": now,
            "url": f"{base_url}/perpetual/{symbol}",
            "summary": summary,
            "confirmSelector": args.confirm_selector,
            "dryRun": args.dry_run,
            "maxAgeSec": args.max_age_sec,
            "approvalTimeoutMs": args.approval_timeout_sec * 1000,
            "steps": steps,
            "variationalOrder": {
                "symbol": symbol,
                "side": side,
                "orderType": "market",
                "quantity": quantity,
                "sizeUsd": args.size_usd,
                "fairPrice": fair.price,
                "pairDirection": args.direction,
                "action": args.action,
                "reduceOnly": reduce_only,
            },
            "signal": {
                "direction": args.direction,
                "action": args.action,
                "leg": {"symbol": symbol, "side": side, "quantity": quantity, "reduce_only": reduce_only},
                "size_usd_per_leg": args.size_usd,
                "zscore": args.zscore,
                "divergence_pct": args.divergence_pct,
                "fair_price": {
                    "BTC": serialize_fair_price(btc),
                    "ETH": serialize_fair_price(eth),
                },
            },
        })
    return requests


def selected_legs(direction: str, legs_arg: str, action: str = "open") -> list[tuple[str, str]]:
    mapping = {
        "LONG_BTC_SHORT_ETH": [("BTC", "BUY"), ("ETH", "SELL")],
        "SHORT_BTC_LONG_ETH": [("BTC", "SELL"), ("ETH", "BUY")],
    }[direction]
    if action == "close":
        inverse = {"BUY": "SELL", "SELL": "BUY"}
        mapping = [(symbol, inverse[side]) for symbol, side in mapping]
    if legs_arg == "both":
        return mapping
    return [leg for leg in mapping if leg[0] == legs_arg]


def requested_quantity(args: argparse.Namespace, symbol: str) -> str:
    raw = args.quantity or (args.btc_quantity if symbol == "BTC" else args.eth_quantity)
    if not raw:
        return ""
    value = str(raw).strip()
    if not value or float(value) <= 0:
        raise SystemExit(f"Invalid {symbol} quantity: {raw}")
    return value


def env_bool(name: str, fallback: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return fallback
    return value.strip().lower() in {"1", "true", "yes", "on"}


def normalize_base_url(value: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise SystemExit(f"Invalid --base-url: {value}")
    return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")


def format_quantity(value: float, decimals: int) -> str:
    if value <= 0:
        raise SystemExit(f"Invalid quantity: {value}")
    fixed = f"{value:.{decimals}f}"
    if float(fixed) <= 0:
        raise SystemExit(f"Quantity rounds to zero at {decimals} decimals: {value}")
    return fixed.rstrip("0").rstrip(".")


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
