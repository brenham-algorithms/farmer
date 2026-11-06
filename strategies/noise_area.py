import logging
from collections import deque
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Deque, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import NoiseAreaParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from tickers import TickerState

# A checkpoint is only acted on if a tick arrives within this many minutes of it, so a
# strategy started mid-session doesn't trade on a stale checkpoint
_MAX_CHECK_DELAY_MINUTES = 2.0


@dataclass
class _Session:
    day: date  # Chicago date
    open: float
    open_valid: bool  # The first trade arrived close enough to the open
    first_t: datetime
    last_t: datetime
    close: float
    symbol: Optional[str]
    closes: List[Optional[float]]  # Last price in each minute of the session
    pv: float = 0.0
    volume: float = 0.0

    @property
    def minutes(self) -> float:
        return (self.last_t - self.first_t).total_seconds() / 60

    @property
    def vwap(self) -> Optional[float]:
        return self.pv / self.volume if self.volume > 0 else None

    def curve(self) -> List[float]:
        """|price / open - 1| at the end of every minute, carrying the last price forward."""
        out, last = [], self.open
        for c in self.closes:
            if c is not None:
                last = c
            out.append(abs(last / self.open - 1))
        return out


@dataclass
class Bands:
    upper: float
    lower: float
    vwap: Optional[float]
    sigma: float


class NoiseArea:
    """
    Intraday momentum on breakouts of the "noise area" (Zarattini, Aziz & Barbon 2024,
    "Beat the Market: An Effective Intraday Momentum Strategy for S&P500 ETF (SPY)").

    For each minute m of the RTH session, sigma(m) is the average of |price(m) / open - 1|
    over the last lookback_days sessions. The noise area is
        upper = max(open, prior close) * (1 + sigma)
        lower = min(open, prior close) * (1 - sigma)
    Moves inside it are treated as noise. Decisions are only made at checkpoints (every
    check_interval_minutes from first_check through last_check):
      - flat: go LONG above upper, SHORT below lower;
      - long: exit if price < max(upper, VWAP); short: exit if price > min(lower, VWAP).
        After an exit the same checkpoint can enter the other way.
    Everything is closed at flat_hour:flat_minute. An optional hard stop is checked on every tick.

    Sessions are built from the tick stream, or from seed candles (aggregation_params with
    1-minute candles and enough lookback for lookback_days sessions). Unseeded backtests need
    persist_across_files: true. Half days are left out of the history. The day is not traded
    if the prior session is stale, the symbol changed (a roll), the date is in skip_dates, or
    the history is not full yet. A continuous symbol such as MNQ.v.0 hides its rolls, so list
    roll days in skip_dates.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: NoiseAreaParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts

        # Signal
        self.lookback_days = params.lookback_days
        self.band_multiplier = params.band_multiplier
        self.use_vwap_stop = params.use_vwap_stop
        self.direction = params.direction
        self.stop_loss_points = params.stop_loss_points

        # Sessions and checkpoints, as minute offsets from the RTH open
        self.rth_start = time(params.rth_start_hour, params.rth_start_minute)
        self.rth_end = time(params.rth_end_hour, params.rth_end_minute)
        self.session_minutes = self._offset(self.rth_end)
        first = self._offset(time(params.first_check_hour, params.first_check_minute))
        last = self._offset(time(params.last_check_hour, params.last_check_minute))
        self.checkpoints = list(range(first, last + 1, params.check_interval_minutes))
        if not self.checkpoints or self.checkpoints[0] < 1:
            raise ValueError("The first checkpoint must be after the RTH open")
        self.flat_time = time(params.flat_hour, params.flat_minute)

        # Data hygiene
        self.skip_dates = {d.replace("-", "") for d in params.skip_dates}
        self.min_session_minutes = params.min_session_minutes
        self.max_open_delay = params.max_open_delay_minutes
        self.max_prior_session_age_days = params.max_prior_session_age_days

        # State
        self._history: Deque[List[float]] = deque(maxlen=self.lookback_days)
        self._prior: Optional[_Session] = None
        self._current: Optional[_Session] = None
        self._next_check = 0
        self._skip_logged: Optional[date] = None
        self.exit_requested = False

        self._seed(candles)

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _round(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, self.precision)

    def _offset(self, t: time) -> int:
        return (t.hour * 60 + t.minute) - (
            self.rth_start.hour * 60 + self.rth_start.minute
        )

    def _minutes_since_open(self, local: datetime) -> float:
        start = local.replace(
            hour=self.rth_start.hour,
            minute=self.rth_start.minute,
            second=0,
            microsecond=0,
        )
        return (local - start).total_seconds() / 60

    def _in_rth(self, local: datetime) -> bool:
        return self.rth_start <= local.time() < self.rth_end

    def _finalize(self, session: _Session) -> None:
        if session.open_valid and session.minutes >= self.min_session_minutes:
            self._history.append(session.curve())
        self._prior = session

    def _on_price(
        self,
        t: datetime,
        price: float,
        vwap_price: float,
        volume: float,
        symbol: Optional[str],
        open_price: Optional[float] = None,
    ) -> None:
        local = t.astimezone(self.tz)
        if not self._in_rth(local):
            return

        day = local.date()
        since_open = self._minutes_since_open(local)
        if self._current is None or self._current.day != day:
            if self._current is not None and self._current.day < day:
                self._finalize(self._current)
            self._current = _Session(
                day=day,
                open=open_price if open_price is not None else price,
                open_valid=since_open <= self.max_open_delay,
                first_t=t,
                last_t=t,
                close=price,
                symbol=symbol,
                closes=[None] * self.session_minutes,
            )
            self._next_check = 0

        s = self._current
        s.last_t, s.close = t, price
        s.closes[min(int(since_open), self.session_minutes - 1)] = price
        s.pv += vwap_price * volume
        s.volume += volume

    def _seed(self, candles: List[Dict[str, Any]]) -> None:
        for candle in candles:
            t = candle["t"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            typical = (candle["h"] + candle["l"] + candle["c"]) / 3
            self._on_price(
                t,
                candle["c"],
                typical,
                candle.get("v", 0),
                candle.get("symbol"),
                open_price=candle["o"],
            )
        if self._history or self._current is not None:
            self.logger.info(
                f"NoiseArea seeded: {len(self._history)} sessions of history, "
                f"prior={self._prior.day if self._prior else None}, "
                f"current={self._current.day if self._current else None}"
            )

    def history_size(self) -> int:
        return len(self._history)

    def is_flat_time(self, t: datetime) -> bool:
        return t.astimezone(self.tz).time() >= self.flat_time

    def _skip_reason(self, tick: Tick) -> Optional[str]:
        s, prior = self._current, self._prior
        if s is None:
            return "no session"
        if s.day.strftime("%Y%m%d") in self.skip_dates:
            return "date is in skip_dates"
        if not s.open_valid:
            return "the session's open was not seen"
        if len(self._history) < self.lookback_days:
            return f"history has {len(self._history)}/{self.lookback_days} sessions"
        if prior is None or prior.day >= s.day:
            return "no prior session"
        if (s.day - prior.day).days > self.max_prior_session_age_days:
            return f"prior session {prior.day} is too old"
        if prior.symbol is not None and prior.symbol != tick.symbol:
            return f"symbol changed {prior.symbol} -> {tick.symbol}"
        return None

    def bands(self, checkpoint: int) -> Optional[Bands]:
        """The noise area at a checkpoint (minutes after the open); None if not tradable."""
        s, prior = self._current, self._prior
        if s is None or prior is None or len(self._history) < self.lookback_days:
            return None
        k = checkpoint - 1  # The minute that ends at the checkpoint
        sigma = (
            self.band_multiplier * sum(c[k] for c in self._history) / len(self._history)
        )
        return Bands(
            upper=max(s.open, prior.close) * (1 + sigma),
            lower=min(s.open, prior.close) * (1 - sigma),
            vwap=s.vwap,
            sigma=sigma,
        )

    def _due_checkpoint(self, local: datetime) -> Optional[int]:
        since_open = self._minutes_since_open(local)
        due = None
        while (
            self._next_check < len(self.checkpoints)
            and self.checkpoints[self._next_check] <= since_open
        ):
            due = self.checkpoints[self._next_check]
            self._next_check += 1
        if due is None or since_open - due > _MAX_CHECK_DELAY_MINUTES:
            return None
        return due

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        """
        Update sessions on every tick. At a checkpoint, set exit_requested if the open
        position (kwargs["direction"]) should close, and return a Signal to enter if flat
        (or exiting) and price is outside the noise area.
        """
        direction: Optional[str] = kwargs.get("direction")
        self.exit_requested = False

        self._on_price(tick.t, tick.price, tick.price, tick.size, tick.symbol)

        local = tick.t.astimezone(self.tz)
        s = self._current
        if s is None or s.day != local.date() or not self._in_rth(local):
            return None

        checkpoint = self._due_checkpoint(local)
        if checkpoint is None:
            return None

        reason = self._skip_reason(tick)
        bands = self.bands(checkpoint) if reason is None else None
        if reason is not None or bands is None:
            if self._skip_logged != s.day:
                self.logger.info(
                    f"[{self._ct(tick.t)}] NOISE AREA not trading: {reason}"
                )
                self._skip_logged = s.day
            return None

        price = tick.price
        long_stop = bands.upper
        short_stop = bands.lower
        if self.use_vwap_stop and bands.vwap is not None:
            long_stop = max(bands.upper, bands.vwap)
            short_stop = min(bands.lower, bands.vwap)

        vwap_str = f"{bands.vwap:.2f}" if bands.vwap is not None else "n/a"
        context = (
            f"price={price} upper={bands.upper:.2f} lower={bands.lower:.2f} "
            f"vwap={vwap_str} sigma={bands.sigma:.5f}"
        )
        self.logger.debug(f"[{self._ct(tick.t)}] NOISE AREA checkpoint {context}")

        if direction == "LONG" and price < long_stop:
            self.exit_requested = True
        elif direction == "SHORT" and price > short_stop:
            self.exit_requested = True

        if direction is not None and not self.exit_requested:
            return None

        if price > bands.upper and self.direction != "SHORT":
            new_direction = "LONG"
        elif price < bands.lower and self.direction != "LONG":
            new_direction = "SHORT"
        else:
            return None

        sign = 1 if new_direction == "LONG" else -1
        stop_loss = (
            self._round(price - sign * self.stop_loss_points)
            if self.stop_loss_points is not None
            else 0.0
        )
        self.logger.info(f"[{self._ct(tick.t)}] {new_direction} NOISE AREA {context}")

        return Signal(
            timestamp=tick.t,
            direction=new_direction,
            entry=price,
            size=self.num_contracts,
            stop_target=stop_loss,
        )

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return noise_area_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return noise_area_handler

    def __repr__(self) -> str:
        return (
            f"NoiseArea(lookback={self.lookback_days}d, x{self.band_multiplier}, "
            f"checks={len(self.checkpoints)}, vwap_stop={self.use_vwap_stop})"
        )


def _exit(
    state: TickerState,
    logger: logging.Logger,
    strategy: NoiseArea,
    tick: Tick,
    price: float,
    reason: str,
) -> None:
    position = state.position
    if position is None:
        return

    pnl = position.close(price)
    state.total_pnl += pnl

    log_with_color(
        logger,
        f"[{strategy._ct(tick.t)}] Noise area {reason}, "
        f"Start = {strategy._ct(position.timestamp)}, End = {strategy._ct(tick.t)}, "
        f"PnL = ${pnl:.2f}",
        Fore.GREEN if pnl > 0 else Fore.RED,
        "info",
    )
    state.position = None


def _stop_hit(position: Position, price: float) -> bool:
    if position.stop_loss == 0.0:
        return False
    if position.direction == "LONG":
        return price <= position.stop_loss
    return price >= position.stop_loss


def noise_area_handler(tick: Tick, logger: logging.Logger, state: TickerState) -> None:
    if type(state.strategy) != NoiseArea:
        raise ValueError(
            f"Expected NoiseArea strategy in state, got {type(state.strategy)}"
        )

    strategy = state.strategy
    position = state.position

    signal = strategy.check(
        tick, direction=position.direction if position is not None else None
    )

    if position is not None:
        if strategy.is_flat_time(tick.t):
            _exit(state, logger, strategy, tick, tick.price, "time exit")
            return
        if _stop_hit(position, tick.price):
            _exit(state, logger, strategy, tick, position.stop_loss, "stop loss")
            return
        if strategy.exit_requested:
            _exit(state, logger, strategy, tick, tick.price, "exit (back inside)")

    if state.position is None and signal is not None:
        if strategy.is_flat_time(tick.t):
            return
        state.position = Position(
            timestamp=signal.timestamp,
            direction=signal.direction,
            entries=[Entry(price=signal.entry, size=signal.size)],
            tick_size=strategy.tick_size,
            tick_value=strategy.tick_value,
            stop_loss=signal.stop_target,
            order_manager=state.order_manager,
        )
