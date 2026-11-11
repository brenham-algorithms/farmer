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


class WickReversalParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["wick_reversal"] = "wick_reversal"
    precision: int = 2

    # Candle timeframe
    candle_minutes: int = 1

    # Wick detection
    min_wick_ticks: int = 80

    # Both-wicks filter; if shorter/longer >= this, skip
    wick_ratio_threshold: float = 0.75

    # Position sizing
    num_contracts: int = 2

    # Stop loss as percentage of wick length
    stop_wick_pct: float = 0.50

    # Take profit as percentage of wick length
    tp_wick_pct: float = 1.0
    tp_contracts: int = 1

    # Trailing stop as percentage of wick length after TP hit
    trail_wick_pct: float = 0.50

    # Daily limits; dollars; realized only
    daily_loss_limit: float = -200.0
    daily_tp_limit: float = 500.0

    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0

    # Volume filters
    min_candle_volume: Optional[int] = None
    max_candle_volume: Optional[int] = None

    # Time filters
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None
    trading_start_minute: int = 0  # e.g. 8:30 CT RTH open = hour 8, minute 30
    trading_end_minute: int = 0

    # Lookback for filtering on isolated highs and lows
    lookback_candles: Optional[int] = None


class EmaBounceParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["ema_bounce"] = "ema_bounce"
    precision: int = 2

    # EMA
    ema_period: int = 200
    candle_length: int = 1

    # Position sizing
    num_contracts: int = 1

    # Zone around EMA in ticks
    zone_ticks: int = 8

    # Risk and reward in ticks
    risk_ticks: int = 40
    reward_ticks: int = 40

    # Re-entry distance; ticks from EMA after trade completes
    re_entry_distance_ticks: int = 20

    # Confirmation; set use_confirmation=False to disable
    use_confirmation: bool = True
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

    # Trading hours
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None

    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0


class PriorDayHlBounceParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["prior_day_hl_bounce"] = "prior_day_hl_bounce"
    precision: int = 2

    # Levels
    support: bool = True
    resistance: bool = True

    # Zone around PDH/PDL (in ticks)
    zone_ticks: int = 8

    # Risk/reward (in ticks)
    risk_ticks: int = 40
    reward_ticks: int = 60

    # Position sizing
    num_contracts: int = 1

    # Confirmation
    use_confirmation: bool = True
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

    # Trading hours
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None

    # Session
    session_reset_hour: int = 17
    session_reset_minute: int = 0
    cooldown_seconds: int = 300


class AbsorptionScannerParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["absorption_scanner"] = "absorption_scanner"
    precision: int = 2

    # Rolling window
    window_seconds: int = 60
    bucket_ticks: int = 4

    # Detection thresholds
    min_absorption_ratio: float = 0.40
    min_window_volume: int = 200
    max_window_volume: Optional[int] = None
    proximity_ticks: int = 8

    # Position sizing
    num_contracts: int = 1

    # Risk/reward
    risk_ticks: int = 40
    reward_ticks: int = 400  # set high to rely on confirmed exit

    # Exit confirmation
    exit_attempt_seconds: int = 30
    exit_delta_ratio_threshold: float = 0.60
    exit_min_response_ticks: int = 2
    exit_min_attempt_volume: int = 200
    exit_absorption_ticks: int = 2

    # Direction filter
    direction: Optional[str] = None  # "LONG", "SHORT", or None for both

    cooldown_seconds: int = 300

    # Daily limits
    daily_loss_limit: float = -500.0
    daily_tp_limit: float = 500.0
    session_reset_hour: int = 17
    session_reset_minute: int = 0

    # Trading hours
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None
    trading_start_minute: int = 0  # e.g. 8:30 CT RTH open = hour 8, minute 30
    trading_end_minute: int = 0

    # Opposing-flow filter: only take signals that follow aggression against the trade.
    # Flow = net delta / volume over the last flow_lookback_minutes completed 1-minute bars,
    # signed in the trade's direction. A signal passes if flow <= -min_opposing_flow.
    flow_lookback_minutes: int = 15
    min_opposing_flow: Optional[float] = None  # e.g. 0.06; None disables the filter


class AbsorptionScannerWithDeltaParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["absorption_scanner_with_delta"] = "absorption_scanner_with_delta"
    precision: int = 2

    # Rolling window
    window_seconds: int = 60
    bucket_ticks: int = 4

    # Absorption detection thresholds
    min_absorption_ratio: float = 0.40
    min_window_volume: int = 200
    max_window_volume: Optional[int] = None
    proximity_ticks: int = 8

    # Delta confirmation (phase 2)
    delta_confirmation_seconds: int = 30
    delta_confirmation_threshold: float = 0.15
    delta_confirmation_min_volume: int = 100

    # Position sizing
    num_contracts: int = 1

    # Risk/reward
    risk_ticks: int = 40
    reward_ticks: int = 400

    # Exit confirmation
    exit_attempt_seconds: int = 30
    exit_delta_ratio_threshold: float = 0.60
    exit_min_response_ticks: int = 2
    exit_min_attempt_volume: int = 200
    exit_absorption_ticks: int = 2

    # Direction filter
    direction: Optional[str] = None  # "LONG", "SHORT", or None for both

    # Daily limits
    daily_loss_limit: float = -500.0
    daily_tp_limit: float = 500.0
    session_reset_hour: int = 17
    session_reset_minute: int = 0

    # Trading hours
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None

    cooldown_seconds: int = 300


class DeltaScannerParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["delta_scanner"] = "delta_scanner"
    precision: int = 2

    # Rolling window
    window_seconds: int = 60

    # Detection thresholds
    delta_ratio_threshold: float = 0.60
    min_window_volume: int = 200
    max_window_volume: Optional[int] = None

    # Position sizing
    num_contracts: int = 1

    # Risk/reward
    risk_ticks: int = 40
    reward_ticks: int = 400

    # Exit confirmation
    exit_attempt_seconds: int = 30
    exit_delta_ratio_threshold: float = 0.60
    exit_min_response_ticks: int = 2
    exit_min_attempt_volume: int = 200
    exit_absorption_ticks: int = 2

    # Direction filter
    direction: Optional[str] = None  # "LONG", "SHORT", or None for both

    cooldown_seconds: int = 300

    # Daily limits
    daily_loss_limit: float = -500.0
    daily_tp_limit: float = 500.0
    session_reset_hour: int = 17
    session_reset_minute: int = 0

    # Trading hours
    trading_start_hour: Optional[int] = None
    trading_end_hour: Optional[int] = None


class ExhaustionFadeParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["exhaustion_fade"] = "exhaustion_fade"
    precision: int = 2

    # Signal bar construction
    lookback_bars: int = 30  # Bar must take out the high/low of the prior N bars
    atr_period: int = 30
    volume_avg_period: int = 30

    # Exhaustion filters (evaluated on the completed 1-minute bar)
    min_volume_multiple: float = 1.5  # Bar volume vs rolling average
    min_delta_ratio: float = 0.0  # Aggressors must push into the extreme...
    max_delta_ratio: float = (
        0.15  # ...but not dominate it (strong initiative continues)
    )
    max_close_position: float = (
        0.40  # Close within this fraction of the bar from the far end
    )
    direction: Optional[Literal["LONG", "SHORT"]] = None  # None trades both sides

    # Position sizing
    num_contracts: int = 1
    use_brackets: bool = False

    # Risk/reward in ATR multiples
    stop_atr_multiple: float = 3.0
    target_atr_multiple: float = 2.5

    # Trading windows (America/Chicago)
    entry_start_hour: int = 8
    entry_start_minute: int = 45
    entry_end_hour: int = 14
    entry_end_minute: int = 0
    flat_hour: int = 14
    flat_minute: int = 59

    cooldown_minutes: int = 15

    # Optional session guards
    max_trades_per_session: Optional[int] = None
    daily_loss_limit: Optional[float] = None
    session_reset_hour: int = 17
    session_reset_minute: int = 0


class GapFillParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["gap_fill"] = "gap_fill"
    precision: int = 2

    # Gap definition: the entry-time price vs the prior RTH session's last trade before
    # rth_end. Gap size is measured against the prior session's RTH range (high - low).
    min_gap_range_ratio: float = 0.5  # |gap| / prior RTH range must be at least this
    max_gap_range_ratio: Optional[float] = None
    min_gap_points: Optional[float] = None
    direction: Optional[Literal["LONG", "SHORT"]] = None  # None trades both sides

    # Exits as multiples of the gap, measured from the entry price
    fill_fraction: float = 1.0  # Target: 1.0 = the prior close, 0.5 = half the gap
    stop_gap_multiple: float = 1.0  # Stop distance beyond the entry, against the fade
    max_stop_points: Optional[float] = None  # Skip the trade if the stop would be wider

    # Position sizing
    num_contracts: int = 1
    use_brackets: bool = False

    # Sessions and windows (America/Chicago)
    rth_start_hour: int = 8
    rth_start_minute: int = 30
    rth_end_hour: int = 15
    rth_end_minute: int = 0
    entry_hour: int = 8
    entry_minute: int = 30
    max_entry_delay_minutes: int = (
        5  # Don't enter if the first tick arrives later than this
    )
    flat_hour: int = 14
    flat_minute: int = 59

    # Data hygiene
    skip_dates: List[str] = []  # YYYYMMDD, e.g. continuous-contract roll days
    min_prior_session_minutes: int = 300  # Skip after half days / partial data
    max_prior_session_age_days: int = (
        5  # Prior session must be this recent (calendar days)
    )


class NoiseAreaParams(BaseModel):
    tick_size: float
    tick_value: float
    kind: Literal["noise_area"] = "noise_area"
    precision: int = 2

    # Noise area (Zarattini, Aziz & Barbon 2024): for each minute of the session,
    # sigma = mean over the last lookback_days sessions of |price / open - 1| at that minute.
    # Upper = max(open, prior close) * (1 + sigma); lower = min(open, prior close) * (1 - sigma).
    lookback_days: int = 14
    band_multiplier: float = 1.0  # Scales sigma; the paper uses 1.0

    # Decisions are only made at checkpoints, every check_interval_minutes from first_check
    # through last_check (CT). Enter when price is outside the noise area; exit a long
    # below max(upper, VWAP) and a short above min(lower, VWAP). An exit can flip.
    check_interval_minutes: int = 30
    first_check_hour: int = 9
    first_check_minute: int = 0
    last_check_hour: int = 14
    last_check_minute: int = 30
    use_vwap_stop: bool = True
    direction: Optional[Literal["LONG", "SHORT"]] = None  # None trades both sides

    # Optional hard stop, checked on every tick (not part of the paper's rules)
    stop_loss_points: Optional[float] = None

    # Position sizing
    num_contracts: int = 1

    # Sessions (America/Chicago)
    rth_start_hour: int = 8
    rth_start_minute: int = 30
    rth_end_hour: int = 15
    rth_end_minute: int = 0
    flat_hour: int = 14
    flat_minute: int = 59

    # Data hygiene
    skip_dates: List[str] = []  # YYYYMMDD, e.g. continuous-contract roll days
    min_session_minutes: int = (
        300  # Shorter sessions (half days) are left out of the history
    )
    max_open_delay_minutes: int = (
        5  # The session's first trade must be this close to the open
    )
    max_prior_session_age_days: int = 5


class DayTypeMonitorParams(BaseModel):
    kind: Literal["day_type_monitor"] = "day_type_monitor"
    precision: int = 2

    # Never trades: labels the RTH session so far and logs label changes, snapshots and a
    # daily summary. The labels describe the day so far; they are not predictions.
    #   trend up/down: trendness (|last - open| / range) >= trend_min_trendness
    #   range: trendness < range_max_trendness with few VWAP crosses
    #   chop: trendness < range_max_trendness and crosses_per_hour >= chop_crosses_per_hour
    #   mixed: in between
    min_minutes: int = 30  # "too early" before this
    trend_min_trendness: float = 0.6
    range_max_trendness: float = 0.35
    chop_crosses_per_hour: float = 4.0
    confirm_minutes: int = 5  # A new label must hold this long before it's logged
    # To leave its current label, trendness must clear a threshold by this much
    # (crosses_per_hour by 10x this), so values near a threshold don't flip the label
    hysteresis: float = 0.05
    snapshot_minutes: int = 30  # Log the state this often; 0 disables

    # range_vs_normal compares today's range with the average over this many full sessions
    lookback_days: int = 20
    min_session_minutes: int = 300  # Shorter sessions (half days) are left out

    # Sessions (America/Chicago)
    rth_start_hour: int = 8
    rth_start_minute: int = 30
    rth_end_hour: int = 15
    rth_end_minute: int = 0


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
    WickReversalParams,
    EmaBounceParams,
    PriorDayHlBounceParams,
    AbsorptionScannerParams,
    AbsorptionScannerWithDeltaParams,
    DeltaScannerParams,
    ExhaustionFadeParams,
    GapFillParams,
    NoiseAreaParams,
    DayTypeMonitorParams,
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


class RedisDataSource(BaseModel):
    kind: Literal["redis"] = "redis"
    redis_host: str = "localhost"
    redis_port: int = 6379
    contract_id: str


DataSource = Union[CsvDataSource, ProjectXDataSource, RedisDataSource]


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


class ProjectXOrderManagerParams(BaseModel):
    enabled: bool = False
    kind: Literal["projectx"] = "projectx"
    base_url: str
    username: str
    api_key: str
    account_id: int
    contract_id: str
    redis_host: str = "localhost"
    redis_port: int = 6379


OrderManagerParams = Union[ProjectXOrderManagerParams]


class StrategyConfig(BaseModel):
    ticker_params: Optional[TickerParams] = None
    aggregation_params: Optional[AggregationParams] = None
    order_manager_params: Optional[OrderManagerParams] = None
    persist_across_files: bool = False
    # Backtests only: close any position still open when a day's file ends
    flatten_at_end_of_file: bool = False
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
