import logging

from aggregators import ProjectXAggregator
from api.models import StrategyConfig
from order_managers import ProjectXOrderManager, ProjectXOrderManagerParams
from strategies import build_strategy
from tickers import ProjectXTicker, RedisTicker, TickerState


class Farmer:
    def __init__(self, logger: logging.Logger, strategy_conf: StrategyConfig):
        if strategy_conf.ticker_params is None:
            raise ValueError("ticker_params must be provided in strategy_conf")

        self.logger = logger
        self.strategy_conf = strategy_conf

        self.candles = []
        if strategy_conf.aggregation_params is not None:
            self.candles = ProjectXAggregator(
                logger, strategy_conf.aggregation_params
            ).get_candles()

        self.strategy = build_strategy(strategy_conf, logger, self.candles)

        self.order_manager = None
        if (
            strategy_conf.order_management is not None
            and strategy_conf.order_management.enabled
        ):
            if strategy_conf.order_management.kind == "projectx":
                self.order_manager = ProjectXOrderManager(
                    logger,
                    ProjectXOrderManagerParams(
                        base_url=strategy_conf.order_management.base_url,
                        user_hub_base_url=strategy_conf.order_management.user_hub_base_url,
                        username=strategy_conf.order_management.username,
                        api_key=strategy_conf.order_management.api_key,
                        account_id=strategy_conf.order_management.account_id,
                        contract_id=strategy_conf.order_management.contract_id,
                    ),
                )

        self.handler_state: TickerState = TickerState(
            strategy=self.strategy,
            total_pnl=0.00,
            position=None,
            order_manager=self.order_manager,
        )

        self.ticker: ProjectXTicker | RedisTicker
        if strategy_conf.ticker_params.data_source.kind == "projectx":
            self.ticker = ProjectXTicker(
                logger,
                strategy_conf.ticker_params,
                self.strategy.get_live_handler(),
                self.handler_state,
            )
        elif strategy_conf.ticker_params.data_source.kind == "redis":
            self.ticker = RedisTicker(
                logger,
                strategy_conf.ticker_params,
                self.strategy.get_live_handler(),
                self.handler_state,
            )
        else:
            raise ValueError(
                f"Invalid data source for Farmer: {strategy_conf.ticker_params.data_source.kind}"
            )


    def start(self):
        if self.order_manager:
            self.order_manager.start()
        
        self.ticker.start()
