import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from aggregators import CsvAggregator
from api.models import BacktestConfig, BacktestResponse, BacktestResult
from core import Tick, run_engine_async
from strategies import build_strategy
from tickers import CsvTicker, TickerState


def _has_trades_file(
    config: BacktestConfig, bt_date: str, logger: logging.Logger
) -> bool:
    # RTH-only data sets have no files for Sundays or exchange holidays
    if config.strategy.ticker_params is None:
        return True  # The caller's validation reports this
    data_dir = getattr(config.strategy.ticker_params.data_source, "data_dir", "")
    path = Path(f"{data_dir}/glbx-mdp3-{bt_date}.trades.csv")
    if not path.exists():
        logger.warning(f"No trades file for {bt_date} ({path}), skipping")
        return False
    return True


def flatten_at_end_of_file(
    state: TickerState, last_tick: Optional[Tick], logger: logging.Logger
) -> None:
    """Close a position left open when the day's data ends, at the last trade price."""
    position = state.position
    if position is None or last_tick is None:
        return

    pnl = position.close(last_tick.price)
    state.total_pnl += pnl
    state.position = None

    tz = ZoneInfo("America/Chicago")
    ts_start = position.timestamp.replace(microsecond=0).astimezone(tz)
    ts_end = last_tick.t.replace(microsecond=0).astimezone(tz)
    logger.info(
        f"End-of-file flatten {position.direction} @ {last_tick.price}, "
        f"Start = {ts_start}, End = {ts_end}, PnL = ${pnl:.2f}"
    )


async def run_backtest_async(
    config: BacktestConfig,
    logger: Optional[logging.Logger] = None,
) -> BacktestResponse:
    if config.strategy.aggregation_params is not None:
        return await _run_backtest_async_with_seeding(config, logger)
    else:
        return await _run_backtest_async_without_seeding(config, logger)


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

    persist = config.strategy.persist_across_files
    if persist:
        strategy = build_strategy(config.strategy, logger, [])
        handler = strategy.get_backtest_handler()
        state = TickerState(strategy=strategy, total_pnl=0.0, position=None)

    running_total = 0.0
    for bt_date in config.get_dates():
        if not _has_trades_file(config, bt_date, logger):
            continue

        logger.info(f"Running backtest {config.name} for date: {bt_date}")

        if not persist:
            strategy = build_strategy(config.strategy, logger, [])
            handler = strategy.get_backtest_handler()
            state = TickerState(strategy=strategy, total_pnl=0.0, position=None)

        ticker = CsvTicker(logger, config.strategy.ticker_params, bt_date)

        last_tick = await run_engine_async(ticker, logger, state, handler)
        if config.strategy.flatten_at_end_of_file:
            flatten_at_end_of_file(state, last_tick, logger)

        config.strategy.ticker_params.start_symbol = ticker.current_symbol

        results.append(
            BacktestResult(
                pnl=round(state.total_pnl - running_total, 2),
                trades_file=str(ticker.trade_path),
            )
        )
        running_total = state.total_pnl if persist else 0.0

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
        if not _has_trades_file(config, bt_date, logger):
            continue

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
        handler = strategy.get_backtest_handler()

        # Initialize / reset handler state
        state: TickerState = TickerState(strategy=strategy)

        ticker = CsvTicker(logger, config.strategy.ticker_params, bt_date)

        last_tick = await run_engine_async(ticker, logger, state, handler)
        if config.strategy.flatten_at_end_of_file:
            flatten_at_end_of_file(state, last_tick, logger)

        config.strategy.ticker_params.start_symbol = ticker.current_symbol

        results.append(
            BacktestResult(
                pnl=round(state.total_pnl, 2),
                trades_file=str(ticker.trade_path),
            )
        )

    return BacktestResponse(
        backtest_name=config.name,
        total_pnl=round(sum(r.pnl for r in results), 2),
        results=results,
    )
