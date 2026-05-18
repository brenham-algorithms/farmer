from typing import Any, AsyncIterator, Callable, Dict, Iterator, Protocol

from .types import Tick


# Pseudo interface for anything that can stream tick objects synchronously
class Ticker(Protocol):
    def __iter__(self) -> Iterator[Tick]: ...


# Pseudo interface for anything that can stream tick objects asynchronously
class AsyncTicker(Protocol):
    def __aiter__(self) -> AsyncIterator[Tick]: ...


# Pseudo interface for anything that can aggregate candles
class Aggregator(Protocol):
    def get_candles(self) -> list[Dict[str, Any]]: ...


# Pseudo interface for anything that can implement a trading strategy
class Strategy(Protocol):
    def check(
        self, tick: Tick, **kwargs: Any
    ) -> Dict[str, Any] | None: ...

    def reset(self) -> None: ...

    def get_handler(self) -> Callable: ...
