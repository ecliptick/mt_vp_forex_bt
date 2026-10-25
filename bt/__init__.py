"""sniper_mt_vp_forex_bt_only — flat forex backtest engine.

A lean, sweep-only backtest repo for the v22 SNIPER recipe on
XAUUSD (MT4 MT5 convention). The engine source is DUPLICATED into
``bt/_engine/`` — there are no cross-repo imports.

Architecture
============

* **Flat layout** — every Python module lives directly under
  ``bt/``. No nested packages. Mirrors ``sniper_mt_vp_bt_only``.
* **One engine** — the VP multi-target engine
  (``run_vp_sniper_backtest``) is the ONLY engine in production
  and research. The bar engine (``run_ict_backtest``) and its
  precompute cache were DELETED on 2026-10-24: both had zero
  production callers, and the recipe object they read was never
  read by the VP engine at all.
* **One source of truth for config** — every canonical value is a
  module constant in :mod:`bt.optimal_config`. There is no recipe
  dataclass, no upstream re-export, and no second copy of any knob.
  The three materialisers (``build_engine_kwargs``,
  ``build_vp_kwargs``, ``build_detector_kwargs``) are the only way
  to produce a kwargs dict, and they raise ``KeyError`` on an
  unknown key so a typo can never silently become a no-op.
* **No defaults policy** — the dataclasses that remain
  (``MarginAndCost``, ``ExposureSnapshot``, ``KellySizing``) are
  required-field.

Live-spread model
=================

There is exactly ONE spread value: the **live MT4 quote on XAUUSD**:

* round-trip spread: ``spread_per_oz_usd = 0.40``
* round-trip commission: ``commission_per_lot_usd = 7.0``

No "live overlay" concept. The historical $0.19 backtest baseline
in the upstream recipe is gone from this repo. Per-trade round-
trip cost at canonical ``lots=0.01, contract_size=100`` (1 oz
notional):

    0.01 x $7 commission + 1 x $0.40 spread = $0.07 + $0.40 = $0.47

Leverage
========

The forex MT4 convention is ``lots x contract_size``:
``lots=0.01`` and ``contract_size=100.0`` -> 1.0 oz
notional per trade (~$3,000 at XAUUSD=$3,000/oz).
100x leverage means $30 margin per 1 oz notional.
"""
from __future__ import annotations

# ── Engine (duplicated from ict_forex_research/src/ + ict_crypto_research/src/) ──
from bt._engine.ict_signals import (  # type: ignore  # noqa: E402
    FvgZone,
    IctSeries,
    compute_simple_atr,
    detect_fvg,
    fvg_retest_signals,
)
from bt._engine.ict_strategy import (  # type: ignore  # noqa: E402
    PendingSignal,
)
from bt._engine.market_structure import (  # type: ignore  # noqa: E402
    BosChochMemory,
    BreakEvent,
    OrderBlock,
    StructureEvent,
    StructureState,
    SwingPoint,
    annotate_choch_plus,
    detect_market_structure,
    structure_conviction,
)
from bt._engine.run_report import (  # type: ignore  # noqa: E402
    append_run_report,
)

# ── Local extensions (single source of truth) ─────────────────────
#
# DEAD-CODE SWEEP (2026-10-22): ``params``, ``vp_config`` and
# ``structural_sl_tp`` were removed — they were either phantom
# (never constructed in production) or divergent duplicates of the
# engine's own geometry. See the dead-code table in AGENTS.md.
#
# CONFIG CONSOLIDATION (2026-10-24): ``optimal_params``,
# ``OPTIMAL_RECIPE_VERSION``, ``run_ict_backtest``, ``Trade`` and
# ``IctBacktestResult`` were removed along with the bar engine and
# the recipe dataclass they existed to serve.
from bt import optimal_config as optimal_mod    # noqa: E402
from bt import volume_profile as vp_mod         # noqa: E402
from bt import vol_state as vol_state_mod       # noqa: E402
from bt import swing_cap as swing_cap_mod       # noqa: E402
from bt import vp_engine as vp_engine_mod       # noqa: E402
from bt import risk as risk_mod                 # noqa: E402
from bt import kelly as kelly_mod               # noqa: E402
from bt import leverage as leverage_mod         # noqa: E402


__all__ = [
    # ── Detector primitives ───────────────────────────────────
    "FvgZone",
    "IctSeries",
    "compute_simple_atr",
    "detect_fvg",
    "fvg_retest_signals",
    "StructureEvent",
    "StructureState",
    "SwingPoint",
    "BreakEvent",
    "OrderBlock",
    "BosChochMemory",
    "detect_market_structure",
    "structure_conviction",
    "annotate_choch_plus",
    # ── Trade / signal primitives ─────────────────────────────
    "PendingSignal",
    # ── Run log ───────────────────────────────────────────────
    "append_run_report",
    # ── Local submodules (re-export for direct import) ────────
    "optimal_mod",
    "vp_mod",
    "vol_state_mod",
    "swing_cap_mod",
    "vp_engine_mod",
    "risk_mod",
    "kelly_mod",
    "leverage_mod",
]
