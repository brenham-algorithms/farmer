from datetime import datetime, timedelta
from typing import Any, Dict, List

import pytest

import aggregators.projectx as projectx
from api.models import AggregationParams, ProjectXDataSource
from config import init_null_logger


class FakeMarketData:
    """Returns one bar per minute of each requested window, newest first, like the API."""

    def __init__(self, base_url: str, jwt_token: str) -> None:
        self.requests: List[Dict[str, Any]] = []

    def bars(self, **payload: Any) -> List[Dict[str, Any]]:
        self.requests.append(payload)
        start = datetime.fromisoformat(payload["startTime"].replace("Z", "+00:00"))
        end = datetime.fromisoformat(payload["endTime"].replace("Z", "+00:00"))
        out = []
        t = start.replace(second=0, microsecond=0)
        while t <= end:  # Inclusive end, so chunk boundaries overlap
            out.append({"t": t.isoformat(), "o": 1, "h": 1, "l": 1, "c": 1, "v": 1})
            t += timedelta(minutes=1)
        return list(reversed(out))[: payload["limit"]]


class FakeAuth:
    def __init__(self, **kwargs: Any) -> None:
        pass

    def login(self) -> str:
        return "jwt"


def test_long_windows_are_split_into_chunks(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(projectx, "Auth", FakeAuth)
    monkeypatch.setattr(projectx, "MarketData", FakeMarketData)
    params = AggregationParams(
        data_source=ProjectXDataSource(
            base_url="",
            market_hub_base_url="",
            username="",
            api_key="",
            contract_id="CON.F.US.MNQ.Z26",
        ),
        lookback_days=30,
        candle_length=1,
    )
    aggregator = projectx.ProjectXAggregator(init_null_logger("INFO"), params)
    candles = aggregator.get_candles()

    requests = aggregator.market_data_client.requests  # type: ignore[attr-defined]
    assert len(requests) == 3  # 43,200 minutes in chunks of 20,000
    assert all(r["limit"] == projectx.MAX_BARS_PER_REQUEST for r in requests)

    times = [c["t"] for c in candles]
    assert times == sorted(times)  # Oldest first
    assert len(times) == len(set(times))  # Overlapping boundaries are de-duplicated
    assert len(times) == 30 * 24 * 60
