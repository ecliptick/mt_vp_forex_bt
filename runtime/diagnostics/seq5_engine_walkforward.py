"""ENGINE-LEVEL walk-forward for the 5-event per-side whitelist.

WHY THIS MODULE EXISTS
======================

``seq5_walkforward.py`` fits the whitelist on one half of the corpus
and scores it on the other — but it scores it **POST-HOC**, on the
already-measured legs. That is a SCREEN, not the verdict:

  * ``contra_close`` nets opposing legs **PAIRWISE**, so removing legs
    removes the netting's counterparties. A post-hoc filter cannot
    see that.
  * the same-direction cap (``max_same_dir_open``) is a BOOK-level
    gate: a whitelist that drops a leg frees a slot for a different
    leg. A post-hoc filter cannot see that either.
  * the conflict policy and the fill lifecycle are re-evaluated when
    the book changes.

This module runs the **ENGINE** on each half with a whitelist fitted
on the OTHER half. That is the only test that validates the specific
shipped keys rather than the shape of the rule.

THE DESIGN
==========

Four arms, each a continuous corpus run:

  unfiltered      no whitelist at all. The CONTROL.
  shipped        the canonical 21-key set (fitted on the WHOLE
                 corpus). The thing being judged.
  fitH1          the set fitted on H1 ONLY. Applied to the H1 run.
  fitH2          the set fitted on H2 ONLY. Applied to the H2 run.

The out-of-sample verdict is the POOLED result of ``fitH1`` (scored
on H2) and ``fitH2`` (scored on H1) — each half scored by a
whitelist that never saw it.

⚠️ THE HALVES ARE RUN AS SEPARATE CONTINUOUS EXECUTIONS. That is
NOT the same book as one continuous run over the whole corpus (the
warmup, the rolling caps and the VP profile all carry across the
boundary). The reason is unavoidable: the whitelist has to be
*decided* before the run starts, and the decision depends on which
half is being scored. The arms are therefore COMPARABLE WITH EACH
OTHER (same window, same warmup, same recipe) but NOT directly
comparable to the whole-corpus numbers in
``seq_whitelist_corpus_ab2.json``.

Usage
-----
    python -m runtime.diagnostics.seq5_engine_walkforward
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
OUT = OUT_DIR / "seq5_engine_walkforward.json"
UNFILTERED_LEDGER = OUT_DIR / "leg_ledger_seqwl_unfiltered.json"


def _fit(sig: np.ndarray, gross: np.ndarray, mask: np.ndarray,
         min_n: int) -> set[str]:
    """Fit the per-side whitelist on the legs in ``mask``.

    Admission: at least ``min_n`` legs AND positive GROSS per leg.
    GROSS, not net — a bucket that is only net-positive because it
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


def _summarise(payload: dict[str, Any]) -> dict[str, Any]:
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
        "pct_days_below_30": (float(100.0 * (counts < 30).mean())
                              if len(counts) else 0.0),
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
        "long_gross_per_leg": (float(gross[long_m].mean())
                               if long_m.any() else 0.0),
        "short_gross_per_leg": (float(gross[short_m].mean())
                                if short_m.any() else 0.0),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-n", type=int, default=50)
    ap.add_argument("--data", type=Path, default=None)
    args = ap.parse_args()

    # ── Step 1: the UNFILTERED corpus ledger (the control) ────────
    # It gives us the signatures and the per-signature gross on the
    # WHOLE corpus, so we can fit on each half.
    print("=" * 78)
    print("STEP 1 -- the unfiltered control ledger")
    print("=" * 78)
    control_path = OUT_DIR / "leg_ledger_seqwlf_unfiltered.json"
    payload = build_ledger(engine_overrides={},
                           out_path=control_path)
    arr = ledger_arrays(payload)
    gross = arr["gross"]
    ts = arr["signal_time_ns"]
    side = arr["side"]
    print(f"  legs={int(gross.size)}  "
          f"{payload['score_start']} .. {payload['score_end']}")

    structure = _structure_for_corpus()
    sig = _signatures(structure, arr["signal_bar"])
    has = sig != ""

    median = float(np.median(ts))
    h1 = ts <= median
    h2 = ~h1

    # ── Step 2: fit the whitelist on EACH half ────────────────────
    print()
    print("=" * 78)
    print("STEP 2 -- fit the whitelist on each half")
    print("=" * 78)
    fits: dict[str, set[str]] = {}
    for half_name, half_m in (("H1", h1), ("H2", h2)):
        keep: set[str] = set()
        for side_name, side_m in (("long", side > 0), ("short", side < 0)):
            for s in _fit(sig, gross, has & side_m & half_m, args.min_n):
                keep.add(f"{side_name}|{s}")
        fits[half_name] = keep
        n_l = sum(1 for k in keep if k.startswith("long|"))
        n_s = sum(1 for k in keep if k.startswith("short|"))
        print(f"  fit on {half_name}: {len(keep)} keys "
              f"({n_l} long / {n_s} short)")

    # ── Step 3: run the ENGINE on each half ───────────────────────
    #
    # Each half is a SEPARATE continuous execution with its own
    # warmup prefix, so the rolling caps are warm. The whitelist is
    # FIXED before the run starts -- that is the whole point.
    print()
    print("=" * 78)
    print("STEP 3 -- run the ENGINE on each half")
    print("=" * 78)

    rows: list[dict[str, Any]] = []
    mid = str(payload["score_start"])[:4] + "-" + str(payload["score_start"])[5:7] + "-01"
    # The midpoint in calendar terms: the median signal time.
    mid_date = pd.Timestamp(median, unit="ns", tz="UTC").strftime("%Y-%m-%d")

    arms = [
        ("unfiltered_H1", "H1", None),
        ("unfiltered_H2", "H2", None),
        # IN-SAMPLE: each half scored by a whitelist fitted on ITSELF.
        # Reported for reference only -- it is the number the fit
        # already optimises, so it is NOT a verdict.
        ("fitH1_on_H1", "H1", fits["H1"]),
        ("fitH2_on_H2", "H2", fits["H2"]),
        # OUT-OF-SAMPLE: each half scored by the OTHER half's fit.
        # THIS is the verdict.
        ("fitH1_on_H2", "H2", fits["H1"]),
        ("fitH2_on_H1", "H1", fits["H2"]),
        # The shipped (whole-corpus fit) set, for comparison.
        ("shipped_on_H1", "H1", _shipped_set()),
        ("shipped_on_H2", "H2", _shipped_set()),
        # LONG-ONLY: the shipped set with every short key removed.
        # The per-side breakdown shows the SHORT side does not
        # generalise engine-level (in-sample +0.0700 -> OOS -0.1803,
        # a sign flip), while the LONG side does (in-sample +0.2233
        # -> OOS +0.2189, essentially no decay). This arm measures
        # the long-only book END TO END, because dropping the short
        # legs also changes the contra-close netting.
        ("longonly_on_H1", "H1", _long_only()),
        ("longonly_on_H2", "H2", _long_only()),
    ]

    for label, half, wl in arms:
        if half == "H1":
            start, end = payload["score_start"], mid_date
        else:
            start, end = mid_date, payload["score_end"]
        out = OUT_DIR / f"leg_ledger_seqwlf_{label}.json"
        print(f"=== {label} ({start} .. {end}) -> {out.name} ===",
              flush=True)
        p = build_ledger(start=start, end=end,
                         engine_overrides={"structure_seq_whitelist": wl},
                         out_path=out)
        s = _summarise(p)
        s["arm"] = label
        s["half"] = half
        s["whitelist_size"] = len(wl) if wl else 0
        s["start"] = start
        s["end"] = end
        rows.append(s)
        print(json.dumps(s, indent=1), flush=True)

    by = {r["arm"]: r for r in rows}

    # ── Step 4: the out-of-sample verdict ─────────────────────────
    #
    # The OOS number is the POOLED result of the two halves, each
    # scored by a whitelist fitted on the OTHER half.
    print()
    print("=" * 78)
    print("STEP 4 -- the out-of-sample verdict (engine-level)")
    print("=" * 78)

    def _pooled(arms_used: tuple[str, str]) -> dict[str, float]:
        """Pool two half-corpus arms into one comparable number."""
        oos = [by[a] for a in arms_used]
        legs = sum(r["legs"] for r in oos)
        gross = sum(r["gross"] for r in oos)
        net = sum(r["net"] for r in oos)
        fees = sum(r["fees"] for r in oos)
        ctrl_legs = sum(by[f"unfiltered_{h}"]["legs"]
                        for h in ("H1", "H2"))
        ctrl_gross = sum(by[f"unfiltered_{h}"]["gross"]
                         for h in ("H1", "H2"))
        ctrl_net = sum(by[f"unfiltered_{h}"]["net"]
                       for h in ("H1", "H2"))
        ctrl_fees = sum(by[f"unfiltered_{h}"]["fees"]
                        for h in ("H1", "H2"))
        return {
            "oos_legs": legs,
            "oos_gross": gross,
            "oos_fees": fees,
            "oos_net": net,
            "oos_gross_per_leg": gross / legs if legs else 0.0,
            "oos_fee_per_leg": fees / legs if legs else 0.0,
            "control_legs": ctrl_legs,
            "control_gross": ctrl_gross,
            "control_net": ctrl_net,
            "control_fees": ctrl_fees,
            "control_gross_per_leg": (ctrl_gross / ctrl_legs
                                      if ctrl_legs else 0.0),
            "d_gross": gross - ctrl_gross,
            "d_net": net - ctrl_net,
        }

    oos = _pooled(("fitH2_on_H1", "fitH1_on_H2"))
    longonly = _pooled(("longonly_on_H1", "longonly_on_H2"))
    insample = _pooled(("fitH1_on_H1", "fitH2_on_H2"))
    shipped = _pooled(("shipped_on_H1", "shipped_on_H2"))
    for name, r in (("OUT-OF-SAMPLE (each half scored by the OTHER's fit)",
                    oos),
                    ("IN-SAMPLE (each half scored by its OWN fit) -- NOT a "
                     "verdict", insample),
                    ("SHIPPED (whole-corpus fit)", shipped),
                    ("LONG-ONLY (shipped minus the short keys)",
                     longonly)):
        print(f"  -- {name} --")
        print(f"     legs      {r['control_legs']:>8} -> {r['oos_legs']:>8}")
        print(f"     gross     ${r['control_gross']:>+10,.0f} -> "
              f"${r['oos_gross']:>+10,.0f}  (d ${r['d_gross']:>+,.0f})")
        print(f"     net       ${r['control_net']:>+10,.0f} -> "
              f"${r['oos_net']:>+10,.0f}  (d ${r['d_net']:>+,.0f})")
        print(f"     gross/leg {r['control_gross_per_leg']:>+8.4f} -> "
              f"{r['oos_gross_per_leg']:>+8.4f}   "
              f"(fee/leg {r['oos_fee_per_leg']:.4f})")
        print()

    report = {
        "ts_utc": pd.Timestamp.utcnow().isoformat(),
        "min_n": args.min_n,
        "split_date": mid_date,
        "control_ledger": {
            "path": str(control_path),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "n_legs": int(gross.size),
        },
        "fits": {k: sorted(v) for k, v in fits.items()},
        "arms": rows,
        "oos": oos,
        "in_sample": insample,
        "shipped": shipped,
        "long_only": longonly,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(report, indent=2, default=str),
                   encoding="utf-8")
    print(f"-> {OUT}")

    registry.record_run(
        kind="seq5_engine_walkforward",
        params={
            "split_date": mid_date,
            "min_n": args.min_n,
            "arms": [a[0] for a in arms],
            "recipe": "canonical, structure_seq_drop_set REMOVED",
            "note": ("halves are separate continuous executions, so they "
                     "are comparable with each other but NOT with the "
                     "whole-corpus numbers"),
        },
        summary={
            "oos_gross": round(oos["oos_gross"], 2),
            "oos_net": round(oos["oos_net"], 2),
            "oos_gross_per_leg": round(oos["oos_gross_per_leg"], 4),
            "control_gross_per_leg": round(oos["control_gross_per_leg"], 4),
            "d_gross": round(oos["d_gross"], 2),
            "d_net": round(oos["d_net"], 2),
            "oos_legs": oos["oos_legs"],
            "control_legs": oos["control_legs"],
            "shipped_gross_per_leg": round(shipped["oos_gross_per_leg"], 4),
            "longonly_gross": round(longonly["oos_gross"], 2),
            "longonly_gross_per_leg": round(
                longonly["oos_gross_per_leg"], 4),
        },
        payload=report,
    )


def _shipped_set() -> frozenset[str]:
    """The canonical 21-key whitelist, straight from the config."""
    from bt.optimal_config import STRUCTURE_SEQ_WHITELIST
    return frozenset(STRUCTURE_SEQ_WHITELIST)


def _long_only() -> frozenset[str]:
    """The shipped set with every short key removed."""
    return frozenset(k for k in _shipped_set() if k.startswith("long|"))


if __name__ == "__main__":
    main()
