"""CONTRA CLOSE — the RISK view (2026-10-25).

WHY THIS IS THE DECISIVE DIAGNOSTIC
===================================

The monthly verdict on ``contra_close`` was: *"the FLAT slice is
negative, so it is a beta proxy, not promotable."*

The pair-level view (``contra_close_actual_vs_simul.py``) measures
the SAME thing at the right unit and gets a materially different
answer:

    corr(paired delta, market move) : +0.49 monthly  ->  +0.19 pair-level
    DOWN slice                      : $0.00 monthly  ->  +$7.50 pair-level
    improved / worsened pairs       : 24 / 10
    mean paired delta               : +$3.97 (t = +1.48, n = 34)

So the remaining objection to the feature is NOT "it is a beta
proxy". It is a **risk** objection, and risk is what this diagnostic
measures.

THE ARGUMENT
============

``contra_close`` converts a certain small gain (the locked spread)
in exchange for giving up a larger contingent gain (the pair's
continuing run to its targets). Per pair that trade is a
**risk-management** trade: it is a certain small gain instead of an
uncertain larger one.

For a feature whose whole purpose is to convert "probable stop-out
into a certain small gain" (the user's own framing), the correct
evaluation is therefore NOT "does it increase P&L" but:

    **Does it reduce the DOWNSIDE, and what does it cost?**

THE FIVE QUESTIONS
===================

1. **MAX DRAWDOWN.** Does the feature reduce the peak-to-trough
   loss? A hedge-netting feature that leaves maxDD unchanged has
   not delivered what it promises.

2. **WORST LEG.** The worst single leg's loss. The feature's
   specific claim is that a hedged leg headed for a stop is rescued.
   That claim is about the TAIL, not the mean.

3. **WORST MONTH.** The worst month. Same reasoning, one level up.

4. **THE COST.** The mean gross per leg. If netting costs a lot of
   gross but buys a lot of drawdown, that is a legitimate trade —
   but it must be stated, not hidden.

5. **THE EXCHANGE RATE.** The explicit ratio: how much mean gross
   is surrendered per dollar of maxDD removed. This is the number
   a risk manager needs, and it is the number the monthly aggregate
   cannot produce (it has no drawdown in it at all).

THE EQUITY CURVE
================

MaxDD requires the per-bar equity curve, which the engine can
produce (``record_equity_curve=True``). It is disabled by default
above 8M bars because it is a 41M-element float64 array — but the
monthly chunks here are ~2-4M bars each, so it is affordable.

Usage
-----
    python -m runtime.diagnostics.contra_close_risk
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
OUT = Path("runtime/diagnostics/_reports/contra_close_risk.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]

# The ladder that the fine sweep identified as the live band.
THRESHOLDS = (0.0, 0.5, 1.0, 2.0, 4.5, 6.5)


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _maxdd(equity: np.ndarray) -> float:
    """Peak-to-trough drawdown of an equity curve, as a POSITIVE number."""
    if equity.size == 0:
        return 0.0
    peak = np.maximum.accumulate(equity)
    dd = peak - equity
    return float(dd.max())


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
        record_equity_curve=True,
    )
    tr = list(res.trades)
    gross = sum(t.pnl_gross_usd for t in tr)
    net = sum(t.pnl_net_usd for t in tr)
    fees = sum(t.fee_usd for t in tr)
    leg_nets = np.asarray([t.pnl_net_usd for t in tr], dtype=np.float64)
    eq = getattr(res, "equity_curve", None)
    if eq is None:
        eq = np.zeros(0, dtype=np.float64)
    else:
        eq = np.asarray(eq, dtype=np.float64)
    return {
        "legs": len(tr),
        "gross": gross,
        "fees": fees,
        "net": net,
        "events": int(getattr(res, "n_contra_close_events", 0)),
        "maxdd": _maxdd(eq),
        "worst_leg": float(leg_nets.min()) if leg_nets.size else 0.0,
        "final_equity": float(eq[-1]) if eq.size else 0.0,
        "gross_per_leg": (gross / len(tr)) if tr else 0.0,
        "net_per_leg": (net / len(tr)) if tr else 0.0,
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    per: dict[float, dict[str, dict]] = {t: {} for t in THRESHOLDS}

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
        mkt = 100.0 * (c1 - c0) / c0
        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

        for thr in THRESHOLDS:
            r = _run(bars, art, spot, thr)
            r["market_pct"] = mkt
            per[thr][ym] = r

        print(f"  {ym} mkt={mkt:+6.2f}%  " + "  ".join(
            f"t{t:g}:dd${per[t][ym]['maxdd']:,.0f}/ev{per[t][ym]['events']}"
            for t in THRESHOLDS), flush=True)

        del bars, art
        import gc
        gc.collect()

    # ── Report ────────────────────────────────────────────────────
    print()
    print("=" * 118)
    print("CONTRA CLOSE — THE RISK VIEW")
    print("=" * 118)
    print(f"{'thr':>6} {'events':>7} {'gross':>10} {'net':>10} "
          f"{'maxDD':>10} {'worstLeg':>10} {'worstMo':>10} "
          f"{'g/leg':>8} {'DDout':>8} {'net/leg':>9}")

    summary: dict[str, dict] = {}
    base = per[0.0]
    for thr in THRESHOLDS:
        p = per[thr]
        gross = sum(v["gross"] for v in p.values())
        net = sum(v["net"] for v in p.values())
        fees = sum(v["fees"] for v in p.values())
        legs = sum(v["legs"] for v in p.values())
        events = sum(v["events"] for v in p.values())
        # The portfolio maxDD is the worst month's maxDD plus the
        # compounding of the monthly net, which is the conservative
        # reading (the monthly chunks are not contiguous).
        maxdd = sum(v["maxdd"] for v in p.values())
        worst_leg = min(v["worst_leg"] for v in p.values())
        worst_mo = min(v["net"] for v in p.values())
        gpl = (gross / legs) if legs else 0.0
        npl = (net / legs) if legs else 0.0

        b_gross = sum(v["gross"] for v in base.values())
        b_net = sum(v["net"] for v in base.values())
        b_maxdd = sum(v["maxdd"] for v in base.values())
        b_legs = sum(v["legs"] for v in base.values())
        b_worst_leg = min(v["worst_leg"] for v in base.values())
        b_worst_mo = min(v["net"] for v in base.values())
        b_gpl = (b_gross / b_legs) if b_legs else 0.0

        dd_out = b_maxdd - maxdd
        summary[f"{thr:g}"] = {
            "events": events, "legs": legs,
            "gross": gross, "fees": fees, "net": net,
            "maxdd_sum": maxdd, "worst_leg": worst_leg,
            "worst_month": worst_mo,
            "gross_per_leg": gpl, "net_per_leg": npl,
            "delta_gross": gross - b_gross,
            "delta_net": net - b_net,
            "delta_maxdd": maxdd - b_maxdd,
            "delta_worst_leg": worst_leg - b_worst_leg,
            "delta_worst_month": worst_mo - b_worst_mo,
            "dd_reduction": dd_out,
            # The exchange rate: gross surrendered per $ of DD removed.
            "gross_per_dd_removed": (
                ((gross - b_gross) / dd_out) if dd_out > 1e-9 else float("nan")),
        }
        print(f"{thr:>6g} {events:>7} {gross:>10,.2f} {net:>10,.2f} "
              f"{maxdd:>10,.2f} {worst_leg:>10,.2f} {worst_mo:>10,.2f} "
              f"{gpl:>8.4f} {dd_out:>8.2f} {npl:>9.4f}")

    print()
    print(f"  OFF baseline: gross=${sum(v['gross'] for v in base.values()):,.2f} "
          f"net=${sum(v['net'] for v in base.values()):,.2f} "
          f"maxDD=${sum(v['maxdd'] for v in base.values()):,.2f} "
          f"worstLeg=${min(v['worst_leg'] for v in base.values()):,.2f} "
          f"worstMo=${min(v['net'] for v in base.values()):,.2f}")
    print()
    print("  EXCHANGE RATE (gross surrendered per $1 of maxDD removed):")
    for thr in THRESHOLDS:
        if thr == 0.0:
            continue
        d = summary[f"{thr:g}"]
        rate = d["gross_per_dd_removed"]
        rr = ("n/a" if not np.isfinite(rate)
              else f"${abs(rate):,.2f} gross per $1 of DD")
        print(f"    thr ${thr:<5g} dGross=${d['delta_gross']:>9,.2f}  "
              f"dDD=${d['delta_maxdd']:>9,.2f}  "
              f"worstLeg ${d['delta_worst_leg']:>+8.2f}  "
              f"worstMo ${d['delta_worst_month']:>+8.2f}  -> {rr}")

    OUT.write_text(json.dumps(
        {"thresholds": list(THRESHOLDS),
         "summary": summary,
         "per_threshold": {f"{t:g}": per[t] for t in THRESHOLDS}},
        indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
