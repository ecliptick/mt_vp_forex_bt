"""PRE-REGISTERED per-side sequence hypotheses (2026-10-24).

This module tests a small, EXPLICIT set of hypotheses about which
market-structure sequences are profitable for longs and for shorts.

Why this exists
---------------
``per_side_sequence_filter.py`` fits a whitelist GREEDILY (admit
every bucket whose gross/leg > 0 on the fit slices) and then scores
it on held-out. The result was: gross delta ~$0 on held-out, with the
entire net "gain" coming from fee savings on 137 fewer legs. That is
the same failure mode as the plain whitelist.

The problem with a greedy fit is that it is a SEARCH: with 8 buckets
per side it will find something that looks good on the fit set by
chance. A pre-registered hypothesis is not a search -- it is a single
statement, tested once, with the option of being refuted.

Where the hypotheses come from
------------------------------
They are read off the ``per_side_sequence_filter`` FIT table, which
is where the signal is strongest and most coherent:

    LONG  after 'b' (BoS bull)      gross/leg -$1.09   t = -2.37
    SHORT after 'b' (BoS bull)      gross/leg -$1.46   t = -3.12

A bullish break of structure is a significantly LOSS-MAKING setup
for BOTH sides. That is one coherent, surprising, testable statement
-- and it is the opposite of the naive "trade with structure" prior,
which is exactly why it is worth a dedicated test.

The hypotheses
--------------
  H1  drop 'b' for BOTH sides.
      The cleanest statement: a bullish BoS is a bad setup either way.
  H2  longs: keep only bearish-last-event buckets ('B','C').
      The "fade the structure" hypothesis for longs.
  H3  longs: keep 'B','C','c'   (the greedy per-side fit).
  H4  shorts: keep 'B','c'       (the greedy per-side fit).
  H5  H1 + the per-side greedies combined (H1 & H3 & H4).
  H6  baseline: keep everything (the control, must reproduce the
      control's numbers exactly).

Metrics
-------
GROSS is the primary metric. A "win" that is only fee savings from
trading less is not an edge -- the plain whitelist's held-out "+$2"
was $70 of fees hiding a -$68 gross loss. Net is reported alongside.

Every hypothesis is scored on the DISJOINT fit and held-out slice
sets, with a t-stat on the held-out kept trades and the flat-slice
test called out separately (the decisive chop check).

Usage
-----
    python -m runtime.diagnostics.per_side_sequence_hypotheses
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from runtime.diagnostics.per_side_sequence_filter import (  # noqa: E402
    FEE,
    HELD_SLICES,
    FIT_SLICES,
    slice_rows,
    _T,
)

OUT = Path("runtime/diagnostics/_reports/per_side_sequence_hypotheses.json")

# Hypothesis -> (long_buckets, short_buckets). ``None`` means "keep
# everything on this side" (the side is unfiltered).
HYPOTHESES: dict[str, tuple[set | None, set | None]] = {
    "H0_control":      (None, None),
    "H1_drop_b_both":  ({"B", "C", "c"}, {"B", "C", "c"}),
    "H2_long_fade":    ({"B", "C"}, None),
    "H3_long_greedy":  ({"B", "C", "c"}, None),
    "H4_short_greedy": (None, {"B", "c"}),
    "H5_combined":     ({"B", "C", "c"}, {"B", "c"}),
    "H6_short_fade":   (None, {"B", "C"}),
}


def _bucket(sig: str) -> str:
    return sig[-1] if sig else "?"


def _side(t) -> str:
    return "long" if int(t.direction) > 0 else "short"


def _filter_rows(rows: list, long_keep: set | None,
                 short_keep: set | None) -> list:
    kept = []
    for t, sig in rows:
        allowed = long_keep if _side(t) == "long" else short_keep
        if allowed is None:
            kept.append(t)
        elif _bucket(sig) in allowed:
            kept.append(t)
    return kept


def _score(slice_rows: list, long_keep: set | None,
           short_keep: set | None) -> dict:
    """Score one hypothesis on a set of slices."""
    base_gross = base_net = wl_gross = wl_net = 0.0
    base_legs = kept_legs = 0
    kept_pnls: list[float] = []
    per_slice = []
    for label, mkt, rows in slice_rows:
        base_trades = [t for t, _ in rows]
        kept = _filter_rows(rows, long_keep, short_keep)
        bg = float(sum(t.pnl_gross_usd for t in base_trades))
        bn = float(sum(t.pnl_net_usd for t in base_trades))
        kg = float(sum(t.pnl_gross_usd for t in kept))
        kn = float(sum(t.pnl_net_usd for t in kept))
        base_gross += bg
        base_net += bn
        wl_gross += kg
        wl_net += kn
        base_legs += len(base_trades)
        kept_legs += len(kept)
        kept_pnls.extend(t.pnl_net_usd for t in kept)
        per_slice.append({
            "slice": label, "mkt_ret_pct": mkt,
            "base_legs": len(base_trades), "kept": len(kept),
            "base_gross": bg, "wl_gross": kg, "d_gross": kg - bg,
            "base_net": bn, "wl_net": kn, "d_net": kn - bn,
        })
    if kept_pnls:
        arr = np.array(kept_pnls, dtype=np.float64)
        sd = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
        t_agg = float(arr.mean() / (sd / np.sqrt(arr.size))) if sd > 0 else 0.0
    else:
        t_agg = 0.0
    flat = [r for r in per_slice if abs(r["mkt_ret_pct"]) < 3.0]
    return {
        "per_slice": per_slice,
        "base_legs": base_legs, "kept_legs": kept_legs,
        "base_gross": base_gross, "wl_gross": wl_gross,
        "d_gross": wl_gross - base_gross,
        "base_net": base_net, "wl_net": wl_net,
        "d_net": wl_net - base_net,
        "t_aggregate": t_agg,
        "flat_d_gross": sum(r["d_gross"] for r in flat),
        "flat_d_net": sum(r["d_net"] for r in flat),
        "flat_n": len(flat),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("== PRE-REGISTERED per-side sequence hypotheses ==")
    print()

    fit_rows, held_rows = [], []
    for label, s, e, _r in FIT_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        fit_rows.append((label, mkt, tagged))
    for label, s, e, _r in HELD_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        held_rows.append((label, mkt, tagged))

    # ---- FIT: which hypotheses look good on the fit slices ----
    print("-- FIT (4 slices) --")
    print(f"  {'hypothesis':<18} {'legs':>6} {'gross':>10} {'net':>9} "
          f"{'d gross':>9} {'d net':>9}")
    fit_scores = {}
    for name, (lk, sk) in HYPOTHESES.items():
        s = _score(fit_rows, lk, sk)
        fit_scores[name] = s
        print(f"  {name:<18} {s['kept_legs']:>6} {s['wl_gross']:>10,.0f} "
              f"{s['wl_net']:>9,.0f} {s['d_gross']:>+9,.0f} "
              f"{s['d_net']:>+9,.0f}")
    control = fit_scores["H0_control"]
    print()
    print(f"  control: {control['base_legs']} legs  gross "
          f"${control['base_gross']:,.0f}  net ${control['base_net']:,.0f}")

    # ---- HELD-OUT: the decisive score ----
    print()
    print("-- HELD-OUT (4 DISJOINT slices) --")
    print(f"  {'hypothesis':<18} {'legs':>6} {'gross':>10} {'net':>9} "
          f"{'d gross':>9} {'d net':>9} {'t':>7} {'flat dnet':>11}")
    held_scores = {}
    for name, (lk, sk) in HYPOTHESES.items():
        s = _score(held_rows, lk, sk)
        held_scores[name] = s
        print(f"  {name:<18} {s['kept_legs']:>6} {s['wl_gross']:>10,.0f} "
              f"{s['wl_net']:>9,.0f} {s['d_gross']:>+9,.0f} "
              f"{s['d_net']:>+9,.0f} {s['t_aggregate']:>+7.2f} "
              f"{s['flat_d_net']:>+11,.0f}")

    # ---- VERDICT per hypothesis ----
    print()
    print("-- VERDICTS --")
    verdicts = {}
    for name in HYPOTHESES:
        s = held_scores[name]
        reasons = []
        if s["d_gross"] > 0:
            reasons.append("gross improved")
        else:
            reasons.append(f"gross NOT improved ({s['d_gross']:+,.0f})")
        if s["t_aggregate"] >= 2.0:
            reasons.append("kept trades significantly profitable")
        elif s["t_aggregate"] <= -2.0:
            reasons.append("kept trades significantly LOSS-MAKING")
        else:
            reasons.append("kept trades not significant")
        if s["flat_d_net"] < 0:
            reasons.append("FLAT-SLICE TEST FAILS")
        else:
            reasons.append("flat-slice test holds")
        # A promotion needs gross improvement AND significance.
        promotable = (s["d_gross"] > 0 and s["t_aggregate"] >= 2.0
                      and s["flat_d_net"] >= 0)
        reasons.append("PROMOTABLE" if promotable else "not promotable")
        verdicts[name] = {"reasons": reasons, "promotable": promotable}
        print(f"  {name:<18} " + "; ".join(reasons))

    # ---- Per-slice detail for the most promising hypothesis ----
    best = max(
        (n for n in HYPOTHESES if n != "H0_control"),
        key=lambda n: held_scores[n]["d_gross"],
    )
    print()
    print(f"-- per-slice detail: {best} --")
    lk, sk = HYPOTHESES[best]
    for r in held_scores[best]["per_slice"]:
        print(f"  {r['slice']:<12} mkt={r['mkt_ret_pct']:>+7.2f}%  "
              f"legs {r['base_legs']:>4}->{r['kept']:>4}  "
              f"gross {r['base_gross']:>8,.0f}->{r['wl_gross']:>8,.0f} "
              f"({r['d_gross']:>+7,.0f})  "
              f"net {r['base_net']:>8,.0f}->{r['wl_net']:>8,.0f} "
              f"({r['d_net']:>+7,.0f})")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "hypotheses": {k: [sorted(v[0]) if v[0] else None,
                               sorted(v[1]) if v[1] else None]
                           for k, v in HYPOTHESES.items()},
            "fit": {k: {kk: vv for kk, vv in v.items() if kk != "per_slice"}
                    for k, v in fit_scores.items()},
            "held": {k: {kk: vv for kk, vv in v.items() if kk != "per_slice"}
                     for k, v in held_scores.items()},
            "held_per_slice": held_scores[best]["per_slice"],
            "best_by_d_gross": best,
            "verdicts": verdicts,
        }, fh, indent=2, default=str)
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
