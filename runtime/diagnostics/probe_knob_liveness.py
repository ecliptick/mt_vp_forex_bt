"""Report the LIVE/REDUNDANT status of the NB74 multi-target knob family.

For a completed backtest, reads the diagnostic TP references off every
closed trade and reports which code path actually produced TP1. This is
the empirical answer to "does the knob move anything?" — as opposed to
reading the allowlist and assuming.
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
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache,
    compute_metrics,
)

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
SCOPE = "XAUUSD_S1_2025Q1__20250101_20250201"


def run(bars, art, label, **engine_over):
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"],
        p95_cap=art["p95_cap"],
        **engine_over,
    )
    res, diag = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
        record_equity_curve=True,
    )
    m = compute_metrics(res)
    tp1 = Counter(str(getattr(t, "tp_struct_ref", "?")).split("+")[0]
                  for t in res.trades)
    tp2 = Counter(str(getattr(t, "tp_struct_ref_2", "?")).split("+")[0]
                  for t in res.trades)
    n = max(len(res.trades), 1)

    # Did TP1 and TP2 actually differ? (leg-collapse detector)
    collapsed = sum(
        1 for t in res.trades
        if float(getattr(t, "target_price_2", 0.0)) > 0.0
        and abs(float(t.target_price_2) - float(t.target_price)) < 0.05
    )
    print(f"\n--- {label} {engine_over if engine_over else ''}")
    print(f"    trades={m['n_trades']:>6}  net=${m['total_pnl_net_usd']:>10.2f}"
          f"  gross=${m['total_pnl_gross_usd']:>10.2f}"
          f"  fees=${m['total_fees_usd']:>9.2f}"
          f"  EV=${m['ev_per_trade_usd']:>7.3f}  hit={m['hit_rate']:.3f}")
    print(f"    TP1 ref: " + "  ".join(
        f"{k}={v} ({v/n*100:.1f}%)" for k, v in tp1.most_common(5)))
    print(f"    TP2 ref: " + "  ".join(
        f"{k}={v} ({v/n*100:.1f}%)" for k, v in tp2.most_common(5)))
    print(f"    legs COLLAPSED (TP1==TP2 within $0.05): "
          f"{collapsed}/{n} = {collapsed/n*100:.1f}%")
    return res, m


def main() -> None:
    cache = ArtifactCache(DATA, SCOPE, "2025-01-01", "2025-02-01")
    bars = cache.load_bars()
    art = cache.build_or_load(bars)
    print(f"{len(bars):,} bars, {len(art['zones']):,} zones, "
          f"cache_hit={art['_cache_hit']}")

    run(bars, art, "CANONICAL")

    # Does nth_hvn_n do ANYTHING?
    for n in (1, 2, 3):
        run(bars, art, f"nth_hvn_n={n}", nth_hvn_n=n)

    # Does lvn_skip_usd do ANYTHING? (BTC-scale hardcoded `lo - 1.0`)
    for v in (0.05, 0.30, 5.00, 25.00):
        run(bars, art, f"lvn_skip_usd={v}", lvn_skip_usd=v)

    # Does partial_close_pct move anything?
    for v in (0.25, 0.50, 0.75):
        run(bars, art, f"partial_close_pct={v}", partial_close_pct=v)

    print("\n== done ==")


if __name__ == "__main__":
    main()