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

    def __repr__(self) -> str:
        return (f"Signal(timestamp={self.timestamp}, direction={self.direction}, "
                f"entry={self.entry}, size={self.size}, "
                f"profit_target={self.profit_target}, stop_target={self.stop_target})")


@dataclass
class AddSignal:
    timestamp: datetime
    entry: float
    size: int

    def __repr__(self) -> str:
        return (f"AddSignal(timestamp={self.timestamp}, entry={self.entry}, "
                f"size={self.size})")
