import argparse
import asyncio
import json

from backtest import run_backtest_async
from config import BacktestSettings, init_backtest_logger


async def main(args) -> None:
    logger = init_backtest_logger(args.level)

    settings = BacktestSettings.build(args)

    if args.name == "all":
        # Run all configured backtests in settings if specified
        configs = settings.backtests
    else:
        # Otherwise look up the specified backtest in settings and raise an error if not present
        configs = [bt for bt in settings.backtests if bt.name == args.name]
        if not configs:
            raise ValueError(f"Backtest '{args.name}' not found in configuration")

    for backtest_conf in configs:
        response = await run_backtest_async(backtest_conf, logger)
        print(json.dumps(response.model_dump(), indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Backtest runner for modular quant trading strategies",
    )
    BacktestSettings.set_args(parser)
    args = parser.parse_args()

    asyncio.run(main(args))
