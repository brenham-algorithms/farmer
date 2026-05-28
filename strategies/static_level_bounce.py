import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
 
from colorama import Fore
 
from api.models import StaticLevelBounceParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState
 
 
class StaticLevelBounce:
    """
    Single static level bounce strategy with delta confirmation.
 
    Monitors a configured price level and enters when price approaches
    and orderflow confirms a bounce.
 
    The "level zone" is defined by ticks_above and ticks_below the level.
    When price enters the zone:
      - From above (support=True): attempt LONG bounces.
      - From below (resistance=True): attempt SHORT bounces.
 
    Attempts restart automatically on expiry as long as price remains
    in the zone. This continues until either an attempt confirms or
    price exits the zone.
 
    Confirmation uses: delta ratio, minimum attempt volume, and
    absorption ratio (passive defense as a proportion of total volume).
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: StaticLevelBounceParams,
    ) -> None:
        self.logger = logger
 
        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts
 
        # Level definition
        self.level = params.level
        self.support = params.support
        self.resistance = params.resistance
 
        # Zone boundaries
        self.zone_upper = self.level + params.ticks_above * self.tick_size
        self.zone_lower = self.level - params.ticks_below * self.tick_size
 
        # Risk/reward
        self.reward_ticks = params.reward_ticks
        self.risk_ticks = params.risk_ticks
 
        # Confirmation
        self.attempt_seconds = params.attempt_seconds
        self.delta_ratio_threshold = params.delta_ratio_threshold
        self.min_response_ticks = params.min_response_ticks
        self.min_attempt_volume = params.min_attempt_volume
        self.min_absorption_ratio = params.min_absorption_ratio
        self.absorption_ticks = params.absorption_ticks
        self.cooldown_seconds = params.cooldown_seconds
 
        # State
        self._attempt: Optional[BandAttempt] = None
        self._cooldown_until: Optional[datetime] = None
        self._last_zone_state: Optional[str] = None
        self._entry_direction: Optional[str] = None
 
        self.logger.info(
            f"StaticLevelBounce initialized: level={self.level:.{self.precision}f} "
            f"zone=[{self.zone_lower:.{self.precision}f}, {self.zone_upper:.{self.precision}f}] "
            f"support={self.support} resistance={self.resistance}"
        )
 
    def _zone_state(self, price: float) -> str:
        if price > self.zone_upper:
            return "ABOVE"
        elif price < self.zone_lower:
            return "BELOW"
        return "IN_ZONE"
 
    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        current_state = self._zone_state(tick.price)
        now = tick.t
        delta = tick.delta()
 
        # --- Price left the zone: reset ---
        if current_state != "IN_ZONE":
            if self._entry_direction is not None:
                self._entry_direction = None
                self._attempt = None
            self._last_zone_state = current_state
            return None
 
        # --- We're in the zone ---
 
        # Cooldown check
        if self._cooldown_until is not None and now < self._cooldown_until:
            self._last_zone_state = current_state
            return None
 
        # Detect zone entry (first tick in zone, determine direction)
        if self._entry_direction is None:
            if self._last_zone_state == "ABOVE" and self.support:
                self._entry_direction = "LONG"
            elif self._last_zone_state == "BELOW" and self.resistance:
                self._entry_direction = "SHORT"
 
            if self._entry_direction is None:
                self._last_zone_state = current_state
                return None
 
        # Active attempt: update and check confirmation
        if self._attempt is not None:
            if self._attempt.is_expired(now):
                self.logger.debug("Level attempt expired, restarting")
                self._attempt = None
                # Fall through to start a new attempt below
            else:
                self._attempt.on_tick(now, tick.price, delta, tick.size)
                if self._confirmed(self._attempt):
                    signal = self._build_entry(self._attempt, tick)
                    self._attempt = None
                    self._entry_direction = None
                    return signal
                self._last_zone_state = current_state
                return None
 
        # No active attempt: start a new one
        self._attempt = BandAttempt(
            direction=self._entry_direction,
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
            f"Level attempt started: {self._entry_direction} @ {tick.price} "
            f"(zone [{self.zone_lower:.{self.precision}f}, {self.zone_upper:.{self.precision}f}])"
        )
 
        self._last_zone_state = current_state
        return None
 
    def _confirmed(self, attempt: BandAttempt) -> bool:
        # 1. Minimum volume
        if attempt.sum_volume < self.min_attempt_volume:
            return False
 
        # 2. Delta ratio in expected direction
        dr = attempt.delta_ratio()
        if attempt.direction == "LONG":
            if dr < self.delta_ratio_threshold:
                return False
        else:
            if dr > -self.delta_ratio_threshold:
                return False
 
        # 3. Price response
        min_resp = self.min_response_ticks * self.tick_size
        if attempt.direction == "LONG":
            if (attempt.last_price - attempt.min_price) < min_resp:
                return False
        else:
            if (attempt.max_price - attempt.last_price) < min_resp:
                return False
 
        # 4. Absorption ratio
        if self.min_absorption_ratio > 0:
            if attempt.absorption_ratio() < self.min_absorption_ratio:
                return False
 
        return True
 
    def _build_entry(self, attempt: BandAttempt, tick: Tick) -> Signal:
        direction = attempt.direction
        entry = tick.price
        dr = attempt.delta_ratio()
        ar = attempt.absorption_ratio()
        vol = attempt.sum_volume
 
        if direction == "LONG":
            take_profit = round(entry + self.reward_ticks * self.tick_size, self.precision)
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            take_profit = round(entry - self.reward_ticks * self.tick_size, self.precision)
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
 
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        log_with_color(
            self.logger,
            f"{direction} LEVEL BOUNCE CONFIRMED at {entry} "
            f"level={self.level:.{self.precision}f} "
            f"dr={dr:.3f} ar={ar:.3f} vol={vol} "
            f"tp={take_profit} sl={stop_loss}",
            Fore.GREEN if direction == "LONG" else Fore.RED,
            "info",
        )

        # self.logger.info(
        #     f"{direction} LEVEL BOUNCE CONFIRMED at {entry} "
        #     f"level={self.level:.{self.precision}f} "
        #     f"dr={dr:.3f} ar={ar:.3f} vol={vol} "
        #     f"tp={take_profit} sl={stop_loss}",
        # )
 
        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.num_contracts,
            profit_target=take_profit,
            stop_target=stop_loss,
        )
 
    def reset(self) -> None:
        self._attempt = None
        self._cooldown_until = None
        self._last_zone_state = None
        self._entry_direction = None
 
    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return static_level_bounce_handler
 
    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return static_level_bounce_handler
 
    def __repr__(self) -> str:
        return (
            f"StaticLevelBounce(level={self.level:.4f}, "
            f"zone=[{self.zone_lower:.4f}, {self.zone_upper:.4f}], "
            f"support={self.support}, resistance={self.resistance})"
        )
 
 
def static_level_bounce_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != StaticLevelBounce:
        raise ValueError(
            f"Expected StaticLevelBounce strategy in state, got {type(state.strategy)}"
        )
 
    strategy = state.strategy
    position = state.position
 
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
            )
        return
 
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
 
        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"Level bounce stop loss, Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}",
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
 
        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"Level bounce take profit, Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
