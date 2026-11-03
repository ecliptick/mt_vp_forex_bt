"""Multi-slice measurement of the dual-mode inverted_bar look-ahead.

Runs the SHIPPED engine over N monthly slices in two arms:

  A. ``entry_mode="dual"``     -- the canonical recipe (AS SHIPPED)
  B. ``entry_mode="immediate"`` -- fires on EVERY trigger zone, i.e.
     WITHOUT the ``int(zone.inverted_bar) >= 0`` skip

Arm B is the LOOK-AHEAD-FREE book, because the immediate path then has
no dependence on the zone's future.  (The sniper path in arm B is
still present but is deduped against the immediate path, so arm B's
book is the immediate book.)

NOTE: "immediate" is not a perfect causal control -- it also changes
which path owns the zone -- but it is the tightest causal comparison
available without patching the engine, and the direction of the
difference is the question that matters.

Run:
    python -m runtime.diagnostics.probe_dual_mode_multislice
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
SLICES = [
    ("2025-01-01", "2025-02-01"),
    ("2025-04-01", "2025-05-01"),
    ("2025-07-01", "2025-08-01"),
    ("2025-10-01", "2025-11-01"),
    ("2026-01-01", "2026-02-01"),
    ("2026-04-01", "2026-05-01"),
]
OUT = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
       / "dual_mode_multislice.json")


def _run(art, bars, entry_mode: str) -> dict:
    from bt.optimal_config import (
        FOREX_CONTRACT_SIZE,
        FOREX_LOTS,
        build_engine_kwargs,
    )
    from bt.vp_engine import run_vp_sniper_backtest_forex

    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        entry_mode=entry_mode,
    )
    res, _diag = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
        record_equity_curve=False,
    )
    trades = list(res.trades)
    gross = float(sum(t.pnl_gross_usd for t in trades))
    fees = float(sum(t.fee_usd for t in trades))
    longs = sum(1 for t in trades if t.direction > 0)
    return {
        "entry_mode": entry_mode,
        "n_legs": len(trades),
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "net": round(gross - fees, 4),
        "gross_per_leg": round(gross / max(1, len(trades)), 6),
        "n_long": longs,
        "n_short": len(trades) - longs,
        "skipped_dual_inverted": int(getattr(res, "n_skipped_dual_inverted", 0)),
    }


def main() -> None:
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    rows = []
    for start, end in SLICES:
        scope = f"XAUUSD_S1_1y__{start[:7]}"
        print(f"[probe] slice {start} -> {end} ...", flush=True)
        bars = load_bars(DATA, start, end)
        if bars.empty:
            print("[probe]   empty, skipped", flush=True)
            continue
        art = ArtifactCache(DATA, scope, start, end).build_or_load(bars)

        d = _run(art, bars, "dual")
        im = _run(art, bars, "immediate")
        row = {
            "slice": f"{start}..{end}",
            "n_zones": len(art["zones"]),
            "dual": d,
            "immediate": im,
            "delta_legs": im["n_legs"] - d["n_legs"],
            "delta_gross": round(im["gross"] - d["gross"], 4),
            "delta_net": round(im["net"] - d["net"], 4),
            "dual_net_positive": d["net"] > 0,
            "immediate_net_positive": im["net"] > 0,
        }
        rows.append(row)
        print(f"[probe]   dual: legs={d['n_legs']} net={d['net']} | "
              f"immediate: legs={im['n_legs']} net={im['net']} | "
              f"d_net={row['delta_net']}", flush=True)

    d_nets = [r["dual"]["net"] for r in rows]
    i_nets = [r["immediate"]["net"] for r in rows]
    out = {
        "slices": rows,
        "n_slices": len(rows),
        "dual_net_sum": round(sum(d_nets), 4),
        "immediate_net_sum": round(sum(i_nets), 4),
        "dual_net_positive_slices": sum(1 for r in rows if r["dual"]["net"] > 0),
        "immediate_net_positive_slices": sum(1 for r in rows if r["immediate"]["net"] > 0),
        "dual_gross_per_leg_mean": round(
            float(np.mean([r["dual"]["gross_per_leg"] for r in rows])), 6),
        "immediate_gross_per_leg_mean": round(
            float(np.mean([r["immediate"]["gross_per_leg"] for r in rows])), 6),
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    print("\n=== dual-mode look-ahead: multi-slice ===")
    print(f"  dual net sum      : {out['dual_net_sum']}")
    print(f"  immediate net sum : {out['immediate_net_sum']}")
    print(f"  dual net-positive slices     : {out['dual_net_positive_slices']}/{len(rows)}")
    print(f"  immediate net-positive slices: {out['immediate_net_positive_slices']}/{len(rows)}")
    print(f"  dual gross/leg mean     : {out['dual_gross_per_leg_mean']}")
    print(f"  immediate gross/leg mean: {out['immediate_gross_per_leg_mean']}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[probe] wrote {OUT}")


if __name__ == "__main__":
    main()
