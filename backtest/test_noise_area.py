from datetime import date, datetime, timedelta, timezone
from typing import Any, Dict, List

import pytest

from api.models import NoiseAreaParams
from config import init_null_logger
from core.types import Tick
from strategies import NoiseArea
from strategies.noise_area import noise_area_handler
from tickers import TickerState

# June: Chicago is UTC-5, so 13:30 UTC = 08:30 CT


def at(day: date, minutes_after_open: float) -> datetime:
    return datetime(
        day.year, day.month, day.day, 13, 30, tzinfo=timezone.utc
    ) + timedelta(minutes=minutes_after_open)


def tick(day: date, minute: float, price: float, symbol: str = "X") -> Tick:
    return Tick(t=at(day, minute), price=price, size=1, side="B", symbol=symbol)


HISTORY_DAYS = [date(2026, 6, 8), date(2026, 6, 9)]
TODAY = date(2026, 6, 10)


def history_session(
    day: date, symbol: str = "X", last_minute: float = 389
) -> List[Tick]:
    """Open 100, 101 from 08:45 (so |move| = 1% at every checkpoint), close 100."""
    return [
        tick(day, 0, 100.0, symbol),
        tick(day, 15, 101.0, symbol),
        tick(day, last_minute, 100.0, symbol),
    ]


def history(symbol: str = "X") -> List[Tick]:
    return [t for d in HISTORY_DAYS for t in history_session(d, symbol)]


def make_strategy(candles: List[Dict[str, Any]] = [], **overrides: Any) -> NoiseArea:
    params = NoiseAreaParams(
        **{"tick_size": 0.25, "tick_value": 0.50, "lookback_days": 2, **overrides}
    )
    return NoiseArea(init_null_logger("INFO"), candles, params)


def run(strategy: NoiseArea, ticks: List[Tick]) -> TickerState:
    state = TickerState(strategy=strategy)
    for t in ticks:
        noise_area_handler(t, init_null_logger("INFO"), state)
    return state


def today(*moves: tuple, symbol: str = "X") -> List[Tick]:
    return [tick(TODAY, 0, 100.0, symbol)] + [
        tick(TODAY, m, p, symbol) for m, p in moves
    ]


def test_bands_from_history() -> None:
    strategy = make_strategy()
    run(strategy, history() + today())
    bands = strategy.bands(30)
    assert bands is not None
    # Open 100, prior close 100, sigma 1%
    assert bands.sigma == pytest.approx(0.01)
    assert (bands.upper, bands.lower) == (pytest.approx(101.0), pytest.approx(99.0))


def test_breakout_long_then_exit_below_vwap() -> None:
    strategy = make_strategy()
    # Enter long at 09:00 at 102; hold at 09:30 (101.5 > VWAP 101.17); exit at 10:00
    state = run(strategy, history() + today((30, 102.0), (60, 101.5), (90, 100.5)))
    assert state.position is None
    assert state.total_pnl == pytest.approx(-3.0)  # (100.5 - 102) points x $2


def test_exit_can_flip_short() -> None:
    strategy = make_strategy()
    state = run(strategy, history() + today((30, 102.0), (60, 98.0)))
    assert state.position is not None and state.position.direction == "SHORT"
    assert state.total_pnl == pytest.approx(-8.0)


def test_between_checkpoints_nothing_happens() -> None:
    strategy = make_strategy()
    state = run(strategy, history() + today((20, 105.0), (31.5, 105.0)))
    # 08:50 is not a checkpoint; 09:01:30 still counts as the 09:00 checkpoint
    assert state.position is not None and state.position.entries[0].price == 105.0
    assert state.position.timestamp == at(TODAY, 31.5)


def test_stale_checkpoint_is_ignored() -> None:
    strategy = make_strategy()
    state = run(strategy, history() + today((33, 105.0)))
    assert state.position is None


def test_time_exit() -> None:
    strategy = make_strategy()
    state = run(strategy, history() + today((30, 102.0), (60, 103.0), (389, 104.0)))
    assert state.position is None
    assert state.total_pnl == pytest.approx(4.0)


def test_hard_stop() -> None:
    strategy = make_strategy(stop_loss_points=1.0)
    state = run(strategy, history() + today((30, 102.0), (40, 100.75)))
    assert state.position is None
    assert state.total_pnl == pytest.approx(-2.0)


@pytest.mark.parametrize(
    "overrides",
    [
        {"lookback_days": 3},
        {"skip_dates": ["2026-06-10"]},
        {"direction": "SHORT"},
        {"max_prior_session_age_days": 0},
    ],
)
def test_not_traded(overrides: Dict[str, Any]) -> None:
    strategy = make_strategy(**overrides)
    state = run(strategy, history() + today((30, 102.0)))
    assert state.position is None


def test_symbol_change_skips_the_day() -> None:
    strategy = make_strategy()
    state = run(strategy, history("MNQM6") + today((30, 102.0), symbol="MNQU6"))
    assert state.position is None


def test_open_not_seen_skips_the_day() -> None:
    strategy = make_strategy()
    state = run(strategy, history() + [tick(TODAY, 10, 100.0), tick(TODAY, 30, 102.0)])
    assert state.position is None


def test_half_day_left_out_of_history() -> None:
    strategy = make_strategy()
    run(strategy, history_session(HISTORY_DAYS[0], last_minute=209))
    run(strategy, history_session(HISTORY_DAYS[1]))
    run(strategy, today())
    assert strategy.history_size() == 1


def candle(day: date, minute: int, o: float, c: float) -> Dict[str, Any]:
    t = at(day, minute).isoformat().replace("+00:00", "Z")
    return {"t": t, "o": o, "h": max(o, c), "l": min(o, c), "c": c, "v": 10}


def test_seeded_from_candles() -> None:
    candles = []
    for d in HISTORY_DAYS:
        candles += [
            candle(d, 0, 100, 100),
            candle(d, 15, 101, 101),
            candle(d, 389, 100, 100),
        ]
    strategy = make_strategy(candles)
    assert strategy.history_size() == 1  # The last seeded session is still open
    state = run(strategy, today((30, 102.0)))
    assert strategy.history_size() == 2
    assert state.position is not None and state.position.direction == "LONG"


def test_flat_before_first_checkpoint_rejected() -> None:
    with pytest.raises(ValueError):
        make_strategy(first_check_hour=8, first_check_minute=30)
