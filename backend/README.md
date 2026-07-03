# Monk Pair Bot — Backend

## Direct-API order routing (replacing browser automation)

The bot originally placed orders on **Variational Omni** by driving a headless
Chrome and clicking buttons. That approach is too slow for pair trading: every
click round-trips through the DOM, so the two legs of a pair (BTC and ETH) open
seconds apart. By the time both fills land, the intended market-neutral entry is
already skewed and accuracy suffers.

This backend talks to the **same JSON backend the Omni web client uses**, so both
legs fire concurrently (`asyncio.gather`) with millisecond-level skew instead of
seconds.

### The actual API (verified from a real HAR capture, 2026-07-03)

Variational does **not** publish a trading API, but the Omni web client talks to
a JSON backend at `https://omni.variational.io/api/*`. The full order flow was
captured from a live browser session and the connector implements it exactly:

1. **Auth** — `POST /api/auth/generate_signing_data {address}` returns a
   SIWE message (text/plain, ~60s expiry); `personal_sign` it with the trading
   wallet key; `POST /api/auth/login {address, signed_message}` (signature hex
   **without** `0x`) returns a session JWT. Every request also carries a
   `vr-connected-address` header.
2. **Trade** — `POST /api/quotes/indicative` with the instrument
   (`{underlying, instrument_type: "perpetual_future", settlement_asset: "USDC",
   funding_interval_s: 3600}`) and a **base-asset qty** returns a short-lived
   `quote_id`; `POST /api/orders/new/market {quote_id, side, max_slippage,
   is_reduce_only}` executes it. **No per-order wallet signature** — the session
   token is enough.
3. **State** — `GET /api/positions` (signed qty: >0 long, <0 short),
   `GET /api/portfolio?compute_margin=true` (balance/uPnL),
   `GET /api/funding/v2` (funding rate).

The connector converts USD notional to base qty via a discovery quote's mark
price, submits immediately after quoting (quotes expire in seconds), and
re-authenticates automatically on a 401.

## The capture workflow (bridge from clicking → API)

You were already driving this flow in a browser, so the exact requests are right
there in the network log:

1. Open Omni in Chrome → **DevTools → Network** tab.
2. Enable **Preserve log**; optionally filter to **Fetch/XHR**.
3. Place (or begin to place) one small order so **auth + RFQ + submit** all fire.
4. Right-click the request list → **Save all as HAR with content**.
5. Extract the endpoints:

   ```bash
   cd backend
   python -m tools.har_extractor capture.har -o config/variational_endpoints.json
   ```

The tool prints the discovered flow and writes an endpoint map. **Secrets
(Authorization / Cookie values, tokens) are redacted** — only header *names* and
JSON *shapes* (typed placeholders) are stored, so the file is safe to keep in the
repo if you wish. A committed reference is at
[`config/variational_endpoints.sample.json`](config/variational_endpoints.sample.json).

The connector (`bot/exchanges/variational.py`) loads this map at startup and uses
it for paths and payload shapes, falling back to documented defaults when a
category is missing.

## Setup

```bash
cd backend
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env          # then edit .env
```

Set at minimum in `.env`:

- `VARIATIONAL_PRIVATE_KEY` — a **dedicated trading wallet** private key. Use a
  wallet that holds only what you intend to trade.
- `VARIATIONAL_DRY_RUN=true` — keep this **on** until you have verified payloads
  against a capture. In dry-run the connector signs and logs the request but does
  **not** submit it.

## Usage

```python
import asyncio
from decimal import Decimal
from bot.exchanges.variational import VariationalConnector
from bot.exchanges.base import Order, Side

async def main():
    async with VariationalConnector() as vx:      # connect() authenticates via SIWE
        # Fire both legs of the pair concurrently — this is the whole point.
        btc, eth = await asyncio.gather(
            vx.place_order(Order("BTC", Side.BUY,  Decimal("500"))),
            vx.place_order(Order("ETH", Side.SELL, Decimal("500"))),
        )
        print(btc, eth)

asyncio.run(main())
```

## Layout

```
backend/
├── bot/
│   ├── config.py                 # typed settings from .env (pydantic-settings)
│   └── exchanges/
│       ├── base.py               # BaseExchange abstraction (all venues)
│       └── variational.py        # Variational direct-API connector (SIWE + RFQ + EIP-712)
├── tools/
│   ├── har_extractor.py          # HAR → endpoint map (the capture bridge)
│   ├── cf_bootstrap.py           # fallback: obtain a Cloudflare cf_clearance cookie
│   └── smoke_test.py             # live check: login + balance + quotes + dry-run pair order
├── config/
│   └── variational_endpoints.sample.json
├── tests/                        # pytest suite (respx-mocked, no network/funds)
├── .env.example
├── requirements.txt
└── pytest.ini
```

## Tests

```bash
cd backend && source .venv/bin/activate
python -m pytest -q
```

The suite mocks the HTTP backend with `respx` and signs with a throwaway public
test key, so it touches **no network and no funds**.

## Status

Endpoints, payload shapes, and the auth flow are **verified against a real HAR
capture (2026-07-03)** — see `config/variational_endpoints.sample.json`. Two
things remain unverifiable from a (Chrome-sanitized) HAR and get confirmed on
the first dry-run against the live backend:

- **JWT transport** — Chrome strips `Cookie`/`Authorization` from HAR exports,
  so the connector sends the login token as `Authorization: Bearer` (standard)
  and also keeps any `Set-Cookie` the server returns. A 401 triggers one
  automatic re-login; if it persists, the token travels some other way — capture
  again with a proxy (e.g. mitmproxy) instead of DevTools.
Keep `VARIATIONAL_DRY_RUN=true` for the first run: it performs the full
login + quote flow live and logs the exact order body without submitting it.

## Cloudflare

The Omni host is behind Cloudflare. A plain HTTP client gets a `403 "Just a
moment..."` challenge because its TLS/HTTP2 fingerprint doesn't match a browser.
Pick a transport via `VARIATIONAL_TRANSPORT`:

**`curl` (default, fastest).** curl_cffi impersonates Chrome's TLS fingerprint,
passing *fingerprint-based* challenges **without a browser**. Try this first.

**`browser` (most robust).** If `curl` still 403s — meaning Cloudflare is running
an interactive JS challenge — switch to the Playwright transport. It runs a
persistent headless Chromium, clears Cloudflare on the initial page load (JS +
`cf_clearance`), and then makes every API call via `fetch()` **inside the page**,
so requests are indistinguishable from the web client. Crucially it does **no DOM
clicking** — the original slowness came from clicking, not from the browser — so
it's still fast, and it logs in once and stays open.

```bash
# on the server:
pip install playwright && python -m playwright install chromium
# in .env:
VARIATIONAL_TRANSPORT=browser
VARIATIONAL_BROWSER_USER_DATA_DIR=/home/ec2-user/.monk_variational_profile   # keeps clearance across restarts
# optional: reuse an existing Chrome instead of `playwright install`:
# VARIATIONAL_BROWSER_EXECUTABLE=/usr/bin/chromium-browser
```

If an interactive challenge needs solving by hand once, set
`VARIATIONAL_BROWSER_HEADLESS=false`, solve it in the visible window, and the
`cf_clearance` persists in the profile dir for headless runs afterward.

**`cf_clearance` cookie (curl + a captured cookie).** A middle option: obtain a
`cf_clearance` with `python -m tools.cf_bootstrap` (run **on the server** — the
cookie is IP-bound) and set `VARIATIONAL_CF_CLEARANCE` + `VARIATIONAL_USER_AGENT`.
It expires in ~30 min–hours; the `browser` transport with a persistent profile is
lower-maintenance.

The connector detects a Cloudflare interstitial and raises an actionable error
naming these options, so you always know which layer to reach for.
