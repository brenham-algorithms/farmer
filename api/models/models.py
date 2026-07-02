from datetime import datetime, timedelta
from typing import List, Literal, Optional, Union

from pydantic import BaseModel


class StaticBounceParams(BaseModel):
    tick_size: float
    tick_value: float
    proximity_threshold: int
    reward_ticks: int
    risk_ticks: int
    tick_tolerance: int
    kind: Literal["static_bounce"] = "static_bounce"
    min_separation: int = 10
    top_n: int = 10
    decay_half_life_days: float = 15.0
    precision: int = 2


class EmaMeanReversionParams(BaseModel):
    tick_size: float
    tick_value: float
    entry_distance_ticks: int  # min ticks from EMA to trigger entry
    risk_ticks: int  # stop loss distance from entry in ticks
    kind: Literal["mean_reversion_ema"] = "mean_reversion_ema"
    precision: int = 2
    ema_period: int = 20  # EMA lookback in candles
    atr_period: int = 14  # ATR lookback in candles (14 is the standard)
    candle_length: int = 5  # minutes per candle (must match aggregation_params)
    reward_ticks: int = 0  # only used when target_ema is False
    target_ema: bool = True  # TP at the EMA level itself
    cooldown_seconds: int = 300  # seconds between trades
    max_distance_ticks: Optional[int] = (
        None  # skip entries if price is too far (knife-catcher guard)
    )
    max_atr: Optional[float] = None  # skip entries when ATR exceeds this value


class VwapMeanReversionParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["vwap_mean_reversion"] = "vwap_mean_reversion"
    precision: int = 2
    session_reset_hour: int = 17
    session_reset_minute: int = 0
    entry_std_dev: float = 2.0
    max_std_dev: float = 4.0
    min_std_dev: Optional[float] = None
    risk_ticks: int = 40
    min_session_volume: int = 1000
    attempt_seconds: int = 30
    delta_ratio_threshold: float = 0.15
    min_response_ticks: int = 2
    cooldown_seconds: int = 300
    min_attempt_volume: int = 0
    min_absorbed_volume: int = 0
    absorption_ticks: int = 2


class VwapMeanReversionLadderParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["vwap_mean_reversion_ladder"] = "vwap_mean_reversion_ladder"
    precision: int = 2

    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0

    # Entry ladder bands (in standard deviations from VWAP)
    entry_std_1: float = 2.0
    size_std_1: int = 1
    entry_std_2: float = 2.5
    size_std_2: int = 1
    entry_std_3: float = 3.0
    size_std_3: int = 2
    max_std_dev: float = 4.0
    min_std_dev_value: Optional[float] = None

    # TP ladder bands (price must cross INSIDE these bands toward VWAP)
    # After hitting tp_std_2, any contracts that remain close at VWAP
    tp_std_3: float = 2.0  # cut size_std_3 at this band
    tp_std_2: float = 1.0  # cut size_std_2 at this band

    # Risk
    risk_ticks: int = 80  # hard stop from first entry, all contracts

    # Session filter
    min_session_volume: int = 1000

    # Cooldown filter
    cooldown_seconds: int = 300

    # Seeding behavior for vwap
    seed_vwap: bool = False


class OrbParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["orb"] = "orb"
    precision: int = 2

    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0

    # Opening range window
    or_start_hour: int = 8
    or_start_minute: int = 30
    or_duration_minutes: int = 15

    # Range filters (in ticks)
    min_range_ticks: int = 12
    max_range_ticks: int = 80

    # Breakout: how far past OR boundary price must go
    breakout_ticks: int = 4

    # Reversion: how close to OR boundary price must return
    reversion_ticks: int = 2

    # Max penetration: how far inside the range before attempt is cancelled
    max_penetration_ticks: int = 4

    # Position sizing
    num_contracts: int = 2
    tp_contracts: int = 1  # how many to close at TP; remainder = runners

    # Risk/reward as multipliers of OR range size
    tp_range_multiplier: float = 1.0
    risk_range_multiplier: float = 1.0

    # Runner trailing stop (in ticks)
    trail_ticks: int = 20

    # Time exit
    exit_hour: int = 15
    exit_minute: int = 0

    # Confirmation
    attempt_seconds: int = 30
    delta_ratio_threshold: float = 0.15
    min_response_ticks: int = 2
    min_attempt_volume: int = 50
    min_absorbed_volume: int = 0
    absorption_ticks: int = 2
    cooldown_seconds: int = 300


class StaticLevelBounceParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["static_level_bounce"] = "static_level_bounce"
    precision: int = 2

    # The level
    level: float
    support: bool = True
    resistance: bool = True

    # Zone definition (ticks above and below the level)
    ticks_above: int = 4
    ticks_below: int = 4

    # Risk/reward (in ticks)
    reward_ticks: int = 20
    risk_ticks: int = 10

    # Position sizing
    num_contracts: int = 1

    # Confirmation
    attempt_seconds: int = 30
    delta_ratio_threshold: float = 0.15
    min_response_ticks: int = 2
    min_attempt_volume: int = 50
    min_absorption_ratio: float = (
        0.0  # 0 = disabled; e.g. 0.20 = 20% of volume must be absorbed
    )
    absorption_ticks: int = 2
    cooldown_seconds: int = 300


class StaticLevelBounceConfirmedExitParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["static_level_bounce_confirmed_exit"] = (
        "static_level_bounce_confirmed_exit"
    )
    precision: int = 2

    # The level
    level: float
    support: bool = True
    resistance: bool = True

    # Zone definition
    ticks_above: int = 4
    ticks_below: int = 4

    # Hard stop safety net (in ticks from entry)
    risk_ticks: int = 40

    # Position sizing
    num_contracts: int = 1

    # Entry confirmation
    entry_attempt_seconds: int = 30
    entry_delta_ratio_threshold: float = 0.15
    entry_min_response_ticks: int = 2
    entry_min_attempt_volume: int = 50
    entry_min_absorption_ratio: float = 0.0
    entry_absorption_ticks: int = 2

    # Exit confirmation
    exit_attempt_seconds: int = 30
    exit_delta_ratio_threshold: float = 0.15
    exit_min_response_ticks: int = 2
    exit_min_attempt_volume: int = 50
    exit_min_absorption_ratio: float = 0.0
    exit_absorption_ticks: int = 2

    cooldown_seconds: int = 300


class EmaMeanReversionConfirmedParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["ema_mean_reversion_confirmed"] = "ema_mean_reversion_confirmed"
    precision: int = 4

    # EMA
    ema_period: int = 20
    candle_length: int = 5

    # ATR; optional volatility filter
    atr_period: int = 56
    max_atr: Optional[float] = None

    # Entry distance
    entry_distance_ticks: int = 50
    max_distance_ticks: Optional[int] = None

    # Hard stop safety net
    risk_ticks: int = 500

    # Entry confirmation
    entry_attempt_seconds: int = 30
    entry_delta_ratio_threshold: float = -0.15
    entry_min_response_ticks: int = 4
    entry_min_attempt_volume: int = 50
    entry_min_absorption_ratio: float = 0.40
    entry_absorption_ticks: int = 4

    cooldown_seconds: int = 300


class VwapCascadeReversalParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["vwap_cascade_reversal"] = "vwap_cascade_reversal"
    precision: int = 2

    # VWAP zone
    zone_ticks: int = 8  # how close to VWAP = "at VWAP"
    scan_ticks: int = 80  # how far above/below VWAP to scan for trapped delta
    bucket_ticks: int = 4  # delta profile bucket size

    # Trapped detection
    min_trapped_delta: int = 200  # minimum net delta to consider traders trapped

    # Cascade detection
    cascade_volume_spike: int = 500  # volume on the continuation move
    cascade_delta_ratio: float = 0.30  # how directional the cascade must be

    # Position
    num_contracts: int = 1
    reward_ticks: int = 40
    risk_ticks: int = 20

    # Exit confirmation
    exit_attempt_seconds: int = 30
    exit_delta_ratio_threshold: float = 0.60
    exit_min_response_ticks: int = 2
    exit_min_attempt_volume: int = 200
    exit_min_absorption_ratio: float = 0.0
    exit_absorption_ticks: int = 2

    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0
    seed_from_candles: bool = False

    cooldown_seconds: int = 300

    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None


class VwapDiagnosticParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["vwap_diagnostic"] = "vwap_diagnostic"
    precision: int = 2
    session_reset_hour: int = 17
    session_reset_minute: int = 0
    seed_from_candles: bool = False


class AbsorptionBounceParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["absorption_bounce"] = "absorption_bounce"
    precision: int = 2

    # Level definition
    level: float
    support: bool = True
    resistance: bool = True

    # Zone definition
    ticks_above: int = 8
    ticks_below: int = 8

    # Risk/reward
    risk_ticks: int = 40
    reward_ticks: int = 400  # set high to effectively disable
    num_contracts: int = 1

    # Entry confirmation
    entry_attempt_seconds: int = 30
    entry_delta_ratio_threshold: float = -0.15
    entry_min_response_ticks: int = 2
    entry_min_attempt_volume: int = 200
    entry_min_absorption_ratio: float = 0.40

    # Exit confirmation
    exit_attempt_seconds: int = 30
    exit_delta_ratio_threshold: float = 0.60
    exit_min_response_ticks: int = 2
    exit_min_attempt_volume: int = 200
    exit_absorption_ticks: int = 2

    # Standard cooldown filter
    cooldown_seconds: int = 300

    # Optional parameters
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None


StrategyParams = Union[
    StaticBounceParams,
    EmaMeanReversionParams,
    VwapMeanReversionParams,
    VwapMeanReversionLadderParams,
    OrbParams,
    StaticLevelBounceParams,
    StaticLevelBounceConfirmedExitParams,
    EmaMeanReversionConfirmedParams,
    VwapCascadeReversalParams,
    VwapDiagnosticParams,
    AbsorptionBounceParams,
]


class CsvDataSource(BaseModel):
    kind: Literal["csv"] = "csv"
    data_dir: str


class ProjectXDataSource(BaseModel):
    kind: Literal["projectx"] = "projectx"
    base_url: str
    market_hub_base_url: str
    username: str
    api_key: str
    contract_id: str


DataSource = Union[CsvDataSource, ProjectXDataSource]


class TickerParams(BaseModel):
    data_source: DataSource
    symbols: List[str]
    start_symbol: str
    pct_margin: float
    abs_margin: int
    min_total_volume: int
    throttle: float = 0.0


class AggregationParams(BaseModel):
    data_source: DataSource
    lookback_days: int
    candle_length: int = 5
    unit: str = "minutes"


class StrategyConfig(BaseModel):
    ticker_params: Optional[TickerParams] = None
    aggregation_params: Optional[AggregationParams] = None
    strategy_params: StrategyParams


class FarmerConfig(BaseModel):
    name: str
    strategy: StrategyConfig


class QueryConfig(BaseModel):
    name: str
    strategy: StrategyConfig


class BacktestConfig(BaseModel):
    name: str
    dates: Optional[List[str]] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    exclude_dates: Optional[List[str]] = None
    strategy: StrategyConfig

    def get_dates(self) -> List[str]:
        if self.dates:
            return self.dates

        if self.start_date and self.end_date:
            start = datetime.strptime(self.start_date, "%Y%m%d").date()
            end = datetime.strptime(self.end_date, "%Y%m%d").date()

            result = []
            d = start
            while d <= end:
                if (
                    d.weekday() != 5
                ):  # Skip Saturdays because futures markets are closed
                    result.append(d.strftime("%Y%m%d"))
                d += timedelta(days=1)
        else:
            raise ValueError(
                "BacktestConfig requires either 'dates' or both 'start_date' and 'end_date'"
            )

        if self.exclude_dates:
            exclude_set = set(self.exclude_dates)
            result = [d for d in result if d not in exclude_set]

        return result


class BacktestResult(BaseModel):
    pnl: float
    trades_file: str


class BacktestResponse(BaseModel):
    backtest_name: str
    total_pnl: float
    results: List[BacktestResult]
