"""WHICH (side, last-event) drop set should the canonical recipe carry?

The 2026-10-24 measurement that produced the canonical rule
``{"short|c", "short|C"}`` was run on the NARROW book (vol gate 3.0,
8 monthly slices, ~700 legs). The canonical vol gate is now 1.0 -- the
WIDE book, ~95k legs -- and the rule has never been re-measured on it.

``seq1_drop_validate.py`` shows the rule's problem: the buckets it
drops are NOT reliably negative on the wide book, and the one that IS
reliably negative (``short|B``) it does NOT drop.

This module scores every candidate drop set on the wide book, on both
halves of the corpus, and reports:

* the GROSS delta (the signal)
* the NET delta (the fee saving -- kept separate, because a rule that
  only moves net is a fee-saving artefact, not a signal)
* the kept book's gross per leg (does the SURVIVING book get better?)

Usage
-----
    python -m runtime.diagnostics.seq1_drop_candidates
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    LEDGER_PATH,
    load_ledger,
    ledger_arrays,
)
from runtime.diagnostics.seq1_drop_validate import (  # noqa: E402
    CANONICAL_DROP,
    _last_event,
    _score,
)
from runtime.diagnostics.seq5_table import (  # noqa: E402
    _structure_for_corpus,
    _signatures,
)
from runtime.results import registry  # noqa: E402

OUT = Path("runtime/diagnostics/_reports/seq1_drop_candidates.json")

CANDIDATES: dict[str, set[tuple[str, str]]] = {
    "none": set(),
    "canonical_short_cC": set(CANONICAL_DROP),
    "short_B": {("short", "B")},
    "short_BcC": {("short", "B"), ("short", "c"), ("short", "C")},
    "short_Bb": {("short", "B"), ("short", "b")},
    "short_all4": {("short", "b"), ("short", "B"),
                   ("short", "c"), ("short", "C")},
    "short_B_long_c": {("short", "B"), ("long", "c")},
    "short_B_long_cC": {("short", "B"), ("long", "c"), ("long", "C")},
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("=" * 78)
    print("CANDIDATE (side, last-event) DROP SETS, SCORED ON THE WIDE BOOK")
    print("=" * 78)

    payload = load_ledger(args.ledger)
    arr = ledger_arrays(payload)
    gross, net, fee = arr["gross"], arr["net"], arr["fee"]
    side = arr["side"]
    print(f"  ledger : {payload['score_start']} .. {payload['score_end']}  "
          f"legs={int(gross.size)}")
    print(f"  overrides: {payload.get('engine_overrides') or '{}'}")
    print()

    structure = _structure_for_corpus()
    sig = _signatures(structure, arr["signal_bar"])
    last = _last_event(sig)
    has = sig != ""

    ts = arr["signal_time_ns"]
    median = float(np.median(ts))
    h1 = ts <= median
    h2 = ~h1

    report: dict[str, Any] = {
        "ledger": {
            "path": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "n_legs": int(gross.size),
        },
        "candidates": {},
    }

    for s_name, s_val in (("long", 1), ("short", -1)):
        m_side = has & (side == s_val)
        print("=" * 78)
        print(f"{s_name.upper()}")
        print("=" * 78)
        print(f"  {'drop set':<20} {'legs':>13} {'d gross':>11} {'d net':>11} "
              f"{'kept g/leg':>11} {'base g/leg':>11} {'H1 d gross':>11} "
              f"{'H2 d gross':>11}")
        rows = []
        for name, drop in CANDIDATES.items():
            d = {k for k in drop if k[0] == s_name}
            if not d and name not in ("none",):
                continue
            sc = _score(sig, last, gross, net, fee, side, m_side, d)
            h1_sc = _score(sig, last, gross, net, fee, side, m_side & h1, d)
            h2_sc = _score(sig, last, gross, net, fee, side, m_side & h2, d)
            sc["h1_d_gross"] = h1_sc["d_gross"]
            sc["h2_d_gross"] = h2_sc["d_gross"]
            sc["drop"] = sorted(f"{s}|{e}" for s, e in d)
            rows.append(sc)
            label = ",".join(sc["drop"]) or "(none)"
            print(f"  {label:<20} {sc['base_legs']:>6}->{sc['kept_legs']:>6} "
                  f"{sc['d_gross']:>+11,.0f} {sc['d_net']:>+11,.0f} "
                  f"{sc['kept_g_per_leg']:>+11.4f} "
                  f"{sc['base_g_per_leg']:>+11.4f} "
                  f"{sc['h1_d_gross']:>+11,.0f} {sc['h2_d_gross']:>+11,.0f}")
        report["candidates"][s_name] = rows
        print()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                        encoding="utf-8")
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq1_drop_candidates",
        params={
            "ledger": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "candidates": sorted(CANDIDATES),
        },
        summary={
            "n_legs": int(gross.size),
            "short_canonical_d_gross": next(
                (r["d_gross"] for r in report["candidates"]["short"]
                 if r["drop"] == ["short|C", "short|c"]), None),
            "short_B_d_gross": next(
                (r["d_gross"] for r in report["candidates"]["short"]
                 if r["drop"] == ["short|B"]), None),
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
