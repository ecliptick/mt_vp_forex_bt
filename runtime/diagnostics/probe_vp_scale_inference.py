"""Infer the correct XAUUSD scale for the VP / HVN knob family.

The canonical XAUUSD run uses BTC-scale VP constants
(``vp_min_zone_usd=5.0``, ``vp_rows=100``) and a BTC-scale TP
lookup radius (``tp_max_distance_usd=50``). On gold this makes:

* every HVN "shelf" >= $5.00 wide, which at the measured $0.274
  VP row height is ~18 rows — a broad band, not a shelf;
* only 1-2 shelves reachable inside the $50 radius, so
  ``nth_hvn_n >= 3`` is structurally unreachable (measured: 0.00%).

This sweeps the VP shape + the TP radius and reports, for each
combination, how many HVN shelves a day actually has and what
fraction of real entries can resolve ``nth_hvn_n`` = 1/2/3.

The goal is a defensible XAUUSD scale, inferred from the measured
price geometry — not a transplanted number.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.timebase import to_ns_arr  # noqa: E402
from bt._engine.volume_profile import (  # noqa: E402
    build_volume_profiles,
    hvn_zones_near,
)
from runtime.sweeps.cached_sweep import load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")


def main() -> None:
    bars = load_bars(DATA, "2025-01-01", "2025-02-01")
    times = to_ns_arr(bars)
    print(f"{len(bars):,} bars\n")

    # VP shapes to test. ``vp_min_zone_usd`` is the lever that turns a
    # "shelf" from a BTC-sized band into a real XAU shelf; the row
    # counts set the resolution.
    shapes = [
        # (vp_rows, vp_min_zone_usd, label)
        (100, 5.00, "CURRENT (BTC)  rows=100 min=$5.00"),
        (100, 1.00, "min=$1.00"),
        (100, 0.50, "min=$0.50"),
        (100, 0.25, "min=$0.25  ~1 row"),
        (200, 0.50, "rows=200 min=$0.50"),
        (200, 0.25, "rows=200 min=$0.25"),
        (400, 0.25, "rows=400 min=$0.25"),
    ]
    radii = (13.0, 20.0, 50.0)

    # Sample real entry points once, reuse for every shape.
    from bt._engine.ict_signals import detect_fvg
    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe

    zones = detect_fvg(
        open_=bars["open"].to_numpy(),
        high=bars["high"].to_numpy(),
        low=bars["low"].to_numpy(),
        close=bars["close"].to_numpy(),
        # 2026-10-24: REQUIRED for a correct resample -- see
        # ``bt._engine.ict_signals._resample_bucket_index``. Without it
        # the detector falls back to POSITIONAL bucketing, which is
        # wrong on any feed with gaps (weekends/rollovers).
        times_utc_ns=times,
        **detector_kwargs_from_recipe(),
    )
    idx = np.linspace(0, len(zones) - 1, 3000).astype(int)
    samples = []
    for i in idx:
        z = zones[i]
        tb = int(getattr(z, "trigger_bar", 0))
        tb = min(max(tb, 0), len(times) - 1)
        entry = float(z.zone_low if z.direction > 0 else z.zone_high)
        samples.append((entry, int(times[tb]), int(z.direction)))
    print(f"sampled {len(samples):,} real entry points\n")

    header = (f"{'vp shape':<32} {'hvns/day':>9} {'shelfW':>8} "
              f"{'gap':>8} {'rowH':>7}")
    for r in radii:
        header += f"  n=1/{r:<4.0f} n=2/{r:<4.0f} n=3/{r:<4.0f}"
    print(header)
    print("-" * len(header))

    for rows, min_usd, label in shapes:
        t0 = time.perf_counter()
        vp = build_volume_profiles(
            bars_1s=bars, file_stem="probe", verbose=False,
            vp_session_secs=86_400, vp_rolling_secs=604_800,
            vp_rows=rows, vp_va_pct=70.0, vp_hvn_peak_pct=30.0,
            vp_hvn_min_prominence_pct=15.0, vp_hvn_zone_pct=50.0,
            vp_lvn_threshold_pct=25.0, vp_min_zone_usd=min_usd,
        )
        widths, gaps, rowh = [], [], []
        for sv in vp.daily_vp:
            if not sv.row_prices_lo.size:
                continue
            rowh.append(float((sv.row_prices_hi - sv.row_prices_lo).mean()))
            zs = sorted(sv.hvn_zones, key=lambda z: z.lo)
            for z in zs:
                widths.append(z.hi - z.lo)
            for a, b in zip(zs, zs[1:]):
                gaps.append(b.lo - a.hi)
        nday = len(vp.daily_vp)
        hvday = np.mean([len(sv.hvn_zones) for sv in vp.daily_vp]) if nday else 0

        # Coverage per (radius, nth).
        cov = {}
        for r in radii:
            hit = {1: 0, 2: 0, 3: 0}
            for entry, t_ns, d in samples:
                hv = hvn_zones_near(vp, t_ns, entry, 1 if d > 0 else -1,
                                    max_distance_usd=r, max_results=3)
                for n in (1, 2, 3):
                    if len(hv) >= n:
                        hit[n] += 1
            for n in (1, 2, 3):
                cov[(n, r)] = hit[n] / len(samples) * 100

        line = (f"{label:<32} {hvday:>9.2f} "
                f"{np.median(widths) if widths else 0:>8.2f} "
                f"{np.median(gaps) if gaps else 0:>8.2f} "
                f"{np.median(rowh) if rowh else 0:>7.3f}")
        for r in radii:
            line += (f"  {cov[(1,r)]:5.1f}% {cov[(2,r)]:5.1f}% {cov[(3,r)]:5.1f}%")
        print(line + f"   [{time.perf_counter()-t0:.1f}s]")

    print("\n== done ==")


if __name__ == "__main__":
    main()