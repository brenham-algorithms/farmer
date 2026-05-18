import logging
from typing import Dict, Any

from aggregators import ProjectXAggregator
from api.models import StrategyConfig
from strategies import build_strategy
from tickers import ProjectXTicker, TickerState


class Farmer:
    def __init__(self, strategy_conf: StrategyConfig, logger: logging.Logger):
        self.strategy_conf = strategy_conf
        self.logger = logger

        self.aggregator = ProjectXAggregator(logger, strategy_conf.aggregation_params)

        self.candles = self.aggregator.get_candles()

        self.strategy = build_strategy(strategy_conf, logger, self.candles)
        self.strategy.vwap.seed_from_candles(self.candles)

        self.handler_state: TickerState = TickerState(
            strategy=self.strategy,
            total_pnl=0.00,
            position=None,
        )

        self.ticker = ProjectXTicker(logger, strategy_conf.ticker_params, self.strategy.get_live_handler(), self.handler_state)
    
    def start(self):
        self.ticker.start()
