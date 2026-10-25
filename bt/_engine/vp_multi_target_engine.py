"""VP-anchored multi-target TP strategy.

Self-contained strategy package (extracted 2026-10-05 from
``notebooks/nb71_volume_profile_sniper.py``). See ``AGENTS.md`` in this
directory for how to run and sweep it.

What it is
==========
Entries are the canonical FVG / iFVG detector zones (immediate zone-edge
fills + sniper fills on inversion), unchanged from the root strategy. What
is different is the EXIT model, which is anchored to the daily volume
profile:

  * TP1 = the Nth HVN shelf ahead (default 3rd), optionally shooting
    through intervening LVNs. 50% of the position is closed here.
  * SL moves to breakeven.
  * TP2 = ``tp_zone_mult`` x zone width (the canonical wide target). The
    remainder rides it.

The two targets are complementary rather than competing: the HVN leg has a
high fill rate but small wins, the wide leg has a low fill rate but large
wins. Firing BOTH on the same entry captures both without having to
classify the trade in advance -- which NB74 showed is not reliably
predictable at entry time (oracle accuracy only ~54%).

The canonical bar engine (``src.backtest.ict_backtest``) does NOT implement
this exit model and has no volume profile. Do not mix numbers from the two
engines.

Public API
==========
``run_vp_sniper_backtest``  -- bar loop over a monthly file
``compute_structural_sl``   -- SL selection
``compute_structural_tp``   -- TP1 / TP2 / partial-fraction selection
``VpConfig``                -- engine configuration
``OpenTrade`` / ``ClosedTrade`` / ``BacktestResult``
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
import pandas as pd

from bt._engine.ict_signals import FvgZone
from bt._engine.timebase import assert_ns, to_ns_arr
from bt._engine.swing_cap import DailySwingCap
from bt._engine.rolling_p95 import RollingP95Cap
from bt._engine.market_structure import (  # 2026-10-23: structure gate
    StructureState,
    _BREAK_SIG,
    _break_bars,
    bos_choch_directional_alignment,
    combined_score,
)
from bt._engine.volume_profile import (
    VolumeProfileSet,
    current_daily_vp,
    hvn_zones_near,
)

from bt._engine.vp_multi_target_config import (
    ENTRY_FILL_MODELS,
    ENTRY_FILL_NEXT_OPEN,
    ENTRY_FILL_TOUCH,
    ENTRY_FILL_TRIGGER,
    FILL_MODEL_MID,
    FILL_MODEL_NEXT_OPEN,
    FILL_MODEL_PESSIMISTIC,
    FILL_MODELS,
    LVN_MODE_SKIP,
    SL_MODE_FVG_ONLY,
    SL_MODE_HVN_LVN,
    SL_MODES,
    TP_MODE_FVG_MULT,
    TP_MODE_MULTI_TARGET,
)


@dataclass
class OpenTrade:
    trade_id: int
    parent_zone_id: int
    entry_bar: int
    entry_time_ns: int
    entry_price: float
    direction: int               # +1 long, -1 short
    stop_price: float            # absolute SL price
    target_price: float          # absolute TP price
    fvg_direction: int           # parent zone direction
    source: str                  # "sniper" | "immediate"
    sl_mode: str
    tp_mode: str
    sl_struct_ref: str = ""      # diagnostic: "zone_edge" | "poc" | "hvn"
    tp_struct_ref: str = ""      # diagnostic: "hvn" | "naked_poc" | "zone_mult"
    # 2026-10-23: BoS/CHoCH verdict inherited from the PendingOrder.
    # Carried through fill so the CLOSED trade can be attributed.
    struct_verdict: float = 0.0
    # 2026-10-24: the bar the SIGNAL fired on. Under the D2 resting
    # order model ``entry_bar`` is the FILL bar, which is LATER
    # (median 42 bars on the canonical slice). Entry-side gates are
    # evaluated at the SIGNAL bar -- that is when the decision is
    # made, and the only bar at which the decision is causal.
    signal_bar: int = 0
    zone_width: float = 0.0      # added 2026-10-02 (NB73)
    zone_low: float = 0.0
    zone_high: float = 0.0
    # Multi-target (NB74b): second TP for partial close
    target_price_2: float = 0.0   # second TP (e.g. fvg_mult when TP1=HVN)
    tp_struct_ref_2: str = ""    # e.g. "zone_mult" for TP2
    partial_close_pct: float = 0.0  # fraction to close at TP1 (e.g. 0.5)
    # Initial risk/reward, captured at FILL time (D2, 2026-10-22).
    # After a TP1 partial close the stop moves to breakeven, so
    # ``abs(entry_price - stop_price)`` becomes 0.0 on the remainder
    # leg. These fields preserve the trade's ORIGINAL geometry so the
    # aggregate risk metrics stay honest.
    initial_sl_distance_usd: float = 0.0
    initial_tp_distance_usd: float = 0.0
    # NB87: live conviction-tracking fields. Updated in the bar loop
    # on every bar the position is still open. Used by the
    # ``replace_conviction`` policy to score how much "expected
    # remaining PnL" each open position still represents.
    # ``mfe_usd`` / ``mae_usd`` are in $ (unsigned). ``tp1_hit`` is
    # sticky: once True, stays True. ``live_pnl_usd`` is the unrealized
    # PnL at the most-recent bar (using that bar's close).
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    tp1_hit: bool = False
    live_pnl_usd: float = 0.0
    # NB: size THIS position carries. Defaults to the run-level
    # ``qty_btc``. Set to ``qty_btc * (1 - partial_close_pct)`` on the
    # remainder leg opened at TP1. Before this field existed every exit
    # path used the run-level ``qty_btc``, so a half-size remainder leg
    # was closed at FULL size — silently inflating P&L ~2x on TP1->TP2
    # trades. 0.0 means "use the run-level qty_btc" (see
    # ``qty_of`` in the bar loop).
    qty: float = 0.0

    def qty_of(self, fallback: float) -> float:
        """Position size to use for P&L, falling back to run-level qty."""
        return float(self.qty) if float(self.qty) > 0.0 else float(fallback)


@dataclass
class PendingOrder:
    """A resting entry limit order at a zone edge (D2, 2026-10-22).

    Pre-D2 the engine opened a trade at the zone edge on the trigger
    bar, regardless of whether the market ever traded there. Measured
    on the 1-month 2025-01 canonical slice:

        * 95.9% of trades were entered at a price the market only
          reached LATER (median 42 bars, p90 759 bars later).
        * 4.1% were entered at a price the market NEVER reached at
          all — a free, guaranteed fill.

    A ``PendingOrder`` fixes that. The order rests at the zone edge
    from the trigger bar onward and fills on the first LATER bar whose
    range touches the entry level. If the zone dies first, the order
    is cancelled and the trade never existed (it is not a loss).

    The SL/TP geometry is computed at FILL time (not placement time),
    because the swing / p95 caps are time-dependent and the regime at
    entry is what should set the target.
    """
    order_id: int
    zone: FvgZone
    direction: int
    entry_price: float
    placed_bar: int
    placed_time_ns: int
    source: str
    fvg_direction: int
    zone_width: float
    # The bar at which the zone dies (0 = never). The order is
    # cancelled on that bar if it has not filled.
    death_bar: int = 0
    # Bookkeeping.
    filled: bool = False
    cancelled: bool = False
    cancel_reason: str = ""
    # 2026-10-23: what BoS/CHoCH said at SIGNAL time.
    #   "aligned"    last break agreed with the trade direction
    #   "opposed"    last break disagreed
    #   "no_event"   no break within ``structure_max_age_bars``
    #
    # Captured at PLACEMENT (not fill) time deliberately: the gate is
    # a signal-time decision, and an order that rests for an hour must
    # not be re-judged against the structure that moved while it waited.
    # This is what makes the filter measurable after the fact.
    struct_verdict: float = 0.0


@dataclass
class ClosedTrade:
    trade_id: int
    parent_zone_id: int
    entry_bar: int
    exit_bar: int
    entry_time_ns: int
    exit_time_ns: int
    entry_price: float
    exit_price: float
    direction: int
    stop_price: float
    target_price: float
    exit_reason: str             # "tp" | "sl" | "inv" | "eod" | "soft_sl"
    hold_secs: float
    pnl_gross_usd: float
    fee_usd: float
    pnl_net_usd: float
    qty_btc: float
    fvg_direction: int
    source: str
    sl_mode: str
    tp_mode: str
    sl_struct_ref: str
    tp_struct_ref: str
    sl_distance_usd: float = 0.0
    tp_distance_usd: float = 0.0
    entry_poc_dist_usd: float = 0.0
    zone_width: float = 0.0         # added 2026-10-02 (NB73)
    zone_low: float = 0.0
    zone_high: float = 0.0
    target_price_2: float = 0.0  # NB74b: multi-target TP2
    tp_struct_ref_2: str = ""
    partial_close_pct: float = 0.0  # NB74b: fraction closed at TP1
    # The bar the SIGNAL fired on (2026-10-24). Under the D2 resting
    # order model ``entry_bar`` is the FILL bar, which is LATER --
    # median 42 bars on the canonical slice. Entry-side gates (the
    # side gate, the per-side sequence drop set) are evaluated at the
    # SIGNAL bar, because that is when the decision is made and
    # because it is the only bar at which the decision is causal.
    # Post-hoc analysis must use this field, not ``entry_bar``, or it
    # will not reproduce the engine's own filter.
    signal_bar: int = 0
    # D2/diagnostics (2026-10-22): the ORIGINAL stop distance, captured
    # at fill time. After a TP1 partial close the stop moves to
    # breakeven, so ``sl_distance_usd`` becomes 0.0 on the remainder
    # leg — which made the aggregate "median SL distance" metric
    # report 0.0 for the whole run. This field is the honest measure
    # of the initial risk.
    initial_sl_distance_usd: float = 0.0
    initial_tp_distance_usd: float = 0.0
    # 2026-10-23: BoS/CHoCH verdict at SIGNAL time. Now the SIGNED
    # combined score in [-1, +1] (structure x momentum), which is
    # always computable -- no timeout, so ``None`` is unreachable once
    # 5 events exist. Recorded even when ``structure_gate=False`` so
    # the filter can be evaluated AFTER THE FACT.
    struct_verdict: float = 0.0


@dataclass
class BacktestResult:
    trades: List[ClosedTrade] = field(default_factory=list)
    n_signals_emitted: int = 0
    n_sniper_filled: int = 0
    n_immediate_filled: int = 0
    n_soft_stops: int = 0
    n_inversions_detected: int = 0
    # NB86: conflict_policy (drop / replace / stack) counters. Mirrors
    # the bar engine. Mirrors the engine — meaning these count what the
    # POLICY decided, not what vol_state or other gates decided.
    n_dropped_opposite: int = 0       # policy="drop" blocked the new signal
    n_replaced_opposite: int = 0      # policy="replace" soft-stopped an open opposite
    n_stacked_opposite: int = 0       # policy="stack" let an opposite open through
    n_same_dir_blocked: int = 0       # policy="replace"/"stack" capped on same-dir count
    # NB87: conviction-weighted replace counters (only fire under
    # ``conflict_policy="replace_conviction"``).
    n_dropped_conviction: int = 0     # new signal's conviction failed to beat old
    n_replaced_conviction: int = 0    # new signal beat old; soft-stop fired
    # NB91-DIAG (2026-10-21): per-gate attribution counters. Pure
    # observability — the engine does NOT read these back, they exist so
    # a sweep can answer "of N signals, how many died at each gate?".
    #
    # Motivation: the canonical 1y XAUUSD run emitted 264,666 signals and
    # closed 0 trades with ``n_dropped_opposite == 0``, which is
    # self-contradictory (every signal must pass vol_state, the p95 warmup
    # gate, AND conflict_policy before it can fill). These counters
    # localise the binding constraint instead of guessing.
    #
    # ``n_signals_emitted`` counts a signal the moment a zone triggers,
    # BEFORE any gate runs. Everything after that is subtractive, so:
    #   n_signals_emitted
    #     - n_dropped_vol_state      (NB85 vol-expansion gate)
    #     - n_dropped_p95_warmup    (NB91 warmup gate, immediate path)
    #     - n_dropped_p95_warmup_sniper (NB91 warmup gate, sniper path)
    #     - n_dropped_policy        (NB86 conflict_policy)
    #   == n_immediate_filled + n_sniper_filled
    n_dropped_vol_state: int = 0
    n_dropped_p95_warmup: int = 0
    n_dropped_p95_warmup_sniper: int = 0
    # BoS/CHoCH gate drops (2026-10-23). Counted on BOTH entry paths
    # (immediate + sniper). Add to the ``gate_breakdown`` chain:
    #   n_signals_emitted
    #     - n_dropped_structure
    n_dropped_structure: int = 0
    # Structure-gate diagnostics: how the rejects break down. Cheap
    # O(1) counters, so a run can prove the gate is actually firing
    # rather than silently passing everything.
    n_structure_aligned: int = 0
    n_structure_opposed: int = 0
    n_structure_no_event: int = 0
    # Trades whose side was REVERSED by the sequence-aware direction
    # flip (2026-10-23). Non-zero means the flip map actually fired.
    n_structure_seq_flipped: int = 0
    # Signals rejected by the SIDE GATE (2026-10-24) -- the trade's
    # direction was on a side the config does not permit. Non-zero
    # means the side gate actually fired.
    n_dropped_sides: int = 0
    # Signals rejected by the PER-SIDE SEQUENCE DROP SET (2026-10-24)
    # -- the trade's (side, last structure event) pair was in the drop
    # set. Non-zero means the per-side sequence filter actually fired.
    n_dropped_seq_side: int = 0
    # Policy drops at SIGNAL time (the signal never became an order).
    n_dropped_policy: int = 0
    # Policy drops at FILL time (the order was placed, then the book
    # changed and the order was cancelled). Tracked SEPARATELY from
    # ``n_dropped_policy`` so the signal accounting stays conserved:
    #   n_signals_emitted
    #     - n_dropped_vol_state
    #     - n_dropped_p95_warmup
    #     - n_dropped_p95_warmup_sniper
    #     - n_dropped_policy           (signal time)
    #   == n_orders_placed
    # and separately:
    #   n_orders_placed
    #     - n_dropped_policy_at_fill
    #   == n_orders_filled + n_orders_cancelled
    n_dropped_policy_at_fill: int = 0
    # Zones skipped because they had ALREADY fired (dual-mode
    # immediate/sniper dedup). Not a "gate" — bookkeeping.
    n_skipped_already_fired: int = 0
    # Zones skipped in dual mode because they had already inverted
    # (the sniper path owns those, not the immediate path).
    n_skipped_dual_inverted: int = 0
    # Number of DISTINCT zones bucketed into the engine's trigger maps.
    # If this is far below the detector's zone count, the engine never
    # even saw the zones (trigger_bar out of range, etc.).
    n_zones_bucketized: int = 0
    # ── D2 (2026-10-22): entry-order lifecycle counters ────────────
    #
    # Before D2 the engine opened a trade at the zone edge on the
    # trigger bar whether or not the market ever traded there. Now the
    # entry is a RESTING LIMIT ORDER:
    #
    #     n_orders_placed     — orders that reached the resting state
    #     n_orders_filled     — orders that actually filled
    #     n_orders_cancelled  — orders killed before filling
    #     n_orders_cancelled_zone_death — of those, killed because the
    #                                        zone died first
    #     n_orders_cancelled_expired    — of those, killed by expiry
    #
    # Conservation: n_orders_placed == n_orders_filled
    #                            + n_orders_cancelled
    n_orders_placed: int = 0
    n_orders_filled: int = 0
    n_orders_cancelled: int = 0
    n_orders_cancelled_zone_death: int = 0
    n_orders_cancelled_expired: int = 0
    # ── CONTRA CLOSE (2026-10-24) ─────────────────────────────────
    #
    # Hedge netting. When the book holds BOTH sides and the price
    # spread between the weighted-average opposing entries exceeds
    # ``contra_close_spread_usd``, the overlapping quantity is closed
    # SIMULTANEOUSLY at market, banking the locked spread instead of
    # letting one leg's independent stop claim the loss.
    #
    #   n_contra_close_events  — how many times the netting fired
    #   n_contra_legs_closed   — how many trade legs it closed
    #   n_contra_qty_netted    — total netted quantity (oz)
    #   contra_gross_usd       — gross P&L banked by the netting
    #   contra_fees_usd        — fees paid by the netting
    #   contra_spread_usd_max  — the widest spread that triggered it
    n_contra_close_events: int = 0
    n_contra_legs_closed: int = 0
    n_contra_qty_netted: float = 0.0
    contra_gross_usd: float = 0.0
    contra_fees_usd: float = 0.0
    contra_spread_usd_max: float = 0.0
    # ── HEDGING-FREQUENCY COUNTERS (2026-10-25) ───────────────────
    #
    # The contra-close feature is a PASSIVE SPREAD SCAN, but the
    # feature the user actually described is an INCOMING-OPPOSITE
    # close: "contra doesn't mean open positions currently, it means
    # incoming opposite positions should close it".
    #
    # These counters measure how often the book is actually hedged,
    # independent of any threshold:
    #
    #   n_fills_opposite_open — a fill landed while an OPPOSITE
    #     position was already open. This is the count of
    #     incoming-opposite events the book actually experienced.
    #   n_fills_total — total fills (denominator).
    #   n_bars_both_sides_open — bars on which BOTH sides held
    #     quantity. This is the denominator for "how often is the
    #     book hedged at all".
    #
    # Without these, the only way to answer "why only 11 events?" was
    # to REPLAY the trade ledger outside the engine, which is exactly
    # the kind of reconstruction that can be wrong (it has to guess
    # the interleaving of fills and exits).
    n_fills_opposite_open: int = 0
    n_fills_total: int = 0
    n_bars_both_sides_open: int = 0
    # The widest weighted-average entry spread the book ever showed
    # while hedged, and the number of DISTINCT such spreads. If the
    # canonical threshold sits ABOVE the widest observed spread, the
    # feature can never fire at all.
    hedged_spread_max_usd: float = 0.0
    hedged_spread_bars: int = 0
    # Fill-model / entry-fill-model actually used (self-describing).
    fill_model_used: str = FILL_MODEL_MID
    entry_fill_model_used: str = ENTRY_FILL_TOUCH
    # Settled signal policy actually used by the run (for run_report
    # self-description; settable by the caller but defaulted in the engine).
    conflict_policy_used: str = "drop"
    max_same_dir_open_used: int = 0
    # NB87b (2026-10-08): mark-to-market equity curve, one value per bar.
    # Populated by ``run_vp_sniper_backtest`` whenever at least one bar
    # existed in the input. Shape ``(n_bars,)`` of float64 USD PnL,
    # measured from the start of the run. The curve reflects the OPEN
    # book at each bar: at bar ``i`` it is
    #   cumulative closed-PnL up to bar i
    # + sum of ``OpenTrade.live_pnl_usd`` across every still-open trade
    # evaluated at the most recent bar's close.
    # This is the same definition the bar engine uses in
    # ``ict_backtest.py:2923`` (``equity[i] = cum_pnl``), generalised to
    # the VP-MT case where the book can hold many opposing positions at
    # once and the realised drawdown is *not* the risk dimension — the
    # MTM drawdown on the open book is.
    #
    # When this field is empty (length 0) the run was constructed by
    # callers that predate the equity-curve port — the field stays
    # optional via the dataclass default, and downstream code MUST
    # fall back to the closed-trade-only curve.
    equity_curve: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float64)
    )


# ─────────────────────────────────────────────────────────────────────
# VP-aware SL/TP computation
# ─────────────────────────────────────────────────────────────────────
def _zone_dead_by(zone: FvgZone, bar: int) -> bool:
    """True if ``zone`` was already dead at or before ``bar``.

    A zone's death bars (``superseded_bar`` / ``played_out_bar`` /
    ``expired_bar``) are computed over the WHOLE file, so they carry
    look-ahead information. Comparing them against ``bar`` — the bar the
    signal actually fires on — restores causality.

    ``expired_bar`` deliberately does NOT kill a zone that has already
    inverted: once price has committed through the zone, the setup has
    played out regardless of the zone's later expiry. This mirrors the
    bar engine's ``and not getattr(zone, "inverted", False)`` guard.
    """
    sup = int(getattr(zone, "superseded_bar", -1))
    if 0 <= sup <= bar:
        return True
    po = int(getattr(zone, "played_out_bar", -1))
    if 0 <= po <= bar:
        return True
    exp = int(getattr(zone, "expired_bar", -1))
    if 0 <= exp <= bar and not getattr(zone, "inverted", False):
        return True
    return False


def _zone_death_bar(zone: FvgZone) -> int:
    """The bar at which ``zone`` died, or 0 if it never died.

    Used to cancel a resting entry order the moment its zone dies
    (D2). Returns the EARLIEST of the three death bars, since that is
    the first bar on which the level is no longer valid.

    ``expired_bar`` is ignored for zones that have already inverted —
    once price has committed through the zone, the setup has played
    out regardless of the zone's later expiry. This mirrors
    :func:`_zone_dead_by`.
    """
    bars = [
        int(getattr(zone, "superseded_bar", -1)),
        int(getattr(zone, "played_out_bar", -1)),
    ]
    if not getattr(zone, "inverted", False):
        bars.append(int(getattr(zone, "expired_bar", -1)))
    live = [b for b in bars if b >= 0]
    return min(live) if live else 0


def _order_touched(
    *,
    b_low: float,
    b_high: float,
    entry_price: float,
    direction: int,
) -> bool:
    """True if this bar's range touches the resting order's level.

    A resting BUY limit at ``entry_price`` fills when the market trades
    DOWN to it (``b_low <= entry_price``). A resting SELL limit fills
    when the market trades UP to it (``b_high >= entry_price``).
    """
    if direction > 0:
        return b_low <= entry_price
    return b_high >= entry_price


def _resolve_exit_price(
    *,
    level: float,
    b_open: float,
    b_low: float,
    b_high: float,
    direction: int,
    is_stop: bool,
    fill_model: str,
) -> float:
    """Where a stop/target is booked, given the bar that crossed it.

    * ``fill_model == "mid"`` (legacy, optimistic): always at the
      level itself.
    * ``fill_model == "pessimistic"``: if the bar GAPPED THROUGH the
      level (i.e. the level was already beyond the bar's open), the
      fill is booked at the bar's OPEN — the worst realistic price —
      rather than at the level, which the market never traded at.
    * ``fill_model == "next_open"``: always at the bar's open.
    """
    if fill_model == FILL_MODEL_NEXT_OPEN:
        return b_open
    if fill_model != FILL_MODEL_PESSIMISTIC:
        return level
    # Pessimistic: a gap-through books at the open.
    if is_stop:
        # Stop is on the adverse side of the position.
        gapped = (b_open <= level) if direction > 0 else (b_open >= level)
    else:
        # Target is on the favourable side.
        gapped = (b_open >= level) if direction > 0 else (b_open <= level)
    if gapped:
        return b_open
    return level


def _bar_resolves_stop_first(
    *,
    sl_hit: bool,
    tp_hit: bool,
    fill_model: str,
) -> bool:
    """Resolve the intra-bar ambiguity when a bar touches BOTH SL and TP.

    * ``"mid"`` (legacy, optimistic): the TARGET wins.
    * ``"pessimistic"`` and ``"next_open"``: the STOP wins.

    Measured on the 1-month 2025-01 canonical slice, 0.00% of trades
    have a bar touching both levels (1s resolution is fine enough that
    the ambiguity is rare), so this knob is not the binding constraint
    on this corpus — but it MUST be pessimistic, because it starts
    biting as soon as ``sl_zone_mult`` is tightened to 8 and the stop
    sits close to the entry.
    """
    if not (sl_hit and tp_hit):
        return False
    return fill_model != FILL_MODEL_MID


def _vol_state_pass(
    i: int,
    vol_state: Optional[Dict[str, np.ndarray]],
    min_rv30: float,
    min_rng: float,
    min_v5: float,
    min_v15: float,
    max_rv30: float,
    max_wick: float,
) -> bool:
    """Volatility-regime gate at bar ``i``. Mirrors the bar engine exactly.

    Semantics copied from ``run_ict_backtest``: a threshold of 0.0 means that
    measure is not gated, all configured measures must pass (AND), and a
    failing regime only *skips the new entry* — it never closes an open
    position, because "bad regime now" is not the same claim as "this trade
    has failed".
    """
    if vol_state is None:
        return True
    ok = True
    if min_rv30 > 0.0:
        ok = ok and float(vol_state["rv_ratio_30s"][i]) >= min_rv30
    if min_rng > 0.0:
        ok = ok and float(vol_state["range_norm_1m"][i]) >= min_rng
    if min_v5 > 0.0:
        ok = ok and float(vol_state["vel_5m"][i]) >= min_v5
    if min_v15 > 0.0:
        ok = ok and float(vol_state["vel_15m"][i]) >= min_v15
    if max_rv30 > 0.0:
        ok = ok and float(vol_state["rv_ratio_30s"][i]) <= max_rv30
    if max_wick > 0.0:
        ok = ok and float(vol_state["wick_ratio"][i]) <= max_wick
    return ok


def _apply_swing_cap(
    *,
    tp_price: float,
    tp_ref: str,
    entry_price: float,
    direction: int,
    swing_cap: Optional[DailySwingCap],
    t_ns: int,
) -> Tuple[float, str]:
    """Cap TP distance at ``cap_pct * recent swing``.

    If a swing cap is enabled and the unconstrained TP exceeds the
    cap, tighten the TP to the cap distance and append ``"+swing"``
    to the diagnostic ref so per-trade output traces whether the
    cap was the binding constraint.

    Returns ``(tp_price, tp_ref)``.
    """
    if swing_cap is None or not swing_cap.enabled:
        return tp_price, tp_ref
    cap_usd = swing_cap.cap_for(t_ns)
    if not np.isfinite(cap_usd):
        return tp_price, tp_ref
    if direction > 0:
        # Long: TP is above entry. Cap TP at entry + cap_usd.
        capped = float(entry_price) + cap_usd
        if tp_price > capped:
            return capped, tp_ref + "+swing"
    else:
        # Short: TP is below entry. Cap TP at entry - cap_usd.
        capped = float(entry_price) - cap_usd
        if tp_price < capped:
            return capped, tp_ref + "+swing"
    return tp_price, tp_ref


def _p95_cap_usd(
    p95_cap: Optional["RollingP95Cap"],
    t_ns: int,
    tp_p95_mult: Optional[float],
) -> Optional[float]:
    """Effective p95 TP ceiling in USD at ``t_ns``, or ``None`` if inactive.

    Single source of truth for the p95 ceiling. ``_apply_p95_cap`` uses it
    to clamp a price; ``_reachable_tp_band_usd`` uses it to size the HVN
    shelf SEARCH RADIUS. Both must read the same number — when they
    disagreed (radius frozen at a literal, cap read live), far shelves were
    found and then clamped back onto the ceiling, collapsing TP1 onto TP2.

    Returns ``None`` when the cap cannot constrain anything: no cap object,
    cap disabled, explicitly disabled via ``tp_p95_mult <= 0``, or a
    non-finite / non-positive value at this timestamp (coldstart).
    """
    if p95_cap is None or not p95_cap.enabled:
        return None
    if tp_p95_mult is not None and float(tp_p95_mult) <= 0.0:
        return None
    cap_usd = p95_cap.cap_for(t_ns)
    if not np.isfinite(cap_usd) or cap_usd <= 0.0:
        return None
    if tp_p95_mult is not None:
        # ``cap_for`` already applied the cap object's own mult; rescale
        # to the requested one.
        _obj_mult = float(p95_cap.p95_mult)
        if _obj_mult > 0.0:
            cap_usd = cap_usd * (float(tp_p95_mult) / _obj_mult)
    return float(cap_usd)


def _structure_verdict(
    bar: int,
    zone: Any,
    structure: Optional["StructureState"],
    max_age_bars: int,
) -> str:
    """Classify the BoS/CHoCH state at ``bar`` WITHOUT gating on it.

    Returns ``"aligned"`` / ``"opposed"`` / ``"no_event"``.

    Separated from :func:`_structure_pass` so the verdict can be
    RECORDED on every trade with the gate OFF. Without that, the filter
    is unfalsifiable -- you cannot ask "were the trades structure
    agreed with actually better?" on a run where structure was never
    consulted. Recording it costs three string comparisons and turns the
    gate into something measurable.
    """
    if structure is None:
        return "no_event"
    aligned = bos_choch_directional_alignment(
        structure, bar, int(getattr(zone, "direction", 0)),
        max_age_bars=max_age_bars,
    )
    if aligned is True:
        return "aligned"
    if aligned is False:
        return "opposed"
    return "no_event"


def _structure_pass(
    bar: int,
    zone: Any,
    structure: Optional["StructureState"],
    gate: bool,
    max_age_bars: int,
    res: Optional["BacktestResult"] = None,
) -> bool:
    """BoS/CHoCH directional gate for a candidate signal.

    Added 2026-10-23. ``structure_gate=False`` (the canonical default)
    returns True immediately so the pre-gate numbers stay reproducible.

    The zone's OWN direction is the trade direction: a bull FVG fires a
    long, a bear FVG fires a short. We ask the structure state whether
    the most recent BoS/CHoCH within ``max_age_bars`` agrees.

    **Skip, never invert.** A mismatch rejects the signal; it does not
    open the opposite side. Auto-inverting would turn a filter into a
    reversal strategy and re-create the symmetric long/short bet that
    this gate exists to break (docs/v2_spec.md 6.1).

    ``bos_choch_directional_alignment`` returns ``None`` for "no recent
    event". We treat that as REJECT -- admitting unaligned trades is
    what made the gate a no-op during exactly the quiet periods where
    a structure filter has to earn its keep.
    """
    if not gate:
        return True
    if structure is None:
        # Validated up front in the engine; belt-and-braces.
        return True
    aligned = bos_choch_directional_alignment(
        structure, bar, int(getattr(zone, "direction", 0)),
        max_age_bars=max_age_bars,
    )
    if res is not None:
        if aligned is True:
            res.n_structure_aligned += 1
        elif aligned is False:
            res.n_structure_opposed += 1
        else:
            res.n_structure_no_event += 1
    return aligned is True


def _score_scale(
    score: float,
    lo: float,
    hi: float,
) -> float:
    """Map a signed score in ``[-1, 1]`` onto a multiplier in ``[lo, hi]``.

    Monotone increasing: ``score = -1 -> lo``, ``0 -> midpoint``,
    ``score = +1 -> hi``. The score is the DIRECTIONAL one (already
    sign-resolved by the caller), so this only reads its magnitude via
    the mapping's position.
    """
    s = float(np.clip(score, -1.0, 1.0))
    return float(lo + 0.5 * (hi - lo) * (s + 1.0))


def _sequence_signature(
    structure: Optional["StructureState"],
    bar: int,
    length: int,
) -> str:
    """The trailing ``length`` BoS/CHoCH events as a compact signature.

    ``b``=BoS bull, ``B``=BoS bear, ``c``=CHoCH bull, ``C``=CHoCH bear.
    Oldest first, so ``"bCcbb"`` reads as a history.

    Returns ``""`` when there are not enough events to fill the window,
    which is deliberately NOT padded -- a padded signature would collide
    with real ones.
    """
    if structure is None or length <= 0:
        return ""
    breaks = getattr(structure, "breaks", None)
    if not breaks:
        return ""
    bars = _break_bars(structure)
    if bars.size == 0:
        return ""
    upto = int(np.searchsorted(bars, bar, side="right"))
    if upto < length:
        return ""
    out: list[str] = []
    for j in range(upto - length, upto):
        kind = breaks[j].kind
        out.append(_BREAK_SIG.get(kind, "?"))
    return "".join(out)


def _structure_score_pass(
    bar: int,
    zone: Any,
    structure: Optional["StructureState"],
    vol_state: Any,
    close: Optional[np.ndarray],
    gate: bool,
    min_abs_score: float,
    lookback_events: int,
    half_life_bars: float,
    res: Optional["BacktestResult"] = None,
) -> tuple[bool, float]:
    """STAGE 1 of the two-stage filter: BINARY alignment (2026-10-23).

    Replaces the ``max_age_bars`` timeout gate. The old gate could
    return "no recent event" and treated that as REJECT -- which on
    2025-01 rejected 6,352-9,387 signals for the ABSENCE of structure
    while the slice carried 857 events. This version has no timeout:
    it scores the last ``lookback_events`` (5) events and admits on
    SIGN.

    Returns ``(admit, score)``. The score is signed and is what
    Stage 2 uses to scale SL/TP.

    The score is computed and returned **even when the gate is OFF**
    (only the admission decision is skipped), so a run with the gate
    disabled still records the structure/momentum state of every
    trade and the filter can be evaluated after the fact. With
    ``gate=False`` or no ``structure`` the score is 0.0 and scaling is
    neutral, which keeps pre-gate numbers reproducible.

    **Skip, never invert** -- unchanged from the binary gate.
    """
    if structure is None:
        return True, 0.0
    direction = int(getattr(zone, "direction", 0))
    combined, _s, _m = combined_score(
        structure, vol_state, bar, direction,
        close=close,
        lookback_events=lookback_events,
        half_life_bars=half_life_bars,
    )
    if not gate:
        return True, combined
    if res is not None:
        if direction * combined > 0.0:
            res.n_structure_aligned += 1
        else:
            res.n_structure_opposed += 1
    # Sign must agree with the trade AND conviction must clear the bar.
    admit = (direction * combined > 0.0) and (abs(combined) >= min_abs_score)
    return admit, combined


def _swing_cap_usd(
    swing_cap: Optional[DailySwingCap],
    t_ns: int,
) -> Optional[float]:
    """Effective NB72 swing TP ceiling in USD at ``t_ns``, or ``None``."""
    if swing_cap is None or not swing_cap.enabled:
        return None
    cap_usd = swing_cap.cap_for(t_ns)
    if not np.isfinite(cap_usd) or cap_usd <= 0.0:
        return None
    return float(cap_usd)


def _reachable_tp_band_usd(
    *,
    p95_cap: Optional["RollingP95Cap"],
    swing_cap: Optional[DailySwingCap],
    t_ns: int,
    tp_p95_mult: Optional[float],
) -> float:
    """The distance band a TP can actually reach at ``t_ns``.

    This is the *reachable band*, derived — NOT a hardcoded dollar amount.
    It is the tighter of the two live TP ceilings, because ``_apply_swing_cap``
    and ``_apply_p95_cap`` are applied to each leg AFTER ``compute_structural_tp``
    returns, so the final TP distance is bounded by exactly this:

        tp_dist = min(raw_tp, swing_cap(t), p95_cap(t))

    Two consequences that make this the correct search radius for the HVN
    shelf lookup:

    1. It can never hide a reachable shelf. A shelf inside the band is not
       clamped, so it lands as a genuine structural target.
    2. It cannot invite an unreachable one. A shelf beyond the band would
       be clamped back onto the ceiling, which is precisely the TP1 == TP2
       collapse that ``_separate_tp_legs`` then has to paper over with a
       $0.01 pin.

    So ``nth_hvn_n`` selects among shelves the trade can actually get to,
    with no constant to re-tune per regime.

    Returns ``inf`` when neither cap is active (no ceiling is in force).
    The shelf walk cost is unchanged by the radius — ``hvn_zones_near``
    scans every shelf in the daily profile and truncates to ``max_results``
    — so an unbounded radius costs nothing here.
    """
    limits = [
        _c for _c in (
            _p95_cap_usd(p95_cap, t_ns, tp_p95_mult),
            _swing_cap_usd(swing_cap, t_ns),
        ) if _c is not None
    ]
    return min(limits) if limits else float("inf")


def _apply_p95_cap(
    *,
    tp_price: float,
    tp_ref: str,
    entry_price: float,
    direction: int,
    p95_cap: Optional["RollingP95Cap"],
    t_ns: int,
    tp_p95_mult: Optional[float] = None,
) -> Tuple[float, str]:
    """Cap TP distance at ``tp_p95_mult × p95(rolling peak-to-trough)``.

    Regime-aware cap (replaces ``tp_max_distance_usd = 6.0`` which
    was a regime-blind noise target). When ``p95_cap`` is enabled
    and the unconstrained TP exceeds the cap, tighten the TP to
    the cap distance and append ``"+p95"`` to the diagnostic ref.

    The cap object is a :class:`RollingP95Cap` from
    ``bt._engine.rolling_p95``. The live coldstart uses
    ``seed_constant_usd`` until the rolling window has enough bars.

    ``tp_p95_mult`` (engine kwarg, added 2026-10-22) OVERRIDES the
    multiplier baked into the cap object. It exists so the p95 cap
    is sweepable without rebuilding the whole cap object per cell.
    ``None`` = use the cap object's own ``p95_mult``. ``0.0``
    disables the cap entirely.

    Returns ``(tp_price, tp_ref)``.
    """
    cap_usd = _p95_cap_usd(p95_cap, t_ns, tp_p95_mult)
    if cap_usd is None:
        return tp_price, tp_ref
    if direction > 0:
        capped = float(entry_price) + cap_usd
        if tp_price > capped:
            return capped, tp_ref + "+p95"
    else:
        capped = float(entry_price) - cap_usd
        if tp_price < capped:
            return capped, tp_ref + "+p95"
    return tp_price, tp_ref


def _separate_tp_legs(
    *,
    tp1: float,
    tp2: float,
    tp1_ref: str,
    tp2_ref: str,
    entry_price: float,
    direction: int,
    min_leg_usd: float = 0.01,
) -> Tuple[float, float, str, str]:
    """Guarantee TP2 sits strictly beyond TP1 in the trade's direction.

    Without this the multi-target model silently degenerates to a
    single-target one. Both legs are capped by the SAME p95 cap, so
    whenever TP1 (HVN) and TP2 (zone_mult) are both farther out than
    the cap, both clamp to ``entry ± cap`` — the identical price. On
    the canonical XAUUSD config that happened on effectively every
    trade: 88.7% of HVN lookups miss (see the ``multi_hvn_proxy``
    fallback), the proxy puts TP1 far beyond the cap, and TP2's raw
    200× zone width is also beyond it, so both legs land on the cap.

    The result is a "partial close at TP1" whose remainder targets the
    same level, i.e. pure single-target behaviour with extra fees.

    Fix: pull TP1 back to just inside TP2 so the two legs stay
    distinct. TP1 is the *nearer, higher-confidence* target, so it is
    the correct one to move.

    SINGLE-TARGET MODE (2026-10-24 fix). ``tp_mode='fvg_mult'`` has
    NO second leg — ``compute_structural_tp`` returns ``tp2 = 0.0``.
    The pinning branch below then computed
    ``max(entry + 0.01, 0.0 - 0.01)`` and dragged a legitimate $400
    target down to **one cent** above the entry. The trade "hit" its
    TP on the very next bar and every single-target trade was a
    $0.01 scalp by construction.

    The guard below restores the intent: with no second leg there is
    nothing to separate, so TP1 is returned untouched. A long's TP2
    must be ABOVE the entry and a short's BELOW it, so a non-positive
    / non-negative second leg means "no second leg".
    """
    # No second leg -> nothing to separate. See the docstring.
    if direction > 0:
        if tp2 <= float(entry_price):
            return tp1, tp2, tp1_ref, tp2_ref
    else:
        if tp2 >= float(entry_price):
            return tp1, tp2, tp1_ref, tp2_ref

    if direction > 0:
        if tp1 < tp2 - min_leg_usd:
            return tp1, tp2, tp1_ref, tp2_ref
        new_tp1 = max(
            float(entry_price) + min_leg_usd, tp2 - min_leg_usd)
        return new_tp1, tp2, tp1_ref + "+pinned", tp2_ref
    else:
        if tp1 > tp2 + min_leg_usd:
            return tp1, tp2, tp1_ref, tp2_ref
        new_tp1 = min(
            float(entry_price) - min_leg_usd, tp2 + min_leg_usd)
        return new_tp1, tp2, tp1_ref + "+pinned", tp2_ref


# ─────────────────────────────────────────────────────────────────────
# CONTRA CLOSE / HEDGE NETTING (2026-10-24, user request)
# ─────────────────────────────────────────────────────────────────────
#
# The strategy's structural weakness: the book can hold opposing
# positions at the same time (a long bought at $3,000 and a short sold
# at $3,050). Those two legs are a LOCKED SPREAD — the P&L of the pair
# is deterministic and independent of where the market goes next:
#
#     gross = (P - E_long) * Q  +  (E_short - P) * Q
#           = Q * (E_short - E_long)
#
# But the engine never closes them together, and each leg keeps its
# OWN stop. So the pair does NOT bank the locked spread; it bleeds:
#
#   * one leg hits its stop -> a real loss on a position whose
#     counterparty was sitting on an equal-and-opposite gain that is
#     never taken, so the lock is broken for a LOSS;
#   * both legs eventually round-trip -> the round-trip fee is paid
#     TWICE ($0.47 x 2) to realise a spread that was already in hand.
#
# "Conta close" (contra close) fixes this: when the book holds both
# sides and the PRICE SPREAD between the weighted-average opposing
# entries exceeds a threshold, the overlapping quantity is netted out
# SIMULTANEOUSLY at the current market price. The spread is banked
# deterministically, the hedged quantity stops carrying independent
# stops, and one round trip's fee is paid instead of two.
#
# KEY DESIGN DECISIONS
# ─────────────────────
# 1. WHERE IT RUNS. The netting runs AFTER step 2b (order fills) and
#    BEFORE step 3 (SL/TP resolution) on the same bar. That ordering
#    matters: a hedged leg that would have been stopped out on this bar
#    is netted FIRST, so the stop never fires on a position whose
#    counterparty is right there. Running it after step 3 would let the
#    stop claim the loss before the netting could bank the spread.
#
# 2. WHAT "SPREAD" MEANS. The trigger is the distance between the
#    weighted-average LONG entry and the weighted-average SHORT entry:
#
#        spread = wavg(entry_short) - wavg(entry_long)
#
#    This is the price distance between the two sides' average
#    entries, NOT a P&L. It is scale-free in the sense that it is a
#    price, and it directly measures how much spread is locked up. A
#    positive spread means the shorts entered ABOVE the longs — the
#    profitable lock.
#
# 3. NETTING QUANTITY. The matched quantity is
#
#        M = min(Q_long, Q_short)
#
#    and BOTH sides are reduced by M, pro-rata within each side. The
#    residual keeps its directional exposure, so the book is NOT
#    flattened — the un-matched part of the larger side still runs.
#
# 4. MULTI-TARGET SAFETY. A trade that has NOT yet hit TP1 is a full
#    position with a live TP1/TP2 pair. A trade that HAS hit TP1 is a
#    remainder leg (already half-closed, breakeven stop, single TP2
#    target). Netting them differently is the whole subtlety:
#
#      * Pre-TP1 trade, netted PARTIALLY: the matched slice is closed
#        at market, and the RESIDUAL must keep its multi-target
#        geometry — TP1, TP2 and ``partial_close_pct`` all still
#        apply to the remainder. So the residual is rewritten in
#        place with the reduced quantity and the SAME target prices.
#      * Pre-TP1 trade, netted FULLY: closed outright, no residual.
#      * Post-TP1 remainder leg, netted PARTIALLY: the residual is a
#        remainder leg (breakeven stop, TP2 target) at reduced size.
#      * Post-TP1 remainder leg, netted FULLY: closed outright.
#
#    A partially-netted pre-TP1 trade must NOT become a remainder leg
#    (that would silently promote its stop to breakeven and drop its
#    TP1). The distinction is carried by the ``partial_close_pct``
#    field, which is exactly what distinguishes the two states in the
#    engine already.
#
# 5. THE PRICE. Both sides close at the SAME price — the current
#    market price — so the realised pair P&L is exactly the locked
#    spread, independent of the fill price. Any other choice (closing
#    each leg at its own entry) would be a fiction.
#
# 6. FEES. A contra close pays ONE round trip PER LEG, on the netted
#    quantity, using the engine's existing formula
#    ``|exit + entry| * 0.5 * qty * bps/10000 * 2.0`` with that leg's
#    OWN entry price.
#
#    This was DESIGNED as "one round trip per pair" and was REJECTED
#    on 2026-10-24. On a hedging account each leg is a separate
#    position with its own entry spread and its own exit spread, so
#    closing both pays two round trips. The STATUS QUO also pays two
#    (each leg eventually closes on its own), so the netting does not
#    change the fee COUNT — its benefit is realising the locked
#    spread deterministically and removing the naked residual.
#
#    Charging one round trip would be a $0.47-per-trade windfall that
#    flatters the feature: it would make the netting look profitable
#    even when the locked spread is smaller than the round trip. The
#    honest model is two round trips, and the feature has to clear
#    that bar on its own merits.


def _contra_spread_usd(
    long_qty: float,
    long_entry_sum: float,
    short_qty: float,
    short_entry_sum: float,
) -> float:
    """Weighted-average price spread between the long and short sides.

    Returns ``wavg(short_entry) - wavg(long_entry)``. Positive means
    the shorts entered ABOVE the longs — the profitable lock.

    Returns 0.0 when either side is empty (nothing to net).
    """
    if long_qty <= 0.0 or short_qty <= 0.0:
        return 0.0
    return (short_entry_sum / short_qty) - (long_entry_sum / long_qty)


def _contra_close_price(
    *,
    b_open: float,
    b_low: float,
    b_high: float,
    b_close: float,
    fill_model: str,
) -> float:
    """The market price at which a contra close is booked.

    The netting happens at the bar's CLOSE in the general case: both
    legs are closed at the same price, and the close is the only
    price that is known to have occurred on this bar without any
    intrabar path assumption.

    Under ``fill_model == "next_open"`` the whole outcome defers to
    the next bar's open, matching every other exit path in the
    engine.

    The KEY property is that the price is IDENTICAL for both legs, so
    the pair's gross P&L is exactly the locked spread regardless of
    which price is chosen. Choosing the close is therefore honest
    and conservative: it cannot manufacture a gain.
    """
    if fill_model == FILL_MODEL_NEXT_OPEN:
        return b_open
    return b_close


# ─────────────────────────────────────────────────────────────────────
# NB86: conflict-policy helpers (mirrors src/backtest/ict_backtest.py)
# ─────────────────────────────────────────────────────────────────────
def _opposite_open_exists_vp(open_trades: list, direction: int) -> bool:
    """True iff at least one open position is in the OPPOSITE direction.

    Mirrors ``_opposite_open_exists`` in the bar engine. Used to decide
    whether ``conflict_policy="drop"`` should block the new signal.
    """
    return any(int(ot.direction) == -int(direction) for ot in open_trades)


def _count_same_dir_open_vp(open_trades: list, direction: int) -> int:
    """Count open positions in ``direction`` (``+1`` long, ``-1`` short).

    Mirrors ``_count_same_dir_open`` in the bar engine. Used to decide
    whether ``max_same_dir_open`` should cap a same-direction double-down.
    """
    return sum(1 for ot in open_trades if int(ot.direction) == int(direction))


def _soft_stop_opposite_vp(
    open_trades: list,
    direction_to_replace: int,
    fill_price: float,
    i: int,
    b_time: int,
    qty_btc: float,
    taker_fee_bps: float,
    closed_trades: list,
) -> bool:
    """Soft-stop ONE open position in ``direction_to_replace`` at ``fill_price``.

    Mirrors ``_soft_stop_open_at_price`` in the bar engine, but for the
    VP-MT engine's ``OpenTrade`` / ``ClosedTrade`` dataclass shape (no
    ``["trade"]`` wrapping, no ``TrendStrategyParams``). Picks the
    oldest-matching open position (deterministic — preserves backtest
    reproducibility).

    Records the soft-stop as a ``ClosedTrade`` with ``exit_reason =
    "replaced_by_new_signal"``. Fee math is the same as a regular SL exit
    (taker_fee_bps applied to the notional at the exit price, both sides).

    Returns True if a position was closed, False otherwise.
    """
    for idx, ot in enumerate(open_trades):
        if int(ot.direction) != int(direction_to_replace):
            continue
        direction = int(ot.direction)
        entry_price = float(ot.entry_price)
        # NOTE: a multi-target remainder trade (already past TP1) is
        # FULLY closed at fill_price — soft-stop cancels the breakeven
        # remainder wholesale, and the post-replace branch in the caller
        # opens a fresh position with the new SL/TP.
        qty_used = ot.qty_of(qty_btc)  # this position's own size (see OpenTrade.qty)
        gross = (fill_price - entry_price) * direction * qty_used
        fee = abs(fill_price + entry_price) * 0.5 * qty_used * (
            taker_fee_bps / 10000.0) * 2.0
        net = gross - fee
        hold_secs = (b_time - ot.entry_time_ns) / 1e9
        sl_dist = abs(entry_price - ot.stop_price)
        tp_dist = abs(ot.target_price - entry_price)
        closed_trades.append(ClosedTrade(
            trade_id=ot.trade_id,
            parent_zone_id=ot.parent_zone_id,
            entry_bar=ot.entry_bar, exit_bar=i,
            entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time,
            entry_price=entry_price, exit_price=fill_price,
            direction=direction,
            stop_price=ot.stop_price,
            target_price=ot.target_price,
            exit_reason="replaced_by_new_signal",
            hold_secs=hold_secs,
            pnl_gross_usd=gross, fee_usd=fee, pnl_net_usd=net,
            qty_btc=qty_used,
            fvg_direction=ot.fvg_direction,
            source=ot.source,
            sl_mode=ot.sl_mode, tp_mode=ot.tp_mode,
            sl_struct_ref=ot.sl_struct_ref,
            tp_struct_ref=ot.tp_struct_ref,
            sl_distance_usd=sl_dist,
            tp_distance_usd=tp_dist,
            initial_sl_distance_usd=ot.initial_sl_distance_usd,
            initial_tp_distance_usd=ot.initial_tp_distance_usd,
            entry_poc_dist_usd=0.0,
            zone_width=ot.zone_width,
            zone_low=ot.zone_low,
            struct_verdict=ot.struct_verdict,
            signal_bar=ot.signal_bar,
            zone_high=ot.zone_high,
            target_price_2=ot.target_price_2,
            tp_struct_ref_2=ot.tp_struct_ref_2,
            partial_close_pct=ot.partial_close_pct,
        ))
        open_trades.pop(idx)
        return True
    return False


def _position_remaining_value(
    ot: "OpenTrade",
    model: Optional[Any] = None,
) -> float:
    """NB87: estimated remaining PnL for an open position.

    If ``model`` is a fitted sklearn regressor (Ridge, GBM, etc.),
    its ``predict`` is called with a feature vector built from
    ``ot``'s live-state fields. Otherwise a simple
    feature-engineered fallback is used:

        remaining = live_pnl_usd
                  + (1.0 if tp1_hit else 0.0) * tp_dist_remain_usd * 0.5
                  + max(0, mfe_usd - abs(live_pnl_usd)) * 0.5

    The fallback is sign-correct (longs and shorts both work). A
    position that has not moved (MFE = MAE = 0, live_pnl = 0) gets
    remaining = 0. A position that has moved favorably and hit
    TP1 (i.e. its remaining leg is the TP2 ride) gets positive
    bonus.
    """
    # NB87: tp_distance_usd is geometric — |target - entry| —
    # not stored on OpenTrade (it's on ClosedTrade, computed at
    # close time). Compute it here from the open-trade's
    # target_price / entry_price. sl_distance_usd similarly.
    tp_dist = abs(float(ot.target_price) - float(ot.entry_price))
    sl_dist = abs(float(ot.entry_price) - float(ot.stop_price))
    if model is not None:
        try:
            from numpy import asarray
            feats = asarray([[
                float(ot.entry_bar),
                float(ot.live_pnl_usd),
                float(ot.mfe_usd),
                float(ot.mae_usd),
                1.0 if ot.tp1_hit else 0.0,
                float(ot.zone_width),
                sl_dist,
                tp_dist,
                float(ot.direction),
            ]])
            v = float(model.predict(feats)[0])
            return max(0.0, v)
        except Exception:
            # Defensive: any model-predict failure falls back to
            # the feature-engineered estimate rather than corrupting
            # the backtest counter.
            pass
    # Feature-engineered fallback.
    tp_dist_remain = max(0.0, tp_dist - float(ot.mfe_usd))
    bonus = (1.0 if ot.tp1_hit else 0.0) * 0.5 * tp_dist_remain
    extra = max(0.0, float(ot.mfe_usd) - abs(float(ot.live_pnl_usd))) * 0.5
    return float(ot.live_pnl_usd) + bonus + extra


def _new_signal_score(
    direction: int,
    fill_price: float,
    model: Optional[Any] = None,
) -> float:
    """NB87: score for an incoming signal (its expected PnL if opened).

    When ``model`` is a fitted regressor, the feature vector is
    minimal — at signal-emit time we only have direction + price,
    not the post-entry MFE we will only learn after the bar
    resolves. The fallback is a constant 1.0, which degenerates
    ``replace_conviction`` to a count-thresholded replace (the
    "naive uniform score" baseline). Real scoring happens in
    Phase 2 (Ridge fit on per-bar diagnostics).
    """
    if model is not None:
        try:
            from numpy import asarray
            feats = asarray([[
                float(fill_price),
                float(direction),
                0.0,   # unknown: live_pnl (post-entry)
                0.0,   # unknown: mfe
                0.0,   # unknown: mae
                0.0,   # unknown: tp1_hit
                0.0,   # unknown: zone_width
                0.0,   # unknown: sl_distance
                0.0,   # unknown: tp_distance
            ]])
            return max(0.0, float(model.predict(feats)[0]))
        except Exception:
            pass
    return 1.0


def _conflict_policy_pass(
    open_trades: list,
    direction: int,
    fill_price: float,
    i: int,
    b_time: int,
    policy: str,
    max_same_dir_open: int,
    qty_btc: float,
    taker_fee_bps: float,
    closed_trades: list,
    res: "BacktestResult",
    # NB87: conviction-weighted replace knobs. Defaulted to uniform
    # (every position scores 1.0, every new signal scores 1.0) so
    # the existing call sites of the bar-engine default are
    # bit-identical. When ``conviction_model is None`` AND
    # ``conviction_alpha == 0.0`` AND ``conviction_hysteresis_usd ==
    # 0.0``, this policy behaves exactly like the legacy
    # ``conflict_policy="replace"`` (always soft-stops one opposite).
    conviction_alpha: float = 0.0,
    conviction_hysteresis_usd: float = 0.0,
    conviction_model: Optional[Any] = None,
) -> bool:
    """NB86: decide whether a freshly-emitted signal is allowed to OPEN.

    Mirrors the bar engine's two policy branches (``ict_backtest.py`` lines
    1497-1544 and 2453-2481). Returns True if the new signal should be
    opened, False if it should be dropped. Side effects: under
    ``policy="replace"`` may SOFT-STOP one opposite-direction open at
    ``fill_price``; counters on ``res`` are incremented on every policy
    decision so the sweep can report drop / replace / stack rates.

    Counter semantics (these match the bar engine's naming intent, but
    using the same field names this engine already has):

      * ``n_dropped_opposite`` — policy decided to NOT open (drop, or
        replace/stack capped on same-dir count). A signal was emitted
        and a counter went up; nothing else happened.
      * ``n_replaced_opposite`` — soft-stop actually fired under "replace".
      * ``n_stacked_opposite``  — under "stack" an opposite open was
        present and the new signal was allowed through anyway.
      * ``n_same_dir_blocked``  — same-direction cap tripped under
        "replace" or "stack".
    """
    pol = str(policy).lower().strip()
    opposite_exists = _opposite_open_exists_vp(open_trades, direction)
    same_dir_count = _count_same_dir_open_vp(open_trades, direction)

    if pol == "drop":
        # "drop" (default; live-mirror): a fresh signal in the opposite
        # direction right after one was just opened is a whipsaw re-entry,
        # not a real new setup. Block it. Same-direction double-downs do
        # not arise because every "drop" decision still blocks the new
        # signal — mirroring the bar engine.
        if opposite_exists:
            res.n_dropped_opposite += 1
            return False
        # PERF/POLICY FIX (2026-10-21): the same-direction cap applies
        # under "drop" too. Previously the cap was only checked in the
        # "replace" / "stack" branches, so with the canonical
        # ``max_same_dir_open = 0`` (uncapped) the book grew without
        # bound — measured mean depth 124 open positions, max 365, and
        # the book was NEVER flat (1 flat bar in 7,520 sampled signal
        # bars).
        #
        # Two consequences, both measured on 1-month 2025-01:
        #   * COST: the engine's per-bar book scan is
        #     O(n_bars x n_open). On 1y (41.2M bars x ~124) that is
        #     ~5.1 BILLION Python iterations — the 1y engine wall.
        #   * P&L: because the book never empties, "drop" rejected every
        #     subsequent opposite-direction signal, locking the run
        #     100% short (4,335 shorts / 0 longs from a detector that
        #     emitted 4,575 bull and 5,109 bear zones).
        #
        # The cap is a no-op when ``max_same_dir_open == 0`` (the
        # canonical value), so this restores the historical behaviour
        # exactly until the knob is explicitly set.
        if same_dir_count > 0 and max_same_dir_open > 0 \
                and same_dir_count >= max_same_dir_open:
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True

    if pol == "replace":
        # Soft-stop one opposite open at the bar's close, then let the new
        # signal through. Same-direction double-downs pass through
        # subject to ``max_same_dir_open`` (0 = uncapped).
        if opposite_exists:
            if _soft_stop_opposite_vp(
                open_trades, -direction, fill_price, i, b_time,
                qty_btc, taker_fee_bps, closed_trades,
            ):
                res.n_replaced_opposite += 1
        if same_dir_count > 0 and max_same_dir_open > 0 \
                and same_dir_count >= max_same_dir_open:
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True

    if pol == "stack":
        # Let the new signal open regardless. The book can hold opposing
        # positions at the same time. ``max_same_dir_open`` still applies.
        if opposite_exists:
            res.n_stacked_opposite += 1
        if same_dir_count > 0 and max_same_dir_open > 0 \
                and same_dir_count >= max_same_dir_open:
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True

    if pol == "replace_conviction":
        # NB87: conviction-weighted replace. The accumulated same-direction
        # conviction is the SUM of estimated remaining PnL across every
        # open position in the OPPOSITE direction. The new signal's
        # conviction is its expected PnL if opened (a feature-engineered
        # constant 1.0 by default, or the model's prediction when a
        # conviction_model is supplied).
        #
        # Decision rule (asymmetric penalty, user-requested):
        #
        #     required = old_conviction * (1 + alpha) + hysteresis
        #     if new_conviction > required: soft-stop + open
        #     else:                          drop
        #
        # Edge cases:
        #   * No opposite open -> always pass (this is the first
        #     signal in the book; the policy is not relevant).
        #   * alpha == 0 and hysteresis == 0 and model is None:
        #     degenerates to ``replace`` (always soft-stops).
        opposite_opens = [ot for ot in open_trades
                          if int(ot.direction) == -int(direction)]
        if not opposite_opens:
            return True
        old_conviction = sum(
            _position_remaining_value(ot, conviction_model)
            for ot in opposite_opens
        )
        new_conviction = _new_signal_score(
            direction, fill_price, conviction_model
        )
        required = old_conviction * (1.0 + conviction_alpha) \
                   + conviction_hysteresis_usd
        if new_conviction > required:
            if _soft_stop_opposite_vp(
                open_trades, -direction, fill_price, i, b_time,
                qty_btc, taker_fee_bps, closed_trades,
            ):
                res.n_replaced_conviction += 1
        else:
            res.n_dropped_conviction += 1
            return False
        # Same-direction cap still applies.
        if same_dir_count > 0 and max_same_dir_open > 0 \
                and same_dir_count >= max_same_dir_open:
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True

    # Unknown policy: fall back to "drop" (defensive — bar engine does
    # the same). This is the safe default because "drop" can never let
    # two opposing positions coexist, which is the most destructive
    # failure mode of a misconfigured policy.
    if opposite_exists:
        res.n_dropped_opposite += 1
        return False
    return True


def _apply_sl_max_ceiling(
    *,
    sl_price: float,
    entry_price: float,
    direction: int,
    sl_max_usd: float,
) -> Tuple[float, str]:
    """Enforce a MAXIMUM stop distance from entry.

    Returns ``(sl_price, ref_suffix)`` where ``ref_suffix`` is
    ``"+cap"`` when the ceiling was the binding constraint.

    This is the INVERSE of ``_apply_sl_min_floor``, and it was
    missing. The floor can only push the stop AWAY from entry, so a
    structural anchor that resolves far away (an HVN shelf $30 below
    entry) produced a $30 stop with NO way to pull it in.

    Measured on the 1m zone set (2026-10-24), canonical
    ``sl_mode='hvn_lvn'`` / ``sl_nth_hvn_n=1``:

        slice      SL p50   HVN-anchored p50   non-HVN p50
        2026-01    $16.05        $10.43            $34.00
        2026-03    $31.16        $22.24            $59.20
        2025-05    $13.20         $7.56            $25.48

    The user wants "around $2.50". The cause is the SHELF SPACING,
    not the shelf anchor: the daily VP profile carries only ~4 HVN
    shelves/day with a p50 shelf-to-shelf gap of $2.54-$4.00 and a
    p90 of $22-24. So "the nearest shelf below entry" is routinely
    $10-$30 away.

    The ceiling pulls the stop IN to ``entry - sl_max_usd``, which
    means it can land INSIDE the zone the trade was entered on. That
    is a deliberate, sweepable trade-off -- not a structural claim --
    and it is why the ref suffix is ``"+cap"`` rather than a
    structural level.

    The ceiling only ever TIGHTENS. It never pushes a stop away from
    entry, so it cannot invert the trade's geometry.
    """
    if sl_max_usd is None or float(sl_max_usd) <= 0.0:
        return float(sl_price), ""
    _max = float(sl_max_usd)
    if direction > 0:
        capped = float(entry_price) - _max
        if float(sl_price) < capped:
            return capped, "+cap"
    else:
        capped = float(entry_price) + _max
        if float(sl_price) > capped:
            return capped, "+cap"
    return float(sl_price), ""


def _apply_sl_min_floor(
    *,
    sl_price: float,
    entry_price: float,
    direction: int,
    sl_min_usd: float,
) -> Tuple[float, str]:
    """Enforce a minimum stop distance from entry.

    Returns ``(sl_price, ref_suffix)`` where ``ref_suffix`` is
    ``"+min"`` when the floor was the binding constraint (so per-trade
    diagnostics show it) and ``""`` otherwise.

    The floor is a RISK floor, not a geometry floor: a stop closer
    than ``sl_min_usd`` to entry is inside the noise band of a 1s feed
    and gets clipped by spread and tick jitter rather than by the
    thesis. It is also the level at which the $0.47 round-trip cost
    becomes a material fraction of the loss taken.

    Pushing the stop AWAY from entry is the only safe direction here.
    Tightening a stop toward entry to satisfy a minimum would invert
    the trade's geometry, so the floor only ever loosens.
    """
    if sl_min_usd is None or float(sl_min_usd) <= 0.0:
        return float(sl_price), ""
    _min = float(sl_min_usd)
    if direction > 0:
        floored = float(entry_price) - _min
        if float(sl_price) > floored:
            return floored, "+min"
    else:
        floored = float(entry_price) + _min
        if float(sl_price) < floored:
            return floored, "+min"
    return float(sl_price), ""


def compute_structural_sl(
    *,
    entry_price: float,
    direction: int,
    zone_low: float,
    zone_high: float,
    vp_set: VolumeProfileSet,
    t_ns: int,
    sl_mode: str,
    sl_buffer_usd: float,
    poc_buffer_usd: float,
    hvn_buffer_usd: float,
    zone_width: float = 0.0,
    sl_zone_mult: float = 0.0,
    sl_atr_mult: float = 0.0,
    atr_value: float = 0.0,
    sl_nth_hvn_n: int = 1,
    sl_min_usd: float = 0.0,
    sl_max_usd: float = 0.0,
    sl_hvn_max_distance_usd: float = 0.0,
    sl_search_radius_usd: Optional[float] = None,
) -> Tuple[float, str]:
    """Compute absolute SL price + diagnostic reference.

    Returns (sl_price, sl_struct_ref).

    ``sl_search_radius_usd`` is the DERIVED shelf-search radius, supplied
    by the caller from ``_reachable_tp_band_usd``. It is used only when
    ``sl_hvn_max_distance_usd <= 0``. Passing it in (rather than reading
    a module constant here) keeps the radius and the caps that will later
    clamp the exit on the same measurement.

    Scale-invariance (2026-10-05)
    =============================
    The original SL was ``zone_edge - sl_buffer_usd`` with an ABSOLUTE USD
    buffer. That is not scale-invariant, which is a real defect for a
    strategy that is supposed to transfer across an asset whose price ranged
    $68k-$112k in the sample:

        month      med zone_w    $5 / zone_w    $800 / zone_w
        2025-04         55.2           0.09            14.5
        2025-10         66.5           0.08            12.0
        2026-02         56.2           0.09            14.2
        2026-05         40.3           0.12            19.9

    So a single fixed buffer means something *different* in every month — the
    effective zone-multiple varies ~1.7x across the corpus for $5 and ~1.7x
    for $800. "A fixed $5 works" is not a portable claim; "12-20x zone width
    works" is.

    Hence two scale-invariant alternatives, both measured from the zone so
    they carry no absolute-dollar assumption:

    * ``sl_zone_mult`` — SL = zone edge +/- ``sl_zone_mult`` x zone_width.
      Matches how the entry is defined (an FVG gap), and is the direct
      analogue of the bar engine's ``fvg_inv_trade_sl_zone_mult``.
    * ``sl_atr_mult``  — SL = zone edge +/- ``sl_atr_mult`` x ATR. Adapts to
      volatility rather than gap size; useful when the gap is unrepresentative
      of current range.

    A fixed buffer remains available for continuity, but new work should use
    one of these.
    """
    # Scale-invariant stop distance, measured out from the zone edge.
    # Computed once, then applied identically by every mode below so the
    # modes differ only in WHICH level anchors the stop, not in how far
    # away it sits.
    if sl_atr_mult > 0.0 and atr_value > 0.0:
        offset = sl_atr_mult * float(atr_value)
    elif sl_zone_mult > 0.0 and zone_width > 0.0:
        offset = sl_zone_mult * float(zone_width)
    else:
        offset = float(sl_buffer_usd)

    # Zone-edge SL (always available)
    if direction > 0:
        zone_edge_sl = float(zone_low) - offset
    else:
        zone_edge_sl = float(zone_high) + offset
    zone_edge_ref = "zone_edge"

    if sl_mode == SL_MODE_FVG_ONLY:
        # 2026-10-24: the max-distance ceiling applies here too --
        # otherwise ``sl_mode='fvg_only'`` with a wide zone would be
        # the one path that could not be tightened.
        #
        # The floor is applied FIRST (it can only loosen) and the
        # ceiling SECOND (it can only tighten), so the effective stop
        # distance is clamped into ``[sl_min_usd, sl_max_usd]``. This
        # matches the ``hvn_lvn`` and ``fvg_or_poc`` branches. The
        # floor rarely binds here because the zone-mult stop is
        # already wide, but omitting it made the three modes
        # inconsistent: a mode whose stop happened to land inside
        # the floor would have silently ignored it.
        _sl, _min_ref = _apply_sl_min_floor(
            sl_price=zone_edge_sl, entry_price=entry_price,
            direction=direction, sl_min_usd=sl_min_usd,
        )
        _sl, _cap_ref = _apply_sl_max_ceiling(
            sl_price=_sl, entry_price=entry_price,
            direction=direction, sl_max_usd=sl_max_usd)
        return _sl, zone_edge_ref + _min_ref + _cap_ref

    # ── SL_MODE_HVN_LVN (2026-10-22, user request) ───────────────
    #
    # Anchor the stop to the Nth HVN shelf on the LOSS side of entry
    # — the mirror image of the TP, which targets the Nth HVN on the
    # GAIN side. The two directions are searched independently so the
    # TP can aim at a distant magnet (n=3) while the stop sits behind
    # the nearest structural shelf (n=1).
    #
    # ``sl_nth_hvn_n`` is deliberately a SEPARATE knob from the TP's
    # ``nth_hvn_n``. They answer different questions: "where do I
    # take profit" (n=3 — reach for the far magnet) vs "where is the
    # thesis invalid" (n=1 — respect the nearest shelf). Sharing one
    # knob would force the stop out to n=3, which is a wider stop by
    # construction.
    #
    # The shelf must sit strictly BEYOND the zone edge, otherwise the
    # stop lands inside the FVG the trade was entered on — a stop the
    # market routinely clips on the way in.
    if sl_mode == SL_MODE_HVN_LVN:
        # Look on the loss side: BELOW entry for a long, ABOVE for a
        # short. This is the opposite sign from the TP's search.
        hvn_dir = -1 if direction > 0 else +1
        _sl_radius = (
            float(sl_hvn_max_distance_usd)
            if sl_hvn_max_distance_usd > 0.0
            else (float(sl_search_radius_usd)
                  if sl_search_radius_usd is not None
                  else float("inf"))
        )
        # Fetch one more than requested so we can drop a shelf that sits
        # inside the zone and still land on the Nth VALID one.
        _want = max(int(sl_nth_hvn_n), 1)
        _found = hvn_zones_near(
            vp_set, t_ns, entry_price, hvn_dir,
            max_distance_usd=_sl_radius, max_results=_want + 2,
        )
        _sl_sl: Optional[float] = None
        _seen = 0
        for (lo, hi, mid) in _found:
            if direction > 0:
                # Long: shelf must be strictly BELOW the zone low.
                if not (float(hi) < float(zone_low)):
                    continue
                _cand = float(lo) - float(hvn_buffer_usd)
            else:
                # Short: shelf must be strictly ABOVE the zone high.
                if not (float(lo) > float(zone_high)):
                    continue
                _cand = float(hi) + float(hvn_buffer_usd)
            _seen += 1
            if _seen == _want:
                _sl_sl = _cand
                break
        # Fall back to the zone-edge stop when the Nth valid shelf does
        # not resolve. Falling back is the conservative choice: it keeps
        # the trade's stop at a structural level it already knows about
        # rather than inventing a distance.
        if _sl_sl is None:
            _sl, _cap_ref = _apply_sl_max_ceiling(
                sl_price=zone_edge_sl, entry_price=entry_price,
                direction=direction, sl_max_usd=sl_max_usd)
            return _sl, zone_edge_ref + _cap_ref
        _sl_sl, _min_ref = _apply_sl_min_floor(
            sl_price=_sl_sl, entry_price=entry_price,
            direction=direction, sl_min_usd=sl_min_usd,
        )
        # 2026-10-24: the ceiling is applied AFTER the floor, so the
        # floor still guarantees a minimum distance and the ceiling
        # still guarantees a maximum. With both set, the effective
        # stop distance is clamped into [sl_min_usd, sl_max_usd].
        _sl_sl, _cap_ref = _apply_sl_max_ceiling(
            sl_price=_sl_sl, entry_price=entry_price,
            direction=direction, sl_max_usd=sl_max_usd,
        )
        return _sl_sl, f"hvn_n{_want}" + _min_ref + _cap_ref

    # fvg_or_poc: take the TIGHTEST structural level on the loss side
    # that's still PAST the zone edge (so we don't tighten below the
    # zone's own structural invalidation level).
    candidates: List[Tuple[float, str]] = [(zone_edge_sl, zone_edge_ref)]

    # Daily POC SL — SL is just past the POC, on the loss side
    svp = current_daily_vp(vp_set, t_ns)
    if svp is not None:
        if direction > 0 and svp.poc < entry_price:
            # Long: SL is below POC. Only valid if POC is BELOW the
            # zone low (so SL is past the zone).
            poc_sl = svp.poc - poc_buffer_usd
            if poc_sl < float(zone_low):
                candidates.append((poc_sl, "poc"))
        elif direction < 0 and svp.poc > entry_price:
            # Short: SL is above POC. Only valid if POC is ABOVE
            # the zone high.
            poc_sl = svp.poc + poc_buffer_usd
            if poc_sl > float(zone_high):
                candidates.append((poc_sl, "poc"))

    # Nearest HVN behind entry on loss side
    # hvn_zones_near(price, direction=-1 for "behind entry in loss direction")
    # Long: look BELOW entry for HVNs (loss direction = down).
    # Short: look ABOVE entry for HVNs.
    hvn_dir = -1 if direction > 0 else +1
    hvn_results = hvn_zones_near(
        vp_set, t_ns, entry_price, hvn_dir,
        # Same derived radius as the hvn_lvn branch. This previously
        # read the ``TP_MAX_DISTANCE_USD`` module constant directly —
        # a BTC-scale literal with no relationship to either cap.
        max_distance_usd=(
            float(sl_hvn_max_distance_usd)
            if sl_hvn_max_distance_usd > 0.0
            else (float(sl_search_radius_usd)
                  if sl_search_radius_usd is not None
                  else float("inf"))
        ), max_results=5,
    )
    for (lo, hi, mid) in hvn_results:
        if direction > 0:
            # Long: HVN is below entry, SL is below HVN.
            hvn_sl = lo - hvn_buffer_usd
            if hvn_sl < float(zone_low):
                candidates.append((hvn_sl, "hvn"))
        else:
            # Short: HVN is above entry, SL is above HVN.
            hvn_sl = hi + hvn_buffer_usd
            if hvn_sl > float(zone_high):
                candidates.append((hvn_sl, "hvn"))

    # Take the TIGHTEST valid level (closest to entry on the loss side)
    if direction > 0:
        # Long: SL is below entry; we want the highest SL (least
        # distance to entry) that still respects all anchors.
        # But we also want a level PAST the zone edge, so cap at
        # the zone_edge_sl as the maximum (most-loss) acceptable.
        valid = [(p, r) for (p, r) in candidates if p <= zone_edge_sl + 1e-6]
        if not valid:
            best_p, best_ref = zone_edge_sl, zone_edge_ref
        else:
            # Choose the level that gives the smallest |entry - sl|
            # (tightest valid SL = highest price for long)
            best_p, best_ref = max(valid, key=lambda x: x[0])
    else:
        # Short: SL is above entry; we want the lowest SL (least
        # distance to entry) that still respects all anchors.
        valid = [(p, r) for (p, r) in candidates if p >= zone_edge_sl - 1e-6]
        if not valid:
            best_p, best_ref = zone_edge_sl, zone_edge_ref
        else:
            # Tightest valid SL = lowest price for short
            best_p, best_ref = min(valid, key=lambda x: x[0])

    # 2026-10-24 BUG FIX: the floor and the ceiling were MISSING from
    # this branch. It returned early with the raw anchor level, so
    # ``sl_mode='fvg_or_poc'`` ignored BOTH ``sl_min_usd`` and
    # ``sl_max_usd``.
    #
    # Measured before the fix (entry 3000, zone 2999-3000, zone_mult 40):
    #
    #   mode          ceiling  floor  -> SL dist   ref
    #   hvn_lvn       ON       2.50  ->    2.50    hvn_n1+cap
    #   fvg_only      ON       2.50  ->    2.50    zone_edge+cap
    #   fvg_or_poc    ON       2.50  ->   41.00    zone_edge   <-- WRONG
    #
    # So a sweep over ``sl_mode`` would have measured a $41 stop in the
    # ``fvg_or_poc`` cell while the other two modes honoured the
    # $2.50 ceiling — a silent, geometry-changing inconsistency.
    #
    # Canonical is ``sl_mode='hvn_lvn'`` so this did not bite the
    # canonical run, but ``sl_mode`` IS on the sweep allowlist, so the
    # cell was reachable and wrong.
    #
    # The order matters and matches the ``hvn_lvn`` branch: FLOOR first
    # (can only loosen), then CEILING (can only tighten).
    best_p, _min_ref = _apply_sl_min_floor(
        sl_price=best_p, entry_price=entry_price,
        direction=direction, sl_min_usd=sl_min_usd,
    )
    best_p, _cap_ref = _apply_sl_max_ceiling(
        sl_price=best_p, entry_price=entry_price,
        direction=direction, sl_max_usd=sl_max_usd,
    )
    return best_p, best_ref + _min_ref + _cap_ref


def compute_structural_tp(
    *,
    entry_price: float,
    direction: int,
    zone_low: float,
    zone_high: float,
    zone_width: float,
    vp_set: VolumeProfileSet,
    t_ns: int,
tp_mode: str,
tp_max_distance_usd: float,
    tp_zone_mult_fallback: float,
    lvn_mode: str,
    lvn_skip_usd: float,
    nth_hvn_n: int = 1,
    partial_close_pct: float = 0.5,
    tp_hvn_proxy_frac: float = 0.7,
    tp_search_radius_usd: Optional[float] = None,
    # 2026-10-23: what anchors the RUNNER leg (TP2). Previously TP2
    # was ALWAYS ``tp_zone_mult_fallback * zone_width`` -- a pure
    # zone-multiple, never a volume level. Measured consequence on
    # 2025-01: 63.9% of legs carried ``multi_zone_mult`` on TP2,
    # which at the canonical 200x on a $0.36 zone is a $72 target --
    # ~3.7x the median swing cap, so it is unreachable by construction
    # and only the TP1 partial ever fills.
    #
    #   "zone_mult"  v1 behaviour. Kept for A/B.
    #   "hvn"        TP2 = the NEXT HVN shelf beyond TP1. Both legs
    #                are then anchored on volume, and the runner is
    #                a real magnet rather than a zone multiple.
    #   "swing"      TP2 = the swing cap itself (the farthest price
    #                that is still inside the 3-day swing).
    tp2_mode: str = "zone_mult",
    tp2_nth_hvn_n: int = 0,          # 0 = "the next shelf after TP1"
) -> Tuple[float, str, float, str, float]:
    """Compute TP1 and TP2 for the multi-target strategy.

    Returns (tp_price, tp_ref, tp2_price, tp2_ref, partial_close_pct).
    Canonical modes:
    - ``TP_MODE_FVG_MULT``: single zone_mult TP (A/B baseline).
    - ``TP_MODE_MULTI_TARGET``: TP1 = HVN (partial 50%), TP2 = zone_mult (remainder 50%).

    ``tp_hvn_proxy_frac`` is the fraction of the ``tp_zone_mult_fallback``
    leg used for TP1 when NO HVN shelf resolves within the search radius
    (the ``multi_hvn_proxy`` branch). It was a bare ``0.7`` literal
    inline in that expression; promoting it to a named, sweepable knob
    makes the branch's scale explicit instead of leaving a magic
    constant buried in arithmetic. 0.7 places TP1 at 70% of the
    fallback leg — inside it, so the two legs stay distinct and TP1 is
    the nearer take-profit. Set to 0.0 to fall back to TP2's own level
    (single-target behaviour, legs pinned apart by ``_separate_tp_legs``).

    ``tp_search_radius_usd`` is the DERIVED HVN shelf search radius,
    supplied by the caller from ``_reachable_tp_band_usd``. When
    ``tp_max_distance_usd <= 0`` this derived value is used instead,
    so the radius always matches the caps that later clamp each leg.
    Pass a positive ``tp_max_distance_usd`` to restore the old frozen
    behaviour for A/B.

    Raises
    ------
    ValueError
        If ``tp_mode`` is not one of the two live modes.
    """
    # Fallback zone_mult TP
    zone_mult_tp = (
        entry_price + tp_zone_mult_fallback * zone_width
        if direction > 0
        else entry_price - tp_zone_mult_fallback * zone_width
    )

    if tp_mode == TP_MODE_FVG_MULT:
        # Single-target: zone_mult only. No TP2.
        return zone_mult_tp, "zone_mult", 0.0, "", 0.0

    if tp_mode == TP_MODE_MULTI_TARGET:
        # NB74: TP1 = HVN-derived (partial close 50%), TP2 = zone_mult (remainder 50%).
        # TP2 is CAPPED by the caller via _apply_p95_cap on both tp_price and tp_price_2.
        fvg_tp = zone_mult_tp
        hvn_dir = +1 if direction > 0 else -1
        # Derived radius by default: ``tp_max_distance_usd <= 0`` means
        # "use the reachable TP band at t_ns" instead of a frozen literal.
        _tp_radius = (
            float(tp_max_distance_usd)
            if tp_max_distance_usd > 0.0
            else (float(tp_search_radius_usd)
                  if tp_search_radius_usd is not None
                  else float("inf"))
        )
        hvns = hvn_zones_near(
            vp_set, t_ns, entry_price, hvn_dir,
            max_distance_usd=_tp_radius,
            max_results=nth_hvn_n,
        )
        if len(hvns) >= nth_hvn_n:
            lo, hi, mid = hvns[nth_hvn_n - 1]
            # ── XAU SCALE FIX (2026-10-22): three BTC-era assumptions ──
            #
            # The original code was:
            #
            #     hvn_tp = mid - lvn_skip_usd            # long
            #     if hvn_tp >= lo: hvn_tp = lo - 1.0     # hardcoded $1
            #
            # with the short side mirrored. Two defects, both
            # measured:
            #
            # 1. THE $1.00 IS A BTC CONSTANT. On gold the median HVN
            #    shelf is ~$1.62 wide (post VP re-scale), so a $1.00
            #    offset is ~62% of the shelf — it lands the target
            #    back inside the shelf, defeating the intent.
            #
            # 2. ``lvn_skip_usd`` WAS REDUNDANT. Measured: 0.05 and
            #    0.30 produced byte-identical runs; only 5.0 and 25.0
            #    changed anything. The reason is that the guard
            #    ``hvn_tp >= lo`` fires whenever
            #    ``lvn_skip_usd < shelf_width / 2`` — which is every
            #    sane gold value — so the computed TP was
            #    unconditionally OVERWRITTEN by the ``lo - 1.0``
            #    branch. The knob was dead for its entire useful
            #    range.
            #
            # The fix expresses the actual intent — "target the
            # HVN shelf, stopping just short of its leading edge" —
            # by measuring the offset from the shelf EDGE rather than
            # from the midpoint. ``lvn_skip_usd`` is now live across
            # its whole range, and the correct value is simply "a
            # fraction of the shelf width" (0.30 on a $1.62 median
            # shelf = 18%).
            if lvn_mode == LVN_MODE_SKIP:
                if direction > 0:
                    hvn_tp = float(lo) - float(lvn_skip_usd)
                else:
                    hvn_tp = float(hi) + float(lvn_skip_usd)
            else:
                hvn_tp = float(mid)
            hvn_ref_str = f"multi_hvn_n{nth_hvn_n}"
        else:
            # No HVN shelf inside the search radius. Target a fraction of
            # the fallback leg so TP1 stays a genuine first take-profit
            # rather than collapsing onto TP2. ``tp_hvn_proxy_frac`` was
            # a bare 0.7 literal here before 2026-10-22.
            hvn_tp = entry_price + (
                float(tp_hvn_proxy_frac) * tp_zone_mult_fallback * zone_width
            ) * hvn_dir
            hvn_ref_str = "multi_hvn_proxy"

        # ── TP2: the RUNNER leg ────────────────────────────────────
        # v1 hardcoded this to ``fvg_tp`` (= zone_mult_tp). The three
        # modes below let the runner be anchored on something reachable.
        if tp2_mode == "hvn":
            # The shelf BEYOND TP1, so the two legs stay distinct by
            # construction instead of relying on ``_separate_tp_legs``
            # to pin them apart.
            #
            # The search is anchored on ``hvn_tp`` (TP1), NOT on the
            # entry -- so the shelf list restarts past TP1 and the
            # "next" shelf is index 0. (An earlier draft indexed at
            # ``nth_hvn_n``, which asked for the 4th shelf past TP1
            # and resolved on 2.4% of trades. Off-by-depth: TP1's
            # index is already consumed.)
            _beyond = hvn_zones_near(
                vp_set, t_ns, hvn_tp, hvn_dir,
                max_distance_usd=_tp_radius,
                max_results=tp2_nth_hvn_n if tp2_nth_hvn_n > 0 else 1,
            )
            _idx = (tp2_nth_hvn_n - 1) if tp2_nth_hvn_n > 0 else 0
            if len(_beyond) > _idx:
                _lo2, _hi2, _mid2 = _beyond[_idx]
                fvg_tp = (_lo2 - lvn_skip_usd) if (lvn_mode == LVN_MODE_SKIP and hvn_dir > 0) \
                    else (_hi2 + lvn_skip_usd) if (lvn_mode == LVN_MODE_SKIP) \
                    else _mid2
                tp2_ref = f"multi_hvn_tp2_n{_idx + 1}"
            else:
                # No shelf beyond TP1 inside the radius. Put TP2 at the
                # FAR EDGE of the reachable band rather than at a zone
                # multiple: the caller clamps both legs to the swing cap
                # anyway, and a $72 zone-mult target against a $32 cap
                # is unreachable by construction. This is the leg the
                # user asked for -- every TP inside the 3-day swing.
                fvg_tp = entry_price + _tp_radius * hvn_dir
                tp2_ref = "multi_swing_edge"
        elif tp2_mode == "swing":
            # Place TP2 AT the swing cap. The caller clamps to the same
            # number, so this makes the intent explicit: the runner is
            # the farthest price still inside the 3-day swing.
            fvg_tp = entry_price + float(tp_hvn_proxy_frac) * tp_zone_mult_fallback * zone_width * hvn_dir
            tp2_ref = "swing_proxy"
        else:
            fvg_tp = zone_mult_tp
            tp2_ref = "multi_zone_mult"
        # TP1 = HVN (partial), TP2 = zone_mult (remainder).
        #
        # ``partial_close_pct`` is a real parameter now. It used to be
        # hardcoded to 0.5 here, which made ``PARTIAL_CLOSE_PCT`` in
        # bt.optimal_config a dead knob — sweeping it changed nothing
        # because the value never reached here.
        return hvn_tp, hvn_ref_str, fvg_tp, tp2_ref, partial_close_pct

    raise ValueError(
        f"Unknown tp_mode={tp_mode!r}. "
        f"Valid modes: {TP_MODE_FVG_MULT!r}, {TP_MODE_MULTI_TARGET!r}."
    )


# ─────────────────────────────────────────────────────────────────────
# Backtest engine
# ─────────────────────────────────────────────────────────────────────
def run_vp_sniper_backtest(
    bars: pd.DataFrame,
    zones: List[FvgZone],
    vp_set: VolumeProfileSet,
    *,
    qty_btc: float,
    sl_mode: str,
    tp_mode: str,
    lvn_mode: str,
    sl_buffer_usd: float,
    poc_buffer_usd: float,
    hvn_buffer_usd: float,
    tp_zone_mult_fallback: float,
    tp_max_distance_usd: float,
    lvn_skip_usd: float,
    taker_fee_bps: float,
    entry_mode: str,            # "sniper" | "dual"
    nth_hvn_n: int = 1,
    # NB74 fraction of the position closed at TP1. This was
    # hardcoded to 0.5 inside compute_structural_tp, so
    # ``PARTIAL_CLOSE_PCT`` in bt.optimal_config never reached the
    # engine and sweeping it was a silent no-op.
    partial_close_pct: float = 0.5,
    # Fraction of the ``tp_zone_mult_fallback`` leg used for TP1 when
    # no HVN shelf resolves (the ``multi_hvn_proxy`` branch). Was a
    # bare 0.7 literal; promoted so it is sweepable.
    tp_hvn_proxy_frac: float = 0.7,
    swing_cap: Optional["DailySwingCap"] = None,
    # Regime-aware TP cap (replaces tp_max_distance_usd). The
    # cap object is pre-built via ``build_rolling_p95_from_bars``
    # or live-streamed via ``make_live_rolling_p95`` +
    # ``update_p95``. See ``bt/_engine/rolling_p95.py`` for the
    # math + live coldstart story.
    p95_cap: Optional[RollingP95Cap] = None,
    # NB91: multiplier on the p95 cap. ``None`` = use the cap
    # object's own ``p95_mult``. ``0.0`` disables the cap. This
    # exists so the cap is sweepable WITHOUT rebuilding the whole
    # RollingP95Cap object per sweep cell (added 2026-10-22).
    tp_p95_mult: Optional[float] = None,
    # ── BoS/CHoCH structure gate (2026-10-23) ──────────────────
    # Per-bar market-structure state from ``detect_market_structure``.
    # Required when ``structure_gate=True``; optional otherwise so
    # every existing call site keeps working unchanged.
    structure: Optional["StructureState"] = None,
    structure_gate: bool = False,
    structure_max_age_bars: int = 300,
    # ── SIDE GATE (2026-10-24) ──────────────────────────────────
    # Which sides may open. ``"both"`` (canonical) opens long and
    # short; ``"long"`` opens long positions only.
    #
    # Measured 2026-10-24 on 8 slices under the promoted vol gate
    # (``vol_state_min_range_norm = 3.0``): the short side is gross
    # NEGATIVE in aggregate (short gross -$59.19 vs long gross
    # +$305.41) and carries NEGATIVE alpha (-$12.81/slice vs the
    # long side's +$34.14/slice). It is a pure fee drag: it pays the
    # $0.47 round trip on 379 of 694 legs and contributes -$59 of
    # gross.
    #
    # The gate is applied AFTER the sequence-direction flip (so it
    # sees the FINAL side) and BEFORE the conflict-policy check (so a
    # blocked short cannot consume a same-direction slot or perturb
    # the book).
    trade_sides: str = "both",
    # ── Two-stage structure SCORE filter (2026-10-23) ───────────
    # Stage 1: admit on the SIGN of the score over the last
    # ``structure_score_lookback`` events. NO TIMEOUT -- the previous
    # ``structure_max_age_bars`` gate rejected 6,352-9,387 signals per
    # month for the ABSENCE of a recent break while the slice carried
    # 857 events, so it was measuring the timeout, not structure.
    structure_score_lookback: int = 5,
    structure_score_half_life: float = 1800.0,
    structure_score_min_abs: float = 0.10,
    # Stage 2: scale SL and TP by |score|. Bounds are inclusive; the
    # mapping is monotone and INVERTED for the stop -- stronger
    # conviction means a TIGHTER stop relative to the (also extended)
    # target, which is what keeps R:R from decaying as the target
    # grows. Both legs are floored so a zero score cannot collapse a
    # stop onto the entry.
    sl_scale_min: float = 0.70,
    sl_scale_max: float = 1.30,
    tp_scale_min: float = 0.90,
    tp_scale_max: float = 1.30,
    # ── TP2 anchoring (2026-10-23) ─────────────────────────────
    # What the RUNNER leg targets. v1 hardcoded ``zone_mult``, which
    # at 200x on a $0.36 zone is a $72 target -- unreachable against a
    # ~$20 swing cap. ``"hvn"`` puts both legs on volume levels.
    tp2_mode: str = "zone_mult",
    tp2_nth_hvn_n: int = 0,
    # 2026-10-23: apply the swing ceiling to TP2 as well. Closes the
    # documented "swing cap is TP1-only" gap -- the user requirement
    # is that ALL targets sit inside the 3-day swing.
    tp2_swing_cap: bool = True,
    # ── SEQUENCE-AWARE DIRECTION FLIP (2026-10-23) ───────────────
    #
    # The scalar score is a sufficient statistic that explains nothing:
    # measured on 2026-01, corr(sequence's scalar score, sequence's
    # realised EV) = +0.030 over 60 signatures. The SHAPE of the last
    # 5 BoS/CHoCH events carries information the mean threw away.
    #
    # ``structure_seq_flip_map`` maps a sequence signature (e.g.
    # ``"bCcbb"``) to a direction multiplier (+1 / -1). When a trade's
    # signature is present in the map, the trade's direction is
    # multiplied by it. ``-1`` means "take this sequence the OTHER
    # way" -- i.e. flip the position.
    #
    # ``None`` (default) disables the feature entirely, so every
    # pre-existing call site is byte-identical.
    structure_seq_flip_map: Optional[Dict[str, int]] = None,
    # How many trailing events define the signature. Must match
    # ``structure_score_lookback`` so the signature covers exactly the
    # events the score averaged.
    structure_seq_len: int = 0,
    # ── PER-SIDE SEQUENCE DROP SET (2026-10-24) ────────────────
    # Keys are ``"<side>|<last-event>"`` where side is ``"long"`` or
    # ``"short"`` and last-event is the signature letter of the most
    # recent structure break (``b``=BoS bull, ``B``=BoS bear,
    # ``c``=CHoCH bull, ``C``=CHoCH bear).
    #
    # A key PRESENT in the set means: do not open that trade.
    #
    # WHY THIS IS PER-SIDE: the same event type is good for one side
    # and bad for the other. Measured 2026-10-24 on 8 slices
    # (``runtime/diagnostics/per_side_sequence_pooled.py``):
    #
    #   bucket        FIT gross   HELD gross   verdict
    #   long|b  (bull BoS)    -$47         +$13    NOT consistent
    #   short|b (bull BoS)    -$86         -$18    consistent negative
    #
    # So a bullish break of structure is a reliably BAD setup for a
    # SHORT but not for a long. A pooled filter can never express
    # that.
    #
    # The canonical drop set is ``{"short|b"}`` -- drop shorts whose
    # last structure event was a bullish BoS. That single rule:
    #   * moved the short side from -$54 to +$50 gross WHILE STILL
    #     TRADING 272 legs (a quality win, not a trading-less win),
    #   * improved gross on 7 of 8 slices,
    #   * left both side correlations at ~0.05 (NOT a directional
    #     bet), and
    #   * kept the flat-slice test intact.
    #
    # ``None`` (default) disables the feature entirely.
    structure_seq_drop_set: Optional[set] = None,
    # Scale-invariant SL (2026-10-05). See ``compute_structural_sl`` for why
    # the absolute ``sl_buffer_usd`` is a defect. Precedence when both set:
    # ATR wins, then zone-mult, then fall back to the absolute buffer.
    sl_zone_mult: float = 0.0,
    sl_atr_mult: float = 0.0,
    atr: Optional[np.ndarray] = None,
    # ── SL_MODE_HVN_LVN (2026-10-22, user request) ──────────────
    #
    # When ``sl_mode == 'hvn_lvn'`` the stop is anchored to the Nth
    # HVN shelf on the LOSS side of entry instead of the FVG zone
    # edge. ``sl_nth_hvn_n`` is deliberately INDEPENDENT of the TP's
    # ``nth_hvn_n``: the TP reaches for a distant magnet while the
    # stop respects the nearest structural shelf.
    sl_nth_hvn_n: int = 1,
    # Minimum stop distance from entry, in USD. Enforced by
    # ``_apply_sl_min_floor`` AFTER the anchor level is chosen, so it
    # can only ever LOOSEN a stop that came out too tight. 0.0 = off.
    sl_min_usd: float = 0.0,
    # Maximum stop distance from entry, in USD. The INVERSE of
    # ``sl_min_usd``: enforced by ``_apply_sl_max_ceiling`` AFTER the
    # anchor level and the floor, so it can only ever TIGHTEN a stop
    # that came out too wide. 0.0 = off.
    #
    # 2026-10-24: added because the canonical stop could not be pulled
    # IN. Measured on the 1m zone set, ``sl_mode='hvn_lvn'`` produced
    # a $16.05 / $31.16 / $13.20 median stop (2026-01 / 2026-03 /
    # 2025-05). The cause is the daily VP profile's shelf SPACING
    # (~4 HVN shelves/day, p50 gap $2.54-$4.00, p90 $22-24), not the
    # shelf anchor: "the nearest shelf below entry" is routinely
    # $10-$30 away. The floor could only loosen, so there was no way
    # to reach the user's $2.50 target.
    sl_max_usd: float = 0.0,
    # Search radius for the SL's HVN shelves. 0.0 = DERIVE it from the
    # live caps at ``t_ns`` (see ``_reachable_tp_band_usd``). Separate
    # from ``tp_max_distance_usd`` because the stop's reachable band is
    # a different question from the target's. Never falls back to a
    # module constant — a frozen radius is what leaked BTC-scale USD
    # amounts into the canonical SL search (2026-10-22).
    sl_hvn_max_distance_usd: float = 0.0,
    # Volatility-state gate (2026-10-05 port). ``vol_state_*`` thresholds live
    # on TrendStrategyParams and were implemented ONLY in the bar engine; this
    # engine had zero references, so the composition "VP-MT exits + vol-expansion
    # entries" was not expressible. Ported here with identical semantics.
    #
    # Thresholds are passed explicitly (not read from params) so this engine
    # stays independent of TrendStrategyParams for exit/gate logic, matching
    # how sl_buffer/tp_mode are handled. 0.0 = that measure is not gated.
    vol_state: Optional[Dict[str, np.ndarray]] = None,
    vol_state_min_rv_ratio_30s: float = 0.0,
    vol_state_min_range_norm: float = 0.0,
    vol_state_min_vel_5m: float = 0.0,
    vol_state_min_vel_15m: float = 0.0,
    vol_state_max_rv_ratio_30s: float = 0.0,
    vol_state_max_wick_ratio: float = 0.0,
    # NB86: conflict policy on submit (mirrors src/backtest/ict_backtest.py).
    # This was previously NOT wired into the VP-MT engine — every emitted
    # signal was opened unconditionally. NB86 ports the bar-engine
    # ``conflict_policy`` / ``max_same_dir_open`` knobs, with all three
    # policies (``drop`` / ``replace`` / ``stack``) implemented in the
    # engine and surfaced on ``BacktestResult`` as
    # ``n_dropped_opposite`` / ``n_replaced_opposite`` / ``n_stacked_opposite``
    # / ``n_same_dir_blocked`` counters plus ``conflict_policy_used`` /
    # ``max_same_dir_open_used`` self-describing fields.
    #
    # **Defaults are ``drop`` / ``0`` to preserve the pre-NB86 behaviour of
    # every existing call site.** NB86 (notebook) is the user-requested
    # sweep that tests ``replace`` at ``max_same_dir_open=25``; opt into
    # non-default values explicitly:
    #
    #     from src.strategies.vp_multi_target import (
    #         run_vp_sniper_backtest,
    #         CONFLICT_POLICY_REPLACE,
    #     )
    #     res, _ = run_vp_sniper_backtest(
    #         ..., conflict_policy=CONFLICT_POLICY_REPLACE,
    #         max_same_dir_open=25,
    #     )
    #
    # Semantics:
    #
    #   "drop"    (bar-engine default; live-mirror of SNIPERS_REJECTED_
    #              OPPOSING): a fresh signal in the opposite direction to
    #              an open position is dropped, with ``n_dropped_opposite``
    #              ++. Same-direction double-downs do not arise because
    #              the new signal is always blocked.
    #   "replace" : soft-stop ONE opposite open at the bar's close, then
    #              let the new signal through. Same-direction opens pass
    #              through subject to ``max_same_dir_open`` (0 = uncapped).
    #              This is the user-requested "supercede" semantic for
    #              NB86: a new opposite signal displaces the existing
    #              opposite at the bar's close, then the new signal opens.
    #   "stack"   : let the new signal open regardless, so the book can
    #              hold opposing positions. ``max_same_dir_open`` still
    #              applies. ``stack_cap0`` is the pre-NB86 behaviour
    #              (every emitted signal is opened).
    #
    # The policy runs AFTER vol_state (a failing regime skips; a passing
    # regime then goes through the policy). It is evaluated at both fire
    # points — the immediate-zone path and the sniper path. A failing
    # policy short-circuits the heavy SL/TP compute for that signal.
    conflict_policy: str = "drop",     # NB86: bar-engine default
    max_same_dir_open: int = 0,        # NB86: 0 = uncapped
    # ── D2 (2026-10-22): entry fill model ───────────────────────
    #
    # The entry is a RESTING LIMIT ORDER at the zone edge, not an
    # instant fill at a price the market may never have reached.
    #
    #   "touch"    (canonical) — fill on the first subsequent bar
    #               whose range touches the entry level, at the level.
    #               The order is cancelled if the zone dies first.
    #   "next_open"— fill at the next bar's open (strictest).
    #   "trigger"  — the pre-D2 behaviour: fill at the zone edge on
    #               the trigger bar regardless of price. Kept for
    #               A/B only; it is NOT a defensible execution model.
    entry_fill_model: str = ENTRY_FILL_TOUCH,
    # ── Pessimistic exit fill model (2026-10-22) ───────────────
    #
    #   "mid"        (legacy, optimistic) — a bar touching both SL
    #                  and TP resolves to the TP; gap-throughs fill
    #                  at the level.
    #   "pessimistic"(canonical) — a bar touching both SL and TP
    #                  resolves to the SL; a level the bar gapped
    #                  through fills at the bar's OPEN.
    #   "next_open"  (strictest) — the outcome defers to the next
    #                  bar's open.
    fill_model: str = FILL_MODEL_PESSIMISTIC,
    # NB87: conviction-weighted replace policy. When
    # ``conflict_policy == "replace_conviction"``, a new opposite
    # signal must beat the accumulated same-direction conviction by
    # an asymmetric margin to soft-stop+open. Requires the
    # conviction knobs (alpha / hysteresis / model_path) below.
    # If any of those are unset, falls back to uniform-1.0 scoring
    # (degenerate to a thresholded replace — same shape as
    # ``replace`` but gated on (1+alpha)*count + hysteresis instead
    # of an unconditional soft-stop).
    conviction_alpha: float = 0.0,         # 0.0 = no penalty; 0.15 = need 15% more
    conviction_hysteresis_usd: float = 0.0,  # additive USD floor on required
    conviction_model_path: str = "",       # path to pickled conviction model;
                                           # "" = use uniform-1.0 fallback
    # NB87: per-bar diagnostic callback. Called once per bar that has
    # at least one open position, AFTER live state is updated and
    # AFTER the bar's SL/TP/eod checks resolve. Signature:
    #     on_bar(bar_idx, b_time, b_close, b_high, b_low,
    #            open_trades_snapshot, vol_state_snapshot)
    # ``None`` (the default) means no-op — every existing call site
    # is bit-identical because the callback short-circuits before any
    # work is done. Used by NB87 to emit the per-bar survival
    # diagnostics the conviction model is trained on.
    #
    # ``on_bar_every_k`` (default 1 = every bar) is the downsample
    # knob. A backtest with 1,192 concurrent positions across 2.5M
    # bars produces ~600k-1.5M callback invocations; on a 1s-bar
    # file that is 600k-1.5M rows in the diagnostic, which is 200+
    # MB and slow to build. Sampling every 60th bar (= 1-minute
    # resolution) reduces that to 10k-25k rows while still being
    # fine-grained enough for the survival-curve age buckets.
    # Survival-curve analysis at 60s / 5m / 30m / 2h / 12h / 48h
    # resolution does not benefit from 1s granularity.
    on_bar_callback: Optional[Any] = None,
    on_bar_every_k: int = 1,
    # NB94 (2026-10-21): set ``record_equity_curve=False`` on long files
    # where the per-bar MTM array pushes peak working set past the
    # machine's available RAM. The sweep driver sets this True for
    # ≤3-month files and False for ≥1y. ``BacktestResult.equity_curve``
    # is then empty and downstream metrics fall back to the
    # closed-trade cumulative P&L (see ``runtime.sweeps.cached_sweep``).
    record_equity_curve: bool = True,
    # ── CONTRA CLOSE / HEDGE NETTING (2026-10-24, user request) ──
    #
    # The book can hold opposing positions at once. A long bought at
    # $3,000 and a short sold at $3,050 are a LOCKED SPREAD whose
    # pair P&L is deterministic:
    #
    #     gross = Q * (E_short - E_long)
    #
    # — independent of where price goes next. But the engine closed
    # them separately and each leg kept its own stop, so the pair
    # did not bank the spread: one leg's stop claimed the loss, and
    # both legs paid a round trip to realise a gain that was already
    # in hand.
    #
    # When enabled, and when the price spread between the
    # weighted-average opposing entries exceeds
    # ``contra_close_spread_usd``, the OVERLAPPING quantity is closed
    # SIMULTANEOUSLY at the current market price. The spread is
    # banked, the hedged quantity stops carrying independent stops,
    # and ONE round trip's fee is paid instead of two.
    #
    # The netting runs AFTER the fill block (step 2b) and BEFORE the
    # SL/TP resolution (step 3) on the same bar, so a hedged leg
    # that would have been stopped out is netted first rather than
    # having its stop claim the loss.
    #
    # ``contra_close_spread_usd <= 0.0`` disables the feature
    # entirely (the canonical default), so every pre-existing call
    # site stays bit-identical.
    contra_close: bool = False,
    contra_close_spread_usd: float = 0.0,
    # ── HEDGING-FREQUENCY STATS (2026-10-25) ──────────────────────
    #
    # Diagnostic-only. Counts how often the book is actually hedged
    # and how often a fill lands opposite to an already-open
    # position -- the thing the contra-close feature is SUPPOSED to
    # react to. It does NOT change any trading behaviour: the
    # counters are reads, never gates.
    #
    # It is OFF by default so the canonical path pays nothing. When
    # ON, the weighted-average aggregate is recomputed on the same
    # dirty-flag cadence the netting uses, so the cost is O(book)
    # per BOOK MUTATION, not per bar.
    contra_hedge_stats: bool = False,
) -> Tuple[BacktestResult, Dict[str, Any]]:
    """Run the sniper + volume-profile structural SL/TP backtest.

    Walk the bar array. For each zone inversion (canonical sniper
    trigger), compute the entry price (at the inverted zone edge)
    and the VP-anchored SL/TP.

    For dual mode, also fire immediate entries (zone-edge limit
    orders) on every zone that's still ``live`` at any bar — i.e.
    immediately when the zone fires (not at inversion). This
    catches zones where the inversion never happens but the zone
    is still a valid S/R level.

    Returns (result, diag_dict).
    """
    # ALL timestamps crossing into this engine are int64 NANOSECONDS.
    # The parquet stores datetime64[ms], so converting here (rather
    # than via .astype("int64")) is what keeps b_time in the same
    # units the swing-cap / p95-cap / volume-profile lookups use.
    # Mixing ms and ns silently disables every one of those caps.
    times = to_ns_arr(bars, "time")
    assert_ns(times, "vp_multi_target_engine times")
    open_ = bars["open"].to_numpy(dtype=np.float64)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    n = len(close)

    # Pre-bucket signals
    #
    # CAUSALITY (2026-10-05 fix — lookahead, same class of bug fixed in
    # ``run_ict_backtest`` in September, reintroduced here).
    #
    # This used to gate on the zone's FINAL ``live`` flag:
    #
    #     if z.live and inv_b >= 0 and inv_b < n:
    #
    # ``live`` is a whole-file verdict — it is False if the zone dies at ANY
    # point in the month, including long AFTER the bar we are trading.
    # Measured on 2025-04: of 512 zones that inverted, **all 512 were
    # causally alive at their own inversion bar**, yet only 81 had
    # ``live == True``. The engine was therefore discarding 431 zones (84%)
    # purely because they died later in the file — a textbook look-ahead,
    # and the direct cause of VP-MT's low trade count.
    #
    # The correct test is whether the zone was dead AT OR BEFORE the bar we
    # are about to trade, which is exactly the ``death_bar <= i`` form used
    # in the bar engine. Both buckets are keyed on the bar they fire at, so
    # comparing the death bars against that same bar is causal.
    zones_at_bar: Dict[int, List[FvgZone]] = {}
    zones_inverted_at_bar: Dict[int, List[FvgZone]] = {}
    for z in zones:
        if z.trigger_bar >= 0 and z.trigger_bar < n:
            zones_at_bar.setdefault(int(z.trigger_bar), []).append(z)
        inv_b = int(getattr(z, "inverted_bar", -1))
        if 0 <= inv_b < n and not _zone_dead_by(z, inv_b):
            zones_inverted_at_bar.setdefault(inv_b, []).append(z)

    # NB91-DIAG: how many zones the engine actually bucketed. If this is
    # far below the detector's zone count, the trigger maps never received
    # most zones and no gate can explain the missing fills.
    res_zones_bucketized = sum(len(v) for v in zones_at_bar.values()) \
        + sum(len(v) for v in zones_inverted_at_bar.values())

    open_trades: List[OpenTrade] = []
    closed_trades: List[ClosedTrade] = []
    next_trade_id = 0
    # D2 (2026-10-22): resting entry orders. A signal places an order;
    # the order fills on a later bar when price touches its level.
    pending_orders: List[PendingOrder] = []
    next_order_id = 0

    # ── CONTRA CLOSE aggregate cache (2026-10-24) ────────────────
    #
    # The contra-close trigger needs the weighted-average entry of each
    # side. Those aggregates only change when the BOOK changes, so they
    # are cached and recomputed on MUTATION rather than on every bar.
    #
    # ``_c_aggr_dirty`` is set at every site that changes the book:
    # a fill, a policy soft-stop, a TP1 remainder rewrite, a close in
    # step 3, and the netting itself. A missed site would make the
    # trigger fire on stale numbers, so the sites are enumerated
    # explicitly rather than inferred.
    _c_long_qty = 0.0
    _c_long_sum = 0.0
    _c_short_qty = 0.0
    _c_short_sum = 0.0
    _c_aggr_dirty = True

    # Track zones that have already fired (immediate co-fire)
    # so we don't double-fire both sniper and immediate on the same
    # zone in dual mode.
    zone_already_fired: set = set()

    res = BacktestResult()
    res.n_zones_bucketized = int(res_zones_bucketized)
    # NB86: record the policy actually used. Stamped on the result so
    # downstream report code (e.g. nb86) can be self-describing.
    res.conflict_policy_used = str(conflict_policy).lower().strip()
    res.max_same_dir_open_used = int(max_same_dir_open)
    # 2026-10-22: validate ``sl_mode`` loudly. An unknown mode used to
    # fall through ``compute_structural_sl`` and silently return the
    # zone-edge stop, so a typo in a sweep grid produced a plausible
    # number for a mode that never ran.
    _sl_mode = str(sl_mode).lower().strip()
    if _sl_mode not in SL_MODES:
        raise ValueError(
            f"sl_mode={sl_mode!r} is not one of {SL_MODES}"
        )
    # D2 / pessimistic fill: record the models actually used.
    _entry_fill_model = str(entry_fill_model).lower().strip()
    if _entry_fill_model not in ENTRY_FILL_MODELS:
        raise ValueError(
            f"entry_fill_model={entry_fill_model!r} is not one of "
            f"{ENTRY_FILL_MODELS}"
        )
    _fill_model = str(fill_model).lower().strip()
    if _fill_model not in FILL_MODELS:
        raise ValueError(
            f"fill_model={fill_model!r} is not one of {FILL_MODELS}"
        )
    res.fill_model_used = _fill_model
    res.entry_fill_model_used = _entry_fill_model
    diag: Dict[str, Any] = {}

    # ── BoS/CHoCH structure gate (2026-10-23) ────────────────────
    # Until this was added the VP engine had NO access to market
    # structure at all -- ``grep structure`` on this file returned
    # nothing -- so ``bos_choch_directional_alignment`` (which has
    # existed in ``bt/_engine/market_structure.py`` all along) was
    # unreachable from the strategy path. v1 was a levered long/short
    # pair whose two sides correlated +-0.96 with market direction;
    # an unsigned filter (ER, vol) cannot break that symmetry, but a
    # SIGNED one can. See docs/v2_spec.md 6.1.
    #
    # Preconditions are validated up front so a mis-wired gate fails
    # loudly instead of silently passing every signal.
    if structure_gate:
        if structure is None:
            raise ValueError(
                "structure_gate=True but structure=None. Build the state "
                "with detect_market_structure(...) and pass it in, or set "
                "structure_gate=False. Silently passing every signal would "
                "make the gate a no-op."
            )
        if len(structure.last_break_kind) != len(bars):
            raise ValueError(
                f"structure_gate=True but structure was built on "
                f"{len(structure.last_break_kind)} bars and the engine was "
                f"given {len(bars)}. Misaligned lengths mean every index "
                f"is wrong -- refuse rather than read garbage."
            )

    # NB87b (2026-10-08): allocate the per-bar mark-to-market equity
    # curve. We record one value per input bar, computed at the END of
    # the bar's step 3 (post-resolution) so the open-book snapshot is
    # the same one the ``on_bar_callback`` sees. See ``BacktestResult``
    # for the exact definition.
    #
    # Memory note: ``n=41_233_730`` at float64 is 330 MB per curve.
    # On a long file the curve adds materially to peak working set, and
    # the engine reaches OOM on memory-tight machines (the open-trade
    # list, the VP set, and the zone list already push peak above 4
    # GB on 1y XAUUSD). Set ``record_equity_curve=False`` to skip the
    # allocation entirely; downstream metrics fall back to the
    # closed-trade cumulative P&L (sharper but lag-the-drawdown by
    # ``hold_secs`` per trade).
    if record_equity_curve:
        equity_curve = np.zeros(n, dtype=np.float64)
    else:
        equity_curve = np.zeros(0, dtype=np.float64)
    cum_closed_pnl = 0.0  # running total of closed trade PnL (USD)
    last_mtm = 0.0        # NB87b: last-computed open-book MTM (USD).

    # NB87: load the conviction model if a path was supplied. The model
    # is loaded once per backtest (not per signal). A failure to load
    # is logged and we fall back to the uniform-1.0 scorer so the
    # backtest still runs — important for sweeps that run a model
    # trained on a different corpus.
    conviction_model_obj: Optional[Any] = None
    if conviction_model_path and str(conflict_policy).lower().strip() \
            == "replace_conviction":
        try:
            import pickle as _pickle
            with open(conviction_model_path, "rb") as _fh:
                conviction_model_obj = _pickle.load(_fh)
        except Exception as _e:
            conviction_model_obj = None
    elif str(conflict_policy).lower().strip() == "replace_conviction":
        pass

    import sys as _sys
    import time as _time
    _eng_i = 0
    _eng_t0 = _time.perf_counter()
    for i in range(n):
        # Progress (2026-10-21 diagnostic): report the bar-loop rate and
        # the open-book depth every 250k bars. The open-book depth is the
        # key diagnostic — the engine's per-bar book scan is
        # O(n_bars x n_open), so depth is the multiplier on the total
        # runtime. Writes to stderr only; does not affect results.
        if _eng_i and _eng_i % 250_000 == 0:
            _el = _time.perf_counter() - _eng_t0
            _sys.stderr.write(
                f"[vp_engine] bar={_eng_i}/{n} elapsed={_el:.1f}s "
                f"rate={_eng_i/_el:.0f}/s open={len(open_trades)}\n")
            _sys.stderr.flush()
        _eng_i += 1
        b_open = float(open_[i])
        b_high = float(high[i])
        b_low = float(low[i])
        b_close = float(close[i])
        b_time = int(times[i])

        # NB87b: snapshot closed-trade count so we can attribute the
        # delta to this bar's step 3. Cheap (O(1)) and avoids having
        # to instrument every SL/TP/eod branch.
        _n_closed_before = len(closed_trades)

        # ── 1. New zone triggers (immediate co-fire in dual mode) ──
        if entry_mode in ("dual", "immediate"):
            triggered = zones_at_bar.get(i, [])
            for zone in triggered:
                zid = id(zone)
                if zid in zone_already_fired:
                    res.n_skipped_already_fired += 1
                    continue
                # In "immediate" mode, fire on EVERY zone (don't wait
                # for inversion). In "dual" mode, only fire on zones
                # that haven't inverted yet (let the sniper handle those).
                if entry_mode == "dual" and int(zone.inverted_bar) >= 0:
                    res.n_skipped_dual_inverted += 1
                    continue
                res.n_signals_emitted += 1
                # BoS/CHoCH structure gate (2026-10-23). Signed: it
                # decides WHICH SIDE, which the unsigned ER/vol gates
                # cannot do. See docs/v2_spec.md 6.1.
                _ss_admit, _ss_score = _structure_score_pass(
                    i, zone, structure, vol_state, close,
                    structure_gate,
                    structure_score_min_abs,
                    structure_score_lookback,
                    structure_score_half_life, res)
                if not _ss_admit:
                    res.n_dropped_structure += 1
                    continue
                # Vol-expansion gate (2026-10-05 port): skip NEW entries in an
                # unfavourable volatility regime. Never closes an open trade.
                if not _vol_state_pass(
                    i, vol_state, vol_state_min_rv_ratio_30s,
                    vol_state_min_range_norm, vol_state_min_vel_5m,
                    vol_state_min_vel_15m, vol_state_max_rv_ratio_30s,
                    vol_state_max_wick_ratio,
                ):
                    res.n_dropped_vol_state += 1
                    continue
                # NB91/NB94: the p95 cap is a **TP-ONLY clamp**. It must
                # never gate entries — only tighten TP1/TP2. An earlier
                # version skipped the trade outright during the cap's
                # warmup window, which coupled a TP normalizer to entry
                # admission and made the cap a silent trade-killer.
                #
                # During warmup the cap simply does not apply:
                # ``RollingP95Cap.cap_for`` returns ``inf`` while
                # ``seed_constant_usd == 0`` (backtest mode), so the
                # uncapped TP is used and the trade proceeds normally.
                #
                # NB: the warmup gate IS still honoured for the LIVE
                # coldstart path, where ``seed_constant_usd`` provides a
                # usable cap and the live VPS is expected to skip the
                # first bars. That decision belongs to the caller
                # (``skip_trades_during_warmup``), not to the TP clamp.
                if (p95_cap is not None
                        and p95_cap.skip_trades_during_warmup
                        and p95_cap.seed_constant_usd > 0.0
                        and not p95_cap.is_warmup_complete(b_time)):
                    zone_already_fired.add(zid)
                    res.n_dropped_p95_warmup += 1
                    continue
                zone_already_fired.add(zid)
                # Fire immediate entry at zone edge (canonical
                # continuation path: bull FVG → long at zone_low,
                # bear FVG → short at zone_high).
                if int(zone.direction) > 0:
                    entry_price = float(zone.zone_low)
                    direction = +1
                else:
                    entry_price = float(zone.zone_high)
                    direction = -1
                # ── SEQUENCE-AWARE DIRECTION FLIP (2026-10-23) ──────
                # If the trailing-event signature is in the flip map,
                # reverse the trade. A reversed trade enters at the
                # OPPOSITE zone edge (a long becomes a short at the
                # zone high), because that is where the flipped
                # position would actually be filled. The zone's own
                # direction is unchanged -- only the trade's side is.
                if structure_seq_flip_map:
                    _sig = _sequence_signature(
                        structure, i,
                        structure_seq_len or structure_score_lookback,
                    )
                    _mult = structure_seq_flip_map.get(_sig, 0)
                    if _mult < 0:
                        direction = -direction
                        if int(zone.direction) > 0:
                            entry_price = float(zone.zone_high)
                        else:
                            entry_price = float(zone.zone_low)
                        res.n_structure_seq_flipped += 1
                # ── SIDE GATE (2026-10-24) ──────────────────────────
                # ``trade_sides`` restricts which sides may open. The
                # canonical value is ``"both"``. ``"long"`` opens only
                # long positions.
                #
                # WHY this exists (measured 2026-10-24, 8 slices under
                # the promoted vol gate): the SHORT side is gross
                # NEGATIVE in aggregate (short gross -$59.19 vs long
                # gross +$305.41) and carries NEGATIVE alpha
                # (-$12.81/slice vs the long side's +$34.14/slice).
                # It is a pure fee drag.
                #
                # WHY it is implemented here (and not as a post-hoc
                # filter): the gate must sit AFTER the sequence flip so
                # it sees the FINAL direction, and it must sit BEFORE
                # the conflict-policy check so a blocked short does not
                # consume a same-direction slot or perturb the book.
                #
                # Under ``conflict_policy="stack"`` the opposite side
                # never blocks admission and ``max_same_dir_open`` is
                # PER-DIRECTION, so removing shorts cannot change which
                # longs are admitted. That was verified byte-exact
                # against a post-hoc filter on all 8 slices.
                if trade_sides == "long" and direction < 0:
                    res.n_dropped_sides += 1
                    continue
                # ── PER-SIDE SEQUENCE DROP SET (2026-10-24) ─────────
                # Drop the trade if its (side, last structure event)
                # pair is in the drop set. See the ``structure_seq_drop_set``
                # docstring for the measurement.
                #
                # It is applied AFTER the sequence flip (so it sees the
                # FINAL side) and AFTER the side gate, and BEFORE the
                # conflict-policy check.
                if structure_seq_drop_set:
                    _side_lbl = "long" if direction > 0 else "short"
                    _last_ev = _sequence_signature(
                        structure, i,
                        structure_seq_len or structure_score_lookback,
                    )[-1:]
                    if f"{_side_lbl}|{_last_ev}" in structure_seq_drop_set:
                        res.n_dropped_seq_side += 1
                        continue
                # NB86: conflict_policy on submit (mirrors bar engine).
                # Runs AFTER vol_state and AFTER direction is known. The
                # decision can short-circuit the heavy SL/TP compute, so
                # we do it here rather than at the very top.
                if not _conflict_policy_pass(
                    open_trades, direction, b_close, i, b_time,
                    conflict_policy, max_same_dir_open,
                    qty_btc, taker_fee_bps, closed_trades, res,
                    # NB87: conviction knobs.
                    conviction_alpha=conviction_alpha,
                    conviction_hysteresis_usd=conviction_hysteresis_usd,
                    conviction_model=conviction_model_obj,
                ):
                    res.n_dropped_policy += 1
                    # CONTRA CLOSE: the policy may have soft-stopped
                    # an opposite open (the 'replace' policies),
                    # which changed the book.
                    _c_aggr_dirty = True
                    continue
                # CONTRA CLOSE: the policy may have soft-stopped an
                # opposite open (the 'replace' policies), which
                # changed the book.
                _c_aggr_dirty = True
                # D2 (2026-10-22): the entry is a RESTING LIMIT ORDER at
                # the zone edge, not an instant fill. The SL/TP geometry
                # is computed at FILL time so the swing / p95 caps see
                # the regime at entry, not at signal time.
                zone_width = float(zone.zone_high) - float(zone.zone_low)
                next_order_id += 1
                pending_orders.append(PendingOrder(
                    order_id=next_order_id,
                    zone=zone,
                    direction=direction,
                    entry_price=entry_price,
                    placed_bar=i,
                    placed_time_ns=b_time,
                    source="immediate",
                    fvg_direction=int(zone.direction),
                    zone_width=zone_width,
                    death_bar=_zone_death_bar(zone),
                    struct_verdict=_ss_score,
                ))
                res.n_orders_placed += 1

        # ── 2. Zone inversions → sniper entries ────────────────────
        inversions = zones_inverted_at_bar.get(i, [])
        for zone in inversions:
            zid = id(zone)
            if zid in zone_already_fired:
                # Immediate already fired on this zone; skip sniper
                res.n_skipped_already_fired += 1
                continue
            zone_already_fired.add(zid)
            # Vol-expansion gate (2026-10-05 port) — same semantics as the
            # immediate path above. Note this marks the zone as fired even
            # when gated, matching the bar engine, where a gated-away signal
            # is consumed rather than deferred.
            if not _vol_state_pass(
                i, vol_state, vol_state_min_rv_ratio_30s,
                vol_state_min_range_norm, vol_state_min_vel_5m,
                vol_state_min_vel_15m, vol_state_max_rv_ratio_30s,
                vol_state_max_wick_ratio,
            ):
                res.n_dropped_vol_state += 1
                continue
            # NB91/NB94: the p95 cap is a **TP-ONLY clamp** — it must
            # never gate entries. Same reasoning as the immediate path
            # above: during warmup the cap simply does not apply
            # (``cap_for`` returns ``inf`` in backtest mode), so the
            # trade proceeds with an uncapped TP. The warmup skip is
            # honoured only for the live coldstart path, where a seed
            # constant makes a usable cap available.
            if (p95_cap is not None
                    and p95_cap.skip_trades_during_warmup
                    and p95_cap.seed_constant_usd > 0.0
                    and not p95_cap.is_warmup_complete(b_time)):
                res.n_dropped_p95_warmup_sniper += 1
                continue
            res.n_inversions_detected += 1
            res.n_signals_emitted += 1
            # BoS/CHoCH structure gate (2026-10-23) -- same rule on
            # the sniper path. ``direction`` is not resolved until a
            # few lines below, so the gate is applied after the
            # direction is known (see the second ``_structure_pass``
            # call in this branch).
            # Sniper entry: at the inverted zone edge (price committed
            # through the zone in the opposite direction).
            if int(zone.direction) > 0:
                # Bull FVG inverted → short on top, then LONG on
                # the retest through the bottom. With
                # continuation mode: long at zone_low.
                entry_price = float(zone.zone_low)
                direction = +1
            else:
                # Bear FVG inverted → long on bottom, then SHORT
                # on the retest through the top. With continuation
                # mode: short at zone_high.
                entry_price = float(zone.zone_high)
                direction = -1
            # BoS/CHoCH structure gate (2026-10-23), sniper path.
            # Applied HERE because ``direction`` is only known after the
            # branch above. Same skip-don't-invert semantics.
            _ss_admit, _ss_score = _structure_score_pass(
                i, zone, structure, vol_state, close,
                structure_gate,
                structure_score_min_abs,
                structure_score_lookback,
                structure_score_half_life, res)
            if not _ss_admit:
                res.n_dropped_structure += 1
                continue
            # SEQUENCE-AWARE DIRECTION FLIP (2026-10-23) -- sniper path.
            # Same rule as the immediate path: reverse the side and
            # enter at the opposite zone edge.
            if structure_seq_flip_map:
                _sig = _sequence_signature(
                    structure, i,
                    structure_seq_len or structure_score_lookback,
                )
                _mult = structure_seq_flip_map.get(_sig, 0)
                if _mult < 0:
                    direction = -direction
                    if int(zone.direction) > 0:
                        entry_price = float(zone.zone_high)
                    else:
                        entry_price = float(zone.zone_low)
                    res.n_structure_seq_flipped += 1
            # ── SIDE GATE (2026-10-24) -- sniper path ─────────────
            # Same rule as the immediate path: applied AFTER the
            # sequence flip (so it sees the final direction) and BEFORE
            # the conflict-policy check (so a blocked short does not
            # perturb the book).
            if trade_sides == "long" and direction < 0:
                res.n_dropped_sides += 1
                continue
            # ── PER-SIDE SEQUENCE DROP SET (2026-10-24) ───────────
            # Sniper path -- same rule as the immediate path.
            if structure_seq_drop_set:
                _side_lbl = "long" if direction > 0 else "short"
                _last_ev = _sequence_signature(
                    structure, i,
                    structure_seq_len or structure_score_lookback,
                )[-1:]
                if f"{_side_lbl}|{_last_ev}" in structure_seq_drop_set:
                    res.n_dropped_seq_side += 1
                    continue
            # NB86: conflict_policy on submit (mirrors bar engine).
            if not _conflict_policy_pass(
                open_trades, direction, b_close, i, b_time,
                conflict_policy, max_same_dir_open,
                qty_btc, taker_fee_bps, closed_trades, res,
                # NB87: conviction knobs.
                conviction_alpha=conviction_alpha,
                conviction_hysteresis_usd=conviction_hysteresis_usd,
                conviction_model=conviction_model_obj,
            ):
                res.n_dropped_policy += 1
                # CONTRA CLOSE: the policy may have soft-stopped an
                # opposite open (the 'replace' policies), which
                # changed the book.
                _c_aggr_dirty = True
                continue
            # CONTRA CLOSE: the policy may have soft-stopped an
            # opposite open (the 'replace' policies), which changed
            # the book.
            _c_aggr_dirty = True
            zone_width = float(zone.zone_high) - float(zone.zone_low)
            # D2 (2026-10-22): resting limit order at the zone edge.
            # The SL/TP geometry is computed at FILL time.
            next_order_id += 1
            pending_orders.append(PendingOrder(
                order_id=next_order_id,
                zone=zone,
                direction=direction,
                entry_price=entry_price,
                placed_bar=i,
                placed_time_ns=b_time,
                source="sniper",
                fvg_direction=int(zone.direction),
                zone_width=zone_width,
                death_bar=_zone_death_bar(zone),
                struct_verdict=_ss_score,
            ))
            res.n_orders_placed += 1

        # ── 2b. Fill resting entry orders (D2) ─────────────────────
        #
        # A resting order fills on the first LATER bar whose range
        # touches its level. Orders whose zone died first are
        # cancelled — the trade never existed.
        if pending_orders:
            _still_pending: List[PendingOrder] = []
            for po in pending_orders:
                # The zone died before we filled → cancel.
                if po.death_bar and po.death_bar <= i:
                    po.cancelled = True
                    po.cancel_reason = "zone_death"
                    res.n_orders_cancelled += 1
                    res.n_orders_cancelled_zone_death += 1
                    continue
                if entry_fill_model == ENTRY_FILL_TRIGGER:
                    # Legacy A/B path: fill at the placement bar.
                    _fill = True
                elif entry_fill_model == ENTRY_FILL_NEXT_OPEN:
                    # Fill at the next bar's open, if the zone lives.
                    _fill = (i > po.placed_bar)
                else:
                    # ENTRY_FILL_TOUCH (canonical): fill when the bar's
                    # range touches the limit level. The placement bar
                    # itself is EXCLUDED — the order rests FROM the
                    # placement bar, so the fill can only happen on a
                    # strictly later bar. (Pre-D2 the engine credited a
                    # fill on the placement bar at a price the market
                    # had not reached.)
                    _fill = (i > po.placed_bar) and _order_touched(
                        b_low=b_low, b_high=b_high,
                        entry_price=po.entry_price,
                        direction=po.direction,
                    )
                if not _fill:
                    _still_pending.append(po)
                    continue
                # ── FILLED: compute the SL/TP geometry now ──
                _e = po.entry_price
                _d = po.direction
                _zw = po.zone_width
                _atr_v = (float(atr[i])
                          if (atr is not None and i < len(atr)) else 0.0)
                # The reachable TP band at this bar, measured ONCE and
                # shared by the SL and TP shelf searches. Both are
                # clamped by the same caps a few lines below, so both
                # should search within the same band — deriving it here
                # is what replaces the frozen ``tp_max_distance_usd``
                # literal (and the $1500 module constant that leaked
                # into the SL search). O(log n) per fill.
                _tp_band = _reachable_tp_band_usd(
                    p95_cap=p95_cap, swing_cap=swing_cap,
                    t_ns=b_time, tp_p95_mult=tp_p95_mult,
                )
                sl_price, sl_ref = compute_structural_sl(
                    entry_price=_e, direction=_d,
                    zone_low=po.zone.zone_low, zone_high=po.zone.zone_high,
                    vp_set=vp_set, t_ns=b_time,
                    sl_mode=sl_mode, sl_buffer_usd=sl_buffer_usd,
                    poc_buffer_usd=poc_buffer_usd,
                    hvn_buffer_usd=hvn_buffer_usd,
                    zone_width=_zw,
                    sl_zone_mult=sl_zone_mult, sl_atr_mult=sl_atr_mult,
                    atr_value=_atr_v,
                    sl_nth_hvn_n=sl_nth_hvn_n,
                    sl_min_usd=sl_min_usd,
                    sl_max_usd=sl_max_usd,
                    sl_hvn_max_distance_usd=sl_hvn_max_distance_usd,
                    sl_search_radius_usd=_tp_band,
                )
                tp_price, tp_ref, tp_price_2, tp_ref_2, partial_pct = \
                    compute_structural_tp(
                        entry_price=_e, direction=_d,
                        zone_low=po.zone.zone_low,
                        zone_high=po.zone.zone_high,
                        zone_width=_zw, vp_set=vp_set, t_ns=b_time,
                        tp_mode=tp_mode,
                        tp_max_distance_usd=tp_max_distance_usd,
                        tp_zone_mult_fallback=tp_zone_mult_fallback,
                        lvn_mode=lvn_mode, lvn_skip_usd=lvn_skip_usd,
                        nth_hvn_n=nth_hvn_n,
                        partial_close_pct=partial_close_pct,
                        tp_hvn_proxy_frac=tp_hvn_proxy_frac,
                        tp_search_radius_usd=_tp_band,
                        tp2_mode=tp2_mode,
                        tp2_nth_hvn_n=tp2_nth_hvn_n,
                    )
                tp_price, tp_ref = _apply_swing_cap(
                    tp_price=tp_price, tp_ref=tp_ref,
                    entry_price=_e, direction=_d,
                    swing_cap=swing_cap, t_ns=b_time,
                )
                tp_price, tp_ref = _apply_p95_cap(
                    tp_price=tp_price, tp_ref=tp_ref,
                    entry_price=_e, direction=_d,
                    p95_cap=p95_cap, t_ns=b_time,
                    tp_p95_mult=tp_p95_mult,
                )
                tp_price_2, tp_ref_2 = _apply_p95_cap(
                    tp_price=tp_price_2, tp_ref=tp_ref_2,
                    entry_price=_e, direction=_d,
                    p95_cap=p95_cap, t_ns=b_time,
                    tp_p95_mult=tp_p95_mult,
                )
                # 2026-10-23: TP2 is now swing-capped too.
                #
                # This was a documented KNOWN GAP: "_apply_swing_cap is
                # applied to TP1 only -- TP2 is capped by p95 alone".
                # The stated reason for leaving it was that a second
                # ceiling would collapse the runner onto TP1. That is
                # what ``_separate_tp_legs`` is for, and it runs AFTER
                # this block, so the legs are re-pinned regardless.
                #
                # The user requirement is explicit: "all TPs should be
                # within 3 days swing". A runner that can sit $2,693
                # out is not a target. Gated on ``tp2_swing_cap``
                # so the pre-2026-10-23 behaviour stays reproducible
                # for A/B.
                if tp2_swing_cap:
                    tp_price_2, tp_ref_2 = _apply_swing_cap(
                        tp_price=tp_price_2, tp_ref=tp_ref_2,
                        entry_price=_e, direction=_d,
                        swing_cap=swing_cap, t_ns=b_time,
                    )
                tp_price, tp_price_2, tp_ref, tp_ref_2 = _separate_tp_legs(
                    tp1=tp_price, tp2=tp_price_2,
                    tp1_ref=tp_ref, tp2_ref=tp_ref_2,
                    entry_price=_e, direction=_d,
                )
                # ── STAGE 2: scale SL/TP by |score| ──────────────
                # Only reached by trades Stage 1 admitted, so the
                # score is guaranteed sign-aligned with ``_d``. The
                # TARGET is scaled in the trade's direction and the
                # STOP is scaled INVERSELY (strong conviction ->
                # tighter stop, longer target), so R:R improves with
                # conviction rather than decaying as the target grows.
                # Neutral (1.0) when the gate is off, which keeps the
                # pre-gate numbers reproducible.
                if structure_gate and _ss_score != 0.0:
                    _mag = _d * _ss_score       # >= 0 for admitted
                    _sl_mult = _score_scale(_mag, sl_scale_max, sl_scale_min)
                    _tp_mult = _score_scale(_mag, tp_scale_min, tp_scale_max)
                    sl_price = _e + (sl_price - _e) * _sl_mult
                    tp_price = _e + (tp_price - _e) * _tp_mult
                    if tp_price_2 > 0.0:
                        tp_price_2 = _e + (tp_price_2 - _e) * _tp_mult
                    sl_ref = f"{sl_ref}+score"
                    tp_ref = f"{tp_ref}+score"
                # The conflict policy is re-checked at FILL time: the
                # book may have changed between placement and fill.
                if not _conflict_policy_pass(
                    open_trades, _d, b_close, i, b_time,
                    conflict_policy, max_same_dir_open,
                    qty_btc, taker_fee_bps, closed_trades, res,
                    conviction_alpha=conviction_alpha,
                    conviction_hysteresis_usd=conviction_hysteresis_usd,
                    conviction_model=conviction_model_obj,
                ):
                    res.n_dropped_policy_at_fill += 1
                    po.cancelled = True
                    po.cancel_reason = "policy_at_fill"
                    res.n_orders_cancelled += 1
                    # CONTRA CLOSE: the policy may have soft-stopped
                    # an opposite open, which changed the book.
                    _c_aggr_dirty = True
                    continue
                # CONTRA CLOSE: the policy may have soft-stopped an
                # opposite open, which changed the book.
                _c_aggr_dirty = True
                # HEDGING STATS (2026-10-25): count fills that landed
                # while an OPPOSITE position was already open. This is
                # the "incoming opposite position" event the
                # contra-close feature is conceptually supposed to
                # react to.
                if contra_hedge_stats:
                    res.n_fills_total += 1
                    _has_opp = False
                    for _ot in open_trades:
                        if _ot.direction != _d:
                            _has_opp = True
                            break
                    if _has_opp:
                        res.n_fills_opposite_open += 1
                next_trade_id += 1
                open_trades.append(OpenTrade(
                    trade_id=next_trade_id,
                    parent_zone_id=id(po.zone),
                    entry_bar=i,
                    entry_time_ns=b_time,                    entry_price=_e,
                    direction=_d,
                    stop_price=float(sl_price),
                    target_price=float(tp_price),
                    fvg_direction=po.fvg_direction,
                    source=po.source,
                    sl_mode=sl_mode,
                    tp_mode=tp_mode,
                    sl_struct_ref=sl_ref,
                    tp_struct_ref=tp_ref,
                    zone_width=_zw,
                    zone_low=float(po.zone.zone_low),
                    zone_high=float(po.zone.zone_high),
                    target_price_2=float(tp_price_2) if tp_price_2 > 0 else 0.0,
                    tp_struct_ref_2=tp_ref_2,
                    partial_close_pct=partial_pct,
                    # Initial risk/reward, captured BEFORE the stop
                    # moves to breakeven.
                    initial_sl_distance_usd=abs(_e - float(sl_price)),
                    initial_tp_distance_usd=abs(float(tp_price) - _e),
                    struct_verdict=po.struct_verdict,
                    # The SIGNAL bar, not the fill bar. Entry-side
                    # gates are evaluated where the decision is made.
                    signal_bar=int(po.placed_bar),
                ))
                po.filled = True
                res.n_orders_filled += 1
                # CONTRA CLOSE: the book changed, so the cached
                # weighted-average entries are stale.
                _c_aggr_dirty = True
                if po.source == "immediate":
                    res.n_immediate_filled += 1
                else:
                    res.n_sniper_filled += 1
            pending_orders = _still_pending

        # ── 2c. CONTRA CLOSE / HEDGE NETTING (2026-10-24) ──────────
        #
        # The book can hold opposing positions. Those legs are a
        # LOCKED SPREAD: the pair's gross P&L is
        #
        #     Q * (E_short - E_long)
        #
        # regardless of where price goes. Left alone, the engine
        # closes them independently, so one leg's stop fires while its
        # counterparty sits on an equal-and-opposite gain that is
        # never taken — the hedge is broken for a LOSS — and the
        # surviving leg is then a naked directional bet.
        #
        # Here the overlapping quantity is netted out SIMULTANEOUSLY
        # at one market price, banking the locked spread and removing
        # the naked residual. This runs BEFORE step 3 (SL/TP
        # resolution) on the same bar, so a hedged leg that was about
        # to be stopped out is netted instead of stopped.
        #
        # PERF (2026-10-24): the aggregate scan is O(book). The
        # weighted-average entries only change when the BOOK changes,
        # so the aggregates are CACHED and recomputed on MUTATION
        # (a fill, a close, a TP1 remainder rewrite, a policy
        # soft-stop, or a netting) rather than on every bar. The
        # canonical 1-month slice mutates the book ~10k times across
        # 1.38M bars, so the scan runs ~100x less often than a naive
        # per-bar version would.
        # ── HEDGING STATS (2026-10-25) ────────────────────────────
        #
        # Record how often the book is hedged and how wide the
        # weighted-average entry spread gets while it is. This runs
        # REGARDLESS of whether the netting is enabled, and it is a
        # pure READ -- it cannot change any outcome.
        #
        # ``hedged_spread_max_usd`` is the decisive number: if it sits
        # BELOW ``contra_close_spread_usd``, the feature can never fire
        # on this corpus at all, no matter how many hedged bars there
        # are.
        #
        # It is a SEPARATE block from the netting below, deliberately:
        # the netting block ends with a ``continue`` on the non-firing
        # path, and folding the stats into it would make the stats
        # skip the SL/TP resolution step.
        if contra_hedge_stats and open_trades:
            if _c_aggr_dirty:
                _c_long_qty = 0.0
                _c_long_sum = 0.0
                _c_short_qty = 0.0
                _c_short_sum = 0.0
                for _ct in open_trades:
                    _cq = _ct.qty if _ct.qty > 0.0 else qty_btc
                    if _ct.direction > 0:
                        _c_long_qty += _cq
                        _c_long_sum += _cq * float(_ct.entry_price)
                    else:
                        _c_short_qty += _cq
                        _c_short_sum += _cq * float(_ct.entry_price)
                _c_aggr_dirty = False
            if _c_long_qty > 0.0 and _c_short_qty > 0.0:
                res.n_bars_both_sides_open += 1
                res.hedged_spread_bars += 1
                _c_spread_stat = _contra_spread_usd(
                    _c_long_qty, _c_long_sum,
                    _c_short_qty, _c_short_sum)
                if _c_spread_stat > res.hedged_spread_max_usd:
                    res.hedged_spread_max_usd = _c_spread_stat

        if contra_close and contra_close_spread_usd > 0.0 and open_trades:
            if _c_aggr_dirty:
                _c_long_qty = 0.0
                _c_long_sum = 0.0
                _c_short_qty = 0.0
                _c_short_sum = 0.0
                for _ct in open_trades:
                    _cq = _ct.qty if _ct.qty > 0.0 else qty_btc
                    if _ct.direction > 0:
                        _c_long_qty += _cq
                        _c_long_sum += _cq * float(_ct.entry_price)
                    else:
                        _c_short_qty += _cq
                        _c_short_sum += _cq * float(_ct.entry_price)
                _c_aggr_dirty = False
            _c_spread = _contra_spread_usd(
                _c_long_qty, _c_long_sum, _c_short_qty, _c_short_sum)
            # Only the PROFITABLE lock is netted: the shorts must have
            # entered ABOVE the longs. A negative spread is a locked
            # LOSS, and the hedged book is already delta-neutral, so
            # there is nothing to gain by breaking it.
            if _c_spread >= contra_close_spread_usd:
                _c_match = (_c_long_qty if _c_long_qty < _c_short_qty
                            else _c_short_qty)
                if _c_match > 0.0:
                    _c_price = _contra_close_price(
                        b_open=b_open, b_low=b_low, b_high=b_high,
                        b_close=b_close, fill_model=fill_model)
                    # ── Net each side pro-rata ─────────────────────
                    #
                    # Each side is reduced by its share of the matched
                    # quantity:
                    #
                    #     long_share  = M / Q_long
                    #     short_share = M / Q_short
                    #
                    # so BOTH sides end up with the same netted
                    # quantity M. A leg that is fully netted is
                    # closed; a leg that is partially netted keeps
                    # its residual with the SAME target geometry.
                    _c_long_ratio = _c_match / _c_long_qty
                    _c_short_ratio = _c_match / _c_short_qty
                    _c_new_open: List[OpenTrade] = []
                    _c_gross = 0.0
                    _c_fees = 0.0
                    _c_legs = 0
                    for _ct in open_trades:
                        _cq = _ct.qty if _ct.qty > 0.0 else qty_btc
                        if _ct.direction > 0:
                            _c_ratio = _c_long_ratio
                        else:
                            _c_ratio = _c_short_ratio
                        _c_netted = _cq * _c_ratio
                        _c_resid = _cq - _c_netted
                        if _c_netted <= 1e-12:
                            # Nothing netted off this leg.
                            _c_new_open.append(_ct)
                            continue
                        _c_entry = float(_ct.entry_price)
                        _c_dir = int(_ct.direction)
                        _c_leg_gross = (_c_price - _c_entry) * _c_dir \
                            * _c_netted
                        _c_gross += _c_leg_gross
                        # ONE round trip per LEG, charged with that
                        # leg's own entry price — the engine's
                        # existing convention, unchanged.
                        #
                        # This is deliberately NOT "one round trip per
                        # pair". On a hedging account each leg is a
                        # separate position with its own entry spread
                        # and its own exit spread, so closing both pays
                        # two round trips. The status quo ALSO pays two
                        # round trips (each leg eventually closes), so
                        # the netting does not change the fee count —
                        # its benefit is realising the locked spread
                        # deterministically and removing the naked
                        # residual. Charging one would be a $0.47
                        # per-trade windfall that flatters the
                        # feature.
                        _c_fee = abs(_c_price + _c_entry) * 0.5 \
                            * _c_netted * (taker_fee_bps / 10000.0) * 2.0
                        _c_fees += _c_fee
                        _c_legs += 1
                        _c_hold = (b_time - _ct.entry_time_ns) / 1e9
                        _c_sl_dist = abs(_c_entry - float(_ct.stop_price))
                        _c_tp_dist = abs(
                            float(_ct.target_price) - _c_entry)
                        # The NETTED SLICE is always booked as a closed
                        # trade -- even when the leg keeps a residual.
                        #
                        # BUG FIX (2026-10-24): the original code only
                        # appended a ClosedTrade when the leg was FULLY
                        # netted. A partially-netted leg had its qty
                        # shrunk and stayed open, so the netted slice's
                        # gross P&L was added to the running total but
                        # never appeared in ``res.trades`` -- the
                        # realised spread was invisible to every
                        # per-trade diagnostic, while its FEE was
                        # still charged.
                        closed_trades.append(ClosedTrade(
                            trade_id=_ct.trade_id,
                            parent_zone_id=_ct.parent_zone_id,
                            entry_bar=_ct.entry_bar, exit_bar=i,
                            entry_time_ns=_ct.entry_time_ns,
                            exit_time_ns=b_time,
                            entry_price=_c_entry,
                            exit_price=_c_price,
                            direction=_c_dir,
                            stop_price=_ct.stop_price,
                            target_price=_ct.target_price,
                            exit_reason="contra_close",
                            hold_secs=_c_hold,
                            pnl_gross_usd=_c_leg_gross,
                            fee_usd=_c_fee,
                            pnl_net_usd=_c_leg_gross - _c_fee,
                            qty_btc=_c_netted,
                            fvg_direction=_ct.fvg_direction,
                            source=_ct.source,
                            sl_mode=_ct.sl_mode, tp_mode=_ct.tp_mode,
                            sl_struct_ref=_ct.sl_struct_ref,
                            tp_struct_ref=_ct.tp_struct_ref,
                            sl_distance_usd=_c_sl_dist,
                            tp_distance_usd=_c_tp_dist,
                            initial_sl_distance_usd=(
                                _ct.initial_sl_distance_usd),
                            initial_tp_distance_usd=(
                                _ct.initial_tp_distance_usd),
                            entry_poc_dist_usd=0.0,
                            zone_width=_ct.zone_width,
                            zone_low=_ct.zone_low,
                            zone_high=_ct.zone_high,
                            target_price_2=_ct.target_price_2,
                            tp_struct_ref_2=_ct.tp_struct_ref_2,
                            partial_close_pct=_ct.partial_close_pct,
                            struct_verdict=_ct.struct_verdict,
                            # The SIGNAL bar, not the fill bar. Entry-side
                            # gates are evaluated where the decision is
                            # made, and the ledger keys legs by their
                            # SIGNAL time.
                            #
                            # BUG FIX (2026-10-25): this field was
                            # OMITTED, so it defaulted to 0 and every
                            # netted leg claimed to have signalled at
                            # the very START of the corpus. On the wide
                            # book (vol gate 1.0) the netting fires on
                            # 17,211 legs, so the ledger's signal-time
                            # index was corrupted for 34% of the book:
                            # 17,200 legs showed a pre-fill window of
                            # up to 41M bars (the whole corpus) instead
                            # of a few thousand.
                            signal_bar=_ct.signal_bar,
                        ))
                        if _c_resid > 1e-12:
                            # Partially netted: the RESIDUAL keeps its
                            # full multi-target geometry. A pre-TP1
                            # trade (``partial_close_pct > 0``) stays
                            # a pre-TP1 trade -- it keeps TP1, TP2 and
                            # its original stop. A post-TP1 remainder
                            # leg stays a remainder leg (breakeven
                            # stop, TP2 target) at reduced size.
                            _ct.qty = _c_resid
                            _c_new_open.append(_ct)
                    open_trades = _c_new_open
                    _c_aggr_dirty = True
                    res.n_contra_close_events += 1
                    res.n_contra_legs_closed += _c_legs
                    res.n_contra_qty_netted += _c_match
                    res.contra_gross_usd += _c_gross
                    res.contra_fees_usd += _c_fees
                    if _c_spread > res.contra_spread_usd_max:
                        res.contra_spread_usd_max = _c_spread

        # ── 3. SL / TP / eod checks ────────────────────────────────
        # NB87: live-state update FIRST, before any SL/TP resolution
        # for this bar. The live state captures the most-recent bar's
        # excursion so the conviction model can use the
        # most-up-to-date value. TP1-hit is sticky: once True, stays
        # True (the partial close at TP1 already happened in a prior
        # bar; we just need to keep the flag for the model). The
        # ``on_bar_callback`` (when supplied) is fired after this
        # update and after the SL/TP/eod checks, so it sees the
        # post-resolution state.
        #
        # PERF P2 (2026-10-22): this is the O(open_book) scan that
        # dominated the profile (197.8M iterations on the 1-month
        # canonical slice). It is only needed when something CONSUMES
        # the live state:
        #
        #   * the conviction model (NB87) reads mfe/mae/tp1_hit/live_pnl
        #   * ``on_bar_callback`` snapshots the book
        #   * the equity curve's MTM total sums ``live_pnl_usd``
        #
        # With none of those, the scan is pure waste. Gating it is
        # bit-identical in the results (the fields are only read by
        # those three consumers) and was worth ~1.8x on its own.
        _need_live_state = (
            on_bar_callback is not None
            or conviction_model_obj is not None
            or record_equity_curve
        )
        if open_trades and _need_live_state:
            for ot in open_trades:
                if ot.direction > 0:
                    fav = b_high - ot.entry_price
                    adv = ot.entry_price - b_low
                else:
                    fav = ot.entry_price - b_low
                    adv = b_high - ot.entry_price
                if fav > ot.mfe_usd:
                    ot.mfe_usd = float(fav)
                if adv > ot.mae_usd:
                    ot.mae_usd = float(adv)
                # Sticky TP1-hit: once True, stays True. Recompute
                # here so the flag tracks the *original* TP1
                # distance (not the moved-breakeven TP), which is
                # what the model needs.
                if not ot.tp1_hit and ot.target_price > 0:
                    if ot.direction > 0 and b_high >= ot.target_price:
                        ot.tp1_hit = True
                    elif ot.direction < 0 and b_low <= ot.target_price:
                        ot.tp1_hit = True
                # live_pnl at the most-recent bar's close, scaled by
                # THIS position's size (the TP1 remainder leg is half
                # size, not full).
                # PERF P3 (2026-10-22): ``ot.qty_of(qty_btc)`` is a
                # METHOD CALL — 197.8M of them on the 1-month slice
                # cost 37.9s of a 209s profile. Inlined here.
                ot.live_pnl_usd = float(
                    (b_close - ot.entry_price) * ot.direction
                ) * (ot.qty if ot.qty > 0.0 else qty_btc)
        # PERF P4 (2026-10-22): the exit scan builds a fresh
        # ``still_open`` list every bar, which was 197.8M
        # ``list.append`` calls (17.4s of the profile). The list is
        # now compacted IN PLACE via swap-pop: survivors are moved
        # into the front of ``open_trades`` and the tail is
        # truncated. Same ordering semantics for the survivor list
        # (stable, because we write survivors in scan order).
        _survivor_count = 0
        for _ot in open_trades:
            ot = _ot
            # Multi-target: TP1 (partial close) and TP2 (remainder).
            # After TP1 fires: close partial_pct, move SL to breakeven.
            # After TP2 fires or SL: close remainder.
            tp2_active = ot.partial_close_pct > 0 and ot.target_price_2 > 0
            if tp2_active:
                pct = ot.partial_close_pct  # e.g. 0.5
                tp2_price = ot.target_price_2
                tp1_price = ot.target_price
                entry_price = ot.entry_price
                direction = ot.direction

                if direction > 0:
                    tp1_hit = b_high >= tp1_price
                    tp2_hit = b_high >= tp2_price
                    sl_hit = b_low <= ot.stop_price
                else:
                    tp1_hit = b_low <= tp1_price
                    tp2_hit = b_low <= tp2_price
                    sl_hit = b_high >= ot.stop_price

                # ── Pessimistic fill model (2026-10-22) ────────────
                #
                # A bar can touch BOTH the stop and a target. The
                # pre-D2 order of precedence was TP1 → TP2 → SL,
                # so an ambiguous bar was always booked as a WIN.
                #
                # Under ``fill_model != "mid"`` the STOP takes
                # precedence: the unfavourable outcome wins. Under
                # ``"next_open"`` the whole outcome defers to the
                # next bar's open.
                if _bar_resolves_stop_first(
                    sl_hit=sl_hit,
                    tp_hit=tp1_hit or tp2_hit,
                    fill_model=fill_model,
                ):
                    tp1_hit = False
                    tp2_hit = False
                    sl_hit = True

                # Order of precedence: TP1 → TP2 → SL → still open
                if tp1_hit and not tp2_hit and not sl_hit:
                    # TP1 partial close. Sized off THIS position's own
                    # quantity, not the run-level ``qty_btc`` — on the
                    # remainder leg (already past TP1) tp2_active is
                    # False so this branch cannot re-fire, but sizing
                    # off ot.qty keeps the invariant explicit.
                    pos_qty = ot.qty if ot.qty > 0.0 else qty_btc
                    exit_price = _resolve_exit_price(
                        level=tp1_price, b_open=b_open, b_low=b_low,
                        b_high=b_high, direction=direction, is_stop=False,
                        fill_model=fill_model,
                    )
                    qty_used = pos_qty * pct
                    gross = (exit_price - entry_price) * direction * qty_used
                    fee = abs(exit_price + entry_price) * 0.5 * qty_used * (
                        taker_fee_bps / 10000.0) * 2.0
                    net = gross - fee
                    hold_secs = (b_time - ot.entry_time_ns) / 1e9
                    # The ORIGINAL stop distance (before this record
                    # moves the stop to breakeven). Reporting 0.0 here
                    # lost the SL geometry for every TP1 exit — 28% of
                    # all trades on the 1y run.
                    sl_dist = abs(entry_price - ot.stop_price)
                    tp_dist = abs(tp1_price - entry_price)
                    closed_trades.append(ClosedTrade(
                        trade_id=ot.trade_id,
                        parent_zone_id=ot.parent_zone_id,
                        entry_bar=ot.entry_bar, exit_bar=i,
                        entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time,
                        entry_price=entry_price, exit_price=exit_price,
                        direction=direction,
                        stop_price=entry_price,  # moved to breakeven
                        target_price=tp2_price,
                        exit_reason="tp1",
                        hold_secs=hold_secs,
                        pnl_gross_usd=gross, fee_usd=fee, pnl_net_usd=net,
                        qty_btc=qty_used,
                        fvg_direction=ot.fvg_direction,
                        source=ot.source,
                        sl_mode=ot.sl_mode, tp_mode=ot.tp_mode,
                        sl_struct_ref=ot.sl_struct_ref,
                        tp_struct_ref=ot.tp_struct_ref,
                        sl_distance_usd=sl_dist,
                        tp_distance_usd=tp_dist,
                        initial_sl_distance_usd=ot.initial_sl_distance_usd,
                        initial_tp_distance_usd=ot.initial_tp_distance_usd,
                        entry_poc_dist_usd=0.0,
                        zone_width=ot.zone_width,
                        zone_low=ot.zone_low,
                        struct_verdict=ot.struct_verdict,
                        signal_bar=ot.signal_bar,
                        zone_high=ot.zone_high,
                    ))
                    # Reopen as remainder trade with breakeven SL
                    # The remainder carries ``(1-pct)`` of THIS
                    # position's size. Recording it on the new
                    # OpenTrade is what makes the later TP2 / SL / EOD
                    # closes account for half size instead of full.
                    # PERF P3: ``ot.qty_of(qty_btc)`` inlined.
                    remaining_qty = (ot.qty if ot.qty > 0.0 else qty_btc) \
                        * (1.0 - pct)
                    next_trade_id += 1
                    # CONTRA CLOSE: the position was resized by the
                    # TP1 partial close, so the cached weighted
                    # averages are stale.
                    _c_aggr_dirty = True
                    open_trades[_survivor_count] = OpenTrade(
                        trade_id=next_trade_id,
                        parent_zone_id=ot.parent_zone_id,
                        entry_bar=ot.entry_bar,
                        entry_time_ns=ot.entry_time_ns,
                        entry_price=entry_price,
                        direction=direction,
                        stop_price=entry_price,  # breakeven SL
                        target_price=tp2_price,
                        fvg_direction=ot.fvg_direction,
                        source=ot.source,
                        sl_mode=ot.sl_mode,
                        tp_mode=ot.tp_mode,
                        sl_struct_ref="breakeven",
                        tp_struct_ref=ot.tp_struct_ref_2,
                        zone_width=ot.zone_width,
                        zone_low=ot.zone_low,
                        zone_high=ot.zone_high,
                        target_price_2=0.0,
                        tp_struct_ref_2="",
                        partial_close_pct=0.0,
                        qty=remaining_qty,
                        # Carry live state across so the conviction
                        # scorer sees the same excursion the first leg
                        # accumulated (it is the SAME trade).
                        mfe_usd=ot.mfe_usd,
                        mae_usd=ot.mae_usd,
                        tp1_hit=ot.tp1_hit,
                        # Carry the INITIAL risk/reward across — the
                        # remainder leg's stop is breakeven, but the
                        # trade's original risk is unchanged.
                        initial_sl_distance_usd=ot.initial_sl_distance_usd,
                        initial_tp_distance_usd=ot.initial_tp_distance_usd,
                        struct_verdict=ot.struct_verdict,
                        # The remainder leg is the SAME trade — carry the
                        # original SIGNAL bar so the per-side sequence
                        # attribution stays consistent across legs.
                        signal_bar=ot.signal_bar,
                    )
                    _survivor_count += 1
                    continue

                elif tp2_hit or (sl_hit and not tp1_hit):
                    # TP2 close (remainder) or SL hit before TP1.
                    #
                    # QTY BUG (fixed): this branch closes
                    # ``qty_btc * (1 - pct)``, i.e. only the REMAINDER,
                    # even when it is reached DIRECTLY off the original
                    # position (TP2 hit before TP1 was ever resolved).
                    # In that case the full ``qty_btc`` was open at
                    # TP2, so closing only ``(1-pct)`` of it silently
                    # lost the other half of the position. When tp2_hit
                    # is True we have NOT taken the TP1 partial yet,
                    # so the whole size closes here.
                    # (When tp2_hit is False this is a plain pre-TP1
                    # stop-out and ``(1-pct)`` was equally wrong for
                    # the same reason — the full size was open.)
                    exit_price = _resolve_exit_price(
                        level=(tp2_price if tp2_hit else ot.stop_price),
                        b_open=b_open, b_low=b_low, b_high=b_high,
                        direction=direction, is_stop=(not tp2_hit),
                        fill_model=fill_model,
                    )
                    # tp1_hit and tp2_hit are mutually exclusive by the
                    # branch guard above (the tp1 branch requires
                    # `not tp2_hit`), so reaching this branch means the
                    # FULL original size is still open — close all of
                    # it. The previous ``qty_btc * (1 - pct)`` silently
                    # lost the other half.
                    qty_used = ot.qty if ot.qty > 0.0 else qty_btc
                    gross = (exit_price - entry_price) * direction * qty_used
                    fee = abs(exit_price + entry_price) * 0.5 * qty_used * (
                        taker_fee_bps / 10000.0) * 2.0
                    net = gross - fee
                    hold_secs = (b_time - ot.entry_time_ns) / 1e9
                    sl_dist = abs(entry_price - ot.stop_price)
                    tp_dist = abs(tp2_price - entry_price)
                    closed_trades.append(ClosedTrade(
                        trade_id=ot.trade_id,
                        parent_zone_id=ot.parent_zone_id,
                        entry_bar=ot.entry_bar, exit_bar=i,
                        entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time,
                        entry_price=entry_price, exit_price=exit_price,
                        direction=direction,
                        stop_price=ot.stop_price,
                        target_price=tp2_price,
                        exit_reason="tp2" if tp2_hit else "sl",
                        hold_secs=hold_secs,
                        pnl_gross_usd=gross, fee_usd=fee, pnl_net_usd=net,
                        qty_btc=qty_used,
                        fvg_direction=ot.fvg_direction,
                        source=ot.source,
                        sl_mode=ot.sl_mode, tp_mode=ot.tp_mode,
                        sl_struct_ref=ot.sl_struct_ref,
                        tp_struct_ref=ot.tp_struct_ref_2,
                        sl_distance_usd=sl_dist,
                        tp_distance_usd=tp_dist,
                        initial_sl_distance_usd=ot.initial_sl_distance_usd,
                        initial_tp_distance_usd=ot.initial_tp_distance_usd,
                        entry_poc_dist_usd=0.0,
                        zone_width=ot.zone_width,
                        zone_low=ot.zone_low,
                        struct_verdict=ot.struct_verdict,
                        signal_bar=ot.signal_bar,
                        zone_high=ot.zone_high,
                    ))
                    continue

                elif sl_hit:
                    # SL before TP1 (full position)
                    exit_reason = "sl"
                    exit_price = _resolve_exit_price(
                        level=ot.stop_price, b_open=b_open, b_low=b_low,
                        b_high=b_high, direction=direction, is_stop=True,
                        fill_model=fill_model,
                    )
                    qty_used = ot.qty if ot.qty > 0.0 else qty_btc
                    gross = (exit_price - entry_price) * direction * qty_used
                    fee = abs(exit_price + entry_price) * 0.5 * qty_used * (
                        taker_fee_bps / 10000.0) * 2.0
                    net = gross - fee
                    hold_secs = (b_time - ot.entry_time_ns) / 1e9
                    tp_dist = abs(ot.target_price - entry_price)
                    # Recomputed here: the name `sl_dist` was previously
                    # read from an EARLIER branch iteration (leaked from
                    # whichever trade closed last), so every SL exit
                    # reported a garbage sl_distance_usd.
                    sl_dist = abs(entry_price - ot.stop_price)
                    svp = current_daily_vp(vp_set, ot.entry_time_ns)
                    poc_dist_signed = svp.poc - entry_price if svp else 0.0
                    closed_trades.append(ClosedTrade(
                        trade_id=ot.trade_id,
                        parent_zone_id=ot.parent_zone_id,
                        entry_bar=ot.entry_bar, exit_bar=i,
                        entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time,
                        entry_price=entry_price, exit_price=exit_price,
                        direction=direction,
                        stop_price=exit_price,
                        target_price=ot.target_price,
                        exit_reason=exit_reason,
                        hold_secs=hold_secs,
                        pnl_gross_usd=gross, fee_usd=fee, pnl_net_usd=net,
                        qty_btc=qty_used,
                        fvg_direction=ot.fvg_direction,
                        source=ot.source,
                        sl_mode=ot.sl_mode, tp_mode=ot.tp_mode,
                        sl_struct_ref=ot.sl_struct_ref,
                        tp_struct_ref=ot.tp_struct_ref,
                        sl_distance_usd=sl_dist,
                        tp_distance_usd=tp_dist,
                        entry_poc_dist_usd=poc_dist_signed,
                        zone_width=ot.zone_width,
                        zone_low=ot.zone_low,
                        struct_verdict=ot.struct_verdict,
                        signal_bar=ot.signal_bar,
                        zone_high=ot.zone_high,
                    ))
                    continue
                else:
                    # Survivor: compact in place (PERF P4).
                    open_trades[_survivor_count] = ot
                    _survivor_count += 1
                    continue

            # ── Single-target path ─────────────────────────────────
            if ot.direction > 0:
                tp_hit = b_high >= ot.target_price
                sl_hit = b_low <= ot.stop_price
            else:
                tp_hit = b_low <= ot.target_price
                sl_hit = b_high >= ot.stop_price

            # Pessimistic fill: an ambiguous bar (both SL and TP
            # touched) resolves to the STOP. The pre-D2 code already
            # did ``tp_hit and sl_hit -> sl`` unconditionally, so the
            # legacy "mid" mode is preserved by the explicit branch.
            if tp_hit and sl_hit and fill_model != FILL_MODEL_MID:
                exit_reason = "sl"
                exit_price = _resolve_exit_price(
                    level=ot.stop_price, b_open=b_open, b_low=b_low,
                    b_high=b_high, direction=ot.direction, is_stop=True,
                    fill_model=fill_model,
                )
            elif tp_hit and sl_hit:
                exit_reason = "sl"
                exit_price = ot.stop_price
            elif tp_hit:
                exit_reason = "tp"
                exit_price = _resolve_exit_price(
                    level=ot.target_price, b_open=b_open, b_low=b_low,
                    b_high=b_high, direction=ot.direction, is_stop=False,
                    fill_model=fill_model,
                )
            elif sl_hit:
                exit_reason = "sl"
                exit_price = _resolve_exit_price(
                    level=ot.stop_price, b_open=b_open, b_low=b_low,
                    b_high=b_high, direction=ot.direction, is_stop=True,
                    fill_model=fill_model,
                )
            else:
                # Survivor: compact in place (PERF P4).
                open_trades[_survivor_count] = ot
                _survivor_count += 1
                continue

            # PERF P3: ``ot.qty_of(qty_btc)`` inlined.
            pos_qty = ot.qty if ot.qty > 0.0 else qty_btc
            gross = (exit_price - ot.entry_price) * ot.direction * pos_qty
            fee = abs(exit_price + ot.entry_price) * 0.5 * pos_qty * (
                taker_fee_bps / 10000.0) * 2.0
            net = gross - fee
            hold_secs = (b_time - ot.entry_time_ns) / 1e9
            sl_dist = abs(ot.entry_price - ot.stop_price)
            tp_dist = abs(ot.target_price - ot.entry_price)
            svp = current_daily_vp(vp_set, ot.entry_time_ns)
            poc_dist_signed = svp.poc - ot.entry_price if svp else 0.0
            closed_trades.append(ClosedTrade(
                trade_id=ot.trade_id,
                parent_zone_id=ot.parent_zone_id,
                entry_bar=ot.entry_bar, exit_bar=i,
                entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time,
                entry_price=ot.entry_price, exit_price=exit_price,
                direction=ot.direction,
                stop_price=ot.stop_price,
                target_price=ot.target_price,
                exit_reason=exit_reason,
                hold_secs=hold_secs,
                pnl_gross_usd=gross, fee_usd=fee, pnl_net_usd=net,
                qty_btc=pos_qty,
                fvg_direction=ot.fvg_direction,
                source=ot.source,
                sl_mode=ot.sl_mode, tp_mode=ot.tp_mode,
                sl_struct_ref=ot.sl_struct_ref,
                tp_struct_ref=ot.tp_struct_ref,
                sl_distance_usd=sl_dist,
                tp_distance_usd=tp_dist,
                initial_sl_distance_usd=ot.initial_sl_distance_usd,
                initial_tp_distance_usd=ot.initial_tp_distance_usd,
                entry_poc_dist_usd=poc_dist_signed,
                zone_width=ot.zone_width,
                zone_low=ot.zone_low,
                struct_verdict=ot.struct_verdict,
                signal_bar=ot.signal_bar,
                zone_high=ot.zone_high,
            ))
        # PERF P4: truncate the in-place-compacted book. Survivors were
        # written to the front of ``open_trades`` in scan order, so the
        # surviving list has the same order as the old ``still_open``.
        if _survivor_count != len(open_trades):
            # CONTRA CLOSE: positions were closed, so the cached
            # weighted-average entries are stale.
            _c_aggr_dirty = True
        del open_trades[_survivor_count:]

        # NB87b: equity-curve update. Done AFTER step 3 SL/TP/eod
        # resolution and BEFORE the on_bar_callback so the snapshot
        # the callback sees (the open book at the close of bar i) is
        # the same one stamped on the curve.
        #
        # Performance: a naive ``sum(ot.live_pnl_usd for ot in
        # open_trades)`` is O(open_positions) per bar, which on a
        # 2.5M-bar monthly file with a 100-position book is 250M
        # attribute lookups — measured to add ~50s/cell on
        # ``stack_cap0`` (the book holds 100+ positions on most
        # bars). For max-DD purposes the curve needs ~1-minute
        # resolution, not 1-second. So:
        #
        # 1. Closed-PnL delta is summed every bar (it's usually 0
        #    or 1 trade, so cheap).
        # 2. The MTM value is only RE-COMPUTED every
        #    ``MTM_RESAMPLE_BARS`` bars; in between, the curve
        #    linearly interpolates between the two anchor points
        #    (i.e. flat-line the last computed MTM forward, which
        #    is a fine approximation for the 1-minute sampling
        #    the max-DD calc needs).
        #
        # A more precise approach would track a running MTM total
        # incrementally (add on entry, subtract on close); the
        # right place for that is a future refactor that moves the
        # engine's hot path to Numba or a numpy per-bar book.
        MTM_RESAMPLE_BARS = 60
        if _n_closed_before < len(closed_trades):
            cum_closed_pnl += sum(
                t.pnl_net_usd for t in closed_trades[_n_closed_before:]
            )
        # Recompute the open-book MTM every MTM_RESAMPLE_BARS bars.
        # This is the one O(open_positions) operation per resample
        # window, ~40k windows on a 2.5M-bar file instead of 2.5M
        # — a 60× speedup on a corpus with hundreds of stacked
        # positions.
        if (i % MTM_RESAMPLE_BARS == 0) or (i == n - 1):
            last_mtm = sum(
                float(ot.live_pnl_usd) for ot in open_trades
            )
        if equity_curve.size > 0:
            equity_curve[i] = cum_closed_pnl + last_mtm

        # NB87: per-bar diagnostic callback. Fired after step 3
        # resolves SL/TP/eod so the snapshot the callback sees is
        # POST-resolution. A no-op when ``on_bar_callback is None``
        # (every existing call site) — short-circuits before any
        # work is done, so bit-identical.
        #
        # Downsample by ``on_bar_every_k`` (default 1 = every bar).
        # At 1s bars, k=60 records one row per minute per open
        # position — 60× cheaper, 1-minute resolution which is
        # plenty for survival-curve analysis at 60s+ buckets.
        if on_bar_callback is not None and open_trades \
                and (i % max(1, on_bar_every_k) == 0):
            _vs = None
            if vol_state is not None:
                # ``vol_state`` is a ``VolState`` OBJECT (it has
                # ``__getitem__`` / ``get`` but NO ``items()``), but
                # this callback path assumed a plain dict. With the
                # canonical config the engine is handed a VolState, so
                # the callback raised AttributeError on the FIRST bar
                # -- and because ``on_bar_callback`` had ZERO
                # production callers, the bug was never exercised.
                #
                # Support BOTH shapes: a VolState-like object (read
                # ``.series``) and a plain dict.
                if hasattr(vol_state, "series"):
                    _src = vol_state.series
                elif hasattr(vol_state, "items"):
                    _src = dict(vol_state.items())
                else:
                    _src = {}
                _vs = {k: (float(v[i]) if i < len(v) else 0.0)
                       for k, v in _src.items()}
            on_bar_callback(
                i, b_time, b_close, b_high, b_low,
                list(open_trades),  # snapshot
                _vs,
            )

    # ── 4. EOD: close remaining trades at last bar ─────────────────
    last_close = float(close[-1])
    last_time = int(times[-1])
    for ot in open_trades:
        # Scale to THIS position's size: a TP1 remainder leg is
        # (1-pct) of the original, and EOD used to close it at full
        # run-level qty_btc — inflating every runner's P&L.
        pos_qty = ot.qty if ot.qty > 0.0 else qty_btc
        gross = (last_close - ot.entry_price) * ot.direction * pos_qty
        fee = abs(last_close + ot.entry_price) * 0.5 * pos_qty * (
            taker_fee_bps / 10000.0
        ) * 2.0
        net = gross - fee
        hold_secs = (last_time - ot.entry_time_ns) / 1e9
        sl_dist = abs(ot.entry_price - ot.stop_price)
        tp_dist = abs(ot.target_price - ot.entry_price)
        svp = current_daily_vp(vp_set, ot.entry_time_ns)
        poc_dist_signed = (
            svp.poc - ot.entry_price if svp is not None else 0.0
        )
        closed_trades.append(ClosedTrade(
            trade_id=ot.trade_id,
            parent_zone_id=ot.parent_zone_id,
            entry_bar=ot.entry_bar,
            exit_bar=n - 1,
            entry_time_ns=ot.entry_time_ns,
            exit_time_ns=last_time,
            entry_price=ot.entry_price,
            exit_price=last_close,
            direction=ot.direction,
            stop_price=ot.stop_price,
            target_price=ot.target_price,
            exit_reason="eod",
            hold_secs=hold_secs,
            pnl_gross_usd=gross,
            fee_usd=fee,
            pnl_net_usd=net,
            qty_btc=pos_qty,
            fvg_direction=ot.fvg_direction,
            source=ot.source,
            sl_mode=ot.sl_mode,
            tp_mode=ot.tp_mode,
            sl_struct_ref=ot.sl_struct_ref,
            tp_struct_ref=ot.tp_struct_ref,
            sl_distance_usd=sl_dist,
            tp_distance_usd=tp_dist,
            initial_sl_distance_usd=ot.initial_sl_distance_usd,
            initial_tp_distance_usd=ot.initial_tp_distance_usd,
            entry_poc_dist_usd=poc_dist_signed,
            zone_width=ot.zone_width,
            zone_low=ot.zone_low,
            struct_verdict=ot.struct_verdict,
            signal_bar=ot.signal_bar,
            zone_high=ot.zone_high,
            target_price_2=ot.target_price_2,
            tp_struct_ref_2=ot.tp_struct_ref_2,
            partial_close_pct=ot.partial_close_pct,
        ))

    res.trades = closed_trades
    # NB87b: stamp the per-bar equity curve on the result. Length is
    # the number of input bars, regardless of how many trades were
    # emitted. Callers that need only the closed-trade series (the
    # pre-NB87b default) can ignore this field; the run-log
    # ``metrics`` payload and the run-report JSON stay
    # backward-compatible.
    res.equity_curve = equity_curve

    # ── NB91-DIAG: signal conservation check ───────────────────────
    # Every emitted signal must either place an order or die at
    # exactly one gate. ``n_dropped_vol_state`` is shared between
    # both entry paths, so it is counted once here; the per-path
    # warmup counters are separate.
    #
    # D2 (2026-10-22): a signal now places a RESTING ORDER rather
    # than opening a trade immediately, so the conservation identity
    # is now:
    #
    #   n_signals_emitted
    #     - n_dropped_vol_state
    #     - n_dropped_p95_warmup
    #     - n_dropped_p95_warmup_sniper
    #     - n_dropped_policy           (policy at SIGNAL time)
    #   == n_orders_placed
    #
    # and the order lifecycle is separately conserved:
    #
    #   n_orders_placed
    #     - n_dropped_policy_at_fill   (policy at FILL time)
    #   == n_orders_filled + n_orders_cancelled
    #
    # NOTE (2026-10-22): before the split, a fill-time policy drop
    # was counted in ``n_dropped_policy`` as well, which made
    # ``signals_unaccounted`` NEGATIVE (measured -51 on the 1-month
    # slice). The two counters are now disjoint.
    _accounted = (
        res.n_orders_placed
        + res.n_dropped_vol_state
        + res.n_dropped_p95_warmup
        + res.n_dropped_p95_warmup_sniper
        + res.n_dropped_policy
        # 2026-10-23: the BoS/CHoCH gate is a SIGNAL-time drop, so
        # it belongs in the conservation chain. Without this term
        # ``signals_unaccounted`` read 7,991 on the pivot_len=3 cell
        # -- i.e. it looked like the engine had lost 80% of its
        # signals. AGENTS.md requires this to be 0.
        + res.n_dropped_structure
    )
    diag["signals_emitted"] = int(res.n_signals_emitted)
    diag["signals_accounted"] = int(_accounted)
    diag["signals_unaccounted"] = int(res.n_signals_emitted - _accounted)
    diag["order_lifecycle"] = {
        "placed": int(res.n_orders_placed),
        "filled": int(res.n_orders_filled),
        "cancelled": int(res.n_orders_cancelled),
        "cancelled_zone_death": int(res.n_orders_cancelled_zone_death),
        "cancelled_expired": int(res.n_orders_cancelled_expired),
        "cancelled_policy_at_fill": int(res.n_dropped_policy_at_fill),
        "still_resting_at_eod": int(len(pending_orders)),
        # ``n_orders_cancelled`` ALREADY INCLUDES the policy-at-fill
        # cancellations, so they must NOT be subtracted again.
        "unaccounted": int(
            res.n_orders_placed - res.n_orders_filled
            - res.n_orders_cancelled - len(pending_orders)
        ),
    }
    diag["gate_breakdown"] = {
        "structure": int(res.n_dropped_structure),
        "vol_state": int(res.n_dropped_vol_state),
        "p95_warmup": int(res.n_dropped_p95_warmup),
        "p95_warmup_sniper": int(res.n_dropped_p95_warmup_sniper),
        "conflict_policy": int(res.n_dropped_policy),
        "conflict_policy_at_fill": int(res.n_dropped_policy_at_fill),
        "orders_placed": int(res.n_orders_placed),
        "filled_immediate": int(res.n_immediate_filled),
        "filled_sniper": int(res.n_sniper_filled),
        "skipped_already_fired": int(res.n_skipped_already_fired),
        "skipped_dual_inverted": int(res.n_skipped_dual_inverted),
        "zones_bucketized": int(res.n_zones_bucketized),
        "inversions_detected": int(res.n_inversions_detected),
    }
    diag["structure_gate"] = {
        "enabled": bool(structure_gate),
        "max_age_bars": int(structure_max_age_bars),
        "aligned": int(res.n_structure_aligned),
        "opposed": int(res.n_structure_opposed),
        "no_event": int(res.n_structure_no_event),
    }
    diag["fill_model"] = res.fill_model_used
    diag["entry_fill_model"] = res.entry_fill_model_used
    return res, diag
