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
The connector handles this in layers:

1. **curl_cffi impersonation (default).** With `VARIATIONAL_IMPERSONATE=chrome`
   (and `pip install curl_cffi`), requests use Chrome's TLS fingerprint, which
   passes fingerprint-based challenges **without running a browser**. This is the
   primary transport; httpx is only the fallback (and what the tests mock).

2. **cf_clearance cookie (fallback).** If the host runs a full JS challenge that
   impersonation can't pass, solve it once with a real browser **on the server**
   and reuse the cookie:

   ```bash
   pip install playwright && python -m playwright install chromium
   python -m tools.cf_bootstrap            # prints cf_clearance + user-agent
   # paste both into .env: VARIATIONAL_CF_CLEARANCE=... / VARIATIONAL_USER_AGENT=...
   ```

   `cf_clearance` is bound to the solving IP, so run `cf_bootstrap` on the same
   server the bot runs on. It expires (~30 min–hours); re-run when auth 403s.

The connector raises a clear, actionable error (naming curl_cffi and
`cf_bootstrap`) whenever it detects a Cloudflare interstitial, so you always know
which layer to reach for.
