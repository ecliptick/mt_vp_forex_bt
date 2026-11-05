from __future__ import annotations
import argparse
import hashlib
import json
import math
import pickle
import sys
import time
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
import numpy as np
import pandas as pd
from bt.optimal_config import FOREX_CONTRACT_SIZE, FOREX_LOTS, STRICT_WICK_MIN_WICK_PRICE_PCT, _DETECTOR_OVERRIDES_ALLOWED as _DETECTOR_OA_FROM_OPT, _STRUCTURE_OVERRIDES_ALLOWED as _STRUCTURE_OA_FROM_OPT, build_detector_kwargs, build_engine_kwargs, build_p95_kwargs, build_structure_kwargs, build_swing_kwargs, build_vp_kwargs
from bt.vp_engine import run_vp_sniper_backtest_forex
from bt._engine.timebase import to_ns_arr
from runtime.results import registry
DEFAULT_DATA = Path('C:\\coding\\ict_tier_v2\\data\\XAUUSD_S1_1y.parquet')
CACHE_ROOT = _REPO_ROOT / 'notebooks' / '.cache'
RESULTS_ROOT = _REPO_ROOT / 'runtime' / 'sweeps' / '_results'
_TS_FMT = '%Y-%m-%dT%H:%M:%SZ'
DETECTOR_KWARGS: dict[str, Any] = build_detector_kwargs()
VP_KWARGS: dict[str, Any] = build_vp_kwargs()
SWING_KWARGS: dict[str, Any] = build_swing_kwargs()
P95_KWARGS: dict[str, Any] = build_p95_kwargs()
STRUCTURE_KWARGS: dict[str, Any] = build_structure_kwargs()

def detector_kwargs_from_recipe(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = build_detector_kwargs(**overrides)
    strict_wick_required = bool(kwargs.get('strict_wick_required', False))
    if strict_wick_required:
        kwargs.pop('strict_wick_min_wick_usd', None)
        kwargs['_strict_wick_use_floor_array'] = True
        kwargs['_strict_wick_price_pct'] = float(STRICT_WICK_MIN_WICK_PRICE_PCT)
    else:
        kwargs['strict_wick_min_wick_usd'] = 0.0
    return kwargs

def daily_mtm_pnl(trades, bars: pd.DataFrame) -> pd.Series:
    t_ns = to_ns_arr(bars)
    day = pd.to_datetime(t_ns, utc=True).normalize()
    last = np.flatnonzero(np.r_[day[1:] != day[:-1], True])
    d_ns, d_close, d_idx = (t_ns[last], bars['close'].to_numpy(np.float64)[last], day[last])
    eq = np.zeros(len(last))
    for t in trades:
        q = float(t.qty_oz) * int(t.direction)
        a = int(np.searchsorted(d_ns, int(t.entry_time_ns)))
        b = int(np.searchsorted(d_ns, int(t.exit_time_ns)))
        if b > a:
            eq[a:b] += (d_close[a:b] - float(t.entry_price)) * q
        eq[b:] += float(t.pnl_net_usd)
    pnl = pd.Series(np.diff(np.r_[0.0, eq]), index=d_idx)
    return pnl.reindex(pd.bdate_range(d_idx.min(), d_idx.max(), tz='UTC').union(d_idx), fill_value=0.0)

def compute_metrics(result: Any, bars: pd.DataFrame | None=None) -> dict[str, Any]:
    trades: list = list(getattr(result, 'trades', []) or [])
    n = len(trades)
    pnls = [float(t.pnl_net_usd) for t in trades]
    gross = [float(getattr(t, 'pnl_gross_usd', 0.0)) for t in trades]
    holds = [float(getattr(t, 'hold_secs', 0.0)) for t in trades]
    sl_dists = [float(getattr(t, 'initial_sl_distance_usd', 0.0) or getattr(t, 'sl_distance_usd', 0.0) or 0.0) for t in trades]
    tp_dists = [float(getattr(t, 'initial_tp_distance_usd', 0.0) or getattr(t, 'tp_distance_usd', 0.0) or 0.0) for t in trades]
    if n and (not any(sl_dists)):
        sl_dists = [abs(float(t.entry_price) - float(t.stop_price)) for t in trades]
    if n and (not any(tp_dists)):
        tp_dists = [abs(float(t.target_price) - float(t.entry_price)) for t in trades]
    wins = [v for v in pnls if v > 0]
    losses = [v for v in pnls if v <= 0]
    sum_wins = float(sum(wins))
    sum_losses = float(abs(sum(losses)))
    eq = np.asarray(getattr(result, 'equity_curve', np.zeros(0)), dtype=float)
    if eq.size > 0 and np.isfinite(eq).any():
        run_max = np.maximum.accumulate(eq)
        dd = run_max - eq
        max_dd = float(np.max(dd))
        curve_source = 'mtm_equity_curve'
    elif n:
        cum = np.cumsum(np.asarray(pnls, dtype=float))
        run_max = np.maximum.accumulate(cum)
        max_dd = float(np.max(run_max - cum))
        curve_source = 'closed_trade_cumsum'
    else:
        max_dd = 0.0
        curve_source = 'empty'
    total_net = float(sum(pnls))
    total_gross = float(sum(gross))
    span_days = 1.0
    if trades:
        t0 = int(trades[0].entry_time_ns)
        t1 = int(trades[-1].exit_time_ns)
        if t1 > t0:
            span_days = (t1 - t0) / 1000000000.0 / 86400.0
    years = max(span_days / 365.25, 1e-09)
    cagr = (1.0 + total_net / 1000.0) ** (1.0 / years) - 1.0 if years > 0 and total_net > -1000.0 else None
    profit_factor = sum_wins / sum_losses if sum_losses > 0 else None
    sharpe_mtm = sharpe_daily = None
    daily_mtm: dict[str, float] = {}
    daily_pnl: dict[str, float] = {}
    _ann = lambda s: float(s.mean()) / float(s.std(ddof=1)) * math.sqrt(252.0) if len(s) > 2 and float(s.std(ddof=1)) > 0 else None
    if n > 2 and bars is not None:
        _m = daily_mtm_pnl(trades, bars)
        assert abs(float(_m.sum()) - total_net) < 1e-06 * max(1.0, abs(total_net)), (float(_m.sum()), total_net)
        _r = pd.Series(pnls, index=pd.to_datetime([int(t.exit_time_ns) for t in trades], utc=True).normalize()).groupby(level=0).sum().reindex(_m.index, fill_value=0.0)
        daily_mtm = {k.strftime('%Y-%m-%d'): round(float(v), 4) for k, v in _m.items()}
        daily_pnl = {k.strftime('%Y-%m-%d'): round(float(v), 4) for k, v in _r.items()}
        sharpe_mtm, sharpe_daily = (_ann(_m), _ann(_r))
    exit_breakdown: dict[str, int] = {}
    for t in trades:
        r = str(getattr(t, 'exit_reason', '?'))
        exit_breakdown[r] = exit_breakdown.get(r, 0) + 1
    source_breakdown: dict[str, int] = {}
    for t in trades:
        r = str(getattr(t, 'source', '?'))
        source_breakdown[r] = source_breakdown.get(r, 0) + 1
    realised_rr = sum_wins / sum_losses if sum_losses > 0 else None
    med_win = float(np.median(wins)) if wins else None
    med_loss = float(np.median(losses)) if losses else None
    payoff = abs(med_win / med_loss) if med_win is not None and med_loss else None
    return {'n_trades': n, 'n_long': int(sum((1 for t in trades if int(t.direction) > 0))), 'n_short': int(sum((1 for t in trades if int(t.direction) < 0))), 'n_wins': len(wins), 'n_losses': len(losses), 'hit_rate': len(wins) / n if n else 0.0, 'total_pnl_gross_usd': total_gross, 'total_pnl_net_usd': total_net, 'ev_per_trade_usd': total_net / n if n else 0.0, 'profit_factor': profit_factor, 'payoff_ratio': payoff, 'max_drawdown_usd': max_dd, 'max_dd_source': curve_source, 'cagr': cagr, 'sharpe_mtm': sharpe_mtm, 'sharpe_daily': sharpe_daily, 'daily_mtm_pnl': daily_mtm, 'daily_pnl': daily_pnl, 'trades_per_day': n / span_days if span_days > 0 else 0.0, 'span_days': span_days, 'median_hold_secs': float(np.median(holds)) if holds else None, 'mean_hold_secs': float(np.mean(holds)) if holds else None, 'median_sl_distance_usd': float(np.median(sl_dists)) if sl_dists else None, 'median_tp_distance_usd': float(np.median(tp_dists)) if tp_dists else None, 'exit_reason_breakdown': exit_breakdown, 'source_breakdown': source_breakdown, 'n_signals_emitted': int(getattr(result, 'n_signals_emitted', 0)), 'n_inversions_detected': int(getattr(result, 'n_inversions_detected', 0)), 'n_immediate_filled': int(getattr(result, 'n_immediate_filled', 0)), 'n_sniper_filled': int(getattr(result, 'n_sniper_filled', 0)), 'n_dropped_opposite': int(getattr(result, 'n_dropped_opposite', 0)), 'n_replaced_opposite': int(getattr(result, 'n_replaced_opposite', 0)), 'n_stacked_opposite': int(getattr(result, 'n_stacked_opposite', 0)), 'n_same_dir_blocked': int(getattr(result, 'n_same_dir_blocked', 0)), 'conflict_policy_used': getattr(result, 'conflict_policy_used', '?'), 'max_same_dir_open_used': int(getattr(result, 'max_same_dir_open_used', 0)), 'n_orders_placed': int(getattr(result, 'n_orders_placed', 0)), 'n_orders_filled': int(getattr(result, 'n_orders_filled', 0)), 'n_orders_cancelled': int(getattr(result, 'n_orders_cancelled', 0)), 'n_orders_cancelled_zone_death': int(getattr(result, 'n_orders_cancelled_zone_death', 0)), 'order_fill_rate': float(getattr(result, 'n_orders_filled', 0)) / float(getattr(result, 'n_orders_placed', 0)) if getattr(result, 'n_orders_placed', 0) else None, 'entry_fill_model_used': getattr(result, 'entry_fill_model_used', '?'), 'fill_model_used': getattr(result, 'fill_model_used', '?'), 'n_contra_close_events': int(getattr(result, 'n_contra_close_events', 0)), 'n_contra_legs_closed': int(getattr(result, 'n_contra_legs_closed', 0)), 'n_contra_qty_netted': float(getattr(result, 'n_contra_qty_netted', 0.0)), 'contra_gross_usd': float(getattr(result, 'contra_gross_usd', 0.0)), 'contra_net_usd': float(getattr(result, 'contra_gross_usd', 0.0)), 'contra_distance_usd_max': float(getattr(result, 'contra_distance_usd_max', 0.0)), 'contra_legs_in_trades': int(exit_breakdown.get('contra_close', 0))}

def _clean(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    return obj

def load_bars(path: Path, start: str | None=None, end: str | None=None) -> pd.DataFrame:
    df = pd.read_parquet(path)
    if 'time' not in df.columns:
        for alt in ('datetime', 'timestamp'):
            if alt in df.columns:
                df = df.rename(columns={alt: 'time'})
                break
    df['time'] = pd.to_datetime(df['time'], utc=True)
    if start is not None:
        df = df[df['time'] >= pd.Timestamp(start, tz='UTC')]
    if end is not None:
        df = df[df['time'] < pd.Timestamp(end, tz='UTC')]
    df = df.sort_values('time').reset_index(drop=True)
    if 'volume' not in df.columns:
        if 'tickv' in df.columns:
            df = df.rename(columns={'tickv': 'volume'})
        else:
            df['volume'] = (df['high'] - df['low']).abs() * 1000.0
    elif 'tickv' in df.columns:
        df = df.drop(columns=['tickv'])
    return df
_ARTIFACT_SOURCES = ('bt/_engine/ict_signals.py', 'bt/_engine/volume_profile.py', 'bt/_engine/swing_cap.py', 'bt/_engine/rolling_p95.py', 'bt/_engine/market_structure.py', 'bt/_engine/timebase.py', 'bt/_engine/vol_state.py', 'bt/optimal_config.py')

def _source_hash() -> str:
    root = Path(__file__).resolve().parents[2]
    return registry.normalised_source_fingerprint(_ARTIFACT_SOURCES)

def _fingerprint(path: Path, start: str | None, end: str | None, shape: dict[str, Any]) -> str:
    st = path.stat()
    payload = json.dumps({'path': str(path.resolve()), 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'start': start, 'end': end, 'shape': shape, 'src': _source_hash()}, sort_keys=True)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]

def _cache_dir(scope: str) -> Path:
    d = CACHE_ROOT / scope
    d.mkdir(parents=True, exist_ok=True)
    return d

class ArtifactCache:

    def __init__(self, data_path: Path, scope: str, start: str | None, end: str | None, vp_overrides: dict[str, Any] | None=None, detector_overrides: dict[str, Any] | None=None, structure_overrides: dict[str, Any] | None=None):
        self.data_path = Path(data_path)
        self.scope = scope
        self.start, self.end = (start, end)
        self.structure_overrides = dict(structure_overrides or {})
        bad_s = set(self.structure_overrides) - _STRUCTURE_OA_FROM_OPT
        if bad_s:
            raise KeyError(f'illegal structure override keys {sorted(bad_s)}; must be in {sorted(_STRUCTURE_OA_FROM_OPT)}')
        self.structure_kwargs = build_structure_kwargs(**self.structure_overrides)
        vp_overrides = dict(vp_overrides or {})
        from bt.optimal_config import _VP_OVERRIDES_ALLOWED
        bad = set(vp_overrides) - _VP_OVERRIDES_ALLOWED
        if bad:
            raise KeyError(f'illegal VP override keys {sorted(bad)}; must be in {sorted(_VP_OVERRIDES_ALLOWED)}')
        self.vp_overrides = vp_overrides
        self.vp_kwargs = build_vp_kwargs(**vp_overrides)
        detector_overrides = dict(detector_overrides or {})
        bad = set(detector_overrides) - _DETECTOR_OA_FROM_OPT
        if bad:
            raise KeyError(f'illegal detector override keys {sorted(bad)}; must be in {sorted(_DETECTOR_OA_FROM_OPT)}')
        self.detector_overrides = detector_overrides
        self.detector_kwargs = detector_kwargs_from_recipe(**detector_overrides)
        shape = {'detector': self.detector_kwargs, 'vp': self.vp_kwargs, 'swing': SWING_KWARGS, 'p95': P95_KWARGS, 'structure': self.structure_kwargs}
        suffix_bits: list[str] = []
        if vp_overrides:
            suffix_bits.append('__vp_' + '_'.join((f'{k}={v}' for k, v in sorted(vp_overrides.items()))))
        if detector_overrides:
            suffix_bits.append('__det_' + '_'.join((f'{k}={v}' for k, v in sorted(detector_overrides.items()))))
        if self.structure_overrides:
            suffix_bits.append('__st_' + '_'.join((f'{k}={v}' for k, v in sorted(self.structure_overrides.items()))))
        self.scope = scope + ''.join(suffix_bits)
        self.fp = _fingerprint(self.data_path, start, end, shape)
        self.dir = _cache_dir(self.scope)
        self.hits: list[str] = []

    @property
    def path(self) -> Path:
        return self.dir / f'artifacts_{self.fp}.pkl'

    def load_bars(self) -> pd.DataFrame:
        return load_bars(self.data_path, self.start, self.end)

    def build_or_load(self, bars: pd.DataFrame) -> dict[str, Any]:
        if self.path.exists():
            t0 = time.perf_counter()
            with open(self.path, 'rb') as fh:
                art = pickle.load(fh)
            art['_cache_hit'] = True
            art['_load_secs'] = time.perf_counter() - t0
            self.hits.append('artifacts')
            return art
        t0 = time.perf_counter()
        from bt._engine.ict_signals import compute_dynamic_wick_floor, detect_fvg
        from bt._engine.rolling_p95 import build_rolling_p95_from_bars
        from bt._engine.swing_cap import build_daily_swing
        from bt._engine.volume_profile import build_volume_profiles
        det_kwargs = dict(self.detector_kwargs)
        use_floor_array = det_kwargs.pop('_strict_wick_use_floor_array', False)
        wick_pct = float(det_kwargs.pop('_strict_wick_price_pct', 0.0))
        from bt._engine.timebase import to_ns_arr
        if use_floor_array and wick_pct > 0.0:
            from bt.optimal_config import STRICT_WICK_RECOMPUTE_SECS
            det_kwargs['strict_wick_floor_usd_per_bar'] = compute_dynamic_wick_floor(bars['close'].to_numpy(dtype=np.float64), price_pct=wick_pct, recompute_secs=int(STRICT_WICK_RECOMPUTE_SECS), times_utc_ns=to_ns_arr(bars))
        zones = detect_fvg(open_=bars['open'].to_numpy(), high=bars['high'].to_numpy(), low=bars['low'].to_numpy(), close=bars['close'].to_numpy(), times_utc_ns=to_ns_arr(bars), **det_kwargs)
        t_zones = time.perf_counter() - t0
        t1 = time.perf_counter()
        vp_set = build_volume_profiles(bars_1s=bars, file_stem=self.scope, verbose=False, **self.vp_kwargs)
        t_vp = time.perf_counter() - t1
        t2 = time.perf_counter()
        swing_cap = build_daily_swing(bars, **SWING_KWARGS)
        t_swing = time.perf_counter() - t2
        t3 = time.perf_counter()
        p95_cap = build_rolling_p95_from_bars(bars, **P95_KWARGS)
        p95_cap.skip_trades_during_warmup = True
        t_p95 = time.perf_counter() - t3
        t4 = time.perf_counter()
        from bt._engine.market_structure import detect_market_structure
        from bt._engine.timebase import to_ns_arr
        structure = detect_market_structure(high=bars['high'].to_numpy(dtype=np.float64), low=bars['low'].to_numpy(dtype=np.float64), close=bars['close'].to_numpy(dtype=np.float64), pivot_len=self.structure_kwargs['pivot_len'], ts_ns=to_ns_arr(bars), resample_to_n_secs=self.structure_kwargs['resample_to_n_secs'], detect_order_blocks=False, detect_liquidity=False)
        t_struct = time.perf_counter() - t4
        t5 = time.perf_counter()
        from bt._engine.vol_state import build_vol_state
        from bt._engine.timebase import to_ns_arr
        vol_state = build_vol_state(bars['high'].to_numpy(dtype=np.float64), bars['low'].to_numpy(dtype=np.float64), bars['close'].to_numpy(dtype=np.float64), bars['open'].to_numpy(dtype=np.float64), measures=('vel_5m', 'vel_15m', 'range_norm_1m', 'wick_ratio', 'rv_ratio_30s', 'shock', 'vol_expand_prev_day'), times_ns=to_ns_arr(bars))
        t_vstate = time.perf_counter() - t5
        art = {'zones': zones, 'vp_set': vp_set, 'swing_cap': swing_cap, 'p95_cap': p95_cap, 'structure': structure, 'vol_state': vol_state, 'n_zones': len(zones), 'build_secs': {'zones': t_zones, 'vp_set': t_vp, 'swing_cap': t_swing, 'p95_cap': t_p95, 'structure': t_struct, 'vol_state': t_vstate}, 'vp_overrides': dict(self.vp_overrides), 'vp_kwargs': dict(self.vp_kwargs), '_cache_hit': False, '_load_secs': 0.0}
        tmp = self.path.with_suffix('.tmp')
        with open(tmp, 'wb') as fh:
            pickle.dump(art, fh, protocol=pickle.HIGHEST_PROTOCOL)
        tmp.replace(self.path)
        self.hits.append('artifacts')
        return art

def run_cell(*, knob: str, value: Any, idx: int, bars, artifacts: dict, notebook: str, scope: str, cache_hit: bool, extra_engine_kwargs: dict | None=None, vp_overrides: dict | None=None, detector_overrides: dict | None=None, structure_overrides: dict | None=None) -> dict[str, Any]:
    engine_over: dict[str, Any] = {}
    vp_over: dict[str, Any] = {}
    det_over: dict[str, Any] = {}
    if knob and knob not in ('_canonical', '_multi'):
        from bt.optimal_config import _ENGINE_OVERRIDES_ALLOWED, _VP_OVERRIDES_ALLOWED, _DETECTOR_OVERRIDES_ALLOWED
        if knob in _ENGINE_OVERRIDES_ALLOWED:
            engine_over[knob] = value
        elif knob in _VP_OVERRIDES_ALLOWED:
            vp_over[knob] = value
        elif knob in _DETECTOR_OVERRIDES_ALLOWED:
            det_over[knob] = value
        else:
            raise KeyError(f'illegal sweep knob {knob!r}; must be in _ENGINE_OVERRIDES_ALLOWED, _VP_OVERRIDES_ALLOWED, or _DETECTOR_OVERRIDES_ALLOWED')
    if extra_engine_kwargs:
        engine_over.update(extra_engine_kwargs)
    if vp_overrides:
        vp_over.update(vp_overrides)
    if detector_overrides:
        det_over.update(detector_overrides)
    st_over: dict[str, Any] = dict(structure_overrides or {})
    ek = build_engine_kwargs(swing_cap=artifacts['swing_cap'], p95_cap=artifacts['p95_cap'], structure=artifacts.get('structure'), vol_state=artifacts.get('vol_state'), **engine_over)
    record_curve = len(bars) <= 8000000
    t0 = time.perf_counter()
    result, diag = run_vp_sniper_backtest_forex(bars=bars, zones=artifacts['zones'], vp_set=artifacts['vp_set'], lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek, record_equity_curve=record_curve)
    wall = time.perf_counter() - t0
    metrics = compute_metrics(result, bars)
    record = {'ts_utc': datetime.now(timezone.utc).strftime(_TS_FMT), 'notebook': notebook, 'knob': knob, 'value': _clean(value), 'cell_idx': idx, 'scope': [scope], 'engine': 'vp_multi_target', 'wall_secs': wall, 'cache_hit': cache_hit, 'n_bars': int(len(bars)), 'metrics': _clean(metrics), 'engine_diag': _clean({'signals_emitted': diag.get('signals_emitted'), 'signals_accounted': diag.get('signals_accounted'), 'signals_unaccounted': diag.get('signals_unaccounted'), 'gate_breakdown': diag.get('gate_breakdown'), 'order_lifecycle': diag.get('order_lifecycle'), 'entry_fill_model': diag.get('entry_fill_model'), 'fill_model': diag.get('fill_model'), 'structure_gate': diag.get('structure_gate')}), 'overrides_vs_canonical': _clean({**engine_over, **vp_over, **det_over, **st_over})}
    out_dir = RESULTS_ROOT / knob
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f'{scope}__cell_{idx}.json'
    with open(out_path, 'w', encoding='utf-8') as fh:
        json.dump(record, fh, indent=2)
    record['_json_path'] = str(out_path)
    return record

def _parse_grid_values(vals: str) -> list[Any]:
    parsed: list[Any] = []
    for raw in vals.split(','):
        raw = raw.strip()
        if not raw:
            continue
        try:
            parsed.append(int(raw))
        except ValueError:
            try:
                parsed.append(float(raw))
            except ValueError:
                if raw.lower() in ('true', 'false'):
                    parsed.append(raw.lower() == 'true')
                else:
                    parsed.append(raw)
    return parsed

def _parse_grid_specs(knobs: str) -> list[tuple[str, list[Any]]]:
    grid_specs: list[tuple[str, list[Any]]] = []
    for part in knobs.split(';'):
        part = part.strip()
        if not part:
            continue
        name, _, vals = part.partition('=')
        name = name.strip()
        if not name or not vals.strip():
            raise SystemExit(f'malformed --knobs segment: {part!r}')
        parsed = _parse_grid_values(vals)
        if not parsed:
            raise SystemExit(f'no values for knob {name!r}')
        grid_specs.append((name, parsed))
    return grid_specs

def main() -> None:
    ap = argparse.ArgumentParser(description='Cached single-knob sweep on XAUUSD 1s.')
    ap.add_argument('--data', type=Path, default=DEFAULT_DATA)
    ap.add_argument('--knob', type=str, default='_canonical')
    ap.add_argument('--grid', type=str, default='', help='Comma-separated values. Empty = [_canonical].')
    ap.add_argument('--knobs', type=str, default='', help="Multi-knob grid. Format: 'name=v1,v2;name2=w1,w2'. Cells are the CARTESIAN PRODUCT of every knob's value list. Use this for the re-sweep matrix (e.g. --knobs 'sl_zone_mult=4,8,12,20,40;max_same_dir_open=10,20;fill_model=pessimistic').")
    ap.add_argument('--start', type=str, default=None, help='Window start (YYYY-MM-DD). DEFAULT = one month (2025-01-01). A longer window prints its estimated cost and EXITS unless --i-know.')
    ap.add_argument('--end', type=str, default=None, help='Window end (YYYY-MM-DD, exclusive). DEFAULT = 2025-02-01.')
    ap.add_argument('--scope', type=str, default='', help='Scope label; defaults to a label derived from dates.')
    ap.add_argument('--notebook', type=str, default='')
    ap.add_argument('--no-cache', action='store_true', help='Ignore any on-disk artifact cache.')
    ap.add_argument('--i-know', action='store_true', help="APPROVAL GATE. A window longer than one month prints its estimated cost and EXITS without running unless this flag is passed. The flag means 'the human has been asked'.")
    args = ap.parse_args()
    if not args.data.exists():
        raise SystemExit(f'data file not found: {args.data}')
    from runtime.sweeps.cost_estimate import DEFAULT_END, DEFAULT_START
    if args.start is None:
        args.start = DEFAULT_START
    if args.end is None:
        args.end = DEFAULT_END
    if args.scope:
        scope = args.scope
    else:
        s = (args.start or 'start').replace('-', '')
        e = (args.end or 'end').replace('-', '')
        scope = f'{args.data.stem}__{s}_{e}'
    notebook = args.notebook or ('canonical_run' if args.knob == '_canonical' else args.knob)
    grid_specs: list[tuple[str, list[Any]]] = []
    if args.knobs.strip():
        grid_specs = _parse_grid_specs(args.knobs)
        from bt.optimal_config import _ENGINE_OVERRIDES_ALLOWED, _VP_OVERRIDES_ALLOWED, _DETECTOR_OVERRIDES_ALLOWED, _STRUCTURE_OVERRIDES_ALLOWED, _SWING_OVERRIDES_ALLOWED
        allowed = set(_ENGINE_OVERRIDES_ALLOWED) | set(_VP_OVERRIDES_ALLOWED) | set(_DETECTOR_OVERRIDES_ALLOWED) | set(_STRUCTURE_OVERRIDES_ALLOWED) | set(_SWING_OVERRIDES_ALLOWED)
        for name, _ in grid_specs:
            if name not in allowed:
                raise SystemExit(f'illegal sweep knob {name!r}; must be in _ENGINE_OVERRIDES_ALLOWED, _VP_OVERRIDES_ALLOWED, _DETECTOR_OVERRIDES_ALLOWED, _STRUCTURE_OVERRIDES_ALLOWED, or _SWING_OVERRIDES_ALLOWED')
    else:
        grid_specs = [(args.knob, [float(x) if '.' in x else int(x) for x in args.grid.split(',')] if args.grid.strip() else [None])]
    from bt.optimal_config import _ENGINE_OVERRIDES_ALLOWED as _EOA, _VP_OVERRIDES_ALLOWED as _VOA, _DETECTOR_OVERRIDES_ALLOWED as _DOA, _STRUCTURE_OVERRIDES_ALLOWED as _SOA
    from bt.optimal_config import _SWING_OVERRIDES_ALLOWED as _SWA
    _STRUCTURE_SWEEP_KEYS = set(_SOA)
    _SWING_SWEEP_KEYS = set(_SWA)

    def _partition(overrides: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        e, v, d = ({}, {}, {})
        for k, val in overrides.items():
            if k in _STRUCTURE_SWEEP_KEYS or k in _SWING_SWEEP_KEYS:
                continue
            if k in _EOA:
                e[k] = val
            elif k in _VOA:
                v[k] = val
            elif k in _DOA:
                d[k] = val
            else:
                raise KeyError(f'{k!r} is not on any sweep allowlist')
        return (e, v, d)
    import itertools
    cells: list[tuple[dict, dict, dict, dict, dict]] = []
    for combo in itertools.product(*[vals for _, vals in grid_specs]):
        overrides = {name: val for (name, _), val in zip(grid_specs, combo)}
        overrides.pop('_canonical', None)
        st_cell = {_k: overrides.pop(_k) for _k in _STRUCTURE_SWEEP_KEYS if _k in overrides}
        sw_cell = {_k: overrides.pop(_k) for _k in _SWING_SWEEP_KEYS if _k in overrides}
        cells.append((*_partition(overrides), st_cell, sw_cell))
    print(f'== cached sweep ==')
    print(f'data    : {args.data}')
    print(f'scope   : {scope}')
    print(f'knob    : {args.knob}')
    print(f'grid    : {grid_specs}')
    print(f'cells   : {len(cells)}')
    from runtime.sweeps.cost_estimate import estimate_sweep
    est = estimate_sweep(args.data, args.start, args.end, n_cells=len(cells), artifacts_cached=False)
    print()
    print(est.render())
    print()
    if est.needs_approval and (not args.i_know):
        print('ABORTED: window is longer than one month. Re-run with --i-know to execute.')
        raise SystemExit(2)
    t0 = time.perf_counter()
    print('loading bars ...', flush=True)
    bars = load_bars(args.data, args.start, args.end)
    print(f"  {len(bars):,} bars in {time.perf_counter() - t0:.1f}s ({bars['time'].min()} .. {bars['time'].max()})")
    if len(bars) == 0:
        raise SystemExit('no bars in range')
    if args.no_cache:
        import glob
        for stale in glob.glob(str(CACHE_ROOT / f'{scope}*')):
            try:
                Path(stale).unlink()
            except OSError:
                pass
        print(f'  --no-cache: cleared any cached pickles under {scope}*')
    cell_caches: dict[tuple, Any] = {}

    def _ensure_cache(vp_over: dict[str, Any], detector_over: dict[str, Any], structure_over: dict[str, Any] | None=None) -> Any:
        key = (tuple(sorted(vp_over.items())), tuple(sorted(detector_over.items())), tuple(sorted((structure_over or {}).items())))
        if key not in cell_caches:
            cell_caches[key] = ArtifactCache(args.data, scope, args.start, args.end, vp_overrides=vp_over, detector_overrides=detector_over, structure_overrides=structure_over)
        return cell_caches[key]

    def _build_artifacts(vp_over: dict[str, Any], detector_over: dict[str, Any], structure_over: dict[str, Any] | None=None) -> dict[str, Any]:
        cache = _ensure_cache(vp_over, detector_over, structure_over)
        art = cache.build_or_load(bars)
        tag = 'HIT ' if art['_cache_hit'] else 'MISS'
        print(f"  artifacts {tag} vp_over={vp_over or '{}'} det_over={detector_over or '{}'} st_over={structure_over or '{}'} -> {art['n_zones']:,} zones", flush=True)
        if not art['_cache_hit']:
            for k, v in art['build_secs'].items():
                print(f'    {k:10s} {v:7.1f}s')
        return art
    records: list[dict[str, Any]] = []
    for idx, (engine_over, vp_over, det_over, st_over, sw_over) in enumerate(cells):
        merged = {**engine_over, **vp_over, **det_over, **st_over, **sw_over}
        over_desc = ', '.join((f'{k}={v}' for k, v in merged.items()))
        print(f'\n-- cell {idx}: {over_desc} --', flush=True)
        art = _build_artifacts(vp_over, det_over, st_over)
        if sw_over:
            from bt._engine.swing_cap import build_daily_swing
            art = {**art, 'swing_cap': build_daily_swing(bars, **build_swing_kwargs(**sw_over))}
        if len(merged) == 1 and (not sw_over):
            (only_k, only_v), = merged.items()
            rec = run_cell(knob=only_k, value=only_v, idx=idx, bars=bars, artifacts=art, notebook=notebook, scope=scope, cache_hit=art['_cache_hit'])
        else:
            rec = run_cell(knob='_multi', value=merged, idx=idx, bars=bars, artifacts=art, notebook=notebook, scope=scope, cache_hit=art['_cache_hit'], extra_engine_kwargs=engine_over, vp_overrides=vp_over, detector_overrides=det_over, structure_overrides=st_over)
        records.append(rec)
        m = rec['metrics']
        registry.record_run(kind='sweep_cell', params={'knob': rec['knob'], 'value': rec['value'], 'overrides': rec['overrides_vs_canonical'], 'scope': scope, 'start': args.start, 'end': args.end, 'data': str(args.data), 'notebook': notebook}, summary={'n_trades': m.get('n_trades'), 'hit_rate': m.get('hit_rate'), 'gross': m.get('total_pnl_gross_usd'), 'net': m.get('total_pnl_net_usd'), 'gross_per_leg': None if not m.get('n_trades') else round(float(m.get('total_pnl_gross_usd') or 0.0) / float(m['n_trades']), 6), 'profit_factor': m.get('profit_factor'), 'sharpe_mtm': m.get('sharpe_mtm'), 'sharpe_daily': m.get('sharpe_daily'), 'trades_per_day': m.get('trades_per_day'), 'max_drawdown_usd': m.get('max_drawdown_usd'), 'wall_secs': round(float(rec.get('wall_secs') or 0.0), 2)}, payload=rec, src_fp=registry.source_fingerprint(_ARTIFACT_SOURCES))
        print(f"   trades={m['n_trades']:>6}  signals={m['n_signals_emitted']:>8}  net=${m['total_pnl_net_usd']:>10.2f}  EV=${m['ev_per_trade_usd']:>8.3f}  sharpe_mtm={(m['sharpe_mtm'] or 0.0):.2f}  sharpe_daily={(m['sharpe_daily'] or 0.0):.2f}  hit={m['hit_rate']:.3f}  maxDD=${m['max_drawdown_usd']:>9.2f}  PF={(m['profit_factor'] if m['profit_factor'] is None else round(m['profit_factor'], 2))}  {rec['wall_secs']:.1f}s")
        print(f"   exits: {m['exit_reason_breakdown']}")
        print(f"   -> {rec['_json_path']}")
    print(f"\n== done: {len(records)} cell(s) in {sum((r['wall_secs'] for r in records)):.1f}s of engine time ==")
if __name__ == '__main__':
    main()
