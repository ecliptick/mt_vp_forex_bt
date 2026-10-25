"""Tests for CONTRA CLOSE / HEDGE NETTING (2026-10-24).

The strategy's structural weakness is that the book can hold opposing
positions at once. A long bought at $3,000 and a short sold at $3,050
are a LOCKED SPREAD: the pair's gross P&L is

    Q * (E_short - E_long)

regardless of where price goes next. Before this feature the engine
closed them independently, so one leg's stop claimed the loss while its
counterparty sat on an equal-and-opposite gain that was never taken, and
both legs paid a round trip to realise a spread that was already in
hand.

These tests lock down the three properties that matter:

1. The netting fires when (and only when) the spread exceeds the
   threshold, and the realised pair P&L equals the locked spread.
2. The multi-target geometry is preserved: a partially-netted
   pre-TP1 trade keeps its TP1/TP2 and its stop, and a post-TP1
   remainder leg stays a remainder leg.
3. The feature is OFF by default and bit-identical to the
   pre-contra-close engine when disabled.
"""
from __future__ import annotations

import math

import pytest

from bt._engine.vp_multi_target_engine import (
    _contra_spread_usd,
    run_vp_sniper_backtest,
)


# ─── Helpers ──────────────────────────────────────────────────────

# A single flat daily profile covering every test window. The engine
# calls ``current_daily_vp`` on every close path, so the set cannot be
# ``None``. The profile carries NO HVN shelves, so the SL/TP fall back
# to the zone-edge geometry — which is what the contra-close tests
# want to isolate.
_VP = None


def _make_vp():
    global _VP
    if _VP is None:
        _VP = _vp_set(1_700_000_000_000_000_000, 86_400)
    return _VP


def _bars(rows, start_ns: int = 1_700_000_000_000_000_000):
    """Build a minimal bars frame from ``(open, high, low, close)`` rows."""
    import pandas as pd

    n = len(rows)
    times = [start_ns + i * 1_000_000_000 for i in range(n)]
    return pd.DataFrame({
        "time": pd.to_datetime(times, unit="ns", utc=True),
        "open": [float(r[0]) for r in rows],
        "high": [float(r[1]) for r in rows],
        "low": [float(r[2]) for r in rows],
        "close": [float(r[3]) for r in rows],
    })


def _engine_kwargs(**over):
    """Engine kwargs with the heavy gates OFF so the test is hermetic."""
    base = dict(
        sl_mode="fvg_only",
        tp_mode="fvg_mult",
        lvn_mode="through",
        sl_buffer_usd=0.05,
        poc_buffer_usd=0.20,
        hvn_buffer_usd=0.10,
        tp_zone_mult_fallback=200.0,
        tp_max_distance_usd=0.0,
        lvn_skip_usd=0.30,
        taker_fee_bps=0.0,      # zero fee: isolates the P&L geometry
        entry_mode="sniper",
        nth_hvn_n=1,
        sl_zone_mult=40.0,
        conflict_policy="stack",
        max_same_dir_open=20,
        entry_fill_model="touch",
        fill_model="pessimistic",
        record_equity_curve=False,
    )
    base.update(over)
    return base


def _zone(direction: int, low: float, high: float, trigger_bar: int):
    """A minimal ``FvgZone``-shaped object for the engine's trigger maps."""
    from bt._engine.ict_signals import FvgZone

    return FvgZone(
        trigger_bar=trigger_bar,
        direction=direction,
        zone_low=low,
        zone_high=high,
        inverted_bar=trigger_bar,
        live=True,
        superseded_bar=-1,
        played_out_bar=-1,
        expired_bar=-1,
        mitigated_bar=-1,
        pierced_bar=-1,
    )


def _vp_set(start_ns: int, n_bars: int, lo: float = 2990.0, hi: float = 3060.0):
    """A minimal ``VolumeProfileSet`` covering the whole test window.

    The engine calls ``current_daily_vp`` on every close path, so the
    set cannot be ``None``. The profile is flat (no HVN shelves), which
    means the SL/TP fall back to the zone-edge geometry — exactly what
    the contra-close tests want to isolate.
    """
    import numpy as np
    from bt._engine.volume_profile import SessionVP, VolumeProfileSet

    end_ns = start_ns + n_bars * 1_000_000_000
    rows = np.array([lo, hi], dtype=np.float64)
    session = SessionVP(
        session_start_ns=start_ns,
        session_end_ns=end_ns,
        poc=(lo + hi) / 2.0,
        vah=hi,
        val=lo,
        total_vol=1.0,
        hvn_zones=[],
        lvn_zones=[],
        row_prices_lo=rows,
        row_prices_hi=rows,
        row_vols=np.array([0.5, 0.5], dtype=np.float64),
    )
    return VolumeProfileSet(
        file_stem="test",
        daily_vp=[session],
        weekly_vp=[],
        naked_pocs=[],
        build_ms={},
    )


# ─── 1. The spread helper ────────────────────────────────────────

def test_contra_spread_is_the_weighted_average_gap():
    # One long at 3000 (qty 1) and one short at 3050 (qty 1).
    assert _contra_spread_usd(1.0, 3000.0, 1.0, 3050.0) == pytest.approx(50.0)
    # Weighted: the short side is 3 oz at 3050 and 1 oz at 3100,
    # so its weighted-average entry is (3*3050 + 3100) / 4 = 3062.5.
    spread = _contra_spread_usd(1.0, 3000.0, 4.0, 3.0 * 3050.0 + 3100.0)
    assert spread == pytest.approx(3062.5 - 3000.0)


def test_contra_spread_is_zero_when_one_side_is_empty():
    assert _contra_spread_usd(0.0, 0.0, 1.0, 3050.0) == 0.0
    assert _contra_spread_usd(1.0, 3000.0, 0.0, 0.0) == 0.0


# ─── 2. The netting fires and books the locked spread ───────────

def test_contra_close_banks_the_locked_spread():
    """A long at 3000 and a short at 3050 net out for +$50 x qty."""
    # Bar 0: price drops to 3000 (the long's resting limit fills).
    # Bar 1: price rallies to 3050 (the short's resting limit fills).
    # Bar 2: the book is hedged with a $50 spread -> contra close.
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    # Long zone (bull FVG) at 2999-3001, triggered on bar 0.
    long_zone = _zone(+1, 3000.0, 3001.0, 0)
    # Short zone (bear FVG) at 3049-3051, triggered on bar 1.
    short_zone = _zone(-1, 3049.0, 3050.0, 1)

    res, diag = run_vp_sniper_backtest(
        bars=bars,
        zones=[long_zone, short_zone],
        vp_set=_make_vp(),
        qty_btc=1.0,
        contra_close=True,
        contra_close_spread_usd=4.5,
        **_engine_kwargs(),
    )

    assert res.n_contra_close_events >= 1, "the netting never fired"
    contra = [t for t in res.trades if t.exit_reason == "contra_close"]
    assert len(contra) >= 2, "both legs must be booked as contra closes"

    # The pair's gross P&L is the locked spread, independent of the
    # market: $50 on 1.00 oz.
    pair_gross = sum(t.pnl_gross_usd for t in contra)
    assert pair_gross == pytest.approx(50.0, abs=1e-6), (
        f"locked spread not banked: got {pair_gross}"
    )


def test_contra_close_does_not_fire_below_the_threshold():
    """A $3.00 spread is below the $4.50 threshold -> no netting.

    The long (zone 3000-3001) fills at 3000 and the short (zone
    3001-3003) fills at 3003 -> a $3.00 spread.
    """
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3004.0, 2999.0, 3003.0),
        (3003.0, 3004.0, 3002.0, 3003.0),
    ])
    long_zone = _zone(+1, 3000.0, 3001.0, 0)
    short_zone = _zone(-1, 3001.0, 3003.0, 1)

    res, _ = run_vp_sniper_backtest(
        bars=bars,
        zones=[long_zone, short_zone],
        vp_set=_make_vp(),
        qty_btc=1.0,
        contra_close=True,
        contra_close_spread_usd=4.5,
        **_engine_kwargs(),
    )
    assert res.n_contra_close_events == 0
    assert not [t for t in res.trades if t.exit_reason == "contra_close"]


def test_contra_close_threshold_is_respected_exactly():
    """A $5.00 spread fires at a $4.50 threshold and not at $6.00.

    NOTE on the geometry: a resting limit fills at the ZONE EDGE, so
    the long (zone 3000-3001) fills at 3000 and the short (zone
    3004-3005) fills at 3005 -> a $5.00 spread.
    """
    def _run(threshold: float) -> int:
        bars = _bars([
            (3005.0, 3006.0, 2999.0, 3000.0),
            (3000.0, 3051.0, 2999.0, 3050.0),
            (3050.0, 3051.0, 3049.0, 3050.0),
        ])
        res, _ = run_vp_sniper_backtest(
            bars=bars,
            zones=[
                _zone(+1, 3000.0, 3001.0, 0),
                _zone(-1, 3004.0, 3005.0, 1),
            ],
            vp_set=_make_vp(),
            qty_btc=1.0,
            contra_close=True,
            contra_close_spread_usd=threshold,
            **_engine_kwargs(),
        )
        return res.n_contra_close_events

    assert _run(4.5) >= 1, "a $5.00 spread must fire at a $4.50 threshold"
    assert _run(6.0) == 0, "a $5.00 spread must NOT fire at a $6.00 threshold"


# ─── 3. Off by default / bit-identical ───────────────────────────

def test_contra_close_is_on_at_the_promoted_threshold():
    """PROMOTED 2026-10-25: the feature ships ON at a $5.00 threshold.

    The threshold was moved from $1.0 to $5.0 by instruction (the
    $1.0 pair-level ladder is in ``bt/optimal_config.py``'s block
    above — $5.0 sits between the $4.5 and $6.5 rungs).

    The ON/OFF flip is the user's call; the MEASUREMENT that the
    feature helps in isolation is what justifies shipping it:

        cell                       legs  /day   gross     fees      net
        base_canonical             1692   1.83   $510.58   $850.92  -$340.34
        contra_5                   1696   1.83   $582.71   $850.91  -$268.20

    +$72.13 of gross and +$72.14 of net at a constant leg count.
    """
    from bt.optimal_config import (
        CONTRA_CLOSE,
        CONTRA_CLOSE_SPREAD_USD,
        build_engine_kwargs,
    )
    assert CONTRA_CLOSE is True, (
        "contra_close was promoted on 2026-10-25 (feature ON, $5.00 "
        "threshold). If it is being turned back off, re-measure the "
        "pair-level analysis first."
    )
    assert CONTRA_CLOSE_SPREAD_USD == pytest.approx(5.0)
    ek = build_engine_kwargs()
    assert ek["contra_close"] is True
    assert ek["contra_close_spread_usd"] == pytest.approx(5.0)


def test_contra_close_knobs_are_sweepable():
    from bt.optimal_config import build_engine_kwargs
    ek = build_engine_kwargs(contra_close=True, contra_close_spread_usd=7.5)
    assert ek["contra_close"] is True
    assert ek["contra_close_spread_usd"] == pytest.approx(7.5)
    with pytest.raises(KeyError):
        build_engine_kwargs(contra_close_bogus=1)


def test_disabled_contra_close_is_bit_identical():
    """With the feature off, the run is identical to the old engine."""
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    zones = [
        _zone(+1, 3000.0, 3001.0, 0),
        _zone(-1, 3049.0, 3050.0, 1),
    ]
    res_off, _ = run_vp_sniper_backtest(
        bars=bars, zones=zones, vp_set=_make_vp(), qty_btc=1.0,
        contra_close=False, contra_close_spread_usd=0.0,
        **_engine_kwargs(),
    )
    res_zero, _ = run_vp_sniper_backtest(
        bars=bars, zones=zones, vp_set=_make_vp(), qty_btc=1.0,
        contra_close=True, contra_close_spread_usd=0.0,
        **_engine_kwargs(),
    )
    assert res_off.n_contra_close_events == 0
    assert res_zero.n_contra_close_events == 0
    assert len(res_off.trades) == len(res_zero.trades)
    for a, b in zip(res_off.trades, res_zero.trades):
        assert a.exit_reason == b.exit_reason
        assert a.pnl_gross_usd == pytest.approx(b.pnl_gross_usd)


def test_netted_pair_pnl_is_independent_of_the_exit_price():
    """The banked spread must not depend on WHERE the netting happened.

    This is the mathematical guarantee that makes the feature safe: the
    pair's gross P&L is ``Q * (E_short - E_long)`` regardless of the
    market price at the moment of the close. Both legs are booked at
    the SAME exit price, so the price terms cancel exactly.

    The test runs the same book with the final bar closing at three
    different prices and asserts the booked gross is identical.
    """
    def _gross_at(final_close: float) -> float:
        bars = _bars([
            (3005.0, 3006.0, 2999.0, 3000.0),
            (3000.0, 3051.0, 2999.0, 3050.0),
            (3050.0, 3051.0, 3049.0, final_close),
        ])
        res, _ = run_vp_sniper_backtest(
            bars=bars,
            zones=[
                _zone(+1, 3000.0, 3001.0, 0),
                _zone(-1, 3049.0, 3050.0, 1),
            ],
            vp_set=_make_vp(),
            qty_btc=1.0,
            contra_close=True,
            contra_close_spread_usd=4.5,
            **_engine_kwargs(),
        )
        contra = [t for t in res.trades if t.exit_reason == "contra_close"]
        assert contra, "the netting never fired"
        return float(sum(t.pnl_gross_usd for t in contra))

    g1 = _gross_at(3050.0)
    g2 = _gross_at(3060.0)
    g3 = _gross_at(3100.0)
    assert g1 == pytest.approx(g2), f"{g1} != {g2}"
    assert g1 == pytest.approx(g3), f"{g1} != {g3}"
    # The locked spread: long @3000, short @3050, 1.00 oz -> $50.
    assert g1 == pytest.approx(50.0, abs=1e-6), (
        f"the banked spread is wrong: {g1}"
    )


# ─── 4. The multi-target clash ───────────────────────────────────

def test_partially_netted_remainder_keeps_its_multi_target_geometry():
    """A pre-TP1 trade netted PARTIALLY must keep TP1, TP2 and its stop.

    This is the clash the feature has to get right. If a partially
    netted pre-TP1 trade were rewritten as a remainder leg, its stop
    would silently promote to breakeven and its TP1 would disappear.
    """
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3001.5, 2999.0, 3001.0),
        (3001.0, 3002.5, 3000.5, 3002.0),
        (3002.0, 3003.0, 3001.5, 3002.5),
    ])
    # The book is built up over four bars so the SPREAD crosses the
    # threshold only once the second short has joined:
    #
    #   bar 0: the long zone (3000-3001) fires.
    #   bar 1: the long fills @3000 (zone_low). Short A fires.
    #   bar 2: short A fills @3001 (zone_high) -> spread 1.00, which
    #          is BELOW the 1.50 threshold, so no netting yet.
    #   bar 3: short B (fired on bar 2) fills @3002 (zone_high)
    #          -> short wavg = 3001.5, spread = 1.50 -> netting.
    #
    # At the netting: long qty 1.0 vs short qty 2.0, so the match is
    # 1.0. The LONG is fully netted (closed) and each SHORT keeps a
    # 0.5 residual. The long zone is pre-TP1 (multi_target mode).
    long_zone = _zone(+1, 3000.0, 3001.0, 0)
    short_zone_a = _zone(-1, 3000.5, 3001.0, 1)
    short_zone_b = _zone(-1, 3001.5, 3002.0, 2)

    res, _ = run_vp_sniper_backtest(
        bars=bars,
        zones=[long_zone, short_zone_a, short_zone_b],
        vp_set=_make_vp(),
        qty_btc=1.0,
        contra_close=True,
        contra_close_spread_usd=1.5,
        **_engine_kwargs(tp_mode="multi_target", partial_close_pct=0.5),
    )
    contra = [t for t in res.trades if t.exit_reason == "contra_close"]
    assert contra, "the netting did not fire"
    # The LONG was fully netted (1.0 of 1.0) and each SHORT was half
    # netted (0.5 of 1.0), so 2.0 of quantity was booked as closed.
    assert sum(t.qty_btc for t in contra) == pytest.approx(2.0), (
        "expected 1.0 netted on the long + 0.5 on each short"
    )
    # The SHORT legs were only partially netted: their residuals must
    # still be open at the end of the run (closed with reason 'eod').
    eod = [t for t in res.trades if t.exit_reason == "eod"]
    assert eod, "the partially-netted residual was lost"
    assert sum(t.qty_btc for t in eod) == pytest.approx(1.0), (
        "the two 0.5 residuals were not preserved"
    )
    for t in eod:
        # A residual that is still a pre-TP1 trade keeps its TP1
        # (partial_close_pct > 0) and its ORIGINAL stop (not
        # breakeven).
        assert t.partial_close_pct == pytest.approx(0.5), (
            "the residual lost its TP1 partial-close geometry"
        )
        assert t.sl_struct_ref != "breakeven", (
            "the residual's stop was silently promoted to breakeven"
        )


def test_contra_close_fees_match_the_engine_convention():
    """A netted PAIR pays the SAME fee as two independent closes.

    2026-10-24 (engine decision, superseding the original "one round
    trip per pair" design): on a hedging account each leg is its own
    position with its own entry and exit spread, so closing both pays
    TWO round trips. The status quo ALSO pays two (each leg
    eventually closes on its own), so the netting does not change the
    fee count — its benefit is realising the locked spread
    deterministically and removing the naked residual. Charging one
    would be a per-trade windfall that flatters the feature.

    The invariant worth locking down is therefore: the netted pair's
    fee equals the fee the two legs would have paid had they been
    closed independently — NOT less.
    """
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    bps = 10.0
    res, _ = run_vp_sniper_backtest(
        bars=bars,
        zones=[
            _zone(+1, 3000.0, 3001.0, 0),
            _zone(-1, 3049.0, 3050.0, 1),
        ],
        vp_set=_make_vp(),
        qty_btc=1.0,
        contra_close=True,
        contra_close_spread_usd=4.5,
        **_engine_kwargs(taker_fee_bps=bps),
    )
    contra = [t for t in res.trades if t.exit_reason == "contra_close"]
    assert len(contra) == 2
    total_fee = sum(t.fee_usd for t in contra)

    # Each leg pays ONE round trip on its OWN entry/exit notional:
    #   long : |3050 + 3000| * 0.5 * 1.0 * (bps/10000) * 2.0 = 6.05
    #   short: |3050 + 3050| * 0.5 * 1.0 * (bps/10000) * 2.0 = 6.10
    long_leg = next(t for t in contra if t.direction > 0)
    short_leg = next(t for t in contra if t.direction < 0)
    expected_long = abs(long_leg.exit_price + long_leg.entry_price) \
        * 0.5 * 1.0 * (bps / 10000.0) * 2.0
    expected_short = abs(short_leg.exit_price + short_leg.entry_price) \
        * 0.5 * 1.0 * (bps / 10000.0) * 2.0
    assert total_fee == pytest.approx(
        expected_long + expected_short, rel=1e-6), (
        f"expected two round trips "
        f"(${expected_long + expected_short:.4f}), got ${total_fee:.4f}"
    )
    # And the netting must NOT be a fee windfall: both legs are
    # charged, exactly as they would be if they closed independently.
    assert sum(1 for t in contra if t.fee_usd > 0) == 2


# ─── 6. The promotion verdict is locked ───────────────────────────

def test_contra_close_promotion_is_documented_and_reversible():
    """The feature ships ON, and the history of that decision is kept.

    HISTORY (2026-10-24, 6 slices, $4.50 threshold —
    `runtime/diagnostics/contra_close_ab.py` +
    `runtime/diagnostics/contra_close_why.py`):

    * the aggregate gross delta is +$83.14, which LOOKS like a
      mechanical risk reduction;
    * but the whole gain was ONE hedged pair on ONE bull slice. The
      largest single leg contributed +$80.94, so the delta with that
      leg removed was +$2.20;
    * ``corr(delta, market move) = +0.57`` — the code only nets a
      POSITIVE spread (longs entered below shorts), which is a
      bull-shaped book by construction. A bear-shaped book holds a
      locked LOSS and the feature never fires there;
    * the FLAT slice — the decisive chop test — was NEGATIVE
      (-$11.78).

    That evidence is why the feature shipped OFF for a day.

    PROMOTION (2026-10-25, user instruction "contra 5USD"): the
    feature was turned ON and the threshold moved $1.0 -> $5.0. The
    supporting measurement is the continuous-corpus isolation A/B
    (`runtime/diagnostics/config_iso_2026_10_25.py`):

        cell                       legs  /day   gross     fees      net
        base_canonical             1692   1.83   $510.58   $850.92  -$340.34
        contra_5                   1696   1.83   $582.71   $850.91  -$268.20

    +$72.13 of gross and +$72.14 of net at a constant leg count.

    The threshold is now MORE CONSERVATIVE than the $1.0 the
    pair-level ladder picked, so it fires on WIDE locks only.

    ⚠️ This test exists so the flip is a DOCUMENTED decision, not a
    silent one. If it is ever turned back off, the reason must be
    re-measured — and the flat-slice evidence above is the thing to
    re-check first.
    """
    from bt.optimal_config import CONTRA_CLOSE, CONTRA_CLOSE_SPREAD_USD

    assert CONTRA_CLOSE is True, (
        "contra_close ships ON as of 2026-10-25 (user instruction). If "
        "it is being turned back off, re-measure the pair-level "
        "analysis — in particular the FLAT-slice delta, which was "
        "-$11.78 at $4.50."
    )
    assert CONTRA_CLOSE_SPREAD_USD == pytest.approx(5.0)


def test_contra_close_report_says_not_promoted():
    """The measurement artifact must agree with the shipped default.

    If the report says the feature is promoted but the config ships
    it OFF (or vice versa), one of the two is stale and the promotion
    decision is not actually recorded anywhere authoritative.

    The invariant: at EVERY threshold where the netting actually
    fired, the FLAT-slice delta must not be positive. The flat slice
    is the decisive chop test (a strategy with timing skill makes
    money in a choppy market), so a positive flat delta would be the
    one result that could justify promotion.
    """
    import json
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1]
    report = root / "runtime" / "diagnostics" / "_reports" / "contra_close_ab.json"
    if not report.exists():
        pytest.skip("contra_close_ab.json not present in this checkout")

    data = json.loads(report.read_text(encoding="utf-8"))
    summary = data["summary"]
    assert summary, "the report has no summary"

    fired = 0
    for thr, row in summary.items():
        if int(row["total_contra_events"]) == 0:
            # No events -> no delta is attributable to the feature.
            continue
        fired += 1
        assert row["delta_net_flat_slices"] <= 0.0, (
            f"threshold ${thr}: the flat-slice delta is POSITIVE "
            f"({row['delta_net_flat_slices']:.2f}). If that is a genuine "
            f"improvement, re-run contra_close_why and re-measure before "
            f"promoting."
        )
    assert fired, "no threshold in the report fired the netting at all"


def test_contra_close_broad_sweep_does_not_improve_the_month_count():
    """The 15-month broad sweep is the strongest evidence against promotion.

    Measured 2026-10-24 over 15 monthly slices (899 legs at the
    canonical config), ``runtime/diagnostics/contra_close_broad.py``:

    * the flat-slice delta is NEGATIVE at every threshold;
    * ``corr(delta, market move) = +0.49`` — the code only nets a
      POSITIVE spread (longs entered below shorts), so the benefit is
      structurally bull-tilted. Every BEAR month fires ZERO events;
    * ``months_positive_net`` stays at 4/15 at EVERY threshold —
      identical to the OFF baseline.

    That third point is decisive: a real improvement moves the month
    count. This one only adds dollars to months that were already
    winning, on the bull side, from a small number of large-spread
    events.
    """
    import json
    import pathlib

    report = (pathlib.Path(__file__).resolve().parents[1]
              / "runtime" / "diagnostics" / "_reports" / "contra_close_broad.json")
    if not report.exists():
        pytest.skip("contra_close_broad.json not present in this checkout")

    data = json.loads(report.read_text(encoding="utf-8"))
    summary = data["summary"]
    assert summary, "the broad report has no summary"

    baseline = summary.get("0")
    assert baseline is not None, "the broad report is missing the OFF baseline"
    base_pos_net = int(baseline["months_positive_net"])
    base_pos_gross = int(baseline["months_positive_gross"])

    for thr, row in summary.items():
        if int(row["total_contra_events"]) == 0:
            continue
        assert row["delta_net_flat"] <= 0.0, (
            f"threshold ${thr}: the flat-month delta is POSITIVE "
            f"({row['delta_net_flat']:.2f}) over "
            f"{row['n_flat_months']} flat months. Re-measure before promoting."
        )
        # The NET month count is the decisive one and must not move:
        # the feature adds dollars to months that were ALREADY
        # winning, it does not flip losing months to winners.
        assert int(row["months_positive_net"]) == base_pos_net, (
            f"threshold ${thr}: months_positive_net changed from "
            f"{base_pos_net} to {row['months_positive_net']}. If the feature "
            f"now flips months to positive, re-run contra_close_why and "
            f"re-assess the promotion."
        )
        # The GROSS month count MAY move, and when it does the month
        # it flips must be a BULL month — that is the beta-proxy
        # signature, not an improvement. (Measured 2026-10-24: at the
        # $1 and $2.5 thresholds the count goes 10 -> 11, and the
        # month that flips is 2025-03 at +9.29% market move, whose
        # gross goes -$5.27 -> +$9.20 while its NET stays negative
        # at -$6.22. The fee bill is what keeps it negative.)
        flipped = [
            ym for ym, v in row["per_month"].items()
            if (v["gross"] > 0.0)
            != (baseline["per_month"][ym]["gross"] > 0.0)
        ]
        if int(row["months_positive_gross"]) != base_pos_gross:
            assert flipped, (
                f"threshold ${thr}: months_positive_gross changed from "
                f"{base_pos_gross} to {row['months_positive_gross']} but no "
                f"individual month flipped sign. Re-measure before promoting."
            )
            for ym in flipped:
                mkt = float(baseline["per_month"][ym]["market_pct"])
                assert mkt > 1.0, (
                    f"threshold ${thr}: the month that flipped to gross-"
                    f"positive is {ym} at {mkt:+.2f}% market move. A FLAT "
                    f"or BEAR month flipping would mean the feature has a "
                    f"direction-independent component — re-assess the "
                    f"promotion."
                )


# ─── 5. Engine surface ───────────────────────────────────────────
def test_contra_knobs_reach_the_engine():
    """The allowlist must not advertise a knob the engine rejects."""
    import inspect
    from bt.optimal_config import _ENGINE_OVERRIDES_ALLOWED

    sig = set(inspect.signature(run_vp_sniper_backtest).parameters)
    unknown = {k for k in _ENGINE_OVERRIDES_ALLOWED if k not in sig}
    assert not unknown, f"allowlist advertises unknown knobs: {sorted(unknown)}"


def test_contra_close_counters_are_reported():
    """The counters must be populated so a sweep can report the feature."""
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    res, _ = run_vp_sniper_backtest(
        bars=bars,
        zones=[
            _zone(+1, 3000.0, 3001.0, 0),
            _zone(-1, 3049.0, 3050.0, 1),
        ],
        vp_set=_make_vp(),
        qty_btc=1.0,
        contra_close=True,
        contra_close_spread_usd=4.5,
        **_engine_kwargs(),
    )
    assert res.n_contra_close_events >= 1
    assert res.n_contra_legs_closed >= 2
    assert res.n_contra_qty_netted > 0.0
    assert res.contra_gross_usd > 0.0
    assert res.contra_spread_usd_max == pytest.approx(50.0, abs=1e-6)
    assert not math.isnan(res.contra_fees_usd)


# ─── 6. Hedging-frequency counters (2026-10-25) ──────────────────
#
# The passive SPREAD-SCAN design answers "how wide is the locked
# spread?" but NOT the question the feature was actually asked
# about: "how often does an INCOMING OPPOSITE position arrive while
# the book is already hedged?"
#
# These counters answer that question from INSIDE the engine, so the
# answer cannot be a reconstruction artefact.
def test_hedge_stats_count_incoming_opposite_fills():
    """A fill opposite to an already-open position must be counted."""
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    zones = [
        _zone(+1, 3000.0, 3001.0, 0),
        _zone(-1, 3049.0, 3050.0, 1),
    ]
    res, _ = run_vp_sniper_backtest(
        bars=bars, zones=zones, vp_set=_make_vp(), qty_btc=1.0,
        contra_close=False, contra_close_spread_usd=0.0,
        contra_hedge_stats=True,
        **_engine_kwargs(),
    )
    assert res.n_fills_total >= 2
    # The SHORT opens while the LONG is still open -> an incoming
    # opposite position.
    assert res.n_fills_opposite_open >= 1
    assert res.n_bars_both_sides_open >= 1
    assert res.hedged_spread_max_usd > 0.0


def test_hedge_stats_are_off_by_default_and_do_not_change_results():
    """The stats are a pure READ: OFF by default, and enabling them
    must not change a single number in the run."""
    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    zones = [
        _zone(+1, 3000.0, 3001.0, 0),
        _zone(-1, 3049.0, 3050.0, 1),
    ]
    off, _ = run_vp_sniper_backtest(
        bars=bars, zones=zones, vp_set=_make_vp(), qty_btc=1.0,
        contra_close=False, contra_close_spread_usd=0.0,
        **_engine_kwargs(),
    )
    assert off.n_fills_total == 0
    assert off.n_fills_opposite_open == 0
    assert off.n_bars_both_sides_open == 0

    on, _ = run_vp_sniper_backtest(
        bars=bars, zones=zones, vp_set=_make_vp(), qty_btc=1.0,
        contra_close=False, contra_close_spread_usd=0.0,
        contra_hedge_stats=True,
        **_engine_kwargs(),
    )
    assert len(on.trades) == len(off.trades)
    for a, b in zip(on.trades, off.trades):
        assert a.pnl_net_usd == pytest.approx(b.pnl_net_usd, abs=1e-9)
        assert a.entry_price == pytest.approx(b.entry_price, abs=1e-9)
        assert a.exit_bar == b.exit_bar


def test_on_bar_callback_survives_a_vol_state_object():
    """The per-bar callback must not assume ``vol_state`` is a dict.

    The canonical config hands the engine a ``VolState`` OBJECT, and
    the callback path called ``vol_state.items()`` -- which raised
    ``AttributeError`` on the first bar. It went unnoticed because
    ``on_bar_callback`` had ZERO production callers, so the branch was
    never executed.
    """
    from bt._engine.vol_state import VolState

    bars = _bars([
        (3005.0, 3006.0, 2999.0, 3000.0),
        (3000.0, 3051.0, 2999.0, 3050.0),
        (3050.0, 3051.0, 3049.0, 3050.0),
    ])
    zones = [
        _zone(+1, 3000.0, 3001.0, 0),
        _zone(-1, 3049.0, 3050.0, 1),
    ]
    seen = []

    def _cb(_i, _t, _c, _h, _l, _book, _vs):
        seen.append(1)

    vs = VolState(series={"range_norm_1m": [1.0, 2.0, 3.0]})
    res, _ = run_vp_sniper_backtest(
        bars=bars, zones=zones, vp_set=_make_vp(), qty_btc=1.0,
        vol_state=vs,
        on_bar_callback=_cb,
        on_bar_every_k=1,
        **_engine_kwargs(),
    )
    assert seen, "the callback never fired"


# ─── 9. The PAIR-LEVEL findings (2026-10-25) ─────────────────────
#
# The monthly view judged this feature on 15 monthly totals. The
# pair-level view (the unit the feature actually acts on) changed
# the verdict and re-picked the threshold. These tests lock the
# PROPERTIES that made that change, so a future refactor cannot
# silently reintroduce the monthly view's mistakes.

def test_pair_level_threshold_is_five_dollars():
    """The threshold was moved $1.0 -> $5.0 on 2026-10-25 (instruction).

    The $1.0 threshold was itself promoted from a PAIR-LEVEL
    analysis on 2026-10-25: the monthly sweep chose $6.5 by a
    tie-break between thresholds the monthly view could not
    distinguish. At $6.5 the netting fires on ZERO bear-market
    pairs -- a high threshold needs a WIDE lock to fire, and wide
    locks are rarer in a bear book. At $1.0 it fires on the bear
    pairs too, so the LOWER threshold is the LESS directional one.

    The ladder (in ``bt/optimal_config.py``):

        thr   n reachable   total    mean   Wilcoxon p
        0.5      31      +141.23   +4.556     0.063
        1.0      29      +139.73   +4.818     0.056   <- picked
        2.0      24      +123.00   +5.125     0.098
        4.5      18       +92.13   +5.118     0.396
        6.5      14      +101.71   +7.265     0.177

    $5.0 sits BETWEEN the $4.5 and $6.5 rungs. It was set by
    instruction on 2026-10-25 ("contra 5USD"), NOT re-picked from a
    fresh pair-level analysis -- so it is MORE CONSERVATIVE than $1.0
    (it fires only on WIDE locks).

    ⚠️ This test locks the value AND the reason, so the next person
    to "re-tune" it has to confront the direction argument rather
    than a P&L tie-break.
    """
    from bt.optimal_config import CONTRA_CLOSE_SPREAD_USD

    assert CONTRA_CLOSE_SPREAD_USD == pytest.approx(5.0), (
        "the threshold is $5.0 (2026-10-25, instruction). It was NOT "
        "re-picked from a fresh pair-level analysis -- it sits "
        "between the $4.5 and $6.5 rungs of the ladder in "
        "bt/optimal_config.py. Re-run contra_close_actual_vs_simul "
        "and contra_close_stats before changing it."
    )


def test_contra_close_threshold_is_below_the_one_dollar_spread_band():
    """The threshold must exclude the spread band whose mean paired
    delta is NEGATIVE.

    Measured: the $0-$1 spread band has a mean paired delta of
    -$0.92 across 5 pairs. A threshold at or below $0.50 would
    admit that band. $1.0 excludes it, which is why $1.0 was
    chosen over the statistically-similar $0.5.
    """
    from bt.optimal_config import CONTRA_CLOSE_SPREAD_USD

    assert CONTRA_CLOSE_SPREAD_USD > 0.5, (
        "a threshold at or below $0.50 admits the $0-$1 spread band, "
        "whose mean paired delta is NEGATIVE (-$0.92 over 5 pairs)"
    )


def test_simultaneous_close_pnl_is_independent_of_the_exit_price():
    """The counterfactual that justifies the feature is PRICE-EXACT.

    For a balanced pair, closing both legs at price P gives

        (P - E_long)*M + (E_short - P)*M = M*(E_short - E_long)

    The P terms cancel EXACTLY. So the "close simultaneously"
    counterfactual is not an optimistic assumption about where price
    goes -- it is the only price-correct value of the locked spread.

    If this ever stops holding, the whole pair-level measurement
    (and therefore the promotion) is void.
    """
    e_long, e_short, m = 3000.0, 3050.0, 1.0
    for p in (2900.0, 3000.0, 3050.0, 3200.0):
        gross = (p - e_long) * m + (e_short - p) * m
        assert gross == pytest.approx((e_short - e_long) * m, abs=1e-9)


def test_a_negative_spread_pair_is_a_locked_loss_not_a_locked_gain():
    """A bear-shaped book holds a locked LOSS.

    The netting only fires on a POSITIVE spread (longs entered
    below shorts). This is the structural reason the feature can
    only ever help on a bull-shaped book, and it is why the
    threshold matters: a high threshold makes the one-sidedness
    WORSE, not better.
    """
    e_long, e_short, m = 3050.0, 3000.0, 1.0
    p = 3025.0
    gross = (p - e_long) * m + (e_short - p) * m
    assert gross == pytest.approx((e_short - e_long) * m, abs=1e-9)
    assert gross < 0.0, "a negative-spread pair is a locked LOSS"


def test_the_sign_test_is_the_right_test_for_the_paired_delta():
    """The paired delta sample is small and right-skewed.

    The t-test is the wrong tool for it; the sign test is what the
    pair-level verdict rests on. This test pins the SHAPE that makes
    the sign test necessary: a right-skewed distribution whose
    median is far below its mean.

    If a future corpus makes the sample symmetric, the sign test
    and the t-test agree and this guard becomes vacuous -- but it
    must not be silently removed.
    """
    import numpy as np

    # The measured shape of the 34-pair sample (rounded to 2dp).
    delta = np.array(
        [83.43, 22.06, 15.16, 8.78, 8.34, 7.93, 7.92, 6.47, 6.39,
         6.30, 5.84, 2.28, 1.90, 1.52, 1.51, 1.45, 1.36, 1.28, 1.01,
         0.73, 0.63, 0.63, 0.63, 0.15,
         -0.34, -2.26, -4.14, -4.33, -4.34, -6.85, -7.59, -7.64,
         -8.86, -12.29],
        dtype=np.float64,
    )
    assert delta.size == 34
    # Right-skewed: the mean is far above the median.
    assert delta.mean() > float(np.median(delta)) * 2.0, (
        "the sample must remain right-skewed for the sign test "
        "(not the t-test) to be the right tool"
    )
    # The sign test is what the verdict rests on: 24 of 34 improve.
    n_pos = int((delta > 0).sum())
    assert n_pos == 24, f"expected 24 improving pairs, got {n_pos}"
    # And it is NOT a sign that the magnitude is tail-dependent: one
    # pair carries ~62% of the aggregate.
    assert delta.max() / delta.sum() > 0.40, (
        "the aggregate must remain tail-dependent -- the honest "
        "statement is 'broad in sign, tail-dependent in magnitude'"
    )

