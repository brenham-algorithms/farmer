import logging
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

from aggregators import CsvAggregator
from api.models import BacktestConfig, BacktestResponse, BacktestResult
from core import run_engine_async
from strategies import build_strategy
from tickers import CsvTicker


async def run_backtest_async(
    config: BacktestConfig,
    logger: Optional[logging.Logger] = None,
) -> BacktestResponse:
    if config.strategy.strategy_params.kind == "static_bounce":
        return await _run_backtest_async_with_seeding(config, logger)
    elif config.strategy.strategy_params.kind == "static_bounce_with_delta":
        return await _run_backtest_async_with_seeding(config, logger)
    elif config.strategy.strategy_params.kind == "mean_reversion_ema":
        return await _run_backtest_async_with_seeding(config, logger)
    elif config.strategy.strategy_params.kind == "vwap_mean_reversion":
        return await _run_backtest_async_without_seeding(config, logger)
    elif config.strategy.strategy_params.kind == "vwap_mean_reversion_with_scaling":
        return await _run_backtest_async_without_seeding(config, logger)
    elif config.strategy.strategy_params.kind == "vwap_mean_reversion_ladder":
        return await _run_backtest_async_without_seeding(config, logger)
    else:
        raise ValueError(
            f"Unsupported strategy kind: {config.strategy.strategy_params.kind}"
        )


async def _run_backtest_async_without_seeding(
    config: BacktestConfig,
    logger: Optional[logging.Logger] = None,
) -> BacktestResponse:
    # Validations
    if config.strategy.ticker_params is None:
        raise ValueError(
            "Ticker parameters are required for backtesting without seeding"
        )

    if config.strategy.ticker_params.data_source.kind != "csv":
        raise ValueError(
            f"Unsupported data_source for backtesting: {config.strategy.ticker_params.data_source.kind}"
        )

    results: List[BacktestResult] = []

    if logger is None:
        logger = logging.getLogger("backtest_async_without_seeding")

    for bt_date in config.get_dates():
        logger.info(f"Running backtest {config.name} for date: {bt_date}")

        # Initialize / reset requested strategy.
        # The candles list is empty because no seeding is required for this backtest mode
        strategy = build_strategy(config.strategy, logger, [])
        handler = strategy.get_handler()

        # Initialize / reset handler state
        state: Dict[str, Any] = {
            "total_pnl": 0.00,
            "position": None,
            "strategy": strategy,
            "tick_size": config.strategy.strategy_params.tick_size,
            "tick_value": config.strategy.strategy_params.tick_value,
        }

        ticker = CsvTicker(logger, config.strategy.ticker_params, bt_date)

        await run_engine_async(ticker, logger, state, handler)

        config.strategy.ticker_params.start_symbol = ticker.current_symbol

        results.append(
            BacktestResult(
                pnl=round(state["total_pnl"], 2),
                trades_file=str(ticker.trade_path),
            )
        )

    return BacktestResponse(
        backtest_name=config.name,
        total_pnl=round(sum(r.pnl for r in results), 2),
        results=results,
    )


async def _run_backtest_async_with_seeding(
    config: BacktestConfig,
    logger: Optional[logging.Logger] = None,
) -> BacktestResponse:
    # Validations
    if config.strategy.aggregation_params is None:
        raise ValueError(
            "Aggregation parameters are required for backtesting with seeding"
        )

    if config.strategy.ticker_params is None:
        raise ValueError("Ticker parameters are required for backtesting with seeding")

    if config.strategy.ticker_params.data_source.kind != "csv":
        raise ValueError(
            f"Unsupported data_source for backtesting: {config.strategy.ticker_params.data_source.kind}"
        )

    results: List[BacktestResult] = []

    if logger is None:
        logger = logging.getLogger("backtest_async_with_seeding")

    for bt_date in config.get_dates():
        d = datetime.strptime(bt_date, "%Y%m%d").date()

        logger.info(f"Running backtest {config.name} for date: {d}")

        start_date = d - timedelta(
            days=config.strategy.aggregation_params.lookback_days
        )
        end_date = d - timedelta(days=1)

        # Build a list of historical candles over the specified time window via CsvAggregator
        aggregator = CsvAggregator(
            logger,
            config.strategy.aggregation_params,
            config.strategy.ticker_params,
            start_date,
            end_date,
        )
        candles = aggregator.get_candles()

        # Initialize / reset requested strategy
        strategy = build_strategy(config.strategy, logger, candles)
        handler = strategy.get_handler()

        # Initialize / reset handler state
        state: Dict[str, Any] = {
            "total_pnl": 0.00,
            "position": None,
            "strategy": strategy,
            "tick_size": config.strategy.strategy_params.tick_size,
            "tick_value": config.strategy.strategy_params.tick_value,
        }

        ticker = CsvTicker(logger, config.strategy.ticker_params, bt_date)

        await run_engine_async(ticker, logger, state, handler)

        config.strategy.ticker_params.start_symbol = ticker.current_symbol

        results.append(
            BacktestResult(
                pnl=round(state["total_pnl"], 2),
                trades_file=str(ticker.trade_path),
            )
        )

    return BacktestResponse(
        backtest_name=config.name,
        total_pnl=round(sum(r.pnl for r in results), 2),
        results=results,
    )
