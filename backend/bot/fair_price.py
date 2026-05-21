"""
Fair price oracle for Variational execution.

Variational is treated as the execution venue only. Trading decisions and order
checks should use an external fair price built from independent venues so a
single stalled or stale site does not dominate the decision.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import statistics
import time
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional

import aiohttp

logger = logging.getLogger(__name__)

DEFAULT_SYMBOLS = ("BTC", "ETH")
BINANCE_FAPI_URL = "https://fapi.binance.com"
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
LIGHTER_WS_URL = "wss://mainnet.zklighter.elliot.ai/stream"
LIGHTER_MARKET_IDS = {"BTC": 1, "ETH": 0}


@dataclass(frozen=True)
class SourcePrice:
    source: str
    symbol: str
    price: float
    timestamp_ms: int
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def age_ms(self) -> int:
        return max(0, int(time.time() * 1000) - self.timestamp_ms)


@dataclass(frozen=True)
class FairPrice:
    symbol: str
    price: float
    sources: List[SourcePrice]
    ignored: List[SourcePrice] = field(default_factory=list)

    @property
    def source_names(self) -> List[str]:
        return [sample.source for sample in self.sources]

    def deviations_bps(self) -> Dict[str, float]:
        if self.price <= 0:
            return {}
        return {
            sample.source: (sample.price - self.price) / self.price * 10_000
            for sample in self.sources
        }


@dataclass(frozen=True)
class FairPriceConfig:
    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    min_sources: int = 2
    request_timeout_sec: float = 5.0
    source_max_age_ms: int = 15_000
    max_deviation_bps: float = 500.0
    binance_base_url: str = BINANCE_FAPI_URL
    hyperliquid_info_url: str = HYPERLIQUID_INFO_URL
    lighter_ws_url: str = LIGHTER_WS_URL
    lighter_market_ids: Dict[str, int] = field(default_factory=lambda: dict(LIGHTER_MARKET_IDS))
    enabled_sources: tuple[str, ...] = ("binance", "lighter", "hyperliquid")

    @classmethod
    def from_env(cls) -> "FairPriceConfig":
        return cls(
            symbols=tuple(_env_list("FAIR_PRICE_SYMBOLS", list(DEFAULT_SYMBOLS))),
            min_sources=int(os.getenv("FAIR_PRICE_MIN_SOURCES", "2")),
            request_timeout_sec=float(os.getenv("FAIR_PRICE_REQUEST_TIMEOUT_SEC", "5")),
            source_max_age_ms=int(float(os.getenv("FAIR_PRICE_SOURCE_MAX_AGE_SEC", "15")) * 1000),
            max_deviation_bps=float(os.getenv("FAIR_PRICE_MAX_DEVIATION_BPS", "500")),
            binance_base_url=os.getenv("FAIR_PRICE_BINANCE_BASE_URL", BINANCE_FAPI_URL),
            hyperliquid_info_url=os.getenv("FAIR_PRICE_HYPERLIQUID_INFO_URL", HYPERLIQUID_INFO_URL),
            lighter_ws_url=os.getenv("FAIR_PRICE_LIGHTER_WS_URL", LIGHTER_WS_URL),
            lighter_market_ids={
                "BTC": int(os.getenv("LIGHTER_BTC_MARKET_ID", str(LIGHTER_MARKET_IDS["BTC"]))),
                "ETH": int(os.getenv("LIGHTER_ETH_MARKET_ID", str(LIGHTER_MARKET_IDS["ETH"]))),
            },
            enabled_sources=tuple(_env_list("FAIR_PRICE_SOURCES", ["binance", "lighter", "hyperliquid"])),
        )


class FairPriceOracle:
    def __init__(self, config: Optional[FairPriceConfig] = None):
        self.config = config or FairPriceConfig.from_env()

    async def fetch(self, symbols: Optional[Iterable[str]] = None) -> Dict[str, FairPrice]:
        symbols_tuple = tuple((symbols or self.config.symbols))
        source_maps = await self.fetch_sources(symbols_tuple)
        return build_fair_prices(
            source_maps,
            symbols=symbols_tuple,
            min_sources=self.config.min_sources,
            source_max_age_ms=self.config.source_max_age_ms,
            max_deviation_bps=self.config.max_deviation_bps,
        )

    async def fetch_sources(self, symbols: Iterable[str]) -> Dict[str, Dict[str, SourcePrice]]:
        symbols_tuple = tuple(symbols)
        timeout = aiohttp.ClientTimeout(total=self.config.request_timeout_sec)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            tasks = []
            source_names = []

            if "binance" in self.config.enabled_sources:
                source_names.append("binance")
                tasks.append(fetch_binance_prices(session, symbols_tuple, self.config.binance_base_url))
            if "hyperliquid" in self.config.enabled_sources:
                source_names.append("hyperliquid")
                tasks.append(fetch_hyperliquid_prices(session, symbols_tuple, self.config.hyperliquid_info_url))
            if "lighter" in self.config.enabled_sources:
                source_names.append("lighter")
                tasks.append(fetch_lighter_prices(
                    session,
                    symbols_tuple,
                    self.config.lighter_ws_url,
                    self.config.lighter_market_ids,
                    self.config.request_timeout_sec,
                ))

            results = await asyncio.gather(*tasks, return_exceptions=True)

        source_maps: Dict[str, Dict[str, SourcePrice]] = {}
        for name, result in zip(source_names, results):
            if isinstance(result, Exception):
                logger.warning("Fair price source failed: %s: %s", name, result)
                source_maps[name] = {}
            else:
                source_maps[name] = result
        return source_maps


def build_fair_prices(
    source_maps: Dict[str, Dict[str, SourcePrice]],
    *,
    symbols: Iterable[str],
    min_sources: int,
    source_max_age_ms: int,
    max_deviation_bps: float,
) -> Dict[str, FairPrice]:
    fair: Dict[str, FairPrice] = {}
    now_ms = int(time.time() * 1000)
    for symbol in symbols:
        samples = [
            prices[symbol]
            for prices in source_maps.values()
            if symbol in prices and prices[symbol].price > 0
        ]
        fresh = [
            sample
            for sample in samples
            if source_max_age_ms <= 0 or now_ms - sample.timestamp_ms <= source_max_age_ms
        ]
        if len(fresh) < min_sources:
            continue

        first_median = float(statistics.median(sample.price for sample in fresh))
        used = [
            sample
            for sample in fresh
            if first_median > 0
            and abs((sample.price - first_median) / first_median * 10_000) <= max_deviation_bps
        ]
        ignored = [sample for sample in fresh if sample not in used]
        if len(used) < min_sources:
            used = fresh
            ignored = []

        fair[symbol] = FairPrice(
            symbol=symbol,
            price=float(statistics.median(sample.price for sample in used)),
            sources=used,
            ignored=ignored,
        )
    return fair


async def fetch_binance_prices(
    session: aiohttp.ClientSession,
    symbols: Iterable[str],
    base_url: str = BINANCE_FAPI_URL,
) -> Dict[str, SourcePrice]:
    result: Dict[str, SourcePrice] = {}
    for symbol in symbols:
        binance_symbol = f"{symbol.upper()}USDT"
        url = f"{base_url.rstrip('/')}/fapi/v1/ticker/bookTicker"
        async with session.get(url, params={"symbol": binance_symbol}) as resp:
            resp.raise_for_status()
            data = await resp.json()
        bid = _safe_float(data.get("bidPrice"))
        ask = _safe_float(data.get("askPrice"))
        price = _mid_or_single(bid, ask)
        if price is not None:
            result[symbol.upper()] = SourcePrice(
                source="binance",
                symbol=symbol.upper(),
                price=price,
                timestamp_ms=int(time.time() * 1000),
                raw={"bid": bid, "ask": ask, "symbol": binance_symbol},
            )
    return result


async def fetch_hyperliquid_prices(
    session: aiohttp.ClientSession,
    symbols: Iterable[str],
    info_url: str = HYPERLIQUID_INFO_URL,
) -> Dict[str, SourcePrice]:
    async with session.post(info_url, json={"type": "allMids"}) as resp:
        resp.raise_for_status()
        data = await resp.json()

    ts = int(time.time() * 1000)
    result: Dict[str, SourcePrice] = {}
    for symbol in symbols:
        price = _safe_float(data.get(symbol.upper()))
        if price is not None:
            result[symbol.upper()] = SourcePrice(
                source="hyperliquid",
                symbol=symbol.upper(),
                price=price,
                timestamp_ms=ts,
                raw={"symbol": symbol.upper()},
            )
    return result


async def fetch_lighter_prices(
    session: aiohttp.ClientSession,
    symbols: Iterable[str],
    ws_url: str = LIGHTER_WS_URL,
    market_ids: Optional[Dict[str, int]] = None,
    timeout_sec: float = 5.0,
) -> Dict[str, SourcePrice]:
    market_ids = market_ids or LIGHTER_MARKET_IDS
    wanted = {symbol.upper() for symbol in symbols}
    id_to_symbol = {market_ids[symbol]: symbol for symbol in wanted if symbol in market_ids}
    result: Dict[str, SourcePrice] = {}

    async def collect() -> None:
        async with session.ws_connect(ws_url) as ws:
            for market_id in id_to_symbol:
                await ws.send_json({"type": "subscribe", "channel": f"ticker/{market_id}"})
            async for msg in ws:
                if msg.type != aiohttp.WSMsgType.TEXT:
                    continue
                sample = parse_lighter_ticker(json.loads(msg.data), id_to_symbol)
                if sample is not None and sample.symbol in wanted:
                    result[sample.symbol] = sample
                    if set(result) >= wanted:
                        return

    try:
        await asyncio.wait_for(collect(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        logger.warning("Lighter fair price sample timed out; collected=%s", sorted(result))
    return result


def parse_lighter_ticker(data: Dict[str, Any], id_to_symbol: Dict[int, str]) -> Optional[SourcePrice]:
    channel = data.get("channel", "")
    msg_type = data.get("type", "")
    if not (channel.startswith("ticker:") and msg_type in {"update/ticker", "subscribed/ticker"}):
        return None
    try:
        market_id = int(channel.split(":", 1)[1])
    except (IndexError, ValueError):
        return None

    symbol = id_to_symbol.get(market_id)
    if not symbol:
        return None
    payload = data.get("ticker", {})
    ask = payload.get("a") or {}
    bid = payload.get("b") or {}
    ask_price = _safe_float(ask.get("price"))
    bid_price = _safe_float(bid.get("price"))
    price = _mid_or_single(bid_price, ask_price)
    if price is None:
        return None
    return SourcePrice(
        source="lighter",
        symbol=symbol,
        price=price,
        timestamp_ms=int(data.get("timestamp") or time.time() * 1000),
        raw={"market_id": market_id, "bid": bid_price, "ask": ask_price},
    )


def _mid_or_single(bid: Optional[float], ask: Optional[float]) -> Optional[float]:
    if bid is None and ask is None:
        return None
    if bid is None:
        return ask
    if ask is None:
        return bid
    return (bid + ask) / 2.0


def _safe_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _env_list(name: str, default: List[str]) -> List[str]:
    value = os.getenv(name, "")
    if not value:
        return default
    return [part.strip() for part in value.split(",") if part.strip()]
