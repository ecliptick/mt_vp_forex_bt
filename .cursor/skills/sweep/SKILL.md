---
name: sweep
description: >-
  Sweep a knob in the SNIPER XAUUSD backtest repo using the cached sweep
  driver, with the one-month default window, the --i-know approval gate, and
  per-cell JSON output. Use when asked to sweep, grid-search, try values for
  a parameter, or find the optimum for a knob such as sl_max_usd,
  vol_state_min_range_norm, nth_hvn_n, vp_rows, or structure_pivot_len.
disable-model-invocation: true
---

# Sweep — one month by default, ask before a full run

## The rule

**Default to ONE month.** A 1-month canonical sweep costs ~6 s end to end. The
dates are the default; the driver enforces them.

A window longer than 31 days **prints its cost estimate and exits with code
2** unless `--i-know` is passed.

> **The `--i-know` gate is not permission.** It guards against an *accidental*
> long run. A long run still needs a **message to the user** with the window,
> the cell count and the estimate, and their go-ahead.

## The workflow

### Step 1 — read the canonical config first

```bash
grep -n "sl_max_usd\|vol_state_min_range_norm\|nth_hvn_n" bt/optimal_config.py
```

**Never sweep a knob you have not read the canonical config for.** The
canonical config table says which knobs are live, which are inert, and which
are dead. A sweep of an inert knob measures nothing — that is the exact
failure this repo keeps documenting.

Dead knobs to never sweep: `commission_per_lot_usd`, `spread_per_oz_usd`
(stripped by the forex wrapper), `sl_atr_mult` (removed), `poc_buffer_usd`
(only read under `sl_mode='fvg_or_poc'`).

**PAIRED knobs.** `sl_max_usd` and `vol_state_min_range_norm` must ALWAYS be
re-measured together — the SL ceiling was measured with the gate off
(optimum $5.00) and with it on (optimum $2.50-$3.00).

### Step 2 — one month, one knob

```bash
python -m runtime.sweeps.cached_sweep --knob sl_zone_mult --grid 10,20,40,80
```

Defaults to the 2025-01 window. ~6 s.

### Step 3 — multi-knob grid

```bash
python -m runtime.sweeps.cached_sweep \
  --knobs "sl_zone_mult=4,8,12,20,40;max_same_dir_open=10,20"
```

Cartesian product; the LAST knob varies fastest.

### Step 4 — force a rebuild

```bash
python -m runtime.sweeps.cached_sweep --knob _canonical --no-cache
```

### Step 5 — the full corpus (ask first)

```bash
python -m runtime.sweeps.cached_sweep --knob _canonical \
  --start 2024-01-01 --end 2026-07-16 --i-know
```

## What gets swept

The sweep surface is **the seven builders** in `bt/optimal_config.py`:

| Builder | Surface |
|---|---|
| `build_engine_kwargs` | `run_vp_sniper_backtest` |
| `build_vp_kwargs` | `build_volume_profiles` |
| `build_detector_kwargs` | `detect_fvg` |
| `build_swing_kwargs` | `build_daily_swing` |
| `build_p95_kwargs` | `build_rolling_p95_from_bars` |
| `build_live_p95_kwargs` | `make_live_rolling_p95` |
| `build_structure_kwargs` | `detect_market_structure` |

A name on no allowlist raises `KeyError` **before any cell runs**. That is
intentional — a knob that is not on an allowlist does not exist, and can never
become a silent no-op.

**Detector config must match the canonical config.** The driver reads it via
`build_detector_kwargs()`. The old driver hardcoded `fvg_min_zone_usd=0.10`
while canonical is `0.30` — a 3x threshold difference that produced 44x more
zones and silently traded a different strategy.

## The output contract

Each cell writes its OWN JSON the moment it finishes:

```
runtime/sweeps/_results/<knob>/<scope>__cell_<idx>.json
```

**No full-sweep JSON written at the end.** A 60-cell sweep that crashes on
cell 57 still leaves 57 readable files on disk.

The same measurement is ALSO recorded in the durable results registry — see
the `results-registry` skill. The registry copy is the committed one.

## The one-month sweep is NOT the canonical measurement

This is a **correctness** issue, not a preference.

| cap | lookback | cold-start behaviour |
|---|---|---|
| p95 TP cap (NB91) | 7 days | returns `inf` -> **uncapped TP** |
| swing TP cap (NB72) | 24h | degenerate on day 1 |
| VP profile | 7d rolling | cold shelves |

Measured on 2025-01 (canonical recipe): chunked-no-warmup net −$8.02,
chunked-with-warmup −$50.11, continuous −$43.00. Same leg count, same
calendar window, **$35 apart in net** — and the sign of the gross flips.

**So: judge every sweep on the LEDGER, not on the month's net.** A month's net
is one draw.

## Operational rules

- **Never run two 1y processes concurrently.** Memory contention causes a **9x**
  slowdown on identical code, and can OOM the process silently.
- **Do not re-measure the same cell twice.** Every cell writes its own JSON —
  check `runtime/sweeps/_results/` first.
- **One 1y process at a time.** "Parallelise the sweep" is a pessimisation at
  this scale.
- Use `python -u` so the engine's progress log is unbuffered.

## Measured cost model (XAUUSD 1s, 1y file)

| scope | bars | artifact build (cold) | engine cell | total, 1 cell |
|---|---|---|---|---|
| 1 month (2025-01) | 1,377,190 | 2.0 s | 1.6 s | ~6 s |
| 1y (2024-01 -> 2026-07) | 41,233,730 | 86.5-97.3 s | 51.0 s (narrow book) | ~2.3-2.7 min |
| 1y, WIDE book (vol gate 1.0) | 41,233,730 | cached | **~300 s** | ~5 min |

**The engine cell cost depends on the BOOK SIZE, not just the bar count.** Any
1y measurement on the current canonical recipe (51,075 legs) costs ~5 minutes
per cell, not ~1.

## Reference

- [`docs/sweeps.md`](../../../docs/sweeps.md) — full policy, the cost estimator,
  the `--i-know` gate implementation.
- [`AGENTS.md`](../../../AGENTS.md) § 1.3 and § 4.
