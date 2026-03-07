# Monk's BTC/ETH Pair Trading Bot — 전체 구현 계획

> **거래소**: Pacifica · Extended · Lighter · Backpack  
> **전략**: BTC/ETH 페어 트레이딩 (상대 강도 역귀 전략)  
> **인프라**: AWS EC2 + React 웹 대시보드

---

## 1. 아키텍처 개요

```
┌─────────────────────────────────────────────────────────────────┐
│                        AWS EC2 (t3.medium)                      │
│                                                                 │
│   ┌──────────────┐    ┌──────────────┐    ┌─────────────────┐  │
│   │  FastAPI      │    │  Bot Engine  │    │  PostgreSQL     │  │
│   │  Backend      │◄──►│  (Python)    │◄──►│  (PNL/Trade DB) │  │
│   │  :8000        │    │  (PM2)       │    │                 │  │
│   └──────┬───────┘    └──────┬───────┘    └─────────────────┘  │
│          │                  │                                   │
│   ┌──────▼───────────────────▼───────────┐                     │
│   │         Exchange API Connectors      │                     │
│   │  Pacifica · Extended · Lighter · Backpack SDK               │
│   └──────────────────────────────────────┘                     │
└─────────────────────────────────────────────────────────────────┘
         ▲ HTTPS + WSS
         │
┌────────▼────────┐
│  React 대시보드  │  (Nginx Reverse Proxy → EC2)
│  (Vercel or     │  ← JWT 로그인 → 파라미터 설정
│   EC2 :3000)    │  ← 실시간 PNL / 포지션 / 로그
└─────────────────┘
```

---

## 2. 입력 인터페이스: 웹 로그인 방식 선택

### ✅ 권장: 웹 대시보드 (JWT Auth) + API Key는 서버 환경변수

**보안 설계:**

| 항목 | 방식 |
|------|------|
| 거래소 API Key/Secret | EC2 서버의 `.env` 파일 (환경변수) — 절대 프론트엔드에 노출 금지 |
| 대시보드 로그인 | ID/PW + JWT 토큰 (bcrypt 해시 저장) |
| 전략 파라미터 입력 | 웹 UI에서 입력 → HTTPS로 백엔드 저장 → DB 보관 |
| 서버 접근 | SSH Key 기반 (비밀번호 로그인 비활성화) |
| HTTPS | Let's Encrypt (Certbot) 무료 SSL |

**왜 웹 UI가 더 나은가:**
- 파라미터를 언제든지 수정 가능 (재배포 없이)
- 대시보드에서 즉시 상태 확인
- 모바일에서도 접근 가능
- API Key를 서버에만 보관하므로 프론트 해킹 시에도 안전

---

## 3. 전략 파라미터 (입력값 명세)

### 3-1. 거래소별 포지션 크기 설정

```json
{
  "exchanges": {
    "pacifica": {
      "enabled": true,
      "position_size_usd": 500,
      "leverage": 3
    },
    "extended": {
      "enabled": true,
      "position_size_usd": 500,
      "leverage": 3
    },
    "lighter": {
      "enabled": true,
      "position_size_usd": 500,
      "leverage": 3
    },
    "backpack": {
      "enabled": true,
      "position_size_usd": 500,
      "leverage": 3
    }
  }
}
```

### 3-2. 진입 조건 (Entry Conditions)

```json
{
  "entry": {
    "divergence_threshold_pct": 2.5,
    "lookback_minutes": 15,
    "confirmation_candles": 2,
    
    "sigma_filter": {
      "enabled": true,
      "window": 100,
      "entry_zscore": 2.0,
      "max_zscore": 3.5,
      "probability_threshold_pct": 95
    }
  }
}
```

| 파라미터 | 설명 | 기본값 |
|----------|------|--------|
| `divergence_threshold_pct` | ETH-BTC 수익률 차이 진입 임계값 (%) | 2.5% |
| `lookback_minutes` | 기준 시간봉 (몇 분 전 대비) | 15분 |
| `confirmation_candles` | 신호 연속 확인 캔들 수 | 2 |
| `sigma_filter.window` | Z-score 계산 윈도우 (캔들 수) | 100 |
| `sigma_filter.entry_zscore` | 진입 최소 Z-score | 2.0 (상위 2.3%) |
| `sigma_filter.max_zscore` | 진입 최대 Z-score (너무 극단적이면 skip) | 3.5 |
| `sigma_filter.probability_threshold_pct` | 진입할 최소 확률 (%) | 95% |

### 3-3. 청산 조건 (Exit Conditions)

```json
{
  "exit": {
    "take_profit_pct": 0.8,
    "stop_loss_pct": -3.0,
    "zscore_revert_threshold": 0.5,
    "max_hold_hours": 24,
    "trailing_stop": {
      "enabled": true,
      "activate_at_pct": 0.5,
      "trail_pct": 0.3
    }
  }
}
```

| 파라미터 | 설명 | 기본값 |
|----------|------|--------|
| `take_profit_pct` | 전체 포지션 수익률 (fee 포함) 도달 시 청산 | 0.8% |
| `stop_loss_pct` | 전체 손실 한도 초과 시 청산 | -3.0% |
| `zscore_revert_threshold` | Z-score가 이 값 이하로 수렴 시 청산 고려 | 0.5 |
| `max_hold_hours` | 최대 포지션 보유 시간 | 24시간 |
| `trailing_stop.activate_at_pct` | 트레일링 스탑 활성화 기준 수익률 | 0.5% |

### 3-4. 리스크 관리

```json
{
  "risk": {
    "max_open_trades": 3,
    "daily_loss_limit_usd": -200,
    "averaging_enabled": true,
    "averaging_trigger_pct": -1.5,
    "averaging_multiplier": 0.5,
    "size_reduction_enabled": true,
    "size_reduction_trigger_pct": -2.0,
    "size_reduction_ratio": 0.5
  }
}
```

---

## 4. Sigma(Z-score) 전략 상세 설명

### 4-1. 스프레드 Z-score 계산

```
spread(t) = return_ETH(t) - return_BTC(t)

mean = 이전 N개 스프레드의 평균
std  = 이전 N개 스프레드의 표준편차

Z-score(t) = (spread(t) - mean) / std
```

### 4-2. Z-score → 발생확률 변환

| Z-score | 발생 확률 (정규분포 기준) | 의미 |
|---------|------------------------|------|
| 1.0 | ~68% 구간 밖 → 약 16% | 약한 신호 |
| 1.5 | ~87% 구간 밖 → 약 6.7% | 보통 |
| 2.0 | ~95% 구간 밖 → 약 2.3% | ✅ 권장 진입 |
| 2.5 | ~99% 구간 밖 → 약 0.6% | 강한 신호 |
| 3.0 | ~99.7% 구간 밖 → 약 0.15% | 매우 강한 신호 |
| 3.5+ | 극단적 이상값 → 진입 skip | 위험 |

### 4-3. 진입 로직 의사결정

```
IF abs(Z-score) >= entry_zscore (2.0)
AND abs(Z-score) <= max_zscore (3.5)
AND probability >= threshold (95%)
AND divergence >= 2.5%
  → ENTRY
ELSE
  → WAIT
```

---

## 5. 디렉토리 구조

```
monk-pair-bot/
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI 엔트리포인트
│   │   ├── auth.py              # JWT 인증
│   │   ├── config.py            # 설정 DB CRUD
│   │   ├── models.py            # DB 모델 (Trade, PNL, Config)
│   │   └── websocket.py         # 실시간 데이터 스트림
│   ├── bot/
│   │   ├── engine.py            # 메인 봇 루프
│   │   ├── signal.py            # Z-score / 시그널 계산
│   │   ├── position_manager.py  # 포지션 관리 / 평균단가
│   │   ├── risk_manager.py      # 손절/익절 로직
│   │   └── exchanges/
│   │       ├── base.py          # 추상 클래스
│   │       ├── pacifica.py      # Pacifica API
│   │       ├── extended.py      # Extended API
│   │       ├── lighter.py       # Lighter API
│   │       └── backpack.py      # Backpack Exchange API
│   ├── .env                     # API Key (Git 제외)
│   └── requirements.txt
│
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Login.jsx
│   │   │   ├── Dashboard.jsx    # 메인 대시보드
│   │   │   └── Settings.jsx     # 파라미터 설정 페이지
│   │   ├── components/
│   │   │   ├── PNLChart.jsx
│   │   │   ├── PositionTable.jsx
│   │   │   ├── TradeLog.jsx
│   │   │   └── SignalMonitor.jsx
│   │   └── api/
│   │       └── client.js        # Axios + JWT 인터셉터
│   └── package.json
│
├── nginx/
│   └── default.conf             # Reverse Proxy 설정
├── docker-compose.yml
└── README.md
```

---

## 6. 핵심 모듈 구현 명세

### 6-1. Signal Engine (`signal.py`)

```python
class SignalEngine:
    def calculate_spread(self, btc_return, eth_return):
        return eth_return - btc_return

    def calculate_zscore(self, spread_series):
        mean = spread_series.rolling(self.window).mean()
        std  = spread_series.rolling(self.window).std()
        return (spread_series - mean) / std

    def zscore_to_probability(self, z):
        from scipy.stats import norm
        return (1 - norm.cdf(abs(z))) * 100  # 단측 확률 (%)

    def should_enter(self, z, divergence_pct):
        prob = self.zscore_to_probability(z)
        return (
            abs(z) >= self.entry_zscore
            and abs(z) <= self.max_zscore
            and prob <= (100 - self.prob_threshold)
            and abs(divergence_pct) >= self.divergence_threshold
        )
```

### 6-2. Position Manager (`position_manager.py`)

```python
class PositionManager:
    def open_pair(self, direction, size_usd, exchange):
        # direction: "LONG_BTC_SHORT_ETH" or "SHORT_BTC_LONG_ETH"
        if direction == "LONG_BTC_SHORT_ETH":
            self.open_long(exchange, "BTC", size_usd)
            self.open_short(exchange, "ETH", size_usd)
        else:
            self.open_short(exchange, "BTC", size_usd)
            self.open_long(exchange, "ETH", size_usd)

    def calculate_total_pnl(self):
        # 양쪽 포지션 PNL + 수수료 합산
        return btc_pnl + eth_pnl - total_fees

    def should_exit(self, total_pnl_pct):
        return (
            total_pnl_pct >= self.take_profit
            or total_pnl_pct <= self.stop_loss
            or self.zscore_reverted()
        )

    def averaging_down(self, losing_leg, size_multiplier=0.5):
        # 손실 중인 레그만 추가 진입 (전략 원문의 averaging 로직)
        pass

    def size_reduction(self, winning_leg, ratio=0.5):
        # 수익 중인 레그 사이즈 축소로 전체 포지션 중립화
        pass
```

### 6-3. 봇 메인 루프 (`engine.py`)

```python
async def main_loop():
    while True:
        # 1. 가격 데이터 수집 (1분 캔들)
        btc_price, eth_price = await fetch_prices()

        # 2. 수익률 계산
        btc_ret = calc_return(btc_prices, lookback_min)
        eth_ret = calc_return(eth_prices, lookback_min)

        # 3. Z-score 계산
        spread = eth_ret - btc_ret
        zscore = signal_engine.calculate_zscore(spread_history)

        # 4. 진입 조건 체크
        if no_open_position:
            if signal_engine.should_enter(zscore, spread):
                direction = "LONG_BTC_SHORT_ETH" if spread > 0 else "SHORT_BTC_LONG_ETH"
                for exchange in enabled_exchanges:
                    await position_manager.open_pair(direction, size_usd[exchange], exchange)
                await db.save_trade(...)

        # 5. 청산 조건 체크
        elif has_open_position:
            total_pnl = position_manager.calculate_total_pnl()
            if position_manager.should_exit(total_pnl):
                for exchange in enabled_exchanges:
                    await position_manager.close_all(exchange)
                await db.save_pnl(...)

        # 6. 리스크 관리 (averaging / size reduction)
        elif needs_risk_action:
            await risk_manager.handle(position)

        await asyncio.sleep(60)  # 1분마다 실행
```

---

## 6-4. Backpack Exchange 커넥터 특이사항

Backpack은 다른 거래소와 인증 방식이 다릅니다:

```python
# Backpack: Ed25519 서명 기반 인증 (API Key/Secret 방식 아님)
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

class BackpackConnector(BaseExchange):
    def __init__(self, private_key_b64: str):
        private_key_bytes = base64.b64decode(private_key_b64)
        self.private_key = Ed25519PrivateKey.from_private_bytes(private_key_bytes)

    def sign_request(self, instruction: str, params: dict, timestamp: int, window: int):
        sign_str = f"instruction={instruction}"
        sorted_params = sorted(params.items())
        if sorted_params:
            sign_str += "&" + "&".join(f"{k}={v}" for k, v in sorted_params)
        sign_str += f"&timestamp={timestamp}&window={window}"
        signature = self.private_key.sign(sign_str.encode())
        return base64.b64encode(signature).decode()

    async def place_order(self, symbol, side, size, order_type="Market"):
        # POST /api/v1/order
        # symbol: "BTC_USDC_PERP" or "ETH_USDC_PERP"
        pass

    async def get_position(self, symbol):
        # GET /api/v1/position
        pass
```

**Backpack API 엔드포인트 참고:**
- Base URL: `https://api.backpack.exchange`
- Perp 심볼: `BTC_USDC_PERP`, `ETH_USDC_PERP`
- 인증: Ed25519 서명 (키 쌍은 Backpack 웹에서 생성)
- `.env`에 저장: `BACKPACK_PRIVATE_KEY=<base64_encoded_private_key>`

---

### 메인 대시보드

```
┌─────────────────────────────────────────────────────────┐
│  MONK PAIR BOT                           [●] RUNNING    │
├──────────────┬──────────────┬───────────────────────────┤
│ 오늘 PNL      │ 누적 PNL      │ 현재 포지션                │
│ +$23.40      │ +$312.80     │ LONG BTC / SHORT ETH       │
│ +4.68%       │ Total ROI    │ Pacifica · Extended        │
├──────────────┴──────────────┴───────────────────────────┤
│  PNL 차트 (시간별)  [1D] [1W] [1M]                       │
│  ████████████████░░░░░░░░░░░░░░░░                       │
├─────────────────────────────────────────────────────────┤
│  현재 포지션                                             │
│  거래소     레그       사이즈    진입가    현재PNL         │
│  Pacifica  BTC Long  $500     $95,200   +$4.20          │
│  Pacifica  ETH Short $500     $3,320    +$6.80          │
│  Extended  BTC Long  $500     $95,200   +$4.10          │
│  ...                                                    │
├─────────────────────────────────────────────────────────┤
│  Signal Monitor                                         │
│  ETH-BTC Spread: +2.7%  Z-score: 2.31  Prob: 98.9%    │
│  [████████████░░░░] Entry Threshold: 2.0               │
├─────────────────────────────────────────────────────────┤
│  거래 로그                                              │
│  14:32 ENTRY LONG_BTC_SHORT_ETH | Z=2.31 | 3 exchanges  │
│  13:15 EXIT +$18.40 | Z reverted to 0.42               │
└─────────────────────────────────────────────────────────┘
```

### 설정 페이지

- 거래소별 활성화 토글 + 포지션 크기 (USD)
- 진입 파라미터: Divergence %, Z-score, 확률 임계값
- 청산 파라미터: TP%, SL%, 최대 보유 시간
- 리스크 관리: Averaging, Size Reduction 설정
- 봇 시작/정지 버튼

---

## 8. EC2 배포 구성

### 서버 스펙 (권장)
- **인스턴스**: t3.small (2vCPU, 2GB RAM) — 월 ~$15
- **OS**: Ubuntu 22.04 LTS
- **스토리지**: 20GB gp3

### 설치 스택
```bash
# System
Ubuntu 22.04
Nginx 1.24
PM2 (Process Manager)

# Backend
Python 3.11
FastAPI + Uvicorn
PostgreSQL 15
Redis (선택, 가격 캐싱)

# Frontend
Node.js 20
React 18 (Vite build)
```

### PM2 프로세스 구성
```json
{
  "apps": [
    {
      "name": "monk-api",
      "script": "uvicorn app.main:app --host 0.0.0.0 --port 8000",
      "cwd": "/home/ubuntu/monk-pair-bot/backend"
    },
    {
      "name": "monk-bot",
      "script": "python bot/engine.py",
      "cwd": "/home/ubuntu/monk-pair-bot/backend",
      "restart_delay": 5000,
      "max_restarts": 10
    }
  ]
}
```

### Nginx 설정
```nginx
server {
    listen 443 ssl;
    server_name yourdomain.com;
    
    # Frontend (React)
    location / {
        root /var/www/monk-bot/dist;
        try_files $uri /index.html;
    }
    
    # Backend API
    location /api/ {
        proxy_pass http://localhost:8000;
        proxy_set_header Authorization $http_authorization;
    }
    
    # WebSocket (실시간 PNL)
    location /ws/ {
        proxy_pass http://localhost:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## 9. 데이터베이스 스키마

```sql
-- 거래 기록
CREATE TABLE trades (
    id          SERIAL PRIMARY KEY,
    exchange    VARCHAR(20),
    direction   VARCHAR(30),   -- LONG_BTC_SHORT_ETH
    size_usd    DECIMAL(12,2),
    btc_entry   DECIMAL(12,2),
    eth_entry   DECIMAL(12,4),
    zscore_at_entry DECIMAL(8,4),
    spread_at_entry DECIMAL(8,4),
    opened_at   TIMESTAMP,
    closed_at   TIMESTAMP,
    pnl_usd     DECIMAL(12,2),
    fees_usd    DECIMAL(8,4),
    net_pnl_usd DECIMAL(12,2),
    exit_reason VARCHAR(50)    -- TP/SL/ZSCORE/MANUAL/TIMEOUT
);

-- 누적 PNL 스냅샷 (시간별)
CREATE TABLE pnl_snapshots (
    id           SERIAL PRIMARY KEY,
    snapshot_at  TIMESTAMP,
    cumulative_pnl DECIMAL(12,2),
    daily_pnl    DECIMAL(12,2),
    open_positions INT
);

-- 봇 설정
CREATE TABLE bot_config (
    id         SERIAL PRIMARY KEY,
    config_key VARCHAR(100) UNIQUE,
    config_val JSONB,
    updated_at TIMESTAMP
);
```

---

## 10. 구현 단계 (Development Roadmap)

### Phase 1 — 기반 구조 (1~2주)
- [ ] EC2 인스턴스 설정, PostgreSQL 설치
- [ ] FastAPI 백엔드 골격 (Auth, Config API)
- [ ] Exchange API Connector 클래스 작성 (4개 거래소: Pacifica, Extended, Lighter, Backpack)
- [ ] 가격 데이터 수집 + Z-score 계산 모듈

### Phase 2 — 봇 엔진 (2~3주)
- [ ] Signal Engine 구현 + 백테스트
- [ ] Position Manager (진입/청산)
- [ ] Risk Manager (Averaging, Size Reduction)
- [ ] 메인 루프 + PM2 데몬화

### Phase 3 — 대시보드 (1~2주)
- [ ] React 대시보드 (Login, Dashboard, Settings)
- [ ] WebSocket 실시간 PNL 스트림
- [ ] PNL 차트 (Recharts)
- [ ] Nginx + SSL 배포

### Phase 4 — 테스트 & 튜닝 (1주)
- [ ] Paper trading 모드 (실제 주문 없이 시뮬레이션)
- [ ] 소액 라이브 테스트
- [ ] 파라미터 최적화

---

## 11. 보안 체크리스트

- [x] 거래소 API Key → 서버 `.env`에만 저장, Git에 절대 커밋 금지
- [x] `.gitignore`에 `.env` 추가
- [x] SSH 접근 → Key 기반 인증만 허용 (비밀번호 로그인 비활성화)
- [x] EC2 Security Group → 443(HTTPS), 22(SSH)만 오픈
- [x] 대시보드 JWT 토큰 만료: 24시간
- [x] API Key 권한: 거래(Trade) 권한만 부여, 출금(Withdraw) 권한 미부여
- [x] 일일 손실 한도 Hard-limit (DB + 봇 양쪽에서 체크)
- [x] 이상 거래 감지 시 Telegram 알림

---

## 12. 구현 시작 순서 제안

1. **어떤 파일부터 만들어 드릴까요?**
   - `backend/bot/exchanges/` — 4개 거래소 API 커넥터 (Pacifica, Extended, Lighter, Backpack)
   - `backend/bot/signal.py` — Z-score 계산 엔진  
   - `frontend/` — React 대시보드
   - `docker-compose.yml` — 전체 환경 구성

2. **각 거래소 API 문서 URL** 을 알고 있다면 공유해 주시면 커넥터를 바로 구현할 수 있습니다.

---

*NFA. 전략 구현 참고 목적. 실제 거래 시 충분한 테스트 후 소액부터 시작하세요.*
