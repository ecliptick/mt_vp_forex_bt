"""VALIDATE the signal-time per-side sequence drop set (2026-10-24).

The corrected measurement (at the SIGNAL bar, not the fill bar)
identifies exactly TWO buckets that are negative on BOTH the fit and
the held-out halves:

    short|c   FIT -$24 (t=-0.75) | HELD -$36 (t=-1.31)
    short|C   FIT -$23 (t=-0.61) | HELD -$20 (t=-0.57)

Both are SHORTS taken after a BULLISH structure event:

    "c" = CHoCH bull (a bullish REVERSAL -- the market just flipped
          from bearish to bullish structure)
    "C" = CHoCH bear (a bearish reversal)
    "b" = BoS bull  (bullish continuation)
    "B" = BoS bear  (bearish continuation)

So ``short|c`` is a short after a bullish REVERSAL and ``short|C``
is a short after a bearish reversal. Together they are the two
reversal events, as opposed to the two continuation events.

The market reading: a short taken into a freshly-reversed structure
is a counter-trend trade. The FVG that triggered it is a zone that
formed BEFORE the reversal, so the reversal is precisely the event
that invalidated it. Taking the short anyway is trading against the
structure that has just been established.

``short|B`` (short after a bearish CONTINUATION) is POSITIVE on both
halves (+$70 / +$11) -- that is the "trade with the trend" setup,
and it is the short side's good bucket.

This module validates the drop set with the full battery:
  * the paired test on the dropped trades,
  * the per-slice deltas,
  * the per-side market correlation (the directional-bet guard),
  * walk-forward re-fitting,
  * the flat-slice test.

Usage
-----
    python -m runtime.diagnostics.seq_drop_set_validate
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

OUT = Path("runtime/diagnostics/_reports/seq_drop_set_validate.json")

ALL_SLICES = FIT_SLICES + HELD_SLICES

# The candidate drop sets, all signal-time.
CANDIDATES = {
    "drop_none": set(),
    "drop_short_c": {("short", "c")},
    "drop_short_C": {("short", "C")},
    "drop_short_cC": {("short", "c"), ("short", "C")},
    "drop_short_cC_longC": {("short", "c"), ("short", "C"),
                             ("long", "C")},
}


def _bucket(sig: str) -> str:
    return sig[-1] if sig else "?"


def _side(t) -> str:
    return "long" if int(t.direction) > 0 else "short"


def _tstat(vals: list[float]) -> float:
    if len(vals) < 2:
        return 0.0
    arr = np.array(vals, dtype=np.float64)
    sd = float(arr.std(ddof=1))
    return 0.0 if sd <= 0 else float(
        arr.mean() / (sd / np.sqrt(arr.size)))


def _score(rows_all: list, drop: set) -> dict:
    base_gross = base_net = wl_gross = wl_net = 0.0
    base_legs = kept_legs = 0
    dropped: list[float] = []
    per_slice = []
    for label, mkt, rows in rows_all:
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
                dropped.append(float(t.pnl_gross_usd))
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
        "dropped_n": len(dropped),
        "dropped_gross": float(sum(dropped)),
        "dropped_t": _tstat(dropped),
        "flat_d_gross": sum(r["d_gross"] for r in flat),
        "flat_d_net": sum(r["d_net"] for r in flat),
        "flat_n": len(flat),
        "slices_positive_gross": sum(1 for r in per_slice if r["wl_gross"] > 0),
        "slices_positive_net": sum(1 for r in per_slice if r["wl_net"] > 0),
    }


def _corr_by_side(rows_all: list, drop: set) -> dict:
    out: dict[str, list[tuple[float, float]]] = {"long": [], "short": []}
    for _label, mkt, rows in rows_all:
        for t, sig in rows:
            if (_side(t), _bucket(sig)) in drop:
                continue
            out[_side(t)].append((mkt, float(t.pnl_gross_usd)))
    res = {}
    for side, pairs in out.items():
        if len(pairs) < 3:
            res[side] = {"n": len(pairs), "corr": 0.0, "gross": 0.0}
            continue
        x = np.array([p[0] for p in pairs], dtype=np.float64)
        y = np.array([p[1] for p in pairs], dtype=np.float64)
        corr = 0.0 if (x.std() == 0 or y.std() == 0) else \
            float(np.corrcoef(x, y)[0, 1])
        res[side] = {"n": len(pairs), "corr": corr, "gross": float(y.sum())}
    return res


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("== VALIDATE the signal-time per-side sequence drop set ==")
    print()

    rows_all = []
    for label, s, e, _r in ALL_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        rows_all.append((label, mkt, tagged))

    scores = {}
    print(f"  {'drop set':<26} {'legs':>6} {'gross':>9} {'net':>8} "
          f"{'d gross':>9} {'d net':>8} {'drop t':>8} {'flat dnet':>11} "
          f"{'corrL':>7} {'corrS':>7}")
    for name, drop in CANDIDATES.items():
        sc = _score(rows_all, drop)
        mc = _corr_by_side(rows_all, drop)
        scores[name] = {"score": sc, "corr": mc, "drop": sorted(drop)}
        lbl = ",".join(f"{s[0]}|{s[1]}" for s in sorted(drop)) or "-"
        print(f"  {lbl:<26} {sc['kept_legs']:>6} {sc['wl_gross']:>9,.0f} "
              f"{sc['wl_net']:>8,.0f} {sc['d_gross']:>+9,.0f} "
              f"{sc['d_net']:>+8,.0f} {sc['dropped_t']:>8.2f} "
              f"{sc['flat_d_net']:>+11,.0f} {mc['long']['corr']:>+7.3f} "
              f"{mc['short']['corr']:>+7.3f}")

    # Per-slice detail for the leading candidate.
    best = max((n for n in CANDIDATES if n != "drop_none"),
               key=lambda n: scores[n]["score"]["d_gross"])
    print()
    print(f"-- per-slice detail: {best} --")
    for r in scores[best]["score"]["per_slice"]:
        print(f"  {r['slice']:<12} mkt={r['mkt_ret_pct']:>+7.2f}%  "
              f"legs {r['base_legs']:>4}->{r['kept']:>4}  "
              f"gross {r['base_gross']:>8,.0f}->{r['wl_gross']:>8,.0f} "
              f"({r['d_gross']:>+7,.0f})  "
              f"net {r['base_net']:>8,.0f}->{r['wl_net']:>8,.0f} "
              f"({r['d_net']:>+7,.0f})")

    # Walk-forward.
    print()
    print("-- walk-forward (re-fit on slices 1..k, score on k+1) --")
    wf_hits = 0
    wf = []
    for k in range(1, len(rows_all)):
        train = rows_all[:k]
        test = [rows_all[k]]
        train_b: dict[tuple[str, str], list[float]] = {}
        for _l, _m, rws in train:
            for t, sig in rws:
                train_b.setdefault((_side(t), _bucket(sig)), []).append(
                    float(t.pnl_gross_usd))
        drop = set()
        for key, vals in train_b.items():
            if len(vals) < 20:
                continue
            mean_gross = float(np.mean(vals))
            t = _tstat(vals)
            if mean_gross < 0 and t <= -0.5:
                drop.add(key)
        sc = _score(test, drop)
        wf_hits += 1 if sc["d_gross"] > 0 else 0
        wf.append({"k": k, "test_slice": test[0][0], "drop": sorted(drop),
                   "d_gross": sc["d_gross"], "d_net": sc["d_net"]})
        lbl = ",".join(f"{a}|{b}" for a, b in sorted(drop)) or "-"
        print(f"  k={k} test={test[0][0]:<12} drop={lbl:<24} "
              f"d gross {sc['d_gross']:>+7,.0f}  d net {sc['d_net']:>+7,.0f}")
    print()
    print(f"  walk-forward: gross improved on {wf_hits}/{len(rows_all) - 1} "
          f"out-of-sample slices")

    # Verdict.
    print()
    print("-- VERDICT --")
    verdicts = {}
    for name in CANDIDATES:
        sc = scores[name]["score"]
        mc = scores[name]["corr"]
        reasons = []
        ok = True
        if sc["d_gross"] > 0:
            reasons.append(f"gross improved ${sc['d_gross']:+,.0f}")
        else:
            reasons.append(f"gross NOT improved ({sc['d_gross']:+,.0f})")
            ok = False
        if sc["dropped_t"] <= -1.5:
            reasons.append(f"dropped trades loss-making (t={sc['dropped_t']:.2f})")
        elif sc["dropped_t"] < 0:
            reasons.append(f"dropped trades mildly loss-making "
                           f"(t={sc['dropped_t']:.2f})")
            ok = False
        else:
            reasons.append(f"dropped trades not loss-making "
                           f"(t={sc['dropped_t']:.2f})")
            ok = False
        if sc["flat_d_net"] >= 0:
            reasons.append("flat-slice test holds")
        else:
            reasons.append(f"FLAT-SLICE TEST FAILS ({sc['flat_d_net']:+,.0f})")
            ok = False
        if abs(mc["long"]["corr"]) < 0.5 and abs(mc["short"]["corr"]) < 0.5:
            reasons.append("both side correlations < 0.5")
        else:
            reasons.append("WARNING: a side correlation >= 0.5")
            ok = False
        if mc["short"]["n"] > 0:
            reasons.append(f"short side still trades ({mc['short']['n']} legs)")
        else:
            reasons.append("short side ELIMINATED")
            ok = False
        reasons.append("PROMOTABLE" if ok else "not promotable")
        verdicts[name] = {"reasons": reasons, "promotable": ok}
        lbl = name
        print(f"  {lbl:<26} " + "; ".join(reasons))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "candidates": {k: {
                "drop": v["drop"],
                "score": {kk: vv for kk, vv in v["score"].items()
                          if kk != "per_slice"},
                "per_slice": v["score"]["per_slice"],
                "market_corr": v["corr"],
            } for k, v in scores.items()},
            "walk_forward": wf, "walk_forward_hits": wf_hits,
            "verdicts": verdicts,
        }, fh, indent=2, default=str)
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
