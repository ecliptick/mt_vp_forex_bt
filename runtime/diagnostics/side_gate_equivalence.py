"""Verify the engine-side SIDE GATE matches a post-hoc filter.

The side gate was measured post-hoc (discard the short legs after the
engine ran) in ``short_side_ablation.py``. This module proves the
ENGINE implementation produces the identical trade set, so the
measurement transfers to the in-engine knob.

Why they should agree
----------------------
Under ``conflict_policy="stack"`` an opposite-direction signal never
blocks admission, and ``max_same_dir_open`` is PER-DIRECTION. So
removing shorts cannot change which longs are admitted. This script
checks that empirically rather than trusting the reasoning.

Usage
-----
    python -m runtime.diagnostics.side_gate_equivalence
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
OUT = Path("runtime/diagnostics/_reports/side_gate_equivalence.json")

SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
    ("2024-06 FLAT", "2024-06-01", "2024-07-01"),
    ("2026-04 FLAT", "2026-04-01", "2026-05-01"),
]


def _artifacts(tag: str, start: str, end: str, bars) -> dict:
    return ArtifactCache(DATA, f"ssa_{tag}", start, end).build_or_load(bars)


def _run(label: str, start: str, end: str, trade_sides: str) -> dict:
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        trade_sides=trade_sides,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    trades = list(res.trades)
    return {
        "trades": trades,
        "legs": len(trades),
        "long_legs": sum(1 for t in trades if int(t.direction) > 0),
        "short_legs": sum(1 for t in trades if int(t.direction) < 0),
        "gross": float(sum(t.pnl_gross_usd for t in trades)),
        "net": float(sum(t.pnl_net_usd for t in trades)),
        "n_dropped_sides": int(getattr(res, "n_dropped_sides", 0)),
    }


def main() -> None:
    print("== side-gate equivalence: engine knob vs post-hoc filter ==")
    print()
    all_ok = True
    rows = []
    for label, s, e in SLICES:
        both = _run(label, s, e, "both")
        gate = _run(label, s, e, "long")

        # Post-hoc reference: take the BOTH arm and drop the shorts.
        ph_legs = both["long_legs"]
        ph_gross = float(sum(t.pnl_gross_usd for t in both["trades"]
                              if int(t.direction) > 0))

        legs_ok = gate["legs"] == ph_legs
        gross_ok = abs(gate["gross"] - ph_gross) < 1e-9
        no_shorts = gate["short_legs"] == 0
        ok = legs_ok and gross_ok and no_shorts
        all_ok = all_ok and ok

        rows.append({
            "slice": label,
            "both_legs": both["legs"], "both_long": both["long_legs"],
            "both_short": both["short_legs"],
            "engine_gate_legs": gate["legs"],
            "posthoc_legs": ph_legs,
            "engine_gate_gross": gate["gross"], "posthoc_gross": ph_gross,
            "engine_gate_net": gate["net"],
            "n_dropped_sides": gate["n_dropped_sides"],
            "equivalent": ok,
        })
        print(f"  {label:<12} both={both['legs']:>4} "
              f"(L{both['long_legs']:>3}/S{both['short_legs']:>3})  "
              f"gate={gate['legs']:>4}  posthoc={ph_legs:>4}  "
              f"dropped_sides={gate['n_dropped_sides']:>4}  "
              f"gross {gate['gross']:>8,.2f} vs {ph_gross:>8,.2f}  "
              f"{'OK' if ok else 'MISMATCH'}", flush=True)

    print()
    print(f"  ALL SLICES EQUIVALENT: {all_ok}")
    if not all_ok:
        print("  => The engine knob does NOT match the post-hoc filter.")
        print("     Do not trust the ablation measurement.")
    else:
        print("  => The engine-side side gate is EXACTLY equivalent to the")
        print("     post-hoc filter, so the ablation measurement transfers.")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "all_equivalent": all_ok}, fh, indent=2,
                  default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
