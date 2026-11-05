"""Refit STRUCTURE_SEQ_WHITELIST: run canonical with the whitelist off, group trade net P&L by
'side|last-5-breaks' at the signal bar, keep keys with net > 0 and at least --min-trades trades.

python -m runtime.sweeps.fit_whitelist --start 2024-01-01 --end 2026-07-17
Fitted in-sample on the window given; the result is optimistic on that same window.
"""
from __future__ import annotations


def main() -> None:
    import argparse
    from collections import defaultdict

    from bt.optimal_config import FOREX_CONTRACT_SIZE, FOREX_LOTS, build_engine_kwargs
    from bt.vp_engine import run_vp_sniper_backtest_forex
    from bt._engine.vp_multi_target_engine import _sequence_signature
    from runtime.sweeps.cached_sweep import DEFAULT_DATA, ArtifactCache, load_bars

    ap = argparse.ArgumentParser()
    ap.add_argument('--start', default='2024-01-01')
    ap.add_argument('--end', default='2026-07-17')
    ap.add_argument('--min-trades', type=int, default=20)
    a = ap.parse_args()
    scope = f"{DEFAULT_DATA.stem}__{a.start.replace('-', '')}_{a.end.replace('-', '')}"
    bars = load_bars(DEFAULT_DATA, a.start, a.end)
    art = ArtifactCache(DEFAULT_DATA, scope, a.start, a.end).build_or_load(bars)
    ek = build_engine_kwargs(swing_cap=art['swing_cap'], p95_cap=art['p95_cap'], structure=art['structure'],
                             vol_state=art['vol_state'], structure_seq_whitelist=frozenset())  # empty = gate off
    res, _ = run_vp_sniper_backtest_forex(bars=bars, zones=art['zones'], vp_set=art['vp_set'], lots=FOREX_LOTS,
                                          contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek, record_equity_curve=False)
    seq_len = int(ek.get('structure_seq_len') or ek.get('structure_score_lookback') or 5)  # engine default lookback
    net, ids = defaultdict(float), defaultdict(set)
    for t in res.trades:  # contra-close legs share the parent's signal bar
        key = f"{'long' if t.direction > 0 else 'short'}|{_sequence_signature(art['structure'], int(t.signal_bar), seq_len)}"
        net[key] += float(t.pnl_net_usd)
        ids[key].add(t.trade_id)
    keep = sorted(k for k in net if not k.endswith('|') and net[k] > 0 and len(ids[k]) >= a.min_trades)
    print(f'gate-off: legs={len(res.trades)} net=${sum(net.values()):,.2f} keys={len(net)}')
    for k in sorted(net, key=net.get, reverse=True)[:40]:
        print(f"  {'*' if k in keep else ' '} {k:16s} n={len(ids[k]):5d} net=${net[k]:9.2f}")
    print(f'whitelist ({len(keep)} keys):')
    print('frozenset({' + ', '.join(repr(k) for k in keep) + '})')


if __name__ == '__main__':
    main()
