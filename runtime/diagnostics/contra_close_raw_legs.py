"""SANITY — dump the RAW legs behind every overlapping opposite-side pair.

The counterfactual in ``contra_close_actual_vs_simul.py`` uses the
CLOSE of the first exit bar as the simultaneous-close price. If the
bar index or the price lookup is off by anything, the whole counter-
factual is garbage. This script prints the raw ``ClosedTrade`` records
behind every pair so the arithmetic can be checked by hand.

It also reports, for each pair, the recorded EXIT PRICE of the first
closer -- which is what a real simultaneous close would have used,
not the bar close.

Usage:
    python -m runtime.diagnostics.contra_close_raw_legs
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
OUT = Path("runtime/diagnostics/_reports/contra_close_raw_legs.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = ["2026-01", "2025-09", "2025-05", "2024-12", "2025-10"]


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    all_out: dict[str, list] = {}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=False,
            contra_close=False, contra_close_spread_usd=0.0,
        )
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek,
        )

        close = bars["close"].to_numpy(dtype=np.float64)
        high = bars["high"].to_numpy(dtype=np.float64)
        low = bars["low"].to_numpy(dtype=np.float64)

        # keep raw legs
        by_parent: dict[int, list] = {}
        for t in res.trades:
            by_parent.setdefault(int(t.parent_zone_id), []).append(t)

        pos = []
        for pid, recs in by_parent.items():
            r0 = recs[0]
            pos.append({
                "parent": pid, "recs": recs,
                "entry_bar": int(r0.entry_bar),
                "exit_bar": max(int(r.exit_bar) for r in recs),
                "direction": int(r0.direction),
                "entry_price": float(r0.entry_price),
                "qty": float(r0.qty_btc),
            })

        longs = [p for p in pos if p["direction"] > 0]
        shorts = [p for p in pos if p["direction"] < 0]

        out_rows = []
        for L in longs:
            for S in shorts:
                lo = max(L["entry_bar"], S["entry_bar"])
                hi = min(L["exit_bar"], S["exit_bar"])
                if lo > hi:
                    continue
                first = L if L["exit_bar"] <= S["exit_bar"] else S
                fb = first["exit_bar"]
                out_rows.append({
                    "long": {
                        "entry_bar": L["entry_bar"], "exit_bar": L["exit_bar"],
                        "entry_price": L["entry_price"], "qty": L["qty"],
                        "legs": [
                            {"exit_bar": int(r.exit_bar),
                             "exit_price": float(r.exit_price),
                             "reason": r.exit_reason,
                             "qty": float(r.qty_btc),
                             "gross": float(r.pnl_gross_usd),
                             "fee": float(r.fee_usd),
                             "net": float(r.pnl_net_usd),
                             "stop": float(r.stop_price),
                             "target": float(r.target_price)}
                            for r in L["recs"]],
                    },
                    "short": {
                        "entry_bar": S["entry_bar"], "exit_bar": S["exit_bar"],
                        "entry_price": S["entry_price"], "qty": S["qty"],
                        "legs": [
                            {"exit_bar": int(r.exit_bar),
                             "exit_price": float(r.exit_price),
                             "reason": r.exit_reason,
                             "qty": float(r.qty_btc),
                             "gross": float(r.pnl_gross_usd),
                             "fee": float(r.fee_usd),
                             "net": float(r.pnl_net_usd),
                             "stop": float(r.stop_price),
                             "target": float(r.target_price)}
                            for r in S["recs"]],
                    },
                    "first_exit_bar": fb,
                    "bar_close": float(close[fb]),
                    "bar_high": float(high[fb]),
                    "bar_low": float(low[fb]),
                    "first_exit_price": float(first["recs"][-1].exit_price),
                })

        all_out[ym] = out_rows
        print(f"== {ym}: {len(out_rows)} overlapping pairs")
        for r in out_rows:
            L, S = r["long"], r["short"]
            print(f"   L@{L['entry_price']:.2f}(q{L['qty']:.2f}) "
                  f"bar{L['entry_bar']}->{L['exit_bar']}  "
                  f"S@{S['entry_price']:.2f}(q{S['qty']:.2f}) "
                  f"bar{S['entry_bar']}->{S['exit_bar']}  "
                  f"firstExitBar={r['first_exit_bar']} "
                  f"close={r['bar_close']:.2f} "
                  f"[{r['bar_low']:.2f},{r['bar_high']:.2f}]")
            for tag, d in (("L", L), ("S", S)):
                for leg in d["legs"]:
                    print(f"      {tag} leg: exit_bar={leg['exit_bar']} "
                          f"px={leg['exit_price']:.2f} {leg['reason']:<10} "
                          f"q={leg['qty']:.2f} gross={leg['gross']:+8.2f} "
                          f"fee={leg['fee']:6.2f} net={leg['net']:+8.2f} "
                          f"stop={leg['stop']:.2f} target={leg['target']:.2f}")
        print()

        del bars, art
        import gc
        gc.collect()

    OUT.write_text(json.dumps(all_out, indent=2, default=float),
                   encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
