from __future__ import annotations
import numpy as np
import pandas as pd
__all__ = ['to_ns_arr', 'to_ns_int', 'assert_ns']

def to_ns_int(value) -> int:
    if isinstance(value, (int, np.integer)):
        return int(value)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize('UTC')
    return int(ts.value)

def to_ns_arr(bars: pd.DataFrame, column: str='time') -> np.ndarray:
    if column not in bars.columns:
        raise ValueError(f'bars is missing the {column!r} column (have: {list(bars.columns)})')
    col = bars[column]
    if not pd.api.types.is_datetime64_any_dtype(col):
        raise ValueError(f'bars[{column!r}] must be datetime64, got {col.dtype}. Parse it with pd.to_datetime(..., utc=True) first.')
    if col.dtype.kind == 'M':
        try:
            return np.asarray(col.array.as_unit('ns').astype('int64'), dtype=np.int64)
        except AttributeError:
            return np.asarray(col.astype('datetime64[ns, UTC]').astype('int64'), dtype=np.int64)
    return np.asarray(col.to_numpy(), dtype=np.int64)

def assert_ns(arr: np.ndarray, label: str='timestamps') -> None:
    if arr.size == 0:
        return
    probe = int(arr[0])
    if not 1e+18 <= abs(probe) < 1e+19:
        raise ValueError(f'{label} does not look like epoch NANOSECONDS (first value {probe} = {probe / 1000000000000.0:.1f}e12). Use bt._engine.timebase.to_ns_arr() to convert.')
