from __future__ import annotations
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple
import numpy as np
import pandas as pd
from bt._engine.ict_signals import FvgZone
from bt._engine.timebase import assert_ns, to_ns_arr
from bt._engine.swing_cap import DailySwingCap
from bt._engine.rolling_p95 import RollingP95Cap
from bt._engine.market_structure import StructureState, _BREAK_SIG, _break_bars, bos_choch_directional_alignment, combined_score
from bt._engine.volume_profile import VolumeProfileSet, current_daily_vp, hvn_zones_near
from bt._engine.vp_multi_target_config import ENTRY_FILL_MODELS, ENTRY_FILL_NEXT_OPEN, ENTRY_FILL_TOUCH, ENTRY_FILL_TRIGGER, FILL_MODEL_MID, FILL_MODEL_NEXT_OPEN, FILL_MODEL_PESSIMISTIC, FILL_MODELS, LVN_MODE_SKIP, SL_MODE_FVG_ONLY, SL_MODE_HVN_LVN, SL_MODES, TP_MODE_FVG_MULT, TP_MODE_HVN_SINGLE, TP_MODE_MULTI_TARGET, TP_MODES

@dataclass
class OpenTrade:
    trade_id: int
    parent_zone_id: int
    entry_bar: int
    entry_time_ns: int
    entry_price: float
    direction: int
    stop_price: float
    target_price: float
    fvg_direction: int
    source: str
    sl_mode: str
    tp_mode: str
    sl_struct_ref: str = ''
    tp_struct_ref: str = ''
    struct_verdict: float = 0.0
    signal_bar: int = 0
    zone_width: float = 0.0
    zone_low: float = 0.0
    zone_high: float = 0.0
    target_price_2: float = 0.0
    tp_struct_ref_2: str = ''
    partial_close_pct: float = 0.0
    peak_price: float = 0.0
    trail_stop_price: float = 0.0
    trail_active: bool = False
    trail_atr_value: float = 0.0
    initial_sl_distance_usd: float = 0.0
    initial_tp_distance_usd: float = 0.0
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    tp1_hit: bool = False
    live_pnl_usd: float = 0.0
    qty: float = 0.0

    def qty_of(self, fallback: float) -> float:
        return float(self.qty) if float(self.qty) > 0.0 else float(fallback)

@dataclass
class PendingOrder:
    order_id: int
    zone: FvgZone
    direction: int
    entry_price: float
    placed_bar: int
    placed_time_ns: int
    source: str
    fvg_direction: int
    zone_width: float
    death_bar: int = 0
    filled: bool = False
    cancelled: bool = False
    cancel_reason: str = ''
    struct_verdict: float = 0.0

@dataclass
class ClosedTrade:
    trade_id: int
    parent_zone_id: int
    entry_bar: int
    exit_bar: int
    entry_time_ns: int
    exit_time_ns: int
    entry_price: float
    exit_price: float
    direction: int
    stop_price: float
    target_price: float
    exit_reason: str
    hold_secs: float
    pnl_gross_usd: float
    pnl_net_usd: float
    qty_oz: float
    fvg_direction: int
    source: str
    sl_mode: str
    tp_mode: str
    sl_struct_ref: str
    tp_struct_ref: str
    sl_distance_usd: float = 0.0
    tp_distance_usd: float = 0.0
    entry_poc_dist_usd: float = 0.0
    zone_width: float = 0.0
    zone_low: float = 0.0
    zone_high: float = 0.0
    target_price_2: float = 0.0
    tp_struct_ref_2: str = ''
    partial_close_pct: float = 0.0
    signal_bar: int = 0
    initial_sl_distance_usd: float = 0.0
    initial_tp_distance_usd: float = 0.0
    struct_verdict: float = 0.0

@dataclass
class BacktestResult:
    trades: List[ClosedTrade] = field(default_factory=list)
    n_signals_emitted: int = 0
    n_sniper_filled: int = 0
    n_immediate_filled: int = 0
    n_soft_stops: int = 0
    n_inversions_detected: int = 0
    n_dropped_opposite: int = 0
    n_replaced_opposite: int = 0
    n_stacked_opposite: int = 0
    n_same_dir_blocked: int = 0
    n_dropped_conviction: int = 0
    n_replaced_conviction: int = 0
    n_dropped_vol_state: int = 0
    n_dropped_p95_warmup: int = 0
    n_dropped_p95_warmup_sniper: int = 0
    n_dropped_structure: int = 0
    n_structure_aligned: int = 0
    n_structure_opposed: int = 0
    n_structure_no_event: int = 0
    n_structure_seq_flipped: int = 0
    n_dropped_sides: int = 0
    n_dropped_seq_whitelist: int = 0
    n_dropped_policy: int = 0
    n_dropped_policy_at_fill: int = 0
    n_skipped_already_fired: int = 0
    n_skipped_dual_inverted: int = 0
    n_zones_bucketized: int = 0
    n_orders_placed: int = 0
    n_orders_filled: int = 0
    n_orders_cancelled: int = 0
    n_orders_cancelled_zone_death: int = 0
    n_orders_cancelled_expired: int = 0
    n_contra_close_events: int = 0
    n_contra_legs_closed: int = 0
    n_contra_qty_netted: float = 0.0
    contra_gross_usd: float = 0.0
    contra_distance_usd_max: float = 0.0
    n_fills_opposite_open: int = 0
    n_fills_total: int = 0
    n_bars_both_sides_open: int = 0
    hedged_distance_max_usd: float = 0.0
    hedged_distance_bars: int = 0
    fill_model_used: str = FILL_MODEL_MID
    entry_fill_model_used: str = ENTRY_FILL_TOUCH
    conflict_policy_used: str = 'drop'
    max_same_dir_open_used: int = 0
    equity_curve: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))

def _zone_dead_by(zone: FvgZone, bar: int) -> bool:
    sup = int(getattr(zone, 'superseded_bar', -1))
    if 0 <= sup <= bar:
        return True
    po = int(getattr(zone, 'played_out_bar', -1))
    if 0 <= po <= bar:
        return True
    exp = int(getattr(zone, 'expired_bar', -1))
    if 0 <= exp <= bar and (not getattr(zone, 'inverted', False)):
        return True
    return False

def _zone_death_bar(zone: FvgZone) -> int:
    bars = [int(getattr(zone, 'superseded_bar', -1)), int(getattr(zone, 'played_out_bar', -1))]
    if not getattr(zone, 'inverted', False):
        bars.append(int(getattr(zone, 'expired_bar', -1)))
    live = [b for b in bars if b >= 0]
    return min(live) if live else 0

def _order_touched(*, b_low: float, b_high: float, entry_price: float, direction: int) -> bool:
    if direction > 0:
        return b_low <= entry_price
    return b_high >= entry_price

def _resolve_exit_price(*, level: float, b_open: float, b_low: float, b_high: float, direction: int, is_stop: bool, fill_model: str) -> float:
    if fill_model == FILL_MODEL_NEXT_OPEN:
        return b_open
    return level

def _bar_resolves_stop_first(*, sl_hit: bool, tp_hit: bool, fill_model: str) -> bool:
    if not (sl_hit and tp_hit):
        return False
    return fill_model != FILL_MODEL_MID

def _build_atr_1m_forward_filled(high: np.ndarray, low: np.ndarray, close: np.ndarray, times_ns: np.ndarray, length: int=14) -> np.ndarray:
    n = int(close.shape[0])
    out = np.zeros(n, dtype=np.float64)
    if n == 0:
        return out
    minute_idx = (times_ns // 60000000000).astype(np.int64)
    uniq, starts = np.unique(minute_idx, return_index=True)
    order = np.argsort(starts)
    uniq = uniq[order]
    starts = starts[order]
    ends = np.concatenate([starts[1:], np.array([n], dtype=np.int64)])
    m = int(uniq.shape[0])
    if m == 0:
        return out
    mh = np.empty(m, dtype=np.float64)
    ml = np.empty(m, dtype=np.float64)
    mc = np.empty(m, dtype=np.float64)
    for k in range(m):
        s, e = (int(starts[k]), int(ends[k]))
        mh[k] = float(np.max(high[s:e]))
        ml[k] = float(np.min(low[s:e]))
        mc[k] = float(close[e - 1])
    atr_m = np.zeros(m, dtype=np.float64)
    if m >= length:
        prev = mc[0]
        tr = np.empty(m, dtype=np.float64)
        for k in range(m):
            cur_prev = mc[k - 1] if k > 0 else prev
            tr[k] = max(float(mh[k] - ml[k]), abs(float(mh[k] - cur_prev)), abs(float(ml[k] - cur_prev)))
        atr_m[length - 1] = float(np.mean(tr[:length]))
        for k in range(length, m):
            atr_m[k] = (atr_m[k - 1] * (length - 1) + tr[k]) / length
    pos = np.searchsorted(starts, np.arange(n), side='right') - 1
    pos = np.clip(pos, 0, m - 1)
    prev_atr = np.zeros(m, dtype=np.float64)
    for k in range(m):
        prev_atr[k] = atr_m[k - 1] if k > 0 else 0.0
    return prev_atr[pos]

def _trail_level_usd(*, entry_price: float, peak_price: float, direction: int, atr_value: float, trail_atr_mult: float, trail_giveback_frac: float, trail_mode: str) -> float:
    mode = str(trail_mode).lower().strip()
    chand = 0.0
    give = 0.0
    if mode in ('chandelier', 'combined'):
        chand = float(peak_price) - float(direction) * float(trail_atr_mult) * float(atr_value)
    if mode in ('giveback', 'combined'):
        give = float(entry_price) + (float(peak_price) - float(entry_price)) * (1.0 - float(trail_giveback_frac))
    if mode == 'chandelier':
        return chand
    if mode == 'giveback':
        return give
    if chand == 0.0:
        return give
    if give == 0.0:
        return chand
    if direction > 0:
        return chand if chand > give else give
    return chand if chand < give else give

def _vol_state_pass(i: int, vol_state: Optional[Dict[str, np.ndarray]], min_rv30: float, min_rng: float, min_v5: float, min_v15: float, max_rv30: float, max_wick: float, min_expand_prev_day: float=0.0, max_expand_prev_day: float=0.0) -> bool:
    if vol_state is None:
        return True
    ok = True
    if min_rv30 > 0.0:
        ok = ok and float(vol_state['rv_ratio_30s'][i]) >= min_rv30
    if min_rng > 0.0:
        ok = ok and float(vol_state['range_norm_1m'][i]) >= min_rng
    if min_v5 > 0.0:
        ok = ok and float(vol_state['vel_5m'][i]) >= min_v5
    if min_v15 > 0.0:
        ok = ok and float(vol_state['vel_15m'][i]) >= min_v15
    if max_rv30 > 0.0:
        ok = ok and float(vol_state['rv_ratio_30s'][i]) <= max_rv30
    if max_wick > 0.0:
        ok = ok and float(vol_state['wick_ratio'][i]) <= max_wick
    if min_expand_prev_day > 0.0:
        ok = ok and float(vol_state['vol_expand_prev_day'][i]) >= min_expand_prev_day
    if max_expand_prev_day > 0.0:
        ok = ok and float(vol_state['vol_expand_prev_day'][i]) <= max_expand_prev_day
    return ok

def _apply_swing_cap(*, tp_price: float, tp_ref: str, entry_price: float, direction: int, swing_cap: Optional[DailySwingCap], t_ns: int) -> Tuple[float, str]:
    if swing_cap is None or not swing_cap.enabled:
        return (tp_price, tp_ref)
    cap_usd = swing_cap.cap_for(t_ns)
    if not np.isfinite(cap_usd):
        return (tp_price, tp_ref)
    if direction > 0:
        capped = float(entry_price) + cap_usd
        if tp_price > capped:
            return (capped, tp_ref + '+swing')
    else:
        capped = float(entry_price) - cap_usd
        if tp_price < capped:
            return (capped, tp_ref + '+swing')
    return (tp_price, tp_ref)

def _p95_cap_usd(p95_cap: Optional['RollingP95Cap'], t_ns: int) -> Optional[float]:
    if p95_cap is None or not p95_cap.enabled:
        return None
    cap_usd = p95_cap.cap_for(t_ns)
    if not np.isfinite(cap_usd) or cap_usd <= 0.0:
        return None
    return float(cap_usd)

def _structure_verdict(bar: int, zone: Any, structure: Optional['StructureState'], max_age_bars: int) -> str:
    if structure is None:
        return 'no_event'
    aligned = bos_choch_directional_alignment(structure, bar, int(getattr(zone, 'direction', 0)), max_age_bars=max_age_bars)
    if aligned is True:
        return 'aligned'
    if aligned is False:
        return 'opposed'
    return 'no_event'

def _structure_pass(bar: int, zone: Any, structure: Optional['StructureState'], gate: bool, max_age_bars: int, res: Optional['BacktestResult']=None) -> bool:
    if not gate:
        return True
    if structure is None:
        return True
    aligned = bos_choch_directional_alignment(structure, bar, int(getattr(zone, 'direction', 0)), max_age_bars=max_age_bars)
    if res is not None:
        if aligned is True:
            res.n_structure_aligned += 1
        elif aligned is False:
            res.n_structure_opposed += 1
        else:
            res.n_structure_no_event += 1
    return aligned is True

def _sequence_signature(structure: Optional['StructureState'], bar: int, length: int) -> str:
    if structure is None or length <= 0:
        return ''
    breaks = getattr(structure, 'breaks', None)
    if not breaks:
        return ''
    bars = _break_bars(structure)
    if bars.size == 0:
        return ''
    upto = int(np.searchsorted(bars, bar, side='right'))
    if upto < length:
        return ''
    out: list[str] = []
    for j in range(upto - length, upto):
        kind = breaks[j].kind
        out.append(_BREAK_SIG.get(kind, '?'))
    return ''.join(out)

def _structure_score_pass(bar: int, zone: Any, structure: Optional['StructureState'], vol_state: Any, close: Optional[np.ndarray], gate: bool, min_abs_score: float, lookback_events: int, half_life_bars: float, res: Optional['BacktestResult']=None) -> tuple[bool, float]:
    if structure is None:
        return (True, 0.0)
    direction = int(getattr(zone, 'direction', 0))
    combined, _s, _m = combined_score(structure, vol_state, bar, direction, close=close, lookback_events=lookback_events, half_life_bars=half_life_bars)
    if not gate:
        return (True, combined)
    if res is not None:
        if direction * combined > 0.0:
            res.n_structure_aligned += 1
        else:
            res.n_structure_opposed += 1
    admit = direction * combined > 0.0 and abs(combined) >= min_abs_score
    return (admit, combined)

def _swing_cap_usd(swing_cap: Optional[DailySwingCap], t_ns: int) -> Optional[float]:
    if swing_cap is None or not swing_cap.enabled:
        return None
    cap_usd = swing_cap.cap_for(t_ns)
    if not np.isfinite(cap_usd) or cap_usd <= 0.0:
        return None
    return float(cap_usd)

def _reachable_tp_band_usd(*, p95_cap: Optional['RollingP95Cap'], swing_cap: Optional[DailySwingCap], t_ns: int) -> float:
    limits = [_c for _c in (_p95_cap_usd(p95_cap, t_ns), _swing_cap_usd(swing_cap, t_ns)) if _c is not None]
    return min(limits) if limits else float('inf')

def _apply_p95_cap(*, tp_price: float, tp_ref: str, entry_price: float, direction: int, p95_cap: Optional['RollingP95Cap'], t_ns: int) -> Tuple[float, str]:
    cap_usd = _p95_cap_usd(p95_cap, t_ns)
    if cap_usd is None:
        return (tp_price, tp_ref)
    if direction > 0:
        capped = float(entry_price) + cap_usd
        if tp_price > capped:
            return (capped, tp_ref + '+p95')
    else:
        capped = float(entry_price) - cap_usd
        if tp_price < capped:
            return (capped, tp_ref + '+p95')
    return (tp_price, tp_ref)

def _separate_tp_legs(*, tp1: float, tp2: float, tp1_ref: str, tp2_ref: str, entry_price: float, direction: int, min_leg_usd: float=0.01) -> Tuple[float, float, str, str]:
    if direction > 0:
        if tp2 <= float(entry_price):
            return (tp1, tp2, tp1_ref, tp2_ref)
    elif tp2 >= float(entry_price):
        return (tp1, tp2, tp1_ref, tp2_ref)
    if direction > 0:
        if tp1 < tp2 - min_leg_usd:
            return (tp1, tp2, tp1_ref, tp2_ref)
        new_tp1 = max(float(entry_price) + min_leg_usd, tp2 - min_leg_usd)
        return (new_tp1, tp2, tp1_ref + '+pinned', tp2_ref)
    else:
        if tp1 > tp2 + min_leg_usd:
            return (tp1, tp2, tp1_ref, tp2_ref)
        new_tp1 = min(float(entry_price) - min_leg_usd, tp2 + min_leg_usd)
        return (new_tp1, tp2, tp1_ref + '+pinned', tp2_ref)

def _contra_distance_usd(long_qty: float, long_entry_sum: float, short_qty: float, short_entry_sum: float) -> float:
    if long_qty <= 0.0 or short_qty <= 0.0:
        return 0.0
    return short_entry_sum / short_qty - long_entry_sum / long_qty

def _contra_close_price(*, b_open: float, b_low: float, b_high: float, b_close: float, fill_model: str) -> float:
    if fill_model == FILL_MODEL_NEXT_OPEN:
        return b_open
    return b_close

def _opposite_open_exists_vp(open_trades: list, direction: int) -> bool:
    return any((int(ot.direction) == -int(direction) for ot in open_trades))

def _count_same_dir_open_vp(open_trades: list, direction: int) -> int:
    return sum((1 for ot in open_trades if int(ot.direction) == int(direction)))

def _soft_stop_opposite_vp(open_trades: list, direction_to_replace: int, fill_price: float, i: int, b_time: int, qty_oz: float, closed_trades: list) -> bool:
    for idx, ot in enumerate(open_trades):
        if int(ot.direction) != int(direction_to_replace):
            continue
        direction = int(ot.direction)
        entry_price = float(ot.entry_price)
        qty_used = ot.qty_of(qty_oz)
        gross = (fill_price - entry_price) * direction * qty_used
        hold_secs = (b_time - ot.entry_time_ns) / 1000000000.0
        sl_dist = abs(entry_price - ot.stop_price)
        tp_dist = abs(ot.target_price - entry_price)
        closed_trades.append(ClosedTrade(trade_id=ot.trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, exit_bar=i, entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time, entry_price=entry_price, exit_price=fill_price, direction=direction, stop_price=ot.stop_price, target_price=ot.target_price, exit_reason='replaced_by_new_signal', hold_secs=hold_secs, pnl_gross_usd=gross, pnl_net_usd=gross, qty_oz=qty_used, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref=ot.sl_struct_ref, tp_struct_ref=ot.tp_struct_ref, sl_distance_usd=sl_dist, tp_distance_usd=tp_dist, initial_sl_distance_usd=ot.initial_sl_distance_usd, initial_tp_distance_usd=ot.initial_tp_distance_usd, entry_poc_dist_usd=0.0, zone_width=ot.zone_width, zone_low=ot.zone_low, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar, zone_high=ot.zone_high, target_price_2=ot.target_price_2, tp_struct_ref_2=ot.tp_struct_ref_2, partial_close_pct=ot.partial_close_pct))
        open_trades.pop(idx)
        return True
    return False

def _position_remaining_value(ot: 'OpenTrade', model: Optional[Any]=None) -> float:
    tp_dist = abs(float(ot.target_price) - float(ot.entry_price))
    sl_dist = abs(float(ot.entry_price) - float(ot.stop_price))
    if model is not None:
        try:
            from numpy import asarray
            feats = asarray([[float(ot.entry_bar), float(ot.live_pnl_usd), float(ot.mfe_usd), float(ot.mae_usd), 1.0 if ot.tp1_hit else 0.0, float(ot.zone_width), sl_dist, tp_dist, float(ot.direction)]])
            v = float(model.predict(feats)[0])
            return max(0.0, v)
        except Exception:
            pass
    tp_dist_remain = max(0.0, tp_dist - float(ot.mfe_usd))
    bonus = (1.0 if ot.tp1_hit else 0.0) * 0.5 * tp_dist_remain
    extra = max(0.0, float(ot.mfe_usd) - abs(float(ot.live_pnl_usd))) * 0.5
    return float(ot.live_pnl_usd) + bonus + extra

def _new_signal_score(direction: int, fill_price: float, model: Optional[Any]=None) -> float:
    if model is not None:
        try:
            from numpy import asarray
            feats = asarray([[float(fill_price), float(direction), 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]])
            return max(0.0, float(model.predict(feats)[0]))
        except Exception:
            pass
    return 1.0

def _conflict_policy_pass(open_trades: list, direction: int, fill_price: float, i: int, b_time: int, policy: str, max_same_dir_open: int, qty_oz: float, closed_trades: list, res: 'BacktestResult', conviction_alpha: float=0.0, conviction_hysteresis_usd: float=0.0, conviction_model: Optional[Any]=None) -> bool:
    pol = str(policy).lower().strip()
    opposite_exists = _opposite_open_exists_vp(open_trades, direction)
    same_dir_count = _count_same_dir_open_vp(open_trades, direction)
    if pol == 'drop':
        if opposite_exists:
            res.n_dropped_opposite += 1
            return False
        if same_dir_count > 0 and max_same_dir_open > 0 and (same_dir_count >= max_same_dir_open):
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True
    if pol == 'replace':
        if opposite_exists:
            if _soft_stop_opposite_vp(open_trades, -direction, fill_price, i, b_time, qty_oz, closed_trades):
                res.n_replaced_opposite += 1
        if same_dir_count > 0 and max_same_dir_open > 0 and (same_dir_count >= max_same_dir_open):
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True
    if pol == 'stack':
        if opposite_exists:
            res.n_stacked_opposite += 1
        if same_dir_count > 0 and max_same_dir_open > 0 and (same_dir_count >= max_same_dir_open):
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True
    if pol == 'replace_conviction':
        opposite_opens = [ot for ot in open_trades if int(ot.direction) == -int(direction)]
        if not opposite_opens:
            return True
        old_conviction = sum((_position_remaining_value(ot, conviction_model) for ot in opposite_opens))
        new_conviction = _new_signal_score(direction, fill_price, conviction_model)
        required = old_conviction * (1.0 + conviction_alpha) + conviction_hysteresis_usd
        if new_conviction > required:
            if _soft_stop_opposite_vp(open_trades, -direction, fill_price, i, b_time, qty_oz, closed_trades):
                res.n_replaced_conviction += 1
        else:
            res.n_dropped_conviction += 1
            return False
        if same_dir_count > 0 and max_same_dir_open > 0 and (same_dir_count >= max_same_dir_open):
            res.n_same_dir_blocked += 1
            res.n_dropped_opposite += 1
            return False
        return True
    if opposite_exists:
        res.n_dropped_opposite += 1
        return False
    return True

def _apply_sl_max_ceiling(*, sl_price: float, entry_price: float, direction: int, sl_max_usd: float) -> Tuple[float, str]:
    if sl_max_usd is None or float(sl_max_usd) <= 0.0:
        return (float(sl_price), '')
    _max = float(sl_max_usd)
    if direction > 0:
        capped = float(entry_price) - _max
        if float(sl_price) < capped:
            return (capped, '+cap')
    else:
        capped = float(entry_price) + _max
        if float(sl_price) > capped:
            return (capped, '+cap')
    return (float(sl_price), '')

def _apply_sl_min_floor(*, sl_price: float, entry_price: float, direction: int, sl_min_usd: float) -> Tuple[float, str]:
    if sl_min_usd is None or float(sl_min_usd) <= 0.0:
        return (float(sl_price), '')
    _min = float(sl_min_usd)
    if direction > 0:
        floored = float(entry_price) - _min
        if float(sl_price) > floored:
            return (floored, '+min')
    else:
        floored = float(entry_price) + _min
        if float(sl_price) < floored:
            return (floored, '+min')
    return (float(sl_price), '')

def compute_structural_sl(*, entry_price: float, direction: int, zone_low: float, zone_high: float, vp_set: VolumeProfileSet, t_ns: int, sl_mode: str, hvn_buffer_usd: float, zone_width: float=0.0, sl_zone_mult: float=0.0, sl_atr_mult: float=0.0, atr_value: float=0.0, sl_nth_hvn_n: int=1, sl_min_usd: float=0.0, sl_max_usd: float=0.0, sl_hvn_max_distance_usd: float=0.0, sl_search_radius_usd: Optional[float]=None) -> Tuple[float, str]:
    if sl_atr_mult > 0.0 and atr_value > 0.0:
        offset = sl_atr_mult * float(atr_value)
    elif sl_zone_mult > 0.0 and zone_width > 0.0:
        offset = sl_zone_mult * float(zone_width)
    else:
        offset = 0.0
    if direction > 0:
        zone_edge_sl = float(zone_low) - offset
    else:
        zone_edge_sl = float(zone_high) + offset
    zone_edge_ref = 'zone_edge'
    if sl_mode == SL_MODE_FVG_ONLY:
        _sl, _min_ref = _apply_sl_min_floor(sl_price=zone_edge_sl, entry_price=entry_price, direction=direction, sl_min_usd=sl_min_usd)
        _sl, _cap_ref = _apply_sl_max_ceiling(sl_price=_sl, entry_price=entry_price, direction=direction, sl_max_usd=sl_max_usd)
        return (_sl, zone_edge_ref + _min_ref + _cap_ref)
    if sl_mode == SL_MODE_HVN_LVN:
        hvn_dir = -1 if direction > 0 else +1
        _sl_radius = float(sl_hvn_max_distance_usd) if sl_hvn_max_distance_usd > 0.0 else float(sl_search_radius_usd) if sl_search_radius_usd is not None else float('inf')
        _want = max(int(sl_nth_hvn_n), 1)
        _found = hvn_zones_near(vp_set, t_ns, entry_price, hvn_dir, max_distance_usd=_sl_radius, max_results=_want + 2)
        _sl_sl: Optional[float] = None
        _seen = 0
        for lo, hi, mid in _found:
            if direction > 0:
                if not float(hi) < float(zone_low):
                    continue
                _cand = float(lo) - float(hvn_buffer_usd)
            else:
                if not float(lo) > float(zone_high):
                    continue
                _cand = float(hi) + float(hvn_buffer_usd)
            _seen += 1
            if _seen == _want:
                _sl_sl = _cand
                break
        if _sl_sl is None:
            _sl, _cap_ref = _apply_sl_max_ceiling(sl_price=zone_edge_sl, entry_price=entry_price, direction=direction, sl_max_usd=sl_max_usd)
            return (_sl, zone_edge_ref + _cap_ref)
        _sl_sl, _min_ref = _apply_sl_min_floor(sl_price=_sl_sl, entry_price=entry_price, direction=direction, sl_min_usd=sl_min_usd)
        _sl_sl, _cap_ref = _apply_sl_max_ceiling(sl_price=_sl_sl, entry_price=entry_price, direction=direction, sl_max_usd=sl_max_usd)
        return (_sl_sl, f'hvn_n{_want}' + _min_ref + _cap_ref)
    candidates: List[Tuple[float, str]] = [(zone_edge_sl, zone_edge_ref)]
    svp = current_daily_vp(vp_set, t_ns)
    if svp is not None:
        if direction > 0 and svp.poc < entry_price:
            poc_sl = svp.poc
            if poc_sl < float(zone_low):
                candidates.append((poc_sl, 'poc'))
        elif direction < 0 and svp.poc > entry_price:
            poc_sl = svp.poc
            if poc_sl > float(zone_high):
                candidates.append((poc_sl, 'poc'))
    hvn_dir = -1 if direction > 0 else +1
    hvn_results = hvn_zones_near(vp_set, t_ns, entry_price, hvn_dir, max_distance_usd=float(sl_hvn_max_distance_usd) if sl_hvn_max_distance_usd > 0.0 else float(sl_search_radius_usd) if sl_search_radius_usd is not None else float('inf'), max_results=5)
    for lo, hi, mid in hvn_results:
        if direction > 0:
            hvn_sl = lo - hvn_buffer_usd
            if hvn_sl < float(zone_low):
                candidates.append((hvn_sl, 'hvn'))
        else:
            hvn_sl = hi + hvn_buffer_usd
            if hvn_sl > float(zone_high):
                candidates.append((hvn_sl, 'hvn'))
    if direction > 0:
        valid = [(p, r) for p, r in candidates if p <= zone_edge_sl + 1e-06]
        if not valid:
            best_p, best_ref = (zone_edge_sl, zone_edge_ref)
        else:
            best_p, best_ref = max(valid, key=lambda x: x[0])
    else:
        valid = [(p, r) for p, r in candidates if p >= zone_edge_sl - 1e-06]
        if not valid:
            best_p, best_ref = (zone_edge_sl, zone_edge_ref)
        else:
            best_p, best_ref = min(valid, key=lambda x: x[0])
    best_p, _min_ref = _apply_sl_min_floor(sl_price=best_p, entry_price=entry_price, direction=direction, sl_min_usd=sl_min_usd)
    best_p, _cap_ref = _apply_sl_max_ceiling(sl_price=best_p, entry_price=entry_price, direction=direction, sl_max_usd=sl_max_usd)
    return (best_p, best_ref + _min_ref + _cap_ref)

def compute_structural_tp(*, entry_price: float, direction: int, zone_low: float, zone_high: float, zone_width: float, vp_set: VolumeProfileSet, t_ns: int, tp_mode: str, tp_max_distance_usd: float, tp_zone_mult_fallback: float, lvn_mode: str, lvn_skip_usd: float, nth_hvn_n: int=1, partial_close_pct: float=0.5, tp_search_radius_usd: Optional[float]=None, tp2_mode: str='zone_mult', tp2_nth_hvn_n: int=0) -> Tuple[float, str, float, str, float]:
    zone_mult_tp = entry_price + tp_zone_mult_fallback * zone_width if direction > 0 else entry_price - tp_zone_mult_fallback * zone_width
    if tp_mode == TP_MODE_FVG_MULT:
        return (zone_mult_tp, 'zone_mult', 0.0, '', 0.0)
    if tp_mode == TP_MODE_HVN_SINGLE:
        hvn_dir_s = +1 if direction > 0 else -1
        _tp_radius_s = float(tp_max_distance_usd) if tp_max_distance_usd > 0.0 else float(tp_search_radius_usd) if tp_search_radius_usd is not None else float('inf')
        _hvns_s = hvn_zones_near(vp_set, t_ns, entry_price, hvn_dir_s, max_distance_usd=_tp_radius_s, max_results=nth_hvn_n)
        if len(_hvns_s) >= nth_hvn_n:
            _lo_s, _hi_s, _mid_s = _hvns_s[nth_hvn_n - 1]
            if lvn_mode == LVN_MODE_SKIP:
                if direction > 0:
                    hvn_single_tp = float(_lo_s) - float(lvn_skip_usd)
                else:
                    hvn_single_tp = float(_hi_s) + float(lvn_skip_usd)
            else:
                hvn_single_tp = float(_mid_s)
            hvn_single_ref = f'hvn_single_n{nth_hvn_n}'
        else:
            hvn_single_tp = entry_price + tp_zone_mult_fallback * zone_width * hvn_dir_s
            hvn_single_ref = 'hvn_single_proxy'
        return (hvn_single_tp, hvn_single_ref, 0.0, '', 0.0)
    if tp_mode == TP_MODE_MULTI_TARGET:
        fvg_tp = zone_mult_tp
        hvn_dir = +1 if direction > 0 else -1
        _tp_radius = float(tp_max_distance_usd) if tp_max_distance_usd > 0.0 else float(tp_search_radius_usd) if tp_search_radius_usd is not None else float('inf')
        hvns = hvn_zones_near(vp_set, t_ns, entry_price, hvn_dir, max_distance_usd=_tp_radius, max_results=nth_hvn_n)
        if len(hvns) >= nth_hvn_n:
            lo, hi, mid = hvns[nth_hvn_n - 1]
            if lvn_mode == LVN_MODE_SKIP:
                if direction > 0:
                    hvn_tp = float(lo) - float(lvn_skip_usd)
                else:
                    hvn_tp = float(hi) + float(lvn_skip_usd)
            else:
                hvn_tp = float(mid)
            hvn_ref_str = f'multi_hvn_n{nth_hvn_n}'
        else:
            hvn_tp = entry_price + tp_zone_mult_fallback * zone_width * hvn_dir
            hvn_ref_str = 'multi_hvn_proxy'
        if tp2_mode == 'hvn':
            _beyond = hvn_zones_near(vp_set, t_ns, hvn_tp, hvn_dir, max_distance_usd=_tp_radius, max_results=tp2_nth_hvn_n if tp2_nth_hvn_n > 0 else 1)
            _idx = tp2_nth_hvn_n - 1 if tp2_nth_hvn_n > 0 else 0
            if len(_beyond) > _idx:
                _lo2, _hi2, _mid2 = _beyond[_idx]
                fvg_tp = _lo2 - lvn_skip_usd if lvn_mode == LVN_MODE_SKIP and hvn_dir > 0 else _hi2 + lvn_skip_usd if lvn_mode == LVN_MODE_SKIP else _mid2
                tp2_ref = f'multi_hvn_tp2_n{_idx + 1}'
            else:
                fvg_tp = entry_price + _tp_radius * hvn_dir
                tp2_ref = 'multi_swing_edge'
        elif tp2_mode == 'swing':
            fvg_tp = entry_price + tp_zone_mult_fallback * zone_width * hvn_dir
            tp2_ref = 'swing_proxy'
        else:
            fvg_tp = zone_mult_tp
            tp2_ref = 'multi_zone_mult'
        return (hvn_tp, hvn_ref_str, fvg_tp, tp2_ref, partial_close_pct)
    raise ValueError(f'Unknown tp_mode={tp_mode!r}. Valid modes: {TP_MODE_FVG_MULT!r}, {TP_MODE_MULTI_TARGET!r}, {TP_MODE_HVN_SINGLE!r}.')

def run_vp_sniper_backtest(bars: pd.DataFrame, zones: List[FvgZone], vp_set: VolumeProfileSet, *, qty_oz: float, sl_mode: str, tp_mode: str, lvn_mode: str, hvn_buffer_usd: float, tp_zone_mult_fallback: float, tp_max_distance_usd: float, lvn_skip_usd: float, entry_mode: str, nth_hvn_n: int=1, partial_close_pct: float=0.5, swing_cap: Optional['DailySwingCap']=None, p95_cap: Optional[RollingP95Cap]=None, structure: Optional['StructureState']=None, structure_gate: bool=False, structure_max_age_bars: int=300, trade_sides: str='both', structure_score_lookback: int=5, structure_score_half_life: float=1800.0, structure_score_min_abs: float=0.1, tp2_mode: str='zone_mult', tp2_nth_hvn_n: int=0, tp2_swing_cap: bool=True, structure_seq_flip_map: Optional[Dict[str, int]]=None, structure_seq_len: int=0, structure_seq_whitelist: Optional[set]=None, sl_zone_mult: float=0.0, sl_atr_mult: float=0.0, atr: Optional[np.ndarray]=None, atr_1m: Optional[np.ndarray]=None, trail_mode: str='off', trail_atr_mult: float=0.0, trail_activation_mult: float=0.0, trail_giveback_frac: float=0.0, trail_sides: str='both', trail_long_mode: str='', trail_long_activation_usd: float=0.0, trail_long_giveback_frac: float=0.0, max_hold_secs: float=0.0, sl_nth_hvn_n: int=1, sl_min_usd: float=0.0, sl_max_usd: float=0.0, sl_hvn_max_distance_usd: float=0.0, vol_state: Optional[Dict[str, np.ndarray]]=None, vol_state_min_rv_ratio_30s: float=0.0, vol_state_min_range_norm: float=0.0, vol_state_min_vel_5m: float=0.0, vol_state_min_vel_15m: float=0.0, vol_state_min_expand_prev_day: float=0.0, vol_state_max_expand_prev_day: float=0.0, vol_state_max_rv_ratio_30s: float=0.0, vol_state_max_wick_ratio: float=0.0, conflict_policy: str='drop', max_same_dir_open: int=0, entry_fill_model: str=ENTRY_FILL_TOUCH, fill_model: str=FILL_MODEL_PESSIMISTIC, conviction_alpha: float=0.0, conviction_hysteresis_usd: float=0.0, conviction_model_path: str='', on_bar_callback: Optional[Any]=None, on_bar_every_k: int=1, record_equity_curve: bool=True, contra_close: bool=False, contra_close_distance_usd: float=0.0, contra_hedge_stats: bool=False) -> Tuple[BacktestResult, Dict[str, Any]]:
    times = to_ns_arr(bars, 'time')
    assert_ns(times, 'vp_multi_target_engine times')
    open_ = bars['open'].to_numpy(dtype=np.float64)
    high = bars['high'].to_numpy(dtype=np.float64)
    low = bars['low'].to_numpy(dtype=np.float64)
    close = bars['close'].to_numpy(dtype=np.float64)
    n = len(close)
    _mv_open = memoryview(open_)
    _mv_high = memoryview(high)
    _mv_low = memoryview(low)
    _mv_close = memoryview(close)
    _mv_times = memoryview(times)
    if atr_1m is not None and len(atr_1m) == n:
        _atr1m = np.asarray(atr_1m, dtype=np.float64)
    else:
        _atr1m = _build_atr_1m_forward_filled(high, low, close, times)
    _trail_on = str(trail_mode).lower().strip() not in ('', 'off', 'none', 'disabled')
    _trail_sides = str(trail_sides).lower().strip()
    if _trail_sides not in ('both', 'long', 'short'):
        raise ValueError(f"trail_sides must be 'both', 'long' or 'short', got {trail_sides!r}")
    _trail_on_long = _trail_on and _trail_sides in ('both', 'long')
    _trail_on_short = _trail_on and _trail_sides in ('both', 'short')
    zones_at_bar: Dict[int, List[FvgZone]] = {}
    zones_inverted_at_bar: Dict[int, List[FvgZone]] = {}
    for z in zones:
        if z.trigger_bar >= 0 and z.trigger_bar < n:
            zones_at_bar.setdefault(int(z.trigger_bar), []).append(z)
        inv_b = int(getattr(z, 'inverted_bar', -1))
        if 0 <= inv_b < n and (not _zone_dead_by(z, inv_b)):
            zones_inverted_at_bar.setdefault(inv_b, []).append(z)
    res_zones_bucketized = sum((len(v) for v in zones_at_bar.values())) + sum((len(v) for v in zones_inverted_at_bar.values()))
    open_trades: List[OpenTrade] = []
    closed_trades: List[ClosedTrade] = []
    next_trade_id = 0
    pending_orders: List[PendingOrder] = []
    next_order_id = 0
    _c_long_qty = 0.0
    _c_long_sum = 0.0
    _c_short_qty = 0.0
    _c_short_sum = 0.0
    _c_aggr_dirty = True
    zone_already_fired: set = set()
    res = BacktestResult()
    res.n_zones_bucketized = int(res_zones_bucketized)
    res.conflict_policy_used = str(conflict_policy).lower().strip()
    res.max_same_dir_open_used = int(max_same_dir_open)
    _sl_mode = str(sl_mode).lower().strip()
    if _sl_mode not in SL_MODES:
        raise ValueError(f'sl_mode={sl_mode!r} is not one of {SL_MODES}')
    _entry_fill_model = str(entry_fill_model).lower().strip()
    if _entry_fill_model not in ENTRY_FILL_MODELS:
        raise ValueError(f'entry_fill_model={entry_fill_model!r} is not one of {ENTRY_FILL_MODELS}')
    _fill_model = str(fill_model).lower().strip()
    if _fill_model not in FILL_MODELS:
        raise ValueError(f'fill_model={fill_model!r} is not one of {FILL_MODELS}')
    res.fill_model_used = _fill_model
    res.entry_fill_model_used = _entry_fill_model
    diag: Dict[str, Any] = {}
    if structure_gate:
        if structure is None:
            raise ValueError('structure_gate=True but structure=None. Build the state with detect_market_structure(...) and pass it in, or set structure_gate=False. Silently passing every signal would make the gate a no-op.')
        if len(structure.last_break_kind) != len(bars):
            raise ValueError(f'structure_gate=True but structure was built on {len(structure.last_break_kind)} bars and the engine was given {len(bars)}. Misaligned lengths mean every index is wrong -- refuse rather than read garbage.')
    if record_equity_curve:
        equity_curve = np.zeros(n, dtype=np.float64)
    else:
        equity_curve = np.zeros(0, dtype=np.float64)
    cum_closed_pnl = 0.0
    last_mtm = 0.0
    conviction_model_obj: Optional[Any] = None
    if conviction_model_path and str(conflict_policy).lower().strip() == 'replace_conviction':
        try:
            import pickle as _pickle
            with open(conviction_model_path, 'rb') as _fh:
                conviction_model_obj = _pickle.load(_fh)
        except Exception as _e:
            conviction_model_obj = None
    elif str(conflict_policy).lower().strip() == 'replace_conviction':
        pass
    import sys as _sys
    import time as _time
    _eng_i = 0
    _eng_t0 = _time.perf_counter()
    for i in range(n):
        if _eng_i and _eng_i % 250000 == 0:
            _el = _time.perf_counter() - _eng_t0
            _sys.stderr.write(f'[vp_engine] bar={_eng_i}/{n} elapsed={_el:.1f}s rate={_eng_i / _el:.0f}/s open={len(open_trades)}\n')
            _sys.stderr.flush()
        _eng_i += 1
        b_open = _mv_open[i]
        b_high = _mv_high[i]
        b_low = _mv_low[i]
        b_close = _mv_close[i]
        b_time = _mv_times[i]
        _n_closed_before = len(closed_trades)
        if entry_mode in ('dual', 'immediate'):
            triggered = zones_at_bar.get(i, [])
            for zone in triggered:
                zid = id(zone)
                if zid in zone_already_fired:
                    res.n_skipped_already_fired += 1
                    continue
                if entry_mode == 'dual':
                    _inv_b = int(getattr(zone, 'inverted_bar', -1))
                    if 0 <= _inv_b <= i:
                        res.n_skipped_dual_inverted += 1
                        continue
                res.n_signals_emitted += 1
                _ss_admit, _ss_score = _structure_score_pass(i, zone, structure, vol_state, close, structure_gate, structure_score_min_abs, structure_score_lookback, structure_score_half_life, res)
                if not _ss_admit:
                    res.n_dropped_structure += 1
                    continue
                if not _vol_state_pass(i, vol_state, vol_state_min_rv_ratio_30s, vol_state_min_range_norm, vol_state_min_vel_5m, vol_state_min_vel_15m, vol_state_max_rv_ratio_30s, vol_state_max_wick_ratio, vol_state_min_expand_prev_day, vol_state_max_expand_prev_day):
                    res.n_dropped_vol_state += 1
                    continue
                if p95_cap is not None and p95_cap.skip_trades_during_warmup and (p95_cap.seed_constant_usd > 0.0) and (not p95_cap.is_warmup_complete(b_time)):
                    zone_already_fired.add(zid)
                    res.n_dropped_p95_warmup += 1
                    continue
                zone_already_fired.add(zid)
                if int(zone.direction) > 0:
                    entry_price = float(zone.zone_low)
                    direction = +1
                else:
                    entry_price = float(zone.zone_high)
                    direction = -1
                if structure_seq_flip_map:
                    _sig = _sequence_signature(structure, i, structure_seq_len or structure_score_lookback)
                    _mult = structure_seq_flip_map.get(_sig, 0)
                    if _mult < 0:
                        direction = -direction
                        if int(zone.direction) > 0:
                            entry_price = float(zone.zone_high)
                        else:
                            entry_price = float(zone.zone_low)
                        res.n_structure_seq_flipped += 1
                if trade_sides == 'long' and direction < 0:
                    res.n_dropped_sides += 1
                    continue
                if structure_seq_whitelist:
                    _side_lbl = 'long' if direction > 0 else 'short'
                    _sig5 = _sequence_signature(structure, i, structure_seq_len or structure_score_lookback)
                    if f'{_side_lbl}|{_sig5}' not in structure_seq_whitelist:
                        res.n_dropped_seq_whitelist += 1
                        continue
                if not _conflict_policy_pass(open_trades, direction, b_close, i, b_time, conflict_policy, max_same_dir_open, qty_oz, closed_trades, res, conviction_alpha=conviction_alpha, conviction_hysteresis_usd=conviction_hysteresis_usd, conviction_model=conviction_model_obj):
                    res.n_dropped_policy += 1
                    _c_aggr_dirty = True
                    continue
                _c_aggr_dirty = True
                zone_width = float(zone.zone_high) - float(zone.zone_low)
                next_order_id += 1
                pending_orders.append(PendingOrder(order_id=next_order_id, zone=zone, direction=direction, entry_price=entry_price, placed_bar=i, placed_time_ns=b_time, source='immediate', fvg_direction=int(zone.direction), zone_width=zone_width, death_bar=_zone_death_bar(zone), struct_verdict=_ss_score))
                res.n_orders_placed += 1
        inversions = zones_inverted_at_bar.get(i, [])
        for zone in inversions:
            zid = id(zone)
            if zid in zone_already_fired:
                res.n_skipped_already_fired += 1
                continue
            zone_already_fired.add(zid)
            if not _vol_state_pass(i, vol_state, vol_state_min_rv_ratio_30s, vol_state_min_range_norm, vol_state_min_vel_5m, vol_state_min_vel_15m, vol_state_max_rv_ratio_30s, vol_state_max_wick_ratio, vol_state_min_expand_prev_day, vol_state_max_expand_prev_day):
                res.n_dropped_vol_state += 1
                continue
            if p95_cap is not None and p95_cap.skip_trades_during_warmup and (p95_cap.seed_constant_usd > 0.0) and (not p95_cap.is_warmup_complete(b_time)):
                res.n_dropped_p95_warmup_sniper += 1
                continue
            res.n_inversions_detected += 1
            res.n_signals_emitted += 1
            if int(zone.direction) > 0:
                entry_price = float(zone.zone_low)
                direction = +1
            else:
                entry_price = float(zone.zone_high)
                direction = -1
            _ss_admit, _ss_score = _structure_score_pass(i, zone, structure, vol_state, close, structure_gate, structure_score_min_abs, structure_score_lookback, structure_score_half_life, res)
            if not _ss_admit:
                res.n_dropped_structure += 1
                continue
            if structure_seq_flip_map:
                _sig = _sequence_signature(structure, i, structure_seq_len or structure_score_lookback)
                _mult = structure_seq_flip_map.get(_sig, 0)
                if _mult < 0:
                    direction = -direction
                    if int(zone.direction) > 0:
                        entry_price = float(zone.zone_high)
                    else:
                        entry_price = float(zone.zone_low)
                    res.n_structure_seq_flipped += 1
            if trade_sides == 'long' and direction < 0:
                res.n_dropped_sides += 1
                continue
            if structure_seq_whitelist:
                _side_lbl = 'long' if direction > 0 else 'short'
                _sig5 = _sequence_signature(structure, i, structure_seq_len or structure_score_lookback)
                if f'{_side_lbl}|{_sig5}' not in structure_seq_whitelist:
                    res.n_dropped_seq_whitelist += 1
                    continue
            if not _conflict_policy_pass(open_trades, direction, b_close, i, b_time, conflict_policy, max_same_dir_open, qty_oz, closed_trades, res, conviction_alpha=conviction_alpha, conviction_hysteresis_usd=conviction_hysteresis_usd, conviction_model=conviction_model_obj):
                res.n_dropped_policy += 1
                _c_aggr_dirty = True
                continue
            _c_aggr_dirty = True
            zone_width = float(zone.zone_high) - float(zone.zone_low)
            next_order_id += 1
            pending_orders.append(PendingOrder(order_id=next_order_id, zone=zone, direction=direction, entry_price=entry_price, placed_bar=i, placed_time_ns=b_time, source='sniper', fvg_direction=int(zone.direction), zone_width=zone_width, death_bar=_zone_death_bar(zone), struct_verdict=_ss_score))
            res.n_orders_placed += 1
        if pending_orders:
            _still_pending: List[PendingOrder] = []
            for po in pending_orders:
                if po.death_bar and po.death_bar <= i:
                    po.cancelled = True
                    po.cancel_reason = 'zone_death'
                    res.n_orders_cancelled += 1
                    res.n_orders_cancelled_zone_death += 1
                    continue
                if entry_fill_model == ENTRY_FILL_TRIGGER:
                    _fill = True
                elif entry_fill_model == ENTRY_FILL_NEXT_OPEN:
                    _fill = i > po.placed_bar
                else:
                    _fill = i > po.placed_bar and _order_touched(b_low=b_low, b_high=b_high, entry_price=po.entry_price, direction=po.direction)
                if not _fill:
                    _still_pending.append(po)
                    continue
                _e = po.entry_price
                _d = po.direction
                _zw = po.zone_width
                _atr_v = float(atr[i]) if atr is not None and i < len(atr) else 0.0
                _trail_a = float(_atr1m[i]) if i < len(_atr1m) else 0.0
                _tp_band = _reachable_tp_band_usd(p95_cap=p95_cap, swing_cap=swing_cap, t_ns=b_time)
                sl_price, sl_ref = compute_structural_sl(entry_price=_e, direction=_d, zone_low=po.zone.zone_low, zone_high=po.zone.zone_high, vp_set=vp_set, t_ns=b_time, sl_mode=sl_mode, hvn_buffer_usd=hvn_buffer_usd, zone_width=_zw, sl_zone_mult=sl_zone_mult, sl_atr_mult=sl_atr_mult, atr_value=_atr_v, sl_nth_hvn_n=sl_nth_hvn_n, sl_min_usd=sl_min_usd, sl_max_usd=sl_max_usd, sl_hvn_max_distance_usd=sl_hvn_max_distance_usd, sl_search_radius_usd=_tp_band)
                tp_price, tp_ref, tp_price_2, tp_ref_2, partial_pct = compute_structural_tp(entry_price=_e, direction=_d, zone_low=po.zone.zone_low, zone_high=po.zone.zone_high, zone_width=_zw, vp_set=vp_set, t_ns=b_time, tp_mode=tp_mode, tp_max_distance_usd=tp_max_distance_usd, tp_zone_mult_fallback=tp_zone_mult_fallback, lvn_mode=lvn_mode, lvn_skip_usd=lvn_skip_usd, nth_hvn_n=nth_hvn_n, partial_close_pct=partial_close_pct, tp_search_radius_usd=_tp_band, tp2_mode=tp2_mode, tp2_nth_hvn_n=tp2_nth_hvn_n)
                tp_price, tp_ref = _apply_swing_cap(tp_price=tp_price, tp_ref=tp_ref, entry_price=_e, direction=_d, swing_cap=swing_cap, t_ns=b_time)
                tp_price, tp_ref = _apply_p95_cap(tp_price=tp_price, tp_ref=tp_ref, entry_price=_e, direction=_d, p95_cap=p95_cap, t_ns=b_time)
                tp_price_2, tp_ref_2 = _apply_p95_cap(tp_price=tp_price_2, tp_ref=tp_ref_2, entry_price=_e, direction=_d, p95_cap=p95_cap, t_ns=b_time)
                if tp2_swing_cap:
                    tp_price_2, tp_ref_2 = _apply_swing_cap(tp_price=tp_price_2, tp_ref=tp_ref_2, entry_price=_e, direction=_d, swing_cap=swing_cap, t_ns=b_time)
                tp_price, tp_price_2, tp_ref, tp_ref_2 = _separate_tp_legs(tp1=tp_price, tp2=tp_price_2, tp1_ref=tp_ref, tp2_ref=tp_ref_2, entry_price=_e, direction=_d)
                if not _conflict_policy_pass(open_trades, _d, b_close, i, b_time, conflict_policy, max_same_dir_open, qty_oz, closed_trades, res, conviction_alpha=conviction_alpha, conviction_hysteresis_usd=conviction_hysteresis_usd, conviction_model=conviction_model_obj):
                    res.n_dropped_policy_at_fill += 1
                    po.cancelled = True
                    po.cancel_reason = 'policy_at_fill'
                    res.n_orders_cancelled += 1
                    _c_aggr_dirty = True
                    continue
                _c_aggr_dirty = True
                if contra_hedge_stats:
                    res.n_fills_total += 1
                    _has_opp = False
                    for _ot in open_trades:
                        if _ot.direction != _d:
                            _has_opp = True
                            break
                    if _has_opp:
                        res.n_fills_opposite_open += 1
                next_trade_id += 1
                open_trades.append(OpenTrade(trade_id=next_trade_id, parent_zone_id=id(po.zone), entry_bar=i, entry_time_ns=b_time, entry_price=_e, direction=_d, stop_price=float(sl_price), target_price=float(tp_price), fvg_direction=po.fvg_direction, source=po.source, sl_mode=sl_mode, tp_mode=tp_mode, sl_struct_ref=sl_ref, tp_struct_ref=tp_ref, zone_width=_zw, zone_low=float(po.zone.zone_low), zone_high=float(po.zone.zone_high), target_price_2=float(tp_price_2) if tp_price_2 > 0 else 0.0, tp_struct_ref_2=tp_ref_2, partial_close_pct=partial_pct, initial_sl_distance_usd=abs(_e - float(sl_price)), initial_tp_distance_usd=abs(float(tp_price) - _e), peak_price=float(_e), trail_stop_price=0.0, trail_active=False, trail_atr_value=float(_trail_a), struct_verdict=po.struct_verdict, signal_bar=int(po.placed_bar)))
                po.filled = True
                res.n_orders_filled += 1
                _c_aggr_dirty = True
                if po.source == 'immediate':
                    res.n_immediate_filled += 1
                else:
                    res.n_sniper_filled += 1
            pending_orders = _still_pending
        if contra_hedge_stats and open_trades:
            if _c_aggr_dirty:
                _c_long_qty = 0.0
                _c_long_sum = 0.0
                _c_short_qty = 0.0
                _c_short_sum = 0.0
                for _ct in open_trades:
                    _cq = _ct.qty if _ct.qty > 0.0 else qty_oz
                    if _ct.direction > 0:
                        _c_long_qty += _cq
                        _c_long_sum += _cq * float(_ct.entry_price)
                    else:
                        _c_short_qty += _cq
                        _c_short_sum += _cq * float(_ct.entry_price)
                _c_aggr_dirty = False
            if _c_long_qty > 0.0 and _c_short_qty > 0.0:
                res.n_bars_both_sides_open += 1
                res.hedged_distance_bars += 1
                _c_dist_stat = _contra_distance_usd(_c_long_qty, _c_long_sum, _c_short_qty, _c_short_sum)
                if _c_dist_stat > res.hedged_distance_max_usd:
                    res.hedged_distance_max_usd = _c_dist_stat
        if contra_close and contra_close_distance_usd > 0.0 and open_trades:
            if _c_aggr_dirty:
                _c_long_qty = 0.0
                _c_long_sum = 0.0
                _c_short_qty = 0.0
                _c_short_sum = 0.0
                for _ct in open_trades:
                    _cq = _ct.qty if _ct.qty > 0.0 else qty_oz
                    if _ct.direction > 0:
                        _c_long_qty += _cq
                        _c_long_sum += _cq * float(_ct.entry_price)
                    else:
                        _c_short_qty += _cq
                        _c_short_sum += _cq * float(_ct.entry_price)
                _c_aggr_dirty = False
            _c_dist = _contra_distance_usd(_c_long_qty, _c_long_sum, _c_short_qty, _c_short_sum)
            if _c_dist >= contra_close_distance_usd:
                _c_match = _c_long_qty if _c_long_qty < _c_short_qty else _c_short_qty
                if _c_match > 0.0:
                    _c_price = _contra_close_price(b_open=b_open, b_low=b_low, b_high=b_high, b_close=b_close, fill_model=fill_model)
                    _c_long_ratio = _c_match / _c_long_qty
                    _c_short_ratio = _c_match / _c_short_qty
                    _c_new_open: List[OpenTrade] = []
                    _c_gross = 0.0
                    _c_legs = 0
                    for _ct in open_trades:
                        _cq = _ct.qty if _ct.qty > 0.0 else qty_oz
                        if _ct.direction > 0:
                            _c_ratio = _c_long_ratio
                        else:
                            _c_ratio = _c_short_ratio
                        _c_netted = _cq * _c_ratio
                        _c_resid = _cq - _c_netted
                        if _c_netted <= 1e-12:
                            _c_new_open.append(_ct)
                            continue
                        _c_entry = float(_ct.entry_price)
                        _c_dir = int(_ct.direction)
                        _c_leg_gross = (_c_price - _c_entry) * _c_dir * _c_netted
                        _c_gross += _c_leg_gross
                        _c_legs += 1
                        _c_hold = (b_time - _ct.entry_time_ns) / 1000000000.0
                        _c_sl_dist = abs(_c_entry - float(_ct.stop_price))
                        _c_tp_dist = abs(float(_ct.target_price) - _c_entry)
                        closed_trades.append(ClosedTrade(trade_id=_ct.trade_id, parent_zone_id=_ct.parent_zone_id, entry_bar=_ct.entry_bar, exit_bar=i, entry_time_ns=_ct.entry_time_ns, exit_time_ns=b_time, entry_price=_c_entry, exit_price=_c_price, direction=_c_dir, stop_price=_ct.stop_price, target_price=_ct.target_price, exit_reason='contra_close', hold_secs=_c_hold, pnl_gross_usd=_c_leg_gross, pnl_net_usd=_c_leg_gross, qty_oz=_c_netted, fvg_direction=_ct.fvg_direction, source=_ct.source, sl_mode=_ct.sl_mode, tp_mode=_ct.tp_mode, sl_struct_ref=_ct.sl_struct_ref, tp_struct_ref=_ct.tp_struct_ref, sl_distance_usd=_c_sl_dist, tp_distance_usd=_c_tp_dist, initial_sl_distance_usd=_ct.initial_sl_distance_usd, initial_tp_distance_usd=_ct.initial_tp_distance_usd, entry_poc_dist_usd=0.0, zone_width=_ct.zone_width, zone_low=_ct.zone_low, zone_high=_ct.zone_high, target_price_2=_ct.target_price_2, tp_struct_ref_2=_ct.tp_struct_ref_2, partial_close_pct=_ct.partial_close_pct, struct_verdict=_ct.struct_verdict, signal_bar=_ct.signal_bar))
                        if _c_resid > 1e-12:
                            _ct.qty = _c_resid
                            _c_new_open.append(_ct)
                    open_trades = _c_new_open
                    _c_aggr_dirty = True
                    res.n_contra_close_events += 1
                    res.n_contra_legs_closed += _c_legs
                    res.n_contra_qty_netted += _c_match
                    res.contra_gross_usd += _c_gross
                    if _c_dist > res.contra_distance_usd_max:
                        res.contra_distance_usd_max = _c_dist
        _need_live_state = on_bar_callback is not None or conviction_model_obj is not None or record_equity_curve
        if open_trades and _need_live_state:
            for ot in open_trades:
                if ot.direction > 0:
                    fav = b_high - ot.entry_price
                    adv = ot.entry_price - b_low
                else:
                    fav = ot.entry_price - b_low
                    adv = b_high - ot.entry_price
                if fav > ot.mfe_usd:
                    ot.mfe_usd = float(fav)
                if adv > ot.mae_usd:
                    ot.mae_usd = float(adv)
                if not ot.tp1_hit and ot.target_price > 0:
                    if ot.direction > 0 and b_high >= ot.target_price:
                        ot.tp1_hit = True
                    elif ot.direction < 0 and b_low <= ot.target_price:
                        ot.tp1_hit = True
                ot.live_pnl_usd = float((b_close - ot.entry_price) * ot.direction) * (ot.qty if ot.qty > 0.0 else qty_oz)
        _survivor_count = 0
        for _ot in open_trades:
            ot = _ot
            tp2_active = ot.partial_close_pct > 0 and ot.target_price_2 > 0
            if tp2_active:
                pct = ot.partial_close_pct
                tp2_price = ot.target_price_2
                tp1_price = ot.target_price
                entry_price = ot.entry_price
                direction = ot.direction
                if direction > 0:
                    tp1_hit = b_high >= tp1_price
                    tp2_hit = b_high >= tp2_price
                    sl_hit = b_low <= ot.stop_price
                else:
                    tp1_hit = b_low <= tp1_price
                    tp2_hit = b_low <= tp2_price
                    sl_hit = b_high >= ot.stop_price
                if _bar_resolves_stop_first(sl_hit=sl_hit, tp_hit=tp1_hit or tp2_hit, fill_model=fill_model):
                    tp1_hit = False
                    tp2_hit = False
                    sl_hit = True
                if tp1_hit and (not tp2_hit) and (not sl_hit):
                    pos_qty = ot.qty if ot.qty > 0.0 else qty_oz
                    exit_price = _resolve_exit_price(level=tp1_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=direction, is_stop=False, fill_model=fill_model)
                    qty_used = pos_qty * pct
                    gross = (exit_price - entry_price) * direction * qty_used
                    hold_secs = (b_time - ot.entry_time_ns) / 1000000000.0
                    sl_dist = abs(entry_price - ot.stop_price)
                    tp_dist = abs(tp1_price - entry_price)
                    closed_trades.append(ClosedTrade(trade_id=ot.trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, exit_bar=i, entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time, entry_price=entry_price, exit_price=exit_price, direction=direction, stop_price=entry_price, target_price=tp2_price, exit_reason='tp1', hold_secs=hold_secs, pnl_gross_usd=gross, pnl_net_usd=gross, qty_oz=qty_used, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref=ot.sl_struct_ref, tp_struct_ref=ot.tp_struct_ref, sl_distance_usd=sl_dist, tp_distance_usd=tp_dist, initial_sl_distance_usd=ot.initial_sl_distance_usd, initial_tp_distance_usd=ot.initial_tp_distance_usd, entry_poc_dist_usd=0.0, zone_width=ot.zone_width, zone_low=ot.zone_low, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar, zone_high=ot.zone_high))
                    remaining_qty = (ot.qty if ot.qty > 0.0 else qty_oz) * (1.0 - pct)
                    next_trade_id += 1
                    _c_aggr_dirty = True
                    open_trades[_survivor_count] = OpenTrade(trade_id=next_trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, entry_time_ns=ot.entry_time_ns, entry_price=entry_price, direction=direction, stop_price=entry_price, target_price=tp2_price, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref='breakeven', tp_struct_ref=ot.tp_struct_ref_2, zone_width=ot.zone_width, zone_low=ot.zone_low, zone_high=ot.zone_high, target_price_2=0.0, tp_struct_ref_2='', partial_close_pct=0.0, qty=remaining_qty, mfe_usd=ot.mfe_usd, mae_usd=ot.mae_usd, tp1_hit=ot.tp1_hit, initial_sl_distance_usd=ot.initial_sl_distance_usd, initial_tp_distance_usd=ot.initial_tp_distance_usd, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar)
                    _survivor_count += 1
                    continue
                elif tp2_hit or (sl_hit and (not tp1_hit)):
                    exit_price = _resolve_exit_price(level=tp2_price if tp2_hit else ot.stop_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=direction, is_stop=not tp2_hit, fill_model=fill_model)
                    qty_used = ot.qty if ot.qty > 0.0 else qty_oz
                    gross = (exit_price - entry_price) * direction * qty_used
                    hold_secs = (b_time - ot.entry_time_ns) / 1000000000.0
                    sl_dist = abs(entry_price - ot.stop_price)
                    tp_dist = abs(tp2_price - entry_price)
                    closed_trades.append(ClosedTrade(trade_id=ot.trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, exit_bar=i, entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time, entry_price=entry_price, exit_price=exit_price, direction=direction, stop_price=ot.stop_price, target_price=tp2_price, exit_reason='tp2' if tp2_hit else 'sl', hold_secs=hold_secs, pnl_gross_usd=gross, pnl_net_usd=gross, qty_oz=qty_used, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref=ot.sl_struct_ref, tp_struct_ref=ot.tp_struct_ref_2, sl_distance_usd=sl_dist, tp_distance_usd=tp_dist, initial_sl_distance_usd=ot.initial_sl_distance_usd, initial_tp_distance_usd=ot.initial_tp_distance_usd, entry_poc_dist_usd=0.0, zone_width=ot.zone_width, zone_low=ot.zone_low, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar, zone_high=ot.zone_high))
                    continue
                elif sl_hit:
                    exit_reason = 'sl'
                    exit_price = _resolve_exit_price(level=ot.stop_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=direction, is_stop=True, fill_model=fill_model)
                    qty_used = ot.qty if ot.qty > 0.0 else qty_oz
                    gross = (exit_price - entry_price) * direction * qty_used
                    hold_secs = (b_time - ot.entry_time_ns) / 1000000000.0
                    tp_dist = abs(ot.target_price - entry_price)
                    sl_dist = abs(entry_price - ot.stop_price)
                    svp = current_daily_vp(vp_set, ot.entry_time_ns)
                    poc_dist_signed = svp.poc - entry_price if svp else 0.0
                    closed_trades.append(ClosedTrade(trade_id=ot.trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, exit_bar=i, entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time, entry_price=entry_price, exit_price=exit_price, direction=direction, stop_price=exit_price, target_price=ot.target_price, exit_reason=exit_reason, hold_secs=hold_secs, pnl_gross_usd=gross, pnl_net_usd=gross, qty_oz=qty_used, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref=ot.sl_struct_ref, tp_struct_ref=ot.tp_struct_ref, sl_distance_usd=sl_dist, tp_distance_usd=tp_dist, entry_poc_dist_usd=poc_dist_signed, zone_width=ot.zone_width, zone_low=ot.zone_low, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar, zone_high=ot.zone_high))
                    continue
                else:
                    open_trades[_survivor_count] = ot
                    _survivor_count += 1
                    continue
            trail_hit = False
            _trail_for_side = _trail_on_long if ot.direction > 0 else _trail_on_short
            if _trail_for_side and ot.trail_atr_value > 0.0:
                _t_atr = float(ot.trail_atr_value)
                if ot.direction > 0:
                    if b_high > ot.peak_price:
                        ot.peak_price = float(b_high)
                    _mfe = float(ot.peak_price) - float(ot.entry_price)
                else:
                    if ot.peak_price == 0.0 or b_low < ot.peak_price:
                        ot.peak_price = float(b_low)
                    _mfe = float(ot.entry_price) - float(ot.peak_price)
                _long = ot.direction > 0
                _act_usd = trail_long_activation_usd if _long and trail_long_activation_usd > 0.0 else float(trail_activation_mult) * _t_atr
                _gb = trail_long_giveback_frac if _long and trail_long_giveback_frac > 0.0 else trail_giveback_frac
                _md = trail_long_mode if _long and trail_long_mode else trail_mode
                if not ot.trail_active:
                    if _mfe >= _act_usd:
                        ot.trail_active = True
                if ot.trail_active:
                    _lvl = _trail_level_usd(entry_price=float(ot.entry_price), peak_price=float(ot.peak_price), direction=int(ot.direction), atr_value=_t_atr, trail_atr_mult=float(trail_atr_mult), trail_giveback_frac=float(_gb), trail_mode=str(_md))
                    if _lvl != 0.0:
                        if ot.direction > 0:
                            if not ot.trail_stop_price or _lvl > ot.trail_stop_price:
                                ot.trail_stop_price = float(_lvl)
                            trail_hit = b_low <= ot.trail_stop_price
                        else:
                            if not ot.trail_stop_price or _lvl < ot.trail_stop_price:
                                ot.trail_stop_price = float(_lvl)
                            trail_hit = b_high >= ot.trail_stop_price
            if ot.direction > 0:
                tp_hit = b_high >= ot.target_price
                sl_hit = b_low <= ot.stop_price
            else:
                tp_hit = b_low <= ot.target_price
                sl_hit = b_high >= ot.stop_price
            if tp_hit and (sl_hit or trail_hit) and (fill_model != FILL_MODEL_MID):
                if sl_hit:
                    exit_reason = 'sl'
                    exit_price = _resolve_exit_price(level=ot.stop_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=ot.direction, is_stop=True, fill_model=fill_model)
                else:
                    exit_reason = 'trail_stop'
                    exit_price = _resolve_exit_price(level=ot.trail_stop_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=ot.direction, is_stop=True, fill_model=fill_model)
            elif tp_hit and (sl_hit or trail_hit):
                if sl_hit:
                    exit_reason = 'sl'
                    exit_price = ot.stop_price
                else:
                    exit_reason = 'trail_stop'
                    exit_price = ot.trail_stop_price
            elif tp_hit:
                exit_reason = 'tp'
                exit_price = _resolve_exit_price(level=ot.target_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=ot.direction, is_stop=False, fill_model=fill_model)
            elif sl_hit:
                exit_reason = 'sl'
                exit_price = _resolve_exit_price(level=ot.stop_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=ot.direction, is_stop=True, fill_model=fill_model)
            elif trail_hit:
                exit_reason = 'trail_stop'
                exit_price = _resolve_exit_price(level=ot.trail_stop_price, b_open=b_open, b_low=b_low, b_high=b_high, direction=ot.direction, is_stop=True, fill_model=fill_model)
            elif max_hold_secs > 0.0 and b_time - ot.entry_time_ns >= max_hold_secs * 1000000000.0:
                exit_reason = 'max_hold'
                exit_price = b_close
            else:
                open_trades[_survivor_count] = ot
                _survivor_count += 1
                continue
            pos_qty = ot.qty if ot.qty > 0.0 else qty_oz
            gross = (exit_price - ot.entry_price) * ot.direction * pos_qty
            hold_secs = (b_time - ot.entry_time_ns) / 1000000000.0
            sl_dist = abs(ot.entry_price - ot.stop_price)
            tp_dist = abs(ot.target_price - ot.entry_price)
            svp = current_daily_vp(vp_set, ot.entry_time_ns)
            poc_dist_signed = svp.poc - ot.entry_price if svp else 0.0
            closed_trades.append(ClosedTrade(trade_id=ot.trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, exit_bar=i, entry_time_ns=ot.entry_time_ns, exit_time_ns=b_time, entry_price=ot.entry_price, exit_price=exit_price, direction=ot.direction, stop_price=ot.stop_price, target_price=ot.target_price, exit_reason=exit_reason, hold_secs=hold_secs, pnl_gross_usd=gross, pnl_net_usd=gross, qty_oz=pos_qty, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref=ot.sl_struct_ref, tp_struct_ref=ot.tp_struct_ref, sl_distance_usd=sl_dist, tp_distance_usd=tp_dist, initial_sl_distance_usd=ot.initial_sl_distance_usd, initial_tp_distance_usd=ot.initial_tp_distance_usd, entry_poc_dist_usd=poc_dist_signed, zone_width=ot.zone_width, zone_low=ot.zone_low, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar, zone_high=ot.zone_high))
        if _survivor_count != len(open_trades):
            _c_aggr_dirty = True
        del open_trades[_survivor_count:]
        MTM_RESAMPLE_BARS = 60
        if _n_closed_before < len(closed_trades):
            cum_closed_pnl += sum((t.pnl_net_usd for t in closed_trades[_n_closed_before:]))
        if i % MTM_RESAMPLE_BARS == 0 or i == n - 1:
            last_mtm = sum((float(ot.live_pnl_usd) for ot in open_trades))
        if equity_curve.size > 0:
            equity_curve[i] = cum_closed_pnl + last_mtm
        if on_bar_callback is not None and open_trades and (i % max(1, on_bar_every_k) == 0):
            _vs = None
            if vol_state is not None:
                if hasattr(vol_state, 'series'):
                    _src = vol_state.series
                elif hasattr(vol_state, 'items'):
                    _src = dict(vol_state.items())
                else:
                    _src = {}
                _vs = {k: float(v[i]) if i < len(v) else 0.0 for k, v in _src.items()}
            on_bar_callback(i, b_time, b_close, b_high, b_low, list(open_trades), _vs)
    last_close = float(close[-1])
    last_time = int(times[-1])
    for ot in open_trades:
        pos_qty = ot.qty if ot.qty > 0.0 else qty_oz
        gross = (last_close - ot.entry_price) * ot.direction * pos_qty
        hold_secs = (last_time - ot.entry_time_ns) / 1000000000.0
        sl_dist = abs(ot.entry_price - ot.stop_price)
        tp_dist = abs(ot.target_price - ot.entry_price)
        svp = current_daily_vp(vp_set, ot.entry_time_ns)
        poc_dist_signed = svp.poc - ot.entry_price if svp is not None else 0.0
        closed_trades.append(ClosedTrade(trade_id=ot.trade_id, parent_zone_id=ot.parent_zone_id, entry_bar=ot.entry_bar, exit_bar=n - 1, entry_time_ns=ot.entry_time_ns, exit_time_ns=last_time, entry_price=ot.entry_price, exit_price=last_close, direction=ot.direction, stop_price=ot.stop_price, target_price=ot.target_price, exit_reason='eod', hold_secs=hold_secs, pnl_gross_usd=gross, pnl_net_usd=gross, qty_oz=pos_qty, fvg_direction=ot.fvg_direction, source=ot.source, sl_mode=ot.sl_mode, tp_mode=ot.tp_mode, sl_struct_ref=ot.sl_struct_ref, tp_struct_ref=ot.tp_struct_ref, sl_distance_usd=sl_dist, tp_distance_usd=tp_dist, initial_sl_distance_usd=ot.initial_sl_distance_usd, initial_tp_distance_usd=ot.initial_tp_distance_usd, entry_poc_dist_usd=poc_dist_signed, zone_width=ot.zone_width, zone_low=ot.zone_low, struct_verdict=ot.struct_verdict, signal_bar=ot.signal_bar, zone_high=ot.zone_high, target_price_2=ot.target_price_2, tp_struct_ref_2=ot.tp_struct_ref_2, partial_close_pct=ot.partial_close_pct))
    res.trades = closed_trades
    res.equity_curve = equity_curve
    _accounted = res.n_orders_placed + res.n_dropped_vol_state + res.n_dropped_p95_warmup + res.n_dropped_p95_warmup_sniper + res.n_dropped_policy + res.n_dropped_structure + res.n_dropped_seq_whitelist
    diag['signals_emitted'] = int(res.n_signals_emitted)
    diag['signals_accounted'] = int(_accounted)
    diag['signals_unaccounted'] = int(res.n_signals_emitted - _accounted)
    diag['order_lifecycle'] = {'placed': int(res.n_orders_placed), 'filled': int(res.n_orders_filled), 'cancelled': int(res.n_orders_cancelled), 'cancelled_zone_death': int(res.n_orders_cancelled_zone_death), 'cancelled_expired': int(res.n_orders_cancelled_expired), 'cancelled_policy_at_fill': int(res.n_dropped_policy_at_fill), 'still_resting_at_eod': int(len(pending_orders)), 'unaccounted': int(res.n_orders_placed - res.n_orders_filled - res.n_orders_cancelled - len(pending_orders))}
    diag['gate_breakdown'] = {'structure': int(res.n_dropped_structure), 'seq_whitelist': int(res.n_dropped_seq_whitelist), 'vol_state': int(res.n_dropped_vol_state), 'p95_warmup': int(res.n_dropped_p95_warmup), 'p95_warmup_sniper': int(res.n_dropped_p95_warmup_sniper), 'conflict_policy': int(res.n_dropped_policy), 'conflict_policy_at_fill': int(res.n_dropped_policy_at_fill), 'orders_placed': int(res.n_orders_placed), 'filled_immediate': int(res.n_immediate_filled), 'filled_sniper': int(res.n_sniper_filled), 'skipped_already_fired': int(res.n_skipped_already_fired), 'skipped_dual_inverted': int(res.n_skipped_dual_inverted), 'zones_bucketized': int(res.n_zones_bucketized), 'inversions_detected': int(res.n_inversions_detected)}
    diag['structure_gate'] = {'enabled': bool(structure_gate), 'max_age_bars': int(structure_max_age_bars), 'aligned': int(res.n_structure_aligned), 'opposed': int(res.n_structure_opposed), 'no_event': int(res.n_structure_no_event)}
    diag['fill_model'] = res.fill_model_used
    diag['entry_fill_model'] = res.entry_fill_model_used
    return (res, diag)
