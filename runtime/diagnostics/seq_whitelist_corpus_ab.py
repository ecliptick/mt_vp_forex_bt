"""End-to-end corpus A/B for the per-side 5-EVENT sequence whitelist.

ONE continuous window over the full corpus (14-day warmup prefix,
executed and discarded), canonical recipe otherwise, differing ONLY in
``structure_seq_whitelist``.

WHY THIS IS A SEPARATE DRIVER. The post-hoc leg filter is a SCREEN,
not a verdict. ``contra_close`` nets opposing legs PAIRWISE, so
removing legs removes the netting's counterparties -- a post-hoc
prediction cannot see that. Every whitelist must be re-run through the
engine.

Each variant writes to its OWN ledger file -- never the canonical path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bt.optimal_config import STRUCTURE_SEQ_WHITELIST  # noqa: E402
from runtime.diagnostics.leg_ledger import build_ledger  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")

# (label, whitelist, drop_set) -- ``None`` means "no filter at all".
#
# THREE ARMS, so the whitelist is ISOLATED and the shipped combination
# is measured:
#
#   unfiltered   drop_set=None, whitelist=None
#                -> the control. Reproduces the ``none`` arm of
#                   ``seq_drop_set_corpus_ab.py`` exactly, which is a
#                   cross-driver consistency check.
#   wl_only     drop_set=None, whitelist=WL
#                -> the WHITELIST ALONE. This is the arm that answers
#                   "is the 5-event shape an edge on its own?"
#   wl_canon    drop_set=canonical, whitelist=WL
#                -> the SHIPPED COMBINATION. What would actually run.
VARIANTS: list[tuple[str, frozenset[str] | None, frozenset[str] | None]] = [
    ("unfiltered", None, None),
    ("wl_only", STRUCTURE_SEQ_WHITELIST, None),
    ("wl_canon", STRUCTURE_SEQ_WHITELIST, frozenset({"short|c", "short|C"})),
]


def summarise(payload: dict) -> dict:
    legs = payload["legs"]
    st = np.array(legs["signal_time_ns"], dtype=np.int64)
    gross = np.array(legs["gross"], dtype=np.float64)
    fee = np.array(legs["fee"], dtype=np.float64)
    side = np.array(legs["side"], dtype=np.int64)
    n = len(st)
    days = (pd.Timestamp(payload["score_end"])
            - pd.Timestamp(payload["score_start"])).days
    day_of = pd.to_datetime(st, unit="ns", utc=True).normalize()
    counts = pd.Series(day_of).value_counts()
    long_m = side > 0
    short_m = side < 0
    return {
        "legs": n,
        "calendar_days": days,
        "legs_per_calendar_day": n / days if days else 0.0,
        "pct_days_below_30": float(100.0 * (counts < 30).mean()) if len(counts) else 0.0,
        "pct_days_above_70": float(100.0 * (counts > 70).mean()) if len(counts) else 0.0,
        "gross": float(gross.sum()),
        "fees": float(fee.sum()),
        "net": float(gross.sum() - fee.sum()),
        "gross_per_leg": float(gross.mean()) if n else 0.0,
        "fee_per_leg": float(fee.mean()) if n else 0.0,
        "hit_rate": float((gross > 0).mean()) if n else 0.0,
        "long_legs": int(long_m.sum()),
        "short_legs": int(short_m.sum()),
        "long_gross": float(gross[long_m].sum()),
        "short_gross": float(gross[short_m].sum()),
        "long_gross_per_leg": float(gross[long_m].mean()) if long_m.any() else 0.0,
        "short_gross_per_leg": float(gross[short_m].mean()) if short_m.any() else 0.0,
    }


def main() -> None:
    rows = []
    for label, whitelist, drop_set in VARIANTS:
        # ``{}`` means "canonical" -- both arms must pass their knobs
        # EXPLICITLY.
        ov = {"structure_seq_whitelist": whitelist,
              "structure_seq_drop_set": drop_set}
        out = OUT_DIR / f"leg_ledger_seqwl_{label}.json"
        print(f"=== whitelist={label} -> {out.name} ===", flush=True)
        payload = build_ledger(engine_overrides=ov, out_path=out)
        s = summarise(payload)
        s["variant"] = label
        s["whitelist_size"] = len(whitelist) if whitelist else 0
        rows.append(s)
        print(json.dumps(s, indent=1), flush=True)

    print()
    print("| variant | wl size | legs | legs/day | %d<30 | %d>70 | gross | fees | net | gross/leg | fee/leg | hit | long g/leg | short g/leg |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['variant']} | {r['whitelist_size']} | {r['legs']} | "
              f"{r['legs_per_calendar_day']:.2f} | "
              f"{r['pct_days_below_30']:.1f}% | {r['pct_days_above_70']:.1f}% | "
              f"${r['gross']:,.2f} | ${r['fees']:,.2f} | ${r['net']:,.2f} | "
              f"{r['gross_per_leg']:.4f} | {r['fee_per_leg']:.4f} | "
              f"{r['hit_rate']:.3f} | {r['long_gross_per_leg']:+.4f} | "
              f"{r['short_gross_per_leg']:+.4f} |")

    (OUT_DIR / "seq_whitelist_corpus_ab.json").write_text(
        json.dumps({"rows": rows}, indent=2, default=str), encoding="utf-8")
    print()
    print(f"-> {OUT_DIR / 'seq_whitelist_corpus_ab.json'}")


if __name__ == "__main__":
    main()
