import math
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from core.types import Tick


class CandleVwap:
    """
    VWAP calculation that internally aggregates ticks into 1-minute
    candles and uses typical price (H+L+C)/3 per candle.

    This produces consistent results between:
      - Backtests (ticks -> internal 1-min candles -> VWAP)
      - Live farming (seed from historical 1-min candles -> continue
        with ticks -> internal 1-min candles -> VWAP)

    The standard tick-level LiveVwap weights every tick individually,
    which gives slightly different values from candle-based VWAP used
    by most charting platforms. This class matches the chart.

    Session-scoped: resets at the configurable session boundary.
    """

    def __init__(
        self,
        session_reset_hour: int = 17,
        session_reset_minute: int = 0,
        tz_name: str = "America/Chicago",
    ) -> None:
        self.session_reset_hour = session_reset_hour
        self.session_reset_minute = session_reset_minute
        self.tz = ZoneInfo(tz_name)

        # VWAP accumulators
        self._sum_v: float = 0.0
        self._sum_pv: float = 0.0
        self._sum_ppv: float = 0.0

        # Current candle state
        self._candle_start: Optional[datetime] = None
        self._candle_open: float = 0.0
        self._candle_high: float = 0.0
        self._candle_low: float = 0.0
        self._candle_close: float = 0.0
        self._candle_volume: int = 0

        # Session tracking
        self._current_session_key: Optional[datetime] = None

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

    def _minute_key(self, t_utc: datetime) -> datetime:
        """Floor timestamp to the minute."""
        return t_utc.replace(second=0, microsecond=0)

    def _close_candle(self) -> None:
        """Close the current candle and update VWAP accumulators."""
        if self._candle_volume <= 0:
            return

        typical_price = (
            self._candle_high + self._candle_low + self._candle_close
        ) / 3.0
        v = self._candle_volume

        self._sum_v += v
        self._sum_pv += typical_price * v
        self._sum_ppv += typical_price * typical_price * v

    def _start_candle(self, tick: Tick, minute_key: datetime) -> None:
        """Start a new candle."""
        self._candle_start = minute_key
        self._candle_open = tick.price
        self._candle_high = tick.price
        self._candle_low = tick.price
        self._candle_close = tick.price
        self._candle_volume = tick.size

    def on_tick(self, tick: Tick) -> None:
        # Session reset
        session = self._session_key(tick.t)
        if session != self._current_session_key:
            # Close any open candle from previous session before resetting
            if self._current_session_key is not None and self._candle_volume > 0:
                self._close_candle()

            self._sum_v = 0.0
            self._sum_pv = 0.0
            self._sum_ppv = 0.0
            self._candle_start = None
            self._candle_volume = 0
            self._current_session_key = session

        minute_key = self._minute_key(tick.t)

        # First tick of the session
        if self._candle_start is None:
            self._start_candle(tick, minute_key)
            return

        # New minute: close previous candle, start new one
        if minute_key != self._candle_start:
            self._close_candle()
            self._start_candle(tick, minute_key)
            return

        # Same minute: update current candle
        if tick.price > self._candle_high:
            self._candle_high = tick.price
        if tick.price < self._candle_low:
            self._candle_low = tick.price
        self._candle_close = tick.price
        self._candle_volume += tick.size

    def seed_from_candles(self, candles: List[Dict[str, Any]]) -> None:
        """
        Pre-populate VWAP from historical candles for the current session.
        Call after construction and before live ticks start flowing.

        Candles must have keys: t, h, l, c, v
        Only candles from the current session (determined by last candle)
        are included.
        """
        if not candles:
            return

        last_t = candles[-1]["t"]
        if isinstance(last_t, str):
            last_t = datetime.fromisoformat(last_t.replace("Z", "+00:00"))
        current_session = self._session_key(last_t)

        for candle in candles:
            t = candle["t"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            if self._session_key(t) != current_session:
                continue

            typical_price = (candle["h"] + candle["l"] + candle["c"]) / 3.0
            v = candle["v"]

            self._sum_v += v
            self._sum_pv += typical_price * v
            self._sum_ppv += typical_price * typical_price * v

        self._current_session_key = current_session

    def _current_candle_contribution(self) -> tuple[float, float, float]:
        """Returns (pv, ppv, v) for the current incomplete candle."""
        if self._candle_volume <= 0:
            return 0.0, 0.0, 0.0
        typical = (self._candle_high + self._candle_low + self._candle_close) / 3.0
        v = self._candle_volume
        return typical * v, typical * typical * v, v

    @property
    def vwap(self) -> float:
        pv, ppv, v = self._current_candle_contribution()
        total_v = self._sum_v + v
        if total_v <= 0:
            return 0.0
        return (self._sum_pv + pv) / total_v

    @property
    def std_dev(self) -> float:
        pv, ppv, v = self._current_candle_contribution()
        total_v = self._sum_v + v
        if total_v <= 0:
            return 0.0
        mean = (self._sum_pv + pv) / total_v
        variance = ((self._sum_ppv + ppv) / total_v) - (mean * mean)
        if variance < 0:
            return 0.0
        return math.sqrt(variance)
