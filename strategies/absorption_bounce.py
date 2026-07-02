import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import AbsorptionBounceParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState


class AbsorptionAttempt:
    """
    Tracks delta, volume, absorption, and price response within a
    single attempt window across the full zone width.

    Absorption is detected per tick: if aggressive volume didn't
    move price, it was absorbed by a passive defender.
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

        # Delta / volume
        self.sum_delta: int = 0
        self.sum_volume: int = 0

        # Absorption tracking
        self.absorbed_sell_vol: int = 0
        self.total_sell_vol: int = 0
        self.absorbed_buy_vol: int = 0
        self.total_buy_vol: int = 0

        # Price tracking
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


class AbsorptionBounce:
    """
    Enters when zone-wide absorption + delta confirmation align within
    a single attempt window.

    Exit (first to fire wins):
      - Static reward_ticks from entry
      - Static risk_ticks from entry (safety net)
      - Confirmed exit: persistent opposite-direction attempts

    Set reward_ticks very high to rely only on confirmed exit + hard stop.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: AbsorptionBounceParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")

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
        self.risk_ticks = params.risk_ticks
        self.reward_ticks = params.reward_ticks
        self.cooldown_seconds = params.cooldown_seconds

        # Entry confirmation
        self.entry_attempt_seconds = params.entry_attempt_seconds
        self.entry_delta_ratio_threshold = params.entry_delta_ratio_threshold
        self.entry_min_response_ticks = params.entry_min_response_ticks
        self.entry_min_attempt_volume = params.entry_min_attempt_volume
        self.entry_min_absorption_ratio = params.entry_min_absorption_ratio

        # Exit confirmation
        self.exit_attempt_seconds = params.exit_attempt_seconds
        self.exit_delta_ratio_threshold = params.exit_delta_ratio_threshold
        self.exit_min_response_ticks = params.exit_min_response_ticks
        self.exit_min_attempt_volume = params.exit_min_attempt_volume
        self.exit_absorption_ticks = params.exit_absorption_ticks

        # Time filters
        self.trading_start_hour = params.trading_start_hour
        self.trading_end_hour = params.trading_end_hour

        # Entry state
        self._attempt: Optional[AbsorptionAttempt] = None
        self._cooldown_until: Optional[datetime] = None
        self._last_zone_state: Optional[str] = None
        self._entry_direction: Optional[str] = None

        # Exit state
        self._exit_attempt: Optional[BandAttempt] = None

        self.logger.info(
            f"AbsorptionBounce initialized: level={self.level:.{self.precision}f} "
            f"zone=[{self.zone_lower:.{self.precision}f}, {self.zone_upper:.{self.precision}f}] "
            f"support={self.support} resistance={self.resistance}"
        )

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _zone_state(self, price: float) -> str:
        if price > self.zone_upper:
            return "ABOVE"
        elif price < self.zone_lower:
            return "BELOW"
        return "IN_ZONE"

    # ─── Entry logic ───

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        current_state = self._zone_state(tick.price)
        now = tick.t

        # Only trade during configured hours
        if self.trading_start_hour is not None or self.trading_end_hour is not None:
            local_hour = now.astimezone(self.tz).hour
            if (
                self.trading_start_hour is not None
                and local_hour < self.trading_start_hour
            ):
                self._last_zone_state = current_state
                return None
            if (
                self.trading_end_hour is not None
                and local_hour >= self.trading_end_hour
            ):
                self._last_zone_state = current_state
                return None

        delta = tick.delta()

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
                        signal = self._build_entry(self._attempt, tick)
                        self._attempt = None
                        return signal

            self._last_zone_state = current_state
            return None

        # Cooldown
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

        # Active attempt
        if self._attempt is not None:
            if self._attempt.is_expired(now):
                self.logger.debug(
                    f"[{self._ct(now)}] Absorption attempt expired, restarting"
                )
                self._attempt = None
            else:
                self._attempt.on_tick(now, tick.price, delta, tick.size)
                if self._entry_confirmed(self._attempt):
                    signal = self._build_entry(self._attempt, tick)
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
            f"[{self._ct(now)}] Absorption attempt started: "
            f"{self._entry_direction} @ {tick.price:.{self.precision}f}"
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

    def _build_entry(self, attempt: AbsorptionAttempt, tick: Tick) -> Signal:
        direction = attempt.direction
        entry = tick.price
        dr = attempt.delta_ratio()
        vol = attempt.sum_volume

        if direction == "LONG":
            ar = attempt.sell_absorption_ratio()
            abs_vol = attempt.absorbed_sell_vol
            take_profit = round(
                entry + self.reward_ticks * self.tick_size, self.precision
            )
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            ar = attempt.buy_absorption_ratio()
            abs_vol = attempt.absorbed_buy_vol
            take_profit = round(
                entry - self.reward_ticks * self.tick_size, self.precision
            )
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)

        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)

        log_with_color(
            self.logger,
            f"[{self._ct(tick.t)}] {direction} ABSORPTION BOUNCE at {entry} "
            f"level={self.level:.{self.precision}f} "
            f"dr={dr:.3f} ar={ar:.3f} abs_vol={abs_vol} vol={vol} "
            f"tp={take_profit} sl={stop_loss}",
            Fore.GREEN if direction == "LONG" else Fore.RED,
            "info",
        )

        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.num_contracts,
            profit_target=take_profit,
            stop_target=stop_loss,
        )

    # ─── Exit confirmation ───

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

    # ─── Lifecycle ───

    def on_entry(self) -> None:
        self._attempt = None
        self._entry_direction = None
        self._exit_attempt = None

    def on_exit(self) -> None:
        self._exit_attempt = None

    def reset(self) -> None:
        self._attempt = None
        self._exit_attempt = None
        self._cooldown_until = None
        self._last_zone_state = None
        self._entry_direction = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return absorption_bounce_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return absorption_bounce_handler

    def __repr__(self) -> str:
        return (
            f"AbsorptionBounce(level={self.level:.4f}, "
            f"zone=[{self.zone_lower:.4f}, {self.zone_upper:.4f}], "
            f"support={self.support}, resistance={self.resistance})"
        )


def absorption_bounce_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != AbsorptionBounce:
        raise ValueError(
            f"Expected AbsorptionBounce strategy in state, "
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
            )
            strategy.on_entry()
        return

    direction = position.direction

    # Static stop loss
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
            f"[{strategy._ct(tick.t)}] Absorption bounce stop loss, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.RED,
            "info",
        )
        state.position = None
        return

    # Static take profit
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
            f"[{strategy._ct(tick.t)}] Absorption bounce take profit, "
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
            f"[{strategy._ct(tick.t)}] Absorption bounce confirmed exit, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
