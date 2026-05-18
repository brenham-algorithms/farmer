import math
from datetime import datetime, timedelta
from typing import Optional, Tuple, List, Dict, Any
from zoneinfo import ZoneInfo

from core import Tick


class LiveVwap:
    """
    Session-based VWAP (Volume Weighted Average Price) with standard deviation bands.

    Unlike EMA/ATR, VWAP is a session-scoped calculation — it resets at a
    configurable time each day. This makes it fundamentally different from
    other indicators: there is no "seeding from history"; instead, VWAP
    accumulates from the first tick of each session.

    Computes:
      VWAP     = sum(P * V) / sum(V)
      Variance = sum(P^2 * V) / sum(V) - VWAP^2
      StdDev   = sqrt(Variance)

    Standard deviation bands are derived on demand at any multiple.
    """

    def __init__(
        self,
        session_reset_hour: int = 17,
        session_reset_minute: int = 0,
        tz_name: str = "America/Chicago",
    ) -> None:
        """
        session_reset_hour/minute: Local time (in tz_name) when the session resets.
            Defaults to 17:00 CT (CME Globex open). Use 8:30 CT for cash-session VWAP.
        """
        self.reset_hour = session_reset_hour
        self.reset_minute = session_reset_minute
        self.tz = ZoneInfo(tz_name)

        self._sum_v: int = 0  # cumulative volume for current session
        self._sum_pv: float = 0.0  # cumulative price*volume for current session
        self._sum_ppv: float = 0.0  # cumulative price^2*volume for current session

        self._current_session_key: Optional[datetime] = None

    def _session_key(self, t_utc: datetime) -> datetime:
        """
        Return a unique key for the session this tick belongs to.

        A "session" runs from reset_time on day D through reset_time on day D+1.
        Any tick whose local time is before today's reset belongs to yesterday's session.
        """
        t_local = t_utc.astimezone(self.tz)
        reset_today = t_local.replace(
            hour=self.reset_hour,
            minute=self.reset_minute,
            second=0,
            microsecond=0,
        )
        if t_local < reset_today:
            return reset_today - timedelta(days=1)
        return reset_today

    def on_tick(self, tick: Tick) -> None:
        session = self._session_key(tick.t)

        if session != self._current_session_key:
            # New session — reset accumulators
            self._sum_v = 0
            self._sum_pv = 0.0
            self._sum_ppv = 0.0
            self._current_session_key = session

        v = tick.size
        p = tick.price

        self._sum_v += v
        self._sum_pv += p * v
        self._sum_ppv += p * p * v

    @property
    def vwap(self) -> float:
        if self._sum_v == 0:
            return 0.0
        return self._sum_pv / self._sum_v

    @property
    def std_dev(self) -> float:
        if self._sum_v == 0:
            return 0.0
        vwap = self.vwap
        variance = (self._sum_ppv / self._sum_v) - (vwap * vwap)
        # Guard against floating-point negatives near zero
        if variance <= 0.0:
            return 0.0
        return math.sqrt(variance)

    @property
    def session_volume(self) -> int:
        return self._sum_v

    def band(self, num_std: float) -> Tuple[float, float]:
        """Return (lower_band, upper_band) at num_std standard deviations from VWAP."""
        v = self.vwap
        sd = self.std_dev
        return (v - num_std * sd, v + num_std * sd)
    
    def seed_from_candles(self, candles: List[Dict[str, Any]]) -> None:
        """
        Pre-populate VWAP accumulators from historical candles for the
        current session. Call this after construction and before live
        ticks start flowing.

        Uses typical price (H+L+C)/3 per candle weighted by volume,
        which is the standard candle-level VWAP approximation. Not as
        precise as tick-level, but far better than starting from zero
        mid-session.

        Only candles belonging to the current session (determined by
        the last candle's timestamp) are included. Earlier sessions
        are ignored.
        """
        if not candles:
            return

        # Determine current session from the last candle's timestamp
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
