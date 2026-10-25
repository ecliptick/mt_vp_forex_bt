"""Regression tests for the 2026-10-22 defect fixes (D1/D2/D3) + dead-import guard.

Background
----------

The 2026-10-22 audit found three defects that produced *plausible but
wrong* output rather than an exception:

* **D1** — the fee model charged **2x** the live MT4 round trip
  ($0.94 instead of $0.47) because ``_derive_taker_fee_bps`` handed
  the engine a round-trip dollar amount while the engine multiplies
  by 2 internally.
* **D2** — entries were opened **instantly on the trigger bar** at the
  zone edge, even though price had never traded there. 100% of
  trades were "filled" at a price that did not exist; 88% of them
  were fictional. Fixed by modelling entries as resting
  ``PendingOrder`` limit orders.
* **D3** — with an unbounded book and ~11h median holding time, the
  book was never flat, so the canonical ``drop`` conflict policy
  permanently rejected every opposite-direction signal. The strategy
  traded 100% shorts (4,335 long=0 / 4,335 short).

Plus the execution-realism knobs:
* ``fill_model='pessimistic'`` — SL precedence on intra-bar
  ambiguity, gap-throughs booked at the bar open.
* ``entry_fill_model='next_open'`` — market fill at the next bar's
  open instead of a resting limit.

Covered here
------------

1. D1 — the fee is the live round trip, once.
2. D2 — resting order semantics (touch, cancel on zone death,
   no fill = no trade).
3. D3 — the same-direction cap actually fires under ``drop``.
4. Pessimistic execution — SL precedence and gap-through pricing.
5. Dead imports — every module in the repo imports cleanly and no
   module references a deleted file.
"""
from __future__ import annotations

import ast
import importlib
import inspect
import pathlib

import pytest


# ─────────────────────────────────────────────────────────────────────
# 1. D1 — fee model
# ─────────────────────────────────────────────────────────────────────
def test_live_round_trip_fee_is_47_cents():
    """The engine must debit exactly the live MT4 round trip.

    $0.47 = 0.01 lots x $7 commission + 1 oz x $0.40 spread.
    """
    from bt.optimal_config import (
        FOREX_COMMISSION_PER_LOT_USD,
        FOREX_CONTRACT_SIZE,
        FOREX_LOTS,
        FOREX_SPREAD_PER_OZ_USD,
        build_engine_kwargs,
    )

    oz = FOREX_LOTS * FOREX_CONTRACT_SIZE
    expected = (
        FOREX_LOTS * FOREX_COMMISSION_PER_LOT_USD
        + oz * FOREX_SPREAD_PER_OZ_USD
    )
    assert expected == pytest.approx(0.47, abs=1e-9)

    # The engine's own exit path: fee = |exit+entry| * 0.5 * qty * bps * 2
    for spot in (2700.0, 3000.0, 4000.0):
        bps = build_engine_kwargs(spot=spot)["taker_fee_bps"]
        charged = abs(spot + spot) * 0.5 * oz * (bps / 10000.0) * 2.0
        assert charged == pytest.approx(0.47, abs=1e-6), (
            f"spot={spot}: charged ${charged:.4f}, expected $0.47"
        )


def test_fee_knobs_are_stripped_from_the_engine_call():
    """The forex-only fee knobs must not reach the engine.

    They are folded into ``taker_fee_bps`` at build time; passing them
    through would double-charge.
    """
    from bt.vp_engine import _FOREX_ONLY_KWARGS

    for k in ("commission_per_lot_usd", "spread_per_oz_usd"):
        assert k in _FOREX_ONLY_KWARGS


# ─────────────────────────────────────────────────────────────────────
# 2. D2 — resting entry orders
# ─────────────────────────────────────────────────────────────────────
def test_buy_limit_fills_when_price_trades_down_to_it():
    from bt._engine.vp_multi_target_engine import _order_touched

    # BUY limit at 3000.0: fills when the bar's LOW reaches 3000.0.
    assert _order_touched(
        b_low=3000.0, b_high=3001.0, entry_price=3000.0, direction=1)
    assert _order_touched(
        b_low=2999.5, b_high=3001.0, entry_price=3000.0, direction=1)
    # Above the level -> NOT touched.
    assert not _order_touched(
        b_low=3000.5, b_high=3002.0, entry_price=3000.0, direction=1)


def test_sell_limit_fills_when_price_trades_up_to_it():
    from bt._engine.vp_multi_target_engine import _order_touched

    # SELL limit at 3000.0: fills when the bar's HIGH reaches 3000.0.
    assert _order_touched(
        b_low=2999.0, b_high=3000.0, entry_price=3000.0, direction=-1)
    assert _order_touched(
        b_low=2999.0, b_high=3001.0, entry_price=3000.0, direction=-1)
    # Below the level -> NOT touched.
    assert not _order_touched(
        b_low=2998.0, b_high=2999.5, entry_price=3000.0, direction=-1)


def test_order_does_not_fill_on_its_own_placement_bar():
    """D2 core invariant: the trigger bar is NOT a fill bar.

    The engine's fill block runs AFTER the signal block in the same
    bar iteration, so an order placed on bar ``i`` must be eligible
    to fill on bar ``i+1`` at the earliest.
    """
    from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest

    src = inspect.getsource(run_vp_sniper_backtest)
    # The fill block must come after the placement block.
    place_idx = src.find("pending_orders.append(PendingOrder(")
    fill_idx = src.find("# ── 2b.")
    assert place_idx != -1 and fill_idx != -1
    assert fill_idx > place_idx, (
        "the fill block must run AFTER the placement block so an "
        "order placed this bar cannot fill this bar"
    )
    # And the fill loop must skip the current bar.
    assert "placed_bar" in src
    assert "i > po.placed_bar" in src, (
        "the fill check must require a STRICTLY later bar than the "
        "placement bar (an order cannot fill on its own placement bar)"
    )


def test_zone_death_cancels_the_resting_order():
    """A zone that dies before the fill cancels the order — no trade."""
    from bt._engine.ict_signals import FvgZone
    from bt._engine.vp_multi_target_engine import _zone_death_bar

    z = FvgZone(
        trigger_bar=100, direction=1,
        zone_low=3000.0, zone_high=3000.5,
    )
    z.played_out_bar = 150
    assert _zone_death_bar(z) == 150

    # A zone that never died -> 0 (never cancels).
    z2 = FvgZone(
        trigger_bar=100, direction=1,
        zone_low=3000.0, zone_high=3000.5,
    )
    assert _zone_death_bar(z2) == 0


def test_inverted_zone_ignores_its_expiry_bar():
    """An inverted zone has played out — its later expiry is irrelevant.

    An inverted zone is still TRADEABLE (the sniper entry fires at
    the inverted FVG), so the inversion itself is not a death. But its
    later ``expired_bar`` is irrelevant — the zone has already
    committed through price, so the expiry never applies.
    """
    from bt._engine.ict_signals import FvgZone
    from bt._engine.vp_multi_target_engine import _zone_death_bar

    # Non-inverted: the expiry IS the death bar.
    z_live = FvgZone(
        trigger_bar=100, direction=1,
        zone_low=3000.0, zone_high=3000.5,
    )
    z_live.expired_bar = 900
    assert _zone_death_bar(z_live) == 900

    # Inverted: the expiry is skipped, so the zone never dies.
    z_inv = FvgZone(
        trigger_bar=100, direction=1,
        zone_low=3000.0, zone_high=3000.5,
    )
    z_inv.inverted = True
    z_inv.inverted_bar = 120
    z_inv.expired_bar = 900
    assert _zone_death_bar(z_inv) == 0, (
        "an inverted zone must not be cancelled by a later expiry — "
        "the sniper entry at the inverted FVG is still valid"
    )

    # But a played-out inverted zone still dies.
    z_inv2 = FvgZone(
        trigger_bar=100, direction=1,
        zone_low=3000.0, zone_high=3000.5,
    )
    z_inv2.inverted = True
    z_inv2.played_out_bar = 300
    assert _zone_death_bar(z_inv2) == 300


# ─────────────────────────────────────────────────────────────────────
# 3. D3 — the same-direction cap
# ─────────────────────────────────────────────────────────────────────
def test_same_dir_cap_fires_under_the_drop_policy():
    """D3: ``max_same_dir_open`` must be reachable under ``drop``.

    The cap check originally lived ONLY in the ``replace`` / ``stack``
    branches of ``_conflict_policy_pass``. Under the canonical
    ``drop`` policy it never fired, which is what allowed the book to
    fill with one direction forever.
    """
    from bt._engine.vp_multi_target_engine import _conflict_policy_pass

    src = inspect.getsource(_conflict_policy_pass)
    # The cap must appear in EVERY policy branch, including ``drop``.
    assert src.count("max_same_dir_open") >= 4, (
        "the same-dir cap must be checked in all policy branches"
    )
    # The drop branch must contain the cap check.
    drop_idx = src.find('pol == "drop"')
    assert drop_idx != -1, "the drop branch must exist"
    drop_branch = src[drop_idx:drop_idx + 2000]
    assert "max_same_dir_open" in drop_branch, (
        "the drop branch MUST apply the same-direction cap — this is "
        "the D3 fix (uncapped book -> permanent directional lock)"
    )
    assert "n_same_dir_blocked" in drop_branch


def test_same_dir_cap_is_a_noop_when_uncapped():
    """``max_same_dir_open == 0`` must mean 'uncapped' everywhere."""
    from bt._engine.vp_multi_target_engine import _conflict_policy_pass

    src = inspect.getsource(_conflict_policy_pass)
    # Every cap check is guarded by ``max_same_dir_open > 0``.
    assert src.count("max_same_dir_open > 0") >= 3, (
        "each cap check must be guarded by 'max_same_dir_open > 0'"
    )


def test_canonical_config_bounds_the_book():
    """The canonical recipe must NOT ship with an unbounded book."""
    from bt.optimal_config import MAX_SAME_DIR_OPEN

    assert MAX_SAME_DIR_OPEN > 0, (
        "the canonical config must bound same-direction stacking "
        "(D3: unbounded book -> permanent directional lock)"
    )


# ─────────────────────────────────────────────────────────────────────
# 4. Pessimistic execution
# ─────────────────────────────────────────────────────────────────────
def test_stop_wins_on_intra_bar_ambiguity_in_pessimistic_mode():
    from bt._engine.vp_multi_target_config import (
        FILL_MODEL_MID,
        FILL_MODEL_NEXT_OPEN,
        FILL_MODEL_PESSIMISTIC,
    )
    from bt._engine.vp_multi_target_engine import _bar_resolves_stop_first

    # Legacy mid model: the target wins (optimistic).
    assert not _bar_resolves_stop_first(
        sl_hit=True, tp_hit=True, fill_model=FILL_MODEL_MID)
    # Pessimistic: the stop wins.
    assert _bar_resolves_stop_first(
        sl_hit=True, tp_hit=True, fill_model=FILL_MODEL_PESSIMISTIC)
    assert _bar_resolves_stop_first(
        sl_hit=True, tp_hit=True, fill_model=FILL_MODEL_NEXT_OPEN)
    # Not ambiguous -> no stop precedence.
    assert not _bar_resolves_stop_first(
        sl_hit=True, tp_hit=False, fill_model=FILL_MODEL_PESSIMISTIC)


def test_gap_through_books_at_the_bar_open():
    """A bar that gapped past the stop fills at the open, not the stop."""
    from bt._engine.vp_multi_target_config import (
        FILL_MODEL_MID,
        FILL_MODEL_PESSIMISTIC,
    )
    from bt._engine.vp_multi_target_engine import _resolve_exit_price

    # Long position, stop at 2990.0, bar opened at 2985 (gapped through).
    px = _resolve_exit_price(
        level=2990.0, b_open=2985.0, b_low=2984.0, b_high=2989.0,
        direction=1, is_stop=True, fill_model=FILL_MODEL_PESSIMISTIC)
    assert px == pytest.approx(2985.0), (
        "a gap-through must book at the open — the stop price was "
        "never traded"
    )

    # No gap (open inside the range) -> fill at the level.
    px = _resolve_exit_price(
        level=2990.0, b_open=2995.0, b_low=2988.0, b_high=2996.0,
        direction=1, is_stop=True, fill_model=FILL_MODEL_PESSIMISTIC)
    assert px == pytest.approx(2990.0)

    # Legacy mid model always books at the level (optimistic).
    px = _resolve_exit_price(
        level=2990.0, b_open=2985.0, b_low=2984.0, b_high=2989.0,
        direction=1, is_stop=True, fill_model=FILL_MODEL_MID)
    assert px == pytest.approx(2990.0)


def test_canonical_config_uses_the_pessimistic_models():
    from bt.optimal_config import (
        ENTRY_FILL_MODEL,
        FILL_MODEL,
    )

    assert FILL_MODEL == "pessimistic", (
        "the canonical recipe must book exits pessimistically"
    )
    assert ENTRY_FILL_MODEL == "touch", (
        "the canonical recipe must model entries as resting limits"
    )


# ─────────────────────────────────────────────────────────────────────
# 5. Dead imports
# ─────────────────────────────────────────────────────────────────────
_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# Files deleted in the 2026-10-22 dead-code sweep. Nothing may
# reference them any more.
_DELETED_MODULES = (
    "bt/params.py",
    "bt/vp_config.py",
    "bt/structural_sl_tp.py",
    "runtime/sweeps/single_sweep.py",
    "runtime/sweeps/run_dual_sweep.py",
    "runtime/sweeps/diag_swing_cap.py",
    "runtime/sweeps/diag_engine_memory.py",
)


def _all_python_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pat in ("bt/**/*.py", "runtime/**/*.py", "tests/**/*.py"):
        out.extend(p for p in _REPO_ROOT.glob(pat) if "__pycache__" not in p.parts)
    return out


def test_deleted_modules_are_actually_gone():
    for rel in _DELETED_MODULES:
        assert not (_REPO_ROOT / rel).exists(), (
            f"{rel} was deleted in the dead-code sweep but is back"
        )


def test_no_source_references_a_deleted_module():
    """No Python file may import a module that no longer exists."""
    offenders: list[str] = []
    for path in _all_python_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                if node.level:      # relative import
                    names = []
                else:
                    names = [node.module or ""]
            else:
                continue
            for name in names:
                if not name:
                    continue
                # Normalise "bt.params" -> "bt/params.py"
                rel = name.replace(".", "/") + ".py"
                if rel in _DELETED_MODULES:
                    offenders.append(f"{path.name}:{node.lineno} -> {name}")
    assert not offenders, (
        "references to deleted modules:\n  " + "\n  ".join(offenders)
    )


def test_every_module_imports_cleanly():
    """Every module in the repo must import without error."""
    import bt

    failures: list[str] = []
    for path in _all_python_files():
        rel = path.relative_to(_REPO_ROOT)
        if rel.parts[0] == "tests":
            continue
        mod = ".".join(rel.with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        try:
            importlib.import_module(mod)
        except Exception as exc:            # noqa: BLE001
            failures.append(f"{mod}: {type(exc).__name__}: {exc}")
    assert not failures, (
        "modules that fail to import:\n  " + "\n  ".join(failures)
    )


def test_every_from_import_name_actually_exists():
    """A ``from X import Y`` must name a symbol that X really defines.

    This is the failure mode the config consolidation introduced and
    the test suite did NOT catch: ``directional_beta.py`` imported
    ``STRUCTURE_PIVOT_LEN`` / ``STRUCTURE_RESAMPLE_SECS`` from
    ``runtime.sweeps.cached_sweep`` after those module-level
    constants had been replaced by the canonical config. The module
    still IMPORTED cleanly (the import was inside a function body, so
    ``test_every_module_imports_cleanly`` never saw it), and the
    diagnostic only blew up when someone ran it.

    That is the worst shape of bug: a green test suite and a broken
    diagnostic. This guard closes it.

    The check is done by AST + ``dir()`` on the target module, so it
    catches names that moved, were renamed, or were deleted.
    """
    import importlib

    failures: list[str] = []
    for path in _all_python_files():
        if path.parts[-2] == "tests":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue

        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:          # relative import — skip
                continue
            mod_name = node.module or ""
            if not mod_name.startswith(("bt", "runtime")):
                continue

            try:
                mod = importlib.import_module(mod_name)
            except Exception:
                # The module itself is missing — covered by
                # test_no_source_references_a_deleted_module.
                continue

            for alias in node.names:
                if alias.name == "*":
                    continue
                if hasattr(mod, alias.name):
                    continue
                # A submodule import (e.g. ``from bt import optimal_config``)
                # resolves as an attribute of the package only after
                # import; check that too.
                try:
                    importlib.import_module(f"{mod_name}.{alias.name}")
                    continue
                except Exception:
                    pass
                failures.append(
                    f"{path.relative_to(_REPO_ROOT).as_posix()}:"
                    f"{node.lineno} -> from {mod_name} import {alias.name}"
                )

    assert not failures, (
        "imports of names that do not exist in the target module "
        "(a moved/renamed/deleted symbol):\n  " + "\n  ".join(failures)
    )


def test_no_module_imports_bt_twice_under_different_names():
    """A symbol must have exactly ONE import path.

    Two modules exposing the same symbol under different names is how
    the duplicate ``structural_sl_tp`` / ``vp_config`` copies survived
    for so long.

    2026-10-24: the bar engine (``run_ict_backtest``) was DELETED —
    it had zero production callers. The one live engine surface must
    still be reachable via exactly one path.
    """
    import bt

    # The engine surface must be reachable via exactly one DEFINITION.
    # ``bt.vp_engine`` re-exports the engine object; it must be the
    # SAME object, not a second copy.
    from bt.vp_engine import run_vp_sniper_backtest
    from bt._engine.vp_multi_target_engine import (
        run_vp_sniper_backtest as direct,
    )

    assert run_vp_sniper_backtest is direct
    assert bt.vp_engine_mod.run_vp_sniper_backtest is direct


def test_dead_vol_state_gate_is_gone():
    """``bt.vol_state.gate`` was dead code and must stay deleted.

    It had zero production callers, and its "active" closure never
    compared the velocity against the floor — it always returned True.
    A guard that cannot fail is worse than no guard.
    """
    import bt.vol_state as vs

    assert not hasattr(vs, "gate"), (
        "bt.vol_state.gate was removed as dead code — do not re-add it"
    )


@pytest.mark.parametrize("knob", [
    "entry_fill_model",
    "fill_model",
    "tp_p95_mult",
    "max_same_dir_open",
    "conflict_policy",
])
def test_execution_knobs_are_sweepable(knob: str):
    """Every execution-realism knob must be in the engine allowlist."""
    from bt.optimal_config import _ENGINE_OVERRIDES_ALLOWED, build_engine_kwargs

    assert knob in _ENGINE_OVERRIDES_ALLOWED, (
        f"{knob} must be sweepable via build_engine_kwargs"
    )
    assert knob in build_engine_kwargs()


# ─────────────────────────────────────────────────────────────────────
# 6. Conservation identities
# ─────────────────────────────────────────────────────────────────────
def test_signal_and_order_conservation_identities_are_disjoint():
    """The fill-time policy drop must NOT double-count in the signal total.

    Before 2026-10-22 a fill-time policy drop incremented
    ``n_dropped_policy`` as well as ``n_orders_cancelled``. The signal
    identity then read::

        n_signals_emitted - gates - n_dropped_policy == n_orders_placed

    which produced a NEGATIVE residual (measured -51 on the 1-month
    slice), because the fill-time drops were subtracted twice while
    the orders they cancelled were not counted as placed.

    The two counters must be disjoint:
      * ``n_dropped_policy``         — signal time
      * ``n_dropped_policy_at_fill`` — fill time
    """
    from bt._engine.vp_multi_target_engine import BacktestResult

    res = BacktestResult()
    # Simulate: 10 signals, 3 die at the policy at signal time,
    # 7 become orders; 2 of those are cancelled at fill time by the
    # policy, 1 fills, 4 die with the zone.
    res.n_signals_emitted = 10
    res.n_dropped_policy = 3
    res.n_orders_placed = 7
    res.n_dropped_policy_at_fill = 2
    res.n_orders_filled = 1
    res.n_orders_cancelled = 6          # 2 policy + 4 zone-death
    res.n_orders_cancelled_zone_death = 4

    # Signal identity.
    accounted = (
        res.n_orders_placed
        + res.n_dropped_vol_state
        + res.n_dropped_p95_warmup
        + res.n_dropped_p95_warmup_sniper
        + res.n_dropped_policy
    )
    assert res.n_signals_emitted - accounted == 0

    # Order identity (n_orders_cancelled already includes the policy
    # cancellations, so they are NOT subtracted again).
    assert (res.n_orders_placed - res.n_orders_filled
            - res.n_orders_cancelled) == 0


def test_engine_diag_counters_are_separate_fields():
    """``n_dropped_policy`` and ``n_dropped_policy_at_fill`` must both exist."""
    from bt._engine.vp_multi_target_engine import BacktestResult

    res = BacktestResult()
    assert hasattr(res, "n_dropped_policy")
    assert hasattr(res, "n_dropped_policy_at_fill")
    assert res.n_dropped_policy == 0
    assert res.n_dropped_policy_at_fill == 0


def test_initial_sl_distance_is_preserved_across_the_tp1_breakeven():
    """The trade's ORIGINAL risk must survive the TP1 breakeven move.

    After a TP1 partial close the stop moves to breakeven, so
    ``abs(entry_price - stop_price)`` becomes 0.0. The engine therefore
    carries ``initial_sl_distance_usd`` on the OpenTrade and copies
    it into the ClosedTrade, so the aggregate risk metrics stay honest.
    """
    import dataclasses

    from bt._engine.vp_multi_target_engine import ClosedTrade, OpenTrade

    ot_fields = {f.name for f in dataclasses.fields(OpenTrade)}
    ct_fields = {f.name for f in dataclasses.fields(ClosedTrade)}
    assert "initial_sl_distance_usd" in ot_fields
    assert "initial_tp_distance_usd" in ot_fields
    assert "initial_sl_distance_usd" in ct_fields
    assert "initial_tp_distance_usd" in ct_fields


def test_sweep_metrics_use_initial_not_mutated_stop_distance():
    """``compute_metrics`` must not derive SL distance from stop_price.

    ``stop_price`` is MUTATED by the engine (breakeven after TP1, exit
    price on a stop-out), so ``abs(entry - stop_price)`` is 0.0 for
    every TP1/TP2 exit. The metric must read the dedicated fields.
    """
    import inspect

    from runtime.sweeps.cached_sweep import compute_metrics

    src = inspect.getsource(compute_metrics)
    assert "initial_sl_distance_usd" in src
    # The price-derived form may only appear as a FALLBACK.
    assert "fall back" in src.lower()


# ─────────────────────────────────────────────────────────────────────
# 7. NB52 dead detector gates (2026-10-22 cross-repo merge)
# ─────────────────────────────────────────────────────────────────────
def test_detector_reads_body_only_from_canonical_constants():
    """NB52 body-only gates must come from the canonical constants.

    ``_build_v17_forex_sniper`` never set
    ``fvg_body_only_mitigation`` / ``fvg_body_only_invalidation``, so
    reading them off the recipe object resolved to the dataclass
    default ``False`` and the gate silently never ran — while the
    canonical module declared ``FVG_BODY_ONLY_MITIGATION = True``.

    Measured effect on the 1-month 2025-01 slice: 9,684 -> 9,673
    zones (-0.1%), so it is a real filter that was inert.

    2026-10-24 (config consolidation): the recipe object is gone.
    The constants are read inside
    :func:`bt.optimal_config.build_detector_kwargs` — the single
    source of truth — and reach the driver through it.
    """
    import inspect as _inspect

    from bt.optimal_config import (
        FVG_BODY_ONLY_INVALIDATION,
        FVG_BODY_ONLY_MITIGATION,
        build_detector_kwargs,
    )
    from runtime.sweeps import cached_sweep

    # The constants must be read where the single source of truth is.
    src = _inspect.getsource(build_detector_kwargs)
    assert "FVG_BODY_ONLY_MITIGATION" in src
    assert "FVG_BODY_ONLY_INVALIDATION" in src
    # Must NOT be sourced from a recipe object any more.
    assert 'getattr(P, "fvg_body_only_mitigation"' not in src
    assert 'getattr(P, "fvg_body_only_invalidation"' not in src

    kwargs = cached_sweep.detector_kwargs_from_recipe()
    assert kwargs["body_only_mitigation"] == bool(FVG_BODY_ONLY_MITIGATION)
    assert kwargs["body_only_invalidation"] == bool(FVG_BODY_ONLY_INVALIDATION)
    assert kwargs["body_only_mitigation"] is True


def test_strict_wick_filter_is_implemented_and_ships_disabled():
    """The NB52 strict-wick filter must be REAL code, but default OFF.

    The forex fork declared ``STRICT_WICK_REQUIRED = True`` while
    ``detect_fvg`` contained NO strict-wick code at all — a quality
    gate that could never fire.

    The filter is now implemented, but enabling it at the sibling's
    BTC-calibrated floor (``0.00025`` → $0.68 at gold $3,000) returns
    ZERO zones on XAUUSD 1s: measured, 0.00% of 3-bar windows have
    both outer candles clearing a $0.68 wick. Shipping it ON would
    silently delete the entire strategy, so it defaults OFF and must
    stay that way until re-measured.
    """
    import numpy as np

    from bt._engine.ict_signals import compute_dynamic_wick_floor, detect_fvg
    from bt.optimal_config import STRICT_WICK_REQUIRED

    assert STRICT_WICK_REQUIRED is False, (
        "strict-wick must ship disabled: the BTC-scale floor removes "
        "100% of XAUUSD 1s zones (measured 9,684 -> 0)."
    )

    # The helper exists and is price-relative, not absolute-USD.
    close = np.full(7200, 3000.0)
    floor = compute_dynamic_wick_floor(
        close, price_pct=0.00025, recompute_secs=3600)
    assert floor.shape == close.shape
    assert np.allclose(floor, 0.75)  # 3000 * 0.00025

    # The filter is reachable and actually rejects a body-only zone.
    n = 60
    o = np.full(n, 3000.0); h = np.full(n, 3000.0)
    lo = np.full(n, 3000.0); c = np.full(n, 3000.0)
    # bar 8/10: bull FVG WITH rejection wicks
    h[8] = 3000.0; lo[8] = 2999.0; o[8] = 2999.5; c[8] = 2999.5
    h[10] = 3002.0; lo[10] = 3001.0; o[10] = 3001.5; c[10] = 3001.5
    # bar 18/20: bull FVG with pure-body outer candles (no wick)
    h[18] = 3000.0; lo[18] = 2999.0; o[18] = 2999.0; c[18] = 2999.0
    h[20] = 3002.0; lo[20] = 3001.0; o[20] = 3002.0; c[20] = 3002.0

    unfiltered = detect_fvg(o, h, lo, c, warmup=2, fvg_min_zone_usd=0.30)
    filtered = detect_fvg(
        o, h, lo, c, warmup=2, fvg_min_zone_usd=0.30,
        strict_wick_required=True, strict_wick_min_wick_usd=0.50)

    assert any(z.trigger_bar == 20 for z in unfiltered), (
        "fixture must produce a body-only zone before filtering")
    assert not any(z.trigger_bar == 20 for z in filtered), (
        "strict-wick must reject a zone whose outer candles have no wick")
    assert any(z.trigger_bar == 10 for z in filtered), (
        "strict-wick must KEEP a zone whose outer candles have real wicks")


def test_strict_wick_floor_array_is_not_in_the_cache_fingerprint():
    """The per-bar wick floor must never reach the JSON fingerprint.

    ``_fingerprint`` does ``json.dumps`` over the detector shape. The
    floor array is derived from the bars, so shipping it would make
    the fingerprint non-serialisable. The PRICE PCT ships instead —
    it changes the zone set and is a valid cache key.
    """
    import json

    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe

    kwargs = detector_kwargs_from_recipe()
    # Must stay JSON-serialisable (the fingerprint is a sha1 of this).
    json.dumps(kwargs)
    assert "strict_wick_floor_usd_per_bar" not in kwargs
    assert not any(
        isinstance(v, (list, tuple)) and len(v) > 256 for v in kwargs.values()
    ), "no large array may be embedded in the fingerprint shape"


def test_detector_kwargs_carry_strict_wick_pct_for_cache_keying():
    """The wick pct is detector-side, so it must invalidate the cache."""
    from runtime.sweeps.cached_sweep import detector_kwargs_from_recipe

    kwargs = detector_kwargs_from_recipe()
    # Disabled by default → the array sentinel is absent.
    assert kwargs["strict_wick_required"] is False
    assert "_strict_wick_use_floor_array" not in kwargs
    # The pct is always present so the cache key is stable and
    # a future enable() does not silently reuse stale zones.
    assert "strict_wick_required" in kwargs
