# Variational Browser Telegram Gate

Telegram approval gate for Variational web UI clicks.

This tool exists because Variational currently does not require a WalletConnect
signature for every order click once the web session is connected. Therefore,
per-order approval must happen before the browser clicks the final order button.

Default behavior is safe:

- `VARIATIONAL_BROWSER_DRY_RUN=true`: Telegram approval is requested, but the
  final click is skipped.
- Persistent browser profile: login/session state is kept under
  `tools/variational-browser/runtime/profile`.
- Screenshot before every approval.
- Telegram allowlist and kill switch.

## Install

```bash
cd ~/monk_trading_bot/tools/variational-browser
npm install
npx playwright install chromium
cp .env.example .env
nano .env
```

You can keep shared Telegram values in `backend/.env`, but if
`tools/variational-wallet` is also running, use a separate Telegram bot token
for this process. Telegram `getUpdates` is single-consumer; two pollers on one
bot token can steal each other's button clicks.

## First Session Setup

On EC2, use noVNC/X11 or run this locally first:

```bash
npm start -- --open
```

Open/connect Variational once. The persistent profile should keep the session.

If you are using the EC2 WalletConnect signer, let the browser open the
WalletConnect modal and try to extract the `wc:` URI:

```bash
npm start -- --connect-wallet
```

`--connect-wallet` first checks the current page state. If the wallet is already
`ready`, it skips creating a new URI. If the page is `auth_required`, it clicks
the authenticate/login button instead. It only creates a fresh `wc:` URI when
the page is actually `disconnected`. Because Variational can briefly render as
disconnected while the browser profile hydrates, the tool waits
`VARIATIONAL_BROWSER_STATE_SETTLE_SEC` before opening a new WalletConnect modal.

If it finds a URI, it writes it to
`tools/variational-browser/runtime/walletconnect_uri.txt` and sends the next
command to Telegram. Keep this browser command running, open a second SSH
terminal, and pair it with:

```bash
cd ~/monk_trading_bot/tools/variational-wallet
npm start -- --uri 'wc:...'
```

During `--connect-wallet`, the browser process does not poll Telegram buttons,
so it will not steal the wallet signer's `SESSION REQUEST` approval callback if
both processes use the same bot token. It waits up to
`VARIATIONAL_BROWSER_CONNECT_WAIT_SEC` for the dApp to reflect the connected
wallet, then sends a fresh screenshot.

Do not stop either process after approving only the session. Variational can send
an additional WalletConnect `SIGN REQUEST` for `authenticate`/login, and that
signature must complete before the page stays connected. For this setup, keep
`VARIATIONAL_WC_DRY_RUN=false` in `tools/variational-wallet/.env` while leaving
`VARIATIONAL_BROWSER_DRY_RUN=true` for safe click testing.

Some Variational sessions do not send the `authenticate` request automatically.
In that case the browser clicks an authenticate/login button during
`--connect-wallet` using `VARIATIONAL_BROWSER_AUTHENTICATE_SELECTORS`. If the
session is already paired and only auth is missing, run:

```bash
npm start -- --authenticate
```

Keep `tools/variational-wallet` running while doing this, then approve the
WalletConnect `SIGN REQUEST` in Telegram.

To check the current browser session without sending an order-click approval:

```bash
npm start -- --status
```

The screenshot caption reports whether a visible `Connect Wallet` button was
found and whether the page is still asking for `Authenticate`. Stages:

- `disconnected`: `Connect Wallet` is still visible.
- `auth_required`: the wallet address/session is visible, but Variational still
  shows `Authenticate` or the wallet prompt.
- `ready`: neither `Connect Wallet` nor `Authenticate` prompts are visible.

The tool first searches the DOM for `wc:` and then clicks a `Copy link` button
and reads the clipboard. If the URI is not found, send the screenshot; the modal
may need a custom `VARIATIONAL_BROWSER_WC_URI_SELECTOR` or
`VARIATIONAL_BROWSER_WC_COPY_SELECTORS`.

## Manual Click Gate

Use this when the Variational page is already prepared and you only want the
final click guarded by Telegram:

```bash
npm start -- --approve-click --selector 'button:has-text("Submit")'
```

The tool sends a screenshot to Telegram. If approved and
`VARIATIONAL_BROWSER_DRY_RUN=false`, it clicks the selector.

Before any order-click request is sent to Telegram, the browser waits up to
`VARIATIONAL_BROWSER_REQUEST_WALLET_READY_WAIT_SEC` for the page to be `ready`.
If the page is `disconnected` or `auth_required`, the request is blocked and a
status screenshot is sent instead of an approval button.

## JSON Request

Future signal bots can create request files:

```json
{
  "id": "test-order-001",
  "createdAt": "2026-05-22T00:00:00.000Z",
  "url": "https://omni.variational.io/perpetual/BTC",
  "summary": "LONG_BTC_SHORT_ETH / BTC BUY / $50",
  "confirmSelector": "button:has-text(\"Submit\")",
  "dryRun": true,
  "variationalOrder": {
    "symbol": "BTC",
    "side": "BUY",
    "orderType": "market",
    "quantity": "0.000645",
    "sizeUsd": 50,
    "fairPrice": 77533.4,
    "pairDirection": "LONG_BTC_SHORT_ETH"
  },
  "steps": [
    { "type": "wait", "ms": 1000 }
  ],
  "signal": {
    "source": "lighter",
    "zscore": 2.3,
    "divergence_pct": 1.6
  }
}
```

When `variationalOrder` is present, the gate navigates to the symbol page,
selects Market, selects Buy/Sell, fills the Size input with `quantity`, then
sends the approval screenshot. The selectors are configurable through
`VARIATIONAL_BROWSER_MARKET_TAB_SELECTORS`,
`VARIATIONAL_BROWSER_BUY_SELECTORS`, `VARIATIONAL_BROWSER_SELL_SELECTORS`, and
`VARIATIONAL_BROWSER_SIZE_INPUT_SELECTORS`. If the Buy/Sell text nodes are not
discoverable in Variational's rendered DOM, the gate can fall back to viewport
click points through `VARIATIONAL_BROWSER_BUY_FALLBACK_POINT` and
`VARIATIONAL_BROWSER_SELL_FALLBACK_POINT`. If the Size field is not a normal
DOM input, it can fall back to `VARIATIONAL_BROWSER_SIZE_FALLBACK_POINT` and
type the generated quantity there. Keep this in dry-run until the Telegram
screenshot confirms the correct side and size are selected.

Process one request:

```bash
npm start -- --request tools/variational-browser/runtime/requests/test-order-001.json
```

Create a dry-run request with Binance/Lighter/Hyperliquid median fair prices:

```bash
cd ~/monk_trading_bot
source venv/bin/activate
python -m backend.scripts.create_variational_browser_request \
  --direction LONG_BTC_SHORT_ETH \
  --size-usd 50
```

The generator creates one request per leg by default. For
`LONG_BTC_SHORT_ETH`, that means a BTC Buy request and an ETH Sell request. The
request summary and `variationalOrder.quantity` use external median fair prices,
not Variational's screen price. The requests stay dry-run by default. Generated
requests include `maxAgeSec` from `VARIATIONAL_REQUEST_MAX_AGE_SEC` and default
to 300 seconds so both legs can be approved sequentially. To create only one leg
while testing selectors:

```bash
python -m backend.scripts.create_variational_browser_request \
  --direction LONG_BTC_SHORT_ETH \
  --size-usd 50 \
  --legs BTC
```

Watch a directory:

```bash
npm start -- --daemon
```

## Live Clicks

Only after screenshots and selectors are verified:

```env
VARIATIONAL_BROWSER_DRY_RUN=false
```

Keep order size small and use a dedicated Variational wallet.
