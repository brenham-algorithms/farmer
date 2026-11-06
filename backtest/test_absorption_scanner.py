from datetime import datetime, timedelta, timezone
from typing import Any, List

import pytest

from api.models import AbsorptionScannerParams
from config import init_null_logger
from core.types import Signal, Tick
from strategies import AbsorptionScanner

# June: Chicago is UTC-5, so 13:00 UTC = 08:00 CT
START = datetime(2026, 6, 10, 13, 0, tzinfo=timezone.utc)


def make_strategy(**overrides: Any) -> AbsorptionScanner:
    params = AbsorptionScannerParams(
        tick_size=0.25,
        tick_value=0.50,
        min_absorption_ratio=0.5,
        min_window_volume=400,
        **overrides,
    )
    return AbsorptionScanner(init_null_logger("INFO"), [], params)


def history(minutes: int, buy: int, sell: int) -> List[Tick]:
    """Quiet minutes at one price; net flow = (buy - sell) / (buy + sell). Too little volume to signal."""
    ticks = []
    for m in range(minutes):
        t = START + timedelta(minutes=m)
        ticks.append(
            Tick(
                t=t + timedelta(seconds=1), price=100.0, size=sell, side="A", symbol="X"
            )
        )
        ticks.append(
            Tick(
                t=t + timedelta(seconds=2), price=100.0, size=buy, side="B", symbol="X"
            )
        )
    return ticks


def sell_burst(minute: int) -> List[Tick]:
    """Aggressive sells that don't move price: a passive buyer absorbing them -> LONG signal."""
    t = START + timedelta(minutes=minute)
    return [
        Tick(t=t + timedelta(seconds=1 + i), price=100.0, size=50, side="A", symbol="X")
        for i in range(10)
    ]


def run(strategy: AbsorptionScanner, ticks: List[Tick]) -> List[Signal]:
    signals = []
    for tick in ticks:
        s = strategy.check(tick)
        if s is not None:
            signals.append(s)
    return signals


def opposing_then_burst(strategy: AbsorptionScanner) -> List[Signal]:
    # Net selling before a LONG signal: flow against the trade
    return run(strategy, history(16, buy=10, sell=20) + sell_burst(16))


def test_filter_disabled_by_default_and_flow_measured() -> None:
    strategy = make_strategy()
    signals = run(strategy, history(16, buy=20, sell=10) + sell_burst(16))
    assert [s.direction for s in signals] == ["LONG"]
    # Supporting flow before a LONG: +10/30
    assert strategy.directional_flow("LONG") == pytest.approx(1 / 3)
    assert strategy.directional_flow("SHORT") == pytest.approx(-1 / 3)


def test_opposing_flow_passes_filter() -> None:
    signals = opposing_then_burst(make_strategy(min_opposing_flow=0.06))
    assert [s.direction for s in signals] == ["LONG"]


def test_supporting_flow_is_filtered_and_starts_cooldown() -> None:
    strategy = make_strategy(min_opposing_flow=0.06)
    ticks = history(16, buy=20, sell=10) + sell_burst(16)
    assert run(strategy, ticks) == []
    # The rejected signal started the cooldown, so a second burst a minute later is ignored
    assert run(strategy, sell_burst(17)) == []


def test_insufficient_history_is_filtered() -> None:
    strategy = make_strategy(min_opposing_flow=0.06)
    assert run(strategy, history(5, buy=10, sell=20) + sell_burst(5)) == []
    assert strategy.directional_flow("LONG") is None


def test_current_minute_is_excluded_from_flow() -> None:
    strategy = make_strategy()
    run(strategy, history(16, buy=10, sell=20) + sell_burst(16))
    # The heavy selling burst is in the still-open minute 16, so flow is unchanged
    assert strategy.directional_flow("LONG") == pytest.approx(-1 / 3)


@pytest.mark.parametrize("start_minute, expected", [(30, 0), (10, 1)])
def test_trading_start_minute(start_minute: int, expected: int) -> None:
    # The signal fires at 08:16 CT
    strategy = make_strategy(trading_start_hour=8, trading_start_minute=start_minute)
    assert len(opposing_then_burst(strategy)) == expected
