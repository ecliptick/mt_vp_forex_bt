"""Guards for the 1-minute TA metric + executor (2026-10-25).

THE POINT OF THIS FILE
======================

The user asked for a metric that measures whether a directional rule is
"correct directionally", that rewards the MAGNITUDE of directional
moves, and that rewards LESS TIME IN MARKET — and then asked whether
that metric is actually CORRECT.

So the metric is a claim, and a claim needs a falsification test. These
tests are the falsification:

1. **The ORACLE** — on a synthetic series where the future is knowable,
   the metric MUST return the right answer. An always-correct rule
   scores +1, its negation -1, a coin flip ~0. If the metric cannot
   pass this, it measures nothing.

2. **The PLACEBO arms must be EXCHANGEABLE** — the ``flip`` and
   ``random`` arms replay the real arm's SCHEDULE (same entry bar,
   same entry price, same exit bar, same exit price, same time in
   market, same path, same fee); ONLY the side differs. This is the
   repo's hard rule for a control, learned from the confounded
   ``leg_random_entry`` test.

3. **The executor must be look-ahead free** — a signal computed on bar
   i may only be traded from bar i+1.

4. **The bar builder must handle the irregular feed** — the canonical
   1s feed has ~46 ticks/minute and weekend gaps. The builder must
   bucket by TIMESTAMP (not row position) and must DROP empty minutes
   rather than forward-fill them.

5. **The fee model must be the live MT4 round trip** — $0.47 at
   $3,000/oz, scaling with the FILL price (the D1 2x bug guard).

6. **The metric must be computable and bounded** — DAC in [-1, +1],
   TIM in [0, 1], and the three user requirements (magnitude, less
   time in market, fee coverage) must be individually mechanical.

Run with::

    python -m pytest tests/test_ta_1m_metric.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from runtime.diagnostics.ta_1m_core import (
    NS_PER_MIN,
    OZ,
    ROUND_TRIP_USD,
    Bars1m,
    _atr,
    _segments,
    build_bars_1m,
    fee_bps_for,
    replay_legs,
    run_ta,
    score_legs,
    score_with_placebos,
    set_fee_spot,
)


# ─────────────────────────────────────────────────────────────────
# Helpers: a synthetic 1m series with a KNOWN future.
# ─────────────────────────────────────────────────────────────────

def _synthetic_1m(closes: np.ndarray, *, spread: float = 0.0
                  ) -> Bars1m:
    """Build a ``Bars1m`` from a close series (one bar per close)."""
    n = len(closes)
    closes = np.asarray(closes, dtype=np.float64)
    high = closes + spread
    low = closes - spread
    return Bars1m(
        time_ns=(np.arange(n, dtype=np.int64) * NS_PER_MIN),
        open=closes.copy(),
        high=high.copy(),
        low=low.copy(),
        close=closes.copy(),
        volume=np.ones(n, dtype=np.float64),
        n_ticks=np.ones(n, dtype=np.int64),
        atr=_atr(high, low, closes, 14),
        atr_n=14,
    )


def _perfect_up(n: int = 400) -> np.ndarray:
    """A strictly rising series: every bar is +$1."""
    return 3000.0 + np.arange(n, dtype=np.float64) * 1.0


def _random_walk(n: int = 600, seed: int = 0) -> np.ndarray:
    """A driftless random walk: no side has an edge."""
    rng = np.random.default_rng(seed)
    return 3000.0 + np.cumsum(rng.normal(0.0, 1.0, size=n))


def _flat_with_range(price: float, n: int, rng_: float = 0.10) -> Bars1m:
    """A dead-flat price with a NON-ZERO bar range.

    Needed by the flat-leg and fee tests: a flat price with a zero
    range has a zero ATR, and a zero ATR means the stop cannot be
    sized — so the executor correctly produces NO legs at all. The
    price is flat, but each bar still has a wick, so the leg's gross
    is exactly zero.
    """
    closes = np.full(n, price, dtype=np.float64)
    high = closes + rng_
    low = closes - rng_
    return Bars1m(
        time_ns=(np.arange(n, dtype=np.int64) * NS_PER_MIN),
        open=closes.copy(), high=high, low=low, close=closes.copy(),
        volume=np.ones(n, dtype=np.float64),
        n_ticks=np.ones(n, dtype=np.int64),
        atr=_atr(high, low, closes, 14),
        atr_n=14,
    )


def _chopped_up(n: int = 600) -> tuple[np.ndarray, np.ndarray]:
    """A perfect uptrend plus a signal that CHOPPS it into many legs.

    A constant signal is ONE leg, and a one-leg sample has no power in
    a sign-flip randomisation test. This fixture yields ~40 legs, all
    of them profitable, which is the configuration the oracle test
    needs.
    """
    closes = _perfect_up(n)
    sig = np.zeros(n, dtype=np.int8)
    sig[(np.arange(n) % 15) < 10] = 1      # 10 bars long, 5 bars flat
    return closes, sig


# ─────────────────────────────────────────────────────────────────
# 1. The ORACLE — the metric must be CORRECT on a known series.
# ─────────────────────────────────────────────────────────────────

def test_oracle_perfect_uptrend_scores_full_directional_correctness():
    """A rule that is ALWAYS long on a rising series must score +1 DAC.

    This is the ground truth. If the metric cannot return +1 here, it
    does not measure directional correctness.
    """
    b = _synthetic_1m(_perfect_up(400))
    signal = np.ones(len(b), dtype=np.int8)
    set_fee_spot(3000.0)

    legs = run_ta(b, signal, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10_000)
    s = score_legs(legs, b, trading_minutes=len(b))

    assert s["n"] > 0
    assert s["dac_path"] == pytest.approx(1.0, abs=1e-9), (
        "a rule that is always right must score DAC = +1")
    assert s["dac_mfe"] == pytest.approx(1.0, abs=1e-9)
    assert s["drem"] > 0.0, "DREM must be positive for a correct rule"


def test_oracle_negated_perfect_uptrend_scores_minus_one():
    """The negated oracle must score -1 — the metric has a SIGN."""
    b = _synthetic_1m(_perfect_up(400))
    signal = -np.ones(len(b), dtype=np.int8)
    set_fee_spot(3000.0)

    legs = run_ta(b, signal, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10_000)
    s = score_legs(legs, b, trading_minutes=len(b))

    assert s["n"] > 0
    assert s["dac_path"] == pytest.approx(-1.0, abs=1e-9), (
        "a rule that is always wrong must score DAC = -1")


def test_oracle_coin_flip_scores_near_zero():
    """A coin-flip rule must score near 0 — the metric is not biased."""
    b = _synthetic_1m(_perfect_up(600))
    rng = np.random.default_rng(7)
    signal = rng.choice([-1, 1], size=len(b)).astype(np.int8)
    set_fee_spot(3000.0)

    legs = run_ta(b, signal, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10_000)
    s = score_legs(legs, b, trading_minutes=len(b))

    assert s["n"] > 0
    assert abs(s["dac_path"]) < 0.35, (
        f"a coin flip scored DAC={s['dac_path']:.3f}; the metric is "
        "biased"
    )


def test_oracle_on_a_driftless_random_walk_scores_near_zero():
    """On a market with no direction, no side can be 'correct'.

    This is the case that exposed the "flat leg = loss" bug: a leg
    that closed at exactly breakeven used to be scored as a LOSS, so
    an indistinguishable rule scored -1 instead of ~0.

    The signal CHOPPS the window into many legs — a constant signal
    is a single leg, and a single leg says nothing about a
    direction call.
    """
    for seed in (1, 2, 3):
        b = _synthetic_1m(_random_walk(600, seed=seed))
        sig = np.zeros(len(b), dtype=np.int8)
        sig[(np.arange(len(b)) % 15) < 10] = 1
        for side in (1, -1):
            signal = side * sig
            set_fee_spot(3000.0)
            legs = run_ta(b, signal, sl_mult=1.0, tp_mult=2.0,
                          max_hold_bars=30)
            s = score_legs(legs, b, trading_minutes=len(b))
            assert s["n"] > 5, "the fixture must produce several legs"
            assert abs(s["dac_path"]) < 0.35, (
                f"seed={seed} side={side} scored DAC={s['dac_path']:.3f} "
                "on a driftless market — the metric is biased"
            )


def test_a_flat_leg_is_neither_right_nor_wrong():
    """``C = 0`` for a zero-P&L leg, and the count is reported."""
    b = _flat_with_range(3000.0, 100)
    signal = np.ones(len(b), dtype=np.int8)
    set_fee_spot(3000.0)

    legs = run_ta(b, signal, sl_mult=1.0, tp_mult=2.0, max_hold_bars=20)
    s = score_legs(legs, b, trading_minutes=len(b))
    assert s["n"] > 0
    assert s["n_flat_legs"] == s["n"], "every leg is flat on a dead-flat series"
    assert s["dac_path"] == pytest.approx(0.0, abs=1e-9), (
        "flat legs must not be scored as losses"
    )
    assert s["drem"] == pytest.approx(0.0, abs=1e-9)


# ─────────────────────────────────────────────────────────────────
# 2. The PLACEBO arms must be EXCHANGEABLE.
# ─────────────────────────────────────────────────────────────────

def test_the_replay_arms_hold_the_whole_schedule_fixed():
    """The placebo must change the SIDE and NOTHING else.

    If the arms had different entry bars, different exits, or a
    different time in market, the comparison would be measuring the
    SCHEDULE, not the direction. This is the ``leg_random_entry``
    confound, locked shut.
    """
    sig = np.zeros(500, dtype=np.int8)
    sig[10:60] = 1
    sig[100:180] = -1
    sig[220:300] = 1
    b = _synthetic_1m(_random_walk(500, seed=3))
    set_fee_spot(3000.0)

    real = run_ta(b, sig, sl_mult=1.5, tp_mult=3.0, max_hold_bars=60)
    flip = replay_legs(b, real, side_mode="flip")
    rnd = replay_legs(b, real, side_mode="random", seed=42)

    assert len(real) == len(flip) == len(rnd)
    for other in (flip, rnd):
        assert [lg.entry_bar for lg in real] == [lg.entry_bar for lg in other]
        assert [lg.exit_bar for lg in real] == [lg.exit_bar for lg in other]
        assert [lg.entry_price for lg in real] == \
            [lg.entry_price for lg in other]
        assert [lg.exit_price for lg in real] == \
            [lg.exit_price for lg in other]
        assert [lg.hold_min for lg in real] == [lg.hold_min for lg in other]
        assert [lg.path_usd for lg in real] == [lg.path_usd for lg in other]
        assert [lg.fee_usd for lg in real] == [lg.fee_usd for lg in other]

    # Only the side differs.
    assert all(r.direction == -f.direction for r, f in zip(real, flip))


def test_the_flip_arm_under_a_fixed_schedule_is_exactly_the_negative():
    """The flip arm's gross must be exactly ``-gross`` of the real arm.

    That is the TAUTOLOGY the repo has already documented for the
    canonical ledger: holding entry, exit and size fixed, negating the
    side negates the P&L exactly. It is recorded so nobody quotes it
    as a test of skill.
    """
    sig = np.zeros(300, dtype=np.int8)
    sig[10:120] = 1
    sig[150:250] = -1
    b = _synthetic_1m(_random_walk(300, seed=4))
    set_fee_spot(3000.0)

    real = run_ta(b, sig, sl_mult=1.5, tp_mult=3.0, max_hold_bars=60)
    flip = replay_legs(b, real, side_mode="flip")

    for r, f in zip(real, flip):
        assert f.gross_usd == pytest.approx(-r.gross_usd, rel=1e-12)
        assert f.hold_min == r.hold_min
        assert f.path_usd == r.path_usd


def test_the_report_flags_the_flip_arm_as_a_tautology():
    """The harness must label the flip arm, not present it as a test."""
    b = _synthetic_1m(_random_walk(400, seed=5))
    signal = np.ones(len(b), dtype=np.int8)
    set_fee_spot(3000.0)

    res = score_with_placebos(
        b, signal, sl_mult=1.5, tp_mult=3.0, max_hold_bars=60,
        n_random=20, seed=1, trading_minutes=len(b))

    assert res["flip_is_tautology_under_fixed_schedule"] is True
    # The flip arm's gross must be the exact negative of the real arm's.
    assert res["flip"]["gross_total"] == pytest.approx(
        -res["real"]["gross_total"], rel=1e-9)


def test_a_perfect_oracle_beats_every_coin_flip_arm():
    """The placebo machinery must DETECT a real edge.

    A validity check that never fires is a broken check. On the
    perfect-up oracle, the real arm must beat all 100 coin-flip arms
    on DREM and the p-value must be at its floor.
    """
    closes, sig = _chopped_up(600)
    b = _synthetic_1m(closes)
    set_fee_spot(3000.0)

    res = score_with_placebos(
        b, sig, sl_mult=1000.0, tp_mult=1000.0, max_hold_bars=10_000,
        n_random=100, seed=5, trading_minutes=len(b))

    assert res["real"]["n"] > 5, "the fixture must produce several legs"
    assert res["metric_is_directionally_valid"] is True
    assert res["drem_excess"] > 0.0
    # 100 random arms + the real one -> the floor of the p-value is
    # 1/101.
    assert res["p_drem_vs_random"] == pytest.approx(1.0 / 101.0, abs=1e-9)


def test_a_coin_flip_rule_is_flagged_invalid():
    """A rule with NO directional skill must be reported INVALID.

    This is the negative control on the validity check itself.
    """
    b = _synthetic_1m(_random_walk(600, seed=13))
    rng = np.random.default_rng(13)
    signal = rng.choice([-1, 1], size=len(b)).astype(np.int8)
    set_fee_spot(3000.0)

    res = score_with_placebos(
        b, signal, sl_mult=1.5, tp_mult=3.0, max_hold_bars=240,
        n_random=100, seed=5, trading_minutes=len(b))

    assert res["metric_is_directionally_valid"] is False, (
        "a coin-flip rule was reported as directionally valid — the "
        "validity check is not doing its job"
    )
    assert "METRIC INVALID" in res["verdict"]


# ─────────────────────────────────────────────────────────────────
# 3. No look-ahead.
# ─────────────────────────────────────────────────────────────────

def test_a_signal_is_only_traded_from_the_bar_after_it_forms():
    """The entry bar must be strictly AFTER the signal bar.

    A signal computed on bar i is known at the CLOSE of bar i, so the
    earliest tradable moment is the OPEN of bar i+1. Entering on bar i
    itself would be a one-bar look-ahead.
    """
    sig = np.zeros(50, dtype=np.int8)
    sig[10:20] = 1
    b = _synthetic_1m(_random_walk(50, seed=6))
    set_fee_spot(3000.0)

    legs = run_ta(b, sig, sl_mult=1.5, tp_mult=3.0, max_hold_bars=10)
    assert legs, "expected at least one leg"
    for lg in legs:
        assert lg.entry_bar == lg.signal_bar + 1, (
            f"entered on bar {lg.entry_bar} for a signal on bar "
            f"{lg.signal_bar} — that is a look-ahead"
        )


def test_a_lookahead_signal_does_not_reach_a_perfect_hit_rate():
    """Peeking at the future must not help.

    A metric that rewards a look-ahead is measuring the future, not
    the rule. The 'oracle' here is a signal that is the NEGATION of
    the realised next-bar move — it can only be built with knowledge
    of the future.
    """
    closes = _random_walk(400, seed=8)
    b = _synthetic_1m(closes)
    fut = np.roll(closes, -1)
    oracle = np.sign(fut - closes).astype(np.int8)
    oracle[-1] = 0
    set_fee_spot(3000.0)

    legs = run_ta(b, oracle, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10_000)
    assert legs
    s = score_legs(legs, b, trading_minutes=len(b))
    assert s["hit_rate"] < 0.95, (
        "the executor harvested a next-close forecast at a ~100% hit "
        "rate — it must not be able to trade the future"
    )


def test_segments_never_enter_on_the_signal_bar():
    """``_segments`` must place the entry strictly after the run start."""
    sig = np.zeros(20, dtype=np.int8)
    sig[5:9] = -1
    segs = _segments(sig)
    assert len(segs) == 1
    entry_bar, exit_due, side = segs[0]
    assert entry_bar == 6
    assert side == -1
    assert exit_due > entry_bar


# ─────────────────────────────────────────────────────────────────
# 4. The bar builder.
# ─────────────────────────────────────────────────────────────────

def _frame(times_ns: np.ndarray, closes: np.ndarray) -> pd.DataFrame:
    return pd.DataFrame({
        "time": pd.to_datetime(times_ns, unit="ns", utc=True),
        "open": closes,
        "high": closes + 0.5,
        "low": closes - 0.5,
        "close": closes,
        "tickv": np.ones(len(closes)),
    })


def test_the_bar_builder_buckets_by_timestamp_not_row_position():
    """Row-position bucketing is the NB94 forward-leak class of bug.

    Two minutes that are FAR apart in wall-clock time but adjacent in
    the frame must land in DIFFERENT 1m buckets. The builder must
    never put them in the same bucket.
    """
    # 3 ticks at t=0, then 3 ticks at t=+10 minutes.
    t = np.array([0, 1, 2, 600, 601, 602], dtype=np.int64) * 10**9
    df = _frame(t, 3000.0 + np.arange(6, dtype=np.float64))
    b = build_bars_1m(df)
    assert len(b) == 2, (
        f"expected 2 one-minute buckets, got {len(b)} — the builder "
        "bucketed by row position"
    )
    assert b.n_ticks.tolist() == [3, 3]


def test_the_bar_builder_drops_empty_minutes_rather_than_filling():
    """A gap must not become a zero-range bar.

    A forward-filled bar has a zero range, which poisons the ATR and
    lets a rule "trade" during a session that did not exist.
    """
    t = np.array([0, 1, 2, 120, 121, 122], dtype=np.int64) * 10**9
    df = _frame(t, 3000.0 + np.arange(6, dtype=np.float64))
    b = build_bars_1m(df)
    assert len(b) == 2
    assert (b.high > b.low).all(), "every built bar must have a range"
    assert np.isfinite(b.atr).all()
    assert b.atr[0] > 0.0


def test_the_bar_builder_reads_millisecond_timestamps_as_nanoseconds():
    """The canonical parquet stores ``timestamp[ms]``.

    ``.astype("int64")`` on that column yields MILLISECONDS, which
    collapses every bar into one bucket (measured: 30,197 minutes
    became 1). The builder must use the ns conversion.
    """
    t_ms = np.array([0, 1, 2, 60_000, 60_001], dtype=np.int64)
    df = pd.DataFrame({
        "time": pd.to_datetime(t_ms, unit="ms", utc=True),
        "open": 3000.0, "high": 3000.5, "low": 2999.5, "close": 3000.0,
        "tickv": 1.0,
    })
    b = build_bars_1m(df)
    assert len(b) == 2, (
        f"expected 2 one-minute buckets, got {len(b)} — the ms/ns "
        "conversion was not applied"
    )


def test_the_bar_builder_aggregates_high_low_open_close_correctly():
    """OHLC aggregation must be first-open, max-high, min-low, last-close."""
    t = np.arange(5, dtype=np.int64) * 10**9
    df = pd.DataFrame({
        "time": pd.to_datetime(t, unit="ns", utc=True),
        "open": [10.0, 11.0, 12.0, 13.0, 14.0],
        "high": [10.5, 15.0, 12.5, 13.5, 14.5],
        "low": [9.5, 10.5, 8.0, 12.5, 13.5],
        "close": [10.2, 14.0, 9.0, 13.0, 14.2],
        "tickv": [1.0, 1.0, 1.0, 1.0, 1.0],
    })
    b = build_bars_1m(df)
    assert len(b) == 1
    assert b.open[0] == pytest.approx(10.0)     # FIRST open
    assert b.high[0] == pytest.approx(15.0)     # MAX high
    assert b.low[0] == pytest.approx(8.0)       # MIN low
    assert b.close[0] == pytest.approx(14.2)    # LAST close
    assert b.n_ticks[0] == 5
    assert b.volume[0] == pytest.approx(5.0)


# ─────────────────────────────────────────────────────────────────
# 5. The fee model.
# ─────────────────────────────────────────────────────────────────

def test_the_fee_is_the_live_mt4_round_trip():
    """$0.47 at $3,000/oz: $0.07 commission + $0.40 spread."""
    assert ROUND_TRIP_USD == pytest.approx(0.47, abs=1e-9)
    bps = fee_bps_for(3000.0)
    charged = 3000.0 * OZ * (bps / 10000.0) * 2.0
    assert charged == pytest.approx(0.47, abs=1e-6), (
        f"charged ${charged:.4f}, expected the $0.47 live round trip")


def test_the_fee_scales_with_the_fill_price():
    """The engine's fee is ``(entry + exit) * oz * bps / 10000``.

    The ``bps`` rate is derived ONCE from the corpus MEDIAN spot (see
    ``_derive_taker_fee_bps``), and then the per-leg dollar cost
    scales with the price that leg ACTUALLY filled at. So a leg that
    filled at $4,000 costs more than a leg that filled at $3,000 —
    which is exactly why the repo reports a MEASURED fee per leg
    ($0.5029 on the 1y corpus) alongside the $0.47 spec at $3,000.
    A flat $0.47 would overstate the edge.
    """
    # The corpus median spot fixes the RATE...
    set_fee_spot(3000.0)

    # ...and the leg's own fill price scales the DOLLAR cost.
    b3k = _flat_with_range(3000.0, 50)
    sig = np.ones(50, dtype=np.int8)
    legs3k = run_ta(b3k, sig, sl_mult=1.0, tp_mult=2.0, max_hold_bars=10)
    assert legs3k
    assert legs3k[0].fee_usd == pytest.approx(0.47, abs=1e-3)

    b4k = _flat_with_range(4000.0, 50)
    legs4k = run_ta(b4k, sig, sl_mult=1.0, tp_mult=2.0, max_hold_bars=10)
    assert legs4k
    assert legs4k[0].fee_usd == pytest.approx(0.47 * 4000.0 / 3000.0,
                                              rel=1e-6)
    assert legs4k[0].fee_usd > legs3k[0].fee_usd

    set_fee_spot(3000.0)


# ─────────────────────────────────────────────────────────────────
# 6. The metric's own bounds and properties.
# ─────────────────────────────────────────────────────────────────

def test_dac_is_bounded_between_minus_one_and_one():
    """DAC is a weighted average of +-1, so it must be in [-1, +1]."""
    rng = np.random.default_rng(21)
    for _ in range(12):
        closes = 3000.0 + np.cumsum(rng.normal(0, 2, size=300))
        b = _synthetic_1m(closes)
        sig = rng.choice([-1, 0, 1], size=300).astype(np.int8)
        set_fee_spot(3000.0)
        legs = run_ta(b, sig, sl_mult=1.5, tp_mult=3.0, max_hold_bars=60)
        s = score_legs(legs, b, trading_minutes=len(b))
        assert -1.0000001 <= s["dac_path"] <= 1.0000001
        assert -1.0000001 <= s["dac_mfe"] <= 1.0000001


def test_tim_is_bounded_between_zero_and_one():
    """TIM is a fraction of the trading session."""
    b = _synthetic_1m(_random_walk(400, seed=22))
    sig = np.ones(400, dtype=np.int8)
    set_fee_spot(3000.0)
    legs = run_ta(b, sig, sl_mult=1.5, tp_mult=3.0, max_hold_bars=60)
    s = score_legs(legs, b, trading_minutes=len(b))
    assert 0.0 <= s["tim"] <= 1.0


def test_drem_rewards_less_time_in_market():
    """The composite must FALL when the same reward takes longer.

    This is the user's "less time in market" requirement, made
    mechanical: the SAME directional reward, earned over MORE minutes,
    must not improve DREM.

    The fixture is a STEP: the price jumps $10 on one bar and is flat
    otherwise, so both arms capture the SAME $10 — but the fast arm
    books it in 10 minutes and the slow arm in 20. The bar range is
    non-zero (a zero-range bar has a zero ATR, and a stop that cannot
    be sized means no trade at all).
    """
    n = 400
    closes = np.full(n, 3010.0)
    closes[:10] = 3000.0            # the step happens on bar 10
    high = closes + 0.10
    low = closes - 0.10
    b = Bars1m(
        time_ns=(np.arange(n, dtype=np.int64) * NS_PER_MIN),
        open=closes.copy(), high=high, low=low, close=closes.copy(),
        volume=np.ones(n, dtype=np.float64),
        n_ticks=np.ones(n, dtype=np.int64),
        atr=_atr(high, low, closes, 14),
        atr_n=14,
    )
    sig = np.ones(n, dtype=np.int8)
    set_fee_spot(3000.0)

    fast = run_ta(b, sig, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10)
    slow = run_ta(b, sig, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=20)

    s_fast = score_legs(fast, b, trading_minutes=len(b))
    s_slow = score_legs(slow, b, trading_minutes=len(b))

    assert s_fast["n"] == 1 and s_slow["n"] == 1
    # BOTH arms captured the SAME reward...
    assert s_fast["gross_total"] == pytest.approx(s_slow["gross_total"])
    assert s_fast["gross_total"] == pytest.approx(10.0, rel=1e-6)
    # ...but the fast arm held less time...
    assert s_fast["median_hold_min"] < s_slow["median_hold_min"]
    # ...so it must score a HIGHER reward per exposure-minute.
    assert s_slow["drem"] < s_fast["drem"], (
        "DREM must reward LESS time in market: the faster arm scored "
        f"{s_fast['drem']:.4f} and the slower one {s_slow['drem']:.4f}"
    )


def test_drem_rewards_magnitude():
    """A BIGGER directional move must score higher than a smaller one.

    This is the user's "reward magnitude of directional moves"
    requirement. Two rules with the same direction correctness but
    different move sizes must be separated by the metric.
    """
    # A series that rises $1/bar then a series that rises $0.1/bar.
    big = _synthetic_1m(3000.0 + np.arange(300, dtype=np.float64))
    small = _synthetic_1m(3000.0 + np.arange(300, dtype=np.float64) * 0.1)
    sig = np.ones(300, dtype=np.int8)
    set_fee_spot(3000.0)

    s_big = score_legs(
        run_ta(big, sig, sl_mult=1000.0, tp_mult=1000.0, max_hold_bars=10),
        big, trading_minutes=len(big))
    s_small = score_legs(
        run_ta(small, sig, sl_mult=1000.0, tp_mult=1000.0, max_hold_bars=10),
        small, trading_minutes=len(small))

    # Same direction correctness (both +1) but different magnitude.
    assert s_big["dac_path"] == pytest.approx(s_small["dac_path"], abs=1e-9)
    assert s_big["drem"] > s_small["drem"], (
        "DREM must reward the MAGNITUDE of the directional move"
    )


def test_the_scorecard_reports_the_fee_ratio():
    """The fee test must be present — a gross edge that does not cover
    the round trip is not an edge."""
    b = _synthetic_1m(_perfect_up(400))
    sig = np.ones(len(b), dtype=np.int8)
    set_fee_spot(3000.0)
    legs = run_ta(b, sig, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10)
    s = score_legs(legs, b, trading_minutes=len(b))
    assert "gross_per_leg_over_fee" in s
    assert s["fee_per_leg"] == pytest.approx(0.47, abs=1e-3)
    assert s["gross_per_leg_over_fee"] > 1.0


def test_the_drift_excess_decomposition_is_present():
    """The repo's drift/excess identity must ride along.

    A directional score that only measures beta is worthless, so the
    scorecard must carry the passive-hold decomposition.
    """
    b = _synthetic_1m(_perfect_up(400))
    sig = np.ones(len(b), dtype=np.int8)
    set_fee_spot(3000.0)
    legs = run_ta(b, sig, sl_mult=1000.0, tp_mult=1000.0,
                  max_hold_bars=10)
    s = score_legs(legs, b, trading_minutes=len(b))
    assert "drift_total" in s
    assert "excess_total" in s
    assert "t_excess" in s
    # The decomposition is an EXACT identity: gross == drift + excess.
    assert s["gross_total"] == pytest.approx(
        s["drift_total"] + s["excess_total"], rel=1e-9)


def test_the_executor_is_pessimistic_on_an_ambiguous_bar():
    """A bar that touches BOTH the stop and the target books the STOP.

    This is the canonical engine's ``fill_model='pessimistic'`` rule,
    and it is the difference between an honest measurement and a
    flattering one.
    """
    n = 10
    closes = np.full(n, 3000.0)
    high = np.full(n, 3000.0)
    low = np.full(n, 3000.0)
    high[3] = 3010.0     # touches the target
    low[3] = 2990.0      # ...and the stop
    b = Bars1m(
        time_ns=(np.arange(n, dtype=np.int64) * NS_PER_MIN),
        open=closes.copy(), high=high, low=low, close=closes.copy(),
        volume=np.ones(n), n_ticks=np.ones(n, dtype=np.int64),
        atr=_atr(high, low, closes, 14), atr_n=14,
    )
    sig = np.ones(n, dtype=np.int8)
    set_fee_spot(3000.0)

    legs = run_ta(b, sig, sl_mult=1.0, tp_mult=1.0, max_hold_bars=8)
    assert legs
    lg = legs[0]
    assert lg.exit_reason == "sl", (
        "an ambiguous bar must book the STOP under the pessimistic model"
    )
    assert lg.gross_usd < 0.0
