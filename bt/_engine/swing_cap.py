from __future__ import annotations
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional
import numpy as np
import pandas as pd
from bt._engine.timebase import assert_ns, to_ns_arr

@dataclass
class DailySwingCap:
    lookback_secs: int = 86400
    cap_pct: float = 0.0
    buffer_t_ns: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    buffer_high: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    buffer_low: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    head: int = 0
    count: int = 0

    @property
    def enabled(self) -> bool:
        return self.cap_pct > 0.0 and self.count > 0

    def cap_for(self, t_ns: int) -> float:
        if not self.enabled:
            return float('inf')
        cutoff = t_ns - int(self.lookback_secs * 1000000000.0)
        if self.count < self.buffer_t_ns.size:
            ts = self.buffer_t_ns[:self.head]
            hs = self.buffer_high[:self.head]
            ls = self.buffer_low[:self.head]
        else:
            tail = np.arange(self.head, self.head + self.count) % self.count
            ts = self.buffer_t_ns[tail]
            hs = self.buffer_high[tail]
            ls = self.buffer_low[tail]
        lo = int(np.searchsorted(ts, cutoff, side='left'))
        hi = int(np.searchsorted(ts, t_ns, side='right'))
        if hi <= lo:
            return float('inf')
        win_high = float(hs[lo:hi].max())
        win_low = float(ls[lo:hi].min())
        swing = win_high - win_low
        return swing * float(self.cap_pct)

def build_daily_swing(bars: pd.DataFrame, *, lookback_secs: int=86400, cap_pct: float=0.5) -> DailySwingCap:
    times_ns = to_ns_arr(bars, 'time')
    assert_ns(times_ns, 'swing_cap times')
    high = bars['high'].to_numpy(dtype=np.float64)
    low = bars['low'].to_numpy(dtype=np.float64)
    target_records = 60
    cadence_secs = max(1, lookback_secs // target_records)
    cadence_ns = int(cadence_secs * 1000000000.0)
    if len(times_ns) > 0:
        first_t = int(times_ns[0])
        bucket = (times_ns - first_t) // cadence_ns
        df = pd.DataFrame({'bucket': bucket, 'high': high, 'low': low, 't': times_ns})
        agg = df.groupby('bucket', sort=True).agg(high=('high', 'max'), low=('low', 'min'), t=('t', 'min')).reset_index()
        sub_t = agg['t'].to_numpy(dtype=np.int64)
        sub_h = agg['high'].to_numpy(dtype=np.float64)
        sub_l = agg['low'].to_numpy(dtype=np.float64)
    else:
        sub_t = np.zeros(0, dtype=np.int64)
        sub_h = np.zeros(0, dtype=np.float64)
        sub_l = np.zeros(0, dtype=np.float64)
    cap = DailySwingCap(lookback_secs=lookback_secs, cap_pct=cap_pct, buffer_t_ns=sub_t, buffer_high=sub_h, buffer_low=sub_l, head=len(sub_t), count=len(sub_t))
    return cap

def save_daily_swing(cap: DailySwingCap, cache_dir: Path) -> None:
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez(cache_dir / 'daily_swing.npz', buffer_t_ns=cap.buffer_t_ns, buffer_high=cap.buffer_high, buffer_low=cap.buffer_low)
    (cache_dir / 'daily_swing_meta.json').write_text(f'{{"lookback_secs": {cap.lookback_secs}, "cap_pct": {cap.cap_pct}, "n_records": {cap.count}}}\n')

def load_daily_swing(cache_dir: Path) -> Optional[DailySwingCap]:
    cache_dir = Path(cache_dir)
    npz_path = cache_dir / 'daily_swing.npz'
    meta_path = cache_dir / 'daily_swing_meta.json'
    if not npz_path.exists() or not meta_path.exists():
        return None
    npz = np.load(npz_path)
    import json as _json
    meta = _json.loads(meta_path.read_text())
    return DailySwingCap(lookback_secs=int(meta['lookback_secs']), cap_pct=float(meta['cap_pct']), buffer_t_ns=npz['buffer_t_ns'], buffer_high=npz['buffer_high'], buffer_low=npz['buffer_low'], head=int(meta['n_records']), count=int(meta['n_records']))

def make_live_swing_cap(*, lookback_secs: int=86400, cap_pct: float=0.5, max_records: int=4096) -> DailySwingCap:
    return DailySwingCap(lookback_secs=lookback_secs, cap_pct=cap_pct, buffer_t_ns=np.zeros(max_records, dtype=np.int64), buffer_high=np.zeros(max_records, dtype=np.float64), buffer_low=np.zeros(max_records, dtype=np.float64), head=0, count=0)

def update_swing(cap: DailySwingCap, t_ns: int, high: float, low: float) -> None:
    if cap.buffer_t_ns.size == 0:
        return
    cap.buffer_t_ns[cap.head] = t_ns
    cap.buffer_high[cap.head] = high
    cap.buffer_low[cap.head] = low
    cap.head = (cap.head + 1) % cap.buffer_t_ns.size
    if cap.count < cap.buffer_t_ns.size:
        cap.count += 1
