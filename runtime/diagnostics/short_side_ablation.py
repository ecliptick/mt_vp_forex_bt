"""SHORT-SIDE ABLATION — is the short side a pure drag? (2026-10-24).

The promoted canonical recipe (``vol_state_min_range_norm = 3.0``)
produced the repo's first direction-independent timing result:

    long   alpha = +$34.14/slice   gross_total = +$305.41   corr = +0.315
    short  alpha = -$12.81/slice   gross_total = -$59.19   corr = +0.448

The long side carries the edge. The short side is **gross-negative
in aggregate** and has **negative alpha**. That is the signature of
a side that is paying fees for a direction bet the strategy is not
actually good at.

This module measures the decisive question:

    **Does dropping (or separately gating) the short side improve
    the strategy's NET, and does it preserve the direction-independent
    property?**

The second half matters as much as the first. A long-only strategy
on a backtest corpus that trends up is a BETA PROXY -- exactly the
defect this repo has spent its history diagnosing. So the module
reports, for every arm:

  * per-slice gross / net / legs
  * the FLAT-slice result (the decisive chop test)
  * the per-side correlation with the market return
  * the aggregate beta and alpha

An arm is only interesting if it improves NET **and** keeps the flat
slices non-negative **and** does not re-introduce a high market
correlation.

Arms tested
-----------
  both      -- the canonical control (both sides trade)
  long_only -- no shorts at all
  long_gate -- shorts allowed, but only when a per-side vol measure
               clears a threshold

Usage
-----
    python -m runtime.diagnostics.short_side_ablation
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/short_side_ablation.json")

# 8 slices: 3 up, 2 down, 3 flat -- the same corpus the beta
# diagnostic uses, so the numbers are comparable.
SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "up"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "up"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "up"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "down"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "down"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2024-06 FLAT", "2024-06-01", "2024-07-01", "flat"),
    ("2026-04 FLAT", "2026-04-01", "2026-05-01", "flat"),
]


def _artifacts(tag: str, start: str, end: str, bars) -> dict:
    return ArtifactCache(DATA, f"ssa_{tag}", start, end).build_or_load(bars)


def _run(label: str, start: str, end: str, arm: str) -> dict:
    """One slice under one arm.

    ``arm`` is one of:
      both      -- canonical control
      long_only -- post-hoc: discard every short leg
    """
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    trades = list(res.trades)
    if arm == "long_only":
        trades = [t for t in trades if int(t.direction) > 0]
    elif arm != "both":
        raise ValueError(f"unknown arm {arm!r}")

    mkt = float((bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0)
    longs = [t for t in trades if int(t.direction) > 0]
    shorts = [t for t in trades if int(t.direction) < 0]
    return {
        "label": label,
        "arm": arm,
        "mkt_ret_pct": mkt,
        "legs": len(trades),
        "gross": float(sum(t.pnl_gross_usd for t in trades)),
        "net": float(sum(t.pnl_net_usd for t in trades)),
        "fees": float(sum(t.fee_usd for t in trades)),
        "long_legs": len(longs),
        "long_gross": float(sum(t.pnl_gross_usd for t in longs)),
        "long_net": float(sum(t.pnl_net_usd for t in longs)),
        "short_legs": len(shorts),
        "short_gross": float(sum(t.pnl_gross_usd for t in shorts)),
        "short_net": float(sum(t.pnl_net_usd for t in shorts)),
        "wall": time.perf_counter() - t0,
    }


def _beta_alpha(rows: list[dict]) -> dict:
    """Regress per-side gross P&L on the market return."""
    mkts = np.array([r["mkt_ret_pct"] for r in rows], dtype=np.float64)
    out: dict = {}
    for side in ("long", "short", "both"):
        key = "gross" if side == "both" else f"{side}_gross"
        y = np.array([r[key] for r in rows], dtype=np.float64)
        if side == "both":
            x = mkts
        else:
            x = np.array([r[f"{side}_legs"] for r in rows], dtype=np.float64)
            # Per-side: relate the side's gross to the market move
            # weighted by how much of the side was actually traded.
            with np.errstate(divide="ignore", invalid="ignore"):
                w = np.where(x > 0, y / np.where(x == 0, 1, x), 0.0)
            if np.allclose(mkts, mkts[0]):
                out[side] = {"beta": 0.0, "alpha": float(y.mean()),
                             "corr": 0.0}
                continue
            slope, intercept = np.polyfit(mkts, w, 1)
            corr = float(np.corrcoef(mkts, w)[0, 1]) if w.std() > 0 else 0.0
            out[side] = {"beta": float(slope), "alpha": float(intercept),
                         "corr": corr}
            continue
        if np.allclose(mkts, mkts[0]) or y.std() == 0:
            out[side] = {"beta": 0.0, "alpha": float(y.mean()), "corr": 0.0}
            continue
        slope, intercept = np.polyfit(mkts, y, 1)
        corr = float(np.corrcoef(mkts, y)[0, 1])
        out[side] = {"beta": float(slope), "alpha": float(intercept),
                     "corr": corr}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--arms", default="both,long_only")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]

    results: dict[str, list[dict]] = {a: [] for a in arms}
    for arm in arms:
        print(f"== arm: {arm} ==")
        for label, s, e, regime in SLICES:
            r = _run(label, s, e, arm)
            results[arm].append(r)
            print(f"  {label:<12} mkt={r['mkt_ret_pct']:>+7.2f}%  "
                  f"legs={r['legs']:>4}  gross=${r['gross']:>8,.0f}  "
                  f"net=${r['net']:>8,.0f}  | L g=${r['long_gross']:>7,.0f} "
                  f"S g=${r['short_gross']:>7,.0f}", flush=True)
        print()

    # ---- summary ----
    summary = {}
    for arm, rows in results.items():
        flat = [r for r in rows if abs(r["mkt_ret_pct"]) < 3.0]
        ba = _beta_alpha(rows)
        summary[arm] = {
            "legs": sum(r["legs"] for r in rows),
            "gross": sum(r["gross"] for r in rows),
            "fees": sum(r["fees"] for r in rows),
            "net": sum(r["net"] for r in rows),
            "slices_positive_gross": sum(1 for r in rows if r["gross"] > 0),
            "slices_positive_net": sum(1 for r in rows if r["net"] > 0),
            "flat_gross": sum(r["gross"] for r in flat),
            "flat_net": sum(r["net"] for r in flat),
            "flat_positive": sum(1 for r in flat if r["gross"] > 0),
            "flat_n": len(flat),
            "beta_analysis": ba,
        }

    print("=" * 78)
    print(f"{'arm':<12} {'legs':>6} {'gross':>10} {'fees':>9} {'net':>10} "
          f"{'flat gross':>11} {'flat +':>7} {'corr(L)':>8} {'corr(S)':>8}")
    for arm in arms:
        s = summary[arm]
        print(f"{arm:<12} {s['legs']:>6} {s['gross']:>10,.0f} "
              f"{s['fees']:>9,.0f} {s['net']:>10,.0f} "
              f"{s['flat_gross']:>11,.0f} "
              f"{s['flat_positive']:>3}/{s['flat_n']:<3} "
              f"{s['beta_analysis']['long']['corr']:>+8.3f} "
              f"{s['beta_analysis']['short']['corr']:>+8.3f}")
    print()

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"slices": SLICES, "results": results,
                   "summary": summary}, fh, indent=2, default=str)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
