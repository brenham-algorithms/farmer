import logging
from typing import Any, Dict, List

from api.models import StrategyConfig
from core import Strategy

from .ema_mean_reversion import EmaMeanReversion
from .ema_mean_reversion_confirmed import EmaMeanReversionConfirmed
from .opening_range_breakout import OpeningRangeBreakout
from .static_bounce import StaticBounce
from .static_level_bounce import StaticLevelBounce
from .static_level_bounce_confirmed_exit import StaticLevelBounceConfirmedExit
from .vwap_mean_reversion import VwapMeanReversion
from .vwap_mean_reversion_ladder import VwapMeanReversionLadder


def build_strategy(
    config: StrategyConfig, logger: logging.Logger, candles: List[Dict[str, Any]]
) -> Strategy:
    if config.strategy_params.kind == "static_bounce":
        return StaticBounce(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "mean_reversion_ema":
        return EmaMeanReversion(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "ema_mean_reversion_confirmed":
        return EmaMeanReversionConfirmed(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "vwap_mean_reversion":
        return VwapMeanReversion(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "vwap_mean_reversion_ladder":
        return VwapMeanReversionLadder(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "orb":
        return OpeningRangeBreakout(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "static_level_bounce":
        return StaticLevelBounce(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "static_level_bounce_confirmed_exit":
        return StaticLevelBounceConfirmedExit(logger, candles, config.strategy_params)
    else:
        raise ValueError(f"Unsupported strategy kind: {config.strategy_params.kind}")
