"""
거래소 커넥터 추상 베이스 클래스.

모든 거래소 커넥터(Backpack, Pacifica, Extended, Lighter)는
이 클래스를 상속받아 구현해야 합니다.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional


class OrderSide(str, Enum):
    """주문 방향."""
    BUY = "Buy"
    SELL = "Sell"


class OrderType(str, Enum):
    """주문 타입."""
    MARKET = "Market"
    LIMIT = "Limit"


class PositionSide(str, Enum):
    """포지션 방향."""
    LONG = "Long"
    SHORT = "Short"


@dataclass
class Ticker:
    """시세 정보."""
    symbol: str
    last_price: float
    bid_price: float
    ask_price: float
    volume_24h: float
    high_24h: float
    low_24h: float
    timestamp: int


@dataclass
class Balance:
    """잔고 정보."""
    asset: str
    available: float
    locked: float

    @property
    def total(self) -> float:
        return self.available + self.locked


@dataclass
class OrderResult:
    """주문 결과."""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    quantity: float
    price: Optional[float] = None
    filled_quantity: float = 0.0
    avg_fill_price: float = 0.0
    status: str = ""
    raw: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Position:
    """포지션 정보."""
    symbol: str
    side: PositionSide
    size: float
    entry_price: float
    mark_price: float
    unrealized_pnl: float
    leverage: float = 1.0
    liquidation_price: Optional[float] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class BaseExchange(ABC):
    """
    거래소 커넥터 추상 베이스 클래스.

    페어 트레이딩 봇에 필요한 최소 인터페이스를 정의합니다:
    - 시세 조회 (ticker, klines)
    - 주문 실행/취소
    - 포지션 조회/청산
    - 잔고 조회
    """

    name: str = "base"

    # ── 시세 데이터 ──────────────────────────────────────────

    @abstractmethod
    async def get_ticker(self, symbol: str) -> Ticker:
        """심볼의 현재 시세를 조회합니다."""

    @abstractmethod
    async def get_tickers(self, symbols: List[str]) -> Dict[str, Ticker]:
        """여러 심볼의 시세를 한번에 조회합니다."""

    @abstractmethod
    async def get_klines(
        self,
        symbol: str,
        interval: str,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """
        캔들스틱(K-line) 데이터를 조회합니다.

        Returns:
            List of dicts with keys:
            open_time, open, high, low, close, volume, close_time
        """

    @abstractmethod
    async def get_orderbook(
        self, symbol: str
    ) -> Dict[str, List[List[float]]]:
        """
        오더북을 조회합니다.

        Returns:
            {"bids": [[price, qty], ...], "asks": [[price, qty], ...]}
        """

    # ── 주문 ────────────────────────────────────────────────

    @abstractmethod
    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        """주문을 실행합니다."""

    @abstractmethod
    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        """주문을 취소합니다. 성공 시 True."""

    @abstractmethod
    async def cancel_all_orders(self, symbol: str) -> int:
        """심볼의 모든 미체결 주문을 취소합니다. 취소된 주문 수를 반환."""

    @abstractmethod
    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderResult]:
        """미체결 주문 목록을 조회합니다."""

    # ── 포지션 ──────────────────────────────────────────────

    @abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """심볼의 현재 포지션을 조회합니다. 포지션이 없으면 None."""

    @abstractmethod
    async def get_positions(self) -> List[Position]:
        """모든 오픈 포지션을 조회합니다."""

    @abstractmethod
    async def close_position(self, symbol: str) -> Optional[OrderResult]:
        """심볼의 포지션을 시장가로 전량 청산합니다."""

    @abstractmethod
    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        """레버리지를 설정합니다."""

    # ── 계정 ────────────────────────────────────────────────

    @abstractmethod
    async def get_balances(self) -> List[Balance]:
        """계정 잔고를 조회합니다."""

    @abstractmethod
    async def get_balance(self, asset: str) -> Optional[Balance]:
        """특정 자산의 잔고를 조회합니다."""

    # ── 유틸리티 ────────────────────────────────────────────

    def perp_symbol(self, base: str) -> str:
        """
        기초자산 코드(BTC, ETH)를 거래소별 Perp 심볼로 변환합니다.
        하위 클래스에서 오버라이드하세요.
        """
        raise NotImplementedError

    async def ping(self) -> bool:
        """거래소 연결 상태를 확인합니다. 기본 구현은 True를 반환합니다."""
        return True

    def __repr__(self) -> str:
        return f"<{self.__class__.__name__} ({self.name})>"
