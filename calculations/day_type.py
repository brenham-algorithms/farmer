from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time
from typing import Deque, List, Optional
from zoneinfo import ZoneInfo


@dataclass
class DayTypeSnapshot:
    """Describes the RTH session from the open up to now."""

    t: datetime
    minutes: float  # Since the RTH open
    open: float
    last: float
    high: float
    low: float
    vwap: Optional[float]
    trendness: float  # |last - open| / (high - low)
    efficiency: float  # |last - open| / distance travelled by 1-minute closes
    vwap_crosses: int  # 1-minute closes that changed side of VWAP
    range_vs_normal: Optional[float]  # (high - low) / average range at this minute
    label: str

    @property
    def range(self) -> float:
        return self.high - self.low

    @property
    def crosses_per_hour(self) -> float:
        return self.vwap_crosses * 60 / self.minutes if self.minutes > 0 else 0.0


@dataclass
class _Session:
    day: date
    open: float
    first_t: datetime
    last_t: datetime
    last: float
    high: float
    low: float
    pv: float = 0.0
    volume: float = 0.0
    minute: int = 0  # Index of the minute in progress
    minute_close: float = 0.0
    minute_vwap: Optional[float] = None
    # Completed minutes
    prev_close: float = 0.0
    prev_side: int = 0
    path: float = 0.0
    crosses: int = 0
    ranges: List[Optional[float]] = field(default_factory=list)  # Range at each minute

    @property
    def vwap(self) -> Optional[float]:
        return self.pv / self.volume if self.volume > 0 else None


def _side(price: float, vwap: Optional[float]) -> int:
    if vwap is None or price == vwap:
        return 0
    return 1 if price > vwap else -1


class DayTypeClassifier:
    """
    Labels the RTH session so far as trend up/down, range, chop or mixed.

    The labels describe what has happened since the open; they are not predictions.
    A 2024-2026 MNQ study found that none of these measures, taken at 10:00-12:00 CT,
    predicted the rest of the day (strategies/docs/day_type_monitor.md).

      - trend up / trend down: trendness >= trend_min_trendness
      - range: trendness < range_max_trendness and few VWAP crosses (rotating between edges)
      - chop: trendness < range_max_trendness and crosses_per_hour >= chop_crosses_per_hour
      - mixed: in between
      - too early: fewer than min_minutes since the open

    Feed it ticks or 1-minute candles through on_price. range_vs_normal needs prior full
    sessions (seed candles, or a persistent backtest).
    """

    def __init__(
        self,
        rth_start: time,
        rth_end: time,
        lookback_days: int = 20,
        min_minutes: int = 30,
        trend_min_trendness: float = 0.6,
        range_max_trendness: float = 0.35,
        chop_crosses_per_hour: float = 4.0,
        min_session_minutes: int = 300,
        hysteresis: float = 0.05,
    ) -> None:
        self.tz = ZoneInfo("America/Chicago")
        self.rth_start = rth_start
        self.rth_end = rth_end
        self.session_minutes = (rth_end.hour * 60 + rth_end.minute) - (
            rth_start.hour * 60 + rth_start.minute
        )
        self.min_minutes = min_minutes
        self.trend_min_trendness = trend_min_trendness
        self.range_max_trendness = range_max_trendness
        self.chop_crosses_per_hour = chop_crosses_per_hour
        self.min_session_minutes = min_session_minutes
        self.hysteresis = hysteresis

        self._history: Deque[List[float]] = deque(maxlen=lookback_days)
        self._current: Optional[_Session] = None
        self._prior: Optional[_Session] = None

    def _minutes_since_open(self, local: datetime) -> float:
        start = local.replace(
            hour=self.rth_start.hour,
            minute=self.rth_start.minute,
            second=0,
            microsecond=0,
        )
        return (local - start).total_seconds() / 60

    def in_rth(self, t: datetime) -> bool:
        return self.rth_start <= t.astimezone(self.tz).time() < self.rth_end

    @property
    def session(self) -> Optional[_Session]:
        return self._current

    @property
    def prior_session(self) -> Optional[_Session]:
        return self._prior

    def history_size(self) -> int:
        return len(self._history)

    def on_price(
        self,
        t: datetime,
        price: float,
        volume: float,
        vwap_price: Optional[float] = None,
        high: Optional[float] = None,
        low: Optional[float] = None,
        open_price: Optional[float] = None,
    ) -> bool:
        """Update with a trade or 1-minute candle. Returns True when a new session starts."""
        local = t.astimezone(self.tz)
        if not self._in_rth_local(local):
            return False

        day = local.date()
        new_session = False
        if self._current is None or self._current.day != day:
            if self._current is not None and self._current.day < day:
                self._finalize(self._current)
            first = open_price if open_price is not None else price
            self._current = _Session(
                day=day,
                open=first,
                first_t=t,
                last_t=t,
                last=price,
                high=first,
                low=first,
                minute_close=first,
                prev_close=first,
                ranges=[None] * self.session_minutes,
            )
            new_session = True

        s = self._current
        minute = min(int(self._minutes_since_open(local)), self.session_minutes - 1)
        if minute > s.minute:
            self._complete_minute(s)
            s.minute = minute

        s.last_t, s.last = t, price
        s.high = max(s.high, price if high is None else high)
        s.low = min(s.low, price if low is None else low)
        s.pv += (price if vwap_price is None else vwap_price) * volume
        s.volume += volume
        s.minute_close = price
        s.minute_vwap = s.vwap
        return new_session

    def _in_rth_local(self, local: datetime) -> bool:
        return self.rth_start <= local.time() < self.rth_end

    def _complete_minute(self, s: _Session) -> None:
        s.path += abs(s.minute_close - s.prev_close)
        s.prev_close = s.minute_close
        side = _side(s.minute_close, s.minute_vwap)
        if side != 0:
            if s.prev_side != 0 and side != s.prev_side:
                s.crosses += 1
            s.prev_side = side
        s.ranges[s.minute] = s.high - s.low

    def _finalize(self, s: _Session) -> None:
        self._complete_minute(s)
        minutes = (s.last_t - s.first_t).total_seconds() / 60
        if minutes >= self.min_session_minutes:
            curve, last = [], 0.0
            for r in s.ranges:
                if r is not None:
                    last = r
                curve.append(last)
            self._history.append(curve)
        self._prior = s

    def _normal_range(self, minute: int) -> Optional[float]:
        if not self._history:
            return None
        avg = sum(c[minute] for c in self._history) / len(self._history)
        return avg if avg > 0 else None

    def snapshot(
        self, t: Optional[datetime] = None, current: Optional[str] = None
    ) -> Optional[DayTypeSnapshot]:
        """The session so far, including the minute in progress. None before the first RTH trade."""
        s = self._current
        if s is None:
            return None
        t = t or s.last_t
        minutes = max(self._minutes_since_open(s.last_t.astimezone(self.tz)), 0.0)

        path = s.path + abs(s.minute_close - s.prev_close)
        crosses = s.crosses
        side = _side(s.minute_close, s.minute_vwap)
        if side != 0 and s.prev_side != 0 and side != s.prev_side:
            crosses += 1

        move = abs(s.last - s.open)
        rng = s.high - s.low
        trendness = move / rng if rng > 0 else 0.0
        efficiency = move / path if path > 0 else 0.0
        normal = self._normal_range(min(int(minutes), self.session_minutes - 1))

        snap = DayTypeSnapshot(
            t=t,
            minutes=minutes,
            open=s.open,
            last=s.last,
            high=s.high,
            low=s.low,
            vwap=s.vwap,
            trendness=trendness,
            efficiency=efficiency,
            vwap_crosses=crosses,
            range_vs_normal=rng / normal if normal else None,
            label="",
        )
        snap.label = self.label(snap, current)
        return snap

    def label(self, snap: DayTypeSnapshot, current: Optional[str] = None) -> str:
        """
        Classify a snapshot. With the current label given, the thresholds move in its
        favour by hysteresis (crosses_per_hour by 10x that), so a value hovering near a
        threshold doesn't flip the label back and forth.
        """
        if snap.minutes < self.min_minutes:
            return "too early"

        h = self.hysteresis
        trend_min = self.trend_min_trendness
        range_max = self.range_max_trendness
        chop_min = self.chop_crosses_per_hour
        if current in ("trend up", "trend down"):
            trend_min -= h
        elif current in ("range", "chop"):
            range_max += h
        elif current == "mixed":
            trend_min += h
            range_max -= h
        if current == "chop":
            chop_min -= 10 * h
        elif current == "range":
            chop_min += 10 * h

        if snap.trendness >= trend_min:
            return "trend up" if snap.last > snap.open else "trend down"
        if snap.trendness < range_max:
            return "chop" if snap.crosses_per_hour >= chop_min else "range"
        return "mixed"
