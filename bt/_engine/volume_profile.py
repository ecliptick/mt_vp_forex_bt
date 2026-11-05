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

@dataclass
class HVNZone:
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
    lo: float
    hi: float
    mid: float
    vol: float
    session_start_ns: int
    session_end_ns: int

@dataclass
class SessionVP:
    session_start_ns: int
    session_end_ns: int
    poc: float
    vah: float
    val: float
    total_vol: float
    hvn_zones: List[HVNZone]
    lvn_zones: List[LVNZone]
    row_prices_lo: np.ndarray
    row_prices_hi: np.ndarray
    row_vols: np.ndarray

@dataclass
class VolumeProfileSet:
    file_stem: str
    daily_vp: List[SessionVP]
    weekly_vp: List[SessionVP]
    naked_pocs: List[Tuple[float, int]]
    build_ms: dict = field(default_factory=dict)

def _build_session_profile(close_lo: np.ndarray, close_hi: np.ndarray, close_arr: np.ndarray, high_arr: np.ndarray, low_arr: np.ndarray, vol_arr: np.ndarray, session_start_ns: int, session_end_ns: int, n_rows: int=100, va_pct: float=70.0, hvn_peak_pct: float=0.3, hvn_min_prominence_pct: float=15.0, hvn_zone_pct: float=50.0, lvn_threshold_pct: float=25.0, min_zone_width_usd: float=5.0) -> SessionVP:
    p_lo = float(close_lo.min()) if close_lo.size else 0.0
    p_hi = float(close_hi.max()) if close_hi.size else 0.0
    if p_hi - p_lo < min_zone_width_usd:
        return SessionVP(session_start_ns=session_start_ns, session_end_ns=session_end_ns, poc=(p_lo + p_hi) / 2.0, vah=p_hi, val=p_lo, total_vol=float(vol_arr.sum()) if vol_arr.size else 0.0, hvn_zones=[], lvn_zones=[], row_prices_lo=np.array([p_lo]), row_prices_hi=np.array([p_hi]), row_vols=np.array([float(vol_arr.sum()) if vol_arr.size else 0.0]))
    step = (p_hi - p_lo) / n_rows
    row_lo = p_lo + np.arange(n_rows) * step
    row_hi = row_lo + step
    row_vols = np.zeros(n_rows, dtype=np.float64)
    if vol_arr.size:
        idx_lo = np.clip(((low_arr - p_lo) / step).astype(np.int64), 0, n_rows - 1)
        idx_hi = np.clip(((high_arr - p_lo) / step).astype(np.int64), 0, n_rows - 1)
        bar_idx = np.arange(len(vol_arr))
        for j in range(len(vol_arr)):
            lo_i = int(idx_lo[j])
            hi_i = int(idx_hi[j])
            if hi_i < lo_i:
                hi_i = lo_i
            share = float(vol_arr[j]) / (hi_i - lo_i + 1)
            row_vols[lo_i:hi_i + 1] += share
    total_vol = float(row_vols.sum())
    if total_vol <= 0:
        return SessionVP(session_start_ns=session_start_ns, session_end_ns=session_end_ns, poc=(p_lo + p_hi) / 2.0, vah=p_hi, val=p_lo, total_vol=0.0, hvn_zones=[], lvn_zones=[], row_prices_lo=row_lo.copy(), row_prices_hi=row_hi.copy(), row_vols=row_vols)
    poc_row = int(np.argmax(row_vols))
    poc_price = float(row_lo[poc_row] + step / 2.0)
    target = total_vol * (va_pct / 100.0)
    va_sum = float(row_vols[poc_row])
    up = poc_row + 1
    dn = poc_row - 1
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
    vah_row = min(up - 1, n_rows - 1)
    val_row = max(dn + 1, 0)
    vah_price = float(row_hi[vah_row])
    val_price = float(row_lo[val_row])
    hvn_zones: List[HVNZone] = []
    vmax = float(row_vols.max())
    if vmax > 0:
        mean_v = total_vol / n_rows
        std_v = float(np.std(row_vols))
        adaptive_floor = mean_v + 1.0 * std_v
        peak_floor = max(adaptive_floor, vmax * hvn_peak_pct / 100.0)
        peaks: List[int] = []
        for r in range(n_rows):
            if row_vols[r] < peak_floor:
                continue
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
            min_l, min_r = (pv, pv)
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
            hvn_zones.append(HVNZone(lo=lo_p, hi=hi_p, mid=(lo_p + hi_p) / 2.0, vol=pv, session_start_ns=session_start_ns, session_end_ns=session_end_ns))
    lvn_zones: List[LVNZone] = []
    mean_v = total_vol / n_rows
    lvn_thresh = mean_v * lvn_threshold_pct / 100.0
    is_low = row_vols < lvn_thresh
    if is_low.any():
        in_run = False
        run_start = -1
        runs: List[Tuple[int, int]] = []
        for r in range(n_rows):
            if is_low[r] and (not in_run):
                run_start = r
                in_run = True
            elif not is_low[r] and in_run:
                runs.append((run_start, r - 1))
                in_run = False
        if in_run:
            runs.append((run_start, n_rows - 1))
        for r0, r1 in runs:
            lo_p = float(row_lo[r0])
            hi_p = float(row_hi[r1])
            if hi_p - lo_p < min_zone_width_usd:
                continue
            avg_v = float(row_vols[r0:r1 + 1].mean())
            lvn_zones.append(LVNZone(lo=lo_p, hi=hi_p, mid=(lo_p + hi_p) / 2.0, vol=avg_v, session_start_ns=session_start_ns, session_end_ns=session_end_ns))
    return SessionVP(session_start_ns=session_start_ns, session_end_ns=session_end_ns, poc=poc_price, vah=vah_price, val=val_price, total_vol=total_vol, hvn_zones=hvn_zones, lvn_zones=lvn_zones, row_prices_lo=row_lo.copy(), row_prices_hi=row_hi.copy(), row_vols=row_vols)

def resample_1s_to_1m(bars: pd.DataFrame) -> pd.DataFrame:
    if 'time' not in bars.columns:
        raise ValueError("bars must have a 'time' column")
    df = bars.set_index(pd.to_datetime(bars['time'], utc=True))
    agg = {'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last', 'volume': 'sum'}
    out = df.resample('1min').agg(agg).dropna(subset=['open']).reset_index()
    out = out.rename(columns={'index': 'time'})
    return out

def _bucket_by_session(times_ns: np.ndarray, session_secs: int) -> List[Tuple[int, int]]:
    if times_ns.size == 0:
        return []
    t0 = int(times_ns[0])
    t_end = int(times_ns[-1])
    if session_secs == 86400:
        sec_per_day = 86400
        day0 = (t0 // 1000000000 - t0 // 1000000000 % sec_per_day) * 1000000000
    elif session_secs == 604800:
        sec_per_week = 604800
        day0 = (t0 // 1000000000 - t0 // 1000000000 % sec_per_week) * 1000000000
    else:
        day0 = (t0 // 1000000000 - t0 // 1000000000 % session_secs) * 1000000000
    boundaries: List[int] = []
    b = day0
    while b <= t_end + session_secs * 1000000000:
        boundaries.append(b)
        b += session_secs * 1000000000
    return boundaries

def _indices_in_window(times_ns: np.ndarray, lo_ns: int, hi_ns: int) -> np.ndarray:
    return (times_ns >= lo_ns) & (times_ns < hi_ns)

def build_volume_profiles(bars_1s: pd.DataFrame, *, file_stem: str, vp_session_secs: int=86400, vp_rolling_secs: int=604800, vp_rows: int=100, vp_va_pct: float=70.0, vp_hvn_peak_pct: float=30.0, vp_hvn_min_prominence_pct: float=15.0, vp_hvn_zone_pct: float=50.0, vp_lvn_threshold_pct: float=25.0, vp_min_zone_usd: float=5.0, verbose: bool=True) -> VolumeProfileSet:
    timings: dict = {}
    t_total0 = time.perf_counter()
    t0 = time.perf_counter()
    bars_1m = resample_1s_to_1m(bars_1s)
    timings['resample_ms'] = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"  VP resample 1s->1m: {len(bars_1s):,} -> {len(bars_1m):,} bars in {timings['resample_ms']:.0f}ms")
    times_ns = to_ns_arr(bars_1m, 'time')
    assert_ns(times_ns, 'volume_profile times')
    open_arr = bars_1m['open'].to_numpy(dtype=np.float64)
    high_arr = bars_1m['high'].to_numpy(dtype=np.float64)
    low_arr = bars_1m['low'].to_numpy(dtype=np.float64)
    close_arr = bars_1m['close'].to_numpy(dtype=np.float64)
    if 'volume' in bars_1m.columns:
        vol_arr = bars_1m['volume'].to_numpy(dtype=np.float64)
    else:
        vol_arr = np.ones_like(close_arr)
    t0 = time.perf_counter()
    daily_boundaries = _bucket_by_session(times_ns, vp_session_secs)
    daily_vp: List[SessionVP] = []
    for i in range(len(daily_boundaries) - 1):
        lo_ns = daily_boundaries[i]
        hi_ns = daily_boundaries[i + 1]
        mask = _indices_in_window(times_ns, lo_ns, hi_ns)
        if not mask.any():
            continue
        svp = _build_session_profile(close_arr[mask].copy(), close_arr[mask].copy(), close_arr[mask], high_arr[mask], low_arr[mask], vol_arr[mask], session_start_ns=lo_ns, session_end_ns=hi_ns, n_rows=vp_rows, va_pct=vp_va_pct, hvn_peak_pct=vp_hvn_peak_pct, hvn_min_prominence_pct=vp_hvn_min_prominence_pct, hvn_zone_pct=vp_hvn_zone_pct, lvn_threshold_pct=vp_lvn_threshold_pct, min_zone_width_usd=vp_min_zone_usd)
        daily_vp.append(svp)
    timings['daily_ms'] = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"  VP daily sessions: {len(daily_vp)} built in {timings['daily_ms']:.0f}ms")
    t0 = time.perf_counter()
    weekly_boundaries = _bucket_by_session(times_ns, vp_rolling_secs)
    weekly_vp: List[SessionVP] = []
    for i in range(len(weekly_boundaries) - 1):
        lo_ns = weekly_boundaries[i]
        hi_ns = weekly_boundaries[i + 1]
        mask = _indices_in_window(times_ns, lo_ns, hi_ns)
        if not mask.any():
            continue
        svp = _build_session_profile(close_arr[mask].copy(), close_arr[mask].copy(), close_arr[mask], high_arr[mask], low_arr[mask], vol_arr[mask], session_start_ns=lo_ns, session_end_ns=hi_ns, n_rows=vp_rows, va_pct=vp_va_pct, hvn_peak_pct=vp_hvn_peak_pct, hvn_min_prominence_pct=vp_hvn_min_prominence_pct, hvn_zone_pct=vp_hvn_zone_pct, lvn_threshold_pct=vp_lvn_threshold_pct, min_zone_width_usd=vp_min_zone_usd)
        weekly_vp.append(svp)
    timings['weekly_ms'] = (time.perf_counter() - t0) * 1000
    if verbose:
        print(f"  VP weekly sessions: {len(weekly_vp)} built in {timings['weekly_ms']:.0f}ms")
    naked_pocs: List[Tuple[float, int]] = []
    for svp in daily_vp:
        naked_pocs.append((svp.poc, svp.session_end_ns))
    timings['naked_ms'] = 0.1
    timings['total_ms'] = (time.perf_counter() - t_total0) * 1000
    if verbose:
        print(f"  VP total build: {timings['total_ms']:.0f}ms; naked_pocs={len(naked_pocs)}")
    return VolumeProfileSet(file_stem=file_stem, daily_vp=daily_vp, weekly_vp=weekly_vp, naked_pocs=naked_pocs, build_ms=timings)
_VP_CACHE_VERSION = 4
_VP_CACHE_KEYS = ('vp_session_secs', 'vp_rolling_secs', 'vp_rows', 'vp_va_pct', 'vp_hvn_peak_pct', 'vp_hvn_min_prominence_pct', 'vp_hvn_zone_pct', 'vp_lvn_threshold_pct', 'vp_min_zone_usd')

def _vp_fingerprint(raw_path: Path, params: dict) -> dict:
    fp = {'_cache_version': _VP_CACHE_VERSION}
    for k in _VP_CACHE_KEYS:
        fp[k] = params.get(k)
    try:
        fp['_raw_mtime_ns'] = int(raw_path.stat().st_mtime_ns)
        fp['_raw_size_bytes'] = int(raw_path.stat().st_size)
    except OSError:
        fp['_raw_mtime_ns'] = -1
        fp['_raw_size_bytes'] = 0
    return fp

def _vp_cache_dir(raw_path: Path) -> Path:
    cache_root = Path('notebooks/.cache') / raw_path.stem / 'vp'
    cache_root.mkdir(parents=True, exist_ok=True)
    return cache_root

def _vp_save(obj, path: Path) -> None:
    with open(path, 'wb') as fh:
        pickle.dump(obj, fh, protocol=pickle.HIGHEST_PROTOCOL)

def _vp_load(path: Path):
    with open(path, 'rb') as fh:
        return pickle.load(fh)

def _vp_is_cache_valid(raw_path: Path, params: dict) -> bool:
    cache_dir = _vp_cache_dir(raw_path)
    fp_path = cache_dir / 'vp_fingerprint.json'
    if not fp_path.exists():
        return False
    try:
        with open(fp_path, 'r', encoding='utf-8') as fh:
            stored = json.load(fh)
    except Exception:
        return False
    fp = _vp_fingerprint(raw_path, params)
    if stored.get('_cache_version') != fp['_cache_version']:
        return False
    for k in _VP_CACHE_KEYS:
        if stored.get(k) != fp[k]:
            return False
    if stored.get('_raw_mtime_ns') != fp['_raw_mtime_ns']:
        return False
    if stored.get('_raw_size_bytes') != fp['_raw_size_bytes']:
        return False
    required = ('vp_daily.pkl', 'vp_weekly.pkl', 'vp_naked_pocs.pkl', 'vp_meta.json')
    return all(((cache_dir / n).exists() for n in required))

def get_or_build_vp(raw_path: Path, bars_1s: pd.DataFrame, *, vp_session_secs: int=86400, vp_rolling_secs: int=604800, vp_rows: int=100, vp_va_pct: float=70.0, vp_hvn_peak_pct: float=30.0, vp_hvn_min_prominence_pct: float=15.0, vp_hvn_zone_pct: float=50.0, vp_lvn_threshold_pct: float=25.0, vp_min_zone_usd: float=5.0, force: bool=False, verbose: bool=True) -> VolumeProfileSet:
    params = {'vp_session_secs': vp_session_secs, 'vp_rolling_secs': vp_rolling_secs, 'vp_rows': vp_rows, 'vp_va_pct': vp_va_pct, 'vp_hvn_peak_pct': vp_hvn_peak_pct, 'vp_hvn_min_prominence_pct': vp_hvn_min_prominence_pct, 'vp_hvn_zone_pct': vp_hvn_zone_pct, 'vp_lvn_threshold_pct': vp_lvn_threshold_pct, 'vp_min_zone_usd': vp_min_zone_usd}
    cache_dir = _vp_cache_dir(raw_path)
    if not force and _vp_is_cache_valid(raw_path, params):
        t0 = time.perf_counter()
        daily_vp = _vp_load(cache_dir / 'vp_daily.pkl')
        weekly_vp = _vp_load(cache_dir / 'vp_weekly.pkl')
        naked_pocs = _vp_load(cache_dir / 'vp_naked_pocs.pkl')
        load_ms = (time.perf_counter() - t0) * 1000
        if verbose:
            print(f'  VP cache hit: {cache_dir.parent.name}/vp ({len(daily_vp)} daily, {len(weekly_vp)} weekly, {len(naked_pocs)} naked POCs) loaded in {load_ms:.0f}ms')
        return VolumeProfileSet(file_stem=raw_path.stem, daily_vp=daily_vp, weekly_vp=weekly_vp, naked_pocs=naked_pocs, build_ms={'load_ms': load_ms, 'total_ms': load_ms, 'cache_hit': True})
    if verbose:
        print(f'  VP cache miss: building {cache_dir.parent.name}/vp ...')
    vps = build_volume_profiles(bars_1s, file_stem=raw_path.stem, vp_session_secs=vp_session_secs, vp_rolling_secs=vp_rolling_secs, vp_rows=vp_rows, vp_va_pct=vp_va_pct, vp_hvn_peak_pct=vp_hvn_peak_pct, vp_hvn_min_prominence_pct=vp_hvn_min_prominence_pct, vp_hvn_zone_pct=vp_hvn_zone_pct, vp_lvn_threshold_pct=vp_lvn_threshold_pct, vp_min_zone_usd=vp_min_zone_usd, verbose=verbose)
    _vp_save(vps.daily_vp, cache_dir / 'vp_daily.pkl')
    _vp_save(vps.weekly_vp, cache_dir / 'vp_weekly.pkl')
    _vp_save(vps.naked_pocs, cache_dir / 'vp_naked_pocs.pkl')
    fp = _vp_fingerprint(raw_path, params)
    with open(cache_dir / 'vp_fingerprint.json', 'w', encoding='utf-8') as fh:
        json.dump(fp, fh, indent=2, sort_keys=True)
    meta = {'cache_version': _VP_CACHE_VERSION, 'raw_path': str(raw_path), 'n_daily': len(vps.daily_vp), 'n_weekly': len(vps.weekly_vp), 'n_naked_pocs': len(vps.naked_pocs), 'build_ms': {k: round(v, 1) for k, v in vps.build_ms.items()}}
    with open(cache_dir / 'vp_meta.json', 'w', encoding='utf-8') as fh:
        json.dump(meta, fh, indent=2, sort_keys=True)
    return vps

def _last_complete(sessions: List[SessionVP], t_ns: int) -> Optional[SessionVP]:
    # No look-ahead: only a session that has fully closed by t_ns is known at t_ns.
    for svp in reversed(sessions):
        if svp.session_end_ns <= t_ns:
            return svp
    return None

def current_daily_vp(vp_set: VolumeProfileSet, t_ns: int) -> Optional[SessionVP]:
    return _last_complete(vp_set.daily_vp, t_ns)

def current_weekly_vp(vp_set: VolumeProfileSet, t_ns: int) -> Optional[SessionVP]:
    return _last_complete(vp_set.weekly_vp, t_ns)

def naked_poc_targets(vp_set: VolumeProfileSet, t_ns: int, price: float, direction: int) -> List[Tuple[float, int]]:
    out: List[Tuple[float, int]] = []
    for poc, expire_ns in vp_set.naked_pocs:
        if expire_ns > t_ns:  # POC not known until its session closes
            continue
        if direction > 0 and poc > price:
            out.append((poc, expire_ns))
        elif direction < 0 and poc < price:
            out.append((poc, expire_ns))
    out.sort(key=lambda t: abs(t[0] - price))
    return out

def hvn_zones_near(vp_set: VolumeProfileSet, t_ns: int, price: float, direction: int, max_distance_usd: float=1000.0, max_results: int=5) -> List[Tuple[float, float, float]]:
    svp = current_daily_vp(vp_set, t_ns)
    if svp is None:
        return []
    out: List[Tuple[float, float, float]] = []
    for z in svp.hvn_zones:
        if direction > 0 and z.lo > price and (z.lo - price <= max_distance_usd):
            out.append((z.lo, z.hi, z.mid))
        elif direction < 0 and z.hi < price and (price - z.hi <= max_distance_usd):
            out.append((z.lo, z.hi, z.mid))
    out.sort(key=lambda t: abs(t[2] - price))
    return out[:max_results]

def lvn_zones_near(vp_set: VolumeProfileSet, t_ns: int, price: float, direction: int, max_distance_usd: float=1000.0, max_results: int=5) -> List[Tuple[float, float, float]]:
    svp = current_daily_vp(vp_set, t_ns)
    if svp is None:
        return []
    out: List[Tuple[float, float, float]] = []
    for z in svp.lvn_zones:
        if direction > 0 and z.lo > price and (z.lo - price <= max_distance_usd):
            out.append((z.lo, z.hi, z.mid))
        elif direction < 0 and z.hi < price and (price - z.hi <= max_distance_usd):
            out.append((z.lo, z.hi, z.mid))
    out.sort(key=lambda t: abs(t[2] - price))
    return out[:max_results]

def poc_distance_usd(vp_set: VolumeProfileSet, t_ns: int, price: float, direction: int) -> Optional[float]:
    svp = current_daily_vp(vp_set, t_ns)
    if svp is None:
        return None
    if direction > 0:
        return float(svp.poc - price)
    else:
        return float(price - svp.poc)
__all__ = ['HVNZone', 'LVNZone', 'SessionVP', 'VolumeProfileSet', 'build_volume_profiles', 'resample_1s_to_1m', 'get_or_build_vp', 'current_daily_vp', 'current_weekly_vp', 'naked_poc_targets', 'hvn_zones_near', 'lvn_zones_near', 'poc_distance_usd', '_VP_CACHE_VERSION']
