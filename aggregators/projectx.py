import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List

from api.models import AggregationParams
from projectx_client import Auth, MarketData


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

        # TODO: Define candle type instead of using a Dict
        self.candles: List[Dict[str, Any]] = []

    def get_candles(self) -> List[Dict[str, Any]]:
        self._poll()

        # We want candles in ascending order (oldest first) for moving average calculations
        return list(reversed(self.candles))

    def _poll(self) -> None:
        num_candles = math.ceil(60 / self.candle_length) * 24 * self.days
        now = datetime.now(timezone.utc)
        start = now - timedelta(days=self.days)

        # Poll candles from server
        self.candles = self.market_data_client.bars(
            contractId=self.contract_id,
            live=False,
            startTime=start.strftime("%Y-%m-%dT%H:%M:%SZ"),
            endTime=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            unit=self.unit,
            unitNumber=self.candle_length,
            limit=num_candles,
            includePartialBar=False,
        )
