"""
백테스트 CLI 진입점.

사용법:
    python -m backend.bot.backtest.run_backtest
    python -m backend.bot.backtest.run_backtest --mode scalp --start 2025-06-01 --end 2026-01-01
    python -m backend.bot.backtest.run_backtest --fee-preset lighter
    python -m backend.bot.backtest.run_backtest --grid-search
    python -m backend.bot.backtest.run_backtest --experiment A
    python -m backend.bot.backtest.run_backtest --experiment all
"""

import argparse
import asyncio
import csv
import itertools
import json
import logging
import sys
import time as _time
from pathlib import Path

from backend.bot.backtest.backtest_engine import BacktestConfig, BacktestEngine
from backend.bot.backtest.simulated_exchange import FeeConfig
from backend.bot.risk_manager import RiskConfig
from backend.bot.signal import MultiTFConfig, TradingMode

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)


# ── 실험 그리드 정의 ───────────────────────────────────────────────

GRID_A = {
    "name": "zscore_revert_threshold",
    "values": [0.3, 0.5, 0.8, 1.0, 1.2, 1.5],
}
GRID_B = {
    "name": "min_hold_minutes",
    "values": [0, 5, 15, 30, 60, 120],
}
GRID_C = {
    "name": "zscore_exit_min_pnl_pct",
    "values": [0.0, 0.1, 0.2, 0.3, 0.5, 0.8],
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="BTC/ETH Pair Trading Backtest")
    parser.add_argument("--mode", default="swing", choices=["scalp", "swing", "position"])
    parser.add_argument("--start", default="2025-03-17", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", default="2026-03-17", help="End date (YYYY-MM-DD)")
    parser.add_argument("--fee-preset", default="backpack",
                        choices=["lighter", "pacifica", "extended", "backpack"])
    parser.add_argument("--size", type=float, default=500.0, help="Position size USD")
    parser.add_argument("--leverage", type=int, default=3)
    parser.add_argument("--force-download", action="store_true", help="Force re-download data")
    parser.add_argument("--grid-search", action="store_true", help="Run parameter grid search")
    parser.add_argument(
        "--experiment",
        choices=["A", "B", "C", "AB", "AC", "BC", "all"],
        help="Run experiment: A(zscore_revert_threshold), B(min_hold_minutes), "
             "C(zscore_exit_min_pnl_pct), AB/AC/BC(2-way), all(3-way)",
    )
    parser.add_argument(
        "--export-results",
        help="Write full ranked grid/experiment results to .csv or .json",
    )
    return parser.parse_args()


async def run_single(config: BacktestConfig) -> None:
    """단일 백테스트 실행."""
    engine = BacktestEngine(config)
    result = await engine.run()
    print(result.summary())


async def run_grid_search(base_config: BacktestConfig, export_path: str | None = None) -> None:
    """파라미터 그리드 서치."""
    grid = {
        "entry_zscore": [1.5, 2.0, 2.5],
        "z_window_5m": [30, 50, 100],
        "take_profit_pct": [0.4, 0.8, 1.5],
        "stop_loss_pct": [-1.5, -3.0, -5.0],
    }

    keys = list(grid.keys())
    combos = list(itertools.product(*grid.values()))
    total = len(combos)

    print(f"\nGrid Search: {total} combinations")
    print("=" * 80)

    results = []

    for i, combo in enumerate(combos, 1):
        params = dict(zip(keys, combo))
        logger.info("Grid %d/%d: %s", i, total, params)

        signal_cfg = MultiTFConfig.from_mode(base_config.mode)
        signal_cfg.entry_zscore = params["entry_zscore"]
        signal_cfg.z_window_5m = params["z_window_5m"]

        risk_cfg = RiskConfig(
            take_profit_pct=params["take_profit_pct"],
            stop_loss_pct=params["stop_loss_pct"],
        )

        config = BacktestConfig(
            mode=base_config.mode,
            start_date=base_config.start_date,
            end_date=base_config.end_date,
            position_size_usd=base_config.position_size_usd,
            leverage=base_config.leverage,
            fee_preset=base_config.fee_preset,
            signal_config=signal_cfg,
            risk_config=risk_cfg,
        )

        engine = BacktestEngine(config)
        result = await engine.run()
        results.append((params, result))

    # Sharpe Ratio 기준 정렬
    results.sort(key=lambda x: x[1].sharpe_ratio, reverse=True)

    print("\n" + "=" * 80)
    print("  GRID SEARCH RESULTS (sorted by Sharpe Ratio)")
    print("=" * 80)
    print(f"  {'Z-win':>5} {'Entry-Z':>7} {'TP%':>6} {'SL%':>6} | "
          f"{'Trades':>6} {'WinR%':>6} {'PnL$':>8} {'Sharpe':>7} {'PF':>6}")
    print("-" * 80)

    for params, r in results[:20]:  # Top 20
        print(
            f"  {params['z_window_5m']:>5} "
            f"{params['entry_zscore']:>7.1f} "
            f"{params['take_profit_pct']:>6.1f} "
            f"{params['stop_loss_pct']:>6.1f} | "
            f"{r.total_trades:>6} "
            f"{r.win_rate:>6.1f} "
            f"{r.total_pnl:>+8.2f} "
            f"{r.sharpe_ratio:>7.2f} "
            f"{r.profit_factor:>6.2f}"
        )

    print("=" * 80)

    # Best result 상세 출력
    if results:
        best_params, best_result = results[0]
        print(f"\n  BEST PARAMS: {best_params}")
        print(best_result.summary())

    if export_path:
        _export_ranked_results(export_path, keys, results)


# ── 실험 (Experiment) ──────────────────────────────────────────────

def _build_experiment_grids(experiment: str) -> tuple[list[str], list[list]]:
    """실험 타입에 따라 파라미터 이름과 그리드 값 리스트를 반환."""
    grids_map = {"A": GRID_A, "B": GRID_B, "C": GRID_C}

    if experiment in ("A", "B", "C"):
        g = grids_map[experiment]
        return [g["name"]], [g["values"]]
    elif experiment == "AB":
        return (
            [GRID_A["name"], GRID_B["name"]],
            [GRID_A["values"], GRID_B["values"]],
        )
    elif experiment == "AC":
        return (
            [GRID_A["name"], GRID_C["name"]],
            [GRID_A["values"], GRID_C["values"]],
        )
    elif experiment == "BC":
        return (
            [GRID_B["name"], GRID_C["name"]],
            [GRID_B["values"], GRID_C["values"]],
        )
    else:  # all
        return (
            [GRID_A["name"], GRID_B["name"], GRID_C["name"]],
            [GRID_A["values"], GRID_B["values"], GRID_C["values"]],
        )


def _apply_experiment_params(
    params: dict,
    base_mode: str,
) -> tuple[MultiTFConfig, RiskConfig]:
    """실험 파라미터를 시그널/리스크 설정에 적용."""
    signal_cfg = MultiTFConfig.from_mode(base_mode)
    risk_cfg = BacktestEngine._default_risk_config(base_mode)

    # 방안 A: zscore_revert_threshold → signal config
    if "zscore_revert_threshold" in params:
        signal_cfg.zscore_revert_threshold = params["zscore_revert_threshold"]

    # 방안 B: min_hold_minutes → risk config
    if "min_hold_minutes" in params:
        risk_cfg.min_hold_minutes = params["min_hold_minutes"]

    # 방안 C: zscore_exit_min_pnl_pct → risk config
    if "zscore_exit_min_pnl_pct" in params:
        risk_cfg.zscore_exit_min_pnl_pct = params["zscore_exit_min_pnl_pct"]

    return signal_cfg, risk_cfg


def _format_avg_hold(avg_hold_hours: float) -> str:
    """평균 보유 시간을 읽기 좋은 형식으로 포맷."""
    minutes = avg_hold_hours * 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    return f"{avg_hold_hours:.1f}h"


def _print_experiment_header(experiment: str, param_names: list[str], total: int) -> None:
    """실험 헤더 출력."""
    desc_map = {
        "A": "zscore_revert_threshold sweep",
        "B": "min_hold_minutes sweep",
        "C": "zscore_exit_min_pnl_pct sweep",
        "AB": "zscore_revert_threshold × min_hold_minutes",
        "AC": "zscore_revert_threshold × zscore_exit_min_pnl_pct",
        "BC": "min_hold_minutes × zscore_exit_min_pnl_pct",
        "all": "A × B × C full grid",
    }
    print()
    print("=" * 90)
    print(f"  EXPERIMENT {experiment}: {desc_map.get(experiment, experiment)}")
    print(f"  Combinations: {total}")
    print("=" * 90)


def _print_experiment_results(
    experiment: str,
    param_names: list[str],
    results: list[tuple[dict, "BacktestResult"]],
) -> None:
    """실험 결과 테이블 출력."""
    # 헤더 구성
    param_headers = "  ".join(f"{n[:12]:>12}" for n in param_names)
    header = (
        f"  {param_headers} | "
        f"{'Trades':>6} {'WinR%':>6} {'PnL$':>9} "
        f"{'Sharpe':>7} {'AvgHold':>8} {'PF':>6}"
    )
    print(header)
    print("-" * 90)

    show_count = min(len(results), 30)
    for params, r in results[:show_count]:
        param_vals = "  ".join(f"{params[n]:>12.2f}" for n in param_names)
        print(
            f"  {param_vals} | "
            f"{r.total_trades:>6} "
            f"{r.win_rate:>6.1f} "
            f"{r.total_pnl:>+9.2f} "
            f"{r.sharpe_ratio:>7.2f} "
            f"{_format_avg_hold(r.avg_hold_hours):>8} "
            f"{r.profit_factor:>6.2f}"
        )

    print("=" * 90)

    if results:
        best_params, best_result = results[0]
        best_str = ", ".join(f"{k}={v}" for k, v in best_params.items())
        print(f"\n  BEST: {best_str}, Sharpe={best_result.sharpe_ratio:.2f}")
        print(best_result.summary())


def _result_export_row(params: dict, result: "BacktestResult") -> dict:
    row = dict(params)
    row.update({
        "mode": result.mode,
        "start_date": result.start_date,
        "end_date": result.end_date,
        "fee_preset": result.fee_preset,
        "position_size_usd": result.position_size_usd,
        "leverage": result.leverage,
        "total_trades": result.total_trades,
        "win_rate": result.win_rate,
        "trades_per_day": result.trades_per_day,
        "total_pnl": result.total_pnl,
        "total_fees": result.total_fees,
        "gross_profit": result.gross_profit,
        "gross_loss": result.gross_loss,
        "profit_factor": result.profit_factor,
        "avg_win": result.avg_win,
        "avg_loss": result.avg_loss,
        "avg_hold_hours": result.avg_hold_hours,
        "max_drawdown_pct": result.max_drawdown_pct,
        "sharpe_ratio": result.sharpe_ratio,
        "calmar_ratio": result.calmar_ratio,
    })
    return row


def _export_ranked_results(
    export_path: str,
    param_names: list[str],
    results: list[tuple[dict, "BacktestResult"]],
) -> None:
    path = Path(export_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = [_result_export_row(params, result) for params, result in results]

    if path.suffix.lower() == ".json":
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    else:
        fieldnames = list(param_names) + [
            "mode", "start_date", "end_date", "fee_preset", "position_size_usd",
            "leverage", "total_trades", "win_rate", "trades_per_day", "total_pnl",
            "total_fees", "gross_profit", "gross_loss", "profit_factor", "avg_win",
            "avg_loss", "avg_hold_hours", "max_drawdown_pct", "sharpe_ratio",
            "calmar_ratio",
        ]
        with path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)

    logger.info("Exported ranked results to %s (%d rows)", path, len(rows))


async def run_experiment(
    base_config: BacktestConfig,
    experiment: str,
    export_path: str | None = None,
) -> None:
    """실험 실행: 지정된 파라미터 그리드를 순회하며 백테스트."""
    from backend.bot.backtest.data_fetcher import load_pair_data

    param_names, grid_values = _build_experiment_grids(experiment)
    combos = list(itertools.product(*grid_values))
    total = len(combos)

    _print_experiment_header(experiment, param_names, total)

    # 데이터를 한 번만 다운로드하고 모든 실험에서 재사용
    warmup_start = BacktestEngine._subtract_hours(base_config.start_date, 48)
    logger.info("Pre-loading data: %s ~ %s", warmup_start, base_config.end_date)
    btc_df, eth_df = await load_pair_data(
        warmup_start, base_config.end_date,
        force=base_config.force_download,
    )
    logger.info("Data loaded: BTC=%d, ETH=%d candles", len(btc_df), len(eth_df))

    results = []
    t0 = _time.time()

    for i, combo in enumerate(combos, 1):
        params = dict(zip(param_names, combo))
        # float 변환 (min_hold_minutes 등 int 값 대응)
        params = {k: float(v) for k, v in params.items()}

        logger.info("Experiment %s [%d/%d]: %s", experiment, i, total, params)

        signal_cfg, risk_cfg = _apply_experiment_params(params, base_config.mode)

        config = BacktestConfig(
            mode=base_config.mode,
            start_date=base_config.start_date,
            end_date=base_config.end_date,
            position_size_usd=base_config.position_size_usd,
            leverage=base_config.leverage,
            fee_preset=base_config.fee_preset,
            signal_config=signal_cfg,
            risk_config=risk_cfg,
            preloaded_btc_df=btc_df,
            preloaded_eth_df=eth_df,
        )

        engine = BacktestEngine(config)
        result = await engine.run()
        results.append((params, result))

    elapsed = _time.time() - t0
    logger.info("Experiment %s completed in %.1fs (%d runs)", experiment, elapsed, total)

    # Sharpe 기준 정렬
    results.sort(key=lambda x: x[1].sharpe_ratio, reverse=True)

    _print_experiment_results(experiment, param_names, results)
    if export_path:
        _export_ranked_results(export_path, param_names, results)


# ── main ──────────────────────────────────────────────────────────

async def main() -> None:
    args = parse_args()

    config = BacktestConfig(
        mode=args.mode,
        start_date=args.start,
        end_date=args.end,
        position_size_usd=args.size,
        leverage=args.leverage,
        fee_preset=args.fee_preset,
        force_download=args.force_download,
    )

    if args.experiment:
        await run_experiment(config, args.experiment, args.export_results)
    elif args.grid_search:
        await run_grid_search(config, args.export_results)
    else:
        await run_single(config)


if __name__ == "__main__":
    asyncio.run(main())
