"""FVG detection CADENCE sweep — 1m vs 3m vs 5m (2026-10-24).

The user: "or, simpler wins, simply do canonical sweep on 3 min
and 5 min fvgs instead".

The detector cadence is a single knob: ``resample_to_n_secs``. The
canonical value is 60 (1m). This sweeps 60 / 180 / 300 and reports
the full recipe on each, per slice, split by regime.

Why this knob is the interesting one
------------------------------------
Everything else in the recipe already agrees on a cadence:

* market structure (BoS/CHoCH pivots) -- 60s
* the volume profile -- built from 1m bars
* the swing cap -- a 24h range

Only the FVG detector was on 1s until 2026-10-23. Moving it to 1m
changed the median zone width from $0.47 to $1.04 and changed the
zone COUNT by an order of magnitude. The open question is whether
there is a cadence where the FVG geometry actually MATCHES the
other indicators -- a 3m or 5m FVG is a different object from a 1m
FVG, and if the recipe has any structural edge it would live where
the zone size is comparable to the HVN shelf spacing (~$2.5-$4.00)
rather than where it is 3x smaller.

What we expect
--------------
If the recipe has no edge, every cadence produces the same shape:
gross near zero, net negative by the fee bill, and the direction bet
uncorrelated with anything but market direction. If a cadence
produces gross-positive on the FLAT slices, that is the first real
candidate the beta-proxy diagnosis would accept.

Mechanics
---------
``structure_overrides={"resample_to_n_secs": N}`` on the ArtifactCache
changes BOTH the FVG detection cadence and the market-structure
pivot cadence, because they are the same underlying "what timeframe
is this system reasoning about" decision. Keeping them together is
deliberate: a 5m FVG with 1m structure pivots would be a franken-
config that matches nothing.

Usage
-----
    python -m runtime.diagnostics.cadence_sweep
"""
from __future__ import annotations

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
OUT = Path("runtime/diagnostics/_reports/cadence_sweep.json")

# (tag, start, end, regime, group)
SLICES: list[tuple[str, str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull", "fit"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear", "fit"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat", "fit"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull", "held"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear", "held"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull", "held"),
]

CADENCES = [60, 180, 300]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    for cad in CADENCES:
        per_slice = []
        for tag, s, e, regime, group in SLICES:
            bars = load_bars(DATA, s, e)
            art = ArtifactCache(
                DATA, f"cad{cad}_{s[:7]}", s, e,
                detector_overrides={"resample_to_n_secs": cad},
                structure_overrides={"resample_to_n_secs": cad},
            ).build_or_load(bars)
            spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
            ek = build_engine_kwargs(
                spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
                structure=art["structure"], vol_state=art["vol_state"],
                structure_gate=False,
            )
            t0 = time.perf_counter()
            res, _ = run_vp_sniper_backtest_forex(
                bars=bars, zones=art["zones"], vp_set=art["vp_set"],
                lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
            )
            tr = list(res.trades)
            zw = np.array([abs(t.zone_width) for t in tr])
            sl = np.array([abs(t.initial_sl_distance_usd) for t in tr])
            tp = np.array([abs(t.initial_tp_distance_usd) for t in tr])
            gross = float(sum(t.pnl_gross_usd for t in tr))
            net = float(sum(t.pnl_net_usd for t in tr))
            m = {
                "tag": tag, "regime": regime, "group": group,
                "zones": len(art["zones"]),
                "legs": len(tr),
                "gross": gross, "net": net, "fees": gross - net,
                "zone_w_p50": float(np.median(zw)) if len(zw) else float("nan"),
                "sl_p50": float(np.median(sl)) if len(sl) else float("nan"),
                "tp_p50": float(np.median(tp)) if len(tp) else float("nan"),
                "hit": (sum(1 for t in tr if t.pnl_net_usd > 0) / max(len(tr), 1)),
                "wall": time.perf_counter() - t0,
            }
            per_slice.append(m)
            print(f"  {tag:<14} zones={m['zones']:>6} legs={m['legs']:>6} "
                  f"gross={gross:>8,.0f} net={net:>8,.0f} "
                  f"zoneW={m['zone_w_p50']:>6.2f} SL={m['sl_p50']:>5.2f} "
                  f"TP={m['tp_p50']:>6.2f}", flush=True)
            del bars, art, res, tr
            import gc
            gc.collect()

        def agg(group):
            sel = [x for x in per_slice if x["group"] == group]
            return {
                "legs": sum(x["legs"] for x in sel),
                "gross": sum(x["gross"] for x in sel),
                "net": sum(x["net"] for x in sel),
                "fees": sum(x["fees"] for x in sel),
                "hit": float(np.mean([x["hit"] for x in sel])),
            }

        def agg_regime(reg):
            sel = [x for x in per_slice if x["regime"] == reg]
            return {
                "legs": sum(x["legs"] for x in sel),
                "gross": sum(x["gross"] for x in sel),
                "net": sum(x["net"] for x in sel),
            }

        row = {
            "resample_to_n_secs": cad,
            "fit": agg("fit"), "held": agg("held"),
            "total": {
                "legs": sum(x["legs"] for x in per_slice),
                "gross": sum(x["gross"] for x in per_slice),
                "net": sum(x["net"] for x in per_slice),
                "fees": sum(x["fees"] for x in per_slice),
            },
            "by_regime": {r: agg_regime(r) for r in ("bull", "bear", "flat")},
            "per_slice": per_slice,
        }
        rows.append(row)
        t = row["total"]
        print(f"\n=== cadence {cad}s ({cad//60}m) ===")
        print(f"  TOTAL legs={t['legs']:>6} gross={t['gross']:>9,.0f} "
              f"net={t['net']:>9,.0f} fees={t['fees']:>9,.0f}")
        for r, a in row["by_regime"].items():
            print(f"    {r:<6} legs={a['legs']:>6} gross={a['gross']:>9,.0f} "
                  f"net={a['net']:>9,.0f}")
        print(flush=True)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
