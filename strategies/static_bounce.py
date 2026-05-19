import logging
from typing import Any, Callable, Dict, List
from zoneinfo import ZoneInfo

from colorama import Fore
from tabulate import tabulate

from api.models import StrategyParams
from calculations import calculate_static_levels
from config import log_with_color
from core import Tick
from tickers import TickerState, Position, Entry

from .signals import Signal

class StaticBounce:
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: StrategyParams,
    ) -> None:
        if params.kind != "static_bounce":
            raise ValueError(f"Invalid strategy_params kind: {params.kind}")

        # Logs trade signals
        self.logger = logger

        # Contains data for historical analysis
        self.candles = candles

        # Tick size and value for the instrument being traded
        self.tick_size = params.tick_size
        self.tick_value = params.tick_value

        # Bounce parameters
        self.proximity_threshold = params.proximity_threshold
        self.reward_ticks = params.reward_ticks
        self.risk_ticks = params.risk_ticks

        # Static level calculation parameters applied to historical candles
        self.tick_tolerance = params.tick_tolerance
        self.min_separation = params.min_separation
        self.top_n = params.top_n
        self.decay_half_life_days = params.decay_half_life_days
        self.precision = params.precision

        # Static levels
        self.support: List[Dict[str, Any]] = []
        self.resistance: List[Dict[str, Any]] = []
        self.support, self.resistance = calculate_static_levels(
            self.candles,
            self.min_separation,
            self.top_n,
            self.tick_tolerance,
            self.tick_size,
            self.decay_half_life_days,
        )

        self.levels = {}
        for lvl in self.support + self.resistance:
            # Round level price to the specified precision for consistency
            lvl["price"] = round(lvl["price"], self.precision)

            # This particular strategy implementation treats all levels as both support and resistance
            lvl["support"] = True
            lvl["resistance"] = True
            self.levels[lvl["price"]] = lvl

        # Last level traded to avoid retests
        self.last_level_traded: float | None = None

    def check(
        self, tick: Tick, **kwargs: Any
    ) -> Signal | None:
        if not self.levels:
            return None

        # Check if the current price is in proximity to any of our levels and signal accordingly
        signals = []
        for level_key in self.levels:
            level = self.levels[level_key]
            distance = abs(tick.price - level["price"])

            # If the current price is close enough to the level, then we may need to emit a LONG or
            # SHORT signal.
            if distance <= (self.proximity_threshold * self.tick_size):
                # Emit a SHORT signal if the current price is slightly under a resistance level
                if tick.price < level["price"] and level["resistance"]:
                    signals.append((level_key, level, distance, "SHORT"))
                # Emit a LONG signal if the current price is slightly over a support level
                elif tick.price > level["price"] and level["support"]:
                    signals.append((level_key, level, distance, "LONG"))

        # If signals is empty, then there is nothing to act on for now
        if not signals:
            return None

        # Pick the closest qualifying level
        level_key, level, _, direction = min(signals, key=lambda x: x[2])

        # Abort if this is a retest of the level
        if self.last_level_traded == level_key:
            return None

        entry = tick.price

        take_profit = (
            entry + self.reward_ticks * self.tick_size
            if direction == "LONG"
            else entry - self.reward_ticks * self.tick_size
        )
        take_profit = round(take_profit, self.precision)

        stop_loss = (
            entry - self.risk_ticks * self.tick_size
            if direction == "LONG"
            else entry + self.risk_ticks * self.tick_size
        )
        stop_loss = round(stop_loss, self.precision)

        self.logger.info(
            f"{direction} signal from {level_key} at {entry}",
            extra={
                "timestamp": tick.t,
                "event": "signal_detected",
                "direction": direction,
                "entry": entry,
                "take_profit": take_profit,
                "stop_loss": stop_loss,
                "trigger_level": level_key,
            },
        )

        self.last_level_traded = level_key

        # return {
        #     "timestamp": tick.t,
        #     "direction": direction,
        #     "entry": entry,
        #     "take_profit": take_profit,
        #     "stop_loss": stop_loss,
        #     "level": level_key,
        # }

        return Signal(
            timestamp=tick.t,
            direction=direction,
            entry=entry,
            size=1,
            profit_target=take_profit,
            stop_target=stop_loss,
        )

    def reset(self) -> None:
        self.last_level_traded = None

    def get_backtest_handler(self) -> Callable:
        return static_bounce_handler

    def __repr__(self) -> str:
        headers = ["Level", "Hits", "Score"]

        support_table = tabulate(
            [
                (
                    f"{lvl['price']:.{self.precision}f}",
                    len(lvl["hits"]),
                    f"{lvl['score']:.2f}",
                )
                for lvl in self.support
            ],
            headers,
            tablefmt="pretty",
        )

        resistance_table = tabulate(
            [
                (
                    f"{lvl['price']:.{self.precision}f}",
                    len(lvl["hits"]),
                    f"{lvl['score']:.2f}",
                )
                for lvl in self.resistance
            ],
            headers,
            tablefmt="pretty",
        )

        return (
            "\nTop Support Levels:\n"
            + support_table
            + "\n\nTop Resistance Levels:\n"
            + resistance_table
        )


def static_bounce_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    """
    Handler for processing ticks in a StaticBounce backtest.
    Updates the state with PnL when a position is closed.
    """
    strategy = state.strategy

    # Handle position
    position = state.position

    if position is None:
        signal = state.strategy.check(tick)
        if signal is not None:
            state.position = Position(
                timestamp=tick.t,
                direction=signal.direction,
                entries=[Entry(price=signal.entry, size=signal.size)],
                tick_size=strategy.tick_size,
                tick_value=strategy.tick_value,
                take_profit=signal.profit_target,
                stop_loss=signal.stop_target,
            )
        return

    tick_size = state.position.tick_size
    tick_value = state.position.tick_value
    market_price = state.position.entries[0].price

    price_diff = 0
    if state.position.direction == "LONG":
        if tick.price >= state.position.take_profit:
            price_diff = state.position.take_profit - market_price
        elif tick.price <= state.position.stop_loss:
            price_diff = state.position.stop_loss - market_price
        else:
            return
    else:
        if tick.price <= state.position.take_profit:
            price_diff = market_price - state.position.take_profit
        elif tick.price >= state.position.stop_loss:
            price_diff = market_price - state.position.stop_loss
        else:
            return

    ticks_moved = price_diff / tick_size
    profit_loss = round(ticks_moved * tick_value, 2)
    state.total_pnl += profit_loss

    ts_start = (
        state.position.timestamp
        .replace(microsecond=0)
        .astimezone(ZoneInfo("America/Chicago"))
    )
    ts_end = tick.t.replace(microsecond=0).astimezone(ZoneInfo("America/Chicago"))

    log_with_color(
        logger,
        f"Trade completed, Start = {ts_start}, End = {ts_end}, PnL = ${profit_loss:.2f}",
        Fore.GREEN if profit_loss > 0 else Fore.RED,
        "info",
    )

    # Reset position
    state.position = None
