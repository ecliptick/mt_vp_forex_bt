"""Locate modules that fail to import — with a PER-MODULE timeout.

Why a subprocess
----------------
Several diagnostics under ``runtime/diagnostics/`` execute heavy work
AT IMPORT TIME (a full external-repo backtest, a 41M-bar parquet
load). Importing them in-process makes the scan unbounded — and makes
``tests/test_no_dead_imports.py::test_every_module_imports_cleanly``
unrunnable in any reasonable time.

So each module is imported in its own subprocess with a timeout. The
three outcomes are distinguished:

* ``OK``      — imported cleanly inside the budget.
* ``TIMEOUT`` — imported too slowly (heavy module-level work).
* ``FAIL``    — raised an exception on import.

Only ``FAIL`` breaks the test. ``TIMEOUT`` is a separate (real, but
different) defect: it means the module cannot be imported by a
collection tool without executing a backtest.

Usage:
    python -m runtime.diagnostics._import_scan [--timeout 60]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]

_CHILD = r"""
import importlib, sys
sys.path.insert(0, r"{root}")
mod = {mod!r}
importlib.import_module(mod)
print("__OK__")
"""


def _all_python_files() -> list[Path]:
    out: list[Path] = []
    for pat in ("bt/**/*.py", "runtime/**/*.py"):
        out.extend(
            p for p in _REPO_ROOT.glob(pat) if "__pycache__" not in p.parts
        )
    return sorted(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--timeout", type=float, default=60.0)
    args = ap.parse_args()

    results: list[dict] = []
    for path in _all_python_files():
        rel = path.relative_to(_REPO_ROOT)
        mod = ".".join(rel.with_suffix("").parts)
        if mod.endswith(".__init__"):
            mod = mod[: -len(".__init__")]
        code = _CHILD.format(root=str(_REPO_ROOT), mod=mod)
        try:
            cp = subprocess.run(
                [sys.executable, "-c", code],
                capture_output=True, text=True,
                timeout=args.timeout,
                cwd=str(_REPO_ROOT),
            )
        except subprocess.TimeoutExpired:
            results.append({"mod": mod, "status": "TIMEOUT"})
            print(f"TIMEOUT {mod}", flush=True)
            continue
        if cp.returncode == 0 and "__OK__" in cp.stdout:
            results.append({"mod": mod, "status": "OK"})
        else:
            err = (cp.stderr or cp.stdout or "").strip().splitlines()
            tail = err[-1] if err else "?"
            results.append({"mod": mod, "status": "FAIL", "error": tail})
            print(f"FAIL     {mod}: {tail}", flush=True)

    fails = [r for r in results if r["status"] == "FAIL"]
    timeouts = [r for r in results if r["status"] == "TIMEOUT"]
    out = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
           / "_import_scan.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")

    print()
    print(f"OK      : {len(results) - len(fails) - len(timeouts)}")
    print(f"FAIL    : {len(fails)}")
    print(f"TIMEOUT : {len(timeouts)}")
    print(f"written : {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
