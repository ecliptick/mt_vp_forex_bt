"""TA GATE SWEEP — how much of the book survives when a TA opens the gate.

THE QUESTION
============

    "try some momentum and trend following TAs. do a quick sweep on the
     MR TAs, where trades must align with the TA's signal (gate open)
     and upon signal change close open trades. tell me the trade
     number decay based on this"

So the measurement is the **TRADE-COUNT DECAY**: the canonical book
has 51,075 legs; after a TA gate it has N; the decay is
``1 - N / 51075``.

WHAT IS MEASURED, AND IN WHICH ORDER
====================================

For every TA cell, on the canonical corpus:

1. **THE GATE AT THE SIGNAL BAR.** A leg survives only if the TA
   signal at the leg's SIGNAL bar equals the leg's side. This is the
   "gate open" rule. It is evaluated at the SIGNAL bar — the causal
   bar — because the engine's own per-side gates are evaluated there
   too, and the repo's rule is "a gate that the engine evaluates at
   bar S must be measured at bar S".

2. **THE GATE AT THE FILL.** The engine's D2 resting-limit order
   re-checks the book at fill time (``policy_at_fill``). The gate does
   the same: if the signal flipped between the signal bar and the
   fill, the order never becomes a trade.

3. **THE EARLY CLOSE.** A surviving trade is closed at the first 1m
   boundary where the signal stops agreeing with its side. The fill is
   the NEXT 1m bar's OPEN, because that is the first tradeable price
   after the change is known.

The decay is reported at BOTH stages, because they are different
questions:

* **gate-only** — the decay from the gate alone (the "gate open"
  rule). This is the number that says how much of the book the TA
  actually endorses.
* **gate + early close** — the same book, but the surviving trades
  are also cut when the signal changes. The trade COUNT is IDENTICAL
  (an early close does not create or destroy a trade), but the P&L
  changes.

So the trade-number decay is a property of the GATE, and the early
close only changes the money. The report states both.

THE TWO NUMBERS THAT MATTER
===========================

* **the decay** — how many legs the TA removes.
* **the surviving book's gross/leg vs the measured fee/leg** — whether
  what is left is worth trading. A gate that removes 90% of the book
  and leaves a book that still cannot cover the round trip has not
  found anything; it has just found a smaller book.

THE PLACEBO IS THE BOOK ITSELF
===============================

There is no need for a random-entry placebo here, and the reason is
structural: the gate does not change WHERE a trade enters or WHEN it
exits (except for the early close, which is the rule under test). It
only changes WHICH trades exist. The book is the control: the same
51,075 legs, ungated.

The comparison that IS meaningful is the **direction-matched random
gate**: take the TA's own gate as a MASK (the set of bars where the
TA agrees with the book's side) and randomise only the TIME of the
mask. That destroys the timing while keeping the fraction of the book
that the TA endorses. It is the exchangeable control for "did the TA
pick the RIGHT bars, or would any mask of the same openness do?".

Usage
-----
    python -m runtime.diagnostics.ta_gate_sweep
    python -m runtime.diagnostics.ta_gate_sweep --families adx_trend supertrend
    python -m runtime.diagnostics.ta_gate_sweep --no-placebo
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from runtime.diagnostics.leg_ledger import LEDGER_PATH  # noqa: E402
from runtime.diagnostics.ta_1m_core import (  # noqa: E402
    NS_PER_MIN,
    STRATEGY_GRIDS,
    Bars1m,
)
from runtime.diagnostics.ta_gate import (  # noqa: E402
    Bars1mGrid,
    apply_gate,
    build_bars_1m_streaming,
    fee_bps_from_legs,
    load_1s_closes,
    load_legs,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA  # noqa: E402

REPORT_DIR = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
REPORT_PATH = REPORT_DIR / "ta_gate_sweep.json"

#: The default family set: the momentum/trend families plus the MR
#: families the user asked to sweep.
DEFAULT_FAMILIES: tuple[str, ...] = (
    # momentum / trend following
    "adx_trend", "supertrend", "ts_ma_cross", "momentum_rsi",
    "cci", "willr", "roc_zero", "kama", "squeeze",
    # mean reversion
    "zscore_revert", "range_fade", "rsi_revert", "bb_fade",
    "vwap_revert",
)

#: The families that are MOMENTUM/TREND (as opposed to MR). The report
#: groups by this so the two halves can be read separately.
TREND_FAMILIES: frozenset[str] = frozenset({
    "adx_trend", "supertrend", "ts_ma_cross", "momentum_rsi",
    "cci", "willr", "roc_zero", "kama", "squeeze",
    # the pre-existing crossing rules are trend following too
    "ema_cross", "macd", "donchian", "slope_trend", "stoch_cross",
    "atr_expansion", "keltner_break",
})


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/Inf -> None, numpy scalars -> python."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (np.floating,)):
        obj = float(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _f(x: Any, spec: str = "8.4f") -> str:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "n/a"
    if not math.isfinite(v):
        return "n/a"
    return format(v, spec)


def _t(x: np.ndarray) -> float:
    """The t-statistic of a mean, with its standard error."""
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 2:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    return float(x.mean() / (sd / math.sqrt(n)))


def _se(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 2:
        return float("nan")
    return float(x.std(ddof=1) / math.sqrt(n))


def _book_stats(gross: np.ndarray, fee: np.ndarray, filled: np.ndarray,
                *, label: str, drift: np.ndarray | None = None,
                excess: np.ndarray | None = None) -> dict[str, Any]:
    """The scorecard for one gated book."""
    g = gross[filled]
    f = fee[filled]
    n = int(g.size)
    if n == 0:
        return {
            "label": label, "n_legs": 0, "gross": 0.0, "fees": 0.0,
            "net": 0.0, "gross_per_leg": 0.0, "fee_per_leg": 0.0,
            "gross_over_fee": 0.0, "hit_rate": 0.0,
            "gross_per_leg_t": float("nan"), "net_per_leg": 0.0,
            "net_per_leg_t": float("nan"),
            "drift_total": 0.0, "excess_total": 0.0,
            "excess_per_leg": 0.0, "excess_per_leg_t": float("nan"),
            "drift_share_of_gross": float("nan"),
        }
    net = g - f
    gross_per_leg = float(g.mean())
    fee_per_leg = float(f.mean())
    out = {
        "label": label,
        "n_legs": n,
        "gross": float(g.sum()),
        "fees": float(f.sum()),
        "net": float(net.sum()),
        "gross_per_leg": gross_per_leg,
        "fee_per_leg": fee_per_leg,
        "gross_over_fee": (gross_per_leg / fee_per_leg
                           if fee_per_leg > 0 else float("nan")),
        "hit_rate": float((g > 0).mean()),
        "gross_per_leg_t": _t(g),
        "net_per_leg": float(net.mean()),
        "net_per_leg_t": _t(net),
        "gross_per_leg_se": _se(g),
    }
    # The DRIFT / EXCESS decomposition — the repo's decisive test.
    if drift is not None and excess is not None:
        d = drift[filled]
        e = excess[filled]
        out["drift_total"] = float(d.sum())
        out["excess_total"] = float(e.sum())
        out["excess_per_leg"] = float(e.mean())
        out["excess_per_leg_t"] = _t(e)
        # A ratio of two near-zero numbers is meaningless; the report
        # says so and the reader must read the ABSOLUTE totals.
        gross_total = float(g.sum())
        out["drift_share_of_gross"] = (
            float(d.sum()) / gross_total
            if abs(gross_total) > 1e-6 else float("nan"))
    return out


def _time_shift_placebo(legs: dict[str, np.ndarray], bars: Bars1m, bps: float,
                        signal: np.ndarray, *, n_arms: int, seed: int,
                        grid: Bars1mGrid,
                        ) -> list[dict[str, Any]]:
    """PLACEBO 1 — the rule's own pattern, moved to a random TIME.

    The TA's signal array is circularly shifted by a random number of
    1m bars and the gate is re-applied. The rule's structure is
    preserved exactly: its state durations, its abstentions, its
    flips, its balance of long and short. Only the TIMING is
    destroyed.

    This is the exchangeable control for "did the TA pick the RIGHT
    moments, or would the same rule at the wrong moments do the same?"
    """
    out: list[dict[str, Any]] = []
    rng = np.random.default_rng(seed)
    n_m = int(signal.size)
    for arm in range(n_arms):
        shift = int(rng.integers(1, max(2, n_m)))
        shifted = np.roll(signal, shift)
        res = apply_gate(legs, shifted, bars, bps=bps, grid=grid)
        filled = res.filled
        g = res.gross_gate_close[filled]
        f = res.fee_gate_close[filled]
        out.append({
            "arm": arm,
            "shift_bars": shift,
            "n_filled": int(filled.sum()),
            "net": float((g - f).sum()),
            "net_per_leg": float((g - f).mean()) if g.size else 0.0,
            "gross_per_leg": float(g.mean()) if g.size else 0.0,
        })
    return out


def _random_subset_placebo(legs: dict[str, np.ndarray], bps: float,
                           n_keep: int, *, n_arms: int, seed: int,
                           ) -> list[dict[str, Any]]:
    """PLACEBO 2 — a RANDOM subset of the SAME SIZE.

    Keep exactly ``n_keep`` legs, chosen at random from the whole
    book. The size is matched to the TA's gate, so the trade count is
    identical and only the SELECTION differs.

    This is the control for "did the TA choose the RIGHT trades, or
    would any subset of the same size have done the same?" — the
    sharpest question the trade-count decay raises, because a smaller
    book can have a better gross/leg purely by luck of the draw.
    """
    out: list[dict[str, Any]] = []
    rng = np.random.default_rng(seed)
    n = int(legs["side"].size)
    if n_keep <= 0 or n_keep > n:
        return out
    gross = legs["gross"].astype(np.float64)
    fee = legs["fee"].astype(np.float64)
    side = legs["side"].astype(np.int64)
    for arm in range(n_arms):
        idx = rng.choice(n, size=n_keep, replace=False)
        g = gross[idx]
        f = fee[idx]
        out.append({
            "arm": arm,
            "n_filled": int(n_keep),
            "net": float((g - f).sum()),
            "net_per_leg": float((g - f).mean()),
            "gross_per_leg": float(g.mean()),
            "n_long": int((side[idx] > 0).sum()),
        })
    return out


def _placebo_p(real: float, arms: list[dict[str, Any]], key: str) -> float:
    """The empirical p-value of the real arm against the placebo arms."""
    if not arms:
        return 1.0
    vals = np.array([a[key] for a in arms], dtype=np.float64)
    vals = vals[np.isfinite(vals)]
    if vals.size == 0:
        return 1.0
    return float((1.0 + (vals >= real).sum()) / (vals.size + 1.0))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--families", type=str, default="",
                    help="Space-separated family names (default: the "
                         "momentum/trend + MR set).")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--no-placebo", action="store_true")
    ap.add_argument("--n-arms", type=int, default=40,
                    help="Placebo arms per cell (default 40).")
    ap.add_argument("--seed", type=int, default=20261025)
    ap.add_argument("--no-cache", action="store_true",
                    help="Rebuild the 1m bars instead of reusing the cache.")
    args = ap.parse_args()

    if not args.data.exists():
        raise SystemExit(f"data file not found: {args.data}")
    if not args.ledger.exists():
        raise SystemExit(
            f"no ledger at {args.ledger}. Build it first:\n"
            f"    python -m runtime.diagnostics.leg_ledger")

    families = (tuple(args.families.split()) if args.families
                else DEFAULT_FAMILIES)
    unknown = [f for f in families if f not in STRATEGY_GRIDS]
    if unknown:
        raise SystemExit(
            f"unknown families {unknown}. Known: {sorted(STRATEGY_GRIDS)}")

    t0 = time.perf_counter()
    print("== TA gate sweep ==", flush=True)
    print(f"data   : {args.data}")
    print(f"ledger : {args.ledger}")
    print(f"families: {len(families)} -> {list(families)}")
    print(flush=True)

    # ── THE BOOK ─────────────────────────────────────────────────
    legs = load_legs(args.ledger)
    meta = legs.pop("_meta")
    n_legs = int(legs["side"].size)
    bps = fee_bps_from_legs(legs)
    print(f"book   : {n_legs:,} legs   fee bps={bps:.10f}   "
          f"built {meta.get('built_utc')}", flush=True)

    # ── THE 1m BARS ──────────────────────────────────────────────
    t1 = time.perf_counter()
    cache = REPORT_DIR / "ta_gate_bars_1m.npz"
    bars = None
    grid = None
    if cache.exists() and not args.no_cache:
        try:
            z = np.load(cache)
            # ``n_rows`` was saved as a 1-element ARRAY, so it reads
            # back as a 1-D array — ``int()`` on that raises in numpy 2.
            nr = np.asarray(z["n_rows"]).reshape(-1)
            if int(nr[0]) == int(args.data.stat().st_size):
                bars = Bars1m(
                    time_ns=z["time_ns"], open=z["open"], high=z["high"],
                    low=z["low"], close=z["close"], volume=z["volume"],
                    n_ticks=z["n_ticks"], atr=z["atr"], atr_n=14,
                )
                row_start = z["row_start"]
                row_end = z["row_end"]
                print(f"1m bars: {len(bars):,} (cached)", flush=True)
        except (OSError, KeyError, ValueError):
            bars = None
    if bars is None:
        grid = build_bars_1m_streaming(args.data, with_row_map=True)
        bars = grid.bars
        print(f"1m bars: {len(bars):,} in {time.perf_counter() - t1:.1f}s",
              flush=True)
        try:
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            np.savez_compressed(
                cache,
                time_ns=bars.time_ns, open=bars.open, high=bars.high,
                low=bars.low, close=bars.close, volume=bars.volume,
                n_ticks=bars.n_ticks, atr=bars.atr,
                row_start=grid.row_start, row_end=grid.row_end,
                n_rows=np.array([args.data.stat().st_size]),
            )
        except OSError:
            pass
    if grid is None:
        # The cached path: rebuild the grid from the cached row map and
        # the parquet's close column.
        t1 = time.perf_counter()
        grid = Bars1mGrid(
            bars=bars, row_start=row_start, row_end=row_end, path=args.data,
            closes_1s=load_1s_closes(args.data),
        )
        print(f"1s closes: {grid.closes_1s.size:,} in "
              f"{time.perf_counter() - t1:.1f}s", flush=True)

    # ── THE BASELINE (the ungated book) ──────────────────────────
    # The ledger ALREADY carries ``drift_usd`` / ``excess_usd`` on its
    # own 1s grid, so the baseline's decomposition is the canonical
    # one — no re-derivation needed.
    baseline = _book_stats(
        legs["gross"].astype(np.float64),
        legs["fee"].astype(np.float64),
        np.ones(n_legs, dtype=bool),
        label="BASELINE (canonical book, ungated)",
        drift=legs["drift_usd"].astype(np.float64),
        excess=legs["excess_usd"].astype(np.float64),
    )
    print(f"baseline: gross ${baseline['gross']:,.2f}  "
          f"net ${baseline['net']:,.2f}  "
          f"gross/leg ${baseline['gross_per_leg']:.4f}  "
          f"fee/leg ${baseline['fee_per_leg']:.4f}  "
          f"ratio {baseline['gross_over_fee']:.2f}x", flush=True)

    cells: list[dict[str, Any]] = []
    t2 = time.perf_counter()
    for fam in families:
        fn, param_grid = STRATEGY_GRIDS[fam]
        for params in param_grid:
            sig = np.asarray(fn(bars, params), dtype=np.int8)
            res = apply_gate(legs, sig, bars, bps=bps, grid=grid)

            n_filled = res.n_filled
            decay = 1.0 - (n_filled / n_legs) if n_legs else float("nan")
            decay_sig = 1.0 - (res.n_gate_pass / n_legs) if n_legs else float("nan")

            gate_only = _book_stats(
                res.gross_gate_only, res.fee_gate_only, res.filled,
                label="gate_only",
                drift=res.drift_gate_only, excess=res.excess_gate_only)
            gate_close = _book_stats(
                res.gross_gate_close, res.fee_gate_close, res.filled,
                label="gate+close",
                drift=res.drift_gate_close, excess=res.excess_gate_close)

            inner = ",".join(f"{k}={v}" for k, v in sorted(params.items()))
            label = f"{fam}({inner})" if inner else fam

            # ── THE PLACEBO ARMS ───────────────────────────────
            # 1. the rule's own pattern, moved to a random TIME.
            # 2. a RANDOM subset of the same size (the sharpest
            #    control: same trade count, different selection).
            shift_arms: list[dict[str, Any]] = []
            subset_arms: list[dict[str, Any]] = []
            if not args.no_placebo:
                # A STABLE seed per cell. ``hash()`` on a str is salted
                # per process (PYTHONHASHSEED), so the placebo arms
                # would differ between runs and the report would not
                # be reproducible.
                arm_seed = (args.seed
                            + int(hashlib.sha256(label.encode()).hexdigest()[:8],
                                  16))
                shift_arms = _time_shift_placebo(
                    legs, bars, bps, sig, n_arms=args.n_arms,
                    seed=arm_seed, grid=grid)
                subset_arms = _random_subset_placebo(
                    legs, bps, n_filled, n_arms=args.n_arms,
                    seed=arm_seed + 999_983)
            real_net_per_leg = gate_close["net_per_leg"]
            real_gross_per_leg = gate_close["gross_per_leg"]
            p_shift = _placebo_p(real_net_per_leg, shift_arms,
                                 "net_per_leg")
            p_subset = _placebo_p(real_net_per_leg, subset_arms,
                                  "net_per_leg")
            p_gross_subset = _placebo_p(real_gross_per_leg, subset_arms,
                                        "gross_per_leg")

            # The per-side split — the long/short asymmetry has been
            # the single most actionable split in this repo.
            side = legs["side"].astype(np.int64)
            per_side = {}
            for s, nm in ((1, "long"), (-1, "short")):
                m = res.filled & (side == s)
                per_side[nm] = {
                    "n": int(m.sum()),
                    "gross": float(res.gross_gate_close[m].sum()),
                    "net": float(res.net_gate_close[m].sum()),
                    "gross_per_leg": (float(res.gross_gate_close[m].mean())
                                      if int(m.sum()) else 0.0),
                }

            cells.append({
                "label": label,
                "family": fam,
                "params": params,
                "is_trend": fam in TREND_FAMILIES,
                "n_legs": n_legs,
                "n_gate_pass": res.n_gate_pass,
                "n_filled": n_filled,
                "n_early_closed": res.n_early_closed,
                "n_gated_out_signal": res.n_gated_out_signal,
                "n_cancelled_at_fill": res.n_cancelled_at_fill,
                "decay_signal": decay_sig,
                "decay": decay,
                "surviving_pct": 100.0 * (1.0 - decay),
                "gate_only": gate_only,
                "gate_close": gate_close,
                "per_side": per_side,
                "net_delta_vs_baseline": gate_close["net"] - baseline["net"],
                "placebo": {
                    "n_arms": args.n_arms,
                    "p_net_vs_time_shift": p_shift,
                    "p_net_vs_random_subset": p_subset,
                    "p_gross_vs_random_subset": p_gross_subset,
                    "time_shift_mean_net_per_leg": (
                        float(np.mean([a["net_per_leg"] for a in shift_arms]))
                        if shift_arms else float("nan")),
                    "random_subset_mean_net_per_leg": (
                        float(np.mean([a["net_per_leg"] for a in subset_arms]))
                        if subset_arms else float("nan")),
                    "random_subset_mean_gross_per_leg": (
                        float(np.mean([a["gross_per_leg"]
                                       for a in subset_arms]))
                        if subset_arms else float("nan")),
                },
            })
            print(f"  {label[:52]:<52} kept {n_filled:>6,} "
                  f"({100.0 * (1.0 - decay):>5.1f}%)  "
                  f"net ${gate_close['net']:>10,.0f}", flush=True)

    sweep_secs = time.perf_counter() - t2

    # ── REPORT ───────────────────────────────────────────────────
    payload = {
        "ts_utc": pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": str(args.data),
        "ledger": str(args.ledger),
        "ledger_built_utc": meta.get("built_utc"),
        "ledger_n_legs": n_legs,
        "fee_bps": bps,
        "n_1m_bars": int(len(bars)),
        "baseline": baseline,
        "families": list(families),
        "sweep_secs": sweep_secs,
        "total_secs": time.perf_counter() - t0,
        "cells": cells,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(_clean(payload), indent=2, default=str), encoding="utf-8")
    print(f"\nwrote {REPORT_PATH}")

    if args.json_only:
        print(json.dumps(_clean(payload), indent=2, default=str))
        return

    _print_tables(cells, baseline, n_legs)


def _print_tables(cells: list[dict[str, Any]], baseline: dict[str, Any],
                  n_legs: int) -> None:
    print()
    print("=" * 150)
    print("TA GATE — TRADE-COUNT DECAY  (gate open at the signal bar; "
          "close on signal change)")
    print("=" * 150)
    print(f"canonical book: {n_legs:,} legs   "
          f"gross ${baseline['gross']:,.2f}   net ${baseline['net']:,.2f}   "
          f"gross/leg ${baseline['gross_per_leg']:.4f}   "
          f"fee/leg ${baseline['fee_per_leg']:.4f}   "
          f"ratio {baseline['gross_over_fee']:.2f}x")
    print()
    print(f"{'':4}{'cell':<50} {'kept':>7} {'kept%':>7} {'decay':>7} "
          f"{'gOnly':>8} {'xOnly':>6} {'ex/leg':>8} {'exT':>6} "
          f"{'gClose':>8} {'xCls':>6} {'exCls':>8} {'exT':>6} {'net':>11} "
          f"{'early':>7} {'pSub':>6} {'pShf':>6}")
    print("-" * 150)
    for c in sorted(cells, key=lambda c: c["decay"]):
        go = c["gate_only"]
        gc = c["gate_close"]
        gr = "T" if c["is_trend"] else "M"
        pl = c.get("placebo") or {}
        print(f"{gr} {c['label'][:48]:<48} "
              f"{c['n_filled']:>7,} {c['surviving_pct']:>6.1f}% "
              f"{c['decay']:>6.1%} "
              f"{go['gross_per_leg']:>8.4f} {go['gross_over_fee']:>5.2f}x "
              f"{go.get('excess_per_leg', float('nan')):>8.4f} "
              f"{_f(go.get('excess_per_leg_t'), '6.2f'):>6} "
              f"{gc['gross_per_leg']:>8.4f} {gc['gross_over_fee']:>5.2f}x "
              f"{gc.get('excess_per_leg', float('nan')):>8.4f} "
              f"{_f(gc.get('excess_per_leg_t'), '6.2f'):>6} "
              f"{gc['net']:>11,.0f} "
              f"{c['n_early_closed']:>7,} "
              f"{_f(pl.get('p_net_vs_random_subset'), '6.3f'):>6} "
              f"{_f(pl.get('p_net_vs_time_shift'), '6.3f'):>6}")
    print("-" * 150)
    print("T = momentum/trend family   M = mean-reversion family")
    print("kept% = share of the canonical book that survives the gate   "
          "decay = 1 - kept%")
    print("gOnly = gross/leg with the gate ONLY (no early close)   "
          "gClose = with the early close")
    print("xOnly/xCls = gross/leg ÷ fee/leg   ex/leg = the TIMING component "
          "(gross - drift)   exT = its t-stat")
    print("net = the gated book with the early close   "
          "early = legs closed early because the signal changed")
    print("pSub = p(net/leg) vs a RANDOM SUBSET of the same size   "
          "pShf = p(net/leg) vs the rule moved to a random TIME")

    # The best cell by the criterion that matters.
    print()
    print("=" * 150)
    print("VERDICT")
    print("=" * 150)
    viable = [c for c in cells
              if c["gate_close"]["gross_over_fee"] >= 1.0]
    print(f"cells measured                       : {len(cells)}")
    print(f"cells whose surviving book covers the "
          f"round trip (x>=1) : {len(viable)}")
    if viable:
        best = max(viable, key=lambda c: c["gate_close"]["net_per_leg"])
        print(f"best by net per surviving leg       : {best['label']}")
        print(f"  kept {best['n_filled']:,} of {n_legs:,} "
              f"(decay {best['decay']:.1%}), "
              f"net/leg ${best['gate_close']['net_per_leg']:.4f}, "
              f"x{best['gate_close']['gross_over_fee']:.2f}")
    else:
        print("NO cell's surviving book covers the round trip.")
        print("The gate shrinks the book but the survivors still do not "
              "pay for themselves.")
    # The tightest gate that still leaves a book worth trading.
    tight = [c for c in cells
             if c["gate_close"]["gross_over_fee"] >= 1.0
             and c["n_filled"] >= 100]
    if tight:
        t2 = min(tight, key=lambda c: c["n_filled"])
        print(f"tightest viable gate                : {t2['label']} "
              f"(kept {t2['n_filled']:,}, decay {t2['decay']:.1%})")


if __name__ == "__main__":
    main()
