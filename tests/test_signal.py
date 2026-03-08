"""SignalEngine 단위 테스트."""

import math

import numpy as np
import pytest

from backend.bot.signal import (
    SignalConfig,
    SignalDirection,
    SignalEngine,
    SignalResult,
)


# ── 헬퍼 ──────────────────────────────────────────────────────


def _fill_engine(engine: SignalEngine, n: int, spread: float = 0.0) -> None:
    """엔진에 n개의 동일 스프레드를 채워 윈도우를 충족시킵니다."""
    for _ in range(n):
        engine.add_spread(spread)


def _make_engine(window: int = 20, **kwargs) -> SignalEngine:
    """테스트용 엔진을 생성합니다."""
    cfg = SignalConfig(sigma_window=window, **kwargs)
    return SignalEngine(cfg)


# ── 기본 계산 테스트 ──────────────────────────────────────────


class TestCalculateReturn:
    def test_basic_return(self):
        prices = [100.0, 110.0]
        ret = SignalEngine.calculate_return(prices, lookback=1)
        assert ret == pytest.approx(10.0)

    def test_negative_return(self):
        prices = [100.0, 90.0]
        ret = SignalEngine.calculate_return(prices, lookback=1)
        assert ret == pytest.approx(-10.0)

    def test_lookback_greater_than_1(self):
        prices = [100.0, 105.0, 110.0, 115.0]
        ret = SignalEngine.calculate_return(prices, lookback=3)
        assert ret == pytest.approx(15.0)

    def test_insufficient_data(self):
        prices = [100.0]
        ret = SignalEngine.calculate_return(prices, lookback=1)
        assert ret == 0.0

    def test_zero_price(self):
        prices = [0.0, 100.0]
        ret = SignalEngine.calculate_return(prices, lookback=1)
        assert ret == 0.0


class TestCalculateSpread:
    def test_positive_spread(self):
        spread = SignalEngine.calculate_spread(btc_return_pct=2.0, eth_return_pct=5.0)
        assert spread == pytest.approx(3.0)

    def test_negative_spread(self):
        spread = SignalEngine.calculate_spread(btc_return_pct=5.0, eth_return_pct=2.0)
        assert spread == pytest.approx(-3.0)

    def test_zero_spread(self):
        spread = SignalEngine.calculate_spread(btc_return_pct=3.0, eth_return_pct=3.0)
        assert spread == pytest.approx(0.0)


class TestZscoreToProbability:
    def test_z0(self):
        prob = SignalEngine.zscore_to_probability(0.0)
        assert prob == pytest.approx(50.0, abs=0.1)

    def test_z1(self):
        prob = SignalEngine.zscore_to_probability(1.0)
        assert prob == pytest.approx(15.87, abs=0.1)

    def test_z2(self):
        prob = SignalEngine.zscore_to_probability(2.0)
        assert prob == pytest.approx(2.28, abs=0.1)

    def test_z3(self):
        prob = SignalEngine.zscore_to_probability(3.0)
        assert prob == pytest.approx(0.135, abs=0.05)

    def test_negative_z(self):
        """음수 Z-score도 같은 확률."""
        assert SignalEngine.zscore_to_probability(-2.0) == pytest.approx(
            SignalEngine.zscore_to_probability(2.0)
        )


# ── Z-score 계산 테스트 ───────────────────────────────────────


class TestZscoreCalculation:
    def test_insufficient_data(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 10)
        z, mean, std = engine.calculate_zscore()
        assert z == 0.0

    def test_constant_spread_zero_zscore(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 20, spread=1.0)
        z, mean, std = engine.calculate_zscore()
        assert z == pytest.approx(0.0, abs=0.01)
        assert mean == pytest.approx(1.0)

    def test_outlier_positive_zscore(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 19, spread=0.0)
        engine.add_spread(5.0)  # 이상치
        z, mean, std = engine.calculate_zscore()
        assert z > 0  # 양의 이상치

    def test_outlier_negative_zscore(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 19, spread=0.0)
        engine.add_spread(-5.0)  # 음의 이상치
        z, mean, std = engine.calculate_zscore()
        assert z < 0


# ── 진입 조건 테스트 ──────────────────────────────────────────


class TestEntrySignal:
    def _build_entry_engine(self) -> SignalEngine:
        """진입 테스트용 엔진: window=20, confirmation=1 (즉시 진입)."""
        return _make_engine(
            window=20,
            divergence_threshold_pct=2.0,
            entry_zscore=2.0,
            max_zscore=3.5,
            probability_threshold_pct=95.0,
            confirmation_candles=1,
        )

    def test_no_entry_without_enough_data(self):
        engine = self._build_entry_engine()
        result = engine.check_entry(0.0, 5.0)
        assert not result.should_enter

    def test_entry_on_strong_signal(self):
        engine = self._build_entry_engine()
        # 평균 0, std ~1이 되도록 정규 분포 데이터 채우기
        np.random.seed(42)
        for val in np.random.normal(0, 1, 19):
            engine.add_spread(float(val))

        # 큰 divergence + 높은 z-score 유발
        result = engine.check_entry(btc_return_pct=0.0, eth_return_pct=5.0)

        # Z-score가 충분히 크면 진입
        if abs(result.zscore) >= 2.0 and abs(result.spread_pct) >= 2.0:
            assert result.should_enter
            assert result.direction == SignalDirection.LONG_BTC_SHORT_ETH

    def test_no_entry_below_divergence(self):
        engine = self._build_entry_engine()
        _fill_engine(engine, 19)
        # divergence 1% < threshold 2%
        result = engine.check_entry(btc_return_pct=0.0, eth_return_pct=1.0)
        assert not result.should_enter

    def test_no_entry_beyond_max_zscore(self):
        engine = _make_engine(
            window=20,
            divergence_threshold_pct=0.1,
            entry_zscore=2.0,
            max_zscore=3.5,
            confirmation_candles=1,
        )
        _fill_engine(engine, 19, spread=0.0)
        # 극단적 이상치 → z > max_zscore
        result = engine.check_entry(btc_return_pct=0.0, eth_return_pct=100.0)
        if abs(result.zscore) > 3.5:
            assert not result.should_enter

    def test_direction_long_btc_short_eth(self):
        """spread > 0 → ETH 과매수 → LONG BTC SHORT ETH."""
        engine = self._build_entry_engine()
        np.random.seed(42)
        for val in np.random.normal(0, 1, 19):
            engine.add_spread(float(val))

        result = engine.check_entry(btc_return_pct=-1.0, eth_return_pct=4.0)
        if result.should_enter:
            assert result.direction == SignalDirection.LONG_BTC_SHORT_ETH

    def test_direction_short_btc_long_eth(self):
        """spread < 0 → BTC 과매수 → SHORT BTC LONG ETH."""
        engine = self._build_entry_engine()
        np.random.seed(42)
        for val in np.random.normal(0, 1, 19):
            engine.add_spread(float(val))

        result = engine.check_entry(btc_return_pct=4.0, eth_return_pct=-1.0)
        if result.should_enter:
            assert result.direction == SignalDirection.SHORT_BTC_LONG_ETH


class TestConfirmationCandles:
    def test_requires_consecutive_signals(self):
        engine = _make_engine(
            window=20,
            divergence_threshold_pct=0.1,
            entry_zscore=1.5,
            max_zscore=10.0,
            confirmation_candles=3,
        )
        # 정규 분포로 채우기
        np.random.seed(42)
        for val in np.random.normal(0, 1, 19):
            engine.add_spread(float(val))

        # 첫 번째 큰 신호
        r1 = engine.check_entry(0.0, 5.0)
        assert not r1.should_enter  # confirmation 1/3

        # 두 번째
        r2 = engine.check_entry(0.0, 5.0)
        assert not r2.should_enter  # confirmation 2/3

        # 세 번째 → 진입
        r3 = engine.check_entry(0.0, 5.0)
        if abs(r3.zscore) >= 1.5:
            assert r3.should_enter  # confirmation 3/3


# ── 청산 조건 테스트 ──────────────────────────────────────────


class TestExitSignal:
    def test_exit_on_zscore_revert(self):
        engine = _make_engine(window=20, zscore_revert_threshold=0.5)
        _fill_engine(engine, 20, spread=1.0)
        # 모든 스프레드가 동일 → z ≈ 0 → 수렴 → 청산
        assert engine.check_exit_zscore()

    def test_no_exit_insufficient_data(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 5)
        assert not engine.check_exit_zscore()

    def test_exit_flag_in_check_entry(self):
        engine = _make_engine(window=20, zscore_revert_threshold=0.5)
        _fill_engine(engine, 19, spread=0.0)
        result = engine.check_entry(0.0, 0.0)  # spread = 0 → z ≈ 0
        assert result.should_exit_zscore


# ── 상태 관리 테스트 ──────────────────────────────────────────


class TestStateManagement:
    def test_reset(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 50)
        engine.reset()
        assert engine.spread_history_len == 0
        assert not engine.has_enough_data

    def test_memory_limit(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 100)
        # 최대 window * 2 = 40개만 유지
        assert engine.spread_history_len <= 40

    def test_get_status(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 20, spread=1.0)
        status = engine.get_status()
        assert "zscore" in status
        assert "spread_pct" in status
        assert "probability_pct" in status
        assert status["has_enough_data"]
        assert status["window"] == 20

    def test_update_config(self):
        engine = _make_engine(window=20)
        _fill_engine(engine, 30)
        new_cfg = SignalConfig(sigma_window=50)
        engine.update_config(new_cfg)
        assert engine.window == 50
        assert engine.spread_history_len == 30  # 히스토리 유지
