import logging
import time as t
from datetime import datetime
from typing import Callable

from projectx_client import Auth
from signalrcore.hub_connection_builder import HubConnectionBuilder

from api.models import TickerParams
from core.types import Tick

from .state import TickerState


class ProjectXTicker:
    def __init__(
        self,
        logger: logging.Logger,
        params: TickerParams,
        handler: Callable,
        state: TickerState,
    ) -> None:
        if params.data_source.kind != "projectx":
            raise ValueError(
                f"Invalid data source for ProjectXTicker: {params.data_source.kind}"
            )

        self.logger = logger
        self.params = params
        self.handler = handler
        self.state = state

        self.jwt_token = Auth(
            base_url=params.data_source.base_url,
            username=params.data_source.username,
            api_key=params.data_source.api_key,
        ).login()

        self.market_hub = (
            HubConnectionBuilder()
            .with_url(
                f"{params.data_source.market_hub_base_url}?access_token={self.jwt_token}",
                options={
                    "access_token_factory": lambda: self.jwt_token,
                    "headers": {},
                    "verify_ssl": True,
                },
            )
            .configure_logging(logging.INFO)
            .with_automatic_reconnect(
                {
                    "type": "raw",
                    "keep_alive_interval": 10,
                    "reconnect_interval": 5,
                    "max_attempts": 5,
                }
            )
            .build()
        )

        # Register market hub handlers
        self.market_hub.on_open(self.on_open)
        self.market_hub.on_close(self.on_close)
        self.market_hub.on_error(self.on_error)
        self.market_hub.on("GatewayTrade", self.on_trade)

    def start(self):
        self.market_hub.start()
        try:
            while True:
                t.sleep(1)
        except KeyboardInterrupt:
            self.logger.info(
                "user stopped market hub", extra={"event": "market_hub_stop"}
            )
            self.market_hub.send(
                "UnsubscribeContractTrades",
                [self.params.data_source.contract_id],
            )
            self.market_hub.stop()

    def on_open(self):
        self.logger.info(
            "user opened connection to market hub",
            extra={"event": "market_hub_connect"},
        )

        # Subscribe to the configured futures contract
        self.market_hub.send(
            "SubscribeContractTrades",
            [self.params.data_source.contract_id],
        )

        self.logger.info(
            f"subscribed to contract {self.params.data_source.contract_id}",
            extra={"event": "market_hub_subscribe"},
        )

    def on_close(self):
        self.logger.info(
            "user disconnected from market hub",
            extra={"event": "market_hub_disconnect"},
        )

    def on_error(self, error):
        self.logger.error(
            "market hub error",
            extra={"event": "market_hub_error", "error": error.error},
        )

    def on_trade(self, args):
        contract_id, trades = args
        for t in trades:
            # Ignore non-standard trades
            if t["type"] > 1:
                continue

            tick = Tick(
                t=datetime.fromisoformat(t["timestamp"].replace("Z", "+00:00")),
                price=t["price"],
                size=t["volume"],
                side="B" if t["type"] == 0 else "A",
                symbol=t["symbolId"],
            )

            self.handler(tick, self.logger, self.state)
