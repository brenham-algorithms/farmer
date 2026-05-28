import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
 
from colorama import Fore
 
from api.models import OrbParams
from calculations.opening_range import LiveOpeningRange
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState
 
 
class OpeningRangeBreakout:
    """
    Opening Range Breakout with reversion confirmation.
 
    Flow:
      1. Opening range (OR) locks after the window closes.
      2. Price must break out beyond OR boundary by breakout_ticks.
      3. Price must revert back to the OR boundary (within reversion_ticks).
      4. A BandAttempt starts at the OR boundary, waiting for delta
         confirmation of a bounce off the level.
      5. On confirmation: enter num_contracts in the breakout direction.
 
    Exit:
      - SL at risk_range_multiplier * range_size from entry.
      - TP at tp_range_multiplier * range_size from entry.
      - On TP hit: cut tp_contracts, remaining become the "runner."
      - Runner has a trailing stop at trail_ticks behind high water mark.
      - Time exit closes everything at exit_hour:exit_minute.
 
    One trade per session.
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: OrbParams,
    ) -> None:
        self.logger = logger
 
        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts
        self.tp_contracts = params.tp_contracts
 
        # Range filters
        self.min_range_ticks = params.min_range_ticks
        self.max_range_ticks = params.max_range_ticks
 
        # Breakout and reversion detection
        self.breakout_ticks = params.breakout_ticks
        self.reversion_ticks = params.reversion_ticks
        self.max_penetration_ticks = params.max_penetration_ticks
 
        # Risk/reward
        self.tp_range_multiplier = params.tp_range_multiplier
        self.risk_range_multiplier = params.risk_range_multiplier
 
        # Runner trailing stop
        self.trail_ticks = params.trail_ticks
 
        # Time exit
        self.exit_hour = params.exit_hour
        self.exit_minute = params.exit_minute
        self.tz = ZoneInfo("America/Chicago")
 
        # Confirmation
        self.attempt_seconds = params.attempt_seconds
        self.delta_ratio_threshold = params.delta_ratio_threshold
        self.min_response_ticks = params.min_response_ticks
        self.min_attempt_volume = params.min_attempt_volume
        self.min_absorbed_volume = params.min_absorbed_volume
        self.absorption_ticks = params.absorption_ticks
        self.cooldown_seconds = params.cooldown_seconds
 
        # Opening range calculator
        self.opening_range = LiveOpeningRange(
            or_start_hour=params.or_start_hour,
            or_start_minute=params.or_start_minute,
            or_duration_minutes=params.or_duration_minutes,
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
 
        # State
        self._breakout_direction: Optional[str] = None
        self._breakout_detected: bool = False
        self._reversion_detected: bool = False
        self._attempt: Optional[BandAttempt] = None
        self._traded_this_session: bool = False
        self._last_session_key: Optional[datetime] = None
 
    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        or_high = kwargs.get("or_high")
        or_low = kwargs.get("or_low")
        or_locked = kwargs.get("or_locked", False)
        range_size = kwargs.get("range_size")
 
        if not or_locked or or_high is None or or_low is None or range_size is None:
            return None
 
        # Session reset
        session_key = self.opening_range._session_key(tick.t)
        if session_key != self._last_session_key:
            self._traded_this_session = False
            self._breakout_direction = None
            self._breakout_detected = False
            self._reversion_detected = False
            self._attempt = None
            self._last_session_key = session_key
 
        if self._traded_this_session:
            return None
 
        # Time filter
        t_local = tick.t.astimezone(self.tz)
        exit_time = t_local.replace(
            hour=self.exit_hour,
            minute=self.exit_minute,
            second=0,
            microsecond=0,
        )
        if t_local >= exit_time:
            return None
 
        # Range size filter
        range_ticks = range_size / self.tick_size
        if range_ticks < self.min_range_ticks or range_ticks > self.max_range_ticks:
            return None
 
        now = tick.t
        delta = tick.delta()
        breakout_dist = self.breakout_ticks * self.tick_size
        reversion_dist = self.reversion_ticks * self.tick_size
 
        # --- (A) Active attempt: update and check confirmation ---
        if self._attempt is not None:
            if self._attempt.is_expired(now):
                self.logger.debug("ORB attempt expired without confirmation")
                self._attempt = None
                self._reversion_detected = False
                return None
 
            # Cancel if price penetrates too far inside the range
            max_pen = self.max_penetration_ticks * self.tick_size
            if self._breakout_direction == "LONG" and tick.price < or_high - max_pen:
                self.logger.debug("ORB attempt cancelled: price penetrated too far below OR high")
                self._attempt = None
                self._reversion_detected = False
                return None
            elif self._breakout_direction == "SHORT" and tick.price > or_low + max_pen:
                self.logger.debug("ORB attempt cancelled: price penetrated too far above OR low")
                self._attempt = None
                self._reversion_detected = False
                return None
 
            self._attempt.on_tick(now, tick.price, delta, tick.size)
 
            if self._confirmed(self._attempt):
                return self._build_entry(tick, or_high, or_low, range_size)
 
            return None
 
        # --- (B) Detect breakout ---
        if not self._breakout_detected:
            if tick.price > or_high + breakout_dist:
                self._breakout_detected = True
                self._breakout_direction = "LONG"
                self.logger.debug(
                    f"ORB breakout detected: LONG @ {tick.price} "
                    f"(or_high={or_high}, threshold={or_high + breakout_dist})"
                )
            elif tick.price < or_low - breakout_dist:
                self._breakout_detected = True
                self._breakout_direction = "SHORT"
                self.logger.debug(
                    f"ORB breakout detected: SHORT @ {tick.price} "
                    f"(or_low={or_low}, threshold={or_low - breakout_dist})"
                )
            return None
 
        # --- (C) Detect reversion to OR boundary ---
        if not self._reversion_detected:
            if self._breakout_direction == "LONG":
                if tick.price <= or_high + reversion_dist:
                    self._reversion_detected = True
                    self.logger.debug(
                        f"ORB reversion detected: price={tick.price} back to or_high={or_high}"
                    )
            elif self._breakout_direction == "SHORT":
                if tick.price >= or_low - reversion_dist:
                    self._reversion_detected = True
                    self.logger.debug(
                        f"ORB reversion detected: price={tick.price} back to or_low={or_low}"
                    )
 
            if not self._reversion_detected:
                return None
 
            # Start confirmation attempt
            self._attempt = BandAttempt(
                direction=self._breakout_direction,
                start_t=now,
                expire_t=now + timedelta(seconds=self.attempt_seconds),
                start_price=tick.price,
                min_price=tick.price,
                max_price=tick.price,
                last_price=tick.price,
                tick_size=self.tick_size,
                absorption_ticks=self.absorption_ticks,
            )
            self._attempt.on_tick(now, tick.price, delta, tick.size)
 
            self.logger.debug(
                f"ORB attempt started: {self._breakout_direction} @ {tick.price}"
            )
            return None
 
        return None
 
    def _confirmed(self, attempt: BandAttempt) -> bool:
        if attempt.sum_volume < self.min_attempt_volume:
            return False
 
        dr = attempt.delta_ratio()
        if attempt.direction == "LONG":
            if dr < self.delta_ratio_threshold:
                return False
        else:
            if dr > -self.delta_ratio_threshold:
                return False
 
        min_resp = self.min_response_ticks * self.tick_size
        if attempt.direction == "LONG":
            if (attempt.last_price - attempt.min_price) < min_resp:
                return False
        else:
            if (attempt.max_price - attempt.last_price) < min_resp:
                return False
 
        if self.min_absorbed_volume > 0:
            if attempt.absorbed_volume < self.min_absorbed_volume:
                return False
 
        return True
 
    def _build_entry(
        self,
        tick: Tick,
        or_high: float,
        or_low: float,
        range_size: float,
    ) -> Signal:
        direction = self._attempt.direction
        entry = tick.price
        dr = self._attempt.delta_ratio()
        vol = self._attempt.sum_volume
        absorbed = self._attempt.absorbed_volume
 
        tp_dist = range_size * self.tp_range_multiplier
        sl_dist = range_size * self.risk_range_multiplier
 
        if direction == "LONG":
            take_profit = round(entry + tp_dist, self.precision)
            stop_loss = round(entry - sl_dist, self.precision)
        else:
            take_profit = round(entry - tp_dist, self.precision)
            stop_loss = round(entry + sl_dist, self.precision)
 
        self._traded_this_session = True
        self._attempt = None
 
        range_ticks = range_size / self.tick_size
 
        self.logger.info(
            f"{direction} ORB CONFIRMED at {entry} "
            f"range=[{or_low:.{self.precision}f}, {or_high:.{self.precision}f}] "
            f"({range_ticks:.0f} ticks) "
            f"dr={dr:.3f} vol={vol} absorbed={absorbed} "
            f"tp={take_profit} sl={stop_loss}",
        )
 
        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.num_contracts,
            profit_target=take_profit,
            stop_target=stop_loss,
        )
 
    def reset(self) -> None:
        self._breakout_direction = None
        self._breakout_detected = False
        self._reversion_detected = False
        self._attempt = None
        self._traded_this_session = False
 
    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return orb_handler
 
    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return orb_handler
 
    def __repr__(self) -> str:
        or_ = self.opening_range
        if or_.is_locked:
            return (
                f"OpeningRangeBreakout(range=[{or_.low:.4f}, {or_.high:.4f}], "
                f"size={or_.range_size:.4f})"
            )
        return "OpeningRangeBreakout(range=pending)"
 
 
def orb_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != OpeningRangeBreakout:
        raise ValueError(
            f"Expected OpeningRangeBreakout strategy in state, got {type(state.strategy)}"
        )
 
    strategy = state.strategy
 
    # Handler owns the opening range update
    strategy.opening_range.on_tick(tick)
 
    # Time exit check
    t_local = tick.t.astimezone(strategy.tz)
    exit_time = t_local.replace(
        hour=strategy.exit_hour,
        minute=strategy.exit_minute,
        second=0,
        microsecond=0,
    )
 
    position = state.position
 
    if position is not None and t_local >= exit_time:
        pnl = position.close(tick.price)
        state.total_pnl += pnl
 
        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"ORB time exit, Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f} ({position.num_contracts()} contract{'s' if position.num_contracts() > 1 else ''})",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
        return
 
    # No position: check for entry
    if position is None:
        or_ = strategy.opening_range
        signal = strategy.check(
            tick,
            or_high=or_.high,
            or_low=or_.low,
            or_locked=or_.is_locked,
            range_size=or_.range_size,
        )
        if signal is not None:
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                take_profit=signal.profit_target,
                stop_loss=signal.stop_target,
            )
        return
 
    direction = position.direction
 
    # --- Not yet unwinding: check SL and TP ---
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
 
            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
 
            log_with_color(
                logger,
                f"ORB stop loss, Start = {ts_start}, End = {ts_end}, "
                f"PnL = ${pnl:.2f}",
                Fore.RED,
                "info",
            )
            state.position = None
            return
 
        # Take profit: cut tp_contracts
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
 
                ts_start = position.timestamp.replace(microsecond=0).astimezone(
                    ZoneInfo("America/Chicago")
                )
                ts_end = tick.t.replace(microsecond=0).astimezone(
                    ZoneInfo("America/Chicago")
                )
 
                log_with_color(
                    logger,
                    f"ORB take profit (all), Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f}",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
                state.position = None
                return
 
            # Partial close: cut tp_contracts, activate runner
            pnl = position.cut(tp_contracts, position.take_profit)
            state.total_pnl += pnl
            position.unwinding = True
 
            # Set trailing stop for the runner
            trail_dist = strategy.trail_ticks * strategy.tick_size
            if direction == "LONG":
                position.stop_loss = tick.price - trail_dist
            else:
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
                f"ORB take profit ({tp_contracts} closed), Start = {ts_start}, End = {ts_end}, "
                f"PnL = ${pnl:.2f} ({remaining} runner{'s' if remaining > 1 else ''} trailing)",
                Fore.GREEN if pnl > 0 else Fore.RED,
                "info",
            )
            return
 
    # --- Unwinding: trailing stop on runner ---
    if position.unwinding:
        trail_dist = strategy.trail_ticks * strategy.tick_size
 
        # Ratchet trailing stop
        if direction == "LONG":
            new_stop = tick.price - trail_dist
            if new_stop > position.stop_loss:
                position.stop_loss = new_stop
        else:
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
 
            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
 
            log_with_color(
                logger,
                f"ORB runner trailing stop, Start = {ts_start}, End = {ts_end}, "
                f"PnL = ${pnl:.2f}",
                Fore.GREEN if pnl > 0 else Fore.RED,
                "info",
            )
            state.position = None
