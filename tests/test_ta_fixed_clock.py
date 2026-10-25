"""The TRIGGER-SELECTION ARTEFACT — the fixed-clock fix (2026-10-25).

THE DISCOVERY
=============

The signal-to-signal (episode) metric measures the market's move
between two points that **the rule itself chose**. The exit point is
not independent of the rule — it is wherever the rule decided to
change its mind.

So a rule that flips its side at a price threshold **selects the exit
price** at the moment the price re-crossed the threshold. For a
mean-reversion rule that is, by construction, a good exit.

The demonstration:

    rule  :  long if close < mean(close), else short
    data  :  a random walk (no trend, no information)

    signal-to-signal ACC :  0.857   <-- an artefact
    fixed-clock ACC (H=30):  0.452   <-- the truth

The pathological rule has ZERO information, so its true accuracy is
~0.5. The episode metric hands it 0.857 because the trigger picks
the exit.

This file locks the fix:

1. The FIXED-CLOCK score must give the pathological rule ~0.5.
2. The pathological rule must NOT pass the sign-skill check.
3. A genuinely skilled rule must STILL pass it.
4. The fixed-clock excess over the market's own drift must be a
   MEANINGFUL number (not the structural zero the episode framing
   produces).
"""
from __future__ import annotations

import numpy as np
import pytest

from runtime.diagnostics.ta_1m_core import NS_PER_MIN, Bars1m, _atr
from runtime.diagnostics.ta_signal_episodes import (
    LONG,
    SHORT,
    build_episodes,
    score_episodes,
    score_fixed_horizon,
    score_fixed_horizon_with_placebo,
)


def _synthetic_1m(closes: np.ndarray) -> Bars1m:
    closes = np.asarray(closes, dtype=np.float64)
    n = len(closes)
    return Bars1m(
        time_ns=(np.arange(n, dtype=np.int64) * NS_PER_MIN),
        open=closes.copy(), high=closes + 0.1, low=closes - 0.1,
        close=closes.copy(), volume=np.ones(n, dtype=np.float64),
        n_ticks=np.ones(n, dtype=np.int64),
        atr=_atr(closes + 0.1, closes - 0.1, closes, 14),
        atr_n=14,
    )


def _random_walk(n: int = 3000, seed: int = 7) -> np.ndarray:
    rng = np.random.default_rng(seed)
    return 3000.0 + np.cumsum(rng.normal(0.0, 0.5, size=n))


def _pathological_rule(closes: np.ndarray) -> np.ndarray:
    """Long below the mean, short above. NO INFORMATION whatsoever.

    The mean is an EXPANDING (causal) one — a full-sample mean would
    itself be a look-ahead, and would manufacture signal instead of
    merely selecting the exit price.
    """
    n = len(closes)
    x = np.empty(n)
    acc = 0.0
    for i in range(n):
        acc += closes[i]
        x[i] = acc / (i + 1)
    return np.where(closes < x, LONG, SHORT).astype(np.int8)


# ─────────────────────────────────────────────────────────────────
# 1. The artefact exists in the episode metric.
# ─────────────────────────────────────────────────────────────────

def test_the_pathological_rule_scores_high_under_the_episode_metric():
    """DOCUMENT THE ARTEFACT — this is the reason the fix exists.

    If this test ever starts failing, the episode metric changed and
    the fix's justification must be re-examined.
    """
    closes = _random_walk()
    b = _synthetic_1m(closes)
    sig = _pathological_rule(closes)
    s = score_episodes(build_episodes(b, sig), b, trading_minutes=len(b))
    assert s["acc"] > 0.60, (
        f"the pathological rule scored ACC={s['acc']:.3f} — the "
        f"trigger-selection artefact is gone, so the fixed-clock fix's "
        f"justification must be re-examined"
    )


# ─────────────────────────────────────────────────────────────────
# 2. The FIXED-CLOCK score must NOT be fooled.
# ─────────────────────────────────────────────────────────────────

def test_the_pathological_rule_scores_near_half_under_the_fixed_clock():
    """The fix must give a zero-information rule its true score."""
    closes = _random_walk()
    b = _synthetic_1m(closes)
    sig = _pathological_rule(closes)
    s = score_fixed_horizon(b, sig, horizons=(5, 15, 30, 60, 120))

    assert s["n_entries"] > 100
    for h, blk in s["by_horizon"].items():
        assert abs(blk["acc"] - 0.5) < 0.12, (
            f"at horizon {h} the pathological rule scored "
            f"ACC={blk['acc']:.3f}; the fixed-clock score must not be "
            f"fooled by the trigger"
        )


def test_the_pathological_rule_fails_the_sign_skill_check():
    """The whole point of the fix: no skill must be reported as skill."""
    closes = _random_walk()
    b = _synthetic_1m(closes)
    sig = _pathological_rule(closes)
    res = score_fixed_horizon_with_placebo(
        b, sig, horizons=(5, 15, 30, 60, 120), n_random=60, seed=3)

    assert res["sign_skill"] is False, res["verdict"]
    assert res["p_wacc_vs_random_side"] > 0.05, res["verdict"]


def test_the_pathological_rule_fails_the_timing_skill_check():
    """It must also fail the interval-selection check."""
    closes = _random_walk()
    b = _synthetic_1m(closes)
    sig = _pathological_rule(closes)
    res = score_fixed_horizon_with_placebo(
        b, sig, horizons=(5, 15, 30, 60, 120), n_random=60, seed=4)
    assert res["timing_skill"] is False, res["verdict"]


# ─────────────────────────────────────────────────────────────────
# 3. A GENUINELY skilled rule must still pass.
# ─────────────────────────────────────────────────────────────────

def test_a_genuinely_skilled_rule_passes_the_fixed_clock_check():
    """The fix must not have simply made everything score zero.

    A rule that is long in a rising market and short in a falling one
    is genuinely skilled, and the fixed-clock score must SEE it.
    """
    n = 3000
    up = np.concatenate([np.full(n // 2, 3000 + np.arange(n // 2) * 0.5),
                         np.full(n // 2, 3000 + np.arange(n // 2) * 0.5)])
    # A clean regime flip: up for the first half, down for the second.
    down = 3000 + n // 2 * 0.5 - np.arange(n // 2) * 0.5
    closes = np.concatenate([up[:n // 2], down])
    b = _synthetic_1m(closes)

    # The rule: long while the price is above its 100-bar average.
    ma = np.convolve(closes, np.ones(100) / 100, mode="full")[:n]
    sig = np.where(closes > ma, LONG, SHORT).astype(np.int8)

    res = score_fixed_horizon_with_placebo(
        b, sig, horizons=(5, 15, 30, 60, 120), n_random=60, seed=5)
    assert res["real"]["acc"] > 0.6, res["verdict"]
    assert res["sign_skill"] is True, res["verdict"]


def test_the_fixed_clock_is_insensitive_to_the_rule_flipping_speed():
    """A rule that flips every bar must not gain accuracy from it.

    Under the episode metric, flipping fast means many tiny episodes
    and the same trigger-selection bonus. Under the fixed clock, the
    score depends on the DIRECTION, not on how often it is restated.
    """
    closes = _random_walk()
    b = _synthetic_1m(closes)
    x = float(np.mean(closes))
    base = np.where(closes < x, LONG, SHORT).astype(np.int8)

    # The same rule, but restated every bar with a random side.
    rng = np.random.default_rng(9)
    noisy = rng.choice([LONG, SHORT], size=len(closes)).astype(np.int8)

    s_base = score_fixed_horizon(b, base, horizons=(15, 30))
    s_noisy = score_fixed_horizon(b, noisy, horizons=(15, 30))
    for h in ("15", "30"):
        assert abs(s_base["by_horizon"][int(h)]["acc"]
                   - s_noisy["by_horizon"][int(h)]["acc"]) < 0.15, (
            "the fixed-clock score must measure the DIRECTION, not the "
            "flip rate"
        )


# ─────────────────────────────────────────────────────────────────
# 4. The excess over the market's own drift must be meaningful.
# ─────────────────────────────────────────────────────────────────

def test_the_fixed_clock_excess_is_not_structurally_zero():
    """The episode framing forces excess = 0; the fixed clock must not.

    Under the fixed clock the delta's horizon is FIXED while the
    drift's horizon is the rule's OWN, so the difference is real
    information. It must not collapse to zero by construction.
    """
    closes = _random_walk(2000, seed=13)
    b = _synthetic_1m(closes)
    x = float(np.mean(closes))
    sig = np.where(closes < x, LONG, SHORT).astype(np.int8)
    s = score_fixed_horizon(b, sig, horizons=(5, 15, 30, 60, 120))
    assert "excess_gross" in s
    # It is a MEASURED number, not identically zero.
    assert s["excess_gross"] != 0.0
    assert s["n_episodes_used"] > 0


def test_the_excess_is_not_the_drift_recomputed():
    """The excess must NOT be a copy of the drift.

    Subtracting the drift measured over the SAME window as the delta
    is structurally zero — the passive hold of the same side over the
    same window earns exactly the delta. That is the repo's documented
    tautology trap, and the fixed-clock excess must avoid it by
    measuring the delta on a FIXED horizon and the drift over the
    rule's OWN hold.
    """
    closes = _random_walk(2000, seed=15)
    b = _synthetic_1m(closes)
    x = float(np.mean(closes))
    sig = np.where(closes < x, LONG, SHORT).astype(np.int8)
    s = score_fixed_horizon(b, sig, horizons=(5, 15, 30, 60, 120))

    assert s["excess_gross"] == pytest.approx(
        s["rule_gross_fixed_clock"] - s["drift_gross"], rel=1e-9)
    # And it must NOT equal the drift (that would be the tautology).
    assert s["excess_gross"] != pytest.approx(s["drift_gross"])


def test_the_market_drift_is_measured_on_the_rule_own_holding():
    """The drift must use the rule's ACTUAL hold, not a fixed one."""
    closes = _random_walk(2000, seed=14)
    b = _synthetic_1m(closes)
    sig = np.ones(len(closes), dtype=np.int8)      # always long
    s = score_fixed_horizon(b, sig, horizons=(5, 15, 30))
    # Always-long on a random walk: one episode, the whole window.
    assert s["n_episodes_used"] == 1
    assert s["drift_gross"] != 0.0


def test_the_fixed_clock_uses_the_next_bars_open():
    """The executable entry/exit must be on the CLOCK, after the bar.

    Only entries whose FULL horizon fits inside the window are
    scored, so every entry contributes exactly ``h`` bars of move.
    """
    closes = np.arange(200, dtype=np.float64) + 3000.0
    b = _synthetic_1m(closes)
    sig = np.ones(200, dtype=np.int8)
    s = score_fixed_horizon(b, sig, horizons=(5,))
    blk = s["by_horizon"][5]
    # Entry at open[i+1], exit at open[i+1+5] -> a $1/bar ramp gives
    # exactly $5 per entry. Only the entries that fit are counted.
    expected_n = 200 - 1 - 5
    assert blk["n"] == expected_n
    assert blk["gross"] == pytest.approx(expected_n * 5 * 1.0, rel=1e-9)
