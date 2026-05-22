"""Variational browser-gate request writer.

The bot does not click Variational directly. It writes request JSON files for
tools/variational-browser, which prepares the UI and asks Telegram for approval.
"""

from __future__ import annotations

import json
import os
from argparse import Namespace
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from backend.bot.fair_price import FairPriceOracle
from backend.bot.position_manager import PairDirection, PairTrade
from backend.scripts.create_variational_browser_request import (
    DEFAULT_REQUEST_DIR,
    ROOT,
    build_requests,
    env_bool,
)


@dataclass(frozen=True)
class VariationalBrowserRequestConfig:
    request_dir: Path = DEFAULT_REQUEST_DIR
    base_url: str = "https://omni.variational.io"
    confirm_selector: str = "auto"
    dry_run: bool = True
    max_age_sec: int = 300
    approval_timeout_sec: int = 120
    legs: str = "both"
    btc_qty_decimals: int = 6
    eth_qty_decimals: int = 4

    @classmethod
    def from_env(cls) -> "VariationalBrowserRequestConfig":
        return cls(
            request_dir=_project_path(os.getenv("VARIATIONAL_BROWSER_REQUEST_DIR", str(DEFAULT_REQUEST_DIR))),
            base_url=os.getenv(
                "VARIATIONAL_BROWSER_BASE_URL",
                os.getenv("VARIATIONAL_BROWSER_URL", "https://omni.variational.io"),
            ),
            confirm_selector=os.getenv("VARIATIONAL_BROWSER_CONFIRM_SELECTOR", "auto"),
            dry_run=env_bool("VARIATIONAL_BROWSER_DRY_RUN", True),
            max_age_sec=int(os.getenv("VARIATIONAL_REQUEST_MAX_AGE_SEC", "300")),
            approval_timeout_sec=int(os.getenv("VARIATIONAL_BROWSER_APPROVAL_TIMEOUT_SEC", "120")),
            legs=os.getenv("VARIATIONAL_BROWSER_ENGINE_LEGS", "both"),
            btc_qty_decimals=int(os.getenv("VARIATIONAL_BROWSER_BTC_QTY_DECIMALS", "6")),
            eth_qty_decimals=int(os.getenv("VARIATIONAL_BROWSER_ETH_QTY_DECIMALS", "4")),
        )


@dataclass(frozen=True)
class VariationalBrowserRequestBatch:
    action: str
    paths: List[Path]
    requests: List[dict]


class VariationalBrowserRequestBridge:
    """Create request files consumed by the Variational browser daemon."""

    def __init__(
        self,
        config: Optional[VariationalBrowserRequestConfig] = None,
        oracle: Optional[FairPriceOracle] = None,
    ):
        self.config = config or VariationalBrowserRequestConfig.from_env()
        self.oracle = oracle or FairPriceOracle()

    async def create_entry_requests(
        self,
        *,
        direction: PairDirection,
        size_usd: float,
        zscore: float = 0.0,
        divergence_pct: float = 0.0,
    ) -> VariationalBrowserRequestBatch:
        return await self._create_requests(
            action="open",
            direction=direction,
            size_usd=size_usd,
            zscore=zscore,
            divergence_pct=divergence_pct,
        )

    async def create_close_requests(
        self,
        *,
        trade: PairTrade,
        reason: str = "",
    ) -> VariationalBrowserRequestBatch:
        return await self._create_requests(
            action="close",
            direction=trade.direction,
            size_usd=trade.btc_leg.size_usd,
            zscore=trade.zscore_at_entry,
            divergence_pct=trade.spread_at_entry,
            btc_quantity=_format_quantity(trade.btc_leg.quantity, self.config.btc_qty_decimals),
            eth_quantity=_format_quantity(trade.eth_leg.quantity, self.config.eth_qty_decimals),
            summary_suffix=f"close_reason: {reason}" if reason else "",
        )

    async def _create_requests(
        self,
        *,
        action: str,
        direction: PairDirection,
        size_usd: float,
        zscore: float,
        divergence_pct: float,
        btc_quantity: str = "",
        eth_quantity: str = "",
        summary_suffix: str = "",
    ) -> VariationalBrowserRequestBatch:
        fair_prices = await self.oracle.fetch(["BTC", "ETH"])
        missing = [symbol for symbol in ("BTC", "ETH") if symbol not in fair_prices]
        if missing:
            raise RuntimeError(f"Missing fair price for Variational request: {', '.join(missing)}")

        args = Namespace(
            direction=direction.value,
            size_usd=size_usd,
            zscore=zscore,
            divergence_pct=divergence_pct,
            action=action,
            base_url=self.config.base_url,
            legs=self.config.legs,
            confirm_selector=self.config.confirm_selector,
            dry_run=self.config.dry_run,
            request_dir=str(self.config.request_dir),
            steps_json="",
            max_age_sec=self.config.max_age_sec,
            approval_timeout_sec=self.config.approval_timeout_sec,
            quantity="",
            btc_quantity=btc_quantity,
            eth_quantity=eth_quantity,
            reduce_only=None,
            btc_qty_decimals=self.config.btc_qty_decimals,
            eth_qty_decimals=self.config.eth_qty_decimals,
        )
        requests = build_requests(args, fair_prices)
        if summary_suffix:
            for request in requests:
                request["summary"] = f"{request['summary']}\n{summary_suffix}"
                request["signal"]["close_reason"] = summary_suffix.split(": ", 1)[-1]

        paths = self.write_requests(requests)
        return VariationalBrowserRequestBatch(action=action, paths=paths, requests=requests)

    def write_requests(self, requests: List[dict]) -> List[Path]:
        self.config.request_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for request in requests:
            path = self.config.request_dir / f"{request['id']}.json"
            path.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
            paths.append(path)
        return paths


def request_quantity(batch: Optional[VariationalBrowserRequestBatch], symbol: str) -> Optional[float]:
    if batch is None:
        return None
    symbol = symbol.upper()
    for request in batch.requests:
        order = request.get("variationalOrder") or {}
        if str(order.get("symbol", "")).upper() == symbol:
            quantity = order.get("quantity")
            return float(quantity) if quantity else None
    return None


def _format_quantity(quantity: float, decimals: int) -> str:
    if quantity <= 0:
        raise RuntimeError(f"Invalid Variational close quantity: {quantity}")
    fixed = f"{quantity:.{decimals}f}".rstrip("0").rstrip(".")
    if not fixed or float(fixed) <= 0:
        raise RuntimeError(f"Variational close quantity rounds to zero: {quantity}")
    return fixed


def _project_path(value: str) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return ROOT / path
