from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from core import Position, Strategy
    from order_managers import ProjectXOrderManager


@dataclass
class TickerState:
    # Standard fields
    strategy: Strategy
    total_pnl: float = 0.0
    tick_counter: int = 0
    position: Optional[Position] = None
    prev_price: Optional[float] = None
    order_manager: Optional[ProjectXOrderManager] = None

    # Aggregation fields
    buckets: Dict[tuple[datetime, str], Dict[str, float]] = field(default_factory=dict)
    candle_length: int = 5
