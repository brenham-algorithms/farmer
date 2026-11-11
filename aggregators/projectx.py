import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from projectx_client import Auth, MarketData

from api.models import AggregationParams

# retrieveBars returns at most this many bars per request
MAX_BARS_PER_REQUEST = 20000


class ProjectXAggregator:
    def __init__(self, logger: logging.Logger, params: AggregationParams) -> None:
        if params.data_source.kind != "projectx":
            raise ValueError(
                f"Invalid data source for ProjectXAggregator: {params.data_source.kind}"
            )

        if params.unit != "minutes":
            raise ValueError(f"Unsupported unit: {params.unit}")

        self.logger = logger

        self.jwt_token = Auth(
            base_url=params.data_source.base_url,
            username=params.data_source.username,
            api_key=params.data_source.api_key,
        ).login()

        self.market_data_client = MarketData(
            params.data_source.base_url, self.jwt_token
        )

        self.contract_id = params.data_source.contract_id
        self.days = params.lookback_days
        self.candle_length = params.candle_length
        self.unit = 2

        self.candles: List[Dict[str, Any]] = []

    def get_candles(self) -> List[Dict[str, Any]]:
        self._poll()

        # We want candles in ascending order (oldest first) for moving average calculations
        return self.candles

    def _poll(self) -> None:
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self.days)

        # Split the window into chunks the API can return in one request
        chunk = timedelta(minutes=MAX_BARS_PER_REQUEST * self.candle_length)
        by_time: Dict[str, Dict[str, Any]] = {}
        chunk_start = start
        while chunk_start < now:
            chunk_end = min(chunk_start + chunk, now)
            bars = self.market_data_client.bars(
                contractId=self.contract_id,
                live=False,
                startTime=chunk_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                endTime=chunk_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                unit=self.unit,
                unitNumber=self.candle_length,
                limit=MAX_BARS_PER_REQUEST,
                includePartialBar=False,
            )
            for bar in bars:
                by_time[bar["t"]] = bar
            chunk_start = chunk_end

        self.candles = sorted(by_time.values(), key=lambda bar: _parse_time(bar["t"]))
        self.logger.info(
            f"Fetched {len(self.candles)} {self.candle_length}-minute candles "
            f"for {self.contract_id} over {self.days} days"
        )


def _parse_time(t: Any) -> datetime:
    if isinstance(t, datetime):
        return t
    return datetime.fromisoformat(str(t).replace("Z", "+00:00"))
