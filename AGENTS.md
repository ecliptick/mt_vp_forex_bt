# sniper_mt_vp_forex_bt_only

Standalone backtest repo for the v22 SNIPER recipe on
**XAUUSD (MT4/MT5 convention)**, 100× leverage on the 0.01-lot base,
with the **live 40¢/oz spread** modelled directly (no backtest-vs-live
split).

This repo is **self-contained**. `bt/_engine/` **is** the canonical
engine source — every algorithm has exactly one definition, there.
There are no `sys.modules['src']` shims, no upstream-repo
dependencies, and no re-export wrappers under `bt/`.

> ### 📖 This file is the OPERATING MANUAL, not the history
>
> `AGENTS.md` answers **"what do I do, and what is true right now?"**
> Everything else lives in `docs/`:
>
> | Doc | What is in it |
> |---|---|
> | [`docs/analysis.md`](docs/analysis.md) | Which knobs are live, redundant, or already settled — the sweep surface by builder |
> | [`docs/evaluation.md`](docs/evaluation.md) | The leg-level methodology, the current verdict, the timing-control correction, the zone-shuffle placebo |
> | [`docs/sweeps.md`](docs/sweeps.md) | Sweep policy, the measured cost model, the `--i-know` gate, the output contract, **the sweep queue** |
> | [`docs/engine_history.md`](docs/engine_history.md) | Every bug found and fixed — leaks, detector bugs, perf, dead knobs, operational traps |
> | [`docs/promotions.md`](docs/promotions.md) | What shipped, what was measured and rejected, the NB lineage |
> | [`docs/trail_stop.md`](docs/trail_stop.md) | The trailing stop: MFE/MFA analysis, the 1m ATR basis, and why the trail is now **scoped to shorts** |
> | [`docs/repo_map.md`](docs/repo_map.md) | Every file and what it is for |
>
> **Rule: measured evidence lives in `docs/`, not in this file.** This
> file stays short enough to be read on every session. When a
> diagnostic produces a table that matters, write it to `docs/` and
> link it here.

---

## ⚡ THE ONE-LINE VERDICT (as of 2026-11-04)

> **The canonical book is PROFITABLE and covers its round trip.**
> The `trail_sides='short'` promotion (2026-11-04) took the corpus
> book from **gross/leg +0.3877 (0.777× the round trip, net −$741)**
> to **+0.7895 (1.691×, net +$2,298)** — and it is the **most
> profitable cell of the scope grid** (`both` −$741, `long` −$585,
> trail OFF +$1,593, **`short` +$2,298**). **The criterion is NET
> PROFIT.** See § 3.9b.

> **The edge is in the LEVEL, not the TIMING.** Shuffling the FVG
> zones' prices while keeping their trigger bars turns **+$510.58**
> into **−$2,398.07** (mean of 3 seeds, sd $428.29) — a **4.70×**
> collapse, and a **negative** number. The trigger bar tells you
> *when* — and that is worth nothing on its own. The zone's price
> relative to the market carries the information.
>
> ⚠️ **These figures are from the NARROW book** (vol gate 3.0, 1,692
> legs). The canonical recipe is the WIDE book (vol gate 1.0, 51,075
> legs). The level-not-timing **placebo** was only ever run on the
> narrow book, and it is **prompt-only** — do not re-run it unprompted.
>
> Full evidence: [`docs/evaluation.md`](docs/evaluation.md).

> **⚠️ `excess` IS NOT A PROMOTION GATE (2026-11-04).** The ledger's
> `excess = gross − passive_hold` benchmarks a leg's realised P&L against a
> **close-to-close** passive hold, but the engine books P&L at the
> **actual SL / TP fill price**. Those are **not exchangeable**:
> 85.9% of `sl` legs fill exactly at the stop and 100% of the rest
> fill adverse, and on **71.6%** of `sl` legs the exit bar's close is
> BEYOND the stop. The identity
>
> ```text
> excess == gap@exit − gap@entry      (gap = (fill − bar_close)·side·qty)
> ```
>
> holds to **1e-15** in every bucket of every book. So a stop or
> target bucket's `excess` is the **EXECUTION-GAP DIFFERENTIAL**, not
> a timing measure — the same confound class as the random-entry
> control's limit-vs-market comparison (§ 5.1 rule 4). The `sl`
> bucket's sign **flips with book composition** (both −$42, short
> +$134, trail OFF +$117), which is what a noise quantity does.
>
> **A promotion is judged on NET PROFIT.** The passive-hold/excess
> decomposition is retained as a DIAGNOSTIC, and the per-bucket sign
> assertion was replaced by the whole-book and per-side forms, which
> DO hold (−$198.80 long, −$259.59 short). Guard:
> `tests/test_signal_bar_completeness.py`.

---

## 1. The rules that are not optional

### 1.1 The unit of evaluation is the LEG, not the month

**Never evaluate this strategy on monthly slices.** The month is a
**reporting** dimension, never an execution boundary and never a unit
of evaluation.

Why: this is a high-frequency strategy producing ~55 legs a day, so
"8 months" is **n=8 draws** from a high-variance distribution — and
the month's own return tells us nothing about the in-month volatility
distribution the strategy actually experienced. Worse, the month
boundary is an **artefact of the engine, not of the market**: the p95
cap (7d), the swing cap (24h) and the VP profile (7d rolling) are all
history-dependent, and in backtest mode the p95 cap returns `inf`
until its window fills. So a cold chunk trades its first ~8 days with
**uncapped** targets.

The replacement is a corpus-wide, per-leg ledger over ONE continuous
window, bucketed by `(side, outcome)`.

**Three rules follow:**

1. **A month is a REPORTING dimension, never an execution boundary.**
   Chunked execution is a harness bug — not a rounding error.
2. **The "flip" is a tautology.** `gross = (exit − entry) × direction ×
   qty`, so `flip = −gross` **EXACTLY**. It carries zero information.
   The direction question is answered by the **per-leg passive-hold /
   excess** decomposition, which depends on the market.
3. **A positive per-leg EV that does not cover the round trip is not
   an edge**, and a near-zero gross makes every gross *ratio*
   meaningless.
4. **A control must be EXCHANGEABLE with what it controls for.**
   Holding the exit fixed is not enough — the entry's *execution
   model* must match too. The random-entry control failed this test;
   the zone shuffle is the version that passes.
5. **NEVER run a placebo unless the user explicitly prompts for it.**
   A placebo costs ~10 minutes on the 1y corpus and **rewrites its
   report artifact**. It is a **confirmation** step for a claim that
   has already been made, not a routine measurement. The answer is
   already recorded in `docs/evaluation.md` — **ask first**.
6. **The trade-count target is 30-70 trades per day.** It is set by
   `vol_state_min_range_norm`, and that knob is **PAIRED with
   `sl_max_usd`** — the two must always be re-measured together.
7. **⛔ NEVER draw a direction-dependence verdict from a MONTHLY
   regression.** A monthly regression of gross on market return has
   **n = 13..31** points. The correlation's standard error at n = 13
   is `1/√(n−3) = 0.32` in Fisher-z space, so the repo's historical
   monthly correlations (**r = +0.459** at n = 13 → **t = 1.57**;
   **r = +0.337** at n = 31 → **t = 1.86**) were **inside their own
   noise band** and carried no information. Worse, monthly
   aggregation destroys the strategy's own time scale: a month's gross
   sums thousands of legs spread over ~30 trading days, so the
   regression asks "did the book happen to sit on the right side of a
   30-day trend" — a statement about the book's COMPOSITION, not
   about whether the entries have skill. **This strategy trades at
   ~7–55 legs a DAY**, so it must be aggregated at the frequency it
   trades at.
   **The sanctioned instruments are:**
   * the **per-leg** decomposition — an exact algebraic identity on
     every leg, so its t-statistic uses **n = thousands of legs**;
   * the **DAILY** aggregation — `python -m
     runtime.diagnostics.daily_direction_dependence` (n ≈ 577 days,
     corr **t = +9.92** on the canonical book — decisive).

   **A monthly correlation is a REPORTING artefact, never a
   verdict.** If a decision in this repo cites a monthly `r`, it is
   re-grounded on the per-leg decomposition before it may ship.

   > **⚠️ THE BAND IS WAIVED, GENERALLY (2026-11-04).** The 5-event
   > sequence whitelist (§ 3.10) trades **7.19 legs/calendar-day** —
   > far below the band. The user waived it for that knob
   > (*"dont need to be too strict on trade count for this"*), and
   > then waived it **again, generally**, when the tier-3 #6 screen
   > showed that **no** `vol_state_min_range_norm` value can reach the
   > band (*"no, stop sweeps, 7 trade a day is fine"*).
   >
   > **The measured reason the band is unreachable:** even with the
   > gate **fully OFF** (`range_norm=0.0`) the book is **7.01
   > trades/day** on 2025-01 — the knob is **at its floor** and cannot
   > widen the book at all. The whitelist's K-curve is the real
   > trade-rate lever (K=16 → 10.44/day, K=17 → 11.25/day,
   > post-hoc), but § 3.10.2 measured end to end that the ranked
   > top-48 variant is **worse on every axis that matters**
   > (gross/leg 0.0458 vs 0.3877, net −$12,561 vs −$741).
   >
   > **The band remains a documented TARGET, not a shipping
   > constraint.** Do not sweep to reach it. The waiver is recorded,
   > and so is the reason it was granted.

### 1.2 The measurement WORKFLOW — cheap first, expensive last

**This is the routine. Follow it in this order and do not skip ahead.**

| step | command | cost | what it tells you |
|---|---|---|---|
| **1. one month** | `python -m runtime.sweeps.cached_sweep --knob X --grid ...` | **~6 s** | is the knob **live**? does the metric move at all? |
| **2. the ledger** | `python -m runtime.diagnostics.leg_ledger` then `leg_buckets` | **~5 min** cold / **~1 s** cached | the **real** verdict: buckets, per-leg passive-hold/excess, payoff geometry |
| **3. the trade count** | `python -m runtime.diagnostics.volgate_trade_count` | ~15-30 min | does the book land in the **30-70 trades/day** band? |
| **4. the placebo** | `python -m runtime.diagnostics.zone_shuffle_placebo` | ~10 min | is the edge in the **level** or the **timing**? **⚠️ ONLY WHEN THE USER PROMPTS FOR IT** |
| **5. the full corpus** | `--i-know` + explicit go-ahead | **~5 min/cell** | only for a candidate that survived 1-4 |

**The cheap, always-allowed steps are 1-3.** Step 4 is prompt-only
because it rewrites `zone_shuffle_placebo.json`, which the guard tests
read. If a claim needs re-measurement, **ASK FIRST**.

> **Why this order.** The corpus-wide ledger run pools every leg
> instead of reading one month, so it gives a *better* answer than a
> 12-month sweep cell — it is a **different measurement**, not a more
> thorough version of the same one.

### 1.3 Sweeping — one month by default, ask before a full run

* Default to **one month** (~6 s end to end). The dates are the
  default; the driver enforces them.
* A window longer than 31 days **prints its cost estimate and exits
  with code 2** unless `--i-know` is passed.
* **Even so, state the window, the cell count and the cost estimate in
  chat before launching a long run.** The gate protects against an
  *accidental* long run; it does not replace telling the user.
* **Never run two 1y processes concurrently** — memory contention
  causes a ~9× slowdown, not a 2×.
* Every sweep is judged on the **ledger**, not on the month's net.

Full policy and the measured cost model: [`docs/sweeps.md`](docs/sweeps.md).

### 1.4 A module must not do work at IMPORT time

**Importing a module must be CHEAP.** Anything that reads data, runs a
backtest, or reaches outside the repo belongs **under an
`if __name__ == "__main__":` guard**.

> **Rule: a guard that cannot run is not a guard.** The import-time
> rule was documented in prose for months while the guard was
> unrunnable. It is now both structural (AST checks) AND fast
> (`test_every_module_imports_cleanly` finishes in **0.94 s**).

### 1.5 A knob that is not on an allowlist does not exist

Passing an unswept knob raises `KeyError` **before** any cell runs —
it can never become a silent no-op. This is the whole point of the
config consolidation: the old failure mode was a sweep that *looked*
like it measured something and measured nothing.

> **⚠️ A knob ON the allowlist can still be dead.** The allowlist is
> only half the contract — a knob is live only if the **engine
> actually reads it**. Adding a constant to `bt/optimal_config.py`
> and to `_ENGINE_OVERRIDES_ALLOWED` without teaching the engine to
> consume it produces a knob that *looks* sweepable and measures
> nothing, which is strictly worse than an absent one: the sweep
> reports a "canonical" cell that is not canonical.
>
> The reverse is also a hard error: a knob the engine **accepts** but
> the config does not forward is a knob with a default, and a default
> is a silent-corruption field (rule 1.6).
>
> Both directions are guarded by
> `tests/test_config_single_source.py::test_no_canonical_constant_is_unread`
> and
> `tests/test_regressions.py::test_all_engine_kwargs_reach_the_engine`.
> **Land a new knob in this order: engine branch → engine mode
> constant → config value → builder → allowlist → test.** A refactor
> that ends with the canonical recipe unable to run has not finished.

### 1.6 A field with a DEFAULT is a silent-corruption field

`ClosedTrade` has `signal_bar: int = 0`, `zone_width: float = 0.0`,
`zone_low`/`zone_high: float = 0.0`. Any construction site that
forgets one produces plausible-looking numbers that are simply wrong —
no crash, no warning. **Walk every construction site and assert the
required fields are passed.**

> **A bug's blast radius scales with how often its code path runs.**
> The same omission was harmless at 93 legs and corrupted 17,211. When
> a knob widens the book, re-run the invariants — the bug was always
> there, only the exposure changed.

### 1.7 A placebo must clear a GEOMETRY GATE before its P&L is read

`zone_shuffle_placebo.py` computes the canonical arm's
`|entry − market@fill|` p50 and refuses to print a verdict if any
placebo arm's is more than `max($2.00, 5 × canonical)` away. An
earlier run without this gate reported a **−$1.24 MILLION** collapse
that was purely a broken-geometry artefact.

### 1.8 A whole-file verdict read at a bar is a look-ahead

**This repo has paid for this FOUR times** (NB94's one-sided rolling
mask, the Oct-2025 `zone.live` fix, the deferred supersession, and the
2026-11-03 dual-mode `inverted_bar` bug).

`inverted_bar` / `superseded_bar` / `played_out_bar` / `expired_bar`
are computed by the detector over the **ENTIRE dataset**. Reading one
at bar `i` leaks the future **unless the value is compared against
`i`**.

The 2026-11-03 instance cost **$2,811** over six monthly slices and
was systematically FAVOURABLE:

```python
# WRONG — reads the zone's FUTURE
if entry_mode == "dual" and int(zone.inverted_bar) >= 0: ...

# RIGHT — compares against the bar being traded
if entry_mode == "dual":
    _inv_b = int(getattr(zone, "inverted_bar", -1))
    if 0 <= _inv_b <= i: ...
```

Measured: **29.2%** of trigger zones were skipped for an inversion a
median **2,518 bars LATER** (max 200,769). The immediate book was a
**selection on the outcome**.

**The rule:** a verdict is only legal at bar `i` if it is compared to
`i`. The sanctioned causal helpers are `_zone_dead_by(zone, bar)` and
the `0 <= _inv_b <= i` comparison.

**The guard:** `tests/test_repo_invariants.py` scans the engine's AST
and fails if any comparison reads a whole-file verdict **without a bar
on the other side**. It follows local bindings and is scoped per
function. If you add a third read site, the guard will tell you.

Full evidence: [`docs/engine_history.md`](docs/engine_history.md).

---

## 2. The RESULTS REGISTRY — where a measurement goes

**Every run that produces a number is recorded in a durable,
commit-stamped registry.** This is the mechanism for "have we measured
this before?" and for "we re-ran the same params after a bug fix —
what moved?".

```
results/
  index.json          # one small entry per run — committed, diffable
  runs/<run_id>.json  # the full payload of one run — committed
```

### 2.1 The four identities — and why they are separate

| field | identifies | answers |
|---|---|---|
| `config_fp` | the **PARAMETERS**, and nothing else | "have we measured this configuration?" |
| `commit` | the **CODE** | "was this measured on the current code?" |
| `dirty` | whether the tree was modified | "is this even reproducible?" |
| `src_fp` | the artifact-producing **sources** | "do these two runs measure the same thing?" |

**`config_fp` deliberately excludes the commit.** That is the whole
point: when a bug fix lands, the SAME parameters are re-measured and
the two records sit side by side with an identical `config_fp` and
different `commit` — so the effect of the fix is visible as a diff.
The commit is metadata, not part of the key.

### 2.2 Using it

```bash
python -m runtime.results.cli list                    # every recorded run
python -m runtime.results.cli list --kind sweep_cell
python -m runtime.results.cli find --knob sl_zone_mult --value 40
python -m runtime.results.cli show <run_id>            # full record
python -m runtime.results.cli compare <run_a> <run_b>  # diff two runs
python -m runtime.results.cli history --kind leg_ledger \
    --params '{"start":"2024-01-01","end":"2026-07-16","warmup_days":14}'
python -m runtime.results.cli stale                    # runs on an old commit
```

Programmatically:

```python
from runtime.results import registry

registry.record_run(
    kind="sweep_cell",
    params={...},          # everything that could change the number
    summary={...},         # the headline metrics only
    payload={...},         # the full result
)
```

**`params` must contain every input that could change the
measurement.** `summary` should hold the few numbers a reader wants to
compare across commits — not the whole payload.

### 2.3 The rules

* **Results are COMMITTED.** A result that is not in git is not a
  result. `results/` is tracked; the old scratch dirs
  (`runtime/sweeps/_results/`, `runtime/diagnostics/_reports/`) stay
  gitignored and are for bulky intermediates only.
* **Never hand-edit `results/index.json`.** It is generated.
* **Every new diagnostic that produces a verdict must call
  `registry.record_run`.** A number nobody can find is not evidence.
* **Before re-measuring, check the registry first.** If the params and
  the commit both match, the number is already known — reuse it.
  Use `cli stale` to find runs that predate the current commit.

---

## 3. Architecture

**Flat `bt/` module — no nested subpackages.** Every Python file lives
directly under `bt/`.

**`bt/_engine/` IS the engine — one definition per algorithm.**
The detectors (`FvgZone`, `detect_fvg`, `detect_market_structure`) and
the multi-target engine (`run_vp_sniper_backtest`) all live there, and
`bt._engine.X` is the public surface. There are **no re-export
wrappers** under `bt/` — the ones that existed (`bt/rolling_p95.py`,
`bt/swing_cap.py`, `bt/vol_state.py`, `bt/volume_profile.py`) were
deleted on 2026-11-03: zero production callers, and they gave a
reader a second name for the same object. To find an algorithm's home,
grep `bt/_engine/`.

**Multi-target wrapper (`bt/vp_engine.py`).** Adds the XAUUSD sizing
convention on top of the engine:
`qty_oz = lots × contract_size` (= 1.0 at canonical 0.01 lots ×
100 contract_size). Strips the forex fee knobs
(`commission_per_lot_usd`, `spread_per_oz_usd`) before forwarding to
the engine — the engine reads `taker_fee_bps` only.

> **This repo is XAUUSD-ONLY.** There are no crypto/BTC references
> in the code, the config, or the docs, and there are none to add.
> The engine's position-size parameter is `qty_oz` — 1 unit = 1 oz of
> gold at canonical sizing. If you find a `qty_btc` / "BTC-scale"
> literal, it is a leftover from the pre-migration era and should be
> translated, not preserved.

### 3.1 Sizing (MT4 XAUUSD, 100× leverage)

| Field | Canonical | Source |
| --- | --- | --- |
| `lots` | `0.01` | MT4 minimum |
| `contract_size` | `100.0` | 100 oz / lot → 1 oz at 0.01 |
| leverage | `100×` | user-specified |
| spot | `~$3,000/oz` | live mid |
| notional | `$3,000` | `lots × contract_size × spot` |
| margin | `$30` | `notional / leverage` |

**P&L per $1 favorable move at 1 oz:** `+$1.00` (long) / `−$1.00`
(short).

### 3.2 Fees (single source of truth)

| Component | Round-trip | Per-trade @ 0.01 lots × 100 |
| --- | --- | --- |
| Commission / lot | `$7.00` | `0.01 × $7 = $0.07` |
| Spread / oz | `$0.40` | `1 × $0.40 = $0.40` |
| **Total** | | **$0.47** at $3,000 spot |

There is exactly ONE fee model. The backtest IS the live quote. No
"baseline $0.19 + overlay $0.21" split.

> **⚠️ The measured fee is NOT always $0.47.** The engine's fee is
> `avg_price × qty × bps × 2`, and `bps` is derived from the corpus
> **median spot**. The corpus trades between **$2,331 and $4,871**, so
> the measured fee on the full corpus is **$0.5029/leg**.
>
> `leg_buckets` reports **both**: `fee_per_leg` (measured, used for
> the verdict) and `fee_per_leg_spec` ($0.47, the contract value at
> $3,000). **The verdict uses the measured fee** — using the flat
> $0.47 overstates the edge by ~7%.

### 3.3 No-defaults policy

Every field on every local dataclass is REQUIRED:

* `MarginAndCost` (output of `margin_and_cost`)
* `ExposureSnapshot` (output of `account_exposure`)
* `KellySizing` (output of `size_lots`)

Construction without all fields raises `TypeError`.

### 3.4 ONE source of truth (config consolidation)

`bt/optimal_config.py` is the **single** place a canonical value
lives. There is no recipe dataclass, no upstream re-export, and no
second copy of any knob.

**Seven materialisers**, each reading the module constants and each
enforcing its own allowlist:

| Builder | Surface | Allowlist |
| --- | --- | --- |
| `build_engine_kwargs(**over)` | `run_vp_sniper_backtest` | `_ENGINE_OVERRIDES_ALLOWED` |
| `build_vp_kwargs(**over)` | `build_volume_profiles` | `_VP_OVERRIDES_ALLOWED` |
| `build_detector_kwargs(**over)` | `detect_fvg` | `_DETECTOR_OVERRIDES_ALLOWED` |
| `build_swing_kwargs(**over)` | `build_daily_swing` | `_SWING_OVERRIDES_ALLOWED` |
| `build_p95_kwargs(**over)` | `build_rolling_p95_from_bars` | `_P95_OVERRIDES_ALLOWED` |
| `build_live_p95_kwargs(**over)` | `make_live_rolling_p95` | `_LIVE_P95_OVERRIDES_ALLOWED` |
| `build_structure_kwargs(**over)` | `detect_market_structure` | `_STRUCTURE_OVERRIDES_ALLOWED` |

> **The two p95 builders are NOT interchangeable.**
> `build_p95_kwargs()` is the **backtest** path (`seed_constant_usd=0.0`
> — "the corpus already has the history the cap needs").
> `build_live_p95_kwargs()` is the **live VPS** path (the baked-in
> `FOREX_P95_RET_USD` coldstart fallback). Keeping them separate is
> what guarantees the backtest can never accidentally pick up the live
> seed constant and silently move every measured number.

> **The four translated builders** (`build_swing_kwargs`,
> `build_p95_kwargs`, `build_live_p95_kwargs`,
> `build_structure_kwargs`) use **canonical knob names** that differ
> from their target's parameter names. The translation maps live in
> `bt/optimal_config.py` and nowhere else.

### 3.5 Canonical config

All values live in `bt/optimal_config.py`.

| Knob | Value | Surface | Note |
| --- | --- | --- | --- |
| `lots` | `0.01` | sizing | MT4 minimum |
| `contract_size` | `100.0` | sizing | 100 oz/lot → 1 oz at 0.01 |
| `taker_fee_bps` | derived | engine | Per-SIDE rate — see § 3.2 |
| `sl_zone_mult` | `40.0` | engine | The FALLBACK stop only |
| `sl_buffer_usd` | `0.05` | engine | |
| `sl_mode` | `hvn_lvn` | engine | Stop anchors on the Nth HVN shelf behind entry |
| `sl_nth_hvn_n` | `1` | engine | Separate from the TP's `nth_hvn_n` |
| `sl_min_usd` | `0.94` | engine | Hard floor. Can only ever **loosen** a too-tight stop |
| `sl_max_usd` | `15.00` | engine | Hard CEILING. **PAIRED with the vol gate.** ⚠️ **UNMEASURED** — see § 4.1 tier 3 #6 |
| `sl_hvn_max_distance_usd` | `0.0` | engine | `0.0` = DERIVE the search radius per bar |
| `nth_hvn_n` | `3` | engine | TP reaches for the 3rd shelf ahead |
| `tp_zone_mult_fallback` | `200.0` | engine | NB75 remainder leg |
| `tp_mode` | `hvn_single` | engine | **ONE HVN target** — no partial, no runner. See § 3.7 |
| `partial_close_pct` | `0.50` | engine | **INACTIVE under `hvn_single`** — only `multi_target` uses it |
| `tp_max_distance_usd` | `0.0` | engine | **DERIVED** — a search radius, never a ceiling |
| `tp_hvn_proxy_frac` | `0.7` | engine | The no-shelf fallback fraction, promoted so it is sweepable |
| `conflict_policy` | `stack` | engine | Both sides trade freely |
| `max_same_dir_open` | `20` | engine | D3 fix — was `0` (uncapped) |
| `entry_fill_model` | `touch` | engine | D2 fix — resting limit order |
| `fill_model` | `pessimistic` | engine | SL precedence + gap-through at open |
| `tp_p95_mult` | `1.0` | engine | NB91 rolling p95 TP cap |
| `entry_mode` | `dual` | engine | Immediate + sniper co-fire |
| `vol_state_min_range_norm` | `1.0` | engine | **PAIRED with `sl_max_usd`.** 55.10 trades/calendar-day |
| `vol_state_min_expand_prev_day` | `1.0` | engine | **PROMOTED 2026-11-04 on EV.** Today-vs-yesterday vol expansion. See § 3.7.2 |
| `vol_state_max_expand_prev_day` | `0.0` | engine | Same measure, upper bound. Ships OFF |
| `structure_seq_drop_set` | **REMOVED 2026-11-04** | — | Deleted as redundant with, and contradictory to, the 5-event whitelist. See § 3.10 |
| `structure_seq_whitelist` | **21 keys** (13 long / 8 short) | engine | **PROMOTED 2026-11-04.** Take only these trailing **5**-event BoS/CHoCH signatures. See § 3.10 |
| `structure_gate` | `False` | engine | **NOT PROMOTED** |
| `trade_sides` | `"both"` | engine | **NOT PROMOTED** — deliberately two-sided |
| `trail_mode` | `'combined'` | engine | The trailing stop. k=2 act=1 gb=0.5. **The worst full-book cell under the `'both'` scope** — see § 3.9b |
| `trail_sides` | `'short'` | engine | **PROMOTED 2026-11-04.** Which sides the trail exits. **NOT the entry gate** — `trade_sides` opens, `trail_sides` exits. The only scope that beats BOTH the shipping recipe (−$741) and the trail-OFF baseline (+$1,593). See § 3.9b |
| `contra_close` | `True` | engine | **PROMOTED 2026-10-25** |
| `contra_close_spread_usd` | `5.0` | engine | Price spread threshold. Fires only on WIDE locks |
| `swing_cap_pct` | `0.25` | engine | NB72 daily-swing TP cap |
| `fvg_min_zone_usd` | `0.30` | detector | **MUST match the canonical recipe** — the old driver used `0.10`, which detected **44×** more zones |
| `resample_to_n_secs` | `60` | detector | FVG detection cadence is 1m, matching the structure cadence |
| `vp_rows` | `200` | VP | Re-scaled from 100 |
| `vp_min_zone_usd` | `0.50` | VP | Re-scaled from $5.00 |
| `structure_pivot_len` | `9` | structure | At the 1m resample cadence |
| `strict_wick_required` | `False` | detector | **Ships DISABLED** — the sibling's $0.68 floor matches **0.00%** of XAUUSD 1s windows |

> **⚠️ The vol gate and the SL ceiling are PAIRED, not independent.**
> `sl_max_usd` was measured with the gate OFF (optimum $5.00) and
> with the gate ON (optimum $2.50-$3.00). They must always be
> re-measured together.

### 3.6 Timestamps are nanoseconds — always

Every timestamp crossing a module boundary is **int64 epoch
nanoseconds**. The canonical parquet stores `timestamp[ms, UTC]`,
and pandas preserves that resolution, so:

```python
# WRONG — yields MILLISECONDS (1_784_143_874_000)
bars["time"].astype("int64")

# RIGHT
from bt._engine.timebase import to_ns_arr
times = to_ns_arr(bars)
```

Mixing the two is silent and catastrophic. `to_ns_arr` / `assert_ns`
are applied in `rolling_p95.py`, `swing_cap.py`, `volume_profile.py`
and the engine bar loop. The `assert_ns` guard raises loudly if a
millisecond array ever slips through again.

---

## 3.7 THE VOLATILITY KNOBS — which one is "the vol gate"?

There are **two** volatility knobs in the book, and they answer
**different questions**. Do not confuse them.

| knob | measure | question it answers | canonical |
| --- | --- | --- | --- |
| `vol_state_min_range_norm` | `range_norm_1m` | "Is **this minute** an outlier vs its own trailing 60 minutes?" | **`1.0` — ACTIVE** |
| `vol_state_min_expand_prev_day` | `vol_expand_prev_day` | "Is **today** running hotter or colder than **yesterday** ran at this time of day?" | **`1.0` — ACTIVE** (promoted 2026-11-04 on EV) |
| `vol_state_max_expand_prev_day` | `vol_expand_prev_day` | inverse: "is today **contracting**?" | `0.0` — ships OFF |

### 3.7.1 `vol_state_min_range_norm` — the ACTIVE gate

`range_norm_1m` = the current 1m candle's range ÷ the median range of
the trailing 60 one-minute candles. `1.0` means "this minute's range
is at least the typical minute's". Measured on the continuous corpus
at the canonical book size: **55.10 trades per calendar day** (64.65
per trading day) — mid-band of the 30–70 target.

**It is PAIRED with `sl_max_usd`.** The two were never independent:
with the gate OFF the SL optimum was `$5.00`; with the gate ON at
`1.0` it is `$8.00`. Always re-measure them **together**.

### 3.7.2 `vol_state_min_expand_prev_day` — the day-shifted vol gate (PROMOTED 2026-11-04)

`vol_expand_prev_day` = **RV(today so far) ÷ RV(yesterday at the same
elapsed fraction of the day)**.

This is the "vol gate expansion" from
[`docs/v2_spec.md`](docs/v2_spec.md) § 4.3. The hypothesis it rests
on: volatility is autocorrelated **across days** (measured on the 1y
corpus: lag-1 corr of consecutive daily ranges **+0.461**, Pearson
+0.611), so yesterday's regime is a usable prior for today. The
`range_norm_1m` gate has a **60-minute** memory — it cannot tell
"the whole day is slow" from "the whole day is fast".

**Why "same elapsed fraction":** raw "today so far vs yesterday's
full day" is mechanically biased low every morning (a partial day
always has less RV than a whole one) and the bias shrinks as the day
progresses. Comparing at the *same fraction* removes it, so `1.0`
means "today is tracking yesterday exactly" at **any** hour.

**Causal by construction:** the previous calendar day is entirely in
the past, and the numerator is a cumulative sum from midnight over
returns up to the current bar only.

**PROMOTED at `1.0` (2026-11-04).** The decision criterion is **EV
(gross per leg)** — *"always analyze based on EV, any EV increase we
promote"* (user instruction). One continuous run over the **full
corpus** (2024-01-01 → 2026-07-16, 927 days, 14-day warmup executed
and discarded), canonical recipe otherwise, via
`runtime/diagnostics/expand_prev_day_corpus_ab.py`:

> **⚠️ THE RECIPE THIS TABLE WAS MEASURED ON IS NOT THE SHIPPING
> RECIPE.** It predates the `trail_sides='short'` promotion
> (2026-11-04), so it was measured with `trail_sides='both'` and the
> 21-key whitelist. The **SHAPE** of the curve (a peak at 1.0, with
> the EV falling away on both sides) is the finding; the **LEVELS** are
> not comparable to the promoted book (§ 3.9b), and the knob must be
> **re-measured** under the promoted recipe before its threshold is
> treated as settled.

| threshold | legs | legs/day | gross | fees | net | **EV (gross/leg)** | passive-hold/leg | excess/leg | fee/leg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.0 (off) | 11,305 | 12.20 | $1,944.53 | $5,690.49 | −$3,745.96 | 0.1720 | +0.2704 | −0.0984 | 0.5034 |
| 0.7 | 9,117 | 9.83 | $2,430.97 | $4,556.08 | −$2,125.11 | 0.2666 | +0.3512 | −0.0846 | 0.4997 |
| 0.85 | 7,822 | 8.44 | $2,329.55 | $3,910.68 | −$1,581.14 | 0.2978 | +0.3740 | −0.0762 | 0.5000 |
| **1.0 (CANONICAL)** | 6,663 | 7.19 | $2,583.18 | $3,324.34 | **−$741.16** | **0.3877** | **+0.4641** | −0.0764 | 0.4989 |
| 1.1 | 5,677 | 6.12 | $1,808.74 | $2,838.53 | −$1,029.78 | 0.3186 | +0.3866 | −0.0680 | 0.5000 |
| 1.2 | 5,088 | 5.49 | $1,541.59 | $2,560.87 | −$1,019.29 | 0.3030 | +0.3740 | −0.0710 | 0.5033 |
| 1.3 | 4,610 | 4.97 | $1,410.23 | $2,327.22 | −$916.99 | 0.3059 | +0.3798 | −0.0739 | 0.5048 |

**EV rises to a peak at `1.0` and then FALLS AWAY.** The full
7-point curve was measured on the corpus (2026-11-04) — `1.1`,
`1.2` and `1.3` were run specifically to confirm that `1.0` is a
peak and not a point on a still-rising slope:

* `1.0` beats every higher threshold: dEV = **+0.0691** (vs 1.1),
  **+0.0847** (vs 1.2), **+0.0818** (vs 1.3).
* The gain over gate-off is dEV = **+0.2157**, **t = +2.60** — the
  lift is **statistically significant**. The net improves in step
  (−$3,746 → −$741) because the fee bill falls faster than the
  gross.

> **The peak is a PASSIVE-HOLD peak, not a timing peak.** At `1.0` the
> passive-hold component peaks too (+0.4641) while `excess` (what the
> entries added on top of a passive hold) is essentially flat at
> −0.07 to −0.10 across the whole range. The higher thresholds
> actually have a *slightly better* excess (−0.0680 at 1.1) but a
> **worse** passive-hold component (+0.3866), and the EV is the sum.
> So the gate is selecting on **the market's move**, not on timing
> skill — consistent with the repo-wide finding that the gross is
> dominated by the passive hold.

> **⚠️ HONEST LIMITS (recorded, not hidden):**
> * The EV lift over gate-off **is statistically significant**
>   (dEV = +0.2157, **t = +2.60**), but the lift over the NEAR
>   neighbours is not: 1.0 vs 0.85 is t = **+1.00**, 1.0 vs 1.1 is
>   t = **+0.71**. The peak is real and sits at `1.0`, but the
>   plateau 0.85–1.0 is not sharply separated from it.
> * **The book is still NEGATIVE** (−$741). The gate **reduces a
>   loss**; it does not make the book profitable. EV/leg ($0.3877)
>   remains **below** the measured round trip ($0.4989), so the
>   strategy is still **fee-limited** by the § 5.2 test.
> * **`excess` (the entries' contribution) stays NEGATIVE** at every
>   threshold (t = −8.45 at 1.0). The gross is still dominated by
>   the passive hold.
> * **Trade count is far below target**: 7.19 legs/calendar-day, and
>   **93.6% of days trade fewer than 30 legs**. The 30–70/day band
>   (§ 1.1 rule 6) is **not** met. The user instruction to promote on
>   EV overrides the trade-count preference — a deliberate
>   trade-off, not an oversight.
>
> **This gate is PAIRED with the exit geometry by construction** — it
> is a volume gate, and the trailing stop changed the payoff
> geometry. **Re-measure it whenever the exit geometry changes.**

> **⚠️ A SUPERSEDED measurement, kept for the record.** On the
> PRE-`trail_mode` recipe (2026-11-03) the same knob showed EV
> −0.1344 → −0.1019 and the book was −$30,707 → −$16,557. On the
> post-`trail_mode` recipe it showed EV 0.2502 → 0.3434. Both
> recipes show the same *shape* (EV up, fewer trades, net up), but
> the levels are **not comparable** — the trailing stop changed the
> book from 27,619 legs to 6,753. A number from one recipe must
> never be quoted against the other.
>
> The table above is the **third** measurement, on the current recipe
> (the trailing stop **and** the 21-key structure whitelist). The
> whitelist change alone moved the book by ~1% of its legs, so the
> curve was re-measured rather than extrapolated.

**Implementation notes.** The measure is built by
`bt/_engine/vol_state.py::_prev_day_expansion` and requires
`times_ns` (int64 epoch nanoseconds — `to_ns_arr(bars)`). The
sweep driver and `directional_beta.py` now build **every** gateable
measure, because a threshold for a measure that was never
materialised raised `KeyError` deep inside `_vol_state_pass` — i.e.
sweeping `vol_state_min_vel_15m` used to **crash** rather than
measure anything.

**Partial-day guard.** Forex sessions break daily and weekend days
are partial (Sunday's session is ~1h). The measure emits `1.0`
("no opinion") when the previous day did not trade up to the same
elapsed fraction, so Monday 10:00 is not compared against Sunday
23:59's tiny cumulative RV.

---

## 3.8 THE CONTRA-CLOSE KNOB — hedge netting

| knob | value | meaning |
| --- | --- | --- |
| `contra_close` | `True` | **PROMOTED 2026-10-25** |
| `contra_close_spread_usd` | `5.0` | The price-spread threshold at which the netting fires |

**The problem it solves.** The book can hold opposing positions. A
long bought at $3,000 and a short sold at $3,050 are a **locked
spread** — the pair's gross P&L is *deterministic*:

\[
\text{gross} = Q \cdot (E_{\text{short}} - E_{\text{long}})
\]

independent of where price goes next. Without simultaneous closure
the engine lets one leg's independent stop claim the loss (while its
counterparty sits on an equal-and-opposite gain that is **never
taken**) and pays **two** round trips to realise a spread that was
already in hand.

**The mechanism.** When the weighted-average spread between opposing
entries exceeds `$5.00`, the overlapping quantity is netted out
**simultaneously at market**. It fires **only on WIDE locks** — a
tight opposing pair is left alone, because its spread is not worth a
round trip.

**Measured on the continuous corpus (2024-01-01 → 2026-07-16, 14-day
warmup prefix, one continuous execution):** fires on **17,211 legs
(34%)** of the wide book for **+$38,763.86 gross**.

**Pair-level ladder** (the threshold was re-picked from this, not
from a monthly sweep):

| threshold | fires | verdict |
| --- | --- | --- |
| $0.5 | most often | over-nets; pays too many round trips |
| $1.0 | — | **promoted from this ladder on 2026-10-25** |
| $2.0 | — | intermediate |
| $4.5 | — | intermediate |
| $6.5 | rarely | under-nets; too many locked spreads survive |
| **$5.00** | — | **CANONICAL** (user instruction, 2026-10-25) |

> **⚠️ The $5.00 threshold was NOT re-picked from the pair-level
> analysis** — it sits between the $4.5 and $6.5 rungs and was set by
> instruction. The promoted feature changed the ON/OFF state; the
> threshold change is the measured part. Re-run
> `runtime/diagnostics/contra_close_actual_vs_simul` and
> `contra_close_stats` if the threshold is ever revisited.

**Honest limits.** The feature adds dollars to months that were
already winning; it does **not** flip a losing month into a winning
one (month count is 4/15 net-positive both ON and OFF). On the FLAT
slice it **costs** money (−$10.26). Six of fifteen months improve on
gross; nine are unchanged or slightly worse.

---

## 3.9 THE `tp_mode` KNOB — `hvn_single` (2026-11-03)

`tp_mode` selects the exit shape. The engine implements exactly
three and raises `ValueError` for anything else:

| mode | shape |
| --- | --- |
| **`hvn_single`** | **CANONICAL.** ONE target on the `nth_hvn_n`-th HVN shelf ahead of entry. No partial close, no runner leg, no breakeven re-injection. The whole position rides to the shelf. |
| `multi_target` | NB74: TP1 = HVN shelf (partial close 50%), TP2 = zone_mult (remainder 50%), breakeven re-injection. The A/B baseline. |
| `fvg_mult` | Single-target zone_mult only, no TP2. |

**Why `hvn_single` is canonical.** The `multi_target` shape splits
the position 50/50 between a partial at TP1 and a remainder at TP2 —
but on the canonical book the two legs **collapse onto the SAME
price** (the swing cap) on effectively every trade, because 88.7% of
HVN lookups miss and the fallback puts TP1 beyond the cap while TP2's
raw 200× zone width is also beyond it. The result is a partial close
at the cap followed by a remainder that runs the same distance for
half the size. That is a **fee-paying shape, not an edge**.

**One-month A/B (2025-01, `vol_state_min_expand_prev_day=0.0`):**

| mode | legs | net | EV/leg | hit | maxDD | PF | exit shape |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `multi_target` | 904 | $140.79 | $0.156 | 0.551 | $283.49 | 1.06 | `tp2` 367, `tp1` 46 |
| **`hvn_single`** | 858 | $138.81 | $0.162 | 0.527 | $283.71 | 1.06 | `tp` 413 (single) |
| `fvg_mult` | 872 | $128.06 | $0.147 | 0.530 | $283.71 | 1.05 | `tp` 409 (single) |

> **⚠️ ONE MONTH. The three modes are within ~$13 of each other on
> net and the exit-reason histograms are the real signal** — the
> `hvn_single` row has a single `tp` bucket where `multi_target` has
> two. The net difference is one draw, not a verdict. The ledger
> decides.

**Note:** `partial_close_pct` is **INACTIVE** under `hvn_single` —
the mode returns `partial_close_pct=0.0`, which the engine reads as
"TP2 is inactive", so the full position closes at TP1. It remains
sweepable for the `multi_target` A/B.

---

## 3.9b THE `trail_sides` KNOB — the per-side trailing scope (PROMOTED 2026-11-04)

**PROMOTED at `'short'` on 2026-11-04.** The canonical value is
`'short'`: the trailing stop applies to the **short** positions only,
and the long positions exit via TP/SL as before.

`trail_sides` scopes the **trailing stop** to one side of the book:
`'both'` | `'long'` | `'short'`. The engine raises `ValueError` on
anything else.

> **⚠️ IT IS NOT THE ENTRY GATE.** `trade_sides` decides which trades
> may **OPEN**; `trail_sides` decides which **POSITIONS** get a
> trailing **EXIT**. Under `trail_sides='short'` **both sides still
> trade** — only the short positions' exit changes. Wiring it into
> the entry path would silently re-create the `trade_sides='long'`
> bet that AGENTS.md § 6 already rejected.

### The measurement that created it

The corpus MFE/MFA analysis (`docs/trail_stop.md` § 8.4) measured the
trail's effect **PER SIDE** on the 8-month corpus (2,539 legs):

| side | n | baseline gross | canonical trail delta | SL rescued | TP cut |
|---|---|---|---|---|---|
| long | 1,685 | +$1,073.50 | **−$906.77** | 503 | 753 |
| **short** | 854 | **−$245.05** | **+$533.38** | 290 | 327 |

**The trail helps exactly the side that is LOSING money.** The short
side's MFA p50 ($3.716) sits close to its MFE p50 ($4.380) — shorts go
against the trade *first*, which is the population a trailing stop is
the right tool for. The long side runs 8.36 ATR of MFE, so truncating
its winners destroys more than the trail captures.

### The END-TO-END engine A/B (the verdict)

The per-side number above is a **replay on a fixed book** — a SCREEN,
not a verdict. The trail changes the book (a trail-closed leg frees a
same-direction slot; it can no longer be netted by contra-close), so
§ 3.11 requires an engine re-run. Driver:
`runtime/diagnostics/trail_sides_corpus_ab.py`, corpus 2024-04-01 →
2024-12-01, one continuous execution, `expand_prev_day=0.0`.

| arm | legs | gross | net | **vs base** | long net | short net | L-trail |
|---|---|---|---|---|---|---|---|
| trail OFF (baseline) | 2,539 | $828.45 | −$316.48 | — | +$317.86 | −$634.34 | 0 |
| combined k=2 act=1 \| both | 2,390 | −$17.65 | −$1,162.44 | −$845.96 | −$803.87 | −$358.57 | 1,298 |
| combined k=2 act=1 \| long | 2,466 | −$307.42 | −$1,452.24 | −$1,135.76 | −$787.88 | −$664.36 | 1,265 |
| **combined k=2 act=1 \| short** | **2,480** | **$1,161.02** | **+$16.12** | **+$332.60** | **+$374.68** | **−$358.55** | **0** |
| chandelier k=32 act=1 \| both | 2,538 | $825.24 | −$319.67 | −$3.19 | +$246.24 | −$565.91 | 34 |
| chandelier k=32 act=1 \| short | 2,538 | $896.87 | −$248.05 | +$68.42 | +$317.86 | −$565.91 | 0 |
| chandelier k=8 act=1 \| both | 2,471 | $215.25 | −$929.59 | −$613.11 | −$444.18 | −$485.41 | 629 |
| chandelier k=8 act=1 \| short | 2,522 | $1,018.69 | −$126.22 | +$190.25 | +$366.57 | −$492.79 | 0 |
| chandelier k=32 act=2 \| short | 2,538 | $890.52 | −$254.39 | +$62.08 | +$317.86 | −$572.25 | 0 |

**Every short-scoped arm beats the trail-OFF baseline. Every
both-scoped and long-scoped arm loses to it.**

#### The scope-gate invariant

`trail_sides='short'` must produce **ZERO long legs exiting on the
trail**. Measured: **0 in every short-scoped arm** ✅ (and 0 short
legs in the long-scoped arm). The invariant is on the **exit reason**,
not on the net — a short-scoped trail CAN legitimately move the long
book's net, because a trail-closed short frees a slot and admits a
later long signal. That is the **book effect** (§ 3.11), not a leak.

#### ⚠️ THE OUT-OF-SAMPLE SPLIT — the DECISIVE test

ONE continuous execution, bucketed by **signal time** into two halves
(§ 1.1 — the half is a REPORTING dimension, never an execution
boundary). **TWO WINDOWS WERE MEASURED AND THEY DISAGREE** — the
canonical recipe is the verdict, because it is the recipe that ships.

**8-month window (2024-04-01 → 2024-12-01, `expand_prev_day=0.0`),
split at 2024-08-01:**

| arm | H1 net | **H1 Δ** | H2 net | **H2 Δ** | wins BOTH? |
|---|---|---|---|---|---|
| trail OFF (baseline) | −$227.62 | — | +$140.94 | — | — |
| combined k=2 act=1 \| both | −$573.94 | −$346.33 | −$500.34 | −$641.27 | no |
| combined k=2 act=1 \| short | −$140.53 | +$87.09 | −$37.30 | **−$178.24** | **NO** |

**⭐ THE CANONICAL RECIPE** (full corpus 2024-01-01 → 2026-07-16,
14d warmup, `expand_prev_day=1.0` + the 21-key whitelist), split at
2025-04-01:

| arm | legs | gross | net | **vs base** | H1 net | **H1 Δ** | H2 net | **H2 Δ** | **wins BOTH?** |
|---|---|---|---|---|---|---|---|---|---|
| trail OFF (baseline) | 7,284 | $4,917.66 | +$1,593.16 | — | +$610.65 | — | +$982.51 | — | — |
| combined k=2 act=1 \| both | 6,663 | $2,583.18 | −$741.16 | **−$2,334.32** | −$349.05 | −$959.69 | −$392.12 | −$1,374.62 | no |
| **combined k=2 act=1 \| short** | **7,122** | **$5,622.77** | **+$2,298.20** | **+$705.04** | **+$646.05** | **+$35.40** | **+$1,652.15** | **+$669.64** | ✅ **YES** |

**✅ THE SHORT SCOPE WINS BOTH HALVES ON THE CANONICAL RECIPE**, and
the scope gate holds (0 long legs on the trail).

> **✅ THE CROSS-CHECK.** The `| both` arm reproduces the canonical
> ledger **EXACTLY** (6,663 legs, gross $2,583.18, net −$741.16 vs
> the ledger's `legs=6663, net=-741.160769`). The driver's warmup
> prefix and windowing are byte-exact with `leg_ledger`.
>
> **⚠️ WHY THE TWO WINDOWS DISAGREE.** The 8-month window had
> **neither** the 21-key whitelist **nor** the prev-day gate at 1.0 —
> it was the **pre-whitelist book**, which no longer exists. This is
> the § 3.10 re-fit lesson: *a set fitted on one book does not
> describe another*.

#### ⚠️ THE HONEST LIMITS

1. **The comparison baseline is NOT the shipping recipe.** The
   trail-OFF baseline is +$1,593.16; the **SHIPPED** recipe (`| both`)
   is **−$741.16**. So +$705.04 is a large improvement on what
   actually ships — but the shipped recipe is still negative.
2. **H1 is thin** (+$35.40 over half the corpus). H2 is strong
   (+$669.64). A thin half is a warning, not a pass.
3. **The trade count RISES** (6,663 → 7,122, +6.9%): trail-closed
   shorts free slots and admit more trades, which carry their own
   fees.
4. **The short side gets WORSE** (−$376.94 vs the baseline's
   −$299.85, a −$77.09 delta). **The entire gain is on the LONG side
   (+$782.13)** — reached *indirectly*, because trail-closed shorts
   free slots and admit better longs. **That is a book-composition
   gain, and it is fragile**: it depends on the same-direction cap
   being binding.
5. **One split point.** A different cut could give a different answer.

### ⭐ THE LEDGER VERDICT — the promotion gate (2026-11-04)

The two things § 3.9b listed as owed before promotion have now been
MEASURED. Driver: `runtime/diagnostics/trail_sides_ledger_verdict.py`
— full corpus (2024-01-01 → 2026-07-16, 927 days), 14d warmup, **one
continuous execution per arm**, canonical recipe otherwise.

> **⚠️ THE CRITERION IS NET PROFIT** (user instruction,
> 2026-11-04: *"just promote whatever is most profitable"*). Every
> arm below is an **EXPLICIT override** — the driver's arms must never
> be taken from the no-overrides canonical path, because after the
> promotion that path IS the `'short'` book and the comparison would
> be the book against itself.

| book | legs | gross | **net** | gross/leg | fee/leg | gross/fee |
|---|---|---|---|---|---|---|
| `trail_sides='both'` (was shipping) | 6,663 | +$2,583.18 | −$741.16 | +0.3877 | 0.4989 | 0.777× |
| `trail_sides='long'` | 7,063 | +$2,739.34 | −$584.98 | +0.3878 | 0.4931 | 0.787× |
| **`trail_mode='off'` (the baseline)** | 7,284 | +$4,917.66 | +$1,593.16 | +0.6751 | 0.4564 | 1.479× |
| **`trail_sides='short'` (PROMOTED)** | **7,122** | **+$5,622.77** | **+$2,298.20** | **+0.7895** | 0.4668 | **1.691×** |

**`short` is the MOST PROFITABLE cell of the grid** (+$2,298.20). It
beats the shipping recipe by **+$3,039.36** and the trail-OFF
baseline by **+$705.04**, and it is the only cell whose gross edge
covers the measured round trip by a wide margin.

#### The passive-hold / excess decomposition (DIAGNOSTIC — not a gate)

| book | passive-hold/leg | hold t | **excess/leg** | **excess t** |
|---|---|---|---|---|
| `both` | +0.4641 | +7.01 | −0.0764 | −8.45 |
| `off` | +0.7407 | +5.03 | −0.0655 | −7.46 |
| **`short`** | **+0.8539** | +6.62 | **−0.0644** | **−7.03** |

> **⚠️ READ THIS BEFORE QUOTING ANY `excess` NUMBER.**
> `excess` benchmarks the realised P&L against a **close-to-close**
> passive hold, but the engine books P&L at the **actual SL / TP fill
> price**. Those are not exchangeable — see the one-line verdict.
> The per-bucket `excess` of a stop or target bucket is the
> **execution-gap differential**, and its SIGN FLIPS with book
> composition (`both` −$42, `short` +$134, trail OFF +$117 on the
> `sl` bucket). The whole-book and per-side forms DO hold:
> **−$458.39** total, **−$198.80** long, **−$259.59** short.
>
> So the honest statement is: the book captures **less** than a
> passive hold of the same positions, and the `short` scope is a
> **better place to stand long**, not a timing edge. That remains
> true — but it is a DIAGNOSTIC, not the promotion criterion.

#### The DAILY direction-dependence test (DIAGNOSTIC — not a gate)

`python -m runtime.diagnostics.daily_direction_dependence` — the
**daily** aggregation (n = 577 days), the sanctioned instrument
(§ 1.1 rule 7):

| book | legs | days | **daily corr** | **corr t** | r² | verdict |
|---|---|---|---|---|---|---|
| `both` | 7,122 | 577 | +0.392 | **+9.92** | 0.154 | market-exposed |
| `off` | 7,284 | 577 | +0.223 | **+5.43** | 0.050 | market-exposed |
| **`short`** | **7,122** | **577** | **+0.392** | **+9.92** | **0.154** | market-exposed |

> **⚠️ THIS IS CONTEXT, NOT A VERDICT.** The criterion is net
> profit. Note that the `short` scope and `both` are the **same
> book** (the scope changes exits, not the daily gross bucket), so
> their daily correlations are identical — as they must be.
>
> **⛔ THE OLD MONTHLY BETA TABLE IS RETIRED.** The repo previously
> regressed gross on the **monthly** market return and quoted
> `corr = +0.337` (n = 31) for the `short` scope. That number is
> **inside its own noise band** — Fisher-z **t = 1.86**, not
> distinguishable from zero — and the **daily** regression on the
> same book measures **+0.392 with t = +9.92**. The daily number is
> the one that is decisive; the monthly one was not.

#### The 2×2 attribution — where the gain comes from

| book | net | **long-side net** | short-side net |
|---|---|---|---|
| `both` | −$741.16 | −$391.31 | −$349.86 |
| `off` | +$1,593.16 | +$1,893.01 | −$299.85 |
| `long` scope | −$584.98 | −$456.02 | −$128.97 |
| **`short` scope** | **+$2,298.20** | **+$2,675.14** | −$376.94 |

The net effect of turning the trail ON for **shorts alone** is
**+$705.04**; for **longs** it is **−$2,178.14**.

> **⚠️ MOST OF THE GAIN IS THE TRAIL BEING *REMOVED* FROM LONGS.**
> The long side's net goes from −$391 to +$2,675 — a **+$3,066** swing
> on a side whose trail was simply switched OFF. The short side, whose
> trail was switched ON, gets **−$27 WORSE**.
>
> The short scope is nevertheless the **correct** configuration: it is
> the only one of the four that beats the trail-OFF baseline, because
> the trail's damage on longs outweighs its benefit on shorts. The
> mechanism is "the trail is a bad exit for this book".

#### The split robustness (7 cut points, one execution)

The § 3.9b honest-limits list named "one split point" as a weakness.
It is now measured: the scope wins both halves at **4 of 7** cut
points vs the canonical recipe and **4 of 7** vs the trail-OFF
baseline. The two failures vs the canonical recipe are the two
EXTREME cuts (2024-07-01 and 2026-01-01), where one half is very
short.

#### ⚠️ WHERE THE GAIN COMES FROM — year × side

> **⚠️ CORRECTED 2026-11-04.** This table was previously read as
> "the gain is regime-concentrated (2026 loses $1,448)". That was
> measured against the **`'both'` scope**, which **does not ship**.
> The promotion baseline is **`trail OFF`**.

| year | gold | `both` | `off` | `short` | Δ vs `both` | **Δ vs `off`** |
|---|---|---|---|---|---|---|
| 2024 | +27.2% | −$325.55 | +$209.99 | +$136.02 | +$461.57 | **−$73.97** |
| 2025 | +64.5% | −$193.01 | +$3,339.64 | +$3,832.42 | +$4,025.43 | **+$492.78** |
| 2026 | **−6.1%** | −$222.60 | −$1,956.47 | −$1,670.24 | −$1,447.64 | **+$286.23** |
| **total** | | **−$741.16** | **+$1,593.16** | **+$2,298.20** | **+$3,039.36** | **+$705.04** |

**Against the baseline a promotion must beat (`trail OFF`), the scope
wins 2 of 3 years** — 2025 +$493, 2026 +$286, 2024 **−$74** (the one
loss is 10% of the total +$705 gain). The original "2026 loses $1,448"
reading came from a baseline that does not ship.

**Year × side, Δ vs `trail OFF`:**

| year | long Δ | short Δ | all Δ |
|---|---|---|---|
| 2024 | +$172.10 | **−$246.07** | −$73.97 |
| 2025 | −$15.72 | **+$508.51** | +$492.78 |
| 2026 | **+$625.76** | −$339.53 | +$286.23 |

**The gain swings between the sides year to year** — 2024 longs,
2025 shorts, 2026 longs. Which side benefits depends on the *ordering*
of signals inside the year, not on a stable side-specific edge.

> **⚠️ BUT THE BOOK'S ABSOLUTE LEVEL IS STILL REGIME-DEPENDENT.**
> In 2026 (gold −6.1%) **both** the baseline (−$1,956) and the
> promoted book (−$1,670) **lose money**. The scope is *less bad* in a
> down year; it does not make the strategy profitable in one.

Reproduce: `python -m runtime.diagnostics.trail_sides_ledger_verdict`
(STEP 5c).

> **✅ PROMOTED 2026-11-04. `TRAIL_SIDES = 'short'`.**
> The canonical ledger was rebuilt under the promoted recipe (7,122
> legs, gross +$5,622.77, net +$2,298.20, gross/leg +0.7895,
> gross/fee 1.691×). The scope-gate invariant holds — **0 long legs**
> exit on the trail.
>
> **Re-measure it whenever the exit geometry changes**, and treat the
> regime dependence of the book's ABSOLUTE level as a live caveat:
> this is a **better place to stand long**, and it earns its keep by
> the NET, not by a timing decomposition.

---

## 3.10 THE 5-EVENT SEQUENCE WHITELIST — `structure_seq_whitelist`

**PROMOTED 2026-11-04.** The canonical value is a set of **21
`<side>|<5-char signature>` keys**: **13 long**, **8 short**.

> The user asked (2026-11-03): *"tabulate bos/choch 5-event sequence
> and the subsequent entries. split between longs and shorts. then, we
> know which 5-seq events to whitelist before each long/short"*.

### What it is

A **whitelist**, not a blacklist: a key PRESENT means **TAKE** that
trade, a key ABSENT means **DROP** it. The signature is the trailing
**5** BoS/CHoCH events at the **signal** bar, oldest first
(`b`=BoS bull, `B`=BoS bear, `c`=CHoCH bull, `C`=CHoCH bear).

It **REPLACED** the 1-event `structure_seq_drop_set`, which was
**REMOVED 2026-11-04** — see § 3.10.1.

### The measurement

`runtime/diagnostics/seq5_table.py` over the **full corpus**
(2024-01-01 → 2026-07-16, one continuous execution, 14d warmup,
**37,159 legs** on the unfiltered book). A signature qualifies when
its gross/leg is positive on **both halves** of the corpus:

| side | qualified | top of the table (gross/leg, H1, H2) |
|---|---|---|
| **long** | **13 of 64** | `Ccbbb` +0.776 (+0.473, +1.035) · `bCcbb` +0.561 (+0.584, +0.542) · `cCcCB` +0.558 (+1.055, +0.161) |
| **short** | **8 of 64** | `BBcbb` +0.716 (+0.624, +0.824) · `BcCcC` +0.532 (+0.229, +0.819) · `cCcbb` +0.397 (+0.244, +0.539) |

> ⚠️ **The set was RE-FITTED on 2026-11-04.** The first version (25
> keys) was fitted on a **64,978-leg** ledger that was built BEFORE
> the recipe changed (the trailing stop became canonical,
> `tp_mode` moved to `hvn_single`, the SL ceiling moved, and the
> `vol_state_min_expand_prev_day` gate went live). That change moved
> the book to 37,159 legs, and a set fitted on the old book does not
> describe the new one — **only 14 of the 25 keys survived**. The
> root cause of the two disagreeing books was a **cache-fingerprint
> bug** (`bt/_engine/vol_state.py` produced a pickled artifact but
> was in neither fingerprint tuple) — fixed 2026-11-04, see
> [`docs/engine_history.md`](docs/engine_history.md).

### The walk-forward (2 folds by time, 2000 random-whitelist permutations as the null)

| side | fold | real Δgross | null p95 | percentile |
|---|---|---|---|---|
| long | H1 → H2 | +$593 | +$888 | 77.0% |
| long | H2 → H1 | +$449 | +$381 | **97.9%** |
| short | H1 → H2 | +$1,605 | +$2,121 | 56.3% |
| short | H2 → H1 | +$1,495 | +$1,550 | **91.7%** |

**The H2→H1 fold is the one that earns the slot on each side** (98%
and 92%). The other two folds (77% and 56.3%) are barely better
than chance, and that is recorded rather than smoothed over.

### The end-to-end A/B — 7 arms, each a full corpus continuous re-run

`runtime/diagnostics/seq_whitelist_corpus_ab2.py`:

| arm | wl | drop set | legs | legs/day | gross | net | gross/leg |
|---|---|---|---|---|---|---|---|
| unfiltered | 0 | 0 | 37,159 | 40.09 | −$4,742 | −$23,091 | −0.1276 |
| old (25 keys) | 25 | 0 | 7,926 | 8.55 | +$2,599 | −$1,367 | +0.3279 |
| old + drop set | 25 | 2 | 6,753 | 7.28 | +$2,319 | −$1,074 | +0.3434 |
| **split (21 keys) — SHIPPED** | **21** | **0** | **6,663** | **7.19** | **+$2,583** | **−$741** | **+0.3877** |
| split + drop set | 21 | 2 | 5,664 | 6.11 | +$2,357 | −$467 | +0.4161 |
| ranked-21/side | 42 | 0 | 12,655 | 13.65 | +$3,917 | −$2,395 | +0.3095 |
| ranked-21/side + drop set | 42 | 2 | 9,377 | 10.12 | +$3,414 | −$1,281 | +0.3640 |

The shipped set beats the old set on **both** gross per leg
(+0.3877 vs +0.3279) and net (−$741 vs −$1,367).

> **⚠️ THIS TABLE PREDATES THE `trail_sides='short'` PROMOTION**
> (2026-11-04). It was measured with `trail_sides='both'`. The
> whitelist's **shape** (21 keys beat the old 25) is the finding; the
> **levels** are not comparable to the promoted book (§ 3.9b). The
> whitelist itself has **not** been re-fitted under the promoted
> recipe — a re-fit is a separate, expensive measurement and is **not
> implied** by the trail promotion.

### 3.10.0 THE ENGINE-LEVEL WALK-FORWARD — the decisive test (2026-11-04)

`runtime/diagnostics/seq5_engine_walkforward.py` — this is the test
that was OPEN when the set was promoted, and it is the one that
decides whether the **specific 21 keys** or only the **shape** of the
rule generalise.

Each half is a **separate continuous execution** with its own warmup
prefix, so the rolling caps are warm. The whitelist is **fixed before
the run starts** — that is the whole point, and it is what the
post-hoc walk-forward cannot do.

| arm | legs | gross | net | gross/leg | long g/leg | short g/leg |
|---|---|---|---|---|---|---|
| unfiltered H1 | 18,640 | −$1,792 | −$10,805 | −0.0961 | +0.0065 | −0.2000 |
| unfiltered H2 | 18,350 | −$1,982 | −$10,505 | −0.1080 | +0.0141 | −0.2405 |
| **fit H1 → score H2** | 3,024 | −$351 | −$1,783 | −0.1159 | **+0.0635** | **−0.4664** |
| **fit H2 → score H1** | 2,645 | +$936 | −$350 | **+0.3540** | **+0.3721** | **+0.2945** |
| *in-sample (fit H1 → H1)* | 3,329 | +$1,085 | −$531 | +0.3260 | +0.3616 | +0.2605 |
| *in-sample (fit H2 → H2)* | 2,412 | −$69 | −$1,214 | −0.0287 | +0.0620 | −0.3260 |
| shipped set on H1 | 3,329 | +$1,085 | −$531 | +0.3260 | +0.3616 | +0.2605 |
| shipped set on H2 | 3,024 | −$351 | −$1,783 | −0.1159 | +0.0635 | −0.4664 |
| **long-only on H1** | 2,157 | +$770 | −$281 | **+0.3569** | +0.3569 | — |
| **long-only on H2** | 1,997 | +$110 | −$843 | **+0.0553** | +0.0553 | — |

> **`fit H1` produced EXACTLY the shipped 21-key set** — the H1 fit
> and the whole-corpus fit agree key-for-key. So `fit H1 → score H2`
> IS the shipped set scored out of sample, and the shipped set's
> H2 number is an **out-of-sample** number.

#### The pooled verdict

| book | legs | gross | net | gross/leg | fee/leg | gross/fee |
|---|---|---|---|---|---|---|
| control (unfiltered) | 36,990 | −$3,774 | −$21,310 | −0.1020 | — | — |
| **OUT-OF-SAMPLE** (each half scored by the OTHER's fit) | 5,669 | **+$586** | −$2,133 | **+0.1033** | 0.4796 | 0.215× |
| *in-sample* (each half scored by its OWN fit) | 5,741 | +$1,016 | −$1,745 | +0.1770 | 0.4810 | 0.368× |
| shipped (whole-corpus fit) | 6,353 | +$735 | −$2,314 | +0.1156 | 0.4799 | 0.241× |
| **long-only** | 4,154 | **+$880** | −$1,124 | **+0.2119** | 0.4825 | 0.439× |

**The OOS gross/leg (+0.1033) is 58% of the in-sample gross/leg
(+0.1770).** The rule generalises, but with real decay — which is
what an honest out-of-sample number looks like.

#### ⚠️ THE SHORT SIDE DOES NOT GENERALISE — a SIGN FLIP

| side | in-sample g/leg | **out-of-sample g/leg** | verdict |
|---|---|---|---|
| **long** | +0.2233 | **+0.2189** | **generalises** (no decay) |
| **short** | +0.0700 | **−0.1803** | **sign flip — does NOT generalise** |

The short side's whole-corpus fit says +0.0700; scored on the half it
never saw, it is **−0.1803**. Its in-sample edge was fit noise.

This is the **same lesson the post-hoc walk-forward already taught**
(§ 3.10) — but measured END TO END this time, where the contra-close
netting and the same-direction cap are re-evaluated.

#### ⚠️ BUT THE LONG-ONLY BOOK IS A DIRECTIONAL BET

The per-leg decomposition, at the leg level, is the decisive number:

| arm | side | gross/leg | passive-hold/leg | hold t | **excess/leg** | **excess t** |
|---|---|---|---|---|---|---|
| long-only H1 | long | +0.3569 | +0.3884 | +5.32 | **−0.0315** | **−2.87** |
| long-only H2 | long | +0.0553 | +0.1569 | +1.00 | **−0.1016** | **−3.47** |

**The long-only book's gross is the market's passive hold, and its
`excess` — what the entries added on top of it — is significantly
NEGATIVE in both halves.** The whitelist has found a better place to
stand long; it has **not** found timing skill.

The **daily** direction-dependence regression agrees, and it is the
sanctioned instrument (§ 1.1 rule 7) —
`python -m runtime.diagnostics.daily_direction_dependence`:

| arm | legs | days | **daily corr** | **corr t** | r² | excess/leg | excess t |
|---|---|---|---|---|---|---|---|
| unfiltered H1 | 18,640 | 418 | +0.040 | +0.82 | 0.002 | −0.0574 | −12.17 |
| unfiltered H2 | 18,350 | 275 | −0.003 | −0.06 | 0.000 | −0.1415 | −15.86 |
| shipped on H1 | 3,329 | 344 | +0.047 | +0.87 | 0.002 | −0.0441 | −4.84 |
| shipped on H2 | 3,024 | 214 | +0.187 | **+2.75** | 0.035 | −0.1299 | −5.62 |
| **long-only H1** | 2,157 | 309 | +0.208 | **+3.69** | 0.043 | −0.0315 | **−2.87** |
| **long-only H2** | 1,997 | 198 | +0.247 | **+3.53** | 0.061 | −0.1016 | **−3.47** |

The long-only book is **significantly long-correlated in BOTH halves**
(t = +3.69 and +3.53), while the unfiltered book is **not correlated at
all** (t = +0.82 / −0.06). The whitelist does not create the
directional exposure — **concentrating** it does.

> **⛔ THE OLD MONTHLY BETA TABLE IS RETIRED.** The repo used to
> regress **monthly** gross on the **monthly** market return. That
> measurement is invalid here: n = 13..31 points, so the correlation's
> standard error is ~0.32 in Fisher-z space and every quoted monthly
> `r` sat inside its own noise band (**r = +0.459** at n = 13 →
> **t = 1.57**; **r = +0.337** at n = 31 → **t = 1.86**). It was also
> **sign-unstable**: the monthly table called `shipped on H2`
> **−0.373**, while the daily regression measures the same book at
> **+0.187** (t = +2.75). A number whose sign flips with the
> aggregation window cannot decide anything.
>
> The historical monthly table and its driver
> (`runtime/diagnostics/seq5_beta_regression.py`) are **superseded by
> `runtime/diagnostics/daily_direction_dependence.py`**.

> **⚠️ THIS IS WHY THE SHIPPED SET KEEPS ALL 21 KEYS.** The
> long-only variant has the better gross/leg (+0.2119 vs +0.1156) and
> the better net (−$1,124 vs −$2,314) — and it is **not** shipped,
> because it is a directional bet on gold wearing a sequence filter
> as a hat. Dropping the short side does not remove the market
> exposure; it **concentrates** it — the daily correlation is
> significant in both halves for long-only (+3.69, +3.53) and
> insignificant for the two-sided shipped book on H1 (+0.87).

### ⚠️ The honest limits — a QUALITY knob, not an EDGE knob

1. **The book is still NET-negative** (−$741 over 927 days). Its
   gross is **positive** (+$2,583) but the per-leg edge (+$0.3877)
   is still **below the measured round trip** ($0.4989). This is the
   same verdict the `vol_state_min_expand_prev_day` gate got
   (§ 3.7.2): the filter makes the surviving trades much better, not
   profitable.
2. **The trade count is far below the 30–70/day band** — 7.19
   legs/calendar-day, with 93.6% of days trading fewer than 30 legs.
   **The user explicitly waived the band for this knob** (*"dont
   need to be too strict on trade count for this"*). To stay inside
   the band the whitelist would have to keep **48 of 64** signatures
   per side — i.e. barely filter at all
   (`runtime/diagnostics/seq5_ksweep.py`). The full-strength set is
   the one that carries the signal. **⚠️ RE-MEASURED ENGINE-LEVEL
   2026-11-04** — see § 3.10.2: the K=48 band variant was run
   through the engine and it is **worse on every axis that matters**.
3. **The shipped set is a whole-corpus fit, and the engine-level
   walk-forward now says exactly how much that costs.** The
   **engine-level** walk-forward (§ 3.10.0) is **DONE**: fit the set
   on H1, rebuild the H2 book with it, and vice versa. The OOS
   gross/leg is **+0.1033** against the in-sample **+0.1770** — a 58%
   retention. The **long** half generalises cleanly; the **short**
   half **sign-flips** and is fit noise. And the long-only variant
   that the short half would have enabled is a **directional bet**
   (per-leg excess significantly negative in both halves, daily
   correlation significant in both halves: t = +3.69 and +3.53) — the
   same market exposure that got `trade_sides="long"` rejected. So all
   21 keys ship: the short keys are the honest cost of keeping the
   book two-sided, and removing them would not remove the market
   exposure, it would concentrate it.
4. **Post-hoc is not the book.** The table is a leg filter; the
   engine-side whitelist re-runs the book, so the conflict policy,
   the same-direction cap and the `contra_close` netting are all
   re-evaluated. See § 3.11 — and § 3.10.0, which is the end-to-end
   re-run that § 3.11 demanded.

### 3.10.2 THE TRADE-COUNT BAND IS UNREACHABLE — MEASURED (2026-11-04)

The § 3.10 claim that "staying in the 30–70/day band would require
keeping 48 of 64 signatures per side — i.e. barely filter at all" was
POST-HOC. It is now **measured END TO END**.

`runtime/diagnostics/seq5_ksweep_engine_check.py` — the ranked top-48
set (96 keys) run through the **engine** over the full corpus, against
the shipped 21-key set:

| book | keys | legs | legs/day | gross | net | gross/leg | fee/leg | gross/fee | excess/leg | excess t |
|---|---|---|---|---|---|---|---|---|---|---|
| **shipped (21 keys)** | 21 | 6,663 | **7.19** | +$2,583 | **−$741** | **+0.3877** | 0.4989 | **0.777×** | −0.0764 | −8.45 |
| **ranked top-48** | 96 | 27,926 | **30.13** | +$1,279 | **−$12,561** | **+0.0458** | 0.4956 | **0.092×** | −0.0918 | −17.37 |

**The post-hoc screen was RIGHT, and the answer is worse than it
promised.** The K=48 variant does land in the band (30.13/day), but:

* gross/leg collapses to **12%** of the shipped set's (+0.0458 vs
  +0.3877);
* net worsens by **−$11,820**;
* **gross/fee falls from 0.777× to 0.092×** — it covers only
  **9%** of the round trip, against 78% for the shipped set;
* its timing component (`excess`) is **strongly negative**
  (t = **−17.37**) — more so than the shipped set's
  (t = −8.45). The extra signatures are **noise**, and noise has
  negative timing;
* the SHORT side turns **negative** (−0.0477 g/leg over 13,524 legs)
  — the ranked set admits 13,524 short legs, and most of them lose.

**So the trade-count band and the edge are in DIRECT conflict.** The
band is reachable only by admitting signatures that destroy the
per-leg edge. The user's waiver (*"dont need to be too strict on trade
count for this"*) is therefore **not** a compromise — it is the only
admissible choice, and it is now measured rather than argued.

> **⚠️ The waiver remains a waiver.** The shipped set trades 7.19
> legs/calendar-day, far below the band. Nothing in this measurement
> changes that. What it changes is that the band is **not** an
> alternative configuration: it is measurably worse.

### 3.10.1 THE 1-EVENT DROP SET IS REMOVED — `structure_seq_drop_set`

**REMOVED 2026-11-04.** The knob, the constant, the engine gate (both
the immediate and the sniper path), the `n_dropped_seq_side` counter
and five diagnostics are **deleted**.

It was not measured wrong — it was **REDUNDANT**, and it
**CONTRADICTED** the whitelist that replaced it:

* The drop set said *"never short after a structure reversal
  ('c'/'C')"*.
* The whitelist said *"short after THIS specific 5-event history is
  good"* — and **3 of the 8 whitelisted short shapes end in a
  reversal**, including the single best one (`short|BcCcC`,
  +0.532 gross/leg).

So the drop set **vetoed the whitelist's best short shape**, and the
two rules could never both be satisfied.

**The drop set's isolated effect on the same 21-key whitelist**
(end-to-end, from the table above):

| | gross | net | legs |
|---|---|---|---|
| drop set OFF | +$2,583 | −$741 | 6,663 |
| drop set ON | +$2,357 | −$467 | 5,664 |
| **Δ (ON − OFF)** | **−$226** | **+$274** | **−999** |

It costs 999 legs and **$226 of GROSS** to save **$274 of NET**. A
net-only win that *loses* gross is a **fee artefact, not a signal**
(§ 5.2). The whitelist alone is the better book.

Locked by `tests/test_regressions.py::test_per_side_sequence_drop_set_is_removed`.

### 3.11 The rule this promotion exposed

> **A drop-set or whitelist MUST be measured END TO END by re-running
> the engine.** The post-hoc leg filter is a *screen* for which
> hypotheses are worth a re-run — never the verdict itself.

`contra_close` nets opposing legs **pairwise**, so removing legs
removes the netting's counterparties. A post-hoc prediction cannot
see that. Measured on the corpus: the post-hoc table said `short|B`
was the best drop set (positive on both halves); the engine-side
re-run said it was the **worst** of the three tested.

Driver: `runtime/diagnostics/seq_whitelist_corpus_ab2.py` (the 7-arm version; the 3-arm driver it supersedes was retired on 2026-11-04 because it passed the now-removed drop set).

---

## 4. What gets swept

The sweep surface is **the seven builders**. Sweep drivers vary ONE
field at a time and re-call the builder that owns that surface.

The override allowlists are the complete sweep surface. A name on
none of them raises `KeyError` **before** any cell runs.

> **⚠️ CHANGED 2026-10-25 — the SL anchor is LIVE again.**
> At the old `$2.50` ceiling the stop was pinned on every leg and the
> HVN SL anchor was **inert**. At `$8.00` there are **8,445** distinct
> stop distances. **Consequence: sweeping `sl_mode` /
> `sl_nth_hvn_n` / `hvn_buffer_usd` now MEASURES SOMETHING.** Re-measure
> them, and re-measure them **together with the vol gate**.

### 4.1 THE SWEEP QUEUE — what to sweep next, in order

**This is the list. Do not sweep it unprompted — the queue is here so
the order is decided, not so it can be launched blind.**

Every entry states the command, the cost, and what would make the
knob ship. **The workflow order in § 1.2 applies to each one**: one
month → ledger → trade count → (placebo only on prompt) → full corpus.

#### Tier 1 — the two NEW knobs

| # | knob | grid | window | cost | shipping bar |
| --- | --- | --- | --- | --- | --- |
| 1 | `vol_state_min_expand_prev_day` | `0,0.8,0.9,1.0,1.1,1.2,1.5` | ~~1 month, then corpus~~ **DONE + PROMOTED 2026-11-04** | ~6 s/cell, ~4 min/cell at 1y | EV/leg up AND legs **in** the 30–70/day band AND the corpus ledger agrees |
| 2 | `vol_state_max_expand_prev_day` | `0,0.5,0.6,0.7,0.8` | 1 month | ~6 s/cell | as above; the inverse gate (vol **contraction** rejection) |

```bash
python -m runtime.sweeps.cached_sweep \
  --knob vol_state_min_expand_prev_day \
  --grid 0.0,0.8,0.9,1.0,1.1,1.2,1.5
```

> **✅ Tier 1 #1 is CLOSED — PROMOTED at `1.0` (2026-11-04).**
> The decision criterion is **EV (gross per leg)**. The corpus curve
> was measured at **7 points** (0.0 / 0.7 / 0.85 / 1.0 / 1.1 / 1.2 /
> 1.3): EV **rises** 0.1720 → 0.2666 → 0.2978 → **0.3877** and then
> **falls away** to 0.3186 / 0.3030 / 0.3059. `1.0` is a genuine
> **peak**, not a point on a rising slope, and the lift over
> gate-off is **statistically significant** (t = **+2.60**). Net
> improves in step (−$3,746 → −$741). Full table and the honest
> limits (book still negative, fee-limited, trade count 7.19/day
> far below the 30–70 band) in § 3.7.2.
>
> ⚠️ The knob is a **volume gate** — re-measure it whenever the exit
> geometry changes (the trailing stop already invalidated the
> 2026-11-03 measurement).

#### Tier 2 — the `tp_mode` A/B (needs the ledger to decide)

| # | knob | grid | shipping bar |
| --- | --- | --- | --- |
| 3 | `tp_mode` | `multi_target,hvn_single,fvg_mult` | ledger buckets + payoff geometry, not the month's net |
| 4 | `nth_hvn_n` | `1,2,3,4,5` | **Re-measure under `hvn_single`** — the value `3` was tuned for the `multi_target` geometry and is a different quantity now |
| 5 | `partial_close_pct` | `0.25,0.5,0.75` | **Only meaningful under `multi_target`** — inert under `hvn_single` |

```bash
python -m runtime.sweeps.cached_sweep \
  --knobs "tp_mode=multi_target,hvn_single,fvg_mult"
```

#### Tier 3 — the PAIRED knobs (must be swept TOGETHER)

| # | knob | grid | note |
| --- | --- | --- | --- |
| 6 | `vol_state_min_range_norm` × `sl_max_usd` | `0.5,1.0,1.5,2.0` × `2.5,4,6,8,12,15` | **PAIRED.** The gate and the ceiling were never independent. The `hvn_single` change moved the payoff geometry, so this pair **must** be re-measured. ⚠️ **OWED:** the ceiling moved `$8.00 → $15.00` on 2026-11-04 and this pair has **not** been re-measured on the corpus — `$15.00` is UNMEASURED, not promoted. **A one-month screen WAS run 2026-11-04** (40 cells, `runtime/results` → `sl_ceiling_paired_1m`) — see the note below |
| 7 | `vol_state_min_range_norm` × `vol_state_min_expand_prev_day` | `0.5,1.0,1.5` × `0,0.9,1.1` | **Two vol gates at once** — are they redundant? Both ask "is the market moving" |

> **📌 WHAT THE 2026-11-04 ONE-MONTH SCREEN OF TIER 3 #6 SHOWED**
> (2025-01, 40 cells, gross/leg — the promotion criterion):
>
> | rn \ sl | 2.5 | 4 | 6 | 8 | 12 | 15 |
> |---|---|---|---|---|---|---|
> | 0.5 | 0.057 | 0.078 | −0.018 | −0.090 | 0.139 | 0.115 |
> | **1.0** | 0.292 | 0.249 | 0.138 | 0.106 | **0.386** | **0.386** |
> | 1.5 | 0.429 | 0.461 | 0.484 | 0.552 | 0.683 | 0.683 |
> | 2.0 | 0.629 | 0.767 | 0.708 | 0.903 | 0.903 | 0.903 |
>
> **Three things this established, none of which need the corpus:**
>
> 1. **At the canonical gate (`range_norm=1.0`), `$12` and `$15` are
>    IDENTICAL** — the ceiling cannot bind past $12 on that month, so
>    `$15.00` and `$12.00` are the same cell there. **The ceiling is
>    LIVE** (the EV spread across the `sl` axis is 0.28 at `rn=1.0`).
> 2. **`$15.00` is already the best ceiling at the canonical gate**
>    (0.386 vs 0.106 at `$8.00`) — so there is no case for *lowering*
>    it on this evidence.
> 3. **`range_norm` cannot reach the 30-70/day band.** Even with the
>    gate fully OFF (`rn=0.0`) the book is **7.01 trades/day** on
>    2025-01 — the knob is **at its floor** and cannot widen the book.
>    The trade-rate lever is the **sequence whitelist** (K-curve) or
>    `vol_state_min_expand_prev_day` OFF (12.20/day), not this knob.
>
> ⚠️ **THIS IS A SCREEN, NOT A VERDICT** (AGENTS.md § 1.2). One month
> is one draw, and the `expand_prev_day` curve (AGENTS.md § 3.7.2)
> shows the same shape recurring: **EV peaks and then falls away.**
> The corpus re-measurement of tier 3 #6 is **still OWED** — this
> screen does not discharge it.
>
> **⚠️ AND THE `$15.00` CEILING IS NOT A BACKSTOP — IT IS THE STOP
> FOR 71% OF THE BOOK.** Measured on the canonical corpus ledger:
> the median stop distance is **exactly `$15.00`**, and **70.88%** of
> legs are capped there. The HVN anchor wants a stop wider than `$15`
> far more often than not, so the ceiling **is** the effective stop
> width for most trades. That is why the payoff geometry is a wide
> 1.105 ratio (win `+$9.19`, loss `−$8.31`, break-even win rate
> **47.51%** against an actual **82.67%** → edge **+35.2 pp**) and why
> the book is **gross-positive** (`+$2,583`, `+$0.3877/leg`) while
> **net-negative** (fee `0.4989/leg`).
>
> **WHERE `$15.00` CAME FROM — it was NOT measured.** It arrived in
> commit `9f29cc8` as a re-interpretation of the knob's purpose
> ("a runaway shelf-anchored stop needs a catastrophic-error
> backstop") with a round number; the `$8.00` measured-optimum table
> was deleted in the same diff. The old `$8.00` WAS a measured
> optimum (2026-10-25, paired with the vol gate). **`$15.00` is an
> assumption.**

> **✅ The test suite is GREEN: 435 passed, 1 skipped.** (Was
> 17 failed / 413 passed before the 2026-11-04 fingerprint work; the
> AST-hash change fixed 11, and the 6 remaining stale assertions were
> realigned to the current contract.) The one **real** debt is the
> `$15.00` SL ceiling — see tier 3 above. Full history:
> [`docs/sweeps.md`](docs/sweeps.md) → *The test suite is GREEN*.
>
> ⚠️ **A CONCURRENT 1y PROCESS MAKES THE SUITE LOOK RED.** The
> import guard (`test_every_module_imports_cleanly`) imports every
> module in ONE process, and the corpus runs hold ~6 GB each. Run
> alongside another 1y measurement it OOMs, and the suite reports
> failures that are **not** code defects — including the six that
> were "KNOWN RED" until 2026-11-04. **A green suite needs the
> machine to itself** (AGENTS.md § 7).

```bash
python -m runtime.sweeps.cached_sweep \
  --knobs "vol_state_min_range_norm=0.5,1.0,1.5;vol_state_min_expand_prev_day=0.0,0.9,1.1"
```

> **✅ Tier 3 #7 is DONE (2026-11-03, 2025-01).** The two gates are
> **NOT redundant** — they are close to **complementary**: the prev-day
> gate adds nothing at `range_norm=0.5`, but it is the only cell that
> turns the month's net positive at `range_norm=1.5`.
>
> | `range_norm` \ `expand` | 0.0 | 0.9 | 1.1 |
> | --- | --- | --- | --- |
> | **0.5** | 1548 legs, −$277.59 | 1071 legs, −$96.72 | 698 legs, −$187.18 |
> | **1.0** | 1058 legs, −$240.40 | 698 legs, **−$51.73** | 432 legs, −$109.26 |
> | **1.5** | 463 legs, −$4.63 | 308 legs, **+$14.95** | 189 legs, −$104.31 |
>
> But **every one of the nine cells is below 30–70 legs/day except
> `range_norm=1.0, expand=0`**, and the corpus verdict (§ 3.7.2) says
> the prev-day gate does not create an edge at any level. So the
> pair still does not ship.

#### Tier 4 — the other dead-or-dormant vol measures

All of these are on the allowlist and **are now sweepable without
crashing** (the driver builds every measure — see § 3.7.2), but
none has ever been swept:

| # | knob | grid | what it would test |
| --- | --- | --- | --- |
| 8 | `vol_state_min_vel_5m` | `0,1e-5,2e-5,5e-5` | short-horizon velocity floor |
| 9 | `vol_state_min_vel_15m` | `0,1e-5,2e-5` | medium-horizon velocity floor |
| 10 | `vol_state_min_rv_ratio_30s` | `0,1.5,2.0,3.0` | 30s realised vol vs its own 24h median |
| 11 | `vol_state_max_rv_ratio_30s` | `0,3.0,5.0,8.0` | the inverse — reject parabolic vol |
| 12 | `vol_state_max_wick_ratio` | `0,0.5,0.7,0.9` | reject long-wick bars (sweeps, not displacements) |

#### Tier 5 — the contra-close threshold (the ladder is stale)

| # | knob | grid | note |
| --- | --- | --- | --- |
| 13 | `contra_close_spread_usd` | `1.0,2.0,3.0,4.5,5.0,6.5,8.0` | The canonical `$5.00` was **set by instruction**, not picked from the pair-level ladder. Re-derive it with `runtime/diagnostics/contra_close_actual_vs_simul` + `contra_close_stats` |

#### Tier 6 — the structural knobs the SL anchor unblocked

> **These only became live on 2026-10-25** (the SL ceiling moved to
> `$8.00` and the HVN stop anchor stopped being inert). They have
> never been swept under the current geometry.

| # | knob | grid | note |
| --- | --- | --- | --- |
| 14 | `sl_mode` | `hvn_lvn,fvg_only,fvg_or_poc` | the third mode's `$1.00` bug is fixed but it is untested at `$8.00` |
| 15 | `sl_nth_hvn_n` | `1,2,3` | separate from the TP's `nth_hvn_n` |
| 16 | `hvn_buffer_usd` | `0.05,0.10,0.20` | the stop's distance from the shelf |

#### Tier 7 — explicitly NOT to be swept

| knob | why |
| --- | --- |
| `commission_per_lot_usd`, `spread_per_oz_usd` | stripped by the forex wrapper — the engine reads `taker_fee_bps` only |
| `sl_atr_mult` | removed from the engine |
| `poc_buffer_usd` | only read under `sl_mode='fvg_or_poc'` |
| `structure_gate` | measured and **NOT PROMOTED**; it loses **−$1,215 of net** against the canonical book and its per-leg `excess` stays significantly negative on both sides |
| `trade_sides="long"` | measured and **NOT PROMOTED**; a long-only book has no hedge in a sell-off. Its daily corr is **higher** (+0.437, t = +10.74) than the two-sided recipe's (+0.392). ⚠️ Its net is **higher** (+$2,731 vs +$2,298) — the knob stays OFF as a deliberate **risk** choice: the short side is net-negative (−$377) BUT a real diversifier (daily corr **−0.195**, t = −4.09 vs the long side's **+0.441**) |
| any HVN/LVN VP knob | the VP profile is **INERT** on this corpus — do not promote on the strength of that finding |

---

## 5. The evaluation harness

These are the tools. Use these; do not roll your own.

| command | what it does |
|---|---|
| `python -m runtime.diagnostics.leg_ledger` | Build/reuse the corpus-wide per-LEG record (continuous run) |
| `python -m runtime.diagnostics.leg_buckets` | The four buckets `(side × outcome)` + passive-hold/excess + payoff geometry |
| `python -m runtime.diagnostics.leg_random_entry` | The random-entry control — **CONFOUNDED**, see `docs/evaluation.md` |
| `python -m runtime.diagnostics.zone_shuffle_placebo` | The decisive level-vs-timing test — **⚠️ PROMPT-ONLY** |
| `python -m runtime.diagnostics.daily_direction_dependence` | The **daily** direction-dependence test (n ≈ 577 days) + the per-leg decomposition. **⛔ NEVER use a monthly regression** (§ 1.1 rule 7) |
| `python -m runtime.diagnostics.volgate_trade_count` | The trade-count ladder (30-70/day target) |
| `python -m runtime.sweeps.cached_sweep` | **THE** sweep driver (single + multi-knob grid) |
| `python -m runtime.sweeps.cost_estimate` | The pre-flight cost estimate + the `--i-know` gate |
| `python -m runtime.results.cli` | The results registry (list / find / compare / history / stale) |

Full file map: [`docs/repo_map.md`](docs/repo_map.md).

### 5.1 The four buckets and the three questions

| bucket | meaning |
|---|---|
| `long_win` | long, gross > 0 |
| `long_loss` | long, gross ≤ 0 |
| `short_win` | short, gross > 0 |
| `short_loss` | short, gross ≤ 0 |

> **⚠️ THE PROMOTION CRITERION IS NET PROFIT** (user instruction,
> 2026-11-04: *"just promote whatever is most profitable. monthly aggs
> are meaningless for this high freq strat"*). Q1 below tells you
> whether the book is profitable; Q2 and Q3 are **diagnostics** that
> explain WHY. Neither `excess` nor the daily direction-dependence
> regression may **block** a promotion on a positive net.
>
> Two reasons, both measured:
>
> 1. **The `excess` benchmark is not exchangeable with the execution
>    model.** The engine books P&L at the **actual SL / TP fill
>    price**; `passive_hold` is a **close-to-close** hold. The
>    identity `excess == gap@exit − gap@entry` holds to 1e-15 in
>    every bucket of every book, so a stop or target bucket's excess
>    is the **execution-gap differential**, not timing skill. Its sign
>    flips with book composition (the `sl` bucket: −$42 on `both`,
>    +$134 on `short`, +$117 on trail OFF). The whole-book and
>    per-side forms DO hold and are still reported.
> 2. **Monthly aggregation is n=31 draws** from a high-variance
>    distribution at ~7 legs/day. The month is a REPORTING dimension
>    (§ 1.1), never an evaluation boundary.

**Q1 — is there an edge at all?** Compare the per-leg gross EV against
the **measured** round trip. The report prints `gross_per_leg /
fee_per_leg` so the comparison is explicit. A gross edge that does not
cover the round trip is **not** an edge.

**Q2 — is the edge direction-independent?** The **per-leg passive-hold /
excess** decomposition:

\[
\text{passive\_hold} = (close_{\text{exit bar}} - close_{\text{entry bar}})
\cdot \text{direction} \cdot \text{qty}
\qquad
\text{excess} = \text{gross} - \text{passive\_hold}
\]

`passive_hold` is what any position of that size and side would have earned
by merely being there over the same bars. `excess` is what the entries added
on top of it. **This is an exact algebraic identity**, not an estimate, and
its standard error uses **n = thousands of legs** — not n = 13..31 months.
⚠️ Read the criterion note above before using a PER-BUCKET `excess`: for a
stop or target bucket it is the execution-gap differential, not a timing
measure.

> **⛔ NEVER regress monthly gross on monthly market return** (§ 1.1
> rule 7). The sanctioned direction-dependence instrument is
> `python -m runtime.diagnostics.daily_direction_dependence` — the
> **daily** aggregation (n ≈ 577 days, corr t = +9.92) plus the
> per-leg decomposition.

**Q3 — is the loss concentrated?** A strategy whose losses are all the
same size is a **geometry** problem; one with a fat loss tail is a
**risk** problem.

### 5.2 The payoff geometry — signal problem or fee problem?

For each side: `avg_win`, `avg_loss`, `payoff_ratio`, the break-even
win rate, and the **edge** in percentage points:

\[
\text{edge\_pp} = 100 \cdot (\text{hit\_rate} - p_{\text{be}})
\]

* `edge_pp ≤ 0` → **no signal**. The side loses before fees.
* `edge_pp > 0` but `gross_per_leg < fee` → **fee-limited**. The
  signal is real but too small to pay for itself.
* `gross_per_leg ≥ fee` → **profitable**.

The two failure modes need **different fixes**, and the monthly
paradigm could not tell them apart.

### 5.3 The canonical ledger is CANONICAL-ONLY

A variant ledger **may not** write the canonical path. `leg_ledger`
raises `SystemExit` if you try. A variant must go to its own file:

```python
build_ledger(engine_overrides=ov,
             out_path=OUT_DIR / "leg_ledger_variant.json")
```

> **Rule: an artifact that every consumer reads as "the canonical
> result" must be structurally unable to hold anything else.**

### 5.4 Execution is CONTINUOUS

The strategy runs over **one continuous window**. The **warmup prefix**
(14 days — exceeds the longest history-dependent lookback: the p95
cap's 7 days, the swing cap's 24h, the VP profile's 7d rolling) is
executed but its **legs are discarded**; only its warmup effect is
kept. Every scored leg therefore has **warm caps**.

**The signal bar is the causal bar.** Every per-side gate is
evaluated at the signal, so the ledger keys legs by their signal
time — not by their fill bar. A bullish BoS invalidates the opposing
bear FVGs, so a bar whose last structure event is a bullish BoS can
*never* produce a short signal: the `short|b` bucket is
**structurally empty**.

---

## 6. Things that were measured and found DEAD

Recording these so they are not re-litigated. Each was measured, not
assumed. Details in [`docs/promotions.md`](docs/promotions.md).

| Feature | Verdict |
|---|---|
| Market structure event whitelist (**the 2026-10-24 monthly artifact**) | **DEAD as measured then** — kept ZERO trades on every slice, because the vol gate left only 5.5 legs/signature against the report's own `min_n` floor of 15. **SUPERSEDED 2026-11-04** by the corpus-level 5-event whitelist, which ships. See § 3.10 |
| The **1-event** sequence drop set (`structure_seq_drop_set`) | **REMOVED 2026-11-04** — not measured wrong, but **redundant with and contradictory to** the 5-event whitelist: it vetoed 3 of the 8 whitelisted short shapes, including the best one. End-to-end it cost $226 of gross to save $274 of net. See § 3.10.1 |
| The coarse **polarity** whitelist (bull/bear/mixed buckets) | **DEAD** — held-out kept trades have aggregate t = −2.15 (significantly loss-making). **A coarse polarity bucket does NOT carry the signal — the specific 5-event shape does.** |
| The sequence flip | **DEAD** — "does NOT generalise, −$8 on held-out slices" |
| `structure_gate` | **NOT PROMOTED** — loses **−$1,215 of net** vs the canonical book; its per-leg `excess` stays significantly negative on both sides |
| `trade_sides = "long"` | **NOT PROMOTED** — a long-only book has no hedge in a sell-off, and its daily corr is **higher** (+0.437, t = +10.74) than the two-sided recipe's (+0.392). ⚠️ Its net is **higher** (+$2,731 vs +$2,298) — the knob stays OFF as a deliberate **risk** choice: the short side is net-negative (−$377, gross/fee 0.669×) BUT a real diversifier (daily corr **−0.195**, t = −4.09 vs the long side's **+0.441**) |
| **`trail_sides = "both"`** (the trail on longs) | **SUPERSEDED 2026-11-04** — the trail on the LONG side costs **−$2,178** on the corpus; scoping it to shorts is the only cell that beats the trail-OFF baseline. See § 3.9b |
| **The trailing stop at ANY setting, unscoped** | **MEASURED, then SCOPED.** The trail's total damage is nearly independent of its tightness (k=2 → −$373; k=4 → −$379; k=8 → −$355) — it is a **geometry** change, not a risk knob, so there was no setting to find. It ships **scoped to shorts only** (`trail_sides='short'`), which is the one scope that beats the trail-OFF baseline. See [`docs/trail_stop.md`](docs/trail_stop.md) |
| HVN/LVN volume profile | **INERT** — the placebo (shuffled volume) lands INSIDE the real profiles' band. Two blockers stand: the TP is the swing cap (89.0% of legs), and LVN is never read |
| NB91 rolling p95 TP cap | **SHADOWED** by NB72 on 100% of trade probes — currently measures nothing |
| `partial_close_pct` / `tp2_mode` / `tp2_nth_hvn_n` / `tp2_swing_cap` | **INERT under the canonical `tp_mode='hvn_single'`** — there is no partial and no runner leg. Live only for the `multi_target` A/B |
| `vol_state_min_vel_5m` / `_vel_15m` / `_min_rv_ratio_30s` / `_max_rv_ratio_30s` / `_max_wick_ratio` | **NEVER SWEPT.** They used to *crash* (`KeyError` — the driver never built their measures). Fixed 2026-11-03; still unswept, so their P&L effect is unknown |

> **⚠️ Do not promote any VP knob on the strength of the HVN/LVN
> finding** — it was measured on the narrow book and has not been
> re-measured on the wide one.

---

## 7. Operational gotchas

**Never run two heavy cells concurrently.** Memory contention causes a
**9×** slowdown on identical code and can OOM the process silently.

> **⚠️ A CONCURRENT RUN MAKES THE TEST SUITE LOOK RED.** This is
> the sharpest edge of the rule above. The import guard
> (`test_every_module_imports_cleanly`) imports EVERY module in ONE
> process; a corpus run holds ~6 GB. Together, they OOM — and the
> suite reports a failure that is **not** a code defect. Measured
> 2026-11-04: a concurrent `trail_sides_corpus_ab` run made the
> suite report `test_every_module_imports_cleanly` as failing, and
> the SAME tree was **435 passed, 1 skipped** four minutes later once
> the machine was free. **Before believing a red suite, check for a
> run in flight:**
>
> ```bash
> powershell -NoProfile -Command "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | Select-Object ProcessId,CommandLine"
> ```

**Diagnostic logging.** The engine reports progress to `stderr` every
250,000 bars:

```
[vp_engine] bar=1250000/41233730 elapsed=51.9s rate=24091/s open=24
```

The `open=` field is the multiplier on total runtime. Run with
`python -u` so output is unbuffered.

More: [`docs/engine_history.md`](docs/engine_history.md).

---

## 8. The skills (`.cursor/skills/`)

Five project skills encode the workflows above. They are **explicitly
invoked** (`disable-model-invocation: true`) — ask for one by name.

| Skill | Use it when |
|---|---|
| **`evaluate`** | Judging a run: the ledger, the four buckets, the per-leg passive-hold/excess decomposition, the payoff geometry. **Before any promotion.** |
| **`sweep`** | Sweeping a knob: the one-month default, the `--i-know` gate, the output contract, the cost model |
| **`results-registry`** | Saving a measurement, looking up a previous one, or diffing a run before/after a bug fix |
| **`promote`** | Deciding whether a measured change ships, and then changing it in one place |
| **`engine-change`** | Editing anything under `bt/` — the invariants that have been broken before, and the re-measurement rule |

```bash
# invoke by name
/evaluate
/sweep
```

> **Skills are the HOW; `AGENTS.md` is the WHAT.** If a skill and this
> file disagree, this file wins — and the skill is wrong and should be
> fixed. The skills are deliberately thinner than this file: they
> point at `docs/` for the evidence rather than duplicating it.

---

## 9. Documentation map

Five project skills encode the workflows above. They are **explicitly
invoked** (`disable-model-invocation: true`) — ask for one by name.

| Skill | Use it when |
|---|---|
| **`evaluate`** | Judging a run: the ledger, the four buckets, the per-leg passive-hold/excess decomposition, the payoff geometry. **Before any promotion.** |
| **`sweep`** | Sweeping a knob: the one-month default, the `--i-know` gate, the output contract, the cost model |
| **`results-registry`** | Saving a measurement, looking up a previous one, or diffing a run before/after a bug fix |
| **`promote`** | Deciding whether a measured change ships, and then changing it in one place |
| **`engine-change`** | Editing anything under `bt/` — the invariants that have been broken before, and the re-measurement rule |

```bash
# invoke by name
/evaluate
/sweep
```

> **Skills are the HOW; `AGENTS.md` is the WHAT.** If a skill and this
> file disagree, this file wins — and the skill is wrong and should be
> fixed. The skills are deliberately thinner than this file: they
> point at `docs/` for the evidence rather than duplicating it.

---

| Doc | Contents |
| --- | --- |
| `AGENTS.md` (this file) | The operating rules: how to evaluate, how to sweep, what is true now |
| `docs/analysis.md` | Which knobs are live, redundant, or already settled — the sweep surface by builder |
| `docs/evaluation.md` | The leg-level methodology, the current verdict, the timing-control correction, the zone-shuffle placebo |
| `docs/sweeps.md` | Sweep policy, the measured cost model, the `--i-know` gate, the output contract, **the sweep queue** |
| `docs/engine_history.md` | Every bug found and fixed — leaks, detector bugs, perf, dead knobs, operational traps |
| `docs/promotions.md` | What shipped, what was measured and rejected, the NB lineage |
| `docs/trail_stop.md` | The trailing stop — MFE/MFA analysis, the 1m ATR basis, the settings it implies |
| `docs/repo_map.md` | Every file and what it is for |
| `docs/nb94_1w_vs_1y.md` | The forward-leak diagnosis |
| `docs/nb95_beta_proxy.md` | The beta-proxy diagnosis and the engine-defect audit |
| `docs/ta_episode_metric.md` | The TA episode metric + the fixed-side placebo that kills the result |
| `docs/ta_gate_sweep_2026_10_25.md` | The TA GATE sweep — trade-number decay, MR vs trend, placebos |
| `docs/v2_spec.md` | The v2 rebuild logic spec |

**The three questions this file must answer for a new session:**

| question | where |
|---|---|
| *which knob is the volatility gate?* | § 3.7 — there are **two**, and BOTH are active: `vol_state_min_range_norm` (1.0) and `vol_state_min_expand_prev_day` (1.0, promoted 2026-11-04) |
| *what does the contra knob do?* | § 3.8 — `contra_close=True`, `contra_close_spread_usd=5.0`, hedge netting on wide locks |
| *what should I sweep next?* | § 4.1 — the seven-tier sweep queue |

> **Rule: measured evidence lives in `docs/`, not in this file.**
> This file states the current truth and the rules. When a diagnostic
> produces a table that matters, write it to `docs/` and link it.
> AGENTS.md is read on every session — it must stay short enough to
> be read.
