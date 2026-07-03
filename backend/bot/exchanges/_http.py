"""Pluggable async HTTP transport for the Variational connector.

The Omni host sits behind Cloudflare. A plain Python HTTP client is fingerprinted
(TLS/JA3 + HTTP2) and served a "Just a moment..." challenge (HTTP 403). The fix
is to impersonate a real browser's TLS handshake, which ``curl_cffi`` does.

This module exposes two interchangeable transports behind a tiny common surface
(``request`` / ``aclose`` / ``headers``):

- **CurlTransport** (preferred): ``curl_cffi`` with Chrome impersonation. Passes
  Cloudflare's fingerprint-based challenges without running a browser.
- **HttpxTransport** (fallback / tests): plain ``httpx``. Used when
  impersonation is disabled or ``curl_cffi`` isn't installed. Kept so the test
  suite can mock the backend with ``respx``.

Both httpx and curl_cffi responses already expose ``status_code`` / ``content`` /
``text`` / ``json()``, so callers use the response directly.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)


def http2_available() -> bool:
    """True if httpx can negotiate HTTP/2 (the optional `h2` package is installed)."""
    try:
        import h2  # noqa: F401
        return True
    except ImportError:
        return False


class HttpxTransport:
    """Plain httpx client. Testable via respx; browser-like headers only get you
    so far past Cloudflare, so this is the fallback, not the default."""

    kind = "httpx"

    def __init__(self, headers: dict[str, str], cookies: Optional[dict], timeout: float):
        import httpx

        self._client = httpx.AsyncClient(
            headers=headers,
            cookies=cookies or None,
            timeout=httpx.Timeout(timeout, connect=5.0),
            http2=http2_available(),
            follow_redirects=True,
        )

    @property
    def headers(self):
        return self._client.headers

    async def request(self, method: str, url: str, **kwargs: Any):
        return await self._client.request(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._client.aclose()


class CurlTransport:
    """curl_cffi AsyncSession impersonating a browser's TLS/HTTP2 fingerprint."""

    kind = "curl_cffi"

    def __init__(
        self,
        impersonate: str,
        headers: dict[str, str],
        cookies: Optional[dict],
        timeout: float,
    ):
        from curl_cffi.requests import AsyncSession

        # Don't override user-agent / sec-ch-ua here — curl_cffi sets them to match
        # the impersonated browser, and a mismatch defeats the whole point.
        self._session = AsyncSession(
            impersonate=impersonate,
            headers=headers,
            cookies=cookies or {},
            timeout=timeout,
        )

    @property
    def headers(self):
        return self._session.headers

    async def request(self, method: str, url: str, **kwargs: Any):
        return await self._session.request(method, url, **kwargs)

    async def aclose(self) -> None:
        await self._session.close()


# Functional headers sent on every request regardless of transport.
def _functional_headers(origin: str) -> dict[str, str]:
    return {
        "content-type": "application/json",
        "accept": "*/*",
        "origin": origin,
        "referer": origin + "/",
    }


# Extra headers that make the httpx fallback look browser-ish. Not applied to the
# curl_cffi transport (it manages these to match its TLS fingerprint).
def _browser_headers(user_agent: str) -> dict[str, str]:
    return {
        "user-agent": user_agent,
        "accept-language": "en-US,en;q=0.9",
        "sec-ch-ua": '"Chromium";v="148", "Google Chrome";v="148", "Not?A_Brand";v="24"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"macOS"',
    }


DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
)


def build_transport(
    *,
    impersonate: str,
    origin: str,
    user_agent: str = "",
    cf_clearance: str = "",
    timeout: float = 10.0,
):
    """Return the best available transport.

    ``impersonate`` non-empty -> try curl_cffi (recommended for the live host).
    Empty, or curl_cffi missing -> httpx with browser-like headers.
    ``cf_clearance`` (optional) is injected as a cookie for the browser-bootstrap
    fallback path (see tools/cf_bootstrap.py).
    """
    ua = user_agent or DEFAULT_USER_AGENT
    cookies = {"cf_clearance": cf_clearance} if cf_clearance else None
    functional = _functional_headers(origin)

    if impersonate:
        try:
            transport = CurlTransport(
                impersonate=impersonate,
                headers=functional,
                cookies=cookies,
                timeout=timeout,
            )
            logger.info("HTTP transport: curl_cffi (impersonate=%s)", impersonate)
            return transport
        except ImportError:
            logger.warning(
                "VARIATIONAL_IMPERSONATE=%s but curl_cffi is not installed; falling "
                "back to httpx, which Cloudflare will likely block. Install it with "
                "`pip install curl_cffi` for the live host.",
                impersonate,
            )

    headers = {**_browser_headers(ua), **functional}
    logger.info("HTTP transport: httpx (no impersonation)")
    return HttpxTransport(headers=headers, cookies=cookies, timeout=timeout)
