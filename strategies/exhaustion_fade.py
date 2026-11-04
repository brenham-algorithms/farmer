import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import ExhaustionFadeParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from tickers import TickerState


@dataclass
class Bar:
    t: datetime  # Minute bucket start (UTC)
    open: float
    high: float
    low: float
    close: float
    volume: int = 0
    delta: int = 0

    def on_tick(self, tick: Tick) -> None:
        self.high = max(self.high, tick.price)
        self.low = min(self.low, tick.price)
        self.close = tick.price
        self.volume += tick.size
        self.delta += tick.delta()

    def delta_ratio(self) -> float:
        return self.delta / self.volume if self.volume else 0.0

    def close_position(self) -> float:
        """Where the close sits in the bar's range: 0 = low, 1 = high."""
        rng = self.high - self.low
        return (self.close - self.low) / rng if rng > 0 else 0.5


class ExhaustionFade:
    """
    Fades failed aggression at a fresh short-term extreme on 1-minute bars.

    A completed 1-minute bar qualifies as a SHORT signal when it:
      1. Takes out the high of the prior lookback_bars bars.
      2. Prints heavy volume (>= min_volume_multiple x rolling average).
      3. Shows modestly positive delta (min_delta_ratio < dr <= max_delta_ratio):
         aggressive buyers pushed into the high, but did not dominate it.
      4. Closes in the bottom max_close_position of its range: the buying
         was absorbed by passive sellers and price was rejected.
    LONG is the mirror image at a fresh low.

    The delta ceiling matters: when aggression at the extreme is one-sided
    (high |dr|), price tends to continue and fading it loses. The edge is
    specifically in heavy, contested, rejected extremes.

    Entry is at the first tick after the signal bar closes. Stop and target
    are ATR multiples from entry. Positions are flattened at flat_hour:flat_minute.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: ExhaustionFadeParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts
        self.use_brackets = params.use_brackets

        # Signal
        self.lookback_bars = params.lookback_bars
        self.atr_period = params.atr_period
        self.volume_avg_period = params.volume_avg_period
        self.min_volume_multiple = params.min_volume_multiple
        self.min_delta_ratio = params.min_delta_ratio
        self.max_delta_ratio = params.max_delta_ratio
        self.max_close_position = params.max_close_position
        self.direction = params.direction

        # Risk/reward
        self.stop_atr_multiple = params.stop_atr_multiple
        self.target_atr_multiple = params.target_atr_multiple

        # Windows
        self.entry_start = time(params.entry_start_hour, params.entry_start_minute)
        self.entry_end = time(params.entry_end_hour, params.entry_end_minute)
        self.flat_time = time(params.flat_hour, params.flat_minute)
        self.cooldown = timedelta(minutes=params.cooldown_minutes)

        # Session guards
        self.max_trades_per_session = params.max_trades_per_session
        self.daily_loss_limit = params.daily_loss_limit
        self.session_reset = timedelta(
            hours=params.session_reset_hour, minutes=params.session_reset_minute
        )

        # State
        history = max(self.lookback_bars, self.atr_period, self.volume_avg_period) + 1
        self._bars: Deque[Bar] = deque(maxlen=history)
        self._true_ranges: Deque[float] = deque(maxlen=self.atr_period)
        self._bar: Optional[Bar] = None
        self._session: Optional[date] = None
        self._next_entry_t: Optional[datetime] = None
        self._trades_this_session = 0
        self._daily_pnl = 0.0

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _session_key(self, t: datetime) -> date:
        # Shift so the session reset time maps to midnight; the date is then the session
        return (t.astimezone(self.tz) - self.session_reset + timedelta(days=1)).date()

    def _round(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, self.precision)

    def atr(self) -> Optional[float]:
        if len(self._true_ranges) < min(10, self.atr_period):
            return None
        return sum(self._true_ranges) / len(self._true_ranges)

    def is_flat_time(self, t: datetime) -> bool:
        return t.astimezone(self.tz).time() >= self.flat_time

    def _reset_session(self, session: date) -> None:
        self._session = session
        self._bars.clear()
        self._true_ranges.clear()
        self._bar = None
        self._next_entry_t = None
        self._trades_this_session = 0
        self._daily_pnl = 0.0

    def _close_bar(self, bar: Bar) -> None:
        prev_close = self._bars[-1].close if self._bars else bar.open
        tr = max(bar.high, prev_close) - min(bar.low, prev_close)
        self._true_ranges.append(tr)
        self._bars.append(bar)

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        """Update bars on every tick; return a Signal on the first tick after a qualifying bar."""
        in_position = kwargs.get("in_position", False)

        session = self._session_key(tick.t)
        if session != self._session:
            self._reset_session(session)

        bucket = tick.t.replace(second=0, microsecond=0)
        if self._bar is not None and bucket == self._bar.t:
            self._bar.on_tick(tick)
            return None

        # A new minute has started: finalize the previous bar and evaluate it
        completed = self._bar
        self._bar = Bar(
            t=bucket, open=tick.price, high=tick.price, low=tick.price, close=tick.price
        )
        self._bar.on_tick(tick)

        if completed is None:
            return None

        prior = list(self._bars)
        self._close_bar(completed)

        if in_position:
            return None

        direction = self._evaluate(completed, prior)
        if direction is None:
            return None

        return self._build_entry(tick, completed, direction)

    def _evaluate(self, bar: Bar, prior: List[Bar]) -> Optional[str]:
        if len(prior) < self.lookback_bars:
            return None

        bar_local = bar.t.astimezone(self.tz).time()
        if bar_local < self.entry_start or bar_local > self.entry_end:
            return None

        if self._next_entry_t is not None and bar.t < self._next_entry_t:
            return None

        if (
            self.max_trades_per_session is not None
            and self._trades_this_session >= self.max_trades_per_session
        ):
            return None

        if (
            self.daily_loss_limit is not None
            and self._daily_pnl <= self.daily_loss_limit
        ):
            return None

        # Volume average includes the signal bar, matching the research definition
        recent = (prior + [bar])[-self.volume_avg_period :]
        avg_volume = sum(b.volume for b in recent) / len(recent)
        if bar.volume < self.min_volume_multiple * avg_volume:
            return None

        window = prior[-self.lookback_bars :]
        prior_high = max(b.high for b in window)
        prior_low = min(b.low for b in window)
        dr = bar.delta_ratio()
        cpos = bar.close_position()

        if (
            self.direction != "LONG"
            and bar.high > prior_high
            and self.min_delta_ratio < dr <= self.max_delta_ratio
            and cpos < self.max_close_position
        ):
            return "SHORT"

        if (
            self.direction != "SHORT"
            and bar.low < prior_low
            and -self.max_delta_ratio <= dr < -self.min_delta_ratio
            and cpos > 1 - self.max_close_position
        ):
            return "LONG"

        return None

    def _build_entry(self, tick: Tick, bar: Bar, direction: str) -> Optional[Signal]:
        atr = self.atr()
        if atr is None or atr <= 0:
            return None

        entry = tick.price
        stop_dist = self.stop_atr_multiple * atr
        target_dist = self.target_atr_multiple * atr

        if direction == "LONG":
            stop_loss = self._round(entry - stop_dist)
            take_profit = self._round(entry + target_dist)
        else:
            stop_loss = self._round(entry + stop_dist)
            take_profit = self._round(entry - target_dist)

        self._trades_this_session += 1
        self._next_entry_t = bar.t + self.cooldown

        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} EXHAUSTION FADE entry={entry} "
            f"bar=[o={bar.open} h={bar.high} l={bar.low} c={bar.close}] "
            f"vol={bar.volume} dr={bar.delta_ratio():.3f} cpos={bar.close_position():.2f} "
            f"atr={atr:.2f} tp={take_profit} sl={stop_loss}"
        )

        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.num_contracts,
            profit_target=take_profit,
            stop_target=stop_loss,
        )

    def on_exit(self, t: datetime, pnl: float) -> None:
        self._daily_pnl += pnl
        # No new signal from a bar that started before the exit
        exit_bucket = t.replace(second=0, microsecond=0)
        if self._next_entry_t is None or exit_bucket > self._next_entry_t:
            self._next_entry_t = exit_bucket + timedelta(minutes=1)

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return exhaustion_fade_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return exhaustion_fade_handler

    def __repr__(self) -> str:
        return (
            f"ExhaustionFade(lookback={self.lookback_bars}, "
            f"vol>={self.min_volume_multiple}x, "
            f"dr=({self.min_delta_ratio}, {self.max_delta_ratio}], "
            f"cpos<{self.max_close_position}, "
            f"sl={self.stop_atr_multiple}atr, tp={self.target_atr_multiple}atr)"
        )


def _exit(
    state: TickerState,
    logger: logging.Logger,
    strategy: ExhaustionFade,
    tick: Tick,
    price: float,
    reason: str,
) -> None:
    position = state.position
    if position is None:
        return

    pnl = position.close(price)
    state.total_pnl += pnl
    strategy.on_exit(tick.t, pnl)

    log_with_color(
        logger,
        f"[{strategy._ct(tick.t)}] Exhaustion fade {reason}, "
        f"Start = {strategy._ct(position.timestamp)}, End = {strategy._ct(tick.t)}, "
        f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
        Fore.GREEN if pnl > 0 else Fore.RED,
        "info",
    )
    state.position = None


def exhaustion_fade_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != ExhaustionFade:
        raise ValueError(
            f"Expected ExhaustionFade strategy in state, got {type(state.strategy)}"
        )

    strategy = state.strategy
    position = state.position

    signal = strategy.check(tick, in_position=position is not None)

    if position is None:
        if signal is not None and not strategy.is_flat_time(tick.t):
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                take_profit=signal.profit_target,
                stop_loss=signal.stop_target,
                order_manager=state.order_manager,
                use_brackets=strategy.use_brackets,
            )
        return

    direction = position.direction

    if (direction == "LONG" and tick.price <= position.stop_loss) or (
        direction == "SHORT" and tick.price >= position.stop_loss
    ):
        _exit(state, logger, strategy, tick, position.stop_loss, "stop loss")
        return

    if (direction == "LONG" and tick.price >= position.take_profit) or (
        direction == "SHORT" and tick.price <= position.take_profit
    ):
        _exit(state, logger, strategy, tick, position.take_profit, "take profit")
        return

    if strategy.is_flat_time(tick.t):
        # Position.close() leaves exits to the broker when brackets are used, but
        # the time exit is not a bracket leg, so flatten explicitly
        if position.order_manager and position.use_brackets:
            position.order_manager.close_position()
        _exit(state, logger, strategy, tick, tick.price, "time exit")
