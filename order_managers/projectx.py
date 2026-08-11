import json
import logging
from typing import Any, Dict, List, Optional

from pydantic import BaseModel
from signalrcore.hub_connection_builder import HubConnectionBuilder

from projectx_client import Auth, Orders, Positions


class ProjectXOrderManagerParams(BaseModel):
    base_url: str
    user_hub_base_url: str
    username: str
    api_key: str
    account_id: int
    contract_id: str


class ProjectXOrderManager:
    """
    Manages order dispatch and position synchronization via the
    ProjectX/TopstepX API.

    Designed to be attached to Position objects. When present,
    Position.close(), .cut(), and .add() automatically dispatch
    orders to the broker.

    On startup, call sync() to query open positions and reconcile
    local state with the server.
    """

    def __init__(
        self,
        logger: logging.Logger,
        params: ProjectXOrderManagerParams,
    ) -> None:
        self.logger = logger
        self.params = params

        # Authenticate
        self.jwt_token = Auth(
            base_url=params.base_url,
            username=params.username,
            api_key=params.api_key,
        ).login()

        # REST clients
        self.orders = Orders(
            base_url=params.base_url,
            jwt_token=self.jwt_token,
        )
        self.positions = Positions(
            base_url=params.base_url,
            jwt_token=self.jwt_token,
        )

        # User hub for realtime fill and position events
        self.user_hub = (
            HubConnectionBuilder()
            .with_url(
                f"{params.user_hub_base_url}?access_token={self.jwt_token}",
                options={
                    "access_token_factory": lambda: self.jwt_token,
                    "headers": {},
                    "verify_ssl": True,
                },
            )
            .configure_logging(logging.WARNING)
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

        # Register user hub handlers
        self.user_hub.on_open(self._on_hub_open)
        self.user_hub.on_close(self._on_hub_close)
        self.user_hub.on_error(self._on_hub_error)
        self.user_hub.on("GatewayUserOrder", self._on_order_event)
        self.user_hub.on("GatewayUserPosition", self._on_position_event)
        self.user_hub.on("GatewayUserAccount", self._on_account_event)
        self.user_hub.on("GatewayUserTrade", self._on_trade_event)

    def start(self) -> None:
        """Start the user hub connection. Call before trading begins."""
        self.user_hub.start()

    def stop(self) -> None:
        """Stop the user hub connection. Call on shutdown."""
        self.user_hub.send("UnsubscribeAccounts", [])
        self.user_hub.send("UnsubscribeOrders", [self.params.account_id])
        self.user_hub.send("UnsubscribePositions", [self.params.account_id])
        self.user_hub.send("UnsubscribeTrades", [self.params.account_id])
        self.user_hub.stop()

    # Reconciliation

    def sync(self) -> Optional[Dict[str, Any]]:
        """
        Query open positions from the server. Returns the position
        dict for our contract if one exists, None otherwise.

        Call on startup to reconcile local state with the server.

        Returns dict with keys: id, contractId, type (1=long),
        size, averagePrice — or None if flat.
        """
        try:
            open_positions = self.positions.search_open(
                accountId=self.params.account_id,
            )

            for pos in open_positions:
                if pos["contractId"] == self.params.contract_id:
                    self.logger.info(
                        f"SYNC: found open position — "
                        f"{'LONG' if pos['type'] == 1 else 'SHORT'} "
                        f"{pos['size']} @ {pos['averagePrice']}"
                    )
                    return pos

            self.logger.info("SYNC: no open position")
            return None

        except Exception as e:
            self.logger.error(f"SYNC FAILED: {e}")
            return None

    # Order dispatch

    def enter_position(self, direction: str, size: int, sl_ticks: int = None, tp_ticks: int = None) -> Optional[int]:
        side = 0 if direction == "LONG" else 1

        payload = {
            "accountId": self.params.account_id,
            "contractId": self.params.contract_id,
            "type": 2,
            "side": side,
            "size": size,
        }

        if sl_ticks is not None:
            sl_offset = -sl_ticks if direction == "LONG" else sl_ticks
            payload["stopLossBracket"] = {"ticks": sl_offset, "type": 4}
        if tp_ticks is not None:
            tp_offset = tp_ticks if direction == "LONG" else -tp_ticks
            payload["takeProfitBracket"] = {"ticks": tp_offset, "type": 1}

        try:
            order_id = self.orders.place(**payload)
            self.logger.info(
                f"ENTRY ORDER PLACED: {'BUY' if side == 0 else 'SELL'} "
                f"{size} {self.params.contract_id} order_id={order_id}"
                f"{f' sl_bracket={sl_ticks}t' if sl_ticks else ''}"
                f"{f' tp_bracket={tp_ticks}t' if tp_ticks else ''}"
            )
            return order_id
        except Exception as e:
            self.logger.error(f"ENTRY ORDER FAILED: {e}")
            return None


    def close_position(self, direction: str, size: int) -> Optional[int]:
        """Place a market order to close a position (opposite side)."""
        side = 1 if direction == "LONG" else 0

        try:
            order_id = self.orders.place(
                accountId=self.params.account_id,
                contractId=self.params.contract_id,
                type=2,
                side=side,
                size=size,
            )

            self.logger.info(
                f"CLOSE ORDER PLACED: {'BUY' if side == 0 else 'SELL'} "
                f"{size} {self.params.contract_id} order_id={order_id}"
            )

            return order_id

        except Exception as e:
            self.logger.error(
                f"CLOSE ORDER FAILED: {'BUY' if side == 0 else 'SELL'} " f"{size} — {e}"
            )
            return None

    def reduce_position(self, direction: str, size: int) -> Optional[int]:
        """Place a market order to reduce position size."""
        return self.close_position(direction, size)

    # User hub event handlers

    def _on_hub_open(self) -> None:
        self.logger.info("user hub connected")

        self.user_hub.send("SubscribeAccounts", [])
        self.user_hub.send("SubscribeOrders", [self.params.account_id])
        self.user_hub.send("SubscribePositions", [self.params.account_id])
        self.user_hub.send("SubscribeTrades", [self.params.account_id])

        self.logger.info("subscribed to user hub events")

    def _on_hub_close(self) -> None:
        self.logger.info("user hub disconnected")

    def _on_hub_error(self, error) -> None:
        self.logger.error(f"user hub error: {error}")

    def _on_order_event(self, args) -> None:
        self.logger.info(f"ORDER EVENT: {json.dumps(args, indent=2)}")

    def _on_position_event(self, args) -> None:
        self.logger.info(f"POSITION EVENT: {json.dumps(args, indent=2)}")

    def _on_account_event(self, args) -> None:
        self.logger.info(f"ACCOUNT EVENT: {json.dumps(args, indent=2)}")

    def _on_trade_event(self, args) -> None:
        self.logger.info(f"TRADE EVENT: {json.dumps(args, indent=2)}")
