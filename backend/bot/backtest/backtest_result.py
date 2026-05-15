"""
Backtest Result — 백테스트 성과 분석 & 리포트.
"""

import time
from dataclasses import dataclass, field
from typing import List, Optional

import numpy as np

from backend.bot.position_manager import PairTrade


@dataclass
class TradeRecord:
    """개별 거래 기록."""
    trade_id: str
    direction: str
    opened_at: float
    closed_at: float
    entry_zscore: float
    pnl_usd: float
    fees_usd: float
    net_pnl_usd: float
    pnl_pct: float
    exit_reason: str
    hold_hours: float


@dataclass
class BacktestResult:
    """백테스트 결과."""
    mode: str = ""
    start_date: str = ""
    end_date: str = ""
    fee_preset: str = ""
    position_size_usd: float = 0.0
    leverage: int = 1

    trades: List[TradeRecord] = field(default_factory=list)
    daily_pnl: List[float] = field(default_factory=list)
    equity_curve: List[float] = field(default_factory=list)

    def add_trade(self, trade: PairTrade, exit_reason: str) -> None:
        hold_hours = (trade.closed_at - trade.opened_at) / 3600.0
        rec = TradeRecord(
            trade_id=trade.trade_id,
            direction=trade.direction.value,
            opened_at=trade.opened_at,
            closed_at=trade.closed_at,
            entry_zscore=trade.zscore_at_entry,
            pnl_usd=trade.total_pnl_usd,
            fees_usd=trade.total_fees_usd,
            net_pnl_usd=trade.net_pnl_usd,
            pnl_pct=trade.pnl_pct,
            exit_reason=exit_reason,
            hold_hours=hold_hours,
        )
        self.trades.append(rec)

    # ── 통계 계산 ─────────────────────────────────────────────

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def winning_trades(self) -> List[TradeRecord]:
        return [t for t in self.trades if t.net_pnl_usd > 0]

    @property
    def losing_trades(self) -> List[TradeRecord]:
        return [t for t in self.trades if t.net_pnl_usd <= 0]

    @property
    def win_rate(self) -> float:
        if not self.trades:
            return 0.0
        return len(self.winning_trades) / len(self.trades) * 100.0

    @property
    def total_pnl(self) -> float:
        return sum(t.net_pnl_usd for t in self.trades)

    @property
    def total_fees(self) -> float:
        return sum(t.fees_usd for t in self.trades)

    @property
    def gross_profit(self) -> float:
        return sum(t.net_pnl_usd for t in self.winning_trades)

    @property
    def gross_loss(self) -> float:
        return abs(sum(t.net_pnl_usd for t in self.losing_trades))

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def avg_win(self) -> float:
        wins = self.winning_trades
        return sum(t.net_pnl_usd for t in wins) / len(wins) if wins else 0.0

    @property
    def avg_loss(self) -> float:
        losses = self.losing_trades
        return sum(t.net_pnl_usd for t in losses) / len(losses) if losses else 0.0

    @property
    def avg_hold_hours(self) -> float:
        if not self.trades:
            return 0.0
        return sum(t.hold_hours for t in self.trades) / len(self.trades)

    @property
    def max_drawdown_pct(self) -> float:
        if not self.equity_curve or len(self.equity_curve) < 2:
            return 0.0
        curve = np.array(self.equity_curve)
        peak = np.maximum.accumulate(curve)
        drawdowns = (curve - peak) / np.where(peak != 0, peak, 1) * 100.0
        return float(np.min(drawdowns))

    @property
    def sharpe_ratio(self) -> float:
        if len(self.daily_pnl) < 2:
            return 0.0
        arr = np.array(self.daily_pnl)
        mean = np.mean(arr)
        std = np.std(arr, ddof=1)
        if std < 1e-10:
            return 0.0
        return float(mean / std * np.sqrt(365))

    @property
    def calmar_ratio(self) -> float:
        mdd = abs(self.max_drawdown_pct)
        if mdd < 1e-10:
            return 0.0
        total_days = len(self.daily_pnl) if self.daily_pnl else 1
        annual_return_pct = (self.total_pnl / self.position_size_usd * 100.0) * (365.0 / max(total_days, 1))
        return annual_return_pct / mdd

    @property
    def trades_per_day(self) -> float:
        if not self.trades:
            return 0.0
        first = min(t.opened_at for t in self.trades)
        last = max(t.closed_at for t in self.trades)
        days = (last - first) / 86400.0
        return len(self.trades) / max(days, 1.0)

    # ── 리포트 출력 ───────────────────────────────────────────

    def summary(self) -> str:
        lines = [
            "",
            "=" * 60,
            f"  BACKTEST RESULT — {self.mode.upper()} MODE",
            "=" * 60,
            f"  Period      : {self.start_date} ~ {self.end_date}",
            f"  Fee preset  : {self.fee_preset}",
            f"  Position    : ${self.position_size_usd:.0f} x {self.leverage}x leverage",
            "-" * 60,
            f"  Total trades     : {self.total_trades}",
            f"  Win rate         : {self.win_rate:.1f}%",
            f"  Trades/day       : {self.trades_per_day:.1f}",
            "-" * 60,
            f"  Total PnL        : ${self.total_pnl:+.2f}",
            f"  Total fees       : ${self.total_fees:.2f}",
            f"  Gross profit     : ${self.gross_profit:.2f}",
            f"  Gross loss       : ${self.gross_loss:.2f}",
            f"  Profit factor    : {self.profit_factor:.2f}",
            "-" * 60,
            f"  Avg winning      : ${self.avg_win:+.2f}",
            f"  Avg losing       : ${self.avg_loss:+.2f}",
            f"  Avg hold time    : {self.avg_hold_hours * 60:.1f}min",
            "-" * 60,
            f"  Max drawdown     : {self.max_drawdown_pct:.2f}%",
            f"  Sharpe ratio     : {self.sharpe_ratio:.2f}",
            f"  Calmar ratio     : {self.calmar_ratio:.2f}",
            "=" * 60,
        ]

        # Exit reason breakdown
        reasons: dict[str, int] = {}
        for t in self.trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        if reasons:
            lines.append("  Exit reasons:")
            for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
                lines.append(f"    {reason:20s} : {count}")
            lines.append("=" * 60)

        return "\n".join(lines)
