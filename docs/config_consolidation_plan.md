# Config consolidation plan — 2026-10-24 (RESOLVED)

## Goal
ONE canonical param set. No duplicate sources of truth. No dead recipe
layer. `bt/optimal_config.py` is the canonical spec **and** impl.

**Status: DONE.** Every decision below is landed and guarded by
`tests/test_config_single_source.py` (30 AST guards) plus the two
runnable audits.

---

## What was found (measured, not assumed)

### F1 — the recipe dataclass was 100% dead on the production path
`run_vp_sniper_backtest` has NO `params` argument.
`bt/vp_engine.run_vp_sniper_backtest_forex` accepted `params=None` and
**never forwarded it** — the body never referenced it.
=> `build_focused_params()` -> `TrendStrategyParams` -> dropped on the floor.

Every key in `_OPTIMAL_OVERRIDES_ALLOWED` was a **silent no-op** on the VP
path. A sweep of `fvg_inv_trade_sl_zone_mult` reported a "canonical"
cell measuring nothing.

### F2 — the recipe's only live reader was 8 detector fields
`runtime/sweeps/cached_sweep.py::detector_kwargs_from_recipe` read 8
fields off the recipe and mapped them onto `detect_fvg`. They now live
as module constants and are materialised by `build_detector_kwargs()`.

### F3 — the HF_TIGHT recipe was 100% dead
`_build_hf_tight()` (~130 lines) was referenced only from **docstrings**
in `bt/_engine/precomputed.py`. Zero code callers.

### F4 — the bar engine was 100% dead
`bt/_engine/ict_backtest.py` (`run_ict_backtest`) — zero production
callers. `bt/_engine/precomputed.py` — zero callers. AGENTS.md itself:
"The VP multi-target engine has no precompute cache."

### F5 — duplicated sources of truth in the wrapper modules

| wrapper constant | canonical | status |
|---|---|---|
| `bt/swing_cap.DEFAULT_SWING_CAP_PCT = 0.7` | `SWING_CAP_PCT = 0.25` | **CONTRADICTORY** (2.8x apart) |
| `bt/swing_cap.DEFAULT_SWING_LOOKBACK_SECS = 86400` | `SWING_LOOKBACK_SECS = 86400` | dup |
| `bt/rolling_p95.DEFAULT_*` (6 copies) | `TP_P95_*` | dup |
| `bt/vol_state.DEFAULT_VOL_STATE_MIN_VEL_{5,15}M` | `VOL_STATE_MIN_VEL_*` | dup |
| `bt/volume_profile.DEFAULT_VP_*` (7 copies, **different names**) | `VP_*` | dup + renamed |
| `bt/vp_engine.DEFAULT_TP_MODE` / `_LEGACY` | `TP_MODE` | dup |
| `bt/risk.DEFAULT_LOTS/CONTRACT_SIZE/LEVERAGE` | `FOREX_*` | dup |
| `bt/leverage.DEFAULT_LEVERAGE` | `FOREX_LEVERAGE` | dup |
| `bt/_engine/vp_multi_target_config.LVN_SKIP_USD = 10.0` | `LVN_SKIP_USD = 0.30` | **CONTRADICTORY** (33x apart) |

Plus a **second implementation** of the NB72 cap geometry:
`bt/swing_cap.cap_distance` / `apply_cap_to_price` duplicated the
engine's own `_apply_swing_cap` (and had zero / test-only callers).

Plus `bt/volume_profile.make_rolling_vp()` — a broken function that
passed non-existent arguments to `build_volume_profiles` and could only
ever raise `TypeError`.

### F6 — the builders appended instead of replacing (SILENT no-op)

Four builders use **canonical knob names** that differ from their
target's parameter names. They did `base.update(overrides)`, which
APPENDED the canonical key rather than replacing the target-named one:

```
build_swing_kwargs(swing_cap_pct=0.5)
-> {'cap_pct': 0.25, 'lookback_secs': 86400, 'swing_cap_pct': 0.5}
```

Splatted into `build_daily_swing` that is a `TypeError`; worse, the
sweep driver kept **its own copy** of the mapping and overrode the
builder's. **The override was measured as nothing.**

### F7 — the p95 multiplier existed in two places, cancelling out

`_p95_cap_usd` computes `cap_for(t) * (engine_mult / obj_mult)` and
`cap_for` already applied the object's own `p95_mult`. The two
therefore cancel, so the cap object's `p95_mult` is a **dead knob**.
The single live multiplier is the engine kwarg `tp_p95_mult`.

---

## What was done

### Deleted

| Deleted | Why |
|---|---|
| `bt/_engine/optimal_config.py` | The `TrendStrategyParams` recipe: TWO recipes (`SNIPER` + `HF_TIGHT`) + an `optimal_params()` override factory. `HF_TIGHT` had **zero** callers. |
| `bt/_engine/ict_backtest.py` | The **bar engine** (`run_ict_backtest`). Zero production callers. |
| `bt/_engine/precomputed.py` | The bar engine's precompute cache. Zero callers. |
| `TrendStrategyParams` (in `bt/_engine/ict_strategy.py`) | The ~1000-field recipe dataclass. Only the dead bar engine read it. `PendingSignal` survives — `ict_signals.py` uses it. |
| `run_vp_sniper_backtest_forex(params=...)` | A silent-drop trap: accepted and **never forwarded**. |
| `bt/swing_cap.cap_distance` / `apply_cap_to_price` | A second implementation of the NB72 cap geometry. |
| `bt/volume_profile.make_rolling_vp()` | Broken — passed non-existent kwargs. Could only raise `TypeError`. |
| Every `DEFAULT_*` duplicate listed in F5 | Second (and in two cases contradictory) sources of truth. |

### The seven materialisers

| Builder | Target | Allowlist | Vocabulary |
| --- | --- | --- | --- |
| `build_engine_kwargs` | `run_vp_sniper_backtest` | `_ENGINE_OVERRIDES_ALLOWED` | target's own names |
| `build_vp_kwargs` | `build_volume_profiles` | `_VP_OVERRIDES_ALLOWED` | target's own names |
| `build_detector_kwargs` | `detect_fvg` | `_DETECTOR_OVERRIDES_ALLOWED` | target's own names |
| `build_swing_kwargs` | `build_daily_swing` | `_SWING_OVERRIDES_ALLOWED` | canonical → translated |
| `build_p95_kwargs` | `build_rolling_p95_from_bars` | `_P95_OVERRIDES_ALLOWED` | canonical → translated |
| `build_live_p95_kwargs` | `make_live_rolling_p95` | `_LIVE_P95_OVERRIDES_ALLOWED` | canonical → translated |
| `build_structure_kwargs` | `detect_market_structure` | `_STRUCTURE_OVERRIDES_ALLOWED` | canonical → translated |

The four translated builders use `_SWING_KNOB_TO_PARAM` /
`_P95_KNOB_TO_PARAM` / `_LIVE_P95_KNOB_TO_PARAM` /
`_STRUCTURE_KNOB_TO_PARAM`. These maps live in `bt/optimal_config.py`
and **nowhere else** — the sweep driver's duplicate copy was deleted.

### The two p95 builders are deliberately separate

`build_p95_kwargs()` is the **backtest** path (`seed_constant_usd=0.0`
— the corpus already has the history the cap needs).
`build_live_p95_kwargs()` is the **live VPS** path (uses the baked-in
`FOREX_P95_RET_USD` as the coldstart fallback).

Splitting them is what guarantees the backtest can never accidentally
pick up the live seed constant and silently move every measured number.

---

## The guards

`tests/test_config_single_source.py` — 30 AST-based guards:

1. No module outside `bt/optimal_config.py` may **define** a
   canonical-named constant (a second source of truth).
2. No canonical constant may be **unread** (a knob that cannot move).
3. Every allowlist key must reach a real parameter of its target.
4. The canonical knob name must **translate** onto the target's
   parameter name (the builders are the single translation point).
5. The sweep driver may not keep its own copy of that mapping.
6. The deleted dead surfaces must stay deleted.

Two runnable audits:

```bash
python -m runtime.diagnostics.audit_config_surface
python -m runtime.diagnostics.audit_constant_reachability
```

The second is the **strict** version: a constant must be passed by
name onto a target surface, not merely mentioned in a docstring. It
is what caught `FOREX_P95_RET_USD` (read only in prose) as a dead knob.

---

## Canonical value table (single source: `bt/optimal_config.py`)

Sizing / fees
```
lots 0.01, contract_size 100.0, leverage 100.0,
commission 7.0/lot, spread 0.40/oz
-> 1.00 oz notional, $0.47 round trip.
```

Entry / fill
```
entry_mode dual, entry_fill_model touch, fill_model pessimistic
```

SL
```
sl_mode hvn_lvn, sl_zone_mult 40.0, sl_buffer_usd 0.05,
sl_nth_hvn_n 1, sl_min_usd 2.50, sl_max_usd 2.50,
sl_hvn_max_distance_usd 0.0, poc_buffer_usd 0.20, hvn_buffer_usd 0.10
```

TP
```
tp_mode multi_target, nth_hvn_n 3, partial_close_pct 0.50,
tp_zone_mult_fallback 200.0, tp_max_distance_usd 0.0,
tp_hvn_proxy_frac 0.7,
tp2_mode hvn, tp2_nth_hvn_n 0, tp2_swing_cap True
```

Gates
```
vol_state_min_range_norm 3.0, vol_state_min_vel_5m 0.0,
vol_state_min_vel_15m 0.0, vol_state_min_rv_ratio_30s 0.0,
vol_state_max_rv_ratio_30s 0.0, vol_state_max_wick_ratio 0.0,
structure_gate False, structure_max_age_bars 300,
trade_sides both, max_same_dir_open 20, conflict_policy stack,
structure_seq_drop_set {"short|c","short|C"}
```

Caps
```
swing_cap_pct 0.25, swing_lookback_secs 86400
tp_p95_mult 1.0, tp_p95_lookback 604800, tp_p95_inner 3600,
tp_p95_q 95.0, tp_p95_statistic p2t_window, forex_p95_ret_usd 8.0
```

VP profile
```
vp_session_secs 86400, vp_rolling_secs 604800, vp_rows 200,
vp_va_pct 70.0, vp_hvn_peak_pct 30.0, vp_hvn_min_prominence_pct 15.0,
vp_hvn_zone_pct 50.0, vp_lvn_threshold_pct 25.0, vp_min_zone_usd 0.50
```

Detector
```
fvg_min_zone_usd 0.30, max_zone_age_bars 3600,
fvg_displacement_ratio 0.0, fvg_body_definition "body",
body_only_mitigation True, body_only_invalidation True,
played_out_min_extension_usd 0.0, invalidation_min_pierce_usd 0.05,
invalidation_min_consecutive_bars 2, require_retest_to_invert True,
supersede_on_new True, fvg_min_lifetime_secs 3,
resample_secs 60, strict_wick_required False,
strict_wick_min_wick_price_pct 0.00025, strict_wick_recompute_secs 3600
```

Structure build
```
structure_resample_secs 60, structure_pivot_len 9
```

---

## Verification

- `pytest -q` — **217 passed**.
- `audit_config_surface` — 0 duplicate constants, 0 dead imports,
  0 unread canonical constants.
- `audit_constant_reachability` — **0 constants reach no surface**
  (89 constants, 0 dead).
- `directional_beta --conflict-policy stack` must still reproduce the
  AGENTS.md numbers (total net −73.60, gross +246.22, fees 319.83).
  This is the value-preservation check: the cleanup must not move a
  single P&L number.
