# Gap Fill (`gap_fill`)

Strategy module: `strategies/gap_fill.py` · Params model: `GapFillParams` in `api/models/models.py`

Findings recorded 2026-11-05 from a statistical study and simulated trades on MNQ 2024-2026 RTH
data. The short version: **fading large gaps looked good on 2025-2026 (+31.6 net points per
trade) but failed its one-shot holdout on 2024 (+4.3 points per trade, t=0.25).** The edge comes
and goes by quarter. The strategy was implemented anyway as a discretionary and context-dependent
tool, not a proven always-on system.

---

## 1. The idea

Overnight, futures can move far from where the regular session (RTH) closed. The classic "gap
fill" idea says price tends to come back to the prior close once the full RTH crowd arrives at
08:30 CT.

The study found that this is only plausible for **large** gaps relative to the prior day's range.
Small and medium gaps tend to keep going, and fading them lost money in both discovery years.

- Gap **up** (open above the prior close) → **SHORT**, target the prior close
- Gap **down** (open below the prior close) → **LONG**, target the prior close

## 2. How it works

### Sessions

- The strategy builds RTH sessions from the tick stream. A session is every trade from
  `rth_start` (08:30 CT) up to but not including `rth_end` (15:00 CT). Pre-market and after-hours
  trades are ignored.
- For each session it keeps the **close** (the last trade before 15:00), the **high**, the
  **low** and the **symbol**.
- The prior session is needed at the open. It comes from:
  - **the tick stream**, when the strategy runs continuously (live) or with
    `persist_across_files: true` (backtests), or
  - **seed candles**, when `aggregation_params` is set. The candles are summarised the same way,
    so a live farmer started in the morning knows yesterday's close.

### Signal (evaluated once a day, at the open)

Only the first tick at or after `entry_hour:entry_minute` (08:30 CT) is evaluated, and only if it
arrives within `max_entry_delay_minutes`. Either way, the day is then done.

1. **Data checks.** The trade is skipped if:
   - the date is in `skip_dates`,
   - there is no prior session, or it is older than `max_prior_session_age_days`,
   - the prior session covered less than `min_prior_session_minutes` (half days, partial data),
   - the prior session traded a different symbol (a contract roll), or
   - a position is already open.
2. **Gap size.** `gap = entry price - prior close` and `ratio = |gap| / prior RTH range`.
   - The ratio must be at least `min_gap_range_ratio` and, optionally, at most
     `max_gap_range_ratio`.
   - Optionally, `|gap|` must be at least `min_gap_points`.
3. **Direction filter (optional):** `direction: LONG` or `SHORT` trades one side only.

Every evaluation is logged with the gap, the prior close, the prior range and the ratio, whether
or not it trades. That is useful context for discretionary use.

### Entry

- The position opens at the evaluated tick's price with `num_contracts`.

### Exits

All exit distances are multiples of the gap, measured from the entry price:

| Exit | Rule |
|---|---|
| Take profit | `fill_fraction x gap` toward the prior close. 1.0 = the prior close exactly. |
| Stop loss | `stop_gap_multiple x gap` away from the prior close. Checked before the target on each tick. |
| Time exit | Close at the first tick at or after `flat_hour:flat_minute` (14:59 CT). |

- `max_stop_points` (optional) skips any trade whose stop would be wider than that many points.
  Large gaps mean large stops; see section 7.
- On **half days** RTH ends before 14:59, so the time exit never fires. In backtests, set
  `flatten_at_end_of_file: true`. Live, the position stays open until a bracket fills or the
  farmer shuts down (which flattens).

### Contract rolls

A roll changes contracts, and the price difference between them looks like a gap.
- With real contract symbols (live, or multi-symbol backtests), the symbol check skips the roll
  day automatically.
- The continuous `MNQ.v.0` symbol never changes, so list the roll days in `skip_dates`. They are
  the days the Databento `instrument_id` changes:

  | Year | Roll days |
  |---|---|
  | 2024 | 03-13, 06-19, 09-18, 12-19 |
  | 2025 | 03-20, 06-18, 09-18, 12-18 |
  | 2026 | 03-19, 06-15, 09-17 |

## 3. Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `tick_size` / `tick_value` | required | Instrument (MNQ: 0.25 / $0.50, i.e. $2 per point) |
| `precision` | 2 | Price rounding |
| `min_gap_range_ratio` | 0.5 | Minimum \|gap\| / prior RTH range |
| `max_gap_range_ratio` | None | Optional maximum ratio |
| `min_gap_points` | None | Optional minimum \|gap\| in points |
| `direction` | None | `LONG`, `SHORT`, or None for both |
| `fill_fraction` | 1.0 | Target as a fraction of the gap; 1.0 = the prior close |
| `stop_gap_multiple` | 1.0 | Stop distance as a multiple of the gap |
| `max_stop_points` | None | Skip trades with a wider stop |
| `num_contracts` | 1 | Contracts per trade |
| `use_brackets` | False | Send stop/target as broker brackets live |
| `rth_start_hour` / `_minute` | 8 / 30 | RTH session start (CT) |
| `rth_end_hour` / `_minute` | 15 / 0 | RTH session end (CT, exclusive); the prior close is the last trade before it |
| `entry_hour` / `_minute` | 8 / 30 | When the gap is evaluated (CT) |
| `max_entry_delay_minutes` | 5 | Skip the day if the first tick in the window arrives later than this |
| `flat_hour` / `_minute` | 14 / 59 | Time exit (CT) |
| `skip_dates` | [] | Never trade these dates (YYYYMMDD or YYYY-MM-DD) |
| `min_prior_session_minutes` | 300 | Minimum span of the prior session's RTH trades |
| `max_prior_session_age_days` | 5 | Maximum calendar days back to the prior session |

## 4. Test setup

- **Data:** Databento GLBX MDP3 trades for the continuous front month `MNQ.v.0`, 07:30-15:00 CT
  only, cached as 1-minute bars for the study.
  - `mnq_2024_historical/`: 2024-01-02 to 2024-12-31
  - `mnq_2025_historical/`: 2025-01-02 to 2025-12-31
  - `mnq_2026_historical/`: 2026-01-02 to 2026-11-03
- **Costs:** all "net" figures subtract 0.9 points ($1.80) per contract round trip.
- **Simulation:** the study used 1-minute bars, taking the stop first when one bar touched both
  the stop and the target. Roll days were skipped, as were half days and the day after one.
- **In-sample vs out-of-sample:**
  - **Discovery:** 2025 and 2026. Many gap sizes and brackets were tried here (about 60 combinations).
  - **Holdout:** 2024. One rule was frozen first and tested once.
- **t-stat:** average net per trade divided by its standard error. About 2 is meaningful for a
  single pre-planned test; after about 60 combinations, the best one reaching 2 is expected by
  luck alone.

## 5. Discovery results (2025-2026)

434 gap days. The median |gap| was 110 points, about a third of the prior day's range (median
ratio 0.34).

### Gap statistics

| Ratio | Days | Median \|gap\| | Filled by 15:00 | Filled in 30 min | Prior close hit first | Mirror level hit first | Fade to close (pts) |
|---|---|---|---|---|---|---|---|
| 0.00-0.05 | 32 | 5 | 100% | 100% | 25% | 22% | +20.5 |
| 0.05-0.10 | 37 | 25 | 86% | 70% | 54% | 41% | -42.5 |
| 0.10-0.20 | 66 | 48 | 82% | 61% | 50% | 50% | +27.9 |
| 0.20-0.35 | 87 | 84 | 53% | 37% | 44% | 55% | -35.9 |
| 0.35-0.60 | 93 | 150 | 43% | 14% | 39% | 37% | +7.2 |
| 0.60+ | 119 | 288 | 31% | 4% | 31% | 13% | +35.6 |
| **All** | 434 | 110 | 56% | 34% | 40% | 35% | +6.3 (t=0.50) |

- **"Mirror level"** is the same distance from the open, on the other side. Comparing which level
  price reaches first controls for volatility. Overall it's close to a coin flip (40% vs 35%), so
  there's no general pull back toward the prior close.
- **Fading every gap and holding to 15:00** made +37.5 points per trade in 2025 and -29.9 in 2026.
  The average means nothing.
- **Pre-market already touched the prior close** (07:30-08:30) on 21% of days. Those were mostly
  small gaps.
- Gaps up and gaps down behaved the same.

### Bracket grid

Fade at the open with target `f x gap` and stop `k x gap`, exit at 15:00. Net points per trade:

| Gaps | f=1, k=1 | Best region | Both years positive? |
|---|---|---|---|
| All | +7.5 (t=1.03) | none | mixed |
| Ratio 0.10-0.35 | -10.0 | none: all 12 combinations lose | **lose in both** |
| Ratio ≥ 0.35 | +22.5 (t=1.67) | f=0.75-1.0 | mostly |
| Ratio ≥ 0.60 | +41.4 (t=2.02) | f=1.0 | yes, for k ≤ 1 |

### Gap-size threshold (f=1, k=1)

| Ratio | Trades | Net pts/trade | t | 2025 | 2026 |
|---|---|---|---|---|---|
| ≥ 0.3 | 243 | +15.9 | 1.29 | +22.9 | +9.2 |
| ≥ 0.4 | 195 | +24.0 | 1.67 | +30.5 | +17.5 |
| **≥ 0.5** | 155 | **+31.6** | 1.86 | +42.7 | +21.0 |
| ≥ 0.6 | 120 | +41.4 | 2.02 | +61.4 | +23.3 |
| ≥ 0.7 | 96 | +56.5 | 2.43 | +82.7 | +32.4 |
| ≥ 0.8 | 73 | +26.6 | 0.97 | +64.1 | -6.1 |
| ≥ 1.0 | 49 | +27.0 | 0.83 | +72.1 | -9.8 |

- 0.4-0.7 is a plateau where both years are positive. **0.5 was frozen** as its middle, rather
  than the best single cell (0.7).
- At ratio ≥ 0.6, the quarterly averages were 2025 Q1 +9.9, Q2 +69.2, Q3 +75.4, Q4 +97.5 and 2026
  Q1 +75.8, Q2 +32.7, **Q3 -6.3, Q4 -10.0**: the edge was fading in the most recent data.
- The median trade was +67.6 points. Without the best 5 trades, the average dropped from +41.4
  to +20.5.

## 6. Holdout result (2024)

The frozen rule: ratio ≥ 0.5, target the prior close, stop one gap beyond the entry, exit at 15:00.

| | Discovery (2025-2026) | **Holdout (2024)** |
|---|---|---|
| Trades | 155 | 72 |
| Net pts/trade | +31.6 | **+4.3** |
| t | 1.86 | **0.25** |
| Win rate | | 54% |
| Median trade | | +42.9 |
| Total | | +306 pts (+$613 per contract) |
| Max drawdown | | -933 pts |
| Average stop | | 187 pts |

- **By quarter:** 2024 Q1 -1.8 points per trade (21 trades), Q2 +47.5 (19), Q3 +5.0 (16),
  **Q4 -39.9 (16)**.
- 2024's gaps were smaller (median 69 points, ratio 0.32). Measuring the gap against the prior
  range kept the trade count reasonable anyway.
- **The typical trade still wins**: the median is clearly positive. The problem is the losers,
  which are full gap-width stops large enough to erase many winners.

## 7. Conclusions

1. **This is not a proven edge.** It failed its only clean test. In two of the three years the
   strategy made money, but its results swing with the regime: strong quarters and losing
   quarters alternate.
2. **The losers are large.** The stop equals the gap, and at ratio ≥ 0.5 that averaged about
   190-320 points ($380-$640 per MNQ contract). Size accordingly, or use `max_stop_points`.
3. **Small and medium gaps continue rather than fill.** Fading ratio 0.10-0.35 lost in every
   combination tested, in both discovery years. That would be a stronger research lead than the
   large-gap fade (as a gap *continuation* idea), but it has its own multiple-testing caveats and
   no holdout left. It would need forward data.
4. **Context may decide it.** The user's view is that this works when the market context suits
   it. That is testable: the strategy logs the gap on every day, so recording market context
   (the overnight range, news, trend, volatility) alongside each day's result will show whether
   any context separates the good quarters from the bad ones. Validate any such rule on data
   recorded after 2026-11-03.
5. **Don't re-tune it on 2024.** That year has now been used. Any change needs forward data.

## 8. Framework backtest (tick data)

The implemented strategy, run through `backtest.py` on tick data, 1 contract:

| Year | Trades | Targets | Stops | Time exits | Gross | Net | Net pts/trade | t |
|---|---|---|---|---|---|---|---|---|
| 2024 (holdout) | 72 | 24 | 19 | 29 | +$751 | +$621 | +4.3 | 0.25 |
| 2025 (discovery) | 79 | 27 | 15 | 37 | +$6,410 | +$6,267 | +39.7 | 1.75 |
| 2026 to Nov 3 (discovery) | 80 | 21 | 15 | 44 | +$3,264 | +$3,120 | +19.5 | 0.80 |

- **It reproduces the study.** 2024 matches the holdout simulation exactly (72 trades, +4.3).
  2025 and 2026 are within a few points of the simulation (+42.7 and +21.0). The small
  differences come from the strategy trading half days and the day after them, which the study
  skipped, and from exact tick ordering instead of 1-minute bars.
- **Time exits include 5 end-of-file flattens** on half days (3 in 2025, 2 in 2026).
- **Only about a third of trades reach the target.** Most of the profit in the good years comes
  from time exits that closed partway to the prior close.

## 9. Reproducing

The config entries in `config.mnq.yaml`:

```bash
python backtest.py --config config.mnq.yaml --name mnq_gap_fill_2024
python backtest.py --config config.mnq.yaml --name mnq_gap_fill_2025
python backtest.py --config config.mnq.yaml --name mnq_gap_fill_2026
```

- They use `persist_across_files: true` so each day knows the prior close, and
  `flatten_at_end_of_file: true` for half days. Roll days are in `skip_dates`.
- The first day of each run has no prior session, so it is skipped.
- **Costs:** the printed `total_pnl` is gross. Subtract $1.80 per contract per trade for net.
- The study scripts (`gap_study.py`, `gap_trade.py`, `gap_robust.py`, `gap_holdout.py`) were in
  the session scratchpad and are not part of the repository.
