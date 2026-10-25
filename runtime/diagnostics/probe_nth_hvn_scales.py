"""Measure the REAL XAUUSD scales for the NB74 multi-target knob surface.

Question this answers, per knob:
  * What is the natural unit on XAUUSD 1s? (zone width, VP row height,
    HVN shelf width, HVN-to-HVN gap)
  * Is the knob LIVE (reaches the engine and changes exits) or
    REDUNDANT (never reached, or always overridden)?

Run:
    python -m runtime.diagnostics.probe_nth_hvn_scales
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from runtime.sweeps.cached_sweep import ArtifactCache  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
START, END = "2025-01-01", "2025-02-01"
SCOPE = "XAUUSD_S1_2025Q1__20250101_20250201"


def pct(xs, qs=(1, 5, 25, 50, 75, 95, 99)):
    a = np.asarray(xs, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        return {}
    return {f"p{q}": float(np.percentile(a, q)) for q in qs}


def main() -> None:
    t0 = time.perf_counter()
    cache = ArtifactCache(DATA, SCOPE, START, END)
    bars = cache.load_bars()
    art = cache.build_or_load(bars)
    zones = art["zones"]
    vp_set = art["vp_set"]
    print(f"loaded {len(bars):,} bars, {len(zones):,} zones, "
          f"{len(vp_set.daily_vp)} daily VPs "
          f"(cache_hit={art['_cache_hit']}) in {time.perf_counter()-t0:.1f}s")

    close = bars["close"].to_numpy(dtype=np.float64)
    print(f"\n== PRICE SCALE ==")
    print(f"  close  min={close.min():.2f} p50={np.median(close):.2f} "
          f"max={close.max():.2f}")

    # ── 1. FVG zone width — the unit for sl_zone_mult / tp_zone_mult ──
    zw = np.array([abs(float(z.zone_high) - float(z.zone_low))
                   for z in zones], dtype=float)
    print(f"\n== 1. FVG ZONE WIDTH (unit of sl_zone_mult=40, "
          f"tp_zone_mult_fallback=200) ==")
    print(f"  {pct(zw)}")
    for m in (6, 40, 200):
        print(f"    sl_zone_mult={m:<4} -> ${m*np.median(zw):8.2f}   "
              f"tp_zone_mult_fallback={m:<4} -> ${m*np.median(zw):8.2f}")

    # ── 2. VP row geometry — the unit of the HVN shelf lookup ──
    svp = vp_set.daily_vp[5] if len(vp_set.daily_vp) > 5 else vp_set.daily_vp[0]
    row_h = svp.row_prices_hi - svp.row_prices_lo
    daily_range = float(svp.vah - svp.val)
    all_rows = []
    for sv in vp_set.daily_vp[:20]:
        rh = sv.row_prices_hi - sv.row_prices_lo
        if rh.size:
            all_rows.append(rh)
    all_rows = np.concatenate([r for r in all_rows if r.size])
    print(f"\n== 2. VP ROW HEIGHT (vp_rows=100 over the daily range) ==")
    print(f"  day range (VAH-VAL) = ${daily_range:.2f}")
    print(f"  row height  {pct(all_rows)}")
    print(f"  n HVN zones in day = {len(svp.hvn_zones)}")

    # ── 3. HVN shelf widths + gaps between consecutive shelves ──
    hw, gaps = [], []
    for sv in vp_set.daily_vp:
        zones_d = sorted(sv.hvn_zones, key=lambda z: z.lo)
        for z in zones_d:
            hw.append(z.hi - z.lo)
        for a, b in zip(zones_d, zones_d[1:]):
            gaps.append(b.lo - a.hi)
    print(f"\n== 3. HVN SHELF WIDTH + SHELF-TO-SHELF GAP ==")
    print(f"  shelf width  {pct(hw)}")
    print(f"  shelf gap    {pct(gaps)}")

    # ── 4. lvn_skip_usd = 0.30 vs the shelf width ──
    # The engine does: hvn_tp = mid - lvn_skip_usd; if hvn_tp >= lo: lo - 1.0
    # The `lo - 1.0` is a HARDCODED ABSOLUTE $1.00 (BTC-scale assumption).
    hw_arr = np.asarray(hw, dtype=float)
    frac = float(np.mean(hw_arr < 0.30)) if hw_arr.size else 0.0
    print(f"\n== 4. LVN_SKIP_USD = 0.30 vs SHELF WIDTH ==")
    print(f"  shelves NARROWER than lvn_skip_usd=0.30: {frac*100:.1f}%")
    print(f"  -> for those shelves the engine takes the `lo - 1.0` branch,")
    print(f"     a HARDCODED $1.00 absolute move (BTC-scale assumption).")

    # ── 5. nth_hvn_n: how many HVNs are inside tp_max_distance_usd=50? ──
    # This is the decisive measurement for nth_hvn_n.
    from bt._engine.volume_profile import hvn_zones_near

    from bt._engine.timebase import to_ns_arr
    times = to_ns_arr(bars)

    # Sample entry points: use real zone edges as entry candidates.
    idx = np.linspace(0, len(zones) - 1, 4000).astype(int)
    samples = [(float(zones[i].zone_low if zones[i].direction > 0
                      else zones[i].zone_high),
                int(times[min(int(getattr(zones[i], "trigger_bar", 0)),
                              len(times) - 1)]),
                int(zones[i].direction)) for i in idx]

    n_beyond = {n: 0 for n in (1, 2, 3, 4, 5)}
    dist_to_nth = {n: [] for n in (1, 2, 3, 4, 5)}
    total = 0
    for entry, t_ns, direction in samples:
        hvns = hvn_zones_near(vp_set, t_ns, entry,
                              1 if direction > 0 else -1,
                              max_distance_usd=50.0, max_results=5)
        total += 1
        for n in (1, 2, 3, 4, 5):
            if len(hvns) >= n:
                n_beyond[n] += 1
                dist_to_nth[n].append(abs(hvns[n - 1][2] - entry))
    print(f"\n== 5. nth_hvn_n COVERAGE (tp_max_distance_usd=50.0) ==")
    print(f"  sampled {total:,} real zone-edge entry points")
    for n in (1, 2, 3, 4, 5):
        c = n_beyond[n] / max(total, 1)
        d = pct(dist_to_nth[n])
        print(f"  nth_hvn_n={n}: hit rate {c*100:6.2f}%   "
              f"dist to shelf mid: p25=${d.get('p25', 0):.2f} "
              f"p50=${d.get('p50', 0):.2f} p75=${d.get('p75', 0):.2f}")

    # ── 6. What does the p95 cap actually allow? ──
    p95 = art["p95_cap"]
    caps = []
    for entry, t_ns, _d in samples[:2000]:
        c = p95.cap_for(t_ns)
        if np.isfinite(c):
            caps.append(float(c))
    if caps:
        print(f"\n== 6. ROLLING p95 CAP (tp_p95_mult=1.0) ==")
        print(f"  {pct(caps)}")
        print(f"  warmup_until_ns = {p95.warmup_until_ns}")

    # ── 7. Swing cap ──
    sw = art["swing_cap"]
    scaps = []
    for entry, t_ns, _d in samples[:2000]:
        c = sw.cap_for(t_ns)
        if np.isfinite(c):
            scaps.append(float(c))
    if scaps:
        print(f"\n== 7. DAILY SWING CAP (SWING_CAP_PCT=0.7) ==")
        print(f"  {pct(scaps)}")

    print("\n== done ==")


if __name__ == "__main__":
    main()