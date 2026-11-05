from datetime import datetime, timedelta, timezone
from typing import Any, List, Optional

from api.models import ExhaustionFadeParams
from config import init_null_logger
from core.types import Signal, Tick
from strategies import ExhaustionFade
from tickers import TickerState

# 15:00 UTC = 10:00 CT (CDT), inside the default entry window
START = datetime(2026, 6, 10, 15, 0, tzinfo=timezone.utc)


def make_strategy(**overrides: Any) -> ExhaustionFade:
    params = ExhaustionFadeParams(tick_size=0.25, tick_value=0.50, **overrides)
    return ExhaustionFade(init_null_logger("INFO"), [], params)


def quiet_bar(minute: int) -> List[Tick]:
    """A small, balanced bar oscillating between 100.00 and 101.00."""
    t = START + timedelta(minutes=minute)
    return [
        Tick(t=t + timedelta(seconds=1), price=100.5, size=10, side="B", symbol="X"),
        Tick(t=t + timedelta(seconds=2), price=101.0, size=10, side="B", symbol="X"),
        Tick(t=t + timedelta(seconds=3), price=100.0, size=10, side="A", symbol="X"),
        Tick(t=t + timedelta(seconds=4), price=100.5, size=10, side="A", symbol="X"),
    ]


def spike_bar(minute: int, buy: int, sell: int) -> List[Tick]:
    """Heavy bar that runs to a new high at 104 and closes back at 100.5."""
    t = START + timedelta(minutes=minute)
    return [
        Tick(t=t + timedelta(seconds=1), price=100.5, size=10, side="B", symbol="X"),
        Tick(t=t + timedelta(seconds=10), price=104.0, size=buy, side="B", symbol="X"),
        Tick(t=t + timedelta(seconds=20), price=103.0, size=sell, side="A", symbol="X"),
        Tick(t=t + timedelta(seconds=50), price=100.5, size=10, side="A", symbol="X"),
    ]


def run(strategy: ExhaustionFade, ticks: List[Tick]) -> Optional[Signal]:
    signal = None
    for tick in ticks:
        s = strategy.check(tick)
        if s is not None:
            signal = s
    return signal


def setup_and_spike(strategy: ExhaustionFade, buy: int, sell: int) -> Optional[Signal]:
    ticks: List[Tick] = []
    for m in range(35):
        ticks += quiet_bar(m)
    ticks += spike_bar(35, buy, sell)
    ticks += [
        Tick(t=START + timedelta(minutes=36), price=100.5, size=1, side="A", symbol="X")
    ]
    return run(strategy, ticks)


def test_contested_rejected_high_signals_short() -> None:
    # 400 buys vs 360 sells: aggressors present but not dominant (dr ~0.05)
    signal = setup_and_spike(make_strategy(), buy=400, sell=360)
    assert signal is not None
    assert signal.direction == "SHORT"
    assert signal.entry == 100.5
    assert signal.stop_target > signal.entry > signal.profit_target


def test_one_sided_aggression_does_not_signal() -> None:
    # 700 buys vs 60 sells (dr ~0.8): initiative buying, which tends to continue
    assert setup_and_spike(make_strategy(), buy=700, sell=60) is None


def test_net_selling_into_high_does_not_signal() -> None:
    # Negative delta at the high: no trapped buyers to fade
    assert setup_and_spike(make_strategy(), buy=300, sell=460) is None


def test_light_volume_does_not_signal() -> None:
    assert (
        setup_and_spike(make_strategy(min_volume_multiple=50.0), buy=400, sell=360)
        is None
    )


def test_outside_entry_window_does_not_signal() -> None:
    assert (
        setup_and_spike(make_strategy(entry_start_hour=11), buy=400, sell=360) is None
    )


def test_handler_time_exit_flattens() -> None:
    strategy = make_strategy(flat_hour=10, flat_minute=37)
    state = TickerState(strategy=strategy)
    handler = strategy.get_backtest_handler()
    logger = init_null_logger("INFO")

    ticks: List[Tick] = []
    for m in range(35):
        ticks += quiet_bar(m)
    ticks += spike_bar(35, buy=400, sell=360)
    ticks += [
        Tick(t=START + timedelta(minutes=36), price=100.5, size=1, side="A", symbol="X")
    ]
    for tick in ticks:
        handler(tick, logger, state)
    assert state.position is not None and state.position.direction == "SHORT"

    exit_tick = Tick(
        t=START + timedelta(minutes=37), price=100.0, size=1, side="A", symbol="X"
    )
    handler(exit_tick, logger, state)
    assert state.position is None
    assert state.total_pnl == 1.0  # 0.5 pts short at $2/pt


def test_direction_filter_blocks_other_side() -> None:
    assert setup_and_spike(make_strategy(direction="LONG"), buy=400, sell=360) is None
    signal = setup_and_spike(make_strategy(direction="SHORT"), buy=400, sell=360)
    assert signal is not None and signal.direction == "SHORT"
