"""Measure the P&L impact of the dual-mode inverted_bar look-ahead.

WHAT THIS DOES
---------------
Runs the SHIPPED engine twice over the same slice:

  A. ``entry_mode="dual"``     -- the canonical recipe
  B. ``entry_mode="immediate"`` -- fires on EVERY trigger zone

In "immediate" mode the engine does NOT apply the
``int(zone.inverted_bar) >= 0`` skip, so arm B contains the trades
that arm A dropped because of a FUTURE inversion.

The two arms are not expected to be equal, and the gap is the size of
the look-ahead's influence on the book.

Run:
    python -m runtime.diagnostics.probe_dual_mode_pnl_impact
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
START = "2025-01-01"
END = "2025-02-01"
OUT = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
       / "dual_mode_pnl_impact.json")


def _run(art, bars, entry_mode: str):
    from bt.optimal_config import (
        FOREX_CONTRACT_SIZE,
        FOREX_LOTS,
        build_engine_kwargs,
    )
    from bt.vp_engine import run_vp_sniper_backtest_forex

    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        entry_mode=entry_mode,
    )
    res, diag = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
        record_equity_curve=False,
    )
    trades = list(res.trades)
    gross = float(sum(t.pnl_gross_usd for t in trades))
    fees = float(sum(t.fee_usd for t in trades))
    return {
        "entry_mode": entry_mode,
        "n_legs": len(trades),
        "gross": round(gross, 4),
        "fees": round(fees, 4),
        "net": round(gross - fees, 4),
        "skipped_dual_inverted": int(getattr(res, "n_skipped_dual_inverted", 0)),
        "skipped_already_fired": int(getattr(res, "n_skipped_already_fired", 0)),
        "n_signals_emitted": int(getattr(res, "n_signals_emitted", 0)),
        "n_orders_placed": int(getattr(res, "n_orders_placed", 0)),
    }


def main() -> None:
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    scope = "XAUUSD_S1_1y__lookahead"
    print(f"[probe] loading bars {START} -> {END} ...", flush=True)
    bars = load_bars(DATA, START, END)
    art = ArtifactCache(DATA, scope, START, END).build_or_load(bars)
    print(f"[probe] bars={len(bars)} zones={len(art['zones'])}", flush=True)

    out = {"scope": scope, "start": START, "end": END, "arms": {}}
    for mode in ("dual", "immediate"):
        t0 = time.perf_counter()
        r = _run(art, bars, mode)
        r["wall_secs"] = round(time.perf_counter() - t0, 1)
        out["arms"][mode] = r
        print(f"[probe] {mode}: {r}", flush=True)

    d = out["arms"]["dual"]
    im = out["arms"]["immediate"]
    out["delta_legs"] = im["n_legs"] - d["n_legs"]
    out["delta_gross"] = round(im["gross"] - d["gross"], 4)
    out["delta_net"] = round(im["net"] - d["net"], 4)
    out["gross_per_leg_dual"] = round(d["gross"] / max(1, d["n_legs"]), 6)
    out["gross_per_leg_immediate"] = round(im["gross"] / max(1, im["n_legs"]), 6)

    print("\n=== dual-mode look-ahead: P&L impact ===")
    for k, v in out.items():
        print(f"  {k}: {v}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[probe] wrote {OUT}")


if __name__ == "__main__":
    main()
