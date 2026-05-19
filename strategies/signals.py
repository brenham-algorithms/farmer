from dataclasses import dataclass
from datetime import datetime
from typing import Optional

@dataclass
class Signal:
    timestamp: datetime
    direction: str
    entry: float
    size: int
    profit_target: Optional[float] = None
    stop_target: Optional[float] = None


@dataclass
class AddSignal:
    timestamp: datetime
    entry: float
    size: int
