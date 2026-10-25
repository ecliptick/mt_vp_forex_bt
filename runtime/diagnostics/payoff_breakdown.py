"""PAYOFF RATIO vs WIN RATE — where does the edge actually come from? (2026-10-24)

WHY THIS EXISTS
---------------
A single-slice probe of the canonical 1m recipe produced a surprising
split: a **11.6% win rate** against a **7.2:1 payoff ratio**, i.e. a
breakeven win rate of **12.13%**. The strategy is not right often --
it is right *cheaply* and wrong *cheaply*, and the asymmetry is what
carries it.

That matters for every knob decision in this repo, because the repo's
documented mechanism for "a tighter stop is better" is:

    a tight stop is hit quickly -> the slot frees -> a NEW trade enters
    -> another round-trip fee is paid

Payoff geometry and win rate react to that mechanism in OPPOSITE
directions, so a change that moves one without the other is easy to
misread. This diagnostic measures both, per exit reason, on every
slice -- so the next knob sweep can be judged on the right axis.

The exit-reason breakdown is the other half. A leg can close at
``tp``/``tp1``/``tp2`` (a win), ``sl`` (a loss), or ``eod``. A run
whose gross is negative *despite* a positive payoff ratio is a run
whose losers are bigger than its winners -- and that is a GEOMETRY
problem (stop too wide), not a SIGNAL problem.

WHAT IS REPORTED
----------------
* win rate, avg win, avg loss, payoff ratio, breakeven win %
* the same, split by exit reason
* the capture ratio: how far winners actually travelled vs their target
* SL / TP distance percentiles (the geometry)

SCALE
-----
Canonical: ``lots=0.01``, ``contract_size=100`` (1 oz), $0.47 round
trip. P&L is exactly linear in qty (verified by
``runtime/diagnostics/verify_diag_scale.py``), so the SIGNS are
scale-invariant -- but the dollars here are the real ones.

    python -m runtime.diagnostics.payoff_breakdown
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
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
OUT = Path("runtime/diagnostics/_reports/payoff_breakdown.json")

SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]


def _run(bars, art, spot) -> list:
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    return list(res.trades)


def _payoff(trades) -> dict:
    """Payoff geometry for a set of legs."""
    if not trades:
        return {"legs": 0}
    gross = np.array([t.pnl_gross_usd for t in trades], dtype=np.float64)
    wins = gross[gross > 0]
    losses = gross[gross <= 0]
    aw = float(wins.mean()) if len(wins) else 0.0
    al = abs(float(losses.mean())) if len(losses) else 0.0
    payoff = aw / al if al > 0 else float("nan")
    out = {
        "legs": len(trades),
        "gross": float(gross.sum()),
        "net": float(sum(t.pnl_net_usd for t in trades)),
        "fees": float(sum(t.fee_usd for t in trades)),
        "win_rate": 100.0 * len(wins) / len(trades),
        "avg_win": aw,
        "avg_loss": al,
        "payoff": payoff,
        "breakeven_win_pct": (100.0 / (1.0 + payoff)) if payoff == payoff else float("nan"),
    }
    # Capture: how far did winners actually travel vs their target?
    if len(wins):
        w_dist = np.array(
            [abs(t.exit_price - t.entry_price) for t, g in zip(trades, gross) if g > 0],
            dtype=np.float64,
        )
        w_tp = np.array(
            [abs(t.initial_tp_distance_usd) for t, g in zip(trades, gross) if g > 0],
            dtype=np.float64,
        )
        med_d, med_tp = float(np.median(w_dist)), float(np.median(w_tp))
        out["winner_median_move"] = med_d
        out["winner_median_target"] = med_tp
        out["capture_pct"] = 100.0 * med_d / med_tp if med_tp > 0 else float("nan")
    return out


def _pct(a: np.ndarray) -> dict:
    if len(a) == 0:
        return {}
    return {f"p{p}": round(float(np.percentile(a, p)), 2) for p in (10, 25, 50, 75, 90, 95)}


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {"slices": {}}
    all_trades: list = []

    for tag, s, e, regime in SLICES:
        print(f"--- {tag} ---", flush=True)
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        t0 = time.perf_counter()
        tr = _run(bars, art, spot)
        print(f"    legs={len(tr)}  wall={time.perf_counter()-t0:.1f}s", flush=True)

        gross = np.array([t.pnl_gross_usd for t in tr], dtype=np.float64)
        by_reason = {}
        for r in sorted({t.exit_reason for t in tr}):
            sub = [t for t in tr if t.exit_reason == r]
            g = np.array([t.pnl_gross_usd for t in sub], dtype=np.float64)
            by_reason[r] = {
                "legs": len(sub),
                "sum_gross": float(g.sum()),
                "mean_gross": float(g.mean()),
                "min": float(g.min()),
                "max": float(g.max()),
            }

        sl = np.array([abs(t.initial_sl_distance_usd) for t in tr], dtype=np.float64)
        tp = np.array([abs(t.initial_tp_distance_usd) for t in tr], dtype=np.float64)

        report["slices"][tag] = {
            "regime": regime,
            "overall": _payoff(tr),
            "by_exit_reason": by_reason,
            "sl_distance_pct": _pct(sl),
            "tp_distance_pct": _pct(tp),
        }
        all_trades.extend(tr)

        ov = report["slices"][tag]["overall"]
        print(f"    win={ov['win_rate']:.1f}%  payoff={ov['payoff']:.3f}  "
              f"breakeven={ov['breakeven_win_pct']:.2f}%  gross=${ov['gross']:,.2f}  "
              f"net=${ov['net']:,.2f}", flush=True)
        for r, d in by_reason.items():
            print(f"      {r:>4} n={d['legs']:>6} sum=${d['sum_gross']:>11,.2f} "
                  f"mean=${d['mean_gross']:>8.3f}", flush=True)

        del bars, art
        import gc
        gc.collect()

    # ── Aggregate across all slices ───────────────────────────────
    print()
    print("=" * 96)
    print("PAYOFF GEOMETRY — ALL SLICES (canonical scale)")
    print("=" * 96)
    agg = _payoff(all_trades)
    report["aggregate"] = agg
    print(f"  legs={agg['legs']}  gross=${agg['gross']:,.2f}  fees=${agg['fees']:,.2f}  "
          f"net=${agg['net']:,.2f}")
    print(f"  win_rate={agg['win_rate']:.2f}%   avg_win=${agg['avg_win']:.3f}   "
          f"avg_loss=${agg['avg_loss']:.3f}")
    print(f"  PAYOFF RATIO = {agg['payoff']:.3f}   breakeven win% = {agg['breakeven_win_pct']:.2f}%")
    if "capture_pct" in agg:
        print(f"  capture: winners moved ${agg['winner_median_move']:.2f} vs "
              f"target ${agg['winner_median_target']:.2f} ({agg['capture_pct']:.1f}%)")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
