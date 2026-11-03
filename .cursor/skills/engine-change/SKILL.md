---
name: engine-change
description: >-
  Change the SNIPER backtest engine, detector, or config safely — covering
  nanosecond timestamps, rolling-window look-ahead guards, the resting-order
  entry model, silent-corruption fields, and the requirement to re-measure
  every affected number after a change. Use when editing anything under
  bt/, bt/_engine/, or bt/optimal_config.py, when fixing a backtest bug, or
  when a code change invalidates previously measured results.
disable-model-invocation: true
---

# Engine change — change the code, then re-measure everything it touched

## The re-measurement rule

**A code change invalidates every number it could affect.** This repo has
been bitten repeatedly by changing the engine and leaving old measurements
standing.

Concretely: after changing anything under `bt/_engine/` or `bt/optimal_config.py`,
say out loud which previously-measured numbers are now suspect. If the answer
is "I don't know", it is all of them.

The registry makes this checkable:

```bash
python -m runtime.results.cli stale    # runs measured on an older commit
```

## The invariants that have been broken before

### Timestamps are nanoseconds — always

Every timestamp crossing a module boundary is **int64 epoch nanoseconds**. The
canonical parquet stores `timestamp[ms, UTC]`, and pandas preserves that
resolution.

```python
# WRONG — yields MILLISECONDS (1_784_143_874_000)
bars["time"].astype("int64")

# RIGHT
from bt._engine.timebase import to_ns_arr
times = to_ns_arr(bars)
```

Mixing the two is silent and catastrophic. The original canonical run emitted
**264,666 signals and closed 0 trades** because the p95 cap computed
`warmup_until_ns` in real nanoseconds while the engine handed it millisecond
timestamps.

### Any rolling-window statistic that reads a buffer needs a TWO-SIDED mask

```python
# WRONG — a look-ahead whose severity scales with file length
mask = ts >= cutoff

# RIGHT
mask = (ts >= lo) & (ts <= hi)
```

A one-sided mask is invisible in unit tests, invisible on short slices, and
**maximal on the long run**. The swing cap's bug inflated the TP cap **18.7x**
($92.66 -> $1,733.62), putting every TP out of reach — and 1-week and 1-month
backtests still looked fine.

**Corollary:** bar aggregation buckets by **timestamp**, never by row
position. Real FX data has weekend/rollover gaps (15.018% of the 1y file's
bars are >1s apart).

### A guard that lives in the callee but is never exercised by any caller is not a guard

The detector's resample fallback warned on every call and nothing failed. A
warning is not a guard — **the guard must live at the call site**.

When you add a required argument (like `times_utc_ns`), **walk every call
site**. There is an AST guard for this:
`tests/test_regressions.py::test_every_detect_fvg_call_site_passes_times_utc_ns`.

### A field with a DEFAULT is a silent-corruption field

`ClosedTrade` has `signal_bar: int = 0`, `zone_width: float = 0.0`,
`zone_low`/`zone_high: float = 0.0`. Any construction site that forgets one
produces plausible-looking numbers that are simply wrong — no crash, no
warning.

**Walk every construction site and assert the required fields are passed.**
Guard: `tests/test_signal_bar_completeness.py`.

> **A bug's blast radius scales with how often its code path runs.** The
> `contra_close` branch's missing `signal_bar` was harmless at 93 legs and
> corrupted 17,211. When a knob widens the book, re-run the invariants — the
> bug was always there, only the exposure changed.

### A bound enforced in one branch of a switch must be enforced in every branch

`sl_mode='fvg_or_poc'` ignored BOTH `sl_min_usd` and `sl_max_usd`, so a
`sl_mode` sweep would have shown a `$19.50` stop while its siblings sat at
`$2.50`. Guard:
`tests/test_smoke.py::test_every_sl_mode_honours_the_floor_and_the_ceiling`.

### A module must not do work at IMPORT time

Anything that reads data, runs a backtest, or reaches outside the repo belongs
under an `if __name__ == "__main__":` guard. Guards:
`tests/test_no_dead_imports.py::test_no_module_does_work_at_import_time` and
`::test_no_module_imports_from_an_upstream_repo_at_module_scope`.

## The config surface

**One place only:** `bt/optimal_config.py`. Seven builders, each with its own
allowlist. A knob not on an allowlist raises `KeyError` before any cell runs.

```python
build_engine_kwargs(**over)     # -> run_vp_sniper_backtest
build_vp_kwargs(**over)         # -> build_volume_profiles
build_detector_kwargs(**over)   # -> detect_fvg
build_swing_kwargs(**over)      # -> build_daily_swing
build_p95_kwargs(**over)        # -> build_rolling_p95_from_bars
build_live_p95_kwargs(**over)   # -> make_live_rolling_p95
build_structure_kwargs(**over)  # -> detect_market_structure
```

The **two p95 builders are not interchangeable**: the backtest path pins
`seed_constant_usd=0.0`, the live path uses the baked-in coldstart constant.
Keeping them separate guarantees the backtest can never pick up the live seed
and silently move every measured number.

## The entry model — do not "fix" it silently

The D2 model: a signal **places** a `PendingOrder`; it fills on a **strictly
later** bar whose range touches the limit level; if the zone dies first the
order is **cancelled**. Median wait is ~4,659 bars on the wide book.

The TP1 branch ends in `continue`, so the freshly-created remainder is **not
re-scanned** against the bar that fired TP1. A bar that reached TP1 and also
traded back through the entry price lets the remainder escape an already-
breached breakeven stop.

**This is FAVOURABLE to the strategy and INCONSISTENT with
`fill_model='pessimistic'`. Do not "fix" it silently** — it moves P&L, and
every number in the repo was measured with it in place. If it is ever changed,
re-measure the canonical slices. Guard:
`tests/test_regressions.py::test_tp1_remainder_is_not_rescanned_against_the_tp1_bar`.

## The workflow

### 1. Before you touch anything

```bash
python -m runtime.results.cli list        # what is currently on record
python -m runtime.results.cli stale       # what predates the current commit
```

### 2. Make the change

Keep it minimal. One behaviour per change.

### 3. Run the guards

```bash
python -m pytest tests/ -q
```

The AST guard suites are fast and catch the whole classes of bug listed
above. `test_every_module_imports_cleanly` should finish in **~1 s** — if it
takes minutes, something started doing work at import time.

### 4. Re-measure the affected numbers

```bash
python -m runtime.diagnostics.leg_ledger --refresh
python -m runtime.diagnostics.leg_buckets
```

Then **record the new run** — the registry is how the change becomes visible:

```bash
python -m runtime.results.cli history --kind leg_ledger --params '{...}'
```

Same `config_fp`, new `commit` — that is the "what did the fix do" view.

### 5. Update the docs that name the number

- `AGENTS.md` — the canonical config table, the DEAD table.
- `docs/engine_history.md` — add the defect and its fix, so nobody
  re-introduces it.
- The doc that quoted the old number.

> **Rule: when a change to the canonical recipe lands, every number that
> was measured on the OLD recipe must be labelled with the recipe it belongs
> to.** Two books live side by side and quoting them interchangeably is the
> fastest way to make the docs wrong.

## Performance notes (only if you touch the hot paths)

| concern | what to know |
|---|---|
| 1m resample aggregation | must use `np.maximum.reduceat`, not a per-bucket mask. A per-bucket mask over a full-length array is **O(n²)** — it cost ~8 hours on the 1y corpus |
| supersession | needs the price-bucket spatial index; the naive scan is O(Z²) = 15.7 h |
| the engine bar loop | the per-bar book scan is `O(n_bars x n_open)`. `open=` in the progress log is the multiplier on total runtime |
| memory | two 1y runs concurrently cause a **9x** slowdown and can OOM silently. Never do it |

## Reference

- [`AGENTS.md`](../../../AGENTS.md) § 1.4, § 1.5, § 1.6, § 3, § 4.
- [`docs/engine_history.md`](../../../docs/engine_history.md) — every defect the
  repo has already paid for.
