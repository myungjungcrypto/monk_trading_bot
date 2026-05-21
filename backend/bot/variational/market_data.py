"""Read-only Variational Omni market data helpers.

The public Variational endpoint is useful only if we can prove the quotes are
fresh enough for a trading workflow. This module keeps the parsing and
freshness math separate from the CLI probe so it can later be reused by the bot.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from statistics import median
from typing import Any, Dict, Iterable, List, Mapping, Optional

VARIATIONAL_OMNI_BASE_URL = "https://omni-client-api.prod.ap-northeast-1.variational.io"


@dataclass(frozen=True)
class QuoteLevel:
    """Bid/ask quote for one Variational size bucket."""

    bid: Optional[Decimal] = None
    ask: Optional[Decimal] = None


@dataclass(frozen=True)
class VariationalListing:
    """Normalized subset of a Variational listing entry."""

    ticker: str
    mark_price: Optional[Decimal]
    funding_rate: Optional[Decimal]
    base_spread_bps: Optional[Decimal]
    quote_updated_at: Optional[datetime]
    size_1k: QuoteLevel
    size_100k: QuoteLevel
    size_1m: QuoteLevel

    def quote_age_sec(self, now: datetime) -> Optional[float]:
        if self.quote_updated_at is None:
            return None
        return max((now - self.quote_updated_at).total_seconds(), 0.0)

    def mid_price(self, size_bucket: str = "size_1k") -> Optional[Decimal]:
        quote = getattr(self, size_bucket, None)
        if not isinstance(quote, QuoteLevel) or quote.bid is None or quote.ask is None:
            return None
        return (quote.bid + quote.ask) / Decimal("2")


@dataclass(frozen=True)
class FreshnessSample:
    """One paired Variational/Binance observation."""

    received_at: datetime
    latency_ms: float
    btc: Optional[VariationalListing]
    eth: Optional[VariationalListing]
    binance_btc: Optional[Decimal] = None
    binance_eth: Optional[Decimal] = None
    error: str = ""

    @property
    def btc_quote_age_sec(self) -> Optional[float]:
        return self.btc.quote_age_sec(self.received_at) if self.btc else None

    @property
    def eth_quote_age_sec(self) -> Optional[float]:
        return self.eth.quote_age_sec(self.received_at) if self.eth else None

    @property
    def btc_mark_diff_bps(self) -> Optional[Decimal]:
        return _diff_bps(self.btc.mark_price if self.btc else None, self.binance_btc)

    @property
    def eth_mark_diff_bps(self) -> Optional[Decimal]:
        return _diff_bps(self.eth.mark_price if self.eth else None, self.binance_eth)


def parse_stats(payload: Mapping[str, Any]) -> Dict[str, VariationalListing]:
    """Parse `/metadata/stats` response into a ticker-indexed map."""

    listings: Dict[str, VariationalListing] = {}
    for raw in payload.get("listings", []) or []:
        if not isinstance(raw, Mapping):
            continue
        ticker = str(raw.get("ticker", "")).upper().strip()
        if not ticker:
            continue
        listings[ticker] = parse_listing(raw)
    return listings


def parse_listing(raw: Mapping[str, Any]) -> VariationalListing:
    quotes = raw.get("quotes") if isinstance(raw.get("quotes"), Mapping) else {}
    return VariationalListing(
        ticker=str(raw.get("ticker", "")).upper().strip(),
        mark_price=_decimal_or_none(raw.get("mark_price")),
        funding_rate=_decimal_or_none(raw.get("funding_rate")),
        base_spread_bps=_decimal_or_none(raw.get("base_spread_bps")),
        quote_updated_at=parse_variational_timestamp(quotes.get("updated_at")),
        size_1k=_parse_quote_level(quotes.get("size_1k")),
        size_100k=_parse_quote_level(quotes.get("size_100k")),
        size_1m=_parse_quote_level(quotes.get("size_1m")),
    )


def parse_variational_timestamp(value: Any) -> Optional[datetime]:
    """Parse Variational ISO timestamps, including nanosecond fractions."""

    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"

    if "." in text:
        prefix, suffix = text.split(".", 1)
        tz_pos = _first_timezone_pos(suffix)
        if tz_pos is None:
            frac, rest = suffix, ""
        else:
            frac, rest = suffix[:tz_pos], suffix[tz_pos:]
        text = f"{prefix}.{frac[:6]}{rest}"

    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def summarize_samples(samples: Iterable[FreshnessSample]) -> Dict[str, Any]:
    rows = list(samples)
    good = [s for s in rows if not s.error and s.btc and s.eth]
    return {
        "samples": len(rows),
        "ok_samples": len(good),
        "errors": len(rows) - len(good),
        "latency_ms": _numeric_summary([s.latency_ms for s in good]),
        "btc_quote_age_sec": _numeric_summary([s.btc_quote_age_sec for s in good]),
        "eth_quote_age_sec": _numeric_summary([s.eth_quote_age_sec for s in good]),
        "btc_mark_diff_bps": _decimal_summary([s.btc_mark_diff_bps for s in good]),
        "eth_mark_diff_bps": _decimal_summary([s.eth_mark_diff_bps for s in good]),
        "btc_mark_changes": _count_changes([s.btc.mark_price if s.btc else None for s in good]),
        "eth_mark_changes": _count_changes([s.eth.mark_price if s.eth else None for s in good]),
        "btc_quote_ts_changes": _count_changes([s.btc.quote_updated_at if s.btc else None for s in good]),
        "eth_quote_ts_changes": _count_changes([s.eth.quote_updated_at if s.eth else None for s in good]),
    }


def _parse_quote_level(raw: Any) -> QuoteLevel:
    if not isinstance(raw, Mapping):
        return QuoteLevel()
    return QuoteLevel(
        bid=_decimal_or_none(raw.get("bid")),
        ask=_decimal_or_none(raw.get("ask")),
    )


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _diff_bps(left: Optional[Decimal], right: Optional[Decimal]) -> Optional[Decimal]:
    if left is None or right is None or right == 0:
        return None
    return (left - right) / right * Decimal("10000")


def _first_timezone_pos(text: str) -> Optional[int]:
    positions = [pos for pos in (text.find("+"), text.find("-")) if pos >= 0]
    return min(positions) if positions else None


def _numeric_summary(values: Iterable[Optional[float]]) -> Dict[str, Optional[float]]:
    clean = sorted(float(v) for v in values if v is not None)
    if not clean:
        return {"min": None, "p50": None, "p95": None, "max": None}
    return {
        "min": clean[0],
        "p50": median(clean),
        "p95": _percentile(clean, 0.95),
        "max": clean[-1],
    }


def _decimal_summary(values: Iterable[Optional[Decimal]]) -> Dict[str, Optional[float]]:
    return _numeric_summary([float(v) for v in values if v is not None])


def _percentile(values: List[float], pct: float) -> float:
    if len(values) == 1:
        return values[0]
    idx = (len(values) - 1) * pct
    low = int(idx)
    high = min(low + 1, len(values) - 1)
    weight = idx - low
    return values[low] * (1 - weight) + values[high] * weight


def _count_changes(values: Iterable[Any]) -> int:
    changes = 0
    previous = object()
    seen = False
    for value in values:
        if value is None:
            continue
        if not seen:
            previous = value
            seen = True
            continue
        if value != previous:
            changes += 1
            previous = value
    return changes

