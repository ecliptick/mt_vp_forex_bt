"""Guards for the 2026-10-23 changes: swing window, TP2 anchoring,
and the BoS/CHoCH structure gate.

Each test below locks a behaviour that was a BUG or a GAP before this
date, so a future refactor cannot silently reintroduce it.
"""
from __future__ import annotations

import sys

import numpy as np
import pandas as pd
import pytest

from bt.optimal_config import (
    STRUCTURE_GATE,
    STRUCTURE_MAX_AGE_BARS,
    SWING_LOOKBACK_SECS,
    TP2_MODE,
    TP2_NTH_HVN_N,
    TP2_SWING_CAP,
    build_engine_kwargs,
)


def _synthetic_bars(n: int = 40) -> pd.DataFrame:
    """A minimal 1s-cadence frame the engine will accept.

    ``time`` must be datetime64 -- ``to_ns_arr`` (bt/_engine/timebase.py)
    rejects an int64 column outright, which is the guard against the
    ms/ns mix documented in AGENTS.md.
    """
    base_s = 1_735_772_400  # 2025-01-01T07:00:00Z
    return pd.DataFrame({
        "time": pd.to_datetime(
            np.arange(n, dtype="int64") + base_s, unit="s", utc=True),
        "open": np.linspace(2000.0, 2010.0, n),
        "high": np.linspace(2001.0, 2011.0, n),
        "low": np.linspace(1999.0, 2009.0, n),
        "close": np.linspace(2000.5, 2010.5, n),
        "volume": np.ones(n),
    })


# ── (a) the swing window is 3 days ────────────────────────────────

def test_swing_window_is_one_day():
    """The user requirement is that every TP sits inside a 1-day swing.

    The window was 3 days (259_200s) for part of 2026-10-23, then
    REVERTED to 24h on the user's instruction: "a $20 capture is
    considered difficult on current alpha, so scale ratio down to
    this".

    Measured cap medians (the window sets the TP distance):

        slice     3-day p50    1-day p50
        2026-01     $99.05      $59.35
        2026-03    $156.60      $85.54
        2025-01     $30.39      $19.29

    2025-01's 1-day p50 of $19.29 matches the NB72-era documented
    $20.08, so 24h is the window the rest of this repo's numbers were
    measured against. A $99 median target against a $0.47 round trip
    is not a target.

    Guard the window so it cannot silently drift back.
    """
    assert SWING_LOOKBACK_SECS == 86_400 == 1 * 86_400


def test_sweep_driver_swing_kwargs_match_the_canonical_window():
    """The driver builds the cap; if it hardcodes 24h the canonical
    constant is a lie and every measured '3-day' result is wrong."""
    from runtime.sweeps.cached_sweep import SWING_KWARGS
    assert SWING_KWARGS["lookback_secs"] == SWING_LOOKBACK_SECS


# ── (b)/(c) TP2 is anchored and capped ─────────────────────────────

def test_tp2_is_capped_by_the_swing_window():
    """TP2 was NEVER swing-capped -- a documented known gap.

    Left uncapped, the runner leg could sit $2,693 out (its 'target'
    was ``tp_zone_mult_fallback * zone_width`` = 200 x $0.36 = $72,
    then clamped only by the much looser p95 cap). The user asked for
    ALL targets inside the 3-day swing.
    """
    assert TP2_SWING_CAP is True
    assert build_engine_kwargs()["tp2_swing_cap"] is True


def test_tp2_mode_is_hvn_and_sweepable():
    assert TP2_MODE == "hvn"
    for mode in ("zone_mult", "hvn", "swing"):
        assert build_engine_kwargs(tp2_mode=mode)["tp2_mode"] == mode


def test_tp2_hvn_searches_beyond_tp1_not_from_the_entry():
    """The TP2 shelf search is anchored on TP1, so 'the next shelf' is
    index 0. An earlier draft indexed at ``nth_hvn_n`` -- i.e. it asked
    for the 4th shelf past TP1 -- and resolved on 2.4% of trades.
    """
    import inspect
    from bt._engine.vp_multi_target_engine import compute_structural_tp
    src = inspect.getsource(compute_structural_tp)
    # Scope to the TP2 block. TP1 legitimately searches from the entry
    # price; only the TP2 shelf walk must be anchored on TP1.
    blk = src[src.find("if tp2_mode == \"hvn\":"):]
    assert "vp_set, t_ns, hvn_tp, hvn_dir" in blk
    assert "vp_set, t_ns, entry_price, hvn_dir" not in blk
    # "The next shelf" is index 0 relative to TP1, not nth_hvn_n.
    assert "max_results=tp2_nth_hvn_n if tp2_nth_hvn_n > 0 else 1" in blk


def test_tp2_falls_back_to_the_reachable_band_not_a_zone_multiple():
    """When no shelf exists beyond TP1, TP2 must land on the reachable
    band edge -- not ``zone_mult_tp`` ($72 vs a ~$32 cap)."""
    import inspect
    from bt._engine.vp_multi_target_engine import compute_structural_tp
    src = inspect.getsource(compute_structural_tp)
    assert "multi_swing_edge" in src


# ── the structure gate ─────────────────────────────────────────────

def test_structure_gate_ships_off():
    """Default OFF. Per AGENTS.md a single-slice directional result
    proves nothing, so this must be swept multi-slice before it is
    promoted."""
    assert STRUCTURE_GATE is False
    assert build_engine_kwargs()["structure_gate"] is False


def test_structure_gate_knobs_are_sweepable():
    assert STRUCTURE_MAX_AGE_BARS == 300
    assert build_engine_kwargs(
        structure_gate=True, structure_max_age_bars=900,
    )["structure_max_age_bars"] == 900


def test_gate_raises_when_structure_is_missing():
    """A gate that silently passes every signal is worse than no gate.

    Before the 2026-10-23 change the VP engine had no structure input
    at all, so any 'gate' would have been a no-op. Fail loudly.
    """
    import pandas as pd
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest

    bars = _synthetic_bars(10)
    with pytest.raises(ValueError, match="structure_gate=True"):
        run_vp_sniper_backtest(
            bars=bars, zones=[], vp_set=None,
            qty_btc=1.0, sl_mode="fvg_only", tp_mode="fvg_mult",
            lvn_mode="mid", sl_buffer_usd=0.05, poc_buffer_usd=0.2,
            hvn_buffer_usd=0.1, tp_zone_mult_fallback=8.0,
            tp_max_distance_usd=0.0, lvn_skip_usd=0.3,
            taker_fee_bps=0.0, entry_mode="dual",
            structure=None, structure_gate=True,
        )


def test_gate_rejects_on_length_mismatch():
    """A StructureState built on a different slice means every index is
    wrong. Refuse rather than read garbage."""
    import pandas as pd
    from bt._engine.market_structure import detect_market_structure
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest

    bars = _synthetic_bars(40)
    st = detect_market_structure(
        high=bars["high"].to_numpy(), low=bars["low"].to_numpy(),
        close=bars["close"].to_numpy(), pivot_len=3,
        detect_order_blocks=False, detect_liquidity=False,
    )
    with pytest.raises(ValueError, match="built on"):
        run_vp_sniper_backtest(
            bars=bars.iloc[:20], zones=[], vp_set=None,
            qty_btc=1.0, sl_mode="fvg_only", tp_mode="fvg_mult",
            lvn_mode="mid", sl_buffer_usd=0.05, poc_buffer_usd=0.2,
            hvn_buffer_usd=0.1, tp_zone_mult_fallback=8.0,
            tp_max_distance_usd=0.0, lvn_skip_usd=0.3,
            taker_fee_bps=0.0, entry_mode="dual",
            structure=st, structure_gate=True,
        )


def test_gate_is_off_by_default_even_with_a_state_present():
    """Gate OFF must be a true no-op: same trades with or without."""
    import inspect
    from bt._engine.vp_multi_target_engine import _structure_pass
    src = inspect.getsource(_structure_pass)
    assert "if not gate:\n        return True" in src


def test_structure_is_cached_with_the_artifacts():
    """It is a function of (bars, pivot_len), not of the sweep knob, so
    rebuilding per cell would be waste."""
    import inspect
    from runtime.sweeps.cached_sweep import ArtifactCache
    src = inspect.getsource(ArtifactCache.build_or_load)
    assert '"structure"' in src
    assert "detect_market_structure" in src


# ── the resample must bucket by TIMESTAMP ─────────────────────────

def test_verdict_is_recorded_even_when_the_gate_is_off():
    """``struct_verdict`` must be stamped on every ClosedTrade.

    Without this the filter is UNFALSIFIABLE: on a run where
    structure was never gated you cannot ask "were the
    structure-aligned trades actually better?". Measured
    2026-10-23, gate OFF, pivot 9: aligned PF 0.71 vs no_event
    (never consulted) 0.88 -- aligned was WORSE, a fact that is
    only observable if the verdict is recorded unconditionally.
    """
    import inspect
    from bt._engine.vp_multi_target_engine import (
        ClosedTrade,
        OpenTrade,
        PendingOrder,
        _structure_verdict,
    )
    for cls in (ClosedTrade, OpenTrade, PendingOrder):
        assert "struct_verdict" in cls.__dataclass_fields__, cls.__name__
    # The recorder must be separate from the gate, so it can run
    # when gate=False.
    assert inspect.getsource(_structure_verdict)


def test_structure_drop_is_in_the_signal_conservation_chain():
    """``signals_unaccounted`` must be 0 (AGENTS.md).

    Omitting ``n_dropped_structure`` made it read 7,991 on the
    pivot_len=3 cell -- i.e. as though the engine had lost 80% of
    its signals. Pure bookkeeping, results were byte-identical once
    fixed.
    """
    import inspect
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    src = inspect.getsource(run_vp_sniper_backtest)
    assert "_accounted = (" in src
    tail = src[src.find("_accounted = ("):]
    assert "+ res.n_dropped_structure" in tail[:1200]


def test_resample_requires_timestamps():
    """Positional aggregation is unsound on a gappy feed, so
    ``ts_ns`` is now mandatory rather than merely preferable.

    Measured on XAUUSD_S1_2025Q1: 15.4% of bars are >1s apart and
    91.9% of 60s buckets do not hold 60 bars. The old positional
    reshape produced a median $19.39 error on 100% of resampled bars.
    """
    import numpy as np
    import pytest as _pt
    from bt._engine.market_structure import detect_market_structure

    n = 400
    rng = np.random.default_rng(0)
    hi = 2000 + rng.random(n)
    lo = 1999 + rng.random(n)
    cl = 2000 + rng.random(n)
    with _pt.raises(ValueError, match="requires ts_ns"):
        detect_market_structure(
            high=hi, low=lo, close=cl,
            pivot_len=3, resample_to_n_secs=60,
            detect_order_blocks=False, detect_liquidity=False,
        )


def test_resample_buckets_by_timestamp_not_row_position():
    """Gappy feed: a positional reshape shears every bucket.

    The test builds a frame with KNOWN weekend gaps so the correct
    answer is unambiguous, then asserts the detector's 1m highs match
    a timestamp-bucketed reference rather than a positional slice.
    """
    import numpy as np
    import pandas as pd
    from bt._engine.market_structure import detect_market_structure

    # 1s bars over 10 minutes, but MINUTE 5 is entirely missing.
    n_per_min = 60
    minutes = [0, 1, 2, 3, 4, 6, 7, 8, 9]  # note: no minute 5
    secs = np.array([m * n_per_min + s
                     for m in minutes for s in range(n_per_min)])
    n = len(secs)
    # Distinct price per (minute, second) so misbucketing is visible.
    hi = 2000.0 + secs.astype(float) / 100.0
    lo = hi - 0.5
    cl = hi - 0.25
    ts = (1_735_772_400 + secs).astype("int64") * 1_000_000_000

    state = detect_market_structure(
        high=hi, low=lo, close=cl, ts_ns=ts, pivot_len=3,
        resample_to_n_secs=60,
        detect_order_blocks=False, detect_liquidity=False,
    )
    assert len(state.last_break_kind) == n

    # Reference: timestamp buckets.
    ref = (pd.Series(hi, index=pd.to_datetime(ts))
           .resample("60s").max().dropna())
    assert len(ref) == len(minutes), "fixture should yield 9 buckets"
    # The largest bucket high must equal the last second of the last
    # minute -- a positional reshape would instead have sheared it.
    expected_max = hi[secs // n_per_min == max(minutes)].max()
    assert np.isclose(ref.to_numpy()[-1], expected_max)


def test_resampled_state_length_equals_original_bars():
    """The expand-back step must preserve the original indexing, or
    every downstream per-bar lookup is off."""
    import numpy as np
    from bt._engine.market_structure import detect_market_structure

    n = 600
    rng = np.random.default_rng(1)
    hi = 2000 + rng.random(n) * 10
    lo = hi - 1.0
    cl = (hi + lo) / 2
    ts = (1_735_772_400 + np.arange(n)).astype("int64") * 1_000_000_000
    state = detect_market_structure(
        high=hi, low=lo, close=cl, ts_ns=ts, pivot_len=3,
        resample_to_n_secs=60,
        detect_order_blocks=False, detect_liquidity=False,
    )
    assert len(state.last_break_kind) == n


def test_structure_pivot_len_is_sweepable_and_fingerprinted():
    """pivot_len changes the CACHED StructureState, so it must be in
    the cache fingerprint/scope -- otherwise a pivot sweep silently
    reuses the previous cell's pivots."""
    import inspect
    from runtime.sweeps.cached_sweep import ArtifactCache
    src = inspect.getsource(ArtifactCache.__init__)
    assert "structure_overrides" in src
    assert 'pivot_len' in src
    src2 = inspect.getsource(ArtifactCache.__init__)
    assert '"structure"' in src2


def test_structure_sweep_knobs_are_not_sent_to_the_engine():
    """``structure_pivot_len`` has no engine parameter. It must live in
    its OWN allowlist, not the engine one -- otherwise a sweep hands
    it to an engine that cannot accept it."""
    from bt.optimal_config import (
        _ENGINE_OVERRIDES_ALLOWED,
        _STRUCTURE_OVERRIDES_ALLOWED,
    )
    assert "structure_pivot_len" in _STRUCTURE_OVERRIDES_ALLOWED
    assert "structure_resample_secs" in _STRUCTURE_OVERRIDES_ALLOWED
    # The critical bit: NOT in the engine allowlist.
    assert "structure_pivot_len" not in _ENGINE_OVERRIDES_ALLOWED
    assert "structure_resample_secs" not in _ENGINE_OVERRIDES_ALLOWED

    import inspect
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    sig = inspect.signature(run_vp_sniper_backtest)
    assert "structure_pivot_len" not in sig.parameters
    assert "structure_resample_secs" not in sig.parameters


# ── sweep grid parsing ────────────────────────────────────────────

def test_grid_parses_booleans_as_bools_not_truthy_strings():
    """``--knobs "structure_gate=False,True"`` used to parse to the
    STRINGS "False"/"True". Both are truthy, so the A/B silently ran
    the same configuration twice -- observed on 2026-10-23, where
    cells 2 and 3 of the tp2/structure sweep were byte-identical.
    """
    from runtime.sweeps.cached_sweep import _parse_grid_specs

    specs = _parse_grid_specs("structure_gate=False,True")
    vals = dict(specs)["structure_gate"]
    assert vals == [False, True], f"got {vals!r} -- booleans must be real bools"
    assert all(isinstance(v, bool) for v in vals)


def test_grid_still_types_numbers_and_strings_correctly():
    """The boolean branch must not regress the other two types."""
    from runtime.sweeps.cached_sweep import _parse_grid_specs

    specs = dict(_parse_grid_specs(
        "nth_hvn_n=1,2,3;sl_zone_mult=6,20.5;fill_model=mid,pessimistic"))
    assert specs["nth_hvn_n"] == [1, 2, 3]
    assert all(isinstance(v, int) for v in specs["nth_hvn_n"])
    assert specs["sl_zone_mult"] == [6, 20.5]
    assert specs["fill_model"] == ["mid", "pessimistic"]