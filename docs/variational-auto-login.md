# Variational 자동 로그인 구조 설명 (API 모드)

> 대상: 이 구조를 자기 프로젝트에 옮기거나 수정하려는 사람 (+ 같이 작업할 AI)
> 기준 코드: `myungjungcrypto/monk_trading_bot` 브랜치 **`claude/variational-api-integration`**
> 실행 모드: `EXECUTION_MODE=variational_api`
> 비밀값(개인키, 토큰, 지갑 주소)은 이 문서에 넣지 않았다. 실제 값은 각자 `backend/.env`에 넣는다.

---

## 1. 한 줄 요약

Variational Omni는 공식 trading API가 없다. 예전에는 Chrome 화면을 클릭하는 방식(Browser 모드)이라
**WalletConnect 세션이 만료될 때마다(기본 7일) 다시 연결·승인**해야 했다.

API 모드는 WalletConnect를 아예 쓰지 않는다. **봇이 자기 지갑 키로 직접 로그인 서명(SIWE)을 해서 JWT를 받고,
만료되면 알아서 다시 로그인**한다. 그래서 1주일마다 수동으로 갱신할 필요가 없다.

```
봇 시작
 └ VariationalConnector.connect()
    ├ headless Chromium 띄우고 omni.variational.io 열기 (Cloudflare 통과)
    ├ POST /api/auth/generate_signing_data  {address}       → SIWE 메시지(텍스트, 60초 만료)
    ├ 그 메시지를 VARIATIONAL_PRIVATE_KEY로 personal_sign
    ├ POST /api/auth/login {address, signed_message}         → {"token": JWT}
    └ 이후 모든 요청 헤더: authorization: Bearer <JWT>, vr-connected-address: <address>

평소
 ├ 2분마다 GET /api/positions 로 세션 점검 (keepalive)
 ├ 어떤 요청이든 401 → 자동 재로그인 후 한 번 재시도
 ├ 점검 실패 → 브라우저+로그인 통째로 재구성 → 실패하면 텔레그램 "session DOWN"
 └ 6시간마다 브라우저 세션 새로 만들기 (메모리 누수 방지, 주문 중엔 안 함)
```

---

## 2. 왜 이 방식이 "주기적 갱신"이 필요 없나

| | Browser 모드 (구) | API 모드 (현재) |
|---|---|---|
| 로그인 주체 | 브라우저 + 외부 WalletConnect 지갑 프로세스 | 봇 자신 (키를 직접 보유) |
| 만료 원인 | WalletConnect 세션(기본 7일), Variational 로그인 | Variational JWT만 |
| 만료 시 | 재연결 → 세션 승인 → 로그인 서명 승인 (자동화했지만 단계가 많아 자주 깨짐) | 401 받으면 서명 1번으로 즉시 재로그인 |
| 주문당 서명 | 없음 | 없음 (RFQ quote → market order) |
| 필요한 프로세스 | variational-chrome, variational-browser, variational-wallet | monk-api 하나 |

핵심: 로그인 메시지는 서버가 매번 새로 만들어 주고(nonce + 60초 만료), 봇은 그걸 받자마자 서명해서 보낸다.
사람이 지갑 팝업에서 하던 "Sign in" 서명을 코드가 대신하는 것뿐이라, 키만 있으면 언제든 다시 로그인할 수 있다.

---

## 3. 핵심 코드 위치

### 3-1. 로그인 / HTTP — `backend/bot/variational/api/api_client.py`

| 함수 | 줄 | 설명 |
|---|---|---|
| `connect()` | ~121 | transport 생성(브라우저 transport면 `astart()`로 Chromium 기동) → 주소 계산 → `vr-connected-address` 헤더 → `authenticate()` |
| `_personal_sign()` | ~190 | `eth_account`로 EIP-191 personal_sign. 서버가 **0x 없는 130자 hex**를 기대해서 접두사를 뗀다. |
| `authenticate()` | ~203 | signing data 요청 → 메시지에 `sign in with your Ethereum account` 있는지 검증 → 서명 → login → JWT를 `authorization: Bearer`로 설정 |
| `_request()` | ~398 | 모든 API 호출의 공통 경로. Cloudflare 챌린지(403/503 + "Just a moment") 감지, **401이면 `authenticate()` 후 1회 재시도** |

### 3-2. Cloudflare 통과 — `backend/bot/variational/api/api_http.py`

`VARIATIONAL_TRANSPORT=browser`일 때 `PlaywrightTransport`(~119)를 쓴다.

- 영구 프로필(`VARIATIONAL_BROWSER_USER_DATA_DIR`)로 Chromium을 띄워 `cf_clearance` 쿠키가 재시작 후에도 남는다.
- API 호출은 DOM 클릭 없이 **페이지 안에서 `fetch()`** 로 보낸다(`request()` ~224). 그래서 쿠키·TLS 지문이 진짜 브라우저와 같다.
- `navigator.webdriver` 숨김(~195), CDP로 User-Agent의 `HeadlessChrome` 제거(~210), 첫 진입 시 Cloudflare 대기 최대 30초.
- 대안 transport: `curl`(curl_cffi로 TLS 지문 위장, JS 챌린지는 못 넘음), `httpx`(테스트용).

### 3-3. 세션 유지 — `backend/bot/variational/api_executor.py`

| 함수 | 줄 | 설명 |
|---|---|---|
| `start_healthcheck()` | ~120 | 봇 시작 시 엔진이 호출. `VARIATIONAL_API_HEALTHCHECK_SEC`(기본 120초) 주기 |
| `_health_loop()` | ~137 | **시작 즉시 1회 점검(pre-warm)** → 첫 주문이 로그인 대기(~5초)를 안 하게. 이후 주기 반복. 루프는 예외로 죽지 않음 |
| `_run_health_check()` | ~159 | 주문 실행 중(`_in_flight > 0`)이면 건너뜀. `get_position("BTC")` 성공하면 OK, 이전에 DOWN이었으면 "recovered" 알림 |
| `_handle_unhealthy()` | ~211 | 점검 실패 → `_rebuild_connector()`(브라우저 닫고 새로 띄워 재로그인) → 성공 알림 / 실패 시 `Variational API session DOWN` 알림. 이후에도 주기마다 계속 재시도 |
| `_recycle_if_stale()` | ~189 | 세션 나이가 `VARIATIONAL_API_RECYCLE_HOURS`(기본 6) 넘으면 유휴 시에만 재구성. 며칠 열어 둔 Chromium 탭이 수 GB까지 커져 서버를 다운시킨 적이 있어서 추가 |

### 3-4. 봇 자체가 꺼졌을 때 — `backend/app/bot_runtime.py`, `backend/app/main.py`

위 keepalive는 **봇이 돌고 있는 동안에만** 동작한다. 봇 엔진이 멈추면 재로그인 시도도 같이 멈춘다. 그래서 2026-10-02에 아래를 추가했다.

| 위치 | 설명 |
|---|---|
| `save_bot_runtime_state()` (bot_runtime.py ~67) | 대시보드 Start/Stop을 DB `bot_config.bot_runtime`에 기록 (`desired_running`, 시작 요청) |
| `BotSupervisor.run()` (bot_runtime.py ~155) | 엔진이 사용자 의도와 다르게 멈추면 DB 설정으로 새 엔진을 만들어 재시작. 30초→60초→…최대 10분 backoff, 1시간 정상 동작하면 backoff 초기화, 텔레그램 알림 |
| `_auto_resume_bot()` (main.py ~374) | 백엔드 재시작 시: 마지막에 켜져 있었으면 포지션 없어도 재개, 열린 거래가 있으면 재개(기존 동작) |
| Stop / Kill Switch | 의도를 먼저 `false`로 바꾼 뒤 멈추므로 자동 재시작과 충돌하지 않음 |

---

## 4. 설정값 (`backend/.env`)

```env
EXECUTION_MODE=variational_api

VARIATIONAL_PRIVATE_KEY=0x...            # 소액 전용 지갑. 절대 커밋 금지
VARIATIONAL_API_BASE=https://omni.variational.io
VARIATIONAL_DRY_RUN=true                 # true면 로그인/quote까지만 하고 주문은 안 보냄
VARIATIONAL_MAX_SLIPPAGE=0.0005

VARIATIONAL_TRANSPORT=browser
VARIATIONAL_BROWSER_EXECUTABLE=/home/ec2-user/.cache/ms-playwright/chromium-1223/chrome-linux64/chrome
VARIATIONAL_BROWSER_USER_DATA_DIR=/home/ec2-user/.monk_variational_profile   # 구 Browser 모드 프로필과 공유 금지

VARIATIONAL_API_HEALTHCHECK_SEC=120
VARIATIONAL_API_RECYCLE_HOURS=6

# 봇 자동 재시작 / 재개
BOT_AUTO_RESTART=true
BOT_AUTO_RESTART_BASE_DELAY_SEC=30
BOT_AUTO_RESTART_MAX_DELAY_SEC=600
BOT_AUTO_RESTART_STABLE_RESET_SEC=3600
AUTO_RESUME_LAST_RUNNING=true
AUTO_RESUME_OPEN_TRADES=true
```

---

## 5. 운영 / 점검

```bash
# 실거래 전 리허설 (기본 dry-run: 로그인 + quote까지만)
python -m backend.scripts.variational_api_smoke
# 아주 작은 실주문으로 열고 바로 반대 주문으로 닫기
VARIATIONAL_DRY_RUN=false python -m backend.scripts.variational_api_smoke --size 5 --live

# 포지션 확인 / 남은 테스트 포지션 정리(reduce-only)
python -m backend.scripts.variational_api_positions
python -m backend.scripts.variational_api_positions --flatten

# 정상 로그
pm2 logs monk-api --lines 200 --nostream | grep -E "Variational session established|pre-warmed|keepalive|auto-reconnected|session DOWN|Recycling"
```

---

## 6. 장애 유형과 대처

| 증상 | 의미 | 대처 |
|---|---|---|
| `401 ... re-authenticating` 로그 | JWT 만료 → 자동 재로그인 | 정상 동작, 조치 불필요 |
| 텔레그램 `session auto-reconnected` | 점검 실패 후 자동 복구 성공 | 조치 불필요 |
| 텔레그램 `session DOWN` + `Target crashed` | 내부 Chromium 탭이 죽고 재구성도 실패 | 봇이 돌고 있으면 2분마다 자동 재시도. 계속되면 `free -m`, `dmesg`로 메모리 확인 후 `pm2 restart monk-api` |
| `blocked by Cloudflare challenge` | Cloudflare가 JS 챌린지를 요구 | `VARIATIONAL_TRANSPORT=browser` 확인. 계속 막히면 `VARIATIONAL_BROWSER_HEADLESS=false`로 사람이 한 번 통과(프로필에 쿠키 저장) |
| `Unexpected signing-data response` | Variational 프론트 API가 바뀜 | `backend/config/variational_endpoints.json`(HAR에서 추출)로 경로 덮어쓰기 가능 |
| `BOT STOPPED` 후 자동 재시작 알림 | 엔진이 혼자 멈춤 → supervisor가 backoff 후 재시작 | 반복되면 `pm2 logs monk-api`에서 Traceback 확인 |

---

## 7. 안전장치 / 주의

- 키를 봇이 직접 들고 있으므로 **반드시 소액 전용 지갑**. 출금 권한이 있는 메인 지갑 키를 쓰지 말 것.
- 로그인 메시지 검증: 서버가 준 텍스트에 SIWE 문구가 없으면 서명하지 않는다. 임의 메시지 서명 경로는 없다.
- 페어 진입 중 한쪽만 체결되면 즉시 반대 reduce-only 주문으로 되돌린다(`_unwind_partial`, executor ~282). 한쪽만 남는 포지션을 만들지 않기 위해서.
- 청산 시 venue에 이미 포지션이 없으면 `external_closed`로 처리(수동 청산/청산 당함 대응).
- `VARIATIONAL_DRY_RUN` 기본값은 true. 실거래는 smoke 테스트 확인 후 false로.

---

## 8. 다른 프로젝트로 옮길 때 체크리스트

1. 지갑 키로 SIWE 로그인: `generate_signing_data` → personal_sign(0x 제거) → `login` → JWT
2. 모든 요청에 `authorization: Bearer <JWT>` + `vr-connected-address` 헤더
3. 401이면 재로그인 후 1회 재시도 (무한 루프 방지)
4. Cloudflare: 영구 프로필 Chromium + 페이지 내 `fetch()` (webdriver 숨김, Headless UA 제거)
5. 주기적 keepalive + 실패 시 세션 재구성 + 알림 (주문 실행 중엔 건드리지 않기)
6. 장시간 열린 브라우저는 주기적으로 재생성 (메모리)
7. 봇 프로세스/엔진 레벨에서도 자동 재시작 (keepalive는 봇이 살아 있어야 동작)
