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
> | [`docs/repo_map.md`](docs/repo_map.md) | Every file and what it is for |
>
> **Rule: measured evidence lives in `docs/`, not in this file.** This
> file stays short enough to be read on every session. When a
> diagnostic produces a table that matters, write it to `docs/` and
> link it here.

---

## ⚡ THE ONE-LINE VERDICT (as of 2026-10-25)

> **The edge is in the LEVEL, not the TIMING.** Shuffling the FVG
> zones' prices while keeping their trigger bars turns **+$510.58**
> into **−$2,398.07** (mean of 3 seeds, sd $428.29) — a **4.70×**
> collapse, and a **negative** number. The trigger bar tells you
> *when* — and that is worth nothing on its own. The zone's price
> relative to the market carries the information.
>
> ⚠️ **These figures are from the NARROW book** (vol gate 3.0, 1,692
> legs). The canonical recipe is the WIDE book (vol gate 1.0, 51,075
> legs). The finding is **robust**: the wide book's `excess` is also
> negative (−$4,080.07, t = −15.63), for every exit reason. But the
> level-not-timing **placebo** was only ever run on the narrow book,
> and it is **prompt-only** — do not re-run it unprompted.
>
> Full evidence: [`docs/evaluation.md`](docs/evaluation.md).

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
   The direction question is answered by the **drift/excess**
   decomposition, which depends on the market.
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

### 1.2 The measurement WORKFLOW — cheap first, expensive last

**This is the routine. Follow it in this order and do not skip ahead.**

| step | command | cost | what it tells you |
|---|---|---|---|
| **1. one month** | `python -m runtime.sweeps.cached_sweep --knob X --grid ...` | **~6 s** | is the knob **live**? does the metric move at all? |
| **2. the ledger** | `python -m runtime.diagnostics.leg_ledger` then `leg_buckets` | **~5 min** cold / **~1 s** cached | the **real** verdict: buckets, drift/excess, payoff geometry |
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
| `sl_min_usd` | `2.50` | engine | Hard floor. Can only ever **loosen** a too-tight stop |
| `sl_max_usd` | `8.00` | engine | Hard CEILING. **PAIRED with the vol gate** |
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
| `structure_seq_drop_set` | `{"short\|c", "short\|C"}` | engine | Do not take a short after a structure **reversal** |
| `structure_seq_whitelist` | **25 keys** (18 long / 7 short) | engine | **PROMOTED 2026-11-04.** Take only these trailing **5**-event BoS/CHoCH signatures. See § 3.10 |
| `structure_gate` | `False` | engine | **NOT PROMOTED** |
| `trade_sides` | `"both"` | engine | **NOT PROMOTED** — deliberately two-sided |
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

| threshold | legs | legs/day | gross | fees | net | **EV (gross/leg)** | fee/leg | excess/leg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.0 (off) | 11,361 | 12.26 | $2,842.28 | $5,763.29 | −$2,921.01 | 0.2502 | 0.5073 | −0.0617 |
| 0.7 | 9,158 | 9.88 | $2,909.64 | $4,606.76 | −$1,697.13 | 0.3177 | 0.5030 | −0.0654 |
| 0.85 | 7,951 | 8.58 | $2,535.63 | $4,000.24 | −$1,464.61 | 0.3189 | 0.5031 | −0.0565 |
| **1.0 (CANONICAL)** | 6,753 | 7.28 | $2,318.84 | $3,393.04 | **−$1,074.20** | **0.3434** | 0.5024 | −0.0589 |

**EV rises monotonically with the threshold and peaks at `1.0`**:
+0.2502 → **+0.3434** (+37%). The net improves in step (−$2,921 →
−$1,074) because the fee bill falls faster than the gross.

> **⚠️ HONEST LIMITS (recorded, not hidden):**
> * The EV lift is **not statistically separable** from its
>   neighbours: 1.0 vs gate-off is t = **+1.18**, 1.0 vs 0.85 is
>   t = **+0.29**. The EV curve is **flat across 0.85–1.0**; 1.0 is
>   the argmax of a flat region, not a sharp optimum.
> * **The book is still NEGATIVE** (−$1,074). The gate **reduces a
>   loss**; it does not make the book profitable. EV/leg ($0.3434)
>   remains **below** the measured round trip ($0.5024), so the
>   strategy is still **fee-limited** by the § 5.2 test.
> * **`excess` (timing, not drift) stays NEGATIVE** at every
>   threshold (t = −6.41 at 1.0). The gross is still dominated by
>   drift.
> * **Trade count is far below target**: 7.28 legs/calendar-day, and
>   **94.6% of days trade fewer than 30 legs**. The 30–70/day band
>   (§ 1.1 rule 6) is **not** met. The user instruction to promote on
>   EV overrides the trade-count preference — a deliberate
>   trade-off, not an oversight.
>
> **This gate is PAIRED with the exit geometry by construction** — it
> is a volume gate, and the trailing stop changed the payoff
> geometry. **Re-measure it whenever the exit geometry changes.**

> **⚠️ A SUPERSEDED measurement, kept for the record.** On the
> PRE-`trail_mode` recipe (2026-11-03) the same knob showed EV
> −0.1344 → −0.1019 and the book was −$30,707 → −$16,557. Both
> recipes show the same *shape* (EV up, fewer trades, net up), but
> the levels are **not comparable** — the trailing stop changed the
> book from 27,619 legs to 6,753. A number from one recipe must
> never be quoted against the other.

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

## 3.10 THE 5-EVENT SEQUENCE WHITELIST — `structure_seq_whitelist`

**PROMOTED 2026-11-04.** The canonical value is a set of **25
`<side>|<5-char signature>` keys**: **18 long**, **7 short**.

> The user asked (2026-11-03): *"tabulate bos/choch 5-event sequence
> and the subsequent entries. split between longs and shorts. then, we
> know which 5-seq events to whitelist before each long/short"*.

### What it is

A **whitelist**, not a blacklist: a key PRESENT means **TAKE** that
trade, a key ABSENT means **DROP** it. The signature is the trailing
**5** BoS/CHoCH events at the **signal** bar, oldest first
(`b`=BoS bull, `B`=BoS bear, `c`=CHoCH bull, `C`=CHoCH bear).

It is a **separate knob** from `structure_seq_drop_set` on purpose:
the drop set is 1-event granularity (*"what is the last break?"*), the
whitelist is 5-event (*"what are the last FIVE breaks?"*). Different
questions, different evidence, so they must move independently.

### The measurement

`runtime/diagnostics/seq5_table.py` over the **full corpus**
(2024-01-01 → 2026-07-16, one continuous execution, 14d warmup,
64,978 legs on the unfiltered book). A signature qualifies when its
gross/leg is positive on **both halves** of the corpus:

| side | qualified | top of the table (gross/leg, H1, H2) |
|---|---|---|
| **long** | **18 of 64** | `Ccbbb` +0.565 (+0.199, +0.952) · `cCBcC` +0.440 (+0.118, +0.762) · `cCcCc` +0.366 (+0.061, +0.617) |
| **short** | **7 of 64** | `BcCcC` +0.480 (+0.390, +0.570) · `CBBcC` +0.415 (+0.633, +0.153) · `cbbCB` +0.364 (+0.167, +0.539) |

### The walk-forward (2 folds by time, 2000 random-whitelist permutations as the null)

| side | fold | real Δgross | null p95 | percentile |
|---|---|---|---|---|
| long | H1 → H2 | +$1,159 | +$1,725 | 69.3% |
| long | H2 → H1 | +$1,069 | +$973 | **97.9%** |
| short | H1 → H2 | +$3,504 | +$3,506 | **95.0%** |
| short | H2 → H1 | +$2,258 | +$2,386 | **87.0%** |

**The short side is the one that earns the slot** — both of its folds
beat "trade less at random". The long side's two folds disagree
(69% vs 98%), so the long set is the weaker half of the promotion.

### ⚠️ The honest limits — a QUALITY knob, not an EDGE knob

1. **The kept book is still gross-negative** and its per-leg edge is
   **fee-limited**. This is the same verdict the
   `vol_state_min_expand_prev_day` gate got (§ 3.7.2): the filter
   makes the surviving trades much better, not profitable.
2. **The shipped set is a whole-corpus fit.** The walk-forward
   validates the *shape* of the rule (fit on one half, score on the
   other). The **engine-level** walk-forward — fit the set on H1,
   rebuild the H2 ledger with it — is still OPEN.
3. **Post-hoc is not the book.** The table is a leg filter; the
   engine-side whitelist re-runs the book, so the conflict policy,
   the same-direction cap and the `contra_close` netting are all
   re-evaluated. See § 3.11.

### 3.11 The rule this promotion exposed

> **A drop-set or whitelist MUST be measured END TO END by re-running
> the engine.** The post-hoc leg filter is a *screen* for which
> hypotheses are worth a re-run — never the verdict itself.

`contra_close` nets opposing legs **pairwise**, so removing legs
removes the netting's counterparties. A post-hoc prediction cannot
see that. Measured on the corpus: the post-hoc table said `short|B`
was the best drop set (positive on both halves); the engine-side
re-run said it was the **worst** of the three tested.

Driver: `runtime/diagnostics/seq_whitelist_corpus_ab.py`.

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
> The decision criterion is **EV (gross per leg)**: EV rises
> monotonically 0.2502 → 0.3177 → 0.3189 → **0.3434** and peaks at
> `1.0`. Net improves in step (−$2,921 → −$1,074). Full table and
> the honest limits (book still negative, EV lift not statistically
> separable, trade count 7.28/day far below the 30–70 band) in
> § 3.7.2.
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
| 6 | `vol_state_min_range_norm` × `sl_max_usd` | `0.5,1.0,1.5,2.0` × `2.5,4,6,8,12` | **PAIRED.** The gate and the ceiling were never independent. The `hvn_single` change moved the payoff geometry, so this pair **must** be re-measured |
| 7 | `vol_state_min_range_norm` × `vol_state_min_expand_prev_day` | `0.5,1.0,1.5` × `0,0.9,1.1` | **Two vol gates at once** — are they redundant? Both ask "is the market moving" |

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
| `structure_gate` | measured and **NOT PROMOTED**; the market-beta correlation is unchanged |
| `trade_sides="long"` | measured and **NOT PROMOTED**; raises market correlation to +0.464 |
| any HVN/LVN VP knob | the VP profile is **INERT** on this corpus — do not promote on the strength of that finding |

---

## 5. The evaluation harness

These are the tools. Use these; do not roll your own.

| command | what it does |
|---|---|
| `python -m runtime.diagnostics.leg_ledger` | Build/reuse the corpus-wide per-LEG record (continuous run) |
| `python -m runtime.diagnostics.leg_buckets` | The four buckets `(side × outcome)` + drift/excess + payoff geometry |
| `python -m runtime.diagnostics.leg_random_entry` | The random-entry control — **CONFOUNDED**, see `docs/evaluation.md` |
| `python -m runtime.diagnostics.zone_shuffle_placebo` | The decisive level-vs-timing test — **⚠️ PROMPT-ONLY** |
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

**Q1 — is there an edge at all?** Compare the per-leg gross EV against
the **measured** round trip. The report prints `gross_per_leg /
fee_per_leg` so the comparison is explicit. A gross edge that does not
cover the round trip is **not** an edge.

**Q2 — is the edge direction-independent?** The **drift / excess**
decomposition, at the leg level:

\[
\text{drift} = (close_{\text{exit bar}} - close_{\text{entry bar}})
\cdot \text{direction} \cdot \text{qty}
\qquad
\text{excess} = \text{gross} - \text{drift}
\]

`drift` is the directional/beta component — what any position of that
size and side would have earned by being there. `excess` is the
timing component. **This is an exact algebraic identity**, not an
estimate.

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
| The coarse **polarity** whitelist (bull/bear/mixed buckets) | **DEAD** — held-out kept trades have aggregate t = −2.15 (significantly loss-making). **A coarse polarity bucket does NOT carry the signal — the specific 5-event shape does.** |
| The sequence flip | **DEAD** — "does NOT generalise, −$8 on held-out slices" |
| `structure_gate` | **NOT PROMOTED** — improves gross but leaves market correlation untouched |
| `trade_sides = "long"` | **NOT PROMOTED** — raises market correlation to +0.464 |
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
| **`evaluate`** | Judging a run: the ledger, the four buckets, drift/excess, the payoff geometry. **Before any promotion.** |
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
| **`evaluate`** | Judging a run: the ledger, the four buckets, drift/excess, the payoff geometry. **Before any promotion.** |
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
