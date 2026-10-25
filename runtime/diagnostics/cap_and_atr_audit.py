"""Audit: why did median TP move from ~$19 to ~$45?

Traces the exact TP chain on the canonical 1y file:

    raw_tp  = HVN shelf (nth_hvn_n)  OR  0.7 * tp_zone_mult * zone_width
    TP1     = min(raw_tp, swing_cap, p95_cap)      <- swing + p95 applied
    TP2     = min(200 * zone_width, p95_cap)       <- SWING CAP NOT APPLIED

Two things are measured:

1. The realised distribution of EACH cap, using the caps' OWN
   ``cap_for`` / ``p95_ret`` rather than an approximation of them.
2. The raw TP the formula produces, given the real median zone width,
   to see which term dominates before any cap binds.

Run: python -m runtime.diagnostics.cap_and_atr_audit
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.timebase import to_ns_arr  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    ATR_LEN,
    SL_MIN_USD,
    SWING_CAP_PCT,
    TP_MAX_DISTANCE_USD,
    TP_HVN_PROXY_FRAC,
    TP_P95_INNER_SECS,
    TP_P95_LOOKBACK_SECS,
    TP_P95_MULT,
    TP_P95_Q,
    TP_ZONE_MULT_FALLBACK,
)
from runtime.sweeps.cached_sweep import load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")


def pct(a: np.ndarray, label: str, unit: str = "$") -> None:
    a = np.asarray(a, dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        print(f"  {label:38s} (empty)")
        return
    print(f"  {label:38s} n={a.size:>9,}  p10={unit}{np.percentile(a,10):8.2f}  "
          f"p50={unit}{np.percentile(a,50):8.2f}  p90={unit}{np.percentile(a,90):8.2f}"
          f"  max={unit}{a.max():9.2f}")


def main() -> None:
    print(f"loading bars from {DATA}")
    bars = load_bars(DATA)
    print(f"  {len(bars):,} bars\n")

    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)

    # ── 1. Zone width — the multiplier behind every raw TP ───────
    print("== RAW TP DECOMPOSITION ==")
    from bt._engine.ict_signals import detect_fvg  # noqa: F401
    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe

    det = detector_kwargs_from_recipe()
    # Strip the strict-wick floor sentinels the cache normally injects.
    det.pop("_strict_wick_use_floor_array", None)
    det.pop("_strict_wick_price_pct", None)
    print(f"  detector kwargs: min_zone={det['fvg_min_zone_usd']} "
          f"max_age_bars={det['max_zone_age_bars']}")
    # ``detect_fvg`` takes raw OHLC arrays, not the frame.
    zones = detect_fvg(
        open_=bars["open"].to_numpy(dtype=np.float64),
        high=high, low=low, close=close,
        # 2026-10-24: REQUIRED for a correct resample -- see
        # ``bt._engine.ict_signals._resample_bucket_index``. Without it
        # the detector falls back to POSITIONAL bucketing, which is
        # wrong on any feed with gaps (weekends/rollovers).
        times_utc_ns=to_ns_arr(bars),
        **det,
    )
    zw = np.asarray(
        [float(z.zone_high) - float(z.zone_low) for z in zones],
        dtype=float)
    pct(zw, "zone_width (all zones)")

    med_zw = float(np.median(zw))
    print(f"\n  median zone_width = ${med_zw:.3f}")
    print(f"  TP1 proxy (0.7 * tp_zone_mult={TP_ZONE_MULT_FALLBACK:g} * zw)"
          f" = ${0.7 * TP_ZONE_MULT_FALLBACK * med_zw:.2f}")
    print(f"  TP2 raw  (tp_zone_mult={TP_ZONE_MULT_FALLBACK:g} * zw)"
          f"          = ${TP_ZONE_MULT_FALLBACK * med_zw:.2f}")

    # ── 2. Each cap, via its own accessor ────────────────────────
    print("\n== CAP DISTRIBUTIONS (the actual clamp values) ==")
    from bt._engine.rolling_p95 import (
        STAT_P2T_HOURLY,
        STAT_P2T_WINDOW,
        build_rolling_p95_from_bars,
    )
    from bt._engine.swing_cap import build_daily_swing

    sw = build_daily_swing(bars, lookback_secs=86_400,
                           cap_pct=SWING_CAP_PCT)
    print(f"  swing cap: enabled={sw.enabled} buffer={sw.count:,} "
          f"lookback={sw.lookback_secs}s cap_pct={sw.cap_pct}")
    # ``cap_for`` over the cap's own buffer timestamps = exactly what the
    # engine queries, one call per signal.
    probe_t = sw.buffer_t_ns[: sw.count]
    swing_vals = np.asarray(
        [sw.cap_for(int(t)) for t in probe_t[:: max(1, sw.count // 20000)]],
        dtype=float)
    pct(swing_vals, "swing_cap.cap_for  [BINDING CEILING]")

    for stat, tag in ((STAT_P2T_WINDOW, "p2t_window  <- CANONICAL now"),
                      (STAT_P2T_HOURLY, "p2t_hourly <- legacy")):
        cap = build_rolling_p95_from_bars(
            bars, lookback_secs=TP_P95_LOOKBACK_SECS,
            inner_secs=TP_P95_INNER_SECS, p95_q=TP_P95_Q,
            p95_mult=TP_P95_MULT, seed_path="constant",
            seed_constant_usd=0.0, statistic=stat)
        pct(cap.p95_ret, f"p95 cap [{tag}]")

    fin = np.isfinite(swing_vals)
    print(f"\n  swing cap binds below the p2t_window cap on "
          f"{100.0 * float(np.mean(swing_vals[fin] < np.nanmedian(cap.p95_ret))):.1f}% "
          f"of probes  ->  swing is the real ceiling, p95 is vestigial")

    # ── 3. ATR — the input sl_atr_mult would consume ─────────────
    print("\n== ATR (sl_atr_mult's input — CURRENTLY NEVER PASSED) ==")
    from bt._engine.ict_signals import compute_simple_atr

    atr = compute_simple_atr(high, low, close, length=int(ATR_LEN))
    pct(atr[~np.isnan(atr)], f"ATR({ATR_LEN}) on 1s bars", unit="$")
    med_atr = float(np.nanmedian(atr))
    print(f"\n  median ATR({ATR_LEN}) = ${med_atr:.4f}")
    print("  sl_atr_mult required to hit given stop distances:")
    for target in (2.5, 3.0, 5.0, 10.0, 14.4):
        print(f"    want ${target:5.2f} stop -> sl_atr_mult = "
              f"{target / med_atr:8.1f}")
    print(f"\n  sl_min_usd floor = ${SL_MIN_USD:.2f}  "
          f"(applied AFTER the anchor, can only loosen)")
    _tp_r = (f"{TP_MAX_DISTANCE_USD:.2f} (frozen override)"
             if TP_MAX_DISTANCE_USD > 0.0
             else "DERIVED per bar = min(swing_cap, p95_cap)")
    print(f"  HVN shelf search radius = {_tp_r}  (radius only, not a clamp)")
    print(f"  tp_hvn_proxy_frac = {TP_HVN_PROXY_FRAC}  "
          f"(TP1 fraction of the fallback leg when no shelf resolves)")


if __name__ == "__main__":
    main()