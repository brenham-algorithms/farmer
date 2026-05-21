import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import VwapMeanReversionLadderParams
from calculations.vwap import LiveVwap
from config import log_with_color
from core.types import AddSignal, Entry, Position, Signal, Tick
from tickers import TickerState


class VwapMeanReversionLadder:
    """
    VWAP mean reversion with laddered entries and tiered take-profit.

    Entry ladder (all on the same side of VWAP):
      - size_std_1 contracts  at entry_std_1 (default 2.0 sigma)
      - +size_std_2 contracts at entry_std_2 (default 2.5 sigma) -> total size_std_1 + size_std_2
      - +size_std_3 contracts at entry_std_3 (default 3.0 sigma) -> total size_std_1 + size_std_2 + size_std_3

    Take-profit ladder (as price reverts toward VWAP):
      - size_std_3 contracts: cut size_std_3 when price crosses back inside tp_std_3 band
      - size_std_2 contracts: cut size_std_2 when price crosses back inside tp_std_2 band
      - size_std_1 contracts:  close when price crosses VWAP

    Hard stop: risk_ticks from first entry, closes all contracts.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: VwapMeanReversionLadderParams,
    ) -> None:
        self.logger = logger

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.risk_ticks = params.risk_ticks
        self.min_session_volume = params.min_session_volume
        self.cooldown_seconds = params.cooldown_seconds

        # Entry bands and sizes
        self.entry_std_1 = params.entry_std_1
        self.size_std_1 = params.size_std_1
        self.entry_std_2 = params.entry_std_2
        self.size_std_2 = params.size_std_2
        self.entry_std_3 = params.entry_std_3
        self.size_std_3 = params.size_std_3
        self.max_std_dev = params.max_std_dev
        self.min_std_dev_value = params.min_std_dev_value

        # TP bands
        self.tp_std_3 = params.tp_std_3
        self.tp_std_2 = params.tp_std_2

        # Vwap
        self.vwap = LiveVwap(
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
        if params.seed_vwap:
            self.vwap.seed_from_candles(candles)

        # State
        self._cooldown_until: Optional[datetime] = None
        self._paused_direction: Optional[str] = None

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        """Check for initial entry at entry_std_1 band."""
        vwap_val = kwargs.get("vwap")
        std_dev = kwargs.get("std_dev")
        session_volume = kwargs.get("session_volume", 0)

        # Entry filters
        if vwap_val is None or std_dev is None:
            return None
        if session_volume < self.min_session_volume:
            return None
        if std_dev <= 0:
            return None
        if self.min_std_dev_value is not None and std_dev < self.min_std_dev_value:
            return None
        if self._cooldown_until is not None and tick.t < self._cooldown_until:
            return None

        distance_std = (tick.price - vwap_val) / std_dev
        abs_distance = abs(distance_std)

        if abs_distance > self.max_std_dev:
            return None
        if abs_distance < self.entry_std_1:
            return None

        direction = "SHORT" if distance_std > 0 else "LONG"

        if self._paused_direction == direction:
            return None

        return self._build_entry(tick, direction, vwap_val, abs_distance)

    def check_add(
        self, tick: Tick, num_contracts: int, direction: str, **kwargs: Any
    ) -> AddSignal | None:
        """
        Check if we should add contracts at the next ladder level.
        Returns AddSignal or None.
        """
        vwap_val = kwargs.get("vwap")
        std_dev = kwargs.get("std_dev")

        if vwap_val is None or std_dev is None or std_dev <= 0:
            return None

        if num_contracts == self.size_std_1:
            target_std = self.entry_std_2
            contracts_to_add = self.size_std_2
        elif num_contracts == self.size_std_1 + self.size_std_2:
            target_std = self.entry_std_3
            contracts_to_add = self.size_std_3
        else:
            return None  # fully scaled

        # Check if price is beyond the target band in the right direction
        distance_std = (tick.price - vwap_val) / std_dev

        if direction == "LONG" and -distance_std < target_std:
            return None
        elif direction == "SHORT" and distance_std < target_std:
            return None

        self.logger.info(
            f"Add {contracts_to_add} contract(s) at {tick.price} "
            f"({target_std}std level)"
        )
        return AddSignal(
            timestamp=tick.t,
            entry=tick.price,
            size=contracts_to_add,
        )

    def _build_entry(
        self,
        tick: Tick,
        direction: str,
        vwap_val: float,
        abs_distance: float,
    ) -> Signal:
        entry = tick.price

        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)

        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)

        self.logger.info(
            f"{direction} VWAP-LADDER entry at {entry} "
            f"vwap={vwap_val:.{self.precision}f} distance={abs_distance:.2f}std"
        )

        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.size_std_1,
            stop_target=stop_loss,
        )

    def on_stop_loss(self, direction: str) -> None:
        self._paused_direction = direction

    def on_vwap_touch(self) -> None:
        self._paused_direction = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return vwap_mean_reversion_ladder_backtest_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return vwap_mean_reversion_ladder_live_handler

    def reset(self) -> None:
        self._cooldown_until = None
        self._paused_direction = None

    def __repr__(self) -> str:
        return (
            f"VwapMeanReversionLadder(vwap={self.vwap.vwap:.2f}, "
            f"std={self.vwap.std_dev:.2f}, "
            f"levels=[{self.entry_std_1}, {self.entry_std_2}, {self.entry_std_3}])"
        )


def vwap_mean_reversion_ladder_live_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != VwapMeanReversionLadder:
        raise ValueError(
            f"Expected VwapMeanReversionLadder strategy in state, got {type(state.strategy)}"
        )

    state.tick_counter += 1

    strategy = state.strategy

    # Handler owns the vwap updates
    strategy.vwap.on_tick(tick)
    vwap_now = strategy.vwap.vwap
    std_dev = strategy.vwap.std_dev

    if state.tick_counter == 1 or state.tick_counter % 10000 == 0:
        logger.debug(
            f"Live handler tick: price={tick.price} vwap={vwap_now:.2f} std_dev={std_dev:.2f}"
        )

    # If price has crossed the vwap since the last trade, clear any directional pause
    prev_price = state.prev_price
    if prev_price is not None and vwap_now > 0:
        if (prev_price - vwap_now) * (tick.price - vwap_now) <= 0:
            strategy.on_vwap_touch()
    state.prev_price = tick.price

    position = state.position

    # If we are not already in a position, then check for initial entry at first ladder level
    if position is None:
        signal = strategy.check(
            tick,
            vwap=vwap_now,
            std_dev=std_dev,
            session_volume=strategy.vwap.session_volume,
        )
        if signal is not None:
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                stop_loss=signal.stop_target,
                unwinding=False,
            )
        return

    direction = position.direction
    num = position.num_contracts()

    # If we hit our hard stop, then close all contracts and reset state
    hard_stopped = False
    if direction == "LONG" and tick.price <= position.stop_loss:
        hard_stopped = True
    elif direction == "SHORT" and tick.price >= position.stop_loss:
        hard_stopped = True

    if hard_stopped:
        pnl = position.close(tick.price)
        state.total_pnl += pnl
        strategy.on_stop_loss(direction)

        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

        log_with_color(
            logger,
            f"HARD STOP all {num} contract(s), Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
            Fore.RED,
            "info",
        )
        state.position = None
        return

    # Check to take profit at the various ladder levels
    if std_dev > 0:
        tp_hit = False
        contracts_to_cut = 0
        tp_label = ""

        if num == strategy.size_std_3 + strategy.size_std_2 + strategy.size_std_1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now - strategy.tp_std_3 * std_dev
            else:
                tp_hit = tick.price <= vwap_now + strategy.tp_std_3 * std_dev
            contracts_to_cut = strategy.size_std_3
            tp_label = f"{strategy.tp_std_3}std"

        elif num == strategy.size_std_2 + strategy.size_std_1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now - strategy.tp_std_2 * std_dev
            else:
                tp_hit = tick.price <= vwap_now + strategy.tp_std_2 * std_dev
            contracts_to_cut = strategy.size_std_2
            tp_label = f"{strategy.tp_std_2}std"

        elif num == strategy.size_std_1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now
            else:
                tp_hit = tick.price <= vwap_now
            contracts_to_cut = strategy.size_std_1
            tp_label = "VWAP"

        if tp_hit and contracts_to_cut > 0:
            pnl = position.cut(contracts_to_cut, tick.price)
            state.total_pnl += pnl
            position.unwinding = True

            remaining = position.num_contracts()

            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )

            if remaining == 0:
                log_with_color(
                    logger,
                    f"CLOSED last contract at {tp_label}, Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
                state.position = None
            else:
                log_with_color(
                    logger,
                    f"CUT {contracts_to_cut} at {tp_label} ({remaining} remaining), "
                    f"Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
            return

    # Scale in at the next ladder level if conditions are met
    if not position.unwinding and num < strategy.size_std_1 + strategy.size_std_2 + strategy.size_std_3:
        add_signal = strategy.check_add(
            tick, num, direction, vwap=vwap_now, std_dev=std_dev
        )
        if add_signal is not None:
            position.entries.append(Entry(price=add_signal.entry, size=add_signal.size))

            logger.info(
                f"Scaled to {position.num_contracts()} contracts "
                f"(+{add_signal.size} at {add_signal.entry})"
            )


def vwap_mean_reversion_ladder_backtest_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != VwapMeanReversionLadder:
        raise ValueError(
            f"Expected VwapMeanReversionLadder strategy in state, got {type(state.strategy)}"
        )

    strategy = state.strategy

    # Handler owns the VWAP update
    strategy.vwap.on_tick(tick)
    vwap_now = strategy.vwap.vwap
    std_dev = strategy.vwap.std_dev

    # If price has crossed the vwap since the last trade, clear any directional pause
    prev_price = state.prev_price
    if prev_price is not None and vwap_now > 0:
        if (prev_price - vwap_now) * (tick.price - vwap_now) <= 0:
            strategy.on_vwap_touch()
    state.prev_price = tick.price

    position = state.position

    # If we are not already in a position, then check for initial entry at first ladder level
    if position is None:
        signal = strategy.check(
            tick,
            vwap=vwap_now,
            std_dev=std_dev,
            session_volume=strategy.vwap.session_volume,
        )
        if signal is not None:
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                stop_loss=signal.stop_target,
                unwinding=False,
            )
        return

    direction = position.direction
    num = position.num_contracts()

    # If we hit our hard stop, then close all contracts and reset state
    hard_stopped = False
    if direction == "LONG" and tick.price <= position.stop_loss:
        hard_stopped = True
    elif direction == "SHORT" and tick.price >= position.stop_loss:
        hard_stopped = True

    if hard_stopped:
        pnl = position.close(tick.price)
        state.total_pnl += pnl
        strategy.on_stop_loss(direction)

        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

        log_with_color(
            logger,
            f"HARD STOP all {num} contract(s), Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
            Fore.RED,
            "info",
        )
        state.position = None
        return

    # Check to take profit at the various ladder levels
    if std_dev > 0:
        tp_hit = False
        contracts_to_cut = 0
        tp_label = ""

        if num == strategy.size_std_3 + strategy.size_std_2 + strategy.size_std_1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now - strategy.tp_std_3 * std_dev
            else:
                tp_hit = tick.price <= vwap_now + strategy.tp_std_3 * std_dev
            contracts_to_cut = strategy.size_std_3
            tp_label = f"{strategy.tp_std_3}std"

        elif num == strategy.size_std_2 + strategy.size_std_1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now - strategy.tp_std_2 * std_dev
            else:
                tp_hit = tick.price <= vwap_now + strategy.tp_std_2 * std_dev
            contracts_to_cut = strategy.size_std_2
            tp_label = f"{strategy.tp_std_2}std"

        elif num == strategy.size_std_1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now
            else:
                tp_hit = tick.price <= vwap_now
            contracts_to_cut = strategy.size_std_1
            tp_label = "VWAP"

        if tp_hit and contracts_to_cut > 0:
            pnl = position.cut(contracts_to_cut, tick.price)
            state.total_pnl += pnl
            position.unwinding = True

            remaining = position.num_contracts()

            ts_start = position.timestamp.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(
                ZoneInfo("America/Chicago")
            )

            if remaining == 0:
                log_with_color(
                    logger,
                    f"CLOSED last contract at {tp_label}, Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
                state.position = None
            else:
                log_with_color(
                    logger,
                    f"CUT {contracts_to_cut} at {tp_label} ({remaining} remaining), "
                    f"Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
            return

    # Scale in at the next ladder level if conditions are met
    if not position.unwinding and num < strategy.size_std_1 + strategy.size_std_2 + strategy.size_std_3:
        add_signal = strategy.check_add(
            tick, num, direction, vwap=vwap_now, std_dev=std_dev
        )
        if add_signal is not None:
            position.entries.append(Entry(price=add_signal.entry, size=add_signal.size))

            logger.info(
                f"Scaled to {position.num_contracts()} contracts "
                f"(+{add_signal.size} at {add_signal.entry})"
            )
