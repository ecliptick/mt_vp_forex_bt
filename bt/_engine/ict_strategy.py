"""PendingSignal — the detector's signal record.

Forked from ``src.core.gmma_signals`` in the parent repo. The GMMA
trend signal generator (``generate_pending_signals``) and all the
GMMA/TEMA-specific knobs have been removed.

WHAT WAS REMOVED (2026-10-24)
------------------------------
``TrendStrategyParams`` — the ~1000-field recipe dataclass. Its only
consumer was ``ict_backtest.run_ict_backtest``, the bar engine, which
had **zero production callers** (the VP multi-target engine never
read it). It was a second source of truth for the same knobs that
``bt/optimal_config.py`` now holds as module constants, and it carried
an entire second recipe (``HF_TIGHT``) with zero callers.

``PendingSignal`` below is the ONLY survivor and it IS live:
``bt/_engine/ict_signals.py`` constructs it.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class PendingSignal:
    """A signal that fired on bar ``trigger_bar`` but won't be filled
    until the next bar's open (industry-standard anti-look-ahead)."""

    trigger_bar: int
    direction: int                       # +1 long, -1 short
    triggered_by: str = "fvg"            # "fvg" | "ifvg" | "orb" | "wyckoff" | "sweep"
    stop_usd: float = 0.0
    target_usd: float = 0.0
    # Per-signal conviction multiplier (0.0 = drop, 1.0 = neutral, up to
    # ~1.5 = boost from a fresh BoS/CHoCH+ in the trade direction).
    conviction: float = 1.0
    # Anchor FVG zone (None for non-FVG sources). The bar-loop uses
    # this to detect zone invalidation while the trade is open and
    # tighten the SL to the zone edge as a "soft stop".
    fvg_zone: object = None
    # Whether the signal originates from an *inverted* FVG. iFVG
    # entries are special: when the underlying FVG gets *re-inverted*
    # (i.e. price trades back through the zone, restoring its original
    # direction), all open iFVG positions must be tightened / closed.
    is_ifvg: bool = False
    # Price-rank tier assigned by ``compute_fvg_price_ranks`` (added
    # 2026-08-20). Values: "A" (top of the day's range for this
    # direction — most extreme), "B" (middle), "C" (weak end — most
    # likely to fail), or "" if the rank-treatment feature is off.
    # "rank_percentile" is the zone's 0..1 position within its
    # direction group for the day (0 = top bull / bottom bear,
    # 1 = bottom bull / top bear). The bar loop uses the tier to
    # scale SL/TP and optionally skip the deepest layers.
    rank_tier: str = ""
    rank_percentile: float = 0.0
    # NOTE: candle_quality (HUNT/MARGINAL/STRONG/TRUE) was REMOVED
    # 2026-09-16 because the underlying classifier required
    # future-looking zone fields (pierced_bar / inverted_bar /
    # mitigated_depth_pct) that aren't known at signal-emission time.
    # The rank_tier (price-rank percentile) is the only tier field
    # with a causal definition.
    # Explicit fill price override (added 2026-09-05). When set, the
    # bar loop uses this as the layer's fill_price directly instead
    # of computing it as ``trigger_close + layer_offset``. Used by
    # the liquidity-sweep signal: the sweep signal places a STOP
    # ORDER at the sweep price, so the fill price is fixed (not a
    # layer-offset above the trigger). The bar loop's existing
    # stop-order fill rules (b_low ≤ target for longs, b_high ≥
    # target for shorts) handle the fill naturally. When ``None``
    # (default), the legacy "trigger_close + layer_offset" semantic
    # applies.
    fill_price: float | None = None
