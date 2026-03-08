"""
Signal Engine — BTC/ETH 페어 트레이딩 Z-score 기반 시그널 생성.

스프레드(ETH 수익률 - BTC 수익률)의 Z-score를 계산하고,
진입/청산 조건을 판단합니다.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional

import numpy as np
from scipy.stats import norm


class SignalDirection(str, Enum):
    """진입 방향."""
    LONG_BTC_SHORT_ETH = "LONG_BTC_SHORT_ETH"  # spread > 0 → ETH 과매수
    SHORT_BTC_LONG_ETH = "SHORT_BTC_LONG_ETH"  # spread < 0 → BTC 과매수
    NONE = "NONE"


@dataclass
class SignalResult:
    """시그널 엔진 출력."""
    should_enter: bool = False
    direction: SignalDirection = SignalDirection.NONE
    zscore: float = 0.0
    spread_pct: float = 0.0
    probability_pct: float = 0.0
    mean: float = 0.0
    std: float = 0.0

    # 청산 관련
    should_exit_zscore: bool = False


@dataclass
class SignalConfig:
    """시그널 엔진 설정 (CLAUDE.md 3-2, 3-3 참조)."""
    # 진입 조건
    divergence_threshold_pct: float = 2.5
    lookback_minutes: int = 15
    confirmation_candles: int = 2

    # Z-score 필터
    sigma_enabled: bool = True
    sigma_window: int = 100
    entry_zscore: float = 2.0
    max_zscore: float = 3.5
    probability_threshold_pct: float = 95.0

    # 청산 조건 (Z-score 수렴)
    zscore_revert_threshold: float = 0.5


class SignalEngine:
    """
    BTC/ETH 페어 트레이딩 시그널 엔진.

    스프레드 = ETH 수익률 - BTC 수익률
    Z-score = (현재 스프레드 - 이동평균) / 이동표준편차

    Z-score가 진입 범위 안에 있고 divergence 임계값을 넘으면 진입 신호.
    Z-score가 수렴 임계값 이하로 떨어지면 청산 신호.
    """

    def __init__(self, config: Optional[SignalConfig] = None):
        self.config = config or SignalConfig()
        # 스프레드 히스토리 (numpy 배열로 유지)
        self._spread_history: list[float] = []
        # 연속 확인 카운터
        self._confirmation_count: int = 0
        self._last_signal_direction: SignalDirection = SignalDirection.NONE

    @property
    def window(self) -> int:
        return self.config.sigma_window

    @property
    def has_enough_data(self) -> bool:
        """Z-score 계산에 충분한 데이터가 있는지."""
        return len(self._spread_history) >= self.window

    @property
    def spread_history_len(self) -> int:
        return len(self._spread_history)

    # ── 핵심 계산 ─────────────────────────────────────────────

    @staticmethod
    def calculate_return(prices: list[float], lookback: int) -> float:
        """
        가격 리스트에서 lookback 기간 수익률(%)을 계산합니다.

        Args:
            prices: 시간순 가격 리스트 (최신이 마지막)
            lookback: 몇 개 전 가격 대비 수익률

        Returns:
            수익률 (%)
        """
        if len(prices) < lookback + 1:
            return 0.0
        old_price = prices[-(lookback + 1)]
        new_price = prices[-1]
        if old_price == 0:
            return 0.0
        return ((new_price - old_price) / old_price) * 100.0

    @staticmethod
    def calculate_spread(btc_return_pct: float, eth_return_pct: float) -> float:
        """스프레드 = ETH 수익률 - BTC 수익률 (%)."""
        return eth_return_pct - btc_return_pct

    def add_spread(self, spread: float) -> None:
        """스프레드 값을 히스토리에 추가합니다."""
        self._spread_history.append(spread)
        # 메모리 관리: 윈도우의 2배까지만 유지
        max_len = self.window * 2
        if len(self._spread_history) > max_len:
            self._spread_history = self._spread_history[-max_len:]

    def calculate_zscore(self) -> tuple[float, float, float]:
        """
        현재 스프레드 히스토리에서 Z-score를 계산합니다.

        Returns:
            (zscore, mean, std)
        """
        if not self.has_enough_data:
            return 0.0, 0.0, 0.0

        arr = np.array(self._spread_history[-self.window:])
        mean = float(np.mean(arr))
        std = float(np.std(arr, ddof=1))  # 표본 표준편차

        if std < 1e-10:  # 표준편차가 0에 가까우면
            return 0.0, mean, std

        current = self._spread_history[-1]
        zscore = (current - mean) / std
        return zscore, mean, std

    @staticmethod
    def zscore_to_probability(z: float) -> float:
        """
        Z-score를 단측 발생 확률(%)로 변환합니다.

        |z|=2.0 → 약 2.28% (상위 2.28%)
        반환값이 작을수록 극단적인 이벤트.
        """
        return float((1 - norm.cdf(abs(z))) * 100.0)

    # ── 진입 판단 ─────────────────────────────────────────────

    def check_entry(self, btc_return_pct: float, eth_return_pct: float) -> SignalResult:
        """
        새로운 수익률 데이터로 진입 조건을 판단합니다.

        1) 스프레드 계산 → 히스토리 추가
        2) Z-score 계산
        3) 진입 조건 체크 (divergence, z-score 범위, 확률, confirmation)

        Args:
            btc_return_pct: BTC lookback 수익률 (%)
            eth_return_pct: ETH lookback 수익률 (%)

        Returns:
            SignalResult
        """
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

        # Z-score 수렴 청산 체크
        if abs(zscore) <= self.config.zscore_revert_threshold:
            result.should_exit_zscore = True

        # 시그마 필터 비활성화 시 divergence만 체크
        if not self.config.sigma_enabled:
            if abs(spread) >= self.config.divergence_threshold_pct:
                direction = (
                    SignalDirection.LONG_BTC_SHORT_ETH
                    if spread > 0
                    else SignalDirection.SHORT_BTC_LONG_ETH
                )
                result = self._apply_confirmation(result, direction)
            else:
                self._confirmation_count = 0
                self._last_signal_direction = SignalDirection.NONE
            return result

        # 진입 조건 체크
        passes = (
            abs(zscore) >= self.config.entry_zscore
            and abs(zscore) <= self.config.max_zscore
            and prob <= (100.0 - self.config.probability_threshold_pct)
            and abs(spread) >= self.config.divergence_threshold_pct
        )

        if passes:
            direction = (
                SignalDirection.LONG_BTC_SHORT_ETH
                if spread > 0
                else SignalDirection.SHORT_BTC_LONG_ETH
            )
            result = self._apply_confirmation(result, direction)
        else:
            self._confirmation_count = 0
            self._last_signal_direction = SignalDirection.NONE

        return result

    def _apply_confirmation(
        self, result: SignalResult, direction: SignalDirection
    ) -> SignalResult:
        """confirmation_candles 연속 조건을 적용합니다."""
        if direction == self._last_signal_direction:
            self._confirmation_count += 1
        else:
            self._confirmation_count = 1
            self._last_signal_direction = direction

        if self._confirmation_count >= self.config.confirmation_candles:
            result.should_enter = True
            result.direction = direction

        return result

    # ── 청산 판단 ─────────────────────────────────────────────

    def check_exit_zscore(self) -> bool:
        """현재 Z-score가 수렴 임계값 이하인지 확인합니다."""
        if not self.has_enough_data:
            return False
        zscore, _, _ = self.calculate_zscore()
        return abs(zscore) <= self.config.zscore_revert_threshold

    # ── 상태 관리 ─────────────────────────────────────────────

    def reset(self) -> None:
        """엔진 상태를 초기화합니다."""
        self._spread_history.clear()
        self._confirmation_count = 0
        self._last_signal_direction = SignalDirection.NONE

    def update_config(self, config: SignalConfig) -> None:
        """설정을 업데이트합니다. 히스토리는 유지."""
        self.config = config

    def get_status(self) -> dict:
        """현재 엔진 상태 요약을 반환합니다 (대시보드용)."""
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
