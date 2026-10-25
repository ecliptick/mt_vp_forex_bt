"""Independent single-slice verification of the capture_scaling scale.

AGENTS.md's capture-scaling table reports 2025-05 FLAT at
gross +$18 / net +$1 on 3,677 legs. The 2026-10-24 rerun of
``runtime.diagnostics.capture_scaling`` reports the SAME legs and the
SAME TP p50 ($38.84) but gross +$1,800-ish -- i.e. 100x larger.

This script runs ONE slice (2025-05, canonical cap_pct=0.70, n_hvn=3)
and prints the raw per-trade P&L so the scale can be checked without
relying on a second table.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.swing_cap import build_daily_swing  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
S, E = "2025-05-01", "2025-06-01"
LOOKBACK = 86_400


def main() -> None:
    bars = load_bars(DATA, S, E)
    art = ArtifactCache(DATA, f"c1m_{S[:7]}", S, E).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    cap = build_daily_swing(bars, lookback_secs=LOOKBACK, cap_pct=0.70)

    ek = build_engine_kwargs(
        spot=spot, swing_cap=cap, p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, nth_hvn_n=3,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    gross = sum(t.pnl_gross_usd for t in tr)
    net = sum(t.pnl_net_usd for t in tr)
    tp = np.array([abs(t.tp_distance_usd) for t in tr])
    sl = np.array([abs(t.sl_distance_usd) for t in tr])
    wins = [t for t in tr if t.pnl_gross_usd > 0]

    print()
    print("=== 2025-05 FLAT, cap_pct=0.70, n_hvn=3, canonical sizing ===")
    print(f"  lots                 = {FOREX_LOTS}")
    print(f"  contract_size        = {FOREX_CONTRACT_SIZE}")
    print(f"  qty_btc (= 1 oz)     = {FOREX_LOTS * FOREX_CONTRACT_SIZE}")
    print(f"  legs                 = {len(tr)}")
    print(f"  TP p50               = ${float(np.median(tp)):.2f}")
    print(f"  SL p50               = ${float(np.median(sl)):.2f}")
    print(f"  gross                = ${gross:,.2f}")
    print(f"  net                  = ${net:,.2f}")
    print(f"  implied fees         = ${gross - net:,.2f}")
    print(f"  implied fee / leg    = ${(gross - net) / len(tr):.4f}")
    print(f"  winning legs         = {len(wins)} ({len(wins) / len(tr):.1%})")
    if wins:
        w = np.array([t.pnl_gross_usd for t in wins])
        print(f"  mean win (gross)     = ${w.mean():.2f}")
        wn = np.array([t.pnl_net_usd for t in wins])
        print(f"  mean win (net)       = ${wn.mean():.2f}")
    print()
    print("  --- first 5 legs, raw ---")
    for t in tr[:5]:
        print(f"    dir={t.direction:+d} gross=${t.pnl_gross_usd:8.2f} "
              f"net=${t.pnl_net_usd:8.2f} tp=${abs(t.tp_distance_usd):7.2f} "
              f"sl=${abs(t.sl_distance_usd):6.2f}")
    print()


if __name__ == "__main__":
    main()
