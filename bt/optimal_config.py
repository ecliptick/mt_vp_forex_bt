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

# Hard FLOOR on stop distance from entry. Can only ever LOOSEN a
# too-tight stop — tightening to satisfy a minimum would invert the
# trade's geometry. A shelf can resolve a few cents from the entry;
# on a 1s feed with a $0.47 round trip, a stop inside the tick band is
# not a stop, it is a fee payment labelled a loss. $2.50 is ~5.3x the
# round-trip cost.
SL_MIN_USD = 2.50

# Hard CEILING on stop distance from entry — the inverse of
# ``sl_min_usd``. Can only ever TIGHTEN a too-wide stop. It is applied
# AFTER the floor, so with both set the effective stop distance lands
# in ``[sl_min_usd, sl_max_usd]``.
#
# Why the stop was too wide (measured on the 1m zone set):
#
#     slice      SL p50   HVN-anchored p50   non-HVN p50
#     2026-01    $16.05        $10.43            $34.00
#     2026-03    $31.16        $22.24            $59.20
#     2025-05    $13.20         $7.56            $25.48
#
# The cause is the SHELF SPACING in the daily VP profile (~4
# shelves/day, p90 gap $22.63), not the anchor. The nearest shelf
# below entry is routinely $10-$30 away, so the fix is a CEILING, not
# a different anchor.
#
# CANONICAL = 8.00 (user instruction, 2026-10-25).
#
# The $2.50 ceiling was PROMOTED on 2026-10-24 under the vol gate at
# 3.0, and it was PAIRED with that gate (see the note below). Both
# halves of that pair changed on 2026-10-25:
#
#   * the ceiling moved 2.50 -> 8.00, and
#   * the vol gate moved 3.0 -> 1.0 (to hit the 30-70 trades/day
#     target), which RE-OPENS the stop band the ceiling was closing.
#
# MEASURED on the continuous corpus (2024-01-01 -> 2026-07-16, 14-day
# warmup prefix, one continuous execution; `runtime/diagnostics/
# config_iso_2026_10_25.py`):
#
#   cell                       legs  /day   gross     fees      net   g/leg
#   base_canonical             1692   1.83   $510.58   $850.92  -$340.34  0.3018
#   sl_max_8                   1705   1.84   $905.43   $850.94   +$54.49  0.5310
#   contra_5                   1696   1.83   $582.71   $850.91  -$268.20  0.3436
#   sl_max_8_contra_5          1713   1.85   $751.77   $850.92   -$99.14  0.4389
#   sl_max_8_contra_5_gate_1  51075  55.10  $7,312.96 $19,758.46 -$12,445.50 0.1432
#
# **The ceiling alone takes the canonical book from -$340.34 to
# +$54.49 NET** — the first net-positive continuous-corpus result in
# this repo's history. It works because at $2.50 the stop was pinned
# on EVERY leg (1 distinct stop distance across the book), so the HVN
# anchor was inert and the payoff geometry was a fixed 1:$8 bet with
# no structural content. At $8.00 the anchor is LIVE again and the
# stop distances actually vary.
#
# NOTE: the ceiling is PAIRED with ``vol_state_min_range_norm`` — they
# must always be re-measured together. With the gate ON at 3.0 the
# optimum was $2.50; with the gate ON at 1.0 it is $8.00. See
# AGENTS.md "Promotions of 2026-10-25".
SL_MAX_USD = 8.00

# 0.0 = DERIVE the SL's shelf-search radius per bar from the live caps
# (``_reachable_tp_band_usd``). It used to fall back to the engine's
# ``TP_MAX_DISTANCE_USD`` module constant (1500.0, BTC-scale), so the
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
# ``$1.00`` BTC constant. The branch now measures from the shelf edge
# (the actual intent), so the knob is live across its range.
#
# Inferred XAU scale: the re-scaled VP gives a median shelf of ~$1.62.
# 0.30 is ~18% of that — a clean $0.30 shy of the edge.
LVN_SKIP_USD = 0.30
LVN_MODE = 'skip'

# ─── TP geometry (NB74 multi-target) ────────────────────────────
#
# NTH_HVN_N — which HVN shelf out of the ones ahead of entry in the
# trade's direction becomes TP1 (the ``partial_close_pct`` exit), with
# the remainder riding to TP2.
#
# It was ALWAYS wired to the engine but was INERT on XAUUSD, because
# the VP profile underneath it was built with BTC-scale constants
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
PARTIAL_CLOSE_PCT = 0.50

# TP2 (the runner leg) anchoring:
#
#   'hvn'    — TP2 = the next HVN shelf beyond TP1. Both legs anchored
#              on volume; the runner is a real magnet. Canonical.
#   'zone_mult' — v1 behaviour (200 x zone_width = a $72 target against
#              a ~$20 cap — unreachable by construction). A/B baseline.
#   'swing'  — TP2 pinned at the swing cap.
#
# 0 = "the next shelf after TP1" (keeps the legs distinct by
# construction rather than relying on _separate_tp_legs to pin them).
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
# Provenance of the old value: 6.0 (upstream crypto) -> 50.0 (BTC
# scale) -> 13.0 ("XAU scale fix"). Each was a hand-picked literal
# copied from the MEASURED MEDIAN of the caps on ONE slice — a regime
# statistic frozen as config.
#
# 0.0 = DERIVE the radius per bar from the same caps that will clamp
# the leg. Pass a positive value to restore frozen behaviour for A/B.
TP_MAX_DISTANCE_USD = 0.0

# Fraction of ``TP_ZONE_MULT_FALLBACK`` used for TP1 when no HVN shelf
# resolves inside the radius (the ``multi_hvn_proxy`` branch). Was a
# bare ``0.7`` literal inside that arithmetic; promoted here so it is
# sweepable and its scale is stated. 0.7 keeps TP1 inside the fallback
# leg so the two legs stay distinct.
TP_HVN_PROXY_FRAC = 0.7

TP_MODE = 'multi_target'

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

# ─── Per-side sequence drop set (PROMOTED 2026-10-24) ───────────
#
# Keys are ``"<side>|<last-event>"`` where side is ``"long"`` or
# ``"short"`` and last-event is the signature letter of the most recent
# structure break (``b``=BoS bull, ``B``=BoS bear, ``c``=CHoCH bull,
# ``C``=CHoCH bear). A key PRESENT in the set means: do not open that
# trade.
#
# THE RULE: do not take a SHORT when the last structure event was a
# REVERSAL ("c" = CHoCH bull, "C" = CHoCH bear). Do take shorts after
# a bearish CONTINUATION ("B" = BoS bear).
#
# MARKET READING. The FVG that triggers a short formed BEFORE the
# reversal. The reversal event is precisely what invalidates it. So
# a short taken after a fresh structure reversal is trading against
# the structure that has just been established. A short after a
# bearish CONTINUATION ("B") is with the trend -- and that bucket is
# POSITIVE on both halves (+$70 fit / +$11 held), so it is kept.
#
# MEASURED AT THE SIGNAL BAR (the bar the engine gates on), 8 slices,
# canonical MT4 sizing, promoted vol gate
# (``runtime/diagnostics/seq_drop_set_validate.py``):
#
#   bucket        FIT gross   HELD gross   verdict
#   long|b          +$7         +$40        keep
#   long|B          +$29        -$13        keep (not consistent)
#   long|c          +$21         $0         keep
#   long|C         +$211         +$2        KEEP -- best long bucket
#   short|B         +$70        +$11        KEEP -- the good short bucket
#   short|c         -$24        -$36        DROP (consistent negative)
#   short|C         -$23        -$20        DROP (consistent negative)
#   short|b          --          --        STRUCTURALLY EMPTY (see below)
#
# WHY THIS IS PER-SIDE: the same event type is good for one side and
# bad for the other. A pooled filter can never express that.
#
# EFFECT of dropping short|c and short|C (8 slices, pooled):
#
#   metric                control    filtered
#   legs                       577         395
#   total gross              +$276       +$379   (+$103)
#   total net                  +$9       +$197   (+$187)
#   short-side gross          -$21        +$82
#   short-side gross/leg    -$0.081     +$0.996  (12x better)
#   long-side gross          +$297       +$297   <- UNTOUCHED
#   long-side legs             313         313   <- UNTOUCHED
#   long-side corr          +0.042      +0.042
#   short-side corr         +0.070      +0.131
#   dropped-trade t-stat        --      -1.55
#   flat-slice d net           --       +$47
#
# WHY THIS IS NOT A "TRADE LESS" WIN:
#   * GROSS improves by +$103. Fee savings are only $86, so the gross
#     delta alone is positive -- the signal is real.
#   * The short side's gross PER LEG goes from NEGATIVE to POSITIVE
#     (12x better). The side is being IMPROVED, not muted.
#   * The LONG side is completely untouched -- same legs, same gross,
#     same correlation. The filter does not disturb it.
#   * BOTH sides still trade (82 short legs, corr +0.131), so this is
#     NOT a directional bet.
#
# THE `short|b` BUCKET IS STRUCTURALLY EMPTY. A bullish BoS
# invalidates the opposing bear FVGs, so no short can be signalled on
# a bar whose last structure event is a bullish BoS. That is a
# property of the strategy, not a data artefact -- see
# ``runtime/diagnostics/seq_signal_vs_fill_bar.py``.
#
# ⚠️ MEASUREMENT TRAP (this cost one wrong promotion). The FIRST
# measurement tagged the trade's FILL bar and found "short|b" with
# 105 trades, a t-stat of -2.10, and a +$104 gross improvement. That
# was a FILL-TIME REGIME measurement, not a signal-time one: under
# the D2 resting-order model the order fills a median 42 bars after
# the signal and the structure has moved on by then. The engine gate
# CANNOT act on a fill-time effect -- the decision is made at the
# signal. The correct signal-time measurement gives a different (and
# much better) answer. Always gate on the signal bar.
#
# Applied AFTER the sequence-direction flip (so it sees the FINAL
# side), AFTER the side gate, and BEFORE the conflict-policy check (so
# a dropped trade cannot consume a same-direction slot or perturb the
# book). Both the immediate and the sniper path are gated; dropped
# signals are counted in ``res.n_dropped_seq_side``.
#
# Locked by ``tests/test_regressions.py::test_per_side_sequence_drop_set_is_promoted``.
STRUCTURE_SEQ_DROP_SET: frozenset[str] = frozenset({"short|c", "short|C"})

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
# the filter rejected ~100% of zones. The sibling BTC recipe's 0.00025
# is a $0.68 floor at gold $3,000, which rejects 100% of zones on
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
    # SL_MODE_HVN_LVN anchor + floor/ceiling. ``sl_nth_hvn_n`` is
    # separate from the TP's ``nth_hvn_n`` on purpose.
    'sl_nth_hvn_n',
    'sl_min_usd',
    'sl_max_usd',
    'sl_hvn_max_distance_usd',
    # HVP chain
    'poc_buffer_usd',
    'hvn_buffer_usd',
    'lvn_skip_usd',
    'lvn_mode',
    # TP fallback + multi-target
    'tp_mode',
    'tp_zone_mult_fallback',
    'tp_max_distance_usd',
    # No-HVN-shelf TP1 fraction (was a bare 0.7 literal inside
    # compute_structural_tp).
    'tp_hvn_proxy_frac',
    # NB91 p95 cap: the caller builds a RollingP95Cap object and
    # passes it as ``p95_cap``. The cap object carries the
    # lookback/inner_secs/p95_q/p95_mult internally.
    'p95_cap',
    # NB74 multi-target kwargs
    'nth_hvn_n',
    'partial_close_pct',
    # TP2 anchoring (the runner leg).
    'tp2_mode',
    'tp2_nth_hvn_n',
    'tp2_swing_cap',
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
    # PER-SIDE SEQUENCE DROP SET (promoted 2026-10-24). A set of
    # "<side>|<last-event>" keys; a key present means "do not open
    # that trade". Canonical = {"short|b"}.
    'structure_seq_drop_set',
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
    'vol_state_max_rv_ratio_30s',
    'vol_state_max_wick_ratio',
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
        # 'sl_atr_mult' intentionally ABSENT — it is not on the sweep
        # surface. The engine keeps its default 0.0, which is the only
        # value that was ever reachable here.
        sl_nth_hvn_n=SL_NTH_HVN_N,
        sl_min_usd=SL_MIN_USD,
        sl_max_usd=SL_MAX_USD,
        sl_hvn_max_distance_usd=SL_HVN_MAX_DISTANCE_USD,
        # ── HVP chain ──
        poc_buffer_usd=POC_BUFFER_USD,
        hvn_buffer_usd=HVN_BUFFER_USD,
        lvn_skip_usd=LVN_SKIP_USD,
        lvn_mode=LVN_MODE,
        # ── TP geometry ──
        tp_mode=TP_MODE,
        tp_zone_mult_fallback=TP_ZONE_MULT_FALLBACK,
        tp_max_distance_usd=TP_MAX_DISTANCE_USD,
        # No-HVN-shelf fallback fraction for TP1.
        tp_hvn_proxy_frac=TP_HVN_PROXY_FRAC,
        # NB74 nth-hvn (TP1 shelf selector) + the TP1 partial fraction.
        nth_hvn_n=NTH_HVN_N,
        partial_close_pct=PARTIAL_CLOSE_PCT,
        # TP2 anchoring (the runner leg).
        tp2_mode=TP2_MODE,
        tp2_nth_hvn_n=TP2_NTH_HVN_N,
        tp2_swing_cap=TP2_SWING_CAP,
        # ── Gates ──
        structure_gate=STRUCTURE_GATE,
        structure_max_age_bars=STRUCTURE_MAX_AGE_BARS,
        # Each vol_state *_min threshold is independent; 0.0 = disabled.
        # The *_max pair rejects when the measure EXCEEDS the threshold.
        vol_state_min_vel_5m=VOL_STATE_MIN_VEL_5M,
        vol_state_min_vel_15m=VOL_STATE_MIN_VEL_15M,
        vol_state_min_rv_ratio_30s=VOL_STATE_MIN_RV_RATIO_30S,
        vol_state_min_range_norm=VOL_STATE_MIN_RANGE_NORM,
        vol_state_max_rv_ratio_30s=VOL_STATE_MAX_RV_RATIO_30S,
        vol_state_max_wick_ratio=VOL_STATE_MAX_WICK_RATIO,
        # ── Book ──
        max_same_dir_open=MAX_SAME_DIR_OPEN,
        conflict_policy=CONFLICT_POLICY,
        trade_sides=TRADE_SIDES,
        # PER-SIDE SEQUENCE DROP SET (2026-10-24): drop shorts whose
        # last structure event was a bullish BoS ('b'). The one
        # (side, event) bucket that is negative on BOTH the fit and
        # the held-out slices. See ``STRUCTURE_SEQ_DROP_SET``.
        structure_seq_drop_set=STRUCTURE_SEQ_DROP_SET,
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
    'STRUCTURE_GATE',
    'STRUCTURE_MAX_AGE_BARS',
    'STRUCTURE_SEQ_DROP_SET',
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
