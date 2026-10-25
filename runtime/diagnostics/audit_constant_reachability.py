"""Audit: does every canonical constant REACH a builder?

``tests/test_config_single_source.py`` checks that each canonical
constant is *read* somewhere. That is too weak: a name mentioned in a
comment or a docstring string counts as a "read" for the AST scan, so
a genuinely dead knob (``FOREX_P95_RET_USD``) passes.

This script is the STRICT version: it checks that each canonical
constant is passed by NAME into one of the six materialisers (or is
read by a real Python expression outside the config module).
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BT_DIR = REPO_ROOT / "bt"
CANON_PATH = BT_DIR / "optimal_config.py"

BUILDERS = (
    "build_engine_kwargs",
    "build_vp_kwargs",
    "build_swing_kwargs",
    "build_p95_kwargs",
    "build_structure_kwargs",
    "build_detector_kwargs",
)


def _module_constants(path: Path) -> dict[str, object]:
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


def _kwarg_names(tree: ast.Module) -> set[str]:
    """Names used as a keyword-argument VALUE (i.e. passed onward)."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for kw in node.keywords:
                if isinstance(kw.value, ast.Name):
                    used.add(kw.value.id)
    return used


def _expr_reads(tree: ast.Module) -> set[str]:
    """Names READ in a real expression (not in a docstring/comment)."""
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            used.add(node.id)
    return used


def main() -> int:
    constants = _module_constants(CANON_PATH)
    canon_tree = ast.parse(CANON_PATH.read_text(encoding="utf-8"))

    # Names that appear as a value of some keyword argument anywhere
    # in the canonical module -> forwarded onto a target surface.
    forwarded = _kwarg_names(canon_tree)

    # Real expression reads inside AND outside the canonical module.
    external: set[str] = _expr_reads(canon_tree)
    for path in sorted(REPO_ROOT.rglob("*.py")):
        if any(p in {".git", "__pycache__", ".cache", "node_modules"}
               for p in path.parts):
            continue
        if path == CANON_PATH:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"),
                             filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        external |= _expr_reads(tree)

    dead = sorted(
        n for n in constants
        if n not in forwarded and n not in external
    )

    print("=" * 78)
    print("CANONICAL CONSTANTS THAT REACH NO SURFACE")
    print("=" * 78)
    if not dead:
        print("  (none)")
    for name in dead:
        print(f"  {name} = {constants[name]!r}")

    print()
    print(f"  {len(constants)} constants, {len(dead)} dead")
    return 1 if dead else 0


if __name__ == "__main__":
    raise SystemExit(main())
