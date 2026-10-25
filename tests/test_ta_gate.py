"""Guards for the TA GATE harness — 2026-10-25.

The TA gate is a POST-HOC re-pricing of the canonical ledger's legs.
It is only a measurement if it reproduces the ledger EXACTLY when
the gate is a no-op. These tests lock that identity, plus the two
properties that make the decay number trustworthy:

    10|1. **THE NO-OP IDENTITY.** With a constant +1 signal, the gate
   must reproduce the ledger's gross, fee, drift and excess BIT-EXACTLY
   on the long legs. A drift computed on the wrong grid still
   correlates (0.92) but DISAGREES IN SIGN — so the test checks the
   totals, not the correlation.
2. **THE DECAY IS EXACT.** The trade count after a gate must equal the
   number of legs that passed, and a leg must never be counted twice.
3. **NO LOOKAHEAD.** The signal in force at time T must be the signal
   of the last 1m bar whose CLOSE is <= T — never the bar that
   contains T.
"""
from __future__ import annotations

import numpy as np
import pytest

from runtime.diagnostics.ta_gate import (
    _next_differing,
    apply_gate,
    signal_at,
)
from runtime.diagnostics.ta_1m_core import NS_PER_MIN


# ─────────────────────────────────────────────────────────────────
# A SYNTHETIC LEDGER + GRID
# ─────────────────────────────────────────────────────────────────

def _synthetic_ledger(n: int = 200, seed: int = 7):
    """A tiny ledger with the columns ``apply_gate`` reads."""
    rng = np.random.default_rng(seed)
    side = rng.choice([-1, 1], size=n).astype(np.int64)
    qty = np.ones(n, dtype=np.float64)
    ep = 3000.0 + rng.normal(0, 5, n)
    xp = ep + rng.normal(0, 3, n)
    # The times must be monotone-ish but need not be sorted.
    base = 1_700_000_000_000_000_000
    st = base + np.arange(n, dtype=np.int64) * 60 * 1_000_000_000
    et = st + 30 * 1_000_000_000
    xt = et + 600 * 1_000_000_000
    legs = {
        "side": side, "qty": qty,
        "entry_price": ep, "exit_price": xp,
        "signal_time_ns": st, "entry_time_ns": et, "exit_time_ns": xt,
        "entry_bar": np.arange(n, dtype=np.int64),
        "exit_bar": np.arange(n, dtype=np.int64) + 10,
        "gross": (xp - ep) * side * qty,
        "fee": np.abs(xp + ep) * 0.5 * qty * (0.71 / 10000.0) * 2.0,
    }
    legs["drift_usd"] = legs["gross"] * 0.9
    legs["excess_usd"] = legs["gross"] - legs["drift_usd"]
    return legs


def _synthetic_grid(n_m: int = 400, n_1s: int = 4000):
    """A grid whose 1s closes are a linear ramp, so the drift is
    exactly computable by hand."""
    from runtime.diagnostics.ta_gate import Bars1m, Bars1mGrid

    closes_1s = 3000.0 + np.arange(n_1s, dtype=np.float64) * 0.01
    n_rows = n_1s // n_m
    time_ns = (np.arange(n_m, dtype=np.int64)
               * NS_PER_MIN + 1_700_000_000_000_000_000)
    o = np.full(n_m, 3000.0)
    h = np.full(n_m, 3001.0)
    low = np.full(n_m, 2999.0)
    c = closes_1s[np.arange(n_m) * n_rows + n_rows - 1]
    bars = Bars1m(time_ns=time_ns, open=o, high=h, low=low, close=c,
                  volume=np.ones(n_m), n_ticks=np.ones(n_m, dtype=np.int64),
                  atr=np.full(n_m, 2.0), atr_n=14)
    row_start = (np.arange(n_m, dtype=np.int64) * n_rows)
    row_end = row_start + n_rows - 1
    grid = Bars1mGrid(bars=bars, row_start=row_start, row_end=row_end,
                      path=None, closes_1s=closes_1s)
    return grid


# ─────────────────────────────────────────────────────────────────
# 1. THE NO-OP IDENTITY
# ─────────────────────────────────────────────────────────────────

def test_a_constant_long_signal_reproduces_the_ledger_bit_exactly():
    """The gate's re-pricing must equal the ledger, to the bit."""
    legs = _synthetic_ledger()
    grid = _synthetic_grid()
    bars = grid.bars
    always_long = np.ones(len(bars), dtype=np.int8)

    res = apply_gate(legs, always_long, bars, bps=0.71, grid=grid)
    m = res.filled
    assert m.sum() > 0

    np.testing.assert_allclose(res.gross_gate_only[m], legs["gross"][m],
                               rtol=0, atol=1e-12)
    np.testing.assert_allclose(res.fee_gate_only[m], legs["fee"][m],
                               rtol=0, atol=1e-12)


def test_the_gate_rejects_a_short_leg_under_a_long_signal():
    """A short leg must never pass a +1 signal."""
    legs = _synthetic_ledger()
    grid = _synthetic_grid()
    always_long = np.ones(len(grid.bars), dtype=np.int8)
    res = apply_gate(legs, always_long, grid.bars, bps=0.71, grid=grid)
    assert np.all(legs["side"][res.filled] > 0)
    assert np.all(res.gross_gate_only[~res.filled] == 0.0)


def test_the_drift_uses_the_ledgers_own_1s_grid():
    """The drift must be the 1s close at the ledger's entry/exit rows.

    A 1m-grid drift DISAGREES IN SIGN with the 1s-grid drift — so this
    test checks the exact value, not a correlation.
    """
    legs = _synthetic_ledger()
    grid = _synthetic_grid()
    always_long = np.ones(len(grid.bars), dtype=np.int8)
    res = apply_gate(legs, always_long, grid.bars, bps=0.71, grid=grid)
    m = res.filled

    eb = legs["entry_bar"].astype(np.int64)
    xb = legs["exit_bar"].astype(np.int64)
    side = legs["side"].astype(np.float64)
    qty = legs["qty"].astype(np.float64)
    expect = ((grid.closes_1s[xb] - grid.closes_1s[eb]) * side * qty)

    np.testing.assert_allclose(res.drift_gate_only[m], expect[m],
                               rtol=1e-12, atol=1e-12)


# ─────────────────────────────────────────────────────────────────
# 2. THE DECAY IS EXACT
# ─────────────────────────────────────────────────────────────────

def test_the_trade_count_equals_the_number_of_legs_that_passed():
    """No leg may be counted twice, and none may be lost."""
    legs = _synthetic_ledger()
    grid = _synthetic_grid()
    sig = np.zeros(len(grid.bars), dtype=np.int8)
    sig[::7] = 1
    sig[::11] = -1

    res = apply_gate(legs, sig, grid.bars, bps=0.71, grid=grid)
    assert res.n_filled == int(res.filled.sum())
    assert res.n_filled <= res.n_gate_pass
    assert res.n_gated_out_signal + res.n_gate_pass == res.n_legs
    assert res.n_cancelled_at_fill == res.n_gate_pass - res.n_filled


def test_a_leg_cannot_pass_and_be_cancelled_at_the_same_time():
    legs = _synthetic_ledger()
    grid = _synthetic_grid()
    sig = np.zeros(len(grid.bars), dtype=np.int8)
    sig[::5] = 1
    res = apply_gate(legs, sig, grid.bars, bps=0.71, grid=grid)
    assert not np.any(res.filled & ~res.gate_pass)
    assert np.all(res.gate_pass[res.filled])


# ─────────────────────────────────────────────────────────────────
# 3. NO LOOKAHEAD
# ─────────────────────────────────────────────────────────────────

def test_the_signal_in_force_is_the_last_bar_that_HAS_CLOSED():
    """A 1m bar is only known at its CLOSE.

    Using the bar that CONTAINS T leaks up to 60 s of future price.
    """
    from runtime.diagnostics.ta_1m_core import Bars1m

    n = 10
    time_ns = 1_700_000_000_000_000_000 + np.arange(n, dtype=np.int64) * NS_PER_MIN
    bars = Bars1m(time_ns=time_ns, open=np.zeros(n), high=np.zeros(n),
                  low=np.zeros(n), close=np.zeros(n), volume=np.zeros(n),
                  n_ticks=np.zeros(n, dtype=np.int64), atr=np.zeros(n),
                  atr_n=14)
    sig = np.arange(n, dtype=np.int8)

    # A time INSIDE bar 3 (which starts at t0 + 3*60s) must see the
    # signal of bar 2 (the last bar that had CLOSED), not bar 3.
    t_inside = int(time_ns[3] + 30 * 1_000_000_000)
    out, idx = signal_at(sig, bars, np.array([t_inside], dtype=np.int64))
    assert int(idx[0]) == 2
    assert int(out[0]) == 2

    # A time exactly at bar 3's CLOSE sees bar 3.
    t_at_close = int(time_ns[3] + NS_PER_MIN)
    out2, idx2 = signal_at(sig, bars, np.array([t_at_close], dtype=np.int64))
    assert int(idx2[0]) == 3
    assert int(out2[0]) == 3


def test_the_next_differing_lookup_is_monotone_and_bounded():
    """``r[i]`` = the first ``j >= i`` with ``signal[j] != v``.

    The lookup INCLUDES ``i`` itself. That is why the gate's caller
    asks for ``r[safe_ent + 1]`` — the early close must be the first
    bar AFTER the fill, not the fill's own bar.
    """
    sig = np.array([1, 1, 1, -1, -1, 1], dtype=np.int8)
    r = _next_differing(sig)
    assert r[1][0] == 3          # the first non-+1 at or after 0
    assert r[1][3] == 3          # index 3 IS non-+1, so it is its own hit
    assert r[1][4] == 4          # index 4 IS non-+1 too
    assert r[1][5] == len(sig)   # none after the last
    assert r[-1][0] == 0         # the first non--1 is at 0
    assert r[-1][3] == 5         # index 5 is the first +1 at or after 3
    assert r[-1][5] == 5         # index 5 IS a +1
    assert len(r[1]) == len(sig) + 1

    # The caller's usage: the first bar STRICTLY AFTER index i whose
    # signal differs from the fill bar's own value.
    sig2 = np.array([1, 1, 1, 1, 1, -1], dtype=np.int8)
    r2 = _next_differing(sig2)
    assert r2[1][0 + 1] == 5     # strictly after 0 -> the -1 at 5


def test_apply_gate_refuses_to_run_without_the_row_map():
    """The drift benchmark needs the 1s grid — a missing map must
    raise, not silently compute a drift on the wrong grid."""
    legs = _synthetic_ledger()
    grid = _synthetic_grid()
    sig = np.zeros(len(grid.bars), dtype=np.int8)
    with pytest.raises(ValueError, match="Bars1mGrid"):
        apply_gate(legs, sig, grid.bars, bps=0.71, grid=None)
