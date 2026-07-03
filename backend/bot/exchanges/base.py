"""Abstract exchange connector.

Every venue (Variational, Pacifica, Extended, Lighter, Backpack) implements this
interface so the pair-trading engine can open/close legs without knowing which
exchange it is talking to. Order routing is async because the whole point of the
direct-API rewrite is to fire both legs with minimal wall-clock skew — see
``open_pair`` in the position manager, which awaits the two legs concurrently.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field
from decimal import Decimal
from enum import Enum
from typing import Any, Optional


class Side(str, Enum):
    BUY = "buy"
    SELL = "sell"

    @property
    def opposite(self) -> "Side":
        return Side.SELL if self is Side.BUY else Side.BUY


class ExchangeError(Exception):
    """Raised for any connector-level failure (auth, HTTP, validation)."""

    def __init__(self, message: str, *, venue: str = "", payload: Any = None):
        super().__init__(message)
        self.venue = venue
        self.payload = payload


@dataclass(slots=True)
class Order:
    """A single-leg order request (venue-agnostic)."""

    symbol: str                      # canonical symbol, e.g. "BTC" / "ETH"
    side: Side
    size_usd: Decimal                # notional in USD/USDC
    reduce_only: bool = False
    order_type: str = "market"       # "market" | "limit"
    price: Optional[Decimal] = None  # required for limit orders
    client_id: Optional[str] = None  # idempotency key when the venue supports it


@dataclass(slots=True)
class OrderResult:
    """Normalized response after an order is accepted (or simulated)."""

    accepted: bool
    venue: str
    symbol: str
    side: Side
    size_usd: Decimal
    filled_price: Optional[Decimal] = None
    order_id: Optional[str] = None
    raw: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False


@dataclass(slots=True)
class Position:
    """Current open position for one symbol."""

    symbol: str
    side: Side
    size: Decimal          # base-asset size (signed positive; side carries direction)
    entry_price: Decimal
    unrealized_pnl: Decimal = Decimal(0)
    raw: dict[str, Any] = field(default_factory=dict)


class BaseExchange(abc.ABC):
    """Common async interface for all venues."""

    #: short venue identifier, e.g. "variational"
    name: str = "base"

    # -- lifecycle ----------------------------------------------------------
    async def connect(self) -> None:
        """Establish the HTTP session and authenticate. Idempotent."""

    async def close(self) -> None:
        """Release network resources."""

    async def __aenter__(self) -> "BaseExchange":
        await self.connect()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.close()

    # -- trading ------------------------------------------------------------
    @abc.abstractmethod
    async def place_order(self, order: Order) -> OrderResult:
        """Submit a single-leg order and return a normalized result."""

    @abc.abstractmethod
    async def get_position(self, symbol: str) -> Optional[Position]:
        """Return the current open position for ``symbol`` (or None)."""

    async def close_position(self, symbol: str) -> Optional[OrderResult]:
        """Flatten ``symbol`` with a reduce-only market order in the opposite side.

        Default implementation composes ``get_position`` + ``place_order``;
        venues with a dedicated close endpoint may override.
        """
        pos = await self.get_position(symbol)
        if pos is None or pos.size == 0:
            return None
        notional = abs(pos.size) * pos.entry_price
        return await self.place_order(
            Order(
                symbol=symbol,
                side=pos.side.opposite,
                size_usd=notional,
                reduce_only=True,
            )
        )

    # -- market data --------------------------------------------------------
    @abc.abstractmethod
    async def get_mark_price(self, symbol: str) -> Decimal:
        """Return the current mark/index price for ``symbol``."""
