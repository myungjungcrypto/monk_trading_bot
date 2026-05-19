"""
Fast Binance BTC/ETH parameter sweep on 5-minute bars.

This is a parameter-search helper, not a replacement for BacktestEngine. It
uses the same broad signal layers (5m z-score/divergence, 1h trend, revert
trigger, risk exits), but evaluates on 5m closes so a sweep can finish quickly.
Promising settings should still be confirmed with the exact engine.
"""

from __future__ import annotations

import argparse
import csv
import math
import time
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd


DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
DEFAULT_START = "2025-11-19"
DEFAULT_END = "2026-05-19"
DEFAULT_WARMUP_START = "2025-11-17"


@dataclass(frozen=True)
class Candidate:
    divergence_threshold_pct: float
    zscore_revert_threshold: float
    min_hold_minutes: float
    zscore_exit_min_pnl_pct: float


@dataclass
class Position:
    direction: str
    opened_at: float
    btc_entry: float
    eth_entry: float
    btc_qty: float
    eth_qty: float
    entry_zscore: float
    costs_usd: float
    peak_pnl_pct: float = 0.0


@dataclass
class Trade:
    direction: str
    opened_at: float
    closed_at: float
    entry_zscore: float
    net_pnl_usd: float
    pnl_pct: float
    exit_reason: str
    hold_hours: float


@dataclass
class SweepResult:
    candidate: Candidate
    trades: list[Trade]
    daily_pnl: list[float]
    equity_curve: list[float]
    elapsed_sec: float

    @property
    def total_trades(self) -> int:
        return len(self.trades)

    @property
    def wins(self) -> list[Trade]:
        return [trade for trade in self.trades if trade.net_pnl_usd > 0]

    @property
    def losses(self) -> list[Trade]:
        return [trade for trade in self.trades if trade.net_pnl_usd <= 0]

    @property
    def win_rate(self) -> float:
        return len(self.wins) / len(self.trades) * 100.0 if self.trades else 0.0

    @property
    def total_pnl(self) -> float:
        return sum(trade.net_pnl_usd for trade in self.trades)

    @property
    def gross_profit(self) -> float:
        return sum(trade.net_pnl_usd for trade in self.wins)

    @property
    def gross_loss(self) -> float:
        return abs(sum(trade.net_pnl_usd for trade in self.losses))

    @property
    def profit_factor(self) -> float:
        if self.gross_loss == 0:
            return float("inf") if self.gross_profit > 0 else 0.0
        return self.gross_profit / self.gross_loss

    @property
    def avg_hold_hours(self) -> float:
        return sum(trade.hold_hours for trade in self.trades) / len(self.trades) if self.trades else 0.0

    @property
    def trades_per_day(self) -> float:
        if not self.trades:
            return 0.0
        first = min(trade.opened_at for trade in self.trades)
        last = max(trade.closed_at for trade in self.trades)
        return len(self.trades) / max((last - first) / 86400.0, 1.0)

    @property
    def max_drawdown_pct(self) -> float:
        if len(self.equity_curve) < 2:
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
        std = np.std(arr, ddof=1)
        if std < 1e-10:
            return 0.0
        return float(np.mean(arr) / std * math.sqrt(365))

    @property
    def calmar_ratio(self) -> float:
        mdd = abs(self.max_drawdown_pct)
        if mdd < 1e-10:
            return 0.0
        total_days = len(self.daily_pnl) or 1
        annual_return_pct = (self.total_pnl / 500.0 * 100.0) * (365.0 / total_days)
        return annual_return_pct / mdd

    @property
    def exit_counts(self) -> Counter:
        return Counter(trade.exit_reason for trade in self.trades)

    def to_row(self) -> dict[str, float]:
        c = self.candidate
        exits = self.exit_counts
        return {
            "divergence_threshold_pct": c.divergence_threshold_pct,
            "zscore_revert_threshold": c.zscore_revert_threshold,
            "min_hold_minutes": c.min_hold_minutes,
            "zscore_exit_min_pnl_pct": c.zscore_exit_min_pnl_pct,
            "total_trades": self.total_trades,
            "win_rate": round(self.win_rate, 4),
            "trades_per_day": round(self.trades_per_day, 4),
            "total_pnl": round(self.total_pnl, 4),
            "profit_factor": round(self.profit_factor, 4),
            "avg_hold_hours": round(self.avg_hold_hours, 4),
            "max_drawdown_pct": round(self.max_drawdown_pct, 4),
            "sharpe_ratio": round(self.sharpe_ratio, 4),
            "calmar_ratio": round(self.calmar_ratio, 4),
            "exit_zscore": exits.get("ZSCORE", 0),
            "exit_tp": exits.get("TP", 0),
            "exit_timeout": exits.get("TIMEOUT", 0),
            "exit_sl": exits.get("SL", 0),
            "exit_trailing": exits.get("TRAILING", 0),
            "elapsed_sec": round(self.elapsed_sec, 2),
        }


class BarSweepSimulator:
    z_window_5m = 50
    entry_zscore = 2.0
    max_zscore = 3.5
    divergence_lookback = 12
    peak_revert_ratio = 0.90

    take_profit_pct = 0.8
    stop_loss_pct = -3.0
    max_hold_hours = 12.0
    trailing_activate_at_pct = 0.5
    trailing_trail_pct = 0.3

    size_usd = 500.0
    taker_fee_bps = 0.0
    slippage_bps = 1.0

    def __init__(self, bars: pd.DataFrame, start: str, candidate: Candidate):
        self.bars = bars
        self.start_dt = _date(start)
        self.candidate = candidate
        self.position: Optional[Position] = None
        self.trades: list[Trade] = []
        self.daily_pnl: list[float] = []
        self.equity_curve: list[float] = []
        self.current_day = ""
        self.day_pnl = 0.0
        self.cumulative_pnl = 0.0
        self.peak_spread = 0.0
        self.peak_direction = "NONE"

    def run(self) -> SweepResult:
        t0 = time.time()
        for row in self.bars.itertuples():
            ts: pd.Timestamp = row.Index
            if ts < self.start_dt:
                self._update_peak(row.fast_spread)
                continue
            self._roll_day(ts)
            self._update_peak(row.fast_spread)

            if self.position is None and self._should_enter(row):
                direction = "LONG_BTC_SHORT_ETH" if row.zscore_5m > 0 else "SHORT_BTC_LONG_ETH"
                self._open(direction, ts.timestamp(), row.btc_close, row.eth_close, row.zscore_5m)

            if self.position is not None:
                reason = self._exit_reason(row, ts.timestamp(), row.btc_close, row.eth_close)
                if reason:
                    self._close(ts.timestamp(), row.btc_close, row.eth_close, reason)

        if self.position is not None:
            last = self.bars.iloc[-1]
            self._close(self.bars.index[-1].timestamp(), last.btc_close, last.eth_close, "END_OF_DATA")

        if self.current_day:
            self.daily_pnl.append(self.day_pnl)

        return SweepResult(
            candidate=self.candidate,
            trades=self.trades,
            daily_pnl=self.daily_pnl,
            equity_curve=self.equity_curve,
            elapsed_sec=time.time() - t0,
        )

    def _roll_day(self, ts: pd.Timestamp) -> None:
        day = ts.strftime("%Y-%m-%d")
        if day == self.current_day:
            return
        if self.current_day:
            self.daily_pnl.append(self.day_pnl)
            self.day_pnl = 0.0
        self.current_day = day

    def _update_peak(self, spread: float) -> None:
        if abs(spread) > abs(self.peak_spread):
            self.peak_spread = spread
            self.peak_direction = "LONG_BTC_SHORT_ETH" if spread > 0 else "SHORT_BTC_LONG_ETH"

    def _should_enter(self, row) -> bool:
        if not np.isfinite(row.zscore_5m):
            return False
        if abs(row.zscore_5m) < self.entry_zscore or abs(row.zscore_5m) > self.max_zscore:
            return False
        if abs(row.divergence_pct) < self.candidate.divergence_threshold_pct:
            return False
        direction = "LONG_BTC_SHORT_ETH" if row.zscore_5m > 0 else "SHORT_BTC_LONG_ETH"
        if row.trend == 0:
            return False
        if row.trend > 0 and direction != "LONG_BTC_SHORT_ETH":
            return False
        if row.trend < 0 and direction != "SHORT_BTC_LONG_ETH":
            return False
        if self.peak_direction != direction or abs(self.peak_spread) < 0.01:
            return False
        if abs(row.fast_spread) > abs(self.peak_spread) * self.peak_revert_ratio:
            return False
        self.peak_spread = 0.0
        self.peak_direction = "NONE"
        return True

    def _open(self, direction: str, timestamp: float, btc_price: float, eth_price: float, zscore: float) -> None:
        self.position = Position(
            direction=direction,
            opened_at=timestamp,
            btc_entry=btc_price,
            eth_entry=eth_price,
            btc_qty=self.size_usd / btc_price,
            eth_qty=self.size_usd / eth_price,
            entry_zscore=zscore,
            costs_usd=self.size_usd * 4 * (self.taker_fee_bps + self.slippage_bps) / 10_000.0,
        )

    def _pnl(self, btc_price: float, eth_price: float) -> tuple[float, float]:
        assert self.position is not None
        p = self.position
        if p.direction == "LONG_BTC_SHORT_ETH":
            gross = (btc_price - p.btc_entry) * p.btc_qty + (p.eth_entry - eth_price) * p.eth_qty
        else:
            gross = (p.btc_entry - btc_price) * p.btc_qty + (eth_price - p.eth_entry) * p.eth_qty
        net = gross - p.costs_usd
        return net, net / (self.size_usd * 2) * 100.0

    def _exit_reason(self, row, timestamp: float, btc_price: float, eth_price: float) -> Optional[str]:
        assert self.position is not None
        pnl_usd, pnl_pct = self._pnl(btc_price, eth_price)
        if pnl_pct >= self.take_profit_pct:
            return "TP"
        if pnl_pct <= self.stop_loss_pct:
            return "SL"
        if pnl_pct >= self.trailing_activate_at_pct:
            self.position.peak_pnl_pct = max(self.position.peak_pnl_pct, pnl_pct)
        if self.position.peak_pnl_pct and self.position.peak_pnl_pct - pnl_pct >= self.trailing_trail_pct:
            return "TRAILING"
        hold_minutes = (timestamp - self.position.opened_at) / 60.0
        if (
            abs(row.zscore_5m) <= self.candidate.zscore_revert_threshold
            and pnl_pct > self.candidate.zscore_exit_min_pnl_pct
            and hold_minutes >= self.candidate.min_hold_minutes
        ):
            return "ZSCORE"
        if hold_minutes / 60.0 >= self.max_hold_hours:
            return "TIMEOUT"
        return None

    def _close(self, timestamp: float, btc_price: float, eth_price: float, reason: str) -> None:
        assert self.position is not None
        pnl_usd, pnl_pct = self._pnl(btc_price, eth_price)
        trade = Trade(
            direction=self.position.direction,
            opened_at=self.position.opened_at,
            closed_at=timestamp,
            entry_zscore=self.position.entry_zscore,
            net_pnl_usd=pnl_usd,
            pnl_pct=pnl_pct,
            exit_reason=reason,
            hold_hours=(timestamp - self.position.opened_at) / 3600.0,
        )
        self.trades.append(trade)
        self.day_pnl += pnl_usd
        self.cumulative_pnl += pnl_usd
        self.equity_curve.append(self.cumulative_pnl)
        self.position = None


def default_candidates() -> list[Candidate]:
    return [
        Candidate(0.03, 1.5, 120, 0.00),
        Candidate(0.03, 1.5, 120, 0.05),
        Candidate(0.03, 1.5, 120, 0.10),
        Candidate(0.03, 1.5, 120, 0.20),
        Candidate(0.30, 1.5, 120, 0.05),
        Candidate(0.80, 1.5, 120, 0.05),
        Candidate(1.50, 1.5, 120, 0.05),
        Candidate(0.30, 1.2, 120, 0.05),
        Candidate(0.30, 1.0, 120, 0.05),
        Candidate(0.30, 1.5, 60, 0.05),
        Candidate(0.30, 1.5, 180, 0.05),
        Candidate(0.80, 1.2, 180, 0.10),
    ]


def load_bars(warmup_start: str, end: str) -> pd.DataFrame:
    btc_path = DATA_DIR / f"BTCUSDT_1m_{warmup_start}_{end}.csv"
    eth_path = DATA_DIR / f"ETHUSDT_1m_{warmup_start}_{end}.csv"
    if not btc_path.exists() or not eth_path.exists():
        raise FileNotFoundError(
            f"Missing cache files: {btc_path.name}, {eth_path.name}. "
            "Run backend.bot.backtest.run_backtest once to download Binance data."
        )

    btc = _prep(pd.read_csv(btc_path), "btc")
    eth = _prep(pd.read_csv(eth_path), "eth")
    df = btc.join(eth, how="inner")

    close_5m = df[["btc_close", "eth_close"]].resample("5min").last().dropna()
    close_1h = df[["btc_close", "eth_close"]].resample("1h").last().dropna()

    bars = close_5m.copy()
    bars["spread_5m"] = _pct(bars["eth_close"], 1) - _pct(bars["btc_close"], 1)
    bars["fast_spread"] = bars["spread_5m"]
    bars["zscore_5m"] = (
        (bars["spread_5m"] - bars["spread_5m"].rolling(50).mean())
        / bars["spread_5m"].rolling(50).std(ddof=1)
    )
    bars["divergence_pct"] = _pct(bars["eth_close"], 12) - _pct(bars["btc_close"], 12)

    spread_1h = _pct(close_1h["eth_close"], 1) - _pct(close_1h["btc_close"], 1)
    trend_z = (spread_1h - spread_1h.rolling(24).mean()) / spread_1h.rolling(24).std(ddof=1)
    trend = pd.Series(0, index=trend_z.index, dtype=int)
    trend.loc[trend_z > 0.5] = 1
    trend.loc[trend_z < -0.5] = -1
    bars["trend"] = trend.reindex(bars.index, method="ffill").fillna(0).astype(int)

    return bars.dropna()


def run_sweep(start: str, end: str, warmup_start: str, output: Path) -> list[SweepResult]:
    bars = load_bars(warmup_start, end)
    results: list[SweepResult] = []
    for idx, candidate in enumerate(default_candidates(), 1):
        result = BarSweepSimulator(bars, start, candidate).run()
        results.append(result)
        row = result.to_row()
        print(
            f"{idx:>2}/12 div={row['divergence_threshold_pct']} "
            f"z={row['zscore_revert_threshold']} hold={row['min_hold_minutes']} "
            f"minpnl={row['zscore_exit_min_pnl_pct']} trades={row['total_trades']} "
            f"pnl={row['total_pnl']} sharpe={row['sharpe_ratio']} pf={row['profit_factor']}",
            flush=True,
        )

    ranked = sorted(results, key=_sort_key, reverse=True)
    rows = [result.to_row() for result in ranked]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
    return ranked


def _sort_key(result: SweepResult) -> tuple[float, float, float]:
    return (result.sharpe_ratio, result.profit_factor, result.total_pnl)


def _prep(df: pd.DataFrame, prefix: str) -> pd.DataFrame:
    out = df[["timestamp_ms", "close"]].copy()
    out["timestamp"] = pd.to_datetime(out["timestamp_ms"], unit="ms", utc=True)
    out = out.set_index("timestamp").drop(columns=["timestamp_ms"])
    out = out.rename(columns={"close": f"{prefix}_close"})
    return out


def _pct(series: pd.Series, periods: int) -> pd.Series:
    return series.pct_change(periods=periods) * 100.0


def _date(value: str) -> pd.Timestamp:
    return pd.Timestamp(datetime.strptime(value, "%Y-%m-%d").replace(tzinfo=timezone.utc))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Fast Binance BTC/ETH parameter sweep")
    parser.add_argument("--start", default=DEFAULT_START)
    parser.add_argument("--end", default=DEFAULT_END)
    parser.add_argument("--warmup-start", default=DEFAULT_WARMUP_START)
    parser.add_argument(
        "--output",
        default=str(DATA_DIR / "binance_6mo_lighter_fast_sweep_20260519.csv"),
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    started = time.time()
    ranked = run_sweep(
        start=args.start,
        end=args.end,
        warmup_start=args.warmup_start,
        output=Path(args.output),
    )
    print(f"\nWrote {args.output}")
    print(f"Elapsed {time.time() - started:.1f}s")
    print("\nTOP 5")
    for result in ranked[:5]:
        print(result.to_row())


if __name__ == "__main__":
    main()
