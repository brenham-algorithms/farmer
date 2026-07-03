import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import VwapCascadeReversalParams
from calculations.candle_vwap import CandleVwap
from calculations.delta_profile import DeltaProfile
from calculations.vwap import LiveVwap
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState


class VwapCascadeReversal:
    """
    Enters a position when stop cascade capitulation is detected
    across the VWAP, expecting a reversal after forced liquidation
    exhausts.

    Entry:
      1. Track delta above/below VWAP via DeltaProfile.
      2. Detect VWAP cross with significant trapped delta.
      3. Monitor continuation move for stop cascade (high volume +
         directional delta on the pain side).
      4. On cascade confirmed: enter in the REVERSAL direction.
         - Trapped longs cascade (selling) -> enter LONG (exhaustion)
         - Trapped shorts cascade (buying) -> enter SHORT (exhaustion)

    Exit (first to fire wins):
      - Static reward_ticks from entry.
      - Static risk_ticks from entry.
      - Confirmed exit: persistent opposite-direction attempts
        (same pattern as StaticLevelBounceConfirmedExit).
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: VwapCascadeReversalParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.num_contracts = params.num_contracts
        self.reward_ticks = params.reward_ticks
        self.risk_ticks = params.risk_ticks
        self.cooldown_seconds = params.cooldown_seconds

        # VWAP zone
        self.zone_ticks = params.zone_ticks
        self.scan_ticks = params.scan_ticks

        # Trapped detection
        self.min_trapped_delta = params.min_trapped_delta

        # Cascade detection
        self.cascade_volume_spike = params.cascade_volume_spike
        self.cascade_delta_ratio = params.cascade_delta_ratio

        # Exit confirmation
        self.exit_attempt_seconds = params.exit_attempt_seconds
        self.exit_delta_ratio_threshold = params.exit_delta_ratio_threshold
        self.exit_min_response_ticks = params.exit_min_response_ticks
        self.exit_min_attempt_volume = params.exit_min_attempt_volume
        self.exit_min_absorption_ratio = params.exit_min_absorption_ratio
        self.exit_absorption_ticks = params.exit_absorption_ticks

        # Trading hours
        self.trading_start_hour = params.trading_start_hour
        self.trading_end_hour = params.trading_end_hour

        # VWAP
        self.vwap = LiveVwap(
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
        if params.seed_from_candles:
            self.vwap.seed_from_candles(candles)

        # Delta profile
        self.delta_profile = DeltaProfile(
            tick_size=self.tick_size,
            bucket_ticks=params.bucket_ticks,
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )

        # Trapped/cascade state
        self._last_side: Optional[str] = None
        self._trapped_direction: Optional[str] = None
        self._trapped_delta: int = 0
        self._trapped_time: Optional[datetime] = None
        self._cascade_delta: int = 0
        self._cascade_volume: int = 0

        # Entry state
        self._cooldown_until: Optional[datetime] = None

        # Exit state
        self._exit_attempt: Optional[BandAttempt] = None

        self._current_session_key: Optional[datetime] = None

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _get_side(self, price: float, vwap_val: float) -> str:
        zone_dist = self.zone_ticks * self.tick_size
        if price > vwap_val + zone_dist:
            return "ABOVE"
        elif price < vwap_val - zone_dist:
            return "BELOW"
        return "IN_ZONE"

    # Entry logic

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        session_key = self.vwap._session_key(tick.t)
        if session_key != self._current_session_key:
            self._reset_trapped()
            self._last_side = None
            self._cooldown_until = None
            self._current_session_key = session_key

        vwap_val = kwargs.get("vwap")
        if vwap_val is None or vwap_val == 0:
            return None

        now = tick.t
        delta = tick.delta()
        scan_range = self.scan_ticks * self.tick_size
        current_side = self._get_side(tick.price, vwap_val)

        # Cooldown
        if self._cooldown_until is not None and now < self._cooldown_until:
            self._last_side = current_side
            return None

        # Detect when the VWAP is crossed which results in traders being trapped

        if self._last_side is not None and current_side != self._last_side:
            if self._last_side == "ABOVE" and current_side in ("BELOW", "IN_ZONE"):
                delta_above = self.delta_profile.zone_delta(
                    vwap_val, vwap_val + scan_range
                )
                if delta_above > self.min_trapped_delta:
                    self._trapped_direction = "LONGS"
                    self._trapped_delta = delta_above
                    self._trapped_time = now
                    self._cascade_delta = 0
                    self._cascade_volume = 0

                    self.logger.debug(
                        f"[{self._ct(now)}] Trapped longs detected: "
                        f"delta_above={delta_above:+d} @ {tick.price:.{self.precision}f}"
                    )

            elif self._last_side == "BELOW" and current_side in ("ABOVE", "IN_ZONE"):
                delta_below = self.delta_profile.zone_delta(
                    vwap_val - scan_range, vwap_val
                )
                if delta_below < -self.min_trapped_delta:
                    self._trapped_direction = "SHORTS"
                    self._trapped_delta = delta_below
                    self._trapped_time = now
                    self._cascade_delta = 0
                    self._cascade_volume = 0

                    self.logger.debug(
                        f"[{self._ct(now)}] Trapped shorts detected: "
                        f"delta_below={delta_below:+d} @ {tick.price:.{self.precision}f}"
                    )

        # Stop cascade detection

        if self._trapped_direction is not None:
            if self._trapped_direction == "LONGS" and current_side == "BELOW":
                self._cascade_delta += delta
                self._cascade_volume += tick.size

                if (
                    self._cascade_volume >= self.cascade_volume_spike
                    and -self._cascade_delta / self._cascade_volume
                    >= self.cascade_delta_ratio
                ):
                    signal = self._build_entry(tick, "LONG", vwap_val)
                    self._reset_trapped()
                    self._last_side = current_side
                    return signal

            elif self._trapped_direction == "SHORTS" and current_side == "ABOVE":
                self._cascade_delta += delta
                self._cascade_volume += tick.size

                if (
                    self._cascade_volume >= self.cascade_volume_spike
                    and self._cascade_delta / self._cascade_volume
                    >= self.cascade_delta_ratio
                ):
                    signal = self._build_entry(tick, "SHORT", vwap_val)
                    self._reset_trapped()
                    self._last_side = current_side
                    return signal

        # Reset trapped on strong move back through VWAP

        if self._trapped_direction == "LONGS" and current_side == "ABOVE":
            self.logger.debug(
                f"[{self._ct(now)}] Trapped longs cleared: price recovered above VWAP"
            )
            self._reset_trapped()

        elif self._trapped_direction == "SHORTS" and current_side == "BELOW":
            self.logger.debug(
                f"[{self._ct(now)}] Trapped shorts cleared: price dropped below VWAP"
            )
            self._reset_trapped()

        self._last_side = current_side
        return None

    def _build_entry(self, tick: Tick, direction: str, vwap_val: float) -> Signal:
        entry = tick.price

        if direction == "LONG":
            take_profit = round(
                entry + self.reward_ticks * self.tick_size, self.precision
            )
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            take_profit = round(
                entry - self.reward_ticks * self.tick_size, self.precision
            )
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)

        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)

        self.logger.info(
            f"[{self._ct(tick.t)}] {direction} CASCADE REVERSAL at {entry} "
            f"VWAP={vwap_val:.{self.precision}f} "
            f"cascade_delta={self._cascade_delta:+d} "
            f"cascade_vol={self._cascade_volume} "
            f"tp={take_profit} sl={stop_loss}",
        )

        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=self.num_contracts,
            profit_target=take_profit,
            stop_target=stop_loss,
        )

    # Exit confirmation logic

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

        if self.exit_min_absorption_ratio > 0:
            if attempt.absorption_ratio() < self.exit_min_absorption_ratio:
                return False

        return True

    # Lifecycle

    def on_entry(self) -> None:
        self._exit_attempt = None

    def on_exit(self) -> None:
        self._exit_attempt = None

    def _reset_trapped(self) -> None:
        self._trapped_direction = None
        self._trapped_delta = 0
        self._trapped_time = None
        self._cascade_delta = 0
        self._cascade_volume = 0

    def reset(self) -> None:
        self._last_side = None
        self._reset_trapped()
        self._exit_attempt = None
        self._cooldown_until = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return vwap_cascade_reversal_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return vwap_cascade_reversal_handler

    def __repr__(self) -> str:
        return (
            f"VwapCascadeReversal(vwap={self.vwap.vwap:.4f}, "
            f"trapped={self._trapped_direction})"
        )


def vwap_cascade_reversal_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != VwapCascadeReversal:
        raise ValueError(
            f"Expected VwapCascadeReversal strategy in state, "
            f"got {type(state.strategy)}"
        )

    strategy = state.strategy

    # Handler owns VWAP and delta profile updates
    strategy.vwap.on_tick(tick)
    strategy.delta_profile.on_tick(tick)

    position = state.position

    # If not in a position, check for cascade entry
    if position is None:
        signal = strategy.check(tick, vwap=strategy.vwap.vwap)
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
            f"[{strategy._ct(tick.t)}] Cascade reversal stop loss, "
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
            f"[{strategy._ct(tick.t)}] Cascade reversal take profit, "
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
            f"[{strategy._ct(tick.t)}] Cascade reversal confirmed exit, "
            f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
