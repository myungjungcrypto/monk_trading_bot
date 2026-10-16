"""The smoke script must not trade unless --live is passed.

The live box runs with VARIATIONAL_DRY_RUN=false, so the rehearsal used to send
a real BTC/ETH pair without --live and, skipping the reverse step, leave it open.
"""

import asyncio
from decimal import Decimal

from backend.bot.variational.api.api_config import VariationalSettings
from backend.bot.variational.api.api_types import OrderResult
from backend.scripts import variational_api_smoke as smoke


class FakeConnector:
    instances: list = []

    def __init__(self, settings):
        self.settings = settings
        self._address = "0x0000000000000000000000000000000000000001"
        self.orders = []
        FakeConnector.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get_balance(self):
        return {"balance": Decimal(0), "upnl": Decimal(0)}

    async def get_position(self, symbol):
        return None

    async def place_order(self, order):
        self.orders.append(order)
        return OrderResult(
            accepted=True, venue="variational", symbol=order.symbol, side=order.side,
            size_usd=order.size_usd, dry_run=self.settings.dry_run,
        )


def _run(monkeypatch, *, env_dry_run, live, hold=True):
    FakeConnector.instances = []
    settings = VariationalSettings(_env_file=None, dry_run=env_dry_run)
    monkeypatch.setattr(smoke, "get_variational_settings", lambda: settings)
    monkeypatch.setattr(smoke, "VariationalConnector", FakeConnector)
    asyncio.run(smoke.main(Decimal("10"), live, hold))
    [conn] = FakeConnector.instances
    return conn


def test_rehearsal_stays_dry_even_when_env_is_live(monkeypatch):
    conn = _run(monkeypatch, env_dry_run=False, live=False)
    assert conn.settings.dry_run is True
    assert len(conn.orders) == 2   # the pair is still rehearsed (quotes only)


def test_live_flag_with_live_env_trades(monkeypatch):
    conn = _run(monkeypatch, env_dry_run=False, live=True, hold=True)
    assert conn.settings.dry_run is False


def test_live_flag_with_dry_env_stays_dry(monkeypatch):
    conn = _run(monkeypatch, env_dry_run=True, live=True, hold=True)
    assert conn.settings.dry_run is True
