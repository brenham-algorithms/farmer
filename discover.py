import argparse

from aggregators import build_aggregator
from config import DiscoverSettings, init_backtest_logger
from strategies import build_strategy


def main(args) -> None:
    logger = init_backtest_logger(args.level)

    settings = DiscoverSettings.build(args)

    # Look up the specified strategy in settings and raise an error if not present
    strategy_conf = None
    for q in settings.queries:
        if q.name == args.query:
            strategy_conf = q.strategy
            break
    if strategy_conf is None:
        raise ValueError(f"Query '{args.query}' not found in configuration")

    aggregator = build_aggregator(strategy_conf, logger)
    strategy = build_strategy(strategy_conf, logger, aggregator.get_candles())

    print(strategy)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Tunable support and resistance level finder"
    )
    DiscoverSettings.set_args(parser)
    args = parser.parse_args()

    main(args)
