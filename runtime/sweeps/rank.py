from __future__ import annotations
import argparse
import json
import math
import numpy as np
from scipy.stats import kurtosis, norm, skew
from runtime.results import registry

EULER = 0.5772156649015329


def deflated_sharpe(sr: float, t: int, g3: float, g4: float, sr0: float) -> float:
    den = math.sqrt(max(1e-12, 1.0 - g3 * sr + (g4 - 1.0) / 4.0 * sr * sr))
    return float(norm.cdf((sr - sr0) * math.sqrt(t - 1) / den))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-01-01')
    ap.add_argument('--end', default='2026-07-16')
    ap.add_argument('--since', default='', help='only runs recorded at or after this UTC timestamp (YYYY-MM-DDTHH:MM:SSZ)')
    ap.add_argument('--sort', default='mtm', choices=('mtm', 'daily', 'dsr'))
    args = ap.parse_args()
    rows = []
    for e in registry.load_index():
        if e.kind != 'sweep_cell' or e.params.get('start') != args.start or e.params.get('end') != args.end:
            continue
        if args.since and e.ts_utc < args.since:
            continue
        m = json.loads((registry.RUNS_DIR / f'{e.run_id}.json').read_text(encoding='utf-8'))['payload']['metrics']
        d = np.asarray(list((m.get('daily_mtm_pnl') or {}).values()), dtype=float)
        if len(d) < 30 or d.std(ddof=1) == 0:
            continue
        v = e.params.get('value')
        label = json.dumps(v) if isinstance(v, dict) or e.params.get('knob') in (None, '_canonical', '_multi') else f"{e.params.get('knob')}={v}"
        rows.append(dict(cell=label, n=m['n_trades'], ev=m['ev_per_trade_usd'], net=m['total_pnl_net_usd'], dd=m['max_drawdown_usd'], mtm=m.get('sharpe_mtm'), daily=m.get('sharpe_daily'), sr=d.mean() / d.std(ddof=1), t=len(d), g3=float(skew(d)), g4=float(kurtosis(d, fisher=False))))
    if not rows:
        raise SystemExit('no runs with daily_mtm_pnl in range')
    n = len(rows)
    sr0 = math.sqrt(float(np.var([r['sr'] for r in rows], ddof=1))) * ((1 - EULER) * norm.ppf(1 - 1 / n) + EULER * norm.ppf(1 - 1 / (n * math.e))) if n > 1 else 0.0
    for r in rows:
        r['dsr'] = deflated_sharpe(r['sr'], r['t'], r['g3'], r['g4'], sr0)
    rows.sort(key=lambda r: -(r[args.sort] or 0.0))
    print(f'{n} trials | Sharpe annualised x sqrt(252) | DSR on daily MTM, SR0 = {sr0 * math.sqrt(252):.2f} annualised')
    print(f"{'#':>2}  {'MTM':>5}  {'daily':>5}  {'DSR':>5}  {'trades':>6}  {'EV':>6}  {'net':>9}  {'maxDD':>6}  cell")
    for i, r in enumerate(rows, 1):
        print(f"{i:>2}  {r['mtm']:>5.2f}  {r['daily']:>5.2f}  {r['dsr']:>5.3f}  {r['n']:>6}  {r['ev']:>6.3f}  {r['net']:>9.2f}  {r['dd']:>6.0f}  {r['cell']}")


if __name__ == '__main__':
    main()
