"""Pluggable async HTTP transport for the Variational connector.

The Omni host sits behind Cloudflare. A plain Python HTTP client is fingerprinted
(TLS/JA3 + HTTP2) and served a "Just a moment..." challenge (HTTP 403). The fix
is to impersonate a real browser's TLS handshake, which ``curl_cffi`` does.

This module exposes interchangeable transports behind a tiny common surface
(``request`` / ``aclose`` / ``headers``, plus an optional async ``astart``):

- **CurlTransport**: ``curl_cffi`` with Chrome impersonation. Passes Cloudflare's
  *fingerprint-based* challenges without running a browser. Fast, but does not
  execute JS, so it can't clear an interactive ("Just a moment...") challenge.
- **PlaywrightTransport** (most robust): runs a persistent headless Chromium and
  issues API calls via ``fetch()`` *inside the page*. The real browser clears
  Cloudflare (JS + cf_clearance cookie) and the fetches inherit that context, so
  requests are indistinguishable from the web client — but with no DOM clicking,
  so it's still fast. The browser logs in once and stays open.
- **HttpxTransport** (fallback / tests): plain ``httpx``. Used when impersonation
  is disabled or ``curl_cffi`` isn't installed. Kept so the test suite can mock
  the backend with ``respx``.

httpx and curl_cffi responses already expose ``status_code`` / ``content`` /
``text`` / ``json()``; PlaywrightTransport returns a shim with the same surface.
"""

from __future__ import annotations

import json as _json
import logging
from typing import Any, Optional
from urllib.parse import urlencode

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


class _ShimResponse:
    """Minimal response with the surface the connector uses."""

    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")

    def json(self) -> Any:
        return _json.loads(self.text)


class PlaywrightTransport:
    """Issue API calls via fetch() inside a persistent headless browser.

    The browser clears Cloudflare on the initial page load (running its JS and
    receiving the cf_clearance cookie); every subsequent same-origin ``fetch``
    inherits that cleared context. This is the most reliable way past an
    interactive challenge, and stays fast because there is no DOM interaction —
    just direct JSON calls, the same ones the web client makes.
    """

    kind = "playwright"

    # JS run in the page: perform a same-origin fetch and return status + body.
    _FETCH_JS = """
    async ({url, method, body, headers}) => {
        const opts = {method, headers, credentials: 'include'};
        if (body !== null) opts.body = body;
        const r = await fetch(url, opts);
        const text = await r.text();
        return {status: r.status, text};
    }
    """

    def __init__(
        self,
        origin: str,
        user_agent: str,
        headless: bool,
        executable_path: str = "",
        user_data_dir: str = "",
        challenge_wait_s: int = 30,
    ):
        self._origin = origin
        self._ua = user_agent
        self._headless = headless
        self._executable_path = executable_path
        self._user_data_dir = user_data_dir
        self._challenge_wait_s = challenge_wait_s
        # Plain dict; the connector sets authorization / vr-connected-address here.
        self.headers: dict[str, str] = {"content-type": "application/json", "accept": "*/*"}
        self._pw = None
        self._browser = None
        self._context = None
        self._page = None

    async def astart(self) -> None:
        try:
            from playwright.async_api import async_playwright
        except ImportError as exc:  # pragma: no cover - env dependent
            raise ImportError(
                "VARIATIONAL_TRANSPORT=browser needs Playwright. Install with:\n"
                "  pip install playwright && python -m playwright install chromium"
            ) from exc

        self._pw = await async_playwright().start()
        launch_kw: dict = {"headless": self._headless}
        if self._executable_path:
            launch_kw["executable_path"] = self._executable_path

        if self._user_data_dir:
            # Persistent profile: keeps cf_clearance + login across restarts.
            self._context = await self._pw.chromium.launch_persistent_context(
                self._user_data_dir, user_agent=self._ua or None, **launch_kw
            )
        else:
            self._browser = await self._pw.chromium.launch(**launch_kw)
            self._context = await self._browser.new_context(user_agent=self._ua or None)
        self._page = self._context.pages[0] if self._context.pages else await self._context.new_page()
        await self._page.goto(self._origin + "/", wait_until="domcontentloaded")

        # Wait out the Cloudflare interstitial if present.
        for _ in range(self._challenge_wait_s):
            content = await self._page.content()
            if "Just a moment" not in content and "cf-challenge" not in content:
                break
            await self._page.wait_for_timeout(1000)
        logger.info("PlaywrightTransport ready (headless=%s)", self._headless)

    async def request(self, method: str, url: str, *, json=None, params=None, **_: Any):
        if params:
            url += ("&" if "?" in url else "?") + urlencode(params)
        payload = {
            "url": url,
            "method": method.upper(),
            "body": _json.dumps(json) if json is not None else None,
            "headers": dict(self.headers),
        }
        result = await self._page.evaluate(self._FETCH_JS, payload)
        return _ShimResponse(result["status"], result["text"])

    async def aclose(self) -> None:
        # Persistent context has no separate browser handle; close the context.
        if self._browser is not None:
            await self._browser.close()
        elif self._context is not None:
            await self._context.close()
        if self._pw is not None:
            await self._pw.stop()


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
    transport: str = "curl",
    impersonate: str = "chrome",
    origin: str,
    user_agent: str = "",
    cf_clearance: str = "",
    headless: bool = True,
    executable_path: str = "",
    user_data_dir: str = "",
    timeout: float = 10.0,
):
    """Return the requested transport.

    ``transport``:
      - "browser"       -> PlaywrightTransport (most robust vs Cloudflare)
      - "httpx"         -> plain httpx (tests / no impersonation)
      - "curl" / "auto" -> curl_cffi impersonation, falling back to httpx
    ``cf_clearance`` (optional) is injected as a cookie for the curl/httpx paths.
    """
    ua = user_agent or DEFAULT_USER_AGENT

    if transport == "browser":
        logger.info("HTTP transport: playwright (browser fetch)")
        return PlaywrightTransport(
            origin=origin, user_agent=ua, headless=headless,
            executable_path=executable_path, user_data_dir=user_data_dir,
        )

    cookies = {"cf_clearance": cf_clearance} if cf_clearance else None
    functional = _functional_headers(origin)

    if transport != "httpx" and impersonate:
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
