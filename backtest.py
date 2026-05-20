import argparse
import asyncio
import json

from backtest import run_backtest_async
from config import BacktestSettings, init_backtest_logger


async def main(args) -> None:
    logger = init_backtest_logger(args.level)

    settings = BacktestSettings.build(args)

    # Look up the specified backtest in settings and raise an error if not present
    backtest_conf = None
    for bt in settings.backtests:
        if bt.name == args.name:
            backtest_conf = bt
            break
    if backtest_conf is None:
        raise ValueError(f"Backtest '{args.name}' not found in configuration")

    response = await run_backtest_async(backtest_conf, logger)

    # Print serialized response to stdout for consumption by caller.
    # Note this same response can be used in upcoming api implementation.
    print(json.dumps(response.model_dump(), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest runner for modular quant trading strategies",
    )
    BacktestSettings.set_args(parser)
    args = parser.parse_args()

    asyncio.run(main(args))
