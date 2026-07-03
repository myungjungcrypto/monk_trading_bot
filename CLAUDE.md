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
- `variationalOrder.reduceOnly=true`이면 Reduce Only 체크박스를 켠 뒤 Size를 입력한다. live reduce-only 클릭은 `VARIATIONAL_BROWSER_REQUIRE_REDUCE_ONLY_CHECKED=true` 기본값에서 체크박스가 실제 checked 상태로 검증될 때만 허용한다. 검증 불가/disabled/unchecked이면 최종 주문 버튼을 누르지 않는다.
- 새 요청의 기본 `confirmSelector`는 `auto`다. Browser gate는 주문 패널 영역의 활성 버튼 후보를 텔레그램 메시지에 표시하고, broad selector(`body`, `html`, `*`)는 live click에서 차단한다.
- `variationalOrder` live click은 `Buy BTC`, `Sell ETH`처럼 side+symbol이 보이는 confirm button만 허용한다. 텍스트가 빈 버튼 후보는 클릭하지 않고 request를 실패 처리한다.
- `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true`이면 backend-created BTC/ETH batch 진입도 텔레그램 승인 없이 자동 클릭한다. 이 경로는 `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD` 이하의 양다리 open batch에만 적용된다. 기본값은 false다.
- `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true`이면 `action=close` + `reduceOnly=true` 요청은 텔레그램 승인 없이 자동 클릭한다.
- `VARIATIONAL_BROWSER_BATCH_REQUESTS=true`이면 BotEngine이 만드는 BTC/ETH 두 다리는 하나의 batch request 파일로 저장된다. 오픈은 한 번 승인으로 두 다리를 순차 클릭하고, 청산은 한 번의 자동 reduce-only batch로 두 다리를 순차 클릭한다. reduce-only 청산 batch에서 한 다리가 실패하면 실패 다리를 `VARIATIONAL_BROWSER_REDUCE_ONLY_BATCH_RETRY_ATTEMPTS` 횟수만큼 재시도한다.
- reduce-only fallback 좌표는 다른 체크박스(TP/SL 등)를 누를 수 있으므로 기본 비활성화했다. selector로 체크 상태를 확인하지 못하면 청산 클릭은 실패 처리된다.

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

이 명령은 EC2 headless 브라우저에서 `Connect Wallet` → `WalletConnect`를 누르고, DOM/`Copy link`에서 `wc:` URI를 추출해 `runtime/walletconnect_uri.txt`에 저장한다. PM2로 켜둔 `tools/variational-wallet`은 기본적으로 이 파일을 감시하고 새 URI가 생기면 자동으로 pairing하므로, 정상 운영에서는 두 번째 SSH 터미널에 `npm start -- --uri 'wc:...'`를 붙여 넣을 필요가 없다. `--connect-wallet` 중에는 Telegram button polling을 하지 않으므로 wallet signer의 승인 callback을 빼앗지 않는다. Variational은 세션 승인 뒤 `authenticate`/login `SIGN REQUEST`를 한 번 더 보낼 수 있으므로, 지갑 프로세스를 유지한 채 해당 서명까지 승인해야 웹 화면이 연결 상태로 유지된다. 이 인증 요청이 자동으로 오지 않는 경우 브라우저 도구가 `VARIATIONAL_BROWSER_AUTHENTICATE_SELECTORS`에 맞는 인증 버튼을 눌러 요청을 발생시킨다. 이미 세션만 연결되어 있고 인증만 남았다면 `cd tools/variational-browser && npm start -- --authenticate`로 인증 버튼만 다시 누를 수 있다. 만약 이때 Variational이 `Connection to your wallet was lost` 또는 Cloudflare `Verify you are human`을 띄우면 WalletConnect SIGN REQUEST까지 도달하지 못한 것이므로 `npm start -- --reset-wallet-session` 후 `npm start -- --connect-wallet`로 처음부터 다시 연결한다. 이 인증 단계에서는 `tools/variational-wallet/.env`의 `VARIATIONAL_WC_DRY_RUN=false`가 필요하고, 주문 클릭 테스트는 별도로 `VARIATIONAL_BROWSER_DRY_RUN=true`를 유지한다.

### 2026-05-22 진행상황

현재 구현/검증된 것:

- EC2 FastAPI backend, React dashboard, nginx reverse proxy, PM2 실행 구조가 동작한다.
- `EXECUTION_MODE=variational_browser`가 추가되어, BotEngine이 거래소 API 대신 Variational browser request 파일을 생성할 수 있다.
- `tools/variational-browser`는 persistent Playwright profile로 Variational Omni 웹 세션을 유지한다.
- WalletConnect 연결은 `tools/variational-wallet`로 처리하고, Variational의 `Authenticate` 서명 요청까지 텔레그램 승인으로 처리한다.
- Browser gate는 BTC/ETH perpetual 페이지 이동, Market 탭 선택, Buy/Sell 선택, Size 입력, Reduce Only 체크, 주문 버튼 후보 탐지, Telegram screenshot approval을 수행한다.
- `confirmSelector=auto`가 기본값이며, Telegram 메시지에 `confirm_button_candidates`를 표시한다. broad selector live click은 차단한다.
- 진입 요청은 양다리로 생성된다. `LONG_BTC_SHORT_ETH`는 BTC Buy + ETH Sell, `SHORT_BTC_LONG_ETH`는 BTC Sell + ETH Buy다.
- 기본값에서는 양다리 진입 요청이 하나의 `variationalBatch` 파일에 묶인다. 수동 승인 모드에서는 Browser gate가 각 다리 preview screenshot을 보낸 뒤 Telegram `Click Pair` 승인 한 번만 받고 BTC/ETH를 순차 클릭한다. 첫 다리 클릭 후 다음 다리가 실패하면 이미 클릭한 진입 다리를 즉시 reduce-only rollback하려고 시도하고, batch를 `clicked`가 아닌 `rolledback`/`partial_failed`로 archive한다.
- 완전 자동 진입 모드(`VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true`)에서는 별도 approval preview pass를 생략하고 `AUTO PAIR OPEN` 알림 뒤 바로 각 다리의 prepared-click flow로 들어간다. 예전에는 BTC/ETH preview 세팅 후 실제 클릭 때 BTC/ETH를 다시 세팅해 총 4번 패널을 만지면서 몇 분 지연될 수 있었으나, 지금은 중복 세팅과 Telegram preview upload 대기를 줄였다.
- 청산 요청은 원래 방향을 반전하고 `reduceOnly=true`를 사용한다. BotEngine 연동 청산은 가상 포지션에 저장된 실제 BTC/ETH 수량을 사용한다.
- 청산 요청도 하나의 `variationalBatch` 파일에 묶이며, `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true` 기본값에서 자동 클릭된다. 이 경로는 모든 다리가 `action=close`와 `reduceOnly=true`를 만족할 때만 작동하며, pre/post screenshot은 텔레그램으로 남긴다. 한 청산 다리가 실패해도 다른 다리 처리를 멈추지 않고 실패 다리를 재시도한 뒤 partial failure 여부를 보고한다. `VARIATIONAL_BROWSER_DRY_RUN=true`이면 자동 경로에서도 클릭하지 않는다.
- `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true` 또는 자동 reduce-only 청산 경로에서는 Telegram preview/status screenshot 전송이 best-effort다. Telegram API가 느리거나 일시적으로 `ETIMEDOUT`을 내더라도 `VARIATIONAL_BROWSER_TELEGRAM_BEST_EFFORT_TIMEOUT_SEC`까지만 기다린 뒤 request watcher와 자동 클릭 흐름은 계속 진행하고, 로그에 `[telegram] ... failed` 경고만 남긴다. 단, 수동 승인 모드는 승인 버튼을 받아야 하므로 Telegram 연결이 필요하다.
- BotEngine은 이제 browser request가 `.clicked.done`으로 archive된 것을 확인한 뒤에만 가상 포지션/DB 오픈을 기록한다.
- Browser daemon이 꺼져 있거나 Telegram 승인 timeout/거절/dry-run/실패가 발생하면 request는 `.aborted.done` 또는 해당 상태로 archive되고, 프론트엔드/DB 포지션은 열리지 않는다.
- 청산은 `.clicked.done` 확인 후에 가상 포지션을 닫는다. 단, 사용자가 Variational 웹에서 이미 수동 청산해서 실제 포지션이 없어진 경우에는 Reduce Only 체크박스가 사라지거나 비활성화될 수 있다. 이때 browser gate가 해당 심볼의 포지션 없음(`No positions`, `Current Position -/0`, 포지션 행 부재)을 확인하면 request를 `.external_closed.done`으로 archive하고, BotEngine은 DB/메모리 포지션을 `EXTERNAL_MANUAL_CLOSE`로 자동 reconcile한다. 이 처리는 새 주문을 클릭하지 않는다.
- 재시작 시 과거 `variational_browser` DB open trade 주변의 open request 파일을 확인한다. BTC/ETH 양쪽 모두 `clicked`가 아니면 `UNCONFIRMED_BROWSER_REQUEST`로 자동 종료 처리해, browser가 꺼진 상태에서 생긴 프론트엔드 전용 가상 포지션을 정리한다.
- request watcher는 `npm start -- --daemon`으로 실행하며, 만료/실패 파일은 `.expired.done` 또는 `.failed.done`으로 archive해 무한 재처리를 막는다.
- 외부 공정가는 Binance, Lighter, Hyperliquid 중 살아 있는 소스의 median을 사용한다. Variational 화면 가격은 주문 UI 확인용이며 신호 기준가로 쓰지 않는다.
- Lighter REST kline 403 문제 때문에 startup warmup은 Binance Futures klines를 우선 사용한다.
- 실행 중 Settings 변경은 `BOT_CONFIG_RELOAD_INTERVAL_SEC` 간격으로 자동 반영된다. 텔레그램에는 `[Monk] CONFIG RELOADED`가 오며, 청산 조건은 열린 포지션에, 진입 조건과 size/leverage/cost는 다음 진입부터 적용된다.
- 대시보드 Emergency Kill Switch가 추가되었다. 활성화하면 backend bot을 즉시 stop하고, 새 start/auto-resume을 차단하며, Variational browser daemon은 request 처리를 pause하고 최종 click 직전에도 kill switch 파일을 다시 확인한다.
- 단일 다리 dry-run, BTC/ETH 다리 dry-run, Telegram 승인 후 live click, reduce-only close click이 소액 테스트에서 동작 확인되었다.
- 2026-05-23 live 테스트에서 실제 Variational 주문은 체결됐지만 backend가 request를 `aborted`로 판단해 DB/프론트 포지션을 열지 않는 race가 확인되었다. 원인은 backend completion timeout 시점에 원본 `.json`을 `.aborted.done`으로 rename했지만, browser daemon이 이미 파일을 읽고 UI 클릭을 진행 중이면 실제 주문은 계속 들어갈 수 있었기 때문이다. 이후 browser가 처리 시작 즉시 `.json.processing`으로 claim하고 backend는 processing request를 abort하지 않도록 수정했다.

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
VARIATIONAL_BROWSER_PROCESSING_TIMEOUT_SEC=900
VARIATIONAL_BROWSER_COMPLETION_POLL_SEC=1
VARIATIONAL_BROWSER_RECONCILE_WINDOW_SEC=600
VARIATIONAL_BROWSER_KILL_SWITCH_PATH=tools/variational-browser/runtime/kill_switch.json
VARIATIONAL_BROWSER_TELEGRAM_BEST_EFFORT_TIMEOUT_SEC=3
VARIATIONAL_BROWSER_POLL_TELEGRAM=true
VARIATIONAL_BROWSER_REQUEST_AUTO_AUTHENTICATE=true
VARIATIONAL_BROWSER_AUTHENTICATE_FALLBACK_ENABLED=true
VARIATIONAL_BROWSER_AUTHENTICATE_FALLBACK_POINT=0.377,0.795
VARIATIONAL_BROWSER_ENTRY_RETRY_COOLDOWN_SEC=120
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=false
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD=100
VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true
VARIATIONAL_BROWSER_BATCH_REQUESTS=true
VARIATIONAL_BROWSER_REQUIRE_REDUCE_ONLY_CHECKED=true
VARIATIONAL_BROWSER_REDUCE_ONLY_FALLBACK_ENABLED=false
VARIATIONAL_BROWSER_CLOSE_RETRY_COOLDOWN_SEC=120
BOT_CONFIG_RELOAD_INTERVAL_SEC=15
```

완전 자동 소액 live 테스트 설정 예:

```env
VARIATIONAL_BROWSER_DRY_RUN=false
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true
VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD=600
VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true
VARIATIONAL_BROWSER_BATCH_REQUESTS=true
VARIATIONAL_BROWSER_CONFIRM_SELECTOR=auto
VARIATIONAL_BROWSER_REQUIRE_REDUCE_ONLY_CHECKED=true
VARIATIONAL_BROWSER_REDUCE_ONLY_FALLBACK_ENABLED=false
VARIATIONAL_BROWSER_TELEGRAM_BEST_EFFORT_TIMEOUT_SEC=3
# 같은 Telegram bot token을 wallet/browser가 공유하면 full-auto에서는 false 권장.
VARIATIONAL_BROWSER_POLL_TELEGRAM=false
```

자동 진입 정상 로그 흐름:

```text
[Monk] ENTRY SIGNAL
[Monk] STATUS
Variational Browser entry requests queued
[Variational Browser] processing batch request: ... legs=2
[Variational Browser] AUTO PAIR OPEN
[Variational Browser] prepared click ... leg: BTC
[Variational Browser] clicked ... leg: BTC
[Variational Browser] prepared click ... leg: ETH
[Variational Browser] clicked ... leg: ETH
[Monk] STATUS
Variational Browser entry clicks confirmed
[Monk] OPENED
```

주의: 자동 모드에서는 예전처럼 `PAIR LEG PREVIEW` 두 장이 먼저 오래 뜨는 흐름이 없어야 한다. 그 로그가 계속 보이면 EC2가 최신 코드를 pull/restart하지 않았거나, 수동 승인 모드로 실행 중인 것이다.

Emergency Kill Switch:

- Dashboard의 `Kill Switch`는 `/api/bot/kill-switch`를 호출해 `tools/variational-browser/runtime/kill_switch.json`을 활성화한다.
- 활성 상태에서는 FastAPI가 bot start를 `423`으로 거부하고, backend 재시작 시 open trade auto-resume도 건너뛴다.
- Variational browser daemon은 kill switch가 active이면 새 request 파일 처리를 멈추고, 이미 클릭 직전인 경로도 `clickConfirm()`에서 한 번 더 차단한다.
- `Reset Kill`은 kill switch만 해제한다. 봇은 자동 재시작하지 않으므로, 실제 Variational 포지션 상태를 확인한 뒤 수동으로 다시 `Run Swing`/start 해야 한다.
- Kill switch를 누르는 순간 이미 첫 다리 click이 끝난 상태라면 두 번째 다리 click은 막힐 수 있다. 이 경우 실제 Variational 포지션을 먼저 확인하고 한쪽 노출이 남았으면 수동 정리한다.
- 실제 포지션이 열려 있고 close 조건을 기다리지 않고 reduce-only close 경로를 테스트하려면 대시보드의 열린 `variational_browser` 포지션에서 `Close Now`를 누르거나 `/api/bot/manual-close`를 호출한다. 이 경로는 BotEngine의 정상 close 처리와 동일하게 browser close request를 만들고 `.clicked.done` 확인 뒤에만 가상/DB 포지션을 닫는다. `force=true`는 이전 실패로 걸린 close retry cooldown만 즉시 해제하며, 실제 주문은 여전히 browser daemon의 reduce-only 안전장치를 통과해야 한다.
- pair close가 부분 성공해 실제 Variational 포지션이 BTC/ETH 한쪽만 남거나 dust만 남은 상태에서는 대시보드 `Force Flatten` 또는 `/api/bot/force-flatten-variational`을 사용한다. 이 경로는 backend의 추적 수량을 쓰지 않고 browser daemon이 Variational Positions 테이블의 `Close All` 컨트롤을 클릭하는 emergency flatten request를 큐에 넣는다. 새 진입/일반 요청은 kill switch에서 멈추지만 `Force Flatten`은 emergency close-all 목적이라 kill switch 중에도 처리된다. 실제 포지션을 화면 기준으로 모두 닫는 목적이므로, 실행 후 DB/대시보드에 open이 남아 있으면 `Clear External`로 reconcile한다.
- 실제 Variational 포지션을 사용자가 웹에서 이미 수동 청산했는데 대시보드/DB에만 `OPEN`이 남으면 `Clear External` 버튼 또는 `/api/bot/reconcile-external-close`를 사용한다. 이 경로는 새 주문을 내지 않고 `variational_browser` DB trade와 BotEngine 메모리 포지션만 `EXTERNAL_MANUAL_CLOSE`로 닫는다. 실제 포지션이 없는 것을 먼저 확인한 뒤에만 사용한다.
- 자동 close 중 Reduce Only를 켤 수 없고 화면상 실제 포지션도 없으면 browser daemon은 이를 실패 재시도가 아니라 `external_closed`로 분류한다. Backend는 이 상태를 받아 같은 `EXTERNAL_MANUAL_CLOSE` reconcile 경로를 자동 실행하므로, 이미 수동 청산된 포지션 때문에 close request가 반복 생성되는 루프를 막는다. Reduce Only 설정 실패 시에는 원인 분석용 실패 screenshot도 Telegram으로 보낸다.
- request 처리 중 Variational 화면이 `auth_required` 상태이면 browser daemon은 `VARIATIONAL_BROWSER_REQUEST_AUTO_AUTHENTICATE=true` 기본값에 따라 Authenticate/Login 버튼을 한 번 자동 클릭하고 ready 상태를 다시 기다린다. 버튼 selector가 보이지 않고 `wallet_prompt_visible=true`만 보이면 `VARIATIONAL_BROWSER_AUTHENTICATE_FALLBACK_ENABLED=true`에서 `VARIATIONAL_BROWSER_AUTHENTICATE_FALLBACK_POINT`를 클릭한다. 이 fallback은 인증 prompt용이며 주문 confirm fallback과 별개다. WalletConnect SIGN REQUEST를 처리하려면 `tools/variational-wallet` signer가 살아 있어야 한다. 그래도 entry가 실패하면 BotEngine은 `VARIATIONAL_BROWSER_ENTRY_RETRY_COOLDOWN_SEC` 동안 새 entry request 생성을 억제해 같은 신호에서 실패 request가 연속 생성되는 것을 막는다.
- CDP로 붙은 Chrome 탭/브라우저가 닫히거나 crash되면 Playwright는 `Target page, context or browser has been closed`, `Target closed`, 또는 `Page crashed`를 낸다. Browser daemon은 이 오류를 일반 `failed`가 아니라 `browser_unavailable`로 archive하고 PM2 재시작을 위해 종료한다. Backend는 `browser_unavailable` completion을 받으면 가상/DB 포지션을 닫지 않고 `VARIATIONAL_BROWSER_INFRA_RETRY_COOLDOWN_SEC`(기본 900초) 동안 entry/close 재시도를 억제한다. 이 상태에서는 Chrome을 `--remote-debugging-port=9222`로 다시 띄우고 `pm2 restart variational-browser --update-env`를 실행해야 한다. PM2 `variational-chrome`을 쓰는 운영이면 `pm2 restart variational-chrome --update-env` 후 `pm2 restart variational-browser --update-env` 순서로 복구한다.
- Variational 화면이 `disconnected`/`auth_required`/human verification 상태라 주문 전 wallet ready 검사를 통과하지 못하면 browser daemon은 request를 `wallet_unavailable`로 archive한다. Backend는 이를 Chrome 장애와 같은 인프라성 실패로 보고 `VARIATIONAL_BROWSER_INFRA_RETRY_COOLDOWN_SEC` 동안 entry/close 재시도를 억제한다. 실제 주문 click 전 실패이므로 가상/DB 포지션은 생성하거나 닫지 않는다. 복구는 `variational-wallet`을 켠 상태에서 `tools/variational-browser`의 `--connect-wallet`/`--authenticate`/필요 시 `--reset-wallet-session` 순서로 진행한다.
- `VARIATIONAL_BROWSER_REQUEST_AUTO_RECONNECT_WALLET=true`이면 request 처리 중 `disconnected`/`auth_required`를 만나도 즉시 실패하지 않고 browser daemon이 `Connect Wallet`/WalletConnect URI handoff/Authenticate를 자동 시도한다. 복구가 끝난 뒤 `validateRequest()`를 다시 호출하므로 reconnect가 오래 걸려 `maxAgeSec`을 넘긴 entry request는 클릭하지 않고 expired 처리된다.
- 2026-06-04/05 reconnect drill: Variational이 disconnected 상태가 되었을 때 `npm start -- --connect-wallet`은 WalletConnect modal을 열고 `Copy link` 버튼/clipboard/shadow-DOM fallback으로 `wc:` URI를 찾아 `tools/variational-browser/runtime/walletconnect_uri.txt`에 저장한다. `variational-wallet`이 이 URI를 pair하고 Telegram에서 session/sign 승인을 받으면 browser 쪽은 다시 Authenticate/Login을 눌러 ready까지 진행한다. QR만 보이고 DOM에 URI가 없던 문제는 Copy link 경로로 해결했다.
- WalletConnect session 승인 직후 Variational 화면은 지갑 주소와 order panel이 보여도 `Authenticate` 버튼이 남아 있을 수 있다. 이 상태를 ready로 오판하면 주문 단계에서 다시 `auth_required`가 나므로, wallet state 판정은 `Authenticate` visible 신호를 ready 신호보다 우선한다. 즉 `Authenticate`가 보이면 먼저 login `personal_sign`을 처리해야 한다.
- 완전 hands-off 복구를 하려면 `variational-wallet`이 상시 켜져 있어야 하고, 자동 승인 범위는 엄격히 제한한다. `VARIATIONAL_WC_AUTO_APPROVE_VARIATIONAL_SESSION=true`는 peer URL이 `https://omni.variational.io`인 WalletConnect session proposal만 자동 승인한다. `VARIATIONAL_WC_AUTO_APPROVE_VARIATIONAL_LOGIN=true`는 Variational 로그인용 `personal_sign`만 자동 승인하며, 메시지의 URI가 `https://omni.variational.io/api/auth/login`, chain이 Arbitrum One, signer가 설정 지갑 주소, 만료가 `VARIATIONAL_WC_AUTO_APPROVE_LOGIN_MAX_EXPIRY_SEC` 이내인지 확인한다. 주문/송금/typed-data/transaction 계열 method는 계속 Telegram 승인 또는 기존 transaction guard를 거쳐야 하며 blanket auto-approve하지 않는다.
- 수동 월렛 재연결 중에는 PM2 `variational-browser` daemon이 같은 Chrome/CDP page를 동시에 조작하지 않도록 `pm2 stop variational-browser` 후 `npm start -- --connect-wallet`/`--authenticate`/`--status`를 실행하고, ready 확인 뒤 `pm2 restart variational-browser --update-env`로 되돌린다.
- `Authentication Error: Connection to your wallet was lost`, `Unable to load configuration data`, Cloudflare `Are you a human?`/`Verify you are human`이 보이면 browser daemon은 더 이상 단순 `auth_required`로 보지 않고 `reconnect_required` 또는 `human_verification_required`로 분류한다. 이 상태에서는 SIGN REQUEST가 wallet으로 가지 않으므로 authenticate 반복 클릭 대신 `npm start -- --reset-wallet-session`으로 Variational local/session storage, IndexedDB, cache, stale `walletconnect_uri.txt`만 지우고 cookies는 유지한 뒤 `npm start -- --connect-wallet`을 실행한다. Cloudflare challenge가 계속 보이면 noVNC/visible browser에서 사용자가 직접 통과해야 하며 자동 우회하지 않는다.
- Wallet state 판정은 부정 신호보다 긍정 신호를 우선한다. `Transfer` 버튼, `Portfolio $...`, `0x...` 지갑 주소 같은 `VARIATIONAL_BROWSER_WALLET_READY_SELECTORS`가 보이면 숨은 `Connect Wallet`/wallet prompt DOM이 남아 있어도 `ready`로 본다. selector가 쪼개진 DOM 때문에 실패하는 경우를 대비해 `document.body.innerText`/`textContent` 및 visible element text에서도 `0x...` 주소와 `Transfer`/`Available to Trade $...`/`Portfolio $...` 조합을 확인한다. 일부 연결된 화면에서는 header/account 텍스트가 Playwright text selector에 잡히지 않고 order panel만 정상화되므로, 주문 버튼의 `Enter Size` 및 `Buy/Sell BTC/ETH` 상태(`VARIATIONAL_BROWSER_WALLET_ORDER_PANEL_READY_SELECTORS`)도 locator와 visible text fallback 양쪽에서 추가 ready 신호로 본다. Variational은 연결된 화면에도 stale text node가 남을 수 있어, screenshot상 연결된 상태인데 `connect_wallet_visible=true`/`wallet_prompt_visible=true`가 같이 찍히는 오탐을 막기 위한 처리다.
- Cloudflare challenge가 visible Playwright Chromium에서도 계속 실패하면 EC2에 `google-chrome-stable`을 설치하고 같은 `runtime/profile`을 `--user-data-dir`로 열어 사람이 직접 clearance를 받는다. 이후 `VARIATIONAL_BROWSER_EXECUTABLE_PATH=/usr/bin/google-chrome-stable`을 설정하면 Playwright도 bundled headless shell 대신 system Chrome binary를 사용한다. noVNC/X11 운영에서는 `VARIATIONAL_BROWSER_HEADLESS=false`로 system Chrome을 headful로 유지할 수 있고, headless 운영으로 되돌릴 때는 Cloudflare clearance가 유지되는지 반드시 `--status`로 확인한다.
- System Chrome을 Playwright가 새로 launch할 때도 Cloudflare가 계속 challenge를 걸면, 사람이 직접 띄운 Chrome을 `--remote-debugging-port=9222`로 유지하고 `VARIATIONAL_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222`로 붙는다. 이 모드에서는 Playwright가 새 브라우저를 만들지 않고 이미 열린 Chrome의 default context/page를 사용하며, 종료 시 Chrome을 닫지 않는다. 장기 운영은 noVNC/Xvfb 위에서 이 Chrome을 상시 유지하는 구성이 가장 안정적이다.
- SSH/XQuartz 터미널에서 직접 띄운 Chrome은 세션이 끊기면 같이 죽을 수 있으므로 장기 운영에서는 `variational-chrome` PM2 app을 사용한다. `tools/variational-browser`의 `npm run start:chrome`은 `google-chrome-stable`을 `--remote-debugging-port=9222`와 shared `runtime/profile`로 실행하고, 화면 없는 EC2에서는 `VARIATIONAL_CHROME_AUTO_XVFB=true`일 때 Xvfb display를 같이 띄운다. `.env`에는 `VARIATIONAL_BROWSER_CDP_ENDPOINT=http://127.0.0.1:9222`를 설정하고 `pm2 start ecosystem.config.json --only variational-chrome --update-env` 후 `pm2 restart variational-browser --update-env`로 browser daemon을 붙인다.
- CDP attach 모드에서는 사람이 띄운 Chrome 창 크기가 작으면 Telegram screenshot도 1440x768처럼 잘려 Positions row가 보이지 않을 수 있다. Browser gate는 startup 및 screenshot 직전에 `VARIATIONAL_BROWSER_VIEWPORT`(기본 `1440x1200`)를 `page.setViewportSize()`로 다시 적용해 포지션 테이블까지 보이도록 한다. 그래도 잘리면 EC2에서 Chrome을 열 때 `--window-size=1440,1200`도 같이 준다.
- Variational size 입력은 일반 input이 아니라 hidden/커스텀 입력이 섞여 있다. Browser gate는 `VARIATIONAL_BROWSER_SIZE_INPUT_SELECTORS`의 첫 번째 locator만 기다리지 않고 모든 visible 후보와 frame을 순회해서 채우며, selector 입력이 실패하면 단일 좌표가 아니라 size 값 영역 주변 여러 fallback point를 시도한다. fallback이 body만 focus하면 `Ctrl+A`로 페이지 전체를 선택하지 않도록 계속 거부하고 다음 point를 시도한다.
- Reduce-only close 수량은 backend가 기억하는 요청 수량과 실제 Variational 체결 포지션 수량이 미세하게 다를 수 있다. Browser gate는 close 직전에 화면의 `Current Position`/포지션 테이블 수량을 읽고, 입력 수량을 `abs(current_position) - 최소 단위` 이하로 cap한다. 화면 수량을 파싱하지 못하면 `VARIATIONAL_BROWSER_REDUCE_ONLY_QUANTITY_SAFETY_BPS`(기본 1bp)만큼 줄인 수량을 사용해 `Reduce-only size must be smaller than the position`으로 confirm 버튼이 비활성화되는 상황을 피한다.
- `variational-wallet`은 수동 실행이 아니라 PM2 상시 프로세스로 켜두는 것을 권장한다. 이 프로세스는 `VARIATIONAL_WC_PAIRING_URI_FILE=tools/variational-browser/runtime/walletconnect_uri.txt`를 감시하고, browser가 새 `wc:` URI를 저장하면 자동으로 WalletConnect pairing을 시작한다. 같은 Telegram bot token을 browser와 공유하는 full-auto 구성에서는 browser daemon의 `VARIATIONAL_BROWSER_POLL_TELEGRAM=false`로 두어 wallet signer가 `wc:` 승인 callback을 가져가게 한다. 브라우저는 이 상태에서도 Telegram status/screenshot 전송은 할 수 있지만, 수동 승인 버튼 polling은 하지 않는다. 현재 운영처럼 wallet/backend와 browser가 서로 다른 Telegram bot token을 쓰면 browser polling을 켜둬도 된다.

```bash
cd ~/monk_trading_bot/tools/variational-wallet
pm2 start npm --name variational-wallet -- start
pm2 save
```

Dashboard HTTPS:

- `http://43.201.222.151/`처럼 IP로 접속하는 현재 구성은 TLS가 없어서 로그인 비밀번호와 JWT가 평문으로 지나간다. 공용 Wi-Fi나 신뢰할 수 없는 네트워크에서는 보안상 취약하다.
- 제대로 운영하려면 도메인 또는 서브도메인을 EC2 IP로 연결하고, nginx + Let's Encrypt/Certbot으로 HTTPS 인증서를 발급한 뒤 HTTP를 HTTPS로 redirect해야 한다. 일반적인 Let's Encrypt 인증서는 bare IP 주소에는 발급되지 않는다.
- 도메인을 붙이기 전 임시 운영에서는 Security Group에서 80 포트를 본인 고정 IP/VPN/Cloudflare Tunnel로 제한하는 편이 안전하다.

남은 주의점:

- Telegram 승인 timeout, 거절, browser daemon 중단은 자동 방어한다. 다만 사용자가 Variational 웹에서 직접 주문/청산하거나, UI 변경으로 잘못된 버튼 후보가 탐지되면 대시보드 가상 포지션과 실제 Variational 포지션이 어긋날 수 있다.
- Browser daemon은 request 처리 시작 시 파일을 `.json.processing`으로 claim한다. Backend는 `.processing` 상태의 요청을 abort로 rename하지 않고 `VARIATIONAL_BROWSER_PROCESSING_TIMEOUT_SEC` 동안 최종 `.clicked.done`/실패 marker를 기다린다. 이는 browser가 이미 클릭 중인데 backend가 먼저 포기해서 실제 포지션만 생기는 race를 줄이기 위한 장치다.
- 대시보드/DB에는 포지션이 없는데 Variational에는 실제 포지션이 있는 상태가 보이면, 새 진입이 중복으로 나갈 수 있으므로 먼저 `pm2 stop monk-api`로 backend를 멈추고 실제 Variational 포지션을 수동 정리한 뒤 재시작한다.
- 따라서 live 테스트는 계속 소액으로 진행하고, Telegram screenshot의 `symbol`, `side`, `quantity`, `reduceOnly`, `confirm_button_candidates`를 확인해야 한다.
- `tools/variational-wallet`과 `tools/variational-browser`가 같은 Telegram bot token으로 동시에 polling하면 callback을 서로 가져갈 수 있다. 가능하면 브라우저 승인용 bot token을 분리한다.
- 2026-06-18: UI click 방식은 BTC leg와 ETH leg 사이에 수십 초~수분 지연이 생겨 실시간 hedge 품질이 낮다. Direct API 전환은 현재 운영 경로를 덮어쓰지 않고, 먼저 `VARIATIONAL_BROWSER_NETWORK_CAPTURE_ENABLED=true`로 기존 browser request 처리 중 Variational same-origin HTTP/WS 요청을 `tools/variational-browser/runtime/network-captures/*.ndjson`에 기록해 주문 payload, auth/session, ack 흐름을 확인하는 단계부터 진행한다. 기본값은 꺼짐이며, headers/response body도 기본 redacted/off다. 실제 주문 시그널을 마냥 기다리지 않기 위해 `backend.scripts.create_variational_browser_request --batch --no-dry-run --size-usd 25`처럼 아주 작은 수동 batch 주문을 생성해 캡처 샘플을 적극적으로 확보할 수 있다. 충분한 캡처 샘플을 확보한 뒤 `variational_api_shadow` 같은 별도 실행 모드에서 dry-run/compare를 거쳐 live direct executor로 넘어간다.
- 2026-07-03: Variational network capture 파일이 수백 MB까지 커지고 `/api/quotes/indicative` 같은 noisy endpoint가 대부분을 차지한다. `backend.scripts.analyze_variational_capture`를 추가해 endpoint 빈도, request processing window, non-noisy POST, 주문/포지션 관련 WebSocket sent frame 후보만 요약한다. 캡처 원본 전체를 공유하지 말고 이 스크립트 출력 중 주문 후보 payload만 확인한다.
- 2026-07-03: 캡처 요약에서 `POST /api/orders/new/market`가 실제 market order submit 후보로 확인됐다. Analyzer는 `/api/quotes/simple`도 noise로 제외하고 `orders/new/market`, `orders/tpsl`, `positions`, auth endpoint의 request/response를 별도 `Important HTTP` 섹션에 출력한다.
- Variational 공식 trading API가 생기면 browser click gate는 제거하고 API execution adapter로 교체하는 것이 최종 목표다.

### English Explanation: Trading Without a Variational API

Variational does not currently expose a public trading API, so this project uses a controlled browser-execution bridge instead of direct API order placement.

The next experimental direction is not to replace the live browser path blindly. First, the browser gate can optionally capture same-origin HTTP and WebSocket traffic while processing existing request files. This capture is disabled by default and writes NDJSON under `tools/variational-browser/runtime/network-captures/`. The goal is to identify the exact order payload, session/auth requirements, acknowledgement flow, and rollback semantics before building a separate direct API executor. The current live click executor remains the fallback until the direct path proves it can open both BTC and ETH legs nearly simultaneously and handle partial failures safely.

The trading signal engine still runs server-side. It receives live BTC/ETH prices, builds the pair-trading signal, and calculates order size from an external fair price. That fair price is the median of independent venues such as Binance, Lighter, and Hyperliquid, so Variational's on-screen price is not used as the decision source.

When the bot wants to trade on Variational, it does not send an order to an exchange API. Instead, it writes a local request file describing the intended leg: symbol, side, quantity, reduce-only flag, fair price snapshot, and expiration time. A separate Playwright browser process watches this request directory. That browser keeps a persistent Variational web session open on the EC2 instance.

For each pair entry, the backend writes one batch request containing both BTC and ETH legs. The browser opens each Variational market page, selects Market order mode, chooses Buy or Sell, and fills the size field. In the default safer mode, it sends preview screenshots for both legs and asks for one Telegram approval for the full pair before clicking. In fully automated mode, `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN=true` skips that approval and skips the separate preview pass for backend-created pair batches under `VARIATIONAL_BROWSER_AUTO_CLICK_OPEN_MAX_SIZE_USD`; it goes directly to the per-leg prepared-click flow. This matters because the preview pass used to set up BTC and ETH once, then the click pass set them up again, creating avoidable delay between signal, BTC fill, and ETH fill. If the first leg clicks and a later leg fails, the browser attempts an immediate reduce-only rollback of the already-clicked entry leg and does not report the batch as successfully clicked.

Close requests are different. They are generated as a batch of reduce-only orders with the exact tracked quantities. Because they reduce exposure rather than create new exposure, the browser can auto-click the batch when `VARIATIONAL_BROWSER_AUTO_CLICK_REDUCE_ONLY=true`, every leg has `action=close`, and every leg has `reduceOnly=true`. The browser attempts to send pre-click and post-click screenshots to Telegram for audit, dry-run mode still skips the click, and failed reduce-only legs are retried before the daemon reports a partial close failure. If reduce-only cannot be enabled because the Variational screen already appears flat for that symbol, the browser archives the request as `external_closed` instead of clicking a non-reduce-only order. The backend then reconciles the virtual/DB position as `EXTERNAL_MANUAL_CLOSE`, preventing repeated close requests after a manual web close. In automatic open or reduce-only mode, Telegram audit messages are best-effort and bounded by `VARIATIONAL_BROWSER_TELEGRAM_BEST_EFFORT_TIMEOUT_SEC`: a temporary Telegram timeout is logged but should not kill the request watcher or block the automatic click path for long. Manual approval mode still depends on Telegram being reachable.

The request files are also claimed explicitly. When the browser daemon starts processing a request, it renames the file to `*.json.processing`. The backend treats that as "the browser is already acting on this request" and will not rename it to `*.aborted.done`; instead it waits up to `VARIATIONAL_BROWSER_PROCESSING_TIMEOUT_SEC` for a final `.clicked.done` or failure marker. This prevents the race where the browser is already clicking a live order but the backend gives up first and records no virtual/DB position.

WalletConnect is used only to keep the Variational web session authenticated. In our testing, Variational did not require a new wallet signature for every order after the web session was authenticated. Because of that, the key approval point for each trade is the Telegram-controlled final browser click, not a per-order blockchain signature.

The system also supports closing positions by generating reduce-only browser requests with the exact tracked quantities. This means the backend can maintain virtual trade/PnL records while the browser bridge performs the actual Variational UI actions. The main risk is that browser automation depends on the website UI staying stable, so every live action is guarded by screenshots, Telegram approval, short expirations, request archival, and small-size testing.

---

*NFA. 전략 구현 참고 목적. 실제 거래 시 충분한 테스트 후 소액부터 시작하세요.*
