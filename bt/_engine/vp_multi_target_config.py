"""Mode-name vocabulary for the VP multi-target strategy.

This module holds the **string vocabulary** the engine switches on
(``sl_mode``, ``tp_mode``, ``lvn_mode``, the fill models) plus the
tuples the engine validates against.

IT IS NOT A CONFIG SOURCE
==========================

No knob VALUE lives here. Every canonical value is a module constant
in :mod:`bt.optimal_config`. This file only names the legal options
for the mode-valued knobs, so the engine can reject a typo with
``ValueError`` instead of silently taking a default branch.

Specifically, the following are canonical and live in
``bt/optimal_config.py``:

* ``SL_MODE`` ('hvn_lvn') — the stop anchor
* ``TP_MODE`` ('multi_target') — the TP branch
* ``LVN_MODE`` ('skip') — the shelf-edge offset behaviour
* ``ENTRY_MODE`` ('dual') — immediate + sniper co-fire
* ``ENTRY_FILL_MODEL`` ('touch') — resting entry order fill
* ``FILL_MODEL`` ('pessimistic') — ambiguous-bar resolution

REMOVED (2026-10-24)
====================

* ``LVN_SKIP_USD = 10.0`` — a **contradictory duplicate** of the
  canonical ``LVN_SKIP_USD = 0.30`` in ``bt/optimal_config.py``
  (33x apart). It was imported by the engine and never read, so it
  was a live-looking second source of truth for a knob that has a
  real one. Deleted.
* ``TP_MAX_DISTANCE_USD`` — a BTC-scale literal (1500.0) that the
  SL's shelf-search radius used to read. The radius is now DERIVED
  per bar from the live caps. Deleted 2026-10-22.
* The unused mode constants (``SL_MODE_FVG_OR_POC``,
  ``TP_MODES``, ``LVN_MODE_THROUGH``, ``LVN_MODES``,
  ``ENTRY_MODE_SNIPER`` / ``ENTRY_MODE_IMMEDIATE`` /
  ``ENTRY_MODE_DUAL`` / ``ENTRY_MODES``) — the engine compares
  ``entry_mode`` and ``tp2_mode`` against string literals, and
  ``fvg_or_poc`` is the engine's fall-through branch rather than a
  named comparison. Deleted 2026-10-24.
"""
from __future__ import annotations

from typing import Tuple

# ── SL modes ─────────────────────────────────────────────────────────
SL_MODE_FVG_ONLY: str = "fvg_only"
# 2026-10-22 (user request): the stop is anchored to the nearest HVN
# shelf on the loss side, searched with its OWN ``nth_hvn_n`` (the
# TP's ``nth_hvn_n`` is a separate knob — see ``SL_NTH_HVN_N`` in
# bt.optimal_config). Clamped to ``sl_min_usd`` so the stop can never
# be closer than a floor to the entry.
SL_MODE_HVN_LVN: str = "hvn_lvn"
# The engine's third branch (``fvg_or_poc``) is the fall-through —
# it takes the tightest of zone edge / POC / HVN — and is therefore
# not named here.
SL_MODES: Tuple[str, ...] = (
    SL_MODE_FVG_ONLY, SL_MODE_HVN_LVN, "fvg_or_poc",
)

# ── TP modes ─────────────────────────────────────────────────────────
TP_MODE_FVG_MULT: str = "fvg_mult"
# NB74b: fire BOTH targets on the same entry (TP1 partial + TP2 remainder).
TP_MODE_MULTI_TARGET: str = "multi_target"

# ── LVN crossing ─────────────────────────────────────────────────────
LVN_MODE_SKIP: str = "skip"      # stop just before the HVN shelf

# ── Entry fill models (D2, 2026-10-22) ──────────────────────────────
#
# How a resting entry order at the zone edge gets filled.
#
# ``ENTRY_FILL_TOUCH`` (canonical)
#     The order rests at the zone edge from the trigger bar onward.
#     It fills on the FIRST subsequent bar whose range touches the
#     entry level, at the entry level itself (a real limit fill).
#     If the zone dies (superseded / played out / expired) before a
#     fill, the order is CANCELLED and the trade never existed.
#
# ``ENTRY_FILL_NEXT_OPEN``
#     The order fills at the NEXT bar's open, unconditionally, as long
#     as the zone is still alive. Strictest entry assumption: no
#     favourable intrabar fill is credited.
#
# The pre-D2 behaviour (fill at the zone edge on the trigger bar,
# regardless of whether price ever went there) is available as
# ``ENTRY_FILL_TRIGGER`` for A/B only. It is NOT a defensible model.
ENTRY_FILL_TOUCH: str = "touch"
ENTRY_FILL_NEXT_OPEN: str = "next_open"
ENTRY_FILL_TRIGGER: str = "trigger"
ENTRY_FILL_MODELS: Tuple[str, ...] = (
    ENTRY_FILL_TOUCH,
    ENTRY_FILL_NEXT_OPEN,
    ENTRY_FILL_TRIGGER,
)

# ── Exit fill models (pessimistic fill, 2026-10-22) ────────────────
#
# How a bar that spans BOTH the stop and the target is resolved, and
# where a gap-through fill is booked.
#
# ``FILL_MODEL_MID`` (legacy / optimistic)
#     A bar touching both SL and TP resolves to the TARGET (the
#     favourable outcome). Gap-through fills are booked at the level.
#
# ``FILL_MODEL_PESSIMISTIC`` (canonical)
#     A bar touching both SL and TP resolves to the STOP (the
#     unfavourable outcome). A level the bar gapped through is booked
#     at the bar's OPEN, not at the level.
#
# ``FILL_MODEL_NEXT_OPEN`` (strictest)
#     The bar's outcome is deferred to the NEXT bar's open, whichever
#     way the level was crossed.
FILL_MODEL_MID: str = "mid"
FILL_MODEL_PESSIMISTIC: str = "pessimistic"
FILL_MODEL_NEXT_OPEN: str = "next_open"
FILL_MODELS: Tuple[str, ...] = (
    FILL_MODEL_MID,
    FILL_MODEL_PESSIMISTIC,
    FILL_MODEL_NEXT_OPEN,
)
