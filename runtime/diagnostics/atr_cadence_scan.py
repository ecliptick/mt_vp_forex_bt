"""Is an ATR-based stop achievable on 1s XAUUSD at all?

``compute_simple_atr`` on 1s bars yields a median TRUE RANGE of
$0.10 (measured). A stop expressed as ``mult x ATR(1s)`` therefore
needs ``mult ~= 24-138`` to reach a tradeable $2.50-14.40 — the
"multiplier" stops being a volatility scaling factor and becomes a
magic constant with no cross-regime meaning.

The fix is cadence, not arithmetic: an ATR computed on DOWNSAMPLED bars
measures the same volatility at a human-meaningful scale. This measures
ATR at 1s / 1m / 5m / 15m / 1h / 4h / 1d and reports the multiplier
needed to express a $2.50 / $5 / $14.40 stop at each cadence.

A cadence is only USABLE if the implied multiplier is O(1)-ish (say
0.5-5.0) — otherwise the ATR is just the zone width wearing a hat.

Run: python -m runtime.diagnostics.atr_cadence_scan
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.ict_signals import compute_simple_atr  # noqa: E402
from bt._engine.timebase import to_ns_arr  # noqa: E402
from runtime.sweeps.cached_sweep import load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")

CADENCES = [
    (1, "1s"), (60, "1m"), (300, "5m"), (900, "15m"),
    (3600, "1h"), (14_400, "4h"), (86_400, "1d"),
]


def downsample(times_ns, h, l, c, secs):
    """Bucket by wall-clock time (never positional — see NB94)."""
    inner_ns = secs * 1_000_000_000
    b = times_ns // inner_ns
    starts = np.flatnonzero(np.concatenate(([True], b[1:] != b[:-1])))
    return (b[starts] * inner_ns,
            np.maximum.reduceat(h, starts),
            np.minimum.reduceat(l, starts),
            c[starts])


def main() -> None:
    bars = load_bars(DATA)
    times_ns = to_ns_arr(bars, "time")
    h = bars["high"].to_numpy(dtype=np.float64)
    l = bars["low"].to_numpy(dtype=np.float64)
    c = bars["close"].to_numpy(dtype=np.float64)
    print(f"{len(bars):,} bars\n")

    print(f"{'cadence':>8}  {'bars':>10}  {'atr p10':>8} {'atr p50':>8} "
          f"{'atr p90':>8}   {'m@$2.50':>8} {'m@$5':>7} {'m@$14.40':>9}  usable?")
    print("-" * 92)
    for secs, tag in CADENCES:
        if secs == 1:
            bt_, bh, bl, bc = times_ns, h, l, c
        else:
            bt_, bh, bl, bc = downsample(times_ns, h, l, c, secs)
        # ATR length ~ 3x the cadence in bars, min 14.
        alen = max(14, min(3 * secs, 500))
        atr = compute_simple_atr(bh, bl, bc, length=alen)
        a = atr[np.isfinite(atr) & (atr > 0)]
        p50 = float(np.median(a))
        m250, m5, m144 = 2.50 / p50, 5.0 / p50, 14.40 / p50
        ok = "YES" if 0.5 <= m5 <= 5.0 else ("borderline" if 0.1 <= m5 <= 20 else "no")
        print(f"{tag:>8}  {len(bt_):>10,}  {np.percentile(a,10):8.3f} "
              f"{p50:8.3f} {np.percentile(a,90):8.3f}   {m250:8.1f} {m5:7.1f} "
              f"{m144:9.1f}  {ok}")
        if secs == 1:
            del bh, bl, bc
        else:
            del bh, bl, bc, bt_
        del atr, a

    print("\nRead: a stop of $5 needs multiplier ~1.0 for the cadence to be a")
    print("real volatility reference. At 1s the multiplier is ~48, i.e. the")
    print("number carries no information across regimes.")


if __name__ == "__main__":
    main()