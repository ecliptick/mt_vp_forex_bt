# Forex (XAUUSD) sweep analysis: NB promotions, redundant gates, sweep priorities

Generated 2026-10-20.

## TL;DR

* The forex research repo's `optimal_config.py` (v22-forex-sniper-2026-09-29)
  is the latest canonical forex recipe. It has been partially updated
  with NB-promoted findings but **many crypto findings are NOT yet
  wired into the forex recipe** — they're in the engine kwargs (NB74
  multi-target, NB83 scale-invariant SL) but not in the params
  dataclass (NB72 swing cap, NB52 strict-wick, NB85 vel gate).
* The SL/TP surface has **at least 8 overlapping knobs** that all
  govern the same decision. The most important NB82 finding
  (zone-anchored SL/TP) wins, but the legacy ATR/USD knobs are
  still on the dataclass and could be reached by a sweep driver.
* The most important sweeps to run on XAUUSD are: (1) NB82 SL/TP
  zone_mults (40/8 BTC vs 3/35 forex canonical), (2) the live
  spread overlay at 0¢ / 21¢ / 30¢, (3) the NB72 swing cap pct
  (0.5 / 0.7 / 1.0), and (4) the NB85 vol-state gate post-fix.

---

## 1. What params have NOT been updated (assuming crypto is latest)

### 1.1 Findings NOT in the forex params dataclass at all

These are formal fields on the crypto `TrendStrategyParams` /
multi-target engine that do NOT exist on the forex one. To apply
them on forex, they go through `engine_kwargs` (kwargs to
`run_vp_sniper_backtest`), not through `build_focused_params`.

| NB | Field | Where it lives on crypto | Status on forex |
| --- | --- | --- | --- |
| NB72 | `swing_cap_pct`, `swing_lookback_secs` | `src/swing_cap.py: DailySwingCap.cap_for(t_ns)` | NOT IN PARAMS. Lives on `engine_kwargs` only via `bt.swing_cap.DailySwingCap`. |
| NB74 | `nth_hvn_n`, `partial_close_pct` | engine kwargs | NOT IN PARAMS. Engine-only. |
| NB83 | `sl_zone_mult`, `sl_buffer_usd`, `sl_atr_mult` | engine kwargs | NOT IN PARAMS. Engine-only. |
| NB85 | `vol_state_min_vel_5m`, `vol_state_min_vel_15m`, etc. | engine kwargs | NOT IN PARAMS. Engine-only. Defaults to 0.0 (gate INACTIVE) until BTC re-validation lands. |
| NB86 | `conflict_policy` (`drop`/`replace`/`stack`) | engine kwargs | NOT IN PARAMS. Defaults to `drop`. |
| NB89 | `max_same_dir_open` | engine kwargs | NOT IN PARAMS. Defaults to 0 (unlimited). |

### 1.2 Findings on the params dataclass but with STALE values

These ARE on `TrendStrategyParams` (the forex engine reads them),
but the canonical forex values do not match the BTC NB promotion.

| Field | Forex canonical (v22) | BTC NB-promoted | Delta |
| --- | --- | --- | --- |
| `fvg_inv_trade_sl_zone_mult` | `3.0` (v22 2026-09-29) | `40.0` (NB82 2026-09-12) | NB82 path is the *new* canonical on BTC. Forex kept v22's 3.0 because the 2026-09-29 v22 sweep found 3.0 was the FX minimum. NB82 needs re-validation on XAUUSD 1s. |
| `fvg_inv_trade_tp_zone_mult` | `35.0` (v22) | `8.0` (NB82) | Same — v22's 35.0 is the FX minimum, NB82's 8.0 is the BTC sweet spot. NB82 has very different R:R math (1:5 vs 1:11). |
| `fvg_inv_trade_tp_boost_ref_usd` | `2.0` (v22) | `1.0` (NB82 BTC) | Both ON, different magnitudes. |
| `ms_pivot_len` | `9` (forex canonical) | `75` (NB54 on BTC) | XAUUSD 1s sweep found NO clean pivot=75 win. Kept at 9. |
| `max_same_dir_open` (params) | `0` (unlimited) | `20` (NB89 BTC) | NB89 not validated on forex. |
| `vol_state_min_vel_5m` (params) | `0.0` (gate INACTIVE) | `0.001` (NB85 BTC, pre-fix) | Forward-look fix applied 2026-10-09; re-validation pending. |

### 1.3 Findings NOT YET promoted on either side

| Finding | State | Notes |
| --- | --- | --- |
| NB54 `ms_pivot_len=75` on BTC | Promoted on BTC, not on forex | XAUUSD 1s sweep found no clean win. Left as a knob. |
| NB85 vol-state gate | Look-ahead fix applied; re-validation pending | Don't trust the BTC CALMAR=254 figure — it's contaminated. |
| NB89 `max_same_dir_open=20` | Promoted on BTC VP-MT, not on forex | Sweep drivers can re-test via `engine_kwargs(max_same_dir_open=20)`. |

---

## 2. Redundant gates: many ways to control SL and TP

The forex `TrendStrategyParams` (and the multi-target engine
kwargs) has **at least 8 overlapping SL/TP controls**. Each
on its own is a parameter; together they form an over-fit risk
because a sweep driver that varies them in combination has
many degrees of freedom but the engine evaluates only one path.

### 2.1 SL controls (5 knobs that ALL touch the stop placement)

| Knob | Where | What it does | NB status |
| --- | --- | --- | --- |
| `sl_usd` | params | Fixed USD SL distance | Legacy HF; not used by SNIPER (`entry_mode='sniper'` ignores it). |
| `sl_atr_mult` | params | ATR-anchored SL | Legacy. NB82 path overrides with `immediate_sl_mode='zone_mult'` (so this is dead in SNIPER mode). |
| `fvg_sl_per_breadth` | params | Zone-breadth anchored SL | Legacy HF. Different param from `fvg_inv_trade_sl_zone_mult`. |
| `fvg_inv_trade_sl_zone_mult` | params | NB82 zone-anchored SL (SNIPER path) | NB82 canonical for SNIPER. |
| `immediate_sl_zone_mult` | params | Legacy HF zone-anchored SL (immediate mode) | HF-only. |

Plus the engine surface:
* `sl_mode` (`fvg_only` | `fvg_or_poc` | `atr_only` |
  `zone_then_atr`) — NB83 promoted `fvg_only`.
* `sl_zone_mult` (engine) — NB83 scale-invariant SL.
* `sl_buffer_usd` (engine) — NB83 USD floor.

**Redundancy:** `sl_usd`, `sl_atr_mult`, `fvg_sl_per_breadth`,
`immediate_sl_zone_mult` are all DEAD in SNIPER mode
(`entry_mode='sniper'`). They are still on the dataclass for
backward compat with the HF path. A sweep that varies them on
the SNIPER path is a no-op.

**Sweep recommendation:** vary ONLY `fvg_inv_trade_sl_zone_mult`
(params) and `sl_zone_mult` / `sl_mode` (engine). The other 5
are off-path.

### 2.2 TP controls (similar story)

| Knob | Where | What it does | NB status |
| --- | --- | --- | --- |
| `tp_usd` | params | Fixed USD TP | Legacy HF; SNIPER ignores. |
| `tp_atr_mult` | params | ATR-anchored TP | Legacy. Dead in SNIPER mode. |
| `fvg_tp_per_breadth` | params | Zone-breadth anchored TP | Legacy HF. |
| `fvg_inv_trade_tp_zone_mult` | params | NB82 zone-anchored TP (SNIPER path) | NB82 canonical. |
| `fvg_inv_trade_tp_boost_ref_usd` | params | NB82 thin-zone boost (≥1.0) | NB82 ON. |
| `immediate_tp_zone_mult` | params | Legacy HF TP | HF-only. |
| `tp_zone_mult_fallback` | engine | TP zone-mult when no HVN shelf | NB82 fallback path. |
| `tp_max_distance_usd` | engine | Hard cap on TP distance | NB72 daily-swing cap is a separate knob. |

**Redundancy:** same pattern. `tp_usd`, `tp_atr_mult`,
`fvg_tp_per_breadth`, `immediate_tp_zone_mult` are dead in
SNIPER mode.

**Two different "caps" on TP distance:**
* `tp_max_distance_usd` (engine) — absolute USD cap (canonical
  `6.0` on XAUUSD 1s).
* `swing_cap_pct` (NB72, engine surface via `DailySwingCap`) —
  fractional cap on the rolling daily swing
  (`swing_cap_pct=0.7` of the 24h range).

These are NOT redundant — they apply at different points in
the trade lifecycle. `tp_max_distance_usd` is a per-trade
hard cap (set at trade open). `swing_cap_pct` is a per-bar
re-evaluation as the daily range grows. The two compete at
trade entry; the swing cap re-fires mid-trade.

### 2.3 Multi-target TP controls

* `nth_hvn_n` (engine) — which HVN shelf is TP1.
* `partial_close_pct` (engine) — fraction of position closed at TP1.
* `tp_zone_mult_fallback` (engine) — TP2 zone-mult.

These are ONLY on the multi-target engine path (NB74/NB75).
The single-target SNIPER path doesn't have them.

### 2.4 Entry mode controls

| Knob | What |
| --- | --- |
| `entry_mode` | `'immediate'` vs `'sniper'` vs `'dual'` |
| `signal_source` | `'fvg'` / `'ifvg'` / `'orb'` / `'wyckoff'` |
| `additional_sources` | extra sources |
| `gate_on_gmma_bias` | legacy no-op |
| `use_ict_signals` | always True in this fork |

**Redundancy:** `signal_source` + `additional_sources` overlap.
If `signal_source='fvg'` and `additional_sources=['fvg']`, that's
redundant. The fork has `use_ict_signals=True` baked.

`gate_on_gmma_bias` is a documented no-op (NB40 finding: the bias
classifier is reactive and gates lose to conviction-weighted TP).

---

## 3. Most important sweeps to run on XAUUSD

The sweep priorities below are ordered by expected information value
per minute of compute. Each sweep is one cell over the canonical
v22 SNIPER recipe on XAUUSD 1s data, varying ONE knob at a time.

### 3.1 SL/TP zone_mult (NB82 vs v22) — HIGHEST PRIORITY

The biggest divergence between BTC NB-promoted (NB82: 40/8) and
forex canonical (v22: 3/35). The R:R math is very different:

* v22 forex: SL=3× zone, TP=35× zone → R:R ≈ 1:11.7. Very wide
  TP. Trades ride structural moves.
* NB82 BTC: SL=40× zone, TP=8× zone → R:R ≈ 1:0.2. Very tight
  TP. Trades take small bites with tight stops.

For XAUUSD with its lower-vol profile (vs BTC), the 1:0.2 NB82
math might not survive — but it might also be a much cleaner
edge. Sweep both directions:

```python
# Forex canonical (3/35)
build_focused_params(fvg_inv_trade_sl_zone_mult=3, fvg_inv_trade_tp_zone_mult=35)
# NB82 promotion (40/8)
build_focused_params(fvg_inv_trade_sl_zone_mult=40, fvg_inv_trade_tp_zone_mult=8)
# Mid-grid (10/15)
build_focused_params(fvg_inv_trade_sl_zone_mult=10, fvg_inv_trade_tp_zone_mult=15)
```

### 3.2 Live spread overlay — HIGH PRIORITY

The user constraint is the 40¢ live overlay. We already wired
it via `LiveSpreadParams(live_spread_per_oz_usd=0.21)`. Sweep
the overlay amount:

```python
# No overlay (backtest-only baseline, overstated net)
LiveSpreadParams(live_spread_per_oz_usd=0.0, live_commission_per_lot_usd=0.0)
# 40¢ canonical (19¢ + 21¢ overlay)
LiveSpreadParams(live_spread_per_oz_usd=0.21, live_commission_per_lot_usd=0.0)
# 50¢ broker (worst-case)
LiveSpreadParams(live_spread_per_oz_usd=0.31, live_commission_per_lot_usd=3.0)
```

At ~$0.21/trade cost delta, the strategy is on the breakeven
edge if it's marginal. This sweep is essentially free.

### 3.3 NB72 swing cap pct — MEDIUM PRIORITY

`DailySwingCap(swing_cap_pct=0.7)` is the BTC-promoted value. On
XAUUSD 1s, the daily range is typically $20-$40. A 0.7 cap means
TP can be at most ~$14-$28 from entry. Sweep:

```python
# No cap (TP can extend to any reasonable zone-mult)
swing_cap_pct=0.0   # effectively no cap; engine uses raw zone_mult TP
# Aggressive cap (BTC BTC-promoted)
swing_cap_pct=0.7
# Loose cap (allow most of daily swing)
swing_cap_pct=1.0
```

### 3.4 NB85 vol-state gate (post-fix) — MEDIUM PRIORITY

The original NB85 BTC finding (CALMAR 254 with `vel_5m>=0.001`)
was contaminated by forward-look. The fix is in
`ict_crypto_research/src/vol_state.py` (read with
`bt.vol_state`). Re-validation on BTC is pending. We can sweep
on XAUUSD:

```python
# INACTIVE (canonical until BTC re-validation lands)
engine_kwargs(vol_state_min_vel_5m=0.0)
# NB85 floor (BTC original, after the fix)
engine_kwargs(vol_state_min_vel_5m=0.001)
# Tight floor (BTC-promoted was very low — try higher)
engine_kwargs(vol_state_min_vel_5m=0.005)
```

NB: do not trust results from this sweep until BTC
re-validation confirms the gate is real.

### 3.5 NB89 max_same_dir_open — MEDIUM PRIORITY

`max_same_dir_open` caps the open book. The BTC value was 20;
the forex default is 0 (unlimited). Sweep:

```python
engine_kwargs(max_same_dir_open=0)    # unlimited (legacy)
engine_kwargs(max_same_dir_open=5)    # tight cap
engine_kwargs(max_same_dir_open=20)     # BTC-promoted
```

NB: this requires the multi-target engine path. The single-target
SNIPER path doesn't honor it.

### 3.6 ms_pivot_len sweep — LOW PRIORITY

`ms_pivot_len=9` is the forex canonical. NB54 found 75 was the
BTC +EV regime. The XAUUSD 1s sweep found no clean win for 75,
but the sweep was narrow. Re-test with broader grid:

```python
build_focused_params(ms_pivot_len=9)     # canonical
build_focused_params(ms_pivot_len=25)    # mid
build_focused_params(ms_pivot_len=75)    # NB54 BTC
```

### 3.7 Things NOT to sweep

* `lots`, `contract_size` — these are sizing. Changing them is
  a different strategy, not a parameter sweep. The 100× leverage
  at 0.01 lots is the user's specification.
* `commission_per_lot_usd`, `spread_per_oz_usd` — the BASELINE
  fee. The live overlay is the sweep knob, not the baseline.
* `sl_tp_tiebreak` — `'sl_first'` is the SNIPER canonical;
  `'tp_first'` is the HF legacy. Sweeping both is an A/B
  between strategies.
* `entry_mode` — switching between `'sniper'` / `'immediate'` /
  `'dual'` is a different strategy. Don't sweep within one.
* `signal_source` — same logic; the recipe is for FVG/iFVG
  primarily.
* The 5 dead SL knobs (`sl_usd`, `sl_atr_mult`,
  `fvg_sl_per_breadth`, `immediate_sl_zone_mult`,
  `immediate_sl_mode`) and 4 dead TP knobs in SNIPER mode —
  they don't fire. Sweeping them is wasted compute.

---

## 4. Summary table

| Sweep | Priority | Cost | Risk of misleading result |
| --- | --- | --- | --- |
| NB82 vs v22 SL/TP zone_mult | HIGH | Low | Medium (R:R math is very different) |
| Live spread overlay 0¢/21¢/30¢ | HIGH | Very low | Low |
| NB72 swing cap pct (0/0.7/1.0) | MEDIUM | Low | Low |
| NB85 vol-state gate (post-fix) | MEDIUM | Low | HIGH — don't trust until BTC re-validation |
| NB89 max_same_dir_open | MEDIUM | Medium | Medium (engine-only) |
| ms_pivot_len (9/25/75) | LOW | Low | Low |
| Live spread + commission combo | MEDIUM | Low | Low |

Total compute: ~7 sweeps × ~5 grid cells each ≈ 35-60 backtest
runs. On 1s XAUUSD data over a 6-month window, each run is
~3-5 minutes; total ~2-5 hours of compute.