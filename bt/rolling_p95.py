"""Rolling p95 peak-to-trough TP cap — public re-export surface.

The implementation lives in ``bt/_engine/rolling_p95.py`` (the
duplicated engine source). This module is the **public import
surface** for it.

DEAD-CODE SWEEP (2026-10-22)
=============================

The previous version of this file ALSO defined ``cap_distance`` and
``apply_cap_to_price``. Both had **zero callers** anywhere in the
repo:

* the engine has its own ``_apply_p95_cap`` (in
  ``bt/_engine/vp_multi_target_engine.py``), which is the only code
  path that applies the cap to a TP;
* no sweep driver called the scalar helpers.

Two divergent copies of the same geometry is exactly how the
pre-D1 fee bug and the stale ``tp_max_distance_usd = 6.0`` constant
survived. They are removed.

CONFIG CONSOLIDATION (2026-10-24)
=================================

The six ``DEFAULT_*`` constants this module used to define were a
second copy of the canonical p95 values in :mod:`bt.optimal_config`
(``TP_P95_LOOKBACK_SECS``, ``TP_P95_INNER_SECS``, ``TP_P95_Q``,
``TP_P95_MULT``, and the seed pair). None of them was read by
anything outside this module. They are deleted — the canonical
values are materialised by :func:`bt.optimal_config.build_p95_kwargs`.

The re-exports below ARE used (``build_rolling_p95_from_bars`` by the
sweep driver and the regression tests; ``save``/``load`` by the
on-disk cap cache), so they stay.
"""
from __future__ import annotations

# Duplicated source from this repo (no upstream import — pure
# standalone copy).
from bt._engine.rolling_p95 import (  # type: ignore  # noqa: E402
    RollingP95Cap,
    build_rolling_p95_from_bars,
    make_live_rolling_p95,
    update_p95,
    save_rolling_p95,
    load_rolling_p95,
)

__all__ = [
    'RollingP95Cap',
    'build_rolling_p95_from_bars',
    'make_live_rolling_p95',
    'update_p95',
    'save_rolling_p95',
    'load_rolling_p95',
]
