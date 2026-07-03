"""Drive the FULL engine path with a synthetic signal (no waiting for a real one).

Real entry signals fire ~0.4x/day, so this injects a synthetic Signal straight
into the live BotEngine to exercise the complete variational_api integration:

    synthetic Signal -> engine._handle_entry -> VariationalApiExecutor (orders)
      -> position_manager.open_virtual_pair (+ optional DB)
      -> engine._handle_exit -> executor close -> close_virtual_pair

It uses the real engine methods, so it verifies the glue that the unit tests
mock. Safe by default: with VARIATIONAL_DRY_RUN=true (the default) the executor
signs/logs but places NO real orders, yet the engine still records/closes the
virtual position — so you can verify the wiring first, then flip to real orders.

Usage (from repo root, PYTHONPATH=repo root):

    # 1) Dry-run: verify signal -> entry -> virtual position -> exit wiring
    python -m backend.scripts.variational_api_engine_test

    # 2) Real small orders end-to-end (opens AND closes a tiny pair)
    VARIATIONAL_DRY_RUN=false python -m backend.scripts.variational_api_engine_test --size 5 --live

    # direction/hold options
    python -m backend.scripts.variational_api_engine_test --direction SHORT_BTC_LONG_ETH
    VARIATIONAL_DRY_RUN=false python -m backend.scripts.variational_api_engine_test --size 5 --live --hold-sec 10

This does NOT touch price feeds, DB, or PM2. Run it while the production bot is
stopped (it trades the same Variational account).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import time

from dotenv import load_dotenv

from backend.bot.engine import BotEngine, BotConfig, EXECUTION_VARIATIONAL_API
from backend.bot.price_buffer import Tick
from backend.bot.risk_manager import ExitReason
from backend.bot.signal import Signal, SignalDirection, TrendDirection

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("engine_test")


def _synthetic_signal(direction: SignalDirection) -> Signal:
    """A signal that would pass all three MultiTF layers."""
    trend = (
        TrendDirection.ETH_OVERBOUGHT
        if direction == SignalDirection.LONG_BTC_SHORT_ETH
        else TrendDirection.BTC_OVERBOUGHT
    )
    zscore = 2.2 if direction == SignalDirection.LONG_BTC_SHORT_ETH else -2.2
    return Signal(
        should_enter=True,
        direction=direction,
        zscore_5m=zscore,
        divergence_pct=1.6,
        probability_pct=97.0,
        trend=trend,
        peak_revert_detected=True,
    )


async def main(size_usd: float, direction_str: str, live: bool, hold_sec: float) -> None:
    load_dotenv()
    direction = SignalDirection(direction_str)

    config = BotConfig(
        position_size_usd=size_usd,
        leverage=3,
        execution_mode=EXECUTION_VARIATIONAL_API,
        trading_mode="swing",
    )
    engine = BotEngine(exchanges={}, config=config)
    engine.telegram = None  # keep this harness quiet; don't spam the alert channel
    dry = engine.variational_bridge._settings.dry_run
    log.info("Engine ready | mode=%s size=$%s dry_run=%s", engine.execution_mode, size_usd, dry)
    if live and dry:
        log.warning("--live passed but VARIATIONAL_DRY_RUN=true; no REAL orders will be sent. "
                    "Re-run with VARIATIONAL_DRY_RUN=false to place real orders.")

    # 1) Seed BTC/ETH prices from the live venue so the entry guard passes and the
    #    virtual position tracks realistic marks.
    connector = await engine.variational_bridge._ensure_connected()
    btc_mark = float(await connector.get_mark_price("BTC"))
    eth_mark = float(await connector.get_mark_price("ETH"))
    now_ms = int(time.time() * 1000)
    engine.price_buffer.btc.update(Tick("variational", "BTC", btc_mark, now_ms))
    engine.price_buffer.eth.update(Tick("variational", "ETH", eth_mark, now_ms))
    log.info("Seeded prices | BTC=%s ETH=%s", btc_mark, eth_mark)

    # 2) Fire the synthetic signal through the REAL entry handler.
    log.info(">>> ENTRY: %s", direction.value)
    await engine._handle_entry(_synthetic_signal(direction))

    open_trades = dict(engine.position_manager.open_trades)
    if not open_trades:
        log.error("No virtual position opened — entry did NOT complete "
                  "(order failed or was suppressed). Check logs above.")
        await engine.variational_bridge.aclose()
        return
    trade_id, trade = next(iter(open_trades.items()))
    log.info("OPENED trade_id=%s | BTC %s qty=%s / ETH %s qty=%s",
             trade_id, trade.btc_leg.side.value, trade.btc_leg.quantity,
             trade.eth_leg.side.value, trade.eth_leg.quantity)

    # 3) Hold briefly, then close through the REAL exit handler.
    log.info("Holding %.0fs before close...", hold_sec)
    await asyncio.sleep(hold_sec)

    log.info(">>> EXIT: %s", trade_id)
    await engine._handle_exit(trade_id, ExitReason.MANUAL, "engine integration test")

    still_open = dict(engine.position_manager.open_trades)
    closed = engine.position_manager._closed_trades
    if trade_id in still_open:
        log.error("EXIT did not complete: trade still open (close order failed?).")
    else:
        log.info("CLOSED trade_id=%s | closed_trades=%d", trade_id, len(closed))

    # 4) Confirm the venue is flat for our test legs.
    for sym in ("BTC", "ETH"):
        pos = await connector.get_position(sym)
        log.info("venue position %s: %s", sym,
                 f"{pos.side.value} {pos.size}" if pos else "flat")

    await engine.variational_bridge.aclose()
    log.info("Engine integration test finished.")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Synthetic-signal engine integration test.")
    p.add_argument("--size", type=float, default=5.0, help="Notional per leg in USD (default 5).")
    p.add_argument("--direction", default="LONG_BTC_SHORT_ETH",
                   choices=["LONG_BTC_SHORT_ETH", "SHORT_BTC_LONG_ETH"])
    p.add_argument("--live", action="store_true", help="Intent to place real orders (also needs VARIATIONAL_DRY_RUN=false).")
    p.add_argument("--hold-sec", type=float, default=3.0, help="Seconds to hold before closing.")
    a = p.parse_args()
    asyncio.run(main(a.size, a.direction, a.live, a.hold_sec))
