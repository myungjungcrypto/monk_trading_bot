"""Show (and optionally flatten) live Variational positions via the direct API.

    # just look
    python -m backend.scripts.variational_api_positions

    # flatten any open BTC/ETH position with reduce-only market orders
    VARIATIONAL_DRY_RUN=false python -m backend.scripts.variational_api_positions --flatten

Read-only by default. --flatten only ever REDUCES (reduce_only=True), so it can
close but never open exposure.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from dotenv import load_dotenv

from backend.bot.variational.api.api_client import VariationalConnector
from backend.bot.variational.api.api_config import get_variational_settings
from backend.bot.variational.api.api_types import Order, Side

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("positions")


async def main(flatten: bool) -> None:
    load_dotenv()
    settings = get_variational_settings()
    async with VariationalConnector(settings) as vx:
        log.info("account %s", vx._address)
        for sym in ("BTC", "ETH"):
            pos = await vx.get_position(sym)
            if pos is None:
                log.info("  %s: flat", sym)
                continue
            log.info("  %s: %s size=%s entry=%s uPnL=%s",
                     sym, pos.side.value, pos.size, pos.entry_price, pos.unrealized_pnl)
            if flatten and not settings.dry_run:
                # Opposite side, exact size, reduce-only: can only close.
                close_side = pos.side.opposite
                res = await vx.place_order(
                    Order(symbol=sym, side=close_side, size_usd=Decimal(0),
                          size_base=pos.size, reduce_only=True)
                )
                log.info("    flatten %s %s size=%s -> accepted=%s order_id=%s",
                         close_side.value, sym, pos.size, res.accepted, res.order_id)
            elif flatten and settings.dry_run:
                log.warning("    --flatten ignored: VARIATIONAL_DRY_RUN=true")

        if flatten and not settings.dry_run:
            await asyncio.sleep(3)  # let the venue settle before re-checking
            log.info("post-flatten state:")
            for sym in ("BTC", "ETH"):
                pos = await vx.get_position(sym)
                log.info("  %s: %s", sym, f"{pos.side.value} {pos.size}" if pos else "flat")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Show/flatten Variational positions via API.")
    p.add_argument("--flatten", action="store_true", help="Close open BTC/ETH with reduce-only orders.")
    a = p.parse_args()
    asyncio.run(main(a.flatten))
