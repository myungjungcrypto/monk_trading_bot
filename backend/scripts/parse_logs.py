#!/usr/bin/env python3
"""
로그 파일에서 거래 기록을 파싱하여 DB에 저장하는 스크립트.

사용법:
  python -m backend.scripts.parse_logs /path/to/uvicorn.log

EC2 서버에서 실행:
  cd ~/monk_trading_bot
  python -m backend.scripts.parse_logs ~/monk_trading_bot/uvicorn.log
"""

import asyncio
import re
import sys
from datetime import datetime, timezone
from typing import List, Dict, Optional

# DB 관련
from backend.app.models import Trade, create_async_session_factory, get_database_url, init_db


def parse_log_file(path: str) -> List[Dict]:
    """로그 파일에서 거래 진입/청산을 파싱합니다."""

    # 패턴: "Pair opened: backpack_1_1710000000 | LONG_BTC_SHORT_ETH | BTC@71000.00 ETH@2100.1234 | Z=1.85"
    open_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Pair opened: (\S+) \| (\S+) \| BTC@([\d.]+) ETH@([\d.]+) \| Z=([\d.-]+)"
    )

    # 패턴: "Pair closed: backpack_1_1710000000 | reason=TP | PNL=$1.23 (0.12%)"
    close_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*Pair closed: (\S+) \| reason=(\S+) \| PNL=\$([\d.-]+) \(([\d.-]+)%\)"
    )

    # 패턴: "ENTRY SIGNAL #1: LONG_BTC_SHORT_ETH | Z5m=1.85 div=0.50% prob=3.2% trend=BULLISH"
    signal_pattern = re.compile(
        r"(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}).*ENTRY SIGNAL #\d+: (\S+) \| Z5m=([\d.-]+) div=([\d.-]+)%"
    )

    # 모드 패턴: "Bot engine starting... mode=scalp"
    mode_pattern = re.compile(r"Bot engine starting\.\.\. mode=(\w+)")

    open_trades: Dict[str, Dict] = {}
    closed_trades: List[Dict] = []
    current_mode = "scalp"

    with open(path, "r") as f:
        for line in f:
            # 모드 변경 감지
            m = mode_pattern.search(line)
            if m:
                current_mode = m.group(1)
                continue

            # 진입
            m = open_pattern.search(line)
            if m:
                ts_str, trade_id, direction, btc_price, eth_price, zscore = m.groups()
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
                open_trades[trade_id] = {
                    "trade_id": trade_id,
                    "exchange": trade_id.split("_")[0],
                    "direction": direction,
                    "btc_entry": float(btc_price),
                    "eth_entry": float(eth_price),
                    "zscore_entry": float(zscore),
                    "opened_at": ts,
                    "signal_mode": current_mode,
                }
                continue

            # 청산
            m = close_pattern.search(line)
            if m:
                ts_str, trade_id, reason, pnl, pnl_pct = m.groups()
                ts = datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)

                if trade_id in open_trades:
                    trade = open_trades.pop(trade_id)
                    trade["closed_at"] = ts
                    trade["exit_reason"] = reason
                    trade["net_pnl_usd"] = float(pnl)
                    trade["pnl_pct"] = float(pnl_pct)
                    closed_trades.append(trade)

    print(f"Parsed: {len(closed_trades)} closed trades, {len(open_trades)} still open")
    return closed_trades


async def save_to_db(trades: List[Dict]) -> int:
    """파싱된 거래를 DB에 저장합니다."""
    db_url = get_database_url(async_mode=True)
    session_factory, engine = create_async_session_factory(db_url)
    await init_db(engine)

    saved = 0
    async with session_factory() as db:
        for t in trades:
            db_trade = Trade(
                exchange=t["exchange"],
                direction=t["direction"],
                size_usd=1000.0,  # 로그에 사이즈 없으면 기본값
                btc_entry=t["btc_entry"],
                eth_entry=t["eth_entry"],
                zscore_entry=t["zscore_entry"],
                spread_entry=0.0,
                signal_mode=t.get("signal_mode", "scalp"),
                opened_at=t["opened_at"],
                closed_at=t.get("closed_at"),
                net_pnl_usd=t.get("net_pnl_usd"),
                exit_reason=t.get("exit_reason"),
            )
            db.add(db_trade)
            saved += 1

        await db.commit()

    await engine.dispose()
    print(f"Saved {saved} trades to DB")
    return saved


def print_analysis(trades: List[Dict]):
    """파싱된 거래의 빠른 분석을 출력합니다."""
    if not trades:
        print("No trades to analyze.")
        return

    pnls = [t.get("net_pnl_usd", 0) for t in trades]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p < 0]

    print("\n" + "=" * 60)
    print("  TRADE ANALYSIS (from logs)")
    print("=" * 60)
    print(f"  Total trades:     {len(trades)}")
    print(f"  Wins:             {len(wins)}")
    print(f"  Losses:           {len(losses)}")
    print(f"  Win rate:         {len(wins)/len(trades)*100:.1f}%")
    print(f"  Total PNL:        ${sum(pnls):.2f}")
    print(f"  Avg PNL/trade:    ${sum(pnls)/len(pnls):.2f}")
    if wins:
        print(f"  Avg win:          ${sum(wins)/len(wins):.2f}")
    if losses:
        print(f"  Avg loss:         ${sum(losses)/len(losses):.2f}")
    print(f"  Best trade:       ${max(pnls):.2f}")
    print(f"  Worst trade:      ${min(pnls):.2f}")

    total_wins_sum = sum(wins) if wins else 0
    total_losses_sum = abs(sum(losses)) if losses else 1
    print(f"  Profit factor:    {total_wins_sum/total_losses_sum:.2f}")

    # 모드별
    modes = {}
    for t in trades:
        mode = t.get("signal_mode", "unknown")
        if mode not in modes:
            modes[mode] = []
        modes[mode].append(t.get("net_pnl_usd", 0))

    if len(modes) > 1:
        print(f"\n  --- By Mode ---")
        for mode, mode_pnls in modes.items():
            w = sum(1 for p in mode_pnls if p > 0)
            print(f"  {mode:12s}: {len(mode_pnls)} trades, WR={w/len(mode_pnls)*100:.0f}%, PNL=${sum(mode_pnls):.2f}")

    # 방향별
    dirs = {}
    for t in trades:
        d = t["direction"]
        if d not in dirs:
            dirs[d] = []
        dirs[d].append(t.get("net_pnl_usd", 0))

    print(f"\n  --- By Direction ---")
    for d, d_pnls in dirs.items():
        w = sum(1 for p in d_pnls if p > 0)
        print(f"  {d[:25]:25s}: {len(d_pnls)} trades, WR={w/len(d_pnls)*100:.0f}%, PNL=${sum(d_pnls):.2f}")

    # 청산 사유별
    reasons = {}
    for t in trades:
        r = t.get("exit_reason", "unknown")
        if r not in reasons:
            reasons[r] = []
        reasons[r].append(t.get("net_pnl_usd", 0))

    print(f"\n  --- By Exit Reason ---")
    for r, r_pnls in reasons.items():
        print(f"  {r:12s}: {len(r_pnls)} trades, PNL=${sum(r_pnls):.2f}")

    print("=" * 60)


async def main():
    if len(sys.argv) < 2:
        print("Usage: python -m backend.scripts.parse_logs <logfile> [--save]")
        print("  --save: Save parsed trades to DB")
        sys.exit(1)

    log_path = sys.argv[1]
    save = "--save" in sys.argv

    trades = parse_log_file(log_path)
    print_analysis(trades)

    if save and trades:
        await save_to_db(trades)
    elif trades and not save:
        print("\nTip: Add --save to save these trades to the DB")


if __name__ == "__main__":
    asyncio.run(main())
