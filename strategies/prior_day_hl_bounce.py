import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
 
from colorama import Fore
 
from api.models import PriorDayHlBounceParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState
 
 
class AbsorptionAttempt:
    """
    Tracks delta, volume, absorption, and price response within a
    single attempt window across the full zone width.
    """
 
    def __init__(
        self,
        direction: str,
        start_t: datetime,
        expire_t: datetime,
        start_price: float,
        tick_size: float,
    ) -> None:
        self.direction = direction
        self.start_t = start_t
        self.expire_t = expire_t
        self.start_price = start_price
        self.tick_size = tick_size
 
        self.sum_delta: int = 0
        self.sum_volume: int = 0
 
        self.absorbed_sell_vol: int = 0
        self.total_sell_vol: int = 0
        self.absorbed_buy_vol: int = 0
        self.total_buy_vol: int = 0
 
        self.min_price: float = start_price
        self.max_price: float = start_price
        self.last_price: float = start_price
        self._prev_price: Optional[float] = None
 
    def on_tick(self, t: datetime, price: float, delta: int, size: int) -> None:
        self.sum_delta += delta
        self.sum_volume += size
        self.last_price = price
 
        if price < self.min_price:
            self.min_price = price
        if price > self.max_price:
            self.max_price = price
 
        if delta > 0:
            self.total_buy_vol += size
            if self._prev_price is not None and price <= self._prev_price:
                self.absorbed_buy_vol += size
        elif delta < 0:
            self.total_sell_vol += size
            if self._prev_price is not None and price >= self._prev_price:
                self.absorbed_sell_vol += size
 
        self._prev_price = price
 
    def is_expired(self, now: datetime) -> bool:
        return now >= self.expire_t
 
    def delta_ratio(self) -> float:
        if self.sum_volume <= 0:
            return 0.0
        return self.sum_delta / self.sum_volume
 
    def sell_absorption_ratio(self) -> float:
        if self.sum_volume <= 0:
            return 0.0
        return self.absorbed_sell_vol / self.sum_volume
 
    def buy_absorption_ratio(self) -> float:
        if self.sum_volume <= 0:
            return 0.0
        return self.absorbed_buy_vol / self.sum_volume
 
 
class PriorDayHlBounce:
    """
    Trades bounces off the prior session's high and low.
 
    Automatically tracks session high/low from the tick stream.
    At each session reset (5 PM CT), the completed session's H/L
    become the prior day levels for the new session.
 
    When price approaches prior day low from above → LONG (support)
    When price approaches prior day high from below → SHORT (resistance)
 
    Uses AbsorptionAttempt for confirmation. Static SL/TP exits.
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: PriorDayHlBounceParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")
 
        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts
 
        # Level config
        self.support = params.support
        self.resistance = params.resistance
        self.zone_ticks = params.zone_ticks
 
        # Risk/reward
        self.risk_ticks = params.risk_ticks
        self.reward_ticks = params.reward_ticks
        self.cooldown_seconds = params.cooldown_seconds
 
        # Confirmation
        self.use_confirmation = params.use_confirmation
        self.entry_attempt_seconds = params.entry_attempt_seconds
        self.entry_delta_ratio_threshold = params.entry_delta_ratio_threshold
        self.entry_min_response_ticks = params.entry_min_response_ticks
        self.entry_min_attempt_volume = params.entry_min_attempt_volume
        self.entry_min_absorption_ratio = params.entry_min_absorption_ratio
 
        # Trading hours
        self.trading_start_hour = params.trading_start_hour
        self.trading_end_hour = params.trading_end_hour
 
        # Exit confirmation
        self.exit_attempt_seconds = params.exit_attempt_seconds
        self.exit_delta_ratio_threshold = params.exit_delta_ratio_threshold
        self.exit_min_response_ticks = params.exit_min_response_ticks
        self.exit_min_attempt_volume = params.exit_min_attempt_volume
        self.exit_absorption_ticks = params.exit_absorption_ticks
 
        # Session
        self.session_reset_hour = params.session_reset_hour
        self.session_reset_minute = params.session_reset_minute
 
        # Session H/L tracking
        self._current_session_key: Optional[datetime] = None
        self._current_high: Optional[float] = None
        self._current_low: Optional[float] = None
        self._prior_high: Optional[float] = None
        self._prior_low: Optional[float] = None
 
        # Entry state
        self._attempt: Optional[AbsorptionAttempt] = None
        self._cooldown_until: Optional[datetime] = None
        self._last_high_zone_state: Optional[str] = None
        self._last_low_zone_state: Optional[str] = None
        self._entry_direction: Optional[str] = None
        self._active_level: Optional[str] = None  # "HIGH" or "LOW"
 
        # Exit state
        self._exit_attempt: Optional[BandAttempt] = None
 
        self.logger.info(
            f"PriorDayHlBounce initialized: "
            f"zone={self.zone_ticks}t "
            f"risk={self.risk_ticks}t reward={self.reward_ticks}t "
            f"support={self.support} resistance={self.resistance}"
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
 
    def _zone_state(self, price: float, level: float) -> str:
        zone_dist = self.zone_ticks * self.tick_size
        if price > level + zone_dist:
            return "ABOVE"
        elif price < level - zone_dist:
            return "BELOW"
        return "IN_ZONE"
 
    def _update_session(self, tick: Tick) -> None:
        """Track session high/low and rotate on session boundary."""
        session = self._session_key(tick.t)
 
        if session != self._current_session_key:
            # Session reset — rotate current to prior
            if self._current_high is not None and self._current_low is not None:
                self._prior_high = self._current_high
                self._prior_low = self._current_low
 
                self.logger.info(
                    f"[{self._ct(tick.t)}] New session — "
                    f"prior day H={self._prior_high:.{self.precision}f} "
                    f"L={self._prior_low:.{self.precision}f}"
                )
 
            self._current_session_key = session
            self._current_high = tick.price
            self._current_low = tick.price
 
            # Reset entry state on new session
            self._attempt = None
            self._cooldown_until = None
            self._last_high_zone_state = None
            self._last_low_zone_state = None
            self._entry_direction = None
            self._active_level = None
            return
 
        # Update current session H/L
        if tick.price > self._current_high:
            self._current_high = tick.price
        if tick.price < self._current_low:
            self._current_low = tick.price
 
    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        now = tick.t
        delta = tick.delta()
 
        # Always update session tracking
        self._update_session(tick)
 
        # Need prior day data
        if self._prior_high is None or self._prior_low is None:
            return None
 
        # In-position guard
        in_position = kwargs.get("in_position", False)
 
        high_zone = self._zone_state(tick.price, self._prior_high)
        low_zone = self._zone_state(tick.price, self._prior_low)
 
        if in_position:
            self._last_high_zone_state = high_zone
            self._last_low_zone_state = low_zone
            return None
 
        # Trading hours filter
        if self.trading_start_hour is not None or self.trading_end_hour is not None:
            local_hour = now.astimezone(self.tz).hour
            if self.trading_start_hour is not None and local_hour < self.trading_start_hour:
                self._last_high_zone_state = high_zone
                self._last_low_zone_state = low_zone
                return None
            if self.trading_end_hour is not None and local_hour >= self.trading_end_hour:
                self._last_high_zone_state = high_zone
                self._last_low_zone_state = low_zone
                return None
 
        # Cooldown
        if self._cooldown_until is not None and now < self._cooldown_until:
            self._last_high_zone_state = high_zone
            self._last_low_zone_state = low_zone
            return None
 
        # Active attempt, update and check
        if self._attempt is not None:
            # Let attempt run outside zone until expiry
            if self._attempt.is_expired(now):
                self.logger.debug(
                    f"[{self._ct(now)}] Prior day {self._active_level} "
                    f"attempt expired, restarting"
                )
                self._attempt = None
                self._entry_direction = None
                self._active_level = None
            else:
                self._attempt.on_tick(now, tick.price, delta, tick.size)
                if self._entry_confirmed(self._attempt):
                    signal = self._build_entry(self._attempt, tick)
                    self._attempt = None
                    self._entry_direction = None
                    self._active_level = None
                    self._last_high_zone_state = high_zone
                    self._last_low_zone_state = low_zone
                    return signal
                self._last_high_zone_state = high_zone
                self._last_low_zone_state = low_zone
                return None
 
        # Detect zone entry at prior day high
        if self.resistance and self._entry_direction is None:
            if high_zone == "IN_ZONE" and self._last_high_zone_state == "BELOW":
                self._entry_direction = "SHORT"
                self._active_level = "HIGH"
 
        # Detect zone entry at prior day low
        if self.support and self._entry_direction is None:
            if low_zone == "IN_ZONE" and self._last_low_zone_state == "ABOVE":
                self._entry_direction = "LONG"
                self._active_level = "LOW"
 
        # No zone entry detected
        if self._entry_direction is None:
            self._last_high_zone_state = high_zone
            self._last_low_zone_state = low_zone
            return None
 
        # No confirmation mode
        if not self.use_confirmation:
            level_price = self._prior_high if self._active_level == "HIGH" else self._prior_low
            signal = self._build_entry_direct(tick, level_price)
            self._entry_direction = None
            self._active_level = None
            self._last_high_zone_state = high_zone
            self._last_low_zone_state = low_zone
            return signal
 
        # Start new attempt
        level_price = self._prior_high if self._active_level == "HIGH" else self._prior_low
 
        self._attempt = AbsorptionAttempt(
            direction=self._entry_direction,
            start_t=now,
            expire_t=now + timedelta(seconds=self.entry_attempt_seconds),
            start_price=tick.price,
            tick_size=self.tick_size,
        )
        self._attempt.on_tick(now, tick.price, delta, tick.size)
 
        self.logger.debug(
            f"[{self._ct(now)}] Prior day {self._active_level} attempt started: "
            f"{self._entry_direction} @ {tick.price:.{self.precision}f} "
            f"level={level_price:.{self.precision}f}"
        )
 
        self._last_high_zone_state = high_zone
        self._last_low_zone_state = low_zone
        return None
 
    def _entry_confirmed(self, attempt: AbsorptionAttempt) -> bool:
        if attempt.sum_volume < self.entry_min_attempt_volume:
            return False
 
        if attempt.direction == "LONG":
            if attempt.sell_absorption_ratio() < self.entry_min_absorption_ratio:
                return False
        else:
            if attempt.buy_absorption_ratio() < self.entry_min_absorption_ratio:
                return False
 
        dr = attempt.delta_ratio()
        if attempt.direction == "LONG":
            if dr < self.entry_delta_ratio_threshold:
                return False
        else:
            if dr > -self.entry_delta_ratio_threshold:
                return False
 
        min_resp = self.entry_min_response_ticks * self.tick_size
        if attempt.direction == "LONG":
            if (attempt.last_price - attempt.min_price) < min_resp:
                return False
        else:
            if (attempt.max_price - attempt.last_price) < min_resp:
                return False
 
        return True
 
    def _build_entry(self, attempt: AbsorptionAttempt, tick: Tick) -> Signal:
        direction = attempt.direction
        entry = tick.price
        dr = attempt.delta_ratio()
        vol = attempt.sum_volume
 
        level_name = "PDH" if self._active_level == "HIGH" else "PDL"
        level_price = self._prior_high if self._active_level == "HIGH" else self._prior_low
 
        if direction == "LONG":
            ar = attempt.sell_absorption_ratio()
            abs_vol = attempt.absorbed_sell_vol
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry + self.reward_ticks * self.tick_size, self.precision)
        else:
            ar = attempt.buy_absorption_ratio()
            abs_vol = attempt.absorbed_buy_vol
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry - self.reward_ticks * self.tick_size, self.precision)
 
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} {level_name} BOUNCE at {entry} "
            f"level={level_price:.{self.precision}f} "
            f"dr={dr:.3f} ar={ar:.3f} abs_vol={abs_vol} vol={vol} "
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
 
    def _build_entry_direct(self, tick: Tick, level_price: float) -> Signal:
        direction = self._entry_direction
        entry = tick.price
 
        level_name = "PDH" if self._active_level == "HIGH" else "PDL"
 
        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry + self.reward_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry - self.reward_ticks * self.tick_size, self.precision)
 
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} {level_name} BOUNCE "
            f"(no confirmation) at {entry} "
            f"level={level_price:.{self.precision}f} "
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
 
    # Exit confirmation

    def check_exit(self, tick: Tick, position_direction: str) -> bool:
        now = tick.t
        delta = tick.delta()
        exit_direction = "SHORT" if position_direction == "LONG" else "LONG"
 
        if self._exit_attempt is not None:
            if self._exit_attempt.is_expired(now):
                self._exit_attempt = None
            else:
                self._exit_attempt.on_tick(now, tick.price, delta, tick.size)
                if self._exit_confirmed(self._exit_attempt):
                    dr = self._exit_attempt.delta_ratio()
                    ar = self._exit_attempt.absorption_ratio()
                    vol = self._exit_attempt.sum_volume
 
                    self.logger.info(
                        f"[{self._ct(now)}] EXIT CONFIRMED ({exit_direction} pressure) "
                        f"@ {tick.price:.{self.precision}f} "
                        f"dr={dr:.3f} ar={ar:.3f} vol={vol}"
                    )
 
                    self._exit_attempt = None
                    return True
                return False
 
        self._exit_attempt = BandAttempt(
            direction=exit_direction,
            start_t=now,
            expire_t=now + timedelta(seconds=self.exit_attempt_seconds),
            start_price=tick.price,
            min_price=tick.price,
            max_price=tick.price,
            last_price=tick.price,
            tick_size=self.tick_size,
            absorption_ticks=self.exit_absorption_ticks,
        )
        self._exit_attempt.on_tick(now, tick.price, delta, tick.size)
 
        return False
 
    def _exit_confirmed(self, attempt: BandAttempt) -> bool:
        if attempt.sum_volume < self.exit_min_attempt_volume:
            return False
 
        dr = attempt.delta_ratio()
        if attempt.direction == "LONG":
            if dr < self.exit_delta_ratio_threshold:
                return False
        else:
            if dr > -self.exit_delta_ratio_threshold:
                return False
 
        min_resp = self.exit_min_response_ticks * self.tick_size
        if attempt.direction == "LONG":
            if (attempt.last_price - attempt.min_price) < min_resp:
                return False
        else:
            if (attempt.max_price - attempt.last_price) < min_resp:
                return False
 
        return True
 
    # Lifecycle
 
    def on_entry(self) -> None:
        self._attempt = None
        self._entry_direction = None
        self._active_level = None
        self._exit_attempt = None
 
    def on_exit(self) -> None:
        self._exit_attempt = None
 
    def reset(self) -> None:
        self._attempt = None
        self._cooldown_until = None
        self._last_high_zone_state = None
        self._last_low_zone_state = None
        self._entry_direction = None
        self._active_level = None
 
    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return prior_day_hl_bounce_handler
 
    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return prior_day_hl_bounce_handler
 
    def __repr__(self) -> str:
        ph = f"{self._prior_high:.4f}" if self._prior_high else "None"
        pl = f"{self._prior_low:.4f}" if self._prior_low else "None"
        return (
            f"PriorDayHlBounce(PDH={ph}, PDL={pl}, "
            f"support={self.support}, resistance={self.resistance})"
        )
 
 
def prior_day_hl_bounce_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != PriorDayHlBounce:
        raise ValueError(
            f"Expected PriorDayHlBounce strategy in state, "
            f"got {type(state.strategy)}"
        )
 
    strategy = state.strategy
    position = state.position
 
    # No position: check for entry
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
            strategy.on_entry()
        return
 
    # Update session tracking while in position
    strategy.check(tick, in_position=True)
 
    direction = position.direction
 
    # Stop loss
    sl_hit = False
    if direction == "LONG" and tick.price <= position.stop_loss:
        sl_hit = True
    elif direction == "SHORT" and tick.price >= position.stop_loss:
        sl_hit = True
 
    if sl_hit:
        pnl = position.close(position.stop_loss)
        state.total_pnl += pnl
        strategy.on_exit()
 
        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"[{strategy._ct(tick.t)}] PDH/L bounce stop loss, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.RED,
            "info",
        )
        state.position = None
        return
 
    # Take profit
    tp_hit = False
    if direction == "LONG" and tick.price >= position.take_profit:
        tp_hit = True
    elif direction == "SHORT" and tick.price <= position.take_profit:
        tp_hit = True
 
    if tp_hit:
        pnl = position.close(position.take_profit)
        state.total_pnl += pnl
        strategy.on_exit()
 
        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"[{strategy._ct(tick.t)}] PDH/L bounce take profit, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
        return
 
    # Confirmed exit
    if strategy.check_exit(tick, direction):
        pnl = position.close(tick.price)
        state.total_pnl += pnl
        strategy.on_exit()
 
        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"[{strategy._ct(tick.t)}] PDH/L bounce confirmed exit, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
