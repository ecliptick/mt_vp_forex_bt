"""WALK-FORWARD validation of the 5-event per-side whitelist.

WHY THIS MODULE EXISTS
======================

``seq5_table.py`` reports, per side, the realised P&L of every trailing
5-event BoS/CHoCH signature. Reading the best rows off that table and
calling them a whitelist is **mining**: there are 4**5 = 1024 possible
shapes and 64 of them occur often enough to measure, so the top rows are
partly luck.

This module answers the two questions that decide whether the whitelist
is real:

1. **Does it GENERALISE?** Fit the whitelist on ONE half of the corpus
   (by time) and score it on the OTHER half, both ways round. The
   in-sample table is not evidence; the out-of-sample delta is.

2. **Is it better than CHANCE?** A whitelist of size k drawn at random
   from the 64 measurable signatures will also produce a positive
   out-of-sample delta SOME of the time. This module builds that NULL
   DISTRIBUTION and reports the real whitelist's percentile in it.

Both are computed on the LEGS, with the same execution model on both
arms (AGENTS.md § 1.1 rule 4: a control must be exchangeable with
what it controls for).

WHAT THIS DOES NOT DO
=====================

It does NOT re-run the engine. The whitelist is applied POST-HOC to the
measured legs, so the conflict policy, the same-direction cap and the
contra-close netting are NOT re-evaluated. A whitelist that drops 85%
of the short side changes the book materially, so an engine-side
implementation must be measured END TO END before it ships.

Usage
-----
    python -m runtime.diagnostics.seq5_walkforward
    python -m runtime.diagnostics.seq5_walkforward --null-iters 5000
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
from runtime.diagnostics.seq5_table import (  # noqa: E402
    _structure_for_corpus,
    _signatures,
)
from runtime.results import registry  # noqa: E402

OUT = Path("runtime/diagnostics/_reports/seq5_walkforward.json")


def _tstat(vals: np.ndarray) -> float:
    if vals.size < 2:
        return 0.0
    sd = float(vals.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    return float(vals.mean() / (sd / np.sqrt(vals.size)))


def _fit(sig: np.ndarray, gross: np.ndarray, mask: np.ndarray,
         min_n: int) -> set[str]:
    """Fit the whitelist on the legs in ``mask``.

    Admission: at least ``min_n`` legs AND positive GROSS per leg.
    GROSS, not net -- a bucket that is only net-positive because it
    trades less is not a signal.
    """
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return set()
    out: set[str] = set()
    for s in np.unique(sig[idx]):
        m = idx[sig[idx] == s]
        if m.size >= min_n and float(gross[m].sum()) > 0.0:
            out.add(str(s))
    return out


def _apply(sig: np.ndarray, gross: np.ndarray, net: np.ndarray,
           mask: np.ndarray, keep: set[str]) -> dict[str, float]:
    """Score a whitelist on the legs in ``mask``."""
    idx = np.flatnonzero(mask)
    kept = [i for i in idx if sig[i] in keep]
    dropped = [i for i in idx if sig[i] not in keep]
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
        "dropped_n": len(dropped),
        "dropped_gross": float(gross[dropped].sum()) if dropped else 0.0,
        "dropped_t": _tstat(gross[dropped]) if dropped else 0.0,
        "kept_gross_per_leg": (k_gross / len(kept)) if kept else 0.0,
        "base_gross_per_leg": (base_gross / idx.size) if idx.size else 0.0,
    }


def _null_distribution(sig: np.ndarray, gross: np.ndarray, net: np.ndarray,
                       fit_mask: np.ndarray, test_mask: np.ndarray,
                       keep_size: int, iters: int, rng: np.random.Generator,
                       min_n: int) -> np.ndarray:
    """The out-of-sample delta of RANDOM whitelists of the same size.

    Each iteration: draw ``keep_size`` signatures AT RANDOM from the
    measurable universe (the signatures that pass the ``min_n`` floor
    on the FIT half), then score that random whitelist on the TEST
    half. The resulting distribution is the "this many trades traded
    less" null: a random whitelist's out-of-sample gain is mostly the
    fee saving from dropping legs, and the real whitelist has to beat
    that to be a signal.
    """
    fit_idx = np.flatnonzero(fit_mask)
    universe = [str(s) for s in np.unique(sig[fit_idx])
                if int((sig[fit_idx] == s).sum()) >= min_n]
    if not universe or keep_size <= 0:
        return np.zeros(0)
    out = np.zeros(iters, dtype=np.float64)
    for i in range(iters):
        pick = set(rng.choice(universe, size=min(keep_size, len(universe)),
                               replace=False).tolist())
        out[i] = _apply(sig, gross, net, test_mask, pick)["d_gross"]
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--min-n", type=int, default=100)
    ap.add_argument("--null-iters", type=int, default=2000)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("=" * 78)
    print("WALK-FORWARD: does the 5-event per-side whitelist GENERALISE?")
    print("=" * 78)

    payload = load_ledger(args.ledger)
    arr = ledger_arrays(payload)
    gross, net = arr["gross"], arr["net"]
    print(f"  ledger : {payload['score_start']} .. {payload['score_end']}  "
          f"legs={int(gross.size)}")
    print(f"  overrides: {payload.get('engine_overrides') or '{}'}")
    print()

    structure = _structure_for_corpus()
    sig = _signatures(structure, arr["signal_bar"])
    has = sig != ""

    ts = arr["signal_time_ns"]
    median = float(np.median(ts))
    h1 = ts <= median
    h2 = ~h1

    rng = np.random.default_rng(20261103)

    report: dict[str, Any] = {"min_n": args.min_n,
                              "null_iters": args.null_iters,
                              "ledger": {
                                  "path": str(args.ledger),
                                  "score_start": payload["score_start"],
                                  "score_end": payload["score_end"],
                                  "engine_overrides": payload.get(
                                      "engine_overrides"),
                                  "n_legs": int(gross.size),
                              },
                              "sides": {}}

    for side, side_name in (("long", "long"), ("short", "short")):
        side_m = has & (arr["side"] > 0 if side == "long"
                        else arr["side"] < 0)
        print("=" * 78)
        print(f"{side_name.upper()}  --  2-fold walk-forward")
        print("=" * 78)
        print(f"  {'fold':<22} {'fit n':>6} {'test legs':>10} "
              f"{'kept':>7} {'base gross':>12} {'wl gross':>11} "
              f"{'d gross':>10} {'d net':>10} {'drop t':>8}")
        folds = []
        for fold_name, fit_m, test_m in (
            ("fit H1 -> test H2", side_m & h1, side_m & h2),
            ("fit H2 -> test H1", side_m & h2, side_m & h1),
        ):
            keep = _fit(sig, gross, fit_m, args.min_n)
            sc = _apply(sig, gross, net, test_m, keep)
            sc["fold"] = fold_name
            sc["keep_size"] = len(keep)
            folds.append(sc)
            print(f"  {fold_name:<22} {len(keep):>6} {sc['base_legs']:>10} "
                  f"{sc['kept_legs']:>7} {sc['base_gross']:>12,.0f} "
                  f"{sc['kept_gross']:>11,.0f} {sc['d_gross']:>+10,.0f} "
                  f"{sc['d_net']:>+10,.0f} {sc['dropped_t']:>+8.2f}")
        print()

        # ── The null distribution ────────────────────────────────
        # For each fold, the random-whitelist null of the SAME size.
        print("  -- null distribution (random whitelists of the same size) --")
        null_stats = []
        for sc, fit_m, test_m in (
            (folds[0], side_m & h1, side_m & h2),
            (folds[1], side_m & h2, side_m & h1),
        ):
            null = _null_distribution(sig, gross, net, fit_m, test_m,
                                      sc["keep_size"], args.null_iters,
                                      rng, args.min_n)
            if null.size == 0:
                continue
            pct = float((null < sc["d_gross"]).mean())
            null_stats.append({
                "fold": sc["fold"],
                "real_d_gross": sc["d_gross"],
                "null_mean": float(null.mean()),
                "null_p95": float(np.percentile(null, 95)),
                "null_p99": float(np.percentile(null, 99)),
                "real_percentile": pct,
            })
            print(f"  {sc['fold']:<22} real d gross ${sc['d_gross']:>+10,.0f}  "
                  f"null mean ${null.mean():>+10,.0f}  "
                  f"null p95 ${np.percentile(null, 95):>+10,.0f}  "
                  f"null p99 ${np.percentile(null, 99):>+10,.0f}  "
                  f"-> percentile {pct * 100:.1f}%")
        print()

        # ── The pooled OOS verdict ───────────────────────────────
        # Both folds together = the whole corpus, scored OUT OF
        # SAMPLE (each half was scored by a whitelist fitted on the
        # OTHER half).
        pooled = {
            "test_legs": sum(f["base_legs"] for f in folds),
            "kept_legs": sum(f["kept_legs"] for f in folds),
            "base_gross": sum(f["base_gross"] for f in folds),
            "kept_gross": sum(f["kept_gross"] for f in folds),
            "d_gross": sum(f["d_gross"] for f in folds),
            "base_net": sum(f["base_net"] for f in folds),
            "kept_net": sum(f["kept_net"] for f in folds),
            "d_net": sum(f["d_net"] for f in folds),
        }
        pooled["base_gross_per_leg"] = (
            pooled["base_gross"] / pooled["test_legs"]
            if pooled["test_legs"] else 0.0)
        pooled["kept_gross_per_leg"] = (
            pooled["kept_gross"] / pooled["kept_legs"]
            if pooled["kept_legs"] else 0.0)
        pooled["fee_per_leg_kept"] = (
            (pooled["kept_gross"] - pooled["kept_net"]) / pooled["kept_legs"]
            if pooled["kept_legs"] else 0.0)
        pooled["kept_gross_over_fee"] = (
            pooled["kept_gross_per_leg"] / pooled["fee_per_leg_kept"]
            if pooled["fee_per_leg_kept"] > 0 else 0.0)

        print("  -- POOLED OUT-OF-SAMPLE (both folds) --")
        print(f"  legs {pooled['test_legs']:>7} -> {pooled['kept_legs']:>7}  "
              f"({pooled['kept_legs'] / max(pooled['test_legs'], 1) * 100:.1f}%)")
        print(f"  gross ${pooled['base_gross']:>+12,.0f} -> "
              f"${pooled['kept_gross']:>+12,.0f}  "
              f"(d ${pooled['d_gross']:>+11,.0f})")
        print(f"  net   ${pooled['base_net']:>+12,.0f} -> "
              f"${pooled['kept_net']:>+12,.0f}  "
              f"(d ${pooled['d_net']:>+11,.0f})")
        print(f"  gross/leg ${pooled['base_gross_per_leg']:>+8.4f} -> "
              f"${pooled['kept_gross_per_leg']:>+8.4f}   "
              f"fee/leg ${pooled['fee_per_leg_kept']:.4f}   "
              f"gross/fee {pooled['kept_gross_over_fee']:.3f}x")
        print()

        report["sides"][side_name] = {
            "folds": folds,
            "null": null_stats,
            "pooled_oos": pooled,
        }

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                        encoding="utf-8")
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq5_walkforward",
        params={
            "ledger": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "min_n": args.min_n,
            "null_iters": args.null_iters,
        },
        summary={
            "n_legs": int(gross.size),
            "long_oos_d_gross": round(
                report["sides"]["long"]["pooled_oos"]["d_gross"], 6),
            "short_oos_d_gross": round(
                report["sides"]["short"]["pooled_oos"]["d_gross"], 6),
            "long_kept_legs": report["sides"]["long"]["pooled_oos"][
                "kept_legs"],
            "short_kept_legs": report["sides"]["short"]["pooled_oos"][
                "kept_legs"],
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
