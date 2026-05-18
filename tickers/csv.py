from __future__ import annotations

import asyncio
import csv
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import AsyncIterator, Dict, Iterable, Iterator

from api.models import TickerParams
from core.types import Tick


# Helper that prunes down to the mircosecond and returns a datetime object
def _parse_ts_event(ts: str) -> datetime:
    if ts.endswith("Z"):
        ts = ts[:-1]
    if "." in ts:
        base, frac = ts.split(".", 1)
        ts = f"{base}.{(frac + '000000')[:6]}"
    return datetime.fromisoformat(ts).replace(tzinfo=timezone.utc)


class CsvTicker:
    """
    Streams trade ticks from a CME-style CSV (GLBX MDP3 trades dump).
    You can throttle playback via 'sleep' or run full speed.
    """

    def __init__(
        self,
        logger: logging.Logger,
        params: TickerParams,
        trade_date: str,
    ) -> None:
        if params.data_source.kind != "csv":
            raise ValueError(
                f"Invalid data_source for CsvTicker: {params.data_source.kind}"
            )

        self.logger = logger
        self.params = params

        self.trade_path = Path(
            f"{params.data_source.data_dir}/glbx-mdp3-{trade_date}.trades.csv"
        )
        self.current_symbol: str = params.start_symbol
        self.symbol_volumes: Dict[str, int] = {symbol: 0 for symbol in params.symbols}

    def _rows(self) -> Iterable[Tick]:
        with self.trade_path.open("r", newline="") as f:
            r = csv.reader(f)
            header = next(r)
            idx = {name: i for i, name in enumerate(header)}
            ts_event_i = idx["ts_event"]
            action_i = idx["action"]
            side_i = idx["side"]
            price_i = idx["price"]
            size_i = idx["size"]
            symbol_i = idx["symbol"]

            for row in r:
                try:
                    # We are only interested in trade events
                    if row[action_i] != "T":
                        continue

                    # Ignore irrelevant symbols
                    if row[symbol_i] not in self.params.symbols:
                        continue

                    # Filter by time if requested
                    t = _parse_ts_event(row[ts_event_i])
                    if self.params.start_time and t < self.params.start_time:
                        continue
                    if self.params.end_time and t > self.params.end_time:
                        continue

                    # Track cumulative volume per symbol
                    self.symbol_volumes[row[symbol_i]] += int(row[size_i])

                    # Switch to the new leader when we have enough confidence it's the new active contract
                    leader = max(
                        self.symbol_volumes, key=lambda s: self.symbol_volumes[s]
                    )
                    if leader != self.current_symbol:
                        total = sum(self.symbol_volumes.values())
                        lead = (
                            self.symbol_volumes[leader]
                            - self.symbol_volumes[self.current_symbol]
                        )

                        if total >= self.params.min_total_volume and (
                            lead >= self.params.abs_margin
                            or self.symbol_volumes[leader]
                            >= self.symbol_volumes[self.current_symbol]
                            * (1 + self.params.pct_margin)
                        ):
                            self.logger.info(
                                f"Switching from {self.current_symbol} to {leader} at {t.isoformat()} with volumes: {self.symbol_volumes}"
                            )
                            self.current_symbol = leader

                    if row[symbol_i] != self.current_symbol:
                        continue

                    yield Tick(
                        t=t,
                        price=float(row[price_i]),
                        size=int(float(row[size_i])),
                        side=row[side_i],
                        symbol=row[symbol_i],
                    )
                except Exception as e:
                    self.logger.error(f"Error parsing row: {row}, error: {e}")
                    continue

    async def __aiter__(self) -> AsyncIterator[Tick]:
        for tick in self._rows():
            if self.params.throttle:
                await asyncio.sleep(self.params.throttle)
            yield tick

    def __iter__(self) -> Iterator[Tick]:
        for tick in self._rows():
            if self.params.throttle:
                time.sleep(self.params.throttle)
            yield tick
