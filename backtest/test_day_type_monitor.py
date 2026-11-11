import logging
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Dict, List

import pytest

from api.models import DayTypeMonitorParams
from calculations import DayTypeClassifier
from core.types import Tick
from strategies import DayTypeMonitor
from strategies.day_type_monitor import day_type_monitor_handler
from tickers import TickerState

# June: Chicago is UTC-5, so 13:30 UTC = 08:30 CT
DAY1 = date(2026, 6, 9)
DAY2 = date(2026, 6, 10)


def at(day: date, minute: float) -> datetime:
    return datetime(
        day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc
    ) + timedelta(minutes=minute)


def tick(day: date, minute: float, price: float) -> Tick:
    return Tick(t=at(day, minute), price=price, size=1, side="B", symbol="X")


def path(day: date, prices: List[float], start: int = 0) -> List[Tick]:
    """One tick per minute."""
    return [tick(day, start + i, p) for i, p in enumerate(prices)]


def classifier() -> DayTypeClassifier:
    return DayTypeClassifier(rth_start=time(8, 30), rth_end=time(15, 0))


def feed(c: DayTypeClassifier, ticks: List[Tick]) -> None:
    for t in ticks:
        c.on_price(t.t, t.price, t.size)


class ListHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


def make_monitor(
    candles: List[Dict[str, Any]] = [], **overrides: Any
) -> tuple[DayTypeMonitor, ListHandler]:
    logger = logging.getLogger(f"day_type_test_{id(overrides)}_{len(candles)}")
    logger.handlers = []
    logger.setLevel(logging.INFO)
    logger.propagate = False
    handler = ListHandler()
    logger.addHandler(handler)
    params = DayTypeMonitorParams(**{"snapshot_minutes": 0, **overrides})
    return DayTypeMonitor(logger, candles, params), handler


def test_trend_up() -> None:
    c = classifier()
    feed(c, path(DAY1, [100 + i for i in range(61)]))
    snap = c.snapshot()
    assert snap is not None
    assert snap.label == "trend up"
    assert (snap.trendness, snap.efficiency) == (pytest.approx(1.0), pytest.approx(1.0))


def test_efficiency_counts_minute_closes() -> None:
    c = classifier()
    feed(c, path(DAY1, [100.0, 101.0, 100.0, 102.0]))
    snap = c.snapshot()
    assert snap is not None
    # Travelled 1 + 1 + 2 = 4 to move 2
    assert snap.efficiency == pytest.approx(0.5)


def test_chop_crosses_vwap_often() -> None:
    c = classifier()
    feed(c, path(DAY1, [100.0, 104.0] + [99.0, 103.0] * 30 + [100.0]))
    snap = c.snapshot()
    assert snap is not None
    assert snap.trendness < 0.35 and snap.crosses_per_hour >= 4
    assert snap.label == "chop"


def test_range_rotates_with_few_crosses() -> None:
    c = classifier()
    up = [100.0 + i / 6 for i in range(60)]  # To 110
    down = [110.0 - i / 3 for i in range(60)]  # To 90
    back = [90.0 + i / 6 for i in range(61)]  # To 100
    feed(c, path(DAY1, up + down + back))
    snap = c.snapshot()
    assert snap is not None
    assert snap.trendness < 0.35 and snap.crosses_per_hour < 4
    assert snap.label == "range"


def test_too_early_and_mixed() -> None:
    c = classifier()
    feed(c, path(DAY1, [100.0 + i / 10 for i in range(20)]))
    snap = c.snapshot()
    assert snap is not None and snap.label == "too early"
    # Up 10, back to +5: trendness 0.5
    feed(c, path(DAY1, [100.0 + i / 4 for i in range(41)], start=20))
    feed(c, path(DAY1, [110.0 - i / 4 for i in range(21)], start=61))
    snap = c.snapshot()
    assert snap is not None and snap.label == "mixed"


def test_range_vs_normal_uses_full_prior_sessions() -> None:
    c = classifier()
    # Day 1: range 10 by minute 30, full session
    feed(c, path(DAY1, [100.0 + i / 3 for i in range(31)]))
    feed(c, [tick(DAY1, 389, 105.0)])
    # Day 2: range 20 by minute 30
    feed(c, path(DAY2, [100.0 + 2 * i / 3 for i in range(31)]))
    snap = c.snapshot()
    assert c.history_size() == 1
    assert snap is not None and snap.range_vs_normal == pytest.approx(2.0)


def test_half_day_left_out_of_history() -> None:
    c = classifier()
    feed(c, path(DAY1, [100.0, 101.0]) + [tick(DAY1, 200, 100.0)])
    feed(c, [tick(DAY2, 0, 100.0)])
    assert c.history_size() == 0


def test_label_change_logged_after_confirm_minutes() -> None:
    monitor, logs = make_monitor()
    state = TickerState(strategy=monitor)
    # Flat for 30 minutes (range), then a steady climb
    ticks = path(DAY1, [100.0, 100.5] * 15) + path(
        DAY1, [100.0 + i for i in range(1, 31)], start=30
    )
    for t in ticks:
        day_type_monitor_handler(t, logging.getLogger("unused"), state)
    changes = [m for m in logs.messages if "DAY TYPE" in m and "->" in m]
    assert changes and "-> trend up" in changes[-1]
    assert monitor.label == "trend up"
    # The climb started at minute 30; trend up has to hold 5 minutes first
    assert monitor.changes[-1] >= "09:05"


def test_summary_logged_once_after_close() -> None:
    monitor, logs = make_monitor()
    ticks = path(DAY1, [100 + i for i in range(61)])
    ticks += [tick(DAY1, 390, 160.0), tick(DAY1, 400, 161.0)]  # After 15:00
    ticks += [tick(DAY2, 0, 150.0)]
    for t in ticks:
        monitor.check(t)
    summaries = [m for m in logs.messages if "DAY TYPE SUMMARY" in m]
    assert len(summaries) == 1
    assert "trend up" in summaries[0] and str(DAY1) in summaries[0]


def test_seeded_mid_session_from_candles() -> None:
    def candle(day: date, minute: int, o: float, c: float) -> Dict[str, Any]:
        t = at(day, minute).isoformat().replace("+00:00", "Z")
        return {"t": t, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 10}

    candles = [candle(DAY1, m, 100.0, 100.0 + (m % 2)) for m in range(390)]
    candles += [candle(DAY2, m, 100.0 + m, 101.0 + m) for m in range(45)]
    monitor, logs = make_monitor(candles)
    assert monitor.classifier.history_size() == 1
    assert monitor.label == "trend up"
    assert "trend up" in repr(monitor) and "history: 1 sessions" in repr(monitor)
    # Seeding replays quietly
    assert not [m for m in logs.messages if "->" in m]


def test_hysteresis_keeps_the_current_label() -> None:
    c = classifier()
    # Up 10, back to +3.6: trendness 0.36, just above range_max_trendness (0.35)
    feed(c, path(DAY1, [100.0 + i / 4 for i in range(41)]))
    feed(c, path(DAY1, [110.0 - i * 0.32 for i in range(21)], start=41))
    snap = c.snapshot()
    assert snap is not None and 0.35 < snap.trendness < 0.40
    assert c.label(snap) == "mixed"
    assert c.label(snap, current="range") == "range"
