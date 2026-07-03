"""Live smoke test for the Variational connector — safe by default.

Runs the real flow against the live backend using the key in .env:

    1. SIWE login (wallet signature -> session JWT)
    2. Account balance (GET /api/portfolio)
    3. Open positions (GET /api/positions)
    4. Mark prices for BTC and ETH (indicative quotes)
    5. A pair-order rehearsal: quotes both legs and, in dry-run mode (the
       default), logs the exact order bodies WITHOUT submitting them.

Usage (on the server):

    cd backend && source .venv/bin/activate
    python -m tools.smoke_test              # respects VARIATIONAL_DRY_RUN in .env
    python -m tools.smoke_test --size 12    # rehearsal notional per leg in USD

Only if VARIATIONAL_DRY_RUN=false in .env AND you pass --live does step 5
actually submit orders. Never do that before a dry run has looked correct.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from bot.config import get_variational_settings
from bot.exchanges.base import Order, Side
from bot.exchanges.variational import VariationalConnector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smoke")


async def main(size_usd: Decimal, allow_live: bool) -> None:
    settings = get_variational_settings()
    if not settings.dry_run and not allow_live:
        log.warning("VARIATIONAL_DRY_RUN=false but --live not passed; forcing dry-run for safety.")
        settings = settings.model_copy(update={"dry_run": True})

    async with VariationalConnector(settings) as vx:
        log.info("[1/5] logged in as %s (dry_run=%s)", vx._address, settings.dry_run)

        bal = await vx.get_balance()
        log.info("[2/5] balance=%s USDC, upnl=%s", bal["balance"], bal["upnl"])

        for sym in ("BTC", "ETH"):
            pos = await vx.get_position(sym)
            log.info("[3/5] position %s: %s", sym, pos or "flat")

        btc_mark = await vx.get_mark_price("BTC")
        eth_mark = await vx.get_mark_price("ETH")
        log.info("[4/5] mark BTC=%s ETH=%s", btc_mark, eth_mark)

        log.info("[5/5] pair rehearsal: LONG BTC / SHORT ETH, %s USD per leg", size_usd)
        btc_res, eth_res = await asyncio.gather(
            vx.place_order(Order(symbol="BTC", side=Side.BUY, size_usd=size_usd)),
            vx.place_order(Order(symbol="ETH", side=Side.SELL, size_usd=size_usd)),
        )
        for res in (btc_res, eth_res):
            log.info(
                "  %s %s $%s -> accepted=%s dry_run=%s est_price=%s order_id=%s",
                res.side.value, res.symbol, res.size_usd,
                res.accepted, res.dry_run, res.filled_price, res.order_id,
            )

    log.info("Smoke test finished.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Variational connector live smoke test.")
    parser.add_argument("--size", type=Decimal, default=Decimal("10"),
                        help="Rehearsal notional per leg in USD (default 10).")
    parser.add_argument("--live", action="store_true",
                        help="Allow real order submission (also requires VARIATIONAL_DRY_RUN=false).")
    args = parser.parse_args()
    asyncio.run(main(args.size, args.live))
