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
found.

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
