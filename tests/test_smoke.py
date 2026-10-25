"""Smoke tests for the bt/ flat forex backtest repo.

These tests verify the import surface, the canonical recipe,
the no-defaults policy, and the live-MT4 fee math (40¢ spread).
Run via::

    cd C:\\coding\\sniper_mt_vp_forex_bt_only
    python -m pytest tests/test_smoke.py -v
"""
from __future__ import annotations

import pytest

import bt
from bt.optimal_config import (
    ENTRY_MODE,
    FOREX_LOTS,
    FOREX_CONTRACT_SIZE,
    FOREX_COMMISSION_PER_LOT_USD,
    FOREX_SPREAD_PER_OZ_USD,
    build_engine_kwargs,
    build_detector_kwargs,
    build_p95_kwargs,
    build_structure_kwargs,
    build_swing_kwargs,
    build_vp_kwargs,
)
from bt.risk import margin_and_cost, pnl_per_usd_move
from bt.kelly import size_lots
from bt.leverage import account_exposure, max_lots_for_exposure


# ─── Import surface ─────────────────────────────────────────────


def test_bt_imports():
    """The dead bar-engine surface is GONE (2026-10-24).

    ``run_ict_backtest`` / ``optimal_params`` / ``OPTIMAL_RECIPE_VERSION``
    were removed with the recipe dataclass they served. Their presence
    must not come back: the VP multi-target engine is the only engine.
    """
    assert not hasattr(bt, 'run_ict_backtest')
    assert not hasattr(bt, 'optimal_params')
    assert not hasattr(bt, 'OPTIMAL_RECIPE_VERSION')
    # The one engine that IS live, reachable via exactly one definition.
    from bt.vp_engine import run_vp_sniper_backtest
    from bt._engine.vp_multi_target_engine import (
        run_vp_sniper_backtest as direct,
    )
    assert run_vp_sniper_backtest is direct


def test_all_submodules_importable():
    """Every bt/ module should be importable without error."""
    from bt import optimal_config, volume_profile, vol_state
    from bt import swing_cap, vp_engine, risk, kelly, leverage
    assert optimal_config is not None


# ─── Canonical recipe ───────────────────────────────────────────


def test_canonical_recipe_is_forex():
    """There is no recipe OBJECT any more — the constants ARE the recipe."""
    assert FOREX_LOTS == 0.01
    assert FOREX_CONTRACT_SIZE == 100.0
    assert FOREX_COMMISSION_PER_LOT_USD == 7.0
    assert FOREX_SPREAD_PER_OZ_USD == 0.40
    assert ENTRY_MODE == 'dual'


def test_canonical_constants():
    assert FOREX_LOTS == 0.01
    assert FOREX_CONTRACT_SIZE == 100.0
    assert FOREX_COMMISSION_PER_LOT_USD == 7.0
    assert FOREX_SPREAD_PER_OZ_USD == 0.40


# ─── No-defaults policy ─────────────────────────────────────────


def test_every_builder_rejects_illegal_overrides():
    """Every builder enforces its own allowlist — one layer, no fallbacks."""
    with pytest.raises(KeyError):
        build_engine_kwargs(bogus_field=42)
    with pytest.raises(KeyError):
        build_vp_kwargs(bogus_field=42)
    with pytest.raises(KeyError):
        build_detector_kwargs(bogus_field=42)
    with pytest.raises(KeyError):
        build_swing_kwargs(bogus_field=42)
    with pytest.raises(KeyError):
        build_p95_kwargs(bogus_field=42)
    with pytest.raises(KeyError):
        build_structure_kwargs(bogus_field=42)


# ─── Live MT4 fee math (40¢/oz) ──────────────────────────────


def test_canonical_live_fees():
    """The single source of truth is the LIVE MT4 quote."""
    assert FOREX_SPREAD_PER_OZ_USD == pytest.approx(0.40, abs=1e-9)
    assert FOREX_COMMISSION_PER_LOT_USD == pytest.approx(7.0, abs=1e-9)


def test_margin_and_cost_live_fees():
    """At canonical sizing, round-trip is $0.07 commission + $0.40 spread = $0.47."""
    mc = margin_and_cost(
        lots=0.01, contract_size=100.0, leverage=100.0,
        spot=3000.0, commission_per_lot_usd=7.0,
        spread_per_oz_usd=0.40,
    )
    assert mc.notional_usd == pytest.approx(3000.0, abs=1e-6)
    assert mc.margin_required_usd == pytest.approx(30.0, abs=1e-6)
    assert mc.round_trip_cost_usd == pytest.approx(0.47, abs=1e-6)


def test_engine_kwargs_carries_live_fees_via_taker_bps():
    """The live MT4 fees reach the engine ONLY as ``taker_fee_bps``.

    ``commission_per_lot_usd`` / ``spread_per_oz_usd`` were previously
    emitted into ``engine_kwargs``, but ``bt.vp_engine._FOREX_ONLY_KWARGS``
    strips them before the call — so they were a silent no-op knob and
    sweeping them changed nothing. The fee is now folded into
    ``taker_fee_bps`` at build time, which IS a real engine parameter.
    """
    ek = build_engine_kwargs()
    # The raw forex knobs must NOT be present (they'd be dead kwargs).
    assert 'spread_per_oz_usd' not in ek
    assert 'commission_per_lot_usd' not in ek
    # ...but their combined effect must be folded into taker_fee_bps.
    # spot=3000, oz=1: round-trip = 0.01*7 + 1*0.40 = $0.47
    # The engine debits `avg * qty * bps/10000 * 2`, so bps must be
    # PER-SIDE: bps = 0.47 * 10000 / (2 * 1 oz * $3000) = 0.7833...
    assert ek['taker_fee_bps'] == pytest.approx(0.783333333, abs=1e-6)
    # ...and the realised round trip is exactly the $0.47 live quote.
    charged = 3000.0 * 1.0 * (ek['taker_fee_bps'] / 10000.0) * 2.0
    assert charged == pytest.approx(0.47, abs=1e-6)


# ─── Structural SL/TP (NB82 + NB83) ──────────────────────────
#
# The structural geometry lives in the ENGINE
# (``bt/_engine/vp_multi_target_engine.py::compute_structural_sl``),
# which is the only code path that computes it. A duplicate copy
# used to live in ``bt/structural_sl_tp.py`` and was DELETED in the
# 2026-10-22 dead-code sweep — it had zero production callers and
# its geometry had already diverged from the engine's.


def test_structural_sl_long():
    """Long trade: SL below zone_bottom."""
    from bt._engine.vp_multi_target_engine import compute_structural_sl
    sl_price, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=None, t_ns=0,
        sl_mode='fvg_only', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
    )
    assert sl_price < 3000.0, "SL must be below entry for a long"
    assert sl_ref == "zone_edge"


def test_structural_sl_short():
    """Short trade: SL above zone_top."""
    from bt._engine.vp_multi_target_engine import compute_structural_sl
    sl_price, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=-1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=None, t_ns=0,
        sl_mode='fvg_only', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
    )
    assert sl_price > 3000.0, "SL must be above entry for a short"
    assert sl_ref == "zone_edge"


# ─── SL_MODE_HVN_LVN (2026-10-22) ────────────────────────────────
#
# The stop is anchored to the Nth HVN shelf on the LOSS side, with a
# hard minimum distance floor. These tests pin the GEOMETRY. They do
# NOT claim the geometry is profitable — see the beta-proxy section
# of AGENTS.md.

def _synthetic_vp(shelves):
    """Build a one-day VolumeProfileSet with the given HVN (lo, hi)."""
    import numpy as np
    from bt._engine.volume_profile import HVNZone, SessionVP, VolumeProfileSet

    lo_ns, hi_ns = 1_700_000_000_000_000_000, 1_700_086_400_000_000_000
    zones = [
        HVNZone(lo=lo, hi=hi, mid=(lo + hi) / 2.0, vol=100.0,
                session_start_ns=lo_ns, session_end_ns=hi_ns)
        for lo, hi in shelves
    ]
    empty = np.array([])
    svp = SessionVP(
        session_start_ns=lo_ns, session_end_ns=hi_ns,
        poc=3000.0, vah=3100.0, val=2900.0, total_vol=1000.0,
        hvn_zones=zones, lvn_zones=[],
        row_prices_lo=empty, row_prices_hi=empty, row_vols=empty,
    )
    return VolumeProfileSet(
        file_stem="test", daily_vp=[svp], weekly_vp=[svp],
        naked_pocs=[], build_ms={},
    )


_T_NS = 1_700_000_000_000_000_000 + 3_600_000_000_000
_SHELVES = [(2985.0, 2986.0), (2970.0, 2971.0), (2950.0, 2951.0)]


def test_sl_hvn_lvn_picks_nth_shelf_behind_entry():
    """Long: n=1 → nearest shelf below, n=2 → second, n=3 → third."""
    from bt._engine.vp_multi_target_engine import compute_structural_sl

    dists = []
    for n in (1, 2, 3):
        sl_price, sl_ref = compute_structural_sl(
            entry_price=3000.0, direction=1,
            zone_low=3000.5, zone_high=3001.0,
            vp_set=_synthetic_vp(_SHELVES), t_ns=_T_NS,
            sl_mode='hvn_lvn', sl_buffer_usd=0.05,
            poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
            zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
            sl_nth_hvn_n=n, sl_min_usd=2.5,
        )
        assert sl_ref == f"hvn_n{n}"
        assert sl_price < 3000.0, "long SL must be below entry"
        # shelf lo minus the 0.10 hvn_buffer
        assert sl_price == pytest.approx({1: 2984.90, 2: 2969.90,
                                          3: 2949.90}[n])
        dists.append(3000.0 - sl_price)
    assert dists == sorted(dists), "n must walk successively farther out"


def test_sl_hvn_lvn_skips_shelf_inside_the_zone():
    """A shelf overlapping the entry zone can't anchor the stop."""
    from bt._engine.vp_multi_target_engine import compute_structural_sl

    sl_price, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=_synthetic_vp([(2999.8, 3000.4)] + _SHELVES), t_ns=_T_NS,
        sl_mode='hvn_lvn', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
        sl_nth_hvn_n=1, sl_min_usd=2.5,
    )
    assert sl_ref == "hvn_n1"
    assert sl_price == pytest.approx(2984.90), (
        "the in-zone shelf must be skipped, not used as the stop"
    )


def test_sl_hvn_lvn_falls_back_to_zone_edge():
    """No shelf on the loss side → the zone-edge stop, not a crash."""
    from bt._engine.vp_multi_target_engine import compute_structural_sl

    sl_price, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=_synthetic_vp([(3200.0, 3201.0)]), t_ns=_T_NS,
        sl_mode='hvn_lvn', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
        sl_nth_hvn_n=1, sl_min_usd=2.5,
    )
    assert sl_ref == "zone_edge"
    # 3000.5 - 40*0.5 = 2980.50
    assert sl_price == pytest.approx(2980.50)


def test_sl_hvn_lvn_min_floor_only_ever_loosens():
    """The $2.50 floor pushes a too-tight stop OUT, never pulls it in."""
    from bt._engine.vp_multi_target_engine import (
        _apply_sl_min_floor, compute_structural_sl,
    )

    # A shelf $1.1 below entry is inside the floor.
    sl_price, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=_synthetic_vp([(2998.2, 2998.9)]), t_ns=_T_NS,
        sl_mode='hvn_lvn', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
        sl_nth_hvn_n=1, sl_min_usd=2.5,
    )
    assert sl_ref == "hvn_n1+min"
    assert sl_price == pytest.approx(2997.50)

    # A stop already wider than the floor is returned untouched.
    for direction, price, entry in (
        (1, 2990.0, 3000.0), (-1, 3010.0, 3000.0),
    ):
        out, ref = _apply_sl_min_floor(
            sl_price=price, entry_price=entry, direction=direction,
            sl_min_usd=2.5,
        )
        assert out == pytest.approx(price)
        assert ref == "", "a stop wider than the floor must not be touched"

    # 0.0 disables the floor entirely.
    out, ref = _apply_sl_min_floor(
        sl_price=2999.99, entry_price=3000.0, direction=1, sl_min_usd=0.0)
    assert out == pytest.approx(2999.99)
    assert ref == ""


def test_sl_max_ceiling_only_ever_tightens():
    """The ceiling pulls a too-wide stop IN, never pushes it out.

    This is the INVERSE of ``_apply_sl_min_floor`` and it was
    missing until 2026-10-24. Without it there was no way to reach
    the user's "$2.50 stop" target, because the floor can only
    loosen.
    """
    from bt._engine.vp_multi_target_engine import (
        _apply_sl_max_ceiling, compute_structural_sl,
    )

    # A shelf $16 below entry is far outside the $2.50 ceiling.
    sl_price, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=_synthetic_vp([(2983.9, 2984.5)]), t_ns=_T_NS,
        sl_mode='hvn_lvn', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
        sl_nth_hvn_n=1, sl_min_usd=2.5, sl_max_usd=2.5,
    )
    assert sl_ref == "hvn_n1+cap"
    assert sl_price == pytest.approx(2997.50)

    # A stop already tighter than the ceiling is returned untouched.
    for direction, price, entry in (
        (1, 2999.0, 3000.0), (-1, 3001.0, 3000.0),
    ):
        out, ref = _apply_sl_max_ceiling(
            sl_price=price, entry_price=entry, direction=direction,
            sl_max_usd=2.5,
        )
        assert out == pytest.approx(price)
        assert ref == "", "a stop tighter than the ceiling must not be touched"

    # 0.0 disables the ceiling entirely.
    out, ref = _apply_sl_max_ceiling(
        sl_price=2900.0, entry_price=3000.0, direction=1, sl_max_usd=0.0)
    assert out == pytest.approx(2900.0)
    assert ref == ""


def test_sl_floor_and_ceiling_clamp_into_the_band():
    """With both set, the effective stop distance is in [min, max]."""
    from bt._engine.vp_multi_target_engine import (
        _apply_sl_max_ceiling, _apply_sl_min_floor,
    )

    def _dist(sl_price, entry=3000.0, direction=1):
        p, _ = _apply_sl_min_floor(
            sl_price=sl_price, entry_price=entry, direction=direction,
            sl_min_usd=2.5)
        p, _ = _apply_sl_max_ceiling(
            sl_price=p, entry_price=entry, direction=direction,
            sl_max_usd=5.0)
        return abs(entry - p)

    # Too tight -> floor pushes out to 2.50.
    assert _dist(2999.0) == pytest.approx(2.50)
    # Too wide -> ceiling pulls in to 5.00.
    assert _dist(2900.0) == pytest.approx(5.00)
    # In band -> untouched.
    assert _dist(2996.0) == pytest.approx(4.00)


def test_every_sl_mode_honours_the_floor_and_the_ceiling():
    """ALL three ``sl_mode`` branches must apply BOTH the floor and the ceiling.

    Regression, 2026-10-24. The ``fvg_or_poc`` branch returned its raw
    anchor level early and never called ``_apply_sl_min_floor`` or
    ``_apply_sl_max_ceiling`` at all, so it ignored BOTH bounds. The
    ``fvg_only`` branch applied the ceiling but not the floor.

    Concretely, with entry 3000.0, zone 3000.5-3001.0, zone_mult 40
    (raw zone-edge stop = 2980.50, i.e. $19.50 away) and a $2.50
    ceiling:

        mode          floor  ceiling  -> SL dist   expected
        hvn_lvn       2.50   2.50    ->    2.50     honoured
        fvg_only      2.50   2.50    ->    2.50     honoured
        fvg_or_poc    2.50   2.50    ->    2.50     honoured

    Before the fix ``fvg_or_poc`` returned $19.50 — a $39 stop in a
    sweep cell whose sibling modes were all pinned at $2.50. The
    canonical mode is ``hvn_lvn`` so the canonical run was never
    affected, but ``sl_mode`` IS on the sweep allowlist, so the cell
    was reachable and wrong.
    """
    from bt._engine.vp_multi_target_engine import compute_structural_sl

    for mode in ('hvn_lvn', 'fvg_only', 'fvg_or_poc'):
        for direction, entry, zone_low, zone_high in (
            (1, 3000.0, 3000.5, 3001.0),
            (-1, 3000.0, 2999.0, 2999.5),
        ):
            sl_price, sl_ref = compute_structural_sl(
                entry_price=entry, direction=direction,
                zone_low=zone_low, zone_high=zone_high,
                vp_set=_synthetic_vp(_SHELVES), t_ns=_T_NS,
                sl_mode=mode, sl_buffer_usd=0.05,
                poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
                zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
                sl_nth_hvn_n=1, sl_min_usd=2.5, sl_max_usd=2.5,
            )
            dist = abs(entry - sl_price)
            assert dist == pytest.approx(2.50), (
                f"{mode} (dir={direction}) produced a ${dist:.2f} stop; "
                f"the $2.50 ceiling was not applied (ref={sl_ref})"
            )

    # The ref must SHOW that the ceiling bound, so per-trade
    # diagnostics can see it.
    _, sl_ref = compute_structural_sl(
        entry_price=3000.0, direction=1,
        zone_low=3000.5, zone_high=3001.0,
        vp_set=_synthetic_vp(_SHELVES), t_ns=_T_NS,
        sl_mode='fvg_or_poc', sl_buffer_usd=0.05,
        poc_buffer_usd=0.20, hvn_buffer_usd=0.10,
        zone_width=0.5, sl_zone_mult=40.0, sl_atr_mult=0.0,
        sl_nth_hvn_n=1, sl_min_usd=2.5, sl_max_usd=2.5,
    )
    assert "+cap" in sl_ref, f"the ceiling must be visible in the ref: {sl_ref}"


def test_sl_max_ceiling_is_a_live_engine_knob():
    """The knob must reach the engine, not just exist in config."""
    ek = build_engine_kwargs(sl_max_usd=3.75)
    assert ek['sl_max_usd'] == 3.75


def test_sl_and_tp_have_independent_nth_hvn():
    """The TP reaches for a distant magnet; the stop respects the near one.

    Sharing one knob would drag the stop out to the TP's shelf, which
    is the opposite of the intent — this is the whole reason
    ``sl_nth_hvn_n`` exists as a separate field.
    """
    ek = build_engine_kwargs()
    assert ek['nth_hvn_n'] == 3, "TP targets the 3rd shelf"
    assert ek['sl_nth_hvn_n'] == 1, "SL anchors on the 1st"
    assert ek['nth_hvn_n'] != ek['sl_nth_hvn_n']

    ek2 = build_engine_kwargs(sl_nth_hvn_n=2)
    assert ek2['sl_nth_hvn_n'] == 2
    assert ek2['nth_hvn_n'] == 3, "varying the SL must not move the TP"


def test_sl_hvn_lvn_is_the_canonical_mode():
    ek = build_engine_kwargs()
    assert ek['sl_mode'] == 'hvn_lvn'
    assert ek['sl_min_usd'] == pytest.approx(2.50)


def test_unknown_sl_mode_raises():
    """A typo in a sweep grid must fail loudly, not silently no-op.

    ``compute_structural_sl`` falls through unknown modes to the
    zone-edge stop, which makes a typo produce a plausible number for
    a mode that never ran.
    """
    import inspect
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest

    src = inspect.getsource(run_vp_sniper_backtest)
    assert "sl_mode=" in src and "SL_MODES" in src, (
        "the engine must validate sl_mode against SL_MODES"
    )
    from bt._engine.vp_multi_target_config import SL_MODE_HVN_LVN, SL_MODES
    assert SL_MODE_HVN_LVN in SL_MODES


# ─── Risk math ──────────────────────────────────────────────────


def test_pnl_per_usd_move_long():
    """At canonical sizing, +$1 move = +$1 P&L (long)."""
    assert pnl_per_usd_move(lots=0.01, contract_size=100.0, direction='long') == pytest.approx(1.0)


def test_pnl_per_usd_move_short():
    """At canonical sizing, +$1 favorable (down) move = +$1 P&L (short)."""
    assert pnl_per_usd_move(lots=0.01, contract_size=100.0, direction='short') == pytest.approx(-1.0)


def test_max_lots_for_exposure():
    """Max lots keeps margin ≤ 50% of equity at 100× leverage."""
    # 1 oz × $3000 / 100 = $30 margin. Equity $1000, cap 50% = $500.
    # Max oz = 500*100/3000 = 16.67 oz = 0.1667 lots at contract_size=100.
    max_lots = max_lots_for_exposure(
        contract_size=100.0, spot=3000.0, leverage=100.0,
        account_equity_usd=1000.0,
        max_book_exposure_pct=0.5,
        base_lots=0.01,
    )
    assert max_lots == pytest.approx(0.1667, abs=0.001)


# ─── Kelly (NB81) ───────────────────────────────────────────────


def test_kelly_positive_edge():
    """55% win rate, 1:1 win/loss ratio → f* = 0.10."""
    s = size_lots(pnl_series_usd=[1.0]*55 + [-1.0]*45)
    assert s.f_star == pytest.approx(0.10, abs=0.01)
    assert s.recommended_lots > 0.01  # > canonical base


def test_kelly_insufficient_data():
    """< 30 trades → recommended lots = base, insufficient=True."""
    s = size_lots(pnl_series_usd=[1.0]*15 + [-1.0]*10)
    assert s.insufficient_data is True
    assert s.recommended_lots == 0.01


# ─── Vol-state gate (NB85) ─────────────────────────────────────
#
# ``bt.vol_state.gate`` was DELETED in the 2026-10-22 dead-code
# sweep: it had zero production callers, and its "active" closure
# never actually compared the velocity against the floor (it always
# returned True). The engine's own ``_vol_state_pass`` is the only
# gate that can fail, and the canonical knobs still reach it via
# ``build_engine_kwargs(vol_state_min_vel_5m=...)``.


# ─── Swing cap (NB72) ───────────────────────────────────────────
#
# The NB72 cap geometry lives in ONE place: the engine's
# ``_apply_swing_cap`` in ``bt/_engine/vp_multi_target_engine.py``.
# The public ``apply_cap_to_price`` / ``cap_distance`` helpers were a
# SECOND implementation of the same geometry and were removed on
# 2026-10-24 (they had zero production callers — ``apply_cap_to_price``
# was called only by a test). This section is now covered by the
# engine-side tests in ``tests/test_regressions.py``.


# ─── Engine kwargs allowlist ───────────────────────────────────


def test_engine_kwargs_illegal_rejected():
    with pytest.raises(KeyError):
        build_engine_kwargs(bogus_field=42)


def test_engine_kwargs_legal_keys():
    ek = build_engine_kwargs(
        sl_zone_mult=40,
        tp_zone_mult_fallback=200,
        taker_fee_bps=2.0,
    )
    assert ek['sl_zone_mult'] == 40
    assert ek['tp_zone_mult_fallback'] == 200
    assert ek['taker_fee_bps'] == 2.0


def test_engine_kwargs_rejects_forex_fee_knobs():
    """The stripped forex fee knobs are no longer a sweep surface."""
    with pytest.raises(KeyError):
        build_engine_kwargs(commission_per_lot_usd=7.0)
    with pytest.raises(KeyError):
        build_engine_kwargs(spread_per_oz_usd=0.40)