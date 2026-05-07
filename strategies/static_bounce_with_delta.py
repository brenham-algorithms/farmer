import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List

from tabulate import tabulate

from api.models import StrategyParams
from calculations import DeltaWindow, calculate_static_levels
from core import Tick

from .static_bounce import _static_bounce_handler

@dataclass
class ZoneAttempt:
    """
    A localized "touch attempt" around a specific level.
    Only includes orderflow that happens during the attempt.
    """

    level: float
    direction: str  # "LONG" or "SHORT"
    start_t: datetime
    expire_t: datetime
    start_price: float

    min_price: float
    max_price: float

    sum_delta: int = 0
    sum_volume: int = 0

    # Last observed price (used to measure response away from excursion)
    last_price: float = 0.0

    def on_tick(self, t: datetime, price: float, delta: int, size: int) -> None:
        self.last_price = price

        if price < self.min_price:
            self.min_price = price
        if price > self.max_price:
            self.max_price = price

        self.sum_delta += delta
        self.sum_volume += size

    def delta_ratio(self) -> float:
        if self.sum_volume <= 0:
            return 0.0
        return self.sum_delta / self.sum_volume

    def is_expired(self, now: datetime) -> bool:
        return now >= self.expire_t


class StaticBounceWithDelta:
    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: StrategyParams,
    ) -> None:
        if params.kind != "static_bounce_with_delta":
            raise ValueError(
                f"Invalid strategy params for StaticBounceWithDelta: {params.kind}"
            )

        self.logger = logger
        self.candles = candles

        # Standard strategy params
        self.tick_size = params.tick_size
        self.proximity_threshold = params.proximity_threshold
        self.reward_ticks = params.reward_ticks
        self.risk_ticks = params.risk_ticks

        # Level calculation params
        self.tick_tolerance = params.tick_tolerance
        self.min_separation = params.min_separation
        self.top_n = params.top_n
        self.decay_half_life_days = params.decay_half_life_days
        self.precision = params.precision

        # Confirmation params
        self.delta_window = DeltaWindow(window_seconds=params.delta_window_seconds)
        self.attempt_seconds = params.attempt_seconds
        self.delta_ratio_threshold = params.delta_ratio_threshold
        self.min_response_ticks = params.min_response_ticks
        self.max_penetration_ticks = params.max_penetration_ticks
        self.cooldown_seconds = params.cooldown_seconds

        # Static levels
        self.support, self.resistance = calculate_static_levels(
            self.candles,
            self.min_separation,
            self.top_n,
            self.tick_tolerance,
            self.tick_size,
            self.decay_half_life_days,
        )

        # Convert levels to a single dictionary for fast lookups by price
        self.levels = {}
        for lvl in self.support + self.resistance:
            lvl["price"] = round(lvl["price"], self.precision)

            # This particular strategy implementation treats all levels as both support and resistance
            lvl["support"] = True
            lvl["resistance"] = True
            self.levels[lvl["price"]] = lvl

        # Last level traded to avoid retests
        self.last_level_traded: float | None = None

        # NEW: attempt state and cooldown tracking
        self.attempt: ZoneAttempt | None = None
        self.cooldowns: Dict[float, datetime] = (
            {}
        )  # level -> last_trade_time (datetime)

    def _in_proximity(self, price: float, level: float) -> bool:
        # Note multiplying proximity_threshold by tick_size to convert from ticks to price units for proper
        # comparison.
        return abs(price - level) <= (self.proximity_threshold * self.tick_size)

    def _cooldown_active(self, level: float, now) -> bool:
        last = self.cooldowns.get(level)
        if last is None:
            return False

        return (now - last).total_seconds() < self.cooldown_seconds

    def _start_attempt(self, level: float, direction: str, tick: Tick) -> ZoneAttempt:
        now = tick.t
        expire_t = now + timedelta(seconds=self.attempt_seconds)
        return ZoneAttempt(
            level=level,
            direction=direction,
            start_t=now,
            expire_t=expire_t,
            start_price=tick.price,
            min_price=tick.price,
            max_price=tick.price,
            last_price=tick.price,
        )

    def _attempt_confirmed(self, attempt: ZoneAttempt) -> bool:
        """
        Confirmation logic:
        - delta impulse in the expected direction (ratio threshold)
        - AND a minimal price response away from the worst excursion
        - AND penetration through the level not too deep
        """
        dr = attempt.delta_ratio()

        # 1) Require meaningful orderflow in direction
        if attempt.direction == "LONG":
            if dr < self.delta_ratio_threshold:
                return False
        else:  # "SHORT"
            if dr > -self.delta_ratio_threshold:
                return False

        # 2) Require rejection/bounce evidence (price moved away from excursion)
        # LONG: bounce from min_price up by N ticks
        # SHORT: drop from max_price down by N ticks
        min_resp = self.min_response_ticks * self.tick_size
        if attempt.direction == "LONG":
            if (attempt.last_price - attempt.min_price) < min_resp:
                return False
        else:
            if (attempt.max_price - attempt.last_price) < min_resp:
                return False

        # 3) Avoid deep penetration through the level (optional but strong)
        max_pen = self.max_penetration_ticks * self.tick_size
        if attempt.direction == "LONG":
            # how far below level did we trade?
            if (attempt.level - attempt.min_price) > max_pen:
                return False
        else:
            # how far above level did we trade?
            if (attempt.max_price - attempt.level) > max_pen:
                return False

        return True

    def check(
        self, tick: Tick, timestamp: Any = None, **kwargs: Any
    ) -> Dict[str, Any] | None:
        now = tick.t
        delta = tick.delta()

        # Keep rolling window (optional; may help later for diagnostics/filters)
        # self.delta_window.on_tick(now, delta, tick.price, tick.size)
        self.delta_window.on_tick(tick)

        if not self.levels:
            return None

        # --- (A) If an attempt is active, update it first ---
        if self.attempt is not None:
            # If attempt expired, drop it
            if self.attempt.is_expired(now):
                self.attempt = None
            else:
                # If price moved away from the level zone, cancel attempt early
                if not self._in_proximity(tick.price, self.attempt.level):
                    self.attempt = None
                else:
                    # still in zone; accumulate local orderflow + track excursion
                    self.attempt.on_tick(now, tick.price, delta, tick.size)

                    # If attempt confirms, emit trade signal
                    if self._attempt_confirmed(self.attempt):
                        level_key = self.attempt.level
                        direction = self.attempt.direction
                        entry = tick.price

                        take_profit = (
                            entry + self.reward_ticks * self.tick_size
                            if direction == "LONG"
                            else entry - self.reward_ticks * self.tick_size
                        )
                        stop_loss = (
                            entry - self.risk_ticks * self.tick_size
                            if direction == "LONG"
                            else entry + self.risk_ticks * self.tick_size
                        )

                        take_profit = round(take_profit, self.precision)
                        stop_loss = round(stop_loss, self.precision)

                        self.logger.info(
                            f"{direction} CONFIRMED at {entry} from {level_key} "
                            f"dr={self.attempt.delta_ratio():.3f} "
                            f"min={self.attempt.min_price:.{self.precision}f} max={self.attempt.max_price:.{self.precision}f}",
                            extra={
                                "timestamp": timestamp,
                                "event": "signal_confirmed",
                                "direction": direction,
                                "entry": entry,
                                "take_profit": take_profit,
                                "stop_loss": stop_loss,
                                "trigger_level": level_key,
                                "delta_ratio": self.attempt.delta_ratio(),
                                "sum_delta": self.attempt.sum_delta,
                                "sum_volume": self.attempt.sum_volume,
                                "min_price": round(
                                    self.attempt.min_price, self.precision
                                ),
                                "max_price": round(
                                    self.attempt.max_price, self.precision
                                ),
                            },
                        )

                        # Bookkeeping
                        self.last_level_traded = level_key
                        self.cooldowns[level_key] = now
                        self.attempt = None  # reset attempt after trade

                        return {
                            "timestamp": timestamp,
                            "direction": direction,
                            "entry": entry,
                            "take_profit": take_profit,
                            "stop_loss": stop_loss,
                            "level": level_key,
                        }

                    # Not confirmed yet; wait for more ticks
                    return None

        # --- (B) No active attempt: find a candidate level and start attempt ---
        signals = []
        for level_key, level in self.levels.items():
            distance = abs(tick.price - level["price"])
            if distance <= (self.proximity_threshold * self.tick_size):
                if tick.price < level["price"] and level["resistance"]:
                    signals.append((level_key, level, distance, "SHORT"))
                elif tick.price > level["price"] and level["support"]:
                    signals.append((level_key, level, distance, "LONG"))

        if not signals:
            return None

        level_key, _, _, direction = min(signals, key=lambda x: x[2])

        # Avoid immediate retest logic (your old one)
        if self.last_level_traded == level_key:
            return None

        # NEW: cooldown
        if self._cooldown_active(level_key, now):
            return None

        # Start a localized attempt; do NOT trade yet
        self.attempt = self._start_attempt(level_key, direction, tick)
        # Include the first tick in attempt accounting
        self.attempt.on_tick(now, tick.price, delta, tick.size)

        self.logger.debug(
            f"Attempt started: {direction} @ level {level_key} price={tick.price} "
            f"expires_in={self.attempt_seconds}s"
        )

        return None

    def reset(self) -> None:
        self.last_level_traded = None
        self.attempt = None
        self.cooldowns.clear()

    def get_handler(self) -> Callable:
        return _static_bounce_handler

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
