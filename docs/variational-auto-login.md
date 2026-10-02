# Variational 자동 로그인/재연결 구조 설명

> 대상: 이 구조를 자기 프로젝트에 옮기거나 수정하려는 사람 (+ 같이 작업할 AI)
> 기준 코드: `myungjungcrypto/monk_trading_bot` 브랜치 `claude/init-project-setup-r3kAN`
> 비밀값(개인키, 토큰, 지갑 주소)은 이 문서에 넣지 않았다. 실제 값은 각자 `.env`에 넣는다.

---

## 1. 한 줄 요약

Variational Omni는 공식 trading API가 없어서 **웹 세션(브라우저)을 계속 로그인된 상태로 유지**해야 한다.
WalletConnect 세션과 Variational 로그인은 주기적으로 끊긴다(체감상 약 1주). 그래서 **"끊기지 않게 연장"하는 게 아니라
"끊기면 사람 손 없이 다시 연결+로그인"** 하도록 만들었다.

끊김 감지 → Connect Wallet 클릭 → WalletConnect URI 추출 → 파일로 전달 → 지갑 데몬이 자동 pairing →
세션 자동 승인 → 로그인 서명(SIWE) 자동 승인 → ready 확인 → 원래 하던 주문 계속.

---

## 2. 구성 요소 (프로세스 3개)

| 프로세스 | 위치 | 역할 |
|---|---|---|
| `variational-chrome` | `tools/variational-browser/src/start_chrome_cdp.mjs` | 실제 Chrome을 `--remote-debugging-port=9222` + 고정 profile로 상시 실행 (PM2, 필요 시 Xvfb). Cloudflare clearance/쿠키가 이 profile에 남는다. |
| `variational-browser` | `tools/variational-browser/src/variational_browser_gate.mjs` | Playwright로 위 Chrome에 CDP attach. 주문 요청 파일 처리 + **지갑 상태 판정 + 자동 재연결/인증 버튼 클릭**. |
| `variational-wallet` | `tools/variational-wallet/src/variational_wallet_bot.mjs` | EC2 소액 전용 EVM 지갑. WalletConnect(Reown WalletKit) 지갑 역할. **URI 파일 감시 → 자동 pairing, Variational 세션/로그인 서명만 자동 승인**, 나머지는 Telegram 승인. |

세 프로세스 모두 PM2로 상시 실행하는 것이 전제다. 특히 `variational-wallet`이 꺼져 있으면 자동 복구가 불가능하다.

---

## 3. 전체 흐름

```
[variational-browser]                          [variational-wallet]
  주문 요청 처리 시작
  └ ensureWalletReadyForRequest()
     ├ assessWalletState() → stage 판정
     ├ ready ............................ 바로 주문 진행
     ├ auth_required → Authenticate 클릭 ─────────┐
     └ disconnected  → connectWallet()             │
          ├ "Connect Wallet" 클릭                  │
          ├ "WalletConnect" 클릭                   │
          ├ Copy link / DOM / clipboard에서 wc: URI│
          └ runtime/walletconnect_uri.txt 저장 ──▶ 2초마다 파일 폴링 (startPairingUriFileWatcher)
                                                   └ 새 URI면 walletKit.pair()
                                                session_proposal
                                                   └ peer가 omni.variational.io면 자동 승인
          waitForWalletReady()                     │
          └ auth_required 보이면 Authenticate 클릭 ┘
                                                session_request (personal_sign, SIWE 로그인)
                                                   └ isAutoApprovedVariationalLogin() 통과 시 자동 서명
          ready 확인
     ├ validateRequest(request)  ← 복구가 오래 걸렸으면 만료된 요청은 클릭 안 함
     └ 요청 페이지 다시 열고 주문 진행
```

---

## 4. 핵심 코드 위치

### 4-1. 브라우저 쪽 (`tools/variational-browser/src/variational_browser_gate.mjs`)

| 함수 | 대략 위치 | 설명 |
|---|---|---|
| `start()` | ~280 | `VARIATIONAL_BROWSER_CDP_ENDPOINT`가 있으면 `chromium.connectOverCDP()`로 기존 Chrome에 붙고, 없으면 `launchPersistentContext(profileDir)`. CDP 모드에서는 종료 시 Chrome을 닫지 않는다. |
| `assessWalletState()` | ~2506 | 화면에서 신호를 모아 stage를 결정. **우선순위가 중요** (아래 4-3). |
| `ensureWalletReadyForRequest()` | ~552 | 모든 주문 경로(단일/배치/force flatten/주문 세팅 후)에서 호출. 복구 가능한 stage(`disconnected`, `auth_required`)면 자동 복구 후 요청을 재검증. |
| `waitForRequestWalletReady()` | ~2471 | 최대 `REQUEST_WALLET_READY_WAIT_SEC` 동안 ready를 기다리며 `auth_required`면 Authenticate 1회 클릭. |
| `connectWallet()` | ~463 | Connect Wallet → WalletConnect → URI 추출 → `runtime/walletconnect_uri.txt` 저장(0600) → `waitForWalletReady()`. 이미 ready면 새 URI를 만들지 않고, `auth_required`면 URI 대신 인증만 진행한다(불필요한 세션 재생성 방지). |
| `extractWalletConnectUri()` | ~2760 | `a[href^="wc:"]`, DOM 텍스트, "Copy link" 버튼 클릭 후 clipboard 읽기 순으로 시도. QR만 보이고 DOM에 URI가 없던 문제를 Copy link 경로로 해결했다. |
| `clickAuthenticateControl()` | ~2446 | Authenticate/Sign In/Log In selector → wallet prompt selector → (옵션) 좌표 fallback 클릭. |
| `authenticateCurrentPage()` | ~608 | `--authenticate` CLI 및 복구 경로에서 사용. |
| `resetWalletSession()` | ~674 | localStorage/sessionStorage/IndexedDB/cache와 오래된 URI 파일만 지운다. **쿠키는 유지**(Cloudflare clearance 보존). |

### 4-2. 지갑 쪽 (`tools/variational-wallet/src/variational_wallet_bot.mjs`)

| 함수 | 대략 위치 | 설명 |
|---|---|---|
| `startPairingUriFileWatcher()` | ~305 | `VARIATIONAL_WC_PAIRING_URI_FILE`을 주기적으로 읽고, 처음 보는 `wc:` URI면 `walletKit.pair()`. 이미 본 URI는 `seenPairingUris`로 중복 pairing 방지. |
| `handleSessionProposal()` | ~366 | `isAutoApprovedVariationalSession()` 통과 시 자동 승인, 아니면 Telegram 승인 버튼. |
| `isAutoApprovedVariationalSession()` | ~601 | 옵션 ON + peer URL이 `https://` + 허용 host(`omni.variational.io`) + 이름/URL에 "variational" 포함. |
| `resolveRequest()` | ~456 | 서명 요청 처리. `isAutoApprovedVariationalLogin()` 통과 시만 자동, 나머지는 전부 Telegram 승인. |
| `isAutoApprovedVariationalLogin()` | ~608 | **자동 서명의 안전장치 본체.** 아래 조건을 전부 만족해야 함. |

`isAutoApprovedVariationalLogin()` 조건:
1. `VARIATIONAL_WC_AUTO_APPROVE_VARIATIONAL_LOGIN=true`
2. method가 `personal_sign`만 (typed-data, transaction 등은 절대 자동 아님)
3. chain이 허용 목록(기본 Arbitrum One `eip155:42161`)
4. signer가 이 지갑 주소
5. 메시지가 SIWE 로그인 형식(`wants you to sign in with your Ethereum account`)이고 지갑 주소 포함
6. `URI:` 라인이 정확히 `https://omni.variational.io/api/auth/login`
7. `Chain ID:` 라인이 chain과 일치
8. `Expiration Time:`이 존재하고, 미래이며, 지금부터 `AUTO_APPROVE_LOGIN_MAX_EXPIRY_SEC`(기본 180초) 이내

### 4-3. 지갑 상태 판정 우선순위 (`assessWalletState`)

```
human_challenge 보임         → human_verification_required   (자동 복구 안 함, 사람이 Cloudflare 통과)
"wallet was lost" 류 보임    → reconnect_required            (자동 복구 안 함, reset 후 재연결)
Authenticate 보임            → auth_required                 (ready 신호보다 우선!)
ready 신호 중 하나라도       → ready
Connect Wallet 보임          → disconnected
wallet prompt만 보임         → auth_required
```

여기서 겪은 함정들:
- WalletConnect 세션 승인 직후, 지갑 주소와 주문 패널이 보여도 **Authenticate 버튼이 남아 있는 상태**가 있다. 이걸 ready로 보면 주문 단계에서 다시 막힌다 → Authenticate를 ready보다 먼저 판정.
- 반대로 연결된 화면에도 숨은 "Connect Wallet" 텍스트 노드가 남아 있어 오탐이 난다 → ready 신호를 Connect Wallet보다 먼저 판정.
- ready 신호는 3종류를 OR로 본다: selector(`Transfer` 버튼, `Portfolio $...`, `0x...` 주소), 전체 frame visible text(주소 + Transfer/Available to Trade/Portfolio), 주문 패널 텍스트(`Enter Size`, `Buy/Sell BTC/ETH`). DOM이 쪼개져서 selector 하나로는 자주 실패하기 때문.

---

## 5. 설정값

### `tools/variational-wallet/.env` (완전 자동 복구용)

```env
WALLETCONNECT_PROJECT_ID=...            # dashboard.walletconnect.com
VARIATIONAL_WALLET_PRIVATE_KEY_FILE=... # 소액 전용 지갑. 파일 경로 방식 권장
TELEGRAM_BOT_TOKEN=...
TELEGRAM_CHAT_ID=...
TELEGRAM_ALLOWED_USER_IDS=...           # 승인 버튼 누를 수 있는 Telegram user id

VARIATIONAL_WC_CHAINS=eip155:42161
VARIATIONAL_WC_DRY_RUN=false            # true면 승인해도 서명 거부 → 로그인 불가

VARIATIONAL_WC_AUTO_APPROVE_VARIATIONAL_SESSION=true
VARIATIONAL_WC_AUTO_APPROVE_VARIATIONAL_LOGIN=true
VARIATIONAL_WC_AUTO_APPROVE_VARIATIONAL_HOSTS=omni.variational.io
VARIATIONAL_WC_AUTO_APPROVE_LOGIN_MAX_EXPIRY_SEC=180

VARIATIONAL_WC_ALLOW_SEND_TRANSACTION=false
VARIATIONAL_WC_STORAGE_PREFIX=monk-variational-wallet

VARIATIONAL_WC_PAIRING_URI_FILE_ENABLED=true
VARIATIONAL_WC_PAIRING_URI_FILE=tools/variational-browser/runtime/walletconnect_uri.txt
VARIATIONAL_WC_PAIRING_URI_POLL_SEC=2
```

### `tools/variational-browser/.env` (관련 부분만)

```env
VARIATIONAL_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222
VARIATIONAL_BROWSER_PROFILE_DIR=tools/variational-browser/runtime/profile

VARIATIONAL_BROWSER_REQUEST_AUTO_AUTHENTICATE=true
VARIATIONAL_BROWSER_REQUEST_AUTO_RECONNECT_WALLET=true
VARIATIONAL_BROWSER_REQUEST_WALLET_READY_WAIT_SEC=15
VARIATIONAL_BROWSER_CONNECT_WAIT_SEC=300
VARIATIONAL_BROWSER_AUTHENTICATE_WAIT_SEC=15
VARIATIONAL_BROWSER_AUTHENTICATE_FALLBACK_ENABLED=true
VARIATIONAL_BROWSER_AUTHENTICATE_FALLBACK_POINT=0.377,0.795

# browser와 wallet이 같은 Telegram bot token을 쓰면 browser polling을 끈다.
# (getUpdates를 서로 뺏어가서 wallet 승인 callback이 안 온다)
VARIATIONAL_BROWSER_POLL_TELEGRAM=false
```

---

## 6. 운영 명령

```bash
# 상시 실행
pm2 start ecosystem.config.json --only variational-chrome --update-env
cd tools/variational-wallet && pm2 start npm --name variational-wallet -- start && pm2 save
cd ../variational-browser && pm2 start npm --name variational-browser -- start -- --daemon

# 수동 점검/복구 (반드시 daemon을 먼저 멈춘다 — 같은 Chrome 탭을 동시에 조작하면 꼬임)
pm2 stop variational-browser
cd tools/variational-browser
npm start -- --status                 # 현재 stage + 스크린샷을 Telegram으로
npm start -- --authenticate           # 세션은 있는데 로그인만 필요할 때
npm start -- --reset-wallet-session   # wallet lost / 설정 로딩 실패 시 (쿠키 유지)
npm start -- --connect-wallet         # 처음부터 재연결
pm2 restart variational-browser --update-env
```

---

## 7. 자동 복구가 안 되는 경우와 대처

| stage / 증상 | 원인 | 대처 |
|---|---|---|
| `human_verification_required` | Cloudflare "Verify you are human" | 자동 우회 안 함. noVNC 등 화면에서 사람이 직접 통과. headless Playwright Chromium에서 계속 실패하면 system Chrome + CDP attach 구성 사용. |
| `reconnect_required` | "Connection to your wallet was lost", "Unable to load configuration data" | SIGN REQUEST까지 못 감. `--reset-wallet-session` → `--connect-wallet`. |
| URI는 저장됐는데 ready 안 됨 | `variational-wallet`이 꺼져 있거나 `DRY_RUN=true`, 또는 Telegram polling 충돌 | wallet PM2 상태, `.env`, bot token 분리 여부 확인. |
| 주문 요청이 `wallet_unavailable`로 archive | 복구 실패 | 백엔드(`backend/bot/engine.py`)가 `VARIATIONAL_BROWSER_INFRA_RETRY_COOLDOWN_SEC`(기본 900초) 동안 entry/close 재시도를 멈춘다. 포지션 DB는 건드리지 않음. |

---

## 8. 알아둘 한계 / 개선 아이디어

- **복구는 "주문 요청이 왔을 때" 일어난다 (lazy).** 주기적으로 상태를 확인하는 keepalive 루프는 없다. 그래서 세션이 끊긴 뒤 첫 주문 시그널은 복구 시간만큼 늦어지고, 복구가 `maxAgeSec`을 넘기면 그 진입은 만료 처리되어 놓친다. 청산 요청은 재시도로 이어진다.
  - 개선안: daemon idle 상태에서 N분마다 `assessWalletState()`를 돌려 `disconnected/auth_required`면 미리 복구하는 heartbeat 추가.
- WalletConnect 세션 자체를 연장(`extendSession`)하는 코드는 없다. 끊기면 새로 pairing하는 방식이다.
- selector는 Variational UI 변경에 취약하다. 전부 env로 덮어쓸 수 있게 되어 있으니 UI가 바뀌면 `.env`의 `*_SELECTORS`부터 확인.
- 자동 승인 범위는 "Variational 세션 제안 + 짧은 만료의 Variational 로그인 personal_sign"으로 엄격히 제한되어 있다. 이 조건을 넓히면 피싱 서명에 그대로 노출되므로, 수정할 때 이 부분은 특히 주의.
- 지갑은 반드시 소액 전용으로. 개인키는 `.env`/파일에만 두고 git에 올리지 말 것.

---

## 9. 다른 프로젝트로 옮길 때 체크리스트

1. Chrome을 고정 profile + CDP 포트로 상시 실행 (쿠키/Cloudflare clearance 유지)
2. Playwright는 `connectOverCDP`로 attach
3. 상태 판정 함수(`assessWalletState`)를 우선순위 그대로 이식
4. Connect Wallet → WalletConnect → URI 추출 → **파일로 전달** (프로세스 간 결합을 파일 하나로 단순화)
5. 지갑 데몬: URI 파일 폴링 + 자동 pairing
6. 자동 승인 필터 2개(session / SIWE login) 이식, 나머지 method는 수동 승인 유지
7. 주문 직전 `ensureWalletReady` 호출 + 복구 후 요청 만료 재검증
8. Telegram bot token을 browser/wallet 간 분리하거나 browser polling off
