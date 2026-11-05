from __future__ import annotations
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional
import numpy as np

class StructureEvent(IntEnum):
    NONE = 0
    BOS_BULL = 1
    BOS_BEAR = -1
    CHOCH_BULL = 2
    CHOCH_BEAR = -2
    LIQ_SWEEP_HIGH = 3
    LIQ_SWEEP_LOW = -3

def is_choch(ev: int) -> bool:
    return ev in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR)

def is_bos(ev: int) -> bool:
    return ev in (StructureEvent.BOS_BULL, StructureEvent.BOS_BEAR)

def is_bull(ev: int) -> bool:
    return ev in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL, StructureEvent.LIQ_SWEEP_LOW)

def is_bear(ev: int) -> bool:
    return ev in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR, StructureEvent.LIQ_SWEEP_HIGH)

@dataclass(frozen=True)
class SwingPoint:
    bar: int
    price: float
    is_high: bool

@dataclass(frozen=True)
class BreakEvent:
    bar: int
    kind: StructureEvent
    broken_swing_bar: int
    broken_swing_price: float

@dataclass
class OrderBlock:
    bar_start: int
    bar_end: int
    is_bull: bool
    ob_value: float
    ob_top: float
    ob_bottom: float
    caused_break_bar: int
    caused_break_kind: StructureEvent
    mitigated_bar: int = -1
    live: bool = True

@dataclass
class LiquiditySweep:
    bar: int
    pivot_bar: int
    pivot_price: float
    is_bear_sweep: bool

@dataclass
class StructureState:
    trend: np.ndarray
    events: np.ndarray
    last_break_bar: np.ndarray
    last_break_kind: np.ndarray
    last_break_age_bars: np.ndarray
    last_choch_bar: np.ndarray
    last_choch_kind: np.ndarray
    last_choch_age_bars: np.ndarray
    last_choch_plus_bar: np.ndarray
    last_choch_plus_age_bars: np.ndarray
    swings: list[SwingPoint] = field(default_factory=list)
    breaks: list[BreakEvent] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)

def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int=14) -> np.ndarray:
    n = close.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    prev_close = np.empty(n, dtype=np.float64)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    out = np.zeros(n, dtype=np.float64)
    if n < length:
        return out
    out[length - 1] = tr[:length].mean()
    for i in range(length, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out

def detect_market_structure(high: np.ndarray, low: np.ndarray, close: np.ndarray, *, pivot_len: int=9, liquidity_len: int=30, detect_order_blocks: bool=True, detect_liquidity: bool=True, atr_length: int=14, resample_to_n_secs: int=0, ts_ns: Optional[np.ndarray]=None) -> StructureState:
    if resample_to_n_secs and resample_to_n_secs > 1:
        n_orig = high.shape[0]
        if ts_ns is None:
            raise ValueError('detect_market_structure(resample_to_n_secs>0) requires ts_ns (epoch-nanosecond timestamps, one per bar). Aggregating by row position is unsound on a gappy feed: measured on XAUUSD_S1_2025Q1, 15.4% of bars are >1s apart and 91.9% of 60s buckets do not hold 60 bars, which sheared 100% of resampled bars (median error $19.39). Pass to_ns_arr(bars).')
        ts_ns = np.asarray(ts_ns, dtype=np.int64)
        if ts_ns.shape[0] != n_orig:
            raise ValueError(f'ts_ns has {ts_ns.shape[0]} entries but high has {n_orig}; they must be one-per-bar.')
        nb = n_orig // resample_to_n_secs
        if nb >= 2 * pivot_len + 1:
            first_ts = int(ts_ns[0])
            _bucket = (ts_ns - first_ts) // int(resample_to_n_secs * 1000000000.0)
            _uniq, _inv = np.unique(_bucket, return_inverse=True)
            nb = int(_uniq.shape[0])
            hi = np.full(nb, -np.inf, dtype=np.float64)
            lo = np.full(nb, +np.inf, dtype=np.float64)
            np.maximum.at(hi, _inv, high)
            np.minimum.at(lo, _inv, low)
            _last = np.zeros(nb, dtype=np.int64)
            np.maximum.at(_last, _inv, np.arange(n_orig, dtype=np.int64))
            cl = close[_last]
            ts_rs = first_ts + _uniq * int(resample_to_n_secs * 1000000000.0)
            state_rs = detect_market_structure(hi, lo, cl, ts_ns=ts_rs, pivot_len=pivot_len, liquidity_len=liquidity_len, detect_order_blocks=detect_order_blocks, detect_liquidity=detect_liquidity, atr_length=atr_length, resample_to_n_secs=0)
            return _expand_state_to_original(state_rs, _last, n_orig)
    n = close.shape[0]
    trend = np.zeros(n, dtype=np.int8)
    events = np.zeros(n, dtype=np.int8)
    last_break_bar = np.full(n, -1, dtype=np.int32)
    last_break_kind = np.zeros(n, dtype=np.int8)
    last_break_age = np.full(n, -1, dtype=np.int32)
    last_choch_bar = np.full(n, -1, dtype=np.int32)
    last_choch_kind = np.zeros(n, dtype=np.int8)
    last_choch_age = np.full(n, -1, dtype=np.int32)
    last_chochp_bar = np.full(n, -1, dtype=np.int32)
    last_chochp_age = np.full(n, -1, dtype=np.int32)
    swings: list[SwingPoint] = []
    breaks: list[BreakEvent] = []
    order_blocks: list[OrderBlock] = []
    sweeps: list[LiquiditySweep] = []
    if n < 2 * pivot_len + 1:
        return StructureState(trend=trend, events=events, last_break_bar=last_break_bar, last_break_kind=last_break_kind, last_break_age_bars=last_break_age, last_choch_bar=last_choch_bar, last_choch_kind=last_choch_kind, last_choch_age_bars=last_choch_age, last_choch_plus_bar=last_chochp_bar, last_choch_plus_age_bars=last_chochp_age, swings=swings, breaks=breaks, order_blocks=order_blocks, sweeps=sweeps)
    swing_high_origin: list[int] = []
    swing_low_origin: list[int] = []
    swing_high_price: list[float] = []
    swing_low_price: list[float] = []
    for i in range(pivot_len, n - pivot_len):
        window_h = high[i - pivot_len:i + pivot_len + 1]
        if high[i] >= window_h.max():
            swing_high_origin.append(i)
            swing_high_price.append(float(high[i]))
        window_l = low[i - pivot_len:i + pivot_len + 1]
        if low[i] <= window_l.min():
            swing_low_origin.append(i)
            swing_low_price.append(float(low[i]))
    all_swings: list[tuple[int, float, bool]] = []
    for b, p in zip(swing_high_origin, swing_high_price):
        all_swings.append((b, p, True))
    for b, p in zip(swing_low_origin, swing_low_price):
        all_swings.append((b, p, False))
    all_swings.sort(key=lambda t: t[0])
    swings = [SwingPoint(bar=b, price=p, is_high=h) for b, p, h in all_swings]
    last_swing_high_bar = -1
    last_swing_high_price = -np.inf
    last_swing_low_bar = -1
    last_swing_low_price = np.inf
    last_swing_high_broken_bar = -1
    last_swing_low_broken_bar = -1
    cur_trend = 1
    high_swing_broken: set[int] = set()
    low_swing_broken: set[int] = set()

    def _commit_break(bar: int, kind: int, sb: int, sp: float) -> None:
        breaks.append(BreakEvent(bar=bar, kind=StructureEvent(kind), broken_swing_bar=sb, broken_swing_price=sp))
        events[bar] = kind
        last_break_bar[bar:] = bar
        last_break_kind[bar:] = kind
    # Causal: a swing is known only at bar + pivot_len, and a break is stamped at the bar whose
    # close crosses an already-known, unbroken swing level.
    cur_swing_idx = 0
    next_swing = swings[cur_swing_idx] if swings else None
    for i in range(n):
        while next_swing is not None and next_swing.bar + pivot_len <= i:
            if next_swing.is_high:
                last_swing_high_bar, last_swing_high_price = next_swing.bar, next_swing.price
            else:
                last_swing_low_bar, last_swing_low_price = next_swing.bar, next_swing.price
            cur_swing_idx += 1
            next_swing = swings[cur_swing_idx] if cur_swing_idx < len(swings) else None
        if last_swing_high_bar >= 0 and last_swing_high_bar not in high_swing_broken and close[i] > last_swing_high_price:
            kind = StructureEvent.BOS_BULL if cur_trend == 1 else StructureEvent.CHOCH_BULL
            _commit_break(i, kind, last_swing_high_bar, last_swing_high_price)
            high_swing_broken.add(last_swing_high_bar)
            cur_trend = 1
        if last_swing_low_bar >= 0 and last_swing_low_bar not in low_swing_broken and close[i] < last_swing_low_price:
            kind = StructureEvent.BOS_BEAR if cur_trend == -1 else StructureEvent.CHOCH_BEAR
            _commit_break(i, kind, last_swing_low_bar, last_swing_low_price)
            low_swing_broken.add(last_swing_low_bar)
            cur_trend = -1
        trend[i] = cur_trend
    if detect_liquidity:
        liq_swings = _pivots_only(high, low, liquidity_len)
        for ps in liq_swings:
            for j in range(ps.bar + liquidity_len, n):
                if high[j] > ps.price and close[j] < ps.price:
                    sweeps.append(LiquiditySweep(bar=j, pivot_bar=ps.bar, pivot_price=ps.price, is_bear_sweep=ps.is_high))
                    events[j] = StructureEvent.LIQ_SWEEP_HIGH if ps.is_high else StructureEvent.LIQ_SWEEP_LOW
                    break
    if detect_order_blocks and breaks:
        atr_arr = _atr(high, low, close, atr_length)
        for be in breaks:
            prior_swing_bar = be.broken_swing_bar
            break_bar = be.bar
            if be.kind in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
                seg_low_idx = int(np.argmin(low[prior_swing_bar:break_bar + 1])) + prior_swing_bar
                seg_low = float(low[seg_low_idx])
                atr_v = float(atr_arr[break_bar]) if atr_v_safe(atr_arr, break_bar) else 0.0
                if atr_v <= 0:
                    atr_v = float(atr_arr[max(0, break_bar - 1)]) if atr_v_safe(atr_arr, max(0, break_bar - 1)) else 0.1
                ob = OrderBlock(bar_start=seg_low_idx, bar_end=break_bar, is_bull=True, ob_value=seg_low, ob_top=seg_low + atr_v, ob_bottom=seg_low, caused_break_bar=break_bar, caused_break_kind=be.kind)
                order_blocks.append(ob)
            else:
                seg_high_idx = int(np.argmax(high[prior_swing_bar:break_bar + 1])) + prior_swing_bar
                seg_high = float(high[seg_high_idx])
                atr_v = float(atr_arr[break_bar]) if atr_v_safe(atr_arr, break_bar) else 0.0
                if atr_v <= 0:
                    atr_v = float(atr_arr[max(0, break_bar - 1)]) if atr_v_safe(atr_arr, max(0, break_bar - 1)) else 0.1
                ob = OrderBlock(bar_start=seg_high_idx, bar_end=break_bar, is_bull=False, ob_value=seg_high, ob_top=seg_high, ob_bottom=seg_high - atr_v, caused_break_bar=break_bar, caused_break_kind=be.kind)
                order_blocks.append(ob)
        for ob in order_blocks:
            for j in range(ob.bar_end + 1, n):
                if ob.is_bull and close[j] < ob.ob_value:
                    ob.mitigated_bar = j
                    ob.live = False
                    break
                if not ob.is_bull and close[j] > ob.ob_value:
                    ob.mitigated_bar = j
                    ob.live = False
                    break
    for i in range(n):
        if last_break_bar[i] >= 0:
            last_break_age[i] = i - last_break_bar[i]
        if last_choch_bar[i] >= 0:
            last_choch_age[i] = i - last_choch_bar[i]
        if last_chochp_bar[i] >= 0:
            last_chochp_age[i] = i - last_chochp_bar[i]
    last_cb = -1
    last_ck = 0
    for be in breaks:
        if be.kind in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            last_cb = be.bar
            last_ck = int(be.kind)
    next_choch_idx = 0
    choches = [be for be in breaks if be.kind in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR)]
    cb = -1
    ck = 0
    chi = 0
    for i in range(n):
        while chi < len(choches) and choches[chi].bar <= i:
            cb = choches[chi].bar
            ck = int(choches[chi].kind)
            chi += 1
        last_choch_bar[i] = cb
        last_choch_kind[i] = ck
        if cb >= 0:
            last_choch_age[i] = i - cb
    return StructureState(trend=trend, events=events, last_break_bar=last_break_bar, last_break_kind=last_break_kind, last_break_age_bars=last_break_age, last_choch_bar=last_choch_bar, last_choch_kind=last_choch_kind, last_choch_age_bars=last_choch_age, last_choch_plus_bar=last_chochp_bar, last_choch_plus_age_bars=last_chochp_age, swings=swings, breaks=breaks, order_blocks=order_blocks, sweeps=sweeps)

def _first_break(close: np.ndarray, start: int, end: int, level: float, sign: int) -> Optional[int]:
    lo = max(0, start)
    hi = min(close.shape[0], end + 1)
    if lo >= hi:
        return None
    seg = close[lo:hi]
    if sign == +1:
        mask = seg > level
    else:
        mask = seg < level
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    return int(idxs[0]) + lo

def _expand_state_to_original(state_rs: StructureState, last_row: np.ndarray, n_orig: int) -> StructureState:
    # last_row[k] = last 1s row inside 1m bucket k: the first row at which bucket k's bar is closed.
    nb = last_row.shape[0]

    def _remap_bar(bar: int) -> int:
        return int(last_row[bar]) if 0 <= bar < nb else -1
    new_breaks = [BreakEvent(bar=_remap_bar(be.bar), kind=be.kind, broken_swing_bar=_remap_bar(be.broken_swing_bar), broken_swing_price=be.broken_swing_price) for be in state_rs.breaks]
    new_swings = [SwingPoint(bar=_remap_bar(sw.bar), price=sw.price, is_high=sw.is_high) for sw in state_rs.swings]
    new_order_blocks = [OrderBlock(bar_start=_remap_bar(ob.bar_start), bar_end=_remap_bar(ob.bar_end), is_bull=ob.is_bull, ob_value=ob.ob_value, ob_top=ob.ob_top, ob_bottom=ob.ob_bottom, caused_break_bar=_remap_bar(ob.caused_break_bar), caused_break_kind=ob.caused_break_kind, mitigated_bar=_remap_bar(ob.mitigated_bar) if ob.mitigated_bar >= 0 else -1, live=ob.live) for ob in state_rs.order_blocks]
    new_sweeps = [LiquiditySweep(bar=_remap_bar(sw.bar), pivot_bar=_remap_bar(sw.pivot_bar), pivot_price=sw.pivot_price, is_bear_sweep=sw.is_bear_sweep) for sw in state_rs.sweeps]
    # k[r] = latest bucket closed at or before row r (-1 before the first close)
    k = np.searchsorted(last_row, np.arange(n_orig), side='right') - 1
    known = k >= 0
    ks = np.maximum(k, 0)
    remap = np.append(last_row, -1).astype(np.int32)  # index -1 -> -1

    def _bars(a: np.ndarray) -> np.ndarray:
        out = np.where(a[ks] >= 0, remap[a[ks]], -1).astype(np.int32)
        out[~known] = -1
        return out

    def _vals(a: np.ndarray, fill: int = 0) -> np.ndarray:
        out = a[ks].copy()
        out[~known] = fill
        return out
    trend = _vals(state_rs.trend).astype(np.int8)
    events = np.zeros(n_orig, dtype=np.int8)
    events[last_row] = state_rs.events  # one row per event, not repeated across the minute
    last_break_bar = _bars(state_rs.last_break_bar)
    last_break_kind = _vals(state_rs.last_break_kind).astype(np.int8)
    last_choch_bar = _bars(state_rs.last_choch_bar)
    last_choch_kind = _vals(state_rs.last_choch_kind).astype(np.int8)
    last_chochp_bar = _bars(state_rs.last_choch_plus_bar)
    rows = np.arange(n_orig, dtype=np.int32)
    last_break_age = np.where(last_break_bar >= 0, rows - last_break_bar, -1).astype(np.int32)
    last_choch_age = np.where(last_choch_bar >= 0, rows - last_choch_bar, -1).astype(np.int32)
    last_chochp_age = np.where(last_chochp_bar >= 0, rows - last_chochp_bar, -1).astype(np.int32)
    return StructureState(trend=trend, events=events, last_break_bar=last_break_bar, last_break_kind=last_break_kind, last_break_age_bars=last_break_age, last_choch_bar=last_choch_bar, last_choch_kind=last_choch_kind, last_choch_age_bars=last_choch_age, last_choch_plus_bar=last_chochp_bar, last_choch_plus_age_bars=last_chochp_age, swings=new_swings, breaks=new_breaks, order_blocks=new_order_blocks, sweeps=new_sweeps)

def atr_v_safe(atr_arr: np.ndarray, i: int) -> bool:
    return 0 <= i < atr_arr.shape[0] and atr_arr[i] > 0

def _pivots_only(high: np.ndarray, low: np.ndarray, pivot_len: int) -> list[SwingPoint]:
    n = high.shape[0]
    out: list[SwingPoint] = []
    if n < 2 * pivot_len + 1:
        return out
    win_size = 2 * pivot_len + 1
    h_wins = np.lib.stride_tricks.sliding_window_view(high, win_size)
    l_wins = np.lib.stride_tricks.sliding_window_view(low, win_size)
    pivot_idx = pivot_len
    pivot_h = h_wins[:, pivot_idx]
    pivot_l = l_wins[:, pivot_idx]
    is_pivot_h = pivot_h >= h_wins.max(axis=1)
    is_pivot_l = pivot_l <= l_wins.min(axis=1)
    n_windows = h_wins.shape[0]
    bar_indices = np.arange(n_windows) + pivot_idx
    for i in np.where(is_pivot_h)[0]:
        out.append(SwingPoint(bar=int(bar_indices[i]), price=float(pivot_h[i]), is_high=True))
    for i in np.where(is_pivot_l)[0]:
        out.append(SwingPoint(bar=int(bar_indices[i]), price=float(pivot_l[i]), is_high=False))
    return out

def annotate_choch_plus(state: StructureState, fvg_zones: list) -> StructureState:
    n = state.trend.shape[0]
    chochp_bar = np.full(n, -1, dtype=np.int32)
    last_cp = -1
    last_cp_age = np.full(n, -1, dtype=np.int32)
    sorted_fvg = sorted(fvg_zones, key=lambda z: z.trigger_bar)

    def _has_fvg_in_leg(start_bar: int, end_bar: int) -> bool:
        lo, hi = (0, len(sorted_fvg))
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_fvg[mid].trigger_bar < start_bar:
                lo = mid + 1
            else:
                hi = mid
        i = lo
        while i < len(sorted_fvg) and sorted_fvg[i].trigger_bar <= end_bar:
            return True
        return False
    chi = 0
    for be in state.breaks:
        if be.kind not in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            continue
        if _has_fvg_in_leg(be.broken_swing_bar, be.bar):
            last_cp = be.bar
    cur = -1
    cur_age = -1
    chochp_breaks = []
    for be in state.breaks:
        if be.kind not in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            continue
        if _has_fvg_in_leg(be.broken_swing_bar, be.bar):
            chochp_breaks.append(be.bar)
    chi = 0
    for i in range(n):
        while chi < len(chochp_breaks) and chochp_breaks[chi] <= i:
            cur = chochp_breaks[chi]
            cur_age = 0
            chi += 1
        chochp_bar[i] = cur
        if cur >= 0:
            last_cp_age[i] = i - cur
    state.last_choch_plus_bar = chochp_bar
    state.last_choch_plus_age_bars = last_cp_age
    return state

def structure_conviction(state: StructureState, bar: int, direction: int, *, bos_boost_max_age_bars: int=60, choch_plus_boost_max_age_bars: int=60, choch_caution_max_age_bars: int=60) -> float:
    if bar < 0 or bar >= state.trend.shape[0]:
        return 1.0
    score = 1.0
    last_bk = int(state.last_break_kind[bar])
    last_bk_age = int(state.last_break_age_bars[bar])
    last_ck = int(state.last_choch_kind[bar])
    last_ck_age = int(state.last_choch_age_bars[bar])
    last_cpk = state.last_choch_plus_bar[bar]
    last_cpk_age = int(state.last_choch_plus_age_bars[bar]) if last_cpk >= 0 else -1
    if last_bk_age >= 0 and last_bk_age <= bos_boost_max_age_bars:
        if direction > 0 and last_bk == StructureEvent.BOS_BULL or (direction < 0 and last_bk == StructureEvent.BOS_BEAR):
            score += 0.25
    if last_cpk_age >= 0 and last_cpk_age <= choch_plus_boost_max_age_bars:
        if direction > 0 and last_ck == StructureEvent.CHOCH_BULL or (direction < 0 and last_ck == StructureEvent.CHOCH_BEAR):
            score += 0.5
    if last_ck_age >= 0 and last_ck_age <= choch_caution_max_age_bars:
        if direction > 0 and last_ck == StructureEvent.CHOCH_BEAR or (direction < 0 and last_ck == StructureEvent.CHOCH_BULL):
            score -= 0.5
    ev = int(state.events[bar])
    if direction > 0 and ev == StructureEvent.LIQ_SWEEP_LOW or (direction < 0 and ev == StructureEvent.LIQ_SWEEP_HIGH):
        score += 0.1
    return max(0.0, min(1.5, score))

@dataclass(frozen=True)
class BosChochEvent:
    bar: int
    kind: int

class BosChochMemory:
    max_events: int
    events: list[BosChochEvent]

    def __init__(self, max_events: int=5) -> None:
        if max_events < 1:
            raise ValueError(f'max_events must be >= 1, got {max_events}')
        self.max_events = int(max_events)
        self.events = []

    def push(self, bar: int, kind: int) -> None:
        if int(kind) not in (StructureEvent.BOS_BULL, StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            return
        self.events.append(BosChochEvent(bar=int(bar), kind=int(kind)))
        if len(self.events) > self.max_events:
            self.events.sort(key=lambda e: e.bar)
            self.events = self.events[-self.max_events:]

    def thesis_after_most_recent(self) -> int:
        if not self.events:
            return 0
        sorted_evs = sorted(self.events, key=lambda e: e.bar)
        last = sorted_evs[-1]
        first = sorted_evs[0]
        if first.kind in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
            thesis = +1
        elif first.kind in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR):
            thesis = -1
        else:
            return 0
        for ev in sorted_evs[1:]:
            if ev.kind == StructureEvent.CHOCH_BULL:
                thesis = +1
            elif ev.kind == StructureEvent.CHOCH_BEAR:
                thesis = -1
        return thesis

    def alignment_at(self, bar: int, direction: int) -> str:
        if not self.events:
            return 'unknown'
        eligible = [e for e in self.events if e.bar <= int(bar)]
        if not eligible:
            return 'unknown'
        eligible.sort(key=lambda e: e.bar)
        if eligible[0].kind in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
            thesis = +1
        elif eligible[0].kind in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR):
            thesis = -1
        else:
            return 'unknown'
        for ev in eligible[1:]:
            if ev.kind == StructureEvent.CHOCH_BULL:
                thesis = +1
            elif ev.kind == StructureEvent.CHOCH_BEAR:
                thesis = -1
        if thesis == 0:
            return 'unknown'
        if direction > 0:
            return 'aligned' if thesis == +1 else 'opposed'
        if direction < 0:
            return 'aligned' if thesis == -1 else 'opposed'
        return 'unknown'

    def clear(self) -> None:
        self.events = []

def bos_choch_directional_alignment(state: StructureState, bar: int, direction: int, *, max_age_bars: int=300) -> bool | None:
    if bar < 0 or bar >= state.last_break_kind.shape[0]:
        return None
    last_bk = int(state.last_break_kind[bar])
    last_bk_age = int(state.last_break_age_bars[bar])
    if last_bk_age < 0 or last_bk_age > max_age_bars:
        return None
    if direction > 0 and last_bk in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
        return True
    if direction < 0 and last_bk in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR):
        return True
    if direction > 0 and last_bk in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR):
        return False
    if direction < 0 and last_bk in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
        return False
    return None
_BREAK_SIGN: dict = {StructureEvent.BOS_BULL: (+1, False), StructureEvent.BOS_BEAR: (-1, False), StructureEvent.CHOCH_BULL: (+1, True), StructureEvent.CHOCH_BEAR: (-1, True)}
_BREAK_BARS_CACHE: dict = {}
_BREAK_SIG: dict = {StructureEvent.BOS_BULL: 'b', StructureEvent.BOS_BEAR: 'B', StructureEvent.CHOCH_BULL: 'c', StructureEvent.CHOCH_BEAR: 'C'}

def _break_bars(state: 'StructureState') -> np.ndarray:
    key = id(state)
    cached = _BREAK_BARS_CACHE.get(key)
    if cached is not None and len(cached) == len(state.breaks):
        return cached
    if state.breaks:
        arr = np.fromiter((b.bar for b in state.breaks), dtype=np.int64, count=len(state.breaks))
    else:
        arr = np.zeros(0, dtype=np.int64)
    _BREAK_BARS_CACHE[key] = arr
    return arr

def structure_score(state: 'StructureState', bar: int, *, lookback_events: int=5, half_life_bars: float=1800.0, choch_weight: float=2.0) -> float:
    if lookback_events <= 0:
        return 0.0
    bars = _break_bars(state)
    if bars.size == 0:
        return 0.0
    upto = int(np.searchsorted(bars, bar, side='right'))
    if upto == 0:
        return 0.0
    lo = max(0, upto - lookback_events)
    num = 0.0
    den = 0.0
    j = upto - 1
    while j >= lo:
        ev = state.breaks[j]
        pair = _BREAK_SIGN.get(ev.kind)
        if pair is not None:
            sign, is_choch = pair
            age = float(bar - ev.bar)
            if age < 0.0:
                age = 0.0
            w = 0.5 ** (age / half_life_bars) * (choch_weight if is_choch else 1.0)
            num += w * sign
            den += w
        j -= 1
    if den <= 0.0:
        return 0.0
    return float(num / den)

def structure_confidence(state: 'StructureState', bar: int, **kw) -> float:
    return abs(structure_score(state, bar, **kw))
MOMENTUM_VEL_5M: float = 0.7
MOMENTUM_WICK: float = 0.3
STRUCTURE_WEIGHT: float = 0.6
MOMENTUM_WEIGHT: float = 0.4

def momentum_score(vol_state: Any, bar: int, close: Optional[np.ndarray]=None, *, vel_weight: float=MOMENTUM_VEL_5M, wick_weight: float=MOMENTUM_WICK, vel_window: int=300, vel_reference: float=0.0015) -> float:
    if vol_state is None or bar < 0:
        return 0.0
    try:
        mag_arr = np.asarray(vol_state['vel_5m'])
    except (KeyError, IndexError, TypeError):
        return 0.0
    if bar >= mag_arr.shape[0]:
        return 0.0
    mag = float(mag_arr[bar])
    if not np.isfinite(mag) or mag <= 0.0:
        return 0.0
    sign = 0.0
    if close is not None and bar >= vel_window:
        p_now = float(close[bar])
        p_prev = float(close[bar - vel_window])
        if np.isfinite(p_now) and np.isfinite(p_prev) and (abs(p_prev) > 1e-12):
            diff = p_now - p_prev
            sign = 1.0 if diff > 0 else -1.0 if diff < 0 else 0.0
    if sign == 0.0:
        return 0.0
    vel_term = sign * float(np.clip(mag / vel_reference, 0.0, 1.0))
    wick_term = 0.0
    try:
        if 'range_norm_1m' in vol_state and 'wick_ratio' in vol_state:
            rng = float(np.asarray(vol_state['range_norm_1m'])[bar])
            wr = float(np.asarray(vol_state['wick_ratio'])[bar])
            if np.isfinite(rng) and np.isfinite(wr):
                expan = float(np.clip(rng / 3.0, 0.0, 1.0))
                accept = float(np.clip(1.0 - wr, 0.0, 1.0))
                wick_term = vel_term * expan * accept
    except (KeyError, IndexError, TypeError):
        wick_term = 0.0
    total = vel_weight * vel_term + wick_weight * wick_term
    return float(np.clip(total, -1.0, 1.0))

def combined_score(state: 'StructureState', vol_state: Any, bar: int, direction: int, *, close: Optional[np.ndarray]=None, structure_weight: float=STRUCTURE_WEIGHT, momentum_weight: float=MOMENTUM_WEIGHT, lookback_events: int=5, half_life_bars: float=1800.0, choch_weight: float=2.0) -> tuple[float, float, float]:
    s = structure_score(state, bar, lookback_events=lookback_events, half_life_bars=half_life_bars, choch_weight=choch_weight)
    m = momentum_score(vol_state, bar, close)
    total = structure_weight * s + momentum_weight * m
    return (float(total), float(s), float(m))
__all__ = ['StructureEvent', 'is_choch', 'is_bos', 'is_bull', 'is_bear', 'SwingPoint', 'BreakEvent', 'OrderBlock', 'LiquiditySweep', 'StructureState', 'BosChochEvent', 'BosChochMemory', 'detect_market_structure', 'annotate_choch_plus', 'structure_conviction', 'bos_choch_directional_alignment', 'structure_score', 'structure_confidence', 'momentum_score', 'combined_score']
