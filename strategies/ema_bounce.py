import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
 
from colorama import Fore
 
from api.models import EmaBounceParams
from calculations.ema import LiveEma
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
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
 
 
class EmaBounce:
    """
    Enters a position when price approaches the EMA and optionally
    absorption confirms a bounce. Static SL and TP.
 
    When price enters a configurable zone around the EMA:
      - From above → LONG (EMA acting as support in uptrend)
      - From below → SHORT (EMA acting as resistance in downtrend)
 
    After a trade completes, price must move re_entry_distance_ticks
    away from the EMA before becoming eligible to trade again.
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: EmaBounceParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")
 
        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts
 
        # EMA
        self.ema = LiveEma(
            period=params.ema_period,
            candle_length_minutes=params.candle_length,
            seed_candles=candles,
        )
 
        # Zone
        self.zone_ticks = params.zone_ticks
 
        # Risk/reward
        self.risk_ticks = params.risk_ticks
        self.reward_ticks = params.reward_ticks
 
        # Re-entry distance
        self.re_entry_distance_ticks = params.re_entry_distance_ticks
        self.re_entry_distance = params.re_entry_distance_ticks * self.tick_size
 
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
 
        # Entry state
        self._attempt: Optional[AbsorptionAttempt] = None
        self._last_zone_state: Optional[str] = None
        self._entry_direction: Optional[str] = None
        self._needs_distance: bool = False
 
        self.logger.info(
            f"EmaBounce initialized: ema_period={params.ema_period} "
            f"candle_length={params.candle_length}m "
            f"zone_ticks={self.zone_ticks} "
            f"risk={self.risk_ticks}t reward={self.reward_ticks}t "
            f"contracts={self.num_contracts}"
        )
 
    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")
 
    def _zone_state(self, price: float, ema_val: float) -> str:
        zone_dist = self.zone_ticks * self.tick_size
        if price > ema_val + zone_dist:
            return "ABOVE"
        elif price < ema_val - zone_dist:
            return "BELOW"
        return "IN_ZONE"
 
    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        ema_val = kwargs.get("ema")
        if ema_val is None or ema_val == 0:
            return None
 
        now = tick.t
        delta = tick.delta()
        current_state = self._zone_state(tick.price, ema_val)
 
        # In-position guard
        in_position = kwargs.get("in_position", False)
        if in_position:
            self._last_zone_state = current_state
            return None
 
        # Trading hours filter
        if self.trading_start_hour is not None or self.trading_end_hour is not None:
            local_hour = now.astimezone(self.tz).hour
            if self.trading_start_hour is not None and local_hour < self.trading_start_hour:
                self._last_zone_state = current_state
                return None
            if self.trading_end_hour is not None and local_hour >= self.trading_end_hour:
                self._last_zone_state = current_state
                return None
 
        # Re-entry distance check
        if self._needs_distance:
            distance = abs(tick.price - ema_val)
            if distance >= self.re_entry_distance:
                self._needs_distance = False
                self.logger.debug(
                    f"[{self._ct(now)}] Re-entry distance met: "
                    f"price={tick.price:.{self.precision}f} "
                    f"ema={ema_val:.{self.precision}f} "
                    f"distance={distance / self.tick_size:.0f}t"
                )
            else:
                self._last_zone_state = current_state
                return None
 
        # Price left the zone: let active attempt run until expiry
        if current_state != "IN_ZONE":
            if self._entry_direction is not None:
                self._entry_direction = None
 
            if self._attempt is not None:
                if self._attempt.is_expired(now):
                    self._attempt = None
                else:
                    self._attempt.on_tick(now, tick.price, delta, tick.size)
                    if self._entry_confirmed(self._attempt):
                        signal = self._build_entry(self._attempt, tick, ema_val)
                        self._attempt = None
                        return signal
 
            self._last_zone_state = current_state
            return None
 
        # Detect zone entry
        if self._entry_direction is None:
            if self._last_zone_state == "ABOVE":
                self._entry_direction = "LONG"
            elif self._last_zone_state == "BELOW":
                self._entry_direction = "SHORT"
 
            if self._entry_direction is None:
                self._last_zone_state = current_state
                return None
 
        # No confirmation mode
        if not self.use_confirmation:
            if self._entry_direction is not None:
                signal = self._build_entry_direct(tick, ema_val)
                self._entry_direction = None
                self._last_zone_state = current_state
                return signal
 
        # Active attempt
        if self._attempt is not None:
            if self._attempt.is_expired(now):
                self.logger.debug(
                    f"[{self._ct(now)}] EMA bounce attempt expired, restarting"
                )
                self._attempt = None
            else:
                self._attempt.on_tick(now, tick.price, delta, tick.size)
                if self._entry_confirmed(self._attempt):
                    signal = self._build_entry(self._attempt, tick, ema_val)
                    self._attempt = None
                    self._entry_direction = None
                    return signal
                self._last_zone_state = current_state
                return None
 
        # Start new attempt
        self._attempt = AbsorptionAttempt(
            direction=self._entry_direction,
            start_t=now,
            expire_t=now + timedelta(seconds=self.entry_attempt_seconds),
            start_price=tick.price,
            tick_size=self.tick_size,
        )
        self._attempt.on_tick(now, tick.price, delta, tick.size)
 
        self.logger.debug(
            f"[{self._ct(now)}] EMA bounce attempt started: "
            f"{self._entry_direction} @ {tick.price:.{self.precision}f} "
            f"ema={ema_val:.{self.precision}f}"
        )
 
        self._last_zone_state = current_state
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
 
    def _build_entry(
        self, attempt: AbsorptionAttempt, tick: Tick, ema_val: float
    ) -> Signal:
        direction = attempt.direction
        entry = tick.price
        dr = attempt.delta_ratio()
        vol = attempt.sum_volume
 
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
 
        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} EMA BOUNCE at {entry} "
            f"ema={ema_val:.{self.precision}f} "
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
 
    def _build_entry_direct(self, tick: Tick, ema_val: float) -> Signal:
        direction = self._entry_direction
        entry = tick.price
 
        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry + self.reward_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry - self.reward_ticks * self.tick_size, self.precision)
 
        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} EMA BOUNCE (no confirmation) at {entry} "
            f"ema={ema_val:.{self.precision}f} "
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
 
    def on_exit(self) -> None:
        self._needs_distance = True
        self._attempt = None
        self._entry_direction = None
 
    def reset(self) -> None:
        self._attempt = None
        self._last_zone_state = None
        self._entry_direction = None
        self._needs_distance = False
 
    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return ema_bounce_handler
 
    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return ema_bounce_handler
 
    def __repr__(self) -> str:
        return (
            f"EmaBounce(ema={self.ema.value:.4f}, "
            f"zone={self.zone_ticks}t, "
            f"risk={self.risk_ticks}t, reward={self.reward_ticks}t)"
        )
 
 
def ema_bounce_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != EmaBounce:
        raise ValueError(
            f"Expected EmaBounce strategy in state, got {type(state.strategy)}"
        )
 
    strategy = state.strategy
 
    # Handler owns EMA update
    strategy.ema.on_tick(tick)
 
    position = state.position
 
    # No position: check for entry
    if position is None:
        signal = strategy.check(tick, ema=strategy.ema.value)
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
 
    # Update zone state while in position
    strategy.check(tick, ema=strategy.ema.value, in_position=True)
 
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
            f"[{strategy._ct(tick.t)}] EMA bounce stop loss, "
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
            f"[{strategy._ct(tick.t)}] EMA bounce take profit, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
