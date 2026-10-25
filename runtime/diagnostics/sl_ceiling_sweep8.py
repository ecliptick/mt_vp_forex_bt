"""SL ceiling sweep, EXTENDED to $8 (2026-10-24).

The user: "do a sweep on SL up to 8$, does higher SL give
profitability?"

This extends ``runtime/diagnostics/sl_ceiling_sweep.py`` (which
stopped at $10 with a coarser grid) with:

* per-slice rows, not just fit/held aggregates -- so a config that
  wins by ONE slice is visible rather than averaged away;
* a regime split (BULL / BEAR / FLAT) -- the beta-proxy diagnosis
  says the flat slices are the decisive test;
* the legs-vs-net decomposition, because "higher SL gives
  profitability" has a very specific mechanical mechanism (wider stop
  -> fewer stop-outs -> fewer round trips -> less fee drag) that must
  be separated from any actual signal improvement.

Every cell is scored on SIX slices: 3 fit (tuned on) + 3 held out.
Gross is reported next to net because on this recipe the two move in
opposite directions.

Usage
-----
    python -m runtime.diagnostics.sl_ceiling_sweep8
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
OUT = Path("runtime/diagnostics/_reports/sl_ceiling_sweep8.json")

# (tag, start, end, regime, group)
SLICES: list[tuple[str, str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull", "fit"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear", "fit"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat", "fit"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull", "held"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear", "held"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull", "held"),
]

CEILINGS = [2.5, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0]


def _prep(tag, s, e):
    bars = load_bars(DATA, s, e)
    art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    return bars, art, spot


def _run(bars, art, spot, ov):
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, **ov,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    sl = np.array([abs(t.sl_distance_usd) for t in tr])
    tp = np.array([abs(t.tp_distance_usd) for t in tr])
    gross = float(sum(t.pnl_gross_usd for t in tr))
    net = float(sum(t.pnl_net_usd for t in tr))
    return {
        "legs": len(tr),
        "gross": gross,
        "net": net,
        "fees": gross - net,
        "ev": (net / len(tr)) if tr else 0.0,
        "tp_p50": float(np.median(tp)) if len(tp) else float("nan"),
        "sl_p50": float(np.median(sl)) if len(sl) else float("nan"),
        "hit": (sum(1 for t in tr if t.pnl_net_usd > 0) / max(len(tr), 1)),
        "wall": time.perf_counter() - t0,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    # Each CEILING is a full pass over all six slices. The slices are
    # loaded and RELEASED one at a time: holding six artifact bundles
    # (zones lists dominate -- ~650 MB per 1.8M-zone set) at once is
    # several GB of resident memory, and the process was OOM-killed
    # when it tried.
    for cap in CEILINGS:
        ov = {"sl_max_usd": cap}
        per_slice = []
        for tag, s, e, regime, group in SLICES:
            bars, art, spot = _prep(tag, s, e)
            m = _run(bars, art, spot, ov)
            m.update(tag=tag, regime=regime, group=group)
            per_slice.append(m)
            # Release before the next slice load.
            del bars, art
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
                "sl_p50": float(np.median([x["sl_p50"] for x in sel])),
                "tp_p50": float(np.median([x["tp_p50"] for x in sel])),
            }

        def agg_regime(reg):
            sel = [x for x in per_slice if x["regime"] == reg]
            return {
                "legs": sum(x["legs"] for x in sel),
                "gross": sum(x["gross"] for x in sel),
                "net": sum(x["net"] for x in sel),
            }

        row = {
            "sl_max_usd": cap,
            "fit": agg("fit"),
            "held": agg("held"),
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
        print(f"\n=== sl_max_usd = ${cap:.2f} ===", flush=True)
        print(f"  {'slice':<14} {'reg':<5} {'grp':<5} {'legs':>6} "
              f"{'gross':>9} {'net':>9} {'hit':>6}")
        for x in per_slice:
            print(f"  {x['tag']:<14} {x['regime']:<5} {x['group']:<5} "
                  f"{x['legs']:>6} {x['gross']:>9,.0f} {x['net']:>9,.0f} "
                  f"{x['hit']:>6.1%}")
        print(f"  {'TOTAL':<14} {'':<5} {'':<5} {t['legs']:>6} "
              f"{t['gross']:>9,.0f} {t['net']:>9,.0f}", flush=True)
        for r, a in row["by_regime"].items():
            print(f"    {r:<6} legs={a['legs']:>6} gross={a['gross']:>9,.0f} "
                  f"net={a['net']:>9,.0f}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
