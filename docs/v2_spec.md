# v2 rebuild — logic spec (pre-code review)

Replaces ~25 knobs and 2 modules with one decision tree.
Source of truth for the current behaviour: `bt/_engine/vp_multi_target_engine.py`.

## 0. Why

The current engine's P&L is explained by market direction, not by FVG
logic (`docs/nb95_beta_proxy.md`). Long gross corr **+0.966**, short gross
corr **−0.961**. The mechanism is a levered long/short pair that is long by
construction. v2 must have a **direction-independent** design or it will
reproduce the same result with less code.

## 1. Zone lifecycle

Three terminal-ish states. Each zone can produce **at most two** entries.

```
        detect (width >= min_zones_atr_mult * ATR)
             |
             v
          [ LIVE ]
             |
   +---------+-------------------------------------------+
   |                                                     |
   | (depth mitigation >= D)                        (invalidated)
   v                                                     v
 ENTRY A : immediate  @ zone edge (limit)         ENTRY B : sniper @ market
   |                                                     |
   |  zone STAYS live                                    |
   +--------------------> zone DEAD <---------------------+
```

Rules:

* **R1** `FRESH` → on the first bar where `mitigated_depth_pct >= D`,
  fire Entry A. The zone is **not** consumed.
* **R2** `FRESH` → on the K-th consecutive close beyond the far edge,
  set `INVALIDATED`, fire Entry B, zone **DEAD**.
* **R3** A zone may fire A then B (2 entries). It may never fire B then A.
* **R4** Silent deaths (no trade, zone just gone): superseded by an
  overlapping zone, played-out by extension past the edge, aged out.
* **R5** Expiry does **not** cancel an already-invalidated zone.
* **R6** Every field is read **relative to the bar being processed**, never
  "did this ever happen in the file". The current
  `int(zone.inverted_bar) >= 0` in the dual-mode skip violates this.

> **R3 cost note.** 2 entries/zone means 2 round trips ($0.94) per zone.
> At the 2025-01 median zone ($0.37) this is a large fraction of any
> plausible target. Sweep A-only vs A+B as the first experiment.

## 1.2 Detector gates (from v1, verified)

| Gate | v1 canonical | v2 |
|---|---|---|
| `body_only_mitigation` | `True` | **ON** — keep |
| `body_only_invalidation` | `True` | **ON** — keep |
| `require_retest_to_invert` | **`False`** | **SWEEP** — default `True` |
| `max_zone_age_bars` | `3_600` | **6h, hardcoded, no knob** |
| `supersede_on_new` | `True` | keep, but see §1.1 |
| `played_out_min_extension_usd` | **`0.0` = DEAD** | decide explicitly |
| `fvg_min_zone_usd` | `$0.30` absolute | replace with ATR multiple |

**On `require_retest_to_invert` (sweep knob).** v1 ships `False`, so the
sniper fires on the pierce streak **alone, with no retest confirmation**.
`True` requires price to return to the original side before the flip. The
`True` hypothesis is stronger — a true invalidation — and the small-TP-on-
retest variant is a weaker, earlier claim. Default `True`; sweep both.

**On `played_out_min_extension_usd`.** It is `0.0` in v1 and the block is
guarded by `if ext > 0:`, so **no zone ever plays out**. In v2 either set
a real extension or delete the concept — do not leave a dead branch.

**On body-only.** The flags are already `True` and forwarded, but v1
reported them as inert. Verify by **behaviour**, not by config:
* a wick-through zone must **not** set `mitigated_bar`
* a wick-through zone must **not** set `pierced_bar`

Both are unit tests in v2. Note that body-only invalidation makes zones
*harder* to kill — combined with the min-bar gate the sniper universe may
be small. Measure zone counts in step 1 before assuming otherwise.

**Two lifetimes collapse to one.** v1 has `expired_bar` from the detector
*and* a hardcoded 3,600-bar horizon in the mitigation walk when
`max_zone_age_bars` is unset. v2 has exactly one.

| | Trigger | Order | Price |
|---|---|---|---|
| **A — immediate** | depth mitigation ≥ D | resting limit | far edge of zone (long: `zone_low`) |
| **B — sniper** | invalidation close | **market** | that bar's close |

* **R7** Entry A is a real resting order (fills on a *later* bar whose
  range touches the level). Pre-D2 instant fills are not defensible.
* **R8** Entry B is a market order at the signal close — the trade thesis
  *is* that the level failed, so waiting for a retest defeats it.
* **R9** Entry A is cancelled **only** on invalidation or expiry.
  **Supersede must NOT cancel a resting order.** (See §1.1 — this was a
  bug in v1.)
* **R10** Exit resolution is **two independent decisions**, not one knob:
  * *Ambiguity* (bar spans both SL and TP, both inside the range): fill
    at the **level**. The market did trade there, and each trade carries
    its own SL/TP, so the coin-flip must not be resolved pessimistically.
  * *Gap-through* (level is beyond the bar's open): fill at the bar's
    **open**. The market never traded at the level; crediting it is the
    E1 defect from the v1 audit.
* **R10a** Both mitigation and invalidation require a **minimum bar count**
  (`mitigate_min_bars`, `invalidate_min_bars`) so insta-kill zones cannot
  fire. Scale-free — a bar count, not a price distance.

### 1.1 Supersede must not cancel orders (bug found 2026-10-23)

In v1 `_zone_death_bar` returns `min(superseded, played_out, expired)`
and the fill loop cancels on any death bar, so supersession killed
resting orders.

That is wrong given the min-bar gate. With `min_bars = 3`:

| bars | zone state | order placed? |
|---|---|---|
| 0–2 | live, gate blocks the signal | **no** |
| 3+ | live | yes |

A supersede can only cancel an order if it lands *after* the order was
placed. A tight supersede window is therefore inert, and supersede mostly
destroys zones that were still going to fire.

**v2 rule:** supersede sets `superseded_bar` for diagnostics and marks the
zone non-fresh for *new* signals, but is **excluded** from the order
cancellation path. Only invalidation and expiry cancel.

## 3. Stop

One rule, one fallback. (Was: 6 writers.)

* **R11** SL = the HVN/LVN shelf **behind** entry, if one exists inside the
  search radius and sits strictly beyond the zone's far edge.
* **R12** Otherwise SL = `atr_stop_mult × ATR(n)` below/above entry.
* **R13** Never tighter than `sl_min_atr_mult × ATR`. Loosen-only.
* **R14** No `sl_zone_mult`, no `sl_buffer_usd`, no `poc_buffer_usd`.
* **R15** The SL shelf-search radius is **independent of any TP cap.**
  v1 passed `min(swing_cap, p95_cap)` as the SL radius, so TP knobs moved
  the stop. Decoupled per the volatility-autocorrelation argument: the cap
  is a TP ceiling only.
* **R15a** Because the radius is decoupled, the SL shelf can resolve
  further than the TP. Guard explicitly: **if `|entry − SL| > |entry − TP|`,
  reject the shelf and use the ATR fallback.** v1 got this implicitly from
  the coupling; v2 states it as a rule so it is visible and testable.
* **R15b** **Correction — v1 does NOT use ATR for the SL.** Canonical is
  `sl_mode='hvn_lvn'` with the shelf; `sl_zone_mult=40` is the *fallback*
  and measured at **45.9% of legs**. `sl_atr_mult` was dead and removed.
  v2's ATR fallback is genuinely new.

## 4. Target

One rule, one fallback, **two legs.** (Was: 5 writers + 3 clamps.)

* **R16** TP = the Nth HVN/LVN shelf **ahead** of entry.
* **R17** If no shelf: TP = `atr_tp_mult × ATR`. No proxy fraction.
* **R18** At most one cap: `tp_cap_swing_pct × (3-day high − low)`.
  default `0.70`, sweep later.
* **R18a** The cap uses an **exact** 3-day rolling high−low. v1's
  `build_daily_swing` resamples to 60 buckets and approximates the range;
  3 days at 60 buckets is ~72 min per bucket. v2 computes the true range.
* **R18b** **Instrument `cap_bound_pct` from cell one.** v1's p95 cap
  tightened **0.0%** of legs and looked healthy for a week. A cap that
  never binds is not a cap.
* **R19** Two legs, both from the **same shelf mechanism**:
  * TP1 = shelf *n* → close `partial_close_pct` (50%).
  * TP2 = shelf *n+1* → remainder.
  * If shelf *n+1* does not exist, the remainder runs to the R18 cap.
* **R19a** **This is the fix for "small TP wins that don't compound."**
  v1's TP2 was `tp_zone_mult_fallback × zone_width` (200× width) — a
  zone-width multiple, not a structural level — and `_separate_tp_legs`
  pinned TP1 onto TP2 whenever both clamped to the cap. TP1 booked a real
  win at a real shelf; the runner then targeted a price that was never
  structural.
* **R19b** **Runner stop = TP1 price** (realised profit becomes the new
  floor). v1 moved the runner to `entry_price` (flat breakeven) — safe, but
  it gives back the entire TP1 move on a retrace. This is the user's
  "move SL of the remaining position."
* **R20** Nothing in the SL or TP path may depend on zone width.

## 4.1 Two SL/TP knob pairs (immediate vs sniper)

Same mechanism, two multipliers. Immediate entries expect closer targets;
sniper entries enter *after* a break, so they need more room.

| | SL | TP |
|---|---|---|
| **A — immediate** | HVN shelf behind, n=1 | shelf ahead, n=`nth_hvn_n` |
| **B — sniper** | HVN shelf behind, n=1 | shelf ahead, n=`nth_hvn_n_sniper` |

One extra knob, no new code path.

## 4.2 Momentum filter — measured, not assumed

Thresholds are from `runtime/diagnostics/probe_momentum_filters` on the
real corpus, NOT from the literature (see the note at the end of this
section — the textbook values are off by an order of magnitude here).

### What was measured

| ER period | p50 | p90 | p95 | p(ER>0.35) |
|---|---|---|---|---|
| 60 bars (1 min) | **0.118** | 0.298 | 0.357 | **5.46%** |
| 300 bars (5 min) | 0.052 | 0.130 | 0.157 | 0.01% |
| 900 bars (15 min) | 0.029 | 0.074 | 0.089 | 0.00% |
| 3600 bars (1 h) | 0.015 | 0.036 | 0.043 | 0.00% |

**The literature thresholds are wrong for this instrument.** The standard
intraday prescription is `ER > 0.35` on 10–14 bars of 5m data. On 1s XAUUSD
that admits **5.46%** of bars at best, and **0.00%** at every period ≥ 5
minutes. ER scales down as `1/√bars` because the denominator (path length)
grows with sample count while the numerator (net displacement) does not.
Copying 0.35 forward would silently gate off the strategy entirely.

**Correct thresholds — period-relative, not absolute:**

* **Immediate entry (A)** — `ER[60] ≥ er_min_immediate`, default **0.15**
  (~p63). Price is making net progress; a resting limit into a level is
  sane.
* **Sniper entry (B)** — a *high* ER is the WRONG gate. The sniper thesis
  is "price broke the level" — which is an efficient displacement, so
  requiring high ER double-counts the signal. Sniper instead wants the
  **displacement** itself: `shock = |r_1s| / median|r_1s|` over the
  invalidation window. Default `shock ≥ 2.0`.
* Both default **OFF** for the first run, per instruction. Enable via
  `build_engine_kwargs(momentum_filter=True)`.

### The 0/0 trap — handled

A dead-flat window has zero net displacement *and* zero path. KAMA's
convention reports that as **ER = 1.0**, i.e. "perfectly efficient." On
gold, with 15% of bars >1s apart, weekend gaps make flat windows common.
Measured flat share: **0.00–0.20%** — small but nonzero at 60 bars.
`kaufman_er` returns **NaN** for that case, and NaN fails the gate. A
filter that treats "no information" as "strong signal" is worse than no
filter.

### Variance ratio — the negative result

VR(q) medians, 3600-bar window, 1y corpus:

| q | 2 | 4 | 8 | 16 | 60 |
|---|---|---|---|---|---|
| p50 VR | **0.950** | **0.916** | **0.890** | **0.865** | **0.802** |
| p(VR>1.05) | 0.85% | 3.78% | 6.34% | 8.86% | 12.22% |

**1s XAUUSD returns are mean-reverting at every horizon tested.** VR < 1
throughout, monotonically decreasing in q, with only 0.85% of bars above
1.05 even at q=2. A momentum/breakout thesis and a negative-autocorrelation
series are in direct conflict.

This does **not** invalidate the strategy — a sweep-and-reverse setup is
*supposed* to be entered against local reversion — but it does mean:

1. **No return-based momentum filter.** A trend filter built on returns is
   fitted to a property this corpus does not have.
2. **A volatility-based filter is still fine**, because vol and return
   autocorrelation decouple (next section). This is the asymmetry that
   makes v2 viable.

### Hurst — confirms the bias warning

| scale | real H | shuffled-noise floor | verdict |
|---|---|---|---|
| 3,600 | 1.0233 | 1.0193 | above floor, +0.004 |
| 86,400 | 0.9954 | 1.0176 | **BELOW floor, −0.022** |

At the daily scale the real series is *less* persistent than shuffled noise.
The apparent signal is the estimator. **Do not use Hurst.** This is the
concrete version of the literature warning, measured on our own data.

## 4.3 Volatility filter — hypothesis CONFIRMED (v2's real edge candidate)

The user's hypothesis: *"volatility is autocorrelated, so the previous
day's high-to-low carries to today, and we can trade the live-vs-prev-day
vol difference."*

**Confirmed, strongly.** Log-log correlation of consecutive daily ranges,
1y corpus (477 days):

| | Pearson | log-log |
|---|---|---|
| 1y | **+0.611** | **+0.461** |

On the 3-month slice the same correlation is **−0.118** — a single quiet
quarter is not enough to see it. This is why v1's flat-slice diagnostics
kept disagreeing with each other: the effect is real but it needs ~1 year
of daily bars to manifest.

**Per-second realized vol, median, 1y:**

| block | $/s |
|---|---|
| 1s | 0.0354 |
| 60s | 0.0163 |
| 1h | 0.00236 |

**Volatility autocorrelation by horizon (lag-1, log space, 1y):**

| horizon | 1s | 60s | 5m | 15m | 1h | 4h | 1d |
|---|---|---|---|---|---|---|---|
| corr | **+0.403** | +0.714 | +0.733 | +0.723 | +0.690 | +0.618 | +0.650 |

**Persistence holds at every horizon including 1s (+0.403).** I had
predicted the textbook microstructure signature — 1s mean-reverting while
1h+ persists — and **I was wrong.** The prediction conflated two distinct
quantities:

* *bid-ask bounce* → negative autocorrelation in **returns** (confirmed: VR < 1)
* *volatility clustering* → positive autocorrelation in **volatility** (confirmed here)

Volatility clusters just as hard at 1s as at 1d. **So a volatility-based
filter is viable at the trade's own timescale even though a return-based
one is not.** That asymmetry is the whole justification for this section.

### How to use it

* `vol_expansion = RV(today so far) / RV(previous day, same elapsed fraction)`
  — normalise by elapsed time, or a morning bar is always "low vol."
* Gate **longs and shorts symmetrically** on expansion; do not use it for
  direction. Direction comes from the FVG.
* Default **OFF** for the first run (`vol_filter=False`), per instruction.

### R21 — zone floor must scale with spread

`fvg_min_zone_usd` is a **flat $0.30**, but the live spread is **$0.40**
round-trip and the median 1s ATR is $0.098. Measured on 2025-01
(1,021 legs): **68.2% of trades had a zone narrower than the spread.**

A zone the market cannot cleanly traverse is not a level — it is a
rounding error, and any fill inside it is a spread donation by
construction.

    zone_floor = max(FVG_MIN_ZONE_USD, zone_min_spread_mult * spread)

Default `zone_min_spread_mult = 1.5` → floor **$0.60**. This is
scale-free (it references the live spread, not a frozen dollar figure),
so it survives a spot or broker change. At 2.0× spread → $0.80.

### R22 — no sub-minute holds (already satisfied; measure, do not fix)

The concern was that sub-minute trades are pure fee churn. Measured on
2025-01, 1,021 legs:

| hold | share |
|---|---|
| ≤ 1 s | **0.00%** |
| ≤ 5 s | 0.10% (1 trade) |
| ≤ 15 s | 0.20% |
| ≤ 60 s | 0.20% (2 trades) |
| median | **27,842 s = 7.7 h** |

**There are no 1s exits to remove.** The D2 resting-limit-order fix is
what guarantees this: a trade cannot open until price actually reaches
the level on a later bar, so sub-second round-trips are structurally
impossible. A `min_hold` filter would be a no-op guard against a
problem that cannot occur.

---

## 4.1b Metric naming traps — two silent ones in `ClosedTrade`

Both were found while measuring R21/R22, and **both produce numbers that
look like a bug in the strategy when they are bugs in the reader.**

**(1) `target_price` is never the leg's own target.**

* On a TP1 partial exit the engine logs `target_price=tp2_price`
  (`vp_multi_target_engine.py:2307`) — the **runner** level.
* On a runner exit it logs the relabelled runner level (line 2466).

So `target_price` reads **$70 median** while the real TP1 geometry is
**$15.57**. An agent (or a human) reading that table concludes "TP is
absurd, 28:1 R:R" and rewrites the target logic. **It would have been
rewriting correct code.**

**(2) `stop_price` is post-breakeven on a TP1 leg**, so `sl_distance_usd`
reads `0.00` there — which is why the p25 of "SL distance" is `$0.00`
in any naive table.

**(3) `target_price_2` is a `0.0` sentinel on 96.7% of legs.** Measured
2025-01: 987 of 1,021 legs have it unset. A naive
`|target_price_2 - entry_price|` over the whole set returns a median of
**$2,692.97** — which is just **spot** ($2,705.97), not a target. Mask
`sentinel > 0` before taking distances; where it *is* set (34 legs) the
median distance is **$69.42**.

Three traps, one shape: **a sentinel or a relabelled field read as a
distance produces a number roughly equal to spot, which is
indistinguishable from a catastrophic bug until you check the units.**
Mask sentinels and read `tp_distance_usd`.

**The honest fields:**

| want | use | never |
|---|---|---|
| leg's take-profit | `tp_distance_usd` | `target_price` |
| risk at entry | `initial_sl_distance_usd` | `stop_price`, `sl_distance_usd` |

Honest 2025-01 geometry:

| metric | p25 | p50 | p95 |
|---|---|---|---|
| SL initial | $6.03 | **$12.57** | $19.62 |
| TP1 partial | $10.38 | **$15.57** | $29.09 |
| leg TP (mixed) | $29.09 | $66.00 | $98.00 |

Real TP1/SL ≈ **1.24**, breakeven win rate ≈ **16%** (vs the bogus
"5.19% / 28:1" the naive read produces). Neither is spectacular, and
**neither is a $72 target.**

* **R21** Flat risk per trade: `risk_pct × equity` at the computed stop
  distance. `qty = risk_usd / |entry − stop|`.
* **R22** Clamp `qty` to the MT4 range [0.01, max_lots].
* **R23** Directional P&L must now scale with the stop, not with a fixed
  1 oz. Without this, wide-stop trades dominate the P&L (v1's
  `sl_zone_mult` ranking artifact).

## 6. Structure filter

* **R24** BOS/CHoCH is a **binary filter**, not a score. Configurable as
  `require_bos_aligned` / `require_choch_against`.
* **R25** v1's conviction path was inert (`conviction_model=None` →
  `_new_signal_score` returns constant 1.0). No score survives into v2.

### 6.1 The primitive already exists; the engine just cannot see it

`bt/_engine/market_structure.py` ships everything:

| function | purpose | v2 use |
|---|---|---|
| `detect_market_structure(...)` | full state machine + `StructureState` | build once per file |
| `bos_choch_directional_alignment(state, bar, dir, max_age_bars=300)` | **explicit gate** | the gate |
| `structure_conviction(...)` | returns a **TP multiplier** | sizing, not gating |
| `BosChochMemory` | rolling last-N events | cheap streaming update |

`bos_choch_directional_alignment` is exactly the primitive needed, and
its semantics are already correct for v2: it **rejects** when no
qualifying event exists (`None` = "no alignment, reject"). The
pass-through alternative would make the gate inert during exactly the
quiet periods where it matters.

**The blocker is that the VP engine is structurally blind to it.**
`grep structure bt/_engine/vp_multi_target_engine.py` → **no matches**.
`run_vp_sniper_backtest` takes `bars, zones, vp_set` and no
`StructureState`. So BoS/CHoCH is not merely unused — it is
**unreachable** from the strategy path, exactly like v1's dead
`ms_pivot_len`. Wiring it means threading `StructureState` through
`run_vp_sniper_backtest`, and it is **free** — the bar-engine
precompute already builds the structure state.

### Why it outranks the momentum filter

v1's core failure was that *both* sides correlate ±0.96 with market
direction — a beta proxy, long by construction. Only a **signed** filter
can break that symmetry:

* **ER / vol filters decide WHETHER to trade.** Unsigned. They cannot
  stop the minority-side trades that create the beta.
* **BoS/CHoCH decides WHICH SIDE.** Signed. It can reject exactly the
  trades that made v1 a levered market bet.

So they compose rather than compete: **structure picks the side, ER and
vol pick the regime.**

### Spec

* `structure_gate = False` by default (first run OFF, consistent with
  the momentum and vol filters).
* `structure_max_age_bars = 300` (5 min on 1s) — matches the engine
  default and the FVG `max_age_secs = 3600` scale.
* **BoS continues, CHoCH reverses.** Align to the *most recent* event's
  resulting thesis per `BosChochMemory`'s documented walk: admit a long
  only when the current thesis is bullish (last event `BOS_BULL`, or
  `CHOCH_BULL` flipped it there).
* **Skip, never auto-invert.** Do not trade the opposite side on
  misalignment — that converts a filter into a reversal strategy and
  would re-create the symmetric bet this rule exists to break.

### 6.2 IMPLEMENTED 2026-10-23 — threaded, wired, measured

`StructureState` now flows `detect_market_structure` → `ArtifactCache`
→ `build_engine_kwargs(structure=…)` → `run_vp_sniper_backtest`, gated
by `_structure_pass` at both emission sites (immediate + sniper). It is
cached with the other artifacts because it is a function of
`(bars, pivot_len)`, not of the sweep knob.

A/B on 2025-01 (9,673 signals, same cached artifacts):

| config | trades | net | dropped | aligned | opposed | no_event |
|---|---|---|---|---|---|---|
| gate OFF | 991 | −$10.00 | 0 | — | — | — |
| gate ON | 953 | −$7.95 | **2,905 (30.0%)** | 6,768 | 2,904 | 1 |

Signal accounting stays conserved (`n_signals_emitted` − drops = placed).
`diag["structure_gate"]` exposes the tri-state so a run can prove the
gate fired rather than silently passing everything. **The gate raises
loudly** if `structure=None` or if the state length ≠ bars length.

`STRUCTURE_PIVOT_LEN = 9` is the only value with any measurement behind
it (it is the `detect_market_structure` default). `ms_pivot_len=75` is
a BTC number and was a dead knob on this engine, so **no gold pivot has
been validated** — sweep it before trusting the gate.

### 6.4 MEASURED 2026-10-23 — is BoS/CHoCH a directional filter?

Two bugs had to be fixed before the question was even answerable.

**(a) The resample was 100% wrong.** `detect_market_structure(resample_to_n_secs=60)`
aggregated with a positional `reshape`, which assumes a gapless feed.
Measured on XAUUSD_S1_2025Q1: **15.4%** of bars are >1s apart and
**91.9%** of 60s buckets do not hold 60 bars (median 48). Result:

```
fraction of resampled bars WRONG   100.0%
abs diff   median $19.39   p95 $56.80   max $85.82
```

Every BoS/CHoCH was derived from fabricated highs. Fixed to bucket by
timestamp; `ts_ns` is now **mandatory** when resampling (it raises
rather than silently shearing). Verified exact against pandas:
max diff `0.000000`, 30,197 buckets both ways. Same bug class as NB94.

**(b) `signals_unaccounted` read 7,991.** The conservation chain omitted
`n_dropped_structure`, making it look as though the engine lost 80% of
its signals. Fixed; now **0**, with byte-identical results.

### pivot_len sweep (gate ON, 2025-01)

| pivot | trades | net | EV | PF | hit | maxDD | aligned | opposed | no_event |
|---|---|---|---|---|---|---|---|---|---|
| **3** | 1,067 | **−$665** | −0.62 | **0.90** | 0.314 | $1,800 | 1,682 | 1,639 | 6,352 |
| 5 | 810 | −$923 | −1.14 | 0.82 | 0.299 | $1,943 | 1,138 | 1,137 | 7,398 |
| 9 | 510 | −$845 | −1.66 | 0.76 | 0.276 | $1,431 | 710 | 696 | 8,267 |
| **15** | 314 | **−$374** | −1.19 | 0.82 | 0.299 | **$1,007** | 410 | 400 | 8,863 |
| 21 | 244 | −$422 | −1.73 | 0.75 | 0.299 | $872 | 318 | 326 | 9,029 |
| 33 | 153 | −$536 | −3.50 | 0.59 | 0.235 | $867 | 202 | 205 | 9,266 |
| 45 | 104 | −$411 | −3.95 | 0.51 | 0.212 | $453 | 141 | 145 | 9,387 |

Monotone, and not usefully: wider pivot → fewer trades, worse EV, worse
PF. Best PF (0.90) is at the *narrowest* pivot. **No cell profitable.**

### Verdict: NOT a directional filter

`struct_verdict` is now recorded on every trade (gate off as well as on),
so P&L can be attributed after the fact. 2025-01, gate OFF, pivot 9:

| verdict | legs | net | EV | win% | PF |
|---|---|---|---|---|---|
| aligned | 123 | −$2 | −0.02 | 25.2% | 0.71 |
| opposed | 116 | −$1 | −0.00 | 30.2% | 0.92 |
| no_event | 1,531 | −$11 | −0.01 | 31.9% | 0.88 |

**Aligned trades have the WORST profit factor (0.71).** The bucket that
"agrees with structure" is the least profitable of the three. Splitting
by direction (the decisive test):

| verdict | side | legs | net | EV | win% |
|---|---|---|---|---|---|
| aligned | long | 70 | +$2 | +0.02 | 38.6% |
| aligned | short | 53 | −$4 | −0.08 | **7.5%** |
| opposed | long | 47 | $0 | +0.00 | 38.3% |
| opposed | short | 69 | −$1 | −0.01 | 24.6% |
| no_event | long | 750 | +$25 | +0.03 | 43.9% |
| no_event | short | 781 | −$36 | −0.05 | 20.5% |

**What the split actually shows is the market, not structure.** The
slice rose **+6.58%**. Every long bucket wins more than its mirror short
bucket regardless of verdict — `no_event` (structure never consulted,
so it is pure control) shows the widest spread: +$0.03 vs −$0.05. That
control bucket is the baseline: it demonstrates the long/short split is
market direction, and the aligned bucket is *not* better than it.

Three failures, in order of severity:

1. **The gate is mostly an absence test.** At pivot 15, 8,863 of 9,673
   probes had *no recent break*. Only 410 were aligned, 400 opposed. So
   ~92% of rejections are "structure said nothing", not "structure
   disagreed". That makes it a trade-frequency dial, not a directional
   filter — which is exactly why trades fall 1,067 → 104 while EV gets
   worse.
2. **Aligned is not better than the control.** The unconsulted bucket
   is at least as good as the structure-aligned one.
3. **`no_event` is the largest bucket and the best-behaved.** The filter
   would be improved by admitting trades structure has no opinion on,
   which inverts the current rule.

### 6.5 Two-stage SCORE filter (2026-10-23) — the designed replacement

**The `no_event` explosion was my bug, not the data.** The claim that
"structure should always exist" is correct and the old gate was wrong.
`bos_choch_directional_alignment` had a `max_age_bars` timeout, and a
timeout return of `None` was treated as REJECT — so the gate was
measuring *the timeout*, not structure. Measured on 2025-01:

```
break events on the slice            857
  BOS_BULL / BOS_BEAR / CHOCH_*       241 / 219 / 218 / 179
bar of the 5th event                  10,860   (0.79% into the slice)
events available at 25/50/75/100%     176 / 330 / 503 / 857
```

Five events are in history for **99.2%** of the slice. The 6,352–9,387
`no_event` rejections were an artifact.

#### Stage 1 — BINARY alignment

```
score = Σ_j ( w_j · mag_j · base_j ) / Σ_j ( w_j · mag_j )

base_j     BoS  = ±1 (bull/bear)
           CHoCH = ±1, weighted 2.0  (a character change carries more
                   information than another continuation break)
w_j        0.5 ** (age_bars / half_life)      half_life = 1800 (30 min)
```

**No timeout.** Events leave the window only when pushed out by 5 newer
ones, so the score is always defined and is never "no structure".
Admit iff `direction · score > 0` and `|score| ≥ 0.10`. Still
**skip, never invert.**

#### Stage 2 — SL/TP scaling

Only Stage 1 survivors. `magnitude = direction · score` ∈ [0, 1] maps to
a stop multiplier and a target multiplier:

| | weak (|s|=0) | strong (|s|=1) |
|---|---|---|
| stop | `sl_scale_max` 1.30 | `sl_scale_min` 0.70 |
| target | `tp_scale_min` 0.90 | `tp_scale_max` 1.30 |

The stop is scaled **inversely**: strong conviction means a *tighter*
stop relative to an *extended* target, so R:R improves with conviction
rather than decaying as the target grows. Neutral (1.0) when the gate is
off, so pre-gate numbers stay reproducible.

#### ⚠️ `vel_5m` is UNSIGNED — measured, and it matters

```
vel[i] = |P_i - P_{i-w}| / P_{i-w}
2025-01, 1,377,190 bars:  min 0.0   p50 0.0003   max 0.0052
```

It never goes negative **by construction**, so it measures how *fast*
price moved, never *which way*. (It is also causality-correct — an
earlier `close[w:] - close[:-w]` form was a forward-looking bug, fixed
2026-10-13. Do not reintroduce it.)

So signed momentum takes its sign from price, via the backward
difference `P_t − P_{t−w}` — the same quantity `vel_5m` takes the
absolute value of. The first implementation returned **0.0 on 100% of
bars** because it assumed `vel_5m` was signed; a silent modulator-veto,
the exact failure mode AGENTS.md warns about. `test_momentum_degrades_to_zero_rather_than_raising`
and the signed-momentum test now lock the sign.

```
                     min     p10     p50     p90     max    zeros
structure          -1.00   -0.71   +0.25   +0.87   +1.00      0%
momentum           -1.00   -0.29   +0.01   +0.31   +0.99      0%
combined           -0.96   -0.45   +0.11   +0.54   +0.97      0%
corr(structure, momentum) = +0.012     (near-orthogonal by design)
```

## 6.9 THE CADENCE DEFECT — FVG ran on 1s while everything else ran on 1m (2026-10-23)
**The user was right: "we do FVG on 1 min candles, not 1 second, only
our execution is 1 second. same as all indicators should also be 1 min."**

Every component in the recipe was on 1m **except the FVG detector**:

| component | cadence | source |
|---|---|---|
| market structure (BoS/CHoCH) | **1m** | `STRUCTURE_RESAMPLE_SECS = 60` |
| volume profile | **1m** | `build_volume_profiles` resamples 1s→1m |
| swing cap | **1m** | built from resampled bars |
| rolling p95 | **1h** | `inner_secs=3600` |
| **FVG detector** | **1s** ← BUG | `resample_to_n_secs` absent from detector kwargs |

`resample_to_n_secs` was never in `detector_kwargs_from_recipe()`, so
`detect_fvg` silently used its default of `0` (= 1s). The recipe's own
`ms_resample_secs = 60` was carried into the *bar* engine and into
`detect_market_structure`, but not into the sweep driver's detector
call.

### What it cost (2026-01, 1.38M 1s bars)

| cadence | zones | med width | mitigated | pierced | **inverted** | superseded |
|---|---|---|---|---|---|---|
| 1s (was) | 116,743 | $0.47 | 173 (0.15%) | 49 (0.04%) | **7 (0.006%)** | 99.61% |
| **1m (now)** | **4,767** | **$1.11** | 208 (4.4%) | 137 (2.9%) | **130 (2.7%)** | 94.2% |

The 1s detector produced **24× more zones** — a $0.47-wide zone on a
tick-dominated 1s feed is noise, and a new one appears every ~250ms, so
the overlap-supersession cascade killed 99.61% of them before price
could commit through. That is the *whole* explanation for the sniper
path being empty. It was never a market fact.

### It also fixes the sniper path (the §6.8 finding)

| slice | mode | legs | signals | inversions | net |
|---|---|---|---|---|---|
| 2026-01 BULL | dual | 2,485 | 4,767 | 130 | +$6 |
| 2026-01 BULL | **sniper** | **141** | 130 | 130 | **+$31** |
| 2026-01 BULL | immediate | 2,481 | 4,767 | 0 | +$10 |
| 2026-03 BEAR | dual | 1,416 | 5,441 | 139 | +$10 |
| 2026-03 BEAR | **sniper** | **145** | 139 | 139 | **+$20** |
| 2025-05 FLAT | dual | 2,115 | 3,895 | 78 | −$27 |
| 2025-05 FLAT | **sniper** | **72** | 78 | 78 | **+$2** |

Sniper went from **3 legs → 141** and is now **net positive on all three
slices including the bear month and the flat month**. That is the first
time any slice-independent result has appeared in this repo.

> **Caveat — this is still not an edge.** The absolute numbers are tiny
> (+$31 on 141 legs = $0.22/trade against a $0.47 round trip — the gross
> must be positive for net to be, so the gross needs checking). The
> *direction* is what matters: the sniper path is now measurable. It was
> not measurable before, so every previous sniper conclusion was
> vacuous.

### Fix

`detector_kwargs_from_recipe()` now carries the recipe's
`ms_resample_secs` into the detector:

```python
fvg_resample = int(getattr(P, "ms_resample_secs", 0) or 0)
if fvg_resample > 0:
    kwargs["resample_to_n_secs"] = fvg_resample
```

The artifact fingerprint already covers `detector_kwargs`, so the cache
invalidates correctly. `max_zone_age_bars` needed no change — the
detector already converts it to the resampled cadence
(`ict_signals.py:1319`).

Guards: `test_fvg_detector_runs_on_the_same_cadence_as_every_other_indicator`,
`test_inversion_is_not_structurally_starved`.

> **This invalidates the §6.7 sequence analysis.** The scalar score and
> the sequence histogram were both computed over a 1s zone set — 24×
> more zones, 60× more noise, and a different trade population. **All
> sequence results must be re-measured at 1m before being believed.**
> The score/scalar finding (corr +0.030) is a property of the *scoring
> function*, not of the zone set, so that specific number stands; the
> per-sequence EV table does not.

### 6.9.1 The sequence signal RE-MEASURED at 1m — and it is far more stable

6 slices, 10,495 legs, pooled:

```
TOTAL legs=10495  net=-$38
64 sequences with >=25 legs.  EV range -$0.16 .. +$0.09   positive: 29/64
corr(sequence scalar score, sequence EV) = +0.034
```

| | 1s cadence (invalid) | **1m cadence (canonical)** |
|---|---|---|
| legs | 21,000+ | 10,495 |
| sequences (≥25 legs) | 63 | 64 |
| EV range | $−0.14 … +$0.21 | $−0.16 … +$0.09 |
| positive sequences | 26/63 | **29/64** |
| top-10 seqs positive in ≥4/6 slices | *not measured* | **8/10** |
| corr(scalar, EV) | +0.030 | +0.034 |

Two things to note.

**The scalar score still explains nothing** (+0.034, unchanged) — that
result was a property of the scoring function and survives the cadence
fix. The *shape* is what carries the information.

**The sequences change identity with cadence.** `bCcbb` was the best
sequence at 1s (EV +$0.13) and is **negative at 1m** (EV −$0.07). At 1s
the "last 5 events" were 5 events out of a 5-second window of tick
noise; at 1m they are 5 events across 5 minutes of real structure. They
are *different signals with the same name*. Every 1s-cadence sequence
result was measuring noise.

**Stability is dramatically better at 1m** — 8 of the top-10 pooled
sequences are net-positive in at least 4 of the 6 slices. That is the
first time a per-signature result has shown any cross-slice
persistence, and it is the reason the 1m cadence matters beyond just
"more correct".

## 6.7 The SEQUENCE, not the score (2026-10-23) — and the anti-predictive hypothesis: TESTED AND REJECTED

The 5-event **scalar score is provably a sufficient statistic — and that is
why it fails.** Measured on 2026-01 (64 distinct sequences, 60 with ≥20
legs):

```
corr( sequence's scalar score , sequence's realised EV ) = +0.030
```

Zero. If the score explained the sequence's edge, this would be high. So
the *shape* of the 5 events carries information the mean threw away, and
the score is measuring something else entirely.

Pooled across 3 trending slices (63 signatures with ≥40 legs):

| best | legs | EV | | worst | legs | EV |
|---|---|---|---|---|---|---|
| `CcCBB` | 103 | **+$0.21** | | `cbCBB` | 157 | **−$0.14** |
| `bCcbb` | 274 | +$0.13 | | `Ccbbb` | 132 | −$0.12 |
| `cbCcC` | 345 | +$0.12 | | `CcbCc` | 253 | −$0.10 |
| `bbbbb` | 118 | +$0.11 | | `cCBBc` | 305 | −$0.10 |

`26/63` sequences are net-positive. Range **$−0.14 … +$0.21** — a ~$0.35
spread per trade, which is real, but **not stable across slices**: the
best signature (`CcCBB`) prints −0.09 / −0.03 / −0.09 / +0.12 / +0.56
across the five slices. There is no signature that wins everywhere, which
is the same verdict as before in a different coordinate.

Your "shaky bullish" intuition has support but not as a *clean-run*
rule — EV by number of BOS↔CHOCH transitions is flat:

```
0 transitions (one clean run)   n_seq=3   mean EV = $+0.03
1 transition                    n_seq=16  mean EV = $+0.01
2 transitions (shaky)           n_seq=24  mean EV = $-002
3 transitions                   n_seq=16  mean EV = $-0.00
4 transitions                   n_seq=4   mean EV = $+0.00
```

Shaky is *slightly* worse than clean, but the ordering does not separate.

### Why sniper is structurally EMPTY (2026-10-23)

`entry_mode` was un-sweepable until 2026-10-23 — it had been removed from
the engine allowlist on 2026-10-21 (correctly: it is an engine kwarg, not
a recipe field), which left the sniper path unreachable for A/B. It is
now restored to `_ENGINE_OVERRIDES_ALLOWED`.

Measuring it exposed something worse than a filter problem.

```
entry_mode   signals    inversions   legs (2026-01)
dual         116,743            7        6,645
immediate    116,743            0        6,645   <- IDENTICAL to dual
sniper             7            7            3
```

**`dual` and `immediate` produce identical leg counts.** The canonical
recipe has never traded a distinct sniper setup — `dual` *is*
`immediate`. Every sniper cell in the 24-cell matrix is 0–14 legs and
statistically empty.

#### Root cause: 99.61% of zones are superseded before they can invert

Inversion funnel, 2026-01 (116,743 zones):

| stage | count | share |
|---|---|---|
| mitigated | 173 | 0.15% |
| pierced | 49 | 0.04% |
| **inverted** | **7** | **0.01%** |
| **superseded** | **116,292** | **99.61%** |
| expired | 451 | 0.39% |
| still live | 451 | 0.39% |

The supersession rule kills a zone as soon as *any* later zone overlaps
its price range (`ict_signals.py:1247`). On 1s gold a new FVG forms every
~250ms and the median zone is $0.37 wide, so **overlap is nearly
guaranteed within one hour** — which is exactly the detector's own
`max_zone_age_bars = 3600`. Measured: zones that pierce but never invert
die a median **3,574 bars** after the pierce, p90 **3,592** — i.e. at the
3600-bar expiry horizon.

So the sequence is:

1. Zone is created.
2. Within ~1 hour (the walk cap), a newer overlapping FVG supersedes it.
3. Price never commits through it, so `pierced_bar` is never set.
4. `inverted_bar` is never set.
5. The sniper path, which fires only on `inverted_bar >= 0`, sees nothing.

**A zone must be pierced within one hour of being created to have any
chance of inverting.** Price at 1s cadence rarely commits $0.05 beyond a
zone edge and returns within that window — `invalidation_min_pierce_usd`
is $0.05 on a zone of median width $0.37, and it needs **2 consecutive**
1s bars plus a retest back through the zone. On a tick-dominated feed
those three conditions almost never co-occur before expiry.

> **This is a detector defect, not a market fact.** iFVG inversion is a
> core ICT concept and should fire far more often than 0.006% of zones.
> Any filter evaluated on the sniper path is being evaluated on 7 trades.
> **Fix the inversion walk before tuning anything on `sniper`.**

Guard: `tests/test_regressions.py` gains an inversion-rate check.

Inverting the Stage 1 sign test so the gate demands the score **disagree**
with the trade, across 6 slices × both polarities:

| slice | regime | aligned | opposed | Δ (aligned−opposed) |
|---|---|---|---|---|
| 2025-01 | bull +6.58% | −$8 | −$9 | +$1 |
| 2025-09 | bull +12.06% | −$20 | −$23 | +$3 |
| 2026-01 | bull +13.21% | −$27 | −$92 | **+$66** |
| 2024-11 | bear −3.52% | −$11 | −$10 | −$1 |
| 2026-03 | bear −12.59% | **+$11** | −$84 | **+$94** |
| 2025-05 | flat +0.50% | −$16 | −$41 | +$25 |
| **TOTAL** | | **−$70** | **−$258** | **+$188** |

**The anti-predictive reading is wrong.** Inverting makes it $188 *worse*
across the corpus. The earlier "decile 1 is best" observation was a
selection artefact of a single slice, not a rule — exactly the error
AGENTS.md warns about. Aligned stays.

### Sniper-only mode is structurally near-empty

| entry_mode | signals | inversions | legs (2026-01) |
|---|---|---|---|
| `dual` | 116,743 | 7 | 6,645 |
| `immediate` | 116,743 | 0 | 6,645 |
| `sniper` | **7** | 7 | **3** |

```
zones with an inverted_bar set: 7 of 116,743   (0.006%)
```

`entry_mode` was **un-sweepable** until 2026-10-23 (it had been removed
from the engine allowlist on 2026-10-21 for a good reason — it is an
engine kwarg, not a recipe field — but that left the sniper path
unreachable for A/B). It is restored to `_ENGINE_OVERRIDES_ALLOWED`.

The finding is that the sniper path is **not a smaller strategy, it is an
empty one**: 0.006% of zones invert. `dual` and `immediate` produce
*identical* leg counts (6,645 = 6,645), so the canonical recipe has never
traded a distinct sniper setup — `dual` is `immediate` plus 7 trades. Every
"sniper-only" cell in the matrix is 0–14 legs and statistically empty.
Sniper evaluation needs the inversion detector fixed first, not a filter.

Gate OFF (so this is pure measurement, no admission effect), 2025-01,
1,770 legs, pivot 9:

| \|score\| | legs | net | EV | win% |
|---|---|---|---|---|
| [0.0, 0.2) | 476 | −$7 | −0.01 | 30.9% |
| [0.2, 0.4) | 589 | **+$2** | **+0.00** | 34.8% |
| [0.4, 0.6) | 485 | −$3 | −0.01 | 31.8% |
| [0.6, 0.8) | 213 | −$5 | −0.02 | 22.1% |
| [0.8, 1.0] | 7 | −$0 | −0.02 | 28.6% |

**Non-monotone, and the high-conviction buckets are the WORST.** Hit
rate *falls* from 34.8% at `|s|∈[0.2,0.4)` to 22.1% at `[0.6,0.8)`.
The signed split:

| side | \|s\|≥ | legs | net | EV | win% |
|---|---|---|---|---|---|
| long | 0.0 | 524 | +$17 | +0.03 | 43.9% |
| long | 0.4 | 343 | +$10 | +0.03 | 42.0% |
| short | 0.0 | 541 | −$22 | −0.04 | 22.6% |
| short | 0.4 | 362 | −$19 | −0.05 | 16.3% |

The slice rose +6.58%, so longs beating shorts is the market, and it
holds at every conviction tier. Crucially, going from `|s|≥0` to
`|s|≥0.4` moves long EV **+0.03 → +0.03** (flat) and makes short EV
**worse** (−0.04 → −0.05). Requiring higher conviction does not improve
either side.

So the honest summary of three independent attempts — binary
`max_age_bars` gate, binary no-timeout gate, continuous 5-event score
with momentum — is the same each time: **structure adds no information
beyond which side the market already moved.** The scoring machinery is
now correct, tested (18 guards), and ready; the recipe is still a beta
proxy. Gate stays **OFF** pending the flat-slice `directional_beta` run.

## 6.11 FLIP vs WHITELIST on the trailing-event signature (2026-10-23)

With the 1m cadence fixed, two ways to use the exact event sequence were
tested. Both are fit on **trending slices** (2026-01, 2025-09, 2026-03)
and evaluated on **held-out slices** (2025-01, 2024-11, 2025-05).

### FLIP — take losing sequences the other way: **does NOT generalise**

Engine surface `structure_seq_flip_map` (new, `None` = disabled, so every
existing call site is byte-identical). A flipped trade reverses side
*and* enters at the opposite zone edge.

| slice | mkt | legs | flipped | net (no flip) | net (flip) | Δ |
|---|---|---|---|---|---|---|
| 2025-01 UP | +6.58% | 1,166 | 726 | −$4 | −$3 | +$2 |
| 2024-11 BEAR | −3.52% | 1,454 | 1,164 | −$13 | −$14 | −$1 |
| 2025-05 FLAT | +0.50% | 2,115 | 1,100 | −$27 | −$35 | −$8 |
| **TOTAL** | | | | **−$45** | **−$52** | **−$8** |

**Rejected.** A losing sequence is not a winning sequence played
backwards — it is a sequence that occurs when price is choppy, and price
stays choppy either way.

### WHITELIST — only take sequences with positive pooled EV: **generalises**

| slice | mkt | legs | kept | base net | wl net | Δ | base EV | **wl EV** |
|---|---|---|---|---|---|---|---|---|
| 2025-01 UP | +6.58% | 1,166 | 384 | −$4 | −$2 | +$2 | −0.004 | **−0.006** |
| 2024-11 BEAR | −3.52% | 1,454 | 296 | −$13 | −$5 | +$8 | −0.009 | **−0.017** |
| 2025-05 FLAT | +0.50% | 2,115 | 697 | −$27 | −$8 | +$19 | −0.013 | **−0.012** |
| **TOTAL** | | | | **−$45** | **−$16** | **+$29** | | |

19 of 64 signatures cleared `EV ≥ +$0.05` with ≥25 pooled legs.

> **This is exactly the trap AGENTS.md warns about.** Net improved by
> $29 but **per-trade EV got WORSE on 2 of 3 slices** (−0.004→−0.006,
> −0.009→−0.017) while the leg count fell 62%. On the flat slice
> "EV improved" ($-0.013 → $-0.012) while still being **negative** —
> that is trading less and paying less fee, not an edge.

### The decisive check — GROSS, before fees

| slice | base gross | wl gross | **Δ gross** | base net | wl net |
|---|---|---|---|---|---|
| 2025-01 UP | +$1 | −$1 | **−$2** | −$4 | −$2 |
| 2024-11 BEAR | −$7 | −$4 | **+$3** | −$13 | −$5 |
| 2025-05 FLAT | −$18 | −$5 | **+$13** | −$27 | −$8 |
| **TOTAL** | **−$24** | **−$9** | **+$14** | −$45 | −$16 |

Gross improves **+$14** and gross is still **−$9** — i.e. the
whitelist trades a genuinely better subset, but **not one that is
profitable before fees**. The $29 net improvement is roughly half gross
improvement and half fee avoidance.

**Verdict: the sequence shape carries real information (the scalar score
does not), but at these magnitudes it is not an edge.** On a $0.47
round trip, the whitelist's best held-out EV is −$0.006/trade. Nothing
here is promotable to live.

### Why "shaky" is still not the right axis

| BOS↔CHOCH transitions | mean EV (1s, invalid) | mean EV (1m, canonical) |
|---|---|---|
| 0 (one clean run) | +$0.03 | — |
| 1 | +$0.01 | — |
| 2 (shaky) | −$0.02 | — |
| 3 | −$0.00 | — |

The clean-vs-shaky gradient does not separate at either cadence. What
*does* separate is the **exact signature**, and only at the 1m cadence.

## 6.3 TP2 anchoring + the swing window — IMPLEMENTED 2026-10-23

Three coupled changes, because "all TPs HVN-based and within the 3-day
swing" is one requirement with three failure modes.

**(a) Swing window 24h → 3 days.** `SWING_LOOKBACK_SECS = 259_200`. A
24h window measures *one* day, which collapses on a quiet UTC session
and tightens the cap for reasons unrelated to the trade. Measured on
2025-01, 24h cap p50 was **$19.27**; the 3-day cap p50 is **$32.58**.
The cap is still `cap_pct × rolling p2t`, so a 3-day range is bounded
by how far gold actually went.

**(b) `tp2_mode`.** v1 anchored TP2 on `tp_zone_mult_fallback ×
zone_width` = 200 × $0.36 = **$72**, against a ~$20 cap — unreachable
by construction, which is why the "remainder" leg was a vacancy.
Options: `zone_mult` (v1 A/B), `hvn` (canonical — next shelf beyond
TP1), `swing` (pinned at the cap).

**(c) TP2 is now swing-capped** (`tp2_swing_cap=True`). This closes the
*documented known gap*: "_apply_swing_cap is applied to TP1 only". The
old reason for leaving it — that a second ceiling would collapse the
runner onto TP1 — is exactly what `_separate_tp_legs` handles, and it
runs after the caps.

### Measured (2025-01, same cached artifacts)

| config | trades | net | legs TP inside 3d swing | tp1 / tp2 / tp / sl |
|---|---|---|---|---|
| v1 baseline | 970 | −$11.20 | **15.8%** | 196 / — / 54 / 680 |
| + tp2 swing cap | 1,708 | −$12.08 | **57.3%** | 212 / 193 / 108 / 1,155 |
| + `tp2_mode='hvn'` | 1,770 | −$13.64 | **64.5%** | 64 / 387 / 64 / 1,215 |
| + structure gate | 1,622 | −$12.29 | **63.6%** | 64 / 351 / 62 / 1,105 |

**Legs with a reachable TP went 15.8% → 63.6%.** The `tp2` exit
appeared from nothing (0 → 351) — that is the runner leg actually
filling for the first time.

**None of this is an edge.** Every cell is net-negative, and the trade
count nearly doubles because capped targets fill where they previously
could not. That is more trades paying more fees, and the AGENTS.md
beta-proxy warning applies in full: these are *geometry* fixes, and the
`structure` pivot has never been validated on gold.

## 7. Book, metrics, performance

* **R26** One cap: `max_concurrent`. No direction lock, no `conflict_policy`
  — all same-direction trades stack up to the cap.
* **R27** Cap is a **risk** control, not a P&L control. v1's cap was the
  root cause of the directional lock and moved net P&L more than every stop
  geometry change combined. Report P&L at ≥3 cap values before believing
  any result.
* **R28** Track `max_concurrent_observed` as a cheap O(1) counter inside
  the existing compaction step. **Never** a separate O(open) pass.

### 7.1 Free metrics

The rule: **nothing added to the per-bar path may be O(open_trades).**

| Metric | Cost | How |
|---|---|---|
| Sharpe, Calmar, maxDD | **free** | per-**trade** equity curve (~1.5k floats, not 1.38M) |
| EV/trade, PF, hit rate | free | already computed per closed trade |
| max open positions | free | O(1) counter in compaction |
| MFE / MAE per trade | free | already tracked (`mfe_usd`, `mae_usd`) |
| `cap_bound_pct` | free | counter at the R18 clamp |
| bar-level MTM equity | **expensive** | flag-gated, ≤3-month files only |

v1's per-bar equity curve was disabled above 8M bars because it costs the
open-book scan P2–P4 removed. Do **not** re-enable it for 1y. Per-trade
equity gives Sharpe/Calmar/maxDD with a one-trade lag and no per-bar work.

MFE/MAE is what distinguishes "TP too far" from "TP never in the money" —
keep it even though nothing currently consumes it.

## 8. Dead on arrival (explicitly removed)

`sl_zone_mult` · `sl_buffer_usd` · `sl_atr_mult` · `poc_buffer_usd` ·
`tp_zone_mult_fallback` · `tp_max_distance_usd` · `tp_hvn_proxy_frac` ·
`lvn_skip_usd` · `lvn_mode` · `sl_nth_hvn_n` (folded into the pair in §4.1) ·
`sl_hvn_max_distance_usd` · `tp_p95_mult` · `swing_cap` (module deleted) ·
`rolling_p95` (module deleted) · all 6 `vol_state_*` (were all `0.0` ⇒
permanently open) · `conviction_*` · `conflict_policy` · `entry_fill_model`
(only `touch` survives) · `fill_model` (split into R10's two decisions) ·
`max_zone_age_bars` (hardcoded 6h) · `_separate_tp_legs` · `_reachable_tp_band_usd`

**Kept, deliberately:** `partial_close_pct` (now with a real TP2 — §4),
`body_only_*` (both on), `supersede_on_new` (minus the cancel behaviour —
§1.1), MFE/MAE.

## 9. Module layout

```
bt/v2/
 spec.py     frozen config — every field required, no defaults
 detect.py   FVG + lifecycle (fresh / mitigated / invalidated / dead)
 levels.py   HVN/LVN shelf lookup + ATR fallback
 entries.py  Entry A + Entry B
 exits.py    SL / TP resolution + risk sizing
 engine.py   bar loop
report.py    per-trade records + summary
runtime/sweeps/v2_sweep.py
tests/test_v2_*.py
```

Runs **alongside** v1. No deletion until v2 beats v1 on the
direction-independent test.

## 10. Validation — the gate, not the P&L

A v2 config is not accepted on net P&L. It must clear, in order:

1. **Flat-market slices.** `|market move| < 3%`. Gross P&L > 0. This is
   the decisive test; v1 scored 1/3.
2. **Long/short gross correlation vs market move.** Both `|corr| < 0.5`.
   v1 scored +0.966 / −0.961.
3. **Long vs short gross both positive**, aggregate over ≥8 monthly slices.
4. Only then: net P&L, EV, PF, maxDD.

Report all four for every run. A config that fails 1–3 is a beta proxy
regardless of what its net P&L says.

## 11. Open questions

* **Q1 — sniper direction.** Continuation (same side as the FVG) or flip
  (opposite side)? See below; this is the highest-leverage unknown.
* **Q2** Entry A depth threshold `D` — sweep 0.3 / 0.5 / 0.7.
* **Q3** Invalidating consecutive closes `K` — sweep 1 / 2 / 3.
* **Q4** Invalidation **threshold**: beyond the zone edge, or
  `edge + invalidate_atr_mult × ATR`? The latter is regime-normalised and
  probably right on 1s gold, but it is a knob again — so exactly one.