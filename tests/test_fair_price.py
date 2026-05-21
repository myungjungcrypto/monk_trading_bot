import time

import pytest

from backend.bot.fair_price import (
    SourcePrice,
    build_fair_prices,
    parse_lighter_ticker,
)


def sample(source: str, symbol: str, price: float, age_ms: int = 0) -> SourcePrice:
    return SourcePrice(
        source=source,
        symbol=symbol,
        price=price,
        timestamp_ms=int(time.time() * 1000) - age_ms,
    )


def test_build_fair_prices_uses_median_with_three_sources():
    fair = build_fair_prices(
        {
            "binance": {"BTC": sample("binance", "BTC", 100.0)},
            "lighter": {"BTC": sample("lighter", "BTC", 101.0)},
            "hyperliquid": {"BTC": sample("hyperliquid", "BTC", 130.0)},
        },
        symbols=["BTC"],
        min_sources=2,
        source_max_age_ms=10_000,
        max_deviation_bps=5_000,
    )

    assert fair["BTC"].price == 101.0
    assert fair["BTC"].source_names == ["binance", "lighter", "hyperliquid"]


def test_build_fair_prices_requires_minimum_fresh_sources():
    fair = build_fair_prices(
        {
            "binance": {"ETH": sample("binance", "ETH", 10.0, age_ms=60_000)},
            "lighter": {"ETH": sample("lighter", "ETH", 10.1)},
            "hyperliquid": {"ETH": sample("hyperliquid", "ETH", 10.2)},
        },
        symbols=["ETH"],
        min_sources=2,
        source_max_age_ms=10_000,
        max_deviation_bps=500,
    )

    assert fair["ETH"].price == pytest.approx(10.15)
    assert fair["ETH"].source_names == ["lighter", "hyperliquid"]


def test_build_fair_prices_omits_symbol_without_enough_sources():
    fair = build_fair_prices(
        {"binance": {"BTC": sample("binance", "BTC", 100.0)}},
        symbols=["BTC"],
        min_sources=2,
        source_max_age_ms=10_000,
        max_deviation_bps=500,
    )

    assert "BTC" not in fair


def test_parse_lighter_ticker_mid_price():
    parsed = parse_lighter_ticker(
        {
            "channel": "ticker:1",
            "type": "update/ticker",
            "timestamp": 123456789,
            "ticker": {
                "b": {"price": "100"},
                "a": {"price": "102"},
            },
        },
        {1: "BTC"},
    )

    assert parsed is not None
    assert parsed.source == "lighter"
    assert parsed.symbol == "BTC"
    assert parsed.price == 101.0
