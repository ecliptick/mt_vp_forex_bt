"""conflict_policy='drop' vs 'stack' with the BOUNDED book (2026-10-24).

The user: "perhaps to get to profit is as simple as using drop? but
previous drop code still gives both longs and shorts in live."

The historical claim in AGENTS.md is that under ``drop`` the book
locked to ONE side (4,335 shorts / 0 longs on 2025-01) because the
book NEVER emptied: ``drop`` rejects any signal opposite to anything
open, so the FIRST opposite signal was rejected forever.

That was measured BEFORE the D3 fix, when the same-direction cap
(``max_same_dir_open``) was structurally unreachable under ``drop``.
The cap is now ``20`` and the check lives in the ``drop`` branch, so
the book can empty and the lock can break.

This diagnostic answers three questions:

1. Does ``drop`` still lock? (long/short split per slice)
2. Does ``drop`` beat ``stack``? (gross and net, per slice)
3. Is the ``drop`` advantage a directional bet? (does it win on the
   slices where the market moved the way the lock pointed?)

The third question is the one that matters. If ``drop`` only wins on
BULL slices by going long and only wins on BEAR slices by going
short, it is the beta proxy again -- just with a different label.

Usage
-----
    python -m runtime.diagnostics.conflict_policy_ab
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
OUT = Path("runtime/diagnostics/_reports/conflict_policy_ab.json")

# (tag, start, end, regime)
SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

POLICIES = ["stack", "drop", "replace"]


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    for policy in POLICIES:
        for tag, s, e, regime in SLICES:
            bars = load_bars(DATA, s, e)
            art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
            spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
            ek = build_engine_kwargs(
                spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
                structure=art["structure"], vol_state=art["vol_state"],
                structure_gate=False,
                conflict_policy=policy,
            )
            t0 = time.perf_counter()
            res, _ = run_vp_sniper_backtest_forex(
                bars=bars, zones=art["zones"], vp_set=art["vp_set"],
                lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
            )
            tr = list(res.trades)
            longs = [t for t in tr if t.direction > 0]
            shorts = [t for t in tr if t.direction < 0]
            mkt = float((bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0)
            m = {
                "policy": policy, "tag": tag, "regime": regime,
                "mkt_ret_pct": mkt,
                "legs": len(tr),
                "n_long": len(longs), "n_short": len(shorts),
                "long_gross": float(sum(t.pnl_gross_usd for t in longs)),
                "short_gross": float(sum(t.pnl_gross_usd for t in shorts)),
                "gross": float(sum(t.pnl_gross_usd for t in tr)),
                "net": float(sum(t.pnl_net_usd for t in tr)),
                "n_dropped_opposite": int(getattr(res, "n_dropped_opposite", 0)),
                "n_replaced_opposite": int(getattr(res, "n_replaced_opposite", 0)),
                "n_stacked_opposite": int(getattr(res, "n_stacked_opposite", 0)),
                "n_same_dir_blocked": int(getattr(res, "n_same_dir_blocked", 0)),
                "wall": time.perf_counter() - t0,
            }
            rows.append(m)
            tot = m["n_long"] + m["n_short"]
            lsh = f"{100*m['n_long']/max(tot,1):>4.0f}/{100*m['n_short']/max(tot,1):>3.0f}"
            print(f"  {policy:<8} {tag:<14} mkt={mkt:>+6.2f}%  legs={tot:>6} "
                  f"L/S={lsh}  gross={m['gross']:>9,.1f}  net={m['net']:>9,.1f}  "
                  f"dropOpp={m['n_dropped_opposite']:>6}  sameDirBlk={m['n_same_dir_blocked']:>6}",
                  flush=True)
            del bars, art, res, tr
            import gc
            gc.collect()
        print()

    # ── Summary ────────────────────────────────────────────────────
    print("=" * 108)
    print("CONFLICT POLICY A/B — 6 slices")
    print("=" * 108)
    print(f"{'policy':<9} {'legs':>7} {'L':>6} {'S':>6} {'L%':>5} {'gross':>10} "
          f"{'net':>10} {'EVgross':>9} {'EVnet':>8}")
    for pol in POLICIES:
        sel = [r for r in rows if r["policy"] == pol]
        L = sum(r["legs"] for r in sel)
        nl = sum(r["n_long"] for r in sel)
        ns = sum(r["n_short"] for r in sel)
        g = sum(r["gross"] for r in sel)
        n = sum(r["net"] for r in sel)
        print(f"{pol:<9} {L:>7} {nl:>6} {ns:>6} {100*nl/max(L,1):>4.0f}% "
              f"{g:>10,.1f} {n:>10,.1f} {g/max(L,1):>9.4f} {n/max(L,1):>8.4f}")

    print()
    print("--- LONG / SHORT gross split (the directional-bet test) ---")
    print(f"{'policy':<9} {'long gross':>12} {'short gross':>12} {'total':>10}")
    for pol in POLICIES:
        sel = [r for r in rows if r["policy"] == pol]
        lg = sum(r["long_gross"] for r in sel)
        sg = sum(r["short_gross"] for r in sel)
        print(f"{pol:<9} {lg:>12,.1f} {sg:>12,.1f} {lg+sg:>10,.1f}")

    print()
    print("--- net by regime (does 'drop' only win where the lock points?) ---")
    print(f"{'policy':<9} " + " ".join(f"{r:>22}" for r in ("bull", "bear", "flat")))
    for pol in POLICIES:
        cells = []
        for reg in ("bull", "bear", "flat"):
            sel = [r for r in rows if r["policy"] == pol and r["regime"] == reg]
            cells.append(f"{sum(x['net'] for x in sel):>10,.1f}")
        print(f"{pol:<9} " + " ".join(f"{c:>22}" for c in cells))

    print()
    print("--- per-slice net by policy ---")
    slices = [(t, r) for t, s, e, r in SLICES]
    print(f"{'policy':<9} " + " ".join(f"{t[:11]:>12}" for t, _ in slices) + "   #pos")
    for pol in POLICIES:
        nets = []
        for t, _ in slices:
            sel = [r for r in rows if r["policy"] == pol and r["tag"] == t]
            nets.append(sum(x["net"] for x in sel))
        print(f"{pol:<9} " + " ".join(f"{n:>12,.1f}" for n in nets)
              + f"   {sum(1 for n in nets if n > 0)}/6")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
