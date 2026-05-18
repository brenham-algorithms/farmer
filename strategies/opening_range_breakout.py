import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Callable
from zoneinfo import ZoneInfo
 
from colorama import Fore

from calculations.opening_range import LiveOpeningRange
from config import log_with_color
from core import Tick
from api.models import OpeningRangeBreakoutParams
from strategies.vwap_mean_reversion import BandAttempt
 
 
class OpeningRangeBreakout:
    """
    Opening Range Breakout (ORB) strategy with delta confirmation.
 
    Defines an "opening range" as the high and low of the first N minutes
    after the cash session open (default: 8:30-8:45 CT for ES/MES).
 
    Entry flow:
      1. Opening range locks after the window closes.
      2. Price breaks above range high or below range low.
      3. BandAttempt starts — accumulates delta for confirmation.
      4. Enter when delta confirms breakout direction.
         (Set attempt_seconds=0 to skip confirmation.)
 
    Exit:
      - TP: all contracts close at 1x range size from entry.
      - SL: all contracts close at opposite side of the range.
      - Time exit: close everything at exit_hour:exit_minute.
 
    One trade per session. If stopped out, the day's thesis is dead.
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: OpeningRangeBreakoutParams,
    ) -> None:
        self.logger = logger
 
        # Core
        self.tick_size = params.tick_size
        self.precision = params.precision
        self.num_contracts = params.num_contracts
        self.min_range_ticks = params.min_range_ticks
        self.max_range_ticks = params.max_range_ticks
        self.cooldown_seconds = params.cooldown_seconds
        self.tp_range_multiplier = params.tp_range_multiplier
 
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
 
        # Opening range calculator
        self.opening_range = LiveOpeningRange(
            or_start_hour=params.or_start_hour,
            or_start_minute=params.or_start_minute,
            or_duration_minutes=params.or_duration_minutes,
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
 
        # State
        self._attempt: Optional[BandAttempt] = None
        self._cooldown_until: Optional[datetime] = None
        self._traded_this_session: bool = False
        self._last_session_key: Optional[datetime] = None
 
    def check(
        self, tick: Tick, timestamp: Any = None, **kwargs: Any
    ) -> Dict[str, Any] | None:
        or_high = kwargs.get("or_high")
        or_low = kwargs.get("or_low")
        or_locked = kwargs.get("or_locked", False)
        range_size = kwargs.get("range_size")
 
        if not or_locked or or_high is None or or_low is None or range_size is None:
            return None
 
        # Reset traded flag on new session
        session_key = self.opening_range._session_key(tick.t)
        if session_key != self._last_session_key:
            self._traded_this_session = False
            self._attempt = None
            self._last_session_key = session_key
 
        if self._traded_this_session:
            return None
 
        if self._cooldown_until is not None and tick.t < self._cooldown_until:
            return None
 
        # Don't enter past exit time
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
        if range_ticks < self.min_range_ticks:
            return None
        if range_ticks > self.max_range_ticks:
            return None
 
        now = tick.t
        delta = tick.delta()
 
        # --- Active attempt: update and check confirmation ---
        if self._attempt is not None:
            if self._attempt.is_expired(now):
                self.logger.debug("ORB attempt expired without confirmation")
                self._attempt = None
                return None
 
            # Cancel if price pulls back inside the range
            if or_low <= tick.price <= or_high:
                self._attempt = None
                return None
 
            self._attempt.on_tick(now, tick.price, delta, tick.size)
 
            if self._confirmed(self._attempt):
                return self._build_entry(
                    self._attempt, tick, or_high, or_low, range_size, timestamp
                )
 
            return None
 
        # --- Breakout detection ---
        direction = None
        if tick.price > or_high:
            direction = "LONG"
        elif tick.price < or_low:
            direction = "SHORT"
 
        if direction is None:
            return None
 
        # No confirmation
        if self.attempt_seconds <= 0:
            return self._build_entry_direct(
                tick, direction, or_high, or_low, range_size, timestamp
            )
 
        # Start confirmation attempt
        self._attempt = BandAttempt(
            direction=direction,
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
            f"ORB attempt started: {direction} @ {tick.price} "
            f"range=[{or_low}, {or_high}]"
        )
 
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
        attempt: BandAttempt,
        tick: Tick,
        or_high: float,
        or_low: float,
        range_size: float,
        timestamp: Any,
    ) -> Dict[str, Any]:
        direction = attempt.direction
        dr = attempt.delta_ratio()
        vol = attempt.sum_volume
        absorbed = attempt.absorbed_volume
        self._attempt = None
 
        entry = tick.price
        tp_distance = range_size * self.tp_range_multiplier
 
        if direction == "LONG":
            stop_loss = round(or_low, self.precision)
            take_profit = round(entry + tp_distance, self.precision)
        else:
            stop_loss = round(or_high, self.precision)
            take_profit = round(entry - tp_distance, self.precision)
 
        self._traded_this_session = True
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        self.logger.info(
            f"{direction} ORB CONFIRMED at {entry} "
            f"range=[{or_low:.{self.precision}f}, {or_high:.{self.precision}f}] "
            f"dr={dr:.3f} vol={vol} absorbed={absorbed} "
            f"tp={take_profit} stop={stop_loss}",
        )
 
        return {
            "timestamp": timestamp,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "num_contracts": self.num_contracts,
        }
 
    def _build_entry_direct(
        self,
        tick: Tick,
        direction: str,
        or_high: float,
        or_low: float,
        range_size: float,
        timestamp: Any,
    ) -> Dict[str, Any]:
        entry = tick.price
        tp_distance = range_size * self.tp_range_multiplier
 
        if direction == "LONG":
            stop_loss = round(or_low, self.precision)
            take_profit = round(entry + tp_distance, self.precision)
        else:
            stop_loss = round(or_high, self.precision)
            take_profit = round(entry - tp_distance, self.precision)
 
        self._traded_this_session = True
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        range_ticks = range_size / self.tick_size
 
        self.logger.info(
            f"{direction} ORB breakout at {entry} "
            f"range=[{or_low:.{self.precision}f}, {or_high:.{self.precision}f}] "
            f"size={range_size:.{self.precision}f} ({range_ticks:.0f} ticks) "
            f"tp={take_profit} stop={stop_loss}",
        )
 
        return {
            "timestamp": timestamp,
            "direction": direction,
            "entry": entry,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "num_contracts": self.num_contracts,
        }
 
    def on_stop_loss(self) -> None:
        self._attempt = None

    def get_handler(self) -> Callable:
        return _opening_range_breakout_handler
 
    def reset(self) -> None:
        self._attempt = None
        self._cooldown_until = None
        self._traded_this_session = False
 
    def __repr__(self) -> str:
        or_ = self.opening_range
        if or_.is_locked:
            return (
                f"OpeningRangeBreakout(range=[{or_.low:.4f}, {or_.high:.4f}], "
                f"size={or_.range_size:.4f})"
            )
        return "OpeningRangeBreakout(range=pending)"


def _opening_range_breakout_handler(
    tick: Tick, logger: logging.Logger, state: Dict[str, Any]
) -> None:
    strategy = state["strategy"]
 
    # Handler owns the opening range update
    strategy.opening_range.on_tick(tick)
 
    tick_size = state["tick_size"]
    tick_value = state["tick_value"]
    position = state["position"]
 
    # --- Time exit check ---
    t_local = tick.t.astimezone(ZoneInfo("America/Chicago"))
    exit_time = t_local.replace(
        hour=strategy.exit_hour,
        minute=strategy.exit_minute,
        second=0,
        microsecond=0,
    )
 
    if position is not None and t_local >= exit_time:
        _orb_close(position, tick.price, "time_exit", tick, tick_size, tick_value, state, logger)
        return
 
    # --- No position: check for entry ---
    if position is None:
        or_ = strategy.opening_range
        signal = strategy.check(
            tick, tick.t,
            or_high=or_.high,
            or_low=or_.low,
            or_locked=or_.is_locked,
            range_size=or_.range_size,
        )
        if signal is not None:
            state["position"] = {
                "direction": signal["direction"],
                "timestamp": signal["timestamp"],
                "entry": signal["entry"],
                "stop_loss": signal["stop_loss"],
                "take_profit": signal["take_profit"],
                "num_contracts": signal["num_contracts"],
            }
        return
 
    direction = position["direction"]
 
    # --- Stop loss ---
    if direction == "LONG" and tick.price <= position["stop_loss"]:
        _orb_close(position, position["stop_loss"], "stop_loss", tick, tick_size, tick_value, state, logger)
        strategy.on_stop_loss()
        return
 
    if direction == "SHORT" and tick.price >= position["stop_loss"]:
        _orb_close(position, position["stop_loss"], "stop_loss", tick, tick_size, tick_value, state, logger)
        strategy.on_stop_loss()
        return
 
    # --- Take profit ---
    if direction == "LONG" and tick.price >= position["take_profit"]:
        _orb_close(position, position["take_profit"], "take_profit", tick, tick_size, tick_value, state, logger)
        return
 
    if direction == "SHORT" and tick.price <= position["take_profit"]:
        _orb_close(position, position["take_profit"], "take_profit", tick, tick_size, tick_value, state, logger)
        return
 
 
def _orb_close(
    position: Dict[str, Any],
    exit_price: float,
    reason: str,
    tick: Tick,
    tick_size: float,
    tick_value: float,
    state: Dict[str, Any],
    logger: logging.Logger,
) -> None:
    direction = position["direction"]
    num = position["num_contracts"]
 
    if direction == "LONG":
        per_contract = (exit_price - position["entry"]) / tick_size * tick_value
    else:
        per_contract = (position["entry"] - exit_price) / tick_size * tick_value
 
    total_pnl = round(per_contract * num, 2)
    state["total_pnl"] += total_pnl
 
    ts_start = (
        position["timestamp"]
        .replace(microsecond=0)
        .astimezone(ZoneInfo("America/Chicago"))
    )
    ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
    log_with_color(
        logger,
        f"ORB closed ({reason}), Start = {ts_start}, End = {ts_end}, "
        f"PnL = ${total_pnl:.2f} ({num} contract{'s' if num > 1 else ''})",
        Fore.GREEN if total_pnl > 0 else Fore.RED,
        "info",
    )
 
    state["position"] = None
