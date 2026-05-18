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


class StaticBounceWithDeltaParams(BaseModel):
    tick_size: float
    tick_value: float
    proximity_threshold: int
    reward_ticks: int
    risk_ticks: int
    tick_tolerance: int
    kind: Literal["static_bounce_with_delta"] = "static_bounce_with_delta"
    min_separation: int = 10
    top_n: int = 10
    decay_half_life_days: float = 15.0
    precision: int = 2
    delta_window_seconds: float = 300.0
    attempt_seconds: int = 30
    delta_ratio_threshold: float = 0.20
    min_response_ticks: int = 3
    max_penetration_ticks: int = 4
    cooldown_seconds: int = 120


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
    entry_std_1: float = 2.0  # 1 contract
    entry_std_2: float = 2.5  # +1 contract (total 2)
    entry_std_3: float = 3.0  # +2 contracts (total 4)
    max_std_dev: float = 4.0  # skip entries beyond this
    min_std_dev_value: Optional[float] = None
 
    # TP ladder bands (price must cross INSIDE these bands toward VWAP)
    # 1 contract closes at VWAP (no param needed)
    tp_std_4: float = 2.0  # cut 2 at this band (when holding 4)
    tp_std_2: float = 1.0  # cut 1 at this band (when holding 2)
 
    # Risk
    risk_ticks: int = 80  # hard stop from first entry, all contracts
 
    # Session filter
    min_session_volume: int = 1000
 
    # Cooldown filter
    cooldown_seconds: int = 300

    # Volatility filters
    candle_length: int = 5  # minutes per candle (must match aggregation_params)
    atr_period: int = 14
    max_atr: Optional[float] = None


class OpeningRangeBreakoutParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["opening_range_breakout"] = "opening_range_breakout"
    precision: int = 2
    num_contracts: int = 2
 
    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0
 
    # Opening range window
    or_start_hour: int = 8
    or_start_minute: int = 30
    or_duration_minutes: int = 15
 
    # Range size filter (in ticks)
    min_range_ticks: int = 12
    max_range_ticks: int = 80
 
    # TP multiplier (1.0 = 1x range size from entry)
    tp_range_multiplier: float = 1.0
 
    # Time exit
    exit_hour: int = 15
    exit_minute: int = 0
 
    # Confirmation (set attempt_seconds=0 to disable)
    attempt_seconds: int = 30
    delta_ratio_threshold: float = 0.15
    min_response_ticks: int = 2
    min_attempt_volume: int = 50
    min_absorbed_volume: int = 0
    absorption_ticks: int = 2
    cooldown_seconds: int = 300


StrategyParams = Union[
    StaticBounceParams,
    StaticBounceWithDeltaParams,
    EmaMeanReversionParams,
    VwapMeanReversionParams,
    VwapMeanReversionLadderParams,
    OpeningRangeBreakoutParams,
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
    start_time: Optional[datetime] = None
    end_time: Optional[datetime] = None


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
