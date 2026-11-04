from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from api.models import WickReversalParams
from config import init_null_logger
from core.types import Signal, Tick
from strategies import WickReversal

RTH = dict(
    trading_start_hour=8,
    trading_start_minute=30,
    trading_end_hour=15,
    trading_end_minute=0,
)


def make_strategy(**overrides: Any) -> WickReversal:
    params = WickReversalParams(tick_size=0.25, tick_value=0.50, **overrides)
    return WickReversal(init_null_logger("INFO"), [], params)


def tick(t: datetime, price: float) -> Tick:
    return Tick(t=t, price=price, size=10, side="A", symbol="X")


def entry_at(entry_utc: datetime, **overrides: Any) -> Optional[Signal]:
    """Feed a long-lower-wick candle in the minute before entry_utc, then one tick at entry_utc."""
    strategy = make_strategy(**overrides)
    signal_minute = entry_utc - timedelta(minutes=1)
    ticks: List[Tick] = [
        tick(
            signal_minute - timedelta(minutes=1), 100.0
        ),  # partial first candle, discarded
        tick(signal_minute, 100.0),
        tick(signal_minute + timedelta(seconds=10), 75.0),  # 25 pt lower wick
        tick(signal_minute + timedelta(seconds=50), 100.5),
        tick(entry_utc, 100.5),
    ]
    signal = None
    for t in ticks:
        s = strategy.check(t)
        if s is not None:
            signal = s
    return signal


# June: Chicago is UTC-5, so 08:30 CT = 13:30 UTC and 15:00 CT = 20:00 UTC
def test_entry_at_rth_open_is_allowed() -> None:
    signal = entry_at(datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc), **RTH)
    assert signal is not None and signal.direction == "LONG"


def test_entry_before_rth_open_is_blocked() -> None:
    assert entry_at(datetime(2026, 6, 10, 13, 29, tzinfo=timezone.utc), **RTH) is None


def test_entry_at_rth_close_is_blocked() -> None:
    assert entry_at(datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc), **RTH) is None


def test_minutes_default_to_whole_hours() -> None:
    # Old hour-only configs behave as before: start hour 8 allows 08:05
    signal = entry_at(
        datetime(2026, 6, 10, 13, 5, tzinfo=timezone.utc),
        trading_start_hour=8,
        trading_end_hour=15,
    )
    assert signal is not None
