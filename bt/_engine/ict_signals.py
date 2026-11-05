from __future__ import annotations
from dataclasses import dataclass, field
import numpy as np
from bt._engine.ict_strategy import PendingSignal

@dataclass
class IctSeries:
    trend: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int8))
    atr: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    n_structure_invalidations: int = 0
    n_ifvg_age_dropped: int = 0
    n_body_mitigations: int = 0
    n_body_inversions: int = 0
    n_inv_trades_submitted: int = 0
    n_inv_trades_filled: int = 0
    n_inv_trades_closed: int = 0
    ranker: RollingFvgRanker | None = None
import bisect
from collections import deque
from dataclasses import dataclass, field
import sys as _sys
import time as _time

@dataclass
class RollingFvgRanker:
    window: int = 20
    _bull: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    _bear: deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def reset(self, window: int=20) -> None:
        self.window = window
        self._bull = deque(maxlen=window)
        self._bear = deque(maxlen=window)

    def add(self, direction: int, anchor_price: float) -> None:
        if direction > 0:
            self._bull.append(float(anchor_price))
        else:
            self._bear.append(float(anchor_price))

    def percentile(self, direction: int, anchor_price: float) -> float:
        if direction > 0:
            buf = self._bull
        else:
            buf = self._bear
        n = len(buf)
        if n < 2:
            return 0.0
        prices = sorted(buf)
        n = len(prices)
        idx = bisect.bisect_left(prices, float(anchor_price))
        if direction > 0:
            pct = 1.0 - float(idx) / float(n - 1) if n > 1 else 0.0
        else:
            pct = float(idx) / float(n - 1) if n > 1 else 0.0
        return pct

    def clear(self) -> None:
        self._bull.clear()
        self._bear.clear()

@dataclass
class RenkoBars:
    brick_size_usd: float
    direction_per_bar: np.ndarray
    close_per_bar: np.ndarray
    brick_count_per_bar: np.ndarray
    n_bricks: int
    brick_open: np.ndarray
    brick_close: np.ndarray
    brick_direction: np.ndarray
    brick_start_bar: np.ndarray
    brick_end_bar: np.ndarray

def compute_renko_bars(close: np.ndarray, brick_size_usd: float) -> RenkoBars:
    n = close.shape[0]
    if brick_size_usd <= 0:
        raise ValueError(f'brick_size_usd must be > 0, got {brick_size_usd}')
    if n == 0:
        return RenkoBars(brick_size_usd=float(brick_size_usd), direction_per_bar=np.zeros(0, dtype=np.int8), close_per_bar=np.zeros(0, dtype=np.float64), brick_count_per_bar=np.zeros(0, dtype=np.int32), n_bricks=0, brick_open=np.zeros(0, dtype=np.float64), brick_close=np.zeros(0, dtype=np.float64), brick_direction=np.zeros(0, dtype=np.int8), brick_start_bar=np.zeros(0, dtype=np.int32), brick_end_bar=np.zeros(0, dtype=np.int32))
    direction_per_bar = np.zeros(n, dtype=np.int8)
    close_per_bar = np.zeros(n, dtype=np.float64)
    brick_count_per_bar = np.full(n, -1, dtype=np.int32)
    brick_open_list: list[float] = []
    brick_close_list: list[float] = []
    brick_dir_list: list[int] = []
    brick_start_list: list[int] = []
    brick_end_list: list[int] = []
    anchor = float(close[0])
    brick_close: float | None = None
    brick_dir: int = 0
    n_bricks = 0
    current_brick_start_bar = -1
    for i in range(n):
        c = float(close[i])
        if brick_close is None:
            close_per_bar[i] = anchor
            direction_per_bar[i] = 0
            brick_count_per_bar[i] = -1
            if c - anchor >= brick_size_usd:
                brick_dir = 1
                brick_close = anchor + brick_size_usd
                current_brick_start_bar = i
                n_bricks = 1
                brick_open_list.append(anchor)
                brick_close_list.append(brick_close)
                brick_dir_list.append(1)
                brick_start_list.append(i)
                brick_end_list.append(i)
            elif anchor - c >= brick_size_usd:
                brick_dir = -1
                brick_close = anchor - brick_size_usd
                current_brick_start_bar = i
                n_bricks = 1
                brick_open_list.append(anchor)
                brick_close_list.append(brick_close)
                brick_dir_list.append(-1)
                brick_start_list.append(i)
                brick_end_list.append(i)
            continue
        if brick_dir > 0:
            if c >= brick_close + brick_size_usd:
                brick_close = brick_close + brick_size_usd
                brick_open_list.append(brick_close - brick_size_usd)
                brick_close_list.append(brick_close)
                brick_dir_list.append(1)
                brick_start_list.append(i)
                brick_end_list.append(i)
                n_bricks += 1
            elif c <= brick_close - brick_size_usd:
                brick_dir = -1
                brick_close = brick_close - brick_size_usd
                brick_open_list.append(brick_close + brick_size_usd)
                brick_close_list.append(brick_close)
                brick_dir_list.append(-1)
                brick_start_list.append(i)
                brick_end_list.append(i)
                n_bricks += 1
                current_brick_start_bar = i
        elif c <= brick_close - brick_size_usd:
            brick_close = brick_close - brick_size_usd
            brick_open_list.append(brick_close + brick_size_usd)
            brick_close_list.append(brick_close)
            brick_dir_list.append(-1)
            brick_start_list.append(i)
            brick_end_list.append(i)
            n_bricks += 1
        elif c >= brick_close + brick_size_usd:
            brick_dir = 1
            brick_close = brick_close + brick_size_usd
            brick_open_list.append(brick_close - brick_size_usd)
            brick_close_list.append(brick_close)
            brick_dir_list.append(1)
            brick_start_list.append(i)
            brick_end_list.append(i)
            n_bricks += 1
            current_brick_start_bar = i
        close_per_bar[i] = brick_close
        direction_per_bar[i] = brick_dir
        brick_count_per_bar[i] = n_bricks - 1
        if n_bricks > 0:
            brick_end_list[-1] = i
    return RenkoBars(brick_size_usd=float(brick_size_usd), direction_per_bar=direction_per_bar, close_per_bar=close_per_bar, brick_count_per_bar=brick_count_per_bar, n_bricks=n_bricks, brick_open=np.asarray(brick_open_list, dtype=np.float64), brick_close=np.asarray(brick_close_list, dtype=np.float64), brick_direction=np.asarray(brick_dir_list, dtype=np.int8), brick_start_bar=np.asarray(brick_start_list, dtype=np.int32), brick_end_bar=np.asarray(brick_end_list, dtype=np.int32))

def _rolling_tier_label(pct: float, a_pct: float, c_pct: float) -> str:
    if not 0.0 <= pct <= 1.0:
        return ''
    if pct <= a_pct:
        return 'A'
    if pct >= 1.0 - c_pct:
        return 'C'
    return 'B'

def compute_simple_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int=1200) -> np.ndarray:
    n = close.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    out = np.zeros(n, dtype=np.float64)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    idx = np.arange(n)
    win_start = np.maximum(0, idx - length + 1)
    counts = (idx + 1 - win_start).astype(np.float64)
    out = (csum[idx + 1] - csum[win_start]) / counts
    return out

def compute_dynamic_wick_floor(close: np.ndarray, *, price_pct: float=0.00025, recompute_secs: int=3600, times_utc_ns: np.ndarray | None=None) -> np.ndarray:
    n = int(close.shape[0])
    out = np.zeros(n, dtype=np.float64)
    if n == 0 or price_pct <= 0.0:
        return out
    if times_utc_ns is not None and np.asarray(times_utc_ns).shape[0] == n:
        bucket_secs = np.asarray(times_utc_ns, dtype=np.int64) // 1000000000
        origin = int(bucket_secs[0])
        bucket_idx = (bucket_secs - origin) // int(recompute_secs)
    else:
        bucket_idx = np.arange(n, dtype=np.int64) // int(recompute_secs)
    n_buckets = int(bucket_idx.max()) + 1 if bucket_idx.size else 0
    if n_buckets == 0:
        return out
    bucket_medians = np.zeros(n_buckets, dtype=np.float64)
    for b in range(n_buckets):
        mask = bucket_idx == b
        if not mask.any():
            continue
        bucket_medians[b] = float(np.median(close[mask]))
    floor_per_bar = bucket_medians[bucket_idx] * float(price_pct)
    out[:] = np.where(np.isfinite(floor_per_bar) & (floor_per_bar > 0.0), floor_per_bar, 0.0)
    return out

@dataclass
class FvgZone:
    trigger_bar: int
    direction: int
    zone_low: float
    zone_high: float
    mitigated_bar: int = -1
    mitigated_depth_pct: float = 0.0
    pierced_bar: int = -1
    inverted_bar: int = -1
    inverted: bool = False
    consumed_bar: int = -1
    expired_bar: int = -1
    superseded_bar: int = -1
    played_out_bar: int = -1
    live: bool = True
    n_touches: int = 0

def mitigation_touch_price(zone_low: float, zone_high: float, direction: int, bar_low: float, bar_high: float, bar_close: float, position_in_bar: float=0.5) -> float | None:
    if position_in_bar < 0.0 or position_in_bar > 1.0:
        raise ValueError(f'position_in_bar must be in [0, 1], got {position_in_bar}')
    if bar_low > zone_high or bar_high < zone_low:
        return None
    if direction > 0:
        entry_edge = float(zone_high)
    else:
        entry_edge = float(zone_low)
    return entry_edge + (float(bar_close) - entry_edge) * position_in_bar

def _resample_bucket_index(times_utc_ns: np.ndarray, n: int, resample_to_n_secs: int) -> np.ndarray:
    if times_utc_ns is not None and len(times_utc_ns) >= n:
        _tns = np.asarray(times_utc_ns, dtype=np.int64)[:n]
        _t0 = int(_tns[0])
        return ((_tns - _t0) // int(resample_to_n_secs * 1000000000)).astype(np.int64)
    import warnings as _warnings
    _warnings.warn('detect_fvg: resample_to_n_secs>0 without times_utc_ns; falling back to POSITIONAL bucketing. This is wrong on any feed with gaps (weekends/rollovers) because row position drifts from wall-clock time.', RuntimeWarning, stacklevel=3)
    return (np.arange(n) // resample_to_n_secs).astype(np.int64)

def detect_fvg(open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, warmup: int=0, max_active_zones: int=0, resample_to_n_secs: int=0, fvg_min_zone_usd: float=0.0, max_zone_age_bars: int=0, fvg_displacement_ratio: float=0.0, fvg_body_definition: str='body', fvg_min_zone_atr_mult: float=0.0, atr_for_min_zone: float=0.0, supersede_on_new: bool=False, invalidation_min_pierce_usd: float=0.0, invalidation_min_consecutive_bars: int=1, require_retest_to_invert: bool=False, played_out_min_extension_usd: float=0.0, body_only_mitigation: bool=False, body_only_invalidation: bool=False, structure_events_per_bar: np.ndarray | None=None, structure_invalidation_age_secs: int=0, times_utc_ns: np.ndarray | None=None, fvg_min_lifetime_secs: int=0, strict_wick_required: bool=False, strict_wick_min_wick_usd: float=0.0, strict_wick_floor_usd_per_bar: np.ndarray | None=None) -> list[FvgZone]:
    n = close.shape[0]
    scan_low = low
    scan_high = high
    scan_open = open_
    scan_close = close
    scan_index_map: np.ndarray | None = None
    if resample_to_n_secs > 0 and n > 0:
        bucket = _resample_bucket_index(times_utc_ns, n, resample_to_n_secs)
        unique_buckets, bucket_starts = np.unique(bucket, return_index=True)
        bucket_ends = np.empty_like(bucket_starts)
        for ub_idx in range(len(unique_buckets) - 1, -1, -1):
            end = bucket_starts[ub_idx + 1] if ub_idx + 1 < len(unique_buckets) else n
            bucket_ends[ub_idx] = end - 1
        scan_open = open_[bucket_starts]
        scan_close = close_[bucket_ends] if False else close[bucket_ends]
        if len(unique_buckets) > 0:
            scan_high = np.maximum.reduceat(high, bucket_starts)
            scan_low = np.minimum.reduceat(low, bucket_starts)
        else:
            scan_high = np.zeros(0, dtype=np.float64)
            scan_low = np.zeros(0, dtype=np.float64)
        scan_index_map = bucket_ends
        n = len(unique_buckets)
    zones: list[FvgZone] = []
    _live_candidates: list[FvgZone] = []
    if n < 3:
        return zones

    def _evict_oldest() -> None:
        if max_active_zones <= 0 or len(_live_candidates) <= max_active_zones:
            return
        oldest_idx = min(range(len(_live_candidates)), key=lambda i: _live_candidates[i].trigger_bar)
        del _live_candidates[oldest_idx]
    _SUP_COMPACT_EVERY = 512
    _since_compact = 0
    _det_n = 0
    _det_t0 = _time.perf_counter()
    _SUP_BUCKET_USD = 5.0
    _sup_buckets: dict[int, list] = {}
    _start = max(2, warmup)
    _i = np.arange(_start, n, dtype=np.int64)
    _h2 = scan_high[_i - 2]
    _l0 = scan_low[_i]
    _h0 = scan_high[_i]
    _l2 = scan_low[_i - 2]
    _is_bull = _h2 < _l0
    _is_bear = _h0 < _l2
    _zl_bull, _zh_bull = (_h2, _l0)
    _zl_bear, _zh_bear = (_h0, _l2)
    _dir = np.where(_is_bull, 1, -1).astype(np.int8)
    _keep = _is_bull | _is_bear
    _zl = np.where(_is_bull, _zl_bull, _zl_bear)
    _zh = np.where(_is_bull, _zh_bull, _zh_bear)
    if fvg_displacement_ratio > 0.0:
        if fvg_body_definition == 'range':
            _c1 = scan_high[_i - 2] - scan_low[_i - 2]
            _c2 = scan_high[_i - 1] - scan_low[_i - 1]
            _c3 = scan_high[_i] - scan_low[_i]
        else:
            _c1 = np.abs(scan_close[_i - 2] - scan_open[_i - 2])
            _c2 = np.abs(scan_close[_i - 1] - scan_open[_i - 1])
            _c3 = np.abs(scan_close[_i] - scan_open[_i])
        _max_outer = np.maximum(_c1, _c3)
        _disp_pass = (_max_outer > 0.0) & (_c2 >= fvg_displacement_ratio * _max_outer)
        _keep &= _disp_pass
    if fvg_min_zone_usd > 0:
        _keep &= _zh - _zl >= fvg_min_zone_usd
    if fvg_min_zone_atr_mult > 0 and atr_for_min_zone > 0:
        _keep &= _zh - _zl >= fvg_min_zone_atr_mult * atr_for_min_zone
    if strict_wick_required:
        if strict_wick_floor_usd_per_bar is not None:
            _wfloor_arr = np.asarray(strict_wick_floor_usd_per_bar)
            if scan_index_map is not None:
                _wfloor = _wfloor_arr[np.clip(scan_index_map[_i], 0, _wfloor_arr.shape[0] - 1)]
            else:
                _wfloor = _wfloor_arr[np.clip(_i, 0, _wfloor_arr.shape[0] - 1)]
        else:
            _wfloor = np.full(_i.shape, float(strict_wick_min_wick_usd))
        _wfloor = np.where(np.isfinite(_wfloor), _wfloor, 0.0)
        _c1_body_lo = np.minimum(scan_open[_i - 2], scan_close[_i - 2])
        _c1_body_hi = np.maximum(scan_open[_i - 2], scan_close[_i - 2])
        _c3_body_lo = np.minimum(scan_open[_i], scan_close[_i])
        _c3_body_hi = np.maximum(scan_open[_i], scan_close[_i])
        _c1_wick_bull = _c1_body_lo - scan_low[_i - 2]
        _c3_wick_bull = scan_high[_i] - _c3_body_hi
        _c1_wick_bear = scan_high[_i - 2] - _c1_body_hi
        _c3_wick_bear = _c3_body_lo - scan_low[_i]
        _c1_wick = np.where(_is_bull, _c1_wick_bull, _c1_wick_bear)
        _c3_wick = np.where(_is_bull, _c3_wick_bull, _c3_wick_bear)
        _active_wick = _wfloor > 0.0
        if _active_wick.any():
            _keep &= ~_active_wick | (_c1_wick >= _wfloor) & (_c3_wick >= _wfloor)
    _cand = np.flatnonzero(_keep)
    for _ci in _cand:
        i = int(_i[_ci])
        direction = int(_dir[_ci])
        zl, zh = (float(_zl[_ci]), float(_zh[_ci]))
        trigger_bar_1s = int(scan_index_map[i]) if scan_index_map is not None else i
        new_zone = FvgZone(trigger_bar=trigger_bar_1s, direction=direction, zone_low=zl, zone_high=zh)
        _b_lo = int(new_zone.zone_low // _SUP_BUCKET_USD)
        _b_hi = int(new_zone.zone_high // _SUP_BUCKET_USD)
        if supersede_on_new:
            new_bar = new_zone.trigger_bar
            candidates = []
            for _b in range(_b_lo, _b_hi + 1):
                candidates.extend(_sup_buckets.get(_b, ()))
            for z in candidates:
                if not z.live:
                    continue
                if z.played_out_bar >= 0 and new_bar >= z.played_out_bar:
                    continue
                if z.expired_bar > 0 and new_bar >= z.expired_bar:
                    continue
                if z.superseded_bar >= 0:
                    continue
                if z.inverted_bar >= 0 and new_bar >= z.inverted_bar:
                    continue
                if z.zone_low <= new_zone.zone_high and new_zone.zone_low <= z.zone_high:
                    z.superseded_bar = new_bar
        zones.append(new_zone)
        _live_candidates.append(new_zone)
        for _b in range(_b_lo, _b_hi + 1):
            _sup_buckets.setdefault(_b, []).append(new_zone)
        _evict_oldest()
        _since_compact += 1
        if _since_compact >= _SUP_COMPACT_EVERY:
            _since_compact = 0
            _live_candidates[:] = [z for z in _live_candidates if z.live]
        _det_n += 1
        if _det_n % 50000 == 0:
            _el = _time.perf_counter() - _det_t0
            _sys.stderr.write(f'[detect_fvg] detect: zones={_det_n} elapsed={_el:.1f}s rate={_det_n / _el:.0f}/s live_cands={len(_live_candidates)}\n')
            _sys.stderr.flush()
    n_1s = close.shape[0]
    _zonestep_t0 = _time.perf_counter()
    _live_zone_count = sum((1 for zz in zones if zz.live and zz.superseded_bar < 0))
    for z_idx, z in enumerate(zones):
        if not z.live:
            continue
        if max_zone_age_bars > 0:
            rs = int(resample_to_n_secs) if resample_to_n_secs > 0 else 1
            z.expired_bar = z.trigger_bar + max_zone_age_bars * rs
        deepest_pct = 0.0
        deepest_bar = -1
        _cap = int(max_zone_age_bars) * (int(resample_to_n_secs) or 1) if max_zone_age_bars > 0 else 3600
        start_bar = z.trigger_bar + 1
        end_bar = z.expired_bar if z.expired_bar > 0 else min(n_1s, start_bar + _cap)
        if z.superseded_bar >= 0:
            end_bar = min(end_bar, z.superseded_bar + 1)
        if start_bar < end_bar:
            if scan_index_map is not None:
                _agg_start = int(np.searchsorted(scan_index_map, start_bar, side='right'))
                _agg_end = int(np.searchsorted(scan_index_map, end_bar, side='right'))
                _agg_start = max(0, min(_agg_start, scan_index_map.size))
                _agg_end = max(_agg_start, min(_agg_end, scan_index_map.size))
                _walk_close = scan_close
                _walk_high = scan_high
                _walk_low = scan_low
                _walk_open = scan_open
                _walk_from = _agg_start
                _walk_to = _agg_end
            else:
                _walk_close = close
                _walk_high = high
                _walk_low = low
                _walk_open = open_
                _walk_from = start_bar
                _walk_to = end_bar
            if _walk_from < _walk_to:
                sl = slice(_walk_from, _walk_to)
                seg_close = _walk_close[sl]
                seg_high = _walk_high[sl]
                seg_low = _walk_low[sl]
                seg_open = _walk_open[sl]
                if scan_index_map is not None:

                    def _to_1s(_local: int) -> int:
                        _k = _walk_from + _local
                        if _k < 0:
                            _k = 0
                        elif _k >= scan_index_map.size:
                            _k = scan_index_map.size - 1
                        return int(scan_index_map[_k])
                else:

                    def _to_1s(_local: int) -> int:
                        return int(_walk_from + _local)
            zl = float(z.zone_low)
            zh = float(z.zone_high)
            zone_h = zh - zl
            if zone_h > 0:
                overlap = np.clip(np.minimum(seg_high, zh) - np.maximum(seg_low, zl), 0.0, None)
                seg_depth_pct = overlap / zone_h
            else:
                seg_depth_pct = np.zeros(end_bar - start_bar, dtype=np.float64)
            in_zone_close = (seg_close >= zl) & (seg_close <= zh)
            if body_only_mitigation:
                if z.direction == 1:
                    body_crossed = (seg_open <= zl) & (seg_close >= zl) | (seg_open >= zl) & (seg_close <= zl) | (seg_close >= zl) & (seg_open >= zl) & ((seg_open <= zh) | (seg_close <= zh))
                else:
                    body_crossed = (seg_open >= zh) & (seg_close <= zh) | (seg_open <= zh) & (seg_close >= zh) | (seg_close <= zh) & (seg_open <= zh) & ((seg_open >= zl) | (seg_close >= zl))
                mit_mask = body_crossed
            else:
                mit_mask = in_zone_close
            mit_idx_local = -1
            if mit_mask.any():
                mit_idx_local = int(np.argmax(mit_mask))
            if seg_depth_pct.size > 0:
                deepest_local = int(np.argmax(seg_depth_pct))
                deepest_pct_val = float(seg_depth_pct[deepest_local])
                if deepest_pct_val > deepest_pct:
                    deepest_pct = deepest_pct_val
                    deepest_bar = _to_1s(deepest_local)
            if zone_h > 0:
                range_overlaps = (seg_high >= zl) & (seg_low <= zh)
                z.n_touches = int(np.count_nonzero(range_overlaps))
            pierce = float(invalidation_min_pierce_usd)
            if body_only_invalidation:
                if z.direction == 1:
                    pierce_mask = (seg_open <= zl - pierce) & (seg_close <= zl - pierce)
                else:
                    pierce_mask = (seg_open >= zh + pierce) & (seg_close >= zh + pierce)
            elif z.direction == 1:
                pierce_mask = seg_close < zl - pierce
            else:
                pierce_mask = seg_close > zh + pierce
            needed = max(1, int(invalidation_min_consecutive_bars))
            pierced_bar_local = -1
            inverted_bar_local = -1
            if pierce_mask.any():
                false_idx = np.where(~pierce_mask)[0]
                k = np.arange(pierce_mask.size, dtype=np.int64)
                if false_idx.size == 0:
                    streak_at = (k + 1).astype(np.int32)
                else:
                    count_le_k = np.searchsorted(false_idx, k, side='left')
                    has_false_le = count_le_k > 0
                    last_false_idx = np.where(has_false_le, false_idx[np.maximum(count_le_k - 1, 0)], -1)
                    streak_at = (k - last_false_idx).astype(np.int32)
                qual = (streak_at >= needed) & pierce_mask
                if qual.any():
                    streak_complete_local = int(np.argmax(qual))
                    pierced_bar_local = max(0, streak_complete_local - needed + 1)
                    if require_retest_to_invert:
                        if z.direction == 1:
                            retest_mask = seg_close > zh
                        else:
                            retest_mask = seg_close < zl
                        retest_search_start = streak_complete_local + 1
                        if retest_search_start < retest_mask.size:
                            retest_seg = retest_mask[retest_search_start:]
                            if retest_seg.any():
                                inverted_bar_local = retest_search_start + int(np.argmax(retest_seg))
                    else:
                        inverted_bar_local = streak_complete_local
            played_out_bar_local = -1
            ext = float(played_out_min_extension_usd)
            if ext > 0:
                if z.direction == 1:
                    po_mask = seg_close > zh + ext
                else:
                    po_mask = seg_close < zl - ext
                if po_mask.any():
                    played_out_bar_local = int(np.argmax(po_mask))
            if mit_idx_local >= 0:
                z.mitigated_bar = _to_1s(mit_idx_local)
            if pierced_bar_local >= 0:
                z.pierced_bar = _to_1s(pierced_bar_local)
            if inverted_bar_local >= 0:
                z.inverted_bar = _to_1s(inverted_bar_local)
                z.inverted = True
            if played_out_bar_local >= 0:
                z.played_out_bar = _to_1s(played_out_bar_local)
                z.live = False
        z.mitigated_depth_pct = float(deepest_pct)
        if z.mitigated_bar < 0 and deepest_pct >= 0.5 and (deepest_bar >= 0):
            z.mitigated_bar = int(deepest_bar)
        if z_idx and z_idx % 50000 == 0:
            _elapsed = _time.perf_counter() - _zonestep_t0
            _rate = z_idx / _elapsed if _elapsed > 0 else 0.0
            _live_so_far = _live_zone_count
            _sys.stderr.write(f'[detect_fvg] zones={z_idx}/{len(zones)} elapsed={_elapsed:.1f}s rate={_rate:.0f}/s live={_live_so_far}\n')
            _sys.stderr.flush()
        if z.live and max_zone_age_bars > 0 and (z.expired_bar > 0) and (z.expired_bar < n):
            pass
    for _z in zones:
        if _z.superseded_bar < 0:
            continue
        if _z.inverted_bar >= 0 and _z.inverted_bar <= _z.superseded_bar:
            _z.live = True
        else:
            _z.live = False
    if structure_events_per_bar is not None and len(structure_events_per_bar) >= n_1s:
        age_cap_1s = 0
        if structure_invalidation_age_secs > 0 and times_utc_ns is not None and (len(times_utc_ns) >= n_1s):
            age_cap_1s = max(1, int(structure_invalidation_age_secs))
        n_buckets = max(1, len(zones) // 5000)
        bucket_edges = np.linspace(0, n_1s, n_buckets + 1, dtype=np.int64)
        buckets: list[list[tuple[int, FvgZone]]] = [[] for _ in range(n_buckets)]
        for zi, z in enumerate(zones):
            bi = int(z.trigger_bar * n_buckets // n_1s)
            bi = max(0, min(bi, n_buckets - 1))
            buckets[bi].append((zi, z))
        event_bar_indices = np.where(structure_events_per_bar[:n_1s] != 0)[0]
        for ev_bar in event_bar_indices:
            ev = int(structure_events_per_bar[ev_bar])
            invalidates_bull_fvg = ev < 0
            invalidates_bear_fvg = ev > 0 and ev in (1, 2)
            if not (invalidates_bull_fvg or invalidates_bear_fvg):
                continue
            bi_end = int(ev_bar * n_buckets // n_1s)
            if bi_end <= 0:
                continue
            bi_end = min(bi_end, n_buckets - 1)
            for bi in range(bi_end + 1):
                for zi, z in buckets[bi]:
                    if not z.live:
                        continue
                    tb = z.trigger_bar
                    if tb >= ev_bar:
                        continue
                    if invalidates_bull_fvg and z.direction != 1:
                        continue
                    if invalidates_bear_fvg and z.direction != -1:
                        continue
                    if age_cap_1s > 0 and times_utc_ns is not None:
                        if tb < 1:
                            continue
                        trig_ns = int(times_utc_ns[tb])
                        ev_ns = int(times_utc_ns[ev_bar])
                        age_s = (ev_ns - trig_ns) / 1000000000.0
                        if age_s > structure_invalidation_age_secs:
                            continue
                    z.inverted_bar = ev_bar
                    z.inverted = True
                    z.live = False
    if fvg_min_lifetime_secs > 0:
        kept: list[FvgZone] = []
        n_dropped_lifetime = 0
        for z in zones:
            candidates: list[int] = []
            if z.mitigated_bar >= 0:
                candidates.append(z.mitigated_bar)
            if z.inverted_bar >= 0:
                candidates.append(z.inverted_bar)
            if z.superseded_bar >= 0:
                candidates.append(z.superseded_bar)
            if z.played_out_bar >= 0:
                candidates.append(z.played_out_bar)
            if not candidates:
                kept.append(z)
                continue
            first_end_bar = min(candidates)
            delta_bars = first_end_bar - int(z.trigger_bar)
            if delta_bars < fvg_min_lifetime_secs:
                n_dropped_lifetime += 1
            else:
                kept.append(z)
        zones = kept
        _last_lifetime_dropped = n_dropped_lifetime
    return zones

def fvg_retest_signals(zones: list[FvgZone], close: np.ndarray, high: np.ndarray, low: np.ndarray, *, require_unmitigated: bool=False, only_inverted: bool=False, max_age_bars: int=5000, min_mitigation_pct: float=0.0, skip_inverted: bool=False, skip_non_inverted: bool=False) -> list[tuple[int, int, FvgZone]]:
    out: list[tuple[int, int, FvgZone]] = []
    n = close.shape[0]
    for z in zones:
        if not z.live:
            continue
        if only_inverted and (not z.inverted):
            continue
        if skip_inverted and z.inverted:
            continue
        if skip_non_inverted and (not z.inverted):
            continue
        if require_unmitigated and z.mitigated_bar >= 0 and (not z.inverted):
            continue
        if min_mitigation_pct > 0.0:
            if z.inverted:
                pass
            elif z.mitigated_depth_pct < min_mitigation_pct:
                z.live = False
                continue
        if z.inverted:
            anchor = z.inverted_bar if z.inverted_bar >= 0 else z.trigger_bar
            start = anchor + 1
        elif z.mitigated_bar >= 0:
            start = z.mitigated_bar + 1
        else:
            start = z.trigger_bar + 1
        expiry_cap = z.expired_bar if z.expired_bar > 0 else n
        if max_age_bars > 0:
            end = min(n, min(start + max_age_bars, expiry_cap))
        else:
            end = min(n, expiry_cap)
        if start >= end:
            z.live = False
            continue
        for j in range(start, end):
            if z.expired_bar > 0 and j >= z.expired_bar:
                z.live = False
                break
            if low[j] <= z.zone_high and high[j] >= z.zone_low:
                d = -z.direction if z.inverted else z.direction
                z.consumed_bar = j
                z.live = False
                out.append((j, d, z))
                break
    return out

@dataclass(frozen=True)
class OrbSession:
    session_start_ns: int
    or_high: float
    or_low: float
    or_end_ns: int
    broke_high_bar: int = -1
    broke_low_bar: int = -1
    bull_break: bool = False
    bear_break: bool = False

def detect_orb(times_utc_ns: np.ndarray, open_: np.ndarray, high: np.ndarray, low: np.ndarray, close: np.ndarray, session_start_hour_utc: int, or_duration_mins: int=15) -> list[OrbSession]:
    NS_PER_MIN = 60000000000
    NS_PER_HOUR = 3600000000000
    NS_PER_DAY = 86400000000000
    window_ns = or_duration_mins * NS_PER_MIN
    session_start_offset_ns = session_start_hour_utc * NS_PER_HOUR
    n = times_utc_ns.shape[0]
    if n == 0:
        return []
    days = (times_utc_ns // NS_PER_DAY).astype(np.int64)
    out: list[OrbSession] = []
    unique_days = np.unique(days)
    for d in unique_days:
        day_start_ns = int(d) * NS_PER_DAY
        or_start_ns = day_start_ns + session_start_offset_ns
        or_end_ns = or_start_ns + window_ns
        day_end_ns = day_start_ns + NS_PER_DAY
        or_mask = (times_utc_ns >= or_start_ns) & (times_utc_ns < or_end_ns)
        if not or_mask.any():
            continue
        or_high = float(high[or_mask].max())
        or_low = float(low[or_mask].min())
        broke_high_bar = -1
        broke_low_bar = -1
        bull_break = False
        bear_break = False
        post_mask = (times_utc_ns >= or_end_ns) & (times_utc_ns < day_end_ns)
        post_idx = np.where(post_mask)[0]
        for j in post_idx:
            c = close[j]
            if broke_high_bar < 0 and c > or_high:
                broke_high_bar = j
                bull_break = True
            if broke_low_bar < 0 and c < or_low:
                broke_low_bar = j
                bear_break = True
            if bull_break and bear_break:
                break
        out.append(OrbSession(session_start_ns=or_start_ns, or_high=or_high, or_low=or_low, or_end_ns=or_end_ns, broke_high_bar=broke_high_bar, broke_low_bar=broke_low_bar, bull_break=bull_break, bear_break=bear_break))
    return out

def orb_signals(sessions: list[OrbSession], *, require_both_breaks: bool=False) -> list[tuple[int, int, OrbSession]]:
    out: list[tuple[int, int, OrbSession]] = []
    for s in sessions:
        if require_both_breaks and (not (s.bull_break and s.bear_break)):
            continue
        if s.bull_break and s.broke_high_bar >= 0:
            out.append((s.broke_high_bar, 1, s))
        if s.bear_break and s.broke_low_bar >= 0:
            out.append((s.broke_low_bar, -1, s))
    return out

@dataclass(frozen=True)
class WyckoffState:
    is_in_range: bool
    is_accumulating: bool
    is_distributing: bool
    is_spring: bool
    is_utad: bool
    range_low: float = np.nan
    range_high: float = np.nan

def _rolling_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    out = np.zeros_like(tr, dtype=np.float64)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    idx = np.arange(tr.shape[0])
    win_start = np.maximum(0, idx - n + 1)
    counts = (idx + 1 - win_start).astype(np.float64)
    out = (csum[idx + 1] - csum[win_start]) / counts
    return out

def detect_wyckoff(high: np.ndarray, low: np.ndarray, close: np.ndarray, ict: IctSeries, *, atr_fast: int=20, atr_slow: int=100, squeeze_ratio: float=0.7, range_lookback: int=100, trend_lookback: int=100, spring_break_usd: float=0.05) -> list[WyckoffState]:
    n = close.shape[0]
    out: list[WyckoffState] = []
    if n == 0:
        return out
    atr_f = _rolling_atr(high, low, close, atr_fast)
    atr_s = _rolling_atr(high, low, close, atr_slow)
    is_squeeze = atr_f < squeeze_ratio * atr_s
    range_high = np.full(n, np.nan)
    range_low = np.full(n, np.nan)
    for i in range(range_lookback, n):
        window_high = high[i - range_lookback:i].max()
        window_low = low[i - range_lookback:i].min()
        range_high[i] = window_high
        range_low[i] = window_low
    prior_trend = ict.trend.astype(np.int8) if hasattr(ict, 'trend') and len(ict.trend) == n else np.zeros(n, dtype=np.int8)
    is_long_trend = np.zeros(n, dtype=np.bool_)
    is_short_trend = np.zeros(n, dtype=np.bool_)
    for i in range(trend_lookback, n):
        recent = prior_trend[i - trend_lookback:i]
        is_long_trend[i] = bool(np.all(recent == 1))
        is_short_trend[i] = bool(np.all(recent == -1))
    for i in range(n):
        in_range = bool(is_squeeze[i])
        accum = in_range and bool(is_short_trend[i])
        distrib = in_range and bool(is_long_trend[i])
        spring = False
        utad = False
        if i >= range_lookback:
            rh = range_high[i]
            rl = range_low[i]
            if np.isfinite(rh) and np.isfinite(rl):
                if low[i] < rl - spring_break_usd and close[i] > rl:
                    spring = True
                if high[i] > rh + spring_break_usd and close[i] < rh:
                    utad = True
        out.append(WyckoffState(is_in_range=in_range, is_accumulating=accum, is_distributing=distrib, is_spring=spring, is_utad=utad, range_low=float(range_low[i]) if np.isfinite(range_low[i]) else np.nan, range_high=float(range_high[i]) if np.isfinite(range_high[i]) else np.nan))
    return out

@dataclass
class IctSignal:
    trigger_bar: int
    direction: int
    source: str
    stop_usd: float
    target_usd: float
    confidence: float = 1.0
    metadata: dict = None
__all__ = ['FvgZone', 'detect_fvg', 'fvg_retest_signals', 'OrbSession', 'detect_orb', 'orb_signals', 'WyckoffState', 'detect_wyckoff', 'RenkoBars', 'compute_renko_bars', 'IctSignal']

def generate_ict_pending_signals(close: np.ndarray, open_: np.ndarray, high: np.ndarray, low: np.ndarray, times_utc_ns: np.ndarray, ict: IctSeries, p, src: str | None=None, precomputed_zones: list | None=None) -> list:
    from bt._engine.ict_strategy import PendingSignal
    src = (src or str(p.signal_source)).lower()
    if src == 'gmma':
        return []
    out: list[PendingSignal] = []
    bias_gate = bool(getattr(p, 'gate_on_gmma_bias', False))
    sl_usd = float(p.sl_usd)
    tp_usd = float(p.tp_usd)
    if bool(getattr(p, 'use_atr_scaling', False)) and ict.atr.shape[0] > 0:
        atr_v = float(ict.atr[-1]) if ict.atr[-1] > 0 else 0.0
        if atr_v > 0:
            sl_usd = float(p.sl_atr_mult) * atr_v
            tp_usd = float(p.tp_atr_mult) * atr_v
    structure_state = None
    use_ms = bool(getattr(p, 'use_market_structure', False))
    src_apply_ms = {'fvg': bool(getattr(p, 'ms_apply_to_fvg', True)), 'ifvg': bool(getattr(p, 'ms_apply_to_ifvg', True)), 'orb': bool(getattr(p, 'ms_apply_to_orb', True)), 'wyckoff': bool(getattr(p, 'ms_apply_to_wyckoff', True))}
    ms_min_conv = float(getattr(p, 'ms_min_conviction', 0.0))
    ms_boost = float(getattr(p, 'ms_boost_conviction', 1.0))
    ms_max_boost_age = int(getattr(p, 'ms_max_boost_age_bars', 60))
    ms_choch_caution_age = int(getattr(p, 'ms_choch_caution_age_bars', 60))
    if use_ms:
        from .market_structure import structure_conviction
        existing_struct = getattr(ict, '_structure_state', None) if ict is not None else None
        if existing_struct is not None:
            structure_state = existing_struct
        else:
            from .market_structure import detect_market_structure
            structure_state = detect_market_structure(high, low, close, pivot_len=int(getattr(p, 'ms_pivot_len', 9)), liquidity_len=int(getattr(p, 'ms_liquidity_len', 30)), detect_order_blocks=bool(getattr(p, 'ms_draw_order_blocks', True)), detect_liquidity=bool(getattr(p, 'ms_draw_liquidity_sweeps', True)))

    def _bias(i: int) -> int:
        if i < 0 or i >= len(ict.trend):
            return 0
        return int(ict.trend[i])
    if src in ('fvg', 'ifvg'):
        only_inv = src == 'ifvg'
        rs = int(p.ifvg_resample_secs if only_inv else p.fvg_resample_secs)
        min_z = float(p.ifvg_min_zone_usd if only_inv else p.fvg_min_zone_usd)
        max_zones = int(p.ifvg_max_cache_size if only_inv else p.fvg_max_cache_size)
        age_secs = int(p.fvg_max_age_secs if not only_inv else p.ifvg_max_age_secs)
        age_bars_legacy = int(p.fvg_max_age_bars if not only_inv else p.ifvg_max_age_bars)
        if age_secs > 0:
            per_bar_secs = rs if rs > 0 else 1
            detector_max_age = max(1, age_secs // per_bar_secs)
        elif age_bars_legacy > 0:
            detector_max_age = age_bars_legacy
        else:
            detector_max_age = 0
        structure_events_for_detector = None
        if bool(getattr(p, 'fvg_invalidate_on_structure', False)):
            from .market_structure import detect_market_structure
            struct_state_for_inv = detect_market_structure(high, low, close, pivot_len=int(getattr(p, 'ms_pivot_len', 9)), liquidity_len=int(getattr(p, 'ms_liquidity_len', 30)), detect_order_blocks=False, detect_liquidity=False, resample_to_n_secs=int(getattr(p, 'ms_resample_secs', 0)))
            structure_events_for_detector = struct_state_for_inv.events
        if precomputed_zones is not None:
            import copy as _copy
            zones = [_copy.copy(z) for z in precomputed_zones]
        else:
            zones = detect_fvg(open_, high, low, close, resample_to_n_secs=rs if rs > 0 else 0, fvg_min_zone_usd=min_z, max_active_zones=max_zones, max_zone_age_bars=detector_max_age, fvg_displacement_ratio=float(p.fvg_displacement_ratio), fvg_body_definition=str(p.fvg_body_definition), fvg_min_zone_atr_mult=float(getattr(p, 'fvg_min_zone_atr_mult', 0.0)), atr_for_min_zone=float(getattr(p, 'fvg_min_zone_atr', 0.0)), supersede_on_new=bool(getattr(p, 'fvg_supersede_on_new', True)), invalidation_min_pierce_usd=float(getattr(p, 'fvg_invalidation_min_pierce_usd', 0.0)), invalidation_min_consecutive_bars=int(getattr(p, 'fvg_invalidation_min_consecutive_bars', 1)), require_retest_to_invert=bool(getattr(p, 'fvg_require_retest_to_invert', True)), played_out_min_extension_usd=float(getattr(p, 'played_out_min_extension_usd', 0.0)), body_only_mitigation=bool(getattr(p, 'fvg_body_only_mitigation', False)), body_only_invalidation=bool(getattr(p, 'fvg_body_only_invalidation', False)), structure_events_per_bar=structure_events_for_detector, structure_invalidation_age_secs=int(getattr(p, 'fvg_structure_invalidation_age_secs', 0)), times_utc_ns=times_utc_ns, fvg_min_lifetime_secs=int(getattr(p, 'fvg_min_lifetime_secs', 0)))
        struct_inv = 0
        for _z in zones:
            if _z.inverted and _z.inverted_bar >= 0 and (_z.pierced_bar < 0):
                struct_inv += 1
        ict.n_structure_invalidations += struct_inv
        if bool(getattr(p, 'fvg_body_only_mitigation', False)):
            ict.n_body_mitigations += sum((1 for _z in zones if _z.mitigated_bar >= 0))
        if bool(getattr(p, 'fvg_body_only_invalidation', False)):
            ict.n_body_inversions += sum((1 for _z in zones if _z.inverted and _z.inverted_bar >= 0))
        rolling_enabled = bool(getattr(p, 'fvg_rolling_treatment_enabled', False))
        if rolling_enabled:
            rolling_window = max(2, int(getattr(p, 'fvg_rolling_window_n', 20)))
            if ict.ranker is None:
                ict.ranker = RollingFvgRanker(window=rolling_window)
            for _z in zones:
                if _z.direction > 0:
                    _anchor = float(_z.zone_high)
                else:
                    _anchor = float(_z.zone_low)
                ict.ranker.add(_z.direction, _anchor)
        retest_secs = int(p.fvg_max_age_secs if not only_inv else p.ifvg_max_age_secs)
        retest_bars_legacy = int(p.fvg_max_age_bars if not only_inv else p.ifvg_max_age_bars)
        if retest_secs > 0:
            per_bar_secs = rs if rs > 0 else 1
            max_age = max(1, retest_secs // per_bar_secs)
        elif retest_bars_legacy > 0:
            max_age = retest_bars_legacy
        else:
            max_age = 5000
        retest_lookback = int(p.fvg_retest_lookback)
        if src in ('fvg', 'ifvg') and bool(getattr(p, 'fvg_sweep_enabled', False)):
            sweep_distance = float(getattr(p, 'fvg_sweep_distance_usd', 0.1))
            sweep_min_zone = float(getattr(p, 'fvg_sweep_min_zone_usd', 0.2))
            sweep_min_dist = float(getattr(p, 'fvg_sweep_min_distance_from_zone', 0.05))
            sweep_atr_mult = float(getattr(p, 'fvg_sweep_atr_mult', 0.5))
            atr_now_for_sweep = float(ict.atr[-1]) if ict.atr.shape[0] > 0 and ict.atr[-1] > 0 else 0.0
            for z in zones:
                if not z.live:
                    continue
                if z.played_out_bar >= 0 or z.expired_bar > 0:
                    continue
                z_width = float(z.zone_high - z.zone_low)
                if z_width < sweep_min_zone:
                    continue
                if sweep_min_dist > 0 and sweep_distance < sweep_min_dist:
                    continue
                if z.direction > 0:
                    sweep_price = float(z.zone_low) - sweep_distance
                    sweep_dir = 1
                    sl_price = float(z.zone_low) - sweep_atr_mult * atr_now_for_sweep if atr_now_for_sweep > 0 else float(z.zone_low) - 0.5
                    tp_price = float(z.zone_high)
                else:
                    sweep_price = float(z.zone_high) + sweep_distance
                    sweep_dir = -1
                    sl_price = float(z.zone_high) + sweep_atr_mult * atr_now_for_sweep if atr_now_for_sweep > 0 else float(z.zone_high) + 0.5
                    tp_price = float(z.zone_low)
                trigger_bar = min(z.trigger_bar + 1, close.shape[0] - 1)
                if bias_gate and _bias(trigger_bar) != 0:
                    effective_dir = _bias(trigger_bar)
                    if only_inv and getattr(p, 'ifvg_reverses_bias', True):
                        effective_dir = -effective_dir
                    if sweep_dir != effective_dir:
                        continue
                scaled_sl = abs(sweep_price - sl_price)
                scaled_tp = abs(tp_price - sweep_price)
                scaled_sl = max(scaled_sl, float(p.dynamic_sl_floor_usd))
                scaled_tp = max(scaled_tp, float(p.dynamic_tp_floor_usd))
                conviction = 1.0
                if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                    conviction = structure_conviction(structure_state, trigger_bar, sweep_dir, bos_boost_max_age_bars=ms_max_boost_age, choch_plus_boost_max_age_bars=ms_max_boost_age, choch_caution_max_age_bars=ms_choch_caution_age)
                    if conviction < ms_min_conv:
                        continue
                    if conviction > 1.0 and ms_boost > 1.0:
                        scaled_tp = scaled_tp * (1.0 + (ms_boost - 1.0) * (conviction - 1.0) / 0.5)
                if rolling_enabled and ict.ranker is not None:
                    _sweep_anchor = float(z.zone_low) if sweep_dir > 0 else float(z.zone_high)
                    rank_pct_s = ict.ranker.percentile(sweep_dir, _sweep_anchor)
                    rank_tier_s = _rolling_tier_label(rank_pct_s, float(getattr(p, 'fvg_rolling_a_pct', 0.2)), float(getattr(p, 'fvg_rolling_c_pct', 0.2)))
                else:
                    rank_tier_s = ''
                    rank_pct_s = 0.0
                out.append(PendingSignal(trigger_bar=trigger_bar, direction=sweep_dir, triggered_by='sweep', stop_usd=scaled_sl, target_usd=scaled_tp, conviction=conviction, fvg_zone=z if src in ('fvg', 'ifvg') else None, is_ifvg=src == 'ifvg', rank_tier=rank_tier_s, rank_percentile=rank_pct_s, fill_price=float(sweep_price)))
        retests = fvg_retest_signals(zones, close, high, low, only_inverted=only_inv, max_age_bars=max_age, min_mitigation_pct=float(getattr(p, 'fvg_min_mitigation_pct', 0.0 if only_inv else 0.0)), skip_inverted=not only_inv, skip_non_inverted=only_inv)
        n_pre_filter = len(retests)
        if int(getattr(p, 'fvg_entry_confirm_bars', 0)) > 0 or int(getattr(p, 'fvg_min_retest_count', 0)) > 0:
            confirm_bars = max(0, int(getattr(p, 'fvg_entry_confirm_bars', 0)))
            min_retests = max(0, int(getattr(p, 'fvg_min_retest_count', 0)))
            kept_retests_f: list[tuple[int, int, FvgZone]] = []
            for r_bar, r_dir, r_zone in retests:
                if r_zone is None:
                    kept_retests_f.append((r_bar, r_dir, r_zone))
                    continue
                if confirm_bars > 0 and r_zone.n_touches < confirm_bars:
                    continue
                if min_retests > 0 and r_zone.n_touches < min_retests:
                    continue
                kept_retests_f.append((r_bar, r_dir, r_zone))
            retests = kept_retests_f
        if only_inv and times_utc_ns is not None and (int(getattr(p, 'fvg_ifvg_min_inversion_age_secs', 0)) > 0):
            min_age_secs = int(p.fvg_ifvg_min_inversion_age_secs)
            n_pre_ifvg = len(retests)
            kept_retests: list[tuple[int, int, FvgZone]] = []
            for r_bar, r_dir, r_zone in retests:
                if r_zone is None or r_zone.inverted_bar < 0:
                    kept_retests.append((r_bar, r_dir, r_zone))
                    continue
                if r_zone.trigger_bar < 0 or r_zone.trigger_bar >= len(times_utc_ns):
                    kept_retests.append((r_bar, r_dir, r_zone))
                    continue
                trig_ns = int(times_utc_ns[r_zone.trigger_bar])
                inv_ns = int(times_utc_ns[r_zone.inverted_bar]) if r_zone.inverted_bar < len(times_utc_ns) else trig_ns
                age_secs = (inv_ns - trig_ns) / 1000000000
                if age_secs >= min_age_secs:
                    kept_retests.append((r_bar, r_dir, r_zone))
            n_dropped_ifvg = n_pre_ifvg - len(kept_retests)
            if n_dropped_ifvg > 0:
                ict.n_ifvg_age_dropped += n_dropped_ifvg
            retests = kept_retests
        if use_ms and structure_state is not None:
            from .market_structure import annotate_choch_plus
            annotate_choch_plus(structure_state, zones)
        sl_per_b = float(p.fvg_sl_per_breadth)
        tp_per_b = float(p.fvg_tp_per_breadth)
        drop_inverted = not only_inv and bool(getattr(p, 'drop_inverted_fvg', True))
        for bar, direction, zone in retests:
            if drop_inverted and zone is not None and zone.inverted:
                continue
            if retest_lookback > 0 and zone is not None:
                anchor = zone.inverted_bar if only_inv else zone.mitigated_bar
                if anchor < 0 or bar - anchor > retest_lookback:
                    continue
            if bias_gate and _bias(bar) != 0:
                effective_dir = _bias(bar)
                if only_inv and getattr(p, 'ifvg_reverses_bias', True):
                    effective_dir = -effective_dir
                if direction != effective_dir:
                    continue
            sl_invert = bool(getattr(p, 'fvg_sl_breadth_invert', False))
            tp_invert = bool(getattr(p, 'fvg_tp_breadth_invert', False))
            if (sl_per_b > 0 or tp_per_b > 0) and zone is not None:
                zone_width = float(zone.zone_high - zone.zone_low)
                if sl_per_b > 0:
                    scaled_sl = sl_per_b * zone_width
                    if sl_invert and scaled_sl > 0:
                        scaled_sl = min(sl_usd, 1.0 / scaled_sl)
                else:
                    scaled_sl = sl_usd
                if tp_per_b > 0:
                    scaled_tp = tp_per_b * zone_width
                    if tp_invert and scaled_tp > 0:
                        scaled_tp = min(tp_usd, 1.0 / scaled_tp)
                else:
                    scaled_tp = tp_usd
                breadth_floor = float(p.fvg_breadth_floor_usd)
                scaled_sl = max(scaled_sl, breadth_floor)
                scaled_tp = max(scaled_tp, breadth_floor)
            else:
                scaled_sl = sl_usd
                scaled_tp = tp_usd
            conviction = 1.0
            if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                conviction = structure_conviction(structure_state, bar, direction, bos_boost_max_age_bars=ms_max_boost_age, choch_plus_boost_max_age_bars=ms_max_boost_age, choch_caution_max_age_bars=ms_choch_caution_age)
                if conviction < ms_min_conv:
                    continue
                if conviction > 1.0 and ms_boost > 1.0:
                    scaled_tp = scaled_tp * (1.0 + (ms_boost - 1.0) * (conviction - 1.0) / 0.5)
            rank_tier = ''
            rank_percentile = 0.0
            if rolling_enabled and ict.ranker is not None and (zone is not None):
                _anchor = float(zone.zone_high) if direction > 0 else float(zone.zone_low)
                rank_percentile = ict.ranker.percentile(direction, _anchor)
                rank_tier = _rolling_tier_label(rank_percentile, float(getattr(p, 'fvg_rolling_a_pct', 0.2)), float(getattr(p, 'fvg_rolling_c_pct', 0.2)))
            out.append(PendingSignal(trigger_bar=bar, direction=direction, triggered_by=src, stop_usd=scaled_sl, target_usd=scaled_tp, conviction=conviction, fvg_zone=zone if src in ('fvg', 'ifvg') else None, is_ifvg=src == 'ifvg', rank_tier=rank_tier, rank_percentile=rank_percentile))
    elif src == 'sweep':
        pass
    elif src == 'orb':
        sessions = detect_orb(times_utc_ns, open_, high, low, close, session_start_hour_utc=int(p.orb_session_hour_utc), or_duration_mins=int(p.orb_duration_mins))
        signals = orb_signals(sessions)
        for bar, direction, _sess in signals:
            if bias_gate and _bias(bar) != 0 and (direction != _bias(bar)):
                continue
            conviction = 1.0
            if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                conviction = structure_conviction(structure_state, bar, direction, bos_boost_max_age_bars=ms_max_boost_age, choch_plus_boost_max_age_bars=ms_max_boost_age, choch_caution_max_age_bars=ms_choch_caution_age)
                if conviction < ms_min_conv:
                    continue
            out.append(PendingSignal(trigger_bar=bar, direction=direction, triggered_by='orb', stop_usd=sl_usd, target_usd=tp_usd, conviction=conviction))
    elif src == 'wyckoff':
        states = detect_wyckoff(high, low, close, ict)
        for i, st in enumerate(states):
            d = 0
            if st.is_spring:
                d = 1
            elif st.is_utad:
                d = -1
            if d == 0:
                continue
            if bias_gate and _bias(i) != 0 and (d != _bias(i)):
                continue
            conviction = 1.0
            if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                conviction = structure_conviction(structure_state, i, d, bos_boost_max_age_bars=ms_max_boost_age, choch_plus_boost_max_age_bars=ms_max_boost_age, choch_caution_max_age_bars=ms_choch_caution_age)
                if conviction < ms_min_conv:
                    continue
            out.append(PendingSignal(trigger_bar=i, direction=d, triggered_by='wyckoff', stop_usd=sl_usd, target_usd=tp_usd, conviction=conviction))
    return out
