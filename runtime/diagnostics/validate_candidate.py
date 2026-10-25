"""HELD-OUT VALIDATION of the combined-screen winner (2026-10-24).

The combined screen found ONE candidate that passes the decisive
test::

    conflict_policy='stack', sl_max_usd=2.50, vol_state_min_range_norm=3.0
    -> 3/6 slices positive, FLAT slice positive, total net +$0.69

(+$0.69 in diagnostic dollars = +$69 canonical.)

But that candidate was SELECTED on those six slices. Two things must
be checked before it is even a candidate:

1. **OUT OF SAMPLE.** Does it hold on months that were not in the
   screen? The screen's six slices were 2026-01, 2026-03, 2025-05,
   2025-01, 2024-11, 2025-09. This runs it on FRESH months drawn
   from the same 1y file.

2. **STATISTICAL SIGNIFICANCE.** The winner trades 525 legs across
   six slices -- ~87/month. At that sample size, a +$0.13/leg edge
   is indistinguishable from zero. This runs a BOOTSTRAP over the
   per-leg P&L and reports the 90% confidence interval on EV/leg and
   on the total.

A candidate that only survives on the slices it was chosen from, or
whose CI straddles zero, is NOT an edge.

Also reported: the per-slice attribution, because the screen winner's
total was dominated by a single BULL slice (+1.27 of +0.69) and
that is the beta-proxy signature.

Usage
-----
    python -m runtime.diagnostics.validate_candidate
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
OUT = Path("runtime/diagnostics/_reports/validate_candidate.json")

# The slices the combined screen was run on.
FIT_SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

# FRESH months, disjoint from the screen. Chosen to span regimes.
HELD_SLICES = [
    ("2024-06 FLAT", "2024-06-01", "2024-07-01", "flat"),
    ("2024-08 BEAR", "2024-08-01", "2024-09-01", "bear"),
    ("2024-12 BULL", "2024-12-01", "2025-01-01", "bull"),
    ("2025-03 FLAT", "2025-03-01", "2025-04-01", "flat"),
    ("2025-06 BULL", "2025-06-01", "2025-07-01", "bull"),
    ("2025-10 BEAR", "2025-10-01", "2025-11-01", "bear"),
    ("2025-12 UP",   "2025-12-01", "2026-01-01", "bull"),
    ("2026-04 BEAR", "2026-04-01", "2026-05-01", "bear"),
    ("2026-06 FLAT", "2026-06-01", "2026-07-01", "flat"),
]

WINNER = {
    "conflict_policy": "stack",
    "sl_max_usd": 2.50,
    "vol_state_min_range_norm": 3.0,
}

RNG = np.random.default_rng(20261024)


def _run_slice(tag, s, e, regime, over):
    bars = load_bars(DATA, s, e)
    art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, **over,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    out = {
        "tag": tag, "regime": regime,
        "legs": len(tr),
        "gross": float(sum(t.pnl_gross_usd for t in tr)),
        "net": float(sum(t.pnl_net_usd for t in tr)),
        "hit": (sum(1 for t in tr if t.pnl_net_usd > 0) / max(len(tr), 1)),
        "wall": time.perf_counter() - t0,
        "_legs": [float(t.pnl_net_usd) for t in tr],
    }
    del bars, art, res, tr
    import gc
    gc.collect()
    return out


def _bootstrap(legs, n_boot=20000):
    """90% CI on EV/leg and on the total, by resampling legs."""
    a = np.asarray(legs, dtype=np.float64)
    n = len(a)
    if n == 0:
        return {"ev": 0.0, "ev_lo": 0.0, "ev_hi": 0.0,
                "total_lo": 0.0, "total_hi": 0.0, "p_gt_zero": 1.0}
    idx = RNG.integers(0, n, size=(n_boot, n))
    samples = a[idx]
    evs = samples.mean(axis=1)
    totals = samples.sum(axis=1)
    return {
        "ev": float(a.mean()),
        "ev_lo": float(np.percentile(evs, 5)),
        "ev_hi": float(np.percentile(evs, 95)),
        "total_lo": float(np.percentile(totals, 5)),
        "total_hi": float(np.percentile(totals, 95)),
        "p_gt_zero": float((evs > 0).mean()),
    }


def _report(label, per_slice, out):
    print()
    print("=" * 100)
    print(f"{label}")
    print("=" * 100)
    print(f"{'slice':<14}{'regime':<7}{'legs':>6}{'gross':>9}{'net':>9}{'hit':>8}")
    for x in per_slice:
        print(f"{x['tag']:<14}{x['regime']:<7}{x['legs']:>6}"
              f"{x['gross']:>9,.2f}{x['net']:>9,.2f}{x['hit']:>8.1%}")
    legs = sum(x["legs"] for x in per_slice)
    gross = sum(x["gross"] for x in per_slice)
    net = sum(x["net"] for x in per_slice)
    n_pos = sum(1 for x in per_slice if x["net"] > 0)
    flat_net = sum(x["net"] for x in per_slice if x["regime"] == "flat")
    all_legs = [v for x in per_slice for v in x["_legs"]]
    boot = _bootstrap(all_legs)
    print(f"{'TOTAL':<14}{'':<7}{legs:>6}{gross:>9,.2f}{net:>9,.2f}")
    print(f"  positive slices: {n_pos}/{len(per_slice)}   "
          f"FLAT slice net: {flat_net:,.2f}")
    print(f"  bootstrap (20k resamples) on EV/leg:")
    print(f"    EV/leg = {boot['ev']:+.4f}   90% CI [{boot['ev_lo']:+.4f}, "
          f"{boot['ev_hi']:+.4f}]   P(EV>0) = {boot['p_gt_zero']:.1%}")
    print(f"    TOTAL  90% CI [{boot['total_lo']:,.2f}, {boot['total_hi']:,.2f}]")
    out[label] = {
        "legs": legs, "gross": gross, "net": net,
        "n_positive_slices": n_pos, "flat_net": flat_net,
        "bootstrap": boot,
        "per_slice": [{k: v for k, v in x.items() if k != "_legs"}
                      for x in per_slice],
    }
    return boot


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    out: dict = {"winner": WINNER}

    print("WINNER CONFIG UNDER TEST:")
    for k, v in WINNER.items():
        print(f"    {k} = {v}")

    fit = [_run_slice(*sl, WINNER) for sl in FIT_SLICES]
    _report("FIT SLICES (used to select the candidate)", fit, out)

    held = [_run_slice(*sl, WINNER) for sl in HELD_SLICES]
    _report("HELD-OUT SLICES (never seen by the screen)", held, out)

    # Combined view.
    allx = fit + held
    print()
    print("=" * 100)
    print("ALL 15 SLICES COMBINED")
    print("=" * 100)
    legs = sum(x["legs"] for x in allx)
    net = sum(x["net"] for x in allx)
    gross = sum(x["gross"] for x in allx)
    n_pos = sum(1 for x in allx if x["net"] > 0)
    flat_net = sum(x["net"] for x in allx if x["regime"] == "flat")
    boot = _bootstrap([v for x in allx for v in x["_legs"]])
    print(f"  legs={legs}  gross={gross:,.2f}  net={net:,.2f}  "
          f"positive slices={n_pos}/15  FLAT net={flat_net:,.2f}")
    print(f"  EV/leg = {boot['ev']:+.4f}   90% CI "
          f"[{boot['ev_lo']:+.4f}, {boot['ev_hi']:+.4f}]   "
          f"P(EV>0) = {boot['p_gt_zero']:.1%}")
    print(f"  TOTAL 90% CI [{boot['total_lo']:,.2f}, {boot['total_hi']:,.2f}]")
    out["combined"] = {
        "legs": legs, "gross": gross, "net": net,
        "n_positive_slices": n_pos, "flat_net": flat_net,
        "bootstrap": boot,
    }

    print()
    print("VERDICT")
    print("-" * 100)
    held_net = sum(x["net"] for x in held)
    held_pos = sum(1 for x in held if x["net"] > 0)
    held_flat = sum(x["net"] for x in held if x["regime"] == "flat")
    lo, hi = boot["ev_lo"], boot["ev_hi"]
    if lo > 0:
        print("  PASS: the 90% CI on EV/leg excludes zero.")
    else:
        print("  FAIL: the 90% CI on EV/leg INCLUDES zero -- the edge is")
        print("        not distinguishable from a coin flip at this sample size.")
    print(f"  held-out net = {held_net:,.2f} ({held_pos}/{len(held)} slices "
          f"positive), held-out FLAT net = {held_flat:,.2f}")
    out["verdict"] = {
        "ci_excludes_zero": bool(lo > 0),
        "held_net": held_net,
        "held_positive_slices": held_pos,
        "held_flat_net": held_flat,
    }

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
