import logging
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, List, Optional
from zoneinfo import ZoneInfo

from api.models import VwapDiagnosticParams
from calculations.candle_vwap import CandleVwap
from calculations.vwap import LiveVwap
from core.types import Signal, Tick
from tickers import TickerState


class VwapDiagnostic:
    """
    Diagnostic strategy that logs both LiveVwap (tick-level) and
    CandleVwap (1-min candle-level) every 30 minutes for comparison
    against charting platforms.
    """

    def __init__(
        self,
        logger: logging.Logger,
        candles: List[Dict[str, Any]],
        params: VwapDiagnosticParams,
    ) -> None:
        self.logger = logger
        self.tz = ZoneInfo("America/Chicago")
        self.precision = params.precision

        self.live_vwap = LiveVwap(
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
        if params.seed_from_candles:
            self.live_vwap.seed_from_candles(candles)

        self.candle_vwap = CandleVwap(
            session_reset_hour=params.session_reset_hour,
            session_reset_minute=params.session_reset_minute,
        )
        if params.seed_from_candles:
            self.candle_vwap.seed_from_candles(candles)

        self._last_log_time: Optional[datetime] = None

    def _ct(self, t: datetime) -> str:
        return t.astimezone(self.tz).strftime("%Y-%m-%d %H:%M:%S CT")

    def check(self, tick: Tick, **kwargs: Any) -> Signal | None:
        self.live_vwap.on_tick(tick)
        self.candle_vwap.on_tick(tick)

        now = tick.t
        should_log = False

        if self._last_log_time is None:
            should_log = True
        elif (now - self._last_log_time).total_seconds() >= 1800:
            should_log = True

        if should_log:
            lv = self.live_vwap.vwap
            ls = self.live_vwap.std_dev
            cv = self.candle_vwap.vwap
            cs = self.candle_vwap.std_dev
            diff = cv - lv

            self.logger.info(
                f"[{self._ct(now)}] price={tick.price:.{self.precision}f} "
                f"LiveVWAP={lv:.{self.precision}f} (std={ls:.{self.precision}f}) "
                f"CandleVWAP={cv:.{self.precision}f} (std={cs:.{self.precision}f}) "
                f"diff={diff:+.{self.precision}f}"
            )

            self._last_log_time = now

        return None

    def reset(self) -> None:
        self._last_log_time = None

    def get_backtest_handler(
        self,
    ) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return vwap_diagnostic_handler

    def get_live_handler(self) -> Callable[[Tick, logging.Logger, TickerState], None]:
        return vwap_diagnostic_handler

    def __repr__(self) -> str:
        return "VwapDiagnostic()"


def vwap_diagnostic_handler(
    tick: Tick, logger: logging.Logger, state: TickerState
) -> None:
    state.strategy.check(tick)
