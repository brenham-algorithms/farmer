import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore
 
from calculations.vwap import LiveVwap
from config import log_with_color
from core import Tick
from api.models import VwapMeanReversionLadderParams
from strategies.vwap_mean_reversion import BandAttempt
 
 
class VwapMeanReversionLadder:
    """
    VWAP mean reversion with laddered entries and tiered take-profit.
 
    Entry ladder (all on the same side of VWAP):
      - 1 contract  at entry_std_1 (default 2.0 sigma)
      - +1 contract at entry_std_2 (default 2.5 sigma) -> total 2
      - +2 contracts at entry_std_3 (default 3.0 sigma) -> total 4
 
    Take-profit ladder (as price reverts toward VWAP):
      - 4 contracts: cut 2 when price crosses back inside tp_std_4 band
      - 2 contracts: cut 1 when price crosses back inside tp_std_2 band
      - 1 contract:  close when price crosses VWAP
 
    Confirmation is optional at each step. Set attempt_seconds to 0
    to disable confirmation entirely (enter/add immediately on band breach).
 
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
        self.precision = params.precision
        self.risk_ticks = params.risk_ticks
        # self.risk_std = params.risk_std
        self.min_session_volume = params.min_session_volume
        self.cooldown_seconds = params.cooldown_seconds
 
        # Entry bands
        self.entry_std_1 = params.entry_std_1
        self.entry_std_2 = params.entry_std_2
        self.entry_std_3 = params.entry_std_3
        self.max_std_dev = params.max_std_dev
        self.min_std_dev = params.min_std_dev
 
        # TP bands
        self.tp_std_4 = params.tp_std_4
        self.tp_std_2 = params.tp_std_2
 
        # Confirmation (shared across all levels)
        self.attempt_seconds = params.attempt_seconds
        self.delta_ratio_threshold = params.delta_ratio_threshold
        self.min_response_ticks = params.min_response_ticks
        self.min_attempt_volume = params.min_attempt_volume
        self.min_absorbed_volume = params.min_absorbed_volume
        self.absorption_ticks = params.absorption_ticks
 
        # VWAP
        self.vwap = LiveVwap(
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
 
        # State
        self._entry_attempt: Optional[BandAttempt] = None
        self._add_attempt: Optional[BandAttempt] = None
        self._cooldown_until: Optional[datetime] = None
        self._paused_direction: Optional[str] = None
 
    def check(
        self, tick: Tick, timestamp: Any = None, **kwargs: Any
    ) -> Dict[str, Any] | None:
        """Check for initial entry at entry_std_1 band."""
        vwap_val = kwargs.get("vwap")
        std_dev = kwargs.get("std_dev")
        session_volume = kwargs.get("session_volume", 0)
 
        if vwap_val is None or std_dev is None:
            return None
        if session_volume < self.min_session_volume:
            return None
        if std_dev <= 0:
            return None
        if self.min_std_dev is not None and std_dev < self.min_std_dev:
            return None
        if self._cooldown_until is not None and tick.t < self._cooldown_until:
            return None
 
        distance_std = (tick.price - vwap_val) / std_dev
        abs_distance = abs(distance_std)
 
        if abs_distance > self.max_std_dev:
            return None
        if abs_distance < self.entry_std_1:
            self._entry_attempt = None
            return None
 
        direction = "SHORT" if distance_std > 0 else "LONG"
 
        if self._paused_direction == direction:
            return None
 
        # No confirmation
        if self.attempt_seconds <= 0:
            return self._build_entry(tick, direction, vwap_val, abs_distance, timestamp)
 
        # Confirmation flow
        delta = tick.delta()
 
        if self._entry_attempt is None:
            self._entry_attempt = self._make_attempt(tick, direction)
            self._entry_attempt.on_tick(tick.t, tick.price, delta, tick.size)
            self.logger.debug(
                f"Entry attempt started: {direction} @ {tick.price} "
                f"distance={abs_distance:.2f}std"
            )
            return None
 
        if self._entry_attempt.is_expired(tick.t):
            self._entry_attempt = None
            return None
 
        self._entry_attempt.on_tick(tick.t, tick.price, delta, tick.size)
 
        if self._confirmed(self._entry_attempt):
            dr = self._entry_attempt.delta_ratio()
            vol = self._entry_attempt.sum_volume
            absorbed = self._entry_attempt.absorbed_volume
            self._entry_attempt = None
            self.logger.info(
                f"Entry confirmed: {direction} dr={dr:.3f} "
                f"vol={vol} absorbed={absorbed}"
            )
            return self._build_entry(tick, direction, vwap_val, abs_distance, timestamp)
 
        return None
 
    def check_add(
        self, tick: Tick, num_contracts: int, direction: str, **kwargs: Any
    ) -> Dict[str, Any] | None:
        """
        Check if we should add contracts at the next ladder level.
        Returns {"contracts_to_add": N, "price": P} or None.
        """
        vwap_val = kwargs.get("vwap")
        std_dev = kwargs.get("std_dev")
 
        if vwap_val is None or std_dev is None or std_dev <= 0:
            return None
 
        if num_contracts == 1:
            target_std = self.entry_std_2
            contracts_to_add = 1
        elif num_contracts == 2:
            target_std = self.entry_std_3
            contracts_to_add = 2
        else:
            return None  # fully scaled
 
        # Check if price is beyond the target band in the right direction
        distance_std = (tick.price - vwap_val) / std_dev
 
        if direction == "LONG" and -distance_std < target_std:
            self._add_attempt = None
            return None
        elif direction == "SHORT" and distance_std < target_std:
            self._add_attempt = None
            return None
 
        # No confirmation
        if self.attempt_seconds <= 0:
            self.logger.info(
                f"Add {contracts_to_add} contract(s) at {tick.price} "
                f"({target_std}std level)"
            )
            return {"contracts_to_add": contracts_to_add, "price": tick.price}
 
        # Confirmation flow
        delta = tick.delta()
 
        if self._add_attempt is None:
            self._add_attempt = self._make_attempt(tick, direction)
            self._add_attempt.on_tick(tick.t, tick.price, delta, tick.size)
            self.logger.debug(
                f"Add attempt started: {direction} @ {tick.price} "
                f"({target_std}std level)"
            )
            return None
 
        if self._add_attempt.is_expired(tick.t):
            self._add_attempt = None
            return None
 
        self._add_attempt.on_tick(tick.t, tick.price, delta, tick.size)
 
        if self._confirmed(self._add_attempt):
            dr = self._add_attempt.delta_ratio()
            vol = self._add_attempt.sum_volume
            absorbed = self._add_attempt.absorbed_volume
            self._add_attempt = None
            self.logger.info(
                f"Add {contracts_to_add} confirmed at {tick.price} "
                f"({target_std}std) dr={dr:.3f} vol={vol} absorbed={absorbed}"
            )
            return {"contracts_to_add": contracts_to_add, "price": tick.price}
 
        return None
 
    def _build_entry(
        self,
        tick: Tick,
        direction: str,
        vwap_val: float,
        abs_distance: float,
        timestamp: Any,
    ) -> Dict[str, Any]:
        entry = tick.price
 
        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
 
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        self.logger.info(
            f"{direction} VWAP-LADDER entry at {entry} "
            f"vwap={vwap_val:.{self.precision}f} distance={abs_distance:.2f}std",
        )
 
        return {
            "timestamp": timestamp,
            "direction": direction,
            "entry": entry,
            "take_profit": None,
            "stop_loss": stop_loss,
        }
 
    def _make_attempt(self, tick: Tick, direction: str) -> BandAttempt:
        return BandAttempt(
            direction=direction,
            start_t=tick.t,
            expire_t=tick.t + timedelta(seconds=self.attempt_seconds),
            start_price=tick.price,
            min_price=tick.price,
            max_price=tick.price,
            last_price=tick.price,
            tick_size=self.tick_size,
            absorption_ticks=self.absorption_ticks,
        )
 
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
 
    def on_stop_loss(self, direction: str) -> None:
        self._paused_direction = direction
        self._entry_attempt = None
        self._add_attempt = None
 
    def on_vwap_touch(self) -> None:
        self._paused_direction = None

    
    def get_handler(self) -> Callable:
        return _vwap_mean_reversion_ladder_handler
 
    def reset(self) -> None:
        self._entry_attempt = None
        self._add_attempt = None
        self._cooldown_until = None
        self._paused_direction = None
 
    def __repr__(self) -> str:
        return (
            f"VwapMeanReversionLadder(vwap={self.vwap.vwap:.4f}, "
            f"std={self.vwap.std_dev:.4f}, "
            f"levels=[{self.entry_std_1}, {self.entry_std_2}, {self.entry_std_3}])"
        )


def _vwap_mean_reversion_ladder_handler(
    tick: Tick, logger: logging.Logger, state: Dict[str, Any]
) -> None:
    strategy = state["strategy"]
 
    # Handler owns the VWAP update
    strategy.vwap.on_tick(tick)
    vwap_now = strategy.vwap.vwap
    std_dev = strategy.vwap.std_dev
 
    tick_size = state["tick_size"]
    tick_value = state["tick_value"]
 
    # VWAP crossing detection — clear directional pause
    prev_price = state.get("prev_price")
    if prev_price is not None and vwap_now > 0:
        if (prev_price - vwap_now) * (tick.price - vwap_now) <= 0:
            strategy.on_vwap_touch()
    state["prev_price"] = tick.price
 
    position = state["position"]
 
    # --- No position: check for initial entry ---
    if position is None:
        signal = strategy.check(
            tick, tick.t,
            vwap=vwap_now,
            std_dev=std_dev,
            session_volume=strategy.vwap.session_volume,
        )
        if signal is not None:
            state["position"] = {
                "direction": signal["direction"],
                "timestamp": signal["timestamp"],
                "entries": [{"price": signal["entry"], "contracts": 1}],
                "num_contracts": 1,
                "stop_loss": signal["stop_loss"],
                "unwinding": False,
            }
        return
 
    direction = position["direction"]
    num = position["num_contracts"]
 
    # --- Hard stop: close ALL contracts ---
    hard_stopped = False
    if direction == "LONG" and tick.price <= position["stop_loss"]:
        hard_stopped = True
    elif direction == "SHORT" and tick.price >= position["stop_loss"]:
        hard_stopped = True
 
    if hard_stopped:
        pnl = _close_partial_lifo(
            position, num, position["stop_loss"],
            direction, tick_size, tick_value,
        )
        state["total_pnl"] += pnl
        strategy.on_stop_loss(direction)
 
        ts_start = (
            position["timestamp"]
            .replace(microsecond=0)
            .astimezone(ZoneInfo("America/Chicago"))
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
        log_with_color(
            logger,
            f"HARD STOP all {num} contract(s), Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
            Fore.RED,
            "info",
        )
        state["position"] = None
        return
 
    # --- Take-profit checks (only when std_dev is valid) ---
    if std_dev > 0:
        tp_hit = False
        contracts_to_cut = 0
        tp_label = ""
 
        if num == 4:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now - strategy.tp_std_4 * std_dev
            else:
                tp_hit = tick.price <= vwap_now + strategy.tp_std_4 * std_dev
            contracts_to_cut = 2
            tp_label = f"{strategy.tp_std_4}std"
 
        elif num == 2:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now - strategy.tp_std_2 * std_dev
            else:
                tp_hit = tick.price <= vwap_now + strategy.tp_std_2 * std_dev
            contracts_to_cut = 1
            tp_label = f"{strategy.tp_std_2}std"
 
        elif num == 1:
            if direction == "LONG":
                tp_hit = tick.price >= vwap_now
            else:
                tp_hit = tick.price <= vwap_now
            contracts_to_cut = 1
            tp_label = "VWAP"
 
        if tp_hit and contracts_to_cut > 0:
            pnl = _close_partial_lifo(
                position, contracts_to_cut, tick.price,
                direction, tick_size, tick_value,
            )
            state["total_pnl"] += pnl
            position["unwinding"] = True
 
            remaining = position["num_contracts"]
 
            ts_start = (
                position["timestamp"]
                .replace(microsecond=0)
                .astimezone(ZoneInfo("America/Chicago"))
            )
            ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))
 
            if remaining == 0:
                log_with_color(
                    logger,
                    f"CLOSED last contract at {tp_label}, Start = {ts_start}, End = {ts_end}, "
                    f"PnL = ${pnl:.2f}, VWAP = {vwap_now:.4f}",
                    Fore.GREEN if pnl > 0 else Fore.RED,
                    "info",
                )
                state["position"] = None
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
 
    # --- Add contracts (only if not unwinding and not fully scaled) ---
    if not position["unwinding"] and num < 4:
        add_signal = strategy.check_add(
            tick, num, direction,
            vwap=vwap_now,
            std_dev=std_dev,
        )
        if add_signal is not None:
            position["entries"].append({
                "price": add_signal["price"],
                "contracts": add_signal["contracts_to_add"],
            })
            position["num_contracts"] += add_signal["contracts_to_add"]
 
            logger.info(
                f"Scaled to {position['num_contracts']} contracts "
                f"(+{add_signal['contracts_to_add']} at {add_signal['price']})"
            )


def _close_partial_lifo(
    position: Dict[str, Any],
    num_to_close: int,
    exit_price: float,
    direction: str,
    tick_size: float,
    tick_value: float,
) -> float:
    '''
    Close num_to_close contracts in LIFO order (last added, first closed).
    Mutates position["entries"] and position["num_contracts"].
    Returns PnL for the closed contracts.
    '''
    pnl = 0.0
    remaining = num_to_close
 
    while remaining > 0 and position["entries"]:
        entry = position["entries"][-1]
        close_qty = min(remaining, entry["contracts"])
 
        if direction == "LONG":
            pnl += (exit_price - entry["price"]) / tick_size * tick_value * close_qty
        else:
            pnl += (entry["price"] - exit_price) / tick_size * tick_value * close_qty
 
        remaining -= close_qty
        entry["contracts"] -= close_qty
        if entry["contracts"] <= 0:
            position["entries"].pop()
 
    position["num_contracts"] -= num_to_close
    return round(pnl, 2)
