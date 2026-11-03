# Sweep analysis: which knobs are live, redundant, or already settled

> **Status: CURRENT (rewritten 2026-11-03).**
>
> The previous version of this file analysed a **recipe dataclass
> (`TrendStrategyParams`) that no longer exists** and compared XAUUSD
> against a second market's numbers. Both are gone: the recipe was
> deleted on 2026-10-24 (config consolidation) and the repo is
> XAUUSD-only.
>
> What survives is the **question the file was written to answer**:
> *which knobs are actually live, and which sweeps are worth running?*
> This version answers that against the **current** surface.

---

## TL;DR

* The sweep surface is **the seven builders** in
  `bt/optimal_config.py` — 87 allowlisted knobs in total. Anything
  not on an allowlist raises `KeyError` and cannot become a silent
  no-op.
* **The recipe is now one market.** Every "NB82 vs v22" or "BTC vs
  forex" comparison in the old file is moot: there is only XAUUSD, and
  the canonical values were chosen on XAUUSD.
* **The measured dead knobs are already documented** in
  `docs/promotions.md` § "Things that were measured and found DEAD".
  Do not re-sweep them.
* **The live surface has shrunk.** The old file's "5 overlapping SL
  controls" and "4 overlapping TP controls" were properties of the
  deleted recipe dataclass. The engine now has **one** SL path
  (`sl_mode`) and **one** TP path (`tp_mode`), each with a small,
  enumerable set of sub-knobs.

---

## 1. The live surface, by builder

Every knob below is on an allowlist and reaches its target. This is
the complete list.

### 1.1 `build_engine_kwargs` — 54 knobs

The engine surface. Grouped by what it actually controls:

| Group | Knobs |
|---|---|
| **Stop geometry** | `sl_mode`, `sl_zone_mult`, `sl_buffer_usd`, `sl_min_usd`, `sl_max_usd`, `sl_nth_hvn_n`, `sl_hvn_max_distance_usd`, `sl_scale_min`, `sl_scale_max`, `hvn_buffer_usd`, `poc_buffer_usd` |
| **Target geometry** | `tp_mode`, `tp2_mode`, `nth_hvn_n`, `tp2_nth_hvn_n`, `partial_close_pct`, `tp_zone_mult_fallback`, `tp_max_distance_usd`, `tp_hvn_proxy_frac`, `tp_scale_min`, `tp_scale_max` |
| **TP caps** | `tp_p95_mult`, `p95_cap`, `swing_cap`, `tp2_swing_cap` |
| **Entry** | `entry_mode`, `entry_fill_model`, `fill_model` |
| **Book / conflict** | `conflict_policy`, `max_same_dir_open`, `trade_sides` |
| **Trailing stop** | `trail_mode`, `trail_atr_mult`, `trail_activation_mult`, `trail_giveback_frac`, `trail_sides` |
| **Hedge netting** | `contra_close`, `contra_close_spread_usd` |
| **Vol gate** | `vol_state`, `vol_state_min_range_norm`, `vol_state_min_vel_5m`, `vol_state_min_vel_15m`, `vol_state_min_rv_ratio_30s`, `vol_state_max_rv_ratio_30s`, `vol_state_max_wick_ratio`, `vol_state_min_expand_prev_day`, `vol_state_max_expand_prev_day` |
| **Structure** | `structure`, `structure_gate`, `structure_seq_whitelist`, `structure_seq_flip_map`, `structure_seq_len`, `structure_max_age_bars`, `structure_score_lookback`, `structure_score_half_life`, `structure_score_min_abs` |
| **LVN** | `lvn_mode`, `lvn_skip_usd` |
| **Fees** | `taker_fee_bps` |

### 1.2 `build_vp_kwargs` — 9 knobs

`vp_rows`, `vp_min_zone_usd`, `vp_rolling_secs`, `vp_session_secs`,
`vp_va_pct`, `vp_hvn_zone_pct`, `vp_hvn_peak_pct`,
`vp_hvn_min_prominence_pct`, `vp_lvn_threshold_pct`.

> ⚠️ **The VP profile is measurably INERT in the canonical recipe.**
> The placebo test in `docs/promotions.md` § "HVN/LVN" shows the
> profile carries no price↔volume information. **Do not promote a VP
> knob on the strength of a single-cell win** — the winning cell would
> be fitting the placebo band.

### 1.3 `build_detector_kwargs` — 14 knobs

`fvg_min_zone_usd`, `resample_to_n_secs`, `body_only_invalidation`,
`body_only_mitigation`, `fvg_body_definition`,
`invalidation_min_consecutive_bars`, `invalidation_min_pierce_usd`,
`fvg_displacement_ratio`, `fvg_min_lifetime_secs`,
`max_zone_age_bars`, `played_out_min_extension_usd`,
`require_retest_to_invert`, `strict_wick_required`,
`supersede_on_new`.

### 1.4 `build_swing_kwargs` — 2 knobs

`swing_cap_pct` (canonical 0.25), `swing_lookback_secs` (86,400).

### 1.5 `build_p95_kwargs` — 6 knobs

`tp_p95_lookback_secs`, `tp_p95_inner_secs`, `tp_p95_q`,
`tp_p95_statistic`, `seed_constant_usd`, `seed_path`.

> ⚠️ **The p95 cap is shadowed by the swing cap** on 100% of trade
> probes, so it currently measures nothing on the canonical recipe.

### 1.6 `build_live_p95_kwargs` — 7 knobs

Same vocabulary as above, but with the baked-in live seed constant.
**Never use this builder in a backtest** — it would move every
measured number.

### 1.7 `build_structure_kwargs` — 2 knobs

`structure_pivot_len` (canonical 9), `structure_resample_secs`.

---

## 2. Redundancy: what still overlaps

The old file's § 2 ("many ways to control SL and TP") was about the
deleted recipe dataclass. The engine surface is much tighter, but two
overlaps remain:

### 2.1 Two TP distance caps, applied at different times

* `tp_max_distance_usd` — a **per-trade** cap, set at trade open.
  Canonical value is **0.0 = DERIVED** (never a fixed ceiling).
* `swing_cap_pct` — a **per-bar** re-evaluation as the rolling daily
  swing grows. Fires mid-trade.

They compete at entry; the swing cap can re-fire after entry. They are
not redundant, but a sweep that moves one and attributes the result
to the other is wrong.

### 2.2 The SL anchor vs the SL bounds

`sl_mode` selects the **anchor** (`hvn_lvn` is canonical).
`sl_min_usd` / `sl_max_usd` **clamp** whatever the anchor produced.
`sl_nth_hvn_n` selects *which* shelf behind entry.

> ⚠️ **The SL ceiling and the vol gate are PAIRED.** `sl_max_usd=8.00`
> and `vol_state_min_range_norm=1.0` were changed together on
> 2026-10-25. **Re-measure them together, never one alone.**

### 2.3 `sl_zone_mult` is a FALLBACK only

Canonical `sl_mode='hvn_lvn'` resolves the stop from the HVN shelf.
`sl_zone_mult` (canonical 40.0) applies **only** when the shelf
cannot be resolved. Sweeping it while the anchor resolves cleanly
measures almost nothing.

### 2.4 The two vol gates overlap more than they look

`vol_state_min_range_norm` and `vol_state_min_expand_prev_day` both
ask "is the market moving", but at **different horizons**:

| | `range_norm_1m` | `vol_expand_prev_day` |
|---|---|---|
| baseline | trailing **60 minutes** | the **previous calendar day** |
| question | is *this minute* an outlier? | is *today* hotter than *yesterday*? |
| memory | ~1 hour | ~1 day |

They are **not redundant by construction** — a 60-minute rolling
median genuinely cannot distinguish "the whole day is slow" from
"the whole day is fast". The two-knob grid
(`vol_state_min_range_norm=0.5,1.0,1.5` ×
`vol_state_min_expand_prev_day=0.0,0.9,1.1`) was run on 2025-01
(AGENTS.md § 4.1 tier 3) and showed them **close to complementary**:
the prev-day gate adds nothing at `range_norm=0.5`, but it is the
only cell that turns the month's net positive at `range_norm=1.5`.

> ⚠️ **Both gates cut the trade count.** The vol gate and the
> 30–70 trades/day target are in **direct tension** — that is
> precisely why `vol_state_min_range_norm` was re-paired from 3.0
> to 1.0 on 2026-10-25. Adding a second gate makes it worse. Any
> promotion of the expansion gate must be judged against the
> trade-count ladder, not just the gross.

### 2.5 `tp_mode='hvn_single'` retires three knobs

Under the canonical `hvn_single` mode these are **INACTIVE**:

| knob | why it is inert |
|---|---|
| `partial_close_pct` | the mode returns `0.0` — there is no partial |
| `tp2_mode` | there is no runner leg |
| `tp2_nth_hvn_n` | ditto |

They remain on the sweep surface for the `multi_target` A/B, but a
sweep of them under the canonical mode measures **nothing**. This is
the § 1.5 rule (a knob on the allowlist can still be dead) showing
up in a new place: the knob is alive in the engine and dead in the
**canonical configuration**.

---

## 3. Sweep priorities

Ordered by expected information value per unit of compute. Cost is
per-cell on the **one-month** window; see `docs/sweeps.md` for the
full cost model.

| Priority | Sweep | Builder | Cost | Why |
|---|---|---|---|---|
| **HIGH** | `sl_max_usd` × `vol_state_min_range_norm` | engine | med | The paired knobs. Both are live and both change the book's size. **Re-measure after the `hvn_single` change** — the payoff geometry moved. |
| **HIGH** | `sl_mode` / `sl_nth_hvn_n` / `hvn_buffer_usd` | engine | med | **Newly live** since the SL ceiling was raised to $8.00 — the HVN anchor has 8,445 distinct stop distances instead of 1. |
| **HIGH** | `contra_close_spread_usd` | engine | med | Fires on 34% of the wide book. A $5.00 threshold was set **by instruction**, not re-picked. Worth a ladder. |
| MED | `tp_mode` | engine | med | Three modes now: `multi_target`, `hvn_single`, `fvg_mult`. `hvn_single` is canonical (2026-11-03) but **not yet ledger-judged**. |
| MED | `trail_sides` | engine | med | **NEW 2026-11-04.** Scopes the trailing stop to one side: `'both'` \| `'long'` \| `'short'`. The corpus MFE/MFA analysis measured the trail's effect PER SIDE and found it **helps the losing side and hurts the winning side** (longs −$906.77, shorts +$533.38 on the replay). `'short'` is the candidate. **NOT the entry gate** — `trade_sides` decides which trades may open, `trail_sides` only changes their exit. |
| MED | `sl_min_usd` | engine | low | The floor is live now (it can only loosen a too-tight stop). |
| MED | `nth_hvn_n` | engine | med | **Re-measure under `hvn_single`** — the value `3` was tuned for the old multi-target geometry and is a different quantity now. |
| MED | `tp2_mode` / `tp2_nth_hvn_n` | engine | med | The runner leg. **INACTIVE under `hvn_single`** — only matters for the `multi_target` A/B. |
| MED | `partial_close_pct` | engine | med | Canonical 0.50. **INACTIVE under `hvn_single`** — the mode returns `0.0`. |
| MED | `vol_state_min_vel_5m` / `_vel_15m` / `_min_rv_ratio_30s` / `_max_rv_ratio_30s` / `_max_wick_ratio` | engine | low | Dormant measures that have **never been swept**. Now sweepable without `KeyError` (the driver builds every measure). |
| DEAD | `structure_seq_drop_set` | engine | — | **REMOVED 2026-11-04.** The knob no longer exists. It was redundant with, and contradicted, the 5-event whitelist — it vetoed 3 of the 8 whitelisted short shapes including the best one. See AGENTS.md § 3.10.1. |
| MED | `structure_seq_whitelist` | engine | med | **PROMOTED 2026-11-04.** **21 keys** (13 long / 8 short), re-fitted on the current 37,159-leg book. A **QUALITY** knob, not an EDGE knob — the kept book's gross is positive (+$2,583) but its per-leg edge (+$0.3877) is still below the measured round trip ($0.4989). The untested extension: the **engine-level** walk-forward (fit the set on H1, rebuild the H2 ledger with it). |
| MED | `structure_pivot_len` | structure | low | Canonical 9. Only value with any measurement behind it. |
| LOW | `resample_to_n_secs` (detector) | detector | **high** | Rebuilds the artifact cache — the most expensive knob class. |
| LOW | `fvg_min_zone_usd` (detector) | detector | **high** | Same — detector knobs invalidate the artifact cache. |
| LOW | VP knobs | vp | **high** | The profile is measurably inert. Do not promote on one cell. |
| LOW | `tp_p95_*` | p95 | med | Shadowed by the swing cap on 100% of probes. |
| — | `swing_cap_pct` | swing | low | Canonical 0.25. Live, but heavily swept. |

> **⚠️ Detector and VP knobs rebuild the artifact cache** (~87–97 s on
> the 1y corpus). Engine-only knobs reuse it. This is a ~15× cost
> difference per cell and it is why the detector/VP rows are ranked
> LOW despite being live.

---

## 4. Things NOT to sweep

* **`lots` / `contract_size`** — these are sizing. Changing them is a
  different strategy, not a parameter sweep. The 100× leverage at
  0.01 lots is the specification.
* **`commission_per_lot_usd` / `spread_per_oz_usd`** — these are
  *stripped* before the engine call. Their effect is folded into
  `taker_fee_bps` at build time. Passing them to the engine is
  forbidden by `bt/vp_engine._FOREX_ONLY_KWARGS`.
* **`sl_atr_mult`** — removed from the sweep surface 2026-10-22.
  Doubly dead: canonical `0.0`, and no `atr=` array is ever passed.
  A 1s-cadence ATR is $0.098 median, so a $5 stop needs
  `mult ≈ 51` — a constant with no cross-regime meaning.
* **`structure_gate`** — measured, **NOT promoted**. It loses **−$1,215
  of net** against the canonical book, and its per-leg `excess` stays
  significantly negative on both sides.
* **`trade_sides = "long"`** — measured, **NOT promoted**. A long-only
  book has no hedge in a sell-off, and its daily market correlation is
  higher than the two-sided recipe's. ⚠️ Its net is **higher** — the
  rejection is a deliberate risk choice, not a measured-worse one.
* **The sequence whitelist** and **the sequence flip** — measured and
  **DEAD**. See `docs/promotions.md`.
* **`entry_mode`** — switching between `'sniper'` / `'immediate'` /
  `'dual'` is a different strategy. Don't sweep within one.
* **The dead SL/TP knobs of the deleted recipe** (`sl_usd`,
  `tp_usd`, `fvg_sl_per_breadth`, `immediate_sl_zone_mult`,
  `immediate_tp_zone_mult`, `fvg_inv_trade_tp_zone_mult`,
  `fvg_inv_trade_tp_boost_ref_usd`, `use_ict_signals`) — these lived
  on `TrendStrategyParams`, which was **deleted** on 2026-10-24.
  They do not exist on any surface.

---

## 5. How to run a sweep

```bash
# the default is ONE month (~6 s)
python -m runtime.sweeps.cached_sweep --knob _canonical

# a single knob
python -m runtime.sweeps.cached_sweep \
  --knob sl_max_usd --grid 4,6,8,10

# a MULTI-knob grid (cartesian product) — for the PAIRED knobs
python -m runtime.sweeps.cached_sweep \
  --knobs "sl_max_usd=4,6,8;vol_state_min_range_norm=1.0,1.5"
```

**The one-month sweep is NOT the canonical measurement.** The p95 cap
(7d), swing cap (24h) and VP profile (7d rolling) are
history-dependent, so a cold chunk trades its first ~8 days with
different targets. See `docs/evaluation.md` § "Chunked vs continuous".

**Every sweep is judged on the ledger**, not on the month's net:

```bash
python -m runtime.diagnostics.leg_ledger
python -m runtime.diagnostics.leg_buckets
```

---

## 6. Historical note

The original version of this file (2026-10-20) was written when the
repo held a `TrendStrategyParams` recipe dataclass and compared it
against a **second market's** promoted values. Both of those are
gone. The file has been rewritten around the question that survives:
*what is live, and what is worth sweeping?*
