"""Guards for the FIXED-SIDE placebo — 2026-10-25.

The episode scoreboard (``ta_1m_signal_sweep``) found cells that beat
the coin-flip-SIDE and random-TIME-SHIFT placebos. That still leaves
the boring explanation open: THE MARKET DRIFTED, and the rule was long.

These tests lock the properties that make the fixed-side arm a valid
control:

1. EXCHANGEABILITY — the arm changes ONLY the side. The intervals,
   the durations, the prices, the abstentions and the time in market
   are all identical to the real arm.
2. The decomposition is EXACT — ``always_long`` and ``always_short``
   are exact negatives of each other, and the rule's WACC is
   reproduced exactly by the scoring code.
3. The arm DETECTS real skill — a synthetic rule that genuinely picks
   the right side MUST beat the constant-side baseline.
4. The arm does NOT fire on a directional bet — a rule that is always
   long on a rising market MUST NOT beat always-long.
"""
from __future__ import annotations

import numpy as np
import pytest

from runtime.diagnostics.ta_fixed_side_placebo import (
    _episode_deltas,
    _force_side,
    _wacc,
)
from runtime.diagnostics.ta_signal_episodes import (
    FLAT,
    LONG,
    SHORT,
    build_episodes,
)
from tests.test_ta_signal_episodes import _synthetic_1m


# ─────────────────────────────────────────────────────────────────
# 1. EXCHANGEABILITY.
# ─────────────────────────────────────────────────────────────────

def test_the_fixed_side_arm_changes_only_the_side():
    """Intervals, prices, durations and abstentions must be untouched."""
    closes = 3000.0 + np.cumsum(np.random.default_rng(3).normal(0, 1, 400))
    b = _synthetic_1m(closes)
    sig = np.zeros(400, dtype=np.int8)
    sig[10:60] = LONG
    sig[100:180] = SHORT
    sig[220:300] = LONG
    eps = build_episodes(b, sig)

    for side in (LONG, SHORT):
        forced = _force_side(eps, side)
        assert len(forced) == len(eps)
        for r, f in zip(eps, forced):
            assert r.signal_bar == f.signal_bar
            assert r.next_signal_bar == f.next_signal_bar
            assert r.close_entry == f.close_entry
            assert r.close_exit == f.close_exit
            assert r.open_entry == f.open_entry
            assert r.open_exit == f.open_exit
            assert r.hold_min == f.hold_min
            if r.state == FLAT:
                assert f.state == FLAT, "an abstention must stay an abstention"
            else:
                assert f.state == side


def test_the_fixed_side_arm_preserves_the_episode_count():
    """Every arm must score the SAME number of directional episodes."""
    closes = 3000.0 + np.cumsum(np.random.default_rng(4).normal(0, 1, 400))
    b = _synthetic_1m(closes)
    sig = np.zeros(400, dtype=np.int8)
    sig[10:60] = LONG
    sig[100:180] = SHORT
    eps = build_episodes(b, sig)

    n_real = len(_episode_deltas(eps, b))
    n_long = len(_episode_deltas(_force_side(eps, LONG), b))
    n_short = len(_episode_deltas(_force_side(eps, SHORT), b))
    assert n_real == n_long == n_short


# ─────────────────────────────────────────────────────────────────
# 2. The decomposition is EXACT.
# ─────────────────────────────────────────────────────────────────

def test_always_long_and_always_short_are_exact_negatives():
    """The two constants must mirror each other exactly.

    If they did not, the "best constant" baseline would depend on an
    arbitrary asymmetry and the edge would be meaningless.
    """
    closes = 3000.0 + np.cumsum(np.random.default_rng(5).normal(0, 1, 400))
    b = _synthetic_1m(closes)
    sig = np.zeros(400, dtype=np.int8)
    sig[10:60] = LONG
    sig[100:180] = SHORT
    eps = build_episodes(b, sig)

    w_long = _wacc(_episode_deltas(_force_side(eps, LONG), b))
    w_short = _wacc(_episode_deltas(_force_side(eps, SHORT), b))
    assert w_long == pytest.approx(-w_short, abs=1e-12)


def test_the_wacc_helper_reproduces_the_scorers_wacc():
    """``_wacc`` must agree with the module's own scorer on the same arm."""
    from runtime.diagnostics.ta_signal_episodes import score_episodes

    closes = 3000.0 + np.cumsum(np.random.default_rng(6).normal(0, 1, 400))
    b = _synthetic_1m(closes)
    sig = np.zeros(400, dtype=np.int8)
    sig[10:60] = LONG
    sig[100:180] = SHORT
    eps = build_episodes(b, sig)

    for arm in (eps, _force_side(eps, LONG), _force_side(eps, SHORT)):
        s = score_episodes(arm, b, trading_minutes=len(b))
        assert _wacc(_episode_deltas(arm, b)) == pytest.approx(
            s["wacc"], abs=1e-9
        )


# ─────────────────────────────────────────────────────────────────
# 3. The arm DETECTS real skill.
# ─────────────────────────────────────────────────────────────────

def test_a_genuine_side_picker_beats_the_constant_side_baseline():
    """A rule that really picks the right side MUST show a positive edge.

    A control that never fires is a broken control.
    """
    # A rising market with a rule that is LONG only on the up-legs and
    # SHORT only on the down-legs: a genuine side picker.
    closes = np.concatenate([
        3000.0 + np.arange(100, dtype=np.float64),      # up
        3100.0 - np.arange(100, dtype=np.float64),      # down
    ])
    b = _synthetic_1m(closes)
    sig = np.zeros(200, dtype=np.int8)
    sig[:100] = LONG
    sig[100:] = SHORT
    eps = build_episodes(b, sig)

    w_rule = _wacc(_episode_deltas(eps, b))
    w_long = _wacc(_episode_deltas(_force_side(eps, LONG), b))
    w_short = _wacc(_episode_deltas(_force_side(eps, SHORT), b))
    edge = w_rule - max(w_long, w_short)
    assert edge > 0.5, (
        f"a genuine side picker showed no edge: rule={w_rule:.3f} "
        f"alwaysL={w_long:.3f} alwaysS={w_short:.3f}"
    )


# ─────────────────────────────────────────────────────────────────
# 4. The arm does NOT fire on a directional bet.
# ─────────────────────────────────────────────────────────────────

def test_a_directional_bet_does_not_beat_the_constant_side_baseline():
    """A rule that is ALWAYS long on a rising market MUST score edge<=0.

    This is the trap the arm exists to catch: "the rule beat the coin
    flip" because the market rose and the rule was long.
    """
    closes = 3000.0 + np.arange(200, dtype=np.float64)   # strictly rising
    b = _synthetic_1m(closes)
    sig = np.ones(200, dtype=np.int8)                     # always long
    eps = build_episodes(b, sig)

    w_rule = _wacc(_episode_deltas(eps, b))
    w_long = _wacc(_episode_deltas(_force_side(eps, LONG), b))
    w_short = _wacc(_episode_deltas(_force_side(eps, SHORT), b))
    edge = w_rule - max(w_long, w_short)
    assert edge <= 1e-9, (
        f"an always-long rule on a rising market showed a POSITIVE edge "
        f"({edge:+.4f}) — the arm is not measuring what it claims"
    )
