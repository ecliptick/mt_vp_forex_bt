"""End-to-end corpus A/B for the 5-EVENT per-side sequence whitelist.

ONE continuous window over the full corpus (14-day warmup prefix,
executed and discarded), canonical recipe otherwise, differing ONLY in
``structure_seq_whitelist`` and ``structure_seq_drop_set``.

WHY THIS IS A SEPARATE DRIVER. The post-hoc leg filter is a SCREEN,
not a verdict. ``contra_close`` nets opposing legs PAIRWISE, so
removing legs removes the netting's counterparties -- a post-hoc
prediction cannot see that. Every whitelist must be re-run through the
engine.

THE THREE SETS
==============

The candidate set was RE-FITTED on 2026-11-04, and the re-fit was
necessary: the canonical recipe changed between the old fit and the
new one (the trailing stop became canonical, ``tp_mode`` moved to
``hvn_single``, the SL ceiling moved, and the
``vol_state_min_expand_prev_day`` gate went live), which changed the
book from 64,978 legs to 37,159 legs. A set fitted on the old book
does not describe the new one.

  OLD       the 25-key set that was fitted on the PRE-trailing-stop
            book. Kept as a RECONCILIATION arm: it is what shipped
            in the earlier session, so it is the baseline the new
            fit has to beat, on the same artifacts.
  SPLIT     the 21-key set re-fitted on the CURRENT book: every
            signature with positive gross/leg on BOTH halves of the
            corpus, n>=50 per half.
  RANKED    the top-21 signatures per side by gross/leg on the
            current book (42 keys). A WHITELIST is a RANKED set, and
            this arm asks whether keeping MORE signatures -- trading
            more, at lower quality -- is better than the strict
            split-half set.

Each set is run TWICE: alone, and combined with the canonical
``structure_seq_drop_set``. The drop set is a 1-EVENT blacklist and
the whitelist is a 5-EVENT whitelist; they overlap in scope (some
whitelisted short signatures end in 'c'/'C', which the drop set
kills), so the combination must be measured, not assumed.

Each variant writes to its OWN ledger file -- never the canonical path.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.diagnostics.leg_ledger import build_ledger  # noqa: E402

# The 1-event drop set was REMOVED from the canonical config on
# 2026-11-04. This A/B is the EVIDENCE for that removal, so it needs
# the old value to run the "+drop set" arms. It is defined HERE, in
# the diagnostic, rather than imported from the config -- the config
# no longer has it, and the config is where a promoted value lives.
#
# If you change this set, you are re-running a historical comparison:
# the canonical recipe is now the whitelist ALONE.
DROP_SET_REMOVED_2026_11_04: frozenset[str] = frozenset({"short|c", "short|C"})

OUT_DIR = Path("runtime/diagnostics/_reports")
TABLE = OUT_DIR / "seq5_table_fresh.json"

# The 25-key set fitted on the PRE-trailing-stop book (2026-11-04,
# earlier session). Reconciliation arm only.
OLD_SET: frozenset[str] = frozenset({
    "long|Ccbbb", "long|cCBcC", "long|cCcCc", "long|cbCcC",
    "long|bCcbb", "long|CcbCc", "long|cCcCB", "long|CBcbC",
    "long|cCcbb", "long|BcCcC", "long|CcbbC", "long|bbbbC",
    "long|CcbCB", "long|bCcCc", "long|bbCBB", "long|CcCcb",
    "long|bbCcb", "long|CBBcb",
    "short|BcCcC", "short|CBBcC", "short|cbbCB", "short|BBcbC",
    "short|cCBBB", "short|BBcbb", "short|BBBcC",
})


def _split_set() -> frozenset[str]:
    """The re-fitted split-half candidate set, from the fresh table."""
    d = json.loads(TABLE.read_text(encoding="utf-8"))
    return frozenset(
        f"{side}|{sig}"
        for side in ("long", "short")
        for sig in d["candidates"][side]
    )


def _ranked_set(k: int = 21) -> frozenset[str]:
    """The top-K signatures per side by gross/leg, from the fresh table."""
    d = json.loads(TABLE.read_text(encoding="utf-8"))
    out: set[str] = set()
    for side in ("long", "short"):
        rows = []
        for sig, v in d[side]["all"].items():
            if v["n"] < 50:
                continue
            rows.append((v["gross_per_leg"], sig))
        rows.sort(key=lambda r: (-r[0], r[1]))
        out |= {f"{side}|{s}" for _, s in rows[:k]}
    return frozenset(out)


def summarise(payload: dict) -> dict[str, Any]:
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
    split = _split_set()
    ranked = _ranked_set(21)
    print(f"sets: old={len(OLD_SET)}  split={len(split)}  ranked={len(ranked)}")

    variants: list[tuple[str, frozenset[str] | None, frozenset[str] | None]] = [
        ("unfiltered", None, None),
        ("old", OLD_SET, None),
        ("old_canon", OLD_SET, DROP_SET_REMOVED_2026_11_04),
        ("split", split, None),
        ("split_canon", split, DROP_SET_REMOVED_2026_11_04),
        ("ranked21", ranked, None),
        ("ranked21_canon", ranked, DROP_SET_REMOVED_2026_11_04),
    ]

    # The "+drop set" arms are HISTORICAL: the engine no longer
    # accepts ``structure_seq_drop_set`` (it was removed from the
    # config AND from the engine on 2026-11-04), so they cannot be
    # re-run. Their measured results live in the committed
    # ``seq_whitelist_corpus_ab2.json`` and in the results registry
    # under ``seq_whitelist_corpus_ab-705ae18fa064d332``.
    #
    # They are still LISTED here so the driver keeps documenting what
    # the comparison was, but they are SKIPPED with a loud note rather
    # than crashing the whole run on a KeyError.
    skipped: list[str] = []

    rows: list[dict[str, Any]] = []
    for label, whitelist, drop_set in variants:
        if drop_set:
            skipped.append(label)
            print(f"=== {label} SKIPPED -- the drop set no longer exists "
                  f"in the engine (removed 2026-11-04). See the "
                  f"committed seq_whitelist_corpus_ab2.json for its "
                  f"measured result. ===", flush=True)
            continue
        ov: dict[str, Any] = {"structure_seq_whitelist": whitelist}
        out = OUT_DIR / f"leg_ledger_seqwl2_{label}.json"
        print(f"=== {label} -> {out.name} ===", flush=True)
        payload = build_ledger(engine_overrides=ov, out_path=out)
        s = summarise(payload)
        s["variant"] = label
        s["whitelist_size"] = len(whitelist) if whitelist else 0
        s["drop_set_size"] = len(drop_set) if drop_set else 0
        rows.append(s)
        print(json.dumps(s, indent=1), flush=True)

    print()
    print("| variant | wl | dropset | legs | legs/day | %d<30 | gross | fees | net | gross/leg | fee/leg | hit | long g/leg | short g/leg |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['variant']} | {r['whitelist_size']} | {r['drop_set_size']} | "
              f"{r['legs']} | {r['legs_per_calendar_day']:.2f} | "
              f"{r['pct_days_below_30']:.1f}% | "
              f"${r['gross']:,.2f} | ${r['fees']:,.2f} | ${r['net']:,.2f} | "
              f"{r['gross_per_leg']:.4f} | {r['fee_per_leg']:.4f} | "
              f"{r['hit_rate']:.3f} | {r['long_gross_per_leg']:+.4f} | "
              f"{r['short_gross_per_leg']:+.4f} |")

    (OUT_DIR / "seq_whitelist_corpus_ab2.json").write_text(
        json.dumps({"rows": rows,
                    "skipped_historical_arms": skipped,
                    "sets": {
                        "old": sorted(OLD_SET),
                        "split": sorted(split),
                        "ranked21": sorted(ranked),
                    }}, indent=2, default=str), encoding="utf-8")
    print()
    print(f"-> {OUT_DIR / 'seq_whitelist_corpus_ab2.json'}")


if __name__ == "__main__":
    main()
