"""SL/TP knob sweep against the 1m zone set (2026-10-23).

The FVG detector moved from 1s to 1m on 2026-10-23, changing the
median zone width from $0.47 to $1.04 (2.2x). Every knob that
MULTIPLIES the zone width is now operating on a different absolute
distance than it was validated against:

    SL (fallback) = sl_zone_mult x zone_width
    TP2 (zone_mult) = tp_zone_mult_fallback x zone_width

The absolute-dollar caps (swing, p95) did NOT change, so the ratio
zone-derived : cap-derived has shifted. Measured before this sweep:

    SL distance  p50 $15.60   (was $2.34 on 1s zones)
    TP distance  p50 $85.67   (was $7.32 on 1s zones)
    R:R          p50 4.55

A p50 TP of $85.67 against a $0.47 round trip is not a target, it is a
hope. This sweep re-fits the geometry to the 1m zone set.

Method
------
* Every cell is fit on the SAME three slices, then scored on a HELD-OUT
  set. Reporting the best cell on the fit slices alone is how you
  manufacture an edge.
* GROSS is reported alongside NET, because a wider stop means fewer
  round trips and hence fewer fees -- "net improved" can be pure fee
  avoidance.
* The winner must clear the GROSS bar on the held-out slices too.

Usage
-----
    python -m runtime.diagnostics.zone_knob_sweep --knob sl_zone_mult
    python -m runtime.diagnostics.zone_knob_sweep --all
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

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
OUT = Path("runtime/diagnostics/_reports/zone_knob_sweep.json")

FIT = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
]
HELD = [
    ("2025-01 UP",   "2025-01-01", "2025-02-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01"),
]

GRIDS: dict[str, list[Any]] = {
    # The SL fallback multiplier. 40 x $1.04 = $41.60 on 1m zones.
    "sl_zone_mult": [4.0, 6.0, 10.0, 16.0, 24.0, 40.0],
    # The floor on |SL - entry|. Was 2.50 against a $0.47 zone.
    "sl_min_usd": [1.0, 2.5, 5.0, 8.0, 12.0],
    # The TP's HVN shelf depth. 3 was fit on $0.47 zones.
    "nth_hvn_n": [1, 2, 3, 4, 5],
    # TP2 anchoring. 200 x $1.04 = $208 on 1m zones.
    "tp_zone_mult_fallback": [20.0, 40.0, 60.0, 100.0, 200.0],
    # The SL's own shelf depth.
    "sl_nth_hvn_n": [1, 2, 3],
}


def _load(s: str, e: str):
    bars = load_bars(DATA, s, e)
    art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    return bars, art, spot


def _run(art, bars, spot, overrides: dict[str, Any]):
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, **overrides,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    net = sum(t.pnl_net_usd for t in tr)
    gross = sum(t.pnl_gross_usd for t in tr)
    sl = np.array([abs(t.sl_distance_usd) for t in tr])
    tp = np.array([abs(t.tp_distance_usd) for t in tr])
    rr = np.array([abs(t.tp_distance_usd) / abs(t.sl_distance_usd)
                   for t in tr if abs(t.sl_distance_usd) > 1e-9])
    return {
        "legs": len(tr),
        "net": net, "gross": gross,
        "ev": net / max(len(tr), 1),
        "gross_ev": gross / max(len(tr), 1),
        "hit": sum(1 for t in tr if t.pnl_net_usd > 0) / max(len(tr), 1),
        "sl_p50": float(np.median(sl)) if len(sl) else float("nan"),
        "tp_p50": float(np.median(tp)) if len(tp) else float("nan"),
        "rr_p50": float(np.median(rr)) if len(rr) else float("nan"),
        "wall": time.perf_counter() - t0,
    }


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--knob", default=None, choices=sorted(GRIDS))
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    knobs = sorted(GRIDS) if (args.all or not args.knob) else [args.knob]
    OUT.parent.mkdir(parents=True, exist_ok=True)

    # Load each slice once; the artifacts are expensive, the cells are not.
    fit_data = [(tag, *_load(s, e)) for tag, s, e in FIT]
    held_data = [(tag, *_load(s, e)) for tag, s, e in HELD]

    results: dict[str, list] = {}
    for knob in knobs:
        print()
        print(f"### {knob} = {GRIDS[knob]}")
        rows = []
        print(f"  {'value':>8} | {'--- FIT ---':^34} | {'--- HELD OUT ---':^34}")
        print(f"  {'':>8} | {'legs':>6} {'gross':>8} {'net':>8} {'R:R':>6} "
              f"| {'legs':>6} {'gross':>8} {'net':>8} {'R:R':>6}")
        for val in GRIDS[knob]:
            ov = {knob: val}
            agg = {"fit": [], "held": []}
            for group, data in (("fit", fit_data), ("held", held_data)):
                for tag, bars, art, spot in data:
                    r = _run(art, bars, spot, ov)
                    agg[group].append(r)
            f = agg["fit"]
            h = agg["held"]
            row = {
                "knob": knob, "value": val,
                "fit": {"legs": sum(x["legs"] for x in f),
                        "gross": sum(x["gross"] for x in f),
                        "net": sum(x["net"] for x in f),
                        "rr_p50": float(np.median(
                            [x["rr_p50"] for x in f]))},
                "held": {"legs": sum(x["legs"] for x in h),
                         "gross": sum(x["gross"] for x in h),
                         "net": sum(x["net"] for x in h),
                         "rr_p50": float(np.median(
                             [x["rr_p50"] for x in h]))},
            }
            rows.append(row)
            print(f"  {val:>8} | {row['fit']['legs']:>6} "
                  f"{row['fit']['gross']:>8,.0f} {row['fit']['net']:>8,.0f} "
                  f"{row['fit']['rr_p50']:>6.2f} | "
                  f"{row['held']['legs']:>6} {row['held']['gross']:>8,.0f} "
                  f"{row['held']['net']:>8,.0f} {row['held']['rr_p50']:>6.2f}",
                  flush=True)
        results[knob] = rows

        # Verdict: the best-on-fit cell, and whether it holds out.
        best = max(rows, key=lambda r: r["fit"]["net"])
        print(f"  -> best on FIT by net: {knob}={best['value']}  "
              f"fit net ${best['fit']['net']:,.0f}  "
              f"held-out net ${best['held']['net']:,.0f}  "
              f"held-out gross ${best['held']['gross']:,.0f}")
        best_g = max(rows, key=lambda r: r["fit"]["gross"])
        print(f"  -> best on FIT by GROSS: {knob}={best_g['value']}  "
              f"fit gross ${best_g['fit']['gross']:,.0f}  "
              f"held-out gross ${best_g['held']['gross']:,.0f}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
