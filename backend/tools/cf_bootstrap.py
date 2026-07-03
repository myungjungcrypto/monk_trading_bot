"""Fallback: obtain a Cloudflare cf_clearance cookie with a real browser.

Use this ONLY if curl_cffi impersonation (VARIATIONAL_IMPERSONATE=chrome) still
gets 403'd — i.e. the host is running a JS challenge that requires executing
Cloudflare's script, not just a matching TLS fingerprint.

How it works: launch a headless Chromium ON THIS SERVER, load the Omni page,
wait for Cloudflare to clear, then print the cf_clearance cookie and the exact
User-Agent. Put both in .env:

    VARIATIONAL_CF_CLEARANCE=<value>
    VARIATIONAL_USER_AGENT=<value>

IMPORTANT: cf_clearance is bound to the IP that solved the challenge. Run this on
the SAME server (same public IP) the bot runs on, or the cookie won't work. The
cookie also expires (~30 min–a few hours); re-run when auth starts 403ing again.

Requires Playwright + Chromium on the server:

    pip install playwright
    python -m playwright install chromium

Usage:

    python -m tools.cf_bootstrap
    python -m tools.cf_bootstrap --url https://omni.variational.io/points --headful
"""

from __future__ import annotations

import argparse
import asyncio
import sys


async def bootstrap(url: str, headless: bool, wait_s: float) -> int:
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(
            "Playwright is not installed. Run:\n"
            "  pip install playwright\n"
            "  python -m playwright install chromium",
            file=sys.stderr,
        )
        return 2

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=headless)
        context = await browser.new_context()
        page = await context.new_page()
        print(f"Loading {url} ...", file=sys.stderr)
        await page.goto(url, wait_until="domcontentloaded")

        # Give Cloudflare time to run its challenge and set cf_clearance.
        cf_clearance = None
        for _ in range(int(wait_s)):
            cookies = await context.cookies()
            match = next((c for c in cookies if c["name"] == "cf_clearance"), None)
            if match:
                cf_clearance = match["value"]
                break
            await page.wait_for_timeout(1000)

        user_agent = await page.evaluate("() => navigator.userAgent")
        await browser.close()

    if not cf_clearance:
        print(
            "No cf_clearance cookie appeared. The challenge may need manual "
            "interaction — re-run with --headful and solve it by hand.",
            file=sys.stderr,
        )
        return 1

    print("# Add these to backend/.env:")
    print(f"VARIATIONAL_CF_CLEARANCE={cf_clearance}")
    print(f"VARIATIONAL_USER_AGENT={user_agent}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Obtain a Cloudflare cf_clearance cookie.")
    parser.add_argument("--url", default="https://omni.variational.io/",
                        help="Page to load (default: https://omni.variational.io/).")
    parser.add_argument("--headful", action="store_true",
                        help="Show the browser window (needed to solve interactive challenges).")
    parser.add_argument("--wait", type=float, default=25,
                        help="Seconds to wait for the challenge to clear (default 25).")
    args = parser.parse_args()
    return asyncio.run(bootstrap(args.url, headless=not args.headful, wait_s=args.wait))


if __name__ == "__main__":
    raise SystemExit(main())
