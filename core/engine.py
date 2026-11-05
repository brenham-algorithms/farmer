import asyncio
import logging
from typing import Awaitable, Callable, Optional

from tickers import TickerState

from .protocols import AsyncTicker, Ticker
from .types import Tick


async def run_engine_async(
    ticker: AsyncTicker,
    logger: logging.Logger,
    state: TickerState,
    on_tick: Callable[[Tick, logging.Logger, TickerState], Awaitable[None] | None],
) -> Optional[Tick]:
    """
    Asynchronous ticker runner. Returns the last tick processed, if any.
    """
    last: Optional[Tick] = None
    async for tick in ticker:
        last = tick
        res = on_tick(tick, logger, state)
        if asyncio.iscoroutine(res):
            await res
    return last


def run_engine(
    ticker: Ticker,
    logger: logging.Logger,
    state: TickerState,
    ontick: Callable[[Tick, logging.Logger, TickerState], None],
) -> Optional[Tick]:
    """
    Synchronous ticker runner. Returns the last tick processed, if any.
    """
    last: Optional[Tick] = None
    for tick in ticker:
        last = tick
        ontick(tick, logger, state)
    return last
