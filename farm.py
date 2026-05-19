import argparse

from config import FarmSettings, init_backtest_logger
from farm import Farmer


def main(args):
    logger = init_backtest_logger()

    settings = FarmSettings.build(args)

    # Look up the specified farmer in settings and raise an error if not present
    strategy_conf = None
    for farmer in settings.farmers:
        if farmer.name == args.name:
            strategy_conf = farmer.strategy
            break
    if strategy_conf is None:
        raise ValueError(f"Farmer '{args.name}' not found in configuration")

    farmer = Farmer(strategy_conf, logger)

    farmer.start()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Modular quant trading bot",
    )
    FarmSettings.set_args(parser)
    args = parser.parse_args()

    main(args)
