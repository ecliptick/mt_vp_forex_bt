"""TA SIGNAL-TO-SIGNAL SCOREBOARD — the EPISODE metric on real TAs.

WHAT THIS IS
============

The user's framing for evaluating a technical-analysis rule:

    each TA has LONG, SHORT and NO-SIGNAL
    -> the rule is a STATE STREAM
    -> the unit of evaluation is the INTERVAL BETWEEN TWO SIGNALS
    -> the metrics are the SIGNAL-TO-SIGNAL DELTA and the ACCURACY

This driver runs the whole TA brainstorm (:mod:`runtime.diagnostics.
ta_1m_core`) through that metric, over one month of 1m bars by
default, and reports for every cell:

    ACC     plain accuracy of the directional calls (0.5 = coin flip)
    WACC    accuracy WEIGHTED BY MAGNITUDE — right on big moves,
            wrong on small ones scores near zero
    SSDR    signal-to-signal delta RATE: directional reward in $/oz
            per exposure-minute. Rewards magnitude AND less time in
            market. THE HEADLINE COMPOSITE.
    TIM     time in market as a fraction of the trading session

AND, more importantly, it answers the two questions the repo's own
rules demand:

    "is the SIGN right?"       -> the coin-flip-SIDE placebo. The
                                  intervals, the prices, the
                                  abstentions and the time in market
                                  are all held fixed; only the side
                                  is randomised.
    "is the INTERVAL right?"    -> the random-TIME-SHIFT placebo. The
                                  rule's whole pattern is moved to a
                                  random time, keeping its sides,
                                  its durations and its abstentions.

A cell that cannot beat the coin-flip side placebo has NO
demonstrated sign skill, and the scoreboard says so. It is not
ranked as if it did.

WHY NOT USE THE OLD DAC/DREM METRIC
===================================

The earlier harness (:mod:`runtime.diagnostics.ta_1m_core`) scored a
rule by TRADING it: ATR stops, ATR targets, a pessimistic fill, a fee.
That measures "is this rule profitable after execution", which
answers the wrong question first. It also silently mixes the entry's
skill with the exit's opinion.

The episode metric has no stops, no targets and no fees. It answers
the question the user actually asked: **was the directional call
right, and how much did it earn?**

THE MONTHLY-GATE RULE
=====================

The window defaults to **2025-01-01 -> 2025-02-01** (the repo's
canonical one-month window). A longer window prints the cost
estimate and **EXITS** unless ``--i-know`` is passed — the same
mechanical gate :mod:`runtime.sweeps.cost_estimate` provides.

Usage
-----
    python -m runtime.diagnostics.ta_1m_signal_sweep
    python -m runtime.diagnostics.ta_1m_signal_sweep --strategy ema_cross
    python -m runtime.diagnostics.ta_1m_signal_sweep --json-only
    python -m runtime.diagnostics.ta_1m_signal_sweep \\
        --start 2025-06-01 --end 2025-07-01 --i-know
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from runtime.diagnostics.ta_1m_core import (  # noqa: E402
    build_bars_1m,
    build_strategy_signals,
)
from runtime.diagnostics.ta_signal_episodes import (  # noqa: E402
    score_fixed_horizon_with_placebo,
    score_signal_with_placebos,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars  # noqa: E402
from runtime.sweeps.cost_estimate import (  # noqa: E402
    DEFAULT_END,
    DEFAULT_START,
    estimate_sweep,
)

REPORT_DIR = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
REPORT_PATH = REPORT_DIR / "ta_1m_signal_sweep.json"


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/Inf -> None, numpy scalars -> python."""
    if isinstance(obj, dict):
        return {k: _clean(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_clean(v) for v in obj]
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.floating):
        obj = float(obj)
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    return obj


def _fmt(x: Any, spec: str = "8.4f") -> str:
    if x is None:
        return "n/a"
    try:
        return format(float(x), spec)
    except (TypeError, ValueError):
        return str(x)


def print_table(cells: list[dict[str, Any]]) -> None:
    """The signal-to-signal scoreboard."""
    print()
    print("=" * 132)
    print("TA SIGNAL-TO-SIGNAL SCOREBOARD  (signal-to-signal delta "
          "metric, no stops/targets/fees)")
    print("=" * 132)
    print(f"{'':4}{'strategy':<26} {'nEp':>5} {'ACC':>7} {'WACC':>8} "
          f"{'SSDR':>10} {'TIM':>7} {'exWACC':>8} {'p_side':>8} "
          f"{'exSSDR':>10} {'p_shift':>8} {'edge_pp':>8}")
    print("-" * 132)
    for c in cells:
        r = c["real"]
        sign = "S+" if c.get("sign_skill") else "S-"
        tim = "T+" if c.get("timing_skill") else "T-"
        print(f"{sign}{tim} {c['label'][:24]:<24} "
              f"{r['n_directional']:>5} "
              f"{_fmt(r['acc'], '7.3f'):>7} "
              f"{_fmt(r['wacc'], '8.3f'):>8} "
              f"{_fmt(r['ssdr'], '10.4f'):>10} "
              f"{_fmt(r['tim'], '7.3f'):>7} "
              f"{_fmt(c.get('wacc_excess_vs_side'), '8.3f'):>8} "
              f"{_fmt(c.get('p_wacc_vs_random_side'), '8.4f'):>8} "
              f"{_fmt(c.get('ssdr_excess_vs_shift'), '10.4f'):>10} "
              f"{_fmt(c.get('p_wacc_vs_random_shift'), '8.4f'):>8} "
              f"{_fmt(r['edge_pp'], '8.2f'):>8}")
    print("-" * 132)
    print("S+ = the SIGN call beats the coin-flip-side placebo "
          "(p<0.05) | S- = it does not")
    print("T+ = the INTERVAL selection beats the random-time-shift "
          "placebo (p<0.05) | T- = it does not")
    print("ACC=accuracy  WACC=magnitude-weighted accuracy  "
          "SSDR=$/oz per exposure-minute  TIM=time in market")


def print_fixed_table(cells: list[dict[str, Any]]) -> None:
    """The FIXED-CLOCK scoreboard — the artefact-immune one.

    The episode (signal-to-signal) score above is reported because the
    user asked for it, but it carries the **trigger-selection
    artefact**: a rule that flips at a price threshold selects its own
    exit price. The fixed-clock table is the one that does not.
    """
    print()
    print("=" * 132)
    print("FIXED-CLOCK SCOREBOARD  (exits on a fixed clock; "
          "immune to the trigger-selection artefact)")
    print("=" * 132)
    print(f"{'':4}{'strategy':<26} {'n':>6} {'ACC':>7} {'WACC':>8} "
          f"{'exWACC':>8} {'p_side':>8} {'exWACC_sh':>10} {'p_shift':>8} "
          f"{'drift':>9} {'TIM':>7}")
    print("-" * 132)
    for c in cells:
        fc = c.get("fixed_clock") or {}
        r = fc.get("real") or {}
        sign = "S+" if fc.get("sign_skill") else "S-"
        tim = "T+" if fc.get("timing_skill") else "T-"
        print(f"{sign}{tim} {c['label'][:24]:<24} "
              f"{r.get('n_entries', 0):>6} "
              f"{_fmt(r.get('acc'), '7.3f'):>7} "
              f"{_fmt(r.get('wacc'), '8.3f'):>8} "
              f"{_fmt(fc.get('wacc_excess_vs_side'), '8.3f'):>8} "
              f"{_fmt(fc.get('p_wacc_vs_random_side'), '8.4f'):>8} "
              f"{_fmt(fc.get('wacc_excess_vs_shift'), '10.3f'):>10} "
              f"{_fmt(fc.get('p_wacc_vs_random_shift'), '8.4f'):>8} "
              f"{_fmt(r.get('drift_gross'), '9.1f'):>9} "
              f"{_fmt(r.get('tim'), '7.3f'):>7}")
    print("-" * 132)
    print("drift = what a passive hold of the SAME sides over the SAME "
          "entry times earned")
    print("(a rule whose ACC is high but whose drift is also high is "
          "just riding the market)")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START,
                    help="Window start (YYYY-MM-DD). DEFAULT = one month.")
    ap.add_argument("--end", type=str, default=DEFAULT_END,
                    help="Window end (YYYY-MM-DD, exclusive).")
    ap.add_argument("--strategy", type=str, default="",
                    help="Only this strategy family (default: all).")
    ap.add_argument("--n-random", type=int, default=100,
                    help="Placebo arms per cell (default 100).")
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--close-delta", action="store_true",
                    help="Score the CLOSE-to-close delta instead of the "
                         "executable open-to-open delta.")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--i-know", action="store_true",
                    help="APPROVAL GATE: a window longer than one month "
                         "prints its cost estimate and exits without "
                         "running unless this flag is passed.")
    args = ap.parse_args()

    if not args.data.exists():
        raise SystemExit(f"data file not found: {args.data}")

    # ── APPROVAL GATE (the repo's mechanical one-month rule) ──────
    est = estimate_sweep(args.data, args.start, args.end, n_cells=1)
    print("== ta signal-to-signal sweep ==")
    print(f"data  : {args.data}")
    print(f"window: {args.start} .. {args.end}")
    print()
    print(est.render())
    print()
    if est.needs_approval and not args.i_know:
        print("ABORTED: window is longer than one month. "
              "Re-run with --i-know to execute.")
        raise SystemExit(2)

    # ── LOAD + BUILD THE 1m BARS ────────────────────────────────
    t0 = time.perf_counter()
    bars = load_bars(args.data, args.start, args.end)
    print(f"loaded {len(bars):,} 1s bars in {time.perf_counter() - t0:.1f}s",
          flush=True)

    t1 = time.perf_counter()
    b1 = build_bars_1m(bars)
    build_secs = time.perf_counter() - t1
    print(f"built {len(b1):,} 1m bars in {build_secs:.1f}s   "
          f"spot=${float(np.median(b1.close)):,.2f}", flush=True)

    if len(b1) < 100:
        raise SystemExit("not enough 1m bars in the window")

    # ── THE CELLS ───────────────────────────────────────────────
    names = [args.strategy] if args.strategy else None
    strategies = build_strategy_signals(b1, names=names)

    cells: list[dict[str, Any]] = []
    t2 = time.perf_counter()
    for si, strat in enumerate(strategies):
        res = score_signal_with_placebos(
            b1, strat.signal,
            n_random=args.n_random,
            seed=args.seed + si * 977,
            trading_minutes=b1.trading_minutes,
            executable=not args.close_delta,
        )
        # The FIXED-CLOCK score — the one that is immune to the
        # trigger-selection artefact. See ta_signal_episodes.
        fixed = score_fixed_horizon_with_placebo(
            b1, strat.signal,
            horizons=(5, 15, 30, 60, 120),
            n_random=args.n_random,
            seed=args.seed + si * 977 + 13,
            trading_minutes=b1.trading_minutes,
        )
        cells.append({
            "label": strat.label,
            "strategy": strat.name,
            "params": strat.params,
            **res,
            "fixed_clock": fixed,
        })
        print(f"  [{si + 1}/{len(strategies)}] {strat.label[:44]:<44} "
              f"{time.perf_counter() - t2:.1f}s", flush=True)

    # ── REPORT ───────────────────────────────────────────────────
    payload = {
        "ts_utc": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": str(args.data),
        "window": {"start": args.start, "end": args.end},
        "metric": "signal_to_signal_episode",
        "delta_basis": "close_to_close" if args.close_delta
                       else "executable_open_to_open",
        "n_1s_bars": int(len(bars)),
        "n_1m_bars": int(len(b1)),
        "build_secs": build_secs,
        "sweep_secs": time.perf_counter() - t2,
        "n_random": args.n_random,
        "seed": args.seed,
        "cells": cells,
    }

    if args.json_only:
        print(json.dumps(_clean(payload), indent=2, default=str))
        return

    print_table(cells)
    print_fixed_table(cells)

    # ── THE VERDICT ──────────────────────────────────────────────
    print()
    print("=" * 132)
    print("VERDICT")
    print("=" * 132)
    if not cells:
        print("no cells measured")
        return

    with_sign = [c for c in cells if c.get("sign_skill")]
    with_timing = [c for c in cells if c.get("timing_skill")]
    both = [c for c in cells
            if c.get("sign_skill") and c.get("timing_skill")]

    print(f"cells measured                          : {len(cells)}")
    print(f"cells with SIGN skill (beats coin flip) : {len(with_sign)}")
    print(f"cells with INTERVAL skill (beats shift) : {len(with_timing)}")
    print(f"cells with BOTH                         : {len(both)}")
    print()

    if not with_sign:
        print("NO cell's DIRECTIONAL CALL was distinguishable from a coin "
              "flip.")
        print("=> On this window the brainstorm shows NO demonstrated "
              "directional skill.")
        print("   That is the honest answer, not a bug.")
    else:
        with_sign.sort(key=lambda c: -float(c.get("wacc_excess_vs_side")
                                             or 0.0))
        print("TOP CELLS BY SIGN SKILL (magnitude-weighted accuracy "
              "excess):")
        print(f"  {'label':<42} {'WACC':>8} {'exWACC':>8} {'p':>7} "
              f"{'ACC':>7} {'SSDR':>10} {'TIM':>7}")
        for c in with_sign[:12]:
            r = c["real"]
            print(f"  {c['label'][:41]:<41} "
                  f"{_fmt(r['wacc'], '8.3f'):>8} "
                  f"{_fmt(c.get('wacc_excess_vs_side'), '8.3f'):>8} "
                  f"{_fmt(c.get('p_wacc_vs_random_side'), '7.4f'):>7} "
                  f"{_fmt(r['acc'], '7.3f'):>7} "
                  f"{_fmt(r['ssdr'], '10.4f'):>10} "
                  f"{_fmt(r['tim'], '7.3f'):>7}")

    if with_timing:
        with_timing.sort(key=lambda c: -float(c.get("ssdr_excess_vs_shift")
                                               or 0.0))
        print()
        print("TOP CELLS BY INTERVAL SKILL (SSDR excess vs the "
              "random-time-shift placebo):")
        print(f"  {'label':<42} {'SSDR':>10} {'exSSDR':>10} {'p':>7} "
              f"{'WACC':>8}")
        for c in with_timing[:8]:
            r = c["real"]
            print(f"  {c['label'][:41]:<41} "
                  f"{_fmt(r['ssdr'], '10.4f'):>10} "
                  f"{_fmt(c.get('ssdr_excess_vs_shift'), '10.4f'):>10} "
                  f"{_fmt(c.get('p_wacc_vs_random_shift'), '7.4f'):>7} "
                  f"{_fmt(r['wacc'], '8.3f'):>8}")

    # The per-side split — the repo's central question.
    print()
    print("PER-SIDE ACCURACY (the repo's central long/short split):")
    print(f"  {'label':<42} {'long ACC':>9} {'short ACC':>10} "
          f"{'nL':>6} {'nS':>6}")
    for c in sorted(cells, key=lambda c: -(c["real"].get("long_acc") or 0))[:12]:
        r = c["real"]
        print(f"  {c['label'][:41]:<41} "
              f"{_fmt(r['long_acc'], '9.3f'):>9} "
              f"{_fmt(r['short_acc'], '10.3f'):>10} "
              f"{r['n_long']:>6} {r['n_short']:>6}")

    print()
    print("NOTE: this metric has NO stops, NO targets and NO fees, so a "
          "positive")
    print("      SSDR is NOT a claim of profitability. It is a claim that "
          "the")
    print("      DIRECTIONAL CALLS were right and earned magnitude while "
          "in")
    print("      the market.")

    # ── THE FIXED-CLOCK VERDICT — the artefact-immune one ─────────
    fc_sign = [c for c in cells
               if (c.get("fixed_clock") or {}).get("sign_skill")]
    fc_both = [c for c in cells
               if (c.get("fixed_clock") or {}).get("sign_skill")
               and (c.get("fixed_clock") or {}).get("timing_skill")]
    print()
    print("=" * 132)
    print("VERDICT — FIXED CLOCK (the artefact-immune score)")
    print("=" * 132)
    print(f"cells measured                        : {len(cells)}")
    print(f"cells with SIGN skill on a fixed clock : {len(fc_sign)}")
    print(f"cells with BOTH on a fixed clock       : {len(fc_both)}")
    print()
    if not fc_sign:
        print("NO cell's directional call is distinguishable from a coin "
              "flip")
        print("ON A FIXED CLOCK. The episode (signal-to-signal) score's "
              "apparent")
        print("winners are the trigger-selection artefact, not skill.")
    else:
        fc_sign.sort(key=lambda c: -float(
            (c.get("fixed_clock") or {}).get("wacc_excess_vs_side") or 0.0))
        print("TOP CELLS ON A FIXED CLOCK (sign skill):")
        print(f"  {'label':<42} {'ACC':>7} {'WACC':>8} {'exWACC':>8} "
              f"{'p':>7} {'exShift':>9} {'p_shift':>8}")
        for c in fc_sign[:12]:
            fc = c.get("fixed_clock") or {}
            r = fc.get("real") or {}
            print(f"  {c['label'][:41]:<41} "
                  f"{_fmt(r.get('acc'), '7.3f'):>7} "
                  f"{_fmt(r.get('wacc'), '8.3f'):>8} "
                  f"{_fmt(fc.get('wacc_excess_vs_side'), '8.3f'):>8} "
                  f"{_fmt(fc.get('p_wacc_vs_random_side'), '7.4f'):>7} "
                  f"{_fmt(fc.get('wacc_excess_vs_shift'), '9.3f'):>9} "
                  f"{_fmt(fc.get('p_wacc_vs_random_shift'), '8.4f'):>8}")
        print()
        print(f"cells with BOTH sign and interval skill on a fixed clock: "
              f"{len(fc_both)}")
        for c in fc_both:
            print(f"  {c['label']}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(_clean(payload), indent=2, default=str),
        encoding="utf-8")
    print()
    print(f"-> {REPORT_PATH}")


if __name__ == "__main__":
    main()
