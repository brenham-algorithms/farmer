# Noise Area (`noise_area`)

Strategy module: `strategies/noise_area.py` · Params model: `NoiseAreaParams` in `api/models/models.py`

Findings recorded 2026-11-06. The short version: **this is a published intraday momentum rule,
tested on MNQ 2024-2026 with its rules fixed in advance. On tick data it made +14.6 net points
per trading day (t=1.84), positive in all three years and in 10 of 12 quarters. That is the best
result of the research so far, but it is still just short of the pre-set bar of t ≥ 2.** All
three years are now used, so forward (paper) trading is the only remaining clean test.

---

## 1. The idea

Most intraday price movement is noise. The strategy estimates how far price normally wanders from
the open at each time of day, and only acts when price moves beyond that. A move outside the
usual range suggests a real supply/demand imbalance, which tends to persist into the close
(intraday momentum).

Source: Zarattini, Aziz & Barbon (2024), "Beat the Market: An Effective Intraday Momentum
Strategy for S&P500 ETF (SPY)",
[SFI working paper 24-97](https://www.sfi.ch/de/publications/n-24-97-beat-the-market-an-effective-intraday-momentum-strategy-for-s-p500-etf-spy).
On SPY, 2007 to early 2024, the authors report a 19.6% annualized return net of costs and a
Sharpe ratio of 1.33 (their backtest, with volatility-targeted sizing).

## 2. How it works

### The noise area

- For every minute `m` of the RTH session (08:30-15:00 CT), `sigma(m)` is the average of
  `|price(m) / open - 1|` over the last `lookback_days` sessions (14). `price(m)` is the last
  trade of that minute.
- At minute `m` today:
  - `upper = max(open, prior close) x (1 + band_multiplier x sigma(m))`
  - `lower = min(open, prior close) x (1 - band_multiplier x sigma(m))`
- Using the prior close as well as the open means an overnight gap widens the area on its side,
  so the gap itself doesn't count as a breakout.

### Decisions (checkpoints only)

Decisions are made only at checkpoints: every `check_interval_minutes` (30) from 09:00 through
14:30 CT. At each checkpoint, using the price of the first tick at or after it:

| Position | Rule |
|---|---|
| Flat | LONG if price > upper; SHORT if price < lower |
| Long | Exit if price < max(upper, VWAP) |
| Short | Exit if price > min(lower, VWAP) |

- An exit can flip: after exiting at a checkpoint, the same checkpoint can enter the other way.
- VWAP is the session VWAP from 08:30. With `use_vwap_stop: false`, only the band is used.
- Between checkpoints nothing happens, unless `stop_loss_points` is set. That optional hard
  stop is checked on every tick and isn't part of the paper's rules.
- Everything is closed at `flat_hour:flat_minute` (14:59 CT).
- A checkpoint is only acted on if a tick arrives within 2 minutes of it. A strategy started
  mid-session therefore doesn't trade on a stale checkpoint.

### Sessions and history

- Sessions are built from the tick stream: open, close (last trade before 15:00), per-minute
  prices, VWAP and symbol.
- The history needs `lookback_days` full sessions before the first trade:
  - **Backtests:** use `persist_across_files: true`. The first 14 sessions of each run only
    build history.
  - **Live:** seed with 1-minute candles through `aggregation_params`. 14 sessions is about 20
    calendar days; the `mnq_noise_area` farmer uses 24 to allow for holidays. The ProjectX
    aggregator splits the request into chunks of 20,000 bars (the API's limit per request).
    A farmer started mid-session takes today's open and VWAP from the candles and joins at
    the next checkpoint.
  - **Live, early closes:** the time exit is 14:59 CT, so list early-close days (Thanksgiving
    and the day after, Christmas Eve) in `skip_dates`. The backtest traded them and flattened
    at the end of the file.
  - **Live, rolls:** the seed candles come from a single `contract_id`. For about three weeks
    after switching to the new contract, part of its history is from when it was the thinly
    traded back month. The backtest used the continuous front month instead.
- **Half days** (sessions shorter than `min_session_minutes`) are left out of the history. A
  session whose first trade came more than `max_open_delay_minutes` after 08:30 has no
  reliable open, so it isn't traded and isn't added to the history.
- **The day isn't traded** if the date is in `skip_dates`, the prior session is older than
  `max_prior_session_age_days`, or the symbol changed since the prior session (a contract
  roll). `MNQ.v.0` hides its rolls, so list them in `skip_dates` (the dates are in
  `gap_fill.md`).

## 3. Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `tick_size` / `tick_value` | required | Instrument (MNQ: 0.25 / $0.50, i.e. $2 per point) |
| `precision` | 2 | Price rounding |
| `lookback_days` | 14 | Sessions averaged for sigma |
| `band_multiplier` | 1.0 | Scales sigma (the paper uses 1.0) |
| `check_interval_minutes` | 30 | Spacing of checkpoints |
| `first_check_hour` / `_minute` | 9 / 0 | First checkpoint (CT) |
| `last_check_hour` / `_minute` | 14 / 30 | Last checkpoint (CT) |
| `use_vwap_stop` | True | Trailing stop is the band or VWAP, whichever is tighter |
| `direction` | None | `LONG`, `SHORT`, or None for both |
| `stop_loss_points` | None | Optional hard stop, checked on every tick |
| `num_contracts` | 1 | Contracts per trade |
| `rth_start_hour` / `_minute` | 8 / 30 | Session start (CT) |
| `rth_end_hour` / `_minute` | 15 / 0 | Session end (CT, exclusive) |
| `flat_hour` / `_minute` | 14 / 59 | Time exit (CT) |
| `skip_dates` | [] | Never trade these dates (YYYYMMDD or YYYY-MM-DD) |
| `min_session_minutes` | 300 | Shorter sessions are left out of the history |
| `max_open_delay_minutes` | 5 | The session's first trade must be this close to 08:30 |
| `max_prior_session_age_days` | 5 | Maximum calendar days back to the prior session |

**Differences from the paper:** fixed contracts instead of volatility-targeted sizing; MNQ
futures instead of SPY; the futures RTH session (08:30-15:00 CT); and the prior close is the
last trade before 15:00 CT.

## 4. Test setup

- **Data:** Databento `MNQ.v.0` trades, 07:30-15:00 CT, 2024-01-02 to 2026-11-03 (as 1-minute
  bars for the study).
- **Costs:** 0.9 points ($1.80) per contract round trip. Every entry costs one round trip.
- **Pre-registration:** the rules and pass criterion were written down before testing (in the
  session scratchpad, `im_prereg.md`). This was a test of a published rule, not a parameter
  search:
  - **Pass:** positive pooled, t ≥ 2, positive in at least 2 of 3 years.
  - It was tested alongside three other published rules (section 5). All four results are
    reported.
- **Study simplifications:** roll days and half days were skipped, and prices were 1-minute
  closes. The first study run also restarted the 14-session history after every half day (about
  10 a year), which wrongly skipped about 40% of eligible days. Section 5 shows both runs; the
  corrected one matches the strategy and the tick backtest.

## 5. Results (study, 1-minute bars)

Net points per day, 1 contract:

| Rule | Days | Net/day | t | 2024 | 2025 | 2026 |
|---|---|---|---|---|---|---|
| Gao et al. 2018: first 30 min → last 30 min | 674 | -0.24 | -0.10 | +1.1 | -3.2 | +1.7 |
| Gao et al., both signals agree | 353 | -1.16 | -0.38 | -0.2 | -5.0 | +2.4 |
| Baltussen et al. 2021: prior close → 14:30, trade last 30 min | 674 | -5.02 | -2.20 | -3.9 | -8.8 | -2.0 |
| Noise area, first run (history-reset bug) | 225 | +18.6 | 1.67 | -1.7 | +26.6 | +34.0 |
| **Noise area, corrected** (per trading day) | 378 | **+12.6** | **1.57** | +9.1 | +15.2 | +13.7 |

- **The corrected noise area traded on 378 of 674 days.** It was positive in all 3 years and
  missed the t ≥ 2 bar. The bug fix makes the test match the rule as written; it wasn't a
  change chosen for its result.
- **The classic intraday momentum rules show nothing on MNQ.** The last-30-minute rule
  (Baltussen) is reliably the *wrong* way: MNQ tends to reverse late in the day. That
  observation is a lead to watch on forward data, not a finding; fading it nets only about +3
  points per day.
- **Other work agrees that simple intraday momentum is weak on MNQ.** A 2026 preprint found
  candle-based intraday momentum signals failed out of sample on MNQ over 2021-2025
  ([arXiv 2605.04004](https://arxiv.org/abs/2605.04004)).

## 6. Framework backtest (tick data)

The implemented strategy on tick data, 1 contract, net of $1.80 per trade:

| Year | Trade days | Trades | Net pts/trade | Net pts/day | t | Net $ | Max DD (pts) |
|---|---|---|---|---|---|---|---|
| 2024 | 142 | 217 | +7.9 | +12.0 | 1.37 | +$3,414 | -583 |
| 2025 | 126 | 214 | +9.9 | +16.9 | 0.97 | +$4,246 | -1,200 |
| 2026 to Nov 3 | 114 | 176 | +10.0 | +15.4 | 1.03 | +$3,507 | -1,093 |
| **All** | 382 | 607 | +9.2 | **+14.6** | **1.84** | **+$11,168** | -1,200 |

- **By quarter (net points):**

  | Year | Q1 | Q2 | Q3 | Q4 |
  |---|---|---|---|---|
  | 2024 | -142 | +393 | +544 | +913 |
  | 2025 | +681 | +1,560 | -659 | +541 |
  | 2026 | +441 | +334 | +299 | +679 |

  10 of 12 quarters were positive.
- **It agrees with the corrected study:** 382 vs 378 trade days, +14.6 vs +12.6 points per day.
  The tick version enters at the first tick after each checkpoint rather than the prior minute's
  close, and it trades half days and roll-adjacent days that the study skipped.
- **Win rate is 45% of days.** The profit comes from fewer, larger winning days, as expected
  for a momentum strategy.
- **Exits:** 393 back inside the band or VWAP, 210 time exits, 4 end-of-file flattens on half
  days.
- **Drawdowns are real:** the worst was 1,200 points ($2,400 per MNQ contract), during 2025 Q3.
  A prop account's trailing drawdown needs to be sized around that.

## 7. Conclusions

1. **The best candidate so far, not a proven edge.** It's a published rule with fixed
   parameters, and it was positive in every year and in 10 of 12 quarters. Its t-stat (1.84) is
   just below the bar. It is also one of four published rules tested, which slightly weakens it.
2. **Don't tune it on 2024-2026.** All three years are used. Changing the lookback, the band
   multiplier or the checkpoint spacing now would be fitting to known data.
3. **Paper trade it forward.** It's intraday, so it fits prop-firm rules, and every new day is
   clean data.
4. **The paper's volatility-targeted sizing** (smaller positions when recent volatility is high)
   is a natural addition. It mainly improves risk-adjusted returns and drawdowns rather than the
   average.

## 8. Reproducing

```bash
python backtest.py --config config.mnq.yaml --name mnq_noise_area_2024
python backtest.py --config config.mnq.yaml --name mnq_noise_area_2025
python backtest.py --config config.mnq.yaml --name mnq_noise_area_2026
```

- They use `persist_across_files: true` and `flatten_at_end_of_file: true`, with roll days in
  `skip_dates`. The first 14 sessions of each run only build history.
- **Costs:** the printed `total_pnl` is gross. Subtract $1.80 per contract per trade for net.
