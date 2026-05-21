from datetime import datetime, timezone
from decimal import Decimal

from backend.bot.variational.market_data import (
    FreshnessSample,
    parse_stats,
    parse_variational_timestamp,
    summarize_samples,
)


def test_parse_variational_nanosecond_timestamp():
    parsed = parse_variational_timestamp("2026-01-06T06:38:52.476166127Z")

    assert parsed == datetime(2026, 1, 6, 6, 38, 52, 476166, tzinfo=timezone.utc)


def test_parse_stats_extracts_btc_quote_fields():
    stats = {
        "listings": [
            {
                "ticker": "BTC",
                "mark_price": "93787.9606019699",
                "funding_rate": "0.037347",
                "base_spread_bps": "0.4307589134",
                "quotes": {
                    "updated_at": "2026-01-06T06:38:52.476166127Z",
                    "size_1k": {"bid": "93750.97", "ask": "93755.01"},
                    "size_100k": {"bid": "93746.13", "ask": "93759.85"},
                },
            }
        ]
    }

    listings = parse_stats(stats)

    btc = listings["BTC"]
    assert btc.mark_price == Decimal("93787.9606019699")
    assert btc.funding_rate == Decimal("0.037347")
    assert btc.size_1k.bid == Decimal("93750.97")
    assert btc.size_1k.ask == Decimal("93755.01")
    assert btc.mid_price("size_1k") == Decimal("93752.99")


def test_summarize_samples_counts_quote_timestamp_changes():
    btc_eth = parse_stats({
        "listings": [
            {
                "ticker": "BTC",
                "mark_price": "100",
                "quotes": {
                    "updated_at": "2026-01-06T00:00:00.000000000Z",
                    "size_1k": {"bid": "99", "ask": "101"},
                },
            },
            {
                "ticker": "ETH",
                "mark_price": "10",
                "quotes": {
                    "updated_at": "2026-01-06T00:00:00.000000000Z",
                    "size_1k": {"bid": "9", "ask": "11"},
                },
            },
        ]
    })
    btc_eth_next = parse_stats({
        "listings": [
            {
                "ticker": "BTC",
                "mark_price": "101",
                "quotes": {
                    "updated_at": "2026-01-06T00:00:01.000000000Z",
                    "size_1k": {"bid": "100", "ask": "102"},
                },
            },
            {
                "ticker": "ETH",
                "mark_price": "10",
                "quotes": {
                    "updated_at": "2026-01-06T00:00:00.000000000Z",
                    "size_1k": {"bid": "9", "ask": "11"},
                },
            },
        ]
    })
    now = datetime(2026, 1, 6, 0, 0, 2, tzinfo=timezone.utc)
    samples = [
        FreshnessSample(now, 10.0, btc_eth["BTC"], btc_eth["ETH"], Decimal("100"), Decimal("10")),
        FreshnessSample(now, 20.0, btc_eth_next["BTC"], btc_eth_next["ETH"], Decimal("100"), Decimal("10")),
    ]

    summary = summarize_samples(samples)

    assert summary["ok_samples"] == 2
    assert summary["btc_mark_changes"] == 1
    assert summary["eth_mark_changes"] == 0
    assert summary["btc_quote_ts_changes"] == 1
    assert summary["eth_quote_ts_changes"] == 0

