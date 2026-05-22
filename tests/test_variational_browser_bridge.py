import asyncio
import json
import time

from backend.bot.exchanges.base import PositionSide
from backend.bot.fair_price import FairPrice, SourcePrice
from backend.bot.position_manager import LegInfo, PairDirection, PairTrade
from backend.bot.variational.browser_requests import (
    VariationalBrowserRequestBridge,
    VariationalBrowserRequestConfig,
    request_quantity,
)


def fair(symbol: str, price: float) -> FairPrice:
    now_ms = int(time.time() * 1000)
    return FairPrice(
        symbol=symbol,
        price=price,
        sources=[
            SourcePrice("binance", symbol, price, now_ms),
            SourcePrice("lighter", symbol, price, now_ms),
        ],
    )


class FakeOracle:
    async def fetch(self, symbols):
        return {
            "BTC": fair("BTC", 100_000.0),
            "ETH": fair("ETH", 2_000.0),
        }


def config(tmp_path):
    return VariationalBrowserRequestConfig(
        request_dir=tmp_path,
        dry_run=True,
        approval_timeout_sec=180,
    )


def test_bridge_writes_entry_requests(tmp_path):
    async def run():
        bridge = VariationalBrowserRequestBridge(config(tmp_path), FakeOracle())
        batch = await bridge.create_entry_requests(
            direction=PairDirection.SHORT_BTC_LONG_ETH,
            size_usd=50,
            zscore=2.1,
            divergence_pct=1.4,
        )

        assert batch.action == "open"
        assert len(batch.paths) == 2
        assert all(path.exists() for path in batch.paths)
        btc = next(r for r in batch.requests if r["variationalOrder"]["symbol"] == "BTC")
        eth = next(r for r in batch.requests if r["variationalOrder"]["symbol"] == "ETH")
        assert btc["variationalOrder"]["side"] == "SELL"
        assert eth["variationalOrder"]["side"] == "BUY"
        assert eth["approvalTimeoutMs"] == 180_000
        assert request_quantity(batch, "ETH") == 0.025

        saved = json.loads(batch.paths[0].read_text())
        assert saved["id"] == batch.requests[0]["id"]

    asyncio.run(run())


def test_bridge_writes_reduce_only_close_requests(tmp_path):
    async def run():
        trade = PairTrade(
            trade_id="variational_browser_1",
            exchange_name="variational_browser",
            direction=PairDirection.SHORT_BTC_LONG_ETH,
            btc_leg=LegInfo(
                asset="BTC",
                side=PositionSide.SHORT,
                size_usd=50,
                quantity=0.000646,
                entry_price=77_000,
            ),
            eth_leg=LegInfo(
                asset="ETH",
                side=PositionSide.LONG,
                size_usd=50,
                quantity=0.0235,
                entry_price=2_100,
            ),
            opened_at=time.time(),
        )
        bridge = VariationalBrowserRequestBridge(config(tmp_path), FakeOracle())
        batch = await bridge.create_close_requests(trade=trade, reason="MANUAL")

        btc = next(r for r in batch.requests if r["variationalOrder"]["symbol"] == "BTC")
        eth = next(r for r in batch.requests if r["variationalOrder"]["symbol"] == "ETH")
        assert btc["variationalOrder"]["side"] == "BUY"
        assert btc["variationalOrder"]["quantity"] == "0.000646"
        assert btc["variationalOrder"]["reduceOnly"] is True
        assert eth["variationalOrder"]["side"] == "SELL"
        assert eth["variationalOrder"]["quantity"] == "0.0235"
        assert eth["variationalOrder"]["reduceOnly"] is True

    asyncio.run(run())
