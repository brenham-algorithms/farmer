import logging
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from api.models import DayTypeMonitorParams
from calculations.day_type import DayTypeClassifier, DayTypeSnapshot
from core.types import Signal, Tick
from tickers import TickerState


class DayTypeMonitor:
    """
    Never trades. Labels the RTH session so far (trend up/down, range, chop, mixed) and logs:
      - each label change, once the new label has held for confirm_minutes;
      - a snapshot every snapshot_minutes (0 disables);
      - a summary of each session after it closes.

    The labels describe the day so far; they don't predict the rest of it (see
    strategies/docs/day_type_monitor.md). Seed with 1-minute candles so range_vs_normal
    has history, or use persist_across_files in backtests. With discover.py, the
    printed repr is the current state.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: DayTypeMonitorParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")
        self.precision = params.precision
        self.confirm_minutes = params.confirm_minutes
        self.snapshot_minutes = params.snapshot_minutes
        self.rth_end = time(params.rth_end_hour, params.rth_end_minute)

        self.classifier = DayTypeClassifier(
            rth_start=time(params.rth_start_hour, params.rth_start_minute),
            rth_end=self.rth_end,
            lookback_days=params.lookback_days,
            min_minutes=params.min_minutes,
            trend_min_trendness=params.trend_min_trendness,
            range_max_trendness=params.range_max_trendness,
            chop_crosses_per_hour=params.chop_crosses_per_hour,
            min_session_minutes=params.min_session_minutes,
            hysteresis=params.hysteresis,
        )

        # Label state for the current session
        self.label: Optional[str] = None  # Last confirmed label
        self._candidate: Optional[str] = None
        self._candidate_since: Optional[datetime] = None
        self._last_minute: Optional[datetime] = None
        self._last_snapshot_log: Optional[datetime] = None
        self._summarized: Optional[date] = None
        self.changes: List[str] = []  # "HH:MM label" for the current session

        self._seed(candles)

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def _seed(self, candles: List[Dict[str, Any]]) -> None:
        for candle in candles:
            t = candle["t"]
            if isinstance(t, str):
                t = datetime.fromisoformat(t.replace("Z", "+00:00"))
            typical = (candle["h"] + candle["l"] + candle["c"]) / 3
            if self.classifier.on_price(
                t,
                candle["c"],
                candle.get("v", 0),
                vwap_price=typical,
                high=candle["h"],
                low=candle["l"],
                open_price=candle["o"],
            ):
                self._reset_labels()
            # Replay label changes quietly, so a mid-session start knows the day so far
            if self.classifier.in_rth(t):
                self._on_minute(t + timedelta(minutes=1), log=False)
        if candles:
            self.logger.info(
                f"DayTypeMonitor seeded: {self.classifier.history_size()} sessions of history"
            )

    def _reset_labels(self) -> None:
        self.label = None
        self._candidate = None
        self._candidate_since = None
        self._last_minute = None
        self._last_snapshot_log = None
        self.changes = []

    def _fmt(self, snap: DayTypeSnapshot) -> str:
        p = self.precision
        rel = (
            f" ({snap.range_vs_normal:.2f}x normal)"
            if snap.range_vs_normal is not None
            else ""
        )
        vwap = f"{snap.vwap:.{p}f}" if snap.vwap is not None else "n/a"
        return (
            f"open={snap.open:.{p}f} last={snap.last:.{p}f} "
            f"move={snap.last - snap.open:+.{p}f} range={snap.range:.{p}f}{rel} "
            f"trendness={snap.trendness:.2f} efficiency={snap.efficiency:.2f} "
            f"vwap={vwap} vwap_crosses={snap.vwap_crosses}"
        )

    def _on_minute(self, t: datetime, log: bool = True) -> None:
        """Re-evaluate the label at most once per minute."""
        minute = t.replace(second=0, microsecond=0)
        if self._last_minute == minute:
            return
        self._last_minute = minute

        snap = self.classifier.snapshot(t, current=self.label)
        if snap is None:
            return

        if snap.label != self._candidate:
            self._candidate, self._candidate_since = snap.label, t
        held = (t - self._candidate_since).total_seconds() / 60  # type: ignore[operator]
        if snap.label != self.label and (
            self.label is None or held >= self.confirm_minutes
        ):
            old = self.label
            self.label = snap.label
            if snap.label != "too early":
                self.changes.append(f"{t.astimezone(self.tz):%H:%M} {snap.label}")
            if log and old is not None:
                self.logger.info(
                    f"[{self._ct(t)}] DAY TYPE {old} -> {snap.label} | {self._fmt(snap)}"
                )

        if log and self.snapshot_minutes > 0:
            due = self._last_snapshot_log is None or (
                t - self._last_snapshot_log
            ) >= timedelta(minutes=self.snapshot_minutes)
            if due:
                self._last_snapshot_log = t
                self.logger.info(
                    f"[{self._ct(t)}] DAY TYPE {self.label} | {self._fmt(snap)}"
                )

    def _summarize(self) -> None:
        s = self.classifier.session
        if s is None or self._summarized == s.day:
            return
        self._summarized = s.day
        snap = self.classifier.snapshot()
        if snap is None:
            return
        self.logger.info(
            f"[{s.day} close] DAY TYPE SUMMARY {snap.label} | "
            f"{self._fmt(snap)} | changes: {', '.join(self.changes) or 'none'}"
        )

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        local = tick.t.astimezone(self.tz)
        s = self.classifier.session

        # Summarize the last session once it's over: after the RTH close, or on a later day
        if s is not None and self._summarized != s.day:
            if local.date() > s.day or (
                local.date() == s.day and local.time() >= self.rth_end
            ):
                self._summarize()

        if self.classifier.on_price(tick.t, tick.price, tick.size):
            self._reset_labels()
        if self.classifier.in_rth(tick.t):
            self._on_minute(tick.t)
        return None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return day_type_monitor_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return day_type_monitor_handler

    def __repr__(self) -> str:
        snap = self.classifier.snapshot(current=self.label)
        if snap is None:
            return "DayTypeMonitor(no RTH session yet)"
        return (
            f"DayTypeMonitor({snap.t.astimezone(self.tz):%Y-%m-%d %H:%M} CT: {snap.label} | {self._fmt(snap)} | "
            f"changes: {', '.join(self.changes) or 'none'} | "
            f"history: {self.classifier.history_size()} sessions)"
        )


def day_type_monitor_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    state.strategy.check(tick)
