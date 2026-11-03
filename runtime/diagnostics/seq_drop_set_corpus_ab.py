"""End-to-end corpus A/B for the per-side sequence drop set.

ONE continuous window over the full corpus (14-day warmup prefix,
executed and discarded), canonical recipe otherwise, differing ONLY in
``structure_seq_drop_set``.

WHY THIS IS A SEPARATE DRIVER. The post-hoc leg filter is a SCREEN,
not a verdict. ``contra_close`` nets opposing legs PAIRWISE, so
removing legs removes the netting's counterparties -- a post-hoc
prediction cannot see that. Every drop set must be re-run through the
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

from runtime.diagnostics.leg_ledger import build_ledger  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")

# (label, drop_set) -- ``None`` means "no filter at all".
VARIANTS: list[tuple[str, frozenset[str] | None]] = [
    ("none", None),
    ("canonical", frozenset({"short|c", "short|C"})),
    ("B", frozenset({"short|B"})),
    ("Bb", frozenset({"short|B", "short|b"})),
    ("BcC", frozenset({"short|B", "short|c", "short|C"})),
]


def summarise(payload: dict) -> dict:
    legs = payload["legs"]
    st = np.array(legs["signal_time_ns"], dtype=np.int64)
    gross = np.array(legs["gross"], dtype=np.float64)
    fee = np.array(legs["fee"], dtype=np.float64)
    n = len(st)
    days = (pd.Timestamp(payload["score_end"])
            - pd.Timestamp(payload["score_start"])).days
    day_of = pd.to_datetime(st, unit="ns", utc=True).normalize()
    counts = pd.Series(day_of).value_counts()
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
    }


def main() -> None:
    rows = []
    for label, drop_set in VARIANTS:
        # ``{}`` means "canonical" -- the unfiltered arm must pass
        # ``structure_seq_drop_set=None`` EXPLICITLY.
        ov = {"structure_seq_drop_set": drop_set}
        out = OUT_DIR / f"leg_ledger_seqdrop_{label}.json"
        print(f"=== drop_set={label} -> {out.name} ===", flush=True)
        payload = build_ledger(engine_overrides=ov, out_path=out)
        s = summarise(payload)
        s["variant"] = label
        s["drop_set"] = sorted(drop_set) if drop_set else None
        rows.append(s)
        print(json.dumps(s, indent=1), flush=True)

    print()
    print("| variant | drop set | legs | legs/day | %d<30 | %d>70 | gross | fees | net | gross/leg | fee/leg | hit |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        ds = ",".join(r["drop_set"]) if r["drop_set"] else "(none)"
        print(f"| {r['variant']} | `{ds}` | {r['legs']} | "
              f"{r['legs_per_calendar_day']:.2f} | "
              f"{r['pct_days_below_30']:.1f}% | {r['pct_days_above_70']:.1f}% | "
              f"${r['gross']:,.2f} | ${r['fees']:,.2f} | ${r['net']:,.2f} | "
              f"{r['gross_per_leg']:.4f} | {r['fee_per_leg']:.4f} | "
              f"{r['hit_rate']:.3f} |")

    (OUT_DIR / "seq_drop_set_corpus_ab.json").write_text(
        json.dumps({"rows": rows}, indent=2, default=str), encoding="utf-8")
    print()
    print(f"-> {OUT_DIR / 'seq_drop_set_corpus_ab.json'}")


if __name__ == "__main__":
    main()
