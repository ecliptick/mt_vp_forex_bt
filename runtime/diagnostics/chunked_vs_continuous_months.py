"""Per-month, per-ledger net. The apples-to-apples check.

The chunked monthly diagnostics reported the recipe as net-POSITIVE
(+$74.51 over 15 months with contra_close ON). The continuous corpus
says net-NEGATIVE. Those cannot both describe the same recipe, so
this diagnostic puts the two on the same axis:

    for each month: chunked net vs continuous net, same calendar window.

Usage:
    python -m runtime.diagnostics/chunked_vs_continuous_months
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

REPORTS = Path("runtime/diagnostics/_reports")
OUT = REPORTS / "chunked_vs_continuous_months.json"

# The months the chunked diagnostics used.
MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12", "2025-01",
    "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-05", "2026-06",
]

LEDGERS = {
    "continuous": REPORTS / "leg_ledger.json",
    "continuous_contra": REPORTS / "leg_ledger_contra.json",
}

# The recipe each slot is EXPECTED to hold. The two arms are compared
# as a pair, so a swapped ledger would silently invert the
# comparison. See ``_monthly`` for the check.
LEDGER_EXPECTATIONS = {
    "leg_ledger.json": {},
    "leg_ledger_contra.json": {"contra_close": "True",
                               "contra_close_spread_usd": "1.0"},
}

# The chunked numbers, as reported by the (monthly) diagnostics.
CHUNKED = {
    "off": {"legs": 899, "gross": 375.03, "fees": 414.31, "net": -39.28},
    "on": {"legs": 904, "gross": 488.82, "fees": 414.31, "net": 74.51},
}


def _monthly(path: Path) -> dict[str, dict[str, float]]:
    d = json.loads(path.read_text(encoding="utf-8"))
    # Each ledger here is a DIFFERENT recipe by design (the canonical
    # one and the contra_close variant), so the overrides are checked
    # against what THIS slot expects rather than against the
    # canonical recipe. What must never happen is a ledger whose
    # identity is unknown being read as one of these two.
    expected = LEDGER_EXPECTATIONS[path.name]
    recorded = d.get("engine_overrides") or {}
    if recorded != expected:
        raise SystemExit(
            f"{path} carries engine_overrides {recorded!r}, but this "
            f"diagnostic expects {expected!r} for that slot. The two "
            "arms are compared as a PAIR, so a swapped ledger would "
            "silently invert the comparison."
        )
    legs = d["legs"]
    ts = pd.to_datetime(legs["signal_time_ns"], unit="ns", utc=True)
    ym = ts.strftime("%Y-%m")
    g = np.asarray(legs["gross"], dtype=float)
    f = np.asarray(legs["fee"], dtype=float)
    out: dict[str, dict[str, float]] = {}
    for m in MONTHS:
        sel = (ym == m)
        out[m] = {
            "legs": int(sel.sum()),
            "gross": float(g[sel].sum()),
            "fees": float(f[sel].sum()),
            "net": float(g[sel].sum() - f[sel].sum()),
        }
    return out


def main() -> None:
    print("== chunked (monthly diagnostics) vs CONTINUOUS (ledger) ==")
    print()
    cont = _monthly(LEDGERS["continuous"])
    contra = _monthly(LEDGERS["continuous_contra"])

    print(f"{'month':9} | {'CONT legs':>10} {'gross':>9} {'net':>9} | "
          f"{'+CONTRA legs':>13} {'net':>9}")
    print("-" * 70)
    tot = {"legs": 0, "gross": 0.0, "fees": 0.0, "net": 0.0}
    tot2 = {"legs": 0, "gross": 0.0, "fees": 0.0, "net": 0.0}
    for m in MONTHS:
        a, b = cont[m], contra[m]
        tot["legs"] += a["legs"]
        tot["gross"] += a["gross"]
        tot["fees"] += a["fees"]
        tot["net"] += a["net"]
        tot2["legs"] += b["legs"]
        tot2["gross"] += b["gross"]
        tot2["fees"] += b["fees"]
        tot2["net"] += b["net"]
        print(f"{m:9} | {a['legs']:>10} {a['gross']:>9,.2f} {a['net']:>9,.2f} "
              f"| {b['legs']:>13} {b['net']:>9,.2f}")
    print("-" * 70)
    print(f"{'TOTAL':9} | {tot['legs']:>10} {tot['gross']:>9,.2f} "
          f"{tot['net']:>9,.2f} | {tot2['legs']:>13} {tot2['net']:>9,.2f}")
    print()
    print(f"CHUNKED   (contra OFF): legs={CHUNKED['off']['legs']} "
          f"gross=${CHUNKED['off']['gross']:,.2f} net=${CHUNKED['off']['net']:,.2f}")
    print(f"CHUNKED   (contra ON ): legs={CHUNKED['on']['legs']} "
          f"gross=${CHUNKED['on']['gross']:,.2f} net=${CHUNKED['on']['net']:,.2f}")
    print(f"CONTINUOUS(contra OFF): legs={tot['legs']} "
          f"gross=${tot['gross']:,.2f} net=${tot['net']:,.2f}")
    print(f"CONTINUOUS(contra ON ): legs={tot2['legs']} "
          f"gross=${tot2['gross']:,.2f} net=${tot2['net']:,.2f}")
    print()
    print("delta(contra ON - OFF):")
    print(f"  chunked   : ${CHUNKED['on']['net'] - CHUNKED['off']['net']:>10,.2f}")
    print(f"  continuous: ${tot2['net'] - tot['net']:>10,.2f}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "months": MONTHS,
        "continuous": cont,
        "continuous_contra": contra,
        "continuous_totals": tot,
        "continuous_contra_totals": tot2,
        "chunked": CHUNKED,
    }, indent=2), encoding="utf-8")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
