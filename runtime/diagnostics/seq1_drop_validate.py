"""VALIDATE the canonical 1-event per-side drop set on the WIDE book.

WHY THIS MODULE EXISTS
======================

The canonical recipe carries::

    STRUCTURE_SEQ_DROP_SET = frozenset({"short|c", "short|C"})

That rule was measured on 2026-10-24 against the **NARROW** book
(``vol_state_min_range_norm = 3.0``, 8 monthly slices, ~700 legs).
The canonical vol gate is now **1.0** — the WIDE book, ~95k legs —
and the rule has **never been re-measured** on it.

AGENTS.md is explicit about this class of problem: the vol gate and
every other geometry knob moved, and the drop set was fitted under a
different book. A rule fitted on 700 legs and never re-measured is a
candidate for being either dead, backwards, or a fee-saving artefact.

THIS MODULE ASKS THREE QUESTIONS
=================================

1. **Is each (side, last-event) bucket's gross negative?** The
   canonical rule drops buckets, so the buckets it drops must be the
   negative ones. If the rule drops a bucket that is BETTER than the
   one it keeps, the rule is backwards.

2. **Does the drop set improve the book OUT OF SAMPLE?** The 2-fold
   walk-forward: fit the drop set on one half of the corpus, score it
   on the other. A rule fitted on 700 legs must beat the null of
   "drop the same NUMBER of legs at random".

3. **Is the improvement GROSS or just FEES?** Dropping 27k legs out
   of 45k short legs saves fees. A rule that only moves NET and
   leaves (or worsens) GROSS per leg is a fee-saving artefact, not a
   signal.

Usage
-----
    python -m runtime.diagnostics.seq1_drop_validate
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
    EVENT_MEANING,
    _structure_for_corpus,
    _signatures,
)
from runtime.results import registry  # noqa: E402

OUT = Path("runtime/diagnostics/_reports/seq1_drop_validate.json")

# The canonical rule, as it stands in ``bt/optimal_config.py``.
CANONICAL_DROP: set[tuple[str, str]] = {("short", "c"), ("short", "C")}


def _tstat(vals: np.ndarray) -> float:
    if vals.size < 2:
        return 0.0
    sd = float(vals.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    return float(vals.mean() / (sd / np.sqrt(vals.size)))


def _bucket_stats(gross: np.ndarray, net: np.ndarray, fee: np.ndarray
                  ) -> dict[str, float]:
    n = int(gross.size)
    if n == 0:
        return {"n": 0, "gross": 0.0, "net": 0.0, "fees": 0.0,
                "g_per_leg": 0.0, "n_per_leg": 0.0, "hit": 0.0, "t": 0.0}
    return {
        "n": n,
        "gross": float(gross.sum()),
        "net": float(net.sum()),
        "fees": float(fee.sum()),
        "g_per_leg": float(gross.sum()) / n,
        "n_per_leg": float(net.sum()) / n,
        "hit": float((gross > 0).mean()),
        "t": _tstat(gross),
    }


def _last_event(sig: np.ndarray) -> np.ndarray:
    """The last event letter of each signature."""
    out = np.full(len(sig), "?", dtype=object)
    idx = np.flatnonzero(sig != "")
    for i in idx:
        out[i] = sig[i][-1]
    return out


def _score(sig: np.ndarray, last: np.ndarray, gross: np.ndarray,
           net: np.ndarray, fee: np.ndarray, side: np.ndarray,
           mask: np.ndarray, drop: set[tuple[str, str]]) -> dict[str, Any]:
    """Apply a drop set to the legs in ``mask``."""
    idx = np.flatnonzero(mask)
    kept_mask = np.zeros(len(sig), dtype=bool)
    for i in idx:
        s = "long" if side[i] > 0 else "short"
        if (s, str(last[i])) not in drop:
            kept_mask[i] = True
    kept = idx[kept_mask[idx]]
    dropped = idx[~kept_mask[idx]]
    return {
        "base_legs": int(idx.size),
        "kept_legs": int(kept.size),
        "dropped_legs": int(dropped.size),
        "base_gross": float(gross[idx].sum()),
        "kept_gross": float(gross[kept].sum()) if kept.size else 0.0,
        "d_gross": (float(gross[kept].sum()) if kept.size else 0.0)
        - float(gross[idx].sum()),
        "base_net": float(net[idx].sum()),
        "kept_net": float(net[kept].sum()) if kept.size else 0.0,
        "d_net": (float(net[kept].sum()) if kept.size else 0.0)
        - float(net[idx].sum()),
        "dropped_gross": float(gross[dropped].sum()) if dropped.size else 0.0,
        "dropped_net": float(net[dropped].sum()) if dropped.size else 0.0,
        "dropped_t": _tstat(gross[dropped]) if dropped.size else 0.0,
        "base_g_per_leg": (float(gross[idx].sum()) / idx.size)
        if idx.size else 0.0,
        "kept_g_per_leg": (float(gross[kept].sum()) / kept.size)
        if kept.size else 0.0,
        "base_n_per_leg": (float(net[idx].sum()) / idx.size)
        if idx.size else 0.0,
        "kept_n_per_leg": (float(net[kept].sum()) / kept.size)
        if kept.size else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--null-iters", type=int, default=2000)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("=" * 78)
    print("CANONICAL 1-EVENT DROP SET, RE-MEASURED ON THE WIDE BOOK")
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
        "canonical_drop": sorted(f"{s}|{e}" for s, e in CANONICAL_DROP),
        "sides": {},
    }

    # ── 1. Every (side, last-event) bucket ──────────────────────
    print("=" * 78)
    print("1. EVERY (side, LAST EVENT) BUCKET  --  H1 / H2 / ALL")
    print("=" * 78)
    print(f"  {'side':<6} {'evt':<4} {'n':>7} {'gross':>11} {'g/leg':>8} "
          f"{'net':>11} {'hit':>6} {'t':>6} | "
          f"{'H1 g/leg':>9} {'H2 g/leg':>9} {'consistent?':>12}")
    for s_name, s_val in (("long", 1), ("short", -1)):
        m_side = has & (side == s_val)
        for evt in ("b", "B", "c", "C"):
            m = m_side & (last == evt)
            if not m.any():
                continue
            st = _bucket_stats(gross[m], net[m], fee[m])
            a = _bucket_stats(gross[m & h1], net[m & h1], fee[m & h1])
            b = _bucket_stats(gross[m & h2], net[m & h2], fee[m & h2])
            consist = ("YES" if (a["g_per_leg"] < 0 and b["g_per_leg"] < 0)
                       else ("no" if a["g_per_leg"] < 0 or b["g_per_leg"] < 0
                             else "POS"))
            print(f"  {s_name:<6} {evt:<4} {st['n']:>7} {st['gross']:>11,.0f} "
                  f"{st['g_per_leg']:>+8.3f} {st['net']:>11,.0f} "
                  f"{st['hit'] * 100:>5.1f}% {st['t']:>+6.2f} | "
                  f"{a['g_per_leg']:>+9.3f} {b['g_per_leg']:>+9.3f} "
                  f"{consist:>12}")
            report["sides"].setdefault(s_name, {})["buckets"] = \
                report["sides"].setdefault(s_name, {}).get("buckets", {})
            report["sides"][s_name]["buckets"][evt] = {
                "all": st, "h1": a, "h2": b,
                "consistent_negative": consist == "YES",
            }
    print()
    print("  consistent? = negative gross/leg on BOTH halves of the corpus")
    print("  POS = the bucket is POSITIVE on both halves")
    print()

    # ── 2. The canonical rule, applied to the wide book ────────
    print("=" * 78)
    print("2. THE CANONICAL RULE, POST-HOC, ON THE WHOLE CORPUS")
    print("=" * 78)
    print(f"  drop set: {sorted(f'{s}|{e}' for s, e in CANONICAL_DROP)}")
    print()
    for s_name, s_val in (("long", 1), ("short", -1)):
        m_side = has & (side == s_val)
        sc = _score(sig, last, gross, net, fee, side, m_side, CANONICAL_DROP)
        print(f"  {s_name:<6} legs {sc['base_legs']:>7} -> {sc['kept_legs']:>7}  "
              f"gross ${sc['base_gross']:>+11,.0f} -> ${sc['kept_gross']:>+11,.0f} "
              f"(d ${sc['d_gross']:>+10,.0f})")
        print(f"         gross/leg ${sc['base_g_per_leg']:>+8.4f} -> "
              f"${sc['kept_g_per_leg']:>+8.4f}   "
              f"net/leg ${sc['base_n_per_leg']:>+8.4f} -> "
              f"${sc['kept_n_per_leg']:>+8.4f}")
        print(f"         dropped {sc['dropped_legs']:>7} legs  "
              f"gross ${sc['dropped_gross']:>+10,.0f}  "
              f"net ${sc['dropped_net']:>+10,.0f}  t={sc['dropped_t']:+.2f}")
        report["sides"].setdefault(s_name, {})["canonical_effect"] = sc
    print()

    # ── 3. Walk-forward: fit the drop set on one half ─────────
    print("=" * 78)
    print("3. WALK-FORWARD  (fit the drop set on one half, score the other)")
    print("=" * 78)
    rng = np.random.default_rng(20261103)
    for s_name, s_val in (("long", 1), ("short", -1)):
        m_side = has & (side == s_val)
        print(f"  -- {s_name} --")
        print(f"  {'fold':<22} {'drop set':<26} {'test legs':>10} "
              f"{'d gross':>11} {'d net':>11} {'null p95':>11} {'pct':>7}")
        folds = []
        for fold_name, fit_m, test_m in (
            ("fit H1 -> test H2", m_side & h1, m_side & h2),
            ("fit H2 -> test H1", m_side & h2, m_side & h1),
        ):
            # FIT: drop every (side, last-event) bucket whose gross per
            # leg is negative on the FIT half, with at least 100 legs.
            fit_idx = np.flatnonzero(fit_m)
            drop: set[tuple[str, str]] = set()
            for evt in ("b", "B", "c", "C"):
                mm = fit_m & (last == evt)
                if int(mm.sum()) < 100:
                    continue
                if float(gross[mm].sum()) < 0.0:
                    drop.add((s_name, evt))
            sc = _score(sig, last, gross, net, fee, side, test_m, drop)
            sc["fold"] = fold_name
            sc["drop"] = sorted(f"{s}|{e}" for s, e in drop)

            # NULL: drop the same NUMBER of legs at random.
            test_idx = np.flatnonzero(test_m)
            null = np.zeros(args.null_iters, dtype=np.float64)
            n_drop = sc["dropped_legs"]
            for i in range(args.null_iters):
                pick = rng.choice(test_idx, size=min(n_drop, test_idx.size),
                                  replace=False)
                null[i] = float(gross[pick].sum())
            # The null is the DROPPED gross (the delta is its negative).
            null_delta = -null
            pct = float((null_delta < sc["d_gross"]).mean())
            sc["null_p95"] = float(np.percentile(null_delta, 95))
            sc["percentile"] = pct
            folds.append(sc)
            print(f"  {fold_name:<22} {','.join(sc['drop']) or '(none)':<26} "
                  f"{sc['base_legs']:>10} {sc['d_gross']:>+11,.0f} "
                  f"{sc['d_net']:>+11,.0f} {sc['null_p95']:>+11,.0f} "
                  f"{pct * 100:>6.1f}%")
        pooled = {
            "test_legs": sum(f["base_legs"] for f in folds),
            "kept_legs": sum(f["kept_legs"] for f in folds),
            "d_gross": sum(f["d_gross"] for f in folds),
            "d_net": sum(f["d_net"] for f in folds),
        }
        print(f"  POOLED OOS: legs {pooled['test_legs']:>7} -> "
              f"{pooled['kept_legs']:>7}  "
              f"d gross ${pooled['d_gross']:>+10,.0f}  "
              f"d net ${pooled['d_net']:>+10,.0f}")
        print()
        report["sides"].setdefault(s_name, {})["walk_forward"] = {
            "folds": folds, "pooled_oos": pooled,
        }

    # ── 4. The short side, in full ─────────────────────────────
    print("=" * 78)
    print("4. THE SHORT SIDE IN FULL (the unfiltered book)")
    print("=" * 78)
    m_short = has & (side < 0)
    st = _bucket_stats(gross[m_short], net[m_short], fee[m_short])
    print(f"  short legs {st['n']:>7}  gross ${st['gross']:>+11,.0f}  "
          f"gross/leg ${st['g_per_leg']:>+8.4f}  "
          f"net ${st['net']:>+11,.0f}  net/leg ${st['n_per_leg']:>+8.4f}")
    print(f"  -> the short side as a whole is "
          f"{'GROSS-NEGATIVE' if st['gross'] < 0 else 'gross-positive'}"
          f" and pays ${st['fees'] / st['n']:.4f}/leg in fees")
    print()
    report["short_side_total"] = st

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                        encoding="utf-8")
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq1_drop_validate",
        params={
            "ledger": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "null_iters": args.null_iters,
        },
        summary={
            "n_legs": int(gross.size),
            "short_gross_per_leg": round(
                report["short_side_total"]["g_per_leg"], 6),
            "short_gross": round(report["short_side_total"]["gross"], 6),
            "canonical_short_d_gross": round(
                report["sides"]["short"]["canonical_effect"]["d_gross"], 6),
            "canonical_short_d_net": round(
                report["sides"]["short"]["canonical_effect"]["d_net"], 6),
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
