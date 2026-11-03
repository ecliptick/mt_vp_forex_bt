"""ENGINE-LEVEL check of the K=48 trade-count-band variant.

WHY THIS MODULE EXISTS
======================

``seq5_ksweep.py`` is POST-HOC: it ranks the 5-event signatures by
gross/leg on the measured legs and keeps the top K. It answers "how
many signatures do I have to keep to stay in the 30-70 legs/day
band?" -- and it says **48**.

But the post-hoc filter is a SCREEN, not a verdict:

  * ``contra_close`` nets opposing legs **PAIRWISE**, so removing legs
    removes the netting's counterparties;
  * the same-direction cap (``max_same_dir_open``) is a BOOK-level
    gate -- a whitelist that drops a leg frees a slot for a different
    leg;
  * a post-hoc ranking by gross/leg is a **selection on the outcome**:
    the top-K set is chosen because it scored well on the very legs
    it is then scored on.

The engine-level walk-forward (``seq5_engine_walkforward.py``)
already proved the last point is fatal for the SHORT side -- its
in-sample +0.0700 became -0.1803 out of sample. A ranked top-48 set
is even MORE exposed to it, because it is ranked by the very statistic
it is judged on.

This module runs the ENGINE on the K=48 variant and on the shipped
21-key set, on the full corpus, and reports them side by side.

THE VERDICT WE ARE TESTING
==========================

The post-hoc K sweep says: "to stay in the 30-70 legs/day band you
would have to keep 48 of 64 signatures per side -- i.e. barely filter
at all". If the engine-side K=48 run confirms a gross/leg close to
the post-hoc +0.0391, the trade-count band and the edge really are in
direct conflict, and the full-strength 21-key set stands (with the
trade-count waiver the user gave).

If instead the engine-side K=48 gross/leg is much HIGHER than the
post-hoc +0.0391, then the post-hoc screen understates it, and the
band might be reachable after all.

Usage
-----
    python -m runtime.diagnostics.seq5_ksweep_engine_check
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    build_ledger,
    ledger_arrays,
    load_ledger,
)
from runtime.diagnostics.seq5_table import (  # noqa: E402
    _signatures,
    _structure_for_corpus,
)
from runtime.results import registry  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")
OUT = OUT_DIR / "seq5_ksweep_engine_check.json"
POSTHOC = OUT_DIR / "seq5_ksweep.json"
UNFILTERED = OUT_DIR / "leg_ledger_seqwl_unfiltered.json"


def _ranked_top_k(sig: np.ndarray, gross: np.ndarray, side: np.ndarray,
                  has: np.ndarray, k: int, min_n: int) -> set[str]:
    """The top-K signatures per side by gross/leg (the ksweep rule)."""
    out: set[str] = set()
    for side_name, smask in (("long", side > 0), ("short", side < 0)):
        idx = np.flatnonzero(has & smask)
        rows: list[tuple[float, str]] = []
        for s in np.unique(sig[idx]):
            k_idx = idx[sig[idx] == s]
            if k_idx.size >= min_n:
                rows.append((float(gross[k_idx].mean()), str(s)))
        rows.sort(key=lambda r: (-r[0], r[1]))
        out |= {f"{side_name}|{s}" for _, s in rows[:k]}
    return out


def _summarise(payload: dict[str, Any]) -> dict[str, Any]:
    legs = payload["legs"]
    st = np.asarray(legs["signal_time_ns"], dtype=np.int64)
    gross = np.asarray(legs["gross"], dtype=np.float64)
    fee = np.asarray(legs["fee"], dtype=np.float64)
    drift = np.asarray(legs["drift_usd"], dtype=np.float64)
    excess = np.asarray(legs["excess_usd"], dtype=np.float64)
    side = np.asarray(legs["side"], dtype=np.int64)
    n = int(st.size)
    days = (pd.Timestamp(payload["score_end"])
            - pd.Timestamp(payload["score_start"])).days
    day_of = pd.to_datetime(st, unit="ns", utc=True).normalize()
    counts = pd.Series(day_of).value_counts()

    def _t(x: np.ndarray) -> float:
        x = np.asarray(x, dtype=float)
        if x.size < 2 or x.std(ddof=1) <= 0:
            return 0.0
        return float(x.mean() / (x.std(ddof=1) / np.sqrt(x.size)))

    long_m = side > 0
    short_m = side < 0
    out = {
        "legs": n,
        "calendar_days": days,
        "legs_per_calendar_day": n / days if days else 0.0,
        "pct_days_below_30": (float(100.0 * (counts < 30).mean())
                              if len(counts) else 0.0),
        "gross": float(gross.sum()),
        "fees": float(fee.sum()),
        "net": float(gross.sum() - fee.sum()),
        "gross_per_leg": float(gross.mean()) if n else 0.0,
        "fee_per_leg": float(fee.mean()) if n else 0.0,
        "hit_rate": float((gross > 0).mean()) if n else 0.0,
        "drift_per_leg": float(drift.mean()) if n else 0.0,
        "drift_t": _t(drift),
        "excess_per_leg": float(excess.mean()) if n else 0.0,
        "excess_t": _t(excess),
        "long_legs": int(long_m.sum()),
        "short_legs": int(short_m.sum()),
        "long_gross_per_leg": (float(gross[long_m].mean())
                               if long_m.any() else 0.0),
        "short_gross_per_leg": (float(gross[short_m].mean())
                                if short_m.any() else 0.0),
    }
    out["gross_over_fee"] = (out["gross_per_leg"] / out["fee_per_leg"]
                             if out["fee_per_leg"] > 0 else 0.0)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--k", type=int, default=48)
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    # ── Step 1: the post-hoc prediction ─────────────────────────
    print("=" * 80)
    print("STEP 1 -- the POST-HOC prediction (the screen)")
    print("=" * 80)
    posthoc = json.loads(POSTHOC.read_text(encoding="utf-8"))
    row = next((r for r in posthoc["rows"] if r["k"] == args.k), None)
    if row is None:
        raise SystemExit(f"the post-hoc sweep has no K={args.k} row")
    print(f"  K={args.k}: {row['kept']} legs, "
          f"{row['legs_per_day']:.2f}/day, gross {row['gross']:+,.0f}, "
          f"net {row['net']:+,.0f}, gross/leg {row['gross_per_leg']:+.4f}")

    # ── Step 2: build the K=48 set on the unfiltered corpus ──────
    print()
    print("=" * 80)
    print("STEP 2 -- build the ranked top-K set")
    print("=" * 80)
    payload = load_ledger(UNFILTERED)
    arr = ledger_arrays(payload)
    gross = arr["gross"]
    side = arr["side"]
    structure = _structure_for_corpus()
    sig = _signatures(structure, arr["signal_bar"])
    has = sig != ""
    top_k = _ranked_top_k(sig, gross, side, has, args.k, args.min_n)
    n_l = sum(1 for k in top_k if k.startswith("long|"))
    n_s = sum(1 for k in top_k if k.startswith("short|"))
    print(f"  {len(top_k)} keys ({n_l} long / {n_s} short)")

    from bt.optimal_config import STRUCTURE_SEQ_WHITELIST

    shipped = frozenset(STRUCTURE_SEQ_WHITELIST)
    print(f"  shipped set: {len(shipped)} keys")

    # ── Step 3: run the ENGINE on each ───────────────────────────
    print()
    print("=" * 80)
    print("STEP 3 -- run the ENGINE (the verdict)")
    print("=" * 80)
    arms = [("shipped_21", shipped), (f"ranked_top{args.k}", top_k)]
    rows: list[dict[str, Any]] = []
    for label, wl in arms:
        out = OUT_DIR / f"leg_ledger_seqwlf_{label}.json"
        print(f"=== {label} ({len(wl)} keys) -> {out.name} ===", flush=True)
        p = build_ledger(engine_overrides={"structure_seq_whitelist": wl},
                         out_path=out)
        s = _summarise(p)
        s["arm"] = label
        s["whitelist_size"] = len(wl)
        rows.append(s)
        print(json.dumps(s, indent=1), flush=True)

    by = {r["arm"]: r for r in rows}
    ship = by["shipped_21"]
    k48 = by[f"ranked_top{args.k}"]

    print()
    print("=" * 80)
    print("THE VERDICT")
    print("=" * 80)
    hdr = (f"  {'book':<16} {'legs':>7} {'legs/d':>8} {'%d<30':>7} "
           f"{'gross':>11} {'net':>11} {'g/leg':>8} {'fee/leg':>8} "
           f"{'excess/l':>10} {'excess t':>9}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for r in rows:
        print(f"  {r['arm']:<16} {r['legs']:>7} "
              f"{r['legs_per_calendar_day']:>8.2f} {r['pct_days_below_30']:>6.1f}% "
              f"{r['gross']:>+11,.0f} {r['net']:>+11,.0f} "
              f"{r['gross_per_leg']:>+8.4f} {r['fee_per_leg']:>8.4f} "
              f"{r['excess_per_leg']:>+10.4f} {r['excess_t']:>+9.2f}")

    print()
    print(f"  post-hoc K={args.k} predicted gross/leg "
          f"{row['gross_per_leg']:+.4f}")
    print(f"  engine-side K={args.k} measured  gross/leg "
          f"{k48['gross_per_leg']:+.4f}")
    print(f"  shipped 21-key set measured     gross/leg "
          f"{ship['gross_per_leg']:+.4f}")
    print()
    print(f"  in the 30-70/day band?  shipped: "
          f"{'YES' if 30 <= ship['legs_per_calendar_day'] <= 70 else 'NO'}  "
          f"K={args.k}: "
          f"{'YES' if 30 <= k48['legs_per_calendar_day'] <= 70 else 'NO'}")

    report = {
        "ts_utc": pd.Timestamp.utcnow().isoformat(),
        "k": args.k,
        "min_n": args.min_n,
        "posthoc_prediction": row,
        "engine_arms": rows,
        "top_k_keys": sorted(top_k),
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                        encoding="utf-8")
    print()
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq5_ksweep_engine_check",
        params={"k": args.k, "min_n": args.min_n,
                "ledger": str(UNFILTERED),
                "recipe": "canonical, structure_seq_drop_set REMOVED"},
        summary={
            "k": args.k,
            "posthoc_gross_per_leg": round(row["gross_per_leg"], 4),
            "engine_gross_per_leg": round(k48["gross_per_leg"], 4),
            "shipped_gross_per_leg": round(ship["gross_per_leg"], 4),
            "engine_legs_per_day": round(k48["legs_per_calendar_day"], 2),
            "shipped_legs_per_day": round(
                ship["legs_per_calendar_day"], 2),
            "engine_net": round(k48["net"], 2),
            "shipped_net": round(ship["net"], 2),
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
