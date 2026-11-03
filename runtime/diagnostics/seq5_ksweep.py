"""HOW MANY 5-EVENT SIGNATURES TO KEEP -- the trade-count/size trade-off.

WHY THIS EXISTS
===============

The 5-event whitelist, applied at full strength, collapses the book from
~40 legs/calendar-day to ~8.5 -- far below the 30-70/day target
(AGENTS.md § 1.1 rule 6). A knob that only trades 8 times a day is a
different strategy, not a better version of this one.

A whitelist is a RANKED set: keep the K signatures with the best
gross/leg, drop the rest. This module sweeps K and reports, for each
K, the legs kept and the gross -- so the trade-count band and the
P&L can be chosen TOGETHER instead of one at the expense of the other.

POST-HOC ONLY. Like ``seq5_table.py``, this filters the MEASURED legs;
it does not re-run the book. It is a SIZING tool -- it picks K -- and
the chosen K must then be re-measured END TO END (the conflict policy,
the same-direction cap and the contra-close netting all move when the
leg population moves).

Usage
-----
    python -m runtime.diagnostics.seq5_ksweep
    python -m runtime.diagnostics.seq5_ksweep --min-n 100
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
import pandas as pd  # noqa: E402

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    load_ledger,
    ledger_arrays,
)
from runtime.diagnostics.seq5_table import (  # noqa: E402
    _signatures,
    _structure_for_corpus,
)
from runtime.results import registry  # noqa: E402

OUT = Path("runtime/diagnostics/_reports/seq5_ksweep.json")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--min-n", type=int, default=50,
                    help="Minimum legs per signature to be eligible.")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("=" * 78)
    print("5-EVENT WHITELIST SIZE SWEEP  (how many signatures to keep)")
    print("=" * 78)

    payload = load_ledger(args.ledger)
    arr = ledger_arrays(payload)
    gross, net = arr["gross"], arr["net"]
    side = arr["side"]
    days = (pd.Timestamp(payload["score_end"])
            - pd.Timestamp(payload["score_start"])).days
    n_all = int(gross.size)
    print(f"  ledger : {payload['score_start']} .. {payload['score_end']}  "
          f"legs={n_all}  days={days}  "
          f"legs/day={n_all / days:.2f}")

    structure = _structure_for_corpus()
    sig = _signatures(structure, arr["signal_bar"])
    has = sig != ""
    print(f"  tagged legs (>=5 structure events): {int(has.sum())}")

    # Per-signature gross/leg, per side, over the tagged population.
    stats: dict[str, dict[str, tuple[int, float]]] = {}
    for side_name, side_mask in (("long", side > 0), ("short", side < 0)):
        m = has & side_mask
        idx = np.flatnonzero(m)
        rows: dict[str, tuple[int, float]] = {}
        for s in np.unique(sig[idx]):
            k = idx[sig[idx] == s]
            rows[str(s)] = (int(k.size), float(gross[k].mean()))
        stats[side_name] = rows

    # Rank each side by gross/leg, best first. Eligibility: min_n legs.
    ranked: dict[str, list[str]] = {}
    for side_name, rows in stats.items():
        elig = [s for s, (n, _) in rows.items() if n >= args.min_n]
        elig.sort(key=lambda s: -rows[s][1])
        ranked[side_name] = elig

    print(f"  eligible signatures (n>={args.min_n}): "
          f"long={len(ranked['long'])} short={len(ranked['short'])}")
    print()

    # ── The K sweep ─────────────────────────────────────────────
    print("=" * 78)
    print("K SWEEP -- keep the top-K signatures per side, by gross/leg")
    print("=" * 78)
    print(f"{'K':>4} {'long kept':>10} {'short kept':>10} {'kept':>8} "
          f"{'legs/day':>9} {'gross':>11} {'d gross':>10} {'net':>11} "
          f"{'g/leg':>8} {'in band':>8}")

    base_gross = float(gross[has].sum())
    base_net = float(net[has].sum())
    rows_out: list[dict[str, Any]] = []
    kmax = max(len(ranked["long"]), len(ranked["short"]))
    for k in range(0, kmax + 1):
        keep_l = set(ranked["long"][:k])
        keep_s = set(ranked["short"][:k])
        keep_mask = np.zeros(n_all, dtype=bool)
        for s_name, keep, smask in (("long", keep_l, side > 0),
                                    ("short", keep_s, side < 0)):
            m = has & smask
            idx = np.flatnonzero(m)
            if idx.size == 0:
                continue
            sel = [i for i in idx if sig[i] in keep]
            keep_mask[sel] = True
        n_kept = int(keep_mask.sum())
        k_gross = float(gross[keep_mask].sum())
        k_net = float(net[keep_mask].sum())
        lday = n_kept / days
        row = {
            "k": k,
            "long_kept": int((keep_mask & (side > 0)).sum()),
            "short_kept": int((keep_mask & (side < 0)).sum()),
            "kept": n_kept,
            "legs_per_day": lday,
            "gross": k_gross,
            "d_gross": k_gross - base_gross,
            "net": k_net,
            "gross_per_leg": (k_gross / n_kept) if n_kept else 0.0,
            "in_trade_count_band": bool(30.0 <= lday <= 70.0),
        }
        rows_out.append(row)
        print(f"{k:>4} {row['long_kept']:>10} {row['short_kept']:>10} "
              f"{n_kept:>8} {lday:>9.2f} {k_gross:>+11,.0f} "
              f"{row['d_gross']:>+10,.0f} {k_net:>+11,.0f} "
              f"{row['gross_per_leg']:>+8.4f} "
              f"{'YES' if row['in_trade_count_band'] else 'no':>8}")

    print()
    print(f"  control (no whitelist): {n_all} legs, "
          f"{n_all / days:.2f}/day, gross {base_gross:+,.0f}, "
          f"net {base_net:+,.0f}")

    # ── The best K that stays in the trade-count band ───────────
    in_band = [r for r in rows_out if r["in_trade_count_band"]]
    best = max(in_band, key=lambda r: r["gross"]) if in_band else None
    print()
    if best is None:
        print("  !! NO K lands in the 30-70 legs/day band. The whitelist "
              "cannot be promoted without breaking the trade-count target.")
    else:
        print(f"  BEST IN-BAND K = {best['k']}: "
              f"{best['kept']} legs ({best['legs_per_day']:.2f}/day), "
              f"gross {best['gross']:+,.0f} (d {best['d_gross']:+,.0f}), "
              f"net {best['net']:+,.0f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps({
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ledger": str(args.ledger),
        "n_legs": n_all,
        "calendar_days": days,
        "min_n": args.min_n,
        "ranked_long": ranked["long"],
        "ranked_short": ranked["short"],
        "rows": rows_out,
        "best_in_band": best,
    }, indent=2, default=str), encoding="utf-8")
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq5_ksweep",
        params={
            "ledger": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "min_n": args.min_n,
        },
        summary={
            "n_legs": n_all,
            "base_gross": round(base_gross, 6),
            "best_in_band_k": best["k"] if best else None,
            "best_in_band_legs_per_day": (
                round(best["legs_per_day"], 4) if best else None),
            "best_in_band_gross": round(best["gross"], 6) if best else None,
        },
        payload={"rows": rows_out, "ranked_long": ranked["long"],
                 "ranked_short": ranked["short"]},
    )


if __name__ == "__main__":
    main()
