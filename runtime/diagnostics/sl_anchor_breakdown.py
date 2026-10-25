"""How much of the book does ``sl_zone_mult`` actually reach?

The user's premise: "zone mult is made redundant by SL atr". Verify
before deleting anything.

With ``sl_mode='hvn_lvn'`` the stop has three possible anchors:

    hvn_n{N}    the Nth HVN shelf on the loss side  (primary)
    zone_edge   the FVG edge +/- sl_zone_mult x zone_width (FALLBACK)
    +min        loosened to the sl_min_usd floor

``sl_zone_mult`` only reaches the FALLBACK. This measures how many
trades actually land there, and how the ATR alternative compares.

Run: python -m runtime.diagnostics.sl_anchor_breakdown
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
    SL_MIN_USD,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
START, END = "2025-01-01", "2025-02-01"


def main() -> None:
    bars = load_bars(DATA, start=START, end=END)
    art = ArtifactCache(DATA, "slanchor", START, END).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    ek = build_engine_kwargs(spot=spot, swing_cap=art["swing_cap"],
                             p95_cap=art["p95_cap"],
                             sl_zone_mult=6.0, conflict_policy="stack")
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    n = len(tr)
    print(f"{n:,} legs\n")

    print("== SL ANCHOR BREAKDOWN (sl_struct_ref) ==")
    refs: dict[str, int] = {}
    for t in tr:
        k = str(getattr(t, "sl_struct_ref", "?"))
        refs[k] = refs.get(k, 0) + 1
    zone_edge = 0
    floored = 0
    for k, v in sorted(refs.items(), key=lambda x: -x[1]):
        print(f"  {k:22s} {v:>8,}  {100.0 * v / n:5.1f}%")
        if k.startswith("zone_edge"):
            zone_edge += v
        if "+min" in k:
            floored += v
    print(f"\n  -> sl_zone_mult REACHES {zone_edge:,} legs "
          f"({100.0 * zone_edge / n:.1f}%)  [zone_edge fallback only]")
    print(f"  -> sl_min_usd floor bound on {floored:,} legs "
          f"({100.0 * floored / n:.1f}%)")

    sl = np.asarray([float(t.initial_sl_distance_usd) for t in tr])
    at_floor = np.isclose(sl, SL_MIN_USD, atol=0.01)
    print(f"\n  median SL ${np.median(sl):.2f}   "
          f"exactly at the ${SL_MIN_USD:.2f} floor: "
          f"{100.0 * float(np.mean(at_floor)):.1f}%")

    # What would an ATR stop give, and how often would the floor eat it?
    print("\n== ATR ALTERNATIVE ==")
    from bt._engine.ict_signals import compute_simple_atr

    atr_all = compute_simple_atr(
        bars["high"].to_numpy(dtype=np.float64),
        bars["low"].to_numpy(dtype=np.float64),
        bars["close"].to_numpy(dtype=np.float64),
        length=3_600,
    )
    # ATR sampled AT each trade's entry bar (the honest comparison).
    entry_bars = []
    times = bars["time"].astype("int64").to_numpy()
    for t in tr[:: max(1, n // 4000)]:
        b = int(np.searchsorted(times, int(t.entry_time_ns) // 1_000_000))
        entry_bars.append(atr_all[min(b, len(atr_all) - 1)])
    atr_at_entry = np.asarray(entry_bars, dtype=float)
    print(f"  ATR(3600) sampled at trade entry bars:")
    print(f"    p10=${np.percentile(atr_at_entry,10):.3f}  "
          f"p50=${np.percentile(atr_at_entry,50):.3f}  "
          f"p90=${np.percentile(atr_at_entry,90):.3f}")
    for mult in (8.0, 16.0, 24.0, 32.0, 48.0):
        stop = mult * atr_at_entry
        pct_floor = 100.0 * float(np.mean(stop < SL_MIN_USD))
        print(f"    mult={mult:5.1f} -> stop p50=${np.median(stop):6.2f}  "
              f"p10=${np.percentile(stop,10):6.2f}  "
              f"p90=${np.percentile(stop,90):7.2f}  |  "
              f"{pct_floor:5.1f}% would be eaten by the ${SL_MIN_USD:.2f} floor")

    print(f"\n  spread of ATR-derived stop widths (p90/p10): "
          f"{np.percentile(24.0 * atr_at_entry, 90) / max(np.percentile(24.0 * atr_at_entry, 10), 1e-9):.1f}x")
    print(f"  current zone-mult stop spread (p90/p10): "
          f"{np.percentile(sl, 90) / max(np.percentile(sl, 10), 1e-9):.1f}x")


if __name__ == "__main__":
    main()