"""Single-knob sweep driver for XAUUSD with on-disk artifact caching.

Why this replaces the old ``runtime/sweeps/single_sweep.py``
==========================================================

The previous driver had three defects that made sweeps
uninterpretable:

1. **No metrics.** It read ``result.n_trades`` / ``result.calmar`` /
   ``result.ev_per_trade`` off ``BacktestResult`` — none of those
   attributes exist. Every cell reported an empty metric dict, so
   ``_pick_best`` silently returned cell 0. Metrics are now computed
   from ``result.trades`` (see :func:`compute_metrics`), which is
   the actual source of truth.

2. **No caching.** ``bars`` / ``zones`` / ``vp_set`` / ``swing_cap`` /
   ``p95_cap`` were rebuilt per cell even though they are functions
   of the FILE, not of the knob. On 1y XAUUSD that is ~45s (VP) +
   ~30s (zones) per cell, which dominates the sweep. They are now
   built ONCE per file and cached to disk via pickle, keyed by the
   params that shaped them.

3. **No per-cell JSON.** Violated the AGENTS.md per-element JSON
   policy — a crash mid-sweep lost everything. Each cell now writes
   its own JSON the moment it finishes.

Per-cell JSON (AGENTS.md policy)
--------------------------------

``runtime/sweeps/_results/<knob>/<scope>__cell_<idx>.json``

Shape is exactly the AGENTS.md contract::

    {ts_utc, notebook, knob, value, cell_idx, scope, engine,
     wall_secs, cache_hit, metrics, overrides_vs_canonical}

Usage
-----

    # Single knob
    python -m runtime.sweeps.cached_sweep \
        --knob sl_zone_mult --grid 10,20,40,80 --start 2025-01-01 --end 2025-02-01

    # Canonical recipe (no knob varied) — the "singular sweep"
    python -m runtime.sweeps.cached_sweep --knob _canonical

    # Use the 1y file
    python -m runtime.sweeps.cached_sweep --knob _canonical \
        --data C:/coding/ict_tier_v2/data/XAUUSD_S1_1y.parquet
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import pickle
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    STRICT_WICK_MIN_WICK_PRICE_PCT,
    _DETECTOR_OVERRIDES_ALLOWED as _DETECTOR_OA_FROM_OPT,
    _STRUCTURE_OVERRIDES_ALLOWED as _STRUCTURE_OA_FROM_OPT,
    build_detector_kwargs,
    build_engine_kwargs,
    build_p95_kwargs,
    build_structure_kwargs,
    build_swing_kwargs,
    build_vp_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402

DEFAULT_DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
CACHE_ROOT = _REPO_ROOT / "notebooks" / ".cache"
RESULTS_ROOT = _REPO_ROOT / "runtime" / "sweeps" / "_results"

_TS_FMT = "%Y-%m-%dT%H:%M:%SZ"

# Detector + VP shape. These determine the artifact fingerprint:
# changing any of them invalidates the cached zones / vp_set / caps.
#
# CRITICAL: `fvg_min_zone_usd` MUST match the canonical recipe's
# ``fvg_inv_trade_min_zone_usd = 0.30``. The previous driver used 0.10,
# which detects ~5,570 zones/day instead of ~388 (14x over-detection).
# At 0.10 the open book reaches the low thousands and the bar loop's
# O(n_bars x n_open_positions) check turns a 1-month run into hours.
# The 0.10 value was silently making sweeps ~100x more expensive AND
# trading a different (much higher-frequency) strategy than the recipe.
# Detector canonical defaults — sourced from bt.optimal_config
# (stage 2 lift, 2026-10-22). The dict form is preserved for
# callers that treat it as a base layer (e.g. the
# ``detector_kwargs_from_recipe`` recipe-merge); the canonical
# source of truth is ``build_detector_kwargs()``.
#
# CRITICAL: ``fvg_min_zone_usd`` MUST match the canonical recipe's
# ``fvg_inv_trade_min_zone_usd = 0.30``. The previous driver used 0.10,
# which detects ~5,570 zones/day instead of ~388 (14x over-detection).
# At 0.10 the open book reaches the low thousands and the bar loop's
# O(n_bars x n_open_positions) check turns a 1-month run into hours.
# The 0.10 value was silently making sweeps ~100x more expensive AND
# trading a different (much higher-frequency) strategy than the recipe.
DETECTOR_KWARGS: dict[str, Any] = build_detector_kwargs()
# ── VP knobs are now sourced from bt.optimal_config (stage 1 lift) ─
#
# Promoted from a hardcoded dict here on 2026-10-22 so a sweep can
# vary ONE VP knob (vp_rows / vp_min_zone_usd / vp_va_pct / ...) and
# have the artifact fingerprint invalidated correctly. The shape and
# the canonical values are unchanged — see
# bt.optimal_config.build_vp_kwargs + AGENTS.md § "XAU SCALE FIX"
# for the rows=100→200 / min=$5.00→$0.50 rationale.
VP_KWARGS: dict[str, Any] = build_vp_kwargs()
SWING_KWARGS: dict[str, Any] = build_swing_kwargs()
P95_KWARGS: dict[str, Any] = build_p95_kwargs()
STRUCTURE_KWARGS: dict[str, Any] = build_structure_kwargs()


def detector_kwargs_from_recipe(**overrides: Any) -> dict[str, Any]:
    """Build the ``detect_fvg`` kwargs from the canonical config.

    2026-10-24 (config consolidation): this used to merge TWO sources —
    the ``bt.optimal_config`` constants AND a recipe object read off
    ``bt._engine.optimal_config.OPTIMAL_PARAMS`` via ``getattr``. The
    recipe object is gone; there is now exactly ONE source of truth
    (:func:`bt.optimal_config.build_detector_kwargs`).

    The strict-wick floor is the one thing that is NOT a plain
    detector kwarg: the per-bar floor array is derived from the bars
    (which are not in the artifact fingerprint's JSON shape), so it is
    attached later by ``ArtifactCache.build_or_load``. This function
    ships the SENTINEL keys that tell it to do so.
    """
    kwargs: dict[str, Any] = build_detector_kwargs(**overrides)

    # ── NB52 strict-wick filter ────────────────────────────────
    # The forex recipe declared ``strict_wick_required=True`` but the
    # detector had NO strict-wick code, so the gate never ran and the
    # knob was dead. ``detect_fvg`` now implements it.
    #
    # The per-bar floor array is NOT part of the kwargs: it is derived
    # from the bars. ``build_or_load`` builds it from the bars it
    # already has and injects it under a sentinel key, so it is picked
    # up by ``detect_fvg`` without polluting the cache fingerprint.
    #
    # ``_strict_wick_price_pct`` IS in the fingerprint payload: it
    # changes the zone set, so a change must invalidate the cached
    # artifacts. The array it produces is derived from the bars
    # (already in the fingerprint via size+mtime+slice), so shipping
    # the pct is sufficient to key the cache.
    strict_wick_required = bool(kwargs.get("strict_wick_required", False))
    if strict_wick_required:
        kwargs.pop("strict_wick_min_wick_usd", None)
        kwargs["_strict_wick_use_floor_array"] = True
        kwargs["_strict_wick_price_pct"] = float(
            STRICT_WICK_MIN_WICK_PRICE_PCT)
    else:
        kwargs["strict_wick_min_wick_usd"] = 0.0

    return kwargs


# ─────────────────────────────────────────────────────────────────────
# Metrics — computed from result.trades, the real source of truth
# ─────────────────────────────────────────────────────────────────────
def compute_metrics(result: Any) -> dict[str, Any]:
    """Full metric set derived from ``result.trades``.

    ``BacktestResult`` carries counters (``n_signals_emitted``,
    ``n_dropped_opposite``, ...) but NOT P&L aggregates, so every
    previous summarizer reading ``result.calmar`` got a 0.0 default.
    Everything here is computed off the closed-trade list.

    Drawdown uses the engine's mark-to-market ``equity_curve`` when
    available (it reflects the OPEN book, which is the real risk on
    a multi-leg book), falling back to closed-trade cumulative P&L.
    """
    trades: list = list(getattr(result, "trades", []) or [])
    n = len(trades)
    pnls = [float(t.pnl_net_usd) for t in trades]
    fees = [float(getattr(t, "fee_usd", 0.0)) for t in trades]
    gross = [float(getattr(t, "pnl_gross_usd", 0.0)) for t in trades]
    holds = [float(getattr(t, "hold_secs", 0.0)) for t in trades]

    # SL / TP distances MUST come from the dedicated fields, NOT from
    # ``entry_price`` vs ``stop_price``.
    #
    # Why: the engine MUTATES ``stop_price`` as the trade progresses —
    # after a TP1 partial close the stop moves to breakeven and the
    # record stores ``stop_price = entry_price``; on a stop-out it
    # stores ``stop_price = exit_price``. So ``abs(entry - stop_price)``
    # is 0.0 for every TP1/TP2 exit, which is why the 1y run reported
    # ``median_sl_distance_usd = 0.0``.
    #
    # ``initial_sl_distance_usd`` is the honest measure: the risk the
    # trade was TAKEN with, captured at fill time.
    sl_dists = [
        float(getattr(t, "initial_sl_distance_usd", 0.0)
              or getattr(t, "sl_distance_usd", 0.0) or 0.0)
        for t in trades
    ]
    tp_dists = [
        float(getattr(t, "initial_tp_distance_usd", 0.0)
              or getattr(t, "tp_distance_usd", 0.0) or 0.0)
        for t in trades
    ]
    # Fall back to the price-derived form only when the dedicated field
    # is absent (e.g. a hand-built ClosedTrade in a unit test).
    if n and not any(sl_dists):
        sl_dists = [abs(float(t.entry_price) - float(t.stop_price))
                    for t in trades]
    if n and not any(tp_dists):
        tp_dists = [abs(float(t.target_price) - float(t.entry_price))
                    for t in trades]

    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v <= 0]
    sum_wins = float(sum(wins))
    sum_losses = float(abs(sum(losses)))

    # ── Equity curve + drawdown ──
    eq = np.asarray(getattr(result, "equity_curve", np.zeros(0)), dtype=float)
    if eq.size > 0 and np.isfinite(eq).any():
        run_max = np.maximum.accumulate(eq)
        dd = run_max - eq
        max_dd = float(np.max(dd))
        curve_source = "mtm_equity_curve"
    elif n:
        # NOTE: np.cumsum returns a 1-D array; under numpy 2.x,
        # float(...) on a 1-D array raises. Take the array form and
        # reduce explicitly.
        cum = np.cumsum(np.asarray(pnls, dtype=float))
        run_max = np.maximum.accumulate(cum)
        max_dd = float(np.max(run_max - cum))
        curve_source = "closed_trade_cumsum"
    else:
        max_dd = 0.0
        curve_source = "empty"

    total_net = float(sum(pnls))
    total_fees = float(sum(fees))
    total_gross = float(sum(gross))

    # ── Annualisation ──
    span_days = 1.0
    if trades:
        t0 = int(trades[0].entry_time_ns)
        t1 = int(trades[-1].exit_time_ns)
        if t1 > t0:
            span_days = (t1 - t0) / 1e9 / 86400.0
    years = max(span_days / 365.25, 1e-9)
    cagr = ((1.0 + total_net / 1000.0) ** (1.0 / years) - 1.0) \
        if (years > 0 and total_net > -1000.0) else None

    profit_factor = (sum_wins / sum_losses) if sum_losses > 0 else None

    # Sharpe: per-trade returns scaled by sqrt(trades/year). Reported
    # alongside trade count so it can be sanity-checked — a Sharpe
    # computed on <100 trades is noise.
    sharpe = None
    if n > 2:
        sd = float(np.std(np.asarray(pnls, dtype=float), ddof=1))
        if sd > 0:
            per_trade = (float(np.mean(pnls)) / sd) * math.sqrt(n / years)

    exit_breakdown: dict[str, int] = {}
    for t in trades:
        r = str(getattr(t, "exit_reason", "?"))
        exit_breakdown[r] = exit_breakdown.get(r, 0) + 1
    source_breakdown: dict[str, int] = {}
    for t in trades:
        r = str(getattr(t, "source", "?"))
        source_breakdown[r] = source_breakdown.get(r, 0) + 1

    # Realised R:R — the number that actually matters here, because
    # sl_zone_mult=40 and tp_zone_mult_fallback=200 are wildly
    # mismatched geometrically and only realised P&L reveals which
    # leg binds.
    realised_rr = (sum_wins / sum_losses) if sum_losses > 0 else None
    med_win = float(np.median(wins)) if wins else None
    med_loss = float(np.median(losses)) if losses else None
    payoff = (abs(med_win / med_loss)
              if med_win is not None and med_loss else None)

    return {
        "n_trades": n,
        "n_long": int(sum(1 for t in trades if int(t.direction) > 0)),
        "n_short": int(sum(1 for t in trades if int(t.direction) < 0)),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "hit_rate": (len(wins) / n) if n else 0.0,
        "total_pnl_gross_usd": total_gross,
        "total_fees_usd": total_fees,
        "total_pnl_net_usd": total_net,
        "ev_per_trade_usd": (total_net / n) if n else 0.0,
        "profit_factor": profit_factor,
        "payoff_ratio": payoff,
        "max_drawdown_usd": max_dd,
        "max_dd_source": curve_source,
        "cagr": cagr,
        "sharpe_per_trade_annualised": sharpe,
        "trades_per_day": (n / span_days) if span_days > 0 else 0.0,
        "span_days": span_days,
        "median_hold_secs": float(np.median(holds)) if holds else None,
        "mean_hold_secs": float(np.mean(holds)) if holds else None,
        "median_sl_distance_usd": float(np.median(sl_dists)) if sl_dists else None,
        "median_tp_distance_usd": float(np.median(tp_dists)) if tp_dists else None,
        "exit_reason_breakdown": exit_breakdown,
        "source_breakdown": source_breakdown,
        # Engine counters (these DO exist on BacktestResult)
        "n_signals_emitted": int(getattr(result, "n_signals_emitted", 0)),
        "n_inversions_detected": int(getattr(result, "n_inversions_detected", 0)),
        "n_immediate_filled": int(getattr(result, "n_immediate_filled", 0)),
        "n_sniper_filled": int(getattr(result, "n_sniper_filled", 0)),
        "n_dropped_opposite": int(getattr(result, "n_dropped_opposite", 0)),
        "n_replaced_opposite": int(getattr(result, "n_replaced_opposite", 0)),
        "n_stacked_opposite": int(getattr(result, "n_stacked_opposite", 0)),
        "n_same_dir_blocked": int(getattr(result, "n_same_dir_blocked", 0)),
        "conflict_policy_used": getattr(result, "conflict_policy_used", "?"),
        "max_same_dir_open_used": int(getattr(result, "max_same_dir_open_used", 0)),
        # D2 (2026-10-22): entry-order lifecycle. The gap between
        # "orders placed" and "orders filled" is the number of signals
        # whose resting limit order never got hit — the trades that
        # the pre-D2 engine invented out of thin air.
        "n_orders_placed": int(getattr(result, "n_orders_placed", 0)),
        "n_orders_filled": int(getattr(result, "n_orders_filled", 0)),
        "n_orders_cancelled": int(getattr(result, "n_orders_cancelled", 0)),
        "n_orders_cancelled_zone_death": int(
            getattr(result, "n_orders_cancelled_zone_death", 0)),
        # The D2 fill rate: what fraction of resting orders actually
        # filled. Pre-D2 this was 100% because the engine invented the
        # fill. On 1y it is 51.7% — the other 48.3% either never got
        # hit or the zone died first.
        "order_fill_rate": (
            (float(getattr(result, "n_orders_filled", 0))
             / float(getattr(result, "n_orders_placed", 0)))
            if getattr(result, "n_orders_placed", 0) else None
        ),
        "entry_fill_model_used": getattr(result, "entry_fill_model_used", "?"),
        "fill_model_used": getattr(result, "fill_model_used", "?"),
        # ── CONTRA CLOSE / HEDGE NETTING (2026-10-24) ─────────────
        # The netting counters are the ONLY way to tell whether the
        # feature did anything on a given slice. A cell with
        # ``n_contra_close_events == 0`` is a cell that measured the
        # OFF behaviour, whatever the flags said.
        "n_contra_close_events": int(
            getattr(result, "n_contra_close_events", 0)),
        "n_contra_legs_closed": int(
            getattr(result, "n_contra_legs_closed", 0)),
        "n_contra_qty_netted": float(
            getattr(result, "n_contra_qty_netted", 0.0)),
        "contra_gross_usd": float(getattr(result, "contra_gross_usd", 0.0)),
        "contra_fees_usd": float(getattr(result, "contra_fees_usd", 0.0)),
        "contra_net_usd": (
            float(getattr(result, "contra_gross_usd", 0.0))
            - float(getattr(result, "contra_fees_usd", 0.0))
        ),
        "contra_spread_usd_max": float(
            getattr(result, "contra_spread_usd_max", 0.0)),
        # The exit-reason breakdown already counts "contra_close"
        # legs; this is the gross/net those legs contributed, computed
        # off the trade list so it agrees with the per-cell JSON.
        "contra_legs_in_trades": int(
            exit_breakdown.get("contra_close", 0)),
    }


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/Inf -> None, numpy scalars -> python."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj


# ─────────────────────────────────────────────────────────────────────
# Bar loading
# ─────────────────────────────────────────────────────────────────────
def load_bars(path: Path, start: str | None = None, end: str | None = None) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if "time" not in df.columns:
        for alt in ("datetime", "timestamp"):
            if alt in df.columns:
                df = df.rename(columns={alt: "time"})
                break
    df["time"] = pd.to_datetime(df["time"], utc=True)
    if start is not None:
        df = df[df["time"] >= pd.Timestamp(start, tz="UTC")]
    if end is not None:
        df = df[df["time"] < pd.Timestamp(end, tz="UTC")]
    df = df.sort_values("time").reset_index(drop=True)
    # The VP builder needs a volume column; XAUUSD has tickv.
    #
    # MEMORY: RENAME, don't copy. On the 1y file every column is
    # 330 MB, and pandas' column assignment ALWAYS copies:
    #
    #     df["volume"] = df["tickv"].astype(float)  # 330 MB duplicate
    #     df["volume"] = df["tickv"]                # 330 MB duplicate
    #
    # ``df.rename(columns={"tickv": "volume"})`` is a label change
    # only — it reuses the SAME array, costing 0 extra bytes.
    # ``tickv`` is read nowhere else in the repo (it exists solely
    # to feed the VP builder), so renaming it is safe.
    if "volume" not in df.columns:
        if "tickv" in df.columns:
            df = df.rename(columns={"tickv": "volume"})
        else:
            df["volume"] = (df["high"] - df["low"]).abs() * 1000.0
    elif "tickv" in df.columns:
        # Both present: tickv is unused, drop the duplicate.
        df = df.drop(columns=["tickv"])
    return df


# ─────────────────────────────────────────────────────────────────────
# Artifact cache
# ─────────────────────────────────────────────────────────────────────
#
# Modules whose SOURCE CODE determines the artifact contents.
#
# The fingerprint already covers the resolved detector / VP / swing / p95
# / structure KWARGS, but a change to the detector IMPLEMENTATION does
# not change any kwarg -- so a stale artifact stayed a cache HIT and
# every downstream diagnostic silently re-measured the OLD zone set.
#
# This actually bit us on 2026-10-24: the zone-lifecycle fix in
# ``bt/_engine/ict_signals.py`` (supersession no longer preempting the
# inversion walk) moved the 2026-01 slice from 130 inversions to 1,270,
# yet every artifact cache built before the fix was still a HIT. The
# directional-gating diagnostic had been measured on the pre-fix set.
#
# Only the artifact-PRODUCING modules are listed. The bar/engine loop
# (``vp_multi_target_engine.py``) is deliberately excluded: it changes
# often and cannot change the contents of a pickled zone set, so
# including it would invalidate hours of artifact rebuilds for nothing.
_ARTIFACT_SOURCES = (
    "bt/_engine/ict_signals.py",
    "bt/_engine/volume_profile.py",
    "bt/_engine/swing_cap.py",
    "bt/_engine/rolling_p95.py",
    "bt/_engine/market_structure.py",
    "bt/_engine/timebase.py",
    "bt/optimal_config.py",
)


def _source_hash() -> str:
    """SHA1 over the artifact-producing source files.

    A missing file contributes a sentinel rather than raising, so a
    partially-installed tree still produces a (stable) fingerprint
    instead of crashing the sweep.
    """
    h = hashlib.sha1()
    root = Path(__file__).resolve().parents[2]
    for rel in _ARTIFACT_SOURCES:
        h.update(rel.encode("utf-8"))
        try:
            h.update((root / rel).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def _fingerprint(path: Path, start: str | None, end: str | None,
                 shape: dict[str, Any]) -> str:
    st = path.stat()
    payload = json.dumps(
        {
            "path": str(path.resolve()),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "start": start,
            "end": end,
            "shape": shape,
            # Source-code fingerprint -- see _ARTIFACT_SOURCES.
            "src": _source_hash(),
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _cache_dir(scope: str) -> Path:
    d = CACHE_ROOT / scope
    d.mkdir(parents=True, exist_ok=True)
    return d


class ArtifactCache:
    """On-disk cache of the per-FILE (knob-independent) artifacts.

    ``bars`` is cheap to re-read so it is not pickled (1y XAUUSD is
    ~600MB). ``zones`` / ``vp_set`` / ``swing_cap`` / ``p95_cap`` are
    the expensive ones (~45s + ~30s) and are pickled together per
    detector/VP fingerprint.
    """

    def __init__(self, data_path: Path, scope: str,
                 start: str | None, end: str | None,
                 vp_overrides: dict[str, Any] | None = None,
                 detector_overrides: dict[str, Any] | None = None,
                 structure_overrides: dict[str, Any] | None = None):
        self.data_path = Path(data_path)
        self.scope = scope
        self.start, self.end = start, end
        # 2026-10-23: structure knobs are per-cache-instance. The
        # StructureState is a function of (bars, pivot_len,
        # resample_to_n_secs) -- NOT of the sweep knob -- but a pivot
        # sweep must still rebuild it, so it goes in the fingerprint
        # and the scope suffix alongside VP/detector.
        self.structure_overrides = dict(structure_overrides or {})
        bad_s = set(self.structure_overrides) - _STRUCTURE_OA_FROM_OPT
        if bad_s:
            raise KeyError(
                f'illegal structure override keys {sorted(bad_s)}; '
                f'must be in {sorted(_STRUCTURE_OA_FROM_OPT)}'
            )
        # ``build_structure_kwargs`` translates the canonical knob
        # names (``structure_pivot_len``) onto the target's parameter
        # names (``pivot_len``). It is the SINGLE translation point.
        self.structure_kwargs = build_structure_kwargs(
            **self.structure_overrides)
        # Stage-1 VP knob lift (2026-10-22): VP overrides are accepted
        # per cache instance, so each unique VP profile gets its own
        # artifact pickle. A cell that varies vp_rows will MISS the
        # canonical-shaped cache and rebuild only the VP profile
        # (zones / swing_cap / p95_cap stay shared because their
        # fingerprints are unchanged).
        #
        # The fingerprint embeds the FULL resolved VP kwargs
        # (`build_vp_kwargs(**vp_overrides)`), not just the diff, so
        # the canonical cell keys cleanly off VP_KWARGS and any
        # override generates a stable, distinct key.
        vp_overrides = dict(vp_overrides or {})
        # Reject unknown VP knobs up front (catches typos in the
        # sweep driver before a slow rebuild).
        from bt.optimal_config import _VP_OVERRIDES_ALLOWED
        bad = set(vp_overrides) - _VP_OVERRIDES_ALLOWED
        if bad:
            raise KeyError(
                f'illegal VP override keys {sorted(bad)}; must be in '
                f'{sorted(_VP_OVERRIDES_ALLOWED)}'
            )
        self.vp_overrides = vp_overrides
        self.vp_kwargs = build_vp_kwargs(**vp_overrides)

        # Stage-2 (2026-10-22): detector overrides are accepted per
        # cache instance for the same reason as VP overrides — the
        # zone set changes shape, so the artifact pickle must
        # invalidate. The validator runs twice (here + in
        # ``detector_kwargs_from_recipe``) so unknown detector knobs
        # surface IMMEDIATELY rather than after a slow rebuild.
        detector_overrides = dict(detector_overrides or {})
        bad = set(detector_overrides) - _DETECTOR_OA_FROM_OPT
        if bad:
            raise KeyError(
                f'illegal detector override keys {sorted(bad)}; '
                f'must be in {sorted(_DETECTOR_OA_FROM_OPT)}'
            )
        self.detector_overrides = detector_overrides
        self.detector_kwargs = detector_kwargs_from_recipe(
            **detector_overrides)

        shape = {
            "detector": self.detector_kwargs,
            "vp": self.vp_kwargs,
            "swing": SWING_KWARGS,
            "p95": P95_KWARGS,
            # 2026-10-23: the StructureState is cached, so its
            # building params MUST be in the fingerprint or a
            # pivot_len sweep would silently reuse the wrong pivots.
            "structure": self.structure_kwargs,
        }
        # Distinct scope per resolved (VP, detector) shape so two
        # sweeps that both vary the same knob at different values
        # don't share a cache, and so the canonical scope stays a
        # clean HIT/MISS for the unpicked recipe.
        suffix_bits: list[str] = []
        if vp_overrides:
            suffix_bits.append("__vp_" + "_".join(
                f"{k}={v}" for k, v in sorted(vp_overrides.items())))
        if detector_overrides:
            suffix_bits.append("__det_" + "_".join(
                f"{k}={v}" for k, v in sorted(detector_overrides.items())))
        if self.structure_overrides:
            suffix_bits.append("__st_" + "_".join(
                f"{k}={v}"
                for k, v in sorted(self.structure_overrides.items())))
        self.scope = scope + "".join(suffix_bits)
        self.fp = _fingerprint(self.data_path, start, end, shape)
        self.dir = _cache_dir(self.scope)
        self.hits: list[str] = []

    @property
    def path(self) -> Path:
        return self.dir / f"artifacts_{self.fp}.pkl"

    def load_bars(self) -> pd.DataFrame:
        return load_bars(self.data_path, self.start, self.end)

    def build_or_load(self, bars: pd.DataFrame) -> dict[str, Any]:
        if self.path.exists():
            t0 = time.perf_counter()
            with open(self.path, "rb") as fh:
                art = pickle.load(fh)
            art["_cache_hit"] = True
            art["_load_secs"] = time.perf_counter() - t0
            self.hits.append("artifacts")
            return art

        t0 = time.perf_counter()
        from bt._engine.ict_signals import (
            compute_dynamic_wick_floor,
            detect_fvg,
        )
        from bt._engine.rolling_p95 import build_rolling_p95_from_bars
        from bt._engine.swing_cap import build_daily_swing
        from bt._engine.volume_profile import build_volume_profiles

        # 2026-10-24 BUG FIX: this used to call
        # ``detector_kwargs_from_recipe()`` with NO arguments, which
        # SILENTLY DISCARDED ``self.detector_overrides`` — including
        # the FVG detection cadence. The fingerprint was computed from
        # the overridden kwargs (so the cache key was right) but the
        # DETECTOR ran with the canonical kwargs. The result: a
        # cadence sweep would build three differently-keyed caches
        # that all contained the SAME 1m zone set.
        #
        # ``self.detector_kwargs`` was built in ``__init__`` from
        # ``detector_kwargs_from_recipe(**detector_overrides)`` and
        # is the same dict that went into the fingerprint, so using it
        # here makes "what we fingerprint" and "what we detect with"
        # the SAME object. Copy it so the ``pop`` calls below do not
        # mutate the cached instance state.
        det_kwargs = dict(self.detector_kwargs)

        # NB52 strict-wick: build the price-relative per-bar floor HERE
        # (from the bars we already hold) rather than in
        # ``detector_kwargs_from_recipe``, so the non-serialisable
        # array never enters the JSON artifact fingerprint. The pct
        # that produced it IS in the fingerprint, so changing it
        # invalidates the cache correctly.
        use_floor_array = det_kwargs.pop(
            "_strict_wick_use_floor_array", False)
        wick_pct = float(det_kwargs.pop("_strict_wick_price_pct", 0.0))
        # 2026-10-23: hoisted out of the conditional below -- the
        # detector now needs it unconditionally (the resample must be
        # bucketed by TIMESTAMP, not by row position).
        from bt._engine.timebase import to_ns_arr
        if use_floor_array and wick_pct > 0.0:
            from bt.optimal_config import STRICT_WICK_RECOMPUTE_SECS
            det_kwargs["strict_wick_floor_usd_per_bar"] = (
                compute_dynamic_wick_floor(
                    bars["close"].to_numpy(dtype=np.float64),
                    price_pct=wick_pct,
                    recompute_secs=int(STRICT_WICK_RECOMPUTE_SECS),
                    times_utc_ns=to_ns_arr(bars),
                )
            )

        zones = detect_fvg(
            open_=bars["open"].to_numpy(),
            high=bars["high"].to_numpy(),
            low=bars["low"].to_numpy(),
            close=bars["close"].to_numpy(),
            # 2026-10-23: REQUIRED for a correct resample. Without it
            # the detector falls back to POSITIONAL bucketing, which
            # is wrong on any feed with gaps (weekends/rollovers)
            # because row position drifts from wall-clock time.
            times_utc_ns=to_ns_arr(bars),
            **det_kwargs,
        )
        t_zones = time.perf_counter() - t0

        t1 = time.perf_counter()
        vp_set = build_volume_profiles(
            bars_1s=bars,
            file_stem=self.scope,
            verbose=False,
            **self.vp_kwargs,
        )
        t_vp = time.perf_counter() - t1

        t2 = time.perf_counter()
        swing_cap = build_daily_swing(bars, **SWING_KWARGS)
        t_swing = time.perf_counter() - t2

        t3 = time.perf_counter()
        p95_cap = build_rolling_p95_from_bars(bars, **P95_KWARGS)
        p95_cap.skip_trades_during_warmup = True
        t_p95 = time.perf_counter() - t3

        # 2026-10-23: BoS/CHoCH structure state. Built HERE so it is
        # cached alongside the other artifacts -- it is a function of
        # (bars, pivot_len), not of the sweep knob, so rebuilding it
        # per cell would be pure waste. The VP engine used to have no
        # access to market structure at all.
        t4 = time.perf_counter()
        from bt._engine.market_structure import detect_market_structure
        from bt._engine.timebase import to_ns_arr
        structure = detect_market_structure(
            high=bars["high"].to_numpy(dtype=np.float64),
            low=bars["low"].to_numpy(dtype=np.float64),
            close=bars["close"].to_numpy(dtype=np.float64),
            pivot_len=self.structure_kwargs["pivot_len"],
            # 2026-10-23: MANDATORY. Without it, ``pivot_len=9`` on 1s
            # bars is a 19-SECOND window -- every micro-tick is a
            # "swing" and the BoS/CHoCH gate fires on noise. 60s makes
            # it a real 9-minute pivot, which is the canonical ICT
            # granularity. See ``detect_market_structure``'s own
            # docstring; it says the same thing.
            ts_ns=to_ns_arr(bars),
            resample_to_n_secs=self.structure_kwargs[
                "resample_to_n_secs"],
            detect_order_blocks=False,
            detect_liquidity=False,
        )
        t_struct = time.perf_counter() - t4

        # 2026-10-23: the SCORE filter needs a ``vol_state`` for the
        # momentum leg. The engine can only build it if the caller
        # supplies one; without it ``momentum_score`` silently
        # degrades to 0.0 and the blend becomes structure-only --
        # which is exactly the kind of silent modulator-veto this
        # repo keeps getting bitten by. So build it here and pass it
        # through explicitly.
        #
        # ``vel_5m`` is UNSIGNED (|P_t - P_{t-w}| / P_{t-w}); the sign
        # comes from price inside ``momentum_score``. These measures
        # are backward-only and already causality-fixed (2026-10-13).
        t5 = time.perf_counter()
        from bt._engine.vol_state import build_vol_state
        vol_state = build_vol_state(
            bars["high"].to_numpy(dtype=np.float64),
            bars["low"].to_numpy(dtype=np.float64),
            bars["close"].to_numpy(dtype=np.float64),
            bars["open"].to_numpy(dtype=np.float64),
            measures=("vel_5m", "range_norm_1m", "wick_ratio"),
        )
        t_vstate = time.perf_counter() - t5

        art = {
            "zones": zones,
            "vp_set": vp_set,
            "swing_cap": swing_cap,
            "p95_cap": p95_cap,
            "structure": structure,
            "vol_state": vol_state,
            "n_zones": len(zones),
            "build_secs": {
                "zones": t_zones, "vp_set": t_vp,
                "swing_cap": t_swing, "p95_cap": t_p95,
                "structure": t_struct,
                "vol_state": t_vstate,
            },
            "vp_overrides": dict(self.vp_overrides),
            "vp_kwargs": dict(self.vp_kwargs),
            "_cache_hit": False,
            "_load_secs": 0.0,
        }
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "wb") as fh:
            pickle.dump(art, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self.path)
        self.hits.append("artifacts")
        return art


# ─────────────────────────────────────────────────────────────────────
# Sweep
# ─────────────────────────────────────────────────────────────────────
def run_cell(*, knob: str, value: Any, idx: int, bars, artifacts: dict,
             notebook: str, scope: str, cache_hit: bool,
             extra_engine_kwargs: dict | None = None,
             vp_overrides: dict | None = None,
             detector_overrides: dict | None = None,
             structure_overrides: dict | None = None) -> dict[str, Any]:
    """Run ONE sweep cell and write its per-cell JSON immediately.

    ``knob`` / ``value`` describe the SINGLE-knob case (legacy CLI).
    ``extra_engine_kwargs`` / ``vp_overrides`` /
    ``detector_overrides`` carry the multi-knob case, where each key is
    routed to the correct surface (engine / VP / detector) by the
    allowlists.

    2026-10-24 (config consolidation): the RECIPE surface is gone.
    There is no ``params`` object any more — the engine reads its
    configuration from the kwargs dict alone, so a recipe-surface knob
    (e.g. ``fvg_inv_trade_sl_zone_mult``) has nowhere to go and now
    RAISES instead of silently no-op'ing.
    """
    engine_over: dict[str, Any] = {}
    vp_over: dict[str, Any] = {}
    det_over: dict[str, Any] = {}

    if knob and knob not in ("_canonical", "_multi"):
        from bt.optimal_config import (
            _ENGINE_OVERRIDES_ALLOWED,
            _VP_OVERRIDES_ALLOWED,
            _DETECTOR_OVERRIDES_ALLOWED,
        )
        if knob in _ENGINE_OVERRIDES_ALLOWED:
            engine_over[knob] = value
        elif knob in _VP_OVERRIDES_ALLOWED:
            vp_over[knob] = value
        elif knob in _DETECTOR_OVERRIDES_ALLOWED:
            det_over[knob] = value
        else:
            raise KeyError(
                f"illegal sweep knob {knob!r}; must be in "
                f"_ENGINE_OVERRIDES_ALLOWED, _VP_OVERRIDES_ALLOWED, "
                f"or _DETECTOR_OVERRIDES_ALLOWED"
            )
    if extra_engine_kwargs:
        engine_over.update(extra_engine_kwargs)
    if vp_overrides:
        vp_over.update(vp_overrides)
    if detector_overrides:
        det_over.update(detector_overrides)

    # 2026-10-23: structure build knobs. NOT engine kwargs -- they
    # shape the cached StructureState, so they must be routed to
    # ArtifactCache BEFORE the artifacts are built (the caller does
    # that) and must not reach build_engine_kwargs (the engine has no
    # such parameter). Recorded here for the audit trail only.
    st_over: dict[str, Any] = dict(structure_overrides or {})

    # Detector knobs are already baked into ``artifacts`` (zones /
    # structure) by ArtifactCache: the cache built them with this
    # cell's detector_overrides, so the engine's signal pipeline sees
    # the right zone set. No additional kwarg flows into the engine
    # call — ``run_vp_sniper_backtest_forex`` is read-only on
    # detector kwargs. Same for VP overrides (the vp_set inside
    # ``artifacts`` IS the profile those knobs built).
    #
    # Therefore all overrides from these two surfaces are recorded
    # in the per-cell JSON for audit, but NOT forwarded into the
    # engine kwargs (which would have silently no-op'd anyway
    # because the engine does not read them).

    # taker_fee_bps is a RATE derived from flat $ fees, so it depends
    # on the corpus spot. Using the 3000.0 default against a corpus
    # that trades near $4,060 under-charges fees by ~26% and flatters
    # every result. Use the corpus median spot.
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=artifacts["swing_cap"],
        p95_cap=artifacts["p95_cap"],
        structure=artifacts.get("structure"),
        # 2026-10-23: the momentum leg of the score filter. Omitting
        # it would silently degrade the blend to structure-only.
        vol_state=artifacts.get("vol_state"),
        **engine_over,
    )

    # NB94: on long files the per-bar mark-to-market equity curve
    # adds 330 MB of static allocation (41M floats on 1y XAUUSD), which
    # has tipped a memory-tight machine into OOM. The sweep driver
    # auto-disables it when the file is large enough that the curve
    # exceeds 5% of peak working set. The fallback path in
    # ``compute_metrics`` uses the closed-trade cumulative P&L, which
    # lags the drawdown by ``hold_secs`` per trade but is meaningful
    # for the canonical-recipe sweep (long hold times average > 1h).
    record_curve = len(bars) <= 8_000_000

    t0 = time.perf_counter()
    result, diag = run_vp_sniper_backtest_forex(
        bars=bars,
        zones=artifacts["zones"],
        vp_set=artifacts["vp_set"],
        lots=FOREX_LOTS,
        contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
        record_equity_curve=record_curve,
    )
    wall = time.perf_counter() - t0

    metrics = compute_metrics(result)
    record = {
        "ts_utc": datetime.now(timezone.utc).strftime(_TS_FMT),
        "notebook": notebook,
        "knob": knob,
        "value": _clean(value),
        "cell_idx": idx,
        "scope": [scope],
        "engine": "vp_multi_target",
        "wall_secs": wall,
        "cache_hit": cache_hit,
        "n_bars": int(len(bars)),
        "spot_median_usd": spot,
        "taker_fee_bps": ek.get("taker_fee_bps"),
        # The engine debits `avg * qty * bps/10000 * 2` at close, so the
        # realised round trip carries the same ×2 factor. Dropping it
        # here would report HALF the fee the engine actually charges.
        "round_trip_cost_usd": (
            FOREX_LOTS * FOREX_CONTRACT_SIZE * spot
            * float(ek.get("taker_fee_bps", 0.0)) / 10000.0 * 2.0),
        "metrics": _clean(metrics),
        # The engine's signal-lifecycle accounting. Persisted per cell
        # so a sweep can be audited without re-running: every emitted
        # signal is attributed to a gate (or to an order that was
        # placed / filled / cancelled).
        "engine_diag": _clean({
            "signals_emitted": diag.get("signals_emitted"),
            "signals_accounted": diag.get("signals_accounted"),
            "signals_unaccounted": diag.get("signals_unaccounted"),
            "gate_breakdown": diag.get("gate_breakdown"),
            "order_lifecycle": diag.get("order_lifecycle"),
            "entry_fill_model": diag.get("entry_fill_model"),
            "fill_model": diag.get("fill_model"),
            # 2026-10-23: proves the BoS/CHoCH gate actually fired,
            # rather than silently passing every signal.
            "structure_gate": diag.get("structure_gate"),
        }),
        "overrides_vs_canonical": _clean(
            {**engine_over, **vp_over, **det_over, **st_over}),
    }

    out_dir = RESULTS_ROOT / knob
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{scope}__cell_{idx}.json"
    with open(out_path, "w", encoding="utf-8") as fh:
        json.dump(record, fh, indent=2)
    record["_json_path"] = str(out_path)
    return record


def _parse_grid_values(vals: str) -> list[Any]:
    """Parse one ``--knobs`` value list into typed Python values.

    Order matters: int, then float, then BOOLEAN, then string. Booleans
    MUST be checked before falling through to ``str`` -- without it,
    ``structure_gate=False,True`` becomes the strings "False"/"True",
    both of which are truthy, and an A/B silently runs the same
    configuration twice. Observed on 2026-10-23: cells 2 and 3 of the
    tp2/structure sweep were byte-identical.
    """
    parsed: list[Any] = []
    for raw in vals.split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(int(raw))
        except ValueError:
            try:
                parsed.append(float(raw))
            except ValueError:
                if raw.lower() in ("true", "false"):
                    parsed.append(raw.lower() == "true")
                else:
                    parsed.append(raw)  # string knob (fill_model)
    return parsed


def _parse_grid_specs(knobs: str) -> list[tuple[str, list[Any]]]:
    """Parse a ``--knobs`` string into ``[(name, [values...]), ...]``."""
    grid_specs: list[tuple[str, list[Any]]] = []
    for part in knobs.split(";"):
        part = part.strip()
        if not part:
            continue
        name, _, vals = part.partition("=")
        name = name.strip()
        if not name or not vals.strip():
            raise SystemExit(f"malformed --knobs segment: {part!r}")
        parsed = _parse_grid_values(vals)
        if not parsed:
            raise SystemExit(f"no values for knob {name!r}")
        grid_specs.append((name, parsed))
    return grid_specs


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Cached single-knob sweep on XAUUSD 1s.")
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--knob", type=str, default="_canonical")
    ap.add_argument("--grid", type=str, default="",
                    help="Comma-separated values. Empty = [_canonical].")
    ap.add_argument("--knobs", type=str, default="",
                    help="Multi-knob grid. Format: 'name=v1,v2;name2=w1,w2'. "
                         "Cells are the CARTESIAN PRODUCT of every knob's "
                         "value list. Use this for the re-sweep matrix "
                         "(e.g. --knobs 'sl_zone_mult=4,8,12,20,40;"
                         "max_same_dir_open=10,20;fill_model=pessimistic').")
    ap.add_argument("--start", type=str, default=None,
                    help="Window start (YYYY-MM-DD). DEFAULT = one month "
                         "(2025-01-01). A longer window prints its "
                         "estimated cost and EXITS unless --i-know.")
    ap.add_argument("--end", type=str, default=None,
                    help="Window end (YYYY-MM-DD, exclusive). DEFAULT = "
                         "2025-02-01.")
    ap.add_argument("--scope", type=str, default="",
                    help="Scope label; defaults to a label derived from dates.")
    ap.add_argument("--notebook", type=str, default="")
    ap.add_argument("--no-cache", action="store_true",
                    help="Ignore any on-disk artifact cache.")
    ap.add_argument("--i-know", action="store_true",
                    help="APPROVAL GATE. A window longer than one month "
                         "prints its estimated cost and EXITS without "
                         "running unless this flag is passed. The flag "
                         "means 'the human has been asked'.")
    args = ap.parse_args()

    if not args.data.exists():
        raise SystemExit(f"data file not found: {args.data}")

    # ── DEFAULT WINDOW = ONE MONTH ───────────────────────────────
    # The old default was ``--start None --end None``, which loaded
    # the ENTIRE 1y corpus (41M bars, ~2 GB) for a sweep that was
    # almost always going to be read off one month. Defaulting to one
    # month makes the cheap thing the default thing; a longer window
    # is still available but has to be asked for.
    from runtime.sweeps.cost_estimate import DEFAULT_END, DEFAULT_START

    if args.start is None:
        args.start = DEFAULT_START
    if args.end is None:
        args.end = DEFAULT_END

    if args.scope:
        scope = args.scope
    else:
        s = (args.start or "start").replace("-", "")
        e = (args.end or "end").replace("-", "")
        scope = f"{args.data.stem}__{s}_{e}"

    notebook = args.notebook or (
        "canonical_run" if args.knob == "_canonical" else args.knob)

    # ── Grid ──────────────────────────────────────────────────────
    # Two modes:
    #   * --knobs  : multi-knob cartesian product.
    #   * --knob   : single knob (legacy), one knob name per run.
    grid_specs: list[tuple[str, list[Any]]] = []
    if args.knobs.strip():
        grid_specs = _parse_grid_specs(args.knobs)
        # Validate the knob names against the allowlists BEFORE running.
        from bt.optimal_config import (
            _ENGINE_OVERRIDES_ALLOWED,
            _VP_OVERRIDES_ALLOWED,
            _DETECTOR_OVERRIDES_ALLOWED,
            _STRUCTURE_OVERRIDES_ALLOWED,
        )
        allowed = (
            set(_ENGINE_OVERRIDES_ALLOWED)
            | set(_VP_OVERRIDES_ALLOWED)
            | set(_DETECTOR_OVERRIDES_ALLOWED)
            | set(_STRUCTURE_OVERRIDES_ALLOWED)
        )
        for name, _ in grid_specs:
            if name not in allowed:
                raise SystemExit(
                    f"illegal sweep knob {name!r}; must be in "
                    f"_ENGINE_OVERRIDES_ALLOWED, _VP_OVERRIDES_ALLOWED, "
                    f"_DETECTOR_OVERRIDES_ALLOWED, "
                    f"or _STRUCTURE_OVERRIDES_ALLOWED"
                )
    else:
        # Single-knob mode: route the knob to the right surface.
        # We accept VP knobs here too (the legacy --knob path), and
        # ``run_cell`` will fail loud if the knob is unknown.
        grid_specs = [(args.knob, (
            [float(x) if "." in x else int(x) for x in args.grid.split(",")]
            if args.grid.strip() else [None]
        ))]

    # Partition the grid into (engine_over, vp_over, detector_over)
    # buckets so each cell can build the right artifacts + kwargs.
    #
    # Engine knobs flow into ``build_engine_kwargs`` (per-cell).
    # VP knobs invalidate the artifact cache and flow into
    # ``build_volume_profiles``.
    # Detector knobs invalidate the artifact cache and flow into
    # ``detect_fvg``.
    #
    # There is NO recipe bucket: the recipe dataclass was deleted on
    # 2026-10-24 (config consolidation). A recipe-surface knob now
    # FAILS LOUD rather than silently measuring nothing.
    from bt.optimal_config import (
        _ENGINE_OVERRIDES_ALLOWED as _EOA,
        _VP_OVERRIDES_ALLOWED as _VOA,
        _DETECTOR_OVERRIDES_ALLOWED as _DOA,
        _STRUCTURE_OVERRIDES_ALLOWED as _SOA,
    )

    # 2026-10-23: ``structure_pivot_len`` / ``structure_resample_secs``
    # are NOT engine kwargs -- they shape the cached StructureState,
    # so they are pulled OUT before partition and routed to
    # ArtifactCache. Leaving them in ``engine_over`` would hand the
    # engine a parameter it does not have.
    #
    # 2026-10-24 (config consolidation): the translation from the
    # canonical knob name to the target's parameter name lives in
    # ``bt.optimal_config.build_structure_kwargs`` -- the SINGLE
    # translation point. This driver used to keep its OWN copy of
    # the mapping (``_STRUCTURE_SWEEP_KEYS``) and assert it against
    # the allowlist. The assert was a band-aid over a duplicated
    # source of truth; the copy is now gone.
    _STRUCTURE_SWEEP_KEYS = set(_SOA)

    def _partition(overrides: dict[str, Any]) -> tuple[
        dict[str, Any], dict[str, Any], dict[str, Any]
    ]:
        e, v, d = {}, {}, {}
        for k, val in overrides.items():
            if k in _STRUCTURE_SWEEP_KEYS:
                # handled by the caller before partition
                continue
            if k in _EOA:
                e[k] = val
            elif k in _VOA:
                v[k] = val
            elif k in _DOA:
                d[k] = val
            # else: validate already rejected, so we never get here.
        return e, v, d

    # Cartesian product, last knob varies fastest (so a single-knob
    # sweep produces the same cell ordering as before).
    import itertools
    cells: list[tuple[dict, dict, dict, dict]] = []
    for combo in itertools.product(*[vals for _, vals in grid_specs]):
        overrides = {name: val for (name, _), val in zip(grid_specs, combo)}
        # The "_canonical" pseudo-knob means "vary nothing" — it must
        # not leak into the override dicts.
        overrides.pop("_canonical", None)
        # 2026-10-23: peel the structure build knobs out BEFORE
        # partition, so they never reach build_engine_kwargs (the
        # engine has no such parameters) and instead rebuild the
        # cached StructureState. The keys stay CANONICAL here
        # (``structure_pivot_len``); the translation to the target's
        # parameter names happens inside ``build_structure_kwargs``.
        st_cell = {
            _k: overrides.pop(_k)
            for _k in _STRUCTURE_SWEEP_KEYS
            if _k in overrides
        }
        cells.append((*_partition(overrides), st_cell))

    print(f"== cached sweep ==")
    print(f"data    : {args.data}")
    print(f"scope   : {scope}")
    print(f"knob    : {args.knob}")
    print(f"grid    : {grid_specs}")
    print(f"cells   : {len(cells)}")

    # ── APPROVAL GATE ────────────────────────────────────────────
    # The operating rule for this repo: sweep ONE month by default,
    # and ASK before anything longer. This makes "ask" mechanical
    # instead of a matter of memory -- the estimate is computed from
    # the parquet footer alone (no bars loaded, no engine run), so
    # the pre-flight is free.
    #
    # Why a gate and not just a rule: the default used to be
    # ``--start None --end None``, which silently loaded the ENTIRE
    # 1y corpus (41M bars, ~2 GB) for a sweep that was usually
    # going to be read off one month anyway. That is how "sweeps
    # happily, progress takes too long" happens.
    from runtime.sweeps.cost_estimate import estimate_sweep

    est = estimate_sweep(
        args.data, args.start, args.end, n_cells=len(cells),
        artifacts_cached=False,
    )
    print()
    print(est.render())
    print()
    if est.needs_approval and not args.i_know:
        print("ABORTED: window is longer than one month. "
              "Re-run with --i-know to execute.")
        raise SystemExit(2)

    t0 = time.perf_counter()
    print("loading bars ...", flush=True)
    bars = load_bars(args.data, args.start, args.end)
    print(f"  {len(bars):,} bars in {time.perf_counter() - t0:.1f}s "
          f"({bars['time'].min()} .. {bars['time'].max()})")
    if len(bars) == 0:
        raise SystemExit("no bars in range")

    if args.no_cache:
        # Best-effort: wipe any cached pickle whose name starts with
        # this scope. Each per-VP-shape cache lives under its own
        # extended scope, so we pattern-match the full family.
        import glob
        for stale in glob.glob(str(CACHE_ROOT / f"{scope}*")):
            try:
                Path(stale).unlink()
            except OSError:
                pass
        print("  --no-cache: cleared any cached pickles under "
              f"{scope}*")

    cell_caches: dict[tuple, Any] = {}

    def _ensure_cache(vp_over: dict[str, Any],
                      detector_over: dict[str, Any],
                      structure_over: dict[str, Any] | None = None
                      ) -> Any:
        # Cache key is (vp_over, detector_over, structure_over): the
        # fingerprint includes all three, so two cells differing in
        # any dimension get separate pickles but reuse
        # zones/vp_set/swing/p95 when the other dims match.
        # ``structure_over`` matters because the StructureState is
        # cached -- a pivot_len sweep MUST NOT reuse the wrong pivots.
        key = (tuple(sorted(vp_over.items())),
               tuple(sorted(detector_over.items())),
               tuple(sorted((structure_over or {}).items())))
        if key not in cell_caches:
            cell_caches[key] = ArtifactCache(
                args.data, scope, args.start, args.end,
                vp_overrides=vp_over,
                detector_overrides=detector_over,
                structure_overrides=structure_over,
            )
        return cell_caches[key]

    def _build_artifacts(vp_over: dict[str, Any],
                         detector_over: dict[str, Any],
                         structure_over: dict[str, Any] | None = None
                         ) -> dict[str, Any]:
        cache = _ensure_cache(vp_over, detector_over, structure_over)
        art = cache.build_or_load(bars)
        tag = "HIT " if art["_cache_hit"] else "MISS"
        print(f"  artifacts {tag} vp_over={vp_over or '{}'} "
              f"det_over={detector_over or '{}'} "
              f"st_over={structure_over or '{}'} "
              f"-> {art['n_zones']:,} zones", flush=True)
        if not art["_cache_hit"]:
            for k, v in art["build_secs"].items():
                print(f"    {k:10s} {v:7.1f}s")
        return art

    records: list[dict[str, Any]] = []
    for idx, (engine_over, vp_over, det_over, st_over) in enumerate(cells):
        merged = {**engine_over, **vp_over, **det_over, **st_over}
        over_desc = ", ".join(f"{k}={v}" for k, v in merged.items())
        print(f"\n-- cell {idx}: {over_desc} --", flush=True)
        art = _build_artifacts(vp_over, det_over, st_over)
        # Single-override cell keeps the legacy scalar contract
        # (knob name + scalar value); multi-override cell uses
        # the "_multi" pseudo-knob with the override dict as value.
        if len(merged) == 1:
            (only_k, only_v), = merged.items()
            rec = run_cell(
                knob=only_k, value=only_v,
                idx=idx, bars=bars, artifacts=art,
                notebook=notebook, scope=scope,
                cache_hit=art["_cache_hit"],
            )
        else:
            rec = run_cell(
                knob="_multi", value=merged,
                idx=idx, bars=bars, artifacts=art,
                notebook=notebook, scope=scope,
                cache_hit=art["_cache_hit"],
                extra_engine_kwargs=engine_over,
                vp_overrides=vp_over,
                detector_overrides=det_over,
                structure_overrides=st_over,
            )
        records.append(rec)
        m = rec["metrics"]
        print(f"   trades={m['n_trades']:>6}  "
              f"signals={m['n_signals_emitted']:>8}  "
              f"net=${m['total_pnl_net_usd']:>10.2f}  "
              f"fees=${m['total_fees_usd']:>9.2f}  "
              f"EV=${m['ev_per_trade_usd']:>8.3f}  "
              f"hit={m['hit_rate']:.3f}  "
              f"maxDD=${m['max_drawdown_usd']:>9.2f}  "
              f"PF={m['profit_factor'] if m['profit_factor'] is None else round(m['profit_factor'], 2)}  "
              f"{rec['wall_secs']:.1f}s")
        print(f"   exits: {m['exit_reason_breakdown']}")
        print(f"   -> {rec['_json_path']}")

    print(f"\n== done: {len(records)} cell(s) in "
          f"{sum(r['wall_secs'] for r in records):.1f}s of engine time ==")


if __name__ == "__main__":
    main()