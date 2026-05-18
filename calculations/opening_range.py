from datetime import datetime, timedelta
from typing import Optional
from zoneinfo import ZoneInfo
 
from core import Tick
 
 
class LiveOpeningRange:
    """
    Tracks the high and low during a configurable opening range window.
 
    Once the window closes, the range is locked and available for the
    strategy to use as breakout levels.
 
    The opening range is defined by:
      - or_start_hour / or_start_minute: when the range window opens (local time)
      - or_duration_minutes: how long the window lasts
 
    State resets each session (defined by session_reset_hour/minute).
    """
 
    def __init__(
        self,
        or_start_hour: int = 8,
        or_start_minute: int = 30,
        or_duration_minutes: int = 15,
        session_reset_hour: int = 17,
        session_reset_minute: int = 0,
        tz_name: str = "America/Chicago",
    ) -> None:
        self.or_start_hour = or_start_hour
        self.or_start_minute = or_start_minute
        self.or_duration_minutes = or_duration_minutes
        self.session_reset_hour = session_reset_hour
        self.session_reset_minute = session_reset_minute
        self.tz = ZoneInfo(tz_name)
 
        self._range_high: Optional[float] = None
        self._range_low: Optional[float] = None
        self._locked: bool = False
        self._current_session_key: Optional[datetime] = None
        self._tick_count: int = 0
 
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
 
    def _in_range_window(self, t_utc: datetime) -> bool:
        t_local = t_utc.astimezone(self.tz)
        window_start = t_local.replace(
            hour=self.or_start_hour,
            minute=self.or_start_minute,
            second=0,
            microsecond=0,
        )
        window_end = window_start + timedelta(minutes=self.or_duration_minutes)
        return window_start <= t_local < window_end
 
    def _past_range_window(self, t_utc: datetime) -> bool:
        t_local = t_utc.astimezone(self.tz)
        window_start = t_local.replace(
            hour=self.or_start_hour,
            minute=self.or_start_minute,
            second=0,
            microsecond=0,
        )
        window_end = window_start + timedelta(minutes=self.or_duration_minutes)
        return t_local >= window_end
 
    def on_tick(self, tick: Tick) -> None:
        session = self._session_key(tick.t)
 
        if session != self._current_session_key:
            self._range_high = None
            self._range_low = None
            self._locked = False
            self._tick_count = 0
            self._current_session_key = session
 
        if self._locked:
            return
 
        if self._in_range_window(tick.t):
            if self._range_high is None or tick.price > self._range_high:
                self._range_high = tick.price
            if self._range_low is None or tick.price < self._range_low:
                self._range_low = tick.price
            self._tick_count += 1
        elif self._past_range_window(tick.t) and self._range_high is not None:
            self._locked = True
 
    @property
    def is_locked(self) -> bool:
        return self._locked
 
    @property
    def high(self) -> Optional[float]:
        return self._range_high
 
    @property
    def low(self) -> Optional[float]:
        return self._range_low
 
    @property
    def range_size(self) -> Optional[float]:
        if self._range_high is None or self._range_low is None:
            return None
        return self._range_high - self._range_low
 
    @property
    def range_ticks(self) -> Optional[float]:
        """Range size is in price; caller can divide by tick_size to get ticks."""
        return self.range_size
 
    @property
    def tick_count(self) -> int:
        return self._tick_count
