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
- [The dual-mode inverted_bar look-ahead](#the-dual-mode-inverted_bar-look-ahead--found-and-fixed-2026-11-03)
- [The 1m detector — three bugs fixed](#the-1m-detector--three-bugs-fixed-2026-10-2324)
- [Dead knobs](#dead-knobs--verified-2026-10-20)
- [Known engine bugs and their fixes](#known-engine-bugs-and-their-fixes-chronological)
- [Performance](#performance--the-1y-detector-and-engine)
- [Operational gotchas](#operational-gotchas)
## The engine fixes that made the backtest honest

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

### Numba: considered, not needed (yet)

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

---

## Operational gotchas

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
