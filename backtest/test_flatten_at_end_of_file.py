from datetime import datetime, timezone

from api.models import ExhaustionFadeParams, StrategyConfig
from config import init_null_logger
from core.types import Entry, Position, Tick
from strategies import ExhaustionFade
from tickers import TickerState

from .runner import flatten_at_end_of_file

T0 = datetime(2026, 6, 10, 19, 0, tzinfo=timezone.utc)
T1 = datetime(2026, 6, 10, 20, 0, tzinfo=timezone.utc)


def make_state() -> TickerState:
    params = ExhaustionFadeParams(tick_size=0.25, tick_value=0.50)
    return TickerState(strategy=ExhaustionFade(init_null_logger("INFO"), [], params))


def short_position() -> Position:
    return Position(
        timestamp=T0,
        direction="SHORT",
        entries=[Entry(price=100.0, size=2)],
        tick_size=0.25,
        tick_value=0.50,
        take_profit=90.0,
        stop_loss=110.0,
    )


def test_flattens_open_position_at_last_price() -> None:
    state = make_state()
    state.position = short_position()
    last = Tick(t=T1, price=98.0, size=1, side="A", symbol="X")

    flatten_at_end_of_file(state, last, init_null_logger("INFO"))

    assert state.position is None
    assert state.total_pnl == 8.0  # 2 pts short x 2 contracts at $2/pt


def test_no_position_is_a_no_op() -> None:
    state = make_state()
    last = Tick(t=T1, price=98.0, size=1, side="A", symbol="X")
    flatten_at_end_of_file(state, last, init_null_logger("INFO"))
    assert state.position is None and state.total_pnl == 0.0


def test_no_ticks_leaves_position_alone() -> None:
    state = make_state()
    state.position = short_position()
    flatten_at_end_of_file(state, None, init_null_logger("INFO"))
    assert state.position is not None and state.total_pnl == 0.0


def test_disabled_by_default() -> None:
    config = StrategyConfig(
        strategy_params=ExhaustionFadeParams(tick_size=0.25, tick_value=0.50)
    )
    assert config.flatten_at_end_of_file is False
