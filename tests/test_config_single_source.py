"""Guards for the ONE-source-of-truth config surface (2026-10-24).

The repo's config discipline is: every canonical value is a module
constant in ``bt/optimal_config.py``, defined EXACTLY ONCE, and
materialised onto a target surface by one of the six builders.

These tests lock that discipline. They are AST-based so they cannot
be fooled by a module that merely *imports* a constant and never
reads it.

Covered
-------
1. No module other than ``bt/optimal_config.py`` may DEFINE a
   canonical-named constant (a second source of truth).
2. No canonical constant may be unread (a knob that cannot move).
3. Every allowlist key must reach a real parameter of its target.
4. The canonical knob name must TRANSLATE onto the target's
   parameter name — the builders are the single translation point.
5. The sweep driver may not keep its own copy of that mapping.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from bt import optimal_config as oc
from bt._engine.ict_signals import detect_fvg
from bt._engine.market_structure import detect_market_structure
from bt._engine.rolling_p95 import build_rolling_p95_from_bars
from bt._engine.swing_cap import build_daily_swing
from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest
from bt._engine.volume_profile import build_volume_profiles

REPO_ROOT = Path(__file__).resolve().parents[1]
BT_DIR = REPO_ROOT / "bt"
CANON_PATH = BT_DIR / "optimal_config.py"


# ── AST helpers ──────────────────────────────────────────────────
def _module_constants(path: Path) -> dict[str, object]:
    """Module-level UPPER_CASE assignments in ``path``."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: dict[str, object] = {}
    for node in tree.body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets = list(node.targets)
            value = node.value
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
            value = node.value
        for tgt in targets:
            if isinstance(tgt, ast.Name) and tgt.id.isupper() and value is not None:
                try:
                    out[tgt.id] = ast.literal_eval(value)
                except (ValueError, SyntaxError, TypeError):
                    out[tgt.id] = "<expr>"
    return out


def _loaded_names(tree: ast.Module) -> set[str]:
    """Names LOADED in the module body (an assignment target is a Store)."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            for tok in node.value.replace("[", " ").replace("]", " ") \
                    .replace(",", " ").replace(".", " ").split():
                used.add(tok.strip("\"'"))
    return used


# ── 1. No second source of truth ────────────────────────────────
def test_canonical_config_has_one_source_of_truth():
    """No module outside ``bt/optimal_config.py`` may DEFINE a canonical
    constant.

    A second definition is a second source of truth, whatever its
    value. The historical offenders were:

    * ``bt/_engine/vp_multi_target_config.LVN_SKIP_USD = 10.0``
      (canonical is 0.30 — 33x apart)
    * ``bt/swing_cap.DEFAULT_SWING_CAP_PCT = 0.7`` (canonical 0.25)
    * ``bt/rolling_p95.DEFAULT_*`` (6 copies of the p95 values)
    * ``bt/vol_state.DEFAULT_VOL_STATE_MIN_VEL_*`` (2 copies)
    * ``bt/volume_profile.DEFAULT_VP_*`` (7 copies, different NAMES)
    * ``bt/vp_engine.DEFAULT_TP_MODE*`` (2 copies)
    * ``bt/risk.DEFAULT_LOTS/CONTRACT_SIZE/LEVERAGE`` (3 copies)
    * ``bt/leverage.DEFAULT_LEVERAGE`` (1 copy)
    """
    canonical = set(_module_constants(CANON_PATH))
    offenders: list[str] = []
    for path in sorted(BT_DIR.rglob("*.py")):
        if path == CANON_PATH:
            continue
        for name in _module_constants(path):
            if name in canonical:
                offenders.append(
                    f"{path.relative_to(REPO_ROOT).as_posix()}::{name}"
                )
    assert not offenders, (
        "canonical constants are DEFINED outside bt/optimal_config.py "
        f"(second source of truth): {offenders}"
    )


def test_no_canonical_constant_is_unread():
    """Every canonical constant must be READ somewhere.

    An unread constant is a knob that cannot move — exactly the
    ``sl_atr_mult`` failure mode this repo has documented.

    "Somewhere" means ANYWHERE in the ``bt/`` package, not just in
    ``bt/optimal_config.py`` itself. Several canonical constants are
    legitimately consumed by a sibling module rather than by the
    config module — ``FOREX_LEVERAGE`` is read by ``bt/leverage.py``
    (as the default for ``account_exposure`` / ``max_lots_for_exposure``)
    and ``FOREX_LOTS`` / ``FOREX_CONTRACT_SIZE`` are read by
    ``bt/risk.py``. Scanning only the defining module reported those
    as dead knobs, which was a FALSE POSITIVE — and a false positive
    in a dead-knob guard is how a real dead knob gets waved through
    later ("the guard is noisy, ignore it").

    So the scan covers the whole package. This can only ever find
    MORE reads than the single-file scan, never fewer, so it cannot
    newly flag a constant that the old test accepted.
    """
    defined = set(_module_constants(CANON_PATH))

    # Reads inside the canonical module itself.
    tree = ast.parse(CANON_PATH.read_text(encoding="utf-8"))
    read_names = _loaded_names(tree)

    # Reads anywhere else in the package.
    for path in sorted(BT_DIR.rglob("*.py")):
        if path == CANON_PATH:
            continue
        try:
            other = ast.parse(path.read_text(encoding="utf-8"),
                               filename=str(path))
        except SyntaxError:
            continue
        read_names |= _loaded_names(other)

    assert read_names, "no canonical constant is read anywhere — AST broken?"
    unread = sorted(n for n in defined if n not in read_names)
    assert not unread, (
        f"canonical constants are never read (dead knobs): {unread}"
    )


# ── 2. Allowlists must reach real parameters ────────────────────
_ALLOWLIST_TARGETS = [
    (oc._ENGINE_OVERRIDES_ALLOWED, run_vp_sniper_backtest),
    (oc._VP_OVERRIDES_ALLOWED, build_volume_profiles),
    (oc._DETECTOR_OVERRIDES_ALLOWED, detect_fvg),
]


def test_engine_vp_detector_allowlists_reach_real_params():
    """These three builders materialise the target's OWN parameter names."""
    for allowed, fn in _ALLOWLIST_TARGETS:
        params = set(inspect.signature(fn).parameters)
        bad = sorted(set(allowed) - params)
        assert not bad, (
            f"{fn.__name__}: allowlist advertises non-parameters {bad}"
        )


@pytest.mark.parametrize(
    "allowed,fn",
    [
        (oc._SWING_OVERRIDES_ALLOWED, build_daily_swing),
        (oc._P95_OVERRIDES_ALLOWED, build_rolling_p95_from_bars),
        (oc._STRUCTURE_OVERRIDES_ALLOWED, detect_market_structure),
    ],
)
def test_cached_object_allowlists_translate_onto_real_params(
    allowed, fn,
):
    """These three builders use CANONICAL knob names and TRANSLATE them.

    The knob name and the target's parameter name differ
    (``swing_cap_pct`` -> ``cap_pct``), so the allowlist key is NOT a
    parameter of the target. What matters is that the translation is
    total and lands on real parameters.
    """
    params = set(inspect.signature(fn).parameters)
    produced = {
        "swing": oc.build_swing_kwargs(),
        "p95": oc.build_p95_kwargs(),
        "structure": oc.build_structure_kwargs(),
    }[[k for k, v in (
        ("swing", oc._SWING_OVERRIDES_ALLOWED),
        ("p95", oc._P95_OVERRIDES_ALLOWED),
        ("structure", oc._STRUCTURE_OVERRIDES_ALLOWED),
    ) if v is allowed][0]]
    bad = sorted(set(produced) - params)
    assert not bad, (
        f"{fn.__name__}: translated kwargs contain non-parameters {bad}"
    )


# ── 3. The translation must actually happen ─────────────────────
def test_canonical_knob_names_reach_the_target_params():
    """A canonical-name override must REPLACE the target-named value.

    The bug this locks: the builders did ``base.update(overrides)``,
    which appended the canonical key instead of replacing the target
    one, so the emitted dict carried BOTH names and the override
    never reached the target function.

    Before: ``build_swing_kwargs(swing_cap_pct=0.5)`` returned
    ``{'cap_pct': 0.25, 'swing_cap_pct': 0.5}`` — the sweep splatted
    it into ``build_daily_swing`` and got ``TypeError: unexpected
    keyword argument 'swing_cap_pct'``.
    """
    swing = oc.build_swing_kwargs(swing_cap_pct=0.5, swing_lookback_secs=3600)
    assert swing["cap_pct"] == 0.5
    assert swing["lookback_secs"] == 3600
    assert "swing_cap_pct" not in swing
    assert "swing_lookback_secs" not in swing

    p95 = oc.build_p95_kwargs(tp_p95_q=90.0, tp_p95_inner_secs=1800)
    assert p95["p95_q"] == 90.0
    assert p95["inner_secs"] == 1800
    assert "tp_p95_q" not in p95
    assert "tp_p95_inner_secs" not in p95

    struct = oc.build_structure_kwargs(
        structure_pivot_len=13, structure_resample_secs=120)
    assert struct["pivot_len"] == 13
    assert struct["resample_to_n_secs"] == 120
    assert "structure_pivot_len" not in struct
    assert "structure_resample_secs" not in struct


def test_translation_rejects_an_unmapped_knob():
    """An unmapped key must raise, not be splatted as an unknown kwarg."""
    with pytest.raises(KeyError):
        oc._translate({"not_mapped": 1}, oc._SWING_KNOB_TO_PARAM)


def test_builders_reject_the_wrong_vocabulary():
    """The TARGET's parameter name is not a valid canonical knob name.

    This is the vocabulary trap: ``build_swing_kwargs(cap_pct=0.5)``
    must fail loudly rather than silently measuring the canonical
    0.25.
    """
    with pytest.raises(KeyError):
        oc.build_swing_kwargs(cap_pct=0.5)
    with pytest.raises(KeyError):
        oc.build_p95_kwargs(p95_q=90.0)
    with pytest.raises(KeyError):
        oc.build_structure_kwargs(pivot_len=13)


# ── 4. The sweep driver may not duplicate the mapping ───────────
def test_sweep_driver_does_not_duplicate_the_structure_mapping():
    """``cached_sweep.py`` used to keep its OWN copy of the
    canonical-name -> parameter-name mapping and assert it against the
    allowlist. The assert was a band-aid over a duplicated source of
    truth; the copy is gone.
    """
    path = REPO_ROOT / "runtime" / "sweeps" / "cached_sweep.py"
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src, filename=str(path))
    mapping_names = {
        n for n in ast.dump(tree).split("'") if "_KNOB_TO_PARAM" in n
    }
    assert not mapping_names, (
        "cached_sweep.py re-defines a canonical->param mapping; the "
        "translation belongs to bt/optimal_config.py only"
    )
    # And the builder's own maps must exist.
    assert oc._SWING_KNOB_TO_PARAM
    assert oc._P95_KNOB_TO_PARAM
    assert oc._STRUCTURE_KNOB_TO_PARAM


# ── 5. The p95 multiplier must exist in exactly one place ──────
def test_p95_multiplier_is_not_a_dead_build_side_knob():
    """``p95_mult`` on the cap OBJECT is cancelled out by the engine.

    ``_p95_cap_usd`` computes ``cap_for(t) * (engine_mult/obj_mult)``
    and ``cap_for`` already applied ``obj_mult`` — so the object's own
    multiplier cancels and only the engine kwarg ``tp_p95_mult``
    moves the cap. It must therefore not be on the build allowlist.
    """
    assert "p95_mult" not in oc._P95_OVERRIDES_ALLOWED
    assert oc.build_p95_kwargs()["p95_mult"] == 1.0
    # The live multiplier is the engine kwarg.
    assert "tp_p95_mult" in oc._ENGINE_OVERRIDES_ALLOWED
    assert oc.build_engine_kwargs()["tp_p95_mult"] == oc.TP_P95_MULT


def test_live_p95_builder_is_the_only_reader_of_the_seed_constant():
    """``FOREX_P95_RET_USD`` must reach exactly ONE surface.

    The backtest corpus already carries the history the p95 cap
    needs, so the backtest builder pins ``seed_constant_usd=0.0``
    ("use the real data"). The LIVE VPS boots with no history and
    needs the baked-in fallback.

    Keeping them on separate builders is what guarantees the backtest
    can never accidentally pick up the live seed constant and silently
    move every measured number.
    """
    backtest = oc.build_p95_kwargs()
    live = oc.build_live_p95_kwargs()

    # The backtest path never sees the live constant.
    assert backtest["seed_constant_usd"] == 0.0
    assert backtest["seed_constant_usd"] != oc.FOREX_P95_RET_USD

    # The live path uses it.
    assert live["seed_constant_usd"] == oc.FOREX_P95_RET_USD
    assert live["seed_path"] == "constant"

    # The live builder's keys must all be real parameters of
    # ``make_live_rolling_p95``.
    from bt._engine.rolling_p95 import make_live_rolling_p95
    params = set(inspect.signature(make_live_rolling_p95).parameters)
    bad = sorted(set(live) - params)
    assert not bad, f"make_live_rolling_p95: non-parameters {bad}"


def test_live_p95_builder_rejects_illegal_overrides():
    """An unknown key must raise, never be splatted as a stray kwarg."""
    with pytest.raises(KeyError):
        oc.build_live_p95_kwargs(p95_q=90.0)      # wrong vocabulary
    with pytest.raises(KeyError):
        oc.build_live_p95_kwargs(bogus_field=1)


# ── 6. The deleted dead surfaces must stay deleted ─────────────
@pytest.mark.parametrize(
    "module_name,attr",
    [
        ("bt.swing_cap", "cap_distance"),
        ("bt.swing_cap", "apply_cap_to_price"),
        ("bt.swing_cap", "DEFAULT_SWING_CAP_PCT"),
        ("bt.swing_cap", "DEFAULT_SWING_LOOKBACK_SECS"),
        ("bt.rolling_p95", "DEFAULT_LOOKBACK_SECS"),
        ("bt.rolling_p95", "DEFAULT_P95_MULT"),
        ("bt.vol_state", "DEFAULT_VOL_STATE_MIN_VEL_5M"),
        ("bt.volume_profile", "make_rolling_vp"),
        ("bt.volume_profile", "DEFAULT_VP_BUCKET_USD"),
        ("bt.volume_profile", "DEFAULT_VP_MIN_SHELF_USD"),
        ("bt.vp_engine", "DEFAULT_TP_MODE"),
        ("bt.vp_engine", "DEFAULT_TP_MODE_LEGACY"),
        ("bt.risk", "DEFAULT_LOTS"),
        ("bt.risk", "DEFAULT_CONTRACT_SIZE"),
        ("bt.risk", "DEFAULT_LEVERAGE"),
        ("bt.leverage", "DEFAULT_LEVERAGE"),
    ],
)
def test_deleted_duplicate_surfaces_stay_deleted(module_name, attr):
    """A duplicate constant must not be able to creep back.

    Each of these was a second copy of a canonical value, and two of
    them CONTRADICTED the canonical value outright.
    """
    import importlib
    mod = importlib.import_module(module_name)
    assert not hasattr(mod, attr), (
        f"{module_name}.{attr} is back — it is a duplicate source of "
        f"truth for a canonical value in bt/optimal_config.py"
    )


def test_vp_multi_target_config_holds_no_knob_values():
    """``bt/_engine/vp_multi_target_config.py`` is MODE VOCABULARY only.

    It used to carry ``LVN_SKIP_USD = 10.0`` — a 33x contradiction of
    the canonical 0.30 — imported by the engine and never read.
    """
    from bt._engine import vp_multi_target_config as cfg
    assert not hasattr(cfg, "LVN_SKIP_USD")
    assert not hasattr(cfg, "TP_MAX_DISTANCE_USD")
    # It must not define any numeric knob of its own.
    numeric = {
        name: val for name, val in _module_constants(
            Path(cfg.__file__)).items()
        if isinstance(val, (int, float)) and not isinstance(val, bool)
    }
    assert not numeric, (
        f"vp_multi_target_config.py defines numeric knobs: {numeric}"
    )
