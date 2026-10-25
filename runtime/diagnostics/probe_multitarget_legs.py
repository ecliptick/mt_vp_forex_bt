"""Measure the multi-target TP leg structure on the CANONICAL 1m zone set.

Question: does the multi-target TP actually produce two DISTINCT legs, or
has it degenerated into a single-target trade?

Instruments ``compute_structural_tp`` to record, per fill:
  * TP1 ref (which branch produced TP1)
  * TP2 ref (which branch produced TP2)
  * the raw TP1/TP2 distances before the clamp chain
  * whether TP1 and TP2 survived ``_separate_tp_legs`` as distinct legs
    or were collapsed onto each other (the "+pinned" marker)

Usage:
    python -m runtime.diagnostics.probe_multitarget_legs
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

import bt._engine.vp_multi_target_engine as E  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/multitarget_legs.json")

SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
]

# ── instrumentation ───────────────────────────────────────────────
_ORIG_TP = E.compute_structural_tp
_RECORDS: list[dict] = []


def _patched(**kw):
    tp1, ref1, tp2, ref2, pct = _ORIG_TP(**kw)
    entry = float(kw["entry_price"])
    d = int(kw["direction"])
    dist1 = abs(tp1 - entry)
    dist2 = abs(tp2 - entry) if tp2 > 0.0 else 0.0
    _RECORDS.append({
        "tp1_ref": ref1,
        "tp2_ref": ref2,
        "dist1": dist1,
        "dist2": dist2,
        "pinned": ref1.endswith("+pinned"),
        "distinct": dist2 > 0.0 and abs(dist2 - dist1) > 0.011,
        "partial_pct": pct,
    })
    return tp1, ref1, tp2, ref2, pct


def main() -> None:
    E.compute_structural_tp = _patched

    all_rows: list[dict] = []
    for tag, s, e in SLICES:
        _RECORDS.clear()
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
        )
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek,
        )
        tr = list(res.trades)
        recs = list(_RECORDS)
        n = len(recs)
        if n == 0:
            continue

        tp1_refs = Counter(r["tp1_ref"] for r in recs)
        tp2_refs = Counter(r["tp2_ref"] for r in recs)
        distinct = sum(1 for r in recs if r["distinct"])
        pinned = sum(1 for r in recs if r["pinned"])
        d1 = np.array([r["dist1"] for r in recs])
        d2 = np.array([r["dist2"] for r in recs])

        row = {
            "tag": tag,
            "fills": n,
            "legs": len(tr),
            "tp1_refs": dict(tp1_refs.most_common()),
            "tp2_refs": dict(tp2_refs.most_common()),
            "tp1_real_shelf_pct": 100.0 * sum(
                v for k, v in tp1_refs.items() if k.startswith("multi_hvn_n")
            ) / n,
            "tp2_real_shelf_pct": 100.0 * sum(
                v for k, v in tp2_refs.items() if "multi_hvn_tp2" in k
            ) / n,
            "legs_distinct_pct": 100.0 * distinct / n,
            "legs_pinned_pct": 100.0 * pinned / n,
            "tp1_dist_p50": float(np.median(d1)),
            "tp2_dist_p50": float(np.median(d2)) if len(d2) else 0.0,
            "tp1_dist_le_2_50_pct": 100.0 * float(np.mean(d1 <= 2.50)) * 100.0,
        }
        all_rows.append(row)

        print(f"\n=== {tag} ===")
        print(f"  fills={n}  legs={len(tr)}")
        print(f"  TP1 from a REAL shelf : {row['tp1_real_shelf_pct']:5.1f}%")
        print(f"  TP2 from a REAL shelf : {row['tp2_real_shelf_pct']:5.1f}%")
        print(f"  legs DISTINCT         : {row['legs_distinct_pct']:5.1f}%")
        print(f"  legs collapsed (+pinned): {row['legs_pinned_pct']:5.1f}%")
        print(f"  TP1 dist p50 = ${row['tp1_dist_p50']:.2f}"
              f"   TP2 dist p50 = ${row['tp2_dist_p50']:.2f}")
        print("  TP1 refs:")
        for k, v in tp1_refs.most_common(6):
            print(f"     {k:32} {v:6}  {100.0*v/n:5.1f}%")
        print("  TP2 refs:")
        for k, v in tp2_refs.most_common(6):
            print(f"     {k:32} {v:6}  {100.0*v/n:5.1f}%")

        del bars, art
        import gc
        gc.collect()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(all_rows, indent=2))
    print(f"\nwrote {OUT}")


if __name__ == "__main__":
    main()
