"""Pick a defensible ATR length for the SL, now that ATR replaces zone_mult.

``ATR_LEN = 1200`` is the RECIPE's ATR (used by the bar engine's ATR
filters). Measured on 1s XAUUSD it is a $0.10 median — because 1200
1s bars is only 20 MINUTES of average 1-second true range. An SL of
``sl_atr_mult x ATR`` would need mult ~= 24-138 to express the $2.50-
14 stops the recipe actually wants, which is a meaningless constant.

This measures ATR across candidate lookbacks (seconds) so the length
can be chosen so that ``sl_atr_mult`` is an O(1) number.

Run: python -m runtime.diagnostics.atr_len_scan
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.ict_signals import compute_simple_atr  # noqa: E402
from bt.optimal_config import SL_MIN_USD  # noqa: E402
from runtime.sweeps.cached_sweep import load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")

# 1s cadence => len == seconds.
CANDIDATE_SECS = (1_200, 3_600, 7_200, 14_400, 36_000, 86_400, 172_800)


def main() -> None:
    bars = load_bars(DATA)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    print(f"{len(bars):,} bars\n")

    print(f"{'len':>8} {'= window':>10}  {'p10':>7} {'p50':>7} {'p90':>7} "
          f"{'max':>8}  {'mult@$2.50':>11} {'mult@$5':>9} {'mult@$14.40':>12}")
    print("-" * 92)
    for secs in CANDIDATE_SECS:
        t0 = time.perf_counter()
        atr = compute_simple_atr(high, low, close, length=secs)
        dt = time.perf_counter() - t0
        a = atr[np.isfinite(atr)]
        p10, p50, p90, mx = (np.percentile(a, 10), np.percentile(a, 50),
                             np.percentile(a, 90), a.max())
        print(f"{secs:>8,} {secs/3600:>9.1f}h  {p10:7.2f} {p50:7.2f} "
              f"{p90:7.2f} {mx:8.2f}  {SL_MIN_USD/p50:11.1f} "
              f"{5.0/p50:9.1f} {14.40/p50:12.1f}   ({dt:.1f}s)")
        del atr, a

    print("\nA length where mult ~1.0 lands the stop near $2.50-$5 is the one")
    print("worth promoting: the constant stays human-meaningful across regimes,")
    print("which is the whole point of an ATR stop over a zone-multiple stop.")


if __name__ == "__main__":
    main()