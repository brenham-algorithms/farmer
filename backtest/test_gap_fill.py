from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from api.models import GapFillParams
from config import init_null_logger
from core.types import Signal, Tick
from strategies import GapFill
from strategies.gap_fill import gap_fill_handler
from tickers import TickerState

# June: Chicago is UTC-5, so 13:30 UTC = 08:30 CT
DAY1 = datetime(2026, 6, 10, 13, 30, tzinfo=timezone.utc)
DAY2 = DAY1 + timedelta(days=1)


def make_strategy(candles: List[Dict[str, Any]] = [], **overrides: Any) -> GapFill:
    params = GapFillParams(tick_size=0.25, tick_value=0.50, **overrides)
    return GapFill(init_null_logger("INFO"), candles, params)


def tick(t: datetime, price: float, symbol: str = "X") -> Tick:
    return Tick(t=t, price=price, size=1, side="B", symbol=symbol)


def prior_session(symbol: str = "X") -> List[Tick]:
    """08:30-14:59 CT: range 90-110, last trade before 15:00 at 100. A 15:10 print is ignored."""
    return [
        tick(DAY1, 95.0, symbol),
        tick(DAY1 + timedelta(hours=1), 110.0, symbol),
        tick(DAY1 + timedelta(hours=3), 90.0, symbol),
        tick(DAY1 + timedelta(hours=6, minutes=29), 100.0, symbol),
        tick(DAY1 + timedelta(hours=6, minutes=40), 120.0, symbol),
    ]


def premarket_and_open(open_price: float, symbol: str = "X") -> List[Tick]:
    return [
        tick(DAY2 - timedelta(minutes=30), 200.0, symbol),  # Pre-market: not RTH
        tick(DAY2, open_price, symbol),
    ]


def signals(strategy: GapFill, ticks: List[Tick]) -> List[Signal]:
    out = []
    for t in ticks:
        s = strategy.check(t)
        if s is not None:
            out.append(s)
    return out


def test_large_gap_up_fades_to_prior_close() -> None:
    strategy = make_strategy()
    out = signals(strategy, prior_session() + premarket_and_open(112.0))
    prior = strategy.prior_session()
    assert prior is not None and (prior.close, prior.range) == (100.0, 20.0)
    # Gap +12 / range 20 = 0.6
    assert len(out) == 1
    s = out[0]
    assert (s.direction, s.entry, s.profit_target, s.stop_target) == (
        "SHORT",
        112.0,
        100.0,
        124.0,
    )


def test_gap_down_with_partial_fill_and_wider_stop() -> None:
    strategy = make_strategy(fill_fraction=0.5, stop_gap_multiple=2.0)
    (s,) = signals(strategy, prior_session() + premarket_and_open(88.0))
    assert (s.direction, s.profit_target, s.stop_target) == ("LONG", 94.0, 64.0)


@pytest.mark.parametrize(
    "overrides, open_price",
    [
        ({}, 109.0),  # ratio 0.45 < 0.5
        ({"max_gap_range_ratio": 0.55}, 112.0),
        ({"min_gap_points": 15.0}, 112.0),
        ({"max_stop_points": 10.0}, 112.0),
        ({"direction": "LONG"}, 112.0),
        ({"skip_dates": ["2026-06-11"]}, 112.0),
        ({"min_prior_session_minutes": 400}, 112.0),
        ({"max_prior_session_age_days": 0}, 112.0),
    ],
)
def test_filters_skip_the_trade(overrides: Dict[str, Any], open_price: float) -> None:
    strategy = make_strategy(**overrides)
    assert signals(strategy, prior_session() + premarket_and_open(open_price)) == []


def test_symbol_change_skips_the_trade() -> None:
    strategy = make_strategy()
    ticks = prior_session("MNQM6") + premarket_and_open(112.0, "MNQU6")
    assert signals(strategy, ticks) == []


def test_only_first_tick_in_entry_window_is_evaluated() -> None:
    strategy = make_strategy()
    ticks = prior_session() + [
        tick(DAY2, 105.0),  # Ratio 0.25: skipped
        tick(DAY2 + timedelta(minutes=1), 115.0),  # Would qualify, but too late
    ]
    assert signals(strategy, ticks) == []


def test_no_entry_after_entry_window() -> None:
    strategy = make_strategy()
    ticks = prior_session() + [tick(DAY2 + timedelta(minutes=5), 112.0)]
    assert signals(strategy, ticks) == []


def test_seeded_from_candles() -> None:
    candles = [
        {"t": "2026-06-10T13:30:00Z", "o": 95, "h": 110, "l": 95, "c": 105, "v": 10},
        {"t": "2026-06-10T19:55:00Z", "o": 105, "h": 105, "l": 90, "c": 100, "v": 10},
        {"t": "2026-06-10T20:05:00Z", "o": 100, "h": 130, "l": 100, "c": 125, "v": 10},
    ]
    strategy = make_strategy(candles)
    (s,) = signals(strategy, premarket_and_open(112.0))
    assert (s.direction, s.profit_target) == ("SHORT", 100.0)


@pytest.mark.parametrize(
    "path, expected_pnl",
    [
        ([106.0, 100.0], 24.0),  # Target: 12 points x $2
        ([118.0, 124.0], -24.0),  # Stop
        ([110.0], 4.0),  # Time exit at 14:59
    ],
)
def test_handler_exits(path: List[float], expected_pnl: float) -> None:
    strategy = make_strategy()
    state = TickerState(strategy=strategy)
    ticks = prior_session() + premarket_and_open(112.0)
    ticks += [tick(DAY2 + timedelta(minutes=10 + i), p) for i, p in enumerate(path)]
    ticks += [tick(DAY2 + timedelta(hours=6, minutes=29), 110.0)]
    for t in ticks:
        gap_fill_handler(t, init_null_logger("INFO"), state)
    assert state.position is None
    assert state.total_pnl == pytest.approx(expected_pnl)
