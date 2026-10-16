"""Telegram messages are cut to the Bot API limit instead of being rejected.

On 2026-10-16 every "session DOWN" alert failed with "message is too long"
(two Playwright call logs in one message), so the outage went unnoticed.
"""

import asyncio

from backend.bot.telegram_notifier import (
    TELEGRAM_MAX_MESSAGE_LEN,
    TelegramNotifier,
    fit_telegram_message,
)


def _utf16_len(text):
    return len(text.encode("utf-16-le")) // 2


def test_short_message_is_unchanged():
    text = "[Monk] ERROR\nVariational API session DOWN"
    assert fit_telegram_message(text) is text


def test_message_at_the_limit_is_unchanged():
    text = "x" * TELEGRAM_MAX_MESSAGE_LEN
    assert fit_telegram_message(text) == text


def test_long_message_is_cut_to_the_limit_keeping_the_head():
    text = "[Monk] ERROR\nVariational API session DOWN\n" + "Call log line\n" * 2000
    fitted = fit_telegram_message(text)
    assert _utf16_len(fitted) <= TELEGRAM_MAX_MESSAGE_LEN
    assert fitted.startswith("[Monk] ERROR\nVariational API session DOWN")
    assert fitted.endswith("(truncated)")


def test_characters_outside_the_bmp_count_double():
    text = "\U0001F6A8" * 3000          # 3000 emoji = 6000 UTF-16 code units
    fitted = fit_telegram_message(text)
    assert _utf16_len(fitted) <= TELEGRAM_MAX_MESSAGE_LEN
    assert fitted.endswith("(truncated)")


class _Resp:
    status = 200

    async def text(self):
        return '{"ok":true}'


class _Post:
    async def __aenter__(self):
        return _Resp()

    async def __aexit__(self, *exc):
        return False


class FakeSession:
    closed = False

    def __init__(self):
        self.payloads = []

    def post(self, url, json):
        self.payloads.append(json)
        return _Post()


def _notifier():
    notifier = TelegramNotifier(bot_token="test-token", chat_id="1")
    session = FakeSession()

    async def get_session():
        return session

    notifier._get_session = get_session
    return notifier, session


def test_error_with_a_huge_detail_is_still_sent():
    notifier, session = _notifier()
    call_log = "Call log:\n" + "  - [pid=1][err] chrome output\n" * 400

    asyncio.run(notifier.error(
        "Variational API session DOWN",
        f"probe error: {call_log}\nreconnect failed: {call_log}",
    ))

    [payload] = session.payloads
    assert _utf16_len(payload["text"]) <= TELEGRAM_MAX_MESSAGE_LEN
    assert payload["text"].startswith("[Monk] ERROR\nVariational API session DOWN")


def test_cut_drops_parse_mode_so_a_split_entity_cannot_fail_the_send():
    notifier, session = _notifier()
    asyncio.run(notifier.send("<b>title</b>\n" + "y" * 5000, parse_mode="HTML"))
    asyncio.run(notifier.send("<b>short</b>", parse_mode="HTML"))

    cut, short = session.payloads
    assert "parse_mode" not in cut
    assert short["parse_mode"] == "HTML"
