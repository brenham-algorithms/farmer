from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, List, Optional


@dataclass(frozen=True)
class Tick:
    t: datetime
    price: float
    size: int
    side: str
    symbol: str

    def delta(self) -> int:
        if self.side == "B":
            return self.size

        if self.side == "A":
            return -self.size

        return 0


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
    take_profit: float = 0.00
    stop_loss: float = 0.00
    unwinding: bool = False
    order_manager: Optional[Any] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self._brackets_active = False
        if self.order_manager and self.entries:
            total_size = sum(e.size for e in self.entries)
            sl_ticks = round(abs(self.entries[0].price - self.stop_loss) / self.tick_size)
            tp_ticks = round(abs(self.take_profit - self.entries[0].price) / self.tick_size) if self.take_profit != 0.0 else None
            self.order_manager.enter_position(self.direction, total_size, sl_ticks=sl_ticks, tp_ticks=tp_ticks)
            self._brackets_active = True
        
    # def __post_init__(self) -> None:
    #     if self.order_manager and self.entries:
    #         total_size = sum(e.size for e in self.entries)
    #         self.order_manager.enter_position(self.direction, total_size)

    def add(self, size: int, add_price: float) -> None:
        self.entries.append(Entry(price=add_price, size=size))
        if self.order_manager:
            self.order_manager.enter_position(self.direction, size)

    def cut(self, size: int, cut_price: float) -> float:
        pnl = 0.0
        if size >= self.num_contracts():
            return self.close(cut_price)

        if self.order_manager and not self._brackets_active:
            self.order_manager.reduce_position(self.direction, size)

        remaining = size
        while remaining > 0 and self.entries:
            entry = self.entries[-1]
            close_qty = min(remaining, entry.size)

            if self.direction == "LONG":
                pnl += (
                    (cut_price - entry.price)
                    / self.tick_size
                    * self.tick_value
                    * close_qty
                )
            else:
                pnl += (
                    (entry.price - cut_price)
                    / self.tick_size
                    * self.tick_value
                    * close_qty
                )

            remaining -= close_qty
            entry.size -= close_qty
            if entry.size <= 0:
                self.entries.pop()

        return round(pnl, 2)

    def close(self, close_price: float) -> float:
        if self.order_manager and not self._brackets_active:
            total_size = self.num_contracts()
            self.order_manager.close_position(self.direction, total_size)

        pnl = 0.0
        for entry in self.entries:
            if self.direction == "LONG":
                pnl += (
                    (close_price - entry.price)
                    / self.tick_size
                    * self.tick_value
                    * entry.size
                )
            else:
                pnl += (
                    (entry.price - close_price)
                    / self.tick_size
                    * self.tick_value
                    * entry.size
                )

        self.entries = []
        return round(pnl, 2)

    def num_contracts(self) -> int:
        return sum(entry.size for entry in self.entries)


@dataclass
class Signal:
    timestamp: datetime
    direction: str
    entry: float
    size: int
    profit_target: float = 0.00
    stop_target: float = 0.00


@dataclass
class AddSignal:
    timestamp: datetime
    entry: float
    size: int
