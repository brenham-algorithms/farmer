import logging
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import GapFillParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from tickers import TickerState


@dataclass
class RthSession:
    day: date  # Chicago date
    first_t: datetime
    last_t: datetime
    close: float  # Last trade before the RTH close
    high: float
    low: float
    symbol: Optional[str] = None

    def update(self, t: datetime, price: float) -> None:
        self.last_t = t
        self.close = price
        self.high = max(self.high, price)
        self.low = min(self.low, price)

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def minutes(self) -> float:
        return (self.last_t - self.first_t).total_seconds() / 60


class GapFill:
    """
    Fades large overnight gaps at the RTH open, targeting the prior RTH close.

    The gap is the price at entry_hour:entry_minute CT (the first tick at or after it)
    minus the prior RTH session's close (the last trade before rth_end). Gap size is
    measured relative to the prior session's RTH range. When |gap| / prior range is at
    least min_gap_range_ratio, the strategy fades it:
      gap up   -> SHORT, target = entry - fill_fraction * gap
      gap down -> LONG,  target = entry + fill_fraction * gap
    with the stop stop_gap_multiple * gap beyond the entry. Positions still open at
    flat_hour:flat_minute are closed. At most one trade per day.

    RTH sessions are built from the tick stream. Seed candles (aggregation_params) can
    supply the prior session when the strategy starts fresh, e.g. live or in seeded
    backtests; unseeded backtests need persist_across_files: true.

    The prior session is not used when it is older than max_prior_session_age_days,
    shorter than min_prior_session_minutes (half days, partial data) or traded a
    different symbol than today's open. Days in skip_dates are never traded. A
    continuous symbol such as MNQ.v.0 hides its rolls, so list roll days there.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: GapFillParams,
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
        self.min_gap_range_ratio = params.min_gap_range_ratio
        self.max_gap_range_ratio = params.max_gap_range_ratio
        self.min_gap_points = params.min_gap_points
        self.direction = params.direction

        # Exits
        self.fill_fraction = params.fill_fraction
        self.stop_gap_multiple = params.stop_gap_multiple
        self.max_stop_points = params.max_stop_points

        # Windows
        self.rth_start = time(params.rth_start_hour, params.rth_start_minute)
        self.rth_end = time(params.rth_end_hour, params.rth_end_minute)
        self.entry_time = time(params.entry_hour, params.entry_minute)
        self.entry_end = (
            datetime.combine(date.min, self.entry_time)
            + timedelta(minutes=params.max_entry_delay_minutes)
        ).time()
        self.flat_time = time(params.flat_hour, params.flat_minute)

        # Data hygiene
        self.skip_dates = {d.replace("-", "") for d in params.skip_dates}
        self.min_prior_session_minutes = params.min_prior_session_minutes
        self.max_prior_session_age_days = params.max_prior_session_age_days

        # State
        self._prior: Optional[RthSession] = None
        self._current: Optional[RthSession] = None
        self._evaluated: Optional[date] = None
        self._daily_pnl = 0.0

        self._seed(candles)

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _round(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, self.precision)

    def _in_rth(self, local: datetime) -> bool:
        return self.rth_start <= local.time() < self.rth_end

    def _on_rth_price(
        self, t: datetime, price: float, symbol: Optional[str], high: float, low: float
    ) -> None:
        day = t.astimezone(self.tz).date()
        if self._current is None or self._current.day != day:
            if self._current is not None and self._current.day < day:
                self._prior = self._current
            self._current = RthSession(day, t, t, price, high, low, symbol)
        self._current.update(t, price)
        self._current.high = max(self._current.high, high)
        self._current.low = min(self._current.low, low)

    def _seed(self, candles: List[Dict[str, Any]]) -> None:
        for candle in candles:
            t = candle["t"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if not self._in_rth(t.astimezone(self.tz)):
                continue
            self._on_rth_price(
                t, candle["c"], candle.get("symbol"), candle["h"], candle["l"]
            )
        if self._prior is not None or self._current is not None:
            self.logger.info(
                f"GapFill seeded: prior={self._prior} current={self._current}"
            )

    def prior_session(self) -> Optional[RthSession]:
        return self._prior

    def is_flat_time(self, t: datetime) -> bool:
        return t.astimezone(self.tz).time() >= self.flat_time

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        """Track RTH sessions on every tick; return a Signal at most once per day, at the open."""
        in_position = kwargs.get("in_position", False)

        local = tick.t.astimezone(self.tz)
        if self._in_rth(local):
            self._on_rth_price(tick.t, tick.price, tick.symbol, tick.price, tick.price)

        today = local.date()
        if self._evaluated == today or not (
            self.entry_time <= local.time() < self.entry_end
        ):
            return None

        # Only the first tick in the entry window is evaluated
        self._evaluated = today
        self._daily_pnl = 0.0

        reason = self._skip_reason(tick, today, in_position)
        prior = self._prior
        if reason is not None or prior is None:
            self.logger.info(f"[{self._ct(tick.t)}] GAP skipped: {reason}")
            return None

        return self._evaluate(tick, prior)

    def _skip_reason(self, tick: Tick, today: date, in_position: bool) -> Optional[str]:
        prior = self._prior
        if in_position:
            return "position already open"
        if today.strftime("%Y%m%d") in self.skip_dates:
            return "date is in skip_dates"
        if prior is None or prior.day >= today:
            return "no prior RTH session"
        if (today - prior.day).days > self.max_prior_session_age_days:
            return f"prior session {prior.day} is too old"
        if prior.minutes < self.min_prior_session_minutes:
            return f"prior session {prior.day} only covers {prior.minutes:.0f} minutes"
        if prior.symbol is not None and prior.symbol != tick.symbol:
            return f"symbol changed {prior.symbol} -> {tick.symbol}"
        if prior.range <= 0:
            return "prior session has no range"
        return None

    def _evaluate(self, tick: Tick, prior: RthSession) -> Optional[Signal]:
        entry = tick.price
        gap = entry - prior.close
        size = abs(gap)
        ratio = size / prior.range
        context = (
            f"open={entry} prior_close={prior.close} ({prior.day}) gap={gap:+.2f} "
            f"prior_range={prior.range:.2f} ratio={ratio:.2f}"
        )

        if size == 0:
            reason: Optional[str] = "no gap"
        elif ratio < self.min_gap_range_ratio:
            reason = f"ratio < {self.min_gap_range_ratio}"
        elif self.max_gap_range_ratio is not None and ratio > self.max_gap_range_ratio:
            reason = f"ratio > {self.max_gap_range_ratio}"
        elif self.min_gap_points is not None and size < self.min_gap_points:
            reason = f"gap < {self.min_gap_points} points"
        elif self.max_stop_points is not None and (
            self.stop_gap_multiple * size > self.max_stop_points
        ):
            reason = f"stop wider than {self.max_stop_points} points"
        else:
            reason = None

        direction = "SHORT" if gap > 0 else "LONG"
        if (
            reason is None
            and self.direction is not None
            and direction != self.direction
        ):
            reason = f"{direction} disabled"

        if reason is not None:
            self.logger.info(f"[{self._ct(tick.t)}] GAP skipped ({reason}): {context}")
            return None

        sign = 1 if direction == "LONG" else -1
        take_profit = self._round(entry + sign * self.fill_fraction * size)
        stop_loss = self._round(entry - sign * self.stop_gap_multiple * size)

        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} GAP FILL {context} "
            f"tp={take_profit} sl={stop_loss}"
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

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return gap_fill_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return gap_fill_handler

    def __repr__(self) -> str:
        return (
            f"GapFill(ratio>={self.min_gap_range_ratio}, "
            f"fill={self.fill_fraction}, stop={self.stop_gap_multiple}x gap, "
            f"entry={self.entry_time}, flat={self.flat_time})"
        )


def _exit(
    state: TickerState,
    logger: logging.Logger,
    strategy: GapFill,
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
        f"[{strategy._ct(tick.t)}] Gap fill {reason}, "
        f"Start = {strategy._ct(position.timestamp)}, End = {strategy._ct(tick.t)}, "
        f"PnL = ${pnl:.2f}",
        Fore.GREEN if pnl > 0 else Fore.RED,
        "info",
    )
    state.position = None


def gap_fill_handler(tick: Tick, logger: logging.Logger, state: TickerState) -> None:
    if type(state.strategy) != GapFill:
        raise ValueError(
            f"Expected GapFill strategy in state, got {type(state.strategy)}"
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
