# Variational Browser Telegram Gate

Telegram approval gate for Variational web UI clicks.

This tool exists because Variational currently does not require a WalletConnect
signature for every order click once the web session is connected. Therefore,
per-order approval must happen before the browser clicks the final order button.

Default behavior is safe:

- `VARIATIONAL_BROWSER_DRY_RUN=true`: Telegram approval is requested, but the
  final click is skipped.
- `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=false`: new pair entries still require
  Telegram approval by default. Set it true only for fully automated small-size
  live mode, and set `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD` to the
  largest per-leg size the daemon may auto-click.
- `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true`: close requests that are
  explicitly `action=close` and `reduceOnly=true` are auto-clicked in live mode
  because they reduce exposure.
- `VARIATIONAL_BROWSER_BATCH_REQUESTS=true`: backend-created BTC/ETH legs are
  written as one batch request file, so one approval covers both entry legs and
  one automatic reduce-only flow closes both legs.
- Persistent browser profile: login/session state is kept under
  `tools/variational-browser/runtime/profile`.
- Screenshot before every approval or automatic reduce-only click.
- Screenshot before every automatic open/close click.
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

Values in `tools/variational-browser/.env` override the shared `backend/.env`.
Use the tool-local file for live-click controls such as
`VARIATIONAL_BROWSER_DRY_RUN=false`.
The request generator also loads both files in the same order, so generated
JSON requests inherit the tool-local dry-run setting unless you pass
`--dry-run` or `--no-dry-run` explicitly.

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
`tools/variational-browser/runtime/walletconnect_uri.txt`. A PM2-running
`tools/variational-wallet` process watches that file by default and pairs with
fresh `wc:` URIs automatically. If the wallet daemon is not running, pair it
manually with:

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
  "confirmSelector": "auto",
  "dryRun": true,
  "variationalOrder": {
    "symbol": "BTC",
    "side": "BUY",
    "orderType": "market",
    "quantity": "0.000645",
    "sizeUsd": 50,
    "fairPrice": 77533.4,
    "pairDirection": "LONG_BTC_SHORT_ETH",
    "action": "open",
    "reduceOnly": false
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

Backend-generated pair trades use a batch request by default:

```json
{
  "id": "variational-20260523T010203-abcd1234",
  "createdAt": "2026-05-23T01:02:03.000Z",
  "summary": "Variational browser batch request: SHORT_BTC_LONG_ETH",
  "dryRun": false,
  "variationalBatch": [
    { "id": "...-btc", "variationalOrder": { "symbol": "BTC", "side": "SELL", "action": "open", "reduceOnly": false } },
    { "id": "...-eth", "variationalOrder": { "symbol": "ETH", "side": "BUY", "action": "open", "reduceOnly": false } }
  ]
}
```

For manual open batches, the browser sends leg preview screenshots and then one
Telegram `Click Pair` approval. After approval it sets up and clicks BTC/ETH
sequentially. If the first entry leg clicks and a later leg fails, the browser
attempts an immediate reduce-only rollback of the already-clicked entry leg and
archives the batch as `rolledback` rather than `clicked`.
If `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true`, the approval wait is skipped for
these backend-created pair batches when the request is under
`VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD`. In this automated path,
the browser skips the separate approval preview pass and goes directly into the
per-leg prepared-click flow. Telegram screenshots and status messages are
best-effort audit logs; a temporary Telegram API timeout should not stop the
daemon from processing the batch.

For close batches, every leg must be `action=close` and `reduceOnly=true`.
Those batches are auto-clicked when `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY`
is enabled. If one reduce-only close leg fails, the daemon keeps processing and
retries failed legs up to `VARIATIONAL_BROWSER_REDUCE_ONLY_BATCH_RETRY_ATTEMPTS`
times before reporting a partial failure.

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
type the generated quantity there. The fallback refuses to press `Ctrl+A` unless
the click focused an editable input, so it cannot accidentally select the whole
page and continue as if size was filled. Keep this in dry-run until the Telegram
screenshot confirms the correct side and size are selected.

For close requests, `variationalOrder.reduceOnly=true` causes the gate to enable
the Reduce Only checkbox before filling size. Live reduce-only clicks require
`VARIATIONAL_BROWSER_REQUIRE_REDUCE_ONLY_CHECKED=true` by default, so the gate
verifies the checkbox is actually checked after setup and again immediately
before the final order click. If the checkbox state cannot be verified, is
disabled, or is unchecked, the click is refused. The Reduce Only fallback is
disabled by default because a stale viewport point can hit another checkbox on
Variational's order panel.

In live mode, close requests are auto-clicked without waiting for Telegram when
all of the following are true:

- `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true`
- `variationalOrder.action` is `close`
- `variationalOrder.reduceOnly` is `true`
- the Reduce Only checkbox is verified checked on the page

The browser still sends pre-click and post-click screenshots for audit. If
`VARIATIONAL_BROWSER_DRY_RUN=true`, the auto reduce-only path reports dry-run
and skips the click. These audit sends are also best-effort in automatic close
mode, while manual approval mode still requires Telegram to be reachable.

With `confirmSelector: "auto"`, the approval caption lists enabled final-button
candidates from the order panel area. Live mode refuses broad selectors such as
`body`, `html`, or `*`; either keep `auto` or provide a precise button selector.

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

Create a reduce-only close request by inverting the original pair direction:

```bash
python -m backend.scripts.create_variational_browser_request \
  --direction SHORT_BTC_LONG_ETH \
  --action close \
  --legs ETH \
  --eth-quantity 0.0235 \
  --approval-timeout-sec 180
```

If Telegram approval times out, discard that request and generate a fresh one.
The approval timeout is stored in each request as `approvalTimeoutMs`, so
`--approval-timeout-sec` overrides the browser `.env` for that order.

Watch a directory:

```bash
npm start -- --daemon
```

Backend auto mode:

```env
EXECUTION_MODE=variational_browser
PRIMARY_EXCHANGE=lighter
VARIATIONAL_BROWSER_ENGINE_LEGS=both
```

With that mode, BotEngine keeps virtual PnL/DB state under the
`variational_browser` exchange name and writes open/close request files
automatically. Keep the browser daemon running so those files are converted into
Telegram approval screenshots. Close requests are reduce-only and use the
tracked virtual leg quantities. If `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY`
is enabled, those close requests are clicked automatically after setup because
they only reduce exposure. With `VARIATIONAL_BROWSER_BATCH_REQUESTS=true`, the
two BTC/ETH legs are written as a single batch file, preventing one leg from
expiring before the other file is processed. Failed reduce-only close legs are
retried before the daemon reports a partial failure.

The daemon claims a request by renaming it to `*.json.processing` before it
touches the Variational UI, then archives the final result as `*.done`. Expired
or malformed requests are archived as `*.expired.done` / `*.failed.done` so an
old file cannot block the watcher forever. The backend will not rename an
already-claimed `.processing` request to `aborted`; it waits up to
`VARIATIONAL_BROWSER_PROCESSING_TIMEOUT_SEC` for the daemon to write a final
`.clicked.done` / failure marker. This avoids the dangerous race where the
browser is already clicking but the backend gives up and records no DB position.

## Live Clicks

Only after screenshots and selectors are verified:

```env
VARIATIONAL_BROWSER_DRY_RUN=false
VARIATIONAL_BROWSER_CONFIRM_SELECTOR=auto
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=false
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD=100
```

The approval message should show a sane `confirm_button_candidates` entry before
you approve a live click. If no candidate is found, the live click is blocked
unless `VARIATIONAL_BROWSER_CONFIRM_FALLBACK_ENABLED=true` is explicitly set.
Keep order size small and use a dedicated Variational wallet.
For `variationalOrder` requests, the auto confirm candidate must have visible
button text that matches the intended side and symbol, such as `Buy BTC` or
`Sell ETH`; blank button candidates are refused in live mode.

For fully automated entry after live screenshots are verified:

```env
VARIATIONAL_BROWSER_DRY_RUN=false
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD=500
VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true
VARIATIONAL_BROWSER_BATCH_REQUESTS=true
```

Auto-open only applies to backend-created BTC/ETH batch requests. The daemon
skips the manual approval preview pass and attempts to send pre-click and
post-click screenshots to Telegram for audit. In fully automated mode those
audit messages are best-effort and bounded by
`VARIATIONAL_BROWSER_TELEGRAM_BEST_EFFORT_TIMEOUT_SEC`, so a slow Telegram
upload should be logged without killing the request watcher or blocking the
click path for long. If the first entry leg clicks and a later leg fails, it
attempts reduce-only rollback for the already-clicked entry leg and does not
archive the batch as `clicked`.
