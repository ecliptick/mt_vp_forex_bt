"""Measure the SL/TP geometry on the 1m zone set (2026-10-23).

The FVG detector moved from 1s to 1m on 2026-10-23, which changed the
median zone width from $0.47 to $1.11 (2.4x). Every SL/TP knob that
multiplies the zone width is therefore operating on a different
absolute distance than it was validated against:

    SL = sl_zone_mult x zone_width
    TP = tp_zone_mult_fallback x zone_width

This dumps the CURRENT geometry so the re-sweep has a baseline, and
reports how much of the SL/TP is zone-derived vs cap-derived (the
swing cap and p95 cap are absolute-dollar, so they do NOT scale with
the zone -- which is exactly why a wider zone now hits them sooner).

Usage
-----
    python -m runtime.diagnostics.zone_geometry
"""
from __future__ import annotations

import json
import sys
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
OUT = Path("runtime/diagnostics/_reports/zone_geometry.json")

SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
]


def _pct(a, q):
    return float(np.percentile(a, q)) if len(a) else float("nan")


def main():
    rows = []
    for tag, s, e in SLICES:
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=False,
        )
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
        )
        tr = list(res.trades)
        zw = np.array([t.zone_width for t in tr])
        sl = np.array([abs(t.sl_distance_usd) for t in tr])
        tp = np.array([abs(t.tp_distance_usd) for t in tr])
        # R:R per leg
        rr = np.array([
            abs(t.tp_distance_usd) / abs(t.sl_distance_usd)
            for t in tr if abs(t.sl_distance_usd) > 1e-9
        ])
        # Where did the SL come from?
        refs: dict[str, int] = {}
        for t in tr:
            refs[t.sl_struct_ref] = refs.get(t.sl_struct_ref, 0) + 1
        # How often was the SL floored at sl_min_usd?
        floored = int(np.sum(sl <= 2.51))
        n = len(tr)
        print()
        print(f"=== {tag}  (1m zones) ===")
        print(f"  legs={n}  zones={len(art['zones'])}")
        print(f"  zone_width   p10 ${_pct(zw,10):>6.2f}  p50 ${_pct(zw,50):>6.2f} "
              f"  p90 ${_pct(zw,90):>6.2f}")
        print(f"  SL distance  p10 ${_pct(sl,10):>6.2f}  p50 ${_pct(sl,50):>6.2f} "
              f"  p90 ${_pct(sl,90):>6.2f}   (floor $2.50 hit: {floored}/{n})")
        print(f"  TP distance  p10 ${_pct(tp,10):>6.2f}  p50 ${_pct(tp,50):>6.2f} "
              f"  p90 ${_pct(tp,90):>6.2f}")
        print(f"  R:R          p10 {_pct(rr,10):>6.2f}  p50 {_pct(rr,50):>6.2f} "
              f"  p90 {_pct(rr,90):>6.2f}")
        print("  SL anchors:  " + ", ".join(
            f"{k}={v} ({v/n:.0%})" for k, v in
            sorted(refs.items(), key=lambda kv: -kv[1])))
        net = sum(t.pnl_net_usd for t in tr)
        gross = sum(t.pnl_gross_usd for t in tr)
        print(f"  gross ${gross:,.0f}   net ${net:,.0f}   "
              f"EV ${net/max(n,1):+.3f}")
        rows.append({
            "slice": tag, "legs": n,
            "zone_width": [_pct(zw, 10), _pct(zw, 50), _pct(zw, 90)],
            "sl": [_pct(sl, 10), _pct(sl, 50), _pct(sl, 90)],
            "tp": [_pct(tp, 10), _pct(tp, 50), _pct(tp, 90)],
            "rr": [_pct(rr, 10), _pct(rr, 50), _pct(rr, 90)],
            "sl_refs": refs, "sl_floored": floored,
            "gross": gross, "net": net,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
