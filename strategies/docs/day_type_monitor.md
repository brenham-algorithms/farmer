# Day Type Monitor (`day_type_monitor`)

Strategy module: `strategies/day_type_monitor.py` · Classifier: `calculations/day_type.py` ·
Params model: `DayTypeMonitorParams` in `api/models/models.py`

Written 2026-11-09. The short version: **this never trades. It labels the RTH session so far as
trend up/down, range, chop or mixed, and logs when the label changes. The labels describe the
day so far; a pre-registered study found that they do not predict the rest of the day (section
5), so don't use them as an entry filter.** Its job is context: a record of what kind of day
each day was, for reviewing strategy results and for the planned daily server records.

---

## 1. The idea

Strategies make money on different kinds of days. On MNQ 2024-2026, `noise_area` lost about 79
points per day on the third of days that trended least and made about 123 on the third that
trended most (`noise_area.md`). A monitor that names the day type, live and in a daily summary,
makes it easier to see why a strategy won or lost and to keep a record of it.

## 2. How it works

### Measures (RTH session so far, 08:30 CT to now)

| Measure | Definition | Reads as |
|---|---|---|
| `trendness` | \|last - open\| / (high - low) | Near 1: closed at an extreme. Near 0: back near the open |
| `efficiency` | \|last - open\| / distance travelled by 1-minute closes | Kaufman efficiency ratio. Small over a full day (0.15 on a strong trend day, near 0 in chop) |
| `vwap_crosses` | 1-minute closes that changed side of session VWAP | Many = chop; few = one-sided or wide rotation |
| `range_vs_normal` | (high - low) / average range at the same minute over the last `lookback_days` full sessions | Above 1 = more movement than usual |

### Labels

| Label | Rule (defaults) |
|---|---|
| too early | Less than `min_minutes` (30) since the open |
| trend up / trend down | trendness >= `trend_min_trendness` (0.6) |
| range | trendness < `range_max_trendness` (0.35) and VWAP crosses < `chop_crosses_per_hour` (4) per hour |
| chop | trendness < 0.35 and crosses >= 4 per hour |
| mixed | anything else |

- The trendness cut points are the MNQ 2024-2026 thirds (0.33 / 0.61), rounded. The chop cut
  is about the median crossing rate at 10:00. None of the thresholds is tuned.
- **Hysteresis:** to leave its current label, trendness must clear the threshold by
  `hysteresis` (0.05), and the crossing rate by 10x that. **Confirmation:** a new label must
  hold for `confirm_minutes` (5) before it is logged. Together they stop a value sitting near a
  threshold from flipping the label every few minutes.

### Logs

- `DAY TYPE old -> new | ...` on each confirmed label change.
- `DAY TYPE label | ...` every `snapshot_minutes` (30; 0 disables).
- `DAY TYPE SUMMARY label | ... | changes: 09:05 trend down, ...` once per session, on the first
  tick after 15:00 CT (or the next day's first tick). RTH-only backtest files have no ticks
  after 15:00, so the last day of a backtest gets no summary.

### Seeding

- **Live and discover:** 1-minute candles through `aggregation_params`. 20 full sessions need
  about 30 calendar days. A mid-session start replays today's candles quietly, so the label and
  change list are current from the first tick.
- **Backtests:** `persist_across_files: true` so the range history carries across files.

## 3. Parameters

| Parameter | Default | Meaning |
|---|---|---|
| `precision` | 2 | Price formatting |
| `min_minutes` | 30 | "too early" before this |
| `trend_min_trendness` | 0.6 | Trend threshold |
| `range_max_trendness` | 0.35 | Range / chop threshold |
| `chop_crosses_per_hour` | 4.0 | VWAP crosses per hour that make a low-trendness day chop |
| `confirm_minutes` | 5 | A new label must hold this long |
| `hysteresis` | 0.05 | Threshold margin in favour of the current label |
| `snapshot_minutes` | 30 | Snapshot interval; 0 disables |
| `lookback_days` | 20 | Full sessions averaged for `range_vs_normal` |
| `min_session_minutes` | 300 | Shorter sessions (half days) are left out of the history |
| `rth_start_hour` / `_minute` | 8 / 30 | Session start (CT) |
| `rth_end_hour` / `_minute` | 15 / 0 | Session end (CT, exclusive) |

## 4. Usage

```bash
# Live, logging all day (config.yaml farmer)
python farm.py --name mnq_day_type --level info

# The day so far, once (config.yaml discover query)
python discover.py --query mnq_day_type

# Backtest over files (config.mnq.yaml)
python backtest.py --config config.mnq.yaml --name mnq_day_type_2026
```

Example, MNQ 2026-11-05 (the post-election selloff):

```
[2026-11-05 09:05:00 CT] DAY TYPE too early -> trend down | open=28204.00 last=27467.75 move=-736.25 range=796.50 (4.01x normal) trendness=0.92 ...
[2026-11-05 close] DAY TYPE SUMMARY trend down | ... move=-1461.00 range=1511.25 (3.46x normal) trendness=0.97 efficiency=0.15 vwap_crosses=0 | changes: 09:05 trend down
```

## 5. Study: do these measures predict the rest of the day?

Pre-registered before testing (session scratchpad, `daytype_prereg.md`). MNQ.v.0 1-minute RTH
bars; half days and roll days excluded.

- **Question:** do measures of the session so far, at 10:00, 11:00 and 12:00 CT, predict (A) how
  much the rest of the day trends, or (B) whether the morning direction continues?
- **Measures (7):** efficiency ratio, trendness, range expansion, VWAP crosses, variance ratio,
  initial-balance extension, delta alignment. 42 tests in all (Spearman rank correlation).
- **Protocol:** discovery on 2025; at most 3 picks; one-shot holdout on 2024 (pass = same sign
  and t >= 2); then 2026.

| Pick (measure, time → target) | 2025 (discovery) | 2024 (holdout) | 2026 |
|---|---|---|---|
| VWAP crosses, 10:00 → direction continues | t = -1.76 | t = -0.58 ✗ | t = -2.30 |
| Efficiency ratio, 10:00 → direction continues | t = +1.72 | t = +0.69 ✗ | t = +0.48 |
| Range expansion, 11:00 → rest of day trends | t = +1.48 | t = +0.15 ✗ | t = -0.95 |

- **Discovery was already null:** the best of 42 tests reached |t| = 1.76, less than chance
  usually produces from 42 tries.
- **All three picks failed the 2024 holdout.** Only VWAP crosses kept its sign every year, and
  it was clear only in 2026 (direction continued on 65% of low-cross days vs 38% of high-cross
  days); it's a lead to watch on forward data at most.
- **Baseline:** the 10:00 direction held to the close on 51-52% of days in every year, a coin
  flip.

## 6. Conclusions

1. **Use it as a record, not a filter.** The labels describe the day so far and don't predict
   the rest of it, so gating entries on them would add noise.
2. **Don't re-mine these measures on 2024-2026.** All three years are used for this question.
3. **Its value is context:** reviewing why a strategy won or lost, and feeding the planned daily
   server records. Paired with a pre-open trend/range call, it lets you score those calls
   against what the day actually did.
