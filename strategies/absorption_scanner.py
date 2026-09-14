import logging
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo
 
import math
 
from colorama import Fore, Style
 
from api.models import AbsorptionScannerParams
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState
 
 
@dataclass
class WindowTick:
    """A tick stored in the rolling window with pre-computed absorption."""
 
    t: datetime
    bucket: float
    size: int
    sell_vol: int  # size if sell-aggressive, else 0
    buy_vol: int  # size if buy-aggressive, else 0
    absorbed_sell: int  # size if absorbed sell (price didn't drop), else 0
    absorbed_buy: int  # size if absorbed buy (price didn't rise), else 0
 
 
class BucketData:
    __slots__ = ["total_vol", "sell_vol", "buy_vol", "absorbed_sell", "absorbed_buy"]
 
    def __init__(self) -> None:
        self.total_vol = 0
        self.sell_vol = 0
        self.buy_vol = 0
        self.absorbed_sell = 0
        self.absorbed_buy = 0
 
    def add(self, wt: WindowTick) -> None:
        self.total_vol += wt.size
        self.sell_vol += wt.sell_vol
        self.buy_vol += wt.buy_vol
        self.absorbed_sell += wt.absorbed_sell
        self.absorbed_buy += wt.absorbed_buy
 
    def remove(self, wt: WindowTick) -> None:
        self.total_vol -= wt.size
        self.sell_vol -= wt.sell_vol
        self.buy_vol -= wt.buy_vol
        self.absorbed_sell -= wt.absorbed_sell
        self.absorbed_buy -= wt.absorbed_buy
 
    def sell_absorption_ratio(self) -> float:
        if self.total_vol <= 0:
            return 0.0
        return self.absorbed_sell / self.total_vol
 
    def buy_absorption_ratio(self) -> float:
        if self.total_vol <= 0:
            return 0.0
        return self.absorbed_buy / self.total_vol
 
    def is_empty(self) -> bool:
        return self.total_vol <= 0
 
 
class AbsorptionScanner:
    """
    Continuously scans for passive defenders using a rolling window
    of recent ticks. No configured level needed — the strategy
    discovers where absorption is happening in real-time.
 
    On every tick:
      1. Update rolling window (add new tick, expire old ones)
      2. Scan price buckets near current price for high absorption
      3. If a passive defender is detected → enter immediately
 
    Passive buyer (high sell absorption) → LONG
    Passive seller (high buy absorption) → SHORT
 
    Exits via static SL/TP and confirmed exit (first to fire wins).
    """
 
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: AbsorptionScannerParams,
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
        self.bucket_ticks = params.bucket_ticks
        self.bucket_size = params.bucket_ticks * self.tick_size
 
        # Detection thresholds
        self.min_absorption_ratio = params.min_absorption_ratio
        self.min_window_volume = params.min_window_volume
        self.max_window_volume = params.max_window_volume
        self.proximity_ticks = params.proximity_ticks
 
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
        self._buckets: Dict[float, BucketData] = defaultdict(BucketData)
        self._prev_price: Optional[float] = None
 
        # Entry state
        self._cooldown_until: Optional[datetime] = None
 
        # Exit state
        self._exit_attempt: Optional[BandAttempt] = None
 
        # Daily PnL tracking
        self._daily_pnl: float = 0.0
        self._current_session_key: Optional[datetime] = None
 
        self.logger.info(
            f"AbsorptionScanner initialized: "
            f"window={self.window_seconds}s "
            f"bucket={self.bucket_ticks}t "
            f"min_ar={self.min_absorption_ratio} "
            f"min_vol={self.min_window_volume} "
            f"proximity={self.proximity_ticks}t"
        )
 
    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")
 
    def _bucket_key(self, price: float) -> float:
        return math.floor(price / self.bucket_size) * self.bucket_size
 
    def _expire_old_ticks(self, now: datetime) -> None:
        cutoff = now - timedelta(seconds=self.window_seconds)
        while self._window and self._window[0].t < cutoff:
            old = self._window.popleft()
            bucket_data = self._buckets[old.bucket]
            bucket_data.remove(old)
            if bucket_data.is_empty():
                del self._buckets[old.bucket]
 
    def _add_tick(self, tick: Tick) -> None:
        delta = tick.delta()
        bucket = self._bucket_key(tick.price)
 
        sell_vol = tick.size if delta < 0 else 0
        buy_vol = tick.size if delta > 0 else 0
        absorbed_sell = 0
        absorbed_buy = 0
 
        if self._prev_price is not None:
            if delta < 0 and tick.price >= self._prev_price:
                absorbed_sell = tick.size
            elif delta > 0 and tick.price <= self._prev_price:
                absorbed_buy = tick.size
 
        wt = WindowTick(
            t=tick.t,
            bucket=bucket,
            size=tick.size,
            sell_vol=sell_vol,
            buy_vol=buy_vol,
            absorbed_sell=absorbed_sell,
            absorbed_buy=absorbed_buy,
        )
 
        self._window.append(wt)
        self._buckets[bucket].add(wt)
        self._prev_price = tick.price
 
    def _scan_for_defender(self, price: float) -> Optional[Dict[str, Any]]:
        """Scan buckets near current price for high absorption."""
        proximity = self.proximity_ticks * self.tick_size
        current_bucket = self._bucket_key(price)
 
        best_signal = None
        best_ratio = 0.0
 
        for bucket, data in self._buckets.items():
            if abs(bucket - current_bucket) > proximity:
                continue
 
            if data.total_vol < self.min_window_volume:
                continue
 
            if self.max_window_volume is not None and data.total_vol > self.max_window_volume:
                continue
 
            sell_ar = data.sell_absorption_ratio()
            buy_ar = data.buy_absorption_ratio()
 
            # Passive buyer detected (sells being absorbed)
            if sell_ar >= self.min_absorption_ratio and sell_ar > best_ratio:
                # Skip if both sides show high absorption (indecision)
                if buy_ar >= self.min_absorption_ratio:
                    continue
                best_signal = {
                    "direction": "LONG",
                    "bucket": bucket,
                    "ar": sell_ar,
                    "absorbed_vol": data.absorbed_sell,
                    "total_vol": data.total_vol,
                }
                best_ratio = sell_ar
 
            # Passive seller detected (buys being absorbed)
            if buy_ar >= self.min_absorption_ratio and buy_ar > best_ratio:
                if sell_ar >= self.min_absorption_ratio:
                    continue
                best_signal = {
                    "direction": "SHORT",
                    "bucket": bucket,
                    "ar": buy_ar,
                    "absorbed_vol": data.absorbed_buy,
                    "total_vol": data.total_vol,
                }
                best_ratio = buy_ar
 
        return best_signal
 
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
            if self.trading_start_hour is not None and local_hour < self.trading_start_hour:
                return None
            if self.trading_end_hour is not None and local_hour >= self.trading_end_hour:
                return None
 
        # Cooldown
        if self._cooldown_until is not None and now < self._cooldown_until:
            return None
 
        # Daily limits
        if self._daily_pnl <= self.daily_loss_limit:
            return None
        if self._daily_pnl >= self.daily_tp_limit:
            return None
 
        # Scan for defenders near current price
        defender = self._scan_for_defender(tick.price)
        if defender is None:
            return None

        if self.direction is not None and defender["direction"] != self.direction:
            return None
 
        return self._build_entry(tick, defender)
 
    def _build_entry(self, tick: Tick, defender: Dict[str, Any]) -> Signal:
        direction = defender["direction"]
        entry = tick.price
        ar = defender["ar"]
        abs_vol = defender["absorbed_vol"]
        total_vol = defender["total_vol"]
        bucket = defender["bucket"]
 
        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry + self.reward_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)
            take_profit = round(entry - self.reward_ticks * self.tick_size, self.precision)
 
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)
 
        defender_type = "passive buyer" if direction == "LONG" else "passive seller"
 
        self.logger.info(
            f"[{self._ct(tick.t)}] {Fore.GREEN if direction == 'LONG' else Fore.RED}{direction}{Style.RESET_ALL} ABSORPTION DETECTED "
            f"({defender_type} @ {bucket:.{self.precision}f}) "
            f"entry={entry} ar={ar:.3f} abs_vol={abs_vol} vol={total_vol} "
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
        self._buckets.clear()
        self._prev_price = None
        self._cooldown_until = None
        self._exit_attempt = None
        self._daily_pnl = 0.0
        self._current_session_key = None
 
    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return absorption_scanner_handler
 
    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return absorption_scanner_handler
 
    def __repr__(self) -> str:
        return (
            f"AbsorptionScanner(window={self.window_seconds}s, "
            f"buckets={len(self._buckets)}, "
            f"ticks_in_window={len(self._window)})"
        )
 
 
def absorption_scanner_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != AbsorptionScanner:
        raise ValueError(
            f"Expected AbsorptionScanner strategy in state, "
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
            f"[{strategy._ct(tick.t)}] Absorption scanner stop loss, "
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
            f"[{strategy._ct(tick.t)}] Absorption scanner take profit, "
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
            f"[{strategy._ct(tick.t)}] Absorption scanner confirmed exit, "
            f"Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f} (daily: ${strategy.daily_pnl:.2f})",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
