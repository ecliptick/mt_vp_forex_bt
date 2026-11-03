# Sweeping — policy, cost model, and the one-month default

> Relocated from `AGENTS.md` on 2026-11-03.
>
> The rule is one line in `AGENTS.md`: **one month by default; ask before a
> full-corpus run.** Everything else — the cost model, the `--i-know` gate,
> the output contract — is here.

## Contents

- [Sweeping: 1 month by default](#sweeping-1-month-by-default-always-ask-before-a-full-run)
- [The measurement WORKFLOW](#the-measurement-workflow--cheap-first-expensive-last)
- [Sweep policy](#sweep-policy--1-month-by-default-ask-before-the-full-run)
- [Measured cost model](#measured-cost-model-xauusd-1s-1y-file)
- [Per-element JSON output](#per-element-json-output-sweep-output-policy)

## Sweeping: 1 month by default, always ask before a full run

**Never launch a multi-month or full-corpus sweep without asking.**
The policy is in § "Sweep policy" below. In short:

* Default to **one month** (~6 s end to end).
* **The driver ENFORCES the ask.** A window longer than 31 days
  prints its cost estimate and **exits with code 2** unless
  `--i-know` is passed. The default window is one month, so the
  cheap thing is the default thing.
* Even so, **state the window, the cell count and the cost estimate
  in chat before launching a long run**, and get the user's
  go-ahead. The gate protects against an *accidental* long run; it
  does not replace telling the user what you are about to do.
* Every sweep is judged on the **leg ledger**, not on the month's
  net.

## The measurement WORKFLOW — cheap first, expensive last

**This is the routine. Follow it in this order and do not skip
ahead.**

| step | command | cost | what it tells you |
|---|---|---|---|
| **1. one month** | `python -m runtime.sweeps.cached_sweep --knob X --grid ...` | **~6 s** | is the knob **live**? does the metric move at all? |
| **2. the ledger** | `python -m runtime.diagnostics.leg_ledger` then `leg_buckets` | **~90 s** cold, **~1 s** cached | the **real** verdict: buckets, drift/excess, payoff geometry |
| **3. the trade count** | `python -m runtime.diagnostics.volgate_trade_count` | ~15 min (6 cells) | does the book land in the **30-70 trades/day** band? |
| **4. the placebo** | `python -m runtime.diagnostics.zone_shuffle_placebo` | ~10 min | is the edge in the **level** or the **timing**? **⚠️ ONLY WHEN THE USER PROMPTS FOR IT** — see rule 5 |
| **5. the full corpus** | `--i-know` + explicit go-ahead | **~3 min/cell** | only for a candidate that survived 1–4 |

> **⚠️ Step 4 (the placebo) is PROMPT-ONLY.** Never launch it
> unprompted: it costs ~10 minutes, it **rewrites
> `zone_shuffle_placebo.json`**, and the guard tests read that
> artifact. The answer is already recorded in § 2b / § 2c. If a
> claim needs re-confirming, **ask the user first**. Steps 1-3 are
> always allowed.

> **The ledger is cheap, and it is cached by fingerprint.** The engine
> walk is **82.3 s** on the 1y corpus (measured 2026-10-25); the
> artifact build is cached alongside it, so a repeat run is **~1 s**.
> The ledger's fingerprint covers the data file, the window and the
> engine kwargs — so it rebuilds automatically when any of those
> change, and never goes stale silently.
>
> **⚠️ The ledger is no longer ~90 s on the WIDE book.** With the
> canonical recipe at vol gate 1.0 (51,075 legs), the engine walk is
> **~300 s** — measured 2026-10-25. The artifact build dominates the
> rest.

**Rules that keep it fast.**

* **A month is enough to answer "is this knob live?"** — a knob
  that does not move a single metric on 1 month will not move it on
  12. Do not spend 3 minutes/cell proving a null.
* **Never sweep a knob you have not read the canonical config for.**
  The canonical config table says which knobs are live, which are
  inert (`sl_mode` at the $2.50 ceiling), and which are dead. A
  sweep of an inert knob is a sweep that measures nothing — the
  exact failure the repo keeps documenting.
* **Do not re-measure the same cell twice.** Every cell writes its
  own JSON under `runtime/sweeps/_results/`. Check the directory
  before running.
* **One 1y process at a time.** Two concurrent 1y runs cost a
  **9×** slowdown each (memory contention), not a 2× — so
  "parallelise the sweep" is a *pessimisation* at this scale.
* **The `--i-know` gate is not permission.** It is a guard against
  accident. A long run still needs a **message to the user** with
  the window, the cell count and the estimate.

> **Why the workflow is ordered this way.** The corpus-wide ledger
> run costs ~90 s and gives a *better* answer than a 12-month sweep
> cell, because it pools every leg instead of reading one month. So
> the full corpus is not "the thorough version of the monthly
> sweep" — it is a **different measurement** with a different cost.
> Use the ledger.

---

## The SWEEP QUEUE — what to sweep next, in order

> **This is the queue. It is a PLAN, not a licence.** The order is
> decided here so it does not have to be re-decided per session. Do
> not launch it unprompted — and do not re-run an entry whose numbers
> already exist (`runtime.results.cli history` first).

The full table, with shipping bars, lives in
[`AGENTS.md` § 4.1](../AGENTS.md). This is the summary.

| tier | knob(s) | grid | why it is in the queue |
| --- | --- | --- | --- |
| **1** | `vol_state_max_expand_prev_day` | `0,0.5,0.6,0.7,0.8` | the inverse gate — vol **contraction** rejection |
| **2** | `tp_mode` | `multi_target,hvn_single,fvg_mult` | the exit-shape A/B; needs the ledger, not the month's net |
| **2** | `nth_hvn_n` | `1,2,3,4,5` | **re-measure under `hvn_single`** — `3` was tuned for the old geometry |
| **3** | `vol_state_min_range_norm` × `sl_max_usd` | `0.5,1.0,1.5,2.0` × `2.5,4,6,8,12` | **PAIRED** — never independent. The `hvn_single` change moved the payoff geometry |
| **3** | `vol_state_min_range_norm` × `vol_state_min_expand_prev_day` | `0.5,1.0,1.5` × `0,0.9,1.1` | are two vol gates redundant? |
| **4** | `vol_state_min_vel_5m` / `_vel_15m` / `_min_rv_ratio_30s` / `_max_rv_ratio_30s` / `_max_wick_ratio` | see AGENTS.md § 4.1 | dormant measures, **never swept**. Now sweepable without crashing (see below) |
| **5** | `contra_close_spread_usd` | `1.0,2.0,3.0,4.5,5.0,6.5,8.0` | the canonical `$5.00` was **set by instruction**, not picked from the ladder |
| **6** | `sl_mode` / `sl_nth_hvn_n` / `hvn_buffer_usd` | see AGENTS.md § 4.1 | only became live on 2026-10-25 (the SL ceiling moved to `$8.00`) |

### The CLI limitation that bites on tiers 2 and 5

`--knob X --grid a,b,c` parses values as **int, then float** — it
cannot carry a string. So a string knob like `tp_mode` or
`fill_model` must go through the **multi-knob** path:

```bash
# WRONG — ValueError: invalid literal for int() with base 10: 'multi_target'
python -m runtime.sweeps.cached_sweep --knob tp_mode --grid multi_target,hvn_single

# RIGHT
python -m runtime.sweeps.cached_sweep --knobs "tp_mode=multi_target,hvn_single,fvg_mult"
```

`_parse_grid_values` tries int → float → **boolean** → string, in
that order. The boolean case matters: `structure_gate=False,True`
would otherwise become two truthy strings and the A/B would silently
run the same configuration twice.

### The `vol_state` measure-set fix (2026-11-03)

`cached_sweep.py` and `directional_beta.py` used to build only three
`vol_state` measures:

```python
measures=("vel_5m", "range_norm_1m", "wick_ratio")
```

Any threshold for a measure that was **not** materialised raised
`KeyError` deep inside the engine's `_vol_state_pass`. Sweeping
`vol_state_min_vel_15m` therefore **crashed** rather than measuring
anything — the worst kind of dead knob, because it looks like a bug
in the sweep rather than a missing measure.

Both call sites now build the **full** set:

```python
measures=(
    "vel_5m", "vel_15m",
    "range_norm_1m", "wick_ratio",
    "rv_ratio_30s", "shock",
    "vol_expand_prev_day",
),
times_ns=to_ns_arr(bars),
```

Cost: **~1.9 s** per month (measured 2026-11-03, up from ~1.2 s) —
negligible against a ~6 s cell, and it makes **every** threshold in
the allowlist sweepable. `vol_expand_prev_day` additionally needs
`times_ns` (int64 epoch nanoseconds) because it needs the calendar-day
boundary; `build_vol_state` **raises** if the measure is requested
without it.

---

## Sweep policy — 1 month by default, ask before the full run

**This is the operating rule for this repo.** The recipe is
high-frequency and the measurement unit is the leg, so the sweep
policy has to match the strategy, not the habit of a monthly
backtest.

### The rules

1. **Default to ONE month.** A 1-month canonical sweep costs
   **~6 s** end to end (measured 2026-10-25: 1.6 s bars + 2.0 s
   artifacts + 1.6 s engine). There is no excuse for running more
   than you need.
2. **Report the full-run cost before running it — AND THE DRIVER
   ENFORCES IT.** The `--i-know` approval gate is **implemented**
   (2026-10-25, `runtime/sweeps/cost_estimate.py`):
   * a window longer than **31 days** prints its estimated cost and
     **exits with code 2 without running anything**, unless
     `--i-know` is passed;
   * the default window is **one month** (`2025-01-01` →
     `2025-02-01`), so the cheap thing is the default thing;
   * the estimate is computed from the parquet **time column only**
     (~0.5 s, exact bar count), so the pre-flight is essentially
     free.

   > **⚠️ This gate did NOT exist until 2026-10-25.** For several
   > revisions this file documented an `--i-know` flag and an ETA
   > prompt that were **never implemented** — the safety was
   > *imagined*. A rule written only in prose is a rule nobody
   > enforces. If the gate ever misbehaves, check
   > `runtime/sweeps/cost_estimate.py` and
   > `tests/test_sweep_policy.py` — not this file.
   >
   > The old default was `--start None --end None`, which silently
   > loaded the **entire 1y corpus** (41M bars, ~2 GB) for a sweep
   > that was almost always read off one month. That default is
   > gone.
3. **Never run two 1y processes concurrently.** They exhaust memory
   and the bar loop stalls for hours (see § "Operational gotcha").
   Measured 2026-10-25: the 1-month artifact build took **18–25 s**
   with a competing process running and **2.0 s** without — a **9×**
   difference on identical code.
4. **Judge the sweep on the LEDGER, not on the month's net.** A
   month's net is one draw. Re-run `leg_ledger` + `leg_buckets` on
   the continuous corpus before believing any cell.

### The one-month window

```bash
# canonical recipe, one month, ~6 s  (the dates are the DEFAULT)
python -m runtime.sweeps.cached_sweep --knob _canonical

# a single knob, warm cache
python -m runtime.sweeps.cached_sweep \
  --knob sl_zone_mult --grid 10,20,40,80 \
  --start 2025-01-01 --end 2025-02-01

# MULTI-knob grid (cartesian product) — the LAST knob varies fastest.
# Each cell writes its own JSON under
# runtime/sweeps/_results/_multi/<scope>__cell_<idx>.json
python -m runtime.sweeps.cached_sweep \
  --knobs "sl_zone_mult=4,8,12,20,40;max_same_dir_open=10,20" \
  --start 2025-01-01 --end 2025-02-01

# force a rebuild
python -m runtime.sweeps.cached_sweep --knob _canonical --no-cache
```

### The confirmation prompt — IT IS NOW REAL

A window longer than one month **prints the estimate and exits**:

```bash
# Prints the estimate, then ABORTS with exit code 2.
python -m runtime.sweeps.cached_sweep --knob _canonical \
  --start 2024-01-01 --end 2026-07-16

# Already approved by the human — this one RUNS.
python -m runtime.sweeps.cached_sweep --knob _canonical \
  --start 2024-01-01 --end 2026-07-16 --i-know
```

The abort prints:

```
  window      : 2024-01-01 .. 2026-07-16   (927 days)
  bars        : ~41,233,730
  artifacts   : COLD (cache MISS)
  load        : ~7.0 s
  artifact    : ~97.2 s
  engine/cell : ~58.5 s
  cells       : 1
  ---------------------------------------------------------------
  ESTIMATED   : ~2.7 min total
  ---------------------------------------------------------------
  NOTE        : this window is longer than 31 days.
                The monthly chunking is an ENGINE artefact --
                the p95 cap (7d), swing cap (24h) and VP profile
                (7d rolling) are history-dependent, so a cold
                chunk trades its first ~8 days UNCAPPED and
                discards part of the trade universe.
                Verify on the continuous corpus with leg_ledger
                + leg_buckets before believing a cell.
  ---------------------------------------------------------------
  re-run with --i-know to execute.

ABORTED: window is longer than one month. Re-run with --i-know to execute.
```

> **Why the estimate is trustworthy.** The bar count is read from
> the parquet **time column**, so it is exact. The rates are
> MEASURED on this machine, not guessed: engine **705,000 bars/s**
> (from the 1y run's own progress log: `bar=41000000/41233730
> elapsed=56.3s rate=728335/s`), artifacts **424,000 bars/s**
> (97.3 s for 41.2M bars), load **7.0 s** for the 41.2M-bar file.
>
> An earlier version of the estimator interpolated within the
> parquet footer's row-group statistics. That was **rejected**: FX
> bars are not uniform in wall-clock time (weekend gaps), so the
> interpolation was off by **−6% to +8%** depending on the window.
> Reading the time column costs 0.5 s and is exact.

**Even with the gate, state the window and the estimate in chat
before launching.** The gate protects against an *accidental* long
run; it does not replace telling the user what you are about to do.

### Measured cost model (XAUUSD 1s, 1y file)

| scope | bars | artifact build (cold) | engine cell | total, 1 cell |
| --- | --- | --- | --- | --- |
| 1 month (2025-01) | 1,377,190 | **2.0 s** | **1.6 s** | **~6 s** |
| 1y (2024-01 → 2026-07) | 41,233,730 | **86.5–97.3 s** | **51.0–58.5 s** (narrow book) | **~2.3–2.7 min cold, ~1 min warm** |
| 1y, **WIDE book** (vol gate 1.0) | 41,233,730 | cached | **~300 s** (51,075 legs) | **~5 min** |

> **⚠️ The engine cell cost depends on the BOOK SIZE, not just the
> bar count.** On the narrow book (vol gate 3.0, 1,692 legs) the 1y
> engine cell is ~51 s. On the **wide book** (vol gate 1.0, 51,075
> legs — the 2026-10-25 canonical recipe) it is **~300 s**, measured
> 2026-10-25. The per-bar open-book scan is `O(n_bars × n_open)` and
> the wide book holds far more positions. **Any 1y measurement on the
> current canonical recipe costs ~5 minutes per cell, not ~1.**
> `runtime/diagnostics/volgate_trade_count.py` (6 cells) therefore
> takes ~15-30 minutes, not ~10.

Cold 1y artifact build breakdown (measured 2026-10-25, from
`build_secs` in the artifact cache; two independent builds):

```
artifacts MISS in 86.5s (114,479 zones at the 1m cadence)
 zones      13.5s   <- detect_fvg, 1m cadence
 vp_set     25.1s
 swing_cap   3.7s
 p95_cap     2.2s
 structure  31.1s
 vol_state  10.9s
```

> **Two corrections to earlier drafts of this table.** An earlier
> version claimed **~190 s** (its stage numbers did not sum to it)
> and another claimed **~10 s** (it read the `detect_fvg` stage
> alone as the whole build). The measured total is **86.5 s**, and
> the **largest single stage is `structure` (31.1 s)**, not
> `detect_fvg` (13.5 s).
>
> A second cold build measured **97.3 s** with `structure` at
> **42.7 s** — the extra came from a concurrent process competing
> for memory bandwidth. **Treat 86.5–97.3 s as the range.**
>
> **The artifact build is now the DOMINANT cost of a cold 1y cell,
> not the engine.** ~87–97 s of artifacts vs. 51.0 s of engine.
> `structure` and `vp_set` are the two stages worth optimising if
> cold-build time ever matters — and both are **cached**, so this
> is paid once per fingerprint, not once per sweep cell.

Engine bar rate on the 1y run: **~730,000–790,000 bars/s**, with a
**measured max open depth of 2** — the vol gate drops 56% of
signals, so the book is nearly always empty and the
O(n_bars × open) scan is effectively free.

> **⚠️ Never run two heavy cells concurrently.** AGENTS.md's
> "Operational gotcha: memory contention kills 1y runs silently"
> applies. The 1-month build measured **18–25 s** while a second
> process was running, and **2.0 s** when it was not — a **9×**
> difference on the same code.

The artifact cache is keyed by the file (size + mtime), the date
slice, the resolved detector / VP / swing / p95 / structure kwargs,
and a SHA1 over the artifact-producing source files. So **a knob
that only touches engine kwargs reuses the cache on every cell**;
a detector / VP / structure knob rebuilds it.

### Per-element JSON output (sweep output policy)

**Rule:** each sweep cell writes its own JSON the moment that cell
finishes. **No full-sweep JSON written at the end.**

The point is fault tolerance — a 60-cell sweep that crashes on cell
57 should still have 57 readable result files on disk, not 0.

```
runtime/sweeps/_results/<knob>/<scope>__cell_<idx>.json
```

Each file is a single JSON object:

```json
{
 "ts_utc": "2026-10-20T19:42:00Z",
 "notebook": "nb86_sl_tp_zone_mult_sweep",
 "knob": "sl_zone_mult",
 "value": 40.0,
 "cell_idx": 3,
 "scope": ["xauusd_s1_1y"],
 "engine": "vp_multi_target",
 "wall_secs": 142.7,
 "cache_hit": true,
 "metrics": { "n_trades": 1842, "hit_rate": 0.512, "...": "..." },
 "overrides_vs_canonical": {"sl_zone_mult": 40.0}
}
```

### Anti-pattern (do NOT do this)

```python
# WRONG — one file at the end of the loop, no per-cell recovery
results = []
for value in grid:
    res, _ = run_vp(...)
    results.append((value, res))
with open("sweep.json", "w") as f:
    json.dump(results, f)  # crash here = total loss
```

### The sweep driver: `runtime/sweeps/cached_sweep.py`

This is the ONLY sweep driver in the repo. It replaces the old
`single_sweep.py` (which reported empty metrics — it read
`result.calmar` / `result.n_trades`, none of which exist on
`BacktestResult`) and `run_dual_sweep.py` (which hardcoded detector
knobs that disagreed with the canonical recipe).

**Detector config must match the canonical config.**
`detector_kwargs_from_recipe()` reads the canonical constants via
`build_detector_kwargs()`. The old driver hardcoded
`fvg_min_zone_usd=0.10` while the canonical says `0.30`. That 3×
threshold difference produced **425,094 zones per month instead of
9,684** (44×), which flooded the open book and made the bar loop's
`O(n_bars × n_open_positions)` check run for hours. It was also
silently trading a different, far higher-frequency strategy than the
recipe specifies.

Guard: `tests/test_regressions.py::test_detector_kwargs_match_the_canonical_recipe`.

### The one-month sweep is NOT the canonical measurement

This is the most important caveat in the file, and it is a
**correctness** issue, not a preference.

The engine's caps are history-dependent:

| cap | lookback | cold-start behaviour |
| --- | --- | --- |
| p95 TP cap (NB91) | 7 days | returns `inf` → **uncapped TP** |
| swing TP cap (NB72) | 24h | degenerate on day 1 |
| VP profile | 7d rolling | cold shelves |

So a chunked run's first ~8 days trade with **different targets**
than the same days in a continuous run.

Measured on **2025-01** (canonical recipe, 1.00 oz):

| execution | legs | gross | fees | net | median TP |
| --- | --- | --- | --- | --- | --- |
| chunked, no warmup (what `cached_sweep` does) | 41 | **+$11.12** | $19.13 | **−$8.02** | $6.15 |
| chunked + 14d warmup prefix | 60 | −$22.82 | $27.28 | −$50.11 | $5.60 |
| **continuous corpus, sliced by signal time** | 41 | **−$27.27** | $15.73 | **−$43.00** | $6.08 |

Same leg count, same calendar window, **$35 apart in net** — and the
sign of the gross flips. Reproduce:

```bash
python -m runtime.diagnostics.chunk_vs_continuous
python -m runtime.diagnostics.warmup_prefix_effect
```

**The fix for the driver** is a warmup prefix: execute from
`start − 14 days` (14 days exceeds the longest lookback: p95's 7
days, the swing cap's 24h, the VP profile's 7d rolling) and keep
only the scored window's legs. `leg_ledger` already does this
(`WARMUP_DAYS = 14`).

---
