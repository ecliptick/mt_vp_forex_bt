"""COMBINED screen — the decisive test (2026-10-24).

Three separate experiments each produced a partial win:

* ``sl_max_usd`` sweep: gross rose from $16 to $43 as the ceiling
  went $2.50 -> $5.00, but NET stayed negative on all 6 slices.
* ``conflict_policy='drop'``: the only policy with positive NET on
  2/6 slices INCLUDING the flat slice, and (after the D3 fix) it no
  longer locks to one side.
* volatility-at-entry gate: filtering to trades whose target is
  reachable raises EVgross by 2-4x and reaches positive net at
  tight thresholds.

None of them is enough alone. This screen runs the CARTESIAN
PRODUCT of the promising settings and asks the only question that
matters: does any combination produce positive NET on a MAJORITY of
slices, including the FLAT slice?

THE DECISIVE TEST
-----------------
The beta-proxy diagnosis (docs/nb95_beta_proxy.md) established that
this recipe's P&L is explained by market DIRECTION. A strategy with
genuine timing skill makes money in a CHOPPY market. So the FLAT
slice (2025-05, +0.50% market move) is the decisive test: a config
that is flat-positive there AND not catastrophic elsewhere is a real
candidate. A config that is only positive on BULL slices is the
beta proxy with a new label.

Each cell reports per-slice net, the flat-slice net, and the count
of positive slices. A cell is only "promising" if the flat slice is
non-negative.

Usage
-----
    python -m runtime.diagnostics.combined_screen
"""
from __future__ import annotations

import itertools
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
OUT = Path("runtime/diagnostics/_reports/combined_screen.json")

# (tag, start, end, regime)
SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

# The grid. Kept small on purpose: this is a SCREEN for candidates,
# not an optimisation, and every extra cell is ~2 min of engine time.
POLICIES = ["stack", "drop"]
SL_CAPS = [2.5, 5.0]
# vol_state_min_range_norm is the LIVE-USABLE volatility gate:
# "the current 1m candle's range must be at least N times the
# trailing 60-minute median range". 0.0 = disabled.
VOL_RANGES = [0.0, 1.5, 2.0, 3.0]


def _slices():
    """Yield (tag, regime, bars, art, spot) one slice at a time."""
    for tag, s, e, regime in SLICES:
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        yield tag, regime, bars, art, spot


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows = []

    grid = list(itertools.product(POLICIES, SL_CAPS, VOL_RANGES))
    print(f"{len(grid)} cells x {len(SLICES)} slices = "
          f"{len(grid)*len(SLICES)} engine runs\n", flush=True)

    for policy, sl_cap, vol_rng in grid:
        per_slice = []
        for tag, regime, bars, art, spot in _slices():
            ek = build_engine_kwargs(
                spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
                structure=art["structure"], vol_state=art["vol_state"],
                structure_gate=False,
                conflict_policy=policy,
                sl_max_usd=sl_cap,
                vol_state_min_range_norm=vol_rng,
            )
            t0 = time.perf_counter()
            res, _ = run_vp_sniper_backtest_forex(
                bars=bars, zones=art["zones"], vp_set=art["vp_set"],
                lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
            )
            tr = list(res.trades)
            gross = float(sum(t.pnl_gross_usd for t in tr))
            net = float(sum(t.pnl_net_usd for t in tr))
            per_slice.append({
                "tag": tag, "regime": regime, "legs": len(tr),
                "gross": gross, "net": net,
                "hit": (sum(1 for t in tr if t.pnl_net_usd > 0)
                        / max(len(tr), 1)),
            })
            del bars, art, res, tr
            import gc
            gc.collect()

        total_net = sum(x["net"] for x in per_slice)
        total_gross = sum(x["gross"] for x in per_slice)
        flat_net = sum(x["net"] for x in per_slice if x["regime"] == "flat")
        flat_gross = sum(x["gross"] for x in per_slice if x["regime"] == "flat")
        n_pos = sum(1 for x in per_slice if x["net"] > 0)
        row = {
            "policy": policy, "sl_max_usd": sl_cap,
            "vol_state_min_range_norm": vol_rng,
            "total_gross": total_gross, "total_net": total_net,
            "flat_gross": flat_gross, "flat_net": flat_net,
            "n_positive_slices": n_pos,
            "per_slice": per_slice,
        }
        rows.append(row)
        print(f"  pol={policy:<6} sl=${sl_cap:<5.2f} vol={vol_rng:<4.1f} | "
              f"gross=${total_gross:>8,.1f} net=${total_net:>9,.1f} | "
              f"FLAT gross=${flat_gross:>7,.1f} net=${flat_net:>7,.1f} | "
              f"pos={n_pos}/6", flush=True)

    # ── Rank ──────────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("RANKED BY TOTAL NET")
    print("=" * 100)
    print(f"{'policy':<8} {'sl$':>6} {'vol':>5} {'legs':>7} {'gross':>9} {'net':>10} "
          f"{'FLAT net':>10} {'pos':>5}")
    for r in sorted(rows, key=lambda x: -x["total_net"]):
        legs = sum(x["legs"] for x in r["per_slice"])
        print(f"{r['policy']:<8} {r['sl_max_usd']:>6.2f} "
              f"{r['vol_state_min_range_norm']:>5.1f} {legs:>7} "
              f"{r['total_gross']:>9,.1f} {r['total_net']:>10,.1f} "
              f"{r['flat_net']:>10,.1f} {r['n_positive_slices']:>4}/6")

    print()
    print("=" * 100)
    print("CANDIDATES (flat slice non-negative AND >=3 positive slices)")
    print("=" * 100)
    cands = [r for r in rows
             if r["flat_net"] >= 0 and r["n_positive_slices"] >= 3]
    if not cands:
        print("  NONE -- no combination passes the decisive test.")
        print("  Every config either loses on the flat slice or wins on")
        print("  too few slices to be more than a directional bet.")
    for r in sorted(cands, key=lambda x: -x["total_net"]):
        print(f"  pol={r['policy']:<6} sl=${r['sl_max_usd']:<5.2f} "
              f"vol={r['vol_state_min_range_norm']:<4.1f}  "
              f"net=${r['total_net']:>9,.1f}  FLAT net=${r['flat_net']:>8,.1f}  "
              f"pos={r['n_positive_slices']}/6")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
