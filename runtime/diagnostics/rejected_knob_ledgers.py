"""Build the `trade_sides='long'` and `structure_gate=True` ledgers.

WHY
===

AGENTS.md § 6 lists two knobs as **NOT PROMOTED**, and both
justifications cited a **monthly market-correlation** regression:

* ``trade_sides="long"``  — "raises market correlation to +0.464"
* ``structure_gate``      — "leaves market correlation untouched"

Those numbers came from a monthly regression with n = 13..31, where the
correlation's standard error is ~0.32 in Fisher-z space. ``r = +0.464``
at n = 13 is **t = 1.86** — indistinguishable from zero. So the
justifications do not hold, and the two knobs have to be re-grounded
on a number that does.

This driver builds the two ledgers so the DAILY direction-dependence
test (``runtime/diagnostics/daily_direction_dependence.py``) can
measure them on the corpus at n = 577 days, and so the per-leg
passive-hold/excess decomposition can speak to them directly.

It is a VARIANT builder: it never writes the canonical ledger path.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    DEFAULT_DATA,
    DEFAULT_END,
    DEFAULT_START,
    WARMUP_DAYS,
    build_ledger,
)

OUT_DIR = Path("runtime/diagnostics/_reports")

ARMS: tuple[tuple[str, dict[str, object]], ...] = (
    ("trade_sides_long", {"trade_sides": "long"}),
    ("structure_gate_on", {"structure_gate": True}),
)


def main() -> None:
    for label, overrides in ARMS:
        out = OUT_DIR / f"leg_ledger_{label}.json"
        print(f"== {label} ==", flush=True)
        build_ledger(
            data=DEFAULT_DATA,
            start=DEFAULT_START,
            end=DEFAULT_END,
            warmup_days=WARMUP_DAYS,
            engine_overrides=dict(overrides),
            out_path=out,
            refresh=True,
            verbose=True,
        )
        print(f"-> {out}", flush=True)


if __name__ == "__main__":
    main()
