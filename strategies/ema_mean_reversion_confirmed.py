import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import EmaMeanReversionConfirmedParams
from calculations.atr import LiveAtr
from calculations.ema import LiveEma
from config import log_with_color
from core.types import Entry, Position, Signal, Tick
from strategies.vwap_mean_reversion import BandAttempt
from tickers import TickerState


class EmaMeanReversionConfirmed:
    """
    EMA mean reversion with confirmed entry via recurring BandAttempts.

    When price reaches entry_distance_ticks from the EMA, persistent
    attempts start. Attempts restart on expiry as long as price remains
    beyond the threshold. Entry only fires when delta/absorption
    confirmation is met.

    Exit is dynamic: the handler closes when price crosses back to the
    live EMA (same as the original EMA mean reversion). A hard stop
    at risk_ticks provides the safety net.

    Optional ATR filter skips entries during high-volatility regimes.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: EmaMeanReversionConfirmedParams,
    ) -> None:
        self.logger = logger

        # Core
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.entry_distance_ticks = params.entry_distance_ticks
        self.max_distance_ticks = params.max_distance_ticks
        self.risk_ticks = params.risk_ticks
        self.cooldown_seconds = params.cooldown_seconds

        # Volatility filter
        self.max_atr = params.max_atr

        # Entry confirmation
        self.entry_attempt_seconds = params.entry_attempt_seconds
        self.entry_delta_ratio_threshold = params.entry_delta_ratio_threshold
        self.entry_min_response_ticks = params.entry_min_response_ticks
        self.entry_min_attempt_volume = params.entry_min_attempt_volume
        self.entry_min_absorption_ratio = params.entry_min_absorption_ratio
        self.entry_absorption_ticks = params.entry_absorption_ticks

        # Build live EMA seeded from historical candles
        self.ema = LiveEma(
            period=params.ema_period,
            candle_length_minutes=params.candle_length,
            seed_candles=candles,
        )

        # Build live ATR seeded from historical candles
        self.atr = LiveAtr(
            period=params.atr_period,
            candle_length_minutes=params.candle_length,
            seed_candles=candles,
        )

        # State
        self._attempt: Optional[BandAttempt] = None
        self._cooldown_until: Optional[datetime] = None

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        ema_val = kwargs.get("ema")
        atr_val = kwargs.get("atr")

        if ema_val is None:
            return None

        now = tick.t
        delta = tick.delta()

        # Cooldown
        if self._cooldown_until is not None and now < self._cooldown_until:
            return None

        # ATR filter
        if self.max_atr is not None and atr_val is not None and atr_val > self.max_atr:
            self._attempt = None
            return None

        # Distance from EMA in ticks
        distance_ticks = (tick.price - ema_val) / self.tick_size
        abs_distance = abs(distance_ticks)

        # Not far enough — clear any attempt and wait
        if abs_distance < self.entry_distance_ticks:
            self._attempt = None
            return None

        # Too far — falling knife guard
        if (
            self.max_distance_ticks is not None
            and abs_distance > self.max_distance_ticks
        ):
            self._attempt = None
            return None

        # Determine direction
        direction = "SHORT" if distance_ticks > 0 else "LONG"

        # Active attempt
        if self._attempt is not None:
            # Direction changed (price crossed EMA while attempt was running)
            if self._attempt.direction != direction:
                self._attempt = None
            elif self._attempt.is_expired(now):
                self.logger.debug("EMA MR attempt expired, restarting")
                self._attempt = None
                # Fall through to start new attempt
            else:
                self._attempt.on_tick(now, tick.price, delta, tick.size)
                if self._confirmed(self._attempt):
                    signal = self._build_entry(
                        self._attempt, tick, ema_val, abs_distance
                    )
                    self._attempt = None
                    return signal
                return None

        # Start new attempt
        self._attempt = BandAttempt(
            direction=direction,
            start_t=now,
            expire_t=now + timedelta(seconds=self.entry_attempt_seconds),
            start_price=tick.price,
            min_price=tick.price,
            max_price=tick.price,
            last_price=tick.price,
            tick_size=self.tick_size,
            absorption_ticks=self.entry_absorption_ticks,
        )
        self._attempt.on_tick(now, tick.price, delta, tick.size)

        self.logger.debug(
            f"EMA MR attempt started: {direction} @ {tick.price} "
            f"ema={ema_val:.{self.precision}f} distance={abs_distance:.1f} ticks"
        )

        return None

    def _confirmed(self, attempt: BandAttempt) -> bool:
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

    def _build_entry(
        self,
        attempt: BandAttempt,
        tick: Tick,
        ema_val: float,
        abs_distance: float,
    ) -> Signal:
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

        self.logger.info(
            f"{direction} EMA-MR CONFIRMED at {entry} "
            f"ema={ema_val:.{self.precision}f} distance={abs_distance:.1f} ticks "
            f"dr={dr:.3f} ar={ar:.3f} vol={vol}",
        )

        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=1,
            stop_target=stop_loss,
        )

    def reset(self) -> None:
        self._attempt = None
        self._cooldown_until = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return ema_mean_reversion_confirmed_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return ema_mean_reversion_confirmed_handler

    def __repr__(self) -> str:
        return (
            f"EmaMeanReversionConfirmed(ema={self.ema.value:.4f}, "
            f"atr={self.atr.value:.4f}, "
            f"entry_dist={self.entry_distance_ticks}, "
            f"risk={self.risk_ticks})"
        )


def ema_mean_reversion_confirmed_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    if type(state.strategy) != EmaMeanReversionConfirmed:
        raise ValueError(
            f"Expected EmaMeanReversionConfirmed strategy in state, "
            f"got {type(state.strategy)}"
        )

    strategy = state.strategy

    # Handler owns EMA and ATR updates
    strategy.ema.on_tick(tick)
    strategy.atr.on_tick(tick)

    position = state.position

    if position is None:
        signal = strategy.check(
            tick,
            ema=strategy.ema.value,
            atr=strategy.atr.value,
        )
        if signal is not None:
            state.position = Position(
                timestamp=signal.timestamp,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                stop_loss=signal.stop_target,
            )
        return

    direction = position.direction
    ema_now = strategy.ema.value

    # Hard stop
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
            f"EMA-MR stop loss, Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}, EMA = {ema_now:.4f}",
            Fore.RED,
            "info",
        )
        state.position = None
        return

    # Dynamic TP: price crosses back to EMA
    tp_hit = False
    if direction == "LONG" and tick.price >= ema_now:
        tp_hit = True
    elif direction == "SHORT" and tick.price <= ema_now:
        tp_hit = True

    if tp_hit:
        pnl = position.close(tick.price)
        state.total_pnl += pnl

        ts_start = position.timestamp.replace(microsecond=0).astimezone(
            ZoneInfo("America/Chicago")
        )
        ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

        log_with_color(
            logger,
            f"EMA-MR take profit (EMA touch), Start = {ts_start}, End = {ts_end}, "
            f"PnL = ${pnl:.2f}, EMA = {ema_now:.4f}",
            Fore.GREEN if pnl > 0 else Fore.RED,
            "info",
        )
        state.position = None
