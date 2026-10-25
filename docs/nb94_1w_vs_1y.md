# NB94 — why 1 week works but 1 year doesn't

Generated 2026-10-21.

## TL;DR

Two independent **forward-leak** bugs in the NB72 swing cap and the
NB91 rolling p95 cap. Both window filters were missing their upper
bound, so on any file longer than the lookback window they pulled in
**future** bars.

The swing cap is the fatal one: it inflated the TP cap by up to
**18.7x**, which put every TP out of reach, so nothing ever closed.

| slice | expected cap | actual (before) | inflation |
| --- | --- | --- | --- |
| 1 week | $17.71 | $42.27 | 2.2x |
| 1 month | $17.06 | $167.15 | 9.8x |
| 3 months | $27.87 | $144.47 | 5.2x |
| full 1y file | $92.66 | **$1,733.62** | **18.7x** |

---

## 1. The signature: the bug is length-dependent AND position-dependent

A 1-week file looked correct because **the whole file is shorter than
the lookback window**, so the forward leak is bounded by the file edges
and stays small. On 1 year there is a real future to leak into.

The leak is also *position*-dependent within the file:

| probe position | "24h window" actually spans | verdict |
| --- | --- | --- |
| early in file | ~1 day | accidentally correct |
| middle (May 2025) | **435 days** | 18.7x inflated |
| near end of file | ~1 day | accidentally correct |

That is why the bug can hide behind a short validation slice and only
manifest on the long run.

---

## 2. Root cause 1 — NB72 swing cap, missing upper bound

`bt/_engine/swing_cap.py` → `DailySwingCap.cap_for`:

```python
cutoff = t_ns - int(self.lookback_secs * 1e9)
mask = ts >= cutoff          # <-- LOWER bound only
win_high = float(hs[mask].max())
win_low  = float(ls[mask].min())
```

The window ran from `t − 24h` to the **end of the buffer**. Measured on
`XAUUSD_S1_1y` at the file midpoint:

```
entries passing the one-sided mask : 17,833   (spanning 435 days)
entries in the correct two-sided win:      58
cap_for (buggy)                     : $1,733.62
cap_for (correct)                   : $92.66
```

### Fix

```python
mask = (ts >= cutoff) & (ts <= t_ns)
```

A trailing window needs **both** bounds. The one-sided mask also
makes the cap depend on how much *future* data happens to be in the
file — the same query timestamp returns different answers depending on
where the file is truncated, which is a textbook look-ahead.

---

## 3. Root cause 2 — NB91 rolling p95, positional reshape

`bt/_engine/rolling_p95.py` → `_downsample_to_inner` bucketed 1h
windows with a raw numpy reshape:

```python
H = high[:n_complete].reshape(n_buckets, inner_secs)   # assumes gapless
```

That assumes every 3,600 consecutive rows span exactly one hour. XAUUSD
has weekend and rollover gaps:

```
bars with a >1s gap                : 6,192,328  (15.018%)
buckets whose span != exactly 1h    : 11,453 of 11,453  (100%)
bucket-span drift vs contiguous     : mean -5,725h, min -11,451h
```

Consequences:

1. A bucket straddling a weekend reports a **multi-day range as if it
   were one hour**, inflating `inner_p2t` and therefore `p95_ret`.
2. The synthetic `bucket_t_ns` advanced by exactly `inner_secs` per
   bucket regardless of real elapsed time, so `buffer_t_ns` ran out of
   sync with the data — on 1y the last bucket landed around **April
   2025** while the bars ran to **July 2026**. `cap_for`'s
   `searchsorted` saturated and returned a **stale value for the last
   ~15 months**.

### Fix

Bucket by wall-clock time: floor each timestamp to the UTC hour
boundary, then aggregate with `np.unique` + `reduceat`. Gaps then
produce genuinely empty buckets instead of sheared ones, and
`bucket_t_ns` is always the real time of the bucket.

Post-fix: 11,453 → **15,012** buckets (≈2.5y × 8,760 h/y, weekend gaps
included), and `cap_for` now **varies** across the file
($10.56 → $17.70 → $30.79 → $34.23) instead of saturating at a stale
$36.09.

---

## 4. What was NOT the cause

Several plausible suspects were measured and ruled out:

* **The p95 warmup gate.** `is_warmup_complete` is True for
  **99.3786%** of bars — only the first 8 days are gated. The warmup
  gate was never the blocker.
* **Millisecond/nanosecond mixing.** The NB91 timebase fix is intact;
  `assert_ns` passes and `warmup_until_ns` is a correct ns timestamp
  (2024-01-09).
* **The conflict policy.** With 264,666 signals emitted and
  `n_dropped_opposite == 0`, the policy was never even reached.

This is why the engine was instrumented with per-gate attribution
counters rather than guessed at:

```
n_signals_emitted
  - n_dropped_vol_state
  - n_dropped_p95_warmup
  - n_dropped_p95_warmup_sniper
  - n_dropped_policy
== n_immediate_filled + n_sniper_filled
```

`diag["gate_breakdown"]` carries the breakdown and
`diag["signals_unaccounted"]` must be 0.

---

## 5. Changes made

| File | Change |
| --- | --- |
| `bt/_engine/swing_cap.py` | `cap_for` mask now bounded above by `t_ns` |
| `bt/_engine/rolling_p95.py` | `_downsample_to_inner` buckets by wall-clock time, not position |
| `bt/_engine/vp_multi_target_engine.py` | per-gate attribution counters + `diag["gate_breakdown"]` |
| `bt/_engine/ict_signals.py` | fixed `NameError` on `time`; removed O(n²) live-zone recount in the progress tick |
| `runtime/sweeps/run_dual_sweep.py` | surfaces the new counters in the summary |
| `tests/test_regressions.py` | 4 new regression tests (all verified to fail on the old code) |
| `runtime/sweeps/diag_swing_cap.py` | new — proves the swing leak in 19s without an FVG run |
| `runtime/sweeps/diag_p95_gate.py` | new — p95 warmup/gap diagnostics |

---

## 6. Verification

| check | before | after |
| --- | --- | --- |
| swing cap, 1y midpoint | $1,733.62 | $81.58 (true $92.66) |
| swing cap, 3 months | $144.47 | $27.87 (true $27.87) |
| p95 buckets | 11,453 | 15,012 |
| p95 `cap_for` at last bar | $36.09 (stale) | $34.23 (live) |
| 3-month canonical run | 0 trades | **22,002 trades** |
| 1-month canonical run | 4,335 trades | 4,704 trades |
| test suite | — | **56 passed** |

The 3-month slice is the proof point: it is a genuinely multi-month
file (the case the bug required) and it now closes trades with sane
`median_tp_distance_usd = $18.46`.

---

## 7. Structural lesson

**Any rolling-window statistic that reads a buffer needs a two-sided
mask.** A one-sided mask is a look-ahead, and its severity scales with
the file length — so it is invisible in unit tests, invisible on short
validation slices, and maximal on the long run you actually care about.

Generalise the rule:

1. Rolling windows → `lo <= t <= hi` (never `t >= lo` alone).
2. Bar aggregation → bucket by **timestamp**, not by row position.
3. Any window statistic → assert the result is **independent of the
   file truncation point**. `test_swing_cap_is_stable_across_file_length`
   is the shape of that test: query the same timestamp against
   several truncations of the file and require identical answers.
