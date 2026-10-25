"""Audit the config surface: duplicate constants + dead imports.

Walks every module under ``bt/`` with AST and reports:

1. Module-level constants (UPPER_CASE) defined OUTSIDE
   ``bt/optimal_config.py`` whose NAME also exists in
   ``bt/optimal_config.py`` — a second source of truth for the
   same knob.
2. Dead imports: a name imported into a module and never referenced
   anywhere in that module's body. Re-exports (a name listed in the
   module's ``__all__``) are NOT dead. ``from __future__ import
   annotations`` is not an import.
3. Canonical constants that are never READ (neither referenced by a
   builder in ``bt/optimal_config.py`` nor imported anywhere in the
   repo) — a knob that cannot move.

Run::

    python -m runtime.diagnostics.audit_config_surface
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BT = ROOT / "bt"
CANON = BT / "optimal_config.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _consts(tree: ast.Module) -> dict[str, object]:
    """Module-level UPPER_CASE assignments, with their assigned value."""
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


def _exported(tree: ast.Module) -> set[str]:
    """Names listed in the module's ``__all__``."""
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name) and tgt.id == "__all__":
                    try:
                        vals = ast.literal_eval(node.value)
                    except (ValueError, SyntaxError):
                        return set()
                    if isinstance(vals, (list, tuple, set)):
                        return {v for v in vals if isinstance(v, str)}
    return set()


def _referenced(tree: ast.Module) -> set[str]:
    """Every name LOADED in the module body (excludes the import stmt).

    A name that appears only inside a STRING annotation (e.g.
    ``Optional["StructureState"]`` under ``from __future__ import
    annotations``) counts as referenced — the annotation would fail
    to resolve at runtime if the import were removed.
    """
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            # Possible stringified forward reference.
            for tok in node.value.replace("[", " ").replace("]", " ") \
                    .replace(",", " ").replace(".", " ").split():
                used.add(tok.strip("\"'"))
    return used


def _dead_imports(tree: ast.Module) -> list[tuple[int, str]]:
    used = _referenced(tree)
    exported = _exported(tree)
    dead: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == "__future__":
                continue
            for alias in node.names:
                bound = alias.asname or alias.name
                if bound == "*":
                    continue
                if bound not in used and bound not in exported:
                    dead.append((node.lineno, bound))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                if bound == "*":
                    continue
                if bound not in used and bound not in exported:
                    dead.append((node.lineno, bound))
    return dead


def _repo_py() -> list[Path]:
    skip = {".pytest_cache", "notebooks", "__pycache__", ".git"}
    return [
        p for p in sorted(ROOT.rglob("*.py"))
        if not (skip & set(p.parts))
    ]


def main() -> int:
    canon_tree = _tree(CANON)
    canon = _consts(canon_tree)
    # Names the canonical module LOADS (a constant assignment target is
    # a Store, not a Load — so a constant that is only DEFINED never
    # appears here, and one that is also READ does).
    canon_read = _referenced(canon_tree)
    bt_files = sorted(BT.rglob("*.py"))
    rel = lambda p: p.relative_to(ROOT).as_posix()  # noqa: E731
    repo_py = _repo_py()

    print("=" * 78)
    print("1. DUPLICATE CONSTANT NAMES (defined outside optimal_config.py)")
    print("=" * 78)
    dupes = 0
    for p in bt_files:
        if p == CANON:
            continue
        for name, val in _consts(_tree(p)).items():
            if name in canon:
                same = canon[name] == val
                print(f"  {rel(p)}::{name} = {val!r}")
                print(f"      canonical  bt/optimal_config.py::{name} = {canon[name]!r}"
                      f"   {'(SAME)' if same else '*** DIFFERENT ***'}")
                dupes += 1
    if not dupes:
        print("  (none)")
    print(f"  -> {dupes} duplicate-name constant(s)")

    print()
    print("=" * 78)
    print("2. DEAD IMPORTS inside bt/ (imported, never referenced)")
    print("=" * 78)
    dead = 0
    for p in bt_files:
        for lineno, name in _dead_imports(_tree(p)):
            print(f"  {rel(p)}:{lineno}  {name}")
            dead += 1
    if not dead:
        print("  (none)")
    print(f"  -> {dead} dead import(s)")

    print()
    print("=" * 78)
    print("3. CANONICAL CONSTANTS NEVER READ (cannot move)")
    print("=" * 78)
    dead_canon = 0
    for name in sorted(canon):
        if name in canon_read:
            continue
        hits = [
            rel(p) for p in repo_py
            if p != CANON and name in p.read_text(encoding="utf-8", errors="ignore")
        ]
        if not hits:
            print(f"  bt/optimal_config.py::{name} = {canon[name]!r}   (0 references)")
            dead_canon += 1
    if not dead_canon:
        print("  (none)")
    print(f"  -> {dead_canon} unread canonical constant(s)")

    return 0


if __name__ == "__main__":
    sys.exit(main())
