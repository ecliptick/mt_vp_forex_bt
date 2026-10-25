"""A/B the SL geometry change on ONE slice, same cached artifacts.

Compares the OLD canonical (sl_mode='fvg_only', sl_zone_mult=40,
nth_hvn_n=1) against the NEW (sl_mode='hvn_lvn', sl_nth_hvn_n=1,
sl_min_usd=2.50, nth_hvn_n=3) so the two differ ONLY in the knobs
the user asked to promote.

Read-only diagnostic. Writes nothing.
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE, FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache, compute_metrics, load_bars,
)

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
START, END = "2025-01-01", "2025-02-01"

OLD = dict(sl_mode="fvg_only", sl_zone_mult=40.0, sl_min_usd=0.0,
           sl_nth_hvn_n=1, nth_hvn_n=1)
NEW = dict(sl_mode="hvn_lvn", sl_zone_mult=40.0, sl_min_usd=2.50,
           sl_nth_hvn_n=1, nth_hvn_n=3)

# Isolate the two promotions from each other as well.
TP_ONLY = dict(OLD, nth_hvn_n=3)
SL_ONLY = dict(OLD, sl_mode="hvn_lvn", sl_min_usd=2.50, sl_nth_hvn_n=1)


def run(bars, art, label, **over):
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(spot=spot, swing_cap=art["swing_cap"],
                             p95_cap=art["p95_cap"], **over)
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
        record_equity_curve=True,
    )
    m = compute_metrics(res)
    sl_refs = Counter(str(getattr(t, "sl_struct_ref", "?"))
                      for t in res.trades)
    tp_refs = Counter(str(getattr(t, "tp_struct_ref", "?")).split("+")[0]
                      for t in res.trades)
    # MED SL/TP MUST come from the INITIAL geometry. After a TP1
    # partial close the engine moves the stop to breakeven and
    # re-labels the leg with target_price = TP2, so reading the live
    # ``stop_price`` / ``target_price`` reports $0.00 stops and
    # understates the distance. See the metric-naming caveat in
    # AGENTS.md — this is the same trap.
    sls = [float(t.initial_sl_distance_usd) for t in res.trades
           if float(getattr(t, "initial_sl_distance_usd", 0.0)) > 0]
    tps = [float(t.initial_tp_distance_usd) for t in res.trades
           if float(t.initial_tp_distance_usd) > 0]
    rr = [t / s for t, s in zip(tps, sls) if s > 0] if len(sls) == len(tps) else []
    print(f"\n=== {label} ===")
    print(f"  trades      {m.get('n_trades')}")
    print(f"  net         ${m.get('total_pnl_net_usd'):,.2f}")
    print(f"  fees        ${m.get('total_fees_usd', float('nan')):,.2f}")
    print(f"  PF          {m.get('profit_factor')}")
    print(f"  hit         {m.get('hit_rate')}")
    print(f"  maxDD       ${m.get('max_drawdown_usd', float('nan')):,.2f}")
    print(f"  medSL       ${np.median(sls):,.2f}" if sls else "  medSL  n/a")
    print(f"  medTP       ${np.median(tps):,.2f}" if tps else "  medTP  n/a")
    print(f"  medR:R      {np.median(rr):.3f}" if rr else "  medR:R n/a")
    print(f"  minSL       ${min(sls):,.2f}" if sls else "  minSL  n/a")
    print(f"  sl_struct   {dict(sl_refs.most_common(6))}")
    print(f"  tp_struct   {dict(tp_refs.most_common(6))}")
    return m


def main():
    bars = load_bars(DATA, start=START, end=END)
    art = ArtifactCache(DATA, "sl_hvn_ab", START, END).build_or_load(bars)
    print(f"artifacts: {len(art['zones'])} zones")

    run(bars, art, "OLD  fvg_only + sl40 + tp n=1 (prior canonical)", **OLD)
    run(bars, art, "TP ONLY   n=3, SL unchanged", **TP_ONLY)
    run(bars, art, "SL ONLY   hvn_lvn + $2.50 min, TP unchanged", **SL_ONLY)
    run(bars, art, "NEW  hvn_lvn + $2.50 min + tp n=3 (canonical)", **NEW)


if __name__ == "__main__":
    main()