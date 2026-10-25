"""HVN / LVN proof harness (2026-10-24).

The question
============

    "Can we get visualised proof that HVN/LVN works in this strategy?
     Do we look at 1s volume or 1 min volume?"

Answered with measurements rather than assertions, in five layers.

L0  CADENCE / SOURCE AUDIT
    Where does the profile's volume come from? Traced through the
    code and measured.

L1  FORWARD-LEAK AUDIT
    The daily profile is built from the WHOLE UTC day and
    ``current_daily_vp`` hands that whole-day profile to the engine
    from 00:01 onward. How much of the profile at time ``t`` was
    built from volume that had not happened yet? And do the HVN
    shelves survive truncation?

L2  PROFILE-LEVEL FORWARD BEHAVIOUR (leak-free)
    Does a volume shelf PREDICT where price goes next, or merely
    DESCRIBE where price already went?
    * Shelf-termination test: when price touches a forward shelf,
      does it STOP there? Compared against matched random bands.
    * LVN fast-move test: distance-normalised speed of the move
      through LVN vs HVN vs neither.

L3  ANCHOR INERTNESS
    Which anchor actually sets the stop and the target, and does
    changing the profile change the resulting PRICE at all?

L4  STRATEGY-LEVEL A/B
    The real engine on the real recipe with the profile swapped:
      canonical    1m volume, full-day (leaky) profile  <- the recipe
      placebo      same shelves/widths/volume, SHUFFLED across
                   prices -> destroys the volume<->price link
      causal_1m    leak-free expanding profile (1m volume)
      canonical_1s 1-SECOND volume profile
      causal_1s    1-second volume, leak-free
      nocap_*      the same with the SL ceiling REMOVED, so the
                   HVN anchor can actually bind

Usage
-----
    python -m runtime.diagnostics.hvn_lvn_proof             # full
    python -m runtime.diagnostics.hvn_lvn_proof --pilot     # 1 slice
    python -m runtime.diagnostics.hvn_lvn_proof --skip-forward

Outputs
-------
    runtime/diagnostics/_reports/hvn_lvn_proof.json
    runtime/diagnostics/_reports/hvn_lvn_proof_*.png
"""
from __future__ import annotations

import argparse
import gc
import json
import sys
import time
from collections import Counter
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

import matplotlib  # noqa: E402
matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402

from bt._engine import volume_profile as VP  # noqa: E402
from bt._engine.timebase import to_ns_arr  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
    build_vp_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache,
    compute_metrics,
    load_bars,
)

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUTDIR = Path("runtime/diagnostics/_reports")
JSON_OUT = OUTDIR / "hvn_lvn_proof.json"

VP_KWARGS = build_vp_kwargs()

SLICES = [
    ("2024-06 FLAT", "2024-06-01", "2024-07-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
    ("2025-01 UP", "2025-01-01", "2025-02-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
    ("2025-09 UP", "2025-09-01", "2025-10-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
]
PILOT_SLICES = [("2025-01 UP", "2025-01-01", "2025-02-01")]

NS = 1_000_000_000

# ──────────────────────────────────────────────────────────────────────
# Profile construction
# ──────────────────────────────────────────────────────────────────────

def _session_frames(bars: pd.DataFrame) -> list[tuple[int, int, pd.DataFrame]]:
    """Split bars into the same UTC-day sessions the engine uses.

    The 1-minute resample drives the session boundaries (exactly as
    ``VP.build_volume_profiles`` does), so a hand-built profile is
    interchangeable with the engine's.
    """
    b1m = VP.resample_1s_to_1m(bars)
    t1m = to_ns_arr(b1m, "time")
    bounds = VP._bucket_by_session(t1m, 86_400)
    out = []
    for i in range(len(bounds) - 1):
        lo, hi = bounds[i], bounds[i + 1]
        m = (t1m >= lo) & (t1m < hi)
        if not m.any():
            continue
        out.append((lo, hi, b1m[m].reset_index(drop=True)))
    return out


def _profile_from(frame: pd.DataFrame, start_ns: int, end_ns: int) -> VP.SessionVP:
    """Build one SessionVP from an OHLCV frame (the engine's own path)."""
    close = frame["close"].to_numpy(dtype=np.float64)
    return VP._build_session_profile(
        close_lo=close,
        close_hi=close,
        close_arr=close,
        high_arr=frame["high"].to_numpy(dtype=np.float64),
        low_arr=frame["low"].to_numpy(dtype=np.float64),
        vol_arr=frame["volume"].to_numpy(dtype=np.float64),
        session_start_ns=int(start_ns),
        session_end_ns=int(end_ns),
        n_rows=VP_KWARGS["vp_rows"],
        va_pct=VP_KWARGS["vp_va_pct"],
        hvn_peak_pct=VP_KWARGS["vp_hvn_peak_pct"],
        hvn_min_prominence_pct=VP_KWARGS["vp_hvn_min_prominence_pct"],
        hvn_zone_pct=VP_KWARGS["vp_hvn_zone_pct"],
        lvn_threshold_pct=VP_KWARGS["vp_lvn_threshold_pct"],
        min_zone_width_usd=VP_KWARGS["vp_min_zone_usd"],
    )


def _profile_from_rows(
    row_lo: np.ndarray, row_hi: np.ndarray, vols: np.ndarray,
    start_ns: int, end_ns: int,
) -> VP.SessionVP:
    """Build a SessionVP directly from price ROWS + per-row volume.

    Used by the placebo: it lets the volume vector be attached to
    arbitrary prices while keeping the engine's own shelf-detection
    code (thresholds, prominence, zone expansion) IDENTICAL.
    """
    n = len(vols)
    if n <= 1:
        return _profile_from(
            pd.DataFrame({
                "close": [float(row_lo[0])], "high": [float(row_hi[0])],
                "low": [float(row_lo[0])], "volume": [float(vols[0])],
            }), start_ns, end_ns)
    step = float(np.median(row_hi - row_lo))
    closes = (row_lo + row_hi) / 2.0
    return VP._build_session_profile(
        close_lo=row_lo,
        close_hi=row_hi,
        close_arr=closes,
        # A synthetic bar that stays strictly inside its own row, so
        # its volume lands in exactly that row.
        high_arr=row_lo + step * 0.999,
        low_arr=row_lo.copy(),
        vol_arr=vols,
        session_start_ns=int(start_ns),
        session_end_ns=int(end_ns),
        n_rows=n,
        va_pct=VP_KWARGS["vp_va_pct"],
        hvn_peak_pct=VP_KWARGS["vp_hvn_peak_pct"],
        hvn_min_prominence_pct=VP_KWARGS["vp_hvn_min_prominence_pct"],
        hvn_zone_pct=VP_KWARGS["vp_hvn_zone_pct"],
        lvn_threshold_pct=VP_KWARGS["vp_lvn_threshold_pct"],
        min_zone_width_usd=VP_KWARGS["vp_min_zone_usd"],
    )


def _vp_set(sessions: list[VP.SessionVP], stem: str) -> VP.VolumeProfileSet:
    """Wrap sessions into the container the engine consumes.

    ``weekly_vp`` is left EMPTY on purpose: an AST audit shows
    ``current_weekly_vp`` has ZERO call sites in the engine, so the
    weekly profile is built and never read.
    """
    return VP.VolumeProfileSet(
        file_stem=stem, daily_vp=sessions, weekly_vp=[],
        naked_pocs=[], build_ms={},
    )


def build_canonical_vp(bars: pd.DataFrame) -> tuple[VP.VolumeProfileSet, list]:
    """The recipe's own profile: 1-minute volume, full UTC day."""
    sessions = [_profile_from(f, lo, hi) for lo, hi, f in _session_frames(bars)]
    return _vp_set(sessions, "canonical"), sessions


def build_1s_vp(bars: pd.DataFrame) -> tuple[VP.VolumeProfileSet, list]:
    """Same session boundaries, profile built from 1-SECOND volume."""
    frames = _session_frames(bars)
    t1s = to_ns_arr(bars, "time")
    sessions = []
    for lo, hi, _ in frames:
        m = (t1s >= lo) & (t1s < hi)
        if not m.any():
            continue
        sessions.append(_profile_from(bars[m].reset_index(drop=True), lo, hi))
    return _vp_set(sessions, "canonical_1s"), sessions


def build_causal_vp(
    bars: pd.DataFrame, *, cadence: str = "1m", checkpoint_secs: int = 1800,
) -> tuple[VP.VolumeProfileSet, list]:
    """LEAK-FREE profile: at each checkpoint the profile only knows
    the volume that had already happened since midnight."""
    if cadence == "1m":
        frames = _session_frames(bars)
    else:
        frames = []
        t1s = to_ns_arr(bars, "time")
        for lo, hi, _ in _session_frames(bars):
            m = (t1s >= lo) & (t1s < hi)
            if m.any():
                frames.append((lo, hi, bars[m].reset_index(drop=True)))

    sessions: list[VP.SessionVP] = []
    for lo, hi, frame in frames:
        t = to_ns_arr(frame, "time")
        if t.size == 0:
            continue
        cps = list(range(lo, hi, checkpoint_secs * NS))
        if not cps or cps[0] > lo:
            cps.insert(0, lo)
        for j, cp in enumerate(cps):
            nxt = cps[j + 1] if j + 1 < len(cps) else hi
            m = t < cp
            if not m.any():
                continue
            sessions.append(_profile_from(
                frame[m].reset_index(drop=True), cp, nxt))
    return _vp_set(sessions, f"causal_{cadence}"), sessions


def build_placebo_vp(
    sessions: list[VP.SessionVP], seed: int = 7,
) -> tuple[VP.VolumeProfileSet, list]:
    """The PLACEBO: same price grid, same total volume, same volume
    distribution, same shelf-detection thresholds -- but the
    volume<->price mapping is destroyed by shuffling ``row_vols``
    across the price rows."""
    rng = np.random.default_rng(seed)
    out: list[VP.SessionVP] = []
    for s in sessions:
        if s.row_vols.size <= 1:
            out.append(s)
            continue
        shuffled = s.row_vols.copy()
        rng.shuffle(shuffled)
        out.append(_profile_from_rows(
            s.row_prices_lo.copy(), s.row_prices_hi.copy(), shuffled,
            s.session_start_ns, s.session_end_ns))
    return _vp_set(out, "placebo"), out


# ──────────────────────────────────────────────────────────────────────
# L0 — cadence / source audit
# ──────────────────────────────────────────────────────────────────────

def cadence_audit(bars: pd.DataFrame) -> dict[str, Any]:
    b1m = VP.resample_1s_to_1m(bars)
    v1s = bars["volume"].to_numpy(dtype=np.float64)
    v1m = b1m["volume"].to_numpy(dtype=np.float64)
    rng1s = bars["high"].to_numpy(dtype=np.float64) - bars["low"].to_numpy(dtype=np.float64)
    rng1m = b1m["high"].to_numpy(dtype=np.float64) - b1m["low"].to_numpy(dtype=np.float64)
    return {
        "source_column": "tickv (renamed 'volume' by load_bars)",
        "profile_cadence": "1 MINUTE (engine resamples 1s->1m internally)",
        "resample_fn": "bt._engine.volume_profile.resample_1s_to_1m",
        "n_1s_bars": int(len(bars)),
        "n_1m_bars": int(len(b1m)),
        "volume_conserved": bool(abs(v1s.sum() - v1m.sum()) < 1e-6),
        "volume_sum": float(v1s.sum()),
        "median_1s_range_usd": float(np.median(rng1s)),
        "median_1m_range_usd": float(np.median(rng1m)),
        "median_1s_range_x60": float(np.median(rng1s) * 60),
        "tickv_vs_range_corr": float(np.corrcoef(v1s, rng1s)[0, 1]),
        "note": ("tickv is a broker tick-volume proxy, not traded volume; "
                 "its correlation with bar range is what makes it usable."),
    }


# ──────────────────────────────────────────────────────────────────────
# L1 — forward-leak audit
# ──────────────────────────────────────────────────────────────────────

def leak_audit(bars: pd.DataFrame) -> dict[str, Any]:
    frames = _session_frames(bars)
    if not frames:
        return {}
    lo, hi, f1m = frames[len(frames) // 2]
    day_lo = pd.Timestamp(int(lo), unit="ns", tz="UTC")
    day_hi = pd.Timestamp(int(hi), unit="ns", tz="UTC")

    t = to_ns_arr(f1m, "time")
    vol = f1m["volume"].to_numpy(dtype=np.float64)
    total = float(vol.sum())
    cut = int(day_lo.value + 12 * 3600 * NS)

    m = t < cut
    trunc = _profile_from(f1m[m].reset_index(drop=True), lo, hi)
    full = _profile_from(f1m, lo, hi)

    def near_match(a: VP.SessionVP, b: VP.SessionVP) -> int:
        n = 0
        for za in a.hvn_zones:
            for zb in b.hvn_zones:
                if abs(za.lo - zb.lo) <= 0.25 and abs(za.hi - zb.hi) <= 0.25:
                    n += 1
                    break
        return n

    return {
        "probe_day": f"{day_lo.date()} .. {day_hi.date()}",
        "day_volume_total": total,
        "volume_from_after_noon": float(vol[t >= cut].sum()),
        "volume_from_after_noon_pct": float(vol[t >= cut].sum() / total * 100) if total else 0.0,
        "cumulative_volume_seen_by_first_bar_pct": float(vol[0] / total * 100) if total else 0.0,
        "full_day_hvn_shelves": len(full.hvn_zones),
        "truncated_noon_hvn_shelves": len(trunc.hvn_zones),
        "near_shared_shelves": near_match(full, trunc),
        "full_day_poc": float(full.poc),
        "truncated_noon_poc": float(trunc.poc),
        "poc_shift_usd": float(full.poc - trunc.poc),
        "verdict_note": ("current_daily_vp() returns the profile of the CONTAINING "
                         "UTC day, built from that day's ENTIRE volume. A bar at "
                         "00:01 therefore sees ~100% of the day's future volume."),
    }


# ──────────────────────────────────────────────────────────────────────
# L2 — leak-free forward behaviour
# ──────────────────────────────────────────────────────────────────────

def forward_behaviour(
    bars: pd.DataFrame, *, checkpoint_secs: int = 3600,
    fwd_secs: int = 3 * 3600, hold_secs: int = 3600,
    n_ctrl: int = 3, seed: int = 11,
) -> dict[str, Any]:
    """Does a LEAK-FREE profile predict where price goes next?

    Shelf-termination test
        For every forward HVN shelf that price touches, look at what
        price does in the ``hold_secs`` AFTER the touch: does it stop
        there (close back below the shelf) or blow straight through?
        Compared against random bands of the SAME WIDTH drawn from the
        SAME price region.

    LVN fast-move test
        For levels price crosses on the way forward, record the
        distance travelled and the time taken, then compare the
        SPEED (dollars per minute) for levels inside an LVN, inside
        an HVN, and inside neither.
    """
    rng = np.random.default_rng(seed)
    frames = _session_frames(bars)

    hvn_stop = hvn_touch = 0
    ctl_stop = ctl_touch = 0
    hvn_excess: list[float] = []
    ctl_excess: list[float] = []
    speed: dict[str, list[float]] = {"lvn": [], "hvn": [], "none": []}
    n_shelves = 0
    n_days = 0

    for lo, hi, f1m in frames:
        t = to_ns_arr(f1m, "time")
        if t.size < 120:
            continue
        n_days += 1
        close = f1m["close"].to_numpy(dtype=np.float64)
        high = f1m["high"].to_numpy(dtype=np.float64)
        low = f1m["low"].to_numpy(dtype=np.float64)
        vol = f1m["volume"].to_numpy(dtype=np.float64)

        for cp in range(lo, hi, checkpoint_secs * NS):
            i0 = int(np.searchsorted(t, cp, side="right")) - 1
            i0 = max(i0, 0)
            if i0 < 60 or i0 >= len(t) - 5:
                continue
            i1 = int(np.searchsorted(t, cp + fwd_secs * NS, side="left"))
            if i1 - i0 < 10:
                continue
            price_now = float(close[i0])

            # ── leak-free profile at this checkpoint ──
            prof = VP._build_session_profile(
                close_lo=close[:i0], close_hi=close[:i0], close_arr=close[:i0],
                high_arr=high[:i0], low_arr=low[:i0], vol_arr=vol[:i0],
                session_start_ns=cp, session_end_ns=cp + fwd_secs * NS,
                n_rows=VP_KWARGS["vp_rows"],
                va_pct=VP_KWARGS["vp_va_pct"],
                hvn_peak_pct=VP_KWARGS["vp_hvn_peak_pct"],
                hvn_min_prominence_pct=VP_KWARGS["vp_hvn_min_prominence_pct"],
                hvn_zone_pct=VP_KWARGS["vp_hvn_zone_pct"],
                lvn_threshold_pct=VP_KWARGS["vp_lvn_threshold_pct"],
                min_zone_width_usd=VP_KWARGS["vp_min_zone_usd"],
            )

            fw_high, fw_low = high[i0:i1], low[i0:i1]
            fw_close = close[i0:i1]
            fw_top = float(fw_high.max())
            fw_bot = float(fw_low.min())
            if fw_top <= price_now or fw_bot >= price_now:
                continue

            def touch_idx(band_lo: float, band_hi: float) -> int:
                """Index of first forward bar whose range touches the band, else -1."""
                hit = (fw_low <= band_hi) & (fw_high >= band_lo)
                idx = np.flatnonzero(hit)
                return int(idx[0]) if idx.size else -1

            def stopped(band_hi: float, ti: int) -> tuple[bool, float]:
                """Did price stop at the band within the hold window?"""
                j1 = min(ti + hold_secs // 60, len(fw_high))
                if j1 <= ti:
                    return False, 0.0
                excess = float(fw_high[ti:j1].max()) - band_hi
                held = bool(fw_close[j1 - 1] <= band_hi)
                return held, excess

            # ── Shelf-termination test ──
            ahead = [z for z in prof.hvn_zones if z.lo > price_now]
            n_shelves += len(ahead)
            if ahead:
                c_lo = min(z.mid for z in ahead)
                c_hi = max(z.mid for z in ahead)
            for z in ahead:
                ti = touch_idx(z.lo, z.hi)
                if ti < 0:
                    continue
                hvn_touch += 1
                held, exc = stopped(z.hi, ti)
                hvn_stop += int(held)
                hvn_excess.append(exc)
                # matched control: same width, same price region
                w = z.hi - z.lo
                for _ in range(n_ctrl):
                    mid = rng.uniform(c_lo, c_hi)
                    cl, ch = mid - w / 2.0, mid + w / 2.0
                    ti2 = touch_idx(cl, ch)
                    if ti2 < 0:
                        continue
                    ctl_touch += 1
                    held2, exc2 = stopped(ch, ti2)
                    ctl_stop += int(held2)
                    ctl_excess.append(exc2)

            # ── LVN fast-move test (distance-normalised) ──
            fwd_end = float(close[i1 - 1])
            if abs(fwd_end - price_now) < 1e-9:
                continue
            up = fwd_end > price_now
            lo_lvl, hi_lvl = (price_now, fwd_end) if up else (fwd_end, price_now)
            span = hi_lvl - lo_lvl
            if span < 0.5:
                continue
            for k in range(1, 11):
                lvl = lo_lvl + span * k / 11.0
                dist = abs(lvl - price_now)
                idx = np.flatnonzero(fw_high >= lvl) if up else np.flatnonzero(fw_low <= lvl)
                if idx.size == 0:
                    continue
                minutes = int(idx[0]) + 1
                if minutes <= 0:
                    continue
                vps = dist / minutes
                if any(z.lo <= lvl <= z.hi for z in prof.lvn_zones):
                    speed["lvn"].append(vps)
                elif any(z.lo <= lvl <= z.hi for z in prof.hvn_zones):
                    speed["hvn"].append(vps)
                else:
                    speed["none"].append(vps)

    def med(x: list[float]) -> float | None:
        return float(np.median(x)) if x else None

    def rate(h: int, n: int) -> float | None:
        return (h / n) if n else None

    return {
        "n_days": n_days,
        "checkpoint_secs": checkpoint_secs,
        "forward_window_secs": fwd_secs,
        "hold_window_secs": hold_secs,
        "hvn_shelves_ahead": n_shelves,
        "hvn_touch_rate": rate(hvn_touch, n_shelves),
        "control_touch_rate": rate(ctl_touch, max(n_shelves * n_ctrl, 1)),
        "hvn_stopped_at_shelf_rate": rate(hvn_stop, hvn_touch),
        "control_stopped_at_band_rate": rate(ctl_stop, ctl_touch),
        "stop_lift_vs_control": (
            (rate(hvn_stop, hvn_touch) - rate(ctl_stop, ctl_touch))
            if hvn_touch and ctl_touch else None),
        "hvn_median_excess_usd": med(hvn_excess),
        "control_median_excess_usd": med(ctl_excess),
        "median_speed_lvn_usd_per_min": med(speed["lvn"]),
        "median_speed_hvn_usd_per_min": med(speed["hvn"]),
        "median_speed_neither_usd_per_min": med(speed["none"]),
        "n_speed_lvn": len(speed["lvn"]),
        "n_speed_hvn": len(speed["hvn"]),
        "n_speed_neither": len(speed["none"]),
        "lvn_speed_ratio_vs_neither": (
            (med(speed["lvn"]) / med(speed["none"]))
            if med(speed["lvn"]) and med(speed["none"]) else None),
    }


# ──────────────────────────────────────────────────────────────────────
# L3/L4 — strategy arms
# ──────────────────────────────────────────────────────────────────────

def _arm_metrics(res: Any) -> dict[str, Any]:
    m = compute_metrics(res)
    trades = list(res.trades)
    tp1 = [t for t in trades if str(getattr(t, "exit_reason", "")) == "tp1"]
    sl_vals = {round(abs(t.initial_sl_distance_usd), 4) for t in trades}
    n_tp1_hvn = sum(1 for t in trades
                     if str(getattr(t, "tp_struct_ref", "")).startswith("multi_hvn_n"))
    n_proxy = sum(1 for t in trades
                  if "multi_hvn_proxy" in str(getattr(t, "tp_struct_ref", "")))
    n_swingedge = sum(1 for t in trades
                      if "multi_swing_edge" in str(getattr(t, "tp_struct_ref", "")))
    n_sl_hvn = sum(1 for t in trades
                   if str(getattr(t, "sl_struct_ref", "")).startswith("hvn"))
    return {
        "legs": m["n_trades"],
        "long": m["n_long"],
        "short": m["n_short"],
        "gross_usd": round(m["total_pnl_gross_usd"], 2),
        "fees_usd": round(m["total_fees_usd"], 2),
        "net_usd": round(m["total_pnl_net_usd"], 2),
        "ev_per_trade_usd": round(m["ev_per_trade_usd"], 4),
        "hit_rate": round(m["hit_rate"], 4),
        "profit_factor": m["profit_factor"],
        "max_dd_usd": round(m.get("max_drawdown_usd", 0.0) or 0.0, 2),
        "median_sl_usd": m.get("median_sl_distance_usd"),
        "median_tp_usd": m.get("median_tp_distance_usd"),
        "exit_breakdown": dict(Counter(str(t.exit_reason) for t in trades)),
        "sl_anchor": dict(Counter(str(t.sl_struct_ref) for t in trades).most_common(8)),
        "tp_anchor": dict(Counter(str(t.tp_struct_ref) for t in trades).most_common(8)),
        "n_tp1_exits": len(tp1),
        "tp1_exit_rate": round(len(tp1) / len(trades), 4) if trades else 0.0,
        "n_legs_tp1_hvn_shelf": n_tp1_hvn,
        "n_legs_tp1_proxy": n_proxy,
        "n_legs_tp_swing_edge": n_swingedge,
        "n_legs_sl_hvn_anchored": n_sl_hvn,
        "sl_distinct_values": len(sl_vals),
        "sl_min_usd": (min(sl_vals) if sl_vals else None),
        "sl_max_usd": (max(sl_vals) if sl_vals else None),
    }


def run_arm(
    bars: pd.DataFrame, art: dict[str, Any],
    vp_set: VP.VolumeProfileSet, label: str, **ek_over: Any,
) -> dict[str, Any]:
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"],
        p95_cap=art["p95_cap"],
        structure=art.get("structure"),
        vol_state=art.get("vol_state"),
        **ek_over,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=vp_set,
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
    )
    out = _arm_metrics(res)
    out.update({"arm": label, "wall_secs": round(time.perf_counter() - t0, 1)})
    return out


# The SL ceiling removes the HVN anchor entirely (every stop becomes
# exactly $2.50). These variants remove the ceiling so the anchor binds.
NOCAP = {"sl_max_usd": 0.0, "sl_min_usd": 0.0}


def run_slice(tag: str, start: str, end: str) -> dict[str, Any]:
    print(f"\n=== slice {tag} ({start} .. {end}) ===", flush=True)
    bars = load_bars(DATA, start, end)
    art = ArtifactCache(DATA, f"c1m_{start[:7]}", start, end).build_or_load(bars)
    print(f"  zones={art['n_zones']} cache_hit={art.get('_cache_hit')}", flush=True)

    t0 = time.perf_counter()
    canon_vp, canon_sessions = build_canonical_vp(bars)
    v1s_vp, _ = build_1s_vp(bars)
    placebo_vp, _ = build_placebo_vp(canon_sessions)
    causal_vp, _ = build_causal_vp(bars, cadence="1m", checkpoint_secs=1800)
    causal_1s_vp, _ = build_causal_vp(bars, cadence="1s", checkpoint_secs=1800)
    print(f"  5 profiles built in {time.perf_counter() - t0:.1f}s", flush=True)

    arms = []
    plan = [
        ("canonical", canon_vp, {}),
        ("placebo", placebo_vp, {}),
        ("causal_1m", causal_vp, {}),
        ("canonical_1s", v1s_vp, {}),
        ("causal_1s", causal_1s_vp, {}),
        ("nocap_canonical", canon_vp, NOCAP),
        ("nocap_placebo", placebo_vp, NOCAP),
        ("nocap_causal", causal_vp, NOCAP),
    ]
    for label, vp, over in plan:
        t0 = time.perf_counter()
        try:
            r = run_arm(bars, art, vp, label, **over)
            arms.append(r)
            print(f"  {label:16} legs={r['legs']:<5} gross=${r['gross_usd']:>9,.0f} "
                  f"net=${r['net_usd']:>9,.0f} EV=${r['ev_per_trade_usd']:>7.3f} "
                  f"tp1={r['tp1_exit_rate']:>6.1%} slvals={r['sl_distinct_values']:<4} "
                  f"({time.perf_counter() - t0:.0f}s)", flush=True)
        except Exception as exc:
            print(f"  !! {label} failed: {exc}", flush=True)
            import traceback
            traceback.print_exc()
    return {"slice": tag, "start": start, "end": end, "arms": arms}


# ──────────────────────────────────────────────────────────────────────
# Charts
# ──────────────────────────────────────────────────────────────────────

C_ACCENT = "#4c8dff"
C_WARN = "#f5a524"
C_DANGER = "#e5484d"
C_GOOD = "#30a46c"
C_MUTE = "#8b8d98"
C_GRID = "#2a2d35"


def _style(ax, title: str, xlabel: str, ylabel: str) -> None:
    ax.set_title(title, fontsize=10.5, color="#e6e6e6", loc="left", pad=9)
    ax.set_xlabel(xlabel, fontsize=8.5, color=C_MUTE)
    ax.set_ylabel(ylabel, fontsize=8.5, color=C_MUTE)
    ax.tick_params(colors=C_MUTE, labelsize=8)
    for s in ax.spines.values():
        s.set_color(C_GRID)
    ax.grid(True, color=C_GRID, alpha=0.55, linewidth=0.7)
    ax.set_facecolor("#15171c")


def chart_profile(bars: pd.DataFrame, path: Path) -> None:
    frames = _session_frames(bars)
    if not frames:
        return
    lo, hi, f1m = frames[len(frames) // 2]
    day = pd.Timestamp(int(lo), unit="ns", tz="UTC")
    t1s = to_ns_arr(bars, "time")
    m1s = (t1s >= lo) & (t1s < hi)
    f1s = bars[m1s].reset_index(drop=True)

    fig, axes = plt.subplots(1, 2, figsize=(13, 5.2), facecolor="#0f1115")
    for ax, prof, title in (
        (axes[0], _profile_from(f1m, lo, hi), "Profile from 1-MINUTE volume (what the engine uses)"),
        (axes[1], _profile_from(f1s, lo, hi), "Profile from 1-SECOND volume"),
    ):
        mid = (prof.row_prices_lo + prof.row_prices_hi) / 2.0
        ax.barh(mid, prof.row_vols,
                height=(prof.row_prices_hi - prof.row_prices_lo),
                color=C_ACCENT, alpha=0.85, edgecolor="none")
        for z in prof.hvn_zones:
            ax.axhspan(z.lo, z.hi, color=C_GOOD, alpha=0.34, zorder=0)
        for z in prof.lvn_zones:
            ax.axhspan(z.lo, z.hi, color=C_DANGER, alpha=0.32, zorder=0)
        ax.axhline(prof.poc, color=C_WARN, lw=1.4, ls="--")
        _style(ax, title, "Tick-volume per price row", "Price (USD/oz)")
        ax.invert_yaxis()
        ax.text(0.995, 0.03, f"POC ${prof.poc:.2f}   HVN {len(prof.hvn_zones)}   "
                            f"LVN {len(prof.lvn_zones)}",
                transform=ax.transAxes, ha="right", color=C_WARN, fontsize=8.5)
    fig.suptitle(f"XAUUSD daily volume profile — {day.date()}",
                 fontsize=12, color="#e6e6e6", x=0.01, ha="left")
    fig.text(0.01, 0.915, "green = HVN shelf    red = LVN shelf    dashed = POC",
             fontsize=8.5, color=C_MUTE)
    fig.tight_layout(rect=(0, 0, 1, 0.93))
    fig.savefig(path, dpi=115, facecolor="#0f1115")
    plt.close(fig)


def chart_leak(bars: pd.DataFrame, leak: dict[str, Any], path: Path) -> None:
    frames = _session_frames(bars)
    lo, hi, f1m = frames[len(frames) // 2]
    t = to_ns_arr(f1m, "time")
    vol = f1m["volume"].to_numpy(dtype=np.float64)
    cum = np.cumsum(vol) / max(vol.sum(), 1e-12) * 100.0
    hours = (t - t[0]) / 3.6e12

    fig, ax = plt.subplots(figsize=(11, 4.6), facecolor="#0f1115")
    ax.plot(hours, cum, color=C_ACCENT, lw=2.0,
            label="Volume actually observed so far in the day")
    ax.axhline(100.0, color=C_DANGER, lw=1.7, ls="--",
               label="What the engine's profile already contains (whole day)")
    ax.axvline(0.0, color=C_WARN, lw=1.4, ls=":")
    _style(ax, "Forward leak in the daily volume profile",
           "Hours into the UTC day", "Cumulative volume known (%)")
    ax.set_ylim(0, 106)
    ax.legend(facecolor="#1b1e25", edgecolor=C_GRID, fontsize=8, labelcolor="#d6d7dd")
    pct = leak.get("volume_from_after_noon_pct")
    if pct is not None:
        ax.text(0.99, 0.06, f"{pct:.0f}% of the day's volume happened AFTER noon",
                transform=ax.transAxes, ha="right", color=C_DANGER, fontsize=9)
    fig.tight_layout()
    fig.savefig(path, dpi=115, facecolor="#0f1115")
    plt.close(fig)


def chart_forward(fb: dict[str, Any], path: Path) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(13, 4.7), facecolor="#0f1115")

    ax = axes[0]
    labels = ["HVN shelf", "random band\n(matched width)"]
    vals = [fb.get("hvn_stopped_at_shelf_rate"), fb.get("control_stopped_at_band_rate")]
    vals = [0.0 if v is None else v * 100 for v in vals]
    ax.bar(labels, vals, color=[C_ACCENT, C_MUTE], width=0.5)
    for i, v in enumerate(vals):
        ax.text(i, v + 1.5, f"{v:.1f}%", ha="center", color="#e6e6e6", fontsize=10)
    lift = fb.get("stop_lift_vs_control")
    if lift is not None:
        ax.text(0.5, 0.90, f"lift = {lift * 100:+.1f} pp", transform=ax.transAxes,
                ha="center", color=C_WARN, fontsize=10)
    _style(ax, "Does price STOP at a volume shelf?",
           "Forward level type", "% of touches where price closed back below the level")
    ax.set_ylim(0, max(vals + [10]) * 1.25)

    ax = axes[1]
    labels = ["inside LVN", "inside HVN", "neither"]
    keys = ["median_speed_lvn_usd_per_min", "median_speed_hvn_usd_per_min",
            "median_speed_neither_usd_per_min"]
    vals = [fb.get(k) or 0.0 for k in keys]
    ax.bar(labels, vals, color=[C_DANGER, C_GOOD, C_MUTE], width=0.5)
    for i, v in enumerate(vals):
        ax.text(i, v + max(vals) * 0.03, f"${v:.3f}/min", ha="center",
                color="#e6e6e6", fontsize=9.5)
    _style(ax, "LVN fast-move-through (distance-normalised)",
           "Level's position in the leak-free profile", "Price speed (USD per minute)")
    ratio = fb.get("lvn_speed_ratio_vs_neither")
    if ratio is not None:
        ax.text(0.5, 0.90, f"LVN / neither = {ratio:.2f}x", transform=ax.transAxes,
                ha="center", color=C_WARN, fontsize=10)
    fig.suptitle("Profile-level forward behaviour — built WITHOUT future volume",
                 fontsize=12, color="#e6e6e6", x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=115, facecolor="#0f1115")
    plt.close(fig)


def chart_arms(agg: list[dict[str, Any]], path: Path) -> None:
    order = ["canonical", "placebo", "causal_1m", "canonical_1s", "causal_1s",
             "nocap_canonical", "nocap_placebo", "nocap_causal"]
    arms = [a for a in order if any(r["arm"] == a for r in agg)]
    labels = [a.replace("_", "\n") for a in arms]
    pick = lambda a, k: next(r[k] for r in agg if r["arm"] == a)  # noqa: E731
    gross = [pick(a, "gross_usd") for a in arms]
    net = [pick(a, "net_usd") for a in arms]
    tp1 = [pick(a, "tp1_exit_rate") * 100 for a in arms]
    legs = [pick(a, "legs") for a in arms]

    fig, axes = plt.subplots(1, 3, figsize=(15, 5.0), facecolor="#0f1115")

    ax = axes[0]
    x = np.arange(len(arms))
    ax.bar(x - 0.2, gross, 0.4, color=C_ACCENT, label="Gross P&L")
    ax.bar(x + 0.2, net, 0.4, color=C_WARN, label="Net P&L")
    ax.set_xticks(x)
    ax.set_xticklabels(labels, fontsize=7.5)
    ax.axhline(0, color=C_MUTE, lw=1)
    _style(ax, "Strategy P&L by profile treatment", "Profile variant",
           "USD (summed over slices)")
    ax.legend(facecolor="#1b1e25", edgecolor=C_GRID, fontsize=8, labelcolor="#d6d7dd")

    ax = axes[1]
    ax.bar(labels, tp1, color=[C_ACCENT if "placebo" not in a else C_MUTE for a in arms],
           width=0.55)
    for i, v in enumerate(tp1):
        ax.text(i, v + 0.6, f"{v:.1f}%", ha="center", color="#e6e6e6", fontsize=9)
    _style(ax, "Share of legs that exited at TP1 (the HVN partial)",
           "Profile variant", "% of legs")

    ax = axes[2]
    ax.bar(labels, legs, color=C_ACCENT, width=0.55)
    for i, v in enumerate(legs):
        ax.text(i, v + max(legs) * 0.02, f"{v:,}", ha="center", color="#e6e6e6", fontsize=8.5)
    _style(ax, "Leg count by profile treatment", "Profile variant",
           "Legs (summed over slices)")

    fig.suptitle("Does the volume profile change the strategy's outcome?",
                 fontsize=12, color="#e6e6e6", x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=115, facecolor="#0f1115")
    plt.close(fig)


def chart_attribution(agg: list[dict[str, Any]], path: Path) -> None:
    arms = [a for a in ("canonical", "placebo", "causal_1m", "canonical_1s",
                        "nocap_canonical", "nocap_placebo")
            if any(r["arm"] == a for r in agg)]
    sl_keys = ["zone_edge+cap", "hvn_n1+cap", "hvn_n1+min", "hvn_n1", "zone_edge",
               "breakeven", "poc"]
    tp_keys = ["multi_hvn_n3", "multi_hvn_proxy", "multi_swing_edge", ""]
    fig, axes = plt.subplots(1, 2, figsize=(14.5, 5.2), facecolor="#0f1115")

    specs = [
        (axes[0], sl_keys, "sl_anchor", "Which level set the STOP"),
        (axes[1], tp_keys, "tp_anchor", "Which level set the TARGET"),
    ]
    for ax, keys, field, ttl in specs:
        data = []
        for a in arms:
            row = next(r for r in agg if r["arm"] == a)
            src = row.get(field, {})
            total = max(sum(src.values()), 1)
            data.append([src.get(k, 0) / total * 100 for k in keys])
        data = np.array(data).T
        left = np.zeros(len(arms))
        palette = [C_ACCENT, C_GOOD, C_WARN, C_DANGER, C_MUTE, "#8b5cf6", "#64748b"]
        for i, k in enumerate(keys):
            ax.barh(arms, data[i], left=left, label=k or "(none)",
                    color=palette[i % len(palette)], height=0.55)
            left += data[i]
        _style(ax, ttl, "% of legs", ttl)
        ax.legend(facecolor="#1b1e25", edgecolor=C_GRID, fontsize=8,
                  labelcolor="#d6d7dd", loc="lower right")
    fig.suptitle("Per-leg anchor attribution — which level actually set SL and TP",
                 fontsize=12, color="#e6e6e6", x=0.01, ha="left")
    fig.tight_layout(rect=(0, 0, 1, 0.92))
    fig.savefig(path, dpi=115, facecolor="#0f1115")
    plt.close(fig)


def chart_inertness(agg: list[dict[str, Any]], path: Path) -> None:
    """SL distinct values per arm: how many different stop distances?"""
    arms = [a for a in ("canonical", "placebo", "causal_1m", "canonical_1s",
                        "nocap_canonical", "nocap_placebo", "nocap_causal")
            if any(r["arm"] == a for r in agg)]
    vals = [next(r["sl_distinct_values"] for r in agg if r["arm"] == a) for a in arms]
    lo = [next(r.get("sl_min_usd") or 0 for r in agg if r["arm"] == a) for a in arms]
    hi = [next(r.get("sl_max_usd") or 0 for r in agg if r["arm"] == a) for a in arms]

    fig, ax = plt.subplots(figsize=(11, 4.4), facecolor="#0f1115")
    x = np.arange(len(arms))
    colors = [C_DANGER if v <= 2 else C_ACCENT for v in vals]
    ax.bar(x, vals, color=colors, width=0.55)
    for i, v in enumerate(vals):
        rng = (f"${lo[i]:.2f}-${hi[i]:.2f}" if lo[i] != hi[i] else f"${lo[i]:.2f}")
        ax.text(i, v + max(vals) * 0.03, f"{v}\n{rng}", ha="center",
                color="#e6e6e6", fontsize=9)
    ax.set_xticks(x)
    ax.set_xticklabels([a.replace("_", "\n") for a in arms], fontsize=8)
    _style(ax, "How many DIFFERENT stop distances does the profile produce?",
           "Profile variant", "Distinct SL distances across all legs")
    fig.tight_layout()
    fig.savefig(path, dpi=115, facecolor="#0f1115")
    plt.close(fig)


# ──────────────────────────────────────────────────────────────────────
# main
# ──────────────────────────────────────────────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--pilot", action="store_true")
    ap.add_argument("--skip-forward", action="store_true")
    args = ap.parse_args()

    OUTDIR.mkdir(parents=True, exist_ok=True)
    slices = PILOT_SLICES if args.pilot else SLICES

    print("=" * 78)
    print("HVN / LVN PROOF HARNESS")
    print("=" * 78)

    probe_s, probe_e = "2025-01-01", "2025-02-01"
    print(f"\n[probe slice] 2025-01 UP")
    pbars = load_bars(DATA, probe_s, probe_e)
    cadence = cadence_audit(pbars)
    print(f"  profile cadence      : {cadence['profile_cadence']}")
    print(f"  1s bars -> 1m bars   : {cadence['n_1s_bars']:,} -> {cadence['n_1m_bars']:,}"
          f"  (volume conserved: {cadence['volume_conserved']})")
    print(f"  median 1s range      : ${cadence['median_1s_range_usd']:.3f}"
          f"  (x60 = ${cadence['median_1s_range_x60']:.2f})")
    print(f"  tickv vs range corr  : {cadence['tickv_vs_range_corr']:.3f}")

    leak = leak_audit(pbars)
    print(f"\n[leak audit] {leak.get('probe_day')}")
    print(f"  volume from after noon      : {leak.get('volume_from_after_noon_pct'):.1f}%")
    print(f"  seen by the day's FIRST bar : {leak.get('cumulative_volume_seen_by_first_bar_pct'):.3f}%")
    print(f"  HVN shelves full-day        : {leak.get('full_day_hvn_shelves')}")
    print(f"  HVN shelves truncated@noon  : {leak.get('truncated_noon_hvn_shelves')}")
    print(f"  shared (near-match)         : {leak.get('near_shared_shelves')}")
    print(f"  POC shift from truncation   : ${leak.get('poc_shift_usd'):+.2f}")

    fb: dict[str, Any] = {}
    if not args.skip_forward:
        print("\n[forward behaviour] leak-free profile, hourly checkpoints ...",
              flush=True)
        t0 = time.perf_counter()
        fb = forward_behaviour(pbars)
        print(f"  HVN shelves ahead          : {fb.get('hvn_shelves_ahead')}")
        print(f"  HVN stop rate at shelf     : {fb.get('hvn_stopped_at_shelf_rate')}")
        print(f"  control stop rate          : {fb.get('control_stopped_at_band_rate')}")
        print(f"  stop lift vs control       : {fb.get('stop_lift_vs_control')}")
        print(f"  speed LVN/HVN/neither $/min : {fb.get('median_speed_lvn_usd_per_min')} / "
              f"{fb.get('median_speed_hvn_usd_per_min')} / "
              f"{fb.get('median_speed_neither_usd_per_min')}")
        print(f"  LVN speed ratio vs neither : {fb.get('lvn_speed_ratio_vs_neither')}"
              f"  ({time.perf_counter() - t0:.0f}s)", flush=True)
    del pbars
    gc.collect()

    results = []
    for tag, s, e in slices:
        try:
            results.append(run_slice(tag, s, e))
        except Exception as exc:
            print(f"  !! slice {tag} failed: {exc}", flush=True)
            import traceback
            traceback.print_exc()
        gc.collect()

    arms_seen: list[str] = []
    for r in results:
        for a in r["arms"]:
            if a["arm"] not in arms_seen:
                arms_seen.append(a["arm"])
    agg = []
    for arm in arms_seen:
        rows = [a for r in results for a in r["arms"] if a["arm"] == arm]
        if not rows:
            continue
        legs = sum(r["legs"] for r in rows)
        agg.append({
            "arm": arm,
            "legs": legs,
            "long": sum(r["long"] for r in rows),
            "short": sum(r["short"] for r in rows),
            "gross_usd": round(sum(r["gross_usd"] for r in rows), 2),
            "fees_usd": round(sum(r["fees_usd"] for r in rows), 2),
            "net_usd": round(sum(r["net_usd"] for r in rows), 2),
            "ev_per_trade_usd": round(sum(r["net_usd"] for r in rows) / max(legs, 1), 4),
            "hit_rate": round(sum(r["hit_rate"] * r["legs"] for r in rows) / max(legs, 1), 4),
            "tp1_exit_rate": round(sum(r["tp1_exit_rate"] * r["legs"] for r in rows) / max(legs, 1), 4),
            "n_tp1_exits": sum(r["n_tp1_exits"] for r in rows),
            "n_legs_tp1_hvn_shelf": sum(r["n_legs_tp1_hvn_shelf"] for r in rows),
            "n_legs_sl_hvn_anchored": sum(r["n_legs_sl_hvn_anchored"] for r in rows),
            "sl_distinct_values": max(r["sl_distinct_values"] for r in rows),
            "sl_min_usd": min((r["sl_min_usd"] for r in rows if r["sl_min_usd"] is not None),
                              default=None),
            "sl_max_usd": max((r["sl_max_usd"] for r in rows if r["sl_max_usd"] is not None),
                              default=None),
            "sl_anchor": {k: sum(r["sl_anchor"].get(k, 0) for r in rows)
                          for k in {kk for r in rows for kk in r["sl_anchor"]}},
            "tp_anchor": {k: sum(r["tp_anchor"].get(k, 0) for r in rows)
                          for k in {kk for r in rows for kk in r["tp_anchor"]}},
            "n_slices": len(rows),
        })

    print("\n" + "=" * 92)
    print(f"{'arm':17}{'legs':>8}{'gross':>11}{'fees':>10}{'net':>11}"
          f"{'EV':>8}{'tp1%':>8}{'slvals':>8}{'slrange':>14}")
    print("-" * 92)
    for a in agg:
        rng = (f"${a['sl_min_usd']:.2f}-${a['sl_max_usd']:.2f}"
               if a["sl_min_usd"] is not None and a["sl_max_usd"] is not None
               and a["sl_min_usd"] != a["sl_max_usd"]
               else (f"${a['sl_max_usd']:.2f}" if a["sl_max_usd"] is not None else "-"))
        print(f"{a['arm']:17}{a['legs']:>8,}{a['gross_usd']:>11,.0f}{a['fees_usd']:>10,.0f}"
              f"{a['net_usd']:>11,.0f}{a['ev_per_trade_usd']:>8.3f}"
              f"{a['tp1_exit_rate'] * 100:>7.1f}%{a['sl_distinct_values']:>8}{rng:>14}")
    print("=" * 92)

    cbars = load_bars(DATA, probe_s, probe_e)
    chart_profile(cbars, OUTDIR / "hvn_lvn_proof_profile.png")
    chart_leak(cbars, leak, OUTDIR / "hvn_lvn_proof_leak.png")
    if fb:
        chart_forward(fb, OUTDIR / "hvn_lvn_proof_forward.png")
    if agg:
        chart_arms(agg, OUTDIR / "hvn_lvn_proof_arms.png")
        chart_attribution(agg, OUTDIR / "hvn_lvn_proof_attribution.png")
        chart_inertness(agg, OUTDIR / "hvn_lvn_proof_inertness.png")
    del cbars
    gc.collect()

    payload = {
        "generated_utc": pd.Timestamp.utcnow().isoformat(),
        "data_file": str(DATA),
        "slices": [{"tag": t, "start": s, "end": e} for t, s, e in slices],
        "cadence_audit": cadence,
        "leak_audit": leak,
        "forward_behaviour": fb,
        "slice_results": results,
        "aggregate": agg,
    }
    with open(JSON_OUT, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, default=str)
    print(f"\n-> {JSON_OUT}")
    for p in sorted(OUTDIR.glob("hvn_lvn_proof_*.png")):
        print(f"-> {p}")


if __name__ == "__main__":
    main()
