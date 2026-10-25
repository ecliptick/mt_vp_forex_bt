"""Scale the capture down to ~$20 (2026-10-23).

The user's brief: "lets use previous 1 day range, not 3. a 20$
capture is considered difficult on current alpha, so scale ratio down
to this. and perhaps hvns should be closer instead of so far?"

Two levers, both currently mis-set for the 1m zone set:

* ``swing_cap_pct`` (0.7) -- the TP ceiling is
  ``cap_pct x 1-day range``. On 2026-01 the 1-day range is $84.79, so
  the cap is $59.35 and the median TP lands at $49.89. To make the
  median capture ~$20 the ratio has to come down to roughly 0.25.
* ``nth_hvn_n`` (3) -- the TP reaches for the 3rd HVN shelf ahead. On
  the 1m zone set the shelves are ~2.7/day, so n=3 walks a long way.
  n=1 is the nearest magnet.

This sweeps both together and reports the CAPTURE (median TP), the
realised gross/net, and the gross-per-trade, so a change can be
judged on whether it is an edge or just a smaller bet.

Every cell is fit on three slices and scored on three held-out slices.

Usage
-----
    python -m runtime.diagnostics.capture_scaling
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.swing_cap import build_daily_swing  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/capture_scaling.json")

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

CAP_PCTS = [0.15, 0.20, 0.25, 0.35, 0.50, 0.70]
NTH_HVN = [1, 2, 3]
LOOKBACK = 86_400  # 1 day, per the user's instruction


def _prep(tag, s, e):
    bars = load_bars(DATA, s, e)
    art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    return bars, art, spot


def _cap_for(pct, bars):
    return build_daily_swing(bars, lookback_secs=LOOKBACK, cap_pct=pct)


def _run(bars, art, spot, cap, overrides):
    ek = build_engine_kwargs(
        spot=spot, swing_cap=cap, p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, **overrides,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    tp = np.array([abs(t.tp_distance_usd) for t in tr])
    sl = np.array([abs(t.sl_distance_usd) for t in tr])
    return {
        "legs": len(tr),
        "net": sum(t.pnl_net_usd for t in tr),
        "gross": sum(t.pnl_gross_usd for t in tr),
        "tp_p50": float(np.median(tp)) if len(tp) else float("nan"),
        "sl_p50": float(np.median(sl)) if len(sl) else float("nan"),
        "wall": time.perf_counter() - t0,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    print()
    print(f"{'cap_pct':>8} {'n_hvn':>6} | "
          f"{'FIT legs':>9} {'TP p50':>7} {'SL p50':>7} "
          f"{'gross':>8} {'net':>8} | "
          f"{'HELD legs':>9} {'TP p50':>7} {'gross':>8} {'net':>8}")
    for pct in CAP_PCTS:
        for n in NTH_HVN:
            ov = {"nth_hvn_n": n}
            agg = {"fit": [], "held": []}
            for group, slices in (("fit", FIT), ("held", HELD)):
                for tag, s, e in slices:
                    # 2026-10-24 MEMORY FIX: the slices are loaded and
                    # RELEASED one at a time. Holding all six artifact
                    # bundles at once is several GB resident (the zone
                    # lists dominate) and the process was OOM-killed
                    # when it tried -- the same failure documented in
                    # ``sl_ceiling_sweep8.py``.
                    bars, art, spot = _prep(tag, s, e)
                    cap = _cap_for(pct, bars)
                    agg[group].append(_run(bars, art, spot, cap, ov))
                    del bars, art, cap
                    import gc
                    gc.collect()
            f, h = agg["fit"], agg["held"]
            row = {
                "cap_pct": pct, "nth_hvn_n": n,
                "fit": {
                    "legs": sum(x["legs"] for x in f),
                    "gross": sum(x["gross"] for x in f),
                    "net": sum(x["net"] for x in f),
                    "tp_p50": float(np.median([x["tp_p50"] for x in f])),
                    "sl_p50": float(np.median([x["sl_p50"] for x in f])),
                },
                "held": {
                    "legs": sum(x["legs"] for x in h),
                    "gross": sum(x["gross"] for x in h),
                    "net": sum(x["net"] for x in h),
                    "tp_p50": float(np.median([x["tp_p50"] for x in h])),
                    "sl_p50": float(np.median([x["sl_p50"] for x in h])),
                },
            }
            rows.append(row)
            print(f"{pct:>8.2f} {n:>6} | {row['fit']['legs']:>9} "
                  f"{row['fit']['tp_p50']:>7.2f} {row['fit']['sl_p50']:>7.2f} "
                  f"{row['fit']['gross']:>8,.0f} {row['fit']['net']:>8,.0f} | "
                  f"{row['held']['legs']:>9} {row['held']['tp_p50']:>7.2f} "
                  f"{row['held']['gross']:>8,.0f} {row['held']['net']:>8,.0f}",
                  flush=True)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
