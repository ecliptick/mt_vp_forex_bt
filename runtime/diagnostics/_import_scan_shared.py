"""Shared-process import scan — mirrors
``tests/test_no_dead_imports.py::test_every_module_imports_cleanly``
exactly, but reports each module's status as it goes.

Why this file exists
--------------------
``_import_scan.py`` imports each module in its OWN subprocess, so it
never sees **shared-process state** — a module that mutates
``sys.path`` / ``sys.modules`` at import time, or one whose import
depends on a module imported earlier.

The pytest guard imports them all in ONE process (the real semantics),
but a failure there gives you no clue WHICH module broke. This scan
reproduces the shared-process semantics and writes each module's
status to disk incrementally, so the culprit is identifiable.

It is also the reason the import-time rule in AGENTS.md matters: when
a module does a multi-minute backtest at import scope, this scan
stalls on it (and the pytest guard OOMs) — which is exactly the
symptom the two new AST guards now prevent.

Usage:
    python -m runtime.diagnostics._import_scan_shared
"""
from __future__ import annotations

import contextlib
import importlib
import io
import pathlib
import sys
import time
import traceback

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_OUT = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
        / "_import_scan_shared.txt")


def _all_python_files() -> list[pathlib.Path]:
    out: list[pathlib.Path] = []
    for pat in ("bt/**/*.py", "runtime/**/*.py", "tests/**/*.py"):
        out.extend(
            p for p in _REPO_ROOT.glob(pat) if "__pycache__" not in p.parts
        )
    return out


def main() -> None:
    _OUT.parent.mkdir(parents=True, exist_ok=True)
    _OUT.write_text("", encoding="utf-8")
    failures: list[str] = []
    n_ok = 0
    t0 = time.time()
    for path in _all_python_files():
        rel = path.relative_to(_REPO_ROOT)
        if rel.parts[0] == "tests":
            continue
        mod = ".".join(rel.with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        buf = io.StringIO()
        t1 = time.time()
        try:
            with contextlib.redirect_stdout(buf):
                with contextlib.redirect_stderr(buf):
                    importlib.import_module(mod)
            n_ok += 1
            line = f"OK   {mod}  ({time.time() - t1:.1f}s)\n"
        except BaseException as exc:  # noqa: BLE001
            tb = traceback.format_exc(limit=8)
            failures.append(f"{mod}\n{tb}")
            line = (f"FAIL {mod}  ({time.time() - t1:.1f}s)  "
                    f"{type(exc).__name__}: {exc}\n")
            print(line, end="", flush=True)
        with _OUT.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write(f"     elapsed {time.time() - t0:.1f}s\n")
            fh.flush()

    with _OUT.open("a", encoding="utf-8") as fh:
        fh.write("\n" + "=" * 70 + "\n")
        fh.write(f"IMPORTED OK: {n_ok}\nFAILURES: {len(failures)}\n\n")
        for f in failures:
            fh.write(f + "\n\n")
    print(f"IMPORTED OK: {n_ok}  FAILURES: {len(failures)}")
    print(f"written: {_OUT}")


if __name__ == "__main__":
    main()
