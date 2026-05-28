"""Variational browser-gate request writer.

The bot does not click Variational directly. It writes request JSON files for
tools/variational-browser, which prepares the UI and asks Telegram for approval.
"""

from __future__ import annotations

import json
import os
import asyncio
import time
from argparse import Namespace
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

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
    completion_timeout_sec: int = 360
    processing_timeout_sec: int = 900
    completion_poll_sec: float = 1.0
    legs: str = "both"
    btc_qty_decimals: int = 6
    eth_qty_decimals: int = 4
    batch_requests: bool = True

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
            completion_timeout_sec=int(os.getenv("VARIATIONAL_BROWSER_COMPLETION_TIMEOUT_SEC", "360")),
            processing_timeout_sec=int(os.getenv("VARIATIONAL_BROWSER_PROCESSING_TIMEOUT_SEC", "900")),
            completion_poll_sec=float(os.getenv("VARIATIONAL_BROWSER_COMPLETION_POLL_SEC", "1")),
            legs=os.getenv("VARIATIONAL_BROWSER_ENGINE_LEGS", "both"),
            btc_qty_decimals=int(os.getenv("VARIATIONAL_BROWSER_BTC_QTY_DECIMALS", "6")),
            eth_qty_decimals=int(os.getenv("VARIATIONAL_BROWSER_ETH_QTY_DECIMALS", "4")),
            batch_requests=env_bool("VARIATIONAL_BROWSER_BATCH_REQUESTS", True),
        )


@dataclass(frozen=True)
class VariationalBrowserRequestBatch:
    action: str
    paths: List[Path]
    requests: List[dict]


@dataclass(frozen=True)
class VariationalBrowserRequestCompletion:
    path: Path
    status: str
    done_path: Optional[Path] = None


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
        if self.config.batch_requests and len(requests) > 1:
            batch = _build_batch_request(requests)
            path = self.config.request_dir / f"{batch['id']}.json"
            path.write_text(json.dumps(batch, indent=2, ensure_ascii=False), encoding="utf-8")
            return [path]

        paths = []
        for request in requests:
            path = self.config.request_dir / f"{request['id']}.json"
            path.write_text(json.dumps(request, indent=2, ensure_ascii=False), encoding="utf-8")
            paths.append(path)
        return paths

    async def wait_for_batch_completion(
        self,
        batch: VariationalBrowserRequestBatch,
        *,
        timeout_sec: Optional[int] = None,
    ) -> List[VariationalBrowserRequestCompletion]:
        """Wait until the browser daemon archives every request file."""
        if timeout_sec is None:
            request_timeout = sum(
                float(request.get("approvalTimeoutMs", self.config.approval_timeout_sec * 1000)) / 1000
                for request in batch.requests
            ) + 60
            timeout = max(self.config.completion_timeout_sec, int(request_timeout))
        else:
            timeout = timeout_sec
        deadline = time.monotonic() + max(timeout, 0)
        pending = set(batch.paths)
        completions: List[VariationalBrowserRequestCompletion] = []

        while pending and time.monotonic() <= deadline:
            for path in list(pending):
                completion = request_completion(path)
                if completion is None:
                    continue
                completions.append(completion)
                pending.remove(path)
            if pending:
                await asyncio.sleep(max(self.config.completion_poll_sec, 0.1))

        processing_deadline = time.monotonic() + max(self.config.processing_timeout_sec, 0)
        while pending and any(request_processing(path) for path in pending) and time.monotonic() <= processing_deadline:
            for path in list(pending):
                completion = request_completion(path)
                if completion is None:
                    continue
                completions.append(completion)
                pending.remove(path)
            if pending and any(request_processing(path) for path in pending):
                await asyncio.sleep(max(self.config.completion_poll_sec, 0.1))

        for path in sorted(pending):
            completion = request_completion(path)
            if completion is not None:
                completions.append(completion)
                continue
            aborted = abort_pending_request(path)
            completions.append(aborted)
        return completions

    def open_request_statuses_for_trade(
        self,
        *,
        direction: PairDirection,
        opened_at: object,
        window_sec: int = 600,
    ) -> Dict[str, str]:
        """Find archived/pending open request statuses near a DB trade timestamp."""
        opened_ts = _to_timestamp(opened_at)
        if opened_ts <= 0 or not self.config.request_dir.exists():
            return {}

        statuses: Dict[str, str] = {}
        for path in self.config.request_dir.glob("*.json*"):
            if path.is_dir():
                continue
            try:
                request = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue

            for leg_request in _iter_leg_requests(request):
                order = leg_request.get("variationalOrder") or {}
                if str(order.get("action", "open")).lower() != "open":
                    continue
                pair_direction = order.get("pairDirection") or leg_request.get("signal", {}).get("direction")
                if str(pair_direction) != direction.value:
                    continue

                created_ts = _to_timestamp(leg_request.get("createdAt") or request.get("createdAt"))
                if created_ts <= 0 or abs(opened_ts - created_ts) > window_sec:
                    continue

                symbol = str(order.get("symbol", "")).upper()
                if symbol not in {"BTC", "ETH"}:
                    continue
                statuses[symbol] = request_file_status(path)

        return statuses


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


def _build_batch_request(requests: List[dict]) -> dict:
    first = requests[0]
    orders = [request.get("variationalOrder") or {} for request in requests]
    action = str(orders[0].get("action", "open")).lower()
    direction = orders[0].get("pairDirection") or first.get("signal", {}).get("direction")
    summary_lines = [
        f"Variational browser batch request: {direction}",
        f"action: {action}",
        "legs:",
    ]
    for order in orders:
        summary_lines.append(
            "  - {symbol} {side} qty={qty} reduce_only={reduce}".format(
                symbol=order.get("symbol"),
                side=order.get("side"),
                qty=order.get("quantity"),
                reduce=order.get("reduceOnly"),
            )
        )
    fair = first.get("signal", {}).get("fair_price", {})
    if fair:
        summary_lines.append("decision_price: external median fair price, not Variational screen price")

    return {
        "id": str(first["id"]).rsplit("-", 1)[0],
        "createdAt": first.get("createdAt"),
        "summary": "\n".join(summary_lines),
        "confirmSelector": first.get("confirmSelector", "auto"),
        "dryRun": first.get("dryRun", True),
        "maxAgeSec": first.get("maxAgeSec", 300),
        "approvalTimeoutMs": max(int(request.get("approvalTimeoutMs", 120_000)) for request in requests),
        "variationalBatch": requests,
        "signal": {
            "direction": direction,
            "action": action,
            "legs": [
                {
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "quantity": order.get("quantity"),
                    "reduce_only": order.get("reduceOnly"),
                }
                for order in orders
            ],
            "size_usd_per_leg": first.get("signal", {}).get("size_usd_per_leg"),
            "zscore": first.get("signal", {}).get("zscore"),
            "divergence_pct": first.get("signal", {}).get("divergence_pct"),
            "fair_price": fair,
        },
    }


def _iter_leg_requests(request: dict) -> List[dict]:
    batch = request.get("variationalBatch")
    if isinstance(batch, list):
        return [item for item in batch if isinstance(item, dict)]
    return [request]


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


def request_completion(path: Path) -> Optional[VariationalBrowserRequestCompletion]:
    if path.exists():
        return None
    done_files = sorted(path.parent.glob(f"{path.name}.*.done"))
    if not done_files:
        return None
    done_path = done_files[-1]
    return VariationalBrowserRequestCompletion(
        path=path,
        status=request_file_status(done_path),
        done_path=done_path,
    )


def request_processing(path: Path) -> bool:
    return Path(f"{path}.processing").exists()


def abort_pending_request(path: Path) -> VariationalBrowserRequestCompletion:
    done_path = Path(f"{path}.aborted.done")
    if request_processing(path):
        return VariationalBrowserRequestCompletion(
            path=path,
            status="processing_timeout",
            done_path=Path(f"{path}.processing"),
        )
    try:
        path.rename(done_path)
        return VariationalBrowserRequestCompletion(path=path, status="aborted", done_path=done_path)
    except FileNotFoundError:
        completion = request_completion(path)
        if completion is not None:
            return completion
    except OSError:
        pass
    return VariationalBrowserRequestCompletion(path=path, status="pending_timeout")


def request_file_status(path: Path) -> str:
    name = path.name
    if name.endswith(".done"):
        marker = name.removesuffix(".done").rsplit(".", 1)[-1]
        return marker or "unknown"
    if name.endswith(".json"):
        return "pending"
    return "unknown"


def completions_all_clicked(completions: List[VariationalBrowserRequestCompletion]) -> bool:
    return bool(completions) and all(completion.status == "clicked" for completion in completions)


def completions_close_resolved(completions: List[VariationalBrowserRequestCompletion]) -> bool:
    """Close batches are resolved by a click or by discovering the venue is already flat."""
    resolved_statuses = {"clicked", "external_closed"}
    return bool(completions) and all(completion.status in resolved_statuses for completion in completions)


def completions_external_closed(completions: List[VariationalBrowserRequestCompletion]) -> bool:
    return bool(completions) and any(completion.status == "external_closed" for completion in completions)


def completions_browser_unavailable(completions: List[VariationalBrowserRequestCompletion]) -> bool:
    return bool(completions) and any(completion.status == "browser_unavailable" for completion in completions)


def format_completions(completions: List[VariationalBrowserRequestCompletion]) -> str:
    return "\n".join(
        f"{completion.path.name}: {completion.status}"
        for completion in completions
    )


def _to_timestamp(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return _to_timestamp(parsed)
        except ValueError:
            return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0
