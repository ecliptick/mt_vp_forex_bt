# Engine history — the defects that were found and fixed

> Relocated from `AGENTS.md` on 2026-11-03. This is the **archaeology**: the
> forward-leak bugs, the 1m-detector bugs, the engine performance work, the
> dead knobs, and the operational traps.
>
> None of it is operating procedure. It is the record of *why the code looks
> the way it does*, so nobody re-introduces a bug the repo already paid for.

## Contents

- [The engine fixes that made the backtest honest](#the-engine-fixes-that-made-the-backtest-honest)
- [Forward-leak bugs (NB94)](#forward-leak-bugs--fixed-2026-10-21-nb94)
- [The replay's zeroed `eod` legs](#the-replays-zeroed-eod-legs--found-and-fixed-2026-11-04)
- [The artifact-cache fingerprint hole](#the-artifact-cache-fingerprint-hole--found-and-fixed-2026-11-04)
- [The per-bucket `excess` was measuring fill geometry](#the-per-bucket-excess-was-measuring-fill-geometry--found-2026-11-04)
- [A concurrent 1y run makes the test suite look RED](#a-concurrent-1y-run-makes-the-test-suite-look-red-2026-11-04)
- [The dual-mode inverted_bar look-ahead](#the-dual-mode-inverted_bar-look-ahead--found-and-fixed-2026-11-03)
- [The 1m detector — three bugs fixed](#the-1m-detector--three-bugs-fixed-2026-10-2324)
- [Dead knobs](#dead-knobs--verified-2026-10-20)
- [Known engine bugs and their fixes](#known-engine-bugs-and-their-fixes-chronological)
- [Performance](#performance--the-1y-detector-and-engine)
- [Operational gotchas](#operational-gotchas)## The engine fixes that made the backtest honest

These are real, measured, and worth keeping. They are not edge work.

| # | Defect | Fix | Effect |
|---|---|---|---|
| **D1** | The engine debits `|exit+entry| × 0.5 × qty × bps × 2`, but `_derive_taker_fee_bps` handed it the **round-trip** dollar amount — a 2× overcharge on every trade. | Hand the engine a **per-side** rate. | $0.94 → **$0.47** per trade |
| **D2** | Entries opened **instantly on the trigger bar** at a price the market never traded. Measured: **95.9%** of pre-D2 fills happened on a later bar (median +42 bars), 4.1% never filled. | A signal now **places** a `PendingOrder`; it fills on a strictly later bar whose range touches the limit level. If the zone dies first, the order is **cancelled**. | trades 5,985 → **717** |
| **D3** | With `conflict_policy='drop'` and an unbounded book, the book **never emptied** and the recipe locked to one side (4,335 shorts / 0 longs) — a book artefact, not a bearish edge. | The same-dir cap check moved into the `drop` branch; canonical `max_same_dir_open = 20`. | two-sided trading; book depth capped at 20 |
| **P2–P4** | The per-bar book scan computed state nobody read, made ~198M method calls, and allocated ~198M lists. | Gate the scan behind its consumers; inline `qty_of`; compact in place. | engine rate 14,700 → **~71,000 bars/s** |

### Pessimistic execution (2026-10-22)

On 1s bars a single bar can touch BOTH the stop and the target, and
can gap through a level entirely. The canonical `fill_model`:

| Mode | Ambiguous bar | Gap-through |
| --- | --- | --- |
| `mid` | TARGET wins (legacy, optimistic) | fills at the level |
| `pessimistic` | **STOP wins** | fills at the **bar's open** |
| `next_open` | STOP wins | always the bar's open |

Measured on 1-month 2025-01, 0.00% of trades have a bar touching
both levels — so this knob is not the binding constraint on *this*
corpus. It **must** be pessimistic anyway, because it starts biting
the moment `sl_zone_mult` is tightened and the stop sits close to
the entry.

### The multi-target lifecycle — TP1 partial → breakeven → TP2

**Verified by measurement, not by reading the code.**
`runtime/diagnostics/breakeven_reminder_check.py` and
`runtime/diagnostics/breakeven_escape_impact.py`.

**Yes — the SL actually moves when the first leg takes 50%.** The TP1
branch closes `pct` of the position and reopens the remainder as a
**new `OpenTrade`** with `stop_price = entry_price` (breakeven),
`sl_struct_ref = "breakeven"`, `partial_close_pct = 0.0` (so TP1
cannot re-fire), `target_price = tp2_price`, and `qty = pos_qty ×
(1 − pct)`.

Because `partial_close_pct == 0`, `tp2_active` is **False** on the
remainder, so it is evaluated by the **single-target** path, where
`sl_hit = b_low <= ot.stop_price` (long) — i.e. the breakeven stop is
a **real, enforced exit**, not a field that is set and ignored.

Measured across six canonical slices: `sl_distance_usd` is **$0.00**
on EVERY sl-exited remainder leg, with an absolute max of **$0.00**
— the signature of a breakeven stop firing precisely at entry.

**The one inconsistency: same-bar breakeven escape.** The TP1 branch
ends in `continue`, so the freshly-created remainder is **not
re-scanned** against the bar that fired TP1. A bar that reached TP1
and also traded back through the entry price therefore lets the
remainder escape a breakeven stop that was already breached.

Measured: **1 of 12 TP1 legs (8.3%)** had such a bar. This is
**FAVOURABLE** to the strategy and is **inconsistent with
`fill_model='pessimistic'`**. **Do not "fix" it silently** — it moves
P&L, and every number in this repo was measured with it in place. If
it is ever changed, re-measure the canonical slices. Guard:
`tests/test_regressions.py::test_tp1_remainder_is_not_rescanned_against_the_tp1_bar`.

## Forward-leak bugs — fixed 2026-10-21 (NB94)

**Symptom:** 1-week / 1-month backtests looked fine; the 1-year run
emitted 264,666 signals and closed **0 trades**.

**Root cause:** two rolling-window statistics whose filters were
**missing their upper bound**, so they read FUTURE bars.

| Where | The bug | Impact |
| --- | --- | --- |
| `bt/_engine/swing_cap.py` `cap_for` | `mask = ts >= cutoff` (no `ts <= t_ns`) | the "24h window" ran to the end of the buffer — up to 435 days. TP cap inflated **18.7×** ($92.66 → $1,733.62), putting every TP out of reach. |
| `bt/_engine/rolling_p95.py` `_downsample_to_inner` | positional `reshape(-1, 3600)` assumes a gapless feed | 15.018% of 1y bars are >1s apart (weekend gaps), so 100% of "1h" buckets sheared. |

### The rule

> **Any rolling-window statistic that reads a buffer needs a TWO-SIDED
> mask.** A one-sided mask is a look-ahead whose severity scales with
> file length — invisible in unit tests, invisible on short validation
> slices, maximal on the long run.

Three corollaries:

1. **Rolling windows** → `lo <= t <= hi`. Never `t >= lo` alone.
2. **Bar aggregation** → bucket by **timestamp**, never by row
   position. Positional reshape assumes no gaps; real FX data has
   weekend/rollover gaps.
3. **Window statistics** → assert the result is **independent of the
   file truncation point**. See
   `tests/test_regressions.py::test_swing_cap_is_stable_across_file_length`.
4. **A fallback that silently degrades correctness is a bug, not a
   convenience.** The detector's resample fallback (positional
   bucketing) was reachable from three call sites — including the
   decisive beta diagnostic. It emitted a `RuntimeWarning` on every
   call and **nothing failed**. **A guard must live at the call site,
   not only in the callee.**

### Why short validation runs hid it

The leak is *position*-dependent: early-in-file and end-of-file probes
are accidentally correct. Only probes in the middle of a long file see
it. A 1-week file is short enough that every probe is an "edge" probe.

### Ruled out (measured, not assumed)

* p95 warmup gate — `is_warmup_complete` is True for **99.3786%** of
  bars; only the first 8 days are gated.
* ms/ns mixing — the NB91 timebase fix is intact, `assert_ns` passes.
* conflict policy — never reached (`n_dropped_opposite == 0`).

### Per-gate attribution counters

The engine counts where each signal dies, so this never needs
guessing again:

```
n_signals_emitted
 - n_dropped_vol_state
 - n_dropped_p95_warmup
 - n_dropped_p95_warmup_sniper
 - n_dropped_policy
== n_immediate_filled + n_sniper_filled
```

`diag["gate_breakdown"]` has the breakdown; `diag["signals_unaccounted"]`
must be **0**.

**The p95 cap is a TP-ONLY clamp.** It must never gate entries — only
tighten TP1/TP2. The warmup gate is a separate, explicit concern.

Full write-up: `docs/nb94_1w_vs_1y.md`.

---

## The replay's zeroed `eod` legs — found and fixed 2026-11-04

**A diagnostic bug, not an engine bug** — but it is here because the
*class* of bug is the one this repo keeps paying for.

### The symptom

The trail MFE/MFA diagnostic's **reconciliation check (b2)** printed:

```
RECONCILIATION: d_gross $+66.6330  d_fees $+0.0000  -> *** MISMATCH ***
```

The replay's own trail-off baseline did not equal the engine's. The
one-month slice reconciled EXACTLY; the 8-month corpus did not.

### The root cause

`_replay_trail()` returns a **gross** alongside the exit price. Every
branch computed it correctly — `sl`, `tp`, `trail_stop` — except the
two **`eod`** branches, which returned a hard-coded `0.0`:

```python
# BEFORE (buggy) — the exit price was right, the gross was zeroed
if n == 0:
    return "eod", eod_px, 0.0
...
if best < 0:
    return "eod", eod_px, 0.0
```

An `eod` leg exits at the engine's `exit_px`, so its gross is
`(exit_px − entry_px) × side × qty` — **not zero**. The 8-month corpus
has **8 `eod` legs** with a combined gross of **−$66.63**, which is
**exactly** the reconciliation gap.

> The `eod` bucket is tiny (0.32% of legs) and its total is small
> (−$66 on 2,539 legs), so the error was **invisible in every headline
> number** — it only showed up in the reconciliation check. That is
> exactly what the check is for.

### Why the one-month slice missed it

2025-01 has **zero** `eod` legs (its exit reasons are `tp` 164,
`sl` 108, `contra_close` 12). The bug is **latent on short windows**
and only fires on windows that contain an end-of-day close. A one-month
validation slice is not a validation of this branch.

### The fix

```python
# AFTER (correct) — the gross is the engine's, in every branch
if n == 0:
    return "eod", eod_px, (eod_px - entry_px) * side
...
if best < 0:
    return "eod", eod_px, (eod_px - entry_px) * side
```

Reconciliation is now `d_gross $+0.0000  d_fees $+0.0000 -> EXACT`.

### The rule

> **A branch that returns a price MUST return the gross that goes with
> it.** A zeroed default in a P&L path is a silent-corruption field —
> the same class as `ClosedTrade.signal_bar = 0` in AGENTS.md § 1.6.

And the meta-rule, which is the one that actually caught it:

> **The reconciliation check is not a formality.** It is the only thing
> standing between a replay and a plausible-looking wrong number. It
> found a bug that had been sitting in a branch that a one-month
> validation slice could never exercise. **When a check fires, fix the
> diagnostic — do not explain the mismatch away.**

---

## The artifact-cache fingerprint hole — found and fixed 2026-11-04

**One nominal config produced TWO different books.** This is the
fifth defect class in this repo's history, and the most insidious
one yet, because it does not corrupt a number — it makes the SAME
number mean two different things.

### The symptom

The 5-event sequence whitelist was fitted on a **64,978-leg** ledger
and promoted. Two days later, the same canonical recipe — same
`build_engine_kwargs()`, same data, same dates — produced **37,159
legs** on a fresh ledger. The two ledgers could not both be right.

The discriminator was not a guess:

| ledger | built | `cache_hit` | vol-gate drops | legs | `trail_stop` in exit reasons? |
|---|---|---|---|---|---|
| `seqdrop_none` | Nov 3 22:30 | **True** | 53,568 | 64,978 | **no** |
| `seqwl_unfiltered` | Nov 4 02:37 | **False** | 90,620 | 37,159 | **yes** |

The second ledger was built with a **fresh artifact cache** — and it
had the trailing stop in its exit reasons. The first was served from
a **cached** artifact that predated the trailing stop going
canonical.

### The root cause

`bt/_engine/vol_state.py` builds the `vol_state` measure the engine
gates entries on. That measure is **pickled into the artifact cache**
as the `"vol_state"` key.

But the module was in **neither** fingerprint tuple:

* `runtime/sweeps/cached_sweep.py::_ARTIFACT_SOURCES`
* `runtime/diagnostics/leg_ledger.py::_source_hash()`'s file list

Both tuples deliberately exclude the engine's bar loop (it changes
often and cannot change a pickled zone set) — but `vol_state.py`
**does** change a pickled artifact. So:

> Editing `vol_state.py` never invalidated the cache. A stale
> `vol_state` kept being served. The engine's `_vol_state_pass`
> compared a threshold against a measure that had never been
> materialised in the cached artifact.

### The blast radius

The whitelist was fitted against the stale book, shipped, and then
re-measured against the fresh one. Only **14 of the 25 keys
survived** the re-fit. The promotion would have shipped a set
describing a strategy that no longer existed.

### The fix

`bt/_engine/vol_state.py` added to **both** fingerprint tuples.

### The rule

> **If a module's OUTPUT is pickled into the artifact, that module
> belongs in the fingerprint tuple.**

The tuples are not "the files that changed the recipe" — they are
"the files that changed the ARTIFACT". A module that produces a
pickled artifact and is missing from the tuple is a **stale-cache
generator**: every number produced through it is unreproducible.

### The companion bug — the signal-conservation chain

Found in the same session, in the same file. The engine's
`signals_unaccounted` diagnostic is required to be **0** (AGENTS.md),
and it is computed as `n_signals_emitted − (orders placed + drops)`.
The whitelist's drop counter — `n_dropped_seq_whitelist` — was
**not in that chain**, exactly like the structure gate was missing
before 2026-10-23.

With the canonical whitelist ON it drops ~109k of the ~118k emitted
signals, so omitting it made `signals_unaccounted` read as a
**massive leak** (a negative number whose magnitude is the
whitelist's drop count). **FIXED** — `n_dropped_seq_whitelist` is
now in `_accounted`, and `gate_breakdown` reports it.

---

## The dual-mode inverted_bar look-ahead — found and fixed 2026-11-03

**This is the fourth forward-leak class in this repo, and the most
expensive one so far** — it was worth **$2,811** over six monthly
slices, and it was systematically FAVOURABLE.

### The bug

`bt/_engine/vp_multi_target_engine.py`, the immediate co-fire step:

```python
# BEFORE (buggy)
if entry_mode == "dual" and int(zone.inverted_bar) >= 0:
    res.n_skipped_dual_inverted += 1
    continue
```

The INTENT is correct: a zone that has already inverted belongs to the
sniper path, so the immediate path must not double-fire it.

But `zone.inverted_bar` is a **whole-file verdict**. The detector
computes it over the ENTIRE dataset, and the engine then reads it at
the zone's **trigger bar**. If the zone inverted at bar `J`, the
immediate path skips the zone — even when `J` is thousands of bars in
the future of the trigger bar.

At the trigger bar, a live engine does not yet know that the zone will
invert at `J`. So the immediate book was a **selection on the
outcome**: zones that eventually invert were silently removed, and
zones that never invert were kept.

### The measurement

Detector-side, on 2025-01 (1.38M bars, 2,475 zones):

| quantity | value |
|---|---|
| trigger zones | 2,475 |
| skipped because `inverted_bar >= 0` | **723 (29.2%)** |
| — of which inverted **before** the trigger bar (correct) | **0** |
| — of which inverted **after** the trigger bar (**LOOK-AHEAD**) | **723** |
| lead = `inverted_bar − trigger_bar` | median **2,518** bars, p75 8,755, p90 30,015, max **200,769** |

So **100% of the skips were look-ahead**. The `lead` of 200,769 bars
is ~2.3 days — the zone inverted two days after it was supposed to
have been traded.

Book-side, six monthly slices, `dual` (as shipped) vs `immediate`
(no `inverted_bar` exclusion at all):

| slice | dual net | immediate net | delta |
|---|---:|---:|---:|
| 2025-01 | **+138.81** | −73.21 | −212.02 |
| 2025-04 | −1353.99 | −1671.59 | −317.60 |
| 2025-07 | −702.62 | −1208.47 | −505.85 |
| 2025-10 | **+494.68** | +7.49 | −487.19 |
| 2026-01 | −567.28 | −957.03 | −389.76 |
| 2026-04 | −2114.55 | −3013.60 | −899.05 |
| **SUM** | **−4104.94** | **−6916.42** | **−2811.48** |

Net-positive slices: **2/6** (as shipped) vs **1/6** (causal). Gross
per leg: **$0.0549** (as shipped) vs **−$0.0799** (causal).

### The fix

Compare the verdict against **the bar being traded**, not against zero:

```python
# AFTER (causal)
if entry_mode == "dual":
    _inv_b = int(getattr(zone, "inverted_bar", -1))
    if 0 <= _inv_b <= i:
        # Already inverted BY THIS bar -> the sniper path owns it.
        res.n_skipped_dual_inverted += 1
        continue
```

A zone that inverted at or before the trigger bar genuinely belongs to
the sniper path. A zone that inverts LATER is the immediate path's to
take — which is what a live engine decides at that bar.

The one-fire-per-zone guarantee is preserved by `zone_already_fired`:
when the zone later inverts, the sniper path finds it already fired
and skips it. A zone still fires exactly ONCE.

**Verification:** post-fix, `dual` equals `immediate` EXACTLY on every
slice (1088/1088, 3109/3109, 1613/1613, 3624/3624, 3158/3158,
3252/3252 legs). The two arms are now the same book, which is the
proof that no whole-file verdict is left in the immediate path.

### The rule

> **A zone's death/inversion bar is a whole-file verdict. Read it at
> bar `i` only by comparing it to `i`.**

This is the *same class* as NB94 (the one-sided rolling mask) and the
October 2025 `zone.live` fix — the third instance of it. The engine
now has two sanctioned causal helpers:

* `_zone_dead_by(zone, bar)` — the sniper path's causal death test.
* the `0 <= _inv_b <= i` comparison — the immediate path's.

### The guard

`tests/test_repo_invariants.py` now scans the engine's AST and fails
if ANY comparison reads `inverted_bar` / `superseded_bar` /
`played_out_bar` / `expired_bar` **without a bar on the other side**.
It follows local bindings (so `_inv_b = int(getattr(zone,
"inverted_bar", -1))` is resolved) and is scoped per function (so a
name reused for a different object cannot produce a false positive).

Both guards were validated by reverting the fix and confirming they
fail on the buggy form:

```
E  line 2826: inverted_bar compared against no bar -> int(zone.inverted_bar) >= 0
```

### Probes

| probe | what it measures |
|---|---|
| `runtime/diagnostics/probe_dual_mode_lookahead.py` | the detector-side skip counts and the `lead` distribution |
| `runtime/diagnostics/probe_dual_mode_pnl_impact.py` | the two-arm P&L on one slice |
| `runtime/diagnostics/probe_dual_mode_multislice.py` | the two-arm book across six slices (pre-fix) |
| `runtime/diagnostics/probe_dual_mode_postfix.py` | the same six slices after the fix |
| `runtime/diagnostics/record_dual_mode_lookahead.py` | writes both books to the results registry |

### Two sibling look-aheads that were CHECKED and are CLEAN

The audit that found the dual-mode bug also checked two neighbours:

1. **`fvg_retest_signals`'s `skip_inverted` exclusion** — CLEAN.
   Measured on 2025-01: all 690 zones the exclusion removed had
   inverted **before** their retest bar (`lead` max **−1**). The
   exclusion is causal. (This path is also not in the production
   pipeline — the engine drives entries from the zone maps, not from
   `fvg_retest_signals`.)
2. **`detect_fvg`'s deferred supersession** — the same *mechanism* as
   the dual-mode bug (`z.live` is a single scalar set from a future
   event) but **NOT** in the production path, because the engine does
   not gate trigger zones on `z.live`. It uses `_zone_dead_by(z, bar)`
   instead, which is causal. Measured: 1,623 supersession-killed
   zones would have produced 1,587 extra FVG retests, of which 1,397
   (88%) had their supersession *after* the retest — a look-ahead, but
   one the shipped engine does not commit.

---

## The 1m detector — three bugs fixed (2026-10-23/24)

The FVG detector moved from 1s to 1m on 2026-10-23, which changed the
median zone width from $0.47 to $1.04 (2.2×) and exposed three
defects that were invisible at 1s.

### Bug 1 — the resample bucketed by ROW POSITION, not by timestamp

```python
# WRONG — assumes a gapless feed: bar i is at second i
bucket = (np.arange(n) // resample_to_n_secs).astype(np.int64)
```

Real FX data has weekend/rollover gaps, so row position drifts away
from wall-clock time and every bucket boundary is wrong by the
accumulated gap. **15.018% of the 1y file's bars are >1s apart.**

The fix buckets by `floor((t − t0) / window)` on the actual
timestamps, and `times_utc_ns` is now **required** for a resample —
falling back to positional bucketing emits a `RuntimeWarning` rather
than silently corrupting every zone.

### Bug 2 — the invalidation walk ran at 1s while detection ran at 1m

The zone is DETECTED on the resampled cadence, but its invalidation
was evaluated on the RAW 1s arrays. Consequences:

* `invalidation_min_consecutive_bars=2` counted TWO **1-second** bars,
  not two 1-minute bars;
* `body_only_invalidation` tested 1s candle bodies, so a single 1s
  wick through the zone edge counted as a full-body pierce.

The walk now slices the **resampled** arrays and maps the result back
to 1s indices via `scan_index_map`.

### Bug 3 — supersession preempted inversion (THE BIG ONE)

**This is why the sniper count was 4.2/day against a ~30/day
expectation.**

Supersession was applied DURING detection (phase 2), and the
mitigation/inversion walk runs AFTER all detection (phase 3). So
during the supersession scan `z.inverted_bar` was **always −1**,
which made the escape hatch (`if z.inverted_bar >= 0`) **structurally
unreachable**.

**The fix defers the DECISION, not the record:**

* During detection, supersession **records** `superseded_bar` but does
  **not** kill the zone.
* The walk runs on every zone (superseded included), capped at
  `superseded_bar + 1`.
* **After** the walk, a zone is DEAD if it was superseded and never
  inverted; it stays **LIVE** if it inverted *before* the superseding
  zone formed. That is a legitimate iFVG — the sniper.

| slice | zones | superseded | inverted | snipers/day | vs target |
|---|---|---|---|---|---|
| 2026-01 | 5,189 | 94.4% | 1,270 | **41.0** | 137% |
| 2026-03 | 6,112 | — | 1,442 | **46.5** | 155% |
| 2025-05 | 4,583 | — | 1,119 | **36.1** | 120% |
| 2025-01 | 2,475 | — | 723 | **23.3** | 78% |

### Bug 1 was only HALF fixed

The 2026-10-23 fix changed `bt/_engine/ict_signals._resample_bucket_index`
to bucket by timestamp, and made `times_utc_ns` **required** — but
**three callers were never updated** to pass it:

| Caller | Impact |
| --- | --- |
| `runtime/diagnostics/directional_beta.py` | **The DECISIVE diagnostic** — computed on a DIFFERENT zone set than the sweep driver produced. |
| `runtime/diagnostics/cap_and_atr_audit.py` | The raw-TP decomposition. |
| `runtime/diagnostics/probe_vp_scale_inference.py` | The VP shape coverage sweep. |

The detector warned on every slice, but **nothing failed**. A warning
is not a guard.

**How badly the zone sets diverge** (1-month 2025-01, same bars, same
detector kwargs, only the bucketing differs):

| bucketing | zones |
| --- | --- |
| positional (wrong) | 2,136 |
| timestamp (correct) | **2,475** |

| overlap | count |
| --- | --- |
| shared (same price band + direction) | **194** |
| only in the positional set | 1,942 |
| only in the timestamp set | 2,280 |

**Only 194 of 2,475 zones (7.8%) are common to both sets.** The two
universes are effectively disjoint. The verdict did not change, but
the numbers moved materially.

> **Rule: when a warning fires in a diagnostic that produces a
> PROMOTION VERDICT, the diagnostic is not trustworthy until the
> warning is gone.** A verdict computed on a differently-bucketed
> universe than the sweep driver is not a verdict about the recipe.

**Guard:** `tests/test_regressions.py::test_every_detect_fvg_call_site_passes_times_utc_ns`
walks every non-test `detect_fvg` call site via AST.

---

## Dead knobs — verified 2026-10-20

Knobs that look live but cannot change the result. **Do not sweep
these; they will silently no-op.**

| Knob | Why it is dead |
| --- | --- |
| `commission_per_lot_usd`, `spread_per_oz_usd` (engine surface) | `bt.vp_engine._FOREX_ONLY_KWARGS` strips them before the engine call. Their effect is folded into `taker_fee_bps` at build time. |
| `sl_atr_mult` | **REMOVED from the sweep surface 2026-10-22.** Doubly dead: canonical `0.0` *and* no `atr=` array is ever passed. A 1s-cadence ATR is **$0.098 median**, so expressing a $5 stop needs `mult ≈ 51` — a constant with no cross-regime meaning. |
| `poc_buffer_usd` | Only read by `compute_structural_sl` when `sl_mode == 'fvg_or_poc'`. Canonical is `hvn_lvn`. |
| `hvn_buffer_usd` | **LIVE** — canonical `sl_mode='hvn_lvn'` reads it (the offset past the shelf edge). |
| `partial_close_pct` | Was hardcoded to 0.5 inside `compute_structural_tp`. **Fixed** — now a real engine parameter. **INACTIVE under the canonical `tp_mode='hvn_single'`** (the mode returns `0.0`). |
| `tp2_mode`, `tp2_nth_hvn_n`, `tp2_swing_cap` | **INACTIVE under the canonical `tp_mode='hvn_single'`** — there is no runner leg. Live only for the `multi_target` A/B. |
| `strict_wick_required` | Ships **OFF**. The borrowed floor ($0.68) matches **0.00%** of XAUUSD 1s windows — enabling it would delete the whole strategy. It is **real and sweepable**, just off. |
| `fvg_body_only_*` | Was silently inert (resolved to the dataclass default `False` and the gate never ran). Now live and wired from the canonical constants. |
| `vol_state_min_vel_15m`, `vol_state_min_rv_ratio_30s`, `vol_state_max_rv_ratio_30s`, `vol_state_max_wick_ratio` | Were **CRASHING** (not no-opping) because the driver never built their measures — `KeyError` in `_vol_state_pass`. **Fixed 2026-11-03**: the driver builds every measure. Still **unswept**, so their effect on P&L is unknown. |

`tests/test_regressions.py::test_all_engine_kwargs_reach_the_engine`
and `::test_engine_accepts_every_allowlisted_knob` guard the engine
surface against re-drift.

---

## Known engine bugs and their fixes (chronological)

### Two `sl_mode` branches ignored the SL floor and/or ceiling (2026-10-24)

| branch | floor (`sl_min_usd`) | ceiling (`sl_max_usd`) | before |
|---|---|---|---|
| `hvn_lvn` (canonical) | applied | applied | OK |
| `fvg_only` | **MISSING** | applied | partial |
| `fvg_or_poc` | **MISSING** | **MISSING** | **broken** |

`fvg_or_poc` returned its raw anchor level directly, so it ignored
BOTH bounds — a `sl_mode` sweep would have shown a `$19.50` stop in
that cell while its two siblings sat at `$2.50`.

> **Rule: a bound that is enforced in one branch of a switch must be
> enforced in every branch.**

Guard: `tests/test_smoke.py::test_every_sl_mode_honours_the_floor_and_the_ceiling`.

### The callback path crashed on `VolState` (2026-10-25)

`on_bar_callback` called `vol_state.items()`, but the canonical config
hands the engine a **`VolState` object** (which has `__getitem__` /
`get` but no `items()`). The callback raised `AttributeError` on the
FIRST bar.

It went unnoticed because `on_bar_callback` had **zero production
callers** — the branch was dead code. Same class of defect as § "Bug
1 was only HALF fixed": **a guard that lives in the callee but was
never exercised by any caller.**

Guard: `tests/test_contra_close.py::test_on_bar_callback_survives_a_vol_state_object`.

### The `contra_close` branch dropped `signal_bar` (2026-10-25)

**This one is a silent data-corruption bug, and it only became
visible when the book got wide.**

`ClosedTrade.signal_bar` **defaults to `0`**. Every exit path in the
engine passes its leg's own signal bar — except the **`contra_close`
netting branch**, which built a `ClosedTrade` without the field. So
every netted leg reported `signal_bar == 0`, i.e. "this trade signalled
at the very start of the corpus".

| book | legs | `contra_close` legs | legs with `signal_bar == 0` |
|---|---|---|---|
| narrow (vol gate 3.0) | 1,692 | ~93 | ~93 (5.5%) |
| **wide (vol gate 1.0)** | **51,075** | **17,211** | **17,211 (34%)** |

On the narrow book it was a rounding error. On the wide book it
corrupted the ledger's **signal-time index** for a third of the book:

* the mean pre-fill window read **9,490,055 bars** (23% of the whole
  corpus) instead of ~4,000;
* 17,200 legs showed a pre-fill window of up to 41M bars — the entire
  corpus — when their real wait was a few thousand bars;
* the random-entry control's confound measurement was computed off
  the corrupted index.

The fix is one line — pass `signal_bar=_ct.signal_bar` — plus the
guards. After the fix the mean pre-fill window is **4,659 bars**, and
the netted legs' pre-fill mean (5,574) matches the non-netted
baseline (4,194).

> **Rule: a field with a DEFAULT is a silent-corruption field.**
> `ClosedTrade` has `signal_bar: int = 0`, `zone_width: float = 0.0`,
> `zone_low`/`zone_high: float = 0.0`. Any construction site that
> forgets one produces plausible-looking numbers that are simply
> wrong — no crash, no warning. **Walk every construction site and
> assert the required fields are passed.**
>
> The second half of the rule: **a bug's blast radius scales with
> how often its code path runs.** The same omission was harmless at
> 93 legs and corrupted 17,211. When a knob widens the book, re-run
> the invariants — the bug was always there, only the exposure
> changed.

Guards: `tests/test_signal_bar_completeness.py` — a source-walking
test that every `ClosedTrade` construction site passes `signal_bar`,
plus two ledger-level end-to-end checks (`signal_bar == 0` count, and
the pre-fill-window backstop).

### An INCOMPLETE refactor left the repo broken (2026-11-03)

**The symptom.** Nine tests failed at once with
`NameError: name 'PARTIAL_CLOSE_PCT' is not defined` and
`ValueError: Unknown tp_mode='hvn_single'`. A concurrent editing
session had removed the multi-target constants from
`bt/optimal_config.py` while rewriting the TP-geometry block to
"single-target HVN" — but it had **not** taught the engine the new
mode, and it had **deleted the constants the builder still read**.

**What was half-done.** Three things were changed together and only
the first was finished:

| Change | Done? |
|---|---|
| `TP_MODE` → `'hvn_single'`, a new "one HVN target" exit mode | **NO** — the engine still branched on `TP_MODE_FVG_MULT` / `TP_MODE_MULTI_TARGET` and raised `ValueError` on anything else |
| `PARTIAL_CLOSE_PCT` / `TP2_MODE` / `TP2_NTH_HVN_N` / `TP2_SWING_CAP` deleted from the config | **NO** — `build_engine_kwargs` still forwarded all four, and the engine still accepts them |
| A trailing stop (`TRAIL_MODE` / `TRAIL_ATR_MULT` / `TRAIL_ACTIVATION_MULT` / `TRAIL_GIVEBACK_FRAC`) and an ATR-scaled SL band (`SL_MIN_ATR_MULT` / `SL_MAX_ATR_MULT`) | **NO** — the constants were added to the config and to the allowlist, but **the engine never reads them**. They were pure DEAD KNOBS, and the allowlist made them *passable*, which is worse than absent |

**Why it was caught.** The repo's own guards did their job:
`test_no_canonical_constant_is_unread` flagged the unread constants,
`test_all_engine_kwargs_reach_the_engine` flagged the allowlist
entries the engine does not accept, and `test_engine_accepts_every_
allowlisted_knob` flagged the mismatch. Nine tests, all loud.

**The first fix — revert.** The config was brought back into
agreement with the engine: `TP_MODE = 'multi_target'` (the mode the
engine implements), the four multi-target constants restored, the
allowlist restored to the engine's real signature, and the
unimplemented trailing-stop / ATR-band constants **removed** — their
design kept as a comment block so it is not lost.

**The second fix — implement it properly (2026-11-03, later the
same day).** Reverting the config threw away the *intent*; the mode
was worth having. The engine now implements `hvn_single` for real:

| layer | what landed |
|---|---|
| `bt/_engine/vp_multi_target_config.py` | `TP_MODE_HVN_SINGLE = "hvn_single"` and a `TP_MODES` tuple |
| `bt/_engine/vp_multi_target_engine.py` | a `TP_MODE_HVN_SINGLE` branch in `compute_structural_tp`: ONE target on the `nth_hvn_n`-th HVN shelf, `partial_close_pct=0.0`, no TP2 |
| `bt/optimal_config.py` | `TP_MODE = 'hvn_single'`, with the geometry argument in the comment |

The multi-target constants (`PARTIAL_CLOSE_PCT` / `TP2_MODE` /
`TP2_NTH_HVN_N` / `TP2_SWING_CAP`) were **kept** — they are still
live for the `multi_target` A/B. The trailing-stop / ATR-band
constants remain **out** of the config, because the engine still
does not read them; their design stays as a comment block.

**Verified.** The one-month A/B on 2025-01 shows the expected exit
shape — `hvn_single` produces a **single** `tp` bucket (413) where
`multi_target` produces `tp2` 367 + `tp1` 46. See
`docs/promotions.md`.

> **Rule: a config constant the engine never reads is a DEAD KNOB.**
> Adding it to the allowlist makes it *worse*, because the knob then
> looks sweepable. A sweep of it reports a "canonical" cell that
> measured nothing — the exact failure mode this repo has already
> paid for twice (`sl_atr_mult`, the `TrendStrategyParams` recipe).
>
> **Rule: an engine mode name is an API, not a string.** A new exit
> mode needs (1) the engine branch, (2) the mode constant in
> `bt/_engine/vp_multi_target_config.py`, (3) the config value, and
> (4) a test that reaches it. Changing (3) alone fails the whole
> book, not one leg.
>
> **Rule: when several changes land together, land them in the order
> that keeps the repo green.** The engine first (it is the
> constraint), then the config, then the builder, then the allowlist,
> then the tests. A refactor that ends in a state where the canonical
> recipe cannot run has not finished — it has just moved the failure.
>
> **Corollary — a revert is not a resolution.** The first fix made
> the repo green by **deleting the feature**. That is a legitimate
> emergency action, but it is a *stopgap*, not a decision: the
> question the feature was asking ("does one target beat a 50/50
> split?") was still unanswered, and the answer was still
> obtainable. The second fix landed the engine branch.

---

### A `vol_state` threshold for an unmaterialised measure raised `KeyError` (2026-11-03)

**The symptom.** Sweeping `vol_state_min_vel_15m` — a knob that is on
the allowlist, is forwarded by the builder, and is listed in the
canonical config — **crashed** with a `KeyError` deep inside the
engine's `_vol_state_pass`.

**Root cause.** The sweep driver built only three `vol_state`
measures:

```python
measures=("vel_5m", "range_norm_1m", "wick_ratio")
```

and `_vol_state_pass` reads the measure it gates on **by key**:

```python
ok = ok and float(vol_state["vel_15m"][i]) >= min_v15
```

Any threshold for a measure that was never built raised `KeyError`
inside the bar loop.

**Why it is worse than a dead knob.** A dead knob returns a
plausible number, so the sweep reports "no effect" and the reader
moves on. This one **threw**, so the natural response was to
distrust the *sweep driver* rather than to notice the missing
measure. Two files had the same defect
(`runtime/sweeps/cached_sweep.py` and
`runtime/diagnostics/directional_beta.py`).

**The fix.** Both call sites now build the **full** set —
`vel_5m`, `vel_15m`, `range_norm_1m`, `wick_ratio`, `rv_ratio_30s`,
`shock`, `vol_expand_prev_day` — at a cost of **~0.7 s per month**.
`vol_expand_prev_day` additionally requires `times_ns`.

> **Rule: a knob is live only if the engine reads it AND the measure
> it gates on is materialised.** The allowlist checks the first
> half; nothing checked the second. The canonical config lists
> seven vol thresholds and the driver built three of their measures.

---

### The day-expansion measure had no timestamp to expand against (2026-11-03)

`build_vol_state` took only OHLC arrays — no clock. The new
`vol_expand_prev_day` measure needs the calendar-day boundary and
the elapsed fraction of the day, so `build_vol_state` gained a
`times_ns` parameter (int64 epoch **nanoseconds**, per the repo
convention — `to_ns_arr(bars)`).

Requesting the measure **without** `times_ns` **raises** rather than
silently returning `1.0`:

```python
if "vol_expand_prev_day" in _want_fp:
    if times_ns is None:
        raise ValueError(
            "build_vol_state: 'vol_expand_prev_day' requires "
            "times_ns (int64 epoch nanoseconds, ascending)."
        )
```

The cache key also fingerprints the timestamps **when that measure is
requested** — otherwise a build over the same OHLC with a different
day alignment would return a stale ratio.

> **Rule: a measure that needs a clock must get one, and must
> refuse to run without it.** A silent `1.0` would be a gate that
> never fires, which reads exactly like "the knob does nothing".

---

### A partial forex day became the denominator of the expansion ratio (2026-11-03)

The corpus has **792 calendar days** but only ~52,000 seconds of
trading per day (24h would be 86,400), and the **weekend days are
partial** — Sunday's session is ~1,235 seconds, Friday's ends early.

Without a guard, Monday 10:00 was compared against **Sunday 23:59**'s
tiny cumulative RV, and the ratio exploded to ~30× on no real
signal. The fix emits `1.0` ("no opinion") when the previous day
did not trade up to the same elapsed fraction:

```python
prev_covers = prev_frac[-1] >= cur_frac
good = (den > 1e-18) & prev_covers
```

> **Rule: a baseline drawn from a different session is not a
> baseline.** Compare like with like — same elapsed fraction, same
> session coverage — or emit "no opinion" rather than a wild number
> that trips a threshold gate spuriously.

---

## Performance — the 1y detector and engine

### Bottleneck A2 — the 1m resample aggregation O(buckets × bars) (FIXED 2026-10-25)

The detector's resample step aggregated the 1s bars into 1m bars
with one **full-length mask per bucket**:

```python
for ub_idx, ub in enumerate(unique_buckets):
    mask = bucket == ub   # <-- full-length comparison, PER BUCKET
    scan_high[ub_idx] = float(high[mask].max())
```

On the 1y corpus: 687,228 buckets × 41,233,730 bars = **28.3
TRILLION** element-ops → **~8 hours**. It was invisible on the
monthly slices (the 1-month corpus is 1/30th the size) and
**completely invisible to the byte-identity checks**, which all ran on
1-month data. The 1y detector had never actually been run end-to-end
at 1m cadence.

**The fix** — `np.maximum.reduceat` / `np.minimum.reduceat`. This is
only valid because `_resample_bucket_index` returns
`floor((t - t0) / window)` on a **non-decreasing** timestamp series,
so the bucket index is **monotonic** and every bucket is a
**contiguous segment** of rows.

**Measured:** 4M bars aggregate in 0.037s (108M bars/s) → the 1y
corpus's aggregation step is **0.4 s** instead of **~8 hours**.

**Equivalence is proven, not assumed:**
`tests/test_detector_cadence.py::test_resample_aggregation_matches_the_loop`
compares the vectorised form against the ORIGINAL loop on both gapped
and gapless synthetic data, and
`::test_resample_bucket_index_is_monotonic_so_reduceat_is_valid`
asserts the monotonicity/contiguity property the fix depends on.

> **Rule: a per-bucket mask over a full-length array is O(n²).** When
> the bucket index is monotonic, use `reduceat`. When it is not, sort
> first — never mask per bucket.

### The other detector bottlenecks (all fixed 2026-10-21)

| # | Phase | Root cause | Before | After |
|---|---|---|---|---|
| A | Python bar loop | 41M Python iterations | 100+ s | **0.06 s** |
| B | Supersession O(Z²) | scan all prior zones per new zone | 15.7 h | ~0.3 s |
| B2 | Unbounded live-candidate list | never-overlapped zones accumulate | ~20 min | **145.2 s** (1y) |
| C | Mitigation walk to EOF | non-sup zones walk to `n_1s` | 10+ min | ~25 s |
| D | Structure invalidation O(N×Z) | all bars × all zones | unknown | <1 s |
| F | VP engine O(n_bars × open) | unbounded book (mean 124 open) | ~hours on 1y | **~51 s** (1y) |

**B2's fix** was a **price-bucket spatial index**: the supersession
predicate is a *price-range overlap* test, so a zone can only be
superseded by a candidate whose price range overlaps it. This is
**exactly equivalent** — the overlap test is untouched, only the set
of zones considered is narrowed. Verified byte-identical on 1-month
data.

**F's fix** was three changes together: gate the per-bar book scan
behind its actual consumers (P2), inline `ot.qty_of(qty_oz)` (P3),
and compact `open_trades` in place (P4). The book is now capped at 20
by D3, and the engine runs at ~71,000 bars/s.

> **Re-measured 2026-10-25 on the current recipe (1m detector + vol
> gate): ~730,000–790,000 bars/s, max open depth 2, 1y engine cell
> 51.0 s.** The ~71,000 bars/s above is the 1s-detector figure.

### Memory: where the 1y working set actually goes (2026-10-22)

| Structure | Size | Grows during the run? |
| --- | --- | --- |
| bars frame (parquet columns) | 2,202.1 MB | no — static |
| engine float64 arrays (o/h/l/c + time ns) | 1,572.9 MB | no — static |
| zone list (1.82M `FvgZone` objects) | 647.3 MB | no — static |
| closed trades (80,833 `ClosedTrade`) | 46.7 MB | **yes** — the only unbounded one |
| equity curve (float64, 41M) | 314.6 MB | no — disabled above 8M bars |
| **total accounted** | **4,783.7 MB** | |

**The answer to "why does memory grow if backtests are sequential and
zones expire?"** — it mostly *doesn't*, and zone expiry is irrelevant
to it:

1. **Zones do NOT expire from memory.** `detect_fvg` returns a list
   of 1.82M `FvgZone` dataclass objects. The engine never deletes
   them; a zone is "expired" logically but the object stays alive.
2. **The bars frame is the floor, not the growth.** 1.98 GB is
   irreducible at 41M rows × 6 columns × 8 bytes.
3. **Only the closed-trade list grows without bound** — 606 bytes
   per trade.
4. **The cache does not help memory at all.** It helps *time*, but the
   artifacts are **unpickled into RAM** for every cell.

`load_bars` used to build a redundant 330 MB `volume` column.
Pandas column assignment always copies, so it is now a **rename**
(`df.rename(columns={"tickv": "volume"})`) — a label-only change,
zero extra bytes.

### Numba: measured, and REJECTED (2026-11-04)

A Numba kernel for the O(n_bars × n_open) loop was the original
plan. It is not the right lever any more. After D2 + D3 + P2–P4 the
book depth is capped at 20 and the scan runs at ~71k bars/s on 1y.

> **Re-measured 2026-10-25 — Numba is even less needed.** The 1m
> detector plus the vol gate moved the 1y bar rate to
> **~730,000–790,000 bars/s** and the **measured max open depth to
> 2**. A 1y engine cell is **51.0 s** and a 60-cell 1y sweep is
> **~51 min** cold (or **~60 min** with the artifact cache warm).
> Numba would optimise a loop that is already ~99% idle.

If the sweep matrix grows past ~60 cells, revisit Numba. Below that,
the effort is better spent on the knobs.

> ### ✅ RE-MEASURED 2026-11-04 — Numba was measured, and it LOSES.
>
> The question was re-opened on the current recipe (trailing stop +
> `hvn_single` + the 21-key whitelist). **Numba was benchmarked, not
> dismissed.** The result: the two places a Numba kernel *could* go
> are both better served by **vectorised NumPy**, and the one place
> Numba would have been a clear win is **already 99% idle**.
>
> **Q1 — what would a jitted bar loop actually buy?**
>
> A Numba-jitted bare bar scan (the shape of the engine's loop: a
> trigger-list walk plus a scalar SL/TP test) runs at **279,184,658
> bars/s** vs **3,306,044 bars/s** for the Python equivalent — **84.4×**
> on the scan alone. But the scan is **not** the engine:
>
> | component | time (1-month slice, 1,377,190 bars) |
> |---|---|
> | bare scan, Numba | **4.9 ms** |
> | bare scan, Python | 416.6 ms |
> | **the actual engine** | **2,994 ms** |
>
> The engine is **7.16×** the bare Python scan. So the Numba scan is
> **4.9 ms of a 2,994 ms engine** — the scan is **0.16%** of the work.
> A fully-jitted engine would still have to do everything else in
> Python (the `OpenTrade` objects, the dataclass construction, the
> `ClosedTrade` appends, the caps' `searchsorted` lookups), because
> Numba cannot own Python objects. **Realistic ceiling: ~1.16×**, and
> only if every one of those object operations were also rewritten.
>
> **Q2 — why the scan is already idle.** The engine's per-bar work is
> gated on events that are **rare**:
>
> | event | bars | share of the corpus |
> |---|---|---|
> | zone trigger | 2,475 | **0.180%** |
> | zone inversion | 699 | **0.051%** |
> | union (entry-relevant) | 3,050 | **0.221%** |
>
> **99.78% of bars do nothing.** The engine's book depth is
> **max 2** on the canonical slice. This is the D3 + vol-gate +
> whitelist story: the recipe itself made the loop idle. There is
> nothing left for a compiler to optimise.
>
> **Q3 — the trailing stop does NOT change this.** The concern was
> that a stateful trailing exit (peak tracking, arming, ratcheting)
> would break the "fixed SL/TP" assumption that makes the loop
> vectorisable. **It does not** — because the trail only runs on
> **open positions**, and there are at most **2** at a time. The
> trail's state lives on the `OpenTrade` object, not in a
> per-bar array, so it is O(open) not O(bars). The trail's
> contribution to the profile is **14,309 calls to
> `_trail_level_usd` = 0.001% of the bar loop**.
>
> **Q4 — the ONE place Numba looked right, and why it still lost.**
> `vol_state._rolling_median_baseline` was a **20.8 s** Python loop
> (the single most expensive artifact in the 1y build). A hand-written
> Numba kernel for it:
>
> | implementation | 1y (1,333,415 buckets, win=60) | speedup |
> |---|---|---|
> | Python loop (original) | 20.15 s | 1.0× |
> | **Numba kernel** | **1.60 s** | **12.6×** |
> | **vectorised NumPy (`sliding_window_view` + `np.median`)** | **1.07 s** | **18.8×** |
>
> **NumPy beat Numba by 1.5×**, and it needs no compiler, no cache
> directory, and no warm-up. The vectorised form is **exactly
> equivalent** (`np.array_equal`, not `allclose`) — see PERF P6 below.
>
> **VERDICT: no Numba.** Two vectorised-NumPy changes bought
> **1.50×** on a 1y cell (352.0 s → 234.5 s) for a fraction of the
> risk. Numba would have bought at most ~1.16× on the engine, **0×
> on the artifacts** (they are already vectorised), and would have
> cost a compile dependency in a repo whose whole value is
> reproducibility.
>
> **The lever was never the compiler. It was the two Python loops
> hiding inside a vectorised-looking module.**

### PERF P5 — `memoryview` instead of numpy scalar boxing (2026-11-04)

The bar loop reads **five scalars per bar**. `float(arr[i])` boxes a
fresh numpy scalar on every read — 5 boxes × 41M bars on 1y.

Measured on the 1-month slice (1,377,190 bars, five arrays read):

| read pattern | time | rate | extra RAM on 1y |
|---|---|---|---|
| `float(arr[i])` (original) | 709.9 ms | 1.94M bars/s | 0 |
| `arr.tolist()` | 311.8 ms | 4.42M bars/s | **+990 MB/array (~5 GB)** |
| `arr.item(i)` | 547.2 ms | 2.52M bars/s | 0 |
| **`memoryview(arr)`** | **270.2 ms** | **5.10M bars/s** | **0** |

`memoryview` is a **VIEW**, not a copy — it costs 0 extra bytes, so it
is the only variant affordable on 1y (the engine already peaks above
4 GB; `.tolist()` would add ~5 GB).

It yields the **same** Python scalars: a float64 memoryview read gives
a Python `float`, an int64 memoryview read gives a Python `int`. The
values are bit-identical.

**End-to-end A/B** (same machine, same session, 5 reps each):

| variant | times | median | best |
|---|---|---|---|
| `float(arr[i])` | 2.536 / 2.522 / 2.878 / 2.838 / 2.897 s | **2.838 s** | 2.522 s |
| `memoryview(arr)` | 2.552 / 2.207 / 2.209 / 2.221 / 2.331 s | **2.221 s** | 2.207 s |

**1.28× on the engine**, with `gross` **bit-identical**
(48.2183186474 on the 2025-01 slice).

### PERF P6 — vectorised `_rolling_median_baseline` (2026-11-04)

`vol_state._rolling_median_baseline` was the **single most expensive
artifact** in the 1y build (**20.8 s** of 92.6 s). It is now a
**vectorised sliding window**:

```python
sw = sliding_window_view(per_bucket, win)   # every FULL window in one view
out[start + win - 1 : end + win - 1] = np.median(sw[start:end], axis=1)
```

The first `win - 1` buckets use a **short** window (length `k + 1`)
and are computed separately; only buckets `>= win - 1` have a
full-length window. Chunked at 100,000 so the temporary stays bounded.

**Exactly equivalent** — verified with `np.array_equal` (not
`allclose`) against the original loop across a sweep of
`(nb, win)` shapes: `nb ∈ {0, 1, 2, 3, 59, 60, 61, 62, 100, 1000,
5000}` × `win ∈ {1, 2, 3, 60, 61, 120}` — **0 mismatches**, plus
zero/constant/random edge cases.

> **Rule: `np.median` over a Python loop is a vectorisation, not a
> micro-optimisation.** The loop was O(n_buckets × win) with a Python
> function call per bucket. `sliding_window_view` makes the windows a
> VIEW — no copy — and `np.median(axis=1)` runs the partition in C.

### The net effect of P5 + P6 (2026-11-04)

| stage | 1y artifact build | 1y engine | **1y cell total** |
|---|---|---|---|
| before | 197.8 s | 154.2 s | **352.0 s** |
| after | 130.5 s | 104.0 s | **234.5 s** |

**1.50×**, with the canonical result **unchanged**: the 1y corpus
returns **6,663 trades** and **gross +$2,583.18** — the same figure
the canonical recipe has always reported.

---

## The per-bucket `excess` was measuring fill geometry — found 2026-11-04

**This is not an engine bug — the engine is right. It is a MEASUREMENT
bug, in the metric, and it had been gating promotions.**

### The symptom

`tests/test_signal_bar_completeness.py::test_excess_is_negative_for_every_exit_reason`
asserted that **every** exit bucket with n ≥ 100 has a **negative**
`excess`. Under the promoted `trail_sides='short'` recipe it failed on
the `sl` bucket, which read **+$134.24**.

The instinct was to relax the test. **That would have been wrong**, and
the reason why is the whole point of this entry.

### Why the `sl` bucket's excess is positive

`excess = gross − drift`, where `drift` is a **close-to-close**
passive hold. But the engine books P&L at the **actual fill price**.
Measured on the promoted book (7,122 legs):

| exit | n | fills exactly at the level | off-level fills |
|---|---|---|---|
| `sl` | 2,665 | **85.9%** | 14.1% — **100% of them ADVERSE** (beyond the stop) |
| `tp` | 2,006 | 80.7% | 19.3% fill **better** (a gap-through fills a resting limit) |

And on **71.6%** of the `sl` legs the exit bar's **close is BEYOND the
stop** — the bar gapped through the level.

So for a stop exit the realised price and the bar's close are
**different prices by construction**. Which gives an exact identity
(verified to **1e-15** in every bucket of every book):

```text
excess == gap@exit − gap@entry
        where gap = (fill_price − bar_close) · side · qty
```

**The `sl` bucket's `excess` is the EXECUTION-GAP DIFFERENTIAL, not a
timing measure.** A stop fill and a close-to-close benchmark are
**not exchangeable** — exactly the confound AGENTS.md § 5.1 rule 4
warns about for the random-entry control ("a control must be
EXCHANGEABLE with what it controls for").

### The tell: the sign FLIPS with book composition

| book | `sl` excess | `sl` t |
|---|---|---|
| scope `'both'` | −$42.30 | −1.52 |
| scope `'short'` (promoted) | **+$134.24** | +3.13 |
| scope `'long'` | −$36.50 | −1.15 |
| trail OFF | +$116.66 | +2.62 |

A property of the **strategy** does not flip because the book
composition changed. A property of the **fill geometry** does.

### The fix

The per-bucket assertion was replaced by the forms that DO hold, and
which have **no fill-price/close divergence** to confound them:

1. the **whole-book** excess (negative: **−$458.39**),
2. the **per-side** excess (negative: **−$198.80** long, **−$259.59**
   short) — these pool every exit reason, so the gap differential
   averages out,
3. every exit bucket with **no fill-price divergence**
   (`contra_close`, `trail_stop`, `replaced_by_new_signal`) — these
   have no confound in them at all.

A NEW guard locks the mechanism itself:
`tests/test_signal_bar_completeness.py::test_pnl_is_booked_at_the_actual_fill_price_not_the_close`
— it asserts that stop exits fill at-or-beyond the stop (never
favourably) and that target exits fill at-or-better than the target.

### The rule

> **A benchmark must be EXCHANGEABLE with the execution model it
> benchmarks.** A close-to-close passive hold is not exchangeable with
> a book that books P&L at the stop/target fill price. When the two
> differ, the difference is **arithmetic**, and a metric built on the
> difference measures the arithmetic.

And the corollary, which is the actual promotion rule:

> **A promotion is judged on NET PROFIT.** The passive-hold/excess
> decomposition and the daily direction-dependence regression are
> **DIAGNOSTICS**. They explain a result; they do not gate it.
>
> ⛔ The **monthly** beta regression it originally named is **RETIRED**
> (2026-11-04): n = 13..31 points put every quoted correlation inside
> its own noise band. See AGENTS.md § 1.1 rule 7.

### The second bug this exposed — the driver's arms

`runtime/diagnostics/trail_sides_ledger_verdict.py` built its
"canonical" arm from the **no-overrides** path. After the promotion
the canonical recipe **IS** `trail_sides='short'`, so the driver
would have compared the book against **itself** — a comparison that
measures nothing and reads like a verdict.

Every arm now names its scope **explicitly** (`trail_sides='both'`,
`'long'`, `'short'`, and `trail_mode='off'`), so the driver keeps
measuring the grid after any change to the canonical value.

> **Rule: a comparison driver's arms must be EXPLICIT OVERRIDES, not
> "whatever the config currently is".** A driver that reads the
> canonical value has no fixed reference, and it silently becomes a
> self-comparison the moment the canonical value changes.

---

## Operational gotchas

### A concurrent 1y run makes the test suite look RED (2026-11-04)

**The trap.** `tests/test_no_dead_imports.py::test_every_module_imports_cleanly`
imports **every** module in the repo in **ONE** process. A corpus run
holds ~6 GB while it works. Run them together and the import guard
OOMs — and the suite reports a failure that is **not** a code defect.

**What it looked like.** On 2026-11-04, with a `trail_sides_corpus_ab`
run in flight, the suite reported:

```
FAILED tests/test_no_dead_imports.py::test_every_module_imports_cleanly
1 failed, 434 passed, 1 skipped in 394.01s (0:06:34)
```

The same tree, four minutes later, with the machine to itself:

```
435 passed, 1 skipped in 27.94s
```

**The rule.** AGENTS.md § 7 already says *"never run two heavy cells
concurrently"*, but the consequence was not stated: **a concurrent run
makes the suite lie.** Before believing a red suite, check for a run
in flight:

```bash
powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object ProcessId,CommandLine"
```

**And the converse trap:** a suite that takes **minutes** instead of
**~28 seconds** is itself the symptom. The 394 s run above was not a
slow machine — it was a machine with 6.7 GB free and two 6 GB
processes on it.

> **This is why the six "KNOWN RED" failures of 2026-11-04 were
> listed as a known baseline.** A known-red baseline is only
> meaningful if the suite is run on a quiet machine; otherwise the
> baseline itself is contaminated.

### A comment edit invalidated seven corpus ledgers (2026-11-04)

**The trap.** The source fingerprint that decides "is this ledger
still current?" was a SHA1 over the **raw bytes** of the
artifact-producing sources:

```python
h.update((_REPO_ROOT / rel).read_bytes())
```

So **any** edit to those files invalidated the ledger — including a
comment. On 2026-11-04, updating the measurement comment block in
`bt/optimal_config.py` (the file that holds the canonical values
*and* their documentation) invalidated **seven** corpus ledgers that
had just been measured, each at roughly four minutes.

**Why that is worse than a wasted rebuild.** The promote workflow is
**change the config → re-measure → document the result**. With a
raw-bytes hash, the *documenting* step changes the hash that says
the measurement is current — the loop destroys its own evidence. And
the file most likely to be edited is `bt/optimal_config.py`, which
holds the canonical values **and** the table explaining them.

**The fix.** The fingerprint is now over the **AST-normalised**
source (`ast.dump(..., include_attributes=False)`), shared by all
three hash sites (`leg_ledger`, `cached_sweep`, `results.registry`):

```python
h.update(ast.dump(ast.parse(src), annotate_fields=True,
                  include_attributes=False).encode("utf-8"))
```

A comment, a docstring's prose, or a whitespace change **does not**
move the hash. Every executable construct — names, literals, control
flow — does. A file that does not parse falls back to raw bytes, so a
partial file still registers a change.

> **Rule it exposes.** A cache key must track **behaviour**, not
> **bytes**. Bytes are a proxy for behaviour that is too coarse: it
> invalidates on edits that cannot change the answer and (worse)
> gives a false sense of safety on edits that can.

Locked by `tests/test_results_registry.py::test_source_fingerprint_ignores_a_comment_only_edit`
and `::test_source_fingerprint_detects_a_behaviour_change`.

### Diagnostic logging

The engine's bar loop reports progress to **stderr** every 250,000
bars:

```
[vp_engine] bar=1250000/41233730 elapsed=51.9s rate=24091/s open=24
```

The `open=` field is the key diagnostic — it is the multiplier on the
total runtime, because the per-bar book scan is
`O(n_bars × n_open)`. A run whose `open=` count is flat and low is
fast; a run whose `open=` grows into the hundreds is the wall.

Run with `python -u` so the output is unbuffered.

### Memory contention kills 1y runs silently

**Observed 2026-10-21.** Two `cached_sweep` 1y runs were accidentally
left running at the same time. Each holds ~6.4 GB resident. Together
they exhaust physical memory and the effect is two-fold:

1. **The bar loop stalled for 2.3 hours.** The `elapsed` counter
   jumped discontinuously:

```
bar=14000000 elapsed= 1036.6s rate=13506/s  <- healthy
bar=14250000 elapsed= 9341.1s rate= 1526/s  <- 2.3 h stall
```

2. **The run was then killed outright** before finishing, with no
   error in the log.

**Rule: never run two 1y `cached_sweep` processes concurrently.**
Check with:

```bash
powershell -NoProfile -Command \
 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
 Select-Object ProcessId,CommandLine | Format-List"
```

**Distinguishing the failure modes** by the progress logger:

| Symptom | Cause |
| --- | --- |
| No `[detect_fvg] detect:` lines | still in `load_bars` / parquet read |
| `detect:` lines stop mid-way | detector OOM or a `detect_fvg` hot spot |
| `elapsed` jumps discontinuously | machine-level memory contention |
| `vp_engine` lines advancing, slow | book depth — expected, not a bug |
| Process gone, log truncated | OOM kill — check for a duplicate run first |

---
