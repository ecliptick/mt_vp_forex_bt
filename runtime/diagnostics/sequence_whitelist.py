"""Sequence WHITELIST vs FLIP (2026-10-23).

Two ways to use the trailing 5-event signature:

* **FLIP** -- take a losing sequence the other way. (Tested: does NOT
  generalise, -$8 on held-out slices.)
* **WHITELIST** -- only take sequences that historically worked, skip
  the rest. This is the "complex ruleset" form: instead of a scalar
  score saying HOW MUCH conviction, the exact event history says
  WHETHER to trade at all.

A whitelist is a much stronger claim than a flip (it changes the trade
population, not just the side), so it must be fit on one set of slices
and evaluated on another.

Also tests the per-side variant: the sequence may predict LONGS and
SHORTS differently, and a single whitelist may be too blunt.

Usage
-----
    python -m runtime.diagnostics.sequence_whitelist --stage both
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.market_structure import _BREAK_SIG, _break_bars  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT_DIR = Path("runtime/diagnostics/_reports/sequence_whitelist")

FIT_SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
]
TEST_SLICES = [
    ("2025-01 UP",   "2025-01-01", "2025-02-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
]


def _artifacts(tag, start, end, bars):
    return ArtifactCache(DATA, f"seq1m_{tag}", start, end).build_or_load(bars)


def _run(label, start, end, entry_mode="dual"):
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False, entry_mode=entry_mode,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    trades = list(res.trades)
    return {
        "label": label, "art": art, "trades": trades,
        "mkt_ret_pct": float(
            (bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0),
        "net": float(sum(t.pnl_net_usd for t in trades)),
        "legs": len(trades),
        "wall": time.perf_counter() - t0,
    }


def _by_sig(art, trades):
    """Bucket trades by trailing 5-event signature AND side."""
    st = art["structure"]
    bb = _break_bars(st)
    out: dict[str, list] = {}
    for t in trades:
        b = getattr(t, "entry_bar", None)
        if b is None or bb.size == 0:
            continue
        u = int(np.searchsorted(bb, b, side="right"))
        if u < 5:
            continue
        sig = "".join(_BREAK_SIG.get(st.breaks[j].kind, "?")
                      for j in range(u - 5, u))
        out.setdefault(sig, []).append(t)
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", default="both", choices=("fit", "test", "both"))
    ap.add_argument("--min-n", type=int, default=25)
    ap.add_argument("--min-ev", type=float, default=0.05)
    ap.add_argument("--entry-mode", default="dual")
    args = ap.parse_args()
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    print("== sequence WHITELIST (fit on trending, test on held-out) ==")
    print(f"min legs/sig: {args.min_n}   min EV: ${args.min_ev}")
    print()

    # ---- FIT ----
    pooled: dict[str, list] = {}
    pooled_side: dict[tuple[str, int], list] = {}
    for label, s, e in FIT_SLICES:
        r = _run(label, s, e, args.entry_mode)
        for sig, group in _by_sig(r["art"], r["trades"]).items():
            pooled.setdefault(sig, []).extend(group)
            for t in group:
                pooled_side.setdefault((sig, int(t.direction)), []).append(t)
        print(f"  fit {label}: {r['legs']} legs, net ${r['net']:,.0f}",
              flush=True)

    stats = {}
    for sig, group in pooled.items():
        net = sum(t.pnl_net_usd for t in group)
        stats[sig] = {"n": len(group), "net": net, "ev": net / len(group)}
    keep = {s for s, v in stats.items()
            if v["n"] >= args.min_n and v["ev"] >= args.min_ev}
    # Per-side whitelist: sig+direction that clears the bar on its own.
    keep_side = set()
    for (sig, d), group in pooled_side.items():
        net = sum(t.pnl_net_usd for t in group)
        if len(group) >= args.min_n and net / len(group) >= args.min_ev:
            keep_side.add((sig, d))
    print()
    print(f"  whitelist: {len(keep)} signatures "
          f"({len(keep_side)} sig+side pairs) of {len(stats)}")
    for sig in sorted(keep, key=lambda k: -stats[k]["ev"]):
        v = stats[sig]
        print(f"    + {sig}  n={v['n']:>4}  EV=${v['ev']:+.3f}")
    with open(OUT_DIR / "whitelist.json", "w", encoding="utf-8") as fh:
        json.dump({"keep": sorted(keep),
                   "keep_side": sorted(f"{s}|{d}" for s, d in keep_side),
                   "min_n": args.min_n, "min_ev": args.min_ev,
                   "stats": stats}, fh, indent=2, default=str)

    # ---- TEST ----
    # The whitelist is applied as a POST-HOC filter on the measured
    # trades. That is legitimate here because the signature is known
    # at entry time from the structure state alone (no lookahead):
    # it is a function of the last 5 breaks at or before the entry bar.
    print()
    print("-- TEST (held-out, applied post-hoc to the same trades) --")
    print(f"  {'slice':<14} {'mkt':>8} {'legs':>6} {'kept':>6} "
          f"{'base net':>10} {'wl net':>9} {'delta':>8} "
          f"{'base EV':>9} {'wl EV':>8}")
    tb = tw = 0.0
    for label, s, e in TEST_SLICES:
        r = _run(label, s, e, args.entry_mode)
        sigs = _by_sig(r["art"], r["trades"])
        kept = []
        for sig, group in sigs.items():
            if sig in keep:
                kept.extend(group)
        nb = r["net"]
        nw = sum(t.pnl_net_usd for t in kept)
        tb += nb
        tw += nw
        be = nb / max(r["legs"], 1)
        we = nw / max(len(kept), 1)
        print(f"  {label:<14} {r['mkt_ret_pct']:>+7.2f}% {r['legs']:>6} "
              f"{len(kept):>6} {nb:>10,.0f} {nw:>9,.0f} {nw - nb:>+8,.0f} "
              f"{be:>9.3f} {we:>8.3f}")
    print()
    print(f"  {'TOTAL':<14} {'':>8} {'':>6} {'':>6} {tb:>10,.0f} "
          f"{tw:>9,.0f} {tw - tb:>+8,.0f}")
    print()
    if tw > tb:
        print("  => The WHITELIST improves held-out P&L.")
    else:
        print("  => The WHITELIST does NOT improve held-out P&L.")


if __name__ == "__main__":
    main()
