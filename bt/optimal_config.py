"""Canonical config — THE single source of truth.

Every canonical value for this repo lives in THIS module, exactly
once, as a module-level constant. There is no recipe dataclass, no
upstream re-export, and no second copy of any knob.

Why this file is shaped this way
===============================

On 2026-10-24 this repo's config surface was audited and found to have
**two** sources of truth for the same knobs, plus a large amount of
superseded machinery:

* ``bt/_engine/optimal_config.py`` held a ``TrendStrategyParams``
  recipe with TWO recipes (SNIPER + HF_TIGHT) and an ``optimal_params()``
  override factory.
* ``bt/optimal_config.py`` re-exported that recipe and added a second
  layer of constants on top.

The fatal defect: **the production engine never read the recipe at
all.** ``run_vp_sniper_backtest`` has no ``params`` argument, and
``bt.vp_engine.run_vp_sniper_backtest_forex`` accepted ``params=None``
and then silently dropped it. So every key in the recipe override
allowlist was a **no-op on the VP path** — a sweep of
``fvg_inv_trade_sl_zone_mult`` would report a "canonical" cell that
measured nothing.

The HF_TIGHT recipe had **zero** code callers (it appeared only in
docstrings). The bar engine (``run_ict_backtest``) and the precompute
cache (``precomputed.py``) had **zero** production callers. All of it
is gone now.

What replaced it
================

* Canonical values: module constants in THIS file, each defined once.
* Materialisation: SEVEN builders, one per target surface, each reading
  the constants directly —
  :func:`build_engine_kwargs` (the VP engine),
  :func:`build_vp_kwargs` (volume profile),
  :func:`build_detector_kwargs` (FVG detector),
  :func:`build_swing_kwargs` (swing cap),
  :func:`build_p95_kwargs` (backtest p95 cap),
  :func:`build_live_p95_kwargs` (live-VPS p95 coldstart), and
  :func:`build_structure_kwargs` (market structure).
  Where the target function's argument name differs from the
  canonical knob name, a translation map renames it at the boundary
  so the target never receives a knob it does not understand.
* Sweeping a knob: pass it to the matching builder. There is no
  second recipe to keep in sync and no dataclass to copy.

The builders still take overrides, but an override is a **per-call
experiment**, never a stored alternative recipe. Unknown keys raise
``KeyError`` — a typo can never silently become a no-op.

Canonical config
================

The canonical recipe for the v22 SNIPER strategy on XAUUSD, in the
MT4/MT5 convention: 100x leverage on the 0.01-lot base, with the
**live 40c/oz spread** modelled directly (no backtest-vs-live split).

Fee model — single source of truth
==================================

There is exactly ONE fee model: the **live MT4 quote on XAUUSD**.

* round-trip spread: ``spread_per_oz_usd = 0.40``
* round-trip commission: ``commission_per_lot_usd = 7.0``

No "baseline $0.19 + overlay" split — the upstream backtest's $0.19/oz
baseline is an artifact of the backtest using tighter-than-live quotes,
and it is NOT carried here. Per-trade cost at canonical
``lots=0.01, contract_size=100`` (1.00 oz notional):

    0.01 x $7 commission + 1 x $0.40 spread = $0.07 + $0.40 = $0.47

See ``AGENTS.md`` for the fee derivation and the full recipe history.
"""
from __future__ import annotations

from typing import Any

# The two P2T statistics the cap can be built with (see the
# ``TP_P95_STATISTIC`` definition below for the measured 6.9x gap).
from bt._engine.rolling_p95 import (  # type: ignore
    STAT_P2T_WINDOW,
)

# ─── Forex canonical sizing (MT4 XAUUSD 100x leverage) ───────────
#
# Single source of truth: the live MT4 quote.

FOREX_LOTS = 0.01                        # MT4 base
FOREX_CONTRACT_SIZE = 100.0              # 100 oz / lot -> 1 oz at 0.01
FOREX_LEVERAGE = 100.0                   # 100x account leverage
FOREX_COMMISSION_PER_LOT_USD = 7.0       # round-trip, $/lot
FOREX_SPREAD_PER_OZ_USD = 0.40           # round-trip, $/oz (live)

# ─── Entry mode & fill models ───────────────────────────────────
#
# ENTRY_MODE: 'dual' = immediate + sniper co-fire. This is the ENGINE
# surface (``engine_kwargs['entry_mode']``). The old recipe's
# ``entry_mode='sniper'`` was never read by the VP engine.
ENTRY_MODE = 'dual'

# ENTRY_FILL_MODEL — how a resting entry order at the zone edge fills.
# See ``bt/_engine/vp_multi_target_config.py``.
#
#   "touch"     — fill on the first LATER bar whose range touches the
#                 limit level. Canonical (D2 fix, 2026-10-22).
#   "next_open" — fill at the next bar's open.
#   "trigger"   — pre-D2 behaviour (instant fill at the zone edge on
#                 the trigger bar). A/B only; NOT defensible.
ENTRY_FILL_MODEL = 'touch'

# FILL_MODEL — how a bar that touches BOTH the stop and the target is
# resolved, and where a gap-through fills.
#
#   "mid"        — legacy optimistic: ambiguous bar books the TARGET.
#   "pessimistic"— canonical: ambiguous bar books the STOP, and a
#                  gapped-through level fills at the bar's OPEN.
#   "next_open"  — strictest: the outcome defers to the next bar.
FILL_MODEL = 'pessimistic'

# ─── SL geometry ────────────────────────────────────────────────
#
# There are FIVE knobs that can move the stop. They are NOT
# independent — ``compute_structural_sl`` picks one offset, then picks
# which LEVEL anchors it:
#
#   1. ``sl_atr_mult``    offset = mult x ATR          (if > 0 AND atr passed)
#   2. ``sl_zone_mult``   offset = mult x zone_width   (elif > 0)
#   3. ``sl_buffer_usd``  offset = ABSOLUTE dollars    (fallback)
#
#   4. ``sl_mode``        WHICH level anchors the stop:
#                         'fvg_only'  -> zone edge only
#                         'fvg_or_poc'-> tightest of zone edge / POC / HVN
#                         'hvn_lvn'   -> Nth HVN shelf on the LOSS side
#   5. ``poc_buffer_usd`` / ``hvn_buffer_usd``
#                         offsets for the POC / HVN anchor levels
#
# In the canonical recipe the offset is EXACTLY
# ``sl_zone_mult x zone_width`` and the stop is anchored by
# ``sl_mode='hvn_lvn'`` with ``zone_edge`` (this same offset) as the
# fallback.
#
# ``sl_zone_mult`` is the FALLBACK stop for trades whose Nth shelf does
# not resolve — measured at 45.9% of legs — so it is load-bearing, not
# a leftover. Zeroing it drops the offset to ``sl_buffer_usd = $0.05``
# and gives those legs a $0.05 stop against a $0.47 round trip: a fee
# payment labelled a stop.
#
# ``sl_atr_mult`` is NOT on the sweep surface. It was doubly dead:
# canonical 0.0 AND the engine is never handed an ``atr=`` array, so
# the ATR branch is unreachable. Beyond that, a 1s-cadence ATR is
# $0.098 median, so expressing a $5 stop would need mult ~= 51 — a
# constant with no cross-regime meaning.
SL_MODE = 'hvn_lvn'
SL_ZONE_MULT = 40.0
SL_BUFFER_USD = 0.05

# Which HVN shelf behind entry anchors the stop. Deliberately SEPARATE
# from the TP's ``nth_hvn_n``: "where do I take profit?" reaches for
# the far magnet; "where is the thesis invalid?" respects the nearest
# structural shelf. Sharing one knob would drag the stop out to the
# 3rd shelf behind entry — wider by construction, the opposite of the
# intent.
SL_NTH_HVN_N = 1

# ─── Structural SL min/max (ABSOLUTE backstops) ──
#
# KEPT as the ONLY SL width guard: a stop inside the fee band is a fee
# payment labelled a loss (floor), and a runaway shelf-anchored stop
# needs a catastrophic-error backstop (ceiling). No ATR scaling — the
# ATR array belongs to the trailing stop only.
SL_MIN_USD = 0.94
SL_MAX_USD = 15.00

# 0.0 = DERIVE the SL's shelf-search radius per bar from the live caps
# (``_reachable_tp_band_usd``). It used to fall back to the engine's
# ``TP_MAX_DISTANCE_USD`` module constant (1500.0 — oversized), so the
# canonical SL searched $1500 of shelves on a $3000 instrument.
SL_HVN_MAX_DISTANCE_USD = 0.0

# HVP chain offsets.
POC_BUFFER_USD = 0.20
HVN_BUFFER_USD = 0.10

# ─── LVN / shelf-edge geometry ──────────────────────────────────
#
# ``lvn_skip_usd`` is the distance from the HVN shelf's LEADING EDGE to
# TP1 — "target the shelf but stop short of it". It was DEAD for its
# entire useful range: the engine's guard ``if hvn_tp >= lo: hvn_tp =
# lo - 1.0`` fired for every ``lvn_skip_usd < shelf_width / 2``, so
# the computed TP was unconditionally overwritten by the hardcoded
# ``$1.00`` absolute constant. The branch now measures from the shelf edge
# (the actual intent), so the knob is live across its range.
#
# Inferred XAU scale: the re-scaled VP gives a median shelf of ~$1.62.
# 0.30 is ~18% of that — a clean $0.30 shy of the edge.
LVN_SKIP_USD = 0.30
LVN_MODE = 'skip'

# ─── TP geometry (single-target HVN, multi-target RETIRED) ──
#
# NTH_HVN_N — which HVN shelf out of the ones ahead of entry in the
# trade's direction becomes the SINGLE target.
#
# It was ALWAYS wired to the engine but was INERT on XAUUSD, because
# the VP profile underneath it was built with oversized constants
# (``vp_min_zone_usd = 5.00`` over a ~$0.25 VP row meant 0.15 HVN
# shelves/day, so n=2 and n=3 resolved on 0.00% of entries).
# After the XAU VP rescale (200 rows / $0.50 floor) all three tiers
# resolve on real entries: n=1 65.0%, n=2 30.3%, n=3 11.4%.
#
# Canonical is 3 (promoted 2026-10-22). At n=1 the nearest shelf
# resolved a median $3.03 away against a $14.40 median stop — R:R
# 0.197, which needs an 84% win rate to break even. n=3 resolves a
# median $19.95 at R:R 1.272.
NTH_HVN_N = 3

# ─── Multi-target RETIRED (2026-11-03) ──────────────────────────
# PARTIAL_CLOSE_PCT / TP2_MODE / TP2_NTH_HVN_N / TP2_SWING_CAP are
# RETIRED. The engine is single-target + trailing stop; there is no
# partial close, no runner leg, no breakeven re-injection. Kept here
# ONLY so old sweep JSON / tests importing them keep importing; the
# engine ignores them and ``build_engine_kwargs`` no longer emits
# them. Do NOT sweep them.
PARTIAL_CLOSE_PCT = 0.50
TP2_MODE = 'hvn'
TP2_NTH_HVN_N = 0
TP2_SWING_CAP = True

# TP fallback leg multiplier, used when no HVN shelf resolves.
TP_ZONE_MULT_FALLBACK = 200.0

# HVN shelf SEARCH RADIUS — DERIVED, not hardcoded.
#
# DESPITE ITS NAME this is not a TP cap — it is only the SEARCH RADIUS
# passed to ``hvn_zones_near``. The actual TP ceiling is
# ``min(swing_cap, p95_cap)``, both measured live per bar.
#
# Provenance of the old value: 6.0 (inherited) -> 50.0 (gold
# scale) -> 13.0 ("XAU scale fix"). Each was a hand-picked literal
# copied from the MEASURED MEDIAN of the caps on ONE slice — a regime
# statistic frozen as config.
#
# 0.0 = DERIVE the radius per bar from the same caps that will clamp
# the leg. Pass a positive value to restore frozen behaviour for A/B.
TP_MAX_DISTANCE_USD = 0.0

# Fraction of ``TP_ZONE_MULT_FALLBACK`` used for the single target when
# no HVN shelf resolves inside the radius (the ``multi_hvn_proxy``
# branch). Was a bare ``0.7`` literal inside that arithmetic; promoted
# here so it is sweepable and its scale is stated.
TP_HVN_PROXY_FRAC = 0.7

# TP exit mode. The engine implements EXACTLY three, and it raises
# ``ValueError`` for anything else:
#
#   'hvn_single'    — ONE HVN target (the ``nth_hvn_n``-th shelf ahead
#                    of entry). No partial close, no runner leg, no
#                    breakeven re-injection. The whole position rides
#                    to the shelf. CANONICAL as of 2026-11-03.
#   'multi_target' — NB74: TP1 = HVN shelf (partial close 50%), TP2 =
#                    zone_mult (remainder 50%), breakeven re-injection.
#                    The A/B baseline.
#   'fvg_mult'     — single-target zone_mult only, no TP2. The A/B
#                    baseline for 'multi_target'.
#
# WHY 'hvn_single' IS CANONICAL: the 'multi_target' shape splits the
# position 50/50 between a partial at TP1 and a remainder at TP2, but
# on the canonical book the two legs collapse onto the SAME price (the
# swing cap) on effectively every trade -- so the "partial" is a
# partial close at the cap followed by a remainder that runs the same
# distance for half the size. That is a fee-paying shape, not an edge.
#
# ⚠️ Do NOT introduce a mode name here that the engine has not been
# taught. ``compute_structural_tp`` raises ``ValueError`` on an unknown
# mode, so a typo fails loudly — but so does a *plausible* new name,
# and it fails the WHOLE BOOK rather than one leg.
TP_MODE = 'hvn_single'

# ─── Trailing stop (CANONICAL, single-target exit) ──
#
# The multi-target partial/remainder shape is RETIRED. The exit is now:
# ONE HVN target + structural SL + trailing stop (chandelier + giveback).
#
#   peak(t)       = max excursion in the trade's direction since entry
#   chandelier(t) = peak(t) - trail_atr_mult * ATR_1m(fill)
#   giveback(t)   = entry + (peak(t) - entry) * (1 - trail_giveback_frac)
#   fires when price crosses the HIGHER (tighter) of the two, but ONLY
#   after MFE >= trail_activation_mult * ATR_1m(fill).
#   ATR basis: 1m Wilder ATR(14) forward-filled onto the 1s grid.
TRAIL_MODE = 'combined'       # 'off' | 'chandelier' | 'giveback' | 'combined'
TRAIL_ATR_MULT = 2.0          # k: trail distance in 1m ATRs
TRAIL_ACTIVATION_MULT = 1.0   # m: arm only after MFE >= m * ATR_1m
TRAIL_GIVEBACK_FRAC = 0.5     # f: exit after giving back f of peak MFE

# Which SIDES the trailing stop applies to: 'both' | 'long' | 'short'.
#
# ⚠️ This is NOT the same knob as ``TRADE_SIDES`` (the entry gate).
# ``TRADE_SIDES`` decides which trades may OPEN; ``TRAIL_SIDES`` decides
# which POSITIONS get a trailing EXIT. Both sides keep trading under
# ``TRAIL_SIDES='short'``.
#
# WHY IT EXISTS (2026-11-04). The corpus MFE/MFA analysis
# (``docs/trail_stop.md`` § 8.4) measured the trail PER SIDE on the
# 8-month corpus (2,539 legs):
#
#   side   n      baseline gross   canonical trail delta
#   long   1,685  +$1,073.50       -$906.77   (753 TPs cut)
#   short    854   -$245.05        +$533.38   (290 SLs rescued)
#
# The trail helps the side that is LOSING money and hurts the side that
# is making it. The short side's MFA p50 ($3.716) sits close to its MFE
# p50 ($4.380) -- shorts go against the trade FIRST, which is exactly
# the population a trailing stop rescues. The long side runs 8.36 ATR of
# MFE, so truncating its winners destroys more than the trail captures.
#
# CANONICAL IS 'both' — the scope is MEASURED, not assumed. The
# 'short'-scoped arm is a CANDIDATE until its end-to-end engine A/B
# exists (the replay number above is a screen, not a verdict: the trail
# changes the BOOK, so it must be re-measured through the engine).
TRAIL_SIDES = 'both'

# ─── Signal gates ───────────────────────────────────────────────
#
# vol_state_min_range_norm: the single largest measured improvement in
# the repo, and the ONLY knob whose gain is NOT "trade less".
#
# ``range_norm_1m`` = the current 1m candle's range divided by the
# median range of the trailing 60 one-minute candles. A threshold of
# 3.0 admits a signal only when the last minute is an OUTLIER of
# volatility — i.e. when the market is actually doing something,
# instead of drifting inside its own noise band.
#
# Measured on 15 monthly slices (2024-06 -> 2026-06), canonical MT4
# sizing (1.00 oz, $0.47 round trip):
#
#   thr   legs    gross       fees       net    gross/leg  g/leg/fee  mo+
#   0.0  50,205    $1,830   $23,596  -$21,358     $0.036     0.078   0/15
#   1.0  28,930    $2,955   $13,597  -$10,389     $0.102     0.217   0/15
#   1.5  12,417    $1,098    $5,836   -$4,635     $0.088     0.188   0/15
#   2.0   5,233      $242    $2,460   -$2,177     $0.046     0.098   1/15
#   2.5   2,396      $165    $1,126     -$947     $0.069     0.147   4/15
#   3.0   1,231      $205      $579     -$364     $0.166     0.354   4/15
#
# Gross-per-leg rises 4.6x while the fee bill falls 97.5% — so this
# is NOT the ``sl_zone_mult`` mechanism ("trade less, pay less fee").
#
# HONEST CAVEAT: net is still -$364 across all 15 months. The gate
# does not make the recipe profitable. It is promoted because it is a
# genuine, large, non-directional improvement in signal QUALITY that
# every later measurement should be built on top of.
#
# PAIRED with ``SL_MAX_USD`` — always re-measure the two together.
#
# ─── RE-PAIRED 2026-10-25: 3.0 -> 1.0 (TRADE-COUNT TARGET) ────────
#
# The 3.0 threshold was promoted to make the gross edge per leg
# better, and it did — but it does so by ADMITTING almost nothing.
# Measured on the continuous corpus at the canonical book size
# (1,692 legs over 927 calendar days) that is **1.83 trades per
# calendar day** — roughly one trade every 13 hours. The gate is the
# binding constraint on the whole recipe.
#
# The user target is **30-70 trades a day**. MEASURED on the
# continuous corpus (2024-01-01 -> 2026-07-16, 14-day warmup prefix,
# one continuous execution; `runtime/diagnostics/
# volgate_trade_count.py`), with the requested SL/contra settings:
#
#   thr   legs   /cal-day  /sig-day      gross        fees         net    g/leg
#   0.0  100008    107.88    126.43   $9,612.27  $34,233.51  -$24,621.24  0.0961
#   0.5   95338    102.85    120.68   $8,197.54  $32,995.30  -$24,797.76  0.0860
#  *1.0   51075     55.10     64.65   $7,312.96  $19,758.46  -$12,445.50  0.1432
#   1.25  30923     33.36     39.54   $5,284.96  $13,017.27   -$7,732.31  0.1709
#   1.5   18981     20.48     24.59   $5,267.46   $8,448.18   -$3,180.72  0.2775
#   2.0    7373      7.95      9.74   $1,647.84   $3,536.14   -$1,888.31  0.2235
#
# **1.0 is the only threshold that lands MID-BAND** — 55.10 per
# calendar day and 64.65 per trading day, both inside 30-70. 1.25
# also lands in band but at the bottom edge (33.36 / 39.54) and
# 1.5 falls out at 20.48. 0.0/0.5 overshoot at 100+.
#
# 1.0 is also the best threshold on GROSS PER LEG among the in-band
# options that clear the count target, and it is 1.49x the gross/leg
# of the gate-off book ($0.1432 vs $0.0961) — the gate still does its
# quality job, it just no longer admits only the top ~2% of bars.
#
# ⚠️ THE NET IS NEGATIVE AT EVERY THRESHOLD. Reaching 30-70
# trades/day costs a lot of gross per leg: the 1.0 book's $0.1432
# gross/leg is BELOW the $0.5029 measured round trip, so the wider
# book is a much bigger fee sink. The count target and the edge are in
# direct tension, and the count target WINS here because it was asked
# for. Read the net column before believing any per-leg edge claim.
VOL_STATE_MIN_RANGE_NORM = 1.0

# The remaining vol-state thresholds. Each ``*_min`` is a scalar
# threshold on the corresponding ``build_vol_state`` measure; 0.0 =
# gate disabled. The ``*_max`` pair is the inverse (rejection above an
# upper limit); 0.0 = no upper limit.
VOL_STATE_MIN_VEL_5M = 0.0
VOL_STATE_MIN_VEL_15M = 0.0
VOL_STATE_MIN_RV_RATIO_30S = 0.0
VOL_STATE_MAX_RV_RATIO_30S = 0.0
VOL_STATE_MAX_WICK_RATIO = 0.0

# ─── PREV-DAY VOL EXPANSION (NEW 2026-11-03, SHIPS DISABLED) ───
#
# ``vol_expand_prev_day`` = RV(today so far) / RV(previous calendar
# day at the SAME elapsed fraction of the day).
#
# This is the "vol gate expansion" in its DAY-SHIFTED form: the
# canonical gate (``VOL_STATE_MIN_RANGE_NORM``) asks "is this minute an
# outlier vs its own trailing 60 minutes"; this asks "is today
# running hotter or colder than yesterday ran at this time of day".
#
# Why it is worth having at all: volatility is autocorrelated ACROSS
# DAYS (measured on the 1y corpus: lag-1 corr of consecutive daily
# ranges **+0.461**), so the previous day's regime is a genuine
# predictor of today's. The trailing-median gate has no memory that
# long — a 60-minute baseline cannot distinguish "the whole day is
# slow" from "the whole day is fast".
#
# Why "same elapsed fraction": raw "today so far vs yesterday's full
# day" is mechanically biased low all morning (a partial day always
# has less RV than a whole one) and the bias shrinks as the day
# progresses. Comparing at the SAME fraction removes it, so 1.0 means
# "today is tracking yesterday exactly" at any hour.
#
# Causal by construction: the previous calendar day is entirely in the
# past, and the numerator is a cumulative sum from midnight over
# returns up to the current bar only.
#
# ⚠️ PROMOTED 2026-11-04 (user instruction: "always analyze based on
# EV, any EV increase we promote").
#
# The measure: ``vol_expand_prev_day`` = RV(today so far) / RV(prev
# day at the same elapsed fraction of the day). The gate admits a bar
# only when today is running HOTTER than yesterday ran at this time of
# day.
#
# CHOSEN ON EV (gross per leg), over the full continuous corpus
# (2024-01-01 -> 2026-07-16, 927 days, 14-day warmup prefix
# executed and discarded) via
# ``runtime/diagnostics/expand_prev_day_corpus_ab.py``:
#
#   thr   legs   legs/day   EV(gross/leg)  drift/leg  excess/leg  fee/leg      net
#   0.0   11305      12.20         0.1720    +0.2704     -0.0984   0.5034  -3745.96
#   0.7    9117       9.83         0.2666    +0.3512     -0.0846   0.4997  -2125.11
#   0.85   7822       8.44         0.2978    +0.3740     -0.0762   0.5000  -1581.14
#   1.0    6663       7.19         0.3877    +0.4641     -0.0764   0.4989   -741.16  <-- EV PEAK
#   1.1    5677       6.12         0.3186    +0.3866     -0.0680   0.5000  -1029.78
#   1.2    5088       5.49         0.3030    +0.3740     -0.0710   0.5033  -1019.29
#   1.3    4610       4.97         0.3059    +0.3798     -0.0739   0.5048   -916.99
#
# EV is the criterion (user instruction). The EV curve RISES to a
# peak at 1.0 and then FALLS AWAY -- 1.1/1.2/1.3 were measured
# specifically to confirm 1.0 is a peak, not a point on a rising
# slope. 1.0 beats every higher threshold (dEV +0.0691 vs 1.1,
# +0.0847 vs 1.2, +0.0818 vs 1.3) and lifts EV over gate-off by
# dEV +0.2157 (t = +2.60 -- statistically significant).
#
# The peak is a DRIFT peak, not a timing peak: drift peaks at 1.0
# (+0.4641) while excess (timing) is flat at -0.07..-0.10 across
# the whole range. The gate selects on directional drift.
#
# ⚠️ HONEST LIMITS (recorded, not hidden):
#   * The EV lift over gate-off IS statistically significant
#     (t = +2.60), but the lift over the NEAR neighbours is not:
#     1.0 vs 0.85 is t = +1.00, 1.0 vs 1.1 is t = +0.71. The
#     plateau 0.85-1.0 is not sharply separated from the peak.
#   * The book is still NEGATIVE at every threshold. The gate
#     REDUCES a loss ($-3,746 -> $-741); it does not make the book
#     profitable. EV/leg (0.3877) remains below the measured round
#     trip (0.4989), so the strategy is still fee-limited.
#   * ``excess`` (the timing component, as opposed to drift) remains
#     NEGATIVE and significant at every threshold (t = -8.45 at
#     1.0). The gross is dominated by drift -- the market going the
#     way the position already pointed.
#   * 7.19 legs/day sits FAR BELOW the 30-70/day target band, and
#     93.6% of days fall under 30 legs. The instruction to promote
#     on EV overrides the trade-count preference -- a deliberate
#     trade-off, not an oversight.
#
# The knob is LIVE and CANONICAL at 1.0. Re-measure it whenever the
# exit geometry changes (it is paired with the SL/volume gate by
# construction: it is a volume gate).
VOL_STATE_MIN_EXPAND_PREV_DAY = 1.0
# The inverse gate: reject when the day is running COOLER than
# yesterday (a "vol contraction" regime). Ships OFF -- never swept.
VOL_STATE_MAX_EXPAND_PREV_DAY = 0.0

# BoS/CHoCH structure gate. Ships OFF.
#
# It was MEASURED and NOT promoted: it improves gross but leaves the
# market-beta correlation untouched (long +0.968 -> +0.970), and
# promoting it would break every call site that builds no
# ``StructureState``. Locked by
# ``tests/test_regressions.py::test_structure_gate_stays_off_until_its_plumbing_is_safe``.
STRUCTURE_GATE = False
# 300 bars = 5 min on the 1s canonical feed.
STRUCTURE_MAX_AGE_BARS = 300

# Which sides may open. "both" = long and short; "long" = long only.
#
# NOT PROMOTED, deliberately two-sided. "long" was measured on
# 2026-10-24 (8 slices): it turns -$74 into +$161, but its market
# correlation rises to +0.464 against the two-sided recipe's aggregate
# +0.315 — a long-only account IS a directional bet, and it has no
# hedge for a gold sell-off. The short side's negative alpha is
# attacked with a per-side sequence filter instead.
TRADE_SIDES = 'both'

# ─── Per-side sequence drop set — REMOVED 2026-11-04 ─────────────
#
# The 1-event drop set (``{"short|c", "short|C"}``) is DELETED. It was
# REDUNDANT with, and in direct CONTRADICTION with, the 5-event whitelist
# below. Measured on the corpus (2024-01-01 -> 2026-07-16, one continuous
# execution, 14d warmup), 3 of the 8 whitelisted SHORT shapes end in a
# reversal ('c'/'C') and were therefore VETOED by this gate:
#
#     short|BcCcC    <- the single best short shape (+0.532 gross/leg)
#     short|CBcCc
#     short|cCcbC
#
# The two gates answer the SAME question at DIFFERENT granularity and
# disagreed: the drop set said "never short after a reversal", the
# whitelist said "short after THIS specific 5-event history is good".
# The 5-event rule is strictly more informed, and it is the one that
# survived the split-half test. Keeping both meant the whitelist's best
# short shape could never fire.
#
# END-TO-END (7-arm corpus A/B, ``seq_whitelist_corpus_ab2.py``):
#
#   arm            wl   dropset   legs  legs/day     gross        net   g/leg
#   unfiltered      0        0  37159    40.09   -$4,742  -$23,091  -0.1276
#   split          21        0   6663     7.19   +$2,583     -$741  +0.3877
#   split_canon    21        2   5664     6.11   +$2,357     -$467  +0.4161
#
# The drop set's isolated effect on the SAME 21-key whitelist:
#
#   gross  +$2,583 -> +$2,357   (ON - OFF = -$226)
#   net    -$741   -> -$467     (ON - OFF = +$274)
#   legs   6,663   -> 5,664    (-999)
#
# So the drop set costs 999 legs and $226 of GROSS to save $274 of NET.
# A NET-only win that loses GROSS is a FEE artefact, not a signal --
# exactly the failure mode AGENTS.md 5.2 distinguishes. The whitelist
# ALONE is the better book: same direction of travel, more gross, more
# legs, and the higher gross/leg.
#
# Locked by ``tests/test_regressions.py::test_per_side_sequence_drop_set_is_removed``.

# ─── PER-SIDE 5-EVENT SEQUENCE WHITELIST (2026-11-04) ──────────
#
# The user asked (2026-11-03):
#   "tabulate bos/choch 5-event sequence and the subsequent entries.
#    split between longs and shorts. then, we know which 5-seq
#    events to whitelist before each long/short"
#
# So: for every distinct trailing 5-event BoS/CHoCH signature that
# precedes an entry, tabulate the realised outcome of the entries
# that followed -- separately for longs and for shorts -- and whitelist
# the signatures worth trading.
#
# WHAT IT IS. A set of ``"<side>|<5-char signature>"`` keys. A key
# PRESENT means TAKE that trade; ABSENT means DROP it. It REPLACED the
# 1-event ``structure_seq_drop_set`` (removed 2026-11-04) -- see the
# tombstone comment above for why the two contradicted each other.
#
# ⚠️ THIS SET WAS RE-FITTED ON 2026-11-04. The first version (25 keys)
# was fitted on a 64,978-leg ledger that was built BEFORE the recipe
# changed (the trailing stop became canonical, ``tp_mode`` moved to
# ``hvn_single``, the SL ceiling moved, and the
# ``vol_state_min_expand_prev_day`` gate went live). That change moved
# the book to 37,159 legs, and a set fitted on the old book does not
# describe the new one -- only 14 of the 25 keys survived. See
# ``docs/engine_history.md`` for the cache-fingerprint bug that made
# the two books disagree.
#
# THE MEASUREMENT. ``runtime/diagnostics/seq5_table.py`` over the
# FULL corpus (2024-01-01 -> 2026-07-16, one continuous execution,
# 14d warmup prefix, 37,159 legs on the unfiltered book):
#
#   LONG  -- 13 of 64 signatures have positive gross/leg on BOTH
#   halves:
#     Ccbbb  +0.776  (+0.473, +1.035)   n=345
#     bCcbb  +0.561  (+0.584, +0.542)   n=385
#     cCcCB  +0.558  (+1.055, +0.161)   n=313
#     CcbCc  +0.529  (+0.621, +0.441)   n=384
#     cbCcC  +0.525  (+0.669, +0.363)   n=298
#     BcCcC  +0.396  (+0.342, +0.433)   n=330
#     bbCcb  +0.390  (+0.060, +0.873)   n=362
#     cCcCc  +0.386  (+0.095, +0.636)   n=318
#     bbbbC  +0.370  (+0.249, +0.481)   n=365
#     cCcbb  +0.273  (+0.113, +0.473)   n=327
#     CcCBc  +0.220  (+0.358, +0.072)   n=359
#     CcbbC  +0.154  (+0.111, +0.199)   n=356
#     CcbCB  +0.081  (+0.169, +0.011)   n=291   n=302
#
#   SHORT -- 8 of 64 signatures qualify:
#     BBcbb  +0.716  (+0.624, +0.824)   n=271
#     BcCcC  +0.532  (+0.229, +0.819)   n=378
#     cCcbb  +0.397  (+0.244, +0.539)   n=294
#     BBBcb  +0.349  (+0.372, +0.325)   n=253
#     Bcbbb  +0.325  (+0.175, +0.487)   n=283
#     CBBBB  +0.272  (+0.231, +0.315)   n=202
#     cCcbC  +0.195  (+0.155, +0.239)   n=340
#     CBcCc  +0.119  (+0.107, +0.129)   n=291
#
# THE WALK-FORWARD TEST (``seq5_walkforward.py``, 2 folds by time,
# 2000 random-whitelist permutations of the same size as the null):
#
#   LONG  fold H1->H2: d gross +$593, null p95 +$888 -> 77.0%
#         fold H2->H1: d gross +$449, null p95 +$381 -> 97.9%
#   SHORT fold H1->H2: d gross +$1,605, null p95 +$2,121 -> 56.3%
#         fold H2->H1: d gross +$1,495, null p95 +$1,550 -> 91.7%
#
# WHY THE SHORT SIDE IS THE ONE THAT EARNS THE SLOT. Its H2->H1 fold
# lands at the 91.7th percentile of the random-whitelist null -- the
# whitelist beats "trade less at random" that way round. The long
# side's H2->H1 fold is at 97.9%. The two OTHER folds (77.0% and
# 56.3%) are barely better than chance, and that is recorded here
# rather than smoothed over.
#
# THE END-TO-END A/B (``seq_whitelist_corpus_ab2.py``, 7 arms, each a
# full corpus continuous re-run -- post-hoc filtering cannot see the
# contra-close netting):
#
#   arm            wl   dropset   legs  legs/day     gross        net   g/leg
#   unfiltered      0        0  37159    40.09   -$4,742  -$23,091  -0.1276
#   old            25        0   7926     8.55   +$2,599   -$1,367  +0.3279
#   old_canon      25        2   6753     7.28   +$2,319   -$1,074  +0.3434
#   split          21        0   6663     7.19   +$2,583     -$741  +0.3877
#   split_canon    21        2   5664     6.11   +$2,357     -$467  +0.4161
#   ranked21       42        0  12655    13.65   +$3,917   -$2,395  +0.3095
#   ranked21_canon 42        2   9377    10.12   +$3,414   -$1,281  +0.3640
#
# The SHIPPED set is ``split``: the split-half candidate set at FULL
# STRENGTH, with the drop set removed. It beats the old set on gross
# per leg (+0.3877 vs +0.3279) and on net (-$741 vs -$1,367).
#
# ⚠️ THE HONEST LIMITS, RECORDED SO THEY ARE NOT FORGOTTEN:
#
#   1. THE BOOK IS STILL NET-NEGATIVE (-$741 over 927 days). The
#      whitelist's kept book has a POSITIVE gross (+$2,583) but its
#      per-leg edge (+$0.3877) is still BELOW the measured round trip
#      ($0.4989). This is a QUALITY knob, not an EDGE knob -- the same
#      verdict the ``vol_state_min_expand_prev_day`` gate got (see
#      AGENTS.md § 3.7.2).
#   2. THE TRADE COUNT IS FAR BELOW THE 30-70/DAY BAND: 7.19
#      legs/calendar-day, and 93.6% of days trade fewer than 30 legs.
#      The user explicitly waived the band for this knob ("dont need
#      to be too strict on trade count for this"). To stay inside the
#      band the whitelist would have to keep 48 of 64 signatures per
#      side -- i.e. barely filter at all (``seq5_ksweep.py``). The
#      full-strength set is the one that carries the signal.
#   3. THE WHITELIST IS FIT ON THE WHOLE CORPUS. The walk-forward
#      validates the SHAPE of the rule (fit-on-half, score-on-other),
#      but the SHIPPED SET is the whole-corpus fit. The
#      engine-level walk-forward (fit the set on H1, rebuild the H2
#      ledger with it) is the remaining test.
#   4. POST-HOC IS NOT THE BOOK. The table above is a leg filter; the
#      engine-side whitelist re-runs the book, so the conflict policy,
#      the same-direction cap and the contra-close netting are all
#      re-evaluated. The engine-side numbers are in
#      ``docs/seq5_table.md``.
#
# Locked by ``tests/test_regressions.py::test_five_event_sequence_whitelist_is_promoted``.
STRUCTURE_SEQ_WHITELIST: frozenset[str] = frozenset({
    # LONG -- 13 of 64 signatures, positive gross/leg on both halves.
    "long|Ccbbb", "long|bCcbb", "long|cCcCB", "long|CcbCc",
    "long|cbCcC", "long|BcCcC", "long|bbCcb", "long|cCcCc",
    "long|bbbbC", "long|cCcbb", "long|CcCBc", "long|CcbbC",
    "long|CcbCB",
    # SHORT -- 8 of 64 signatures, positive gross/leg on both halves.
    "short|BBcbb", "short|BcCcC", "short|cCcbb", "short|BBBcb",
    "short|Bcbbb", "short|CBBBB", "short|cCcbC", "short|CBcCc",
})

# Book concurrency cap (D3 fix, 2026-10-22).
#
# With an unbounded book the book NEVER emptied and the conflict policy
# locked the run 100% to one side. The cap is the ONLY signal-level
# gate now that the policy is 'stack'.
MAX_SAME_DIR_OPEN = 20

# Conflict policy. 'stack' lets both sides trade freely; the
# same-direction cap above is the only gate that can reject a signal.
# 'drop' remains available for A/B via build_engine_kwargs.
CONFLICT_POLICY = 'stack'

# ─── CONTRA CLOSE / HEDGE NETTING (2026-10-24, user request) ───
#
# The book can hold opposing positions. A long bought at $3,000 and a
# short sold at $3,050 are a LOCKED SPREAD: the pair's gross P&L is
# deterministic --
#
#     gross = Q * (E_short - E_long)
#
# -- independent of where price goes next. Without simultaneous
# closure the engine lets one leg's independent stop claim the loss
# (while its counterparty sits on an equal-and-opposite gain that is
# never taken) and pays TWO round trips to realise a spread that was
# already in hand.
#
# ``CONTRA_CLOSE_SPREAD_USD`` is the PRICE SPREAD between the
# weighted-average opposing entries at which the overlapping quantity
# is netted out simultaneously at market.
#
#   0.0 = OFF (the canonical default).
#
#   1.0 = PROMOTED 2026-10-25 (threshold) from the PAIR-LEVEL
#   analysis, which replaced the monthly one. The previous value was
#   6.5, chosen on 2026-10-25 from a monthly threshold sweep that
#   picked the wrong objective.
#
# -- WHY THE MONTHLY SWEEP PICKED THE WRONG VALUE -----------------
#
# The monthly sweep chose $6.5 by "basin robustness" -- a tie-break
# between thresholds the monthly view could not tell apart. That
# tie-break was made on a statistic the monthly view could not
# measure: WHETHER THE FEATURE FIRES IN A BEAR-SHAPED BOOK.
#
# At $6.5 the feature fires on ZERO bear-market pairs:
#
#     thr    events   dGross    UP dG   DOWN dG   FLAT dG
#       0        0     0.00      0.00      0.00      0.00
#    6.5       11   +98.45  +105.07      0.00     -6.63
#
# The reason is structural and was documented all along: the netting
# only fires on a POSITIVE spread (longs entered BELOW shorts), which
# is a BULL-shaped book. A bear-shaped book holds a locked LOSS, so
# there is nothing to bank. At a HIGH threshold the feature needs a
# WIDE lock to fire, and wide locks are rarer in a bear book. At
# $1.0 it fires on the 2 bear-market pairs as well:
#
#     thr    events   dGross    UP dG   DOWN dG   FLAT dG
#    1.0       20  +113.80  +116.31     +7.75   -10.26
#
# So the LOWER threshold is the LESS directional one. The monthly
# view scored $6.5 as "not firing on bear months", read that as
# "the gain is all in the UP slice", and concluded the feature was
# a beta proxy. The truth is the opposite: the HIGH threshold
# CAUSES the one-sidedness by refusing to net the small locks that
# a bear-shaped book produces.
#
# -- THE STATISTICS (pair-level, n=34) ---------------------------
#
# The unit of evaluation for this feature is the PAIR, not the
# month: the feature does not change which signals are taken, it
# changes how an overlapping pair is resolved. Measured at the
# pair level over 15 months
# (``runtime/diagnostics/contra_close_actual_vs_simul.py``,
# ``runtime/diagnostics/contra_close_stats.py``):
#
#     mean paired delta      +$3.97   (se $2.68)   t = +1.48
#     Wilcoxon signed-rank    z = +1.67           p = 0.096
#     SIGN TEST              24/34 positive      p = 0.024  <-- SIGNIFICANT
#     bootstrap 95% CI       [-$0.17, +$9.89]    P(mean<=0) = 0.033
#     corr(delta, market)    +0.19   (the MONTHLY view said +0.49)
#
# The sample is small (n=34) and right-skewed (the $78.44-spread
# pair on 2026-01 contributes $83 of the $135 total), so the
# t-test is the wrong tool and Wilcoxon/sign are the right ones. The
# SIGN TEST is the decisive one: 24 of 34 pairs improve. It survives
# dropping the single best pair (23/33, p = 0.035), so the result
# is NOT one lucky trade.
#
# Excluding the best THREE pairs the sign test is p = 0.071 and the
# Wilcoxon p = 0.347, so the honest statement is: the improvement is
# broad (24/34 pairs) but the MAGNITUDE of the aggregate is
# tail-dependent.
#
# -- WHY $1.0 SPECIFICALLY ---------------------------------------
#
#     thr   n reachable   total    mean   Wilcoxon p
#      0.5      31      +141.23   +4.556     0.063
#    1.0      29      +139.73   +4.818     0.056   <-- CHOSEN
#      2.0      24      +123.00   +5.125     0.098
#    4.5      18       +92.13   +5.118     0.396
#    6.5      14      +101.71   +7.265     0.177
#
# $1.0 is the threshold where the Wilcoxon test is closest to
# significance AND the DOWN slice is non-zero. $0.5 is
# statistically similar but fires on two extra pairs with a NEGATIVE
# combined delta (the $0-$1 spread band has a mean delta of -$0.92
# across 5 pairs), so $1.0 excludes that band. $4.5 and $6.5 are
# measurably worse on the test.
#
# -- THE RISK ARGUMENT (the feature's actual job) ---------------
#
# ``contra_close`` converts a certain small gain (the locked spread)
# in exchange for giving up a larger contingent gain. Per pair that
# is a RISK-MANAGEMENT trade, so it must be judged on risk, not on
# P&L alone. Measured
# (``runtime/diagnostics/contra_close_risk.py``, 15 months):
#
#     thr   events   gross      net    maxDD   g/leg   fee/leg
#       0        0   375.03  -39.28  1,102.20  0.4172   0.4609
#    1.0       20   488.82  +74.51  1,078.84  0.5407   0.4583
#    6.5       11   473.47  +59.16  1,079.29  0.5261   0.4603
#
# Three things follow:
#
# 1. **THE GROSS EDGE CROSSES THE ROUND TRIP.** At OFF the strategy
#    earns $0.4172 gross per leg against a $0.4609 fee -- it does
#    NOT cover its own commission. With the feature ON at $1.0 it
#    earns $0.5407 against $0.4583 -- **it does**. The feature is
#    the first thing in this repo's history to lift the gross edge
#    over the fee line, and the account goes from -$39.28 net to
#    **+$74.51** over the 15 months.
# 2. **MAX DRAWDOWN FALLS** from $1,102.20 to $1,078.84 (-$23.36),
#    at a cost of $113.80 of gross -- an exchange rate of ~$4.87 of
#    gross per $1 of drawdown removed.
# 3. **THE WORST LEG AND THE WORST MONTH ARE UNCHANGED**
#    (-$13.58 and -$81.39). The feature does not rescue the worst
#    outcome; it lifts the typical one.
#
# -- WHAT IS STILL NOT CLAIMED ------------------------------------
#
# * The month count is 4/15 net-positive both ON and OFF. The
#   feature adds dollars to months that were already winning; it
#   does not flip a losing month into a winning one. (2025-03 is
#   the exception: -$20.68 -> -$6.22, still negative.)
# * The FLAT slice is -$10.26. It is the one month-set where the
#   feature costs money (2025-05's hedged pair was headed for its
#   targets). This is the honest cost of the risk-management trade
#   and it is NOT hidden.
# * 6 of 15 months improve on gross; 9 are unchanged or slightly
#   worse.
# * The feature is still NOT the default. See ``CONTRA_CLOSE`` below.
#
# NOTE: promoting the THRESHOLD is not promoting the FEATURE.
# ``CONTRA_CLOSE`` remains False. The knob is on the sweep surface
# so the A/B is reproducible.
#
# ─── PROMOTED 2026-10-25: feature ON, threshold $5.00 ─────────────
#
# (user instruction, 2026-10-25: "contra 5USD")
#
# The threshold moved $1.0 -> $5.0 and the feature moved OFF -> ON.
# The threshold change is the measured part; the ON/OFF flip is the
# user's call.
#
# MEASURED on the continuous corpus (2024-01-01 -> 2026-07-16, 14-day
# warmup prefix, one continuous execution;
# `runtime/diagnostics/config_iso_2026_10_25.py`):
#
#   cell                       legs  /day   gross     fees      net   g/leg
#   base_canonical             1692   1.83   $510.58   $850.92  -$340.34  0.3018
#   contra_5                   1696   1.83   $582.71   $850.91  -$268.20  0.3436
#
# **The feature HELPS in isolation**: +$72.13 of gross and +$72.14 of
# net at a constant leg count (+4 legs). It is a risk-management
# feature that banks a locked spread instead of letting one leg's
# independent stop claim the loss.
#
# A $5.00 threshold is MORE CONSERVATIVE than the $1.0 that was
# promoted on 2026-10-25: it fires only on WIDE locks, so it banks
# fewer, larger spreads. The measured evidence at $1.0 is in
# ``bt/optimal_config.py``'s pair-level analysis above (24/34 pairs
# improve, sign test p = 0.024). At $5.00 the feature fires less
# often, so the risk reduction is smaller and the P&L contribution is
# smaller too — the interaction with the SL ceiling is what matters:
# with the ceiling at $8.00 the stop is 3.2x further away, so more
# opposing entries overlap and the netting has more to work with.
#
# ⚠️ THE THRESHOLD WAS NOT RE-PICKED FROM A PAIR-LEVEL ANALYSIS at
# $5.00 — it was set by instruction. The pair-level ladder at $0.5 /
# $1.0 / $2.0 / $4.5 / $6.5 is in the block above; $5.0 sits between
# the $4.5 and $6.5 rungs. Re-run
# `runtime/diagnostics/contra_close_actual_vs_simul` and
# `contra_close_stats` if the threshold is ever revisited.
CONTRA_CLOSE = True
CONTRA_CLOSE_SPREAD_USD = 5.0

# ─── TP caps ─────────────────────────────────────────────────────
#
# NB72 swing cap: ``SWING_CAP_PCT x (rolling 24h high - low)``.
#
# The window was reverted 3 days -> 24h on 2026-10-23: the 3-day cap
# p50 was $99.05 on 2026-01 vs $59.35 for 1 day. A $99 median target
# against a $0.47 round trip is not a target.
#
# SWING_CAP_PCT was retuned 0.70 -> 0.25 on 2026-10-24 (user request:
# "a $20 capture is considered difficult on current alpha, so scale
# ratio down to this"). Measured with ``sl_max_usd=2.50``, fit on 3
# slices / scored on 3 held-out, canonical MT4 sizing:
#
#   cap_pct | nth_hvn_n | TP p50 | FIT gross | FIT net | HELD gross
#     0.15  |     3     | $13.28 |   $760    | -$5,150 |   $792
#     0.20  |     3     | $16.96 |   $937    | -$4,974 |   $871
#    *0.25  |    *3     | $19.86 |   $574    | -$5,337 |  $1,046
#     0.35  |     3     | $27.02 |   $690    | -$5,221 |   $811
#     0.50  |     3     | $37.90 |  $2,728   | -$3,182 |  $1,730
#     0.70  |     3     | $50.69 |  $4,153   | -$1,751 |  $2,340
#
# 0.25 lands the median capture at $19.86 — the $20 target, to the
# cent. DO NOT read the rising gross column as "wider is better":
# gross rises with cap_pct because a WIDER target means fewer trades
# reach it, so the surviving book is the subset with room to run. NET
# is negative in every cell.
SWING_CAP_PCT = 0.25
SWING_LOOKBACK_SECS = 86_400  # 1 day

# NB91 rolling p95 cap: TP cannot exceed
# ``TP_P95_MULT x p95(rolling peak-to-trough over TP_P95_LOOKBACK_SECS)``.
#
# WHICH P2T statistic: the module documented a "rolling peak-to-trough
# over 7 days" but computed the p95 of HOURLY ranges. Measured on
# XAUUSD 2025-01:
#
#   STAT_P2T_HOURLY  med $12.09   (1h range, p95 over 168h)
#   STAT_P2T_WINDOW  med $83.34   (true 7d max(high)-min(low))
#
# 6.9x apart. A typical DAY on that slice moved $26.04, so the legacy
# cap was HALF a typical day's range — a cap that tight cannot express
# a tradeable TP.
TP_P95_LOOKBACK_SECS = 604_800     # 7 days
TP_P95_INNER_SECS = 3_600          # 1 hour sub-windows
TP_P95_Q = 95.0                    # p95 of inner P2T values

# THE p95 multiplier. There is exactly ONE of these, and it lives on
# the ENGINE kwargs (``tp_p95_mult``) — not on the cap object.
#
# Why: ``_p95_cap_usd`` in the engine computes
# ``cap_for(t) * (engine_mult / obj_mult)``, and ``cap_for`` already
# applied the object's own ``p95_mult``. The two therefore CANCEL:
#
#     obj_mult=1.0, engine_mult=1.0 -> v * 1.0 * (1.0/1.0) = v
#     obj_mult=2.0, engine_mult=1.0 -> v * 2.0 * (1.0/2.0) = v   <-- no-op
#     obj_mult=1.0, engine_mult=2.0 -> v * 1.0 * (2.0/1.0) = 2v  <-- moves
#
# So the cap object's ``p95_mult`` is a DEAD knob on this path, and
# ``build_p95_kwargs`` pins it to 1.0. ``tp_p95_mult`` is the single
# live multiplier and it is on ``_ENGINE_OVERRIDES_ALLOWED``; sweeping
# ``tp_p95_mult`` is what actually moves the cap.
TP_P95_MULT = 1.0                  # cap at 1x p95 (set 0.0 to disable)
TP_P95_STATISTIC = STAT_P2T_WINDOW

# Live VPS coldstart constant. Computed once from a long historical
# backtest via ``scripts/compute_p95_constant.py`` and baked in. Until
# the live rolling window accumulates enough bars, this is the fallback
# the engine uses.
FOREX_P95_RET_USD = 8.0

# ─── Detector ───────────────────────────────────────────────────
#
# These define the canonical zone set. Every sweep inherits them by
# default; varying one invalidates the artifact cache (the zone set
# changes).
#
# fvg_min_zone_usd MUST be 0.30. The pre-canonical driver used 0.10,
# which detects ~5,570 zones/day instead of ~388 (14x
# over-detection) and floods the open book, turning a 1-month run into
# hours. The 0.10 value was silently trading a different strategy.
DETECTOR_FVG_MIN_ZONE_USD = 0.30
DETECTOR_MAX_ZONE_AGE_BARS = 3_600     # 6h horizon
DETECTOR_FVG_DISPLACEMENT_RATIO = 0.0
DETECTOR_FVG_BODY_DEFINITION = 'body'
DETECTOR_PLAYED_OUT_MIN_EXTENSION_USD = 0.0
DETECTOR_INVALIDATION_MIN_PIERCE_USD = 0.05
DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS = 2
# NB52 c-delta body-only gates. Read from these CONSTANTS, not off a
# recipe object: the old recipe builder never set
# ``fvg_body_only_mitigation`` / ``fvg_body_only_invalidation``, so they
# silently resolved to the dataclass default ``False`` and the gate
# never ran — even though the canonical declared it ON. The effect is
# real but small (measured -11 zones on the 1-month 2025-01 slice,
# 9,684 -> 9,673).
FVG_BODY_ONLY_MITIGATION = True
FVG_BODY_ONLY_INVALIDATION = True
# Require a retest back through the original side before the flip.
DETECTOR_REQUIRE_RETEST_TO_INVERT = True
# Supersession: a new overlapping zone kills the old one.
DETECTOR_SUPERSEDE_ON_NEW = True
# Minimum zone lifetime before it can fire.
DETECTOR_MIN_LIFETIME_SECS = 3

# ATR window length (in 1s bars).
#
# DIAGNOSTIC ONLY. The ATR is the input to ``sl_atr_mult``, which is
# NOT on the sweep surface (doubly dead: canonical 0.0 AND the engine
# is never handed an ``atr=`` array). This constant exists so the ATR
# audits (``runtime/diagnostics/cap_and_atr_audit.py``,
# ``atr_len_scan.py``) quote the RECIPE's ATR window instead of
# hardcoding one — 1200 x 1s bars = 20 minutes of true range, which
# measures $0.098 median on 1s XAUUSD.
ATR_LEN = 1200

# Detector CADENCE. 60 = 1m.
#
# Every other indicator runs on 1m (structure pivots, volume profile,
# swing cap); the FVG detector was the only component still on 1s. On
# 1s tick-dominated gold a new $0.47-wide zone appears every ~250ms,
# so overlap-supersession killed 99.61% of zones before price could
# commit — 24x more zones, and a structurally EMPTY sniper path. The
# 1m detector produces 24x fewer zones with a 2.4x wider median.
#
# This MUST equal the structure cadence (STRUCTURE_RESAMPLE_SECS = 60).
DETECTOR_RESAMPLE_SECS = 60

# NB52 c-delta (strict-wick + body-only).
#
# STRICT_WICK_MIN_WICK_PRICE_PCT was 0.10 until 2026-10-22 — TEN
# PERCENT of spot. On gold at $3,000 that is a $300 minimum wick, so
# the filter rejected ~100% of zones. The borrowed 0.00025 is a
# $0.68 floor at gold $3,000, which rejects 100% of zones on
# 1s XAUUSD (measured: 9,684 zones -> 0).
#
# The filter is IMPLEMENTED and live (see
# ``bt/_engine/ict_signals.compute_dynamic_wick_floor``) so it can be
# swept, but it ships DISABLED. Do not flip it on without re-measuring
# the surviving-zone count first.
STRICT_WICK_REQUIRED = False
STRICT_WICK_MIN_WICK_PRICE_PCT = 0.00025
STRICT_WICK_RECOMPUTE_SECS = 3600

# ─── VP profile (volume profile build shape) ─────────────────────
#
# These define the canonical VOLUME PROFILE that every sweep inherits
# by default. The XAU scale fix moved rows 100->200 and min $5.00->$0.50:
# a $5 floor spans 20 rows of a $0.25 profile, so almost nothing
# qualified, and 200 rows gives a $0.13 row — ~3x finer than the
# median FVG zone.
VP_SESSION_SECS = 86_400       # daily POC bucket
VP_ROLLING_SECS = 604_800      # 7-day rolling bucket
VP_ROWS = 200                  # rows/session
VP_VA_PCT = 70.0               # value area captures 70% of session volume
VP_HVN_PEAK_PCT = 30.0         # floor as fraction of tallest row
VP_HVN_MIN_PROMINENCE_PCT = 15.0  # prominence filter (% of peak vol)
VP_HVN_ZONE_PCT = 50.0         # HVN shelf expansion (% of prominence)
VP_LVN_THRESHOLD_PCT = 25.0    # LVN row-volume threshold (% of row mean)
VP_MIN_ZONE_USD = 0.50         # min shelf width in USD

# ─── Structure build ─────────────────────────────────────────────
#
# These shape the CACHED ``StructureState``. They are NOT engine
# kwargs — they are a separate allowlist because the engine has no
# such parameters.
#
# STRUCTURE_RESAMPLE_SECS is MANDATORY, not a tuning knob. On raw 1s
# bars ``pivot_len`` is a ~19-SECOND window, so every micro-tick
# registers as a swing. 60s makes pivot_len a 9-minute pivot
# (canonical ICT granularity).
STRUCTURE_RESAMPLE_SECS = 60
STRUCTURE_PIVOT_LEN = 9


# ─── Allowlists (the sweep surface) ──────────────────────────────
#
# These are the sweep-tunable fields. Anything else MUST be the
# canonical value.
#
# There is exactly ONE layer: a builder per surface, each reading the
# constants above. There is no recipe dataclass underneath and no
# second copy of any knob.

# ─── VP build allowlist ──────────────────────────────────────────
# Knobs that change the VOLUME PROFILE shape (and therefore the
# reachable HVN shelves + the LVN map). They are SEPARATE from the
# engine kwargs: ``tp_max_distance_usd`` etc. are CONSUMERS of the
# profile, not shapers. Varying one requires the artifact cache to
# be rebuilt.
_VP_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    'vp_session_secs',
    'vp_rolling_secs',
    'vp_rows',
    'vp_va_pct',
    'vp_hvn_peak_pct',
    'vp_hvn_min_prominence_pct',
    'vp_hvn_zone_pct',
    'vp_lvn_threshold_pct',
    'vp_min_zone_usd',
})

# ─── Detector allowlist ──────────────────────────────────────────
# Knobs that flow into :func:`bt._engine.ict_signals.detect_fvg`.
# Any change here invalidates the artifact pickle (zones change).
_DETECTOR_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    # Zone shape / sizing
    'fvg_min_zone_usd',
    'max_zone_age_bars',
    # Detection cadence: 60 = 1m, 180 = 3m, 300 = 5m. Maps 1:1 onto
    # ``detect_fvg``'s own signature argument.
    'resample_to_n_secs',
    # Displacement quality + body definition
    'fvg_displacement_ratio',
    'fvg_body_definition',
    # NB52 c-delta (body-only semantics). The detector accepts these
    # WITHOUT the ``fvg_`` prefix.
    'body_only_mitigation',
    'body_only_invalidation',
    # Played-out + invalidation depth
    'played_out_min_extension_usd',
    'invalidation_min_pierce_usd',
    'invalidation_min_consecutive_bars',
    # Zone lifecycle
    'require_retest_to_invert',
    'supersede_on_new',
    'fvg_min_lifetime_secs',
    # NB52 strict-wick master switch. The per-bar floor array is
    # always derived from ``STRICT_WICK_MIN_WICK_PRICE_PCT`` and the
    # bars — sweeping the boolean is the sweepable surface.
    'strict_wick_required',
})

_ENGINE_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    # NB83 scale-invariant structural SL
    'sl_mode',
    'sl_zone_mult',
    'sl_buffer_usd',
    # SL_MODE_HVN_LVN anchor. ``sl_nth_hvn_n`` is
    # separate from the TP's ``nth_hvn_n`` on purpose.
    'sl_nth_hvn_n',
    'sl_min_usd',
    'sl_max_usd',
    'sl_hvn_max_distance_usd',
    # Trailing stop (canonical single-target exit).
    'trail_mode',
    'trail_atr_mult',
    'trail_activation_mult',
    'trail_giveback_frac',
    'trail_sides',
    # 1m Wilder ATR(14) forward-filled onto the 1s grid. Built by the
    # sweep driver, passed per bar to the engine.
    'atr_1m',
    # HVP chain
    'poc_buffer_usd',
    'hvn_buffer_usd',
    'lvn_skip_usd',
    'lvn_mode',
    # TP single-target (HVN). Multi-target (partial_close_pct / TP2)
    # is RETIRED — the engine ignores those keys.
    'tp_mode',
    'tp_zone_mult_fallback',
    'tp_max_distance_usd',
    # No-HVN-shelf single-target fallback fraction (was a bare 0.7
    # literal inside compute_structural_tp).
    'tp_hvn_proxy_frac',
    # NB91 p95 cap: the caller builds a RollingP95Cap object and
    # passes it as ``p95_cap``. The cap object carries the
    # lookback/inner_secs/p95_q/p95_mult internally.
    'p95_cap',
    # Single-target shelf selector. (Retired multi-target keys
    # ``partial_close_pct`` / ``tp2_mode`` / ``tp2_nth_hvn_n`` /
    # ``tp2_swing_cap`` are deliberately ABSENT: passing them raises
    # KeyError instead of silently measuring nothing.)
    'nth_hvn_n',
    # BoS/CHoCH directional gate. The engine was completely blind to
    # market structure before 2026-10-23.
    'structure',
    'structure_gate',
    'structure_max_age_bars',
    # The two-stage SCORE filter (stage 1 admission + stage 2 SL/TP
    # scaling). The driver builds the objects.
    'vol_state',
    'structure_score_lookback',
    'structure_score_half_life',
    'structure_score_min_abs',
    # SEQUENCE-AWARE DIRECTION FLIP. Maps a trailing BoS/CHoCH
    # signature to a direction multiplier; None disables it.
    'structure_seq_flip_map',
    'structure_seq_len',
    # PER-SIDE 5-EVENT SEQUENCE WHITELIST (promoted 2026-11-04). A
    # set of "<side>|<5-char signature>" keys; a key PRESENT means
    # "TAKE that trade". It REPLACED the 1-event drop set, which was
    # removed on 2026-11-04 as redundant and self-contradictory.
    'structure_seq_whitelist',
    'sl_scale_min',
    'sl_scale_max',
    'tp_scale_min',
    'tp_scale_max',
    # NB72 swing cap (object; the caller constructs a DailySwingCap
    # from bars via ``build_daily_swing``).
    'swing_cap',
    # NB85 vol-state gate (engine kwarg). Each is a scalar threshold
    # on the corresponding ``build_vol_state`` measure; 0.0 = disabled
    # for the ``*_min`` family, "no upper limit" for the ``*_max`` pair.
    'vol_state_min_rv_ratio_30s',
    'vol_state_min_range_norm',
    'vol_state_min_vel_5m',
    'vol_state_min_vel_15m',
    'vol_state_min_expand_prev_day',
    'vol_state_max_rv_ratio_30s',
    'vol_state_max_wick_ratio',
    'vol_state_max_expand_prev_day',
    # NB89 concurrency cap.
    'max_same_dir_open',
    # NB86 conflict policy.
    'conflict_policy',
    # SIDE GATE: "both" (canonical) or "long".
    'trade_sides',
    # D2: entry fill model — the entry is a resting limit order.
    'entry_fill_model',
    # Pessimistic exit fill model.
    'fill_model',
    # NB91 p95 cap MULTIPLIER. This is the knob that turns the cap off
    # (0.0) or loosens it (>1.0).
    'tp_p95_mult',
    # NB73 entry mode (engine surface): 'sniper' | 'dual'.
    'entry_mode',
    # NB74 tp_mode (engine surface): 'multi_target' | 'fvg_mult'.
    'tp_mode',
    # NB80 taker fee bps (entry+exit bps, engine reads directly).
    # This is the ONLY fee knob the engine consumes — the forex
    # commission/spread components are folded in at build time.
    'taker_fee_bps',
    # CONTRA CLOSE / HEDGE NETTING (2026-10-24). The book can hold
    # opposing positions, and those legs are a LOCKED SPREAD. These
    # two knobs net the overlapping quantity out SIMULTANEOUSLY at
    # market once the price spread between the weighted-average
    # opposing entries exceeds the threshold — banking the spread
    # instead of letting one leg's independent stop claim the loss.
    'contra_close',
    'contra_close_spread_usd',
})

# ─── Structure BUILD parameters (NOT engine kwargs) ─────────────
#
# These shape the CACHED ``StructureState``, so they route to
# ``ArtifactCache`` and enter the cache fingerprint. They are
# deliberately a SEPARATE allowlist: the guard
# ``tests/test_regressions.py::test_engine_accepts_every_allowlisted_knob``
# asserts everything in ``_ENGINE_OVERRIDES_ALLOWED`` is a real
# parameter of ``run_vp_sniper_backtest`` — and these are not. Filing
# them under the engine allowlist would let a sweep cell accept the
# override and hand it to an engine that has no such parameter.
#
# ``structure_pivot_len`` is in MINUTES (bars of the resampled chart),
# not seconds.
_STRUCTURE_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    'structure_pivot_len',
    'structure_resample_secs',
})

# ─── Swing-cap BUILD parameters (NOT engine kwargs) ─────────────
#
# The swing cap is a CACHED OBJECT built from the bars
# (``bt/_engine/swing_cap.build_daily_swing``). The engine receives
# the finished object as ``swing_cap=``; it has no ``swing_cap_pct``
# parameter of its own.
_SWING_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    'swing_cap_pct',
    'swing_lookback_secs',
})

# ─── P95-cap BUILD parameters (NOT engine kwargs) ───────────────
#
# Same shape as the swing cap: the engine receives the finished
# ``RollingP95Cap`` object as ``p95_cap=``. ``seed_path`` /
# ``seed_constant_usd`` are the LIVE-VPS coldstart knobs and are
# irrelevant in the backtest (``seed_constant_usd=0.0`` = use the
# real data).
#
# NOTE there is deliberately NO ``p95_mult`` here — the cap object's
# own multiplier is cancelled out by the engine (see ``TP_P95_MULT``).
# The live multiplier is the engine kwarg ``tp_p95_mult``.
_P95_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    'tp_p95_lookback_secs',
    'tp_p95_inner_secs',
    'tp_p95_q',
    'tp_p95_statistic',
    'seed_path',
    'seed_constant_usd',
})

# ─── P95-cap LIVE-VPS BUILD parameters ───────────────────────────
#
# The live VPS boots with NO history, so the cap must fall back to
# the baked-in ``FOREX_P95_RET_USD`` until the rolling window fills.
# This is a SEPARATE allowlist from the backtest one so the backtest
# path can never pick up the live seed constant and silently change
# the measured numbers.
_LIVE_P95_OVERRIDES_ALLOWED: frozenset[str] = frozenset({
    'tp_p95_lookback_secs',
    'tp_p95_inner_secs',
    'tp_p95_q',
    'tp_p95_statistic',
    'seed_path',
    'seed_constant_usd',
    'max_inner_bars',
})


# ─── Canonical knob name -> target parameter name ───────────────
#
# Four of the six builders materialise a kwargs dict for a target
# function whose parameter names DIFFER from the canonical knob
# names. These maps are the SINGLE translation point — they live
# here, next to the constants, and nowhere else.
#
# Before this existed, the builders did ``base.update(overrides)``,
# which APPENDED the canonical-named key instead of replacing the
# target-named one. The result was a kwargs dict carrying BOTH
# names, so:
#
#     build_swing_kwargs(swing_cap_pct=0.5)
#     -> {'cap_pct': 0.25, 'lookback_secs': 86400, 'swing_cap_pct': 0.5}
#
# The sweep then splatted that into ``build_daily_swing`` and got
# ``TypeError: got an unexpected keyword argument 'swing_cap_pct'``
# — or, worse, was silently overridden by the caller's own copy of
# the mapping. The override was **measured as nothing**.
#
# Same bug for p95 and structure. Guarded by
# ``tests/test_regressions.py::test_canonical_knob_names_reach_the_target_params``.
_SWING_KNOB_TO_PARAM: dict[str, str] = {
    'swing_cap_pct': 'cap_pct',
    'swing_lookback_secs': 'lookback_secs',
}

_P95_KNOB_TO_PARAM: dict[str, str] = {
    'tp_p95_lookback_secs': 'lookback_secs',
    'tp_p95_inner_secs': 'inner_secs',
    'tp_p95_q': 'p95_q',
    'tp_p95_statistic': 'statistic',
    'seed_path': 'seed_path',
    'seed_constant_usd': 'seed_constant_usd',
}

# The LIVE builder adds the ring-buffer size. Same canonical knob
# names as the backtest builder — the extra key is the only delta.
_LIVE_P95_KNOB_TO_PARAM: dict[str, str] = {
    **_P95_KNOB_TO_PARAM,
    'max_inner_bars': 'max_inner_bars',
}

_STRUCTURE_KNOB_TO_PARAM: dict[str, str] = {
    'structure_pivot_len': 'pivot_len',
    'structure_resample_secs': 'resample_to_n_secs',
}


def _translate(
    overrides: dict[str, Any],
    knob_to_param: dict[str, str],
) -> dict[str, Any]:
    """Rename canonical knob names onto the target's parameter names.

    Raises ``KeyError`` if an override key has no mapping — an
    unmapped key would otherwise be splatted into the target
    function as an unknown keyword argument.
    """
    unmapped = sorted(set(overrides) - set(knob_to_param))
    if unmapped:
        raise KeyError(
            f'no target-parameter mapping for {unmapped}; '
            f'known mappings: {sorted(knob_to_param)}'
        )
    return {knob_to_param[k]: v for k, v in overrides.items()}


def _derive_taker_fee_bps(
    *,
    spot: float,
    commission_per_lot_usd: float,
    spread_per_oz_usd: float,
    lots: float,
    contract_size: float,
) -> float:
    """Convert the LIVE MT4 fee knob to the engine's ``taker_fee_bps``.

    The engine's exit paths all compute the fee as::

        fee = |exit_price + entry_price| * 0.5 * qty * (bps / 10000) * 2.0
            = avg_price * qty * (bps / 10000) * 2.0

    The trailing ``* 2.0`` is the **entry + exit** factor: the engine
    debits the FULL round-trip cost at the moment the trade closes.
    So ``bps`` must be a **per-side** rate.

    The live MT4 quote gives a **round-trip** cost::

        round_trip = lots * commission_per_lot_usd + oz * spread_per_oz_usd

    Therefore::

        bps = round_trip * 10000 / (2 * qty * avg_price)

    D1 FIX (2026-10-22): the previous implementation omitted the
    ``2``, i.e. it passed a ROUND-TRIP dollar amount into a PER-SIDE
    rate slot. The engine then charged the round trip twice::

        spec round-trip $0.47 -> engine charged $0.94 per full-size trade

    Measured on the 1-month 2025-01 canonical slice: fee/trade mean
    $0.80 (max $0.98) against a spec of $0.47. The regression test
    ``tests/test_regressions.py::test_round_trip_fee_is_not_double_charged``
    locks this down.

    Returns 0.0 if the inputs are degenerate.
    """
    oz = lots * contract_size
    if oz <= 0 or spot <= 0:
        return 0.0
    rt = lots * commission_per_lot_usd + oz * spread_per_oz_usd
    # Engine formula: fee = |avg_price| x 0.5 x qty x (bps/10000) x 2
    #                 = qty x avg_price x bps/10000  (ROUND TRIP)
    # So: bps = rt x 10000 / (2 x qty x avg_price)
    #     ^^ the 2 is the entry+exit factor already baked into the
    #        engine's fee expression. Omitting it charges 2x the quote.
    return rt * 10000.0 / (2.0 * oz * spot)


def build_engine_kwargs(
    *,
    spot: float = 3000.0,
    **overrides: Any,
) -> dict[str, Any]:
    """Materialize the engine kwargs for ``run_vp_sniper_backtest``.

    This is THE way to build engine kwargs. It reads the canonical
    constants in this module directly — there is no recipe object
    underneath and no second copy of any value.

    Parameters
    ----------
    spot : float
        XAUUSD price used to convert the flat ``$`` fees into the
        engine's ``taker_fee_bps`` RATE.

        This matters: ``taker_fee_bps`` is a *rate*, not a dollar
        amount, so the realised cost is ``qty x spot x bps/10000``.
        Holding spot at the ``3000.0`` default while the corpus
        actually trades at $4,060 (the 1y file) charges
        ``0.47 x 3000/4060 = $0.347`` per trade — a 26% under-charge
        that flatters every sweep. **Pass the real spot from the data.**

    **overrides
        Sweep-tunable engine knobs (see ``_ENGINE_OVERRIDES_ALLOWED``).
        An unknown key raises ``KeyError`` — a typo can never silently
        become a no-op.

    Returns
    -------
    dict
        Mapping ready to splat into ``run_vp_sniper_backtest``.
    """
    illegal = set(overrides) - _ENGINE_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for engine_kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_ENGINE_OVERRIDES_ALLOWED)}'
        )

    base: dict[str, Any] = dict(
        # ── SL geometry ──
        sl_mode=SL_MODE,
        sl_zone_mult=SL_ZONE_MULT,
        sl_buffer_usd=SL_BUFFER_USD,
        sl_nth_hvn_n=SL_NTH_HVN_N,
        # Absolute backstops (degenerate-ATR only).
        sl_min_usd=SL_MIN_USD,
        sl_max_usd=SL_MAX_USD,
        sl_hvn_max_distance_usd=SL_HVN_MAX_DISTANCE_USD,
        # ── Trailing stop (canonical single-target exit) ──
        trail_mode=TRAIL_MODE,
        trail_atr_mult=TRAIL_ATR_MULT,
        trail_activation_mult=TRAIL_ACTIVATION_MULT,
        trail_giveback_frac=TRAIL_GIVEBACK_FRAC,
        trail_sides=TRAIL_SIDES,
        # ── HVP chain ──
        poc_buffer_usd=POC_BUFFER_USD,
        hvn_buffer_usd=HVN_BUFFER_USD,
        lvn_skip_usd=LVN_SKIP_USD,
        lvn_mode=LVN_MODE,
        # ── TP geometry (single-target HVN) ──
        tp_mode=TP_MODE,
        tp_zone_mult_fallback=TP_ZONE_MULT_FALLBACK,
        tp_max_distance_usd=TP_MAX_DISTANCE_USD,
        # No-HVN-shelf fallback fraction for the single target.
        tp_hvn_proxy_frac=TP_HVN_PROXY_FRAC,
        # Single-target shelf selector (multi-target retired: no
        # partial_close_pct / TP2 keys emitted).
        nth_hvn_n=NTH_HVN_N,
        # ── Gates ──
        structure_gate=STRUCTURE_GATE,
        structure_max_age_bars=STRUCTURE_MAX_AGE_BARS,
        # Each vol_state *_min threshold is independent; 0.0 = disabled.
        # The *_max pair rejects when the measure EXCEEDS the threshold.
        vol_state_min_vel_5m=VOL_STATE_MIN_VEL_5M,
        vol_state_min_vel_15m=VOL_STATE_MIN_VEL_15M,
        vol_state_min_rv_ratio_30s=VOL_STATE_MIN_RV_RATIO_30S,
        vol_state_min_range_norm=VOL_STATE_MIN_RANGE_NORM,
        vol_state_min_expand_prev_day=VOL_STATE_MIN_EXPAND_PREV_DAY,
        vol_state_max_rv_ratio_30s=VOL_STATE_MAX_RV_RATIO_30S,
        vol_state_max_wick_ratio=VOL_STATE_MAX_WICK_RATIO,
        vol_state_max_expand_prev_day=VOL_STATE_MAX_EXPAND_PREV_DAY,
        # ── Book ──
        max_same_dir_open=MAX_SAME_DIR_OPEN,
        conflict_policy=CONFLICT_POLICY,
        trade_sides=TRADE_SIDES,
        # PER-SIDE 5-EVENT SEQUENCE WHITELIST (2026-11-04): TAKE a
        # trade only if its (side, trailing-5-event signature) is in
        # the whitelist. It REPLACED the 1-event ``structure_seq_
        # drop_set``, which was removed on 2026-11-04: the two gates
        # contradicted each other (the drop set vetoed 3 of the 8
        # whitelisted short shapes, including the best one) and the
        # whitelist ALONE measured better. See the tombstone comment
        # above and ``STRUCTURE_SEQ_WHITELIST``.
        structure_seq_whitelist=STRUCTURE_SEQ_WHITELIST,
        # CONTRA CLOSE / HEDGE NETTING (2026-10-24). OFF by default;
        # see CONTRA_CLOSE / CONTRA_CLOSE_SPREAD_USD above.
        contra_close=CONTRA_CLOSE,
        contra_close_spread_usd=CONTRA_CLOSE_SPREAD_USD,
        # ── Execution realism ──
        entry_fill_model=ENTRY_FILL_MODEL,
        fill_model=FILL_MODEL,
        # NB91 p95 cap multiplier (None = use the cap object's own).
        tp_p95_mult=TP_P95_MULT,
        # NB73 entry mode. This is the ENGINE surface — the engine
        # reads it directly, not off a params dataclass.
        entry_mode=ENTRY_MODE,
        # Derived taker fee (entry+exit bps) from the LIVE fees.
        # The engine reads ONLY taker_fee_bps — the commission/spread
        # components are folded in here at build time, so they are not
        # emitted as engine kwargs.
        taker_fee_bps=_derive_taker_fee_bps(
            spot=spot,
            commission_per_lot_usd=FOREX_COMMISSION_PER_LOT_USD,
            spread_per_oz_usd=FOREX_SPREAD_PER_OZ_USD,
            lots=FOREX_LOTS,
            contract_size=FOREX_CONTRACT_SIZE,
        ),
    )
    base.update(overrides)
    return base


def build_vp_kwargs(**overrides: Any) -> dict[str, Any]:
    """Materialize the VOLUME-PROFILE build kwargs.

    The VP profile is rebuilt per file (expensive; ~45s on 1y XAUUSD),
    and its fingerprint changes with every override — so varying a VP
    knob forces an artifact-cache miss. That is intentional: the
    profile's geometry is what makes ``nth_hvn_n`` /
    ``tp_max_distance_usd`` even reachable.

    Parameters
    ----------
    **overrides
        Keys must live in :data:`_VP_OVERRIDES_ALLOWED`. Keys outside
        the allowlist raise ``KeyError``.

    Returns
    -------
    dict
        Mapping ready to splat into
        :func:`bt._engine.volume_profile.build_volume_profiles`
        and into the VP cache fingerprint.
    """
    illegal = set(overrides) - _VP_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for VP kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_VP_OVERRIDES_ALLOWED)}'
        )
    base: dict[str, Any] = dict(
        vp_session_secs=VP_SESSION_SECS,
        vp_rolling_secs=VP_ROLLING_SECS,
        vp_rows=VP_ROWS,
        vp_va_pct=VP_VA_PCT,
        vp_hvn_peak_pct=VP_HVN_PEAK_PCT,
        vp_hvn_min_prominence_pct=VP_HVN_MIN_PROMINENCE_PCT,
        vp_hvn_zone_pct=VP_HVN_ZONE_PCT,
        vp_lvn_threshold_pct=VP_LVN_THRESHOLD_PCT,
        vp_min_zone_usd=VP_MIN_ZONE_USD,
    )
    base.update(overrides)
    return base


def build_swing_kwargs(**overrides: Any) -> dict[str, Any]:
    """Materialize the ``build_daily_swing`` kwargs.

    The swing cap (NB72) is a CACHED OBJECT built from the bars, so
    the engine receives the finished object as ``swing_cap=`` — it has
    no ``swing_cap_pct`` parameter of its own.

    The allowlist uses the CANONICAL knob names
    (``swing_cap_pct`` / ``swing_lookback_secs``); this builder is the
    single translation point onto the target's parameter names
    (``cap_pct`` / ``lookback_secs``).

    Parameters
    ----------
    **overrides
        Keys must live in :data:`_SWING_OVERRIDES_ALLOWED`. Keys
        outside the allowlist raise ``KeyError``.

    Returns
    -------
    dict
        Mapping ready to splat into
        :func:`bt._engine.swing_cap.build_daily_swing`.
    """
    illegal = set(overrides) - _SWING_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for swing kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_SWING_OVERRIDES_ALLOWED)}'
        )
    base: dict[str, Any] = dict(
        cap_pct=SWING_CAP_PCT,
        lookback_secs=SWING_LOOKBACK_SECS,
    )
    base.update(_translate(overrides, _SWING_KNOB_TO_PARAM))
    return base


def build_p95_kwargs(**overrides: Any) -> dict[str, Any]:
    """Materialize the ``build_rolling_p95_from_bars`` kwargs.

    Same shape as :func:`build_swing_kwargs` — the p95 cap (NB91) is
    a CACHED OBJECT and the engine takes it as ``p95_cap=``.

    ``seed_path`` / ``seed_constant_usd`` are the LIVE-VPS coldstart
    knobs. In the backtest ``seed_constant_usd=0.0`` means "use the
    real data" (there IS history).

    NOTE there is deliberately NO ``p95_mult`` on the allowlist. The
    cap object's own multiplier is a DEAD knob on this path — the
    engine divides it straight back out (see ``TP_P95_MULT`` above).
    The single live multiplier is the engine kwarg ``tp_p95_mult``,
    which is on ``_ENGINE_OVERRIDES_ALLOWED``.

    Parameters
    ----------
    **overrides
        Keys must live in :data:`_P95_OVERRIDES_ALLOWED`. Keys
        outside the allowlist raise ``KeyError``.

    Returns
    -------
    dict
        Mapping ready to splat into
        :func:`bt._engine.rolling_p95.build_rolling_p95_from_bars`.
    """
    illegal = set(overrides) - _P95_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for p95 kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_P95_OVERRIDES_ALLOWED)}'
        )
    base: dict[str, Any] = dict(
        lookback_secs=TP_P95_LOOKBACK_SECS,
        inner_secs=TP_P95_INNER_SECS,
        p95_q=TP_P95_Q,
        # Neutral scale on the cap object. The LIVE multiplier is the
        # engine kwarg ``tp_p95_mult``.
        p95_mult=1.0,
        statistic=TP_P95_STATISTIC,
        # Backtest: there IS history, so the seed constant is 0.0
        # ("use the real data"). The live VPS path uses
        # ``seed_path='constant'`` with ``FOREX_P95_RET_USD`` until
        # the rolling window fills.
        seed_path="constant",
        seed_constant_usd=0.0,
    )
    base.update(_translate(overrides, _P95_KNOB_TO_PARAM))
    return base


def build_live_p95_kwargs(**overrides: Any) -> dict[str, Any]:
    """Materialize the LIVE-VPS ``make_live_rolling_p95`` kwargs.

    The BACKTEST path (:func:`build_p95_kwargs`) pins
    ``seed_constant_usd=0.0`` because the corpus already carries the
    history the cap needs. The LIVE path is the opposite: a fresh VPS
    has no history, so the cap must fall back to the baked-in
    constant :data:`FOREX_P95_RET_USD` until the rolling window
    fills.

    This is the ONLY surface that reads :data:`FOREX_P95_RET_USD`.
    Keeping it on its own builder (rather than a flag on
    :func:`build_p95_kwargs`) means the backtest path can never
    accidentally pick up the live seed constant and silently change
    the measured numbers.

    Parameters
    ----------
    **overrides
        Keys must live in :data:`_LIVE_P95_OVERRIDES_ALLOWED`. Keys
        outside the allowlist raise ``KeyError``.

    Returns
    -------
    dict
        Mapping ready to splat into
        :func:`bt._engine.rolling_p95.make_live_rolling_p95`.
    """
    illegal = set(overrides) - _LIVE_P95_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for live p95 kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_LIVE_P95_OVERRIDES_ALLOWED)}'
        )
    base: dict[str, Any] = dict(
        lookback_secs=TP_P95_LOOKBACK_SECS,
        inner_secs=TP_P95_INNER_SECS,
        p95_q=TP_P95_Q,
        # Neutral scale on the cap object — same reason as the
        # backtest builder. The live multiplier is still the engine
        # kwarg ``tp_p95_mult``.
        p95_mult=1.0,
        statistic=TP_P95_STATISTIC,
        # LIVE coldstart: the baked-in fallback until the rolling
        # window has enough bars.
        seed_path="constant",
        seed_constant_usd=FOREX_P95_RET_USD,
        max_inner_bars=4_096,
    )
    base.update(_translate(overrides, _LIVE_P95_KNOB_TO_PARAM))
    return base


def build_structure_kwargs(**overrides: Any) -> dict[str, Any]:
    """Materialize the ``detect_market_structure`` kwargs.

    The structure state (NB BoS/CHoCH) is a CACHED OBJECT built from
    the bars, so the engine receives the finished ``StructureState``.

    Parameters
    ----------
    **overrides
        Keys must live in :data:`_STRUCTURE_OVERRIDES_ALLOWED`. Keys
        outside the allowlist raise ``KeyError``.

    Returns
    -------
    dict
        Mapping with ``pivot_len`` and ``resample_to_n_secs``.
    """
    illegal = set(overrides) - _STRUCTURE_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for structure kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_STRUCTURE_OVERRIDES_ALLOWED)}'
        )
    base: dict[str, Any] = dict(
        pivot_len=STRUCTURE_PIVOT_LEN,
        resample_to_n_secs=STRUCTURE_RESAMPLE_SECS,
    )
    base.update(_translate(overrides, _STRUCTURE_KNOB_TO_PARAM))
    return base


def build_detector_kwargs(**overrides: Any) -> dict[str, Any]:
    """Materialize the ``detect_fvg`` kwargs from canonical + overrides.

    The detector is what actually emits the zone set. A change in any
    of these knobs invalidates the artifact pickle (zones change). The
    fingerprint side of that is handled by ``ArtifactCache`` in
    ``runtime/sweeps/cached_sweep.py``.

    Parameters
    ----------
    **overrides
        Keys must live in :data:`_DETECTOR_OVERRIDES_ALLOWED`. Keys
        outside the allowlist raise ``KeyError``.

    Returns
    -------
    dict
        Mapping ready to splat into
        :func:`bt._engine.ict_signals.detect_fvg`. The strict-wick
        sentinel keys (``_strict_wick_use_floor_array`` /
        ``_strict_wick_price_pct``) are appended by
        ``ArtifactCache.build_or_load`` — the floor pct is a regime
        constant, not per-cell.
    """
    illegal = set(overrides) - _DETECTOR_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(
            f'illegal override keys for detector kwargs: '
            f'{sorted(illegal)}. Allowed: '
            f'{sorted(_DETECTOR_OVERRIDES_ALLOWED)}'
        )
    base: dict[str, Any] = dict(
        fvg_min_zone_usd=DETECTOR_FVG_MIN_ZONE_USD,
        max_zone_age_bars=DETECTOR_MAX_ZONE_AGE_BARS,
        # Detection cadence: 60 = 1m, 180 = 3m, 300 = 5m.
        #
        # The zone detector MUST run on the same timeframe as every
        # other indicator. The structure runs on 1m pivots
        # (``STRUCTURE_RESAMPLE_SECS = 60``) and the volume profile is
        # built from resampled 1m bars, so a 1s FVG detector was the
        # only component still on 1s -- it saw 60x more bars and
        # therefore 60x more (mostly noise) zones than the rest of the
        # system reasons about. This was NOT wired before 2026-10-23:
        # ``resample_to_n_secs`` was absent from the detector kwargs,
        # so ``detect_fvg`` silently defaulted to 0 (= 1s).
        resample_to_n_secs=DETECTOR_RESAMPLE_SECS,
        fvg_displacement_ratio=DETECTOR_FVG_DISPLACEMENT_RATIO,
        fvg_body_definition=DETECTOR_FVG_BODY_DEFINITION,
        # NB52 body-only gates. The detector accepts these WITHOUT the
        # ``fvg_`` prefix (see ``bt._engine.ict_signals.detect_fvg``).
        body_only_mitigation=FVG_BODY_ONLY_MITIGATION,
        body_only_invalidation=FVG_BODY_ONLY_INVALIDATION,
        played_out_min_extension_usd=DETECTOR_PLAYED_OUT_MIN_EXTENSION_USD,
        invalidation_min_pierce_usd=DETECTOR_INVALIDATION_MIN_PIERCE_USD,
        invalidation_min_consecutive_bars=DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS,
        # Zone lifecycle.
        require_retest_to_invert=DETECTOR_REQUIRE_RETEST_TO_INVERT,
        supersede_on_new=DETECTOR_SUPERSEDE_ON_NEW,
        fvg_min_lifetime_secs=DETECTOR_MIN_LIFETIME_SECS,
        strict_wick_required=STRICT_WICK_REQUIRED,
    )
    base.update(overrides)
    return base


__all__ = [
    # Constructors
    'build_engine_kwargs',
    'build_vp_kwargs',
    'build_swing_kwargs',
    'build_p95_kwargs',
    'build_live_p95_kwargs',
    'build_structure_kwargs',
    'build_detector_kwargs',
    # Sizing / fees
    'FOREX_LOTS',
    'FOREX_CONTRACT_SIZE',
    'FOREX_COMMISSION_PER_LOT_USD',
    'FOREX_SPREAD_PER_OZ_USD',
    # Entry / fill
    'ENTRY_MODE',
    'ENTRY_FILL_MODEL',
    'FILL_MODEL',
    # SL
    'SL_MODE',
    'SL_ZONE_MULT',
    'SL_BUFFER_USD',
    'SL_NTH_HVN_N',
    'SL_MIN_USD',
    'SL_MAX_USD',
    'SL_HVN_MAX_DISTANCE_USD',
    'POC_BUFFER_USD',
    'HVN_BUFFER_USD',
    'LVN_SKIP_USD',
    'LVN_MODE',
    # TP
    'TP_MODE',
    'NTH_HVN_N',
    'PARTIAL_CLOSE_PCT',
    'TP2_MODE',
    'TP2_NTH_HVN_N',
    'TP2_SWING_CAP',
    'TP_ZONE_MULT_FALLBACK',
    'TP_MAX_DISTANCE_USD',
    'TP_HVN_PROXY_FRAC',
    # Gates
    'VOL_STATE_MIN_RANGE_NORM',
    'VOL_STATE_MIN_VEL_5M',
    'VOL_STATE_MIN_VEL_15M',
    'VOL_STATE_MIN_RV_RATIO_30S',
    'VOL_STATE_MAX_RV_RATIO_30S',
    'VOL_STATE_MAX_WICK_RATIO',
    'VOL_STATE_MIN_EXPAND_PREV_DAY',
    'VOL_STATE_MAX_EXPAND_PREV_DAY',
    'STRUCTURE_GATE',
    'STRUCTURE_MAX_AGE_BARS',
    'STRUCTURE_SEQ_WHITELIST',
    'TRADE_SIDES',
    'MAX_SAME_DIR_OPEN',
    'CONFLICT_POLICY',
    # Caps
    'SWING_CAP_PCT',
    'SWING_LOOKBACK_SECS',
    'TP_P95_LOOKBACK_SECS',
    'TP_P95_INNER_SECS',
    'TP_P95_Q',
    'TP_P95_MULT',
    'TP_P95_STATISTIC',
    'FOREX_P95_RET_USD',
    # Detector
    'DETECTOR_FVG_MIN_ZONE_USD',
    'DETECTOR_MAX_ZONE_AGE_BARS',
    'DETECTOR_FVG_DISPLACEMENT_RATIO',
    'DETECTOR_FVG_BODY_DEFINITION',
    'DETECTOR_PLAYED_OUT_MIN_EXTENSION_USD',
    'DETECTOR_INVALIDATION_MIN_PIERCE_USD',
    'DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS',
    'FVG_BODY_ONLY_MITIGATION',
    'FVG_BODY_ONLY_INVALIDATION',
    'DETECTOR_REQUIRE_RETEST_TO_INVERT',
    'DETECTOR_SUPERSEDE_ON_NEW',
    'DETECTOR_MIN_LIFETIME_SECS',
    'DETECTOR_RESAMPLE_SECS',
    'ATR_LEN',
    'STRICT_WICK_REQUIRED',
    'STRICT_WICK_MIN_WICK_PRICE_PCT',
    'STRICT_WICK_RECOMPUTE_SECS',
    # VP profile
    'VP_SESSION_SECS',
    'VP_ROLLING_SECS',
    'VP_ROWS',
    'VP_VA_PCT',
    'VP_HVN_PEAK_PCT',
    'VP_HVN_MIN_PROMINENCE_PCT',
    'VP_HVN_ZONE_PCT',
    'VP_LVN_THRESHOLD_PCT',
    'VP_MIN_ZONE_USD',
    # Structure build
    'STRUCTURE_RESAMPLE_SECS',
    'STRUCTURE_PIVOT_LEN',
    # Allowlists (callers may want to validate their own keys)
    '_VP_OVERRIDES_ALLOWED',
    '_DETECTOR_OVERRIDES_ALLOWED',
    '_ENGINE_OVERRIDES_ALLOWED',
    '_STRUCTURE_OVERRIDES_ALLOWED',
    '_SWING_OVERRIDES_ALLOWED',
    '_P95_OVERRIDES_ALLOWED',
    '_LIVE_P95_OVERRIDES_ALLOWED',
    '_derive_taker_fee_bps',
]
