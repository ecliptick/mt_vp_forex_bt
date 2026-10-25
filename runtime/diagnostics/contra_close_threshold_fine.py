"""DECISIVE — can the EXISTING passive scan capture the loss, or is new
engine code needed?

Three implementations of the same mechanism are in play:

  A. PASSIVE SCAN (what ships today, ``contra_close``).
     Every bar, look at the book's weighted-average entry spread; if it
     exceeds ``contra_close_spread_usd``, net the overlapping quantity
     immediately. Trigger = SPREAD THRESHOLD.

  B. EXIT-TRIGGERED (what the user actually described).
     When a leg is about to close on its stop/target, net the opposing
     leg's overlapping quantity SIMULTANEOUSLY at the same price.
     Trigger = AN EXIT.

  C. THE COUNTERFACTUAL (measured in
     ``contra_close_actual_vs_simul.py``): what the pair would have
     realised if both legs had been closed together at the first
     exit. This is the UPPER BOUND on what B can deliver.

This script runs A over a fine threshold ladder and compares it to C,
so we can answer: does A already do the job, or must B be built?

Usage:
    python -m runtime.diagnostics.contra_close_threshold_fine
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
OUT = Path("runtime/diagnostics/_reports/contra_close_threshold_fine.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]

# Fine ladder. ``0.01`` is the tightest the engine allows (it requires
# ``contra_close_spread_usd > 0.0``) and means "net the moment ANY
# positive lock exists".
LADDER = [0.01, 0.25, 0.5, 1.0, 1.5, 2.0, 3.0, 4.5, 6.5, 10.0]


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _run(bars, art, spot, thr: float) -> dict:
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        contra_close=(thr > 0.0),
        contra_close_spread_usd=thr,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
    )
    tr = list(res.trades)
    return {
        "legs": len(tr),
        "gross": sum(t.pnl_gross_usd for t in tr),
        "fees": sum(t.fee_usd for t in tr),
        "net": sum(t.pnl_net_usd for t in tr),
        "events": int(getattr(res, "n_contra_close_events", 0)),
        "contra_gross": float(getattr(res, "contra_gross_usd", 0.0)),
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    market: dict[str, float] = {}
    per: dict[float, dict[str, dict]] = {t: {} for t in LADDER}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            print(f"  skip {ym}", flush=True)
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        c0 = float(bars["close"].iloc[0])
        c1 = float(bars["close"].iloc[-1])
        market[ym] = 100.0 * (c1 - c0) / c0
        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

        for thr in LADDER:
            r = _run(bars, art, spot, thr)
            r["market_pct"] = market[ym]
            per[thr][ym] = r

        print(f"  {ym} mkt={market[ym]:+6.2f}%  "
              + "  ".join(f"t{thr:g}:{per[thr][ym]['events']}"
                          for thr in LADDER), flush=True)

        del bars, art
        import gc
        gc.collect()

    # ── Report ────────────────────────────────────────────────────
    print()
    print("=" * 112)
    print("CONTRA CLOSE — FINE THRESHOLD LADDER (passive scan)")
    print("=" * 112)
    print(f"{'thr':>7} {'events':>7} {'dGross':>11} {'dNet':>11} "
          f"{'UP dG':>10} {'DOWN dG':>10} {'FLAT dG':>10} "
          f"{'mo+gross':>10} {'mo+net':>9}")

    base = per[0.01]
    # The OFF baseline is the same engine with the feature disabled.
    # ``0.01`` is the tightest threshold, so it is NOT the baseline --
    # recompute the baseline explicitly from the per-threshold ladder's
    # own event-free rows is not possible, so use the OFF run.
    # (The OFF run is the thr whose events == 0 for every month; with
    #  this ladder there is none, so the OFF baseline is taken from the
    #  FIRST ladder entry being effectively "net on any positive lock"
    #  and the true OFF baseline is re-derived below.)

    # True OFF baseline.
    off: dict[str, dict] = {}
    for ym in MONTHS:
        if ym not in market:
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        r = _run(bars, art, spot, 0.0)
        r["market_pct"] = market[ym]
        off[ym] = r
        del bars, art
        import gc
        gc.collect()

    summary: dict[str, dict] = {}
    for thr in LADDER:
        p = per[thr]
        dg = sum(v["gross"] - off[ym]["gross"] for ym, v in p.items())
        dn = sum(v["net"] - off[ym]["net"] for ym, v in p.items())
        ev = sum(v["events"] for v in p.values())
        gp = sum(1 for ym, v in p.items() if v["gross"] > 0)
        np_ = sum(1 for ym, v in p.items() if v["net"] > 0)
        gp0 = sum(1 for ym, v in off.items() if v["gross"] > 0)
        np0 = sum(1 for ym, v in off.items() if v["net"] > 0)
        cells = {}
        for key, pred in (("up", lambda m: m > 1.0),
                          ("down", lambda m: m < -1.0),
                          ("flat", lambda m: -1.0 <= m <= 1.0)):
            cells[key] = sum(
                v["gross"] - off[ym]["gross"]
                for ym, v in p.items() if pred(v["market_pct"]))
        summary[f"{thr:g}"] = {
            "events": ev, "delta_gross": dg, "delta_net": dn,
            "delta_gross_up": cells["up"],
            "delta_gross_down": cells["down"],
            "delta_gross_flat": cells["flat"],
            "months_positive_gross": gp, "months_positive_net": np_,
            "baseline_months_positive_gross": gp0,
            "baseline_months_positive_net": np0,
        }
        print(f"{thr:>7g} {ev:>7} {dg:>11,.2f} {dn:>11,.2f} "
              f"{cells['up']:>10,.2f} {cells['down']:>10,.2f} "
              f"{cells['flat']:>10,.2f} "
              f"{gp:>5}/{len(p):<4} {np_:>4}/{len(p):<4}")

    # ── Compare against the counterfactual upper bound ───────────
    cf_path = Path("runtime/diagnostics/_reports/"
                   "contra_close_actual_vs_simul.json")
    cf_delta = None
    if cf_path.exists():
        cf = json.loads(cf_path.read_text(encoding="utf-8"))
        rows = []
        for ym, v in cf["per_month"].items():
            for r in v["rows"]:
                r["mkt"] = v["market_pct"]
                rows.append(r)
        cf_delta = sum(r["delta"] for r in rows)
        print()
        print(f"counterfactual upper bound (exit-triggered, every pair): "
              f"${cf_delta:,.2f}")
        print(f"best passive-scan threshold achieves: "
              f"${max(v['delta_gross'] for v in summary.values()):,.2f}")
        capture = (max(v["delta_gross"] for v in summary.values())
                   / cf_delta * 100.0) if cf_delta else float("nan")
        print(f"-> the passive scan captures {capture:.0f}% of the "
              f"counterfactual")
        print()
        print("counterfactual delta BY SPREAD (what threshold would be "
              "needed to capture each pair):")
        for thr in LADDER:
            sel = [r for r in rows if r["spread"] >= thr]
            print(f"  thr ${thr:<6g} pairs={len(sel):>2} "
                  f"delta=${sum(r['delta'] for r in sel):>9,.2f}")

    OUT.write_text(json.dumps(
        {"ladder": LADDER, "summary": summary,
         "counterfactual_delta": cf_delta,
         "off_baseline": off,
         "per_threshold": {f"{t:g}": per[t] for t in LADDER}},
        indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
