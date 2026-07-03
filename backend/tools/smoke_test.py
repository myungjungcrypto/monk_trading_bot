"""Live smoke test for the Variational connector — safe by default.

Runs the real flow against the live backend using the key in .env:

    1. SIWE login (wallet signature -> session JWT)
    2. Account balance (GET /api/portfolio)
    3. Open positions (GET /api/positions)
    4. Mark prices for BTC and ETH (indicative quotes)
    5. Pair action:
       - DEFAULT (dry-run): quotes both legs and logs the exact order bodies
         WITHOUT submitting.
       - --live: actually opens a BTC-long / ETH-short pair, then (unless --hold)
         reverses it with equal-and-opposite orders so your test exposure returns
         to roughly where it started. It uses same-size opposite orders rather
         than a full close, so any pre-existing positions are left untouched.

Usage (on the server):

    python -m tools.smoke_test                                   # dry-run rehearsal
    VARIATIONAL_DRY_RUN=false python -m tools.smoke_test --size 5 --live
    VARIATIONAL_DRY_RUN=false python -m tools.smoke_test --size 5 --live --hold

Live submission requires BOTH --live AND VARIATIONAL_DRY_RUN=false, so you can't
place real orders by accident.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from bot.config import get_variational_settings
from bot.exchanges.base import Order, OrderResult, Side
from bot.exchanges.variational import VariationalConnector

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("smoke")


def _fmt(res: OrderResult) -> str:
    return (f"{res.side.value} {res.symbol} ${res.size_usd} -> accepted={res.accepted} "
            f"dry_run={res.dry_run} est_price={res.filled_price} order_id={res.order_id}")


async def _open_pair(vx: VariationalConnector, size: Decimal):
    """LONG BTC / SHORT ETH, both legs fired concurrently."""
    return await asyncio.gather(
        vx.place_order(Order(symbol="BTC", side=Side.BUY, size_usd=size)),
        vx.place_order(Order(symbol="ETH", side=Side.SELL, size_usd=size)),
    )


async def _reverse_pair(vx: VariationalConnector, size: Decimal):
    """Undo the test exposure: opposite side, same notional, NOT reduce-only.

    reduce_only is intentionally avoided: a pre-existing position on the same
    symbol can make the reversing side 'increase' rather than 'reduce', which the
    venue would reject. Same-size opposite market orders net our test to ~flat
    without depending on that.
    """
    return await asyncio.gather(
        vx.place_order(Order(symbol="BTC", side=Side.SELL, size_usd=size)),
        vx.place_order(Order(symbol="ETH", side=Side.BUY, size_usd=size)),
    )


async def main(size_usd: Decimal, allow_live: bool, hold: bool) -> None:
    settings = get_variational_settings()
    live = allow_live and not settings.dry_run
    if allow_live and settings.dry_run:
        log.warning("--live passed but VARIATIONAL_DRY_RUN=true; staying in dry-run. "
                    "Re-run with VARIATIONAL_DRY_RUN=false to submit real orders.")

    async with VariationalConnector(settings) as vx:
        log.info("[1/5] logged in as %s (live=%s)", vx._address, live)

        bal = await vx.get_balance()
        log.info("[2/5] balance=%s USDC, upnl=%s", bal["balance"], bal["upnl"])

        for sym in ("BTC", "ETH"):
            pos = await vx.get_position(sym)
            log.info("[3/5] position %s: %s", sym,
                     f"{pos.side.value} {pos.size} @ {pos.entry_price}" if pos else "flat")

        btc_mark = await vx.get_mark_price("BTC")
        eth_mark = await vx.get_mark_price("ETH")
        log.info("[4/5] mark BTC=%s ETH=%s", btc_mark, eth_mark)

        log.info("[5/5] pair: LONG BTC / SHORT ETH, %s USD per leg (live=%s)", size_usd, live)
        opened = await _open_pair(vx, size_usd)
        for res in opened:
            log.info("  OPEN  %s", _fmt(res))

        if live and not hold:
            await asyncio.sleep(2)  # let fills register before reversing
            reversed_ = await _reverse_pair(vx, size_usd)
            for res in reversed_:
                log.info("  CLOSE %s", _fmt(res))
            # Show where the test left things.
            await asyncio.sleep(1)
            for sym in ("BTC", "ETH"):
                pos = await vx.get_position(sym)
                log.info("  final %s: %s", sym,
                         f"{pos.side.value} {pos.size} @ {pos.entry_price}" if pos else "flat")
        elif live and hold:
            log.info("  --hold set: positions left OPEN. Close them yourself when done.")

    log.info("Smoke test finished.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Variational connector live smoke test.")
    parser.add_argument("--size", type=Decimal, default=Decimal("10"),
                        help="Notional per leg in USD (default 10).")
    parser.add_argument("--live", action="store_true",
                        help="Submit real orders (also requires VARIATIONAL_DRY_RUN=false).")
    parser.add_argument("--hold", action="store_true",
                        help="With --live: leave the pair open instead of reversing it.")
    args = parser.parse_args()
    asyncio.run(main(args.size, args.live, args.hold))
