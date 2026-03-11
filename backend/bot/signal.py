"""
Signal Engine — BTC/ETH 페어 트레이딩 시그널 생성.

v2: 3-Layer 멀티 타임프레임 필터
  Layer 1 — 추세 필터 (1시간봉): 구조적 과매수/과매도 확인
  Layer 2 — 진입 시그널 (5분봉): Z-score + divergence
  Layer 3 — 트리거 (실시간 틱): peak 수렴 감지

기존 단일 Z-score 엔진(SignalEngine)도 하위 호환을 위해 유지합니다.
"""

import logging
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque, List, Optional

import numpy as np
from scipy.stats import norm

from backend.bot.price_buffer import Candle, PriceBuffer, SymbolBuffer, Tick

logger = logging.getLogger(__name__)


class SignalDirection(str, Enum):
    """진입 방향."""
    LONG_BTC_SHORT_ETH = "LONG_BTC_SHORT_ETH"  # ETH 과매수 → ETH 숏
    SHORT_BTC_LONG_ETH = "SHORT_BTC_LONG_ETH"  # BTC 과매수 → BTC 숏
    NONE = "NONE"


class TrendDirection(str, Enum):
    """1시간봉 추세 방향."""
    ETH_OVERBOUGHT = "ETH_OVERBOUGHT"   # ETH가 BTC 대비 강세 → LONG_BTC_SHORT_ETH 허용
    BTC_OVERBOUGHT = "BTC_OVERBOUGHT"   # BTC가 ETH 대비 강세 → SHORT_BTC_LONG_ETH 허용
    NEUTRAL = "NEUTRAL"


class TradingMode(str, Enum):
    """운영 모드."""
    SCALP = "scalp"
    SWING = "swing"
    POSITION = "position"


@dataclass
class Signal:
    """시그널 엔진 출력."""
    should_enter: bool = False
    direction: SignalDirection = SignalDirection.NONE
    zscore_5m: float = 0.0
    divergence_pct: float = 0.0
    probability_pct: float = 0.0
    trend: TrendDirection = TrendDirection.NEUTRAL
    peak_revert_detected: bool = False

    # 청산 관련
    should_exit_zscore: bool = False
    zscore_current: float = 0.0


# ── 기존 단일 Z-score 엔진 (하위 호환) ─────────────────────

@dataclass
class SignalResult:
    """시그널 엔진 출력 (v1 호환)."""
    should_enter: bool = False
    direction: SignalDirection = SignalDirection.NONE
    zscore: float = 0.0
    spread_pct: float = 0.0
    probability_pct: float = 0.0
    mean: float = 0.0
    std: float = 0.0
    should_exit_zscore: bool = False


@dataclass
class SignalConfig:
    """시그널 엔진 설정 (v1 호환)."""
    divergence_threshold_pct: float = 2.5
    lookback_minutes: int = 15
    confirmation_candles: int = 2
    sigma_enabled: bool = True
    sigma_window: int = 100
    entry_zscore: float = 2.0
    max_zscore: float = 3.5
    probability_threshold_pct: float = 95.0
    zscore_revert_threshold: float = 0.5


# ── 모드별 파라미터 프리셋 ───────────────────────────────────

@dataclass
class MultiTFConfig:
    """멀티 타임프레임 시그널 설정."""
    mode: TradingMode = TradingMode.SWING

    # Layer 2: 5분봉 Z-score
    z_window_5m: int = 50
    entry_zscore: float = 2.0
    max_zscore: float = 3.5
    divergence_threshold_pct: float = 1.5

    # Layer 2: divergence lookback (5분봉 개수, 기본 12 = 60분)
    divergence_lookback: int = 12

    # Layer 3: 틱 peak 수렴
    peak_revert_ratio: float = 0.90

    # 청산 조건
    zscore_revert_threshold: float = 0.5

    @classmethod
    def scalp(cls) -> "MultiTFConfig":
        return cls(
            mode=TradingMode.SCALP,
            z_window_5m=30,
            entry_zscore=1.5,
            max_zscore=3.0,
            divergence_threshold_pct=0.3,
            divergence_lookback=3,
            peak_revert_ratio=0.95,
            zscore_revert_threshold=0.3,
        )

    @classmethod
    def swing(cls) -> "MultiTFConfig":
        return cls(
            mode=TradingMode.SWING,
            z_window_5m=50,
            entry_zscore=2.0,
            max_zscore=3.5,
            divergence_threshold_pct=1.5,
            peak_revert_ratio=0.90,
            zscore_revert_threshold=0.5,
        )

    @classmethod
    def position(cls) -> "MultiTFConfig":
        return cls(
            mode=TradingMode.POSITION,
            z_window_5m=100,
            entry_zscore=2.5,
            max_zscore=4.0,
            divergence_threshold_pct=2.0,
            peak_revert_ratio=0.85,
            zscore_revert_threshold=0.8,
        )

    @classmethod
    def from_mode(cls, mode: str) -> "MultiTFConfig":
        modes = {
            "scalp": cls.scalp,
            "swing": cls.swing,
            "position": cls.position,
        }
        factory = modes.get(mode, cls.swing)
        return factory()


# ── 멀티 타임프레임 시그널 엔진 ───────────────────────────────

class MultiTimeframeSignalEngine:
    """
    3-Layer 멀티 타임프레임 시그널 엔진.

    Layer 1: 1시간봉 추세 필터 — 진입 방향과 일치하는지 확인
    Layer 2: 5분봉 Z-score + divergence — 진입 조건
    Layer 3: 실시간 틱 peak 수렴 감지 — 정확한 타이밍

    PriceBuffer에서 데이터를 직접 읽어 시그널을 평가합니다.
    """

    def __init__(self, config: Optional[MultiTFConfig] = None):
        self.config = config or MultiTFConfig.swing()

        # 5분봉 스프레드 히스토리
        self._spread_5m: Deque[float] = deque(maxlen=self.config.z_window_5m * 2)

        # 1시간봉 스프레드 히스토리
        self._spread_1h: Deque[float] = deque(maxlen=100)

        # 틱 수준 peak 추적 (항상 업데이트, Layer 1/2와 독립)
        self._tick_spread_peak: float = 0.0
        self._tick_spread_current: float = 0.0
        self._tick_spread_direction: SignalDirection = SignalDirection.NONE
        self._peak_ready: bool = False  # peak 이후 revert 가능 상태

        # 진단 로그 스로틀 (초당 1회 제한)
        self._last_log_time: dict[str, float] = {}

    @property
    def has_enough_data(self) -> bool:
        return len(self._spread_5m) >= self.config.z_window_5m

    @property
    def spread_history_len(self) -> int:
        return len(self._spread_5m)

    @property
    def window(self) -> int:
        return self.config.z_window_5m

    # ── 메인 평가 ───────────────────────────────────────────

    def evaluate(self, price_buffer: PriceBuffer) -> Signal:
        """
        PriceBuffer 데이터로 3-Layer 시그널을 평가합니다.

        Args:
            price_buffer: 틱→캔들이 집계된 PriceBuffer

        Returns:
            Signal 객체 (should_enter, direction 등)
        """
        signal = Signal()

        if not price_buffer.has_data:
            return signal

        # 스프레드 히스토리 업데이트
        self._update_spreads(price_buffer)

        # Layer 3 peak 추적은 항상 실행 (Layer 1/2와 독립)
        self._update_tick_peak(price_buffer)

        if not self.has_enough_data:
            return signal

        # 현재 5m Z-score
        z5m, mean, std = self._zscore(self._spread_5m, self.config.z_window_5m)
        signal.zscore_5m = z5m
        signal.zscore_current = z5m
        signal.probability_pct = self._zscore_to_probability(z5m)

        # 현재 divergence (5분봉 기준, lookback은 모드별 설정)
        div5m = self._divergence(price_buffer, lookback=self.config.divergence_lookback)
        signal.divergence_pct = div5m

        # Z-score 수렴 청산 체크
        if abs(z5m) <= self.config.zscore_revert_threshold:
            signal.should_exit_zscore = True

        # ── Layer 1: 1시간봉 추세 필터 ──
        trend = self._check_trend()
        signal.trend = trend
        if trend == TrendDirection.NEUTRAL:
            return signal

        # ── Layer 2: 5분봉 Z-score + divergence ──
        if abs(z5m) < self.config.entry_zscore:
            return signal
        if abs(z5m) > self.config.max_zscore:
            return signal
        if abs(div5m) < self.config.divergence_threshold_pct:
            if self._log_throttle("layer2_div"):
                logger.info(
                    "Layer2 blocked: div=%.4f%% < threshold=%.2f%% (Z=%.2f trend=%s)",
                    div5m, self.config.divergence_threshold_pct, z5m, trend.value,
                )
            return signal

        # 방향 결정 (z5m > 0 → ETH 과매수 → LONG_BTC_SHORT_ETH)
        direction = (
            SignalDirection.LONG_BTC_SHORT_ETH if z5m > 0
            else SignalDirection.SHORT_BTC_LONG_ETH
        )

        # 추세 방향과 일치하는지 확인
        if not self._trend_matches(trend, direction):
            if self._log_throttle("trend_mismatch"):
                logger.info(
                    "Layer1/2 trend mismatch: trend=%s direction=%s Z=%.2f",
                    trend.value, direction.value, z5m,
                )
            return signal

        # ── Layer 3: 틱 수준 peak 수렴 감지 ──
        peak_reverted = self._check_peak_revert(direction)
        signal.peak_revert_detected = peak_reverted
        if not peak_reverted:
            if self._log_throttle("layer3"):
                logger.info(
                    "Layer3 blocked: peak=%.4f current=%.4f ratio=%.2f (need <=%.2f) dir=%s",
                    self._tick_spread_peak, self._tick_spread_current,
                    self.config.peak_revert_ratio, self.config.peak_revert_ratio,
                    direction.value,
                )
            return signal

        # 3개 레이어 모두 통과
        logger.info(
            "ALL LAYERS PASSED: Z=%.2f div=%.2f%% trend=%s dir=%s peak=%.4f",
            z5m, div5m, trend.value, direction.value, self._tick_spread_peak,
        )
        signal.should_enter = True
        signal.direction = direction
        return signal

    # ── Layer 1: 추세 필터 ──────────────────────────────────

    def _check_trend(self) -> TrendDirection:
        """1시간봉 스프레드 Z-score로 추세 방향을 판단합니다."""
        if len(self._spread_1h) < 10:
            return TrendDirection.NEUTRAL

        arr = np.array(list(self._spread_1h)[-24:])  # 최근 24시간
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))

        if std < 1e-10:
            return TrendDirection.NEUTRAL

        current = self._spread_1h[-1]
        z = (current - mean) / std

        if z > 0.5:
            return TrendDirection.ETH_OVERBOUGHT
        elif z < -0.5:
            return TrendDirection.BTC_OVERBOUGHT
        return TrendDirection.NEUTRAL

    def _trend_matches(self, trend: TrendDirection, direction: SignalDirection) -> bool:
        """추세 방향과 진입 방향이 일치하는지."""
        if trend == TrendDirection.ETH_OVERBOUGHT:
            return direction == SignalDirection.LONG_BTC_SHORT_ETH
        elif trend == TrendDirection.BTC_OVERBOUGHT:
            return direction == SignalDirection.SHORT_BTC_LONG_ETH
        return False

    # ── Layer 2: Z-score 계산 ───────────────────────────────

    @staticmethod
    def _zscore(spreads: Deque[float], window: int) -> tuple[float, float, float]:
        """스프레드 히스토리에서 Z-score를 계산합니다."""
        if len(spreads) < window:
            return 0.0, 0.0, 0.0

        arr = np.array(list(spreads)[-window:])
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))

        if std < 1e-10:
            return 0.0, mean, std

        current = spreads[-1]
        return (current - mean) / std, mean, std

    @staticmethod
    def _zscore_to_probability(z: float) -> float:
        """Z-score → 단측 발생 확률 (%)."""
        return float((1 - norm.cdf(abs(z))) * 100.0)

    def _divergence(self, price_buffer: PriceBuffer, lookback: int = 12) -> float:
        """5분봉 기준 divergence(%) 계산."""
        btc_prices = price_buffer.btc.get_close_prices("5m", lookback + 1)
        eth_prices = price_buffer.eth.get_close_prices("5m", lookback + 1)

        if len(btc_prices) < 2 or len(eth_prices) < 2:
            return 0.0

        btc_ret = (btc_prices[-1] - btc_prices[0]) / btc_prices[0] * 100.0 if btc_prices[0] != 0 else 0.0
        eth_ret = (eth_prices[-1] - eth_prices[0]) / eth_prices[0] * 100.0 if eth_prices[0] != 0 else 0.0

        return eth_ret - btc_ret

    # ── Layer 3: Peak 수렴 감지 ─────────────────────────────

    def _update_tick_peak(self, price_buffer: PriceBuffer) -> None:
        """
        틱 스프레드의 peak을 항상 추적합니다.
        Layer 1/2 통과 여부와 무관하게 매 틱마다 호출.
        """
        btc_ticks = price_buffer.btc.get_recent_ticks(100)
        eth_ticks = price_buffer.eth.get_recent_ticks(100)

        if len(btc_ticks) < 10 or len(eth_ticks) < 10:
            return

        btc_latest = btc_ticks[-1].price
        eth_latest = eth_ticks[-1].price
        btc_base = btc_ticks[-min(50, len(btc_ticks))].price
        eth_base = eth_ticks[-min(50, len(eth_ticks))].price

        if btc_base == 0 or eth_base == 0:
            return

        current_spread = (eth_latest / eth_base - 1) * 100 - (btc_latest / btc_base - 1) * 100
        self._tick_spread_current = current_spread
        abs_spread = abs(current_spread)

        # peak 업데이트
        if abs_spread > abs(self._tick_spread_peak):
            self._tick_spread_peak = current_spread
            self._peak_ready = True
            # 방향 추적: spread > 0 → ETH 과매수
            self._tick_spread_direction = (
                SignalDirection.LONG_BTC_SHORT_ETH if current_spread > 0
                else SignalDirection.SHORT_BTC_LONG_ETH
            )

    def _check_peak_revert(self, direction: SignalDirection) -> bool:
        """
        peak에서 수렴 전환이 감지되었는지 확인합니다.
        _update_tick_peak()에서 이미 peak이 추적되고 있으므로
        여기서는 revert 조건만 체크합니다.
        """
        if not self._peak_ready:
            return False

        if abs(self._tick_spread_peak) < 0.01:
            return False

        abs_current = abs(self._tick_spread_current)
        revert_level = abs(self._tick_spread_peak) * self.config.peak_revert_ratio

        if abs_current <= revert_level and self._tick_spread_direction == direction:
            # 수렴 감지 → peak 리셋
            self._tick_spread_peak = 0.0
            self._peak_ready = False
            return True

        return False

    def _log_throttle(self, key: str, interval: float = 30.0) -> bool:
        """진단 로그를 interval초에 1회로 제한합니다."""
        import time
        now = time.time()
        last = self._last_log_time.get(key, 0.0)
        if now - last >= interval:
            self._last_log_time[key] = now
            return True
        return False

    # ── 스프레드 히스토리 업데이트 ──────────────────────────

    def _update_spreads(self, price_buffer: PriceBuffer) -> None:
        """PriceBuffer에서 최신 스프레드를 계산하여 히스토리에 추가합니다."""
        # 5분봉 스프레드
        btc_5m = price_buffer.btc.get_close_prices("5m", 2)
        eth_5m = price_buffer.eth.get_close_prices("5m", 2)
        if len(btc_5m) >= 2 and len(eth_5m) >= 2:
            btc_ret = (btc_5m[-1] - btc_5m[-2]) / btc_5m[-2] * 100 if btc_5m[-2] != 0 else 0
            eth_ret = (eth_5m[-1] - eth_5m[-2]) / eth_5m[-2] * 100 if eth_5m[-2] != 0 else 0
            spread_5m = eth_ret - btc_ret
            # 중복 방지: 마지막 값과 다를 때만 추가
            if not self._spread_5m or abs(self._spread_5m[-1] - spread_5m) > 1e-10:
                self._spread_5m.append(spread_5m)

        # 1시간봉 스프레드
        btc_1h = price_buffer.btc.get_close_prices("1h", 2)
        eth_1h = price_buffer.eth.get_close_prices("1h", 2)
        if len(btc_1h) >= 2 and len(eth_1h) >= 2:
            btc_ret = (btc_1h[-1] - btc_1h[-2]) / btc_1h[-2] * 100 if btc_1h[-2] != 0 else 0
            eth_ret = (eth_1h[-1] - eth_1h[-2]) / eth_1h[-2] * 100 if eth_1h[-2] != 0 else 0
            spread_1h = eth_ret - btc_ret
            if not self._spread_1h or abs(self._spread_1h[-1] - spread_1h) > 1e-10:
                self._spread_1h.append(spread_1h)

    # ── 상태 관리 ───────────────────────────────────────────

    def update_config(self, config: MultiTFConfig) -> None:
        """설정을 업데이트합니다."""
        self.config = config
        self._spread_5m = deque(maxlen=config.z_window_5m * 2)
        logger.info("Signal config updated: mode=%s", config.mode.value)

    def reset(self) -> None:
        """엔진 상태를 초기화합니다."""
        self._spread_5m.clear()
        self._spread_1h.clear()
        self._tick_spread_peak = 0.0
        self._tick_spread_current = 0.0
        self._tick_spread_direction = SignalDirection.NONE
        self._peak_ready = False

    def get_status(self) -> dict:
        """현재 엔진 상태 (대시보드용)."""
        z5m, mean, std = self._zscore(self._spread_5m, self.config.z_window_5m) if self.has_enough_data else (0, 0, 0)
        prob = self._zscore_to_probability(z5m) if z5m != 0 else 50.0
        current_spread = self._spread_5m[-1] if self._spread_5m else 0.0
        trend = self._check_trend()

        return {
            "mode": self.config.mode.value,
            "spread_5m_pct": round(current_spread, 4),
            "zscore_5m": round(z5m, 4),
            "probability_pct": round(prob, 2),
            "trend_1h": trend.value,
            "spread_5m_history_len": len(self._spread_5m),
            "spread_1h_history_len": len(self._spread_1h),
            "window": self.config.z_window_5m,
            "has_enough_data": self.has_enough_data,
            "peak_spread": round(self._tick_spread_peak, 4),
            "current_tick_spread": round(self._tick_spread_current, 4),
            "peak_ready": self._peak_ready,
        }


# ── 기존 SignalEngine (v1 호환) ───────────────────────────────

class SignalEngine:
    """
    BTC/ETH 페어 트레이딩 시그널 엔진 (v1).

    단일 타임프레임 Z-score 기반. 하위 호환을 위해 유지.
    새 코드에서는 MultiTimeframeSignalEngine을 사용하세요.
    """

    def __init__(self, config: Optional[SignalConfig] = None):
        self.config = config or SignalConfig()
        self._spread_history: list[float] = []
        self._confirmation_count: int = 0
        self._last_signal_direction: SignalDirection = SignalDirection.NONE

    @property
    def window(self) -> int:
        return self.config.sigma_window

    @property
    def has_enough_data(self) -> bool:
        return len(self._spread_history) >= self.window

    @property
    def spread_history_len(self) -> int:
        return len(self._spread_history)

    @staticmethod
    def calculate_return(prices: list[float], lookback: int) -> float:
        if len(prices) < lookback + 1:
            return 0.0
        old_price = prices[-(lookback + 1)]
        new_price = prices[-1]
        if old_price == 0:
            return 0.0
        return ((new_price - old_price) / old_price) * 100.0

    @staticmethod
    def calculate_spread(btc_return_pct: float, eth_return_pct: float) -> float:
        return eth_return_pct - btc_return_pct

    def add_spread(self, spread: float) -> None:
        self._spread_history.append(spread)
        max_len = self.window * 2
        if len(self._spread_history) > max_len:
            self._spread_history = self._spread_history[-max_len:]

    def calculate_zscore(self) -> tuple[float, float, float]:
        if not self.has_enough_data:
            return 0.0, 0.0, 0.0
        arr = np.array(self._spread_history[-self.window:])
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))
        if std < 1e-10:
            return 0.0, mean, std
        current = self._spread_history[-1]
        zscore = (current - mean) / std
        return zscore, mean, std

    @staticmethod
    def zscore_to_probability(z: float) -> float:
        return float((1 - norm.cdf(abs(z))) * 100.0)

    def check_entry(self, btc_return_pct: float, eth_return_pct: float) -> SignalResult:
        spread = self.calculate_spread(btc_return_pct, eth_return_pct)
        self.add_spread(spread)
        result = SignalResult(spread_pct=spread)

        if not self.has_enough_data:
            self._confirmation_count = 0
            return result

        zscore, mean, std = self.calculate_zscore()
        prob = self.zscore_to_probability(zscore)
        result.zscore = zscore
        result.mean = mean
        result.std = std
        result.probability_pct = prob

        if abs(zscore) <= self.config.zscore_revert_threshold:
            result.should_exit_zscore = True

        if not self.config.sigma_enabled:
            if abs(spread) >= self.config.divergence_threshold_pct:
                direction = (
                    SignalDirection.LONG_BTC_SHORT_ETH if spread > 0
                    else SignalDirection.SHORT_BTC_LONG_ETH
                )
                result = self._apply_confirmation(result, direction)
            else:
                self._confirmation_count = 0
                self._last_signal_direction = SignalDirection.NONE
            return result

        passes = (
            abs(zscore) >= self.config.entry_zscore
            and abs(zscore) <= self.config.max_zscore
            and prob <= (100.0 - self.config.probability_threshold_pct)
            and abs(spread) >= self.config.divergence_threshold_pct
        )

        if passes:
            direction = (
                SignalDirection.LONG_BTC_SHORT_ETH if spread > 0
                else SignalDirection.SHORT_BTC_LONG_ETH
            )
            result = self._apply_confirmation(result, direction)
        else:
            self._confirmation_count = 0
            self._last_signal_direction = SignalDirection.NONE

        return result

    def _apply_confirmation(self, result: SignalResult, direction: SignalDirection) -> SignalResult:
        if direction == self._last_signal_direction:
            self._confirmation_count += 1
        else:
            self._confirmation_count = 1
            self._last_signal_direction = direction

        if self._confirmation_count >= self.config.confirmation_candles:
            result.should_enter = True
            result.direction = direction
        return result

    def check_exit_zscore(self) -> bool:
        if not self.has_enough_data:
            return False
        zscore, _, _ = self.calculate_zscore()
        return abs(zscore) <= self.config.zscore_revert_threshold

    def reset(self) -> None:
        self._spread_history.clear()
        self._confirmation_count = 0
        self._last_signal_direction = SignalDirection.NONE

    def update_config(self, config: SignalConfig) -> None:
        self.config = config

    def get_status(self) -> dict:
        zscore, mean, std = self.calculate_zscore() if self.has_enough_data else (0, 0, 0)
        prob = self.zscore_to_probability(zscore) if zscore != 0 else 50.0
        current_spread = self._spread_history[-1] if self._spread_history else 0.0
        return {
            "spread_pct": round(current_spread, 4),
            "zscore": round(zscore, 4),
            "probability_pct": round(prob, 2),
            "mean": round(mean, 4),
            "std": round(std, 4),
            "history_len": len(self._spread_history),
            "window": self.window,
            "has_enough_data": self.has_enough_data,
            "confirmation_count": self._confirmation_count,
        }
