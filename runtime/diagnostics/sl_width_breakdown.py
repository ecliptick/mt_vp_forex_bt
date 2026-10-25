"""Where does the SL width come from? (2026-10-24)

The user: "SLs are too big. im aiming for around 2.5$. does this mean
hvn sl is too wide?"

Short answer: YES, and the mechanism is measurable. The canonical stop
is:

    SL = max(shelf_lo - hvn_buffer_usd, entry - sl_min_usd)

for a long, when the 1st HVN shelf behind entry resolves. Two things
can make that wide:

1. The shelf itself sits far below the entry (shelf SPACING too wide).
2. The floor ``sl_min_usd`` is not binding because it only LOOSENS.

This measures, per trade leg:
  * the distance from entry to the 1st shelf behind it
  * the shelf-to-shelf spacing in the daily VP profile
  * how much the $2.50 floor is actually doing

If shelf spacing is the problem, then shrinking ``sl_min_usd`` will
NOT help -- the floor only loosens. The lever would be a shelf-TARGET
stop (aim at a fixed distance) instead of a shelf-ANCHORED stop, or
tightening the VP profile so shelves are denser.

Usage
-----
    python -m runtime.diagnostics.sl_width_breakdown
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
OUT = Path("runtime/diagnostics/_reports/sl_width_breakdown.json")

SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
]


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

        sl = np.array([abs(t.sl_distance_usd) for t in tr])
        zw = np.array([t.zone_width for t in tr])
        # How many legs are anchored on an HVN shelf at all?
        hvn_legs = [t for t in tr if t.sl_struct_ref.startswith("hvn")]
        other = [t for t in tr if not t.sl_struct_ref.startswith("hvn")]

        print()
        print(f"=== {tag} ===")
        print(f"  legs {len(tr)}   median zone width ${np.median(zw):.2f}")
        print(f"  SL distance overall   p10 ${np.percentile(sl,10):>6.2f}  "
              f"p50 ${np.percentile(sl,50):>6.2f}  p90 ${np.percentile(sl,90):>6.2f}")
        if hvn_legs:
            h = np.array([abs(t.sl_distance_usd) for t in hvn_legs])
            print(f"  SL on HVN shelves     n={len(hvn_legs):<5}  "
                  f"p10 ${np.percentile(h,10):>6.2f}  "
                  f"p50 ${np.percentile(h,50):>6.2f}  "
                  f"p90 ${np.percentile(h,90):>6.2f}")
        if other:
            o_ = np.array([abs(t.sl_distance_usd) for t in other])
            print(f"  SL on zone_edge/etc   n={len(other):<5}  "
                  f"p10 ${np.percentile(o_,10):>6.2f}  "
                  f"p50 ${np.percentile(o_,50):>6.2f}  "
                  f"p90 ${np.percentile(o_,90):>6.2f}")

        # ── Shelf geometry in the VP profile ──────────────────────
        # The daily VP profile's HVN shelves: how far apart are they?
        vp = art["vp_set"]
        shelves = getattr(vp, "hvn_zones", None) or getattr(vp, "hvn", None)
        gaps = []
        if shelves is not None:
            per_day = {}
            for z in shelves:
                d = int(z.get("day")) if isinstance(z, dict) else getattr(z, "day", 0)
                per_day.setdefault(d, []).append(z)
            for d, zs in per_day.items():
                if len(zs) < 2:
                    continue
                zs2 = sorted(zs, key=lambda z: (
                    z.get("lo") if isinstance(z, dict) else z.lo))
                for a, b in zip(zs2, zs2[1:]):
                    lo_a = a.get("lo") if isinstance(a, dict) else a.lo
                    lo_b = b.get("lo") if isinstance(b, dict) else b.lo
                    hi_a = a.get("hi") if isinstance(a, dict) else a.hi
                    gaps.append(lo_b - hi_a)
            if gaps:
                gp = np.array(gaps)
                print(f"  HVN shelf gap       p10 ${np.percentile(gp,10):>6.2f}  "
                      f"p50 ${np.percentile(gp,50):>6.2f}  "
                      f"p90 ${np.percentile(gp,90):>6.2f}   "
                      f"(n={len(gp)} gaps)")
        else:
            print(f"  (vp_set has no hvn_zones attr; attrs: "
                  f"{[a for a in dir(vp) if not a.startswith('_')][:8]})")

        # ── What would a $2.50-capped stop have done? ─────────────
        # Simulate: clamp |SL| to 2.50 and re-score the SAME trades.
        # This is a counterfactual on realised exits -- it tells us
        # what the P&L WOULD have been if the stop had been 2.50,
        # assuming the stop had been there at entry (so exits that
        # happened beyond 2.50 would have exited at 2.50).
        capped = []
        for t in tr:
            d = abs(t.sl_distance_usd)
            if d <= 2.50:
                capped.append(t.pnl_net_usd)
                continue
            # The stop was hit (loss) -> with a 2.50 stop the loss is
            # capped at 2.50 per unit. If the trade WON, the stop
            # distance did not matter -> unchanged.
            if t.pnl_gross_usd < 0:
                capped.append(-2.50 * t.qty_btc)
            else:
                capped.append(t.pnl_net_usd)
        cur_net = sum(t.pnl_net_usd for t in tr)
        cap_net = sum(capped)
        cur_gross = sum(t.pnl_gross_usd for t in tr)
        cap_gross = cur_gross + (cap_net - cur_net)
        print(f"  counterfactual $2.50 SL cap:  net ${cur_net:,.0f} -> "
              f"${cap_net:,.0f}   gross ${cur_gross:,.0f} -> ${cap_gross:,.0f}")

        rows.append({
            "slice": tag, "legs": len(tr),
            "sl_p50": float(np.median(sl)),
            "hvn_n": len(hvn_legs),
            "hvn_sl_p50": (float(np.median(
                [abs(t.sl_distance_usd) for t in hvn_legs]))
                if hvn_legs else None),
            "shelf_gap_p": (list(np.percentile(gaps, [10, 50, 90]))
                            if gaps else None),
            "net_now": cur_net, "net_cap250": cap_net,
            "gross_now": cur_gross, "gross_cap250": cap_gross,
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
