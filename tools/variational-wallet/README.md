# Variational WalletConnect Telegram Wallet

Small-account WalletConnect signer for Variational semi-automation.

The process acts as a WalletConnect wallet:

1. Variational shows a WalletConnect URI/QR.
2. This process pairs with that URI.
3. Session proposals and signing requests are sent to Telegram.
4. Only an allowlisted Telegram user can approve or reject.
5. After approval, the local small wallet signs the request.

The default is intentionally safe:

- `VARIATIONAL_WC_DRY_RUN=true`: after Telegram approval, requests are rejected instead of signed.
- `VARIATIONAL_WC_ALLOW_SEND_TRANSACTION=false`: transaction broadcast is disabled.
- `VARIATIONAL_WC_MAX_NATIVE_VALUE_WEI=0`: native token transfers are blocked.

## Install

```bash
cd ~/monk_trading_bot/tools/variational-wallet
npm install
cp .env.example .env
nano .env
```

You can also keep shared Telegram values in `backend/.env`; this process loads both
`backend/.env` and `tools/variational-wallet/.env`.

## Required Values

- `WALLETCONNECT_PROJECT_ID`: create it in the WalletConnect dashboard.
- `VARIATIONAL_WALLET_PRIVATE_KEY` or `VARIATIONAL_WALLET_PRIVATE_KEY_FILE`: small wallet only.
- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`
- `TELEGRAM_ALLOWED_USER_IDS`: numeric Telegram account ids allowed to approve.

## Pair

Copy the WalletConnect URI from the Variational wallet connect modal, then:

```bash
npm start -- --uri 'wc:...'
```

Or prompt for the URI:

```bash
npm start -- --pair
```

## PM2

After a successful manual pairing test:

```bash
pm2 start npm --name variational-wallet --prefix ~/monk_trading_bot/tools/variational-wallet -- start
pm2 save
```

Keep the process running to preserve the WalletConnect session. If Variational
disconnects the session, run with `--pair` again.

## Live Signing

Only after the dry-run request summaries match what Variational shows:

```env
VARIATIONAL_WC_DRY_RUN=false
```

Leave `VARIATIONAL_WC_ALLOW_SEND_TRANSACTION=false` unless Variational actually
uses `eth_sendTransaction` and you have tested it with a tiny balance.

