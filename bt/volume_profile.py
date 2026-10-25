"""Daily volume profile — public re-export surface.

The implementation lives in ``bt/_engine/volume_profile.py`` (the
duplicated engine source). This module is the **public import
surface** for it.

Source
------

``ict_crypto_research/src/volume_profile.py`` — the daily VP
rolling window the multi-target engine consumes (NB80).
Duplicated verbatim into ``bt/_engine/volume_profile.py`` so
this repo is standalone.

CONFIG CONSOLIDATION (2026-10-24)
=================================

This file used to define a SECOND, PARALLEL volume-profile knob set
(``DEFAULT_VP_LOOKBACK_SECS``, ``DEFAULT_VP_BUCKET_USD``,
``DEFAULT_VP_MIN_SHELF_USD``, ``DEFAULT_VP_TOPN_HVNS``,
``DEFAULT_VP_PROFILE_MODE``, ``DEFAULT_VP_INCLUDE_SESSION``,
``DEFAULT_VP_SESSION_BREAKS``) plus a ``make_rolling_vp()`` factory
that claimed to build the daily VP.

Both are deleted:

* **The constants were a second source of truth** with DIFFERENT
  names AND DIFFERENT values from the canonical ``VP_*`` set in
  :mod:`bt.optimal_config`. For example the canonical profile is
  ``vp_rows=200`` / ``vp_min_zone_usd=0.50``, while this file
  described a profile as ``bucket_usd=0.50`` / ``min_shelf_usd=0.20``
  — a vocabulary that does not exist anywhere in the engine.
* **``make_rolling_vp()`` was BROKEN.** It forwarded
  ``lookback_secs`` / ``bucket_usd`` / ``min_shelf_usd`` /
  ``top_n_hvns`` / ``profile_mode`` / ``include_session`` /
  ``session_breaks`` to ``build_volume_profiles``, which accepts
  NONE of them:

      TypeError: build_volume_profiles() got an unexpected keyword
      argument 'lookback_secs'

  It had zero callers, so the crash never fired.

The canonical profile shape is materialised by
:func:`bt.optimal_config.build_vp_kwargs` and consumed by
``build_volume_profiles`` directly.

The re-exports below ARE used, so they stay.
"""
from __future__ import annotations

# Duplicated source from ict_crypto_research/src/volume_profile.py
# — standalone copy in this repo, no shadow-package workaround.
from bt._engine.volume_profile import (  # type: ignore  # noqa: E402
    HVNZone,
    LVNZone,
    SessionVP,
    VolumeProfileSet,
    build_volume_profiles,
    get_or_build_vp,
    current_daily_vp,
    current_weekly_vp,
    naked_poc_targets,
    hvn_zones_near,
    lvn_zones_near,
    poc_distance_usd,
    resample_1s_to_1m,
)

__all__ = [
    'HVNZone',
    'LVNZone',
    'SessionVP',
    'VolumeProfileSet',
    'build_volume_profiles',
    'get_or_build_vp',
    'current_daily_vp',
    'current_weekly_vp',
    'naked_poc_targets',
    'hvn_zones_near',
    'lvn_zones_near',
    'poc_distance_usd',
    'resample_1s_to_1m',
]
