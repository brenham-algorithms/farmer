import json
import logging
from datetime import datetime
from typing import Callable

import redis

from api.models import TickerParams
from core.types import Tick

from .state import TickerState


class RedisTicker:
    """
    Subscribes to a Redis pub/sub channel and feeds ticks to the
    handler. Designed to consume ticks published by the distributor.

    Follows the same callback pattern as ProjectXTicker: handler and
    state are passed in the constructor, and the handler is called
    on every tick.
    """

    def __init__(
        self,
        logger: logging.Logger,
        params: TickerParams,
        handler: Callable,
        state: TickerState,
    ) -> None:
        if params.data_source.kind != "redis":
            raise ValueError(
                f"Invalid data source for RedisTicker: {params.data_source.kind}"
            )

        self.logger = logger
        self.params = params
        self.handler = handler
        self.state = state

        self.redis = redis.Redis(
            host=params.data_source.redis_host,
            port=params.data_source.redis_port,
            db=0,
        )

        self.channel = f"ticks:{params.data_source.contract_id}"
        self.pubsub = self.redis.pubsub()

    def start(self):
        self.pubsub.subscribe(self.channel)
        self.logger.info(f"subscribed to Redis channel {self.channel}")

        try:
            for message in self.pubsub.listen():
                if message["type"] != "message":
                    continue

                try:
                    trade = json.loads(message["data"])

                    # Ignore non-standard trades
                    if trade["type"] > 1:
                        continue

                    tick = Tick(
                        t=datetime.fromisoformat(
                            trade["timestamp"].replace("Z", "+00:00")
                        ),
                        price=trade["price"],
                        size=trade["volume"],
                        side="B" if trade["type"] == 0 else "A",
                        symbol=trade["symbolId"],
                    )

                    self.handler(tick, self.logger, self.state)

                except Exception as e:
                    self.logger.error(f"Error processing Redis message: {e}")
                    continue

        except KeyboardInterrupt:
            self.logger.info("user stopped Redis ticker")
            self.pubsub.unsubscribe(self.channel)
            self.pubsub.close()
            self.redis.close()
