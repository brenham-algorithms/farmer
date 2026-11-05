# Wick Reversal (`wick_reversal`)

Strategy module: `strategies/wick_reversal.py` · Params model: `WickReversalParams` in `api/models/models.py`

Findings recorded 2026-11-04 from backtests on MNQ 2025 and 2026 data. The short version:
**the original configuration loses about $25k/year on RTH after costs, and the tuned
configuration is better but has no statistically demonstrated edge.** Don't trade either with
real money without forward (paper) testing first.

---

## 1. The idea

A long wick on a short-term candle means price pushed somewhere and was rejected. The strategy
bets that the rejection holds and trades in the opposite direction of the wick:

- A long **lower** wick (sellers pushed down and got rejected) → **LONG**
- A long **upper** wick (buyers pushed up and got rejected) → **SHORT**

## 2. How it works

### Candles

- The strategy builds its own candles from ticks, `candle_minutes` long and aligned to the
  Chicago-time clock (5-minute candles start at :00, :05, ...).
- The first candle of each session is partial, so it is discarded.
- The session resets at `session_reset_hour:session_reset_minute` CT, 17:00 by default. A reset
  clears the candle history and the daily P&L.

### Signal (evaluated when a candle completes)

1. **Volume filter (optional):** the candle's volume must be within
   `[min_candle_volume, max_candle_volume]`.
2. **Wick size:** the upper wick is `high - max(open, close)` and the lower wick is
   `min(open, close) - low`. A wick qualifies if it is at least `min_wick_ticks`.
3. **Both wicks qualify:** if `shorter / longer >= wick_ratio_threshold` the candle is treated as
   indecision and skipped. Otherwise the longer wick is traded.
4. **Lookback filter (optional):** with `lookback_candles = N`, a LONG requires the candle's low to
   be at or below the lowest low of the previous N candles. A SHORT requires a high at or above the
   highest high. In other words, the wick must be a fresh short-term extreme.

### Entry

- The position opens on the first tick of the next candle, at that tick's price, with
  `num_contracts`.
- The entry is skipped if:
  - a position is already open (the signal is logged as skipped),
  - the entry time is outside the trading window, or
  - the session's realized P&L has reached `daily_loss_limit` or `daily_tp_limit`.
- The trading window is `[trading_start_hour:trading_start_minute, trading_end_hour:trading_end_minute)`
  in CT. The minute fields were added 2026-11-04 so the window can start at the 08:30 RTH open.
  They default to 0, so hour-only configs behave as before.

### Exits

All exit distances are fractions of the signal wick's length, measured from the entry price:

| Exit | Rule |
|---|---|
| Stop loss | `stop_wick_pct x wick` against the position. Closes everything. Checked before the target on each tick. |
| Take profit | `tp_wick_pct x wick` in favour. Closes `tp_contracts`. If that is all the contracts, the trade ends there. |
| Runner trail | After the partial target, the remaining contracts trail at `trail_wick_pct x wick` behind price, ratcheting only in the trade's favour. |

- **No time exit.** A position can stay open past the end of the trading window, until a stop or
  target is hit. In backtests, set `flatten_at_end_of_file: true` on the strategy config so
  positions still open when a day's data ends are closed and counted. Without it they are silently
  dropped.
- **Daily limits** use *realized gross* dollars (costs are not included), including partial target
  fills. They block new entries but never close an open trade.

### Live trading caveats (not yet verified against a live account)

- The handler always creates positions with `use_brackets=True`. The entry order carries a stop
  bracket and a target bracket for the **full** position size.
- With brackets, `Position.cut()` and `Position.close()` deliberately send no orders. So live:
  - the partial target and the trailing runner **are not executed**; the broker's brackets
    decide the exit for both contracts;
  - the runner's trailing stop is never sent to the broker.
- Live results can therefore differ substantially from backtests whenever `tp_contracts <
  num_contracts`.
- There is no time exit live either, so positions can be held past 15:00 CT and overnight.

## 3. Parameters

| Parameter | Default | Original config | Tuned config | Meaning |
|---|---|---|---|---|
| `tick_size` / `tick_value` | required | 0.25 / 0.50 | same | Instrument (MNQ: $0.50 per tick, $2 per point) |
| `precision` | 2 | 2 | 2 | Price rounding |
| `candle_minutes` | 1 | 1 | 1 | Candle length in minutes |
| `min_wick_ticks` | 80 | 40 | **120** | Minimum wick length to qualify |
| `wick_ratio_threshold` | 0.75 | 0.75 | 0.75 | Skip if both wicks qualify and shorter/longer is at least this |
| `lookback_candles` | None | None | **30** | Wick must be an extreme of the previous N candles |
| `min_candle_volume` | None | 2000 | **None** | Minimum candle volume |
| `max_candle_volume` | None | 7000 | **None** | Maximum candle volume |
| `num_contracts` | 2 | 2 | 2 | Contracts per entry |
| `stop_wick_pct` | 0.50 | 1.0 | 1.0 | Stop distance as a multiple of the wick |
| `tp_wick_pct` | 1.0 | 1.0 | **1.5** | Target distance as a multiple of the wick |
| `tp_contracts` | 1 | 1 | 1 | Contracts closed at the target; the rest run |
| `trail_wick_pct` | 0.50 | 0.2 | **1.0** | Runner trailing distance as a multiple of the wick |
| `daily_loss_limit` | -200.0 | -600.0 | -600.0 | Stop new entries when realized session P&L is at or below this |
| `daily_tp_limit` | 500.0 | 800.0 | 800.0 | Stop new entries when realized session P&L is at or above this |
| `trading_start_hour` / `_minute` | None / 0 | 8 / 30 | 8 / 30 | Entry window start (CT); was 11:00 before 2026-11-04 |
| `trading_end_hour` / `_minute` | None / 0 | 15 / 0 | 15 / 0 | Entry window end (CT, exclusive) |
| `session_reset_hour` / `_minute` | 17 / 0 | 17 / 0 | 17 / 0 | Session boundary (CT) |

## 4. Test setup

- **Data:** Databento GLBX MDP3 trades for the continuous front month `MNQ.v.0`, 07:30-15:00 CT
  only (`download_databento.py`). May-Aug 2026 was converted from the full-day files with
  `convert_to_rth_continuous.py`; trade-for-trade, it gives identical results to the originals.
  - `mnq_2025_historical/`: 2025-01-02 to 2025-12-31, 258 files
  - `mnq_2026_historical/`: 2026-01-02 to 2026-11-03, 218 files
- **Costs:** the framework reports gross P&L. All "net" figures below subtract $1.80 per contract
  round trip (about $1.24 commission and fees plus 1 tick of slippage), so $3.60 per 2-contract trade.
- **End of day:** `flatten_at_end_of_file: true` for every run.
- **In-sample vs out-of-sample:**
  - The original config was chosen on 2026-06-18 to 2026-06-29, so that window is in-sample.
  - The tuned config was chosen on 2025 only.
  - 2026 excluding Jun 18-29 is the out-of-sample (holdout) test for both. It is not perfectly
    clean: other strategies had already been evaluated on 2026, and the tuned filter (a wick at a
    fresh extreme) resembles the `exhaustion_fade` idea developed on May-Aug 2026.
- **t-stat:** the average net per trade divided by its standard error. Below about 1 is noise;
  about 2 is a meaningful result for a single pre-planned test. Treat values that came out of a
  parameter search with suspicion.

## 5. Results: original configuration (RTH 08:30-15:00)

| Period | Trades | Trades/day | Gross | Net | Avg net | Win rate | PF | t | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| Jun 18-29, 2026 (in-sample) | 509 | 63.6 | +$207 | -$1,626 | -$3.19 | 51% | 0.90 | -1.18 | -$2,410 |
| 2025 | 6,882 | 27.1 | -$501 | **-$25,277** | -$3.67 | 50% | 0.88 | -5.24 | -$26,220 |
| 2026 excl. Jun 18-29 | 7,024 | 33.9 | -$812 | **-$26,099** | -$3.72 | 50% | 0.87 | -5.53 | -$26,905 |

- **There's no edge before costs.** The average gross per trade is -$0.07 (2025) and -$0.12 (2026),
  a coin flip. At about 30 trades a day, the $3.60 cost turns that into about -$25k a year.
- **Losses are consistent.** Every quarter of both years lost money. 2025: Q1 -$5,980, Q2 -$11,692,
  Q3 -$3,199, Q4 -$4,405. 2026: Q1 -$6,945, Q2 -$8,032, Q3 -$9,191, Q4 (to Nov 3) -$1,931.
- **Every entry hour lost money** in both years. The worst were 09:00-11:00 CT.
- **Both directions lost.** 2025: longs -$18,213, shorts -$7,063. 2026: longs -$14,833, shorts -$11,266.
- The test windows contained 8 trading days. The t-stats above are for the trades, not the days.

## 6. Optimization

### Method

1. **Simulator:** a fast simulator (`sim_wick.py` in the session scratchpad) mirrors the strategy:
   1-minute candles for signals and 1-second bars for exits, taking the stop first if a bar touches
   both. Against the tick backtest on 2025 with the original config, 97% of entries matched to the
   second and every matched trade had the same win/loss outcome. It was about $0.06 per trade
   optimistic.
2. **Grid:** 3,840 configs, evaluated on **2025 only**:

   | Parameter | Values |
   |---|---|
   | `candle_minutes` | 1, 2, 3, 5 |
   | `min_wick_ticks` | 40, 60, 80, 120, 160 |
   | `lookback_candles` | none, 10, 30, 60 |
   | volume filter | 2000-7000 or none |
   | `stop_wick_pct` | 0.5, 1.0 |
   | `tp_wick_pct` | 0.5, 1.0, 1.5 |
   | runner | trail at 0.2, 0.5 or 1.0, or close both contracts at the target |

3. **Qualifying rule:** at least 100 trades in 2025 and positive net in at least 3 of 4 quarters,
   ranked by t-stat. The plateau was preferred over the single best cell.
4. **Holdout:** the chosen config was committed before looking at 2026, tested once on 2026
   excluding Jun 18-29, and confirmed with the real tick backtester.

### What the grid showed (2025, simulator)

- **Most of the parameter space loses.** Of the 2,744 configs with at least 100 trades, only 12%
  were profitable after costs. The mean t-stat was -2.15.
- **Bigger wicks do better.** Mean t by `min_wick_ticks`: 40: -4.24, 60: -2.40, 80: -1.16,
  120: -0.33, 160: -0.33. Much of this is simply fewer trades and less cost drag.
- **The other parameters matter much less:**
  - stop 1.0 beats 0.5 (mean t -1.68 vs -2.61);
  - 1-minute candles do best (-1.73 vs -2.0 to -2.6 for 2, 3 and 5 minutes);
  - the volume filter makes no difference;
  - the runner style barely matters.
- **Wick size x lookback** (1-minute candles, stop 1.0, target 1.5, trail 1.0, no volume filter), t-stat:

  | min wick \ lookback | none | 10 | 30 | 60 |
  |---|---|---|---|---|
  | 40 | -4.67 | -3.35 | -2.66 | -2.10 |
  | 60 | -1.02 | -1.14 | -0.37 | -0.12 |
  | 80 | 0.13 | 0.17 | 1.14 | 1.33 |
  | 120 | -0.15 | 1.52 | **2.38** | 1.77 |
  | 160 | 1.99 | 2.81 | 2.88 | 2.09 |

  The 160-tick row has only 37-91 trades per cell. 120 ticks with a 30-candle lookback was
  chosen as the centre of the positive region with enough trades (126 in the simulator).

### Tuned configuration

```yaml
candle_minutes: 1
min_wick_ticks: 120        # was 40
lookback_candles: 30       # new
# volume filter removed (was 2000-7000)
stop_wick_pct: 1.0
tp_wick_pct: 1.5           # was 1.0
trail_wick_pct: 1.0        # was 0.2
num_contracts: 2
tp_contracts: 1
# RTH 08:30-15:00 CT, daily limits unchanged
```

## 7. Results: tuned configuration (tick backtester, RTH)

| Period | Trades | Trades/day traded | Gross | Net | Avg net | Win rate | PF | t | Max DD |
|---|---|---|---|---|---|---|---|---|---|
| 2025 (tuning year) | 114 | 1.8 | +$5,178 | **+$4,767** | +$41.82 | 48% | 1.51 | 1.97 | -$1,587 |
| **2026 excl. Jun 18-29 (holdout)** | 150 | 1.6 | +$2,110 | **+$1,570** | +$10.47 | 44% | 1.12 | **0.64** | -$1,774 |
| Jun 18-29, 2026 | 11 | 2.2 | -$178 | -$218 | -$19.78 | 36% | 0.81 | -0.32 | -$405 |

- **By quarter:**
  - 2025: Q1 +$1,419, Q2 +$2,672, Q3 +$288, Q4 +$389.
  - 2026 holdout: Q1 +$1,201, Q2 +$998, Q3 **-$941**, Q4 (to Nov 3) +$312.
- **April 2025 (the tariff-crash volatility) drove the tuning year:** 38 trades netted +$3,255.
  The other 76 trades of 2025 netted +$1,513 ($19.90 per trade).
- **The direction mix flipped between years.** 2025: longs +$1,189 (75 trades), shorts +$3,578
  (39 trades). 2026 holdout: longs +$1,653 (95 trades), shorts -$82 (55 trades). An edge that
  switches sides like this is more likely noise.
- **Exits in the 2026 holdout:** 84 stops, 64 partial targets followed by a trailing stop, and 2
  closed by the end-of-file flatten.
- **Trade swings are large.** The standard deviation of net per trade is about $200, so the $10.47
  average is well inside the noise. At that average and spread it would take about 1,480 trades
  to reach t=2: roughly 8 years at 15 trades a month.
- **The simulator disagreed on the holdout.** It gave $0.03 per trade versus the tick backtester's
  $10.47. With so few large-wick trades, small differences in how the trailing stop fills move the
  total a lot. This makes the exact tuned parameter values less trustworthy.

## 8. Conclusions

1. **Don't run the original configuration.** It has no edge before costs and trades about 30 times
   a day, which costs about $25k a year in commissions and slippage on 2 contracts. The result is
   consistent across both years, every quarter, every hour and both directions.
2. **The tuned configuration is much less bad but unproven.** It is roughly breakeven to mildly
   positive out of sample (t=0.64). Its tuning-year profit depended heavily on one volatile month.
3. **The thresholds are in ticks, so they don't scale with the market.** MNQ traded about 30%
   higher in 2026 than in 2025, so a 120-tick wick is a relatively smaller move in 2026. A
   volatility-scaled threshold (e.g. a wick of at least k x ATR) would be the natural next
   experiment, as a new research round with its own holdout.
4. **Fix the live behaviour before any live use.** Live trading uses full-size brackets, so the
   partial target and trailing runner don't happen live, and there is no time exit (section 2).
5. **The cleanest next test is forward paper trading** (`farm.py` with the order manager
   disabled). Data recorded from now on can't have influenced any choice made here.

## 9. Reproducing

The config entries in `config.mnq.yaml` (each carries its results in comments):

```bash
python backtest.py --config config.mnq.yaml --name mnq_wick_reversal              # original params, Jun 18-29 2026
python backtest.py --config config.mnq.yaml --name mnq_wick_reversal_tuned_2025   # tuned, all of 2025
python backtest.py --config config.mnq.yaml --name mnq_wick_reversal_tuned_2026   # tuned, 2026 to Nov 3
```

- **Runtime:** the runner processes one day at a time, at about 11 seconds per day. A full year
  takes about 45 minutes.
- **Costs:** the printed `total_pnl` is gross. Subtract $3.60 per trade for net.
