"""Verify: is ``median_tp_distance_usd`` the TP1 leg or the TP2 leg?

The 1y cell at 20:27 reported ``median_tp_distance_usd = 45.56`` while
the 16:17 cell (same file, same ``sl_zone_mult=6``) reported 7.32.
Two candidate explanations:

  A. The p95 "peak-to-trough" clamp stopped working.
  B. The metric is TP1, TP1 is clamped by the SWING cap (not the p95
     cap), and the swing cap is large over a 24h window on 1y.

This settles it by measuring the engine's own stored field against the
trace, and by computing the TRADE-WEIGHTED (not time-weighted) cap
distribution -- trades cluster in volatile periods, so the trade-weighted
cap median is the one that matters.

Run: python -m runtime.diagnostics.tp_metric_identity
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    SWING_CAP_PCT,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache,
    compute_metrics,
    load_bars,
)

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
START, END = "2025-01-01", "2025-02-01"


def pct(a, label, unit="$"):
    a = np.asarray([v for v in a if v is not None], dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        print(f"  {label:44s} (empty)")
        return
    print(f"  {label:44s} n={a.size:>8,}  p10={unit}{np.percentile(a,10):7.2f}  "
          f"p50={unit}{np.percentile(a,50):7.2f}  p90={unit}{np.percentile(a,90):8.2f}  "
          f"max={unit}{a.max():9.2f}")


def main() -> None:
    bars = load_bars(DATA, start=START, end=END)
    art = ArtifactCache(DATA, "tpmetric", START, END).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    ek = build_engine_kwargs(spot=spot, swing_cap=art["swing_cap"],
                             p95_cap=art["p95_cap"],
                             sl_zone_mult=6.0, conflict_policy="stack")
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    m = compute_metrics(res)
    print(f"{len(tr):,} legs, {res.n_orders_filled:,} filled orders\n")

    print("== WHAT THE SWEEP REPORTS ==")
    print(f"  median_tp_distance_usd (reported) = "
          f"{m.get('median_tp_distance_usd')}")
    print(f"  median_sl_distance_usd (reported) = "
          f"{m.get('median_sl_distance_usd')}")

    print("\n== THE UNDERLYING FIELDS ==")
    pct([t.initial_tp_distance_usd for t in tr], "initial_tp_distance_usd (TP1)")
    pct([t.initial_sl_distance_usd for t in tr], "initial_sl_distance_usd")
    tp2 = [abs(float(t.target_price_2) - float(t.entry_price))
           for t in tr if float(getattr(t, "target_price_2", 0.0) or 0.0) > 0]
    pct(tp2, "TP2 distance (target_price_2)")

    refs: dict[str, int] = {}
    for t in tr:
        k = str(getattr(t, "tp_struct_ref", "?"))
        refs[k] = refs.get(k, 0) + 1
    print("\n  leg tp_struct_ref breakdown:")
    for k, v in sorted(refs.items(), key=lambda x: -x[1]):
        print(f"    {k:36s} {v:>8,}  {100.0*v/max(len(tr),1):5.1f}%")

    print("\n== CAP, TIME-WEIGHTED vs TRADE-WEIGHTED ==")
    sw = art["swing_cap"]
    tw = [float(t.entry_time_ns) for t in tr]
    time_vals, trade_vals = [], []
    for t_ns in tw[:: max(1, len(tw) // 20000)]:
        c = sw.cap_for(int(t_ns))
        if np.isfinite(c):
            trade_vals.append(c)
    probe = sw.buffer_t_ns[: sw.count]
    for t_ns in probe[:: max(1, sw.count // 20000)]:
        c = sw.cap_for(int(t_ns))
        if np.isfinite(c):
            time_vals.append(c)
    pct(time_vals, "swing cap @ uniform TIME samples")
    pct(trade_vals, "swing cap @ actual TRADE entry times")

    p95 = art["p95_cap"]
    p95_trade = [p95.cap_for(int(t.entry_time_ns)) for t in
                 tr[:: max(1, len(tr) // 20000)]]
    pct(p95_trade, "p95 cap @ actual TRADE entry times")
    fin = [(a, b) for a, b in zip(trade_vals, p95_trade)
           if np.isfinite(a) and np.isfinite(b)]
    if fin:
        a = np.asarray([x[0] for x in fin])
        b = np.asarray([x[1] for x in fin])
        print(f"\n  swing < p95 on {100.0*np.mean(a < b):5.1f}% of trade probes")
        print(f"  => the p95 clamp can only bind where p95 < swing, i.e. "
              f"{100.0*np.mean(b < a):5.1f}%")

    print(f"\n  swing cap built with cap_pct={SWING_CAP_PCT}, "
          f"lookback=24h -> cap = {SWING_CAP_PCT} x (24h high - 24h low)")


if __name__ == "__main__":
    main()