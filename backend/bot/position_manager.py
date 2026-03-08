"""
Position Manager — BTC/ETH 페어 트레이딩 포지션 관리.

거래소에서 페어 포지션(BTC Long + ETH Short 또는 그 반대)을
열고, 모니터링하고, 청산하는 역할을 담당합니다.
"""

import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional

from backend.bot.exchanges.base import (
    BaseExchange,
    OrderResult,
    OrderSide,
    OrderType,
    Position,
    PositionSide,
)

logger = logging.getLogger(__name__)


class PairDirection(str, Enum):
    """페어 포지션 방향."""
    LONG_BTC_SHORT_ETH = "LONG_BTC_SHORT_ETH"
    SHORT_BTC_LONG_ETH = "SHORT_BTC_LONG_ETH"


@dataclass
class LegInfo:
    """단일 레그 (BTC 또는 ETH) 정보."""
    asset: str                    # "BTC" or "ETH"
    side: PositionSide            # LONG or SHORT
    size_usd: float               # 진입 크기 (USD)
    quantity: float = 0.0         # 실제 체결 수량
    entry_price: float = 0.0      # 진입 가격
    current_price: float = 0.0    # 현재 가격
    unrealized_pnl: float = 0.0   # 미실현 PNL
    order_result: Optional[OrderResult] = None


@dataclass
class PairTrade:
    """페어 트레이드 (BTC + ETH 양쪽 포지션)."""
    trade_id: str
    exchange_name: str
    direction: PairDirection
    btc_leg: LegInfo
    eth_leg: LegInfo
    opened_at: float = 0.0       # Unix timestamp
    closed_at: float = 0.0
    zscore_at_entry: float = 0.0
    spread_at_entry: float = 0.0
    total_fees_usd: float = 0.0
    is_open: bool = True

    @property
    def total_pnl_usd(self) -> float:
        """양쪽 레그 PNL 합산 (수수료 미포함)."""
        return self.btc_leg.unrealized_pnl + self.eth_leg.unrealized_pnl

    @property
    def net_pnl_usd(self) -> float:
        """수수료 포함 순 PNL."""
        return self.total_pnl_usd - self.total_fees_usd

    @property
    def total_size_usd(self) -> float:
        """양쪽 레그 합산 사이즈."""
        return self.btc_leg.size_usd + self.eth_leg.size_usd

    @property
    def pnl_pct(self) -> float:
        """전체 사이즈 대비 PNL %."""
        if self.total_size_usd == 0:
            return 0.0
        return (self.net_pnl_usd / self.total_size_usd) * 100.0


class PositionManager:
    """
    페어 트레이딩 포지션 매니저.

    - 페어 진입 (BTC + ETH 동시 주문)
    - 포지션 모니터링 (현재가 업데이트, PNL 계산)
    - 페어 청산
    - Averaging / Size Reduction
    """

    def __init__(self):
        self._open_trades: Dict[str, PairTrade] = {}  # trade_id → PairTrade
        self._closed_trades: List[PairTrade] = []
        self._trade_counter: int = 0

    @property
    def open_trades(self) -> Dict[str, PairTrade]:
        return self._open_trades

    @property
    def has_open_position(self) -> bool:
        return len(self._open_trades) > 0

    @property
    def open_trade_count(self) -> int:
        return len(self._open_trades)

    def _next_trade_id(self, exchange_name: str) -> str:
        self._trade_counter += 1
        return f"{exchange_name}_{self._trade_counter}_{int(time.time())}"

    # ── 진입 ──────────────────────────────────────────────────

    async def open_pair(
        self,
        exchange: BaseExchange,
        direction: PairDirection,
        size_usd: float,
        leverage: int,
        zscore: float = 0.0,
        spread_pct: float = 0.0,
    ) -> Optional[PairTrade]:
        """
        페어 포지션을 엽니다.

        Args:
            exchange: 거래소 커넥터
            direction: LONG_BTC_SHORT_ETH 또는 SHORT_BTC_LONG_ETH
            size_usd: 각 레그 포지션 크기 (USD)
            leverage: 레버리지
            zscore: 진입 시 Z-score (기록용)
            spread_pct: 진입 시 스프레드 (기록용)

        Returns:
            PairTrade 또는 실패 시 None
        """
        btc_symbol = exchange.perp_symbol("BTC")
        eth_symbol = exchange.perp_symbol("ETH")

        # 레버리지 설정
        try:
            await exchange.set_leverage(btc_symbol, leverage)
        except Exception as e:
            logger.warning("Failed to set leverage: %s", e)

        # 방향 결정
        if direction == PairDirection.LONG_BTC_SHORT_ETH:
            btc_side = OrderSide.BUY
            eth_side = OrderSide.SELL
            btc_pos_side = PositionSide.LONG
            eth_pos_side = PositionSide.SHORT
        else:
            btc_side = OrderSide.SELL
            eth_side = OrderSide.BUY
            btc_pos_side = PositionSide.SHORT
            eth_pos_side = PositionSide.LONG

        # 현재가 조회하여 수량 계산
        try:
            btc_ticker = await exchange.get_ticker(btc_symbol)
            eth_ticker = await exchange.get_ticker(eth_symbol)
        except Exception as e:
            logger.error("Failed to fetch tickers: %s", e)
            return None

        btc_qty = self._calculate_quantity(size_usd, btc_ticker.last_price, "BTC")
        eth_qty = self._calculate_quantity(size_usd, eth_ticker.last_price, "ETH")

        if btc_qty <= 0 or eth_qty <= 0:
            logger.error("Invalid quantity: BTC=%s, ETH=%s", btc_qty, eth_qty)
            return None

        # 양쪽 동시 주문
        btc_order = None
        eth_order = None
        try:
            btc_order = await exchange.place_order(
                symbol=btc_symbol,
                side=btc_side,
                quantity=btc_qty,
                order_type=OrderType.MARKET,
            )
            logger.info(
                "BTC leg opened: %s %s qty=%s",
                btc_side.value, btc_symbol, btc_qty,
            )
        except Exception as e:
            logger.error("BTC order failed: %s", e)
            return None

        try:
            eth_order = await exchange.place_order(
                symbol=eth_symbol,
                side=eth_side,
                quantity=eth_qty,
                order_type=OrderType.MARKET,
            )
            logger.info(
                "ETH leg opened: %s %s qty=%s",
                eth_side.value, eth_symbol, eth_qty,
            )
        except Exception as e:
            logger.error("ETH order failed, rolling back BTC: %s", e)
            # BTC 레그 롤백
            try:
                await exchange.close_position(btc_symbol)
            except Exception as rollback_err:
                logger.error("BTC rollback failed: %s", rollback_err)
            return None

        # PairTrade 생성
        trade_id = self._next_trade_id(exchange.name)
        btc_entry = btc_order.avg_fill_price or btc_ticker.last_price
        eth_entry = eth_order.avg_fill_price or eth_ticker.last_price

        trade = PairTrade(
            trade_id=trade_id,
            exchange_name=exchange.name,
            direction=direction,
            btc_leg=LegInfo(
                asset="BTC",
                side=btc_pos_side,
                size_usd=size_usd,
                quantity=btc_order.filled_quantity or btc_qty,
                entry_price=btc_entry,
                current_price=btc_entry,
                order_result=btc_order,
            ),
            eth_leg=LegInfo(
                asset="ETH",
                side=eth_pos_side,
                size_usd=size_usd,
                quantity=eth_order.filled_quantity or eth_qty,
                entry_price=eth_entry,
                current_price=eth_entry,
                order_result=eth_order,
            ),
            opened_at=time.time(),
            zscore_at_entry=zscore,
            spread_at_entry=spread_pct,
        )

        self._open_trades[trade_id] = trade
        logger.info(
            "Pair opened: %s | %s | BTC@%.2f ETH@%.4f | Z=%.2f",
            trade_id, direction.value, btc_entry, eth_entry, zscore,
        )
        return trade

    # ── 포지션 업데이트 ───────────────────────────────────────

    async def update_positions(self, exchange: BaseExchange) -> None:
        """
        거래소에서 현재 포지션 정보를 가져와 오픈 트레이드를 업데이트합니다.
        """
        try:
            positions = await exchange.get_positions()
        except Exception as e:
            logger.error("Failed to fetch positions: %s", e)
            return

        pos_map: Dict[str, Position] = {p.symbol: p for p in positions}
        btc_symbol = exchange.perp_symbol("BTC")
        eth_symbol = exchange.perp_symbol("ETH")

        for trade in self._open_trades.values():
            if trade.exchange_name != exchange.name:
                continue

            btc_pos = pos_map.get(btc_symbol)
            eth_pos = pos_map.get(eth_symbol)

            if btc_pos:
                trade.btc_leg.current_price = btc_pos.mark_price
                trade.btc_leg.unrealized_pnl = btc_pos.unrealized_pnl
            if eth_pos:
                trade.eth_leg.current_price = eth_pos.mark_price
                trade.eth_leg.unrealized_pnl = eth_pos.unrealized_pnl

    # ── 청산 ──────────────────────────────────────────────────

    async def close_pair(
        self,
        trade_id: str,
        exchange: BaseExchange,
        reason: str = "MANUAL",
    ) -> Optional[PairTrade]:
        """
        페어 포지션을 양쪽 모두 청산합니다.

        Args:
            trade_id: 트레이드 ID
            exchange: 거래소 커넥터
            reason: 청산 사유 (TP/SL/ZSCORE/MANUAL/TIMEOUT)

        Returns:
            청산된 PairTrade 또는 None
        """
        trade = self._open_trades.get(trade_id)
        if trade is None:
            logger.warning("Trade not found: %s", trade_id)
            return None

        btc_symbol = exchange.perp_symbol("BTC")
        eth_symbol = exchange.perp_symbol("ETH")

        # 양쪽 청산
        errors = []
        try:
            await exchange.close_position(btc_symbol)
            logger.info("BTC leg closed: %s", trade_id)
        except Exception as e:
            errors.append(f"BTC close failed: {e}")
            logger.error("BTC close failed: %s", e)

        try:
            await exchange.close_position(eth_symbol)
            logger.info("ETH leg closed: %s", trade_id)
        except Exception as e:
            errors.append(f"ETH close failed: {e}")
            logger.error("ETH close failed: %s", e)

        if errors:
            logger.error("Close errors for %s: %s", trade_id, errors)
            # 부분 청산이라도 기록
        trade.is_open = False
        trade.closed_at = time.time()

        del self._open_trades[trade_id]
        self._closed_trades.append(trade)

        logger.info(
            "Pair closed: %s | reason=%s | PNL=$%.2f (%.2f%%)",
            trade_id, reason, trade.net_pnl_usd, trade.pnl_pct,
        )
        return trade

    async def close_all(self, exchange: BaseExchange, reason: str = "MANUAL") -> List[PairTrade]:
        """거래소의 모든 오픈 트레이드를 청산합니다."""
        trade_ids = [
            tid for tid, t in self._open_trades.items()
            if t.exchange_name == exchange.name
        ]
        closed = []
        for tid in trade_ids:
            trade = await self.close_pair(tid, exchange, reason)
            if trade:
                closed.append(trade)
        return closed

    # ── Averaging Down ────────────────────────────────────────

    async def averaging_down(
        self,
        trade_id: str,
        exchange: BaseExchange,
        multiplier: float = 0.5,
    ) -> bool:
        """
        손실 중인 레그에 추가 진입 (평균단가 낮추기).

        Args:
            trade_id: 트레이드 ID
            exchange: 거래소 커넥터
            multiplier: 기존 사이즈 대비 추가 비율 (0.5 = 50%)
        """
        trade = self._open_trades.get(trade_id)
        if trade is None:
            return False

        # 손실 레그 식별
        losing_leg = None
        if trade.btc_leg.unrealized_pnl < trade.eth_leg.unrealized_pnl:
            losing_leg = trade.btc_leg
        else:
            losing_leg = trade.eth_leg

        if losing_leg.unrealized_pnl >= 0:
            logger.info("No losing leg, skip averaging")
            return False

        symbol = exchange.perp_symbol(losing_leg.asset)
        add_size_usd = losing_leg.size_usd * multiplier
        add_qty = self._calculate_quantity(
            add_size_usd, losing_leg.current_price, losing_leg.asset
        )

        if add_qty <= 0:
            return False

        side = OrderSide.BUY if losing_leg.side == PositionSide.LONG else OrderSide.SELL

        try:
            result = await exchange.place_order(
                symbol=symbol,
                side=side,
                quantity=add_qty,
                order_type=OrderType.MARKET,
            )
            losing_leg.size_usd += add_size_usd
            losing_leg.quantity += result.filled_quantity or add_qty
            # 평균 진입가 업데이트
            fill_price = result.avg_fill_price or losing_leg.current_price
            total_cost = (
                losing_leg.entry_price * (losing_leg.quantity - add_qty)
                + fill_price * add_qty
            )
            losing_leg.entry_price = total_cost / losing_leg.quantity

            logger.info(
                "Averaging down: %s %s +$%.0f qty=%.4f new_avg=%.2f",
                losing_leg.asset, trade_id, add_size_usd, add_qty, losing_leg.entry_price,
            )
            return True
        except Exception as e:
            logger.error("Averaging down failed: %s", e)
            return False

    # ── Size Reduction ────────────────────────────────────────

    async def size_reduction(
        self,
        trade_id: str,
        exchange: BaseExchange,
        ratio: float = 0.5,
    ) -> bool:
        """
        수익 중인 레그의 일부를 청산하여 포지션을 중립화합니다.

        Args:
            trade_id: 트레이드 ID
            exchange: 거래소 커넥터
            ratio: 청산 비율 (0.5 = 50%)
        """
        trade = self._open_trades.get(trade_id)
        if trade is None:
            return False

        # 수익 레그 식별
        winning_leg = None
        if trade.btc_leg.unrealized_pnl > trade.eth_leg.unrealized_pnl:
            winning_leg = trade.btc_leg
        else:
            winning_leg = trade.eth_leg

        if winning_leg.unrealized_pnl <= 0:
            logger.info("No winning leg, skip size reduction")
            return False

        symbol = exchange.perp_symbol(winning_leg.asset)
        reduce_qty = winning_leg.quantity * ratio

        if reduce_qty <= 0:
            return False

        # 포지션 축소 (반대 방향으로 reduce_only)
        close_side = (
            OrderSide.SELL if winning_leg.side == PositionSide.LONG else OrderSide.BUY
        )

        try:
            await exchange.place_order(
                symbol=symbol,
                side=close_side,
                quantity=reduce_qty,
                order_type=OrderType.MARKET,
                reduce_only=True,
            )
            winning_leg.quantity -= reduce_qty
            winning_leg.size_usd *= (1 - ratio)

            logger.info(
                "Size reduction: %s %s -%.4f (%.0f%%)",
                winning_leg.asset, trade_id, reduce_qty, ratio * 100,
            )
            return True
        except Exception as e:
            logger.error("Size reduction failed: %s", e)
            return False

    # ── 유틸리티 ──────────────────────────────────────────────

    @staticmethod
    def _calculate_quantity(size_usd: float, price: float, asset: str) -> float:
        """USD 크기를 수량으로 변환합니다."""
        if price <= 0:
            return 0.0
        raw = size_usd / price
        # 거래소별 소수점 자릿수 (Backpack 기준)
        if asset.upper() == "BTC":
            return round(raw, 5)  # BTC: 소수점 5자리
        elif asset.upper() == "ETH":
            return round(raw, 4)  # ETH: 소수점 4자리
        return round(raw, 6)

    def get_summary(self) -> Dict[str, Any]:
        """현재 포지션 요약 (대시보드용)."""
        total_pnl = sum(t.net_pnl_usd for t in self._open_trades.values())
        total_size = sum(t.total_size_usd for t in self._open_trades.values())

        trades_info = []
        for tid, trade in self._open_trades.items():
            trades_info.append({
                "trade_id": tid,
                "exchange": trade.exchange_name,
                "direction": trade.direction.value,
                "btc_entry": trade.btc_leg.entry_price,
                "eth_entry": trade.eth_leg.entry_price,
                "btc_pnl": round(trade.btc_leg.unrealized_pnl, 2),
                "eth_pnl": round(trade.eth_leg.unrealized_pnl, 2),
                "total_pnl": round(trade.net_pnl_usd, 2),
                "pnl_pct": round(trade.pnl_pct, 2),
                "opened_at": trade.opened_at,
            })

        return {
            "open_trades": len(self._open_trades),
            "total_pnl_usd": round(total_pnl, 2),
            "total_size_usd": round(total_size, 2),
            "trades": trades_info,
        }
