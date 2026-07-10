# Farmer

A modular, event-driven trading framework for developing, testing, and deploying systematic futures trading strategies against tick-level market data.

Strategies are written once and run identically in backtesting and live environments. The framework processes tick data through configurable strategy modules that analyze orderflow (delta, absorption, volume), react to price levels, and emit structured trade signals.

## Architecture

```
Tick Data → Engine → Strategy → Signal → Handler → Position Management
```

**Tick sources:** CSV files (backtesting), ProjectX/TopstepX SignalR WebSocket (live), Redis subscriber (planned).

**Strategies** encapsulate trading logic behind a common protocol. Each strategy implements `check()` to evaluate ticks and emit entry signals, and exposes `get_backtest_handler()` / `get_live_handler()` to provide the appropriate position management logic. Strategies are configured entirely via YAML with no code changes required to swap between them.

**Handlers** own the position lifecycle — entry, exit (static TP/SL, confirmed exits, trailing stops), and PnL tracking. Handlers are co-located with their strategy for self-contained modules.

## Strategies

**StaticLevelBounce** — Monitors a configured price level. When price enters the zone, persistent attempts run until delta/absorption confirmation fires or price leaves. Static TP/SL exits.

**StaticLevelBounceConfirmedExit** — Same entry as StaticLevelBounce, but exits via persistent opposite-direction attempts that detect when the trade is reversing. Hard stop as safety net.

**AbsorptionBounce** — Enters when zone-wide absorption confirms a passive defender at a level. Tracks absorption across the full zone width within each attempt window rather than at a narrow price point. Three exit paths: static TP, static SL, and confirmed exit.

**VwapMeanReversionLadder** — Tiered entries at VWAP standard deviation bands with LIFO profit-taking. Configurable sizes per band level.

**OpeningRangeBreakout** — Defines opening range from the first N minutes of the cash session. Waits for breakout, reversion to the OR boundary, and delta confirmation before entering. Partial TP with trailing stop on the runner.

**EmaMeanReversionConfirmed** — Enters when price is extended from the EMA and absorption/delta confirmation fires. Dynamic exit at the EMA.

**VwapCascadeReversal** — Detects stop cascade capitulation across VWAP and enters expecting a reversal after forced liquidation exhausts.

## Calculations

Reusable building blocks that strategies compose:

- **LiveVwap / CandleVwap** — Session-scoped VWAP with standard deviation bands. Tick-level and candle-level variants.
- **LiveEma / LiveAtr** — EMA and ATR seeded from historical candles, updated live.
- **LiveOpeningRange** — Tracks high/low during a configurable opening window.
- **DeltaProfile** — Cumulative delta per price bucket across a session. Detects trapped traders.
- **AbsorptionProfile** — Tracks where aggressive volume failed to move price. Finds passive defenders.
- **BandAttempt** — Reusable confirmation pattern tracking delta ratio, volume, absorption, and price response within a time window.

## Entrypoints

**backtest.py** — Run strategies against historical CSV tick data.

```bash
python backtest.py --config config.yaml --name mes_level_bounce_7500
```

**farm.py** — Run strategies live against the ProjectX market hub.

```bash
python farm.py --name mnq_absorption_bounce --level debug --strategy.strategy_params.level 29800
```

**discover.py** — Identify statistically significant support and resistance levels from historical data.

```bash
python discover.py --query mes_static_levels --strategy.strategy_params.top_n 5
```

All entrypoints support dot-notation CLI overrides for rapid parameter tuning without editing YAML.

## Configuration

Strategies are fully configured via YAML. YAML anchors keep shared parameters DRY.

```yaml
backtests:
  - name: "mnq_absorption_bounce"
    dates:
      - "20260623"
    strategy:
      ticker_params:
        data_source:
          kind: "csv"
          data_dir: "mnq_historical"
        symbols: ["MNQU6"]
        start_symbol: "MNQU6"
        pct_margin: 0.05
        abs_margin: 5000
        min_total_volume: 20000
      strategy_params:
        kind: "absorption_bounce"
        tick_size: 0.25
        tick_value: 0.50
        level: 29800.00
        support: true
        resistance: true
        ticks_above: 20
        ticks_below: 20
        risk_ticks: 100
        reward_ticks: 400
        entry_attempt_seconds: 30
        entry_delta_ratio_threshold: -0.15
        entry_min_attempt_volume: 200
        entry_min_absorption_ratio: 0.40
```

## Setup

```bash
python -m venv venv
source venv/bin/activate
pip install setuptools
pip install -e .
```

Requires Python 3.12+.

## Related Repos

- [projectx-python](https://github.com/brenham-algorithms/projectx-python) — shared ProjectX/TopstepX API client
- [distributor](https://github.com/brenham-algorithms/distributor) — market data feed distributor (Redis fan-out for concurrent strategy execution)

## Disclaimer

This is an experimental trading framework and not financial advice. Use at your own risk.
