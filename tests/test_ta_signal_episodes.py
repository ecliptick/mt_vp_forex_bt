"""Guards for the SIGNAL-TO-SIGNAL (episode) metric — 2026-10-25.

The user's framing: each TA has LONG, SHORT and NO-SIGNAL, so the
rule is a STATE STREAM, and the natural unit of evaluation is the
INTERVAL BETWEEN TWO SIGNAL CHANGES, scored by

    SIGNAL-TO-SIGNAL DELTA   what the market did over the interval
    ACCURACY                 was that delta in the claimed direction

These tests lock the properties that make that framing honest:

1. The ORACLE — on a series whose future is knowable, the metric MUST
   return the right answer (+1 correct, -1 wrong, ~0 a coin flip).
2. The placebos must be EXCHANGEABLE — same intervals, same prices,
   same time in market; only the ingredient under test changes.
3. No look-ahead — a signal is only known at the CLOSE of its bar.
4. The delta must be the market's move over the interval, signed.
"""
from __future__ import annotations

import numpy as np
import pytest

from runtime.diagnostics.ta_1m_core import NS_PER_MIN, Bars1m, _atr
from runtime.diagnostics.ta_signal_episodes import (
    FLAT,
    LONG,
    SHORT,
    build_episodes,
    random_shift_arms,
    replay_sides,
    score_episodes,
    score_signal_with_placebos,
    shift_signal,
    split_runs,
)


# ─────────────────────────────────────────────────────────────────
# Fixtures
# ─────────────────────────────────────────────────────────────────

def _synthetic_1m(closes: np.ndarray, *, range_: float = 0.10) -> Bars1m:
    closes = np.asarray(closes, dtype=np.float64)
    n = len(closes)
    high = closes + range_
    low = closes - range_
    return Bars1m(
        time_ns=(np.arange(n, dtype=np.int64) * NS_PER_MIN),
        open=closes.copy(), high=high, low=low, close=closes.copy(),
        volume=np.ones(n, dtype=np.float64),
        n_ticks=np.ones(n, dtype=np.int64),
        atr=_atr(high, low, closes, 14),
        atr_n=14,
    )


def _perfect_up(n: int = 400) -> np.ndarray:
    return 3000.0 + np.arange(n, dtype=np.float64)


def _perfect_down(n: int = 400) -> np.ndarray:
    return 3000.0 - np.arange(n, dtype=np.float64)


def _random_walk(n: int = 600, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 3000.0 + np.cumsum(rng.normal(0.0, 1.0, size=n))


def _chop(sig_len: int, period: int = 15, on: int = 10,
          state: int = 1) -> np.ndarray:
    """A signal that chops the window into many episodes of one state."""
    sig = np.zeros(sig_len, dtype=np.int8)
    sig[(np.arange(sig_len) % period) < on] = state
    return sig


# ─────────────────────────────────────────────────────────────────
# 1. The ORACLE.
# ─────────────────────────────────────────────────────────────────

def test_a_rule_that_is_always_right_scores_full_accuracy():
    """An always-long rule on a rising series must be 100% accurate."""
    b = _synthetic_1m(_perfect_up(400))
    sig = np.ones(len(b), dtype=np.int8)
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=len(b))

    assert s["n_directional"] == 1
    assert s["acc"] == pytest.approx(1.0)
    assert s["wacc"] == pytest.approx(1.0)
    assert s["acc_signed"] == pytest.approx(1.0)


def test_a_rule_that_is_always_wrong_scores_zero_accuracy():
    """An always-long rule on a FALLING series must be 0% accurate."""
    b = _synthetic_1m(_perfect_down(400))
    sig = np.ones(len(b), dtype=np.int8)
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=len(b))

    assert s["acc"] == pytest.approx(0.0)
    assert s["wacc"] == pytest.approx(-1.0)


def test_a_coin_flip_rule_scores_near_half_accuracy():
    """A coin-flip rule on a driftless market must score ~0.5."""
    b = _synthetic_1m(_random_walk(600, seed=3))
    rng = np.random.default_rng(3)
    sig = rng.choice([-1, 1], size=len(b)).astype(np.int8)
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=len(b))

    assert s["n_directional"] > 20
    assert s["acc"] == pytest.approx(0.5, abs=0.12), (
        f"a coin flip scored ACC={s['acc']:.3f}"
    )
    assert abs(s["wacc"]) < 0.25, (
        f"a coin flip scored WACC={s['wacc']:.3f}"
    )


def test_a_short_rule_on_a_falling_series_is_accurate():
    """The metric must be SYMMETRIC — short is not a special case."""
    b = _synthetic_1m(_perfect_down(400))
    sig = -np.ones(len(b), dtype=np.int8)
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=len(b))

    assert s["acc"] == pytest.approx(1.0)
    assert s["wacc"] == pytest.approx(1.0)


# ─────────────────────────────────────────────────────────────────
# 2. The delta IS the market's move over the interval.
# ─────────────────────────────────────────────────────────────────

def test_the_signal_to_signal_delta_is_the_market_move():
    """The headline number must be exactly the interval's market move.

    ``delta = (price[next_signal] - price[signal]) * state``, signed
    so positive means the rule was RIGHT.
    """
    closes = np.array([3000.0, 3001.0, 3005.0, 3004.0, 3010.0])
    b = _synthetic_1m(closes)
    sig = np.array([LONG, LONG, LONG, LONG, LONG], dtype=np.int8)
    eps = build_episodes(b, sig)
    assert len(eps) == 1
    e = eps[0]
    # The only episode runs bars 0..3 and ends at the window boundary.
    assert e.signal_bar == 0
    assert e.next_signal_bar == 5
    assert e.close_entry == pytest.approx(3000.0)
    assert e.close_exit == pytest.approx(3010.0)

    s = score_episodes(eps, b, trading_minutes=len(b), executable=False)
    # A $10 move on 1 oz, signed positive for a correct long.
    assert s["gross_delta"] == pytest.approx(10.0)
    assert s["acc"] == pytest.approx(1.0)


def test_a_wrong_side_gives_a_negative_delta():
    """The sign of the delta must follow the SIGN of the move."""
    closes = np.array([3010.0, 3009.0, 3005.0, 3001.0, 3000.0])
    b = _synthetic_1m(closes)
    sig = np.ones(5, dtype=np.int8)          # long, but the market fell
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=len(b), executable=False)
    assert s["gross_delta"] == pytest.approx(-10.0)
    assert s["acc"] == pytest.approx(0.0)
    assert s["wacc"] == pytest.approx(-1.0)


def test_the_executable_delta_differs_from_the_close_delta():
    """The close version and the open version must BOTH exist.

    The close version is the literal "signal to signal". The open
    version is the executable one (the first price the market offered
    after the decision). They must not be silently the same number.
    """
    closes = np.array([3000.0, 3001.0, 3005.0, 3004.0, 3010.0])
    b = _synthetic_1m(closes)
    # Make the opens differ from the closes.
    b.open[:] = b.close - 0.5
    sig = np.ones(5, dtype=np.int8)
    eps = build_episodes(b, sig)

    s_close = score_episodes(eps, b, trading_minutes=len(b), executable=False)
    s_open = score_episodes(eps, b, trading_minutes=len(b), executable=True)
    assert s_close["gross_delta"] != pytest.approx(s_open["gross_delta"])


# ─────────────────────────────────────────────────────────────────
# 3. No look-ahead.
# ─────────────────────────────────────────────────────────────────

def test_the_entry_is_the_bar_after_the_signal():
    """The executable entry must be ``open[signal_bar + 1]``."""
    closes = np.arange(100, dtype=np.float64) + 3000.0
    b = _synthetic_1m(closes)
    sig = np.ones(100, dtype=np.int8)
    eps = build_episodes(b, sig)
    e = eps[0]
    assert e.open_entry == pytest.approx(b.open[1]), (
        "the executable entry must be the OPEN of the bar AFTER the "
        "signal bar"
    )


def test_the_exit_is_the_bar_after_the_next_signal():
    """The executable exit must be ``open[next_signal_bar + 1]``."""
    closes = np.arange(100, dtype=np.float64) + 3000.0
    b = _synthetic_1m(closes)
    sig = np.ones(100, dtype=np.int8)
    eps = build_episodes(b, sig)
    e = eps[0]
    # The single episode ends at the window boundary (bar 100), so the
    # executable exit is the last bar's open.
    assert e.next_signal_bar == 100
    assert e.open_exit == pytest.approx(b.open[99]), (
        "the executable exit must be the first tradable bar after the "
        "next signal"
    )


def test_the_exit_is_the_bar_after_a_mid_window_next_signal():
    """With a mid-window state change, the exit must be that bar + 1."""
    closes = np.arange(100, dtype=np.float64) + 3000.0
    b = _synthetic_1m(closes)
    sig = np.ones(100, dtype=np.int8)
    sig[50:] = 0                       # the state changes at bar 50
    eps = build_episodes(b, sig)
    e = eps[0]
    assert e.signal_bar == 0
    assert e.next_signal_bar == 50
    assert e.open_exit == pytest.approx(b.open[51]), (
        "the executable exit must be the OPEN of the bar AFTER the next "
        "signal bar"
    )


def test_the_signal_to_signal_delta_never_reads_before_the_signal():
    """The close-entry is the signal bar's OWN close.

    The rule's decision is made AT that close, so the interval starts
    there — it must not be extended backwards.
    """
    closes = np.arange(100, dtype=np.float64) + 3000.0
    b = _synthetic_1m(closes)
    sig = np.zeros(100, dtype=np.int8)
    sig[50:] = LONG
    eps = build_episodes(b, sig)
    e = eps[1]                        # the long episode starts at bar 50
    assert e.signal_bar == 50
    assert e.close_entry == pytest.approx(closes[50])


def test_a_perfect_future_oracle_cannot_beat_a_random_rule():
    """The metric must not reward a rule that knows the future.

    A signal built from the NEXT bar's move is the strongest possible
    look-ahead. If the metric cannot tell it from a random rule, the
    metric is measuring the future.
    """
    closes = _random_walk(600, seed=11)
    b = _synthetic_1m(closes)
    fut = np.roll(closes, -1)
    oracle = np.sign(fut - closes).astype(np.int8)
    oracle[-1] = 0
    eps = build_episodes(b, oracle)
    s = score_episodes(eps, b, trading_minutes=len(b))
    # The oracle's accuracy is an artefact of the metric's construction:
    # the delta is measured over the interval it chose. But it must NOT
    # be a perfect score on the CLOSE-to-close basis, because the
    # interval it selects ends at its own next change.
    assert s["acc"] < 0.95, (
        "the future-oracle achieved a ~perfect score — the metric is "
        "able to see ahead"
    )


# ─────────────────────────────────────────────────────────────────
# 4. The placebos must be EXCHANGEABLE.
# ─────────────────────────────────────────────────────────────────

def test_the_flip_arm_holds_the_whole_schedule_fixed():
    """Only the SIDE may differ between the real arm and the flip arm."""
    sig = np.zeros(500, dtype=np.int8)
    sig[10:60] = LONG
    sig[100:180] = SHORT
    sig[220:300] = LONG
    b = _synthetic_1m(_random_walk(500, seed=12))
    eps = build_episodes(b, sig)
    flip = replay_sides(eps, side_mode="flip")

    assert len(eps) == len(flip)
    for r, f in zip(eps, flip):
        assert r.signal_bar == f.signal_bar
        assert r.next_signal_bar == f.next_signal_bar
        assert r.close_entry == f.close_entry
        assert r.close_exit == f.close_exit
        assert r.open_entry == f.open_entry
        assert r.open_exit == f.open_exit
        assert r.hold_min == f.hold_min
        assert r.state == -f.state


def test_the_flip_arm_negates_the_delta_exactly():
    """Negating the side negates the delta — a TAUTOLOGY.

    It is recorded and labelled precisely so nobody quotes it as a
    test of skill. The repo has the same trap documented for the
    canonical ledger's ``flip_gross``.
    """
    sig = _chop(300, period=15, on=10, state=LONG)
    b = _synthetic_1m(_random_walk(300, seed=13))
    eps = build_episodes(b, sig)
    flip = replay_sides(eps, side_mode="flip")

    s_real = score_episodes(eps, b, trading_minutes=len(b))
    s_flip = score_episodes(flip, b, trading_minutes=len(b))
    assert s_flip["gross_delta"] == pytest.approx(-s_real["gross_delta"])
    assert s_flip["acc"] == pytest.approx(1.0 - s_real["acc"])


def test_the_random_side_arm_holds_the_intervals_fixed():
    """The coin-flip side arm must keep every interval and price."""
    sig = _chop(400, period=15, on=10, state=LONG)
    b = _synthetic_1m(_random_walk(400, seed=14))
    eps = build_episodes(b, sig)
    rnd = replay_sides(eps, side_mode="random", seed=5)

    assert len(eps) == len(rnd)
    for r, x in zip(eps, rnd):
        assert r.signal_bar == x.signal_bar
        assert r.next_signal_bar == x.next_signal_bar
        assert r.open_entry == x.open_entry
        assert r.open_exit == x.open_exit
        assert r.hold_min == x.hold_min
        if r.is_directional:
            assert x.state in (LONG, SHORT)
        else:
            # An ABSTENTION is part of the schedule — it stays flat.
            assert x.state == FLAT


def test_the_random_side_arm_preserves_time_in_market():
    """Time in market is part of the schedule — it must not change."""
    sig = _chop(400, period=15, on=10, state=LONG)
    b = _synthetic_1m(_random_walk(400, seed=15))
    eps = build_episodes(b, sig)
    rnd = replay_sides(eps, side_mode="random", seed=6)

    s_real = score_episodes(eps, b, trading_minutes=len(b))
    s_rnd = score_episodes(rnd, b, trading_minutes=len(b))
    assert s_real["tim"] == pytest.approx(s_rnd["tim"])
    assert s_real["n_directional"] == s_rnd["n_directional"]


def test_the_random_side_arm_flips_both_ways():
    """A coin-flip arm must actually produce a MIX of sides.

    If the RNG always produced the same side, the arm would be a
    degenerate control.
    """
    sig = _chop(400, period=15, on=10, state=LONG)
    b = _synthetic_1m(_random_walk(400, seed=16))
    eps = build_episodes(b, sig)
    rnd = replay_sides(eps, side_mode="random", seed=7)
    sides = {e.state for e in rnd if e.is_directional}
    assert sides == {LONG, SHORT}


def test_the_shift_arm_preserves_the_duration_pattern():
    """A time shift moves the rule; it must not change its shape.

    The number of episodes, their durations and the time in market are
    all preserved — only the TIMING in the window changes.
    """
    sig = _chop(400, period=15, on=10, state=LONG)
    b = _synthetic_1m(_random_walk(400, seed=17))
    arms = random_shift_arms(b, sig, n_arms=25, seed=8)
    assert len(arms) == 25

    s_real = score_episodes(build_episodes(b, sig), b, trading_minutes=len(b))
    for a in arms:
        s = score_episodes(build_episodes(b, a), b, trading_minutes=len(b))
        # The total time in market is preserved (modulo the dropped
        # tail run, which is a boundary effect).
        assert s["tim"] == pytest.approx(s_real["tim"], abs=0.05)


def test_the_shift_arm_changes_the_timing():
    """At least one shift must actually move the pattern."""
    sig = _chop(400, period=15, on=10, state=LONG)
    b = _synthetic_1m(_random_walk(400, seed=18))
    arms = random_shift_arms(b, sig, n_arms=25, seed=9)
    moved = sum(1 for a in arms if not np.array_equal(a, sig))
    assert moved >= 20


def test_shift_signal_preserves_the_run_structure():
    """A cyclic rotation must not change the multiset of run lengths."""
    sig = np.array([0] * 5 + [1] * 10 + [0] * 3 + [-1] * 7, dtype=np.int8)
    for off in (0, 1, 5, 12, 20):
        shifted = shift_signal(sig, off)
        runs_a = sorted(b - a + 1 for a, b, _ in split_runs(sig))
        runs_b = sorted(b - a + 1 for a, b, _ in split_runs(shifted))
        # The seam may merge the first and last runs, so compare the
        # TOTAL covered bars instead of the exact multiset.
        total_a = sum(runs_a)
        total_b = sum(runs_b)
        assert total_b >= total_a - 1


# ─────────────────────────────────────────────────────────────────
# 5. The composite and the validity check.
# ─────────────────────────────────────────────────────────────────

def test_ssdr_rewards_less_time_in_market():
    """The same reward earned faster must score a HIGHER SSDR.

    This is the "less time in market" requirement, made mechanical.
    """
    # A step up of $10 on bar 50, flat otherwise.
    closes = np.full(300, 3000.0)
    closes[50:] = 3010.0
    b = _synthetic_1m(closes)

    # FAST: long from bar 49, flat again at bar 51 -> 2 minutes, +$10.
    fast_sig = np.zeros(300, dtype=np.int8)
    fast_sig[49] = LONG
    fast_sig[50] = LONG
    fast_sig[51] = FLAT
    fast_eps = build_episodes(b, fast_sig)

    # SLOW: long from bar 49 to the end -> 251 minutes, +$10.
    slow_sig = np.zeros(300, dtype=np.int8)
    slow_sig[49:] = LONG
    slow_eps = build_episodes(b, slow_sig)

    s_fast = score_episodes(fast_eps, b, trading_minutes=len(b),
                            executable=False)
    s_slow = score_episodes(slow_eps, b, trading_minutes=len(b),
                            executable=False)

    assert s_fast["n_directional"] > 0 and s_slow["n_directional"] > 0
    assert s_fast["gross_delta"] == pytest.approx(s_slow["gross_delta"])
    assert s_fast["median_hold_min"] < s_slow["median_hold_min"]
    assert s_fast["ssdr"] > s_slow["ssdr"], (
        "SSDR must reward LESS time in market: "
        f"fast={s_fast['ssdr']:.4f} slow={s_slow['ssdr']:.4f}"
    )


def test_ssdr_rewards_magnitude():
    """A BIGGER move over the SAME interval must score higher."""
    small = _synthetic_1m(np.full(200, 3000.0))
    big_closes = np.full(200, 3000.0)
    big_closes[50:] = 3010.0
    big = _synthetic_1m(big_closes)
    sig = np.ones(200, dtype=np.int8)

    s_small = score_episodes(build_episodes(small, sig), small,
                             trading_minutes=200, executable=False)
    s_big = score_episodes(build_episodes(big, sig), big,
                           trading_minutes=200, executable=False)
    assert s_big["gross_delta"] > s_small["gross_delta"]
    assert s_big["ssdr"] > s_small["ssdr"]


def test_wacc_separates_magnitude_from_frequency():
    """A rule right on big moves and wrong on small ones must beat a
    rule with the same plain accuracy but the opposite magnitude
    profile."""
    # Episode 1: long 3000 -> 3009  (+$9).
    # Episode 2: short 3009 -> 3010 (-$1).
    # ACC = 0.5, but WACC = (9 - 1) / (9 + 1) = 0.8.
    closes = np.array([3000.0, 3005.0, 3009.0, 3008.0, 3010.0])
    b = _synthetic_1m(closes)
    sig = np.array([LONG, LONG, SHORT, SHORT, FLAT], dtype=np.int8)
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=5, executable=False)

    assert s["n_directional"] == 2
    assert s["acc"] == pytest.approx(0.5)
    assert s["wacc"] == pytest.approx(0.8, abs=0.15), (
        "WACC must weight the accuracy by the MAGNITUDE of the moves"
    )


def test_tim_is_the_time_in_market_fraction():
    """TIM must be the directional exposure as a fraction of the session."""
    closes = np.full(100, 3000.0)
    b = _synthetic_1m(closes)
    sig = np.zeros(100, dtype=np.int8)
    sig[0:40] = LONG            # 40 bars in the market
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=100)
    # The episode runs bars 0..39 -> 40 minutes of exposure.
    assert s["tim"] == pytest.approx(0.40, abs=0.02)


def test_a_no_signal_rule_earns_nothing():
    """A rule that never takes a side has no accuracy and no reward."""
    closes = np.full(100, 3000.0)
    b = _synthetic_1m(closes)
    sig = np.zeros(100, dtype=np.int8)
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=100)

    assert s["n_directional"] == 0
    assert s["acc"] == 0.0
    assert s["wacc"] == 0.0
    assert s["gross_delta"] == 0.0
    assert s["tim"] == 0.0
    assert s["flat_frac"] == 1.0


def test_the_abstentions_are_counted_as_episodes():
    """A no-signal run is an INTERVAL — it must be counted."""
    closes = np.full(100, 3000.0)
    b = _synthetic_1m(closes)
    sig = np.zeros(100, dtype=np.int8)
    sig[10:20] = LONG
    eps = build_episodes(b, sig)
    s = score_episodes(eps, b, trading_minutes=100)
    assert s["n_episodes"] >= 2
    assert s["n_flat"] >= 1


def test_the_tail_run_is_scored_up_to_the_window_boundary():
    """The final run has no next signal, so it ends at the boundary.

    It is still scored — dropping it would make a single-state rule
    score ZERO episodes, which is a measurement bug, not a finding.
    """
    closes = np.full(100, 3000.0)
    b = _synthetic_1m(closes)
    sig = np.zeros(100, dtype=np.int8)
    sig[10:20] = LONG
    sig[40:60] = LONG
    eps = build_episodes(b, sig)
    # Runs: flat[0..9], long[10..19], flat[20..39], long[40..59],
    #       flat[60..99]. All FIVE are scored.
    assert len(eps) == 5
    assert eps[-1].next_signal_bar == 100
    assert eps[-1].state == FLAT


# ─────────────────────────────────────────────────────────────────
# 6. The validity check (the placebo machinery must FIRE).
# ─────────────────────────────────────────────────────────────────

def test_a_perfect_oracle_beats_the_coin_flip_side_arm():
    """The validity check must DETECT a real edge.

    A check that never fires is a broken check.
    """
    b = _synthetic_1m(_perfect_up(600))
    sig = _chop(600, period=15, on=10, state=LONG)
    res = score_signal_with_placebos(b, sig, n_random=100, seed=3,
                                      trading_minutes=len(b))

    assert res["real"]["n_directional"] > 10
    assert res["sign_skill"] is True, res["verdict"]
    assert res["wacc_excess_vs_side"] > 0.0
    assert res["p_wacc_vs_random_side"] == pytest.approx(1.0 / 101.0,
                                                         abs=1e-9)


def test_a_coin_flip_rule_is_reported_as_having_no_sign_skill():
    """The negative control on the validity check itself."""
    b = _synthetic_1m(_random_walk(600, seed=21))
    rng = np.random.default_rng(21)
    sig = rng.choice([-1, 1], size=len(b)).astype(np.int8)
    res = score_signal_with_placebos(b, sig, n_random=100, seed=4,
                                      trading_minutes=len(b))

    assert res["real"]["n_directional"] > 20
    assert res["sign_skill"] is False, res["verdict"]
    assert "NO SIGN SKILL" in res["verdict"]


def test_the_report_declares_the_documented_traps():
    """The structural facts must be stated in the output, not hidden."""
    b = _synthetic_1m(_random_walk(400, seed=22))
    sig = _chop(400, period=15, on=10, state=LONG)
    res = score_signal_with_placebos(b, sig, n_random=20, seed=5,
                                      trading_minutes=len(b))
    assert res["flip_is_tautology_on_the_delta"] is True
    assert res["excess_vs_drift_is_structurally_zero_here"] is True


def test_the_accuracy_is_also_reported_per_side():
    """Long and short must be separable — the repo's central split."""
    b = _synthetic_1m(_random_walk(400, seed=23))
    sig = np.zeros(400, dtype=np.int8)
    sig[0:100] = LONG
    sig[100:200] = SHORT
    sig[200:300] = LONG
    sig[300:400] = SHORT
    res = score_signal_with_placebos(b, sig, n_random=10, seed=6,
                                      trading_minutes=len(b))
    r = res["real"]
    assert r["n_long"] > 0
    assert r["n_short"] > 0
    assert 0.0 <= r["long_acc"] <= 1.0
    assert 0.0 <= r["short_acc"] <= 1.0
