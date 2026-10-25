import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore, Style

from api.models import DeltaScannerParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState


@dataclass
class WindowTick:
    """A tick stored in the rolling window."""

    t: datetime
    size: int
    sell_vol: int  # size if sell-aggressive, else 0
    buy_vol: int  # size if buy-aggressive, else 0


class DeltaScanner:
    """
    Continuously scans for directional aggression using a rolling window
    of recent ticks. Complements AbsorptionScanner: while absorption
    detects passive defenders (reversals), DeltaScanner detects aggressive
    attackers (continuations / breakouts).

    On every tick:
      1. Update rolling window (add new tick, expire old ones)
      2. Check aggregate delta ratio across the entire window
      3. If aggressive one-sided flow is detected -> enter in that direction

    High positive delta ratio (buyers dominating) -> LONG (continuation up)
    High negative delta ratio (sellers dominating) -> SHORT (continuation down)

    Exits via static SL/TP and confirmed exit (first to fire wins).
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: DeltaScannerParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts

        # Rolling window
        self.window_seconds = params.window_seconds

        # Detection thresholds
        self.delta_ratio_threshold = params.delta_ratio_threshold
        self.min_window_volume = params.min_window_volume
        self.max_window_volume = params.max_window_volume

        # Risk/reward
        self.risk_ticks = params.risk_ticks
        self.reward_ticks = params.reward_ticks
        self.cooldown_seconds = params.cooldown_seconds

        # Daily limits
        self.daily_loss_limit = params.daily_loss_limit
        self.daily_tp_limit = params.daily_tp_limit
        self.session_reset_hour = params.session_reset_hour
        self.session_reset_minute = params.session_reset_minute

        # Exit confirmation
        self.exit_attempt_seconds = params.exit_attempt_seconds
        self.exit_delta_ratio_threshold = params.exit_delta_ratio_threshold
        self.exit_min_response_ticks = params.exit_min_response_ticks
        self.exit_min_attempt_volume = params.exit_min_attempt_volume
        self.exit_absorption_ticks = params.exit_absorption_ticks

        # Direction filter
        self.direction = params.direction

        # Trading hours
        self.trading_start_hour = params.trading_start_hour
        self.trading_end_hour = params.trading_end_hour

        # Rolling window state
        self._window: deque[WindowTick] = deque()
        self._total_vol: int = 0
        self._buy_vol: int = 0
        self._sell_vol: int = 0

        # Entry state
        self._cooldown_until: Optional[datetime] = None

        # Exit state
        self._exit_attempt: Optional[BandAttempt] = None

        # Daily PnL tracking
        self._daily_pnl: float = 0.0
        self._current_session_key: Optional[datetime] = None

        self.logger.info(
            f"DeltaScanner initialized: "
            f"window={self.window_seconds}s "
            f"delta_threshold={self.delta_ratio_threshold} "
            f"min_vol={self.min_window_volume}"
        )

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _expire_old_ticks(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        while self._window and self._window[0].t < cutoff:
            old = self._window.popleft()
            self._total_vol -= old.size
            self._buy_vol -= old.buy_vol
            self._sell_vol -= old.sell_vol

    def _add_tick(self, tick: Tick) -> None:
        delta = tick.delta()
        sell_vol = tick.size if delta < 0 else 0
        buy_vol = tick.size if delta > 0 else 0

        wt = WindowTick(
            t=tick.t,
            size=tick.size,
            sell_vol=sell_vol,
            buy_vol=buy_vol,
        )

        self._window.append(wt)
        self._total_vol += wt.size
        self._buy_vol += wt.buy_vol
        self._sell_vol += wt.sell_vol

    def _check_delta(self) -> Optional[Dict[str, Any]]:
        """Check aggregate delta ratio across the entire rolling window."""
        if self._total_vol < self.min_window_volume:
            return None

        if (
            self.max_window_volume is not None
            and self._total_vol > self.max_window_volume
        ):
            return None

        dr = (self._buy_vol - self._sell_vol) / self._total_vol
        abs_dr = abs(dr)

        if abs_dr < self.delta_ratio_threshold:
            return None

        # Positive delta = buyers dominating = LONG (continuation up)
        # Negative delta = sellers dominating = SHORT (continuation down)
        direction = "LONG" if dr > 0 else "SHORT"

        return {
            "direction": direction,
            "dr": dr,
            "buy_vol": self._buy_vol,
            "sell_vol": self._sell_vol,
            "total_vol": self._total_vol,
        }

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

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        now = tick.t

        # Session reset
        session = self._session_key(now)
        if session != self._current_session_key:
            self._daily_pnl = 0.0
            self._current_session_key = session

        # Always update the rolling window
        self._expire_old_ticks(now)
        self._add_tick(tick)

        # In-position guard
        in_position = kwargs.get("in_position", False)
        if in_position:
            return None

        # Trading hours filter
        if self.trading_start_hour is not None or self.trading_end_hour is not None:
            local_hour = now.astimezone(self.tz).hour
            if (
                self.trading_start_hour is not None
                and local_hour < self.trading_start_hour
            ):
                return None
            if (
                self.trading_end_hour is not None
                and local_hour >= self.trading_end_hour
            ):
                return None

        # Cooldown
        if self._cooldown_until is not None and now < self._cooldown_until:
            return None

        # Daily limits
        if self._daily_pnl <= self.daily_loss_limit:
            return None
        if self._daily_pnl >= self.daily_tp_limit:
            return None

        # Check aggregate delta across the window
        delta_signal = self._check_delta()
        if delta_signal is None:
            return None

        if self.direction is not None and delta_signal["direction"] != self.direction:
            return None

        return self._build_entry(tick, delta_signal)

    def _build_entry(self, tick: Tick, delta_signal: Dict[str, Any]) -> Signal:
        direction = delta_signal["direction"]
        entry = tick.price
        dr = delta_signal["dr"]
        buy_vol = delta_signal["buy_vol"]
        sell_vol = delta_signal["sell_vol"]
        total_vol = delta_signal["total_vol"]

        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(
                entry + self.reward_ticks * self.tick_size, self.precision
            )
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(
                entry - self.reward_ticks * self.tick_size, self.precision
            )

        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)

        flow_type = "buyer aggression" if direction == "LONG" else "seller aggression"

        self.logger.info(
            f"[{self._ct(tick.t)}] {Fore.GREEN if direction == 'LONG' else Fore.RED}{direction}{Style.RESET_ALL} DELTA DETECTED "
            f"({flow_type}) "
            f"entry={entry} dr={dr:.3f} buy={buy_vol} sell={sell_vol} vol={total_vol} "
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

    # --- Exit confirmation ---

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
        self._exit_attempt = None

    def on_exit(self) -> None:
        self._exit_attempt = None

    def add_pnl(self, pnl: float) -> None:
        self._daily_pnl += pnl

    @property
    def daily_pnl(self) -> float:
        return self._daily_pnl

    def reset(self) -> None:
        self._window.clear()
        self._total_vol = 0
        self._buy_vol = 0
        self._sell_vol = 0
        self._cooldown_until = None
        self._exit_attempt = None
        self._daily_pnl = 0.0
        self._current_session_key = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return delta_scanner_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return delta_scanner_handler

    def __repr__(self) -> str:
        return (
            f"DeltaScanner(window={self.window_seconds}s, "
            f"ticks_in_window={len(self._window)}, "
            f"vol={self._total_vol})"
        )


def delta_scanner_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != DeltaScanner:
        raise ValueError(
            f"Expected DeltaScanner strategy in state, " f"got {type(state.strategy)}"
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

    # Keep window updated while in position
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
        strategy.add_pnl(pnl)
        strategy.on_exit()

        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

        log_with_color(
            logger,
            f"[{strategy._ct(tick.t)}] Delta scanner stop loss, "
            f"Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
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
        strategy.add_pnl(pnl)
        strategy.on_exit()

        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

        log_with_color(
            logger,
            f"[{strategy._ct(tick.t)}] Delta scanner take profit, "
            f"Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
        return

    # Confirmed exit
    if strategy.check_exit(tick, direction):
        pnl = position.close(tick.price)
        state.total_pnl += pnl
        strategy.add_pnl(pnl)
        strategy.on_exit()

        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

        log_with_color(
            logger,
            f"[{strategy._ct(tick.t)}] Delta scanner confirmed exit, "
            f"Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
