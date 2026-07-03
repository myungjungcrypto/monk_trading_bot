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

### Why it isn't just "call the REST API"

Variational does **not** publish a trading API, and Omni is an on-chain
(Arbitrum) **RFQ** protocol rather than a CEX. Two things follow:

1. **Auth is a wallet signature, not an API key.** We sign a SIWE
   (Sign-In-With-Ethereum) message with the trading wallet's private key to get a
   session token.
2. **A trade is a signed message.** You request a quote (RFQ); the backend
   returns the exact terms; you sign them with **EIP-712** typed data and submit
   the signature. The OLP settles it.

Because the surface is undocumented, we **do not hard-code guessed endpoints**.
We capture the real flow from the browser and load it from an *endpoint map*.

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
│   └── har_extractor.py          # HAR → endpoint map (the capture bridge)
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

## Status / what still needs a real capture

The connector's structure (auth handshake, RFQ→sign→submit, positions, market
data, dry-run gate) is complete and tested against mocks. The following are
best-effort defaults that must be confirmed against a real HAR capture before
live trading, because the API is undocumented:

- Exact endpoint **paths** and **HTTP methods** (in `_DEFAULT_PATHS`).
- The **SIWE message** format Omni expects (`_build_siwe_message`).
- The **EIP-712 typed data** for a trade — the connector expects the RFQ response
  to carry it (`typed_data` / `eip712`); confirm the field name and whether the
  client must construct it instead.
- The **listing symbols** (`_SYMBOL_MAP`) and response field names for
  positions/mark price.

Run the capture workflow, regenerate the endpoint map, and adjust these in one
place. The tests document the expected shapes.
