"""Daily-swing TP cap (NB72) — public re-export surface.

The implementation lives in ``bt/_engine/swing_cap.py`` (the
duplicated engine source). This module is the **public import
surface** for it.

THE CANONICAL SWING-CAP VALUES LIVE IN ``bt/optimal_config.py``
=============================================================

``SWING_CAP_PCT`` (0.25) and ``SWING_LOOKBACK_SECS`` (86_400) are
module constants in :mod:`bt.optimal_config` — the repo's single
source of truth. They are materialised into the builder kwargs by
:func:`bt.optimal_config.build_swing_kwargs`.

This file used to define its own ``DEFAULT_SWING_CAP_PCT = 0.7`` and
``DEFAULT_SWING_LOOKBACK_SECS = 86_400``. The 0.7 was a **direct
contradiction** of the canonical 0.25, and neither constant was read
by anything outside this module. Both are deleted.

The scalar helpers are deleted too
==================================

``cap_distance`` and ``apply_cap_to_price`` were a SECOND
implementation of the NB72 cap geometry:

* the engine has its own ``_apply_swing_cap`` (in
  ``bt/_engine/vp_multi_target_engine.py``), which is the only code
  path that applies the cap to a TP;
* ``cap_distance`` had ZERO callers anywhere in the repo;
* ``apply_cap_to_price`` was called only by a test.

Two copies of the same geometry is exactly how the pre-D1 fee bug
and the stale ``tp_max_distance_usd = 6.0`` constant survived. Both
helpers are removed.

The re-exports below ARE used (``build_daily_swing`` by the sweep
driver and the regression tests; ``save``/``load`` by the on-disk
cap cache), so they stay.
"""
from __future__ import annotations

# Duplicated source from ict_crypto_research/src/swing_cap.py —
# standalone copy in this repo, no shadow-package workaround.
from bt._engine.swing_cap import (  # type: ignore  # noqa: E402
    DailySwingCap,
    build_daily_swing,
    make_live_swing_cap,
    update_swing,
    save_daily_swing,
    load_daily_swing,
)

__all__ = [
    'DailySwingCap',
    'build_daily_swing',
    'make_live_swing_cap',
    'update_swing',
    'save_daily_swing',
    'load_daily_swing',
]
