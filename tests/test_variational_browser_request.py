import argparse
import time

import pytest

from backend.bot.fair_price import FairPrice, SourcePrice
from backend.scripts.create_variational_browser_request import (
    build_requests,
    format_quantity,
    normalize_base_url,
    selected_legs,
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


def args(**overrides):
    defaults = {
        "direction": "LONG_BTC_SHORT_ETH",
        "size_usd": 50.0,
        "zscore": None,
        "divergence_pct": None,
        "base_url": "https://omni.variational.io/perpetual/BTC",
        "legs": "both",
        "confirm_selector": "auto",
        "dry_run": True,
        "request_dir": "",
        "steps_json": "",
        "max_age_sec": 300,
        "btc_qty_decimals": 6,
        "eth_qty_decimals": 4,
    }
    defaults.update(overrides)
    return argparse.Namespace(**defaults)


def test_selected_legs_maps_pair_direction():
    assert selected_legs("LONG_BTC_SHORT_ETH", "both") == [("BTC", "BUY"), ("ETH", "SELL")]
    assert selected_legs("SHORT_BTC_LONG_ETH", "both") == [("BTC", "SELL"), ("ETH", "BUY")]
    assert selected_legs("LONG_BTC_SHORT_ETH", "BTC") == [("BTC", "BUY")]


def test_build_requests_creates_two_variational_legs():
    requests = build_requests(
        args(),
        {
            "BTC": fair("BTC", 100_000.0),
            "ETH": fair("ETH", 2_500.0),
        },
    )

    assert len(requests) == 2
    btc, eth = requests
    assert btc["url"] == "https://omni.variational.io/perpetual/BTC"
    assert btc["maxAgeSec"] == 300
    assert btc["confirmSelector"] == "auto"
    assert btc["variationalOrder"] == {
        "symbol": "BTC",
        "side": "BUY",
        "orderType": "market",
        "quantity": "0.0005",
        "sizeUsd": 50.0,
        "fairPrice": 100_000.0,
        "pairDirection": "LONG_BTC_SHORT_ETH",
    }
    assert eth["url"] == "https://omni.variational.io/perpetual/ETH"
    assert eth["variationalOrder"]["side"] == "SELL"
    assert eth["variationalOrder"]["quantity"] == "0.02"


def test_build_requests_can_create_single_leg():
    requests = build_requests(
        args(direction="SHORT_BTC_LONG_ETH", legs="ETH"),
        {
            "BTC": fair("BTC", 100_000.0),
            "ETH": fair("ETH", 2_000.0),
        },
    )

    assert len(requests) == 1
    assert requests[0]["variationalOrder"]["symbol"] == "ETH"
    assert requests[0]["variationalOrder"]["side"] == "BUY"
    assert requests[0]["variationalOrder"]["quantity"] == "0.025"


def test_normalize_base_url_strips_perpetual_path():
    assert normalize_base_url("https://omni.variational.io/perpetual/BTC") == "https://omni.variational.io"


def test_format_quantity_keeps_nonzero_small_values():
    with pytest.raises(SystemExit):
        format_quantity(0.00000049, 6)
    with pytest.raises(SystemExit):
        format_quantity(0, 6)
