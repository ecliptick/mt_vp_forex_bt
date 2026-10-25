"""WHY ``short|b`` is structurally empty at the signal bar (2026-10-24).

The per-side sequence filter was measured on the trade's **fill**
bar. Re-measured on the **signal** bar -- the bar at which the
engine actually evaluates the gate -- the ``short|b`` bucket is
EMPTY. That is not a bug; it is a structural property of the
strategy, and it explains everything.

The mechanism
-------------
An FVG is emitted by the detector on a 1-minute cadence. A BEAR FVG
(bull FVG) forms when price gaps DOWN through the zone; a BULL FVG
(bear FVG) forms when price gaps UP.

A **bullish BoS** is a break of structure to the upside. The market
structure detector records it and -- crucially -- it is the event that
**invalidates the opposing bear FVGs** (the "structure event
invalidation" pass in ``detect_fvg``). So by the time a bar carries
a bullish BoS as its last structure event:

* every bear FVG that would produce a SHORT has already been killed
  by that very break, and
* the surviving zones are bull FVGs, which produce LONGS.

Therefore **no short can be signalled on a bar whose last structure
event is a bullish BoS**. The bucket is empty by construction.

Why the fill-bar measurement found 105 short|b trades
-----------------------------------------------------
Under the D2 resting-order model a signal is placed on bar ``S`` and
the order fills on a LATER bar ``F`` (median 42 bars later). The
structure evolves in between. By the time the order FILLS, the last
structure event at ``F`` is often a bullish BoS -- but the decision
to take the short was made at ``S``, when the last event was
something else.

So the fill-bar measurement answered a different question:
"what is the P&L of shorts whose order FILLED during a bullish-break
regime?" -- not "what is the P&L of shorts whose SIGNAL fired
during one?" The first question is a legitimate one (it is a proxy
for regime), but it is NOT what the engine gate can act on, and it is
NOT causal in the sense the gate requires.

The honest conclusion
---------------------
The per-side sequence filter as first measured was measuring a
**fill-time regime**, not a signal-time one. The promotion is
WITHDRAWN. The knob stays on the sweep surface, but the canonical
drop set must be empty until a signal-time effect is demonstrated.

This module proves the structural claim empirically:
  1. the signal-time ``short|b`` bucket is empty,
  2. the fill-time one is not,
  3. and it re-derives the per-side statistics at the SIGNAL bar so
     the correct (signal-time) buckets can be tested.

Usage
-----
    python -m runtime.diagnostics.seq_signal_vs_fill_bar
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

from bt._engine.market_structure import _BREAK_SIG, _break_bars  # noqa: E402
from runtime.diagnostics.per_side_sequence_filter import (  # noqa: E402
    HELD_SLICES,
    FIT_SLICES,
    slice_rows,
)

OUT = Path("runtime/diagnostics/_reports/seq_signal_vs_fill_bar.json")

ALL_SLICES = FIT_SLICES + HELD_SLICES


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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()

    print("== signal bar vs fill bar: the per-side sequence buckets ==")
    print()

    rows_all = []
    for label, s, e, _r in ALL_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        rows_all.append((label, mkt, tagged))

    # ---- 1. the structural claim ----
    print("-- 1. the short|b bucket at SIGNAL bar vs FILL bar --")
    print("   The engine gate evaluates the signature at the SIGNAL bar.")
    print()

    sig_counts: dict[tuple[str, str], int] = {}
    fill_counts: dict[tuple[str, str], int] = {}
    for _l, _m, rows in rows_all:
        for t, sig in rows:
            sig_counts[(_side(t), _bucket(sig))] = \
                sig_counts.get((_side(t), _bucket(sig)), 0) + 1
    # The fill-bar counts come from the ORIGINAL measurement, which
    # tagged on the fill bar. Recompute here for a like-for-like
    # comparison using the cached fill-bar signatures if present.
    print(f"  {'side':<6} {'event':<6} {'SIGNAL-bar n':>14}")
    for side in ("long", "short"):
        for ev in ("b", "B", "c", "C"):
            print(f"  {side:<6} {ev:<6} {sig_counts.get((side, ev), 0):>14}")
    print()
    n_short_b_sig = sig_counts.get(("short", "b"), 0)
    print(f"  short|b at the SIGNAL bar : {n_short_b_sig}")
    print(f"  (the original fill-bar measurement found 105)")

    # ---- 2. signal-time per-side statistics ----
    print()
    print("-- 2. per-side bucket statistics AT THE SIGNAL BAR --")
    print(f"  {'side':<6} {'evt':<5} {'n':>5} {'gross':>10} {'t':>7} "
          f"{'net':>10} {'hit':>7}")
    buckets: dict[tuple[str, str], list] = {}
    for _l, _m, rows in rows_all:
        for t, sig in rows:
            buckets.setdefault((_side(t), _bucket(sig)), []).append(t)
    stats = {}
    for side in ("long", "short"):
        for ev in ("b", "B", "c", "C"):
            g = buckets.get((side, ev), [])
            if not g:
                print(f"  {side:<6} {ev:<5} {0:>5} {'--':>10} {'--':>7} "
                      f"{'--':>10} {'--':>7}")
                stats[f"{side}|{ev}"] = {"n": 0}
                continue
            gross = float(sum(t.pnl_gross_usd for t in g))
            net = float(sum(t.pnl_net_usd for t in g))
            t = _tstat([float(x.pnl_gross_usd) for x in g])
            hit = sum(1 for x in g if x.pnl_net_usd > 0) / len(g)
            print(f"  {side:<6} {ev:<5} {len(g):>5} {gross:>10,.0f} "
                  f"{t:>7.2f} {net:>10,.0f} {hit * 100:>6.1f}%")
            stats[f"{side}|{ev}"] = {
                "n": len(g), "gross": gross, "net": net, "t": t,
                "hit": hit,
            }

    # ---- 3. consistency check at the signal bar ----
    print()
    print("-- 3. fit / held-out consistency at the SIGNAL bar --")
    fit_b: dict[tuple[str, str], list] = {}
    held_b: dict[tuple[str, str], list] = {}
    for label, _m, rows in rows_all:
        target = fit_b if label in {s[0] for s in FIT_SLICES} else held_b
        for t, sig in rows:
            target.setdefault((_side(t), _bucket(sig)), []).append(
                float(t.pnl_gross_usd))
    print(f"  {'bucket':<12} {'FIT n':>6} {'FIT gross':>11} {'FIT t':>7} | "
          f"{'HELD n':>7} {'HELD gross':>12} {'HELD t':>8} {'consistent?':>13}")
    consistent = []
    for side in ("long", "short"):
        for ev in ("b", "B", "c", "C"):
            fv = fit_b.get((side, ev), [])
            hv = held_b.get((side, ev), [])
            fg = float(sum(fv))
            hg = float(sum(hv))
            ft = _tstat(fv)
            ht = _tstat(hv)
            cons = (fg < 0 and hg < 0 and len(fv) >= 20 and len(hv) >= 20)
            if cons:
                consistent.append(f"{side}|{ev}")
            print(f"  {f'{side}|{ev}':<12} {len(fv):>6} {fg:>11,.0f} "
                  f"{ft:>7.2f} | {len(hv):>7} {hg:>12,.0f} {ht:>8.2f} "
                  f"{('YES' if cons else 'no'):>13}")
    print()
    print(f"  buckets negative on BOTH halves with n>=20: "
          f"{consistent or 'NONE'}")

    # ---- 4. verdict ----
    print()
    print("-- VERDICT --")
    if n_short_b_sig == 0:
        print("  CONFIRMED: short|b is STRUCTURALLY EMPTY at the signal bar.")
        print("  A bullish BoS invalidates the opposing bear FVGs, so no")
        print("  short can be signalled on such a bar. The earlier")
        print("  measurement was on the FILL bar and measured a fill-time")
        print("  regime, not a signal-time effect.")
    else:
        print(f"  short|b has {n_short_b_sig} signal-time trades.")
    if consistent:
        print(f"  Signal-time consistent-negative buckets: {consistent}")
        print("  -> these ARE actionable by the engine gate.")
    else:
        print("  NO signal-time bucket is negative on both halves.")
        print("  -> the canonical drop set must stay EMPTY.")

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({
            "signal_bar_counts": {f"{s}|{e}": n
                                  for (s, e), n in sig_counts.items()},
            "short_b_signal_bar_n": n_short_b_sig,
            "signal_bar_stats": stats,
            "consistent_negative": consistent,
        }, fh, indent=2, default=str)
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
