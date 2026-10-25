"""Verify the DIAGNOSTIC SCALE vs the CANONICAL MT4 scale (2026-10-24).

Every diagnostic in ``runtime/diagnostics/`` calls::

    run_vp_sniper_backtest_forex(..., lots=0.01, contract_size=1.0, ...)

``qty_btc = lots * contract_size`` -> **0.01 oz**.

The CANONICAL MT4 sizing is::

    lots=0.01, contract_size=100.0  ->  **1.00 oz**

P&L is LINEAR in qty (gross = move * qty, fee = avg_px * qty * bps * 2),
so every dollar figure in the diagnostic reports is **100x too small**
relative to the canonical sizing the reports claim to be about.

This script runs ONE slice at both scales and checks the ratio, so the
claim is measured rather than asserted.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import build_engine_kwargs  # noqa: E402
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
S, E = "2026-01-01", "2026-02-01"


def run(bars, art, spot, lots, contract_size):
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, sl_max_usd=2.5,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=lots, contract_size=contract_size, engine_kwargs=ek,
    )
    tr = list(res.trades)
    return {
        "legs": len(tr),
        "gross": float(sum(t.pnl_gross_usd for t in tr)),
        "net": float(sum(t.pnl_net_usd for t in tr)),
    }


def main() -> None:
    bars = load_bars(DATA, S, E)
    art = ArtifactCache(DATA, f"c1m_{S[:7]}", S, E).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    print(f"slice {S}..{E}   zones={len(art['zones']):,}   spot=${spot:,.2f}\n")

    diag = run(bars, art, spot, lots=0.01, contract_size=1.0)
    canon = run(bars, art, spot, lots=0.01, contract_size=100.0)

    print(f"{'scale':34s} {'legs':>6s} {'gross':>12s} {'fees':>12s} {'net':>12s} {'fee/leg':>9s}")
    for label, m in (("DIAGNOSTIC  lots=0.01 cs=1.0  (0.01 oz)", diag),
                     ("CANONICAL   lots=0.01 cs=100  (1.00 oz)", canon)):
        fees = m["gross"] - m["net"]
        print(f"{label:34s} {m['legs']:>6d} {m['gross']:>12.4f} {fees:>12.4f} "
              f"{m['net']:>12.4f} {fees / max(m['legs'], 1):>9.4f}")

    print(f"\nratio gross = {canon['gross'] / diag['gross']:.6f}")
    print(f"ratio net   = {canon['net'] / diag['net']:.6f}")
    print(f"ratio fees  = {(canon['gross'] - canon['net']) / (diag['gross'] - diag['net']):.6f}")
    print("\nExpected: exactly 100.000000 for every ratio (P&L is linear in qty).")
    print("Canonical round-trip fee per leg should be $0.47.")


if __name__ == "__main__":
    sys.exit(main())
