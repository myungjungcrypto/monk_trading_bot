"""
Simulated Exchange — 백테스트용 모의 거래소.

BaseExchange 인터페이스를 구현하며, 실제 API 호출 없이 모의 체결합니다.
수수료/슬리피지 모델링을 포함합니다.
"""

import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from backend.bot.exchanges.base import (
    Balance,
    BaseExchange,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    PositionSide,
    TickCallback,
    Ticker,
)


@dataclass
class FeeConfig:
    """수수료/슬리피지 설정."""
    taker_fee_pct: float = 0.05   # 0.05% per leg
    slippage_pct: float = 0.01    # 0.01% slippage

    @classmethod
    def lighter(cls) -> "FeeConfig":
        return cls(taker_fee_pct=0.0, slippage_pct=0.01)

    @classmethod
    def pacifica(cls) -> "FeeConfig":
        return cls(taker_fee_pct=0.02, slippage_pct=0.01)

    @classmethod
    def extended(cls) -> "FeeConfig":
        return cls(taker_fee_pct=0.02, slippage_pct=0.01)

    @classmethod
    def backpack(cls) -> "FeeConfig":
        return cls(taker_fee_pct=0.06, slippage_pct=0.01)

    @classmethod
    def from_preset(cls, name: str) -> "FeeConfig":
        presets = {
            "lighter": cls.lighter,
            "pacifica": cls.pacifica,
            "extended": cls.extended,
            "backpack": cls.backpack,
        }
        factory = presets.get(name, cls.backpack)
        return factory()


@dataclass
class SimPosition:
    """시뮬레이션 포지션."""
    symbol: str
    side: PositionSide
    size: float          # quantity
    entry_price: float
    leverage: float = 1.0


class SimulatedExchange(BaseExchange):
    """
    백테스트용 모의 거래소.

    현재 가격은 외부에서 set_price()로 주입합니다.
    """

    name = "simulated"

    def __init__(self, fee_config: Optional[FeeConfig] = None):
        self.fee_config = fee_config or FeeConfig()
        self._prices: Dict[str, float] = {}       # symbol → last price
        self._positions: Dict[str, SimPosition] = {}  # symbol → position
        self._leverage: Dict[str, int] = {}
        self._total_fees: float = 0.0

    # ── 가격 주입 ─────────────────────────────────────────────

    def set_price(self, symbol: str, price: float) -> None:
        self._prices[symbol] = price

    def get_current_price(self, symbol: str) -> float:
        return self._prices.get(symbol, 0.0)

    # ── 슬리피지 적용 ─────────────────────────────────────────

    def _apply_slippage(self, price: float, side: OrderSide) -> float:
        slip = self.fee_config.slippage_pct / 100.0
        if side == OrderSide.BUY:
            return price * (1 + slip)
        return price * (1 - slip)

    def _calc_fee(self, size_usd: float) -> float:
        return size_usd * self.fee_config.taker_fee_pct / 100.0

    # ── BaseExchange 구현 ─────────────────────────────────────

    def perp_symbol(self, base: str) -> str:
        return f"{base}-PERP"

    async def get_ticker(self, symbol: str) -> Ticker:
        price = self._prices.get(symbol, 0.0)
        return Ticker(
            symbol=symbol,
            last_price=price,
            bid_price=price,
            ask_price=price,
            volume_24h=0.0,
            high_24h=price,
            low_24h=price,
            timestamp=0,
        )

    async def get_tickers(self, symbols: List[str]) -> Dict[str, Ticker]:
        return {s: await self.get_ticker(s) for s in symbols}

    async def get_klines(self, symbol: str, interval: str, limit: int = 100) -> List[Dict[str, Any]]:
        return []

    async def get_orderbook(self, symbol: str) -> Dict[str, List[List[float]]]:
        price = self._prices.get(symbol, 0.0)
        return {"bids": [[price, 1.0]], "asks": [[price, 1.0]]}

    async def place_order(
        self,
        symbol: str,
        side: OrderSide,
        quantity: float,
        order_type: OrderType = OrderType.MARKET,
        price: Optional[float] = None,
        reduce_only: bool = False,
    ) -> OrderResult:
        base_price = self._prices.get(symbol, 0.0)
        fill_price = self._apply_slippage(base_price, side)
        size_usd = quantity * fill_price
        fee = self._calc_fee(size_usd)
        self._total_fees += fee

        # 포지션 업데이트
        if reduce_only:
            pos = self._positions.get(symbol)
            if pos:
                pos.size -= quantity
                if pos.size <= 0:
                    del self._positions[symbol]
        else:
            existing = self._positions.get(symbol)
            if existing:
                # 같은 방향이면 추가
                new_side = PositionSide.LONG if side == OrderSide.BUY else PositionSide.SHORT
                if existing.side == new_side:
                    total_cost = existing.entry_price * existing.size + fill_price * quantity
                    existing.size += quantity
                    existing.entry_price = total_cost / existing.size
                else:
                    # 반대 방향 → 포지션 축소/반전
                    existing.size -= quantity
                    if existing.size <= 0:
                        del self._positions[symbol]
            else:
                pos_side = PositionSide.LONG if side == OrderSide.BUY else PositionSide.SHORT
                self._positions[symbol] = SimPosition(
                    symbol=symbol,
                    side=pos_side,
                    size=quantity,
                    entry_price=fill_price,
                    leverage=float(self._leverage.get(symbol, 1)),
                )

        return OrderResult(
            order_id=str(uuid.uuid4())[:8],
            symbol=symbol,
            side=side,
            order_type=order_type,
            quantity=quantity,
            price=fill_price,
            filled_quantity=quantity,
            avg_fill_price=fill_price,
            status="FILLED",
        )

    async def cancel_order(self, symbol: str, order_id: str) -> bool:
        return True

    async def cancel_all_orders(self, symbol: str) -> int:
        return 0

    async def get_open_orders(self, symbol: Optional[str] = None) -> List[OrderResult]:
        return []

    async def get_position(self, symbol: str) -> Optional[Position]:
        sim = self._positions.get(symbol)
        if sim is None:
            return None
        mark = self._prices.get(symbol, sim.entry_price)
        pnl = self._calc_unrealized_pnl(sim, mark)
        return Position(
            symbol=symbol,
            side=sim.side,
            size=sim.size,
            entry_price=sim.entry_price,
            mark_price=mark,
            unrealized_pnl=pnl,
            leverage=sim.leverage,
        )

    async def get_positions(self) -> List[Position]:
        positions = []
        for symbol, sim in self._positions.items():
            mark = self._prices.get(symbol, sim.entry_price)
            pnl = self._calc_unrealized_pnl(sim, mark)
            positions.append(Position(
                symbol=symbol,
                side=sim.side,
                size=sim.size,
                entry_price=sim.entry_price,
                mark_price=mark,
                unrealized_pnl=pnl,
                leverage=sim.leverage,
            ))
        return positions

    async def close_position(self, symbol: str) -> Optional[OrderResult]:
        sim = self._positions.get(symbol)
        if sim is None:
            return None
        close_side = OrderSide.SELL if sim.side == PositionSide.LONG else OrderSide.BUY
        result = await self.place_order(
            symbol=symbol,
            side=close_side,
            quantity=sim.size,
            reduce_only=True,
        )
        return result

    async def set_leverage(self, symbol: str, leverage: int) -> bool:
        self._leverage[symbol] = leverage
        return True

    async def get_balances(self) -> List[Balance]:
        return [Balance(asset="USDT", available=100000.0, locked=0.0)]

    async def get_balance(self, asset: str) -> Optional[Balance]:
        return Balance(asset=asset, available=100000.0, locked=0.0)

    async def connect_ws(self, symbols: List[str], on_tick: TickCallback) -> None:
        pass  # 백테스트에서는 사용하지 않음

    async def disconnect_ws(self) -> None:
        pass

    # ── 유틸리티 ──────────────────────────────────────────────

    @staticmethod
    def _calc_unrealized_pnl(pos: SimPosition, mark_price: float) -> float:
        if pos.side == PositionSide.LONG:
            return (mark_price - pos.entry_price) * pos.size
        else:
            return (pos.entry_price - mark_price) * pos.size

    @property
    def total_fees(self) -> float:
        return self._total_fees

    def reset(self) -> None:
        self._prices.clear()
        self._positions.clear()
        self._total_fees = 0.0
