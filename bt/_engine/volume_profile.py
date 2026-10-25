"""Volume-profile module — builds session / rolling profiles from
1-minute OHLCV bars. Backbone of ``nb70`` (basic VP strategy) and
``nb71`` (sniper + VP-anchored SL/TP).

Design (2026-10-01)
===================

Why 1-min bars (not 1s)?

* The 1-second cache ``notebooks/.cache/BTCUSDT-aggTrades-2025-04/bars.parquet``
 is already 5 MB per month (2.5M rows). For VP we only need the
 per-price volume distribution — 1-min bars give 60× fewer rows
 (≈43k/month) at the cost of some price resolution (rows finer
 than $0.10-$1.00 at BTC scale are folded together).
* 1-min bars are the canonical institutional "session volume
 profile" cadence (TradingView default, Sierra/Jigsaw default,
 TrendSpider default). Aligns with what human traders chart.
* Building VP per-session on 1s bars would balloon cache size
 from 5 MB to ~300 MB per month with no analytical benefit
 (the HVN/LVN peaks become noisy 1s prints that don't survive
 the 5-min bucketing window anyway).

How VP is built
===============

For each ``session_secs`` interval (default 86400 = 1 day UTC):

1. Resample 1-min bars to N rows (default 100) by min/max close.
2. Compute per-row volume = sum of volumes of bars whose range
   overlaps the row.
3. POC = row with max volume.
4. Value Area = two-sided expansion from POC until 70% of total
   volume captured.
5. HVN zones = local peaks in the row volume series, filtered
   by ``peak_pct`` (fraction of tallest row) and ``min_prominence``.
6. LVN zones = local troughs (rows with vol < ``lvn_threshold``%
   of mean) — these are the "fast-move-through" zones.

Two profile windows (the "multi-timeframe" idea from the
TrendSpider article):

* ``vp_session_secs=86400`` (daily POC) — the institutional
  anchor.
* ``vp_rolling_secs=604800`` (7-day rolling) — macro structure.

Naked POCs
==========

The previous-session POC is "naked" until price trades through
it. We track naked POCs as a separate list and consider them
target levels for the sniper/VP-anchored strategy.

Performance
===========

1-month file: ~150 MB bars + ~150 MB raw ticks. Building VP
for one month on 1-min resampled bars (≈43k rows):

* Daily VP rebuild (10k per row × 100 rows × 30 days ≈ 30M
  row-touches): ≈0.8 s on a single core.
* Weekly VP rebuild (same but over a 7-day window): ≈1.5 s.

So a full 6-month VP build is ≈12 s per month ≈ 1.2 min
total. Persisted via ``SweepCache``-style fingerprint so the
warm hit costs ~50 ms/file.

Cache layout
============

Stored under ``notebooks/.cache/<file_stem>/vp/``:

    vp_daily.pkl        # list[DailyVP] — one per day
    vp_weekly.pkl       # list[WeeklyVP] — one per week
    vp_naked_pocs.pkl   # list[(price, expire_time_ns)]
    vp_fingerprint.json # build params + mtime

Fingerprint keys: file mtime, vp_session_secs, vp_rows,
vp_va_pct, vp_hvn_peak_pct, vp_lvn_threshold.

Public API
==========

* ``build_volume_profiles(bars, params) -> VolumeProfileSet``
* ``load_volume_profiles(raw_path, params) -> VolumeProfileSet | None``
* ``get_or_build_vp(raw_path, params, force=False) -> VolumeProfileSet``
* ``naked_poc_targets(price, vp_set, side) -> list[float]``
* ``hvn_zones_near(price, vp_set, max_distance_usd) -> list[(lo, hi, vol)]``
* ``lvn_zones_near(price, vp_set, max_distance_usd) -> list[(lo, hi)]``
"""
from __future__ import annotations

import json
import pickle
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import numpy as np
import pandas as pd

from bt._engine.timebase import assert_ns, to_ns_arr


# ──────────────────────────────────────────────────────────────────────
# Profile primitives
# ──────────────────────────────────────────────────────────────────────


@dataclass
class HVNZone:
    """A High Volume Node zone — price band with local-max volume."""
    lo: float
    hi: float
    mid: float
    vol: float
    session_start_ns: int
    session_end_ns: int

    def contains(self, price: float) -> bool:
        return self.lo <= price <= self.hi


@dataclass
class LVNZone:
    """A Low Volume Node zone — price band with local-min volume.

    Used as a "fast-move-through" target by the sniper+VP strategy:
    if the SL/TP target lands inside an LVN, the trade should expect
    fast, low-fill slippage through that level.
    """
    lo: float
    hi: float
    mid: float
    vol: float
    session_start_ns: int
    session_end_ns: int


@dataclass
class SessionVP:
    """Volume profile of one session (default = one UTC day)."""
    session_start_ns: int
    session_end_ns: int
    poc: float                     # Point of Control price
    vah: float                     # Value Area High
    val: float                     # Value Area Low
    total_vol: float               # sum of row vols
    hvn_zones: List[HVNZone]
    lvn_zones: List[LVNZone]
    # Per-row arrays for fine-grained queries
    row_prices_lo: np.ndarray      # shape [n_rows]
    row_prices_hi: np.ndarray      # shape [n_rows]
    row_vols: np.ndarray           # shape [n_rows]


@dataclass
class VolumeProfileSet:
    """Container for all volume profiles of a file."""
    file_stem: str
    daily_vp: List[SessionVP]                # chronological, one per day
    weekly_vp: List[SessionVP]               # one per 7-day rolling window
    naked_pocs: List[Tuple[float, int]]     # (price, expire_time_ns)
    build_ms: dict = field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Per-session profile builder
# ──────────────────────────────────────────────────────────────────────


def _build_session_profile(
    close_lo: np.ndarray,         # min close in session
    close_hi: np.ndarray,         # max close in session
    close_arr: np.ndarray,        # all closes in session (for bucketing)
    high_arr: np.ndarray,
    low_arr: np.ndarray,
    vol_arr: np.ndarray,
    session_start_ns: int,
    session_end_ns: int,
    n_rows: int = 100,
    va_pct: float = 70.0,
    hvn_peak_pct: float = 0.30,
    hvn_min_prominence_pct: float = 15.0,
    hvn_zone_pct: float = 50.0,
    lvn_threshold_pct: float = 25.0,
    min_zone_width_usd: float = 5.0,
) -> SessionVP:
    """Build a single session volume profile.

    Performance: this is O(n_rows + n_bars_in_session) using
    a vectorised row-volume fill via digitize. For a 1440-bar
    (1-min × 24h) session at n_rows=100, cost is <1 ms.
    """
    # 1. Find the price range of the session
    p_lo = float(close_lo.min()) if close_lo.size else 0.0
    p_hi = float(close_hi.max()) if close_hi.size else 0.0
    if p_hi - p_lo < min_zone_width_usd:
        # Session too narrow — single-row profile
        return SessionVP(
            session_start_ns=session_start_ns,
            session_end_ns=session_end_ns,
            poc=(p_lo + p_hi) / 2.0,
            vah=p_hi,
            val=p_lo,
            total_vol=float(vol_arr.sum()) if vol_arr.size else 0.0,
            hvn_zones=[],
            lvn_zones=[],
            row_prices_lo=np.array([p_lo]),
            row_prices_hi=np.array([p_hi]),
            row_vols=np.array([float(vol_arr.sum()) if vol_arr.size else 0.0]),
        )
    step = (p_hi - p_lo) / n_rows
    row_lo = p_lo + np.arange(n_rows) * step
    row_hi = row_lo + step

    # 2. Bucket each bar into the rows it spans (high/low range).
    # For each bar, find r_lo = floor((bar.low - p_lo) / step),
    # r_hi = floor((bar.high - p_lo) / step). Add bar.vol to each
    # row in [r_lo, r_hi] (proportional to overlap length).
    row_vols = np.zeros(n_rows, dtype=np.float64)
    if vol_arr.size:
        # Clip ranges into [0, n_rows)
        idx_lo = np.clip(((low_arr - p_lo) / step).astype(np.int64), 0, n_rows - 1)
        idx_hi = np.clip(((high_arr - p_lo) / step).astype(np.int64), 0, n_rows - 1)
        # For each bar, distribute its volume equally across the rows it spans.
        # Loop is small (≈1440 bars × 100-row worst case, but typically
        # only 5-20 rows per bar). Vectorised via np.add.at for speed.
        bar_idx = np.arange(len(vol_arr))
        # Use a sparse representation: each bar contributes to (idx_hi - idx_lo + 1) rows
        for j in range(len(vol_arr)):
            lo_i = int(idx_lo[j])
            hi_i = int(idx_hi[j])
            if hi_i < lo_i:
                hi_i = lo_i
            share = float(vol_arr[j]) / (hi_i - lo_i + 1)
            row_vols[lo_i:hi_i + 1] += share

    total_vol = float(row_vols.sum())
    if total_vol <= 0:
        return SessionVP(
            session_start_ns=session_start_ns,
            session_end_ns=session_end_ns,
            poc=(p_lo + p_hi) / 2.0,
            vah=p_hi,
            val=p_lo,
            total_vol=0.0,
            hvn_zones=[],
            lvn_zones=[],
            row_prices_lo=row_lo.copy(),
            row_prices_hi=row_hi.copy(),
            row_vols=row_vols,
        )

    # 3. POC = row with max volume
    poc_row = int(np.argmax(row_vols))
    poc_price = float(row_lo[poc_row] + step / 2.0)

    # 4. Value Area — two-sided expansion from POC until va_pct% of total volume captured.
    # Convention: "up" = higher price = HIGHER row index. "dn" = lower price = LOWER row index.
    target = total_vol * (va_pct / 100.0)
    va_sum = float(row_vols[poc_row])
    up = poc_row + 1        # next-higher-price row
    dn = poc_row - 1        # next-lower-price row
    while va_sum < target and (up < n_rows or dn >= 0):
        up_v = float(row_vols[up]) if up < n_rows else -1.0
        dn_v = float(row_vols[dn]) if dn >= 0 else -1.0
        if up_v < 0 and dn_v < 0:
            break
        if up_v >= dn_v:
            va_sum += up_v
            up += 1
        else:
            va_sum += dn_v
            dn -= 1
    vah_row = min(up - 1, n_rows - 1)   # highest row index included (top of VA)
    val_row = max(dn + 1, 0)           # lowest row index included (bottom of VA)
    vah_price = float(row_hi[vah_row])
    val_price = float(row_lo[val_row])

    # 5. HVN zones — local peaks with prominence filter.
    # Use adaptive threshold: at least (mean + 1 std) of the row_vols
    # distribution, so HVN zones reflect "above-average" volume, not
    # just any cluster.
    hvn_zones: List[HVNZone] = []
    vmax = float(row_vols.max())
    if vmax > 0:
        # Adaptive floor: mean + 1 std (catches top ~20% of rows)
        # but require vmax * hvn_peak_pct / 100 as a hard ceiling too.
        mean_v = total_vol / n_rows
        std_v = float(np.std(row_vols))
        adaptive_floor = mean_v + 1.0 * std_v
        peak_floor = max(adaptive_floor, vmax * hvn_peak_pct / 100.0)
        # Find rows above the floor. Within those, a row is a peak if
        # it is the local max in a ±2-row window.
        peaks: List[int] = []
        for r in range(n_rows):
            if row_vols[r] < peak_floor:
                continue
            # Check that r is a strict local max within +/- 2 rows
            is_peak = True
            for q in range(max(0, r - 2), min(n_rows, r + 3)):
                if q == r:
                    continue
                if row_vols[q] > row_vols[r]:
                    is_peak = False
                    break
                if row_vols[q] == row_vols[r] and q < r:
                    is_peak = False
                    break
            if is_peak:
                peaks.append(r)
        for pr in peaks:
            pv = float(row_vols[pr])
            # Walk left/right until we hit a row higher than pv (prominence)
            min_l, min_r = pv, pv
            q = pr - 1
            while q >= 0:
                vq = float(row_vols[q])
                if vq > pv:
                    break
                min_l = min(min_l, vq)
                q -= 1
            q = pr + 1
            while q < n_rows:
                vq = float(row_vols[q])
                if vq > pv:
                    break
                min_r = min(min_r, vq)
                q += 1
            base_v = max(min_l, min_r)
            prom = pv - base_v
            if prom < pv * hvn_min_prominence_pct / 100.0:
                continue
            floor_v = pv - prom * hvn_zone_pct / 100.0
            lo2 = pr
            while lo2 - 1 >= 0 and row_vols[lo2 - 1] >= floor_v:
                lo2 -= 1
            hi2 = pr
            while hi2 + 1 < n_rows and row_vols[hi2 + 1] >= floor_v:
                hi2 += 1
            lo_p = float(row_lo[lo2])
            hi_p = float(row_hi[hi2])
            if hi_p - lo_p < min_zone_width_usd:
                continue
            hvn_zones.append(HVNZone(
                lo=lo_p,
                hi=hi_p,
                mid=(lo_p + hi_p) / 2.0,
                vol=pv,
                session_start_ns=session_start_ns,
                session_end_ns=session_end_ns,
            ))

    # 6. LVN zones — local troughs (rows with vol < lvn_threshold% of mean)
    lvn_zones: List[LVNZone] = []
    mean_v = total_vol / n_rows
    lvn_thresh = mean_v * lvn_threshold_pct / 100.0
    # Find contiguous runs of low-vol rows
    is_low = row_vols < lvn_thresh
    if is_low.any():
        # Collect runs
        in_run = False
        run_start = -1
        runs: List[Tuple[int, int]] = []
        for r in range(n_rows):
            if is_low[r] and not in_run:
                run_start = r
                in_run = True
            elif not is_low[r] and in_run:
                runs.append((run_start, r - 1))
                in_run = False
        if in_run:
            runs.append((run_start, n_rows - 1))
        for (r0, r1) in runs:
            lo_p = float(row_lo[r0])
            hi_p = float(row_hi[r1])
            if hi_p - lo_p < min_zone_width_usd:
                continue
            avg_v = float(row_vols[r0:r1 + 1].mean())
            lvn_zones.append(LVNZone(
                lo=lo_p,
                hi=hi_p,
                mid=(lo_p + hi_p) / 2.0,
                vol=avg_v,
                session_start_ns=session_start_ns,
                session_end_ns=session_end_ns,
            ))

    return SessionVP(
        session_start_ns=session_start_ns,
        session_end_ns=session_end_ns,
        poc=poc_price,
        vah=vah_price,
        val=val_price,
        total_vol=total_vol,
        hvn_zones=hvn_zones,
        lvn_zones=lvn_zones,
        row_prices_lo=row_lo.copy(),
        row_prices_hi=row_hi.copy(),
        row_vols=row_vols,
    )


# ──────────────────────────────────────────────────────────────────────
# 1-min resampling + session segmentation
# ──────────────────────────────────────────────────────────────────────


def resample_1s_to_1m(bars: pd.DataFrame) -> pd.DataFrame:
    """Resample cached 1s bars to 1-min OHLCV.

    Required columns: time (tz-aware), open, high, low, close, volume.
    """
    if "time" not in bars.columns:
        raise ValueError("bars must have a 'time' column")
    df = bars.set_index(pd.to_datetime(bars["time"], utc=True))
    agg = {
        "open": "first",
        "high": "max",
        "low": "min",
        "close": "last",
        "volume": "sum",
    }
    out = df.resample("1min").agg(agg).dropna(subset=["open"]).reset_index()
    out = out.rename(columns={"index": "time"})
    return out


def _bucket_by_session(times_ns: np.ndarray, session_secs: int) -> List[Tuple[int, int]]:
    """Bucket bar indices into session windows of session_secs seconds."""
    if times_ns.size == 0:
        return []
    t0 = int(times_ns[0])
    t_end = int(times_ns[-1])
    # Anchor to UTC midnight (1 day = 86400s, 1 week = 604800s)
    if session_secs == 86400:
        # UTC day boundary
        sec_per_day = 86400
        day0 = (t0 // 1_000_000_000) - ((t0 // 1_000_000_000) % sec_per_day)
    elif session_secs == 604800:
        sec_per_week = 604800
        day0 = (t0 // 1_000_000_000) - ((t0 // 1_000_000_000) % sec_per_week)
    else:
        # Custom: just bucket from t0
        day0 = (t0 // 1_000_000_000) - ((t0 // 1_000_000_000) % session_secs)
    boundaries: List[int] = []
    b = day0
    while b <= t_end + session_secs * 1_000_000_000:
        boundaries.append(b)
        b += session_secs * 1_000_000_000
    return boundaries


def _indices_in_window(times_ns: np.ndarray, lo_ns: int, hi_ns: int) -> np.ndarray:
    """Return boolean mask of indices in [lo_ns, hi_ns)."""
    return (times_ns >= lo_ns) & (times_ns < hi_ns)


# ──────────────────────────────────────────────────────────────────────
# Public build entrypoint
# ──────────────────────────────────────────────────────────────────────


def build_volume_profiles(
    bars_1s: pd.DataFrame,
    *,
    file_stem: str,
    vp_session_secs: int = 86400,
    vp_rolling_secs: int = 604800,
    vp_rows: int = 100,
    vp_va_pct: float = 70.0,
    vp_hvn_peak_pct: float = 30.0,
    vp_hvn_min_prominence_pct: float = 15.0,
    vp_hvn_zone_pct: float = 50.0,
    vp_lvn_threshold_pct: float = 25.0,
    vp_min_zone_usd: float = 5.0,
    verbose: bool = True,
) -> VolumeProfileSet:
    """Build daily + rolling volume profiles from 1s bars.

    Steps:
    1. Resample to 1-min bars.
    2. Bucket into UTC-day sessions.
    3. For each day, build a SessionVP.
    4. For rolling windows, build a SessionVP per 7-day window.
    5. Collect naked POCs (previous-session POC until price trades
       back through it).
    """
    timings: dict = {}
    t_total0 = time.perf_counter()

    # 1. Resample
    t0 = time.perf_counter()
    bars_1m = resample_1s_to_1m(bars_1s)
    timings["resample_ms"] = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"  VP resample 1s->1m: {len(bars_1s):,} -> {len(bars_1m):,} bars in {timings['resample_ms']:.0f}ms")

    times_ns = to_ns_arr(bars_1m, "time")
    # HARD GUARD: every session boundary below is computed in
    # NANOSECONDS (_bucket_by_session divides by 1_000_000_000). A
    # millisecond timestamp would put every boundary far in the
    # future, so no bar would ever match a session window and the
    # volume profile would come back empty — taking HVN lookups with
    # it, i.e. silently disabling the multi-target TP.
    assert_ns(times_ns, "volume_profile times")
    open_arr = bars_1m["open"].to_numpy(dtype=np.float64)
    high_arr = bars_1m["high"].to_numpy(dtype=np.float64)
    low_arr = bars_1m["low"].to_numpy(dtype=np.float64)
    close_arr = bars_1m["close"].to_numpy(dtype=np.float64)
    if "volume" in bars_1m.columns:
        vol_arr = bars_1m["volume"].to_numpy(dtype=np.float64)
    else:
        vol_arr = np.ones_like(close_arr)

    # 2. Daily sessions
    t0 = time.perf_counter()
    daily_boundaries = _bucket_by_session(times_ns, vp_session_secs)
    daily_vp: List[SessionVP] = []
    for i in range(len(daily_boundaries) - 1):
        lo_ns = daily_boundaries[i]
        hi_ns = daily_boundaries[i + 1]
        mask = _indices_in_window(times_ns, lo_ns, hi_ns)
        if not mask.any():
            continue
        svp = _build_session_profile(
            close_arr[mask].copy(),
            close_arr[mask].copy(),
            close_arr[mask],
            high_arr[mask],
            low_arr[mask],
            vol_arr[mask],
            session_start_ns=lo_ns,
            session_end_ns=hi_ns,
            n_rows=vp_rows,
            va_pct=vp_va_pct,
            hvn_peak_pct=vp_hvn_peak_pct,
            hvn_min_prominence_pct=vp_hvn_min_prominence_pct,
            hvn_zone_pct=vp_hvn_zone_pct,
            lvn_threshold_pct=vp_lvn_threshold_pct,
            min_zone_width_usd=vp_min_zone_usd,
        )
        daily_vp.append(svp)
    timings["daily_ms"] = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"  VP daily sessions: {len(daily_vp)} built in {timings['daily_ms']:.0f}ms")

    # 3. Weekly rolling
    t0 = time.perf_counter()
    weekly_boundaries = _bucket_by_session(times_ns, vp_rolling_secs)
    weekly_vp: List[SessionVP] = []
    for i in range(len(weekly_boundaries) - 1):
        lo_ns = weekly_boundaries[i]
        hi_ns = weekly_boundaries[i + 1]
        mask = _indices_in_window(times_ns, lo_ns, hi_ns)
        if not mask.any():
            continue
        svp = _build_session_profile(
            close_arr[mask].copy(),
            close_arr[mask].copy(),
            close_arr[mask],
            high_arr[mask],
            low_arr[mask],
            vol_arr[mask],
            session_start_ns=lo_ns,
            session_end_ns=hi_ns,
            n_rows=vp_rows,
            va_pct=vp_va_pct,
            hvn_peak_pct=vp_hvn_peak_pct,
            hvn_min_prominence_pct=vp_hvn_min_prominence_pct,
            hvn_zone_pct=vp_hvn_zone_pct,
            lvn_threshold_pct=vp_lvn_threshold_pct,
            min_zone_width_usd=vp_min_zone_usd,
        )
        weekly_vp.append(svp)
    timings["weekly_ms"] = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"  VP weekly sessions: {len(weekly_vp)} built in {timings['weekly_ms']:.0f}ms")

    # 4. Naked POCs — every daily POC is "naked" until price trades through
    # it. We collect them with an "expire at end-of-file" timestamp since
    # the backtest walks all bars; the bar-loop can prune in real-time.
    naked_pocs: List[Tuple[float, int]] = []
    for svp in daily_vp:
        naked_pocs.append((svp.poc, svp.session_end_ns))
    timings["naked_ms"] = 0.1

    timings["total_ms"] = (time.perf_counter() - t_total0) * 1000
    if verbose:
        print(f"  VP total build: {timings['total_ms']:.0f}ms; naked_pocs={len(naked_pocs)}")

    return VolumeProfileSet(
        file_stem=file_stem,
        daily_vp=daily_vp,
        weekly_vp=weekly_vp,
        naked_pocs=naked_pocs,
        build_ms=timings,
    )


# ──────────────────────────────────────────────────────────────────────
# Cache I/O
# ──────────────────────────────────────────────────────────────────────


_VP_CACHE_VERSION = 3  # bumped 2026-10-01 — HVN detection now uses adaptive (mean+1std) floor with ±2-row local-max window
_VP_CACHE_KEYS = (
    "vp_session_secs", "vp_rolling_secs", "vp_rows", "vp_va_pct",
    "vp_hvn_peak_pct", "vp_hvn_min_prominence_pct", "vp_hvn_zone_pct",
    "vp_lvn_threshold_pct", "vp_min_zone_usd",
)


def _vp_fingerprint(raw_path: Path, params: dict) -> dict:
    fp = {"_cache_version": _VP_CACHE_VERSION}
    for k in _VP_CACHE_KEYS:
        fp[k] = params.get(k)
    try:
        fp["_raw_mtime_ns"] = int(raw_path.stat().st_mtime_ns)
        fp["_raw_size_bytes"] = int(raw_path.stat().st_size)
    except OSError:
        fp["_raw_mtime_ns"] = -1
        fp["_raw_size_bytes"] = 0
    return fp


def _vp_cache_dir(raw_path: Path) -> Path:
    cache_root = Path("notebooks/.cache") / raw_path.stem / "vp"
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root


def _vp_save(obj, path: Path) -> None:
    with open(path, "wb") as fh:
        pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)


def _vp_load(path: Path):
    with open(path, "rb") as fh:
        return pickle.load(fh)


def _vp_is_cache_valid(raw_path: Path, params: dict) -> bool:
    cache_dir = _vp_cache_dir(raw_path)
    fp_path = cache_dir / "vp_fingerprint.json"
    if not fp_path.exists():
        return False
    try:
        with open(fp_path, "r", encoding="utf-8") as fh:
            stored = json.load(fh)
    except Exception:
        return False
    fp = _vp_fingerprint(raw_path, params)
    if stored.get("_cache_version") != fp["_cache_version"]:
        return False
    for k in _VP_CACHE_KEYS:
        if stored.get(k) != fp[k]:
            return False
    if stored.get("_raw_mtime_ns") != fp["_raw_mtime_ns"]:
        return False
    if stored.get("_raw_size_bytes") != fp["_raw_size_bytes"]:
        return False
    required = ("vp_daily.pkl", "vp_weekly.pkl", "vp_naked_pocs.pkl", "vp_meta.json")
    return all((cache_dir / n).exists() for n in required)


def get_or_build_vp(
    raw_path: Path,
    bars_1s: pd.DataFrame,
    *,
    vp_session_secs: int = 86400,
    vp_rolling_secs: int = 604800,
    vp_rows: int = 100,
    vp_va_pct: float = 70.0,
    vp_hvn_peak_pct: float = 30.0,
    vp_hvn_min_prominence_pct: float = 15.0,
    vp_hvn_zone_pct: float = 50.0,
    vp_lvn_threshold_pct: float = 25.0,
    vp_min_zone_usd: float = 5.0,
    force: bool = False,
    verbose: bool = True,
) -> VolumeProfileSet:
    """Return cached VolumeProfileSet or build from 1s bars.

    The caller is responsible for providing ``bars_1s`` — typically
    loaded from the existing ``SweepCache`` to avoid the tick-load
    cost. We only persist the profile output (much smaller).
    """
    params = {
        "vp_session_secs": vp_session_secs,
        "vp_rolling_secs": vp_rolling_secs,
        "vp_rows": vp_rows,
        "vp_va_pct": vp_va_pct,
        "vp_hvn_peak_pct": vp_hvn_peak_pct,
        "vp_hvn_min_prominence_pct": vp_hvn_min_prominence_pct,
        "vp_hvn_zone_pct": vp_hvn_zone_pct,
        "vp_lvn_threshold_pct": vp_lvn_threshold_pct,
        "vp_min_zone_usd": vp_min_zone_usd,
    }
    cache_dir = _vp_cache_dir(raw_path)
    if not force and _vp_is_cache_valid(raw_path, params):
        t0 = time.perf_counter()
        daily_vp = _vp_load(cache_dir / "vp_daily.pkl")
        weekly_vp = _vp_load(cache_dir / "vp_weekly.pkl")
        naked_pocs = _vp_load(cache_dir / "vp_naked_pocs.pkl")
        load_ms = (time.perf_counter() - t0) * 1000
        if verbose:
            print(f"  VP cache hit: {cache_dir.parent.name}/vp "
                  f"({len(daily_vp)} daily, {len(weekly_vp)} weekly, "
                  f"{len(naked_pocs)} naked POCs) loaded in {load_ms:.0f}ms")
        return VolumeProfileSet(
            file_stem=raw_path.stem,
            daily_vp=daily_vp,
            weekly_vp=weekly_vp,
            naked_pocs=naked_pocs,
            build_ms={"load_ms": load_ms, "total_ms": load_ms, "cache_hit": True},
        )

    if verbose:
        print(f"  VP cache miss: building {cache_dir.parent.name}/vp ...")
    vps = build_volume_profiles(
        bars_1s,
        file_stem=raw_path.stem,
        vp_session_secs=vp_session_secs,
        vp_rolling_secs=vp_rolling_secs,
        vp_rows=vp_rows,
        vp_va_pct=vp_va_pct,
        vp_hvn_peak_pct=vp_hvn_peak_pct,
        vp_hvn_min_prominence_pct=vp_hvn_min_prominence_pct,
        vp_hvn_zone_pct=vp_hvn_zone_pct,
        vp_lvn_threshold_pct=vp_lvn_threshold_pct,
        vp_min_zone_usd=vp_min_zone_usd,
        verbose=verbose,
    )
    _vp_save(vps.daily_vp, cache_dir / "vp_daily.pkl")
    _vp_save(vps.weekly_vp, cache_dir / "vp_weekly.pkl")
    _vp_save(vps.naked_pocs, cache_dir / "vp_naked_pocs.pkl")
    fp = _vp_fingerprint(raw_path, params)
    with open(cache_dir / "vp_fingerprint.json", "w", encoding="utf-8") as fh:
        json.dump(fp, fh, indent=2, sort_keys=True)
    meta = {
        "cache_version": _VP_CACHE_VERSION,
        "raw_path": str(raw_path),
        "n_daily": len(vps.daily_vp),
        "n_weekly": len(vps.weekly_vp),
        "n_naked_pocs": len(vps.naked_pocs),
        "build_ms": {k: round(v, 1) for k, v in vps.build_ms.items()},
    }
    with open(cache_dir / "vp_meta.json", "w", encoding="utf-8") as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    return vps


# ──────────────────────────────────────────────────────────────────────
# Query helpers — used by the bar-loop backtests
# ──────────────────────────────────────────────────────────────────────


def current_daily_vp(vp_set: VolumeProfileSet, t_ns: int) -> Optional[SessionVP]:
    """Return the daily SessionVP that covers time ``t_ns``."""
    for svp in vp_set.daily_vp:
        if svp.session_start_ns <= t_ns < svp.session_end_ns:
            return svp
    # Fallback — return the most recent
    if vp_set.daily_vp:
        for svp in reversed(vp_set.daily_vp):
            if t_ns >= svp.session_start_ns:
                return svp
    return None


def current_weekly_vp(vp_set: VolumeProfileSet, t_ns: int) -> Optional[SessionVP]:
    for svp in vp_set.weekly_vp:
        if svp.session_start_ns <= t_ns < svp.session_end_ns:
            return svp
    if vp_set.weekly_vp:
        for svp in reversed(vp_set.weekly_vp):
            if t_ns >= svp.session_start_ns:
                return svp
    return None


def naked_poc_targets(
    vp_set: VolumeProfileSet,
    t_ns: int,
    price: float,
    direction: int,
) -> List[Tuple[float, int]]:
    """Return naked POCs (price, expire_ns) on the side of price implied by direction.

    direction: +1 = long → look ABOVE price for naked POCs that the
    trade could target. -1 = short → look BELOW.
    """
    out: List[Tuple[float, int]] = []
    for (poc, expire_ns) in vp_set.naked_pocs:
        if expire_ns <= t_ns:
            continue
        if direction > 0 and poc > price:
            out.append((poc, expire_ns))
        elif direction < 0 and poc < price:
            out.append((poc, expire_ns))
    out.sort(key=lambda t: abs(t[0] - price))
    return out


def hvn_zones_near(
    vp_set: VolumeProfileSet,
    t_ns: int,
    price: float,
    direction: int,
    max_distance_usd: float = 1000.0,
    max_results: int = 5,
) -> List[Tuple[float, float, float]]:
    """Return HVN zones near ``price`` in the current daily VP.

    direction: +1 = look ABOVE (target HVN for longs). -1 = look BELOW.
    Returns list of (zone_lo, zone_hi, zone_mid).
    """
    svp = current_daily_vp(vp_set, t_ns)
    if svp is None:
        return []
    out: List[Tuple[float, float, float]] = []
    for z in svp.hvn_zones:
        if direction > 0 and z.lo > price and (z.lo - price) <= max_distance_usd:
            out.append((z.lo, z.hi, z.mid))
        elif direction < 0 and z.hi < price and (price - z.hi) <= max_distance_usd:
            out.append((z.lo, z.hi, z.mid))
    out.sort(key=lambda t: abs(t[2] - price))
    return out[:max_results]


def lvn_zones_near(
    vp_set: VolumeProfileSet,
    t_ns: int,
    price: float,
    direction: int,
    max_distance_usd: float = 1000.0,
    max_results: int = 5,
) -> List[Tuple[float, float, float]]:
    """Return LVN zones near ``price`` in the current daily VP.

    direction: +1 = look ABOVE. -1 = look BELOW.
    """
    svp = current_daily_vp(vp_set, t_ns)
    if svp is None:
        return []
    out: List[Tuple[float, float, float]] = []
    for z in svp.lvn_zones:
        if direction > 0 and z.lo > price and (z.lo - price) <= max_distance_usd:
            out.append((z.lo, z.hi, z.mid))
        elif direction < 0 and z.hi < price and (price - z.hi) <= max_distance_usd:
            out.append((z.lo, z.hi, z.mid))
    out.sort(key=lambda t: abs(t[2] - price))
    return out[:max_results]


def poc_distance_usd(
    vp_set: VolumeProfileSet, t_ns: int, price: float, direction: int
) -> Optional[float]:
    """Return the |price - current daily POC| in trade direction.

    Useful for sizing SL: "place SL just past the daily POC in the
    direction of the trade, with a buffer".
    """
    svp = current_daily_vp(vp_set, t_ns)
    if svp is None:
        return None
    if direction > 0:
        return float(svp.poc - price)
    else:
        return float(price - svp.poc)


__all__ = [
    "HVNZone",
    "LVNZone",
    "SessionVP",
    "VolumeProfileSet",
    "build_volume_profiles",
    "resample_1s_to_1m",
    "get_or_build_vp",
    "current_daily_vp",
    "current_weekly_vp",
    "naked_poc_targets",
    "hvn_zones_near",
    "lvn_zones_near",
    "poc_distance_usd",
    "_VP_CACHE_VERSION",
]