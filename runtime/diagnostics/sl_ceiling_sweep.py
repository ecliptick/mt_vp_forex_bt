"""Sweep the SL ceiling (sl_max_usd) on the 1m zone set (2026-10-24).

The user: "SLs are too big. im aiming for around $2.50$. does this mean
hvn sl is too wide?"

Answer: YES, and the cause is the SHELF SPACING, not the anchor. The
daily VP profile carries ~4 HVN shelves/day with a p50 gap of
$2.54-$4.00 and a p90 of $22-24, so "the nearest shelf below entry"
is routinely $10-$30 away. The ``sl_min_usd`` floor can only
LOOSEN a stop, so there was no way to pull it in.

``sl_max_usd`` (new knob, 2026-10-24) is the ceiling: it clamps the
stop distance to at most N dollars, only ever TIGHTENING.

Every cell is fit on three slices and scored on three held-out slices.
Gross is reported alongside net, because a tighter stop means more
stop-outs AND more round trips -- "net improved" can be noise.

Usage
-----
    python -m runtime.diagnostics.sl_ceiling_sweep
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
OUT = Path("runtime/diagnostics/_reports/sl_ceiling_sweep.json")

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

CEILINGS = [0.0, 1.5, 2.0, 2.5, 3.0, 4.0, 6.0, 10.0]


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
    return {
        "legs": len(tr),
        "net": sum(t.pnl_net_usd for t in tr),
        "gross": sum(t.pnl_gross_usd for t in tr),
        "tp_p50": float(np.median(tp)) if len(tp) else float("nan"),
        "sl_p50": float(np.median(sl)) if len(sl) else float("nan"),
        "sl_p90": float(np.percentile(sl, 90)) if len(sl) else float("nan"),
        "hit": (sum(1 for t in tr if t.pnl_net_usd > 0) / max(len(tr), 1)),
        "wall": time.perf_counter() - t0,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows = []
    print()
    print(f"{'ceil':>6} | {'--- FIT ---':^40} | {'--- HELD OUT ---':^30}")
    print(f"{'':>6} | {'legs':>6} {'SL p50':>7} {'SL p90':>7} {'gross':>8} "
          f"{'net':>8} {'hit':>6} | {'legs':>6} {'gross':>8} {'net':>8}")
    for cap in CEILINGS:
        # BUG FIX 2026-10-24: ``ov = {} if cap == 0.0 else {...}`` did NOT
        # disable the ceiling -- it FELL BACK to the canonical
        # ``sl_max_usd=2.50`` from ``build_engine_kwargs``. The "off"
        # row was therefore byte-identical to the ``2.5`` row (same
        # legs, gross, net and SL p50), i.e. the sweep could not
        # measure "ceiling disabled" at all.
        #
        # ``sl_max_usd <= 0.0`` is the engine's documented "disabled"
        # value (see ``_apply_sl_max_ceiling``), so pass it EXPLICITLY
        # rather than omitting the key.
        ov = {"sl_max_usd": cap}
        agg = {"fit": [], "held": []}
        for group, slices in (("fit", FIT), ("held", HELD)):
            for tag, s, e in slices:
                # 2026-10-24 MEMORY FIX: the slices are loaded and
                # RELEASED one at a time. Holding all six artifact
                # bundles at once is several GB resident (the zone
                # lists dominate) and the process was OOM-killed when
                # it tried -- the same failure documented in
                # ``sl_ceiling_sweep8.py``.
                bars, art, spot = _prep(tag, s, e)
                agg[group].append(_run(bars, art, spot, ov))
                del bars, art
                import gc
                gc.collect()
        f, h = agg["fit"], agg["held"]
        row = {
            "sl_max_usd": cap,
            "fit": {
                "legs": sum(x["legs"] for x in f),
                "gross": sum(x["gross"] for x in f),
                "net": sum(x["net"] for x in f),
                "sl_p50": float(np.median([x["sl_p50"] for x in f])),
                "sl_p90": float(np.median([x["sl_p90"] for x in f])),
                "hit": float(np.mean([x["hit"] for x in f])),
            },
            "held": {
                "legs": sum(x["legs"] for x in h),
                "gross": sum(x["gross"] for x in h),
                "net": sum(x["net"] for x in h),
                "sl_p50": float(np.median([x["sl_p50"] for x in h])),
            },
        }
        rows.append(row)
        print(f"{cap:>6.1f} | {row['fit']['legs']:>6} "
              f"{row['fit']['sl_p50']:>7.2f} {row['fit']['sl_p90']:>7.2f} "
              f"{row['fit']['gross']:>8,.0f} {row['fit']['net']:>8,.0f} "
              f"{row['fit']['hit']:>6.1%} | {row['held']['legs']:>6} "
              f"{row['held']['gross']:>8,.0f} {row['held']['net']:>8,.0f}",
              flush=True)

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
