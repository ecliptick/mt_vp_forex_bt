from __future__ import annotations
import json
import math
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional, Sequence
_OUT_ROOT = Path('notebooks')
_TS_FMT = '%Y-%m-%dT%H:%M:%SZ'

def _to_jsonable(obj: Any) -> Any:
    if is_dataclass(obj):
        return _to_jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_jsonable(v) for v in obj]
    if isinstance(obj, (set, frozenset)):
        return sorted((_to_jsonable(v) for v in obj))
    if isinstance(obj, Path):
        return str(obj)
    try:
        import numpy as np
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            v = float(obj)
            return v if math.isfinite(v) else None
        if isinstance(obj, np.ndarray):
            return [_to_jsonable(x) for x in obj.tolist()]
    except Exception:
        pass
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj

def _safe_stats(values: Sequence[float]) -> dict:
    finite = [v for v in values if v is not None and math.isfinite(v)]
    n = len(finite)
    if n == 0:
        return {'mean': None, 'median': None, 'max': None, 'min': None}
    finite_sorted = sorted(finite)
    mid = n // 2
    if n % 2:
        median = finite_sorted[mid]
    else:
        median = 0.5 * (finite_sorted[mid - 1] + finite_sorted[mid])
    return {'mean': sum(finite) / n, 'median': median, 'max': finite_sorted[-1], 'min': finite_sorted[0]}

def _compute_metrics(res, params) -> dict:
    trades = list(getattr(res, 'trades', []))
    pnls_net = [float(t.pnl_usd) for t in trades]
    pnls_gross = pnls_net
    wins = [v for v in pnls_net if v > 0]
    losses = [v for v in pnls_net if v <= 0]
    holds = [float(getattr(t, 'hold_secs', 0.0)) for t in trades]
    directions = [int(getattr(t, 'direction', 0)) for t in trades]
    running = 0.0
    peak = 0.0
    max_dd = 0.0
    for v in pnls_net:
        running += v
        if running > peak:
            peak = running
        dd = running - peak
        if dd < max_dd:
            max_dd = dd
    sum_wins = sum(wins) if wins else 0.0
    sum_losses = abs(sum(losses)) if losses else 0.0
    sum_gross = sum(pnls_gross)
    n = len(trades)
    win_stats = _safe_stats(wins)
    loss_stats = _safe_stats(losses)
    hold_stats = _safe_stats(holds)
    exit_breakdown: dict = {}
    for t in trades:
        r = str(getattr(t, 'exit_reason', ''))
        exit_breakdown[r] = exit_breakdown.get(r, 0) + 1
    pnl_first = trades[0].entry_time if trades else 0
    pnl_last = trades[-1].entry_time if trades else 0
    days = max(1, (pnl_last - pnl_first) / 1000000000.0 / 86400.0) if pnl_first and pnl_last else 1
    metrics = {'n_trades': n, 'n_long': sum((1 for d in directions if d > 0)), 'n_short': sum((1 for d in directions if d < 0)), 'n_signals_emitted': int(getattr(res, 'n_signals_emitted', 0)), 'n_signals_consumed': int(getattr(res, 'n_signals_consumed', 0)), 'n_fills': int(getattr(res, 'n_fills', 0)), 'n_soft_stops': int(getattr(res, 'n_soft_stops', 0)), 'win_rate_pct': 100.0 * len(wins) / n if n else 0.0, 'pnl_gross_usd': sum_gross, 'pnl_net_usd': sum(pnls_net), 'ev_per_trade_usd': sum(pnls_net) / n if n else 0.0, 'mean_win_usd': win_stats['mean'], 'mean_loss_usd': loss_stats['mean'], 'median_win_usd': win_stats['median'], 'median_loss_usd': loss_stats['median'], 'largest_win_usd': win_stats['max'], 'largest_loss_usd': loss_stats['min'], 'profit_factor': sum_wins / sum_losses if sum_losses > 0 else None, 'payoff_ratio': abs(win_stats['mean'] / loss_stats['mean']) if win_stats['mean'] is not None and loss_stats['mean'] else None, 'max_drawdown_usd': max_dd, 'max_drawdown_pct': 100.0 * max_dd / peak if peak > 0 else 0.0, 'trades_per_day': n / days if days > 0 else 0.0, 'median_hold_secs': hold_stats['median'], 'mean_hold_secs': hold_stats['mean'], 'exit_reason_breakdown': exit_breakdown, 'taker_bps_charged': float(getattr(params, 'taker_fee_bps', 0.0))}
    return metrics

def append_run_report(*, notebook: str, scenario: str, scope: Iterable[str], engine: str, comments: str, hypothesis: str='', verdict: str='inconclusive', params: Any, result: Any, metrics_extra: Optional[Mapping[str, Any]]=None, overrides_vs_canonical: Optional[Mapping[str, Any]]=None, canonical_recipe_version: Optional[str]=None, out_root: Optional[Path]=None, dedup_keys: Optional[Sequence[str]]=None) -> Path:
    if not comments or not isinstance(comments, str) or (not comments.strip()):
        raise ValueError('`comments` is required (AGENTS.md § Required comments).')
    if not scope:
        raise ValueError('`scope` must list the months/files covered (AGENTS.md § Required metrics).')
    root = Path(out_root) if out_root else _OUT_ROOT
    out_dir = root / f'{notebook}_outputs'
    out_dir.mkdir(parents=True, exist_ok=True)
    jsonl_path = out_dir / f'{notebook}__runs.jsonl'
    if hasattr(params, 'as_dict'):
        params_dict = params.as_dict() if callable(params.as_dict) else params.as_dict
    elif is_dataclass(params):
        params_dict = asdict(params)
    elif isinstance(params, dict):
        params_dict = params
    else:
        params_dict = dict(vars(params)) if hasattr(params, '__dict__') else {}
    metrics = _compute_metrics(result, params)
    if metrics_extra:
        metrics.update({k: _to_jsonable(v) for k, v in metrics_extra.items()})
    record = {'ts_utc': datetime.now(timezone.utc).strftime(_TS_FMT), 'notebook': notebook, 'scenario': scenario, 'scope': list(scope), 'engine': engine, 'comments': comments.strip(), 'hypothesis': hypothesis, 'verdict': verdict, 'canonical_recipe_version': canonical_recipe_version or '', 'params': _to_jsonable(params_dict), 'overrides_vs_canonical': _to_jsonable(dict(overrides_vs_canonical or {})), 'metrics': _to_jsonable(metrics)}
    if dedup_keys and jsonl_path.exists():
        scope0 = str(record['scope'][0])
        new_keys = {k: record['overrides_vs_canonical'].get(k) for k in dedup_keys}
        kept: list[str] = []
        with open(jsonl_path, 'r', encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    prior = json.loads(line)
                except Exception:
                    kept.append(line)
                    continue
                prior_scope0 = str((prior.get('scope') or [''])[0])
                if prior_scope0 != scope0:
                    kept.append(line)
                    continue
                prior_overrides = prior.get('overrides_vs_canonical') or {}
                prior_keys = {k: prior_overrides.get(k) for k in dedup_keys}
                if prior_keys == new_keys:
                    continue
                kept.append(line)
        with open(jsonl_path, 'w', encoding='utf-8') as fh:
            fh.write('\n'.join(kept) + ('\n' if kept else ''))
    with open(jsonl_path, 'a', encoding='utf-8') as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + '\n')
    return jsonl_path
