"""
Risk Manager — 리스크 관리 모듈.

TP/SL, 트레일링 스탑, 일일 손실 한도, 최대 보유 시간 등
리스크 관련 의사결정을 담당합니다.
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Dict, List, Optional

from backend.bot.position_manager import PairTrade

logger = logging.getLogger(__name__)


class ExitReason(str, Enum):
    """청산 사유."""
    TAKE_PROFIT = "TP"
    STOP_LOSS = "SL"
    ZSCORE_REVERT = "ZSCORE"
    TRAILING_STOP = "TRAILING"
    TIMEOUT = "TIMEOUT"
    DAILY_LIMIT = "DAILY_LIMIT"
    MANUAL = "MANUAL"


class RiskAction(str, Enum):
    """리스크 매니저가 요청하는 액션."""
    HOLD = "HOLD"                    # 유지
    EXIT = "EXIT"                    # 청산
    AVERAGING_DOWN = "AVERAGING"     # 물타기
    SIZE_REDUCTION = "REDUCTION"     # 사이즈 축소


@dataclass
class RiskDecision:
    """리스크 판단 결과."""
    action: RiskAction = RiskAction.HOLD
    reason: ExitReason = ExitReason.MANUAL
    message: str = ""


@dataclass
class RiskConfig:
    """리스크 관리 설정 (CLAUDE.md 3-3, 3-4 참조)."""
    # 청산 조건
    take_profit_pct: float = 0.8
    stop_loss_pct: float = -3.0
    max_hold_hours: float = 24.0

    # 트레일링 스탑
    trailing_stop_enabled: bool = True
    trailing_activate_at_pct: float = 0.5
    trailing_trail_pct: float = 0.3

    # 리스크 관리
    max_open_trades: int = 3
    daily_loss_limit_usd: float = -200.0

    # Averaging
    averaging_enabled: bool = True
    averaging_trigger_pct: float = -1.5
    averaging_multiplier: float = 0.5

    # Size Reduction
    size_reduction_enabled: bool = True
    size_reduction_trigger_pct: float = -2.0
    size_reduction_ratio: float = 0.5


class RiskManager:
    """
    리스크 매니저.

    각 오픈 트레이드에 대해 TP/SL/트레일링/타임아웃을 체크하고,
    일일 손실 한도, 최대 포지션 수를 관리합니다.
    """

    def __init__(self, config: Optional[RiskConfig] = None):
        self.config = config or RiskConfig()
        # 트레일링 스탑: trade_id → 최고 수익률(%)
        self._peak_pnl_pct: Dict[str, float] = {}
        # 일일 실현 손익
        self._daily_realized_pnl: float = 0.0
        self._daily_reset_date: str = ""

    # ── 메인 판단 ─────────────────────────────────────────────

    def evaluate(self, trade: PairTrade, zscore_reverted: bool = False) -> RiskDecision:
        """
        트레이드에 대한 리스크 판단을 수행합니다.

        Args:
            trade: 오픈 트레이드
            zscore_reverted: Z-score가 수렴 임계값 이하인지

        Returns:
            RiskDecision
        """
        pnl_pct = trade.pnl_pct

        # 1. 일일 손실 한도 체크
        self._check_daily_reset()
        if self._daily_realized_pnl <= self.config.daily_loss_limit_usd:
            return RiskDecision(
                action=RiskAction.EXIT,
                reason=ExitReason.DAILY_LIMIT,
                message=f"Daily loss limit reached: ${self._daily_realized_pnl:.2f}",
            )

        # 2. 익절 (Take Profit)
        if pnl_pct >= self.config.take_profit_pct:
            return RiskDecision(
                action=RiskAction.EXIT,
                reason=ExitReason.TAKE_PROFIT,
                message=f"TP hit: {pnl_pct:.2f}% >= {self.config.take_profit_pct}%",
            )

        # 3. 손절 (Stop Loss)
        if pnl_pct <= self.config.stop_loss_pct:
            return RiskDecision(
                action=RiskAction.EXIT,
                reason=ExitReason.STOP_LOSS,
                message=f"SL hit: {pnl_pct:.2f}% <= {self.config.stop_loss_pct}%",
            )

        # 4. 트레일링 스탑
        if self.config.trailing_stop_enabled:
            trailing = self._check_trailing_stop(trade.trade_id, pnl_pct)
            if trailing is not None:
                return trailing

        # 5. Z-score 수렴 청산
        if zscore_reverted and pnl_pct > 0:
            return RiskDecision(
                action=RiskAction.EXIT,
                reason=ExitReason.ZSCORE_REVERT,
                message=f"Z-score reverted with profit: {pnl_pct:.2f}%",
            )

        # 6. 최대 보유 시간 초과
        hold_hours = (time.time() - trade.opened_at) / 3600.0
        if hold_hours >= self.config.max_hold_hours:
            return RiskDecision(
                action=RiskAction.EXIT,
                reason=ExitReason.TIMEOUT,
                message=f"Max hold time exceeded: {hold_hours:.1f}h >= {self.config.max_hold_hours}h",
            )

        # 7. Averaging Down 조건
        if (
            self.config.averaging_enabled
            and pnl_pct <= self.config.averaging_trigger_pct
            and pnl_pct > self.config.stop_loss_pct  # SL 근처면 averaging 안 함
        ):
            return RiskDecision(
                action=RiskAction.AVERAGING_DOWN,
                reason=ExitReason.MANUAL,
                message=f"Averaging trigger: {pnl_pct:.2f}% <= {self.config.averaging_trigger_pct}%",
            )

        # 8. Size Reduction 조건
        if (
            self.config.size_reduction_enabled
            and pnl_pct <= self.config.size_reduction_trigger_pct
            and pnl_pct > self.config.stop_loss_pct
        ):
            return RiskDecision(
                action=RiskAction.SIZE_REDUCTION,
                reason=ExitReason.MANUAL,
                message=f"Size reduction trigger: {pnl_pct:.2f}% <= {self.config.size_reduction_trigger_pct}%",
            )

        # 유지
        return RiskDecision(
            action=RiskAction.HOLD,
            message=f"Hold: PNL={pnl_pct:.2f}%, hold={hold_hours:.1f}h",
        )

    # ── 트레일링 스탑 ─────────────────────────────────────────

    def _check_trailing_stop(
        self, trade_id: str, pnl_pct: float
    ) -> Optional[RiskDecision]:
        """트레일링 스탑 체크. 청산 시 RiskDecision 반환, 아니면 None."""
        activate = self.config.trailing_activate_at_pct
        trail = self.config.trailing_trail_pct

        # 활성화 기준 미달
        peak = self._peak_pnl_pct.get(trade_id, 0.0)

        if pnl_pct >= activate:
            # 최고점 갱신
            if pnl_pct > peak:
                self._peak_pnl_pct[trade_id] = pnl_pct
                peak = pnl_pct

            # 최고점 대비 trail_pct 이상 하락 시 청산
            drawdown = peak - pnl_pct
            if drawdown >= trail:
                return RiskDecision(
                    action=RiskAction.EXIT,
                    reason=ExitReason.TRAILING_STOP,
                    message=f"Trailing stop: peak={peak:.2f}% → now={pnl_pct:.2f}% (dd={drawdown:.2f}%)",
                )

        return None

    # ── 일일 손실 관리 ────────────────────────────────────────

    def record_realized_pnl(self, pnl_usd: float) -> None:
        """실현 PNL을 기록합니다."""
        self._check_daily_reset()
        self._daily_realized_pnl += pnl_usd
        logger.info("Daily PNL updated: $%.2f (total: $%.2f)", pnl_usd, self._daily_realized_pnl)

    def _check_daily_reset(self) -> None:
        """날짜가 바뀌면 일일 PNL을 리셋합니다."""
        today = time.strftime("%Y-%m-%d")
        if today != self._daily_reset_date:
            self._daily_realized_pnl = 0.0
            self._daily_reset_date = today
            logger.info("Daily PNL reset for %s", today)

    @property
    def daily_realized_pnl(self) -> float:
        self._check_daily_reset()
        return self._daily_realized_pnl

    # ── 진입 허용 체크 ────────────────────────────────────────

    def can_open_trade(self, current_open_count: int) -> bool:
        """새 트레이드를 열 수 있는지 확인합니다."""
        self._check_daily_reset()

        if current_open_count >= self.config.max_open_trades:
            logger.info("Max open trades reached: %d", current_open_count)
            return False

        if self._daily_realized_pnl <= self.config.daily_loss_limit_usd:
            logger.info("Daily loss limit reached: $%.2f", self._daily_realized_pnl)
            return False

        return True

    # ── 트레이드 종료 정리 ────────────────────────────────────

    def on_trade_closed(self, trade_id: str, realized_pnl: float) -> None:
        """트레이드가 종료되었을 때 호출합니다."""
        self.record_realized_pnl(realized_pnl)
        self._peak_pnl_pct.pop(trade_id, None)

    # ── 설정 ──────────────────────────────────────────────────

    def update_config(self, config: RiskConfig) -> None:
        """설정을 업데이트합니다."""
        self.config = config

    def get_status(self) -> dict:
        """현재 리스크 매니저 상태 (대시보드용)."""
        self._check_daily_reset()
        return {
            "daily_realized_pnl": round(self._daily_realized_pnl, 2),
            "daily_loss_limit": self.config.daily_loss_limit_usd,
            "max_open_trades": self.config.max_open_trades,
            "trailing_peaks": dict(self._peak_pnl_pct),
        }
