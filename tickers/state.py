from dataclasses import dataclass
from datetime import datetime
from typing import Any, Dict, List, Optional

from core.protocols import Strategy


@dataclass
class Entry:
    price: float
    size: int


@dataclass
class Position:
    timestamp: datetime
    direction: str
    entries: List[Entry]
    tick_size: float
    tick_value: float
    stop_loss: Optional[float] = None


@dataclass
class TickerState:
    strategy: Strategy
    total_pnl: float = 0.0
    tick_counter: int = 0
    position: Optional[Position] = None
    prev_price: Optional[float] = None
        