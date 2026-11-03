"""Guards for the repo's XAUUSD-only, self-contained invariants.

Two things must stay true, and both are easy to break by accident:

1. **No crypto/BTC vocabulary in the source.** The engine was
   migrated from a second market. A leftover ``qty_btc`` parameter or
   a "BTC-scale literal" comment sends the next reader (or agent)
   looking for a market that does not exist here.

2. **``bt/_engine/`` is the engine, and there is exactly one
   definition of every algorithm.** A re-export wrapper under
   ``bt/`` gives an object a second name; a second copy of a file
   gives an algorithm a second definition. Both are how the pre-D1
   fee bug and the stale ``tp_max_distance_usd`` constant survived.

The scans below are **AST/text based**, so they are fast and cannot
be fooled by a slow import.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[1]

# ── The banned vocabulary ───────────────────────────────────────────
# ``btc`` / ``crypto`` / ``bitcoin`` as standalone words. Matched
# case-insensitively so ``BTC`` and ``btc`` are both caught.
_BANNED_RE = re.compile(r"\b(btc|crypto|bitcoin)\b", re.IGNORECASE)

# Files that legitimately MENTION the banned words in order to say
# "do not add these back". Each one is a guard, not a source file.
_ALLOWED_MENTIONS = {
    "tests/test_repo_invariants.py",   # this file
    "AGENTS.md",                       # the XAUUSD-ONLY rule
    "docs/analysis.md",                # the rewrite note
}


def _source_files() -> list[Path]:
    """Every ``.py`` and ``.md`` file in the repo, minus VCS noise."""
    out: list[Path] = []
    for pattern in ("*.py", "*.md"):
        for p in _REPO_ROOT.rglob(pattern):
            parts = set(p.relative_to(_REPO_ROOT).parts)
            if parts & {".git", "__pycache__", "notebooks", "results"}:
                continue
            out.append(p)
    return sorted(out)


def test_no_source_file_mentions_crypto_or_btc():
    """No ``.py`` or ``.md`` file may use crypto/BTC vocabulary.

    This repo is XAUUSD-only. The engine's position-size parameter is
    ``qty_oz`` (1 unit = 1 oz of gold). A stray ``qty_btc`` or
    "BTC-scale" comment is a leftover from the migration and sends
    the next reader hunting for a market that is not here.

    The only permitted mentions are the ones that exist to forbid
    the vocabulary — see ``_ALLOWED_MENTIONS``.
    """
    offenders: list[str] = []
    for p in _source_files():
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel in _ALLOWED_MENTIONS:
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        for i, line in enumerate(text.splitlines(), 1):
            if _BANNED_RE.search(line):
                offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "crypto/BTC vocabulary must not appear in this XAUUSD-only repo:\n  "
        + "\n  ".join(offenders)
    )


def test_the_engine_has_no_btc_named_parameter():
    """``qty_btc`` must not exist as an identifier anywhere.

    The rename to ``qty_oz`` is mechanical, but a surviving
    ``qty_btc`` in one function signature is enough to make the
    engine's sizing convention ambiguous.
    """
    offenders: list[str] = []
    for p in _REPO_ROOT.rglob("*.py"):
        if "__pycache__" in p.parts or ".git" in p.parts:
            continue
        rel = p.relative_to(_REPO_ROOT).as_posix()
        if rel == "tests/test_repo_invariants.py":
            # This file names the old identifier on purpose.
            continue
        text = p.read_text(encoding="utf-8", errors="replace")
        if "qty_btc" in text:
            for i, line in enumerate(text.splitlines(), 1):
                if "qty_btc" in line:
                    offenders.append(f"{rel}:{i}: {line.strip()[:100]}")
    assert not offenders, (
        "``qty_btc`` was renamed to ``qty_oz``; stragglers:\n  "
        + "\n  ".join(offenders)
    )


# ── One definition per algorithm ───────────────────────────────────

# The wrapper modules that used to re-export the engine's own modules.
_DELETED_WRAPPERS = (
    "bt/rolling_p95.py",
    "bt/swing_cap.py",
    "bt/vol_state.py",
    "bt/volume_profile.py",
)


@pytest.mark.parametrize("rel_path", _DELETED_WRAPPERS)
def test_the_reexport_wrappers_stay_deleted(rel_path: str):
    """A re-export wrapper must not come back.

    ``bt/rolling_p95.py`` and its three siblings were pure wrappers
    over ``bt/_engine/`` with zero production callers. They were
    deleted on 2026-11-03.

    They are not merely unused — they are actively harmful: a wrapper
    gives an object a second name, and a reader who finds
    ``bt.rolling_p95`` reasonably assumes there is a reason the engine
    source is not importable directly.
    """
    assert not (_REPO_ROOT / rel_path).exists(), (
        f"{rel_path} was deleted (2026-11-03) — bt._engine.X is the surface"
    )


def test_no_module_outside_the_engine_re_exports_an_engine_module():
    """Nothing under ``bt/`` may re-export from ``bt/_engine/``.

    This is the structural form of the same rule: a module whose
    entire body is an import-from-``bt._engine`` re-export is a
    wrapper, and wrappers are deleted. The engine's own modules
    (``bt/_engine/*``) import from each other legitimately.
    """
    wrappers: list[str] = []
    for p in sorted((_REPO_ROOT / "bt").rglob("*.py")):
        if "_engine" in p.relative_to(_REPO_ROOT).parts:
            continue
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        body = tree.body
        # Strip the module docstring and the ``__future__`` import.
        stmts = [
            s for s in body
            if not (isinstance(s, ast.Expr) and isinstance(s.value, ast.Constant))
            and not (isinstance(s, ast.ImportFrom) and s.module == "__future__")
        ]
        if not stmts:
            continue
        # Every remaining statement must be an import FROM bt._engine.
        all_engine_imports = all(
            isinstance(s, ast.ImportFrom) and (s.module or "").startswith("bt._engine")
            for s in stmts
        )
        if all_engine_imports:
            rel = p.relative_to(_REPO_ROOT).as_posix()
            wrappers.append(rel)
    assert not wrappers, (
        "these modules are pure re-export wrappers over bt/_engine/ — delete them:\n  "
        + "\n  ".join(wrappers)
    )


def test_the_engine_directory_is_the_canonical_source():
    """``bt/_engine/`` must hold one definition of each algorithm.

    A duplicated engine file is the failure mode that produced the
    pre-D1 fee bug and the stale ``tp_max_distance_usd`` constant:
    two copies of the same geometry, one of which nobody updates.
    """
    engine = _REPO_ROOT / "bt" / "_engine"
    assert engine.is_dir()

    # The engine's core algorithms, by their module name.
    expected = {
        "vp_multi_target_engine.py",
        "vp_multi_target_config.py",
        "ict_signals.py",
        "ict_strategy.py",
        "market_structure.py",
        "volume_profile.py",
        "vol_state.py",
        "swing_cap.py",
        "rolling_p95.py",
        "run_report.py",
        "timebase.py",
    }
    actual = {p.name for p in engine.glob("*.py")} - {"__init__.py"}
    assert expected <= actual, (
        "the engine is missing a module:\n  " + "\n  ".join(sorted(expected - actual))
    )

    # No engine module may exist under ``bt/`` directly (that would be
    # a second copy at a different import path).
    for p in sorted((_REPO_ROOT / "bt").glob("*.py")):
        if p.name == "__init__.py":
            continue
        assert p.name not in expected, (
            f"bt/{p.name} is a second copy of an engine module — "
            f"the engine lives ONLY in bt/_engine/"
        )


# ─────────────────────────────────────────────────────────────────────
# 3. Whole-file verdicts must not be read at a bar
# ─────────────────────────────────────────────────────────────────────
#
# THE BUG THIS GUARDS (found and fixed 2026-11-03)
# ===============================================
# ``bt/_engine/vp_multi_target_engine.py`` skipped an immediate entry
# whenever the zone had *ever* inverted::
#
#     if entry_mode == "dual" and int(zone.inverted_bar) >= 0:
#         res.n_skipped_dual_inverted += 1
#         continue
#
# ``inverted_bar`` is computed by the detector over the WHOLE file.
# Read at the zone's trigger bar, it is the FUTURE. Measured on
# 2025-01 (1.38M bars, 2,475 zones):
#
#     trigger zones                         2,475
#     skipped because inverted_bar >= 0       723   (29.2%)
#       of which inverted BEFORE trigger         0   (correct)
#       of which inverted AFTER trigger       723   (LOOK-AHEAD)
#     lead = inverted_bar - trigger_bar: median 2,518 bars, p75 8,755,
#     p90 30,015, max 200,769.
#
# So 29.2% of the immediate book was a SELECTION ON THE OUTCOME: zones
# that eventually invert were silently removed, and zones that never
# invert were kept. The book was measured on a criterion a live engine
# cannot evaluate at the trigger bar.
#
# Over six monthly slices the look-ahead was worth **$2,811** — it was
# systematically FAVOURABLE, and it flipped two net-positive slices to
# negative.
#
# THE RULE
# ========
# ``inverted_bar`` / ``superseded_bar`` / ``played_out_bar`` /
# ``expired_bar`` are whole-file verdicts. Reading one at bar ``i``
# leaks the future unless the value is compared against ``i``.
#
# The engine already does this correctly in two places —
# ``_zone_dead_by(zone, bar)`` and the sniper bucket at
# ``zones_inverted_at_bar`` — so the fix has a precedent to follow.

# ─────────────────────────────────────────────────────────────────────
# 3. Whole-file verdicts must not be read at a bar
# ─────────────────────────────────────────────────────────────────────
#
# THE BUG THIS GUARDS (found and fixed 2026-11-03)
# ===============================================
# ``bt/_engine/vp_multi_target_engine.py`` skipped an immediate entry
# whenever the zone had *ever* inverted::
#
#     if entry_mode == "dual" and int(zone.inverted_bar) >= 0:
#         res.n_skipped_dual_inverted += 1
#         continue
#
# ``inverted_bar`` is computed by the detector over the WHOLE file.
# Read at the zone's trigger bar, it is the FUTURE. Measured on
# 2025-01 (1.38M bars, 2,475 zones):
#
#     trigger zones                         2,475
#     skipped because inverted_bar >= 0       723   (29.2%)
#       of which inverted BEFORE trigger         0   (correct)
#       of which inverted AFTER trigger       723   (LOOK-AHEAD)
#     lead = inverted_bar - trigger_bar: median 2,518 bars, p75 8,755,
#     p90 30,015, max 200,769.
#
# So 29.2% of the immediate book was a SELECTION ON THE OUTCOME: zones
# that eventually invert were silently removed, and zones that never
# invert were kept. The book was measured on a criterion a live engine
# cannot evaluate at the trigger bar.
#
# Over six monthly slices the look-ahead was worth **$2,811** — it was
# systematically FAVOURABLE, and it flipped two net-positive slices to
# negative.
#
# THE RULE
# ========
# ``inverted_bar`` / ``superseded_bar`` / ``played_out_bar`` /
# ``expired_bar`` are whole-file verdicts. Reading one at bar ``i``
# leaks the future unless the value is compared against ``i``.
#
# The engine already does this correctly in two places —
# ``_zone_dead_by(zone, bar)`` and the sniper bucket at
# ``zones_inverted_at_bar`` — so the fix has a precedent to follow.

_VERDICT_FIELDS = {"inverted_bar", "superseded_bar", "played_out_bar",
                    "expired_bar"}
# The sanctioned causal helpers compare internally.
_CAUSAL_HELPERS = {"_zone_dead_by", "_zone_death_bar"}
# Names that denote "the bar we are trading / comparing against".
_BAR_NAMES = {"i", "bar", "inv_b", "tb", "j", "_inv_b", "_bar",
               "trigger_bar", "placed_bar"}


def _engine_tree() -> ast.Module:
    """The parsed AST of the canonical engine module."""
    p = _REPO_ROOT / "bt" / "_engine" / "vp_multi_target_engine.py"
    return ast.parse(p.read_text(encoding="utf-8"))


def _scope_assignments(node: ast.AST) -> dict[str, ast.AST]:
    """Map ``local name -> assigned value``, SCOPED to ``node``.

    A whole-file verdict is very often read into a local first::

        _inv_b = int(getattr(zone, "inverted_bar", -1))
        if 0 <= _inv_b <= i: ...

    so the guard must follow the assignment to know that ``_inv_b`` IS
    the verdict. The map is built per enclosing scope, so a name reused
    for something else in a different scope (``po`` is used for both a
    PendingOrder and a death-bar int) cannot produce a false positive.
    """
    out: dict[str, ast.AST] = {}
    for child in ast.walk(node):
        if isinstance(child, ast.Assign):
            for tgt in child.targets:
                if isinstance(tgt, ast.Name):
                    out[tgt.id] = child.value
    return out


def _reads_verdict(node: ast.AST, fields: set[str],
                   assigns: dict[str, ast.AST]) -> set[str]:
    """Which whole-file verdict fields are read by ``node``.

    Handles three access forms:

      * ``zone.inverted_bar``                -> ast.Attribute
      * ``getattr(zone, "inverted_bar", -1)`` -> ast.Call
      * a local bound to one of the above    -> followed via ``assigns``
    """
    found: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Attribute) and sub.attr in fields:
            found.add(sub.attr)
        elif isinstance(sub, ast.Call):
            fn = sub.func
            if isinstance(fn, ast.Name) and fn.id == "getattr":
                for arg in sub.args[1:2]:
                    if isinstance(arg, ast.Constant) and \
                            isinstance(arg.value, str) and \
                            arg.value in fields:
                        found.add(arg.value)
        elif isinstance(sub, ast.Name) and sub.id in assigns:
            # Follow the local binding (one level is enough — the engine
            # never chains these). Guard against self-reference.
            if assigns[sub.id] is not node:
                found |= _reads_verdict(assigns[sub.id], fields, {})
    return found


def _scoped_walk(tree: ast.Module):
    """Yield ``(scope, node)`` for every Compare in the tree.

    ``scope`` is the nearest enclosing FUNCTION, used to build a
    per-function assignment map. It must be the function and not the
    innermost block, because the assignment usually sits in a sibling
    branch of the same function::

        if entry_mode == "dual":
            _inv_b = int(getattr(zone, "inverted_bar", -1))
            if 0 <= _inv_b <= i:      # <- the Compare

    Scoping to the ``if`` block would still work here, but scoping to
    the enclosing ``for`` body -- where the same local name is reused
    for a different object -- would not.
    """
    def visit(node: ast.AST, scope: ast.AST):
        for child in ast.iter_child_nodes(node):
            new_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                new_scope = child
            if isinstance(child, ast.Compare):
                yield scope, child
            yield from visit(child, new_scope)

    yield from visit(tree, tree)


def test_no_whole_file_zone_verdict_is_read_without_a_bar_comparison():
    """A zone's final verdict must never be read at a bar without
    comparing it to that bar.

    See the section comment above for the measurement. The engine is
    the ONLY place a whole-file verdict is read at a bar, so the scan
    is scoped to it.
    """
    tree = _engine_tree()

    offenders: list[str] = []
    seen_scopes: set[int] = set()
    for scope, node in _scoped_walk(tree):
        key = id(scope)
        if key not in seen_scopes:
            seen_scopes.add(key)
        assigns = _scope_assignments(scope)

        # The verdict may appear on either side of the comparison.
        operands = [node.left] + list(node.comparators)
        verdict_operands = [
            op for op in operands
            if _reads_verdict(op, _VERDICT_FIELDS, assigns)
        ]
        if not verdict_operands:
            continue
        # Skip the sanctioned helpers (they compare internally).
        if any(h in ast.unparse(node) for h in _CAUSAL_HELPERS):
            continue
        # At least one OTHER operand must be a bar.
        bar_operands = [
            op for op in operands
            if isinstance(op, ast.Name) and op.id in _BAR_NAMES
        ]
        if not bar_operands:
            fields = sorted(
                {f for op in verdict_operands
                 for f in _reads_verdict(op, _VERDICT_FIELDS, assigns)}
            )
            offenders.append(
                f"line {node.lineno}: {'/'.join(fields)} compared against "
                f"no bar -> {ast.unparse(node)}"
            )

    assert not offenders, (
        "these comparisons read a WHOLE-FILE zone verdict without "
        "comparing it to the bar being traded — that is a look-ahead:\n  "
        + "\n  ".join(offenders)
    )


def test_the_dual_mode_inverted_test_is_causal():
    """The dual-mode skip must compare ``inverted_bar`` to the traded bar.

    This is the specific line the previous test generalises. The buggy
    form was::

        if entry_mode == "dual" and int(zone.inverted_bar) >= 0:

    The causal form compares against the bar being traded (``i``), so a
    zone that inverts LATER stays in the immediate book — which is what
    a live engine sees at the trigger bar.

    The verdict may be read as ``zone.inverted_bar`` or as
    ``getattr(zone, "inverted_bar", -1)`` (bound to a local first);
    both forms are accepted, as long as the COMPARISON is against a bar.
    """
    tree = _engine_tree()

    checked = False
    offenders: list[str] = []
    for scope, node in _scoped_walk(tree):
        assigns = _scope_assignments(scope)
        operands = [node.left] + list(node.comparators)
        if not any(_reads_verdict(op, {"inverted_bar"}, assigns)
                   for op in operands):
            continue
        checked = True
        bar_operands = [
            op for op in operands
            if isinstance(op, ast.Name) and op.id in _BAR_NAMES
        ]
        if not bar_operands:
            offenders.append(f"line {node.lineno}: {ast.unparse(node)}")

    assert checked, (
        "the dual-mode inverted-bar comparison was not found — the "
        "engine's structure changed; update this guard"
    )
    assert not offenders, (
        "the dual-mode inverted test reads a whole-file verdict:\n  "
        + "\n  ".join(offenders)
    )
