from __future__ import annotations
from typing import Any
from bt._engine.rolling_p95 import STAT_P2T_WINDOW
FOREX_LOTS = 0.01
FOREX_CONTRACT_SIZE = 100.0
FOREX_LEVERAGE = 100.0
ENTRY_MODE = 'dual'
ENTRY_FILL_MODEL = 'touch'
FILL_MODEL = 'pessimistic'
SL_MODE = 'hvn_lvn'
SL_ZONE_MULT = 40.0
SL_NTH_HVN_N = 1
SL_MIN_USD = 0.94
SL_MAX_USD = 15.0
SL_HVN_MAX_DISTANCE_USD = 0.0
HVN_BUFFER_USD = 0.1
LVN_SKIP_USD = 0.3
LVN_MODE = 'skip'
NTH_HVN_N = 3
TP_ZONE_MULT_FALLBACK = 200.0
TP_MAX_DISTANCE_USD = 0.0
TP_MODE = 'hvn_single'
TRAIL_MODE = 'combined'
TRAIL_ATR_MULT = 2.0
TRAIL_ACTIVATION_MULT = 1.0
TRAIL_GIVEBACK_FRAC = 0.5
TRAIL_SIDES = 'both'
TRAIL_LONG_MODE = 'giveback'
TRAIL_LONG_ACTIVATION_USD = 10.0
TRAIL_LONG_GIVEBACK_FRAC = 0.25
MAX_HOLD_SECS = 0.0
VOL_STATE_MIN_RANGE_NORM = 1.0
VOL_STATE_MIN_VEL_5M = 0.0
VOL_STATE_MIN_VEL_15M = 0.0
VOL_STATE_MIN_RV_RATIO_30S = 0.0
VOL_STATE_MAX_RV_RATIO_30S = 0.0
VOL_STATE_MAX_WICK_RATIO = 0.0
VOL_STATE_MIN_EXPAND_PREV_DAY = 1.0
VOL_STATE_MAX_EXPAND_PREV_DAY = 0.0
STRUCTURE_GATE = False
STRUCTURE_MAX_AGE_BARS = 300
TRADE_SIDES = 'both'
STRUCTURE_SEQ_WHITELIST: frozenset[str] = frozenset({'long|BBBBB', 'long|BBBBc', 'long|BBBcC', 'long|BBBcb', 'long|BBcbb', 'long|BcCBc', 'long|BcbbC', 'long|Bcbbb', 'long|CBBBc', 'long|CBBcb', 'long|CBcCc', 'long|CBcbC', 'long|CBcbb', 'long|CcCBB', 'long|CcCcC', 'long|CcCcb', 'long|CcbCB', 'long|CcbCc', 'long|Ccbbb', 'long|bCBcb', 'long|bCcCB', 'long|bCcCc', 'long|bCcbC', 'long|bCcbb', 'long|bbCBc', 'long|bbCcC', 'long|bbCcb', 'long|bbbCB', 'long|bbbCc', 'long|bbbbC', 'long|bbbbb', 'long|cCBcb', 'long|cCcbC', 'long|cCcbb', 'long|cbCBB', 'long|cbCBc', 'long|cbCcb', 'long|cbbCB', 'long|cbbbb', 'short|BBBBB', 'short|BBBBc', 'short|BcCBB', 'short|BcCcC', 'short|BcbCB', 'short|BcbCc', 'short|CBBBB', 'short|CBBBc', 'short|CBcCc', 'short|CcbCB', 'short|Ccbbb', 'short|bCBBB', 'short|bCBBc', 'short|bCBcb', 'short|bCcCB', 'short|bbCBB', 'short|bbbCc', 'short|cCBBc', 'short|cCBcC', 'short|cCcCc', 'short|cCcbC', 'short|cbCBc', 'short|cbCcC', 'short|cbbCc'})
MAX_SAME_DIR_OPEN = 20
CONFLICT_POLICY = 'stack'
CONTRA_CLOSE = True
CONTRA_CLOSE_DISTANCE_USD = 5.0
SWING_CAP_PCT = 3.0
SWING_LOOKBACK_SECS = 86400
TP_P95_LOOKBACK_SECS = 604800
TP_P95_INNER_SECS = 3600
TP_P95_Q = 95.0
TP_P95_STATISTIC = STAT_P2T_WINDOW
FOREX_P95_RET_USD = 8.0
DETECTOR_FVG_MIN_ZONE_USD = 0.3
DETECTOR_MAX_ZONE_AGE_BARS = 3600
DETECTOR_FVG_DISPLACEMENT_RATIO = 0.0
DETECTOR_FVG_BODY_DEFINITION = 'body'
DETECTOR_PLAYED_OUT_MIN_EXTENSION_USD = 0.0
DETECTOR_INVALIDATION_MIN_PIERCE_USD = 0.05
DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS = 2
FVG_BODY_ONLY_MITIGATION = True
FVG_BODY_ONLY_INVALIDATION = True
DETECTOR_REQUIRE_RETEST_TO_INVERT = True
DETECTOR_SUPERSEDE_ON_NEW = True
DETECTOR_MIN_LIFETIME_SECS = 3
ATR_LEN = 1200
DETECTOR_RESAMPLE_SECS = 60
STRICT_WICK_REQUIRED = False
STRICT_WICK_MIN_WICK_PRICE_PCT = 0.00025
STRICT_WICK_RECOMPUTE_SECS = 3600
VP_SESSION_SECS = 86400
VP_ROLLING_SECS = 604800
VP_ROWS = 200
VP_VA_PCT = 70.0
VP_HVN_PEAK_PCT = 30.0
VP_HVN_MIN_PROMINENCE_PCT = 15.0
VP_HVN_ZONE_PCT = 50.0
VP_LVN_THRESHOLD_PCT = 25.0
VP_MIN_ZONE_USD = 0.5
STRUCTURE_RESAMPLE_SECS = 60
STRUCTURE_PIVOT_LEN = 9
_VP_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'vp_session_secs', 'vp_rolling_secs', 'vp_rows', 'vp_va_pct', 'vp_hvn_peak_pct', 'vp_hvn_min_prominence_pct', 'vp_hvn_zone_pct', 'vp_lvn_threshold_pct', 'vp_min_zone_usd'})
_DETECTOR_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'fvg_min_zone_usd', 'max_zone_age_bars', 'resample_to_n_secs', 'fvg_displacement_ratio', 'fvg_body_definition', 'body_only_mitigation', 'body_only_invalidation', 'played_out_min_extension_usd', 'invalidation_min_pierce_usd', 'invalidation_min_consecutive_bars', 'require_retest_to_invert', 'supersede_on_new', 'fvg_min_lifetime_secs', 'strict_wick_required'})
_ENGINE_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'sl_mode', 'sl_zone_mult', 'sl_nth_hvn_n', 'sl_min_usd', 'sl_max_usd', 'sl_hvn_max_distance_usd', 'trail_mode', 'trail_atr_mult', 'trail_activation_mult', 'trail_giveback_frac', 'trail_sides', 'trail_long_mode', 'trail_long_activation_usd', 'trail_long_giveback_frac', 'max_hold_secs', 'atr_1m', 'hvn_buffer_usd', 'lvn_skip_usd', 'lvn_mode', 'tp_mode', 'tp_zone_mult_fallback', 'tp_max_distance_usd', 'p95_cap', 'nth_hvn_n', 'structure', 'structure_gate', 'structure_max_age_bars', 'vol_state', 'structure_score_lookback', 'structure_score_half_life', 'structure_score_min_abs', 'structure_seq_flip_map', 'structure_seq_len', 'structure_seq_whitelist', 'swing_cap', 'vol_state_min_rv_ratio_30s', 'vol_state_min_range_norm', 'vol_state_min_vel_5m', 'vol_state_min_vel_15m', 'vol_state_min_expand_prev_day', 'vol_state_max_rv_ratio_30s', 'vol_state_max_wick_ratio', 'vol_state_max_expand_prev_day', 'max_same_dir_open', 'conflict_policy', 'trade_sides', 'entry_fill_model', 'fill_model', 'entry_mode', 'tp_mode', 'contra_close', 'contra_close_distance_usd'})
_STRUCTURE_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'structure_pivot_len', 'structure_resample_secs'})
_SWING_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'swing_cap_pct', 'swing_lookback_secs'})
_P95_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'tp_p95_lookback_secs', 'tp_p95_inner_secs', 'tp_p95_q', 'tp_p95_statistic', 'seed_path', 'seed_constant_usd'})
_LIVE_P95_OVERRIDES_ALLOWED: frozenset[str] = frozenset({'tp_p95_lookback_secs', 'tp_p95_inner_secs', 'tp_p95_q', 'tp_p95_statistic', 'seed_path', 'seed_constant_usd', 'max_inner_bars'})
_SWING_KNOB_TO_PARAM: dict[str, str] = {'swing_cap_pct': 'cap_pct', 'swing_lookback_secs': 'lookback_secs'}
_P95_KNOB_TO_PARAM: dict[str, str] = {'tp_p95_lookback_secs': 'lookback_secs', 'tp_p95_inner_secs': 'inner_secs', 'tp_p95_q': 'p95_q', 'tp_p95_statistic': 'statistic', 'seed_path': 'seed_path', 'seed_constant_usd': 'seed_constant_usd'}
_LIVE_P95_KNOB_TO_PARAM: dict[str, str] = {**_P95_KNOB_TO_PARAM, 'max_inner_bars': 'max_inner_bars'}
_STRUCTURE_KNOB_TO_PARAM: dict[str, str] = {'structure_pivot_len': 'pivot_len', 'structure_resample_secs': 'resample_to_n_secs'}

def _translate(overrides: dict[str, Any], knob_to_param: dict[str, str]) -> dict[str, Any]:
    unmapped = sorted(set(overrides) - set(knob_to_param))
    if unmapped:
        raise KeyError(f'no target-parameter mapping for {unmapped}; known mappings: {sorted(knob_to_param)}')
    return {knob_to_param[k]: v for k, v in overrides.items()}

def build_engine_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _ENGINE_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for engine_kwargs: {sorted(illegal)}. Allowed: {sorted(_ENGINE_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(sl_mode=SL_MODE, sl_zone_mult=SL_ZONE_MULT, sl_nth_hvn_n=SL_NTH_HVN_N, sl_min_usd=SL_MIN_USD, sl_max_usd=SL_MAX_USD, sl_hvn_max_distance_usd=SL_HVN_MAX_DISTANCE_USD, trail_mode=TRAIL_MODE, trail_atr_mult=TRAIL_ATR_MULT, trail_activation_mult=TRAIL_ACTIVATION_MULT, trail_giveback_frac=TRAIL_GIVEBACK_FRAC, trail_sides=TRAIL_SIDES, trail_long_mode=TRAIL_LONG_MODE, trail_long_activation_usd=TRAIL_LONG_ACTIVATION_USD, trail_long_giveback_frac=TRAIL_LONG_GIVEBACK_FRAC, max_hold_secs=MAX_HOLD_SECS, hvn_buffer_usd=HVN_BUFFER_USD, lvn_skip_usd=LVN_SKIP_USD, lvn_mode=LVN_MODE, tp_mode=TP_MODE, tp_zone_mult_fallback=TP_ZONE_MULT_FALLBACK, tp_max_distance_usd=TP_MAX_DISTANCE_USD, nth_hvn_n=NTH_HVN_N, structure_gate=STRUCTURE_GATE, structure_max_age_bars=STRUCTURE_MAX_AGE_BARS, vol_state_min_vel_5m=VOL_STATE_MIN_VEL_5M, vol_state_min_vel_15m=VOL_STATE_MIN_VEL_15M, vol_state_min_rv_ratio_30s=VOL_STATE_MIN_RV_RATIO_30S, vol_state_min_range_norm=VOL_STATE_MIN_RANGE_NORM, vol_state_min_expand_prev_day=VOL_STATE_MIN_EXPAND_PREV_DAY, vol_state_max_rv_ratio_30s=VOL_STATE_MAX_RV_RATIO_30S, vol_state_max_wick_ratio=VOL_STATE_MAX_WICK_RATIO, vol_state_max_expand_prev_day=VOL_STATE_MAX_EXPAND_PREV_DAY, max_same_dir_open=MAX_SAME_DIR_OPEN, conflict_policy=CONFLICT_POLICY, trade_sides=TRADE_SIDES, structure_seq_whitelist=STRUCTURE_SEQ_WHITELIST, contra_close=CONTRA_CLOSE, contra_close_distance_usd=CONTRA_CLOSE_DISTANCE_USD, entry_fill_model=ENTRY_FILL_MODEL, fill_model=FILL_MODEL, entry_mode=ENTRY_MODE)
    base.update(overrides)
    return base

def build_vp_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _VP_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for VP kwargs: {sorted(illegal)}. Allowed: {sorted(_VP_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(vp_session_secs=VP_SESSION_SECS, vp_rolling_secs=VP_ROLLING_SECS, vp_rows=VP_ROWS, vp_va_pct=VP_VA_PCT, vp_hvn_peak_pct=VP_HVN_PEAK_PCT, vp_hvn_min_prominence_pct=VP_HVN_MIN_PROMINENCE_PCT, vp_hvn_zone_pct=VP_HVN_ZONE_PCT, vp_lvn_threshold_pct=VP_LVN_THRESHOLD_PCT, vp_min_zone_usd=VP_MIN_ZONE_USD)
    base.update(overrides)
    return base

def build_swing_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _SWING_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for swing kwargs: {sorted(illegal)}. Allowed: {sorted(_SWING_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(cap_pct=SWING_CAP_PCT, lookback_secs=SWING_LOOKBACK_SECS)
    base.update(_translate(overrides, _SWING_KNOB_TO_PARAM))
    return base

def build_p95_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _P95_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for p95 kwargs: {sorted(illegal)}. Allowed: {sorted(_P95_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(lookback_secs=TP_P95_LOOKBACK_SECS, inner_secs=TP_P95_INNER_SECS, p95_q=TP_P95_Q, p95_mult=1.0, statistic=TP_P95_STATISTIC, seed_path='constant', seed_constant_usd=0.0)
    base.update(_translate(overrides, _P95_KNOB_TO_PARAM))
    return base

def build_live_p95_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _LIVE_P95_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for live p95 kwargs: {sorted(illegal)}. Allowed: {sorted(_LIVE_P95_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(lookback_secs=TP_P95_LOOKBACK_SECS, inner_secs=TP_P95_INNER_SECS, p95_q=TP_P95_Q, p95_mult=1.0, statistic=TP_P95_STATISTIC, seed_path='constant', seed_constant_usd=FOREX_P95_RET_USD, max_inner_bars=4096)
    base.update(_translate(overrides, _LIVE_P95_KNOB_TO_PARAM))
    return base

def build_structure_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _STRUCTURE_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for structure kwargs: {sorted(illegal)}. Allowed: {sorted(_STRUCTURE_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(pivot_len=STRUCTURE_PIVOT_LEN, resample_to_n_secs=STRUCTURE_RESAMPLE_SECS)
    base.update(_translate(overrides, _STRUCTURE_KNOB_TO_PARAM))
    return base

def build_detector_kwargs(**overrides: Any) -> dict[str, Any]:
    illegal = set(overrides) - _DETECTOR_OVERRIDES_ALLOWED
    if illegal:
        raise KeyError(f'illegal override keys for detector kwargs: {sorted(illegal)}. Allowed: {sorted(_DETECTOR_OVERRIDES_ALLOWED)}')
    base: dict[str, Any] = dict(fvg_min_zone_usd=DETECTOR_FVG_MIN_ZONE_USD, max_zone_age_bars=DETECTOR_MAX_ZONE_AGE_BARS, resample_to_n_secs=DETECTOR_RESAMPLE_SECS, fvg_displacement_ratio=DETECTOR_FVG_DISPLACEMENT_RATIO, fvg_body_definition=DETECTOR_FVG_BODY_DEFINITION, body_only_mitigation=FVG_BODY_ONLY_MITIGATION, body_only_invalidation=FVG_BODY_ONLY_INVALIDATION, played_out_min_extension_usd=DETECTOR_PLAYED_OUT_MIN_EXTENSION_USD, invalidation_min_pierce_usd=DETECTOR_INVALIDATION_MIN_PIERCE_USD, invalidation_min_consecutive_bars=DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS, require_retest_to_invert=DETECTOR_REQUIRE_RETEST_TO_INVERT, supersede_on_new=DETECTOR_SUPERSEDE_ON_NEW, fvg_min_lifetime_secs=DETECTOR_MIN_LIFETIME_SECS, strict_wick_required=STRICT_WICK_REQUIRED)
    base.update(overrides)
    return base
__all__ = ['build_engine_kwargs', 'build_vp_kwargs', 'build_swing_kwargs', 'build_p95_kwargs', 'build_live_p95_kwargs', 'build_structure_kwargs', 'build_detector_kwargs', 'FOREX_LOTS', 'FOREX_CONTRACT_SIZE', 'ENTRY_MODE', 'ENTRY_FILL_MODEL', 'FILL_MODEL', 'SL_MODE', 'SL_ZONE_MULT', 'SL_NTH_HVN_N', 'SL_MIN_USD', 'SL_MAX_USD', 'SL_HVN_MAX_DISTANCE_USD', 'HVN_BUFFER_USD', 'LVN_SKIP_USD', 'LVN_MODE', 'TP_MODE', 'NTH_HVN_N', 'TP_ZONE_MULT_FALLBACK', 'TP_MAX_DISTANCE_USD', 'VOL_STATE_MIN_RANGE_NORM', 'VOL_STATE_MIN_VEL_5M', 'VOL_STATE_MIN_VEL_15M', 'VOL_STATE_MIN_RV_RATIO_30S', 'VOL_STATE_MAX_RV_RATIO_30S', 'VOL_STATE_MAX_WICK_RATIO', 'VOL_STATE_MIN_EXPAND_PREV_DAY', 'VOL_STATE_MAX_EXPAND_PREV_DAY', 'STRUCTURE_GATE', 'STRUCTURE_MAX_AGE_BARS', 'STRUCTURE_SEQ_WHITELIST', 'TRADE_SIDES', 'MAX_SAME_DIR_OPEN', 'CONFLICT_POLICY', 'SWING_CAP_PCT', 'SWING_LOOKBACK_SECS', 'TP_P95_LOOKBACK_SECS', 'TP_P95_INNER_SECS', 'TP_P95_Q', 'TP_P95_STATISTIC', 'FOREX_P95_RET_USD', 'DETECTOR_FVG_MIN_ZONE_USD', 'DETECTOR_MAX_ZONE_AGE_BARS', 'DETECTOR_FVG_DISPLACEMENT_RATIO', 'DETECTOR_FVG_BODY_DEFINITION', 'DETECTOR_PLAYED_OUT_MIN_EXTENSION_USD', 'DETECTOR_INVALIDATION_MIN_PIERCE_USD', 'DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS', 'FVG_BODY_ONLY_MITIGATION', 'FVG_BODY_ONLY_INVALIDATION', 'DETECTOR_REQUIRE_RETEST_TO_INVERT', 'DETECTOR_SUPERSEDE_ON_NEW', 'DETECTOR_MIN_LIFETIME_SECS', 'DETECTOR_RESAMPLE_SECS', 'ATR_LEN', 'STRICT_WICK_REQUIRED', 'STRICT_WICK_MIN_WICK_PRICE_PCT', 'STRICT_WICK_RECOMPUTE_SECS', 'VP_SESSION_SECS', 'VP_ROLLING_SECS', 'VP_ROWS', 'VP_VA_PCT', 'VP_HVN_PEAK_PCT', 'VP_HVN_MIN_PROMINENCE_PCT', 'VP_HVN_ZONE_PCT', 'VP_LVN_THRESHOLD_PCT', 'VP_MIN_ZONE_USD', 'STRUCTURE_RESAMPLE_SECS', 'STRUCTURE_PIVOT_LEN', '_VP_OVERRIDES_ALLOWED', '_DETECTOR_OVERRIDES_ALLOWED', '_ENGINE_OVERRIDES_ALLOWED', '_STRUCTURE_OVERRIDES_ALLOWED', '_SWING_OVERRIDES_ALLOWED', '_P95_OVERRIDES_ALLOWED', '_LIVE_P95_OVERRIDES_ALLOWED']
