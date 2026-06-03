import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
 
from colorama import Fore
 
from api.models import StaticLevelBounceConfirmedExitParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState
 
 
class StaticLevelBounceConfirmedExit:
    """
    Static level bounce with confirmation on both entry AND exit.
 
    Entry:
      Same as StaticLevelBounce — price enters the level zone, persistent
      attempts run until confirmed or price leaves the zone. Confirmation
      uses entry_delta_ratio_threshold, entry_min_attempt_volume, and
      entry_min_absorption_ratio.
 
    Exit:
      Once in a position, persistent attempts run in the OPPOSITE direction.
      When the opposite-direction attempt confirms (orderflow shows the
      trade reversing), the position is closed at market.
 
      A hard stop (risk_ticks) remains as a safety net in case exit
      confirmation never fires.
 
    Both entry and exit confirmation parameters are independently
    configurable.
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: StaticLevelBounceConfirmedExitParams,
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
 
        # Risk (hard stop safety net)
        self.risk_ticks = params.risk_ticks
 
        # Entry confirmation
        self.entry_attempt_seconds = params.entry_attempt_seconds
        self.entry_delta_ratio_threshold = params.entry_delta_ratio_threshold
        self.entry_min_response_ticks = params.entry_min_response_ticks
        self.entry_min_attempt_volume = params.entry_min_attempt_volume
        self.entry_min_absorption_ratio = params.entry_min_absorption_ratio
        self.entry_absorption_ticks = params.entry_absorption_ticks
 
        # Exit confirmation
        self.exit_attempt_seconds = params.exit_attempt_seconds
        self.exit_delta_ratio_threshold = params.exit_delta_ratio_threshold
        self.exit_min_response_ticks = params.exit_min_response_ticks
        self.exit_min_attempt_volume = params.exit_min_attempt_volume
        self.exit_min_absorption_ratio = params.exit_min_absorption_ratio
        self.exit_absorption_ticks = params.exit_absorption_ticks
 
        self.cooldown_seconds = params.cooldown_seconds
 
        # Entry state
        self._entry_attempt: Optional[BandAttempt] = None
        self._cooldown_until: Optional[datetime] = None
        self._last_zone_state: Optional[str] = None
        self._entry_direction: Optional[str] = None
 
        # Exit state
        self._exit_attempt: Optional[BandAttempt] = None
 
        self.logger.info(
            f"StaticLevelBounceConfirmedExit initialized: "
            f"level={self.level:.{self.precision}f} "
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

        if current_state != "IN_ZONE":
            if self._entry_direction is not None:
                # Clear direction if we are not in the zone anymore.
                # But don't clear _entry_attempt; let it run until expiry or confirmation.
                self._entry_direction = None

            self._last_zone_state = current_state

            # If an attempt is still active, keep updating it
            if self._entry_attempt is not None:
                if self._entry_attempt.is_expired(tick.t):
                    self._entry_attempt = None
                else:
                    self._entry_attempt.on_tick(tick.t, tick.price, tick.delta(), tick.size)
                    if self._entry_confirmed(self._entry_attempt):
                        signal = self._build_entry(self._entry_attempt, tick)
                        self._entry_attempt = None
                        return signal
            return None
 
        # Cooldown check
        if self._cooldown_until is not None and now < self._cooldown_until:
            self._last_zone_state = current_state
            return None
 
        # Detect zone entry
        if self._entry_direction is None:
            if self._last_zone_state == "ABOVE" and self.support:
                self._entry_direction = "LONG"
            elif self._last_zone_state == "BELOW" and self.resistance:
                self._entry_direction = "SHORT"
 
            if self._entry_direction is None:
                self._last_zone_state = current_state
                return None
 
        # Active entry attempt
        if self._entry_attempt is not None:
            if self._entry_attempt.is_expired(now):
                self.logger.debug("Entry attempt expired, restarting")
                self._entry_attempt = None
            else:
                self._entry_attempt.on_tick(now, tick.price, delta, tick.size)
                if self._entry_confirmed(self._entry_attempt):
                    signal = self._build_entry(self._entry_attempt, tick)
                    self._entry_attempt = None
                    self._entry_direction = None
                    return signal
                self._last_zone_state = current_state
                return None
 
        # Start new entry attempt
        self._entry_attempt = BandAttempt(
            direction=self._entry_direction,
            start_t=now,
            expire_t=now + timedelta(seconds=self.entry_attempt_seconds),
            start_price=tick.price,
            min_price=tick.price,
            max_price=tick.price,
            last_price=tick.price,
            tick_size=self.tick_size,
            absorption_ticks=self.entry_absorption_ticks,
        )
        self._entry_attempt.on_tick(now, tick.price, delta, tick.size)
 
        self.logger.debug(
            f"Entry attempt started: {self._entry_direction} @ {tick.price}"
        )
 
        self._last_zone_state = current_state
        return None
 
    def _entry_confirmed(self, attempt: BandAttempt) -> bool:
        if attempt.sum_volume < self.entry_min_attempt_volume:
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
 
        if self.entry_min_absorption_ratio > 0:
            if attempt.absorption_ratio() < self.entry_min_absorption_ratio:
                return False
 
        return True
 
    def _build_entry(self, attempt: BandAttempt, tick: Tick) -> Signal:
        direction = attempt.direction
        entry = tick.price
        dr = attempt.delta_ratio()
        ar = attempt.absorption_ratio()
        vol = attempt.sum_volume
 
        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
 
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        log_with_color(
            self.logger,
            f"{direction} LEVEL BOUNCE CONFIRMED at {entry} "
            f"level={self.level:.{self.precision}f} "
            f"dr={dr:.3f} ar={ar:.3f} vol={vol} "
            f"sl={stop_loss} (exit by confirmation)",
            Fore.GREEN if direction == "LONG" else Fore.RED,
            "info",
        )
 
        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.num_contracts,
            stop_target=stop_loss,
        )
 
    def check_exit(self, tick: Tick, position_direction: str) -> bool:
        """
        Called by handler on every tick while in a position.
        Runs persistent attempts in the opposite direction.
        Returns True when exit is confirmed.
        """
        now = tick.t
        delta = tick.delta()
 
        exit_direction = "SHORT" if position_direction == "LONG" else "LONG"
 
        # Active exit attempt
        if self._exit_attempt is not None:
            if self._exit_attempt.is_expired(now):
                self.logger.debug("Exit attempt expired, restarting")
                self._exit_attempt = None
            else:
                self._exit_attempt.on_tick(now, tick.price, delta, tick.size)
                if self._exit_confirmed(self._exit_attempt):
                    dr = self._exit_attempt.delta_ratio()
                    ar = self._exit_attempt.absorption_ratio()
                    vol = self._exit_attempt.sum_volume
                    self.logger.info(
                        f"EXIT CONFIRMED ({exit_direction} pressure) @ {tick.price} "
                        f"dr={dr:.3f} ar={ar:.3f} vol={vol}"
                    )
                    self._exit_attempt = None
                    return True
                return False
 
        # Start new exit attempt
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
 
        if self.exit_min_absorption_ratio > 0:
            if attempt.absorption_ratio() < self.exit_min_absorption_ratio:
                return False
 
        return True
 
    def on_entry(self) -> None:
        """Called by handler when position is opened."""
        self._entry_attempt = None
        self._entry_direction = None
        self._exit_attempt = None
 
    def on_exit(self) -> None:
        """Called by handler when position is closed."""
        self._exit_attempt = None
 
    def reset(self) -> None:
        self._entry_attempt = None
        self._exit_attempt = None
        self._cooldown_until = None
        self._last_zone_state = None
        self._entry_direction = None
 
    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return static_level_bounce_confirmed_exit_handler
 
    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return static_level_bounce_confirmed_exit_handler
 
    def __repr__(self) -> str:
        return (
            f"StaticLevelBounceConfirmedExit(level={self.level:.4f}, "
            f"zone=[{self.zone_lower:.4f}, {self.zone_upper:.4f}], "
            f"support={self.support}, resistance={self.resistance})"
        )
 
 
def static_level_bounce_confirmed_exit_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != StaticLevelBounceConfirmedExit:
        raise ValueError(
            f"Expected StaticLevelBounceConfirmedExit strategy in state, "
            f"got {type(state.strategy)}"
        )
 
    strategy = state.strategy
    position = state.position
 
    # If we are not in a position, check if we should be
    if position is None:
        signal = strategy.check(tick)
        if signal is not None:
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                stop_loss=signal.stop_target,
            )
            strategy.on_entry()
        return
 
    direction = position.direction
 
    # Hard stop safety net
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
            f"Level bounce hard stop, Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}",
            Fore.RED,
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
            f"Level bounce confirmed exit, Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
