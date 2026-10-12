import json
import logging
import threading
from typing import Any, Dict, Optional

import redis
from projectx_client import Auth, Orders, Positions

from api.models import ProjectXOrderManagerParams


class ProjectXOrderManager:
    """
    Manages order dispatch and position synchronization via the
    ProjectX/TopstepX API.

    Reads user hub events (orders, positions, accounts, trades) from
    Redis channels published by the distributor, rather than connecting
    to SignalR directly. This allows multiple farmer instances to share
    a single market hub + user hub connection.

    Order placement still goes directly to the ProjectX REST API.
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

        # Redis subscription for user events
        self.redis = redis.Redis(host=params.redis_host, port=params.redis_port, db=0)
        self.pubsub = self.redis.pubsub()
        self._listener_thread: Optional[threading.Thread] = None
        self._stopping = False

    def start(self) -> None:
        """Subscribe to user event channels and start listener thread."""
        self.pubsub.subscribe(
            "user:orders",
            "user:positions",
            "user:accounts",
            "user:trades",
        )

        self._listener_thread = threading.Thread(target=self._listen, daemon=True)
        self._listener_thread.start()

        self.logger.info("order manager started — listening to Redis user events")

    def stop(self) -> None:
        """Stop the listener thread and clean up."""
        self._stopping = True

        try:
            self.pubsub.unsubscribe()
            self.pubsub.close()
        except Exception:
            pass

        try:
            self.redis.close()
        except Exception:
            pass

        if self._listener_thread is not None:
            self._listener_thread.join(timeout=2)

        self.logger.info("order manager stopped")

    def _listen(self) -> None:
        """Background thread that reads user events from Redis."""
        try:
            for message in self.pubsub.listen():
                if self._stopping:
                    break

                if message["type"] != "message":
                    continue

                try:
                    channel = message["channel"]
                    if isinstance(channel, bytes):
                        channel = channel.decode("utf-8")

                    data = json.loads(message["data"])

                    if channel == "user:orders":
                        self._on_order_event(data)
                    elif channel == "user:positions":
                        self._on_position_event(data)
                    elif channel == "user:accounts":
                        self._on_account_event(data)
                    elif channel == "user:trades":
                        self._on_trade_event(data)

                except Exception as e:
                    self.logger.error(f"Error processing user event: {e}")
        except Exception:
            if not self._stopping:
                self.logger.error("Redis listener disconnected unexpectedly")

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

    def enter_position(
        self,
        direction: str,
        size: int,
        sl_ticks: Optional[int] = None,
        tp_ticks: Optional[int] = None,
    ) -> Optional[int]:
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

    def close_position(self) -> bool:
        """Place a market order to close a position along with any existing brackets."""
        try:
            result = self.positions.close_contract(
                accountId=self.params.account_id,
                contractId=self.params.contract_id,
            )
            self.logger.info(f"CLOSE POSITION: {self.params.contract_id}")
            return result.get("success", False)
        except Exception as e:
            self.logger.error(f"CLOSE POSITION FAILED: {e}")
            return False

    def reduce_position(self, direction: str, size: int) -> Optional[int]:
        """Place a market order to reduce position size."""
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
                f"REDUCE POSITION PLACED: {'BUY' if side == 0 else 'SELL'} "
                f"{size} {self.params.contract_id} order_id={order_id}"
            )

            return order_id

        except Exception as e:
            self.logger.error(
                f"REDUCE POSITION FAILED: {'BUY' if side == 0 else 'SELL'} "
                f"{size} — {e}"
            )
            return None

    # User event handlers (received via Redis from distributor)

    def _on_order_event(self, data: Any) -> None:
        self.logger.debug(f"ORDER EVENT: {json.dumps(data, indent=2)}")

    def _on_position_event(self, data: Any) -> None:
        self.logger.debug(f"POSITION EVENT: {json.dumps(data, indent=2)}")

    def _on_account_event(self, data: Any) -> None:
        self.logger.debug(f"ACCOUNT EVENT: {json.dumps(data, indent=2)}")

    def _on_trade_event(self, data: Any) -> None:
        self.logger.debug(f"TRADE EVENT: {json.dumps(data, indent=2)}")
