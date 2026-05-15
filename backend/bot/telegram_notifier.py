"""
Telegram notification client for bot events.

The notifier is best-effort by design: Telegram failures are logged, but they
never block signal evaluation, paper tracking, live orders, or risk handling.
"""

import logging
import os
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import aiohttp

if TYPE_CHECKING:
    from backend.bot.position_manager import PairTrade
    from backend.bot.signal import Signal

logger = logging.getLogger(__name__)


@dataclass
class TelegramConfig:
    token: str = ""
    chat_id: str = ""
    enabled: bool = True
    timeout_sec: float = 8.0

    @classmethod
    def from_env(cls) -> "TelegramConfig":
        return cls(
            token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            chat_id=os.getenv("TELEGRAM_CHAT_ID", "").strip(),
            enabled=os.getenv("TELEGRAM_ENABLED", "true").lower() == "true",
            timeout_sec=float(os.getenv("TELEGRAM_TIMEOUT_SEC", "8")),
        )


class TelegramNotifier:
    """Send trading and bot lifecycle notifications through Telegram Bot API."""

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(
        self,
        bot_token: Optional[str] = None,
        chat_id: Optional[str] = None,
        config: Optional[TelegramConfig] = None,
    ):
        if config is not None:
            self.config = config
        elif bot_token is not None or chat_id is not None:
            self.config = TelegramConfig(token=bot_token or "", chat_id=chat_id or "")
        else:
            self.config = TelegramConfig.from_env()
        self._session: Optional[aiohttp.ClientSession] = None

    @classmethod
    def from_env(cls) -> Optional["TelegramNotifier"]:
        config = TelegramConfig.from_env()
        if not config.enabled or not config.token or not config.chat_id:
            logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
            return None
        return cls(config=config)

    @property
    def enabled(self) -> bool:
        return bool(self.config.enabled and self.config.token and self.config.chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            timeout = aiohttp.ClientTimeout(total=self.config.timeout_sec)
            self._session = aiohttp.ClientSession(timeout=timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, text: str, parse_mode: Optional[str] = None) -> bool:
        if not self.enabled:
            return False

        payload = {
            "chat_id": self.config.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            session = await self._get_session()
            url = self.BASE_URL.format(token=self.config.token)
            async with session.post(url, json=payload) as resp:
                if resp.status < 300:
                    return True
                body = await resp.text()
                logger.warning("Telegram send failed: %s %s", resp.status, body[:300])
        except Exception as exc:
            logger.warning("Telegram send error: %s", exc)
        return False

    async def status(self, text: str) -> None:
        await self.send(f"[Monk] STATUS\n{text}")

    async def error(self, title: str, detail: str = "") -> None:
        text = f"[Monk] ERROR\n{title}"
        if detail:
            text += f"\n{detail}"
        await self.send(text)

    async def notify_entry(
        self,
        direction: str,
        exchange: str,
        size_usd: float,
        leverage: int,
        zscore: float,
        divergence: float,
        probability: float,
        trend: str,
    ) -> None:
        await self.send(
            "\n".join([
                "[Monk] ENTRY",
                f"direction: {direction}",
                f"exchange: {exchange}",
                f"size: ${size_usd:.2f} x {leverage}",
                f"zscore: {zscore:.3f}",
                f"divergence: {divergence:.4f}%",
                f"probability: {probability:.2f}%",
                f"trend: {trend}",
            ])
        )

    async def notify_exit(
        self,
        trade_id: str,
        reason: str,
        pnl_usd: float,
        pnl_pct: float,
        direction: str = "",
    ) -> None:
        sign = "+" if pnl_usd >= 0 else ""
        lines = [
            "[Monk] EXIT",
            f"trade_id: {trade_id}",
            f"reason: {reason}",
            f"PNL: {sign}${pnl_usd:.2f} ({sign}{pnl_pct:.3f}%)",
        ]
        if direction:
            lines.insert(2, f"direction: {direction}")
        await self.send("\n".join(lines))

    async def entry_signal(
        self,
        signal: "Signal",
        direction: str,
        execution_mode: str,
        size_usd: float,
        btc_price: float,
        eth_price: float,
    ) -> None:
        await self.send(
            "\n".join([
                "[Monk] ENTRY SIGNAL",
                f"mode: {execution_mode}",
                f"direction: {direction}",
                f"size: ${size_usd:.2f} per leg",
                f"BTC: {btc_price:.2f}",
                f"ETH: {eth_price:.4f}",
                f"zscore_5m: {signal.zscore_5m:.3f}",
                f"divergence: {signal.divergence_pct:.4f}%",
                f"trend: {signal.trend.value}",
            ])
        )

    async def trade_opened(self, trade: "PairTrade", execution_mode: str) -> None:
        await self.send(
            "\n".join([
                "[Monk] OPENED",
                f"mode: {execution_mode}",
                f"trade_id: {trade.trade_id}",
                f"exchange: {trade.exchange_name}",
                f"direction: {trade.direction.value}",
                f"BTC entry: {trade.btc_leg.entry_price:.2f}",
                f"ETH entry: {trade.eth_leg.entry_price:.4f}",
                f"zscore_entry: {trade.zscore_at_entry:.3f}",
                f"spread_entry: {trade.spread_at_entry:.4f}%",
            ])
        )

    async def trade_closed(self, trade: "PairTrade", reason: str, message: str = "") -> None:
        closed_at = trade.closed_at or time.time()
        hold_minutes = max((closed_at - trade.opened_at) / 60.0, 0.0)
        lines = [
            "[Monk] CLOSED",
            f"trade_id: {trade.trade_id}",
            f"exchange: {trade.exchange_name}",
            f"reason: {reason}",
            f"direction: {trade.direction.value}",
            f"PNL: ${trade.net_pnl_usd:.2f} ({trade.pnl_pct:.3f}%)",
            f"fees: ${trade.total_fees_usd:.4f}",
            f"hold: {hold_minutes:.1f}m",
            f"BTC: {trade.btc_leg.entry_price:.2f} -> {trade.btc_leg.current_price:.2f}",
            f"ETH: {trade.eth_leg.entry_price:.4f} -> {trade.eth_leg.current_price:.4f}",
        ]
        if message:
            lines.append(f"note: {message}")
        await self.send("\n".join(lines))

    async def notify_averaging(self, trade_id: str, message: str) -> None:
        await self.send("\n".join(["[Monk] AVERAGING DOWN", f"trade_id: {trade_id}", message]))

    async def notify_size_reduction(self, trade_id: str, message: str) -> None:
        await self.send("\n".join(["[Monk] SIZE REDUCTION", f"trade_id: {trade_id}", message]))

    async def notify_bot_started(self, mode: str, exchanges: list, size: float, leverage: int) -> None:
        await self.send(
            "\n".join([
                "[Monk] BOT STARTED",
                f"mode: {mode}",
                f"exchanges: {', '.join(exchanges)}",
                f"size: ${size:.2f} x {leverage}",
            ])
        )

    async def notify_bot_stopped(self) -> None:
        await self.send("[Monk] BOT STOPPED")
