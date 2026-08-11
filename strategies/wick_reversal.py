import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore, Style

from api.models import WickReversalParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from tickers import TickerState


@dataclass
class InternalCandle:
    open: float
    high: float
    low: float
    close: float
    volume: int

    def upper_wick(self) -> float:
        return self.high - max(self.open, self.close)

    def lower_wick(self) -> float:
        return min(self.open, self.close) - self.low


class WickReversal:
    """
    Enters a position when the previous candle has a significant wick,
    indicating strong rejection and potential reversal.

    When a new candle starts, the strategy evaluates the previous candle:
      - Lower wick >= min_wick_ticks → LONG (strong support/rejection)
      - Upper wick >= min_wick_ticks → SHORT (strong resistance/rejection)
      - Both wicks significant and close in length → skip (indecision)

    Position management:
      - Stop at stop_wick_pct x wick length from entry
      - TP at tp_wick_pct x wick length → cut tp_contracts
      - Remaining contracts trail at trail_wick_pct x wick length

    Daily realized PnL limits stop new entries but don't close open trades.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: WickReversalParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts

        # Candle
        self.candle_minutes = params.candle_minutes

        # Wick detection
        self.min_wick_ticks = params.min_wick_ticks
        self.min_wick_size = params.min_wick_ticks * self.tick_size
        self.wick_ratio_threshold = params.wick_ratio_threshold

        # Candle volume filters
        self.min_candle_volume = params.min_candle_volume
        self.max_candle_volume = params.max_candle_volume

        # Lookback filter; wick must be at an extreme of last N candles
        self.lookback_candles = params.lookback_candles

        # Position management
        self.stop_wick_pct = params.stop_wick_pct
        self.tp_wick_pct = params.tp_wick_pct
        self.tp_contracts = params.tp_contracts
        self.trail_wick_pct = params.trail_wick_pct

        # Daily limits
        self.daily_loss_limit = params.daily_loss_limit
        self.daily_tp_limit = params.daily_tp_limit

        # Trading hours
        self.trading_start_hour = params.trading_start_hour
        self.trading_end_hour = params.trading_end_hour

        # Session
        self.session_reset_hour = params.session_reset_hour
        self.session_reset_minute = params.session_reset_minute

        # Internal candle state
        self._candle: Optional[InternalCandle] = None
        self._candle_key: Optional[datetime] = None
        self._first_candle_discarded: bool = False

        # Pending signal (emitted on first tick of new candle)
        self._pending_signal: Optional[Dict[str, Any]] = None

        # Candle history for lookback filter
        self._candle_history: deque[InternalCandle] = deque(
            maxlen=self.lookback_candles if self.lookback_candles else None
        )

        # Daily PnL tracking
        self._daily_pnl: float = 0.0
        self._current_session_key: Optional[datetime] = None

        self.logger.info(
            f"WickReversal initialized: candle_minutes={self.candle_minutes} "
            f"min_wick_ticks={self.min_wick_ticks} "
            f"stop_pct={self.stop_wick_pct} tp_pct={self.tp_wick_pct} "
            f"trail_pct={self.trail_wick_pct}"
        )

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _session_key(self, t_utc: datetime) -> datetime:
        t_local = t_utc.astimezone(self.tz)
        reset_today = t_local.replace(
            hour=self.session_reset_hour,
            minute=self.session_reset_minute,
            second=0,
            microsecond=0,
        )
        if t_local < reset_today:
            return reset_today - timedelta(days=1)
        return reset_today

    def _candle_minute_key(self, t_utc: datetime) -> datetime:
        """Floor timestamp to candle boundary."""
        t_local = t_utc.astimezone(self.tz)
        minute = (t_local.minute // self.candle_minutes) * self.candle_minutes
        return t_local.replace(minute=minute, second=0, microsecond=0)

    def _evaluate_candle(self, candle: InternalCandle, close_time: datetime) -> None:
        """Evaluate a completed candle for wick signal."""
        # Volume filters
        if (
            self.min_candle_volume is not None
            and candle.volume < self.min_candle_volume
        ):
            self.logger.debug(
                f"[{self._ct(close_time)}] Candle volume {candle.volume} below "
                f"min {self.min_candle_volume}, skipping"
            )
            return

        if (
            self.max_candle_volume is not None
            and candle.volume > self.max_candle_volume
        ):
            self.logger.debug(
                f"[{self._ct(close_time)}] Candle volume {candle.volume} above "
                f"max {self.max_candle_volume}, skipping"
            )
            return

        upper = candle.upper_wick()
        lower = candle.lower_wick()

        upper_qualifies = upper >= self.min_wick_size
        lower_qualifies = lower >= self.min_wick_size

        # Neither wick qualifies
        if not upper_qualifies and not lower_qualifies:
            return

        # Both qualify; check if they're too close in length
        if upper_qualifies and lower_qualifies:
            longer = max(upper, lower)
            shorter = min(upper, lower)
            if longer > 0 and shorter / longer >= self.wick_ratio_threshold:
                self.logger.debug(
                    f"[{self._ct(close_time)}] Both wicks significant and close in length "
                    f"(upper={upper / self.tick_size:.0f}t, lower={lower / self.tick_size:.0f}t, "
                    f"ratio={shorter / longer:.2f}), skipping"
                )
                return
            # Trade the longer wick
            if lower > upper:
                direction = "LONG"
                wick_length = lower
            else:
                direction = "SHORT"
                wick_length = upper
        elif lower_qualifies:
            direction = "LONG"
            wick_length = lower
        else:
            direction = "SHORT"
            wick_length = upper

        wick_ticks = wick_length / self.tick_size

        # Lookback filter; wick must be at an extreme of recent candles
        if self.lookback_candles and len(self._candle_history) >= self.lookback_candles:
            if direction == "LONG":
                lowest_low = min(c.low for c in self._candle_history)
                if candle.low > lowest_low:
                    self.logger.debug(
                        f"[{self._ct(close_time)}] Lower wick at {candle.low:.{self.precision}f} "
                        f"not the lowest low ({lowest_low:.{self.precision}f}) "
                        f"in last {self.lookback_candles} candles, skipping"
                    )
                    return
            else:
                highest_high = max(c.high for c in self._candle_history)
                if candle.high < highest_high:
                    self.logger.debug(
                        f"[{self._ct(close_time)}] Upper wick at {candle.high:.{self.precision}f} "
                        f"not the highest high ({highest_high:.{self.precision}f}) "
                        f"in last {self.lookback_candles} candles, skipping"
                    )
                    return

        self.logger.info(
            f"[{self._ct(close_time)}] WICK SIGNAL: {Fore.GREEN if direction == 'LONG' else Fore.RED}{direction}{Style.RESET_ALL} "
            f"wick={wick_ticks:.0f} ticks "
            f"O={candle.open:.{self.precision}f} H={candle.high:.{self.precision}f} "
            f"L={candle.low:.{self.precision}f} C={candle.close:.{self.precision}f}"
        )

        self._pending_signal = {
            "direction": direction,
            "wick_length": wick_length,
        }

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        now = tick.t

        # Session reset
        session = self._session_key(now)
        if session != self._current_session_key:
            self._daily_pnl = 0.0
            self._current_session_key = session
            self._candle = None
            self._candle_key = None
            self._first_candle_discarded = False
            self._pending_signal = None
            self._candle_history.clear()

        candle_key = self._candle_minute_key(now)

        # First tick ever; start building first candle (will be discarded)
        if self._candle_key is None:
            self._candle_key = candle_key
            self._candle = InternalCandle(
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.size,
            )
            return None

        # New candle boundary
        if candle_key != self._candle_key:
            if not self._first_candle_discarded:
                # Discard the first (partial) candle
                self._first_candle_discarded = True
            elif self._candle is not None:
                # Evaluate the completed candle
                self._evaluate_candle(self._candle, now)
                # Add to history for lookback filter
                self._candle_history.append(self._candle)

            # Start new candle
            self._candle_key = candle_key
            self._candle = InternalCandle(
                open=tick.price,
                high=tick.price,
                low=tick.price,
                close=tick.price,
                volume=tick.size,
            )

            # Emit pending signal on first tick of new candle
            in_position = kwargs.get("in_position", False)
            if self._pending_signal is not None:
                if in_position:
                    direction = self._pending_signal["direction"]
                    wick_ticks = self._pending_signal["wick_length"] / self.tick_size
                    self.logger.info(
                        f"[{self._ct(now)}] WICK SIGNAL (IN POSITION, SKIPPED): {direction} "
                        f"wick={wick_ticks:.0f}t"
                    )
                    self._pending_signal = None
                    return None

                # Check trading hours
                if (
                    self.trading_start_hour is not None
                    or self.trading_end_hour is not None
                ):
                    local_hour = now.astimezone(self.tz).hour
                    if (
                        self.trading_start_hour is not None
                        and local_hour < self.trading_start_hour
                    ):
                        self._pending_signal = None
                        return None
                    if (
                        self.trading_end_hour is not None
                        and local_hour >= self.trading_end_hour
                    ):
                        self._pending_signal = None
                        return None

                # Check daily limits before entering
                if self._daily_pnl <= self.daily_loss_limit:
                    self.logger.info(
                        f"[{self._ct(now)}] Daily loss limit hit (${self._daily_pnl:.2f}), "
                        f"skipping entry"
                    )
                    self._pending_signal = None
                    return None

                if self._daily_pnl >= self.daily_tp_limit:
                    self.logger.info(
                        f"[{self._ct(now)}] Daily TP limit hit (${self._daily_pnl:.2f}), "
                        f"skipping entry"
                    )
                    self._pending_signal = None
                    return None

                direction = self._pending_signal["direction"]
                wick_length = self._pending_signal["wick_length"]
                entry = tick.price

                if direction == "LONG":
                    stop_loss = round(
                        entry - self.stop_wick_pct * wick_length, self.precision
                    )
                    take_profit = round(
                        entry + self.tp_wick_pct * wick_length, self.precision
                    )
                else:
                    stop_loss = round(
                        entry + self.stop_wick_pct * wick_length, self.precision
                    )
                    take_profit = round(
                        entry - self.tp_wick_pct * wick_length, self.precision
                    )

                wick_ticks = wick_length / self.tick_size

                self.logger.info(
                    f"[{self._ct(now)}] {Fore.GREEN if direction == 'LONG' else Fore.RED}{direction}{Style.RESET_ALL} WICK REVERSAL at {entry} "
                    f"wick={wick_ticks:.0f}t "
                    f"tp={take_profit} sl={stop_loss} "
                    f"contracts={self.num_contracts}"
                )

                self._pending_signal = None

                return Signal(
                    timestamp=now,
                    direction=direction,
                    entry=entry,
                    size=self.num_contracts,
                    profit_target=take_profit,
                    stop_target=stop_loss,
                )

            return None

        # Update current candle
        if self._candle is not None:
            if tick.price > self._candle.high:
                self._candle.high = tick.price
            if tick.price < self._candle.low:
                self._candle.low = tick.price
            self._candle.close = tick.price
            self._candle.volume += tick.size

        return None

    def add_pnl(self, pnl: float) -> None:
        """Called by handler after each trade closes."""
        self._daily_pnl += pnl

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def reset(self) -> None:
        self._candle = None
        self._candle_key = None
        self._first_candle_discarded = False
        self._pending_signal = None
        self._candle_history.clear()
        self._daily_pnl = 0.0
        self._current_session_key = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return wick_reversal_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return wick_reversal_handler

    def __repr__(self) -> str:
        return (
            f"WickReversal(candle_min={self.candle_minutes}, "
            f"min_wick={self.min_wick_ticks}t, "
            f"daily_pnl=${self._daily_pnl:.2f})"
        )


def wick_reversal_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != WickReversal:
        raise ValueError(
            f"Expected WickReversal strategy in state, " f"got {type(state.strategy)}"
        )

    strategy = state.strategy
    position = state.position

    # No position; check for entry
    if position is None:
        signal = strategy.check(tick)
        if signal is not None:
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                take_profit=signal.profit_target,
                stop_loss=signal.stop_target,
                order_manager=state.order_manager,
            )
        return

    # Still need to update the candle even while in a position
    strategy.check(tick, in_position=True)

    direction = position.direction

    # Not yet unwinding; check SL and TP
    if not position.unwinding:
        # Stop loss: close all
        sl_hit = False
        if direction == "LONG" and tick.price <= position.stop_loss:
            sl_hit = True
        elif direction == "SHORT" and tick.price >= position.stop_loss:
            sl_hit = True

        if sl_hit:
            pnl = position.close(position.stop_loss)
            state.total_pnl += pnl
            strategy.add_pnl(pnl)

            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )

            log_with_color(
                logger,
                f"[{strategy._ct(tick.t)}] Wick reversal stop loss, "
                f"Start = {ts_start}, End = {ts_end}, "
                f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
                Fore.RED,
                "info",
            )
            state.position = None
            return

        # Take profit; cut tp_contracts
        tp_hit = False
        if direction == "LONG" and tick.price >= position.take_profit:
            tp_hit = True
        elif direction == "SHORT" and tick.price <= position.take_profit:
            tp_hit = True

        if tp_hit:
            tp_contracts = strategy.tp_contracts
            remaining_before = position.num_contracts()

            # If tp_contracts >= total, close everything
            if tp_contracts >= remaining_before:
                pnl = position.close(position.take_profit)
                state.total_pnl += pnl
                strategy.add_pnl(pnl)

                ts_start = position.timestamp.replace(microsecond=0).astimezone(
                    ZoneInfo("America/Chicago")
                )
                ts_end = tick.t.replace(microsecond=0).astimezone(
                    ZoneInfo("America/Chicago")
                )

                log_with_color(
                    logger,
                    f"[{strategy._ct(tick.t)}] Wick reversal take profit (all), "
                    f"Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
                state.position = None
                return

            # Partial close; cut tp_contracts, activate trailing
            pnl = position.cut(tp_contracts, position.take_profit)
            state.total_pnl += pnl
            strategy.add_pnl(pnl)
            position.unwinding = True

            # Calculate wick length from the signal's TP distance
            if direction == "LONG":
                wick_length = (
                    position.take_profit - position.entries[0].price
                ) / strategy.tp_wick_pct
                trail_dist = strategy.trail_wick_pct * wick_length
                position.stop_loss = tick.price - trail_dist
            else:
                wick_length = (
                    position.entries[0].price - position.take_profit
                ) / strategy.tp_wick_pct
                trail_dist = strategy.trail_wick_pct * wick_length
                position.stop_loss = tick.price + trail_dist

            remaining = position.num_contracts()

            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )

            log_with_color(
                logger,
                f"[{strategy._ct(tick.t)}] Wick reversal take profit "
                f"({tp_contracts} closed), "
                f"Start = {ts_start}, End = {ts_end}, "
                f"PnL = ${pnl:.2f} ({remaining} runner{'s' if remaining > 1 else ''} "
                f"trailing) (daily: ${strategy.daily_pnl:.2f})",
                Fore.GREEN if pnl > 0 else Fore.RED,
                "info",
            )
            return

    # Unwinding; trailing stop on runner
    if position.unwinding:
        # Recalculate wick length and trail distance
        if direction == "LONG":
            wick_length = (
                position.take_profit - position.entries[0].price
            ) / strategy.tp_wick_pct
            trail_dist = strategy.trail_wick_pct * wick_length
            new_stop = tick.price - trail_dist
            if new_stop > position.stop_loss:
                position.stop_loss = new_stop
        else:
            wick_length = (
                position.entries[0].price - position.take_profit
            ) / strategy.tp_wick_pct
            trail_dist = strategy.trail_wick_pct * wick_length
            new_stop = tick.price + trail_dist
            if new_stop < position.stop_loss:
                position.stop_loss = new_stop

        # Check trailing stop hit
        trail_hit = False
        if direction == "LONG" and tick.price <= position.stop_loss:
            trail_hit = True
        elif direction == "SHORT" and tick.price >= position.stop_loss:
            trail_hit = True

        if trail_hit:
            pnl = position.close(position.stop_loss)
            state.total_pnl += pnl
            strategy.add_pnl(pnl)

            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )

            log_with_color(
                logger,
                f"[{strategy._ct(tick.t)}] Wick reversal trailing stop, "
                f"Start = {ts_start}, End = {ts_end}, "
                f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
                Fore.GREEN if pnl > 0 else Fore.RED,
                "info",
            )
            state.position = None
