import asyncio
import logging
from typing import Any, Awaitable, Callable, Dict

from tickers import TickerState

from .protocols import AsyncTicker, Ticker
from .types import Tick


async def run_engine_async(
    ticker: AsyncTicker,
    logger: logging.Logger,
    state: TickerState,
    on_tick: Callable[[Tick, logging.Logger, TickerState], Awaitable[None] | None],
):
    """
    Asynchronous ticker runner.
    """
    async for tick in ticker:
        res = on_tick(tick, logger, state)
        if asyncio.iscoroutine(res):
            await res


def run_engine(
    ticker: Ticker,
    logger: logging.Logger,
    state: TickerState,
    ontick: Callable[[Tick, logging.Logger, TickerState], None],
) -> None:
    """
    Synchronous ticker runner.
    """
    for tick in ticker:
        ontick(tick, logger, state)
