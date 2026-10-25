"""POOLED validation of the per-side sequence filter (2026-10-24).

The 4-fit / 4-held split gives each side of the test only ~45-60
legs, so a single bucket's t-stat lands around -0.5 on the held-out
half. That is a POWER problem, not evidence of absence.

This module pools ALL 8 slices (the same corpus the beta diagnostic
uses) and re-tests the one hypothesis that survived the
fit/held-out consistency check:

    **Drop SHORT trades whose last structure event is a bullish BoS
    ('b' -- a bullish break of structure).**

The per-side framing matters: the SAME event type is fine for longs
(long|'b' is NOT negative on held-out) and bad for shorts. That is
precisely the per-side behaviour the user asked for, and it is why a
pooled whitelist could never have found it.

What this module reports
-------------------------
  * The POOLED paired test on the dropped bucket (n ~105 legs).
  * A WALK-FORWARD check: fit on slices 1..k, score on slice k+1,
    for every k. A filter that only works because of how the corpus
    was split fails this.
  * The per-side market correlation before and after -- the guard
    against turning the strategy into a directional bet.
  * The flat-slice test, which is the decisive chop check.

Usage
-----
    python -m runtime.diagnostics.per_side_sequence_pooled
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

OUT = Path("runtime/diagnostics/_reports/per_side_sequence_pooled.json")

# The hypothesis under test. A set of (side, last-event) to DROP.
DROP_SHORT_B = {("short", "b")}

ALL_SLICES = FIT_SLICES + HELD_SLICES


def _bucket(sig: str) -> str:
    return sig[-1] if sig else "?"


def _side(t) -> str:
    return "long" if int(t.direction) > 0 else "short"


def _tstat(vals: list[float]) -> tuple[float, float]:
    """Return (mean, t-stat) for a list of P&L values."""
    if not vals:
        return 0.0, 0.0
    arr = np.array(vals, dtype=np.float64)
    if arr.size < 2:
        return float(arr.mean()), 0.0
    sd = float(arr.std(ddof=1))
    if sd <= 0:
        return float(arr.mean()), 0.0
    return float(arr.mean()), float(arr.mean() / (sd / np.sqrt(arr.size)))


def _side_corr(rows: list, mkt: float) -> tuple[float, float]:
    """(corr, n) of gross P&L vs a single slice's market move."""
    if len(rows) < 3:
        return 0.0, len(rows)
    x = np.full(len(rows), mkt, dtype=np.float64)
    y = np.array([float(t.pnl_gross_usd) for t in rows], dtype=np.float64)
    if x.std() == 0 or y.std() == 0:
        return 0.0, len(rows)
    return float(np.corrcoef(x, y)[0, 1]), len(rows)


def _score(slice_rows_: list, drop: set) -> dict:
    base_gross = base_net = wl_gross = wl_net = 0.0
    base_legs = kept_legs = 0
    dropped: list[float] = []
    dropped_net: list[float] = []
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
                dropped.append(float(t.pnl_gross_usd))
                dropped_net.append(float(t.pnl_net_usd))
        per_slice.append({
            "slice": label, "mkt_ret_pct": mkt,
            "base_legs": len(base), "kept": len(kept),
            "base_gross": bg, "wl_gross": kg, "d_gross": kg - bg,
            "base_net": bn, "wl_net": kn, "d_net": kn - bn,
        })
    flat = [r for r in per_slice if abs(r["mkt_ret_pct"]) < 3.0]
    dmean, dt = _tstat(dropped)
    return {
        "per_slice": per_slice,
        "base_legs": base_legs, "kept_legs": kept_legs,
        "base_gross": base_gross, "wl_gross": wl_gross,
        "d_gross": wl_gross - base_gross,
        "base_net": base_net, "wl_net": wl_net,
        "d_net": wl_net - base_net,
        "dropped_n": len(dropped),
        "dropped_gross": float(sum(dropped)),
        "dropped_mean_gross": dmean,
        "dropped_t": dt,
        "dropped_net": float(sum(dropped_net)),
        "flat_d_gross": sum(r["d_gross"] for r in flat),
        "flat_d_net": sum(r["d_net"] for r in flat),
        "flat_n": len(flat),
        "slices_positive_gross": sum(1 for r in per_slice if r["wl_gross"] > 0),
        "slices_positive_net": sum(1 for r in per_slice if r["wl_net"] > 0),
    }


def _corr_by_side(slice_rows_: list, drop: set) -> dict:
    """Per-side gross P&L vs market return, pooled across slices."""
    out: dict[str, list[tuple[float, float]]] = {"long": [], "short": []}
    for _label, mkt, rows in slice_rows_:
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

    print("== POOLED validation: drop SHORT trades after a bullish BoS ==")
    print()

    rows_all = []
    for label, s, e, _r in ALL_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        rows_all.append((label, mkt, tagged))
        print(f"  {label:<12} mkt={mkt:>+7.2f}%  legs={len(tagged):>4}",
              flush=True)

    # ---- 1. pooled paired test ----
    print()
    print("-- 1. POOLED paired test (all 8 slices) --")
    base = _score(rows_all, set())
    filt = _score(rows_all, DROP_SHORT_B)
    print(f"  control : {base['base_legs']:>4} legs  gross "
          f"${base['base_gross']:>8,.0f}  net ${base['base_net']:>8,.0f}")
    print(f"  filtered: {filt['kept_legs']:>4} legs  gross "
          f"${filt['wl_gross']:>8,.0f}  net ${filt['wl_net']:>8,.0f}")
    print()
    print(f"  dropped trades        : n={filt['dropped_n']}")
    print(f"  dropped gross P&L     : ${filt['dropped_gross']:,.0f} "
          f"(mean ${filt['dropped_mean_gross']:+.3f})")
    print(f"  dropped net P&L       : ${filt['dropped_net']:,.0f}")
    print(f"  PAIRED t-stat         : {filt['dropped_t']:+.2f}")
    print()
    print(f"  gross delta           : ${filt['d_gross']:+,.0f}")
    print(f"  net delta             : ${filt['d_net']:+,.0f}")
    print(f"  slices +gross         : {filt['slices_positive_gross']}/8 "
          f"(control {base['slices_positive_gross']}/8)")
    print(f"  slices +net           : {filt['slices_positive_net']}/8 "
          f"(control {base['slices_positive_net']}/8)")
    print(f"  FLAT slices           : d gross ${filt['flat_d_gross']:+,.0f}  "
          f"d net ${filt['flat_d_net']:+,.0f}  (n={filt['flat_n']})")

    # ---- 2. per-side correlation ----
    print()
    print("-- 2. per-side market correlation (the directional-bet guard) --")
    cb = _corr_by_side(rows_all, set())
    cf = _corr_by_side(rows_all, DROP_SHORT_B)
    print(f"  {'':<8} {'control':>18} {'filtered':>18}")
    print(f"  {'side':<8} {'corr':>9} {'gross':>9} {'corr':>9} {'gross':>9}")
    for side in ("long", "short"):
        print(f"  {side:<8} {cb[side]['corr']:>+9.3f} "
              f"{cb[side]['gross']:>+9,.0f} {cf[side]['corr']:>+9.3f} "
              f"{cf[side]['gross']:>+9,.0f}")
    print()
    print(f"  long-side market correlation: {cb['long']['corr']:+.3f} -> "
          f"{cf['long']['corr']:+.3f}")
    print(f"  short-side market correlation: {cb['short']['corr']:+.3f} -> "
          f"{cf['short']['corr']:+.3f}")
    print(f"  BOTH sides still trade: long n={cf['long']['n']}, "
          f"short n={cf['short']['n']}")

    # ---- 3. walk-forward ----
    print()
    print("-- 3. WALK-FORWARD (fit on slices 1..k, score on slice k+1) --")
    print(f"  {'k':>3} {'train slices':<34} {'test slice':<12} "
          f"{'d gross':>9} {'d net':>9}")
    wf_hits = 0
    wf_total = 0
    wf = []
    for k in range(1, len(rows_all)):
        train = rows_all[:k]
        test = [rows_all[k]]
        # Re-fit the drop-set on the TRAIN slices only.
        train_buckets: dict[tuple[str, str], list[float]] = {}
        for _l, _m, rws in train:
            for t, sig in rws:
                train_buckets.setdefault((_side(t), _bucket(sig)), []).append(
                    float(t.pnl_gross_usd))
        drop = set()
        for key, vals in train_buckets.items():
            m, t = _tstat(vals)
            if m < 0 and t <= -1.0 and len(vals) >= 20:
                drop.add(key)
        s = _score(test, drop)
        wf_hits += 1 if s["d_gross"] > 0 else 0
        wf_total += 1
        wf.append({"k": k, "test_slice": test[0][0], "drop": sorted(drop),
                   "d_gross": s["d_gross"], "d_net": s["d_net"]})
        print(f"  {k:>3} {', '.join(r[0] for r in train):<34} "
              f"{test[0][0]:<12} {s['d_gross']:>+9,.0f} {s['d_net']:>+9,.0f}")
    print()
    print(f"  walk-forward: gross improved on {wf_hits}/{wf_total} "
          f"out-of-sample slices")

    # ---- 4. per-slice detail ----
    print()
    print("-- 4. per-slice detail (filtered) --")
    for r in filt["per_slice"]:
        print(f"  {r['slice']:<12} mkt={r['mkt_ret_pct']:>+7.2f}%  "
              f"legs {r['base_legs']:>4}->{r['kept']:>4}  "
              f"gross {r['base_gross']:>8,.0f}->{r['wl_gross']:>8,.0f} "
              f"({r['d_gross']:>+7,.0f})  "
              f"net {r['base_net']:>8,.0f}->{r['wl_net']:>8,.0f} "
              f"({r['d_net']:>+7,.0f})")

    # ---- 5. verdict ----
    print()
    print("-- VERDICT --")
    reasons = []
    promotable = True
    if filt["d_gross"] > 0:
        reasons.append(f"pooled gross improved ${filt['d_gross']:+,.0f}")
    else:
        reasons.append(f"pooled gross NOT improved ({filt['d_gross']:+,.0f})")
        promotable = False
    if filt["dropped_t"] <= -2.0:
        reasons.append(f"dropped trades significantly loss-making "
                       f"(t={filt['dropped_t']:.2f})")
    elif filt["dropped_t"] <= -1.0:
        reasons.append(f"dropped trades loss-making but not significant "
                       f"(t={filt['dropped_t']:.2f})")
        promotable = False
    else:
        reasons.append(f"dropped trades not significant "
                       f"(t={filt['dropped_t']:.2f})")
        promotable = False
    if filt["flat_d_net"] >= 0:
        reasons.append(f"flat-slice test holds ({filt['flat_d_net']:+,.0f})")
    else:
        reasons.append(f"FLAT-SLICE TEST FAILS ({filt['flat_d_net']:+,.0f})")
        promotable = False
    if abs(cf["long"]["corr"]) < 0.5 and abs(cf["short"]["corr"]) < 0.5:
        reasons.append("both side correlations < 0.5 (not a directional bet)")
    else:
        reasons.append("WARNING: a side correlation is >= 0.5")
        promotable = False
    if wf_hits / max(wf_total, 1) >= 0.6:
        reasons.append(f"walk-forward {wf_hits}/{wf_total} slices positive")
    else:
        reasons.append(f"walk-forward only {wf_hits}/{wf_total} positive")
        promotable = False
    if cf["short"]["n"] > 0:
        reasons.append(f"short side still trades ({cf['short']['n']} legs)")
    else:
        reasons.append("WARNING: the short side was eliminated entirely")
        promotable = False
    reasons.append("PROMOTABLE" if promotable else "not promotable")
    print("  " + "; ".join(reasons))

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "drop": sorted(DROP_SHORT_B),
            "control": {k: v for k, v in base.items() if k != "per_slice"},
            "filtered": {k: v for k, v in filt.items() if k != "per_slice"},
            "per_slice": filt["per_slice"],
            "corr_control": cb, "corr_filtered": cf,
            "walk_forward": wf,
            "walk_forward_hits": wf_hits, "walk_forward_total": wf_total,
            "reasons": reasons, "promotable": promotable,
        }, fh, indent=2, default=str)
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
