"""Parity checks: ensure the engine wrapper behaves the same
as the underlying crypto engine when the inputs match.

These tests run only if the engine is importable and a sample
dataset is available. Skip if not.
"""
from __future__ import annotations

import sys

import pytest


# Skip these if the crypto engine isn't importable
try:
    from bt.vp_engine import run_vp_sniper_backtest_forex
    _engine_available = True
except ImportError:
    _engine_available = False


pytestmark = pytest.mark.skipif(
    not _engine_available, reason="multi-target engine not importable"
)


def test_sweep_driver_smoke():
    """The sweep driver instantiates without error."""
    from runtime.sweeps import cached_sweep
    assert cached_sweep is not None
    assert hasattr(cached_sweep, "run_cell")
    assert hasattr(cached_sweep, "compute_metrics")


def test_knob_allowlist_split():
    """The sweep driver rejects illegal knobs.

    2026-10-24 (config consolidation): the RECIPE surface is GONE.
    ``_OPTIMAL_OVERRIDES_ALLOWED`` described knobs that were routed to
    a recipe object the VP engine never read — every one of them was a
    silent no-op. The canonical knob now lives in the ENGINE allowlist.
    """
    from bt.optimal_config import (
        _DETECTOR_OVERRIDES_ALLOWED,
        _ENGINE_OVERRIDES_ALLOWED,
        _P95_OVERRIDES_ALLOWED,
        _STRUCTURE_OVERRIDES_ALLOWED,
        _SWING_OVERRIDES_ALLOWED,
        _VP_OVERRIDES_ALLOWED,
    )
    # Engine knobs (the canonical surface)
    assert 'sl_zone_mult' in _ENGINE_OVERRIDES_ALLOWED
    assert 'nth_hvn_n' in _ENGINE_OVERRIDES_ALLOWED
    assert 'vol_state_min_range_norm' in _ENGINE_OVERRIDES_ALLOWED
    # Detector knobs
    assert 'fvg_min_zone_usd' in _DETECTOR_OVERRIDES_ALLOWED
    assert 'resample_to_n_secs' in _DETECTOR_OVERRIDES_ALLOWED
    # VP / swing / p95 / structure build knobs
    assert 'vp_rows' in _VP_OVERRIDES_ALLOWED
    assert 'swing_cap_pct' in _SWING_OVERRIDES_ALLOWED
    # ``tp_p95_mult`` moved to the ENGINE allowlist in the 2026-10-24
    # config consolidation: the cap OBJECT's own multiplier is
    # cancelled out by the engine, so the only live multiplier is the
    # engine kwarg. It must NOT be on the P95 (cap-build) allowlist —
    # putting it there would advertise a knob that does nothing.
    assert 'tp_p95_mult' in _ENGINE_OVERRIDES_ALLOWED
    assert 'tp_p95_mult' not in _P95_OVERRIDES_ALLOWED
    assert 'structure_pivot_len' in _STRUCTURE_OVERRIDES_ALLOWED
    # The forex commission/spread knobs are NOT engine kwargs — they are
    # stripped by bt.vp_engine before the engine call, so listing them
    # made them a silent no-op knob. See test_engine_kwargs_carries_live_fees_via_taker_bps.
    assert 'commission_per_lot_usd' not in _ENGINE_OVERRIDES_ALLOWED
    assert 'spread_per_oz_usd' not in _ENGINE_OVERRIDES_ALLOWED
    assert 'taker_fee_bps' in _ENGINE_OVERRIDES_ALLOWED


def test_engine_kwargs_keys_all_reach_the_engine():
    """Every emitted engine kwarg is an actual parameter of the engine.

    This is the guard that would have caught the forex fee knobs: any
    key in ``build_engine_kwargs()`` that the engine does not accept
    is a dead knob that a sweep would silently no-op.
    """
    import inspect

    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
    from bt.optimal_config import build_engine_kwargs

    sig = set(inspect.signature(run_vp_sniper_backtest).parameters)
    emitted = set(build_engine_kwargs())
    # swing_cap / p95_cap are optional and default to None, so they
    # only appear when a caller supplies them — they ARE valid params.
    dead = {k for k in emitted if k not in sig}
    assert not dead, f"engine_kwargs emits non-engine (dead) knobs: {sorted(dead)}"