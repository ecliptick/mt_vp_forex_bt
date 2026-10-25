"""Regression tests for the bugs that silently zeroed out the backtest.

Each test here corresponds to a defect that produced plausible-looking
but WRONG output rather than an exception — which is why they slipped
through until the canonical run came back with 264,666 signals and
0 trades.

Covered
-------
1. ``timebase`` — millisecond timestamps vs nanosecond cutoffs. This
   was the root cause of the 0-trade run.
2. Multi-target quantity accounting — the TP2 / SL / EOD paths closed
   a half-size remainder leg at FULL size.
3. ``partial_close_pct`` — hardcoded to 0.5, making the knob dead.
4. ``_separate_tp_legs`` — TP1 and TP2 collapsing onto one price.
5. Dead engine kwargs — anything emitted that the engine cannot accept.
"""
from __future__ import annotations

import inspect

import numpy as np
import pandas as pd
import pytest

from bt._engine.timebase import assert_ns, to_ns_arr, to_ns_int
from bt.optimal_config import FOREX_CONTRACT_SIZE, FOREX_LOTS


# ─────────────────────────────────────────────────────────────────────
# 1. Timebase
# ─────────────────────────────────────────────────────────────────────
def _bars_with_ms_dtype(n: int = 5) -> pd.DataFrame:
    """Bars whose time column has MILLISECOND resolution.

    This is exactly what ``pd.read_parquet`` produces for the canonical
    XAUUSD files, whose arrow schema is ``timestamp[ms, tz=UTC]``.
    """
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    idx = pd.date_range(t0, periods=n, freq="1s")
    return pd.DataFrame({
        "time": idx.astype("datetime64[ms, UTC]"),
        "open": np.arange(n, dtype=float),
        "high": np.arange(n, dtype=float) + 1,
        "low": np.arange(n, dtype=float) - 1,
        "close": np.arange(n, dtype=float) + 0.5,
    })


def test_to_ns_arr_upconverts_millisecond_dtype():
    """A [ms] dtype must NOT yield millisecond integers."""
    bars = _bars_with_ms_dtype()
    assert str(bars["time"].dtype) == "datetime64[ms, UTC]"
    # The naive conversion is milliseconds — this is the bug.
    naive = bars["time"].astype("int64").to_numpy()
    assert naive[0] < 10 ** 13, "fixture should be ms-resolution"
    # The helper upconverts to nanoseconds.
    out = to_ns_arr(bars)
    assert out[0] == naive[0] * 1_000_000
    assert out[0] > 10 ** 18


def test_assert_ns_rejects_millisecond_array():
    ms = _bars_with_ms_dtype()["time"].astype("int64").to_numpy()
    with pytest.raises(ValueError, match="NANOSECONDS"):
        assert_ns(ms)


def test_assert_ns_accepts_nanosecond_array():
    assert_ns(to_ns_arr(_bars_with_ms_dtype()))


def test_to_ns_int_scalar():
    assert to_ns_int("2025-01-01") == int(
        pd.Timestamp("2025-01-01", tz="UTC").value)


def test_p95_cap_warms_up_under_ms_backed_parquet():
    """The 0-trade bug, end to end.

    ``build_rolling_p95_from_bars`` must produce a warmup timestamp in
    the SAME units the engine's ``b_time`` uses. Before ``timebase.py``
    the cap returned a nanosecond warmup while the engine passed
    millisecond timestamps, so ``is_warmup_complete`` was False on
    every bar and ``skip_trades_during_warmup`` discarded every trade.
    """
    from bt._engine.rolling_p95 import build_rolling_p95_from_bars

    # 10 days of 1s bars starting at a round ms epoch.
    n = 10 * 86_400
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    bars = pd.DataFrame({
        "time": pd.date_range(t0, periods=n, freq="1s")
                  .astype("datetime64[ms, UTC]"),
        "high": np.full(n, 2650.0),
        "low": np.full(n, 2640.0),
        "close": np.full(n, 2645.0),
    })
    cap = build_rolling_p95_from_bars(
        bars, lookback_secs=604_800, inner_secs=3_600,
        p95_q=95.0, p95_mult=1.0,
    )
    times = to_ns_arr(bars)
    # Warmup must NOT be complete on the first bar ...
    assert cap.is_warmup_complete(int(times[0])) is False
    # ... but MUST be complete 10 days in, otherwise every trade in a
    # backtest is silently dropped.
    assert cap.is_warmup_complete(int(times[-1])) is True
    # And the cap must return a finite, positive, sane USD value.
    v = cap.cap_for(int(times[-1]))
    assert np.isfinite(v) and 0.0 < v < 100.0


# ─────────────────────────────────────────────────────────────────────
# 2. Multi-target quantity accounting
# ─────────────────────────────────────────────────────────────────────
def test_open_trade_qty_defaults_to_run_level():
    from bt._engine.vp_multi_target_engine import OpenTrade
    ot = OpenTrade(
        trade_id=1, parent_zone_id=1, entry_bar=0, entry_time_ns=0,
        entry_price=100.0, direction=1, stop_price=99.0, target_price=102.0,
        fvg_direction=1, source="sniper", sl_mode="fvg_only",
        tp_mode="multi_target",
    )
    assert ot.qty_of(1.0) == 1.0        # falls back to run-level
    ot.qty = 0.5
    assert ot.qty_of(1.0) == 0.5        # remainder leg carries half


def test_multi_target_qty_uses_per_position_size():
    """Every exit path must scale P&L by THIS position's size.

    Regression for: the TP1 remainder leg was created but never
    recorded its size, so the TP2 / SL / EOD branches closed it at the
    full run-level ``qty_btc`` — roughly doubling reported P&L on
    every TP1 -> TP2 trade.
    """
    from bt._engine.vp_multi_target_engine import OpenTrade
    src = inspect.getsource(
        __import__("bt._engine.vp_multi_target_engine",
                   fromlist=["run_vp_sniper_backtest"]))
    # No exit may size gross P&L off the bare run-level qty_btc.
    offenders = [
        line.strip() for line in src.splitlines()
        if "* ot.direction * qty_btc" in line
        or "0.5 * qty_btc * (" in line
    ]
    assert not offenders, (
        "exit paths still size off run-level qty_btc: "
        f"{offenders}"
    )


def test_tp2_branch_closes_full_size():
    """TP2 reached before TP1 must close the FULL original size.

    Regression for ``qty_used = qty_btc * (1.0 - pct)`` on the
    TP2-before-TP1 path, which silently discarded half the position.
    """
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    src = inspect.getsource(run_vp_sniper_backtest)
    assert "qty_btc * (1.0 - pct)" not in src, (
        "TP2 branch still closes only (1-pct) of the position"
    )


# ─────────────────────────────────────────────────────────────────────
# 2b. TP1 breakeven on the remainder leg
# ─────────────────────────────────────────────────────────────────────
def test_tp1_remainder_leg_carries_a_breakeven_stop():
    """After the TP1 partial close, the remainder's stop must be the
    ENTRY price (breakeven), and it must be a live, enforced stop --
    not a field that is set and then ignored.

    Why this matters: the engine reopens the remainder with
    ``stop_price=entry_price`` and ``partial_close_pct=0.0``. Because
    ``partial_close_pct == 0`` the remainder falls through to the
    SINGLE-TARGET exit path, where ``sl_hit`` is tested against
    ``ot.stop_price``. If the breakeven stop were dropped there, the
    remainder would ride with NO stop at all -- the exact opposite of
    the intent ("move the stop to breakeven, the remainder rides it").

    Measured on 2025-05 (canonical recipe, 52 remainder legs):
    the 11 sl-exited remainders realised a p50 of $0.00 with
    ``sl_distance_usd`` p50 = 0.00 and max = 0.00 -- i.e. the stop
    fired exactly at entry, as intended.

    Re-verified 2026-10-24 across ALL SIX canonical slices with the
    vol gate ON (``range_norm = 3.0``): 12 TP1 legs, 12 remainder
    legs, and ``sl_distance_usd`` == $0.00 with an ABSOLUTE max of
    $0.00 on every sl-exited remainder leg.
    """
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    src = inspect.getsource(run_vp_sniper_backtest)

    # (a) the remainder is created with the breakeven stop ...
    assert "stop_price=entry_price,  # breakeven SL" in src, (
        "the TP1 remainder leg is not reopened at the breakeven stop"
    )
    # ... and is tagged so per-trade diagnostics can identify it.
    assert 'sl_struct_ref="breakeven"' in src, (
        "the TP1 remainder leg is not tagged 'breakeven' -- the SL "
        "move is then invisible in per-trade diagnostics"
    )
    # (b) the remainder must NOT re-arm the multi-target path, or the
    #     TP1 partial could fire twice on the remainder.
    assert "partial_close_pct=0.0," in src, (
        "the TP1 remainder leg still carries a non-zero "
        "partial_close_pct -- it would re-fire the partial close"
    )
    # (c) the single-target path must test the stop against
    #     ``ot.stop_price`` (the moved breakeven), not a stale field.
    assert "sl_hit = b_low <= ot.stop_price" in src, (
        "the long single-target path no longer tests the stop against "
        "ot.stop_price -- the breakeven stop is not enforced"
    )
    assert "sl_hit = b_high >= ot.stop_price" in src, (
        "the short single-target path no longer tests the stop against "
        "ot.stop_price -- the breakeven stop is not enforced"
    )


def test_tp1_remainder_is_not_rescanned_against_the_tp1_bar():
    """The remainder is created on the TP1 bar and must not be
    re-scanned against THAT bar.

    The TP1 branch ends in ``continue``, so the freshly-created
    remainder is not re-tested against the bar that fired TP1. A bar
    that hit TP1 and ALSO traded back through the entry price therefore
    lets the remainder escape a breakeven stop that was already
    breached.

    Measured on 2025-05: 5 of 52 TP1 legs (9.6%) had a bar that both
    reached TP1 and breached breakeven. 4 of the 5 were later stopped
    out at breakeven, 1 went on to hit TP2.

    Re-measured 2026-10-24 across six slices with the vol gate ON:
    1 of 12 TP1 legs (8.3%) -- the same rate.

    This is FAVOURABLE to the strategy (a free ride instead of a
    stop-out), and it is INCONSISTENT with ``fill_model='pessimistic'``,
    whose contract is "a bar that touches both the stop and the target
    resolves to the STOP". It is locked here as DOCUMENTED behaviour,
    not endorsed -- if the inconsistency is ever fixed, this guard must
    be updated in the same commit.
    """
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    src = inspect.getsource(run_vp_sniper_backtest)
    tp1_block = src[src.find("if tp1_hit and not tp2_hit and not sl_hit:"):]
    tp1_block = tp1_block[:tp1_block.find("elif tp2_hit or (sl_hit and not tp1_hit):")]
    assert "_survivor_count += 1\n                    continue" in tp1_block, (
        "the TP1 branch no longer `continue`s -- if it fell through, "
        "the remainder would be resolved on the same bar it was created"
    )


# ─────────────────────────────────────────────────────────────────────
# 3. partial_close_pct reachability
# ─────────────────────────────────────────────────────────────────────
def test_partial_close_pct_reaches_compute_structural_tp():
    """The knob must not be hardcoded to 0.5 in the engine."""
    from bt._engine.vp_multi_target_engine import (
        compute_structural_tp, run_vp_sniper_backtest)

    sig = inspect.signature(compute_structural_tp).parameters
    assert "partial_close_pct" in sig, (
        "compute_structural_tp has no partial_close_pct parameter — "
        "bt.optimal_config.PARTIAL_CLOSE_PCT would be a dead knob"
    )
    src = inspect.getsource(run_vp_sniper_backtest)
    assert "partial_close_pct=partial_close_pct" in src, (
        "run_vp_sniper_backtest does not forward partial_close_pct"
    )


def test_partial_close_pct_in_engine_kwargs():
    from bt.optimal_config import build_engine_kwargs
    ek = build_engine_kwargs()
    assert ek["partial_close_pct"] == pytest.approx(0.50)
    ek2 = build_engine_kwargs(partial_close_pct=0.25)
    assert ek2["partial_close_pct"] == pytest.approx(0.25)


# ─────────────────────────────────────────────────────────────────────
# 4. TP leg separation
# ─────────────────────────────────────────────────────────────────────
def test_separate_tp_legs_pins_collapsed_long():
    from bt._engine.vp_multi_target_engine import _separate_tp_legs
    tp1, tp2, r1, r2 = _separate_tp_legs(
        tp1=100.0, tp2=100.0, tp1_ref="a", tp2_ref="b",
        entry_price=99.0, direction=1)
    assert tp1 < tp2
    assert r1.endswith("+pinned")


def test_separate_tp_legs_pins_collapsed_short():
    from bt._engine.vp_multi_target_engine import _separate_tp_legs
    tp1, tp2, _, _ = _separate_tp_legs(
        tp1=100.0, tp2=100.0, tp1_ref="a", tp2_ref="b",
        entry_price=101.0, direction=-1)
    assert tp1 > tp2


def test_separate_tp_legs_leaves_good_geometry_alone():
    from bt._engine.vp_multi_target_engine import _separate_tp_legs
    tp1, tp2, r1, _ = _separate_tp_legs(
        tp1=100.0, tp2=102.0, tp1_ref="a", tp2_ref="b",
        entry_price=99.0, direction=1)
    assert (tp1, tp2) == (100.0, 102.0)
    assert r1 == "a"


def test_separate_tp_legs_wired_into_engine():
    """TP legs must be separated at FILL time (D2 unified the entry paths).

    Before D2 there were TWO entry paths (immediate + sniper), each
    opening a trade directly, and each needed its own
    ``_separate_tp_legs`` call. D2 (2026-10-22) replaced both with
    a single ``PendingOrder`` queue: a signal PLACES an order, and
    the order FILLS later. There is now exactly ONE place where a
    trade is opened — the fill block — so exactly ONE
    ``_separate_tp_legs`` call.

    The invariant that actually matters: the call must happen at
    FILL time (after the fill), not at signal time (before it), so
    the swing / p95 caps see the regime at entry.
    """
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    src = inspect.getsource(run_vp_sniper_backtest)
    assert src.count("_separate_tp_legs(") == 1, (
        "there is exactly ONE entry path (the fill block) after D2, "
        "so _separate_tp_legs must be called exactly once"
    )
    # The fill block must come AFTER the order-placement block.
    fill_idx = src.find("# ── 2b.")
    sep_idx = src.find("_separate_tp_legs(")
    assert fill_idx != -1, "the 2b fill block must exist"
    assert sep_idx > fill_idx, (
        "_separate_tp_legs must run at FILL time, not at signal time"
    )


# ─────────────────────────────────────────────────────────────────────
# 5. Dead knobs
# ─────────────────────────────────────────────────────────────────────
def test_all_engine_kwargs_reach_the_engine():
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    from bt.optimal_config import build_engine_kwargs

    sig = set(inspect.signature(run_vp_sniper_backtest).parameters)
    dead = {k for k in build_engine_kwargs() if k not in sig}
    assert not dead, f"dead engine kwargs (sweeps would no-op): {sorted(dead)}"


def test_engine_accepts_every_allowlisted_knob():
    """Guard the allowlists against drift from the engine signature."""
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    from bt.optimal_config import _ENGINE_OVERRIDES_ALLOWED

    sig = set(inspect.signature(run_vp_sniper_backtest).parameters)
    unknown = {k for k in _ENGINE_OVERRIDES_ALLOWED if k not in sig}
    assert not unknown, (
        f"allowlist advertises knobs the engine rejects: {sorted(unknown)}"
    )


def _engine_round_trip_cost(spot: float, bps: float, qty: float) -> float:
    """The engine's ACTUAL round-trip fee for a flat-in/flat-out trade.

    Mirrors every exit path in ``bt/_engine/vp_multi_target_engine.py``::

        fee = |exit + entry| * 0.5 * qty * (bps / 10000) * 2.0

    The trailing ``* 2.0`` is the entry+exit factor. Do NOT omit it
    when asserting on fees — that omission is exactly the D1 bug.
    """
    return abs(spot + spot) * 0.5 * qty * (bps / 10000.0) * 2.0


def test_round_trip_fee_is_not_double_charged():
    """D1: the engine must charge the LIVE round trip, not 2x it.

    The engine debits the full entry+exit cost at close time via
    ``fee = avg_price * qty * (bps/10000) * 2.0``. ``_derive_taker_fee_bps``
    therefore has to hand it a PER-SIDE rate. It used to hand over the
    ROUND-TRIP dollar amount instead, so every trade was charged
    $0.94 against a $0.47 spec.

    This is the test that would have caught the double charge.
    """
    from bt.optimal_config import build_engine_kwargs

    oz = FOREX_LOTS * FOREX_CONTRACT_SIZE
    for spot in (2700.0, 3000.0, 4060.0):
        bps = build_engine_kwargs(spot=spot)["taker_fee_bps"]
        charged = _engine_round_trip_cost(spot, bps, oz)
        assert charged == pytest.approx(0.47, abs=1e-6), (
            f"at spot={spot} the engine charged ${charged:.4f}, "
            f"expected the $0.47 live round trip"
        )
        # The old (buggy) implementation produced exactly 2x.
        assert charged != pytest.approx(0.94, abs=1e-4)


def test_taker_fee_bps_is_spot_dependent():
    """taker_fee_bps is a RATE — the $ fee only holds at the right spot.

    ``build_engine_kwargs`` defaults to ``spot=3000.0``. Against a
    corpus trading near $4,060 (the 1y XAUUSD file) that silently
    under-charges fees by ~26%, flattering every sweep.
    """
    from bt.optimal_config import build_engine_kwargs

    at3k = build_engine_kwargs(spot=3000.0)["taker_fee_bps"]
    at4k = build_engine_kwargs(spot=4000.0)["taker_fee_bps"]
    assert at3k > at4k, "higher spot must mean fewer bps for the same $ fee"
    # Round-trip $ must be preserved across spot.
    oz = FOREX_LOTS * FOREX_CONTRACT_SIZE
    cost3k = _engine_round_trip_cost(3000.0, at3k, oz)
    cost4k = _engine_round_trip_cost(4000.0, at4k, oz)
    assert cost3k == pytest.approx(cost4k, rel=1e-9)
    assert cost3k == pytest.approx(0.47, abs=1e-4)


def test_sweep_driver_passes_corpus_spot():
    """The driver must not silently use the 3000.0 default."""
    import inspect

    from runtime.sweeps import cached_sweep
    src = inspect.getsource(cached_sweep.run_cell)
    assert "spot=" in src and "median" in src, (
        "run_cell must derive spot from the bars, not rely on the "
        "3000.0 default"
    )


def test_detector_kwargs_match_the_canonical_recipe():
    """The sweep driver must detect the CANONICAL zones, not its own.

    2026-10-24 (config consolidation): the recipe object
    (``bt._engine.optimal_config.OPTIMAL_PARAMS``) is GONE. The
    canonical values now live as module constants in
    ``bt.optimal_config`` and are materialised by
    ``build_detector_kwargs()``.
    """
    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe
    from bt.optimal_config import (
        DETECTOR_FVG_MIN_ZONE_USD,
        DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS,
        DETECTOR_INVALIDATION_MIN_PIERCE_USD,
        DETECTOR_REQUIRE_RETEST_TO_INVERT,
        DETECTOR_SUPERSEDE_ON_NEW,
    )

    kw = detector_kwargs_from_recipe()
    assert kw["fvg_min_zone_usd"] == pytest.approx(
        float(DETECTOR_FVG_MIN_ZONE_USD))
    assert kw["require_retest_to_invert"] == bool(
        DETECTOR_REQUIRE_RETEST_TO_INVERT)
    assert kw["supersede_on_new"] == bool(DETECTOR_SUPERSEDE_ON_NEW)
    assert kw["invalidation_min_pierce_usd"] == pytest.approx(
        float(DETECTOR_INVALIDATION_MIN_PIERCE_USD))
    assert kw["invalidation_min_consecutive_bars"] == int(
        DETECTOR_INVALIDATION_MIN_CONSECUTIVE_BARS)
    # The 0.10 value that caused 14x over-detection must not return.
    assert kw["fvg_min_zone_usd"] >= 0.30


def test_fvg_detector_runs_on_the_same_cadence_as_every_other_indicator():
    """The FVG detector must run at the recipe's 1m cadence, not 1s.

    2026-10-23. Every other component in the recipe was already on
    1m: the market-structure detector (``STRUCTURE_RESAMPLE_SECS=60``)
    and the volume profile (built from resampled 1m bars). The FVG
    detector was NOT -- ``resample_to_n_secs`` was absent from
    ``detector_kwargs_from_recipe``, so ``detect_fvg`` silently
    defaulted to 0 (= 1s).

    Consequences measured on 2026-01 (1.38M 1s bars):

        cadence   zones   med width   inverted   superseded
        1s        116,743    $0.47          7        99.6%
        1m          4,767    $1.11        130        94.2%

    The 1s detector produced 24x more zones (mostly noise), and only
    0.006% of them ever inverted -- which is why the sniper path was
    structurally empty. At the correct 1m cadence, 2.7% invert.
    """
    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe
    from bt.optimal_config import (
        DETECTOR_RESAMPLE_SECS,
        STRUCTURE_RESAMPLE_SECS,
    )

    kw = detector_kwargs_from_recipe()
    assert kw.get("resample_to_n_secs") == STRUCTURE_RESAMPLE_SECS, (
        "FVG detection cadence must match the structure cadence "
        f"({STRUCTURE_RESAMPLE_SECS}s); got {kw.get('resample_to_n_secs')!r}. "
        "See docs/v2_spec.md 6.9.")
    # And it must be the canonical value, not a coincidence.
    assert kw["resample_to_n_secs"] == int(DETECTOR_RESAMPLE_SECS)


def test_entry_mode_is_sweepable_on_the_engine():
    """The sniper path must be reachable for A/B (2026-10-23).

    ``entry_mode`` was removed from the engine allowlist on 2026-10-21
    because it is an engine kwarg, not a recipe field. That was right
    about ``build_focused_params`` but left it un-sweepable, so the
    sniper-only path had never been measured.
    """
    from bt.optimal_config import build_engine_kwargs

    ek = build_engine_kwargs(entry_mode="sniper")
    assert ek["entry_mode"] == "sniper"


def test_inversion_is_not_structurally_starved():
    """Guard the sniper defect (2026-10-23).

    At the 1s FVG cadence, 99.61% of zones were superseded before
    they could invert and only 7 zones per month inverted (0.006%),
    leaving ``entry_mode='sniper'`` statistically empty.

    The root cause was NOT the zone lifetime -- it was the DETECTION
    CADENCE. A 1s FVG detector sees 60x more bars than the 1m
    cadence every other indicator uses, so it produced 24x more
    (mostly noise) zones, and a new overlapping zone superseded each
    one long before price could commit through it.

    With the detector moved to the recipe's 1m cadence the inversion
    rate recovers on its own: 7 -> 130 inversions per month
    (0.006% -> 2.7%). No supersession rule was changed.

    This test locks the CADENCE, because that is the actual cause.
    """
    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe

    kw = detector_kwargs_from_recipe()
    assert int(kw.get("resample_to_n_secs", 0)) == 60, (
        "the FVG detector must run at 1m; at 1s the supersession "
        "cascade starves the inversion path (7 inversions/month vs "
        "130 at 1m). See docs/v2_spec.md 6.9.")


def test_every_detect_fvg_call_site_passes_times_utc_ns():
    """No ``detect_fvg`` caller may fall back to POSITIONAL bucketing.

    2026-10-24. ``detect_fvg`` buckets the 1s feed into its detection
    cadence (60s). When ``times_utc_ns`` is not supplied it falls back
    to bucketing by ROW POSITION, which assumes a gapless feed. The
    real corpus has weekend/rollover gaps -- 15.018% of the 1y file's
    bars are >1s apart -- so row position drifts away from wall-clock
    time and every bucket boundary after the first gap is wrong.

    This is the same defect class as the NB94 forward-leak
    (``rolling_p95._downsample_to_inner``'s ``reshape(-1, 3600)``).

    Three call sites were affected, and one of them was the DECISIVE
    diagnostic (``runtime/diagnostics/directional_beta.py``) -- the
    test that decides whether the recipe has a real edge. Its verdict
    was being computed on a differently-bucketed zone set than the
    sweep driver produced.

    The detector already warns (``RuntimeWarning``) when it falls back,
    but a warning is not a guard: nothing failed when it fired.

    The rule enforced here is the precise one: a call that ASKS for a
    resample (either explicitly, or by splatting a kwargs dict that
    carries ``resample_to_n_secs``) MUST also pass ``times_utc_ns``.
    A call that never resamples (1s native) is unaffected -- for it
    the positional and timestamp bucketing are identical -- so it is
    not flagged.
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    sources = [
        p for p in repo.rglob("*.py")
        if "tests" not in p.parts
        and ".venv" not in p.parts
        and "__pycache__" not in p.parts
        and p.name != "ict_signals.py"
    ]

    offenders: list[str] = []
    checked = 0
    for path in sources:
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "detect_fvg":
                continue
            checked += 1
            kw = {k.arg for k in node.keywords if k.arg is not None}
            has_splat = any(k.arg is None for k in node.keywords)
            if "times_utc_ns" in kw:
                continue
            # Does this call ask for a resample?
            wants_resample = False
            for k in node.keywords:
                if k.arg == "resample_to_n_secs":
                    wants_resample = True
                elif k.arg is None:
                    # ``**kwargs`` -- the recipe-derived detector kwargs
                    # carry ``resample_to_n_secs`` (60 on the canonical
                    # recipe), so a splat must be treated as a resample.
                    wants_resample = True
            if wants_resample or has_splat:
                offenders.append(
                    f"{path.relative_to(repo)}:{node.lineno}")

    assert checked > 0, "the scan found no detect_fvg call sites at all"
    assert not offenders, (
        "these detect_fvg call sites request a resample but do not pass "
        "times_utc_ns, so they silently use POSITIONAL bucketing (wrong "
        f"on any feed with gaps): {offenders}"
    )


def test_every_diagnostic_states_its_position_size():
    """No diagnostic may report dollars without declaring the size.

    2026-10-24. Fifteen diagnostics in ``runtime/diagnostics/`` ran at
    ``lots=0.01, contract_size=1.0`` -> **0.01 oz**, while the
    canonical MT4 position is ``lots=0.01, contract_size=100.0`` ->
    **1.00 oz**. P&L is exactly linear in qty, so every dollar figure
    in their reports was **100x too small**.

    The scale error did not by itself produce a wrong conclusion, but
    it HID one: the AGENTS.md "gross is positive on all three slices"
    claim was really a $12 diagnostic-scale number, and it survived a
    ``cap_pct`` retune that actually turned 2026-01 gross NEGATIVE.

    The rule enforced here: a diagnostic that calls
    ``run_vp_sniper_backtest_forex`` must either use the canonical
    sizing, or pass an explicit ``contract_size`` -- and the
    canonical constants are the only sanctioned way to do the former.
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    diag_dir = repo / "runtime" / "diagnostics"
    assert diag_dir.is_dir(), "the diagnostics directory moved"

    offenders: list[str] = []
    checked = 0
    for path in sorted(diag_dir.glob("*.py")):
        if path.name == "verify_diag_scale.py":
            # This is the script that MEASURES the ratio. It calls the
            # engine at both scales on purpose.
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "run_vp_sniper_backtest_forex":
                continue
            checked += 1
            kw = {k.arg for k in node.keywords if k.arg is not None}
            has_splat = any(k.arg is None for k in node.keywords)

            cs_node = None
            for k in node.keywords:
                if k.arg == "contract_size":
                    cs_node = k.value
                    break

            if cs_node is None or has_splat:
                offenders.append(
                    f"{path.relative_to(repo)}:{node.lineno}")
                continue

            # A bare NUMERIC LITERAL means the position size is
            # hard-coded at the call site and easy to get wrong by
            # 100x. Require a constant REFERENCE (e.g.
            # ``FOREX_CONTRACT_SIZE``) so the size is
            # self-documenting and cannot silently drift away from
            # the canonical 1.00 oz.
            #
            # The canonical diagnostics use a Name
            # (``FOREX_CONTRACT_SIZE``) or an expression over the
            # canonical constants (``FOREX_LOTS * 100.0``); both are
            # accepted. A literal like ``contract_size=1.0`` is the
            # 0.01 oz bug.
            if isinstance(cs_node, ast.Constant) and isinstance(
                    cs_node.value, (int, float)):
                offenders.append(
                    f"{path.relative_to(repo)}:{node.lineno} "
                    f"(contract_size={cs_node.value!r} is a literal; "
                    f"use FOREX_CONTRACT_SIZE)")
                continue

            if "lots" not in kw:
                offenders.append(
                    f"{path.relative_to(repo)}:{node.lineno} "
                    f"(no explicit lots= keyword)")

    assert checked > 0, "the scan found no diagnostic call sites at all"
    assert not offenders, (
        "these diagnostics call run_vp_sniper_backtest_forex with a "
        "hard-coded position size, so their dollar figures are 100x too "
        "small (0.01 oz instead of the canonical 1.00 oz). Use "
        "lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE: "
        f"{offenders}"
    )


def test_the_leg_ledger_runs_one_continuous_window_not_month_chunks():
    """The ledger must NOT slice the execution into monthly chunks.

    2026-10-25. Monthly chunking was measured to DISCARD ~10% of the
    trade universe (2025 Q1: 126 legs continuous vs 113 chunked),
    because each cold chunk traded with UNCAPPED targets for its
    first ~8 days -- in backtest mode the p95 cap returns ``inf``
    until its 7-day window fills.

    That is a CORRECTNESS bug in the evaluation harness, not a
    measurement preference. The ledger's whole point is that the
    execution window and the reporting window are decoupled: the
    month is a REPORTING dimension computed from the leg
    timestamps, never an execution boundary.

    The guard is on the SOURCE, not on a report: a report can be
    stale, but the script cannot silently revert to chunking.
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    path = repo / "runtime" / "diagnostics" / "leg_ledger.py"
    assert path.exists(), "leg_ledger.py moved"

    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))

    # 1. There must be NO per-month loop. The forbidden shapes are:
    #    - a ``pd.date_range(start, end, freq="MS")`` month iterator
    #    - a ``.resample("M")`` / ``.resample("ME")`` monthly bucket
    #    - a ``strftime("%Y-%m")`` used to SPLIT the bars
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name == "resample":
                freq = None
                if node.args:
                    freq = ast.dump(node.args[0])
                for kw in node.keywords:
                    if kw.arg == "freq":
                        freq = ast.dump(kw.value)
                assert freq is None or not any(
                    tag in freq for tag in ("'M'", '"M"', "'ME'", '"ME"')
                ), (
                    "leg_ledger.py resamples MONTHLY -- that re-introduces "
                    "the month boundary as an execution boundary, which "
                    "discards ~10% of the trade universe."
                )

    # 2. The ledger MUST carry a warmup prefix, and it MUST be
    #    applied by WIDENING the execution window, not by chunking.
    assert "WARMUP_DAYS" in src, "the warmup prefix constant is gone"
    assert "exec_start" in src, (
        "the ledger no longer separates the execution window from the "
        "scored window -- the warmup prefix must WIDEN the execution, "
        "not become a chunk boundary."
    )

    # 3. The default window must be the WHOLE corpus, not one month.
    tree_defs: dict[str, ast.AST] = {}
    for n in tree.body:
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
            if n.value is not None:
                tree_defs[n.target.id] = n.value
        elif isinstance(n, ast.Assign) and len(n.targets) == 1:
            if isinstance(n.targets[0], ast.Name):
                tree_defs[n.targets[0].id] = n.value

    def _const(name: str) -> str | None:
        node = tree_defs.get(name)
        return node.value if isinstance(node, ast.Constant) else None

    assert _const("DEFAULT_START") == "2024-01-01", (
        "the ledger's default window must be the whole corpus, not a "
        f"month; got {_const('DEFAULT_START')!r}"
    )
    assert _const("DEFAULT_END") == "2026-07-16", (
        "the ledger's default window must be the whole corpus, not a "
        f"month; got {_const('DEFAULT_END')!r}"
    )


def test_the_flip_is_never_used_as_a_direction_test():
    """The "flip" is a tautology and must not be sold as a test.

    The engine computes ``gross = (exit - entry) * direction * qty``,
    so negating the direction negates the gross EXACTLY. A flip
    counterfactual is therefore ``-gross`` and is arithmetically
    incapable of carrying information about whether the strategy has
    an edge.

    The direction question is answered by the drift/excess
    decomposition, which depends on the MARKET and is not a
    tautology.
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    path = repo / "runtime" / "diagnostics" / "leg_buckets.py"
    assert path.exists(), "leg_buckets.py moved"

    src = path.read_text(encoding="utf-8")

    # The flip column may EXIST (the ledger documents the trap), but
    # it must be LABELLED as a tautology wherever it is reported.
    assert "flip_gross_total_is_tautology" in src, (
        "the flip column is reported without being labelled a "
        "tautology -- it must never be mistaken for a test."
    )

    # And the real decomposition must be present.
    assert "drift_usd" in src, "the drift decomposition is missing"
    assert "excess_usd" in src, "the excess decomposition is missing"

    # No function may compute a "counterfactual" by negating the
    # gross. That is the tautology in code form.
    tree = ast.parse(src, filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub):
            # ``gross - drift`` is the ALLOWED form.
            left = ast.dump(node.left)
            right = ast.dump(node.right)
            assert "flip" not in left.lower(), (
                "a flip value is being subtracted -- the flip is a "
                "tautology and cannot be a counterfactual."
            )
            assert "flip" not in right.lower(), (
                "a flip value is being subtracted -- the flip is a "
                "tautology and cannot be a counterfactual."
            )


def test_every_diagnostic_sweep_can_actually_disable_its_knob():
    """A sweep cell labelled "off" must really be off.

    2026-10-24. ``runtime/diagnostics/sl_ceiling_sweep.py`` built its
    engine kwargs with::

        ov = {} if cap == 0.0 else {"sl_max_usd": cap}

    The ``cap == 0.0`` case therefore OMITTED the key, so
    ``build_engine_kwargs`` filled in the CANONICAL ``sl_max_usd=2.50``
    and the "ceiling off" row was byte-identical to the ``2.5`` row
    (same legs, gross, net AND SL p50 of $2.50).

    The sweep could not measure "ceiling disabled" at all -- yet the
    row was labelled ``0.0 (off)`` and was quoted as such.

    The rule: a diagnostic sweep that varies a knob must pass it
    EXPLICITLY for EVERY cell, including the "off" cell, because
    omitting the key means "use the canonical default", not "disable".
    The engine's documented disabled sentinel is a value ``<= 0.0``
    (see ``_apply_sl_max_ceiling`` / ``_apply_sl_min_floor``).

    This is the same class of bug as the ``times_utc_ns`` omission
    (positional bucketing) and the position-size omission (0.01 oz):
    **a default silently substituted for an intent.**
    """
    import ast
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    diag_dir = repo / "runtime" / "diagnostics"
    assert diag_dir.is_dir(), "the diagnostics directory moved"

    # Knob -> the canonical value that "omitting the key" would
    # silently substitute. A sweep cell that omits it gets that value
    # instead of the value it asked for.
    KNOB_DEFAULTS = {
        "sl_max_usd": 2.5,
        "sl_min_usd": 2.5,
    }

    offenders: list[str] = []
    checked = 0
    for path in sorted(diag_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            # Look for ``ov = {} if <cond> else {"knob": ...}`` -- the
            # shape that omits the knob on one branch.
            if not isinstance(node.value, ast.IfExp):
                continue
            names: set[str] = set()
            for branch in (node.value.body, node.value.orelse):
                if isinstance(branch, ast.Dict):
                    for k in branch.keys:
                        if isinstance(k, ast.Constant) and isinstance(
                                k.value, str):
                            names.add(k.value)
            if not names:
                continue
            # The "else" branch names the knob; the "body" branch does
            # not -> the body branch OMITS it.
            else_names = {
                k.value for k in node.value.orelse.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            } if isinstance(node.value.orelse, ast.Dict) else set()
            body_names = {
                k.value for k in node.value.body.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            } if isinstance(node.value.body, ast.Dict) else set()
            omitted = else_names - body_names
            relevant = omitted & set(KNOB_DEFAULTS)
            if relevant:
                checked += 1
                offenders.append(
                    f"{path.relative_to(repo)}:{node.lineno} "
                    f"omits {sorted(relevant)} on one branch -- the "
                    f"engine then silently uses the canonical "
                    f"{ {k: KNOB_DEFAULTS[k] for k in relevant} } "
                    f"instead of the value the cell asked for"
                )

    # A sweep that conditionally omits a knob is the shape this guard
    # exists to catch. If NO diagnostic uses that shape any more, the
    # guard is still valid (it will catch a reintroduction) -- but we
    # must be sure the scan can actually SEE the pattern, so the test
    # asserts the scan is wired up, not that a bug exists.
    scanned = 0
    for path in sorted(diag_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"),
                         filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign) and isinstance(
                    node.value, ast.IfExp):
                scanned += 1

    assert scanned > 0, (
        "the scan found no conditional-override patterns at all -- the "
        "guard has gone stale and no longer checks anything"
    )
    assert not offenders, (
        "these diagnostics omit a swept knob on one branch, so that "
        "branch silently measures the CANONICAL value instead of the "
        "requested one (an 'off' cell that is not off): "
        f"{offenders}"
    )


# ─────────────────────────────────────────────────────────────────────
# 6. Metrics layer
# ─────────────────────────────────────────────────────────────────────
def test_compute_metrics_reads_real_trade_fields():
    """Metrics must come from result.trades, not invented attributes."""
    from runtime.sweeps.cached_sweep import compute_metrics
    from bt._engine.vp_multi_target_engine import ClosedTrade

    tr = ClosedTrade(
        trade_id=1, parent_zone_id=1, entry_bar=0, exit_bar=10,
        entry_time_ns=0, exit_time_ns=int(3600e9),
        entry_price=100.0, exit_price=101.0, direction=1,
        stop_price=99.0, target_price=103.0, exit_reason="tp",
        hold_secs=3600.0, pnl_gross_usd=1.0, fee_usd=0.5,
        pnl_net_usd=0.5, qty_btc=1.0, fvg_direction=1, source="sniper",
        sl_mode="fvg_only", tp_mode="multi_target",
        sl_struct_ref="zone_edge", tp_struct_ref="hvn",
    )
    res = type("R", (), {"trades": [tr], "equity_curve": np.zeros(0)})()
    m = compute_metrics(res)
    assert m["n_trades"] == 1
    assert m["total_pnl_net_usd"] == pytest.approx(0.5)
    assert m["total_fees_usd"] == pytest.approx(0.5)
    assert m["ev_per_trade_usd"] == pytest.approx(0.5)
    assert m["hit_rate"] == pytest.approx(1.0)


def test_compute_metrics_handles_empty_result():
    from runtime.sweeps.cached_sweep import compute_metrics
    res = type("R", (), {"trades": [], "equity_curve": np.zeros(0)})()
    m = compute_metrics(res)
    assert m["n_trades"] == 0
    assert m["total_pnl_net_usd"] == 0.0
    assert m["max_drawdown_usd"] == 0.0


# ─────────────────────────────────────────────────────────────────────
# 7. Swing cap (NB72) — the forward-leak that made 1y differ from 1 week
# ─────────────────────────────────────────────────────────────────────
def _swing_cap_fixture() -> pd.DataFrame:
    """Synthetic bars with a KNOWN trailing 24h range at every point.

    Price rises linearly from 2000 to 3000 over 10 days at exactly
    1 bar/second, so the trailing-24h range is constant and easy to
    assert on. A 1h bar's range is therefore also exactly 1.0.
    """
    n = 10 * 86_400
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    ramp = np.linspace(2000.0, 3000.0, n)
    return pd.DataFrame({
        "time": pd.date_range(t0, periods=n, freq="1s")
                  .astype("datetime64[ms, UTC]"),
        "open": ramp,
        "high": ramp,
        "low": ramp,
        "close": ramp,
        "volume": np.ones(n),
    })


def test_swing_cap_does_not_leak_future_bars():
    """``cap_for`` must be bounded ABOVE by the query timestamp too.

    The original mask was ``ts >= cutoff`` with no upper bound, so the
    "24h window" ran from ``t - 24h`` to the END OF THE BUFFER. On a
    multi-month file that is months of future data: measured on
    XAUUSD_S1_1y, ``cap_for`` at the file midpoint returned $1,733.62
    where the true trailing 24h cap was $92.66 (18.7x inflated). The
    inflated cap pushed every TP out of reach, so nothing ever closed.

    This is exactly why a 1-week file looked fine — there, the forward
    leak is bounded by the file edges and the error stays small.
    """
    from bt._engine.swing_cap import build_daily_swing

    bars = _swing_cap_fixture()
    cap = build_daily_swing(bars, lookback_secs=86_400, cap_pct=0.7)
    times = to_ns_arr(bars)

    # Query at several points; a leak inflates the cap as the file
    # gets longer, so the mid-file probe is the sharpest test.
    for frac in (0.05, 0.3, 0.5, 0.8, 0.95):
        i = int(len(bars) * frac)
        got = cap.cap_for(int(times[i]))

        # Ground truth: trailing 24h only (the cap is a trailing window).
        w = bars.iloc[max(0, i - 86_400):i]
        want = (float(w["high"].max()) - float(w["low"].min())) * 0.7

        # Sampling is at ~24min cadence, so the cap can only ever be
        # SMALLER than the true range. Anything ABOVE it is a leak.
        assert got <= want * 1.05, (
            f"swing cap leaked future data at frac={frac}: "
            f"got {got:.2f} > true trailing-24h cap {want:.2f}"
        )


def test_swing_cap_is_stable_across_file_length():
    """A longer file must not inflate the cap at a fixed probe time.

    This is the 1-week-works / 1-year-doesn't signature: with the
    one-sided mask, the same query returns a progressively larger cap
    as more future data is appended after it.
    """
    from bt._engine.swing_cap import build_daily_swing

    bars = _swing_cap_fixture()
    probe_idx = int(len(bars) * 0.2)
    t_probe = int(to_ns_arr(bars)[probe_idx])

    caps = []
    for frac in (0.3, 0.5, 0.7, 1.0):
        sub = bars.iloc[:int(len(bars) * frac)].reset_index(drop=True)
        cap = build_daily_swing(sub, lookback_secs=86_400, cap_pct=0.7)
        caps.append(cap.cap_for(t_probe))

    # Every truncation must give the SAME answer at the same timestamp.
    assert max(caps) - min(caps) < 1e-6, (
        f"cap depends on how much FUTURE data follows the query: {caps}"
    )


# ─────────────────────────────────────────────────────────────────────
# 8. Rolling p95 (NB91) — downsampling must respect wall-clock time
# ─────────────────────────────────────────────────────────────────────
def test_p95_downsampler_respects_wall_clock_gaps():
    """1h buckets must be 1h of REAL time, not 3600 consecutive rows.

    The old positional reshape assumed a gapless feed. XAUUSD has
    weekend gaps: 15.018% of bars on the 1y file are separated by
    more than 1s, and 100% of the resulting buckets spanned other than
    exactly 1h (mean drift -5,725h).
    """
    from bt._engine.rolling_p95 import _downsample_to_inner

    # 3 hours of bars, then a 2-day gap, then 3 more hours.
    seg = 3 * 3600
    times = np.concatenate([
        np.arange(seg, dtype=np.int64) * 1_000_000_000,
        np.arange(seg, dtype=np.int64) * 1_000_000_000
        + int(3 * 3600e9 + 2 * 86_400e9),
    ])
    n = len(times)
    ramp = np.linspace(100.0, 200.0, n)

    bucket_t, bucket_h, _ = _downsample_to_inner(
        ramp, ramp, times, inner_secs=3600, first_t_ns=int(times[0]))

    # Six populated hours, not three sheared mega-buckets.
    assert len(bucket_t) == 6, (
        f"expected 6 wall-clock hour buckets, got {len(bucket_t)}"
    )
    # Every bucket timestamp is a multiple of the hour boundary.
    assert np.all(bucket_t % (3600 * 10 ** 9) == 0)
    # Timestamps are strictly increasing with real time.
    assert np.all(np.diff(bucket_t) > 0)
    # No bucket may span the gap: consecutive buckets differ by more
    # than 1h exactly where the gap is.
    deltas = np.diff(bucket_t)
    assert deltas.max() > 3600 * 10 ** 9, "gap was not represented"


def test_p95_cap_tracks_regime_across_a_long_file():
    """``cap_for`` must not saturate on long files.

    With the reshape, ``buffer_t_ns`` ran out of sync with the data and
    ended ~April 2025 while the bars ran to July 2026, so every lookup
    in the last 15 months returned the same stale value. This test
    asserts the cap VARIES across a long synthetic file.
    """
    from bt._engine.rolling_p95 import (
        build_rolling_p95_from_bars, STAT_P2T_HOURLY, STAT_P2T_WINDOW,
    )

    # 60 days of 1m bars (cheap but long enough to span regimes).
    n = 60 * 1440
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    # Two regimes: quiet first, volatile second.
    seg = n // 2
    rng = np.linspace(2000.0, 2100.0, n)
    rng[:seg] += np.sin(np.arange(seg) / 60.0) * 0.05   # ±$0.05
    rng[seg:] += np.sin(np.arange(n - seg) / 60.0) * 5.0  # ±$5.00
    bars = pd.DataFrame({
        "time": pd.date_range(t0, periods=n, freq="1min")
                  .astype("datetime64[ms, UTC]"),
        "high": rng + 0.5,
        "low": rng - 0.5,
        "close": rng,
    })
    cap = build_rolling_p95_from_bars(
        bars, lookback_secs=604_800, inner_secs=3600,
        p95_q=95.0, p95_mult=1.0, statistic=STAT_P2T_WINDOW,
    )
    times = to_ns_arr(bars)

    quiet = cap.cap_for(int(times[int(n * 0.45)]))
    volatile = cap.cap_for(int(times[int(n * 0.95)]))
    assert np.isfinite(quiet) and np.isfinite(volatile)

    # INVARIANT: the cap must RISE after a volatility regime shift.
    #
    # The old assertion was ``volatile > quiet * 2.0``, calibrated
    # against the legacy HOURLY statistic. That statistic forgets a
    # quiet regime within hours, so it adapted 5.05x within 6 days of
    # the flip. The true window peak-to-trough cannot do that BY
    # CONSTRUCTION: a quiet stretch stays inside the trailing 7-day
    # window until it physically expires, so the cap necessarily
    # climbs more slowly (measured 1.79x on this fixture).
    #
    # Demanding 2.0x therefore tested the legacy statistic's memory
    # constant, not the anti-saturation property this test exists to
    # protect. A saturated cap would be FLAT (ratio 1.0) and would
    # return one repeated value for every timestamp. So the real
    # invariants are: it rises at all, and it keeps returning
    # DISTINCT values across the file.
    assert volatile > quiet * 1.25, (
        f"cap failed to track the regime shift: "
        f"quiet={quiet:.2f} volatile={volatile:.2f}"
    )

    # Anti-saturation: the cap must not be stuck on one stale value.
    probe = np.linspace(0.30, 0.95, 40)
    vals = np.array([cap.cap_for(int(times[int(n * f)])) for f in probe])
    vals = vals[np.isfinite(vals)]
    assert vals.size >= 30, f"too few finite caps: {vals.size}"
    assert len(np.unique(np.round(vals, 6))) >= 10, (
        f"cap is saturating -- only "
        f"{len(np.unique(np.round(vals, 6)))} distinct values over "
        f"{vals.size} probes"
    )

    # And the legacy statistic must still be selectable + track it
    # FASTER, which is the whole point of keeping it around.
    legacy = build_rolling_p95_from_bars(
        bars, lookback_secs=604_800, inner_secs=3600,
        p95_q=95.0, p95_mult=1.0, statistic=STAT_P2T_HOURLY,
    )
    lq = legacy.cap_for(int(times[int(n * 0.45)]))
    lv = legacy.cap_for(int(times[int(n * 0.95)]))
    assert lv > lq * 2.0, (
        f"legacy hourly statistic lost its fast adaptation: "
        f"quiet={lq:.2f} volatile={lv:.2f}"
    )


# ─────────────────────────────────────────────────────────────────────
# 9. The p95 cap is a TP-ONLY clamp — it must never gate entries
# ─────────────────────────────────────────────────────────────────────
def test_p95_cap_never_gates_entries_in_backtest_mode():
    """A TP normalizer must not decide whether a trade is allowed.

    The NB91 warmup gate skipped entries outright while the cap was
    warming up. That couples a TP-distance normalizer to entry
    admission, which is a design error: on the 1y file it meant the
    first 8 days of the corpus produced no trades at all, and it
    made a *cap* bug (the forward leak) look like a *gate* bug.

    In BACKTEST mode there is no seed constant, so ``cap_for``
    returns ``inf`` during warmup and the TP is simply uncapped.
    The trade must still be taken.
    """
    from bt._engine.rolling_p95 import build_rolling_p95_from_bars

    n = 12 * 86_400
    t0 = pd.Timestamp("2025-01-01", tz="UTC")
    bars = pd.DataFrame({
        "time": pd.date_range(t0, periods=n, freq="1s")
                  .astype("datetime64[ms, UTC]"),
        "high": np.full(n, 2650.0),
        "low": np.full(n, 2640.0),
        "close": np.full(n, 2645.0),
    })
    cap = build_rolling_p95_from_bars(
        bars, lookback_secs=604_800, inner_secs=3_600,
        p95_q=95.0, p95_mult=1.0,
        seed_path="constant",
        seed_constant_usd=0.0,      # backtest mode: NO seed
    )
    cap.skip_trades_during_warmup = True
    times = to_ns_arr(bars)

    # During warmup the cap must be INACTIVE (not a rejection).
    assert cap.is_warmup_complete(int(times[0])) is False
    assert cap.cap_for(int(times[0])) == float("inf"), (
        "backtest mode must leave the TP uncapped during warmup, "
        "not treat the missing cap as a reason to skip the trade"
    )

    # The live coldstart path DOES have a seed constant, and there the
    # warmup skip is meaningful — the cap is usable immediately.
    live = build_rolling_p95_from_bars(
        bars, lookback_secs=604_800, inner_secs=3_600,
        p95_q=95.0, p95_mult=1.0,
        seed_path="constant",
        seed_constant_usd=8.0,      # live VPS constant
    )
    assert live.cap_for(int(times[0])) == pytest.approx(8.0)


def test_p95_warmup_gate_is_not_active_without_a_seed():
    """Engine-level guard: the warmup entry-skip requires a seed.

    The engine's entry path must not skip a trade during the p95
    warmup window unless a seed constant makes a usable cap
    available (the live coldstart case). In backtest mode the
    condition is unreachable, so the counter must stay at zero.
    """
    import inspect
    from bt._engine import vp_multi_target_engine as eng

    src = inspect.getsource(eng.run_vp_sniper_backtest)
    # Every warmup skip must be guarded by the seed check.
    assert src.count("p95_cap.seed_constant_usd > 0.0") == 2, (
        "both entry paths (immediate + sniper) must require a "
        "non-zero seed before skipping a trade during p95 warmup"
    )
    # And the skip must never be unconditional on the cap alone.
    assert "and not p95_cap.is_warmup_complete(b_time)" in src


# ─────────────────────────────────────────────────────────────────────
# 7. HVN shelf-search radius vs the TP caps (2026-10-22)
#
# The radius used to be a frozen literal (``tp_max_distance_usd``,
# 6.0 -> 50.0 -> 13.0) copied from the MEASURED MEDIAN of the caps on
# ONE slice, while the caps themselves were read live. Two consequences,
# both silent:
#
#   1. A radius wider than the reachable band finds shelves that the
#      cap then clamps back onto the ceiling, collapsing TP1 onto TP2.
#   2. ``compute_structural_sl`` fell back to the engine module
#      constant ``TP_MAX_DISTANCE_USD = 1500.0`` (BTC-scale) when
#      ``sl_hvn_max_distance_usd <= 0`` — which is canonical.
#
# The radius is now DERIVED from the same caps that clamp the leg, so
# the two cannot disagree.
# ─────────────────────────────────────────────────────────────────────
def test_hvn_search_radius_is_derived_not_hardcoded():
    """No absolute-USD radius may be baked into the engine's source.

    Parsed with ``ast`` rather than substring-matched, so a mention in a
    comment or docstring (this file has several) cannot mask a real
    reference.
    """
    import ast
    from bt._engine import vp_multi_target_engine as eng
    from bt._engine import vp_multi_target_config as cfg

    def _names_referencing(tree, name):
        return [
            n for n in ast.walk(tree)
            if isinstance(n, ast.Name) and n.id == name
        ]

    for mod in (eng, cfg):
        tree = ast.parse(inspect.getsource(mod))
        assert not _names_referencing(tree, "TP_MAX_DISTANCE_USD"), (
            f"{mod.__name__} still reads a TP_MAX_DISTANCE_USD module "
            "constant. The radius must be derived per bar."
        )


def test_canonical_radius_knob_requests_derivation():
    """``0.0`` = derive. A non-zero canonical value re-freezes a regime."""
    from bt.optimal_config import (
        SL_HVN_MAX_DISTANCE_USD,
        TP_MAX_DISTANCE_USD,
        build_engine_kwargs,
    )

    assert TP_MAX_DISTANCE_USD == 0.0, (
        "TP_MAX_DISTANCE_USD must stay 0.0 (derive from the caps). "
        "A positive value re-introduces a frozen regime constant."
    )
    assert SL_HVN_MAX_DISTANCE_USD == 0.0, (
        "SL_HVN_MAX_DISTANCE_USD must stay 0.0 (derive from the caps)."
    )

    ek = build_engine_kwargs()
    assert ek["tp_max_distance_usd"] == 0.0
    assert ek["sl_hvn_max_distance_usd"] == 0.0


def test_reachable_band_is_the_tighter_live_cap():
    """The derived radius must equal ``min`` of the ACTIVE caps.

    If this ever exceeds a live cap, far shelves get found and then
    clamped back onto the ceiling — the TP1 == TP2 collapse that
    ``_separate_tp_legs`` was papering over with a $0.01 pin.
    """
    from bt._engine.vp_multi_target_engine import (
        _p95_cap_usd,
        _reachable_tp_band_usd,
    )

    T_NS = 1_700_000_000_000_000_000

    class _P95:
        enabled = True
        p95_mult = 1.0

        def cap_for(self, t_ns):
            return 13.33

    class _Swing:
        enabled = True

        def cap_for(self, t_ns):
            return 20.08

    # Both active -> the tighter one binds.
    assert _reachable_tp_band_usd(
        p95_cap=_P95(), swing_cap=_Swing(), t_ns=T_NS, tp_p95_mult=1.0,
    ) == pytest.approx(13.33)

    # Only the swing cap active -> it is the whole band.
    assert _reachable_tp_band_usd(
        p95_cap=None, swing_cap=_Swing(), t_ns=T_NS, tp_p95_mult=1.0,
    ) == pytest.approx(20.08)

    # Neither active -> unbounded, NOT a silent hardcoded default.
    assert _reachable_tp_band_usd(
        p95_cap=None, swing_cap=None, t_ns=T_NS, tp_p95_mult=1.0,
    ) == float("inf")

    # Explicitly disabled p95 is treated as inactive.
    assert _p95_cap_usd(_P95(), T_NS, 0.0) is None


def test_tp_hvn_proxy_frac_is_a_real_knob():
    """The no-shelf fallback fraction must be a parameter, not a literal.

    It was a bare ``0.7`` inside the ``multi_hvn_proxy`` branch, which
    fired on a large share of legs — a magic constant in arithmetic
    that no sweep could reach. Asserted on the AST *body* (the ``0.7``
    keyword DEFAULT in the signature is legitimate).
    """
    import ast
    from bt._engine.vp_multi_target_engine import compute_structural_tp
    from bt.optimal_config import TP_HVN_PROXY_FRAC, build_engine_kwargs

    assert "tp_hvn_proxy_frac" in inspect.signature(
        compute_structural_tp).parameters

    fn = next(
        n for n in ast.parse(inspect.getsource(compute_structural_tp)).body
        if isinstance(n, ast.FunctionDef)
    )
    body_literals = [
        c.value for stmt in fn.body for c in ast.walk(stmt)
        if isinstance(c, ast.Constant) and isinstance(c.value, float)
    ]
    assert 0.7 not in body_literals, (
        "the 0.7 literal must not remain in the compute_structural_tp body"
    )

    ek = build_engine_kwargs(tp_hvn_proxy_frac=0.5)
    assert ek["tp_hvn_proxy_frac"] == pytest.approx(0.5)
    # Canonical is unchanged, so behaviour is bit-identical by default.
    assert build_engine_kwargs()["tp_hvn_proxy_frac"] == pytest.approx(
        TP_HVN_PROXY_FRAC)


def test_vol_gate_range_norm_is_promoted_to_1_for_the_trade_count_target():
    """The vol gate is the one knob whose gain is NOT "trade less".

    2026-10-24. Measured on 15 monthly slices at canonical MT4 sizing
    (``runtime/diagnostics/vol_gate_broad.py``):

        thr   legs    gross       fees       net   gross/leg  g/leg/fee
        0.0  50,205    $1,830   $23,596  -$21,358     $0.036     0.078
        3.0   1,231      $205      $579     -$364     $0.166     0.354

    Gross-per-leg-over-fee rises 4.6x while the fee bill falls 97.5%
    and GROSS RISES. That distinguishes it from the ``sl_zone_mult``
    mechanism AGENTS.md warns about (trade less, pay fewer fees, gross
    flat), which is why it is promoted as a signal-quality fix.

    ── RE-PAIRED 2026-10-25: 3.0 -> 1.0 (TRADE-COUNT TARGET) ──────

    The 3.0 threshold buys that quality by admitting almost nothing:
    on the continuous corpus it produced **1.83 trades per calendar
    day** (1,692 legs over 927 days) — one trade every ~13 hours.

    The target is **30-70 trades a day**. Measured on the continuous
    corpus (2024-01-01 -> 2026-07-16, 14-day warmup prefix, one
    continuous execution; ``runtime/diagnostics/
    volgate_trade_count.py``), with the 2026-10-25 SL/contra
    settings:

        thr   legs   /cal-day  /sig-day      gross        fees         net    g/leg
        0.0  100008    107.88    126.43   $9,612.27  $34,233.51  -$24,621.24  0.0961
        0.5   95338    102.85    120.68   $8,197.54  $32,995.30  -$24,797.76  0.0860
       *1.0   51075     55.10     64.65   $7,312.96  $19,758.46  -$12,445.50  0.1432
        1.25  30923     33.36     39.54   $5,284.96  $13,017.27   -$7,732.31  0.1709
        1.5   18981     20.48     24.59   $5,267.46   $8,448.18   -$3,180.72  0.2775
        2.0    7373      7.95      9.74   $1,647.84   $3,536.14   -$1,888.31  0.2235

    **1.0 is the only threshold that lands MID-BAND** — 55.10 per
    calendar day, 64.65 per trading day. 1.25 is in band but at the
    bottom edge; 1.5 falls out; 0.0/0.5 overshoot at 100+.

    The gate still does its quality job: $0.1432 gross/leg is 1.49x
    the gate-off book's $0.0961.

    ⚠️ THE NET IS NEGATIVE AT EVERY THRESHOLD. The wider book is a
    much bigger fee sink, and $0.1432 gross/leg is BELOW the $0.5029
    measured round trip. The count target and the edge are in direct
    tension and the count target WINS, because it was asked for.
    """
    from bt.optimal_config import VOL_STATE_MIN_RANGE_NORM, build_engine_kwargs

    assert VOL_STATE_MIN_RANGE_NORM == pytest.approx(1.0), (
        "the vol gate threshold changed -- re-run "
        "runtime.diagnostics.volgate_trade_count (the threshold is "
        "PAIRED with the SL ceiling; do not tune it in isolation)"
    )
    ek = build_engine_kwargs()
    assert ek["vol_state_min_range_norm"] == pytest.approx(1.0)
    # It must be a real, sweepable knob on the engine surface.
    assert build_engine_kwargs(
        vol_state_min_range_norm=1.5
    )["vol_state_min_range_norm"] == pytest.approx(1.5)


def test_vol_gate_measure_is_built_by_the_drivers():
    """A promoted gate is DEAD if the driver never builds the measure.

    ``_vol_state_pass`` returns True when ``vol_state`` is None, so a
    driver that omits ``range_norm_1m`` from the built measures would
    silently ship a no-op gate -- the exact class of defect this repo
    calls out in "Dead knobs". Both artifact builders must request it.
    """
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    for rel in ("runtime/sweeps/cached_sweep.py",
                "runtime/diagnostics/directional_beta.py"):
        src = (repo / rel).read_text(encoding="utf-8")
        assert "range_norm_1m" in src, (
            f"{rel} does not build the range_norm_1m measure -- the "
            f"promoted vol gate would silently pass every signal"
        )


def test_vol_gate_is_live_in_the_engine():
    """The gate must reject a bar whose range_norm is below the floor."""
    from bt._engine.vp_multi_target_engine import _vol_state_pass

    vol = {"range_norm_1m": np.array([0.5, 1.5, 2.9, 3.0, 3.1])}
    # Threshold 3.0 -> admit only the bars at/above 3.0.
    admits = [_vol_state_pass(i, vol, 0.0, 3.0, 0.0, 0.0, 0.0, 0.0)
              for i in range(len(vol["range_norm_1m"]))]
    assert admits == [False, False, False, True, True]

    # With the gate disabled (threshold 0.0) everything is admitted.
    admits_off = [_vol_state_pass(i, vol, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
                  for i in range(len(vol["range_norm_1m"]))]
    assert all(admits_off)


def test_sl_ceiling_is_promoted_to_8_and_is_paired_with_the_vol_gate():
    """The SL ceiling is $8.00 and it is PAIRED with the vol gate.

    HISTORY (2026-10-24) -- the ceiling was $2.50, NOT $5.00, and the
    joint screen (``runtime/diagnostics/combined_screen.py``, 6
    slices, canonical scale) measured the ceiling CONDITIONAL on the
    gate, and the optimum INVERTS:

        vol gate | best ceiling | net      | gross  | flat net
          0.0    |    $5.00     | -$5,345 | $4,264 | -$1,345
          1.5    |    $5.00     | -$1,264 | $1,091 |   -$136
          2.0    |    $2.50     |   -$582 |   $394 |    +$44
          3.0    |    $2.50     |   +$69 |   $309 |    +$62

    With the gate at 3.0, $2.50 beat $5.00 on every metric.

    ── RE-PAIRED 2026-10-25: ceiling $2.50 -> $8.00, gate 3.0 -> 1.0 ──

    Both halves of the pair changed together. The $2.50 ceiling was
    pinning EVERY stop (1 distinct stop distance across the book), so
    the HVN SL anchor was inert and the geometry was a fixed 1:$8 bet
    with no structural content. Re-opening the band to $8.00 makes
    the anchor live again.

    MEASURED on the continuous corpus (2024-01-01 -> 2026-07-16,
    14-day warmup prefix, one continuous execution;
    ``runtime/diagnostics/config_iso_2026_10_25.py``):

        cell                       legs  /day   gross     fees      net   g/leg
        base_canonical             1692   1.83   $510.58   $850.92  -$340.34  0.3018
        sl_max_8                   1705   1.84   $905.43   $850.94   +$54.49  0.5310
        contra_5                   1696   1.83   $582.71   $850.91  -$268.20  0.3436
        sl_max_8_contra_5          1713   1.85   $751.77   $850.92   -$99.14  0.4389
        sl_max_8_contra_5_gate_1  51075  55.10  $7,312.96 $19,758.46 -$12,445.50 0.1432

    **The ceiling ALONE takes the canonical book from -$340.34 to
    +$54.49 NET** -- the first net-positive continuous-corpus result
    in this repo's history.

    ⚠️ THE NET GOES DEEP NEGATIVE ONCE THE BOOK IS WIDENED. The
    30-70 trades/day target (gate 1.0) costs a lot of gross per leg:
    $0.1432 gross/leg is BELOW the $0.5029 measured round trip. The
    count target and the edge are in direct tension; the count target
    WINS because it was asked for.
    """
    from bt.optimal_config import (
        SL_MAX_USD,
        VOL_STATE_MIN_RANGE_NORM,
        build_engine_kwargs,
    )

    assert SL_MAX_USD == pytest.approx(8.0), (
        "the SL ceiling changed -- re-run "
        "runtime.diagnostics.config_iso_2026_10_25 (the ceiling is "
        "PAIRED with the vol gate; do not tune it in isolation)"
    )
    assert VOL_STATE_MIN_RANGE_NORM == pytest.approx(1.0)
    assert build_engine_kwargs()["sl_max_usd"] == pytest.approx(8.0)
    assert build_engine_kwargs(sl_max_usd=5.0)["sl_max_usd"] == pytest.approx(5.0)


def test_structure_gate_stays_off_until_its_plumbing_is_safe():
    """The structure gate is the one knob NOT promoted, for a hard reason.

    ``structure_gate=True`` makes the engine raise if ``structure`` is
    None (see ``run_vp_sniper_backtest``). Promoting it to the
    canonical default would break every call site that does not build a
    StructureState -- 4 production diagnostics plus the test suite.

    It is also NOT promoted on evidence: the measured side-correlations
    are unchanged by the gate (long +0.968 -> +0.970, short -0.980 ->
    -0.971), so it does NOT break the market-beta lock that
    AGENTS.md identifies as the recipe's core defect.

    Flip this test only together with the plumbing work.
    """
    from bt.optimal_config import STRUCTURE_GATE, build_engine_kwargs

    assert STRUCTURE_GATE is False
    assert build_engine_kwargs()["structure_gate"] is False


def test_structure_sequence_whitelist_is_empty_and_must_not_be_promoted():
    """The 5-event signature whitelist carries NO exploitable P&L.

    2026-10-24, re-measured at canonical MT4 sizing
    (``runtime/diagnostics/sequence_whitelist.py``, log
    ``_reports/wl_rerun.log``). 64 distinct signatures; the BEST EV is
    $0.008 on 235 legs (``bbCcb``) against a $0.05 admission bar.

    The script prints "the WHITELIST improves held-out P&L" because
    kept-net ($0) > base-net (-$23) -- but ``kept == 0`` on every
    slice. That "+$23" is the P&L of NOT TRADING, not of better trade
    selection. Promoting an empty whitelist would be a no-op that
    looks like a win, which is the exact failure mode this repo's
    "never report a single-slice winner" rule exists to prevent.

    The FLIP variant is likewise dead: its own docstring records
    "-$8 on held-out slices".
    """
    import json
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    wl_path = (repo / "runtime" / "diagnostics" / "_reports"
               / "sequence_whitelist" / "whitelist.json")
    assert wl_path.exists(), "the whitelist report is missing -- re-run it"

    wl = json.loads(wl_path.read_text(encoding="utf-8"))
    stats = wl["stats"]
    assert stats, "no signatures were measured"

    # Only signatures that clear the report's OWN sample-size floor can
    # be called an edge. A signature observed on ONE leg has an EV of
    # whatever that leg happened to make -- that is a sample-size
    # artifact, not a signal, and it is exactly the "single-slice
    # winner" failure mode this repo's rules exist to prevent.
    #
    # 2026-10-24 (vol gate ON): the top-EV signature was ``CcCcC``
    # with EV=$16.50 on n=1 -- above the $0.05 bar but on a single
    # leg, and the top signature by EV at n>=5 was ``CcCBB`` with
    # EV=$14.61 on n=12. The report's own ``min_n`` floor of 15
    # correctly refused both (``keep == []``): NO signature reaches
    # 15 legs, so the whitelist is empty for a SAMPLE-SIZE reason, not
    # an EV reason.
    #
    # So the invariant is:
    #   (a) the whitelist is EMPTY, and
    #   (b) no signature that DOES clear min_n clears the EV bar.
    # (b) is vacuous when nothing clears min_n -- in that case the
    # emptiness is explained by the sample-size floor, which is the
    # STRONGER statement.
    min_n = int(wl.get("min_n", 1))
    eligible = {k: v for k, v in stats.items() if int(v["n"]) >= min_n}

    if eligible:
        best_sig, best = max(eligible.items(), key=lambda kv: kv[1]["ev"])
        assert best["ev"] < wl["min_ev"], (
            f"signature {best_sig} now clears the ${wl['min_ev']} bar with "
            f"EV=${best['ev']:.4f} on {best['n']} legs -- re-measure the "
            f"whitelist on held-out slices before promoting it"
        )
    else:
        # Nothing reached the sample-size floor. The whitelist is empty
        # because there is not enough evidence for ANY signature, which
        # is a stronger form of "no edge" than "EV too low".
        max_n = max(int(v["n"]) for v in stats.values())
        assert max_n < min_n, (
            f"no signature reached min_n={min_n} (max n={max_n}) -- the "
            f"whitelist is empty for a sample-size reason; re-measure on "
            f"more slices before drawing any conclusion"
        )

    assert not wl["keep"], (
        f"the whitelist is no longer empty ({len(wl['keep'])} signatures "
        f"kept) -- re-measure it on held-out slices before promoting it"
    )


def test_structure_whitelist_powered_heldout_is_not_significantly_profitable():
    """The POWERED whitelist test (disjoint fit/held-out, coarse buckets).

    The plain whitelist is empty for a SAMPLE-SIZE reason once the vol
    gate is on (331 legs across 60 signatures = 5.5 legs/sig, versus
    a ``min_n`` floor of 15). That is a real limitation, so
    ``sequence_whitelist_powered.py`` coarsens the signature into 3
    polarity buckets and fits on 4 slices / tests on 4 DISJOINT slices.

    Result (2026-10-24): fit picks the ``bear`` bucket (EV +$0.485,
    t +0.52 -- not significant), and on held-out the kept trades have
    **aggregate t = -2.15**, i.e. significantly LOSS-MAKING. Gross
    falls $68 while net rises $2 -- the net "gain" is entirely fee
    savings from trading less, and it destroys the decisive flat slice
    (2025-05: +$62 net -> -$22 net).

    A filter that changes WHICH trades you take cannot manufacture an
    edge the trade population does not have.
    """
    import json
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    out = (repo / "runtime" / "diagnostics" / "_reports"
           / "sequence_whitelist_powered" / "powered_whitelist.json")
    assert out.exists(), "the powered whitelist report is missing -- re-run it"

    pw = json.loads(out.read_text(encoding="utf-8"))

    t_agg = pw["held_t_aggregate"]
    assert t_agg < 2.0, (
        f"the whitelisted held-out trades now have t={t_agg:.2f} -- they may "
        f"be significantly profitable. Re-measure before promoting."
    )
    assert t_agg < 0.0, (
        f"the whitelisted held-out trades have t={t_agg:.2f} (negative) -- "
        f"the whitelist is actively harmful on a gross basis."
    )

    # The net "improvement" must not be hiding a gross deterioration.
    fee = 0.47  # canonical MT4 round trip: $0.07 commission + $0.40 spread
    n_base = sum(r["legs"] for r in pw["held"])
    gross_base = pw["held_total_base"] + n_base * fee
    gross_wl = pw["held_total_wl"] + pw["held_kept_n"] * fee
    assert gross_wl < gross_base, (
        f"the whitelist now improves GROSS by ${gross_wl - gross_base:,.0f} "
        f"on held-out -- re-measure before promoting."
    )


def test_seq_drop_set_engine_matches_the_posthoc_filter():
    """The engine's per-side sequence drop set must be EXACTLY
    equivalent to the post-hoc filter that was measured.

    If they diverge, the measurement does not transfer to the knob
    and every number in AGENTS.md § "The per-side sequence filter"
    is about a strategy the engine does not run.

    Checked on all 8 benchmark slices: leg count AND gross P&L must
    match the cent.
    """
    import json
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    out = (repo / "runtime" / "diagnostics" / "_reports"
           / "seq_drop_set_equivalence.json")
    assert out.exists(), (
        "the equivalence report is missing -- run "
        "`python -m runtime.diagnostics.seq_drop_set_equivalence`"
    )
    d = json.loads(out.read_text(encoding="utf-8"))
    assert d["all_equivalent"] is True, (
        "the engine-side drop set no longer matches the post-hoc filter "
        f"on: {[r['slice'] for r in d['rows'] if not r['equivalent']]}"
    )
    assert len(d["rows"]) == 8, "the equivalence check must cover 8 slices"
    for r in d["rows"]:
        assert r["engine_legs"] == r["posthoc_legs"], (
            f"{r['slice']}: engine kept {r['engine_legs']} legs, the "
            f"post-hoc filter kept {r['posthoc_legs']}"
        )
        assert abs(r["engine_gross"] - r["posthoc_gross"]) < 1e-9, (
            f"{r['slice']}: engine gross {r['engine_gross']} != post-hoc "
            f"{r['posthoc_gross']}"
        )
        # The gate must actually be doing something.
        assert r["n_dropped_seq_side"] > 0, (
            f"{r['slice']}: the drop set never fired -- the gate is dead"
        )


def test_entry_side_gates_must_evaluate_at_the_signal_bar():
    """The D2 resting-order model makes the SIGNAL bar and the FILL
    bar different -- and the per-side sequence filter measured on the
    wrong one gives a different, WRONG answer.

    Under D2 a signal is placed on bar ``S`` and the order fills on a
    LATER bar ``F`` (median 42 bars later on the canonical slice). The
    structure evolves in between.

    The engine's entry-side gates (the side gate, the per-side
    sequence drop set) are evaluated at ``S``, because that is when
    the decision is made and the only bar at which it is causal.

    The first measurement of the per-side filter tagged ``F`` and
    concluded "drop short|b" (105 trades, t = -2.10, +$104 gross).
    That was a FILL-TIME REGIME effect. At the SIGNAL bar the
    ``short|b`` bucket is STRUCTURALLY EMPTY -- a bullish BoS
    invalidates the opposing bear FVGs, so no short can be signalled
    on such a bar.

    This test locks the two facts that make the trap impossible to
    fall into again:
      1. ``ClosedTrade.signal_bar`` exists and is populated, and it
         is what the analysis must use.
      2. ``short|b`` is empty at the signal bar.
    """
    import json
    import pathlib

    from bt._engine.vp_multi_target_engine import ClosedTrade

    # (1) The field exists.
    assert "signal_bar" in ClosedTrade.__dataclass_fields__, (
        "ClosedTrade.signal_bar is missing -- the per-side sequence "
        "analysis cannot be reproduced without it"
    )

    repo = pathlib.Path(__file__).resolve().parents[1]
    out = (repo / "runtime" / "diagnostics" / "_reports"
           / "seq_signal_vs_fill_bar.json")
    assert out.exists(), (
        "the signal-vs-fill report is missing -- run "
        "`python -m runtime.diagnostics.seq_signal_vs_fill_bar`"
    )
    d = json.loads(out.read_text(encoding="utf-8"))

    # (2) short|b is structurally empty at the signal bar.
    assert d["short_b_signal_bar_n"] == 0, (
        f"short|b has {d['short_b_signal_bar_n']} signal-time trades -- "
        f"the structural claim (a bullish BoS invalidates the opposing "
        f"bear FVGs) no longer holds. Re-measure before changing the "
        f"drop set."
    )
    # Every other short bucket must be populated -- otherwise the
    # analysis is broken in a different way.
    counts = d["signal_bar_counts"]
    for ev in ("B", "c", "C"):
        assert counts.get(f"short|{ev}", 0) > 0, (
            f"short|{ev} is empty at the signal bar -- the signal-time "
            f"tagging is broken"
        )


def test_per_side_sequence_drop_set_is_promoted():
    """The per-side sequence filter is PROMOTED.

    The rule: do not take a SHORT when the last structure event was
    a REVERSAL ("c" = CHoCH bull, "C" = CHoCH bear). Do take shorts
    after a bearish CONTINUATION ("B" = BoS bear).

    2026-10-24, ``runtime/diagnostics/seq_drop_set_validate.py`` over
    8 slices, measured at the SIGNAL bar (the bar the engine gates
    on). Exactly two buckets are negative on BOTH the fit and the
    held-out halves: ``short|c`` (FIT -$24 / HELD -$36) and
    ``short|C`` (FIT -$23 / HELD -$20).

    Effect (pooled, 8 slices):

        metric                control    filtered
        legs                       577         395
        total gross              +$276       +$379
        total net                  +$9       +$197
        short-side gross          -$21        +$82
        short-side gross/leg    -$0.081     +$0.996
        long-side gross          +$297       +$297   <- UNTOUCHED
        long-side legs             313         313   <- UNTOUCHED
        dropped-trade t-stat       --      -1.55

    Why this is a QUALITY win and not a "trade less" win:
      * GROSS improves by +$103 while fee savings are only $86, so
        the gross delta alone is positive.
      * The short side's gross PER LEG goes from NEGATIVE to
        POSITIVE (12x better).
      * The LONG side is completely untouched.
      * BOTH sides still trade -- this is not a directional bet.

    ⚠️ THE MEASUREMENT TRAP THIS TEST GUARDS. The first measurement
    tagged the trade's FILL bar and found "short|b" with 105 trades,
    t = -2.10 and a +$104 gross improvement. That was a FILL-TIME
    REGIME effect, not a signal-time one: under the D2 resting-order
    model the order fills a median 42 bars after the signal. The
    engine gate cannot act on a fill-time effect. The signal-time
    measurement is the one that matters, and it gives a different
    (and better) answer. ``short|b`` is in fact STRUCTURALLY EMPTY
    at the signal bar -- a bullish BoS invalidates the opposing bear
    FVGs, so no short can be signalled on such a bar.
    """
    import json
    import pathlib

    from bt.optimal_config import (
        STRUCTURE_SEQ_DROP_SET,
        build_engine_kwargs,
    )

    assert STRUCTURE_SEQ_DROP_SET == frozenset({"short|c", "short|C"}), (
        f"the canonical drop set is {sorted(STRUCTURE_SEQ_DROP_SET)} -- "
        f"re-measure the per-side sequence filter before changing it"
    )
    ek = build_engine_kwargs()
    assert ek["structure_seq_drop_set"] == STRUCTURE_SEQ_DROP_SET

    repo = pathlib.Path(__file__).resolve().parents[1]
    out = (repo / "runtime" / "diagnostics" / "_reports"
           / "seq_drop_set_validate.json")
    assert out.exists(), (
        "the validated per-side report is missing -- run "
        "`python -m runtime.diagnostics.seq_drop_set_validate`"
    )
    d = json.loads(out.read_text(encoding="utf-8"))
    cand = d["candidates"]["drop_short_cC"]
    sc = cand["score"]
    mc = cand["market_corr"]

    # The dropped trades must remain loss-making.
    assert sc["dropped_t"] < 0, (
        f"the dropped buckets' t-stat is now {sc['dropped_t']:.2f} -- they "
        f"are no longer loss-making. Re-measure."
    )
    # GROSS must improve -- a net-only win is just fee savings.
    assert sc["d_gross"] > 0, (
        f"pooled gross no longer improves ({sc['d_gross']:+,.0f}) -- re-measure."
    )
    # The flat-slice test must hold.
    assert sc["flat_d_net"] >= 0, (
        f"the flat-slice test now fails ({sc['flat_d_net']:+,.0f}) -- re-measure."
    )
    # BOTH sides must still trade -- the whole point of the filter.
    assert mc["long"]["n"] > 0 and mc["short"]["n"] > 0, (
        "the filter eliminated a side entirely -- that is a long-only "
        "strategy in disguise, which is explicitly rejected"
    )
    # And neither side may become a directional bet.
    assert abs(mc["long"]["corr"]) < 0.5, (
        f"long-side market correlation is {mc['long']['corr']:.3f} -- the "
        f"strategy has become a directional bet"
    )
    assert abs(mc["short"]["corr"]) < 0.5, (
        f"short-side market correlation is {mc['short']['corr']:.3f} -- the "
        f"strategy has become a directional bet"
    )
    # The LONG side must be completely untouched by the filter.
    base_mc = d["candidates"]["drop_none"]["market_corr"]
    assert mc["long"]["n"] == base_mc["long"]["n"], (
        "the long trade set changed -- the filter must not disturb it"
    )
    # The short side's gross PER LEG must improve (quality, not volume).
    base_short = base_mc["short"]
    assert (mc["short"]["gross"] / mc["short"]["n"]
            > base_short["gross"] / base_short["n"]), (
        "the short side's gross per leg did not improve -- this would be "
        "a volume reduction, not a quality win"
    )


def test_short_side_is_a_measured_drag_but_the_strategy_stays_two_sided():
    """The short side is gross-NEGATIVE in aggregate under the
    promoted vol gate -- but the strategy stays TWO-SIDED.

    2026-10-24, ``runtime/diagnostics/short_side_ablation.py`` on 8
    slices: long gross +$305.41, short gross -$59.19. Long alpha
    +$34.14/slice, short alpha -$12.81/slice. A long-only arm
    measures +$161 net vs -$74 two-sided.

    USER INSTRUCTION (2026-10-24): *"we should never trade one side
    only, if shorts have ablation, merely hardcore profitable market
    struct event sequence separately for longs and shorts."*

    So the long-only arm is DELIBERATELY NOT PROMOTED. It raises the
    market correlation to +0.464 (vs the two-sided recipe's aggregate
    +0.315) and leaves the account with no hedge in a gold sell-off
    -- the exact defect this repo was built to diagnose. The short
    side's negative alpha is attacked with a PER-SIDE SEQUENCE
    FILTER instead, which keeps the side alive.

    This test asserts:
      (a) the measurement exists (so the decision is made on data),
      (b) the canonical config is two-sided.
    """
    import json
    import pathlib

    from bt.optimal_config import TRADE_SIDES, build_engine_kwargs

    repo = pathlib.Path(__file__).resolve().parents[1]
    out = (repo / "runtime" / "diagnostics" / "_reports"
           / "short_side_ablation.json")
    assert out.exists(), (
        "the short-side ablation report is missing -- run "
        "`python -m runtime.diagnostics.short_side_ablation` before "
        "changing the long/short balance"
    )
    d = json.loads(out.read_text(encoding="utf-8"))
    assert "both" in d["summary"], "the control arm is missing"
    assert d["summary"]["both"]["legs"] > 0, "the control arm has no trades"

    # The canonical recipe must remain two-sided.
    assert TRADE_SIDES == "both", (
        f"TRADE_SIDES is {TRADE_SIDES!r}. The strategy must stay two-sided "
        f"-- a long-only account is a directional bet (corr +0.464) with no "
        f"hedge in a sell-off. Gate the short side with the per-side "
        f"sequence filter instead."
    )
    assert build_engine_kwargs()["trade_sides"] == "both"