import logging
from typing import Any, Dict, List

from api.models import StrategyConfig
from core import Strategy

from .ema_mean_reversion import EmaMeanReversion
from .static_bounce import StaticBounce
from .vwap_mean_reversion import VwapMeanReversion
from .vwap_mean_reversion_ladder import VwapMeanReversionLadder


def build_strategy(
    config: StrategyConfig, logger: logging.Logger, candles: List[Dict[str, Any]]
) -> Strategy:
    if config.strategy_params.kind == "static_bounce":
        return StaticBounce(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "mean_reversion_ema":
        return EmaMeanReversion(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "vwap_mean_reversion":
        return VwapMeanReversion(logger, candles, config.strategy_params)
    elif config.strategy_params.kind == "vwap_mean_reversion_ladder":
        return VwapMeanReversionLadder(logger, candles, config.strategy_params)
    else:
        raise ValueError(f"Unsupported strategy kind: {config.strategy_params.kind}")
