# Monk's BTC/ETH Pair Trading Bot — 구현 계획 (v2)

> **거래소**: Pacifica · Extended · Lighter · Backpack
> **전략**: BTC/ETH 페어 트레이딩 (상대 강도 역귀 전략)
> **인프라**: AWS EC2 + React 웹 대시보드
> **데이터**: WebSocket 실시간 스트림 (REST 폴링 사용 안 함)

---

## 1. 아키텍처 개요

```
┌──────────────────────────────────────────────────────────────────────┐
│                          AWS EC2 (t3.small)                          │
│                                                                      │
│  ┌─────────────────────────────────────────────────────────────┐    │
│  │                   WebSocket Price Hub                        │    │
│  │  각 거래소 WS 연결 유지 → 실시간 BTC/ETH 틱 수신            │    │
│  │  Pacifica WS · Extended WS · Lighter WS · Backpack WS       │    │
│  └──────────────────────┬──────────────────────────────────────┘    │
│                         │ 틱 발생 시 즉시 push                       │
│  ┌──────────────────────▼──────────────────────────────────────┐    │
│  │                    Bot Engine (Python)                       │    │
│  │  PriceBuffer → SpreadCalc → MultiTF Signal → OrderManager   │    │
│  └──────┬───────────────────────────────────────┬──────────────┘    │
│         │                                       │                   │
│  ┌──────▼──────┐                       ┌────────▼────────┐          │
│  │  FastAPI    │                       │   PostgreSQL    │          │
│  │  :8000      │                       │  trades / pnl   │          │
│  └──────┬──────┘                       └─────────────────┘          │
└─────────┼────────────────────────────────────────────────────────────┘
          │ HTTPS + WSS
┌─────────▼────────┐
│  React 대시보드   │  JWT 로그인 → 파라미터 설정 → 실시간 PNL
└──────────────────┘
```

---

## 2. 보안 설계

| 항목 | 방식 |
|------|------|
| 거래소 API Key/Secret | EC2 `.env` 파일 — 프론트엔드 절대 노출 금지 |
| Backpack 인증 | Ed25519 개인키 (base64) → `.env` 저장 |
| 대시보드 로그인 | ID/PW + JWT (bcrypt 해시, 만료 24h) |
| 전략 파라미터 | 웹 UI 입력 → HTTPS → DB 저장 |
| SSH 접근 | Key 기반만 허용, 비밀번호 비활성화 |
| EC2 Security Group | 443(HTTPS), 22(SSH)만 오픈 |
| API Key 권한 | Trade 권한만, Withdraw 권한 절대 미부여 |

---

## 3. 데이터 수집: WebSocket 실시간 스트림

### ❌ 기존 방식 (사용 안 함)
```python
# REST 폴링 — 느리고 rate limit 위험
while True:
    price = requests.get("/api/price")
    await asyncio.sleep(60)
```

### ✅ 개선 방식: WebSocket 상시 연결
```python
# 거래소 WS에서 틱 발생 시 즉시 콜백
async def on_tick(exchange: str, symbol: str, price: float, timestamp: int):
    price_buffer.update(exchange, symbol, price, timestamp)
    spread = spread_calculator.compute()          # 즉시 스프레드 재계산
    await signal_engine.evaluate(spread)          # 즉시 시그널 평가
    await dashboard_ws.broadcast(spread)          # 대시보드 실시간 업데이트

# 각 거래소별 WS 연결 (비동기 병렬)
async def start_price_hub():
    await asyncio.gather(
        pacifica_ws.connect("BTC-PERP", "ETH-PERP", callback=on_tick),
        extended_ws.connect("BTC-PERP", "ETH-PERP", callback=on_tick),
        lighter_ws.connect("BTC-PERP", "ETH-PERP",  callback=on_tick),
        backpack_ws.connect("BTC_USDC_PERP", "ETH_USDC_PERP", callback=on_tick),
    )
```

### Price Buffer 구조
```python
class PriceBuffer:
    """
    각 거래소 틱을 수신해 1초 OHLC 캔들로 집계.
    1분봉 / 5분봉 / 1시간봉은 1초 캔들에서 자동 합산.
    """
    def __init__(self, max_candles=500):
        self.ticks: deque = deque(maxlen=10000)   # 원시 틱
        self.candles_1s:  deque = deque(maxlen=500)
        self.candles_1m:  deque = deque(maxlen=500)
        self.candles_5m:  deque = deque(maxlen=200)
        self.candles_1h:  deque = deque(maxlen=100)
```

---

## 4. 멀티 타임프레임 시그널 (3-Layer Filter)

단일 봉/임계값 대신 3단계 필터로 노이즈는 줄이고 거래 빈도는 높임.

```
Layer 1 — 추세 필터 (1시간봉)
  목적: "지금 ETH가 BTC 대비 구조적 과매수/과매도 구간인가?"
  조건: 1h 스프레드 Z-score 방향이 진입 방향과 일치
  → 역방향이면 진입 차단

Layer 2 — 진입 시그널 (5분봉)
  목적: "지금 벌어지는 중인가?"
  조건: 5분봉 Z-score ≥ 1.5 AND divergence ≥ 1.0%

Layer 3 — 트리거 (실시간 틱)
  목적: "지금이 정확한 진입 타이밍인가?"
  조건: 실시간 스프레드가 최댓값에서 수렴 전환 감지
        (peak_spread × 0.95 이하로 내려오는 순간 진입)

→ 3개 레이어 모두 통과 시 주문 실행
```

### 시그널 엔진 구현
```python
class MultiTimeframeSignalEngine:

    def evaluate(self, price_buffer: PriceBuffer) -> Signal | None:

        # Layer 1: 1h 추세 방향 확인
        trend = self._check_trend(price_buffer.candles_1h)
        if trend == TrendDirection.NEUTRAL:
            return None

        # Layer 2: 5분봉 Z-score + divergence
        z5m   = self._zscore(price_buffer.candles_5m, window=self.z_window_5m)
        div5m = self._divergence(price_buffer.candles_5m, lookback=12)  # 60분
        if abs(z5m) < self.entry_zscore or abs(div5m) < self.divergence_threshold:
            return None
        if abs(z5m) > self.max_zscore:  # 극단적 이상값 skip
            return None

        # Layer 3: 틱 수준에서 peak 수렴 감지
        if not self._is_reverting(price_buffer.ticks):
            return None

        # 방향 결정
        direction = (
            "LONG_BTC_SHORT_ETH" if z5m > 0   # ETH 과매수 → ETH 숏
            else "SHORT_BTC_LONG_ETH"
        )
        prob = self._zscore_to_probability(z5m)

        return Signal(direction=direction, zscore=z5m,
                      divergence=div5m, probability=prob)

    def _zscore_to_probability(self, z: float) -> float:
        from scipy.stats import norm
        return (1 - norm.cdf(abs(z))) * 100  # 단측 (%)
```

---

## 5. 운영 모드별 파라미터

웹 대시보드 Settings에서 모드 선택 가능. 모드별 기본값:

### 스캘핑 모드 (빠른 회전, 소액)
```json
{
  "mode": "scalp",
  "data_feed": "websocket",
  "signal": {
    "z_window_5m": 30,
    "entry_zscore": 1.5,
    "max_zscore": 3.0,
    "divergence_threshold_pct": 0.3,
    "divergence_lookback": 6,
    "peak_revert_ratio": 0.95
  },
  "exit": {
    "take_profit_pct": 0.4,
    "stop_loss_pct": -1.5,
    "zscore_revert_threshold": 0.3,
    "min_hold_minutes": 60,
    "zscore_exit_min_pnl_pct": 0.05,
    "max_hold_hours": 2
  },
  "expected_trades_per_day": "20~50회"
}
```

### 스윙 모드 (느린 회전, 중간 사이즈) — 기본값
```json
{
  "mode": "swing",
  "data_feed": "websocket",
  "signal": {
    "z_window_5m": 50,
    "entry_zscore": 2.0,
    "max_zscore": 3.5,
    "divergence_threshold_pct": 1.5,
    "divergence_lookback": 12,
    "peak_revert_ratio": 0.90
  },
  "exit": {
    "take_profit_pct": 0.8,
    "stop_loss_pct": -3.0,
    "zscore_revert_threshold": 1.5,
    "min_hold_minutes": 120,
    "zscore_exit_min_pnl_pct": 0.05,
    "max_hold_hours": 12
  },
  "expected_trades_per_day": "0.3~0.5회"
}
```

### 포지션 모드 (홀딩)
```json
{
  "mode": "position",
  "data_feed": "websocket",
  "signal": {
    "z_window_5m": 100,
    "entry_zscore": 2.5,
    "max_zscore": 4.0,
    "divergence_threshold_pct": 2.0,
    "divergence_lookback": 24,
    "peak_revert_ratio": 0.85
  },
  "exit": {
    "take_profit_pct": 2.0,
    "stop_loss_pct": -5.0,
    "zscore_revert_threshold": 0.8,
    "min_hold_minutes": 180,
    "zscore_exit_min_pnl_pct": 0.1,
    "max_hold_hours": 48
  },
  "expected_trades_per_day": "1~3회"
}
```

### 2026-05-19 Binance 6개월 백테스트 기준값

> 기준 데이터: Binance Futures `BTCUSDT` / `ETHUSDT` 1분봉  
> 기간: 2025-11-19 ~ 2026-05-19 (181일), warmup 48시간  
> 실행 가정: Lighter, 레그당 $500, 총 노출 $1000, 3x 레버리지, taker fee 0bp, slippage 1bp  
> 백테스트 모드: alert-only와 맞추기 위해 averaging / size reduction 비활성화

권장 운영값:

```json
{
  "mode": "swing",
  "signal": {
    "z_window_5m": 50,
    "entry_zscore": 2.0,
    "max_zscore": 3.5,
    "divergence_threshold_pct": 1.5,
    "divergence_lookback": 12,
    "peak_revert_ratio": 0.90
  },
  "exit": {
    "take_profit_pct": 0.8,
    "stop_loss_pct": -3.0,
    "zscore_revert_threshold": 1.5,
    "min_hold_minutes": 120,
    "max_hold_hours": 12,
    "zscore_exit_min_pnl_pct": 0.05
  },
  "costs": {
    "taker_fee_bps": 0,
    "slippage_bps": 1
  }
}
```

정확 엔진 검증 결과:

| 항목 | 값 |
|------|----|
| 총 거래 수 | 77 |
| 승률 | 94.8% |
| 거래 빈도 | 0.4회/일 |
| 순손익 | 약 +$305.07 |
| Profit Factor | 10.53 |
| 평균 보유 | 131.2분 |
| Max Drawdown | -27.20% |
| Sharpe | 5.10 |
| Calmar | 4.64 |
| 청산 사유 | ZSCORE 46 · TP 27 · TIMEOUT 4 |

APR 환산:

| 기준 자본 | 계산 | APR |
|-----------|------|-----|
| 총 노출 기준 | $305.07 / $1000 × 365 / 181 | 약 61.5% |
| 레그당 크기 기준 | $305.07 / $500 × 365 / 181 | 약 123.0% |
| 3x 증거금 기준 | $305.07 / ($1000 / 3) × 365 / 181 | 약 184.6% |

운영 판단은 총 노출 기준 APR을 기본값으로 본다. 증거금 기준 APR은 레버리지 효과를 보여주지만 funding, 미체결, 청산 버퍼, 실시간 슬리피지 확대를 반영하지 못한다.

빠른 5분봉 스윕 요약:

| divergence | zscore revert | min hold | min net pnl | trades | PnL | Sharpe | PF |
|------------|---------------|----------|-------------|--------|-----|--------|----|
| 1.5 | 1.5 | 120m | 0.05% | 12 | +$33.39 | 2.82 | 7.71 |
| 0.8 | 1.2 | 180m | 0.10% | 36 | +$26.46 | 1.40 | 1.51 |
| 0.8 | 1.5 | 120m | 0.05% | 36 | +$23.22 | 1.30 | 1.50 |
| 0.3 | 1.5 | 120~180m | 0.05% | 155~159 | 손실 | 음수 | < 1 |
| 0.03 | 1.5 | 120m | 0~0.2% | 223~257 | 손실 | 음수 | < 1 |

해석:
- `divergence_threshold_pct`가 0.3% 이하이면 거래 수는 늘지만 1bp 슬리피지만 반영해도 손익이 무너진다.
- Binance 6개월 기준으로는 `divergence_threshold_pct=1.5`가 가장 보수적이고 품질이 좋다.
- `zscore_exit_min_pnl_pct=0`은 너무 약하다. 최소 0.05% 순수익 버퍼를 둔다.
- `min_hold_minutes=120`은 강제 청산 시간이 아니라 Z-score 수렴 청산을 허용하기 전 최소 보유 시간이다. 강제 시간 청산은 `max_hold_hours=12`이며, `max_hold_hours <= 0`이면 시간청산을 끈다.

### Startup Warmup

운영 중 `PRIMARY_EXCHANGE=lighter`를 쓰더라도 시작 직후 5분봉/1시간봉 warmup은 Binance Futures klines를 우선 사용한다.
Lighter REST kline API가 403을 반환하면 봇이 `data=0/50` 상태로 오래 대기하기 때문에, 테스트/운영 재시작 시에는 Binance 히스토리로 `PriceBuffer`와 `SignalEngine`을 먼저 채운다. 실시간 가격 스트림과 주문 실행 venue는 별도 설정을 따른다.

```env
WARMUP_BINANCE_ENABLED=true
WARMUP_BINANCE_FIRST=true
WARMUP_BINANCE_TIMEOUT_SEC=10
WARMUP_BINANCE_KLINES_URL=https://fapi.binance.com/fapi/v1/klines
```

---

## 6. 거래소별 포지션 설정

```json
{
  "exchanges": {
    "pacifica":  { "enabled": true, "position_size_usd": 500, "leverage": 3 },
    "extended":  { "enabled": true, "position_size_usd": 500, "leverage": 3 },
    "lighter":   { "enabled": true, "position_size_usd": 500, "leverage": 3 },
    "backpack":  { "enabled": true, "position_size_usd": 500, "leverage": 3 }
  }
}
```

### 거래소별 수수료 전략
| 거래소 | 수수료 | 권장 모드 |
|--------|--------|----------|
| Lighter | ~0% | 스캘핑 가능 |
| Pacifica | 저수수료 | 스캘핑 / 스윙 |
| Extended | 저수수료 | 스캘핑 / 스윙 |
| Backpack | 일반 수수료 | 스윙 / 포지션 권장 |

> 스캘핑 시 수수료 구조: divergence 1.0% - fee×4레그 = 실수익
> Lighter 우선 배분 권장

---

## 7. 리스크 관리

```json
{
  "risk": {
    "max_open_trades": 3,
    "daily_loss_limit_usd": -200,
    "averaging": {
      "enabled": true,
      "trigger_pct": -1.5,
      "size_multiplier": 0.5,
      "max_avg_count": 2
    },
    "size_reduction": {
      "enabled": true,
      "trigger_pct": -2.0,
      "reduce_ratio": 0.5
    }
  }
}
```

---

## 8. Z-score → 발생확률 참고표

| Z-score | 발생 확률 | 스캘핑 | 스윙 | 포지션 |
|---------|----------|--------|------|--------|
| 1.0 | ~16% | — | — | — |
| 1.5 | ~6.7% | ✅ 진입 | — | — |
| 2.0 | ~2.3% | ✅ | ✅ 진입 | — |
| 2.5 | ~0.6% | ✅ | ✅ | ✅ 진입 |
| 3.5+ | ~0.02% | ⚠️ skip | ⚠️ skip | ⚠️ skip |

---

## 9. Backpack Exchange 인증 특이사항

```python
# Ed25519 서명 기반 (API Key/Secret 방식 아님)
import base64
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

class BackpackConnector(BaseExchange):
    def __init__(self, private_key_b64: str):
        key_bytes = base64.b64decode(private_key_b64)
        self.private_key = Ed25519PrivateKey.from_private_bytes(key_bytes)

    def sign_request(self, instruction, params, timestamp, window=5000):
        parts = [f"instruction={instruction}"]
        for k, v in sorted(params.items()):
            parts.append(f"{k}={v}")
        parts += [f"timestamp={timestamp}", f"window={window}"]
        msg = "&".join(parts)
        sig = self.private_key.sign(msg.encode())
        return base64.b64encode(sig).decode()

    # 심볼: "BTC_USDC_PERP", "ETH_USDC_PERP"
    # Base URL: https://api.backpack.exchange
    # WS URL:   wss://ws.backpack.exchange
```

`.env` 저장:
```
BACKPACK_PRIVATE_KEY=<base64_encoded_ed25519_private_key>
```

---

## 10. 메인 봇 루프 (WebSocket 기반)

```python
# engine.py — REST 폴링 루프 없음, 이벤트 드리븐
async def run():
    await asyncio.gather(
        price_hub.start(),          # WS 연결 유지 + 재연결 자동화
        position_monitor.start(),   # 포지션 PNL 실시간 감시
        risk_monitor.start(),       # 일일 손실 한도 감시
        dashboard_broadcaster.start(), # 대시보드 WS 브로드캐스트
    )

# 가격 이벤트 → 시그널 → 주문 (이벤트 드리븐)
async def on_tick(exchange, symbol, price, ts):
    price_buffer.update(exchange, symbol, price, ts)
    signal = signal_engine.evaluate(price_buffer)

    if signal and not position_manager.has_open():
        await position_manager.open_pair(signal)

    elif position_manager.has_open():
        pnl = position_manager.total_pnl()
        if position_manager.should_exit(pnl, signal_engine.zscore_current()):
            await position_manager.close_all(reason="TP/SL/REVERT")
        elif risk_manager.needs_averaging(pnl):
            await risk_manager.average_down()
        elif risk_manager.needs_size_reduction(pnl):
            await risk_manager.reduce_winning_leg()
```

---

## 11. 디렉토리 구조

```
monk_trading_bot/
├── CLAUDE.md                        # 이 파일
├── .gitignore                       # .env 반드시 포함
├── docker-compose.yml
│
├── backend/
│   ├── .env                         # API Keys (Git 제외)
│   ├── requirements.txt
│   ├── app/
│   │   ├── main.py                  # FastAPI 엔트리포인트
│   │   ├── auth.py                  # JWT 인증
│   │   ├── config.py                # 설정 CRUD
│   │   ├── models.py                # DB 모델
│   │   └── ws_broadcast.py          # 대시보드 WebSocket
│   └── bot/
│       ├── engine.py                # 메인 이벤트 루프
│       ├── price_hub.py             # WebSocket 가격 허브
│       ├── price_buffer.py          # 틱 → 캔들 집계
│       ├── signal.py                # MultiTF 시그널 엔진
│       ├── position_manager.py      # 포지션 관리
│       ├── risk_manager.py          # Averaging / SizeReduction
│       └── exchanges/
│           ├── base.py              # 추상 클래스
│           ├── pacifica.py
│           ├── extended.py
│           ├── lighter.py
│           └── backpack.py          # Ed25519 인증
│
├── frontend/
│   ├── src/
│   │   ├── pages/
│   │   │   ├── Login.jsx
│   │   │   ├── Dashboard.jsx
│   │   │   └── Settings.jsx         # 모드/파라미터 설정
│   │   ├── components/
│   │   │   ├── PNLChart.jsx
│   │   │   ├── PositionTable.jsx
│   │   │   ├── TradeLog.jsx
│   │   │   ├── SignalMonitor.jsx     # 실시간 Z-score 게이지
│   │   │   └── SpreadChart.jsx      # 실시간 스프레드 차트
│   │   └── api/client.js
│   └── package.json
│
└── nginx/default.conf
```

---

## 12. DB 스키마

```sql
CREATE TABLE trades (
    id              SERIAL PRIMARY KEY,
    exchange        VARCHAR(20),
    direction       VARCHAR(30),
    size_usd        DECIMAL(12,2),
    btc_entry       DECIMAL(12,2),
    eth_entry       DECIMAL(12,4),
    zscore_entry    DECIMAL(8,4),
    spread_entry    DECIMAL(8,4),
    signal_mode     VARCHAR(20),       -- scalp / swing / position
    opened_at       TIMESTAMPTZ,
    closed_at       TIMESTAMPTZ,
    pnl_usd         DECIMAL(12,2),
    fees_usd        DECIMAL(8,4),
    net_pnl_usd     DECIMAL(12,2),
    exit_reason     VARCHAR(50)        -- TP/SL/ZSCORE/MANUAL/TIMEOUT
);

CREATE TABLE pnl_snapshots (
    id             SERIAL PRIMARY KEY,
    snapshot_at    TIMESTAMPTZ,
    cumulative_pnl DECIMAL(12,2),
    daily_pnl      DECIMAL(12,2),
    open_positions INT
);

CREATE TABLE bot_config (
    id         SERIAL PRIMARY KEY,
    config_key VARCHAR(100) UNIQUE,
    config_val JSONB,
    updated_at TIMESTAMPTZ
);
```

---

## 13. EC2 배포

```
인스턴스: t3.small (2vCPU 2GB) — 월 ~$15
OS: Ubuntu 22.04 LTS
스택: Python 3.11 / FastAPI / PostgreSQL 15 / Redis / Nginx / PM2
```

```json
// ecosystem.config.json (PM2)
{
  "apps": [
    { "name": "monk-api", "script": "uvicorn app.main:app --host 0.0.0.0 --port 8000",
      "cwd": "/home/ubuntu/monk_trading_bot/backend" },
    { "name": "monk-bot", "script": "python -m bot.engine",
      "cwd": "/home/ubuntu/monk_trading_bot/backend",
      "restart_delay": 3000, "max_restarts": 20 }
  ]
}
```

---

## 14. 구현 순서 (Claude Code 작업 순서)

```
Phase 1: 거래소 커넥터
  exchanges/base.py → pacifica.py → extended.py → lighter.py → backpack.py
  각 거래소 WebSocket 연결 + 주문 실행 테스트

Phase 2: 데이터 엔진
  price_hub.py (WS 허브) → price_buffer.py (틱→캔들) → signal.py (MultiTF)

Phase 3: 트레이딩 엔진
  position_manager.py → risk_manager.py → engine.py (메인 루프)

Phase 4: 백엔드 API
  models.py → auth.py → config.py → main.py → ws_broadcast.py

Phase 5: 프론트엔드
  Login → Dashboard (실시간 PNL/포지션) → Settings (파라미터) → SpreadChart

Phase 6: 배포 & 테스트
  Docker Compose → EC2 배포 → Paper trading → 소액 라이브
```

---

## 15. Variational 반자동/텔레그램 승인 봇 계획

Variational Omni는 공식 Trading API가 아직 공개되지 않았으므로, 자동화는 다음 순서로 진행한다.

### Phase V1: Read API 신선도 검증

목적: Variational `/metadata/stats`가 신호 기준으로 쓸 만큼 실시간인지 확인한다.

실행:

```bash
python -m backend.scripts.check_variational_read_api --samples 300 --interval 1
```

판정 기준:
- `quotes.updated_at` 기준 p95 quote age가 5~10초 이하면 신호 보조값으로 사용 가능.
- 30초 이상 stale 구간이 잦으면 Variational 가격은 참고용으로만 사용.
- 문서상 bid/ask quote는 최대 600초 캐시될 수 있으므로, 실거래 신호는 Binance/Lighter WS 기준을 우선한다.

### Phase V2: WalletConnect 지원 검증

목적: Variational 웹앱을 WalletConnect로 연결할 수 있는지 확인한다.

권장 구조:
- EC2에 소액 전용 EVM 지갑을 둔다.
- WalletConnect 세션은 봇이 유지한다.
- Variational에서 서명 요청이 오면, 봇은 요청 내용을 해석해 텔레그램으로 보낸다.

실행:

```bash
cd tools/variational-wallet
npm install
cp .env.example .env
nano .env
npm start -- --pair
```

초기값은 `VARIATIONAL_WC_DRY_RUN=true`다. 이 상태에서는 텔레그램 승인까지는 테스트하지만 실제 서명은 거부한다.

### Phase V3: 텔레그램 승인 기반 서명

목적: 텔레그램 승인 버튼을 누른 경우에만 EC2 소액 지갑으로 서명한다.

실측 결과:
- Variational 웹은 WalletConnect 연결 후 주문마다 추가 지갑 서명을 요구하지 않을 수 있다.
- 이 경우 WalletConnect signer는 세션 연결/재연결용이고, 주문별 승인 지점은 지갑 서명이 아니라 웹페이지 최종 주문 버튼 클릭이다.

필수 안전장치:
- Telegram user id allowlist.
- 승인 유효시간 30~60초.
- 1회성 nonce.
- 주문당 최대 notional, 일일 주문 횟수, 일일 손실 제한.
- 승인 직전 Binance/Lighter/Hyperliquid median fair price 재검증.
- Variational quote age 제한.
- 서명 요청/스크린샷/응답/결과 DB 저장.
- Kill Switch 버튼.

추천 실행 방식:
1. Binance/Lighter/Hyperliquid 외부 가격에서 신호와 주문 기준가를 생성한다.
2. Variational read API는 quote freshness와 화면 검증 보조값으로만 사용한다.
3. WalletConnect 서명 요청을 텔레그램으로 전송.
4. 승인 시 소액 지갑으로만 서명.
5. dry-run 요청 내용이 Variational 화면과 일치하는 것을 확인한 뒤 `VARIATIONAL_WC_DRY_RUN=false`로 전환한다.

### Phase V3.5: 외부 공정가 기준

목적: Variational 자체 가격이 멈추거나 늦게 갱신될 수 있으므로, 주문 판단 기준은 외부 가격 median으로 잡는다.

구현:
- `backend.bot.fair_price.FairPriceOracle`
- 소스: Binance USDT-M bookTicker, Lighter ticker WebSocket, Hyperliquid `allMids`
- 최소 소스 수: `FAIR_PRICE_MIN_SOURCES=2`
- 기본 공정가: 살아 있는 소스들의 median
- 한 소스가 멈추거나 튀어도 median과 source age/deviation guard로 방어한다.

EC2 확인:

```bash
cd ~/monk_trading_bot
source venv/bin/activate
python -m backend.scripts.check_fair_price
python -m backend.scripts.check_fair_price --json
```

Variational browser gate 요청 생성:

```bash
python -m backend.scripts.create_variational_browser_request \
  --direction LONG_BTC_SHORT_ETH \
  --size-usd 50
```

생성된 요청은 `tools/variational-browser/runtime/requests/*.json`에 저장된다. 기본값은 두 다리 모두 생성한다.

- `LONG_BTC_SHORT_ETH` → BTC Buy 요청 + ETH Sell 요청
- `SHORT_BTC_LONG_ETH` → BTC Sell 요청 + ETH Buy 요청

각 요청의 `variationalOrder.quantity`는 Binance/Lighter/Hyperliquid median fair price 기준으로 `size_usd / fair_price`를 계산한다. 기본값은 dry-run이고, 두 다리를 텔레그램으로 순차 승인할 수 있도록 생성 요청에는 기본 `maxAgeSec=300`, `approvalTimeoutMs=120000`을 넣는다. 요청 유효 시간은 `VARIATIONAL_REQUEST_MAX_AGE_SEC` 또는 `--max-age-sec`, 텔레그램 승인 대기 시간은 `VARIATIONAL_BROWSER_APPROVAL_TIMEOUT_SEC` 또는 `--approval-timeout-sec`로 조정한다.

청산 요청은 원래 진입 방향을 기준으로 `--action close`를 붙인다. 이때 스크립트가 다리 방향을 자동 반전하고 `reduceOnly=true`를 넣는다. 실제 포지션 수량과 정확히 맞추려면 `--btc-quantity` / `--eth-quantity`를 사용한다.

```bash
python -m backend.scripts.create_variational_browser_request \
  --direction SHORT_BTC_LONG_ETH \
  --action close \
  --legs ETH \
  --eth-quantity 0.0235 \
  --approval-timeout-sec 180
```

단일 다리 selector만 테스트할 때:

```bash
python -m backend.scripts.create_variational_browser_request \
  --direction LONG_BTC_SHORT_ETH \
  --size-usd 50 \
  --legs BTC
```

운영 원칙:
- Variational 화면 가격은 체결 UI 확인용이다.
- 진입/청산 신호, 텔레그램 승인 요약, 주문 직전 sanity check는 외부 median fair price를 기준으로 한다.
- 3개 중 1개 소스가 응답하지 않아도 2개 이상이면 진행 가능하다.
- Browser gate는 `variationalOrder`가 있으면 symbol 페이지 이동 → Market 탭 → Buy/Sell 선택 → Size 입력 → 스크린샷 승인 순서로 처리한다.
- Size fallback은 클릭 후 실제 editable input이 포커스된 경우에만 `Ctrl+A`와 quantity 입력을 수행한다. 포커스가 body 등에 남아 있으면 페이지 전체 선택을 방지하고 request를 실패 처리한다.
- `variationalOrder.reduceOnly=true`이면 Reduce Only 체크박스를 켠 뒤 Size를 입력한다.
- 새 요청의 기본 `confirmSelector`는 `auto`다. Browser gate는 주문 패널 영역의 활성 버튼 후보를 텔레그램 메시지에 표시하고, broad selector(`body`, `html`, `*`)는 live click에서 차단한다.
- `variationalOrder` live click은 `Buy BTC`, `Sell ETH`처럼 side+symbol이 보이는 confirm button만 허용한다. 텍스트가 빈 버튼 후보는 클릭하지 않고 request를 실패 처리한다.
- `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true`이면 backend-created BTC/ETH batch 진입도 텔레그램 승인 없이 자동 클릭한다. 이 경로는 `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD` 이하의 양다리 open batch에만 적용된다. 기본값은 false다.
- `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true`이면 `action=close` + `reduceOnly=true` 요청은 텔레그램 승인 없이 자동 클릭한다.
- `VARIATIONAL_BROWSER_BATCH_REQUESTS=true`이면 BotEngine이 만드는 BTC/ETH 두 다리는 하나의 batch request 파일로 저장된다. 오픈은 한 번 승인으로 두 다리를 순차 클릭하고, 청산은 한 번의 자동 reduce-only batch로 두 다리를 순차 클릭한다. reduce-only 청산 batch에서 한 다리가 실패하면 실패 다리를 `VARIATIONAL_BROWSER_REDUCE_ONLY_BATCH_RETRY_ATTEMPTS` 횟수만큼 재시도한다.

자동 봇 연동:
- `EXECUTION_MODE=variational_browser`이면 BotEngine은 실제 거래소 API 주문을 넣지 않고, `variational_browser` 가상 포지션으로 PnL/DB를 추적한다.
- 진입 신호가 나면 `backend.bot.variational.browser_requests.VariationalBrowserRequestBridge`가 BTC/ETH 두 다리 요청 파일을 자동 생성한다.
- 청산 신호가 나면 기존 가상 포지션의 실제 추적 수량으로 `--action close`와 같은 reduce-only 청산 요청 파일을 자동 생성한다.
- 요청 파일은 `VARIATIONAL_BROWSER_REQUEST_DIR` 아래에 저장되며, `tools/variational-browser`를 `npm start -- --daemon`으로 켜두면 순차 처리된다.
- 브라우저 요청 생성 실패, 텔레그램 거절, timeout, dry-run, daemon 중단 시 가상 포지션을 열거나 닫지 않는다. BotEngine은 두 다리가 모두 `.clicked.done`이 된 뒤에만 DB/대시보드 포지션 상태를 변경한다.
- 실행 중 Settings 변경은 DB 저장 후 `BOT_CONFIG_RELOAD_INTERVAL_SEC` 주기로 hot reload된다. TP/SL, Z-score revert, min/max hold 같은 청산 조건은 열린 포지션에도 반영되고, entry z-score/divergence/size/leverage/cost 설정은 다음 진입부터 반영된다.
- `execution_mode` 또는 `primary_exchange` 변경은 실행 중 안전하게 바꾸지 않는다. 이 둘은 stop/start 또는 PM2 restart 후 적용한다.

### Phase V4: 브라우저 클릭 게이트

목적: 주문마다 지갑 서명이 발생하지 않는 경우, Playwright가 최종 주문 버튼을 누르기 전에 텔레그램 승인을 요구한다.

실행:

```bash
cd tools/variational-browser
npm install
npx playwright install chromium
cp .env.example .env
nano .env
npm start -- --open
```

운영 구조:
- Persistent browser profile에 Variational 로그인/WalletConnect 세션을 유지한다.
- 신호 봇은 `tools/variational-browser/runtime/requests/*.json` 주문 요청 파일을 만든다.
- Browser gate는 주문창 세팅 후 스크린샷을 텔레그램으로 전송한다.
- Telegram `Click` 승인 시에만 최종 주문 버튼 후보를 클릭한다.
- 기본값은 `VARIATIONAL_BROWSER_DRY_RUN=true`이므로, 처음에는 승인 후에도 클릭하지 않는다.

주의:
- `tools/variational-wallet`과 `tools/variational-browser`를 같은 Telegram bot token으로 동시에 실행하면 `getUpdates` 이벤트를 서로 가져갈 수 있다.
- 둘을 동시에 켤 때는 `VARIATIONAL_BROWSER_TELEGRAM_BOT_TOKEN`에 별도 봇 토큰을 쓰는 것을 권장한다.
- selector와 버튼 후보는 Variational UI 변경에 취약하므로, 라이브 전에는 최소 주문으로 스크린샷의 `confirm_button_candidates`를 확인한다.

WalletConnect 연결:

```bash
cd tools/variational-browser
npm start -- --connect-wallet
```

이 명령은 EC2 headless 브라우저에서 `Connect Wallet` → `WalletConnect`를 누르고, DOM/`Copy link`에서 `wc:` URI를 추출해 `runtime/walletconnect_uri.txt`에 저장한다. URI를 찾으면 브라우저 프로세스를 켜둔 채 두 번째 SSH 터미널에서 `tools/variational-wallet`에 URI를 넘겨 세션을 연결한다. `--connect-wallet` 중에는 Telegram button polling을 하지 않으므로 wallet signer의 승인 callback을 빼앗지 않는다. Variational은 세션 승인 뒤 `authenticate`/login `SIGN REQUEST`를 한 번 더 보낼 수 있으므로, 지갑 프로세스를 유지한 채 해당 서명까지 승인해야 웹 화면이 연결 상태로 유지된다. 이 인증 요청이 자동으로 오지 않는 경우 브라우저 도구가 `VARIATIONAL_BROWSER_AUTHENTICATE_SELECTORS`에 맞는 인증 버튼을 눌러 요청을 발생시킨다. 이미 세션만 연결되어 있고 인증만 남았다면 `cd tools/variational-browser && npm start -- --authenticate`로 인증 버튼만 다시 누를 수 있다. 이 인증 단계에서는 `tools/variational-wallet/.env`의 `VARIATIONAL_WC_DRY_RUN=false`가 필요하고, 주문 클릭 테스트는 별도로 `VARIATIONAL_BROWSER_DRY_RUN=true`를 유지한다.

### 2026-05-22 진행상황

현재 구현/검증된 것:

- EC2 FastAPI backend, React dashboard, nginx reverse proxy, PM2 실행 구조가 동작한다.
- `EXECUTION_MODE=variational_browser`가 추가되어, BotEngine이 거래소 API 대신 Variational browser request 파일을 생성할 수 있다.
- `tools/variational-browser`는 persistent Playwright profile로 Variational Omni 웹 세션을 유지한다.
- WalletConnect 연결은 `tools/variational-wallet`로 처리하고, Variational의 `Authenticate` 서명 요청까지 텔레그램 승인으로 처리한다.
- Browser gate는 BTC/ETH perpetual 페이지 이동, Market 탭 선택, Buy/Sell 선택, Size 입력, Reduce Only 체크, 주문 버튼 후보 탐지, Telegram screenshot approval을 수행한다.
- `confirmSelector=auto`가 기본값이며, Telegram 메시지에 `confirm_button_candidates`를 표시한다. broad selector live click은 차단한다.
- 진입 요청은 양다리로 생성된다. `LONG_BTC_SHORT_ETH`는 BTC Buy + ETH Sell, `SHORT_BTC_LONG_ETH`는 BTC Sell + ETH Buy다.
- 기본값에서는 양다리 진입 요청이 하나의 `variationalBatch` 파일에 묶인다. Browser gate는 각 다리 preview screenshot을 보낸 뒤 Telegram `Click Pair` 승인 한 번만 받고 BTC/ETH를 순차 클릭한다. 첫 다리 클릭 후 다음 다리가 실패하면 이미 클릭한 진입 다리를 즉시 reduce-only rollback하려고 시도하고, batch를 `clicked`가 아닌 `rolledback`/`partial_failed`로 archive한다.
- 청산 요청은 원래 방향을 반전하고 `reduceOnly=true`를 사용한다. BotEngine 연동 청산은 가상 포지션에 저장된 실제 BTC/ETH 수량을 사용한다.
- 청산 요청도 하나의 `variationalBatch` 파일에 묶이며, `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true` 기본값에서 자동 클릭된다. 이 경로는 모든 다리가 `action=close`와 `reduceOnly=true`를 만족할 때만 작동하며, pre/post screenshot은 텔레그램으로 남긴다. 한 청산 다리가 실패해도 다른 다리 처리를 멈추지 않고 실패 다리를 재시도한 뒤 partial failure 여부를 보고한다. `VARIATIONAL_BROWSER_DRY_RUN=true`이면 자동 경로에서도 클릭하지 않는다.
- BotEngine은 이제 browser request가 `.clicked.done`으로 archive된 것을 확인한 뒤에만 가상 포지션/DB 오픈을 기록한다.
- Browser daemon이 꺼져 있거나 Telegram 승인 timeout/거절/dry-run/실패가 발생하면 request는 `.aborted.done` 또는 해당 상태로 archive되고, 프론트엔드/DB 포지션은 열리지 않는다.
- 청산도 동일하게 `.clicked.done` 확인 후에만 가상 포지션을 닫는다. 실패하면 프론트엔드 포지션을 유지해 실제 Variational 포지션과 어긋나는 것을 막는다.
- 재시작 시 과거 `variational_browser` DB open trade 주변의 open request 파일을 확인한다. BTC/ETH 양쪽 모두 `clicked`가 아니면 `UNCONFIRMED_BROWSER_REQUEST`로 자동 종료 처리해, browser가 꺼진 상태에서 생긴 프론트엔드 전용 가상 포지션을 정리한다.
- request watcher는 `npm start -- --daemon`으로 실행하며, 만료/실패 파일은 `.expired.done` 또는 `.failed.done`으로 archive해 무한 재처리를 막는다.
- 외부 공정가는 Binance, Lighter, Hyperliquid 중 살아 있는 소스의 median을 사용한다. Variational 화면 가격은 주문 UI 확인용이며 신호 기준가로 쓰지 않는다.
- Lighter REST kline 403 문제 때문에 startup warmup은 Binance Futures klines를 우선 사용한다.
- 실행 중 Settings 변경은 `BOT_CONFIG_RELOAD_INTERVAL_SEC` 간격으로 자동 반영된다. 텔레그램에는 `[Monk] CONFIG RELOADED`가 오며, 청산 조건은 열린 포지션에, 진입 조건과 size/leverage/cost는 다음 진입부터 적용된다.
- 단일 다리 dry-run, BTC/ETH 다리 dry-run, Telegram 승인 후 live click, reduce-only close click이 소액 테스트에서 동작 확인되었다.

현재 운영 흐름:

```bash
# 1) Browser watcher는 계속 켜둔다.
cd ~/monk_trading_bot/tools/variational-browser
npm start -- --daemon

# 2) Backend를 최신 코드로 실행한다.
cd ~/monk_trading_bot
pm2 restart monk-api --update-env

# 3) Dashboard에서 Execution Mode를 Variational Browser로 두고 Run Swing을 누른다.
#    또는 API/UI로 bot start를 호출한다.
```

정상 로그 예:

```text
Config: size=$500, leverage=3x, execution=variational_browser, telegram=True
Warmup: using binance klines
Warmup complete from binance: ...
[Variational Browser] request watcher started
```

관련 안전 설정:

```env
VARIATIONAL_BROWSER_COMPLETION_TIMEOUT_SEC=360
VARIATIONAL_BROWSER_COMPLETION_POLL_SEC=1
VARIATIONAL_BROWSER_RECONCILE_WINDOW_SEC=600
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=false
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD=100
VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true
VARIATIONAL_BROWSER_BATCH_REQUESTS=true
BOT_CONFIG_RELOAD_INTERVAL_SEC=15
```

남은 주의점:

- Telegram 승인 timeout, 거절, browser daemon 중단은 자동 방어한다. 다만 사용자가 Variational 웹에서 직접 주문/청산하거나, UI 변경으로 잘못된 버튼 후보가 탐지되면 대시보드 가상 포지션과 실제 Variational 포지션이 어긋날 수 있다.
- 따라서 live 테스트는 계속 소액으로 진행하고, Telegram screenshot의 `symbol`, `side`, `quantity`, `reduceOnly`, `confirm_button_candidates`를 확인해야 한다.
- `tools/variational-wallet`과 `tools/variational-browser`가 같은 Telegram bot token으로 동시에 polling하면 callback을 서로 가져갈 수 있다. 가능하면 브라우저 승인용 bot token을 분리한다.
- Variational 공식 trading API가 생기면 browser click gate는 제거하고 API execution adapter로 교체하는 것이 최종 목표다.

### English Explanation: Trading Without a Variational API

Variational does not currently expose a public trading API, so this project uses a controlled browser-execution bridge instead of direct API order placement.

The trading signal engine still runs server-side. It receives live BTC/ETH prices, builds the pair-trading signal, and calculates order size from an external fair price. That fair price is the median of independent venues such as Binance, Lighter, and Hyperliquid, so Variational's on-screen price is not used as the decision source.

When the bot wants to trade on Variational, it does not send an order to an exchange API. Instead, it writes a local request file describing the intended leg: symbol, side, quantity, reduce-only flag, fair price snapshot, and expiration time. A separate Playwright browser process watches this request directory. That browser keeps a persistent Variational web session open on the EC2 instance.

For each pair entry, the backend writes one batch request containing both BTC and ETH legs. The browser opens each Variational market page, selects Market order mode, chooses Buy or Sell, fills the size field, and sends preview screenshots for both legs. In the default safer mode, it asks for one Telegram approval for the full pair before clicking. In fully automated mode, `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true` skips that approval for backend-created pair batches under `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD`. If the first leg clicks and a later leg fails, the browser attempts an immediate reduce-only rollback of the already-clicked entry leg and does not report the batch as successfully clicked.

Close requests are different. They are generated as a batch of reduce-only orders with the exact tracked quantities. Because they reduce exposure rather than create new exposure, the browser can auto-click the batch when `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true`, every leg has `action=close`, and every leg has `reduceOnly=true`. The browser still sends pre-click and post-click screenshots to Telegram for audit, dry-run mode still skips the click, and failed reduce-only legs are retried before the daemon reports a partial close failure.

WalletConnect is used only to keep the Variational web session authenticated. In our testing, Variational did not require a new wallet signature for every order after the web session was authenticated. Because of that, the key approval point for each trade is the Telegram-controlled final browser click, not a per-order blockchain signature.

The system also supports closing positions by generating reduce-only browser requests with the exact tracked quantities. This means the backend can maintain virtual trade/PnL records while the browser bridge performs the actual Variational UI actions. The main risk is that browser automation depends on the website UI staying stable, so every live action is guarded by screenshots, Telegram approval, short expirations, request archival, and small-size testing.

---

*NFA. 전략 구현 참고 목적. 실제 거래 시 충분한 테스트 후 소액부터 시작하세요.*
