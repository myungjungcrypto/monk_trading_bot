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

If it finds a URI, it writes it to
`tools/variational-browser/runtime/walletconnect_uri.txt` and sends the next
command to Telegram. Pair it with:

```bash
cd ~/monk_trading_bot/tools/variational-wallet
npm start -- --uri 'wc:...'
```

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

## JSON Request

Future signal bots can create request files:

```json
{
  "id": "test-order-001",
  "createdAt": "2026-05-22T00:00:00.000Z",
  "url": "https://omni.variational.io",
  "summary": "LONG BTC / SHORT ETH, $50 per leg",
  "confirmSelector": "button:has-text(\"Submit\")",
  "dryRun": true,
  "steps": [
    { "type": "fill", "selector": "input[name=\"size\"]", "value": "50" },
    { "type": "wait", "ms": 1000 }
  ],
  "signal": {
    "source": "lighter",
    "zscore": 2.3,
    "divergence_pct": 1.6
  }
}
```

Process one request:

```bash
npm start -- --request tools/variational-browser/runtime/requests/test-order-001.json
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
