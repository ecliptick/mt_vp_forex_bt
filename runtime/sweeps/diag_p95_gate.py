"""Diagnostic: is the NB91 p95 warmup gate swallowing every bar on 1y?

Loads the canonical parquet, builds ONLY the p95 cap (fast, ~2s), and
compares the cap's internal timestamp space against the engine's
``b_time`` space at several points in the file. No FVG detection.

Run:
  python runtime/sweeps/diag_p95_gate.py
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bt._engine.rolling_p95 import build_rolling_p95_from_bars  # noqa: E402
from bt._engine.timebase import to_ns_arr  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")


def main() -> None:
    print("loading bars ...", flush=True)
    df = pd.read_parquet(DATA)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df.sort_values("time").reset_index(drop=True)
    if "volume" not in df.columns:
        for cand in ("tick_volume", "real_volume", "tickv"):
            if cand in df.columns:
                df["volume"] = df[cand].astype(float)
                break
    print(f"  rows={len(df):,}  range={df['time'].min()} .. {df['time'].max()}")

    times = to_ns_arr(df, "time")
    print(f"  times[0]={times[0]}  times[-1]={times[-1]}")

    print("building p95 cap ...", flush=True)
    cap = build_rolling_p95_from_bars(
        df,
        lookback_secs=604_800,
        inner_secs=3_600,
        p95_q=95.0,
        p95_mult=1.0,
        seed_path="constant",
        seed_constant_usd=0.0,
    )

    print()
    print("=== p95 cap internals ===")
    print(f"  warmup_until_ns   = {cap.warmup_until_ns}")
    print(f"  = {pd.Timestamp(cap.warmup_until_ns, unit='ns', tz='UTC')}")
    print(f"  enabled           = {cap.enabled}")
    print(f"  skip_during_warmup= {cap.skip_trades_during_warmup}")
    print(f"  buffer_t_ns.size  = {cap.buffer_t_ns.size}")
    print(f"  buffer_t_ns[0]   = {cap.buffer_t_ns[0] if cap.buffer_t_ns.size else None}")
    print(f"  p95_ret.size      = {cap.p95_ret.size}")
    fin = cap.p95_ret[np.isfinite(cap.p95_ret)]
    print(f"  p95_ret finite    = {fin.size}")
    if fin.size:
        print(f"    mean={fin.mean():.2f} median={np.median(fin):.2f} "
              f"max={fin.max():.2f}")

    print()
    print("=== is_warmup_complete / cap_for at probe bars ===")
    n = len(df)
    probes = [
        0,
        int(n * 0.0001),
        int(n * 0.001),
        int(n * 0.01),
        int(n * 0.10),
        int(n * 0.50),
        n - 1,
    ]
    header = f"{'bar':>12} {'time':>22} {'warm?':>6} {'cap_for':>12}"
    print(header)
    for i in probes:
        i = max(0, min(i, n - 1))
        t = int(times[i])
        warm = cap.is_warmup_complete(t)
        c = cap.cap_for(t)
        ts = pd.Timestamp(t, unit="ns", tz="UTC")
        print(f"{i:>12,} {str(ts):>22} {str(warm):>6} {c:>12.2f}")

    print()
    frac_warm = float(np.mean([cap.is_warmup_complete(int(t)) for t in times[::997]]))
    print(f"fraction of bars where is_warmup_complete=True: {frac_warm:.6f}")

    print()
    print("=== contiguous-gap check (why the reshape may shear) ===")
    deltas = np.diff(times)
    uniq, counts = np.unique(deltas, return_counts=True)
    order = np.argsort(-counts)
    print("  top-5 bar deltas (ns) -> count  = implied gap:")
    for k in order[:5]:
        d = int(uniq[k])
        print(f"    {d:>15,} -> {counts[k]:>12,}  = {d/1e9:>10.1f}s")
    big = int((deltas > 1_000_000_000).sum())
    print(f"  bars with a >1s gap: {big:,} ({big/len(deltas)*100:.3f}%)")
    frac = int((deltas > 3_600_000_000_000).sum())
    print(f"  bars with a >1h gap: {frac:,} ({frac/len(deltas)*100:.3f}%)")

    # The reshape in _downsample_to_inner assumes every 3600 consecutive
    # rows span exactly one hour. Measure how far off that is.
    n_complete = (len(df) // 3600) * 3600
    spans = times[3599:n_complete:3600] - times[0:n_complete - 3599:3600]
    expected = np.arange(1, len(spans) + 1, dtype=np.int64) * 3600_000_000_000
    drift = spans - expected
    print()
    print(f"  reshape bucket count = {len(spans):,}")
    print(f"  bucket-span drift vs contiguous assumption (hours):")
    print(f"    min={drift.min()/3.6e12:+.2f}h  max={drift.max()/3.6e12:+.2f}h "
          f"mean={drift.mean()/3.6e12:+.2f}h")
    n_wrong = int((drift != 0).sum())
    print(f"    buckets whose span != exactly 1h: {n_wrong:,} "
          f"({n_wrong/len(spans)*100:.2f}%)")


if __name__ == "__main__":
    main()