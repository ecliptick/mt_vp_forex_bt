"""Truncation check: every input value at row r must not change when bars after r are removed."""
import numpy as np
import pandas as pd

from bt._engine.market_structure import detect_market_structure
from bt._engine.rolling_p95 import build_rolling_p95_from_bars
from bt._engine.swing_cap import build_daily_swing
from bt._engine.timebase import to_ns_arr
from bt._engine.volume_profile import build_volume_profiles, hvn_zones_near


def _bars(n=3 * 86400, seed=0):
    rng = np.random.default_rng(seed)
    t = np.sort(rng.choice(np.arange(n * 1.1, dtype=np.int64), n, replace=False))  # ~9% gaps
    c = 2000 + np.cumsum(rng.normal(0, 0.05, n))
    return pd.DataFrame({'time': pd.to_datetime(t * 1_000_000_000 + 1_704_067_200_000_000_000, utc=True),
                         'open': c, 'high': c + 0.03, 'low': c - 0.03, 'close': c, 'volume': rng.integers(1, 9, n).astype(float)})


def test_no_lookahead():
    full = _bars()
    cut = len(full) * 2 // 3
    part = full.iloc[:cut].reset_index(drop=True)
    tn = to_ns_arr(full)
    probe = np.linspace(100, cut - 1, 400).astype(int)

    def ms(b):
        return detect_market_structure(b['high'].to_numpy(), b['low'].to_numpy(), b['close'].to_numpy(), pivot_len=9,
                                       ts_ns=to_ns_arr(b), resample_to_n_secs=60, detect_order_blocks=False, detect_liquidity=False)
    a, b = ms(full), ms(part)
    for f in ('trend', 'last_break_bar', 'last_break_kind', 'last_choch_bar', 'last_choch_kind'):
        assert np.array_equal(getattr(a, f)[:cut - 60], getattr(b, f)[:cut - 60]), f  # last minute of `part` is partial
    for mk in (lambda x: build_daily_swing(x, lookback_secs=86400, cap_pct=3.0),
               lambda x: build_rolling_p95_from_bars(x, lookback_secs=86400, inner_secs=3600, p95_mult=1.0)):
        ca, cb = mk(full), mk(part)
        assert all(ca.cap_for(int(tn[i])) == cb.cap_for(int(tn[i])) for i in probe)
    va = build_volume_profiles(full, file_stem='x', verbose=False)
    vb = build_volume_profiles(part, file_stem='x', verbose=False)
    for i in probe:
        p = float(full['close'].iat[i])
        assert hvn_zones_near(va, int(tn[i]), p, 1) == hvn_zones_near(vb, int(tn[i]), p, 1)


if __name__ == '__main__':
    test_no_lookahead()
    print('ok')
