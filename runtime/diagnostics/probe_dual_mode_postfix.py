"""Re-measure the dual-mode book AFTER the 2026-11-03 causality fix.

BEFORE the fix, the immediate path skipped any zone with
``zone.inverted_bar >= 0`` -- a whole-file verdict read at the zone's
trigger bar.  The fix changes the test to "had the zone ALREADY
inverted BY THIS BAR?".

This probe runs the SHIPPED engine (now fixed) in "dual" mode over N
monthly slices and reports the book, so the fix's effect is measured
against the pre-fix numbers recorded in
``dual_mode_multislice.json``.

Run:
    python -m runtime.diagnostics.probe_dual_mode_postfix
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
       / "dual_mode_postfix.json")
PREV = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
        / "dual_mode_multislice.json")


def _run(art, bars, entry_mode: str) -> dict:
    import inspect

    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
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
    # Drop any knob the engine does not accept. A concurrent refactor can
    # leave the config advertising knobs the engine has not grown yet;
    # that is ITS problem, and it must not make this probe unrunnable.
    # The dropped knobs are recorded in the result so the reader can see
    # the book was measured under a reduced config.
    accepted = set(inspect.signature(run_vp_sniper_backtest).parameters)
    dropped = sorted(set(ek) - accepted)
    for k in dropped:
        ek.pop(k, None)

    res, _diag = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
        record_equity_curve=False,
    )
    trades = list(res.trades)
    gross = float(sum(t.pnl_gross_usd for t in trades))
    fees = float(sum(t.fee_usd for t in trades))
    return {
        "entry_mode": entry_mode,
        "dropped_unknown_knobs": dropped,
        "n_legs": len(trades),
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "net": round(gross - fees, 4),
        "gross_per_leg": round(gross / max(1, len(trades)), 6),
        "skipped_dual_inverted": int(getattr(res, "n_skipped_dual_inverted", 0)),
        "skipped_already_fired": int(getattr(res, "n_skipped_already_fired", 0)),
    }


def main() -> None:
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    prev = {}
    if PREV.exists():
        p = json.loads(PREV.read_text(encoding="utf-8"))
        prev = {r["slice"]: r for r in p.get("slices", [])}

    rows = []
    for start, end in SLICES:
        scope = f"XAUUSD_S1_1y__{start[:7]}"
        print(f"[probe] slice {start} -> {end} ...", flush=True)
        bars = load_bars(DATA, start, end)
        if bars.empty:
            continue
        art = ArtifactCache(DATA, scope, start, end).build_or_load(bars)

        d = _run(art, bars, "dual")
        im = _run(art, bars, "immediate")
        key = f"{start}..{end}"
        row = {
            "slice": key,
            "n_zones": len(art["zones"]),
            "dual_postfix": d,
            "immediate": im,
            "delta_legs": im["n_legs"] - d["n_legs"],
            "delta_net": round(im["net"] - d["net"], 4),
        }
        if key in prev:
            pd_ = prev[key]["dual"]
            row["dual_pre_fix"] = pd_
            row["delta_vs_pre_fix_legs"] = d["n_legs"] - pd_["n_legs"]
            row["delta_vs_pre_fix_net"] = round(d["net"] - pd_["net"], 4)
        rows.append(row)
        print(f"[probe]   dual(post-fix): legs={d['n_legs']} net={d['net']} "
              f"| immediate: legs={im['n_legs']} net={im['net']}", flush=True)

    d_nets = [r["dual_postfix"]["net"] for r in rows]
    i_nets = [r["immediate"]["net"] for r in rows]
    out = {
        "slices": rows,
        "n_slices": len(rows),
        "dual_postfix_net_sum": round(sum(d_nets), 4),
        "immediate_net_sum": round(sum(i_nets), 4),
        "dual_postfix_net_positive_slices": sum(1 for r in rows if r["dual_postfix"]["net"] > 0),
        "immediate_net_positive_slices": sum(1 for r in rows if r["immediate"]["net"] > 0),
        "dual_postfix_gross_per_leg_mean": round(
            float(np.mean([r["dual_postfix"]["gross_per_leg"] for r in rows])), 6),
        "immediate_gross_per_leg_mean": round(
            float(np.mean([r["immediate"]["gross_per_leg"] for r in rows])), 6),
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }
    if "dual_pre_fix" in rows[0]:
        out["dual_pre_fix_net_sum"] = round(
            sum(r["dual_pre_fix"]["net"] for r in rows), 4)
        out["net_sum_delta_pre_to_post"] = round(
            out["dual_postfix_net_sum"] - out["dual_pre_fix_net_sum"], 4)

    print("\n=== dual-mode: POST-FIX ===")
    for k, v in out.items():
        if k != "slices":
            print(f"  {k}: {v}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[probe] wrote {OUT}")


if __name__ == "__main__":
    main()
