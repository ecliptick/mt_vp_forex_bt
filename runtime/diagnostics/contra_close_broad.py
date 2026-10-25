"""CONTRA CLOSE — validation on EVERY cached month (2026-10-24).

WHY THIS EXISTS. The 6-slice A/B (`runtime/diagnostics/contra_close_ab.py`)
measured a total delta of +$83.14 gross at a $4.50 threshold. That
looks like a win, but:

  * the gain is CONCENTRATED in 2026-01 BULL (+$85.72 of the $83.14);
  * the FLAT slice — the decisive test the beta-proxy diagnosis
    demands — is NEGATIVE (-$11.78);
  * there were only NINE contra events in total, on 384 legs.

The mechanism diagnostic (`contra_close_why.py`) shows the mechanism is
a genuine risk reduction: on 2026-01 the netted legs would have been
STOPPED OUT in the OFF run (3 of 4 legs -> 'sl'), so the netting
avoided real losses. But on 2025-05 FLAT the same legs would have
reached TP1/TP2 (+$36.50) and the netting gave that up for +$24.73.

So the netting is NOT a free win — it TRADES a certain small gain for
the avoidance of a certain larger loss, and which side wins depends on
the regime. That is a real trade-off, not an edge, and it needs a
sample big enough to say which side wins more often.

The 6-slice sample is exactly the trap AGENTS.md documents for the
vol-state gate: "A gate evaluated on 3-6 slices will find a threshold
that 'works'. Validate on every cached month before believing it."
The vol gate put +$69 on 6 slices and -$364 on 15 months.

This runs every cached month, splits the result by market direction,
and reports the flat-month result separately.

Usage
-----
    python -m runtime.diagnostics.contra_close_broad
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
    CONTRA_CLOSE_SPREAD_USD as CANON_SPREAD_USD,
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/contra_close_broad.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]

THRESHOLDS = [0.0, 1.0, 2.5, 4.5, 5.0, 5.5, 6.0, 6.5, 7.5, 10.0, 12.0]
FEE_PER_LEG = 0.47


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
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    gross = sum(t.pnl_gross_usd for t in tr)
    net = sum(t.pnl_net_usd for t in tr)
    contra = [t for t in tr if t.exit_reason == "contra_close"]
    return {
        "legs": len(tr),
        "gross": gross,
        "fees": sum(t.fee_usd for t in tr),
        "net": net,
        "n_contra_events": int(getattr(res, "n_contra_close_events", 0)),
        "n_contra_legs": int(getattr(res, "n_contra_legs_closed", 0)),
        "contra_qty": float(getattr(res, "n_contra_qty_netted", 0.0)),
        "contra_gross": float(getattr(res, "contra_gross_usd", 0.0)),
        "contra_fees": float(getattr(res, "contra_fees_usd", 0.0)),
        "contra_spread_max": float(getattr(res, "contra_spread_usd_max", 0.0)),
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    market: dict[str, float] = {}
    rows: dict[float, dict[str, dict]] = {t: {} for t in THRESHOLDS}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            print(f"  skip {ym}: no cache", flush=True)
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

        line = [f"  {ym} mkt={market[ym]:+6.2f}%"]
        for thr in THRESHOLDS:
            r = _run(bars, art, spot, thr)
            r["market_pct"] = market[ym]
            rows[thr][ym] = r
            line.append(
                f"t{thr:g}: ev={r['n_contra_events']:>2} "
                f"dG=${r['gross'] - rows[0.0][ym]['gross']:>8,.1f} "
                f"net=${r['net']:>8,.1f}"
            )
        print("  ".join(line), flush=True)

        del bars, art
        import gc
        gc.collect()

    # ── Report ────────────────────────────────────────────────────
    print()
    print("=" * 108)
    print("CONTRA CLOSE — ALL CACHED MONTHS "
          "(canonical scale: 0.01 lots x 100 cs, 1.00 oz)")
    print("=" * 108)
    print(f"{'thr':>6} {'legs':>7} {'gross':>11} {'fees':>10} {'net':>11} "
          f"{'dGross':>10} {'dNet':>10} {'events':>7} {'mo+gross':>9} "
          f"{'mo+net':>8}")

    base = rows[0.0]
    summary: dict[str, dict] = {}
    for thr in THRESHOLDS:
        per = rows[thr]
        legs = sum(v["legs"] for v in per.values())
        gross = sum(v["gross"] for v in per.values())
        fees = sum(v["fees"] for v in per.values())
        net = sum(v["net"] for v in per.values())
        d_gross = gross - sum(v["gross"] for v in base.values())
        d_net = net - sum(v["net"] for v in base.values())
        ev = sum(v["n_contra_events"] for v in per.values())
        gpos = sum(1 for v in per.values() if v["gross"] > 0)
        npos = sum(1 for v in per.values() if v["net"] > 0)
        d_gross_flat = sum(
            v["gross"] - base[ym]["gross"]
            for ym, v in per.items()
            if -1.0 <= v["market_pct"] <= 1.0)
        d_net_flat = sum(
            v["net"] - base[ym]["net"]
            for ym, v in per.items()
            if -1.0 <= v["market_pct"] <= 1.0)
        n_flat = sum(1 for v in per.values() if -1.0 <= v["market_pct"] <= 1.0)
        summary[f"{thr:g}"] = {
            "months": len(per), "legs": legs, "gross": gross,
            "fees": fees, "net": net,
            "delta_gross": d_gross, "delta_net": d_net,
            "delta_gross_flat": d_gross_flat,
            "delta_net_flat": d_net_flat,
            "n_flat_months": n_flat,
            "total_contra_events": ev,
            "months_positive_gross": gpos,
            "months_positive_net": npos,
            "per_month": per,
        }
        print(f"{thr:>6g} {legs:>7} {gross:>11,.2f} {fees:>10,.2f} "
              f"{net:>11,.2f} {d_gross:>10,.2f} {d_net:>10,.2f} "
              f"{ev:>7} {gpos:>4}/{len(per):<4} {npos:>3}/{len(per):<4}"
              + (f"  FLAT dG={d_gross_flat:>9,.2f} dN={d_net_flat:>9,.2f}"
                 f" ({n_flat} mo)" if thr > 0 else ""))

    # ── Directional split ─────────────────────────────────────────
    print()
    print("DIRECTIONAL CHECK — delta gross, split by market direction")
    print(f"{'thr':>6} | {'UP months':>26} | {'DOWN months':>26} | "
          f"{'FLAT months':>26}")
    print(f"{'':>6} | {'n':>3} {'dGross':>11} {'dNet':>11} | "
          f"{'n':>3} {'dGross':>11} {'dNet':>11} | "
          f"{'n':>3} {'dGross':>11} {'dNet':>11}")
    for thr in THRESHOLDS:
        per = rows[thr]
        groups: dict[str, list] = {"up": [], "down": [], "flat": []}
        for ym, v in per.items():
            m = v["market_pct"]
            key = "up" if m > 1.0 else ("down" if m < -1.0 else "flat")
            groups[key].append((v, base[ym]))
        cells = []
        for key in ("up", "down", "flat"):
            g = groups[key]
            dg = sum(v["gross"] - b["gross"] for v, b in g)
            dn = sum(v["net"] - b["net"] for v, b in g)
            cells.append((len(g), dg, dn))
        (nu, dgu, dnu), (nd, dgd, dnd), (nf, dgf, dnf) = cells
        print(f"{thr:>6g} | {nu:>3} {dgu:>11,.2f} {dnu:>11,.2f} | "
              f"{nd:>3} {dgd:>11,.2f} {dnd:>11,.2f} | "
              f"{nf:>3} {dgf:>11,.2f} {dnf:>11,.2f}")

    # ── Correlation of the delta with the market move ─────────────
    #
    # The reference threshold is the CANONICAL one, read from the
    # config rather than hardcoded here. A previous version hardcoded
    # ``rows[4.5]`` and crashed with a KeyError whenever the ladder
    # was edited to omit 4.5 — losing the whole report after ~15
    # minutes of measurement. Resolve it against the thresholds
    # actually swept, and degrade to NaN instead of raising.
    print()
    xs, ys = [], []
    corr_ref = min(THRESHOLDS, key=lambda t: abs(t - CANON_SPREAD_USD))
    if corr_ref not in rows:
        corr = float("nan")
    else:
        for ym in base:
            xs.append(base[ym]["market_pct"])
            ys.append(rows[corr_ref][ym]["gross"] - base[ym]["gross"])
        if len(xs) > 2:
            corr = float(np.corrcoef(xs, ys)[0, 1])
        else:
            corr = float("nan")
        print(f"corr(delta gross @ ${corr_ref:.2f}, market move) = {corr:+.4f}  "
              f"(n={len(xs)} months)")

    out = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "months": list(market),
        "thresholds": THRESHOLDS,
        "summary": summary,
        "corr_delta_vs_market_ref_threshold": corr_ref,
        "corr_delta_vs_market": corr,
    }
    OUT.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
