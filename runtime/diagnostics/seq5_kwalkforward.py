"""WALK-FORWARD for the RANKED-K form of the 5-event whitelist.

WHY THIS MODULE EXISTS
======================

``seq5_ksweep.py`` showed the trade-count constraint binds HARD: the
full-strength whitelist trades ~8 legs/calendar-day, far below the
30-70/day target (AGENTS.md § 1.1 rule 6). The only way to keep the
whitelist inside the band is to keep MANY signatures -- the top-K by
gross/leg, per side, for K around 48 of 64.

That makes "which signatures to drop" a RANKED decision, and a ranked
decision fitted on the whole corpus is mining. This module asks the
only question that matters: if the RANKING is fitted on one half of
the corpus and applied to the OTHER half, does the out-of-sample
gross still improve, and does the out-of-sample trade count still
land in the band?

The answer is reported for EVERY K, so the K can be chosen on
out-of-sample evidence rather than on the in-sample maximum.

WHAT IS DIFFERENT FROM ``seq5_walkforward.py``
==============================================

That module fits an ADAPTIVE rule ("every signature with positive
gross on the fit half"), so the whitelist SIZE changes from fold to
fold. This module fixes the SIZE (K) and fits only the RANKING, so
the two folds are directly comparable and the size can be swept
against the trade-count band.

Usage
-----
    python -m runtime.diagnostics.seq5_kwalkforward
    python -m runtime.diagnostics.seq5_kwalkforward --min-n 50
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

OUT = Path("runtime/diagnostics/_reports/seq5_kwalkforward.json")


def _rank(sig: np.ndarray, gross: np.ndarray, mask: np.ndarray,
          min_n: int) -> list[str]:
    """Rank the eligible signatures of one side by gross/leg, best first."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return []
    rows: list[tuple[float, str]] = []
    for s in np.unique(sig[idx]):
        k = idx[sig[idx] == s]
        if k.size < min_n:
            continue
        rows.append((float(gross[k].mean()), str(s)))
    rows.sort(key=lambda r: (-r[0], r[1]))
    return [s for _, s in rows]


def _apply(sig: np.ndarray, gross: np.ndarray, net: np.ndarray,
           mask: np.ndarray, keep: set[str]) -> dict[str, float]:
    """Score a whitelist on the legs in ``mask``."""
    idx = np.flatnonzero(mask)
    kept = [i for i in idx if sig[i] in keep]
    base_gross = float(gross[idx].sum())
    base_net = float(net[idx].sum())
    k_gross = float(gross[kept].sum()) if kept else 0.0
    k_net = float(net[kept].sum()) if kept else 0.0
    return {
        "base_legs": int(idx.size),
        "kept_legs": len(kept),
        "base_gross": base_gross,
        "kept_gross": k_gross,
        "d_gross": k_gross - base_gross,
        "base_net": base_net,
        "kept_net": k_net,
        "d_net": k_net - base_net,
        "dropped_n": int(idx.size) - len(kept),
        "dropped_gross": base_gross - k_gross,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, required=True)
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("=" * 78)
    print("K-WALK-FORWARD: does the RANKED-K 5-event whitelist GENERALISE?")
    print("=" * 78)

    payload = load_ledger(args.ledger)
    arr = ledger_arrays(payload)
    gross, net = arr["gross"], arr["net"]
    side = arr["side"]
    days = (pd.Timestamp(payload["score_end"])
            - pd.Timestamp(payload["score_start"])).days
    print(f"  ledger : {payload['score_start']} .. {payload['score_end']}  "
          f"legs={int(gross.size)}  days={days}")

    structure = _structure_for_corpus()
    sig = _signatures(structure, arr["signal_bar"])
    has = sig != ""

    ts = arr["signal_time_ns"]
    median = float(np.median(ts))
    h1, h2 = ts <= median, ~(ts <= median)

    # Per side, the two folds.
    folds = [
        ("H1->H2", h1, h2),
        ("H2->H1", h2, h1),
    ]

    report: dict[str, Any] = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ledger": str(args.ledger),
        "n_legs": int(gross.size),
        "calendar_days": days,
        "min_n": args.min_n,
        "sides": {},
    }

    for side_name, side_mask in (("long", side > 0), ("short", side < 0)):
        print()
        print("=" * 78)
        print(f"{side_name.upper()}  --  K-walk-forward (2 folds by time)")
        print("=" * 78)
        print(f"{'K':>4} | {'H1->H2 d gross':>15} {'H2->H1 d gross':>15} "
              f"{'pooled d gross':>16} {'pooled legs/day':>16} "
              f"{'pooled d net':>14} {'in band':>8}")

        # Fit the ranking on each fold's fit half, once.
        rankings: dict[str, list[str]] = {}
        for fname, fit_m, _ in folds:
            rankings[fname] = _rank(sig, gross, side_mask & has & fit_m,
                                    args.min_n)

        rows_out: list[dict[str, Any]] = []
        kmax = min(len(r) for r in rankings.values())
        for k in range(0, kmax + 1):
            per_fold = []
            for fname, fit_m, test_m in folds:
                keep = set(rankings[fname][:k])
                sc = _apply(sig, gross, net, side_mask & has & test_m, keep)
                sc["fold"] = fname
                per_fold.append(sc)
            pooled_legs = sum(f["kept_legs"] for f in per_fold)
            pooled_dg = sum(f["d_gross"] for f in per_fold)
            pooled_dn = sum(f["d_net"] for f in per_fold)
            pooled_base = sum(f["base_legs"] for f in per_fold)
            lday = pooled_legs / days
            row = {
                "k": k,
                "folds": per_fold,
                "pooled_d_gross": pooled_dg,
                "pooled_d_net": pooled_dn,
                "pooled_kept_legs": pooled_legs,
                "pooled_base_legs": pooled_base,
                "pooled_legs_per_day": lday,
                "in_trade_count_band": bool(30.0 <= lday <= 70.0),
            }
            rows_out.append(row)
            if k % 4 == 0 or k in (kmax,):
                print(f"{k:>4} | {per_fold[0]['d_gross']:>+15,.0f} "
                      f"{per_fold[1]['d_gross']:>+15,.0f} "
                      f"{pooled_dg:>+16,.0f} {lday:>16.2f} "
                      f"{pooled_dn:>+14,.0f} "
                      f"{'YES' if row['in_trade_count_band'] else 'no':>8}")

        # The best in-band K on POOLED OUT-OF-SAMPLE gross.
        in_band = [r for r in rows_out if r["in_trade_count_band"]]
        best = max(in_band, key=lambda r: r["pooled_d_gross"]) if in_band else None
        print()
        if best is None:
            print(f"  {side_name.upper()}: no K lands in the 30-70 band.")
        else:
            print(f"  {side_name.upper()}: BEST OUT-OF-SAMPLE IN-BAND K = "
                  f"{best['k']}  ->  d gross {best['pooled_d_gross']:+,.0f}, "
                  f"d net {best['pooled_d_net']:+,.0f}, "
                  f"{best['pooled_legs_per_day']:.2f} legs/day")
        report["sides"][side_name] = {
            "rankings": rankings,
            "rows": rows_out,
            "best_in_band": best,
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                        encoding="utf-8")
    print()
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq5_kwalkforward",
        params={
            "ledger": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "min_n": args.min_n,
        },
        summary={
            "n_legs": int(gross.size),
            "long_best_k": (report["sides"]["long"]["best_in_band"] or {}).get("k"),
            "short_best_k": (report["sides"]["short"]["best_in_band"] or {}).get("k"),
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
