import pytest

from backend.bot.warmup import _binance_klines_to_candles, _env_bool


def test_binance_klines_to_candles_converts_array_rows():
    candles = _binance_klines_to_candles([
        [
            1710000000000,
            "100.0",
            "110.0",
            "95.0",
            "105.0",
            "123.45",
            1710000299999,
            "0",
            10,
            "0",
            "0",
            "0",
        ]
    ])

    assert len(candles) == 1
    candle = candles[0]
    assert candle.open_time == 1710000000000
    assert candle.close_time == 1710000299999
    assert candle.open == 100.0
    assert candle.high == 110.0
    assert candle.low == 95.0
    assert candle.close == 105.0
    assert candle.volume == 123
    assert candle.is_closed is True


def test_binance_klines_to_candles_skips_bad_rows():
    candles = _binance_klines_to_candles([
        ["bad"],
        [1710000000000, "1", "2", "0.5", "1.5", "1", 1710000299999],
    ])

    assert len(candles) == 1
    assert candles[0].close == 1.5


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on"])
def test_env_bool_true_values(monkeypatch, raw):
    monkeypatch.setenv("WARMUP_TEST_BOOL", raw)

    assert _env_bool("WARMUP_TEST_BOOL", False) is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", ""])
def test_env_bool_false_values(monkeypatch, raw):
    monkeypatch.setenv("WARMUP_TEST_BOOL", raw)

    assert _env_bool("WARMUP_TEST_BOOL", True) is False
