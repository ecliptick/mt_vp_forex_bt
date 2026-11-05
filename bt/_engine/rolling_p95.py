from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple
import numpy as np
import pandas as pd
from bt._engine.timebase import assert_ns, to_ns_arr
STAT_P2T_WINDOW = 'p2t_window'
STAT_P2T_HOURLY = 'p2t_hourly'
P95_STATISTICS = (STAT_P2T_WINDOW, STAT_P2T_HOURLY)
P95_STATISTIC_CANONICAL = STAT_P2T_WINDOW

@dataclass
class RollingP95Cap:
    lookback_secs: int = 604800
    inner_secs: int = 3600
    p95_q: float = 95.0
    p95_mult: float = 0.0
    statistic: str = P95_STATISTIC_CANONICAL
    seed_path: str = ''
    seed_constant_usd: float = 0.0
    warmup_until_ns: int = 0
    skip_trades_during_warmup: bool = True
    buffer_t_ns: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    buffer_high: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    buffer_low: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    p95_ret: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))

    @property
    def enabled(self) -> bool:
        return self.p95_mult > 0.0

    def is_warmup_complete(self, t_ns: int) -> bool:
        if not self.enabled:
            return True
        return int(t_ns) >= int(self.warmup_until_ns)

    def cap_for(self, t_ns: int) -> float:
        if not self.enabled:
            return float('inf')
        if int(t_ns) < int(self.warmup_until_ns):
            if self.seed_constant_usd > 0.0:
                return float(self.seed_constant_usd) * float(self.p95_mult)
            return float('inf')
        if self.p95_ret.size == 0 or self.buffer_t_ns.size == 0:
            return float('inf')
        idx = int(np.searchsorted(self.buffer_t_ns, t_ns))
        if idx >= self.p95_ret.size:
            idx = self.p95_ret.size - 1
        v = float(self.p95_ret[idx])
        if not np.isfinite(v) or v <= 0.0:
            return float(self.seed_constant_usd) if self.seed_constant_usd > 0.0 else float('inf')
        return v * float(self.p95_mult)

def _rolling_peak_to_trough_fast(high: np.ndarray, low: np.ndarray, window: int) -> np.ndarray:
    n = len(high)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if window < 1 or window > n:
        window = n
    if window == 1:
        return high - low
    w_h = np.lib.stride_tricks.sliding_window_view(high, window)
    w_l = np.lib.stride_tricks.sliding_window_view(low, window)
    out = np.full(n, np.nan, dtype=np.float64)
    out[window - 1:] = w_h.max(axis=1) - w_l.min(axis=1)
    return out

def _rolling_peak_to_trough(high: np.ndarray, low: np.ndarray, window: int) -> np.ndarray:
    n = len(high)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if window < 1 or window > n:
        window = n
    from collections import deque
    max_dq: deque = deque()
    min_dq: deque = deque()
    for i in range(n):
        while max_dq and max_dq[0] <= i - window:
            max_dq.popleft()
        while min_dq and min_dq[0] <= i - window:
            min_dq.popleft()
        h = high[i]
        l = low[i]
        while max_dq and high[max_dq[-1]] <= h:
            max_dq.pop()
        max_dq.append(i)
        while min_dq and low[min_dq[-1]] >= l:
            min_dq.pop()
        min_dq.append(i)
        if i >= window - 1:
            win_high = high[max_dq[0]]
            win_low = low[min_dq[0]]
            out[i] = win_high - win_low
    return out

def _rolling_percentile(values: np.ndarray, window: int, q: float) -> np.ndarray:
    n = len(values)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if window < 1 or window > n:
        window = n
    if window == 1:
        out[:] = values
        return out
    windows = np.lib.stride_tricks.sliding_window_view(values, window_shape=window)
    nan_row = np.isnan(windows).any(axis=1)
    sorted_w = np.sort(windows, axis=1)
    idx = int(round(q / 100.0 * (window - 1)))
    p_vals = sorted_w[:, idx]
    p_vals[nan_row] = np.nan
    out[window - 1:] = p_vals
    return out

def _downsample_to_inner(high: np.ndarray, low: np.ndarray, times_ns: np.ndarray, inner_secs: int, first_t_ns: int) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    n = len(high)
    if n == 0:
        return (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64))
    inner_ns = int(inner_secs) * 1000000000
    bucket_idx = times_ns // inner_ns
    is_new = np.empty(len(bucket_idx), dtype=bool)
    is_new[0] = True
    np.not_equal(bucket_idx[1:], bucket_idx[:-1], out=is_new[1:])
    starts = np.flatnonzero(is_new)
    n_buckets = len(starts)
    if n_buckets == 0:
        return (np.zeros(0, dtype=np.int64), np.zeros(0, dtype=np.float64), np.zeros(0, dtype=np.float64))
    bucket_high = np.maximum.reduceat(high, starts)
    bucket_low = np.minimum.reduceat(low, starts)
    bucket_t_ns = bucket_idx[starts].astype(np.int64) * inner_ns
    return (bucket_t_ns, bucket_high, bucket_low)

def build_rolling_p95_from_bars(bars: pd.DataFrame, *, lookback_secs: int=604800, inner_secs: int=3600, p95_q: float=95.0, p95_mult: float=1.0, seed_path: str='constant', seed_constant_usd: float=0.0, statistic: str=STAT_P2T_WINDOW) -> RollingP95Cap:
    times_ns = to_ns_arr(bars, 'time')
    assert_ns(times_ns, 'rolling_p95 times')
    high = bars['high'].to_numpy(dtype=np.float64)
    low = bars['low'].to_numpy(dtype=np.float64)
    first_t_ns = int(times_ns[0]) if times_ns.size > 0 else 0
    sub_t, sub_h, sub_l = _downsample_to_inner(high, low, times_ns, inner_secs, first_t_ns)
    p95_window = max(2, int(lookback_secs // inner_secs))
    if statistic == STAT_P2T_WINDOW:
        inner_p2t = _rolling_peak_to_trough_fast(sub_h, sub_l, p95_window)
        p95_series = np.where(np.isnan(inner_p2t), np.inf, inner_p2t)
    elif statistic == STAT_P2T_HOURLY:
        inner_p2t = sub_h - sub_l
        p95_series = _rolling_percentile(inner_p2t, p95_window, p95_q)
        p95_series = np.where(np.isnan(p95_series), np.inf, p95_series)
    else:
        raise ValueError(f'statistic={statistic!r} is not one of {P95_STATISTICS!r}.')
    _ = float(p95_q)
    warmup_until_ns = 0
    if sub_t.size > 0:
        warmup_until_ns = int(sub_t[0]) + int((lookback_secs + inner_secs) * 1000000000)
    return RollingP95Cap(lookback_secs=lookback_secs, inner_secs=inner_secs, p95_q=p95_q, p95_mult=p95_mult, statistic=str(statistic), seed_path=seed_path, seed_constant_usd=seed_constant_usd, warmup_until_ns=warmup_until_ns, skip_trades_during_warmup=True, buffer_t_ns=sub_t, buffer_high=sub_h, buffer_low=sub_l, p95_ret=p95_series)

def make_live_rolling_p95(*, lookback_secs: int=604800, inner_secs: int=3600, p95_q: float=95.0, p95_mult: float=1.0, seed_path: str='constant', seed_constant_usd: float=0.0, max_inner_bars: int=4096, statistic: str=P95_STATISTIC_CANONICAL) -> RollingP95Cap:
    cap = RollingP95Cap(lookback_secs=lookback_secs, inner_secs=inner_secs, p95_q=p95_q, p95_mult=p95_mult, statistic=str(statistic), seed_path=seed_path, seed_constant_usd=seed_constant_usd, buffer_t_ns=np.zeros(max_inner_bars, dtype=np.int64), buffer_high=np.zeros(max_inner_bars, dtype=np.float64), buffer_low=np.zeros(max_inner_bars, dtype=np.float64), p95_ret=np.full(max_inner_bars, np.inf, dtype=np.float64))
    return cap

def update_p95(cap: RollingP95Cap, t_ns: int, high: float, low: float) -> None:
    if cap.buffer_t_ns.size == 0:
        return
    head = int(getattr(cap, '_head', 0))
    cap.buffer_t_ns[head] = t_ns
    cap.buffer_high[head] = high
    cap.buffer_low[head] = low
    head = (head + 1) % cap.buffer_t_ns.size
    object.__setattr__(cap, '_head', head)
    p95_window = max(2, int(cap.lookback_secs // cap.inner_secs))
    n = cap.buffer_t_ns.size
    tail_idx = (head - 1) % n
    win_h = np.empty(p95_window, dtype=np.float64)
    win_l = np.empty(p95_window, dtype=np.float64)
    for k in range(p95_window):
        idx = (head - 1 - k) % n
        win_h[k] = cap.buffer_high[idx]
        win_l[k] = cap.buffer_low[idx]
    statistic = getattr(cap, 'statistic', P95_STATISTIC_CANONICAL)
    if statistic == STAT_P2T_WINDOW:
        cap.p95_ret[tail_idx] = float(win_h.max() - win_l.min())
        return
    if statistic != STAT_P2T_HOURLY:
        raise ValueError(f'statistic={statistic!r} is not one of {P95_STATISTICS!r}.')
    arr = win_h - win_l
    arr = arr[np.isfinite(arr) & (arr > 0.0)]
    if arr.size > 0:
        cap.p95_ret[tail_idx] = float(np.percentile(arr, cap.p95_q))
    else:
        cap.p95_ret[tail_idx] = np.inf

def save_rolling_p95(cap: RollingP95Cap, cache_dir: Path) -> None:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez(cache_dir / 'rolling_p95.npz', buffer_t_ns=cap.buffer_t_ns, buffer_high=cap.buffer_high, buffer_low=cap.buffer_low, p95_ret=cap.p95_ret)
    (cache_dir / 'rolling_p95_meta.json').write_text(f'''{{"lookback_secs": {cap.lookback_secs}, "inner_secs": {cap.inner_secs}, "p95_q": {cap.p95_q}, "p95_mult": {cap.p95_mult}, "statistic": "{getattr(cap, 'statistic', P95_STATISTIC_CANONICAL)}", "seed_path": "{cap.seed_path}", "seed_constant_usd": {cap.seed_constant_usd}, "n_records": {int(cap.buffer_t_ns.size)}}}\n''')

def load_rolling_p95(cache_dir: Path) -> Optional[RollingP95Cap]:
    cache_dir = Path(cache_dir)
    npz_path = cache_dir / 'rolling_p95.npz'
    meta_path = cache_dir / 'rolling_p95_meta.json'
    if not npz_path.exists() or not meta_path.exists():
        return None
    import json as _json
    npz = np.load(npz_path)
    meta = _json.loads(meta_path.read_text())
    cap = RollingP95Cap(lookback_secs=int(meta['lookback_secs']), inner_secs=int(meta['inner_secs']), p95_q=float(meta['p95_q']), statistic=str(meta.get('statistic', P95_STATISTIC_CANONICAL)), p95_mult=float(meta['p95_mult']), seed_path=str(meta.get('seed_path', '')), seed_constant_usd=float(meta.get('seed_constant_usd', 0.0)), buffer_t_ns=npz['buffer_t_ns'], buffer_high=npz['buffer_high'], buffer_low=npz['buffer_low'], p95_ret=npz['p95_ret'])
    return cap
__all__ = ['RollingP95Cap', 'build_rolling_p95_from_bars', 'make_live_rolling_p95', 'update_p95', 'save_rolling_p95', 'load_rolling_p95', 'STAT_P2T_WINDOW', 'STAT_P2T_HOURLY', 'P95_STATISTICS', 'P95_STATISTIC_CANONICAL']
