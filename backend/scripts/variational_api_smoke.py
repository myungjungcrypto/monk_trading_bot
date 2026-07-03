"""Live smoke test for the Variational direct-API connector (integrated bot).

Verifies the API execution path end-to-end using the key + transport in .env,
before you flip EXECUTION_MODE=variational_api. Safe by default (dry-run).

    python -m backend.scripts.variational_api_smoke                       # dry-run rehearsal
    VARIATIONAL_DRY_RUN=false python -m backend.scripts.variational_api_smoke --size 5 --live

Live submission needs BOTH --live AND VARIATIONAL_DRY_RUN=false. With --live (and
no --hold) it opens a tiny BTC-long / ETH-short pair, then reverses it with
equal-and-opposite orders so your test exposure nets back to ~flat.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from decimal import Decimal

from backend.bot.variational.api.api_client import VariationalConnector
from backend.bot.variational.api.api_config import get_variational_settings
from backend.bot.variational.api.api_types import Order, OrderResult, Side

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("variational_api_smoke")


def _fmt(res: OrderResult) -> str:
    return (f"{res.side.value} {res.symbol} ${res.size_usd} -> accepted={res.accepted} "
            f"dry_run={res.dry_run} qty={res.filled_qty} est_price={res.filled_price} "
            f"order_id={res.order_id}")


async def _open_pair(vx: VariationalConnector, size: Decimal):
    return await asyncio.gather(
        vx.place_order(Order(symbol="BTC", side=Side.BUY, size_usd=size)),
        vx.place_order(Order(symbol="ETH", side=Side.SELL, size_usd=size)),
    )


async def _reverse_pair(vx: VariationalConnector, size: Decimal):
    return await asyncio.gather(
        vx.place_order(Order(symbol="BTC", side=Side.SELL, size_usd=size)),
        vx.place_order(Order(symbol="ETH", side=Side.BUY, size_usd=size)),
    )


async def main(size_usd: Decimal, allow_live: bool, hold: bool) -> None:
    settings = get_variational_settings()
    live = allow_live and not settings.dry_run
    if allow_live and settings.dry_run:
        log.warning("--live passed but VARIATIONAL_DRY_RUN=true; staying in dry-run.")

    async with VariationalConnector(settings) as vx:
        log.info("[1/4] logged in as %s (transport=%s live=%s)",
                 vx._address, settings.transport, live)
        bal = await vx.get_balance()
        log.info("[2/4] balance=%s USDC upnl=%s", bal["balance"], bal["upnl"])
        for sym in ("BTC", "ETH"):
            pos = await vx.get_position(sym)
            log.info("[3/4] position %s: %s", sym,
                     f"{pos.side.value} {pos.size} @ {pos.entry_price}" if pos else "flat")

        log.info("[4/4] pair rehearsal: LONG BTC / SHORT ETH, %s USD/leg (live=%s)", size_usd, live)
        for res in await _open_pair(vx, size_usd):
            log.info("  OPEN  %s", _fmt(res))
        if live and not hold:
            await asyncio.sleep(2)
            for res in await _reverse_pair(vx, size_usd):
                log.info("  CLOSE %s", _fmt(res))
            await asyncio.sleep(1)
            for sym in ("BTC", "ETH"):
                pos = await vx.get_position(sym)
                log.info("  final %s: %s", sym,
                         f"{pos.side.value} {pos.size} @ {pos.entry_price}" if pos else "flat")
        elif live and hold:
            log.info("  --hold set: pair left OPEN.")

    log.info("Smoke test finished.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Variational API connector smoke test.")
    p.add_argument("--size", type=Decimal, default=Decimal("10"))
    p.add_argument("--live", action="store_true")
    p.add_argument("--hold", action="store_true")
    a = p.parse_args()
    asyncio.run(main(a.size, a.live, a.hold))
