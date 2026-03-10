"""
Telegram 알림 모듈 — 거래 진입/청산/리스크 이벤트를 텔레그램으로 전송.

사용법:
  1. @BotFather에서 봇 생성 → 토큰 획득
  2. 봇에게 /start 메시지 전송 후 chat_id 확인
  3. .env에 TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID 설정
"""

import asyncio
import logging
import os
from typing import Optional

import aiohttp

logger = logging.getLogger(__name__)


class TelegramNotifier:
    """텔레그램 Bot API를 통해 알림을 전송합니다."""

    BASE_URL = "https://api.telegram.org/bot{token}/sendMessage"

    def __init__(self, bot_token: str, chat_id: str):
        self.bot_token = bot_token
        self.chat_id = chat_id
        self._url = self.BASE_URL.format(token=bot_token)
        self._session: Optional[aiohttp.ClientSession] = None

    @classmethod
    def from_env(cls) -> Optional["TelegramNotifier"]:
        """환경변수에서 설정을 읽어 인스턴스를 생성합니다. 미설정 시 None."""
        token = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "").strip()
        if not token or not chat_id:
            logger.warning("Telegram not configured (TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID missing)")
            return None
        return cls(token, chat_id)

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def send(self, text: str, parse_mode: str = "HTML") -> bool:
        """메시지를 전송합니다. 실패 시 False 반환 (봇 중단 없음)."""
        try:
            session = await self._get_session()
            payload = {
                "chat_id": self.chat_id,
                "text": text,
                "parse_mode": parse_mode,
            }
            async with session.post(self._url, json=payload, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                if resp.status == 200:
                    return True
                body = await resp.text()
                logger.warning("Telegram send failed (%d): %s", resp.status, body)
                return False
        except Exception as e:
            logger.warning("Telegram send error: %s", e)
            return False

    # ── 거래 이벤트 알림 ─────────────────────────────────

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
        msg = (
            f"🟢 <b>ENTRY</b>\n"
            f"Direction: <code>{direction}</code>\n"
            f"Exchange: {exchange}\n"
            f"Size: ${size_usd:.0f} × {leverage}x\n"
            f"Z-score: {zscore:.2f} | Div: {divergence:.2f}%\n"
            f"Prob: {probability:.1f}% | Trend: {trend}"
        )
        await self.send(msg)

    async def notify_exit(
        self,
        trade_id: str,
        reason: str,
        pnl_usd: float,
        pnl_pct: float,
        direction: str = "",
    ) -> None:
        emoji = "🔴" if pnl_usd < 0 else "🟢"
        sign = "+" if pnl_usd >= 0 else ""
        msg = (
            f"{emoji} <b>EXIT — {reason}</b>\n"
            f"Trade: <code>{trade_id[:8]}</code>\n"
        )
        if direction:
            msg += f"Direction: <code>{direction}</code>\n"
        msg += (
            f"PNL: <b>{sign}${pnl_usd:.2f}</b> ({sign}{pnl_pct:.2f}%)"
        )
        await self.send(msg)

    async def notify_averaging(self, trade_id: str, message: str) -> None:
        msg = (
            f"⚠️ <b>AVERAGING DOWN</b>\n"
            f"Trade: <code>{trade_id[:8]}</code>\n"
            f"{message}"
        )
        await self.send(msg)

    async def notify_size_reduction(self, trade_id: str, message: str) -> None:
        msg = (
            f"⚠️ <b>SIZE REDUCTION</b>\n"
            f"Trade: <code>{trade_id[:8]}</code>\n"
            f"{message}"
        )
        await self.send(msg)

    async def notify_bot_started(self, mode: str, exchanges: list, size: float, leverage: int) -> None:
        msg = (
            f"🤖 <b>BOT STARTED</b>\n"
            f"Mode: {mode}\n"
            f"Exchanges: {', '.join(exchanges)}\n"
            f"Size: ${size:.0f} × {leverage}x"
        )
        await self.send(msg)

    async def notify_bot_stopped(self) -> None:
        await self.send("🛑 <b>BOT STOPPED</b>")
