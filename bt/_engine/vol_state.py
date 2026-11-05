from __future__ import annotations
from collections import OrderedDict
from dataclasses import dataclass, field
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view
__all__ = ['VolState', 'build_vol_state', 'VOL_MEASURES']
VOL_MEASURES = ('rv_5s', 'rv_10s', 'rv_30s', 'rv_60s', 'rv_share_5s', 'rv_share_10s', 'rv_share_30s', 'rv_ratio_30s', 'range_norm_1m', 'vel_1m', 'vel_5m', 'vel_15m', 'shock', 'wick_ratio', 'vol_expand_prev_day')
_RV_WINDOWS = (5, 10, 30, 60)
_VEL_WINDOWS = (60, 300, 900)
_CACHE_MAX = 8
_CACHE: 'OrderedDict[tuple, VolState]' = OrderedDict()

@dataclass
class VolState:
    series: dict[str, np.ndarray] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def __getitem__(self, key: str) -> np.ndarray:
        return self.series[key]

    def __contains__(self, key: str) -> bool:
        return key in self.series

    def get(self, key: str) -> np.ndarray | None:
        return self.series.get(key)

def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    if w <= 1:
        return x.astype(np.float64, copy=True)
    c = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    out = c[w:] - c[:-w]
    return np.concatenate((np.zeros(w - 1, dtype=np.float64), out))

def _segment_bounds(idx: np.ndarray, n_seg: int) -> tuple[np.ndarray, np.ndarray]:
    starts = np.searchsorted(idx, np.arange(n_seg), side='left')
    starts = np.minimum(starts, max(0, idx.shape[0] - 1))
    ends = np.searchsorted(idx, np.arange(1, n_seg + 1), side='left')
    ends = np.maximum(ends, starts + 1)
    return (starts, ends)

def _per_bucket_stat(x: np.ndarray, bucket_idx: np.ndarray, n_buckets: int) -> np.ndarray:
    sums = np.bincount(bucket_idx, weights=x, minlength=n_buckets)
    counts = np.bincount(bucket_idx, minlength=n_buckets).astype(np.float64)
    counts = np.where(counts < 1.0, 1.0, counts)
    return sums / counts

def _per_bucket_median(x: np.ndarray, bucket_idx: np.ndarray, n_buckets: int) -> np.ndarray:
    starts, ends = _segment_bounds(bucket_idx, n_buckets)
    out = np.empty(n_buckets, dtype=np.float64)
    for k in range(n_buckets):
        out[k] = float(np.median(x[starts[k]:ends[k]]))
    return out

def _rolling_median_baseline(per_bucket: np.ndarray, win: int) -> np.ndarray:
    nb = per_bucket.shape[0]
    out = np.empty(nb, dtype=np.float64)
    if nb == 0:
        return out
    if win <= 1:
        return per_bucket.astype(np.float64, copy=True)
    n_short = min(win - 1, nb)
    for k in range(n_short):
        out[k] = float(np.median(per_bucket[0:k + 1]))
    if nb <= win - 1:
        return out
    sw = sliding_window_view(per_bucket, win)
    body = sw.shape[0]
    chunk = 100000
    for start in range(0, body, chunk):
        end = min(start + chunk, body)
        out[start + win - 1:end + win - 1] = np.median(sw[start:end], axis=1)
    return out

def _rolling_mean_baseline(per_bucket: np.ndarray, win: int) -> np.ndarray:
    nb = per_bucket.shape[0]
    c = np.concatenate(([0.0], np.cumsum(per_bucket, dtype=np.float64)))
    hi = np.arange(1, nb + 1)
    lo = np.maximum(0, hi - win)
    tot = c[hi] - c[lo]
    cnt = (hi - lo).astype(np.float64)
    cnt = np.where(cnt < 1.0, 1.0, cnt)
    return tot / cnt

def _prev_day_expansion(r2: np.ndarray, times_ns: np.ndarray) -> np.ndarray:
    n = r2.shape[0]
    out = np.ones(n, dtype=np.float64)
    day_ns = 86400 * 1000000000
    day_id = times_ns // day_ns
    uniq_days, day_start = np.unique(day_id, return_index=True)
    day_start = np.append(day_start, n)
    csum = np.concatenate(([0.0], np.cumsum(r2, dtype=np.float64)))
    day_of_bar = np.searchsorted(uniq_days, day_id, side='left')
    rv_so_far = csum[1:] - csum[day_start[day_of_bar]]
    sec_of_day = times_ns % day_ns // 1000000000
    frac = sec_of_day.astype(np.float64) / 86400.0
    for d in range(1, len(uniq_days)):
        prev_lo = day_start[d - 1]
        prev_hi = day_start[d]
        cur_lo = day_start[d]
        cur_hi = day_start[d + 1] if d + 1 < len(day_start) else n
        if cur_hi <= cur_lo:
            continue
        prev_csum_off = csum[prev_lo]
        prev_rv = csum[prev_lo + 1:prev_hi + 1] - prev_csum_off
        prev_frac = frac[prev_lo:prev_hi]
        cur_frac = frac[cur_lo:cur_hi]
        cur_rv = rv_so_far[cur_lo:cur_hi]
        idx = np.searchsorted(prev_frac, cur_frac, side='right') - 1
        idx = np.clip(idx, 0, prev_rv.shape[0] - 1)
        den = prev_rv[idx]
        num = cur_rv
        prev_covers = prev_frac[-1] >= cur_frac
        good = (den > 1e-18) & prev_covers
        out[cur_lo:cur_hi] = np.where(good, num / np.where(good, den, 1.0), 1.0)
    return out

def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    den = np.where(np.abs(den) < 1e-18, np.nan, den)
    out = np.divide(num, den, out=np.ones_like(num, dtype=np.float64), where=~np.isnan(den))
    return np.where(np.isfinite(out), out, 1.0)

def build_vol_state(high: np.ndarray, low: np.ndarray, close: np.ndarray, open_: np.ndarray | None=None, *, measures: tuple[str, ...] | list[str]=('rv_ratio_30s',), rv_baseline_secs: int=86400, range_baseline_mins: int=60, shock_baseline_secs: int=3600, candle_secs: int=60, min_bars_since_close: int=1, elapsed_adjust: bool=False, use_cache: bool=True, times_ns: np.ndarray | None=None) -> VolState:
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    n = close.shape[0]
    if n == 0:
        raise ValueError('build_vol_state: empty input arrays')
    _ck = None
    if use_cache:
        _want_fp = tuple(sorted(measures))
        _ts_fp = None
        if 'vol_expand_prev_day' in _want_fp:
            if times_ns is None:
                raise ValueError("build_vol_state: 'vol_expand_prev_day' requires times_ns (int64 epoch nanoseconds, ascending).")
            _ts_fp = (int(times_ns[0]), int(times_ns[-1]), int(times_ns.shape[0]))
        _ck = (hash((n, float(close[0]), float(close[-1]), float(high.sum()), float(low.sum()), float(close.sum()))), _want_fp, int(rv_baseline_secs), int(range_baseline_mins), int(shock_baseline_secs), int(candle_secs), int(min_bars_since_close), bool(elapsed_adjust), None if open_ is None else float(open_.sum()), _ts_fp)
        _hit = _CACHE.get(_ck)
        if _hit is not None:
            return _hit
    if high.shape[0] != n or low.shape[0] != n:
        raise ValueError('build_vol_state: high/low/close length mismatch')
    if open_ is not None:
        open_ = np.asarray(open_, dtype=np.float64)
        if open_.shape[0] != n:
            raise ValueError('build_vol_state: open_ length mismatch')
    want = set(measures)
    unknown = want - set(VOL_MEASURES)
    if unknown:
        raise ValueError(f'build_vol_state: unknown measure(s) {sorted(unknown)}; valid = {list(VOL_MEASURES)}')
    series: dict[str, np.ndarray] = {}
    with np.errstate(divide='ignore', invalid='ignore'):
        r = np.diff(np.log(close), prepend=close[:1])
    r[0] = 0.0
    r = np.where(np.isfinite(r), r, 0.0)
    r2 = r * r
    abs_r = np.abs(r)
    cs = max(1, int(candle_secs))
    bucket_idx = np.arange(n, dtype=np.int64) // cs
    n_buckets = int(bucket_idx[-1]) + 1
    if elapsed_adjust:
        usable = bucket_idx
    else:
        usable = bucket_idx - 1 - max(0, int(min_bars_since_close))
    usable = np.clip(usable, 0, n_buckets - 1)
    needed_rv: set[int] = set()
    for m in want:
        parts = m.split('_')
        if len(parts) >= 2 and parts[-1].endswith('s'):
            try:
                w = int(parts[-1].rstrip('s'))
            except ValueError:
                continue
            if w in _RV_WINDOWS and m.startswith('rv'):
                needed_rv.add(w)
    needed_rv = {w for w in needed_rv if w in _RV_WINDOWS}
    rv_base_minutes = max(4, int(rv_baseline_secs) // cs)
    need_any_share = any((m.startswith('rv_share_') for m in want))
    if need_any_share:
        rv_wide = _rolling_sum(r2, max(_RV_WINDOWS))
        series[f'rv_{max(_RV_WINDOWS)}s'] = rv_wide
    for w in sorted(needed_rv):
        series[f'rv_{w}s'] = _rolling_sum(r2, w)
    for w in sorted(needed_rv):
        name = f'rv_share_{w}s'
        if name not in want:
            continue
        series[name] = _safe_ratio(series[f'rv_{w}s'], rv_wide)
    if 'rv_ratio_30s' in want:
        rv30 = series['rv_30s']
        per_b = _per_bucket_median(rv30, bucket_idx, n_buckets)
        base = _rolling_median_baseline(per_b, rv_base_minutes)
        series['rv_ratio_30s'] = _safe_ratio(rv30, base[usable])
    if 'range_norm_1m' in want or 'wick_ratio' in want:
        b_starts, b_ends = _segment_bounds(bucket_idx, n_buckets)
        if np.any(np.diff(b_starts) <= 0):
            b_high = np.array([high[b_starts[k]:b_ends[k]].max() for k in range(n_buckets)])
            b_low = np.array([low[b_starts[k]:b_ends[k]].min() for k in range(n_buckets)])
        else:
            b_high = np.maximum.reduceat(high, b_starts)
            b_low = np.minimum.reduceat(low, b_starts)
        b_range = b_high - b_low
        if 'wick_ratio' in want:
            b_close = close[b_ends - 1]
            if open_ is not None:
                b_open = open_[b_starts]
            else:
                b_open = b_close
            upper = b_high - np.maximum(b_open, b_close)
            lower = np.minimum(b_open, b_close) - b_low
            span = np.where(b_range > 1e-12, b_range, np.nan)
            wr = np.maximum(upper, lower) / span
            wr = np.where(np.isfinite(wr), wr, 0.0)
            wr = np.clip(wr, 0.0, 1.0)
            series['wick_ratio'] = wr[usable]
        if 'range_norm_1m' in want:
            win_m = max(4, int(range_baseline_mins))
            base = _rolling_median_baseline(b_range, win_m)
            if elapsed_adjust:
                elapsed = np.arange(n, dtype=np.float64) - bucket_idx * cs + 1.0
                scale = np.sqrt(cs / np.maximum(elapsed, 1.0))
                series['range_norm_1m'] = _safe_ratio(b_range[bucket_idx] * scale, base[usable])
            else:
                series['range_norm_1m'] = _safe_ratio(b_range[usable], base[usable])
    for w in _VEL_WINDOWS:
        name = f'vel_{w // 60}m'
        if name not in want:
            continue
        if w >= n:
            series[name] = np.zeros(n, dtype=np.float64)
            continue
        back = close[:n - w]
        fwd = close[w:]
        vel = np.abs(fwd - back) / np.where(np.abs(back) > 1e-12, back, np.nan)
        full = np.concatenate((np.zeros(w, dtype=np.float64), vel))
        series[name] = np.where(np.isfinite(full), full, 0.0)
    if 'shock' in want:
        sb = max(4, int(shock_baseline_secs) // cs)
        per_b = _per_bucket_median(abs_r, bucket_idx, n_buckets)
        base = _rolling_median_baseline(per_b, sb)
        series['shock'] = _safe_ratio(abs_r, base[usable])
    if 'vol_expand_prev_day' in want:
        if times_ns is None:
            raise ValueError("build_vol_state: 'vol_expand_prev_day' requires times_ns (int64 epoch nanoseconds, ascending).")
        _ts = np.asarray(times_ns, dtype=np.int64)
        if _ts.shape[0] != n:
            raise ValueError(f'build_vol_state: times_ns length mismatch ({_ts.shape[0]} vs {n})')
        series['vol_expand_prev_day'] = _prev_day_expansion(r2, _ts)
    _vs = VolState(series=series, meta={'n_1s': int(n), 'measures': sorted(series.keys()), 'requested': sorted(want), 'candle_secs': cs, 'rv_baseline_secs': int(rv_baseline_secs), 'range_baseline_mins': int(range_baseline_mins), 'shock_baseline_secs': int(shock_baseline_secs), 'min_bars_since_close': int(min_bars_since_close), 'elapsed_adjust': bool(elapsed_adjust)})
    if _ck is not None:
        _CACHE[_ck] = _vs
        _CACHE.move_to_end(_ck)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return _vs
