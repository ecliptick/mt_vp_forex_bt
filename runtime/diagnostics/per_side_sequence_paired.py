"""PAIRED analysis of the per-side sequence filter (2026-10-24).

The statistical correction
--------------------------
``per_side_sequence_hypotheses.py`` scores a filter with a t-stat on
the KEPT trades' mean P&L. **That is the wrong test for a filter.**

A filter does not claim "the trades I keep are profitable". It claims
"the trades I DROP are worse than the ones I keep". The correct
statistic is the **paired delta**: for each dropped trade, the P&L it
would have made. Testing the dropped trades' mean directly is
equivalent (delta = -sum of dropped P&L) and has far more power,
because it uses the paired structure instead of an absolute level.

Concretely, for a bucket (side, last-event):

    delta_gross = gross(kept) - gross(all) = -gross(dropped)

So "drop bucket X" is justified exactly when ``gross(X) < 0`` with a
significantly negative mean -- NOT when the kept trades look good.

This module:
  1. Reports every (side, last-event) bucket's gross on FIT and on
     HELD **separately**, with a paired t-stat on the bucket's own
     P&L. A bucket is a real effect only if it is negative on BOTH
     sets -- that is the test that a fit-set artefact fails.
  2. Scores the "drop bucket X" hypotheses with the PAIRED test.
  3. Reports the resulting per-side market correlation, because the
     whole point of the user's instruction is to keep the strategy
     TWO-SIDED and not turn it into a directional bet.

Usage
-----
    python -m runtime.diagnostics.per_side_sequence_paired
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
    HELD_SLICES,
    FIT_SLICES,
    slice_rows,
)

OUT = Path("runtime/diagnostics/_reports/per_side_sequence_paired.json")


def _bucket(sig: str) -> str:
    return sig[-1] if sig else "?"


def _side(t) -> str:
    return "long" if int(t.direction) > 0 else "short"


def _tstat(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    arr = np.array(vals, dtype=np.float64)
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return 0.0
    return float(arr.mean() / (sd / np.sqrt(arr.size)))


def _collect(slice_rows_: list) -> dict:
    """bucket key -> list of gross P&L values."""
    out: dict[tuple[str, str], list[float]] = {}
    for _label, _mkt, rows in slice_rows_:
        for t, sig in rows:
            out.setdefault((_side(t), _bucket(sig)), []).append(
                float(t.pnl_gross_usd))
    return out


def _score(slice_rows_: list, drop: set) -> dict:
    """Score 'drop these (side, bucket) pairs' with the paired test."""
    base_gross = base_net = wl_gross = wl_net = 0.0
    base_legs = kept_legs = 0
    dropped_gross: list[float] = []
    per_slice = []
    for label, mkt, rows in slice_rows_:
        base = [t for t, _ in rows]
        kept = [t for t, sig in rows
                if (_side(t), _bucket(sig)) not in drop]
        bg = float(sum(t.pnl_gross_usd for t in base))
        bn = float(sum(t.pnl_net_usd for t in base))
        kg = float(sum(t.pnl_gross_usd for t in kept))
        kn = float(sum(t.pnl_net_usd for t in kept))
        base_gross += bg
        base_net += bn
        wl_gross += kg
        wl_net += kn
        base_legs += len(base)
        kept_legs += len(kept)
        for t, sig in rows:
            if (_side(t), _bucket(sig)) in drop:
                dropped_gross.append(float(t.pnl_gross_usd))
        per_slice.append({
            "slice": label, "mkt_ret_pct": mkt,
            "base_legs": len(base), "kept": len(kept),
            "base_gross": bg, "wl_gross": kg, "d_gross": kg - bg,
            "base_net": bn, "wl_net": kn, "d_net": kn - bn,
        })
    flat = [r for r in per_slice if abs(r["mkt_ret_pct"]) < 3.0]
    return {
        "per_slice": per_slice,
        "base_legs": base_legs, "kept_legs": kept_legs,
        "base_gross": base_gross, "wl_gross": wl_gross,
        "d_gross": wl_gross - base_gross,
        "base_net": base_net, "wl_net": wl_net,
        "d_net": wl_net - base_net,
        # PAIRED test: the dropped trades' own mean gross.
        "dropped_n": len(dropped_gross),
        "dropped_gross": float(sum(dropped_gross)),
        "dropped_t": _tstat(dropped_gross),
        "flat_d_gross": sum(r["d_gross"] for r in flat),
        "flat_d_net": sum(r["d_net"] for r in flat),
        "flat_n": len(flat),
    }


def _market_corr(slice_rows_: list, drop: set) -> dict:
    """Per-side gross vs the market return, after the filter.

    This is the guard the user's instruction demands: the filter must
    NOT turn the strategy into a directional bet.
    """
    per_side: dict[str, list[tuple[float, float]]] = {"long": [], "short": []}
    for _label, mkt, rows in slice_rows_:
        for t, sig in rows:
            if (_side(t), _bucket(sig)) in drop:
                continue
            per_side[_side(t)].append((mkt, float(t.pnl_gross_usd)))
    out = {}
    for side, pairs in per_side.items():
        if len(pairs) < 3:
            out[side] = {"n": len(pairs), "corr": 0.0}
            continue
        x = np.array([p[0] for p in pairs], dtype=np.float64)
        y = np.array([p[1] for p in pairs], dtype=np.float64)
        if x.std() == 0 or y.std() == 0:
            out[side] = {"n": len(pairs), "corr": 0.0}
            continue
        out[side] = {"n": len(pairs),
                     "corr": float(np.corrcoef(x, y)[0, 1])}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("== PAIRED per-side sequence analysis ==")
    print()

    fit_rows, held_rows = [], []
    for label, s, e, _r in FIT_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        fit_rows.append((label, mkt, tagged))
    for label, s, e, _r in HELD_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        held_rows.append((label, mkt, tagged))

    fit_buckets = _collect(fit_rows)
    held_buckets = _collect(held_rows)

    # ---- 1. every bucket, on BOTH sets ----
    print("-- bucket gross by (side, last event) --")
    print("   A bucket is REAL only if it is negative on FIT *and* HELD.")
    print()
    print(f"  {'side':<6} {'evt':<5} {'meaning':<28} "
          f"{'FIT n':>6} {'FIT gross':>11} {'FIT t':>7} | "
          f"{'HELD n':>7} {'HELD gross':>11} {'HELD t':>7} {'consistent?':>13}")
    meaning = {
        "b": "BoS bull (bull continuation)",
        "B": "BoS bear (bear continuation)",
        "c": "CHoCH bull (bull reversal)",
        "C": "CHoCH bear (bear reversal)",
    }
    bucket_rows = []
    for side in ("long", "short"):
        for evt in ("b", "B", "c", "C"):
            fv = fit_buckets.get((side, evt), [])
            hv = held_buckets.get((side, evt), [])
            fg = float(sum(fv))
            hg = float(sum(hv))
            ft = _tstat(fv)
            ht = _tstat(hv)
            # "consistent" = negative gross on BOTH sets.
            consistent = (fg < 0 and hg < 0)
            print(f"  {side:<6} {evt:<5} {meaning[evt]:<28} "
                  f"{len(fv):>6} {fg:>11,.0f} {ft:>7.2f} | "
                  f"{len(hv):>7} {hg:>11,.0f} {ht:>7.2f} "
                  f"{('YES' if consistent else 'no'):>13}")
            bucket_rows.append({
                "side": side, "event": evt,
                "fit_n": len(fv), "fit_gross": fg, "fit_t": ft,
                "held_n": len(hv), "held_gross": hg, "held_t": ht,
                "consistent_negative": consistent,
            })

    # ---- 2. the "drop bucket" hypotheses, scored PAIRED ----
    print()
    print("-- 'drop bucket' hypotheses, PAIRED test on the dropped trades --")
    print("   The t-stat is on the DROPPED trades' own gross P&L.")
    print("   A negative t means the dropped trades were reliably losers.")
    print()

    consistent = {(r["side"], r["event"]) for r in bucket_rows
                  if r["consistent_negative"]}
    candidates = {
        "drop_consistent": set(consistent),
        "drop_b_both": {("long", "b"), ("short", "b")},
        "drop_short_b": {("short", "b")},
        "drop_long_b": {("long", "b")},
        "drop_short_bc": {("short", "b"), ("short", "C")},
        "drop_none": set(),
    }

    print(f"  {'hypothesis':<20} {'FIT d gross':>13} {'FIT t':>7} | "
          f"{'HELD d gross':>14} {'HELD t':>8} {'HELD d net':>12} "
          f"{'flat d net':>12} {'corrL':>7} {'corrS':>7}")
    scores = {}
    for name, drop in candidates.items():
        fs = _score(fit_rows, drop)
        hs = _score(held_rows, drop)
        mc = _market_corr(held_rows, drop)
        scores[name] = {"fit": fs, "held": hs, "drop": sorted(drop),
                        "market_corr": mc}
        print(f"  {name:<20} {fs['d_gross']:>+13,.0f} {fs['dropped_t']:>7.2f} | "
              f"{hs['d_gross']:>+14,.0f} {hs['dropped_t']:>8.2f} "
              f"{hs['d_net']:>+12,.0f} {hs['flat_d_net']:>+12,.0f} "
              f"{mc['long']['corr']:>+7.3f} {mc['short']['corr']:>+7.3f}")

    # ---- 3. per-slice detail for the best ----
    best = max((n for n in candidates if n != "drop_none"),
               key=lambda n: scores[n]["held"]["d_gross"])
    print()
    print(f"-- per-slice detail: {best}  (dropping {scores[best]['drop']}) --")
    for r in scores[best]["held"]["per_slice"]:
        print(f"  {r['slice']:<12} mkt={r['mkt_ret_pct']:>+7.2f}%  "
              f"legs {r['base_legs']:>4}->{r['kept']:>4}  "
              f"gross {r['base_gross']:>8,.0f}->{r['wl_gross']:>8,.0f} "
              f"({r['d_gross']:>+7,.0f})  "
              f"net {r['base_net']:>8,.0f}->{r['wl_net']:>8,.0f} "
              f"({r['d_net']:>+7,.0f})")

    # ---- 4. verdict ----
    print()
    print("-- VERDICTS --")
    verdicts = {}
    for name in candidates:
        hs = scores[name]["held"]
        reasons = []
        if hs["d_gross"] > 0:
            reasons.append("held-out gross improved")
        else:
            reasons.append(f"held-out gross NOT improved ({hs['d_gross']:+,.0f})")
        if hs["dropped_t"] <= -2.0:
            reasons.append("dropped trades significantly loss-making")
        elif hs["dropped_t"] >= 2.0:
            reasons.append("dropped trades significantly PROFITABLE "
                           "(filter is backwards)")
        else:
            reasons.append("dropped trades not significant")
        if hs["flat_d_net"] < 0:
            reasons.append("FLAT-SLICE TEST FAILS")
        else:
            reasons.append("flat-slice test holds")
        mc = scores[name]["market_corr"]
        if abs(mc["long"]["corr"]) > 0.6 or abs(mc["short"]["corr"]) > 0.6:
            reasons.append("WARNING: side correlation > 0.6 "
                           "(directional-bet risk)")
        else:
            reasons.append("side correlations acceptable")
        promotable = (hs["d_gross"] > 0 and hs["dropped_t"] <= -2.0
                      and hs["flat_d_net"] >= 0)
        reasons.append("PROMOTABLE" if promotable else "not promotable")
        verdicts[name] = {"reasons": reasons, "promotable": promotable}
        print(f"  {name:<20} " + "; ".join(reasons))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "buckets": bucket_rows,
            "scores": {k: {
                "drop": v["drop"],
                "fit": {kk: vv for kk, vv in v["fit"].items()
                        if kk != "per_slice"},
                "held": {kk: vv for kk, vv in v["held"].items()
                         if kk != "per_slice"},
                "held_per_slice": v["held"]["per_slice"],
                "market_corr": v["market_corr"],
            } for k, v in scores.items()},
            "verdicts": verdicts,
        }, fh, indent=2, default=str)
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
