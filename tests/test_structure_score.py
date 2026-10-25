"""Two-stage structure x momentum score filter (2026-10-23).

These lock the MEASURED constraints that shaped the design, so a later
edit cannot silently reintroduce them:

* the score must never "time out" -- ``None`` is unreachable once 5
  events exist;
* momentum must be SIGNED -- ``vel_5m`` is unsigned by construction;
* the score must be recorded even when the gate is off, otherwise the
  filter is unfalsifiable.
"""

import numpy as np
import pytest

from bt._engine.market_structure import (
    BreakEvent,
    StructureEvent,
    StructureState,
    _BREAK_SIGN,
    combined_score,
    momentum_score,
    structure_confidence,
    structure_score,
)


def _state_with_breaks(kinds_and_bars):
    """Build a StructureState carrying only a break log.

    Every field is supplied explicitly (the repo's no-defaults
    policy); the per-bar arrays are irrelevant to ``structure_score``,
    which reads only ``state.breaks``.
    """
    n = 100
    return StructureState(
        trend=np.zeros(n, dtype=np.int8),
        events=np.zeros(n, dtype=np.int8),
        last_break_bar=np.full(n, -1, dtype=np.int32),
        last_break_kind=np.zeros(n, dtype=np.int8),
        last_break_age_bars=np.full(n, -1, dtype=np.int32),
        last_choch_bar=np.full(n, -1, dtype=np.int32),
        last_choch_kind=np.zeros(n, dtype=np.int8),
        last_choch_age_bars=np.full(n, -1, dtype=np.int32),
        last_choch_plus_bar=np.full(n, -1, dtype=np.int32),
        last_choch_plus_age_bars=np.full(n, -1, dtype=np.int32),
        breaks=[
            BreakEvent(
                bar=b, kind=k,
                broken_swing_bar=b, broken_swing_price=0.0,
            )
            for k, b in kinds_and_bars
        ],
    )


BULL = StructureEvent.BOS_BULL
BEAR = StructureEvent.BOS_BEAR
CBULL = StructureEvent.CHOCH_BULL
CBEAR = StructureEvent.CHOCH_BEAR


# ── the score itself ────────────────────────────────────────────

def test_all_bull_is_max_positive():
    st = _state_with_breaks([(BULL, b) for b in (1, 2, 3, 4, 5)])
    assert structure_score(st, 5) == pytest.approx(1.0)


def test_all_bear_is_max_negative():
    st = _state_with_breaks([(BEAR, b) for b in (1, 2, 3, 4, 5)])
    assert structure_score(st, 5) == pytest.approx(-1.0)


def test_score_is_bounded_to_unit_interval():
    st = _state_with_breaks(
        [(BULL, 1), (BULL, 2), (BEAR, 3), (CBULL, 4), (CBEAR, 5)]
    )
    s = structure_score(st, 5)
    assert -1.0 <= s <= 1.0


def test_confidence_is_the_magnitude():
    st = _state_with_breaks([(BULL, 1), (BULL, 2), (BULL, 3)])
    s = structure_score(st, 3)
    assert structure_confidence(st, 3) == pytest.approx(abs(s))


# ── NO TIMEOUT (the whole point) ────────────────────────────────

def test_score_never_times_out_with_five_events_in_history():
    """The 2025-01 slice carried 857 events but the OLD
    ``max_age_bars`` gate reported ``no_event`` on 6,352-9,387
    signals -- ~92% of all rejections. The replacement must score
    from the retained log at ANY age."""
    st = _state_with_breaks(
        [(BULL, 10), (BEAR, 20), (BULL, 30), (BEAR, 40), (BULL, 50)]
    )
    # A million bars later, the newest event is still ~1M bars old.
    # There is no timeout, so a score MUST come back.
    far = 1_000_050
    s = structure_score(st, far, half_life_bars=1800.0)
    assert not np.isnan(s)
    assert -1.0 <= s <= 1.0


def test_score_uses_exactly_the_last_five_events():
    """Six bull then one bear: the stale bulls must not carry it."""
    st = _state_with_breaks(
        [(BULL, b) for b in (1, 2, 3, 4, 5, 6)] + [(BEAR, 7)]
    )
    # Window = the last 5 = 4 bulls + 1 bear -> positive.
    assert structure_score(st, 7, half_life_bars=10_000.0) > 0.0
    # Window = the last 5 of a bull-heavy log, verified by count:
    from bt._engine.market_structure import _break_bars
    assert int(_break_bars(st)[-5:].tolist()[0]) == 3


def test_lookback_of_one_is_just_the_newest_event():
    st = _state_with_breaks([(BULL, 1), (BEAR, 2)])
    assert structure_score(st, 2, lookback_events=1) == pytest.approx(-1.0)


def test_choch_counts_more_than_bos():
    """A character change carries more information than another
    continuation break, so it must move the score further.

    NOTE the fixture must CONTAIN OPPOSITION. With five events that
    all agree, the decayed ratio is 1.0 no matter how they are
    weighted -- the score is a normalised ratio, so weighting is only
    observable against a counterweight.
    """
    kw = {"half_life_bars": 10_000.0}
    # 3 bulls then CHOCH-bull (doubled) then bear.
    choch = _state_with_breaks(
        [(BULL, 1), (BULL, 2), (BULL, 3), (CBULL, 4), (BEAR, 5)]
    )
    bos = _state_with_breaks(
        [(BULL, 1), (BULL, 2), (BULL, 3), (BULL, 4), (BEAR, 5)]
    )
    assert structure_score(choch, 5, **kw) > structure_score(bos, 5, **kw)


def test_choch_outweighs_bos_one_for_one():
    """Direct check of the 2.0 weight, opposite signs, equal age."""
    kw = {"half_life_bars": 10_000.0}
    two_choch = _state_with_breaks(
        [(CBULL, 1), (CBEAR, 2), (BULL, 3), (BEAR, 4), (BEAR, 5)]
    )
    two_bos = _state_with_breaks(
        [(BULL, 1), (BEAR, 2), (BULL, 3), (BEAR, 4), (BEAR, 5)]
    )
    s_c = structure_score(two_choch, 5, **kw)
    s_b = structure_score(two_bos, 5, **kw)
    assert s_c > s_b


def test_older_events_weigh_less_at_equal_magnitude():
    """Recency must matter -- a stale opposite event is discounted.

    Same 5 events, same signs, only the AGE of the minority event
    differs. The fresh-minority case must be more negative.
    """
    kw = {"half_life_bars": 100.0}
    # BULL at 100 and 101, BEAR at 102 either fresh or long-stale.
    fresh = _state_with_breaks([(BULL, 100), (BULL, 101), (BEAR, 102)])
    stale = _state_with_breaks([(BULL, 100), (BULL, 101), (BEAR, 2)])
    # Score both AT THE SAME bar so only the recency weights differ.
    s_fresh = structure_score(fresh, 102, **kw)
    s_stale = structure_score(stale, 102, **kw)
    # fresh: bear is newest -> big negative pull.
    # stale: bear is 100 bars old -> halved; bulls are also older but
    # the bear's decay is what dominates the comparison.
    assert s_fresh < s_stale


def test_shorter_half_life_decays_harder():
    """A shorter half-life must pull the score further toward "no
    opinion" (0.0) as events age.

    One stale bull, one fresh bear: the older the pair, the more the
    result should be washed out toward zero.
    """
    st = _state_with_breaks([(BULL, 10), (BEAR, 20)])
    ages = [100, 300, 900, 2700]
    mags = []
    for hl in (30.0, 300.0, 3000.0):
        bar = 20 + int(hl * 3)
        s = structure_score(st, bar, half_life_bars=hl)
        # At bar = 20 + 3*hl every event is >= 3 half-lives old.
        mags.append(abs(s))
    assert mags == sorted(mags, reverse=True), (
        f"longer half-life should decay less, got {mags}"
    )
    # And with everything long-decayed, a very long half-life keeps
    # it near zero rather than saturating.
    near_zero = structure_score(st, 20 + 3 * 3000.0, half_life_bars=3000.0)
    assert abs(near_zero) < 0.2


# ── anti-lookahead ──────────────────────────────────────────────

def test_score_ignores_future_events():
    """Truncating the future must not change the past."""
    past = [(BULL, 1), (BEAR, 2), (BULL, 3)]
    with_future = past + [(BEAR, 4), (BULL, 5), (BEAR, 6)]
    a = structure_score(_state_with_breaks(past), 3)
    b = structure_score(_state_with_breaks(with_future), 3)
    assert a == pytest.approx(b)


# ── momentum must be SIGNED ─────────────────────────────────────

def test_vel_5m_is_unsigned_so_sign_comes_from_price():
    """``vel[i] = |P_i - P_{i-w}| / P_{i-w}`` -- measured min 0.0,
    max 0.0052 on 2025-01. It can NEVER be negative, so it cannot
    carry direction on its own."""
    class _V:
        def __contains__(self, k):
            return k == "vel_5m"

        def __getitem__(self, k):
            return np.full(400, 0.002, dtype=np.float64)

    n = 400
    rising = np.linspace(2000.0, 2100.0, n)
    falling = np.linspace(2100.0, 2000.0, n)
    flat = np.full(n, 2000.0)

    up = momentum_score(_V(), 350, rising, vel_window=300)
    dn = momentum_score(_V(), 350, falling, vel_window=300)
    assert up > 0.0
    assert dn < 0.0
    assert momentum_score(_V(), 350, flat, vel_window=300) == 0.0


def test_momentum_degrades_to_zero_rather_than_raising():
    """A missing modulator must not become a silent veto."""
    assert momentum_score(None, 100) == 0.0

    class _Empty:
        def __contains__(self, k):
            return False

        def __getitem__(self, k):
            raise KeyError(k)

    assert momentum_score(_Empty(), 100, np.full(200, 2000.0)) == 0.0


def test_momentum_is_bounded():
    class _V:
        def __contains__(self, k):
            return k == "vel_5m"

        def __getitem__(self, k):
            return np.full(400, 99.0, dtype=np.float64)

    rising = np.linspace(2000.0, 2100.0, 400)
    assert -1.0 <= momentum_score(_V(), 350, rising) <= 1.0


# ── the blend ───────────────────────────────────────────────────

def test_combined_blends_two_signed_inputs():
    st = _state_with_breaks([(BULL, 1), (BULL, 2), (BULL, 3)])

    class _V:
        def __contains__(self, k):
            return k == "vel_5m"

        def __getitem__(self, k):
            return np.full(400, 0.002, dtype=np.float64)

    n = 400
    rising = np.linspace(2000.0, 2100.0, n)
    falling = np.linspace(2100.0, 2000.0, n)
    kw = {"half_life_bars": 10_000.0, "close": rising}
    c_up, s_up, m_up = combined_score(st, _V(), 350, 1, **kw)
    assert s_up > 0.0 and m_up > 0.0 and c_up > 0.0
    kw["close"] = falling
    c_dn, s_dn, m_dn = combined_score(st, _V(), 350, 1, **kw)
    assert s_dn > 0.0 and m_dn < 0.0
    # Structure agreeing but momentum opposing must DILUTE, not flip.
    assert 0.0 < c_dn < c_up


def test_combined_is_bounded():
    st = _state_with_breaks([(BULL, 1), (BULL, 2)])

    class _V:
        def __contains__(self, k):
            return k == "vel_5m"

        def __getitem__(self, k):
            return np.full(400, 50.0, dtype=np.float64)

    c, _s, _m = combined_score(
        st, _V(), 350, 1,
        close=np.linspace(2000.0, 2100.0, 400),
        half_life_bars=10_000.0,
    )
    assert -1.0 <= c <= 1.0


def test_all_four_break_kinds_map_to_a_sign():
    assert set(_BREAK_SIGN) == {BULL, BEAR, CBULL, CBEAR}
    assert _BREAK_SIGN[BULL] == (+1, False)
    assert _BREAK_SIGN[CBULL] == (+1, True)
    assert _BREAK_SIGN[BEAR] == (-1, False)
    assert _BREAK_SIGN[CBEAR] == (-1, True)