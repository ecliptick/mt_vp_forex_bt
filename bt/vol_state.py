"""Vol-state gate (NB85 + forward-look fix) — public re-export surface.

The implementation lives in ``bt/_engine/vol_state.py`` (the
duplicated engine source). This module is the **public import
surface** for it.

Source
------

``ict_crypto_research/src/vol_state.py`` NB85 (2026-09-29).
The original NB85 finding (BTC VP-MT, CALMAR 254 with vel5)
was found to be contaminated by a **forward-looking velocity**
computation on 2026-10-08: the original ``vel_5m`` was
``P_{t-w} - P_t`` (effectively negative-look-ahead), so the
NB85 "win" was the strategy peaking at the bar BEFORE the
state it claimed to gate on.

Fix (applied 2026-10-09)
-----

Shift the denominator back so the velocity is computed strictly
backward in time. ``vel_5m = (P_t - P_{t-300}) / P_{t-300}`` —
reads the prior 5m of price movement relative to its starting
value. There is no negative shift; the state at time ``t`` is
purely a function of the bars at ``t`` and ``t-w``.

Re-validation of the BTC NB85 win is PENDING as of
2026-10-13 — the look-ahead fix invalidated the original
finding, but the corrected gate has not been swept end-to-end.
The canonical config keeps ``vol_state_min_vel_5m=0.0``
(INACTIVE) on both crypto and forex until the BTC re-validation
lands. Sweep drivers can toggle it via
``build_engine_kwargs(vol_state_min_vel_5m=...)``.

DEAD-CODE SWEEP (2026-10-22)
============================

The previous version of this file also defined ``gate()``. It was
**dead**:

* the engine has its own ``_vol_state_pass`` (in
  ``bt/_engine/vp_multi_target_engine.py``), which is the only code
  path that evaluates the gate;
* when the gate was INACTIVE (the canonical case — both floors are
  0.0) ``gate()`` returned a constant-``True`` lambda that ignored
  its arguments entirely;
* when it was ACTIVE, the returned closure STILL ignored the price
  series and only checked the warmup window — it never actually
  compared ``vel_5m`` against the floor.

A "gate" that cannot fail is worse than no gate: it looks like a
guard in a code review. It is removed. The canonical knobs
(``vol_state_min_vel_5m`` / ``vol_state_min_vel_15m``) still reach
the engine through ``build_engine_kwargs``.

CONFIG CONSOLIDATION (2026-10-24)
=================================

``DEFAULT_VOL_STATE_MIN_VEL_5M`` / ``DEFAULT_VOL_STATE_MIN_VEL_15M``
were a second copy of the canonical values in
:mod:`bt.optimal_config` (``VOL_STATE_MIN_VEL_5M`` /
``VOL_STATE_MIN_VEL_15M``). Neither was read by anything outside
this module. They are deleted.

``VEL_5M_WINDOW_SECS`` / ``VEL_15M_WINDOW_SECS`` describe the
velocity measure windows, which are properties of the ENGINE's
``build_vol_state`` implementation, not of this repo's config — they
stay here as documentation of the measure definition.

The re-exports below ARE used, so they stay.
"""
from __future__ import annotations

# Duplicated source from ict_crypto_research/src/vol_state.py —
# standalone copy in this repo, no shadow-package workaround.
from bt._engine.vol_state import (  # type: ignore  # noqa: E402
    VolState,
    build_vol_state,
    VOL_MEASURES,
)

# The velocity measures ``vel_5m`` and ``vel_15m`` are names
# inside ``VolState.series`` (arrays indexed by measure name),
# NOT separate functions. The forward-look fix in NB85 was
# applied to ``build_vol_state`` so the velocity is computed
# strictly backward in time (``P_t - P_{t-w}``).
VEL_5M_WINDOW_SECS = 300       # 5min
VEL_15M_WINDOW_SECS = 900      # 15min

__all__ = [
    'VolState',
    'build_vol_state',
    'VOL_MEASURES',
    'VEL_5M_WINDOW_SECS',
    'VEL_15M_WINDOW_SECS',
]
