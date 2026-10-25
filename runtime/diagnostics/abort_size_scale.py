"""A/B the position size to find the 100x scale discrepancy.

Hypothesis: the 2026-10-24 SL-ceiling / capture-scaling artifacts were
produced with a 100x-too-small position (``contract_size=1.0`` ->
0.01 oz) instead of the canonical ``contract_size=100.0`` -> 1 oz.
Every gross/net figure in those reports -- and in the AGENTS.md
tables transcribed from them -- would then be 100x too small.

Evidence so far:
  * stored sl_ceiling_sweep.json  (sl_max_usd=2.5, fit):
      legs 12,756, gross 41.53, net -17.51, fees 59.04
    -> implied fee/leg = $0.00463
  * fresh capture_scaling rerun   (cap 0.70, n=3, fit):
      legs 12,756, gross 4,153,  net -1,751, fees 5,904
    -> implied fee/leg = $0.464
  * canonical spec (AGENTS.md, tests/test_no_dead_imports.py):
      $0.47 round trip at 0.01 lots x 100 contract_size = 1 oz

This script runs the SAME slice at contract_size=100.0 and 1.0 and
prints both, so the multiplier is measured rather than inferred.

Usage
-----
    python -m runtime.diagnostics.abort_size_scale
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


def _run(bars, art, spot, contract_size: float) -> dict:
    ek = build_engine_kwargs(
        spot=spot, swing_cap=build_daily_swing(bars, lookback_secs=LOOKBACK, cap_pct=0.70),
        p95_cap=art["p95_cap"], structure=art["structure"],
        vol_state=art["vol_state"], structure_gate=False, nth_hvn_n=3,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=contract_size, engine_kwargs=ek,
    )
    tr = list(res.trades)
    gross = float(sum(t.pnl_gross_usd for t in tr))
    net = float(sum(t.pnl_net_usd for t in tr))
    return {
        "legs": len(tr),
        "gross": gross,
        "net": net,
        "fees": gross - net,
        "fee_per_leg": (gross - net) / len(tr) if tr else 0.0,
        "tp_p50": float(np.median([abs(t.tp_distance_usd) for t in tr])),
        "sl_p50": float(np.median([abs(t.sl_distance_usd) for t in tr])),
    }


def main() -> None:
    bars = load_bars(DATA, S, E)
    art = ArtifactCache(DATA, f"c1m_{S[:7]}", S, E).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    big = _run(bars, art, spot, FOREX_CONTRACT_SIZE)   # 100.0 -> 1 oz
    small = _run(bars, art, spot, 1.0)                  # 1.0 -> 0.01 oz

    print()
    print("=== 2025-05 FLAT, cap_pct=0.70, n_hvn=3 ===")
    print(f"{'metric':<14}{'cs=100 (1 oz)':>18}{'cs=1 (0.01 oz)':>18}{'ratio':>10}")
    for k in ("legs", "gross", "net", "fees", "fee_per_leg", "tp_p50", "sl_p50"):
        b, s = big[k], small[k]
        ratio = (b / s) if s else float("nan")
        print(f"{k:<14}{b:>18,.4f}{s:>18,.4f}{ratio:>10.2f}")

    print()
    print("=== cross-check vs the stored 2026-10-24 artifacts ===")
    print("AGENTS.md '2025-05 FLAT' row (canonical cfg): legs 3,677, "
          "TP p50 $38.84, gross +$18, net +$1")
    print(f"cs=100 : legs {big['legs']:,}, TP p50 ${big['tp_p50']:.2f}, "
          f"gross ${big['gross']:,.2f}, net ${big['net']:,.2f}")
    print(f"cs=1   : legs {small['legs']:,}, TP p50 ${small['tp_p50']:.2f}, "
          f"gross ${small['gross']:,.2f}, net ${small['net']:,.2f}")
    print()
    print("AGENTS.md 'sl_max_usd=2.5 FIT' row: legs 12,756, gross $42, net -$18")
    print("stored sl_ceiling_sweep.json      : legs 12,756, gross 41.53, net -17.51")
    print(f"cs=1 reproduces the stored row    : gross ${small['gross']:,.2f} "
          f"(scaled to fit legs) -> see ratio column")
    print()


if __name__ == "__main__":
    main()
