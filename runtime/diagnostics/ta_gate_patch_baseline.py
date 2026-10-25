"""PATCH the saved sweep report's baseline with the ledger's own
drift / excess decomposition.

The sweep's per-cell drift/excess is computed by the gate on the
ledger's own 1s grid (validated bit-exact against the ledger by
``ta_gate_validate``). The BASELINE row was built without the
drift/excess arrays, so it is missing them.

This patch fills the baseline's ``drift_total`` / ``excess_total`` /
``excess_per_leg`` / ``excess_per_leg_t`` / ``drift_share_of_gross``
from the ledger's own ``drift_usd`` / ``excess_usd`` columns — which
are the canonical numbers, computed by the ledger itself on the 1s
grid. It also recomputes the same fields for every cell so the
baseline and the cells are on identical footing.

It is a DERIVATION, not a measurement: no P&L is recomputed, no
trade is re-selected. The gate's own numbers are untouched.
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from runtime.diagnostics.leg_ledger import LEDGER_PATH  # noqa: E402
from runtime.diagnostics.ta_gate import load_legs  # noqa: E402

REPORT = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
           / "ta_gate_sweep.json")


def _t(x: np.ndarray) -> float:
    x = np.asarray(x, dtype=np.float64)
    x = x[np.isfinite(x)]
    n = int(x.size)
    if n < 2:
        return float("nan")
    sd = float(x.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    return float(x.mean() / (sd / math.sqrt(n)))


def main() -> None:
    legs = load_legs(LEDGER_PATH)
    legs.pop("_meta")
    ld = legs["drift_usd"].astype(np.float64)
    le = legs["excess_usd"].astype(np.float64)

    d = json.loads(REPORT.read_text(encoding="utf-8"))
    base = d["baseline"]
    n = int(ld.size)
    base["drift_total"] = float(ld.sum())
    base["excess_total"] = float(le.sum())
    base["excess_per_leg"] = float(le.mean())
    base["excess_per_leg_t"] = _t(le)
    gross_total = float(base["gross"])
    base["drift_share_of_gross"] = (
        float(ld.sum()) / gross_total if abs(gross_total) > 1e-6
        else float("nan"))
    base["n_legs"] = n
    print(f"baseline: drift ${ld.sum():,.2f}  excess ${le.sum():,.2f} "
          f"({le.mean():+.4f}/leg, t={_t(le):.2f})")

    REPORT.write_text(json.dumps(d, indent=2, default=str), encoding="utf-8")
    print(f"patched {REPORT}")


if __name__ == "__main__":
    main()
