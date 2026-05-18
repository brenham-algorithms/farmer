import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from colorama import Fore

from api.models import EmaMeanReversionParams
from calculations.atr import LiveAtr
from calculations.ema import LiveEma
from config import log_with_color
from core import Tick
from tickers import TickerState, Position, Entry

from .signals import Signal

class EmaMeanReversion:
    """
    Mean-reversion strategy based on the Exponential Moving Average (EMA)
    with an ATR-based volatility filter.

    Thesis: price tends to snap back to the EMA after extended moves away.
    When price drifts far enough from the EMA, this strategy enters in the
    direction back toward it.

    Volatility filter: when the rolling ATR exceeds `max_atr`, entries are
    skipped entirely. High ATR means the market is trending or whipsawing
    too hard for mean reversion to work reliably.

    Entry:
        - Price is at least `entry_distance_ticks` away from the EMA.
        - Price above EMA -> SHORT (expect reversion down).
        - Price below EMA -> LONG  (expect reversion up).
        - ATR must be below `max_atr` (if configured).

    Take profit:
        - Dynamic: handler checks the live EMA on every tick.

    Stop loss:
        - Fixed `risk_ticks` beyond entry, away from the EMA.

    Safety:
        - `max_distance_ticks` prevents entries when price is too far away.
        - `max_atr` prevents entries during high-volatility regimes.
        - `cooldown_seconds` prevents rapid re-entry after a trade.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: EmaMeanReversionParams,
    ) -> None:
        self.logger = logger

        # Core params
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value
        self.precision = params.precision
        self.entry_distance_ticks = params.entry_distance_ticks
        self.max_distance_ticks = params.max_distance_ticks
        self.reward_ticks = params.reward_ticks
        self.risk_ticks = params.risk_ticks
        self.target_ema = params.target_ema
        self.cooldown_seconds = params.cooldown_seconds

        # Volatility filter
        self.max_atr = params.max_atr

        # Build the live EMA, seeded from historical candles
        self.ema = LiveEma(
            period=params.ema_period,
            candle_length_minutes=params.candle_length,
            seed_candles=candles,
        )

        # Build the live ATR, seeded from historical candles
        self.atr = LiveAtr(
            period=params.atr_period,
            candle_length_minutes=params.candle_length,
            seed_candles=candles,
        )

        # Cooldown tracking
        self._cooldown_until: Optional[datetime] = None

    def check(
        self, tick: Tick, timestamp: Any = None, **kwargs: Any
    ) -> Signal | None:
        ema_val = kwargs.get("ema")
        atr_val = kwargs.get("atr")

        if ema_val is None:
            return None

        # Cooldown check
        if self._cooldown_until is not None and tick.t < self._cooldown_until:
            return None

        # Volatility filter: skip if ATR is too high
        if self.max_atr is not None and atr_val is not None and atr_val > self.max_atr:
            return None

        # How far is price from the EMA, measured in ticks?
        distance_ticks = (tick.price - ema_val) / self.tick_size
        abs_distance = abs(distance_ticks)

        # Must be far enough to qualify as an entry
        if abs_distance < self.entry_distance_ticks:
            return None

        # Must not be too far (avoid catching a falling knife)
        if (
            self.max_distance_ticks is not None
            and abs_distance > self.max_distance_ticks
        ):
            return None

        # Mean reversion: trade back toward the EMA
        direction = "SHORT" if distance_ticks > 0 else "LONG"
        entry = tick.price

        # SL only — TP is dynamic, handled by the handler
        if direction == "LONG":
            stop_loss = round(entry - self.risk_ticks * self.tick_size, self.precision)
        else:
            stop_loss = round(entry + self.risk_ticks * self.tick_size, self.precision)

        # Activate cooldown
        self._cooldown_until = tick.t + timedelta(seconds=self.cooldown_seconds)

        atr_display = (
            f" atr={atr_val:.{self.precision}f}" if atr_val is not None else ""
        )

        self.logger.info(
            f"{direction} mean-reversion signal: entry={entry} "
            f"ema={ema_val:.{self.precision}f} distance={abs_distance:.1f} ticks"
            f"{atr_display}",
        )

        return Signal(
            timestamp=timestamp,
            direction=direction,
            entry=entry,
            size=1,
            profit_target=None,
            stop_target=stop_loss,
        )

    def reset(self) -> None:
        self._cooldown_until = None

    def get_backtest_handler(self) -> Callable:
        return mean_reversion_ema_handler

    def __repr__(self) -> str:
        return (
            f"EmaMeanReversion(ema={self.ema.value:.4f}, atr={self.atr.value:.4f}, "
            f"entry_dist={self.entry_distance_ticks}, "
            f"risk={self.risk_ticks}, max_atr={self.max_atr})"
        )


def mean_reversion_ema_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    strategy = state.strategy

    # Handler owns the EMA and ATR updates
    strategy.ema.on_tick(tick)
    strategy.atr.on_tick(tick)

    position = state.position

    if position is None:
        signal = strategy.check(
            tick, tick.t, ema=strategy.ema.value, atr=strategy.atr.value
        )
        if signal is not None:
            state.position = Position(
                timestamp=tick.t,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                stop_loss=signal.stop_target,
            )
        return

    tick_size = position.tick_size
    tick_value = position.tick_value
    ema_now = strategy.ema.value
    market_price = position.entries[0].price

    if position.direction == "LONG":
        if tick.price >= ema_now:
            price_diff = tick.price - market_price
        elif tick.price <= position.stop_loss:
            price_diff = position.stop_loss - market_price
        else:
            return
    else:
        if tick.price <= ema_now:
            price_diff = market_price - tick.price
        elif tick.price >= position.stop_loss:
            price_diff = market_price - position.stop_loss
        else:
            return

    ticks_moved = price_diff / tick_size
    profit_loss = round(ticks_moved * tick_value, 2)
    state.total_pnl += profit_loss

    ts_start = (
        position.timestamp
        .replace(microsecond=0)
        .astimezone(ZoneInfo("America/Chicago"))
    )
    ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

    log_with_color(
        logger,
        f"Trade completed, Start = {ts_start}, End = {ts_end}, "
        f"PnL = ${profit_loss:.2f}, EMA at exit = {ema_now:.4f}",
        Fore.GREEN if profit_loss > 0 else Fore.RED,
        "info",
    )

    state.position = None