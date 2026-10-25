"""VOL GATE across EVERY cached month (2026-10-24) — broad validation.

WHY THIS EXISTS
---------------
``runtime/diagnostics/vol_gate_multislice.py`` found that the
``vol_state_min_range_norm`` gate raises the GROSS EDGE PER LEG by
7.6x ($0.078 -> $0.589) at threshold 3.0, which crosses the $0.47
round trip and turns the total net positive for the first time.

But that result rested on only 525 legs (51-126 per slice) and its
HELD-OUT group was NEGATIVE (FIT +$123.98 / HELD -$55.39). A
525-leg sample is far too thin to promote anything, and this repo's
rule is explicit: a single-slice (or thin-slice) winner is not
evidence.

So this runs the SAME gate across every month that already has a
cached 1m artifact bundle -- 15 months, ~10x the sample -- and
reports the per-slice detail so the regime dependence is visible
rather than averaged away.

The decisive questions this answers:

1. Is the 7.6x gross-per-leg improvement REAL, or is it a small-sample
   artifact? (It must reproduce on most months.)
2. Is the improvement DIRECTIONAL? A gate that only helps on months
   the market moved one way is a beta proxy. So the per-month market
   return is measured and reported alongside.
3. Does the FLAT slice improve? That is the repo's stated decisive
   test for timing skill.

    python -m runtime.diagnostics.vol_gate_broad
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
from runtime.diagnostics.vol_gate_multislice import THRESHOLDS  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/vol_gate_broad.json")
CACHE_ROOT = Path("notebooks/.cache")

# Only months that ALREADY have a cached 1m artifact bundle, so the
# whole sweep is a cache HIT and costs no detector time.
MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]

FEE_PER_LEG = 0.47  # $0.07 commission + $0.40 spread, round trip


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _run(bars, art, spot, thr: float) -> dict:
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        vol_state_min_range_norm=thr,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    gross = sum(t.pnl_gross_usd for t in tr)
    net = sum(t.pnl_net_usd for t in tr)
    wins = [t for t in tr if t.pnl_gross_usd > 0]
    losses = [t for t in tr if t.pnl_gross_usd <= 0]
    aw = float(np.mean([t.pnl_gross_usd for t in wins])) if wins else 0.0
    al = abs(float(np.mean([t.pnl_gross_usd for t in losses]))) if losses else 0.0
    payoff = aw / al if al > 0 else float("nan")
    return {
        "legs": len(tr),
        "gross": gross,
        "net": net,
        "win_rate": 100.0 * len(wins) / max(len(tr), 1),
        "payoff": payoff,
        "breakeven_win_pct": (100.0 / (1.0 + payoff)) if payoff == payoff else float("nan"),
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    rows: dict[float, dict[str, dict]] = {thr: {} for thr in THRESHOLDS}
    market: dict[str, float] = {}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            print(f"  skip {ym}: no cache", flush=True)
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        # The market's own move over the slice -- the beta reference.
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
                f"thr{thr}: n={r['legs']:>4} g/leg=${r['gross']/max(r['legs'],1):>6.3f} "
                f"net=${r['net']:>9,.1f}"
            )
        print("  ".join(line), flush=True)

        del bars, art
        import gc
        gc.collect()

    # ── Report ────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("VOL GATE — ALL CACHED MONTHS (canonical scale: 0.01 lots x 100 cs, $0.47 round trip)")
    print("=" * 100)
    print(f"{'thr':>5} {'legs':>7} {'gross':>11} {'fees':>10} {'net':>11} "
          f"{'g/leg':>8} {'g/leg/fee':>10} {'months+':>8} {'gross+':>7}")
    summary = {}
    for thr in THRESHOLDS:
        per = rows[thr]
        legs = sum(v["legs"] for v in per.values())
        gross = sum(v["gross"] for v in per.values())
        fees = legs * FEE_PER_LEG
        net = sum(v["net"] for v in per.values())
        pos = sum(1 for v in per.values() if v["net"] > 0)
        gpos = sum(1 for v in per.values() if v["gross"] > 0)
        g_leg = gross / max(legs, 1)
        summary[thr] = {
            "months": len(per),
            "legs": legs, "gross": gross, "fees": fees, "net": net,
            "gross_per_leg": g_leg,
            "gross_per_leg_over_fee": g_leg / FEE_PER_LEG,
            "months_positive_net": pos,
            "months_positive_gross": gpos,
            "per_month": per,
        }
        print(f"{thr:>5.1f} {legs:>7} {gross:>11,.2f} {fees:>10,.2f} {net:>11,.2f} "
              f"{g_leg:>8.4f} {g_leg/FEE_PER_LEG:>10.3f} {pos:>5}/{len(per)} {gpos:>5}/{len(per)}")

    # ── Directional dependence: does the gate only help one way? ──
    print()
    print("DIRECTIONAL CHECK — gross per leg, split by market direction")
    print(f"{'thr':>5} | {'UP months':>28} | {'DOWN months':>28} | {'FLAT months':>28}")
    print(f"{'':>5} | {'n':>5} {'g/leg':>9} {'net':>10} | {'n':>5} {'g/leg':>9} {'net':>10} | "
          f"{'n':>5} {'g/leg':>9} {'net':>10}")
    for thr in THRESHOLDS:
        per = rows[thr]
        groups = {"up": [], "down": [], "flat": []}
        for ym, v in per.items():
            m = market[ym]
            key = "up" if m > 1.0 else ("down" if m < -1.0 else "flat")
            groups[key].append(v)
        cells = []
        for key in ("up", "down", "flat"):
            g = groups[key]
            n = sum(x["legs"] for x in g)
            gl = sum(x["gross"] for x in g) / max(n, 1)
            nt = sum(x["net"] for x in g)
            cells.append(f"{n:>5} {gl:>9.4f} {nt:>10,.1f}")
        print(f"{thr:>5.1f} | {cells[0]} | {cells[1]} | {cells[2]}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"summary": summary, "market_pct": market}, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
