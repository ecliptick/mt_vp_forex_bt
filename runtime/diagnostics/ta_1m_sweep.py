"""TA 1-MINUTE SWEEP — score simple TA rules on one month of 1m bars.

WHAT THIS DOES
==============

The repo's evidence is about ONE strategy (the v22 SNIPER FVG recipe)
on 1s bars. This runs a **brainstorm**: a set of simple, hand-written
technical-analysis rules, each on **1-MINUTE** bars, scored by the
metric in :mod:`runtime.diagnostics.ta_1m_core`.

For every strategy cell the harness reports three numbers and, more
importantly, **proves whether the metric is directionally valid for
that cell**:

* ``DAC``    — direction correctness, weighted by the magnitude of the
  directional moves that happened while the rule was in the market.
* ``DREM``   — directional reward per exposure-minute: the composite
  that rewards magnitude AND less time in market.
* ``TIM``    — time in market as a fraction of the trading session.

And the validity check, which is the point of the whole harness:

* ``as_is``   the rule's own side
* ``flip``    the negated side — destroys DIRECTION, keeps the schedule
* ``random``  N coin-flip sides — destroys DIRECTION, keeps the schedule

The placebo arms share the ENTRY BARS and the ENTIRE execution model
(same ATR stops, same pessimistic fill, same fee model). Only the SIDE
differs. That is what makes them EXCHANGEABLE — the repo's hard rule
for a control, learned from the confounded ``leg_random_entry`` test.

If the real arm cannot be told from the placebos, the metric is
reported as **INVALID for that cell**, not as a score.

ONE MONTH ONLY
==============

The window defaults to **2025-01-01 -> 2025-02-01** (the repo's
canonical one-month window, from ``runtime.sweeps.cost_estimate``).
A longer window prints the cost estimate and EXITS unless
``--i-know`` is passed — the same mechanical gate the sweep driver
uses.

Usage
-----
    python -m runtime.diagnostics.ta_1m_sweep
    python -m runtime.diagnostics.ta_1m_sweep --strategy ema_cross
    python -m runtime.diagnostics.ta_1m_sweep --json-only
    python -m runtime.diagnostics.ta_1m_sweep --sweep-exit
    python -m runtime.diagnostics.ta_1m_sweep --start 2025-06-01 \\
        --end 2025-07-01
"""
from __future__ import annotations

import argparse
import json
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
    DEFAULT_MAX_HOLD_BARS,
    DEFAULT_SL_ATR_MULT,
    DEFAULT_TP_ATR_MULT,
    build_bars_1m,
    build_strategy_signals,
    score_with_placebos,
    set_fee_spot,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars  # noqa: E402
from runtime.sweeps.cost_estimate import (  # noqa: E402
    DEFAULT_END,
    DEFAULT_START,
    estimate_sweep,
)

REPORT_DIR = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
REPORT_PATH = REPORT_DIR / "ta_1m_sweep.json"

#: The exit grid used by ``--sweep-exit``. It is a SEPARATE sweep so
#: the strategy sweep is not confounded by the exit geometry.
EXIT_GRID: list[dict[str, Any]] = [
    {"sl_mult": 1.0, "tp_mult": 2.0, "max_hold_bars": 120},
    {"sl_mult": 1.5, "tp_mult": 3.0, "max_hold_bars": 240},
    {"sl_mult": 2.0, "tp_mult": 4.0, "max_hold_bars": 240},
    {"sl_mult": 1.5, "tp_mult": 2.0, "max_hold_bars": 60},
    {"sl_mult": 2.5, "tp_mult": 5.0, "max_hold_bars": 480},
]


def _clean(obj: Any) -> Any:
    """JSON-safe: NaN/Inf -> None, numpy scalars -> python."""
    import math
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
    """The scoreboard."""
    print()
    print("=" * 128)
    print("TA 1-MINUTE SCOREBOARD  (one month)")
    print("=" * 128)
    print(f"{'strategy':<26} {'n':>6} {'DAC':>7} {'DREM':>9} "
          f"{'TIM':>7} {'exDREM':>9} {'p':>7} {'gross/lg':>9} "
          f"{'fee/lg':>8} {'x fee':>7} {'net':>10} {'t_exc':>7}")
    print("-" * 128)
    for c in cells:
        real = c["real"]
        flag = "OK " if c.get("metric_is_directionally_valid") else "INV"
        print(f"{flag} {c['label'][:23]:<23} {real['n']:>6} "
              f"{_fmt(real['dac_path'], '7.3f'):>7} "
              f"{_fmt(real['drem'], '9.4f'):>9} "
              f"{_fmt(real['tim'], '7.3f'):>7} "
              f"{_fmt(c.get('drem_excess'), '9.4f'):>9} "
              f"{_fmt(c.get('p_drem_vs_random'), '7.4f'):>7} "
              f"{_fmt(real['gross_per_leg'], '9.3f'):>9} "
              f"{_fmt(real['fee_per_leg'], '8.3f'):>8} "
              f"{_fmt(real['gross_per_leg_over_fee'], '7.2f'):>7} "
              f"{_fmt(real['net_total'], '10.2f'):>10} "
              f"{_fmt(real['t_excess'], '7.2f'):>7}")
    print("-" * 128)
    print("OK = the metric is directionally valid for this cell (beats "
          "the coin-flip placebo at p<0.05)")
    print("INV = the metric CANNOT distinguish this rule from a coin "
          "flip on this window -> the score is not evidence of skill")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START,
                    help="Window start (YYYY-MM-DD). DEFAULT = one month.")
    ap.add_argument("--end", type=str, default=DEFAULT_END,
                    help="Window end (YYYY-MM-DD, exclusive).")
    ap.add_argument("--strategy", type=str, default="",
                    help="Only this strategy family (default: all).")
    ap.add_argument("--sweep-exit", action="store_true",
                    help="Also sweep the exit geometry (a separate grid).")
    ap.add_argument("--n-random", type=int, default=100,
                    help="Coin-flip placebo arms per cell (default 100).")
    ap.add_argument("--seed", type=int, default=11)
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
    print("== ta 1m sweep ==")
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
    spot = float(np.median(b1.close))
    set_fee_spot(spot)
    print(f"built {len(b1):,} 1m bars in {build_secs:.1f}s   "
          f"spot=${spot:,.2f}   "
          f"median 1m ATR=${float(np.median(b1.atr)):.3f}   "
          f"round trip=${0.01 * 7.0 + 1.0 * 0.40:.2f}", flush=True)

    if len(b1) < 100:
        raise SystemExit("not enough 1m bars in the window")

    # ── THE CELLS ───────────────────────────────────────────────
    names = [args.strategy] if args.strategy else None
    strategies = build_strategy_signals(b1, names=names)

    exit_grid = EXIT_GRID if args.sweep_exit else [{
        "sl_mult": DEFAULT_SL_ATR_MULT,
        "tp_mult": DEFAULT_TP_ATR_MULT,
        "max_hold_bars": DEFAULT_MAX_HOLD_BARS,
    }]

    cells: list[dict[str, Any]] = []
    t2 = time.perf_counter()
    for si, strat in enumerate(strategies):
        for eg in exit_grid:
            label = strat.label
            if args.sweep_exit:
                label += (f"|sl{eg['sl_mult']}tp{eg['tp_mult']}"
                          f"h{eg['max_hold_bars']}")
            res = score_with_placebos(
                b1, strat.signal,
                sl_mult=float(eg["sl_mult"]),
                tp_mult=float(eg["tp_mult"]),
                max_hold_bars=int(eg["max_hold_bars"]),
                n_random=args.n_random,
                seed=args.seed + si * 977,
                trading_minutes=len(b1),
            )
            cells.append({
                "label": label,
                "strategy": strat.name,
                "params": strat.params,
                "exit": dict(eg),
                **res,
            })
        print(f"  [{si + 1}/{len(strategies)}] {strat.name:<18} "
              f"{len(cells)} cell(s)  ({time.perf_counter() - t2:.1f}s)",
              flush=True)

    # ── REPORT ───────────────────────────────────────────────────
    payload = {
        "ts_utc": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": str(args.data),
        "window": {"start": args.start, "end": args.end},
        "n_1s_bars": int(len(bars)),
        "n_1m_bars": int(len(b1)),
        "spot_median_usd": spot,
        "median_1m_atr_usd": float(np.median(b1.atr)),
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

    # ── THE VERDICT ──────────────────────────────────────────────
    valid = [c for c in cells if c.get("metric_is_directionally_valid")]
    print()
    print("=" * 128)
    print("VERDICT")
    print("=" * 128)
    if not cells:
        print("no cells measured")
        return

    print(f"cells measured              : {len(cells)}")
    print(f"cells with a valid direction : {len(valid)}")
    print()

    if not valid:
        print("NO cell's direction call was distinguishable from a coin "
              "flip.")
        print("=> The metric reports NO directional skill in this "
              "brainstorm on this window.")
        print("   A score of 0 here is the honest answer, not a bug.")
    else:
        valid.sort(key=lambda c: -float(c.get("drem_excess") or 0.0))
        print("TOP CELLS (metric directionally valid):")
        print(f"  {'label':<40} {'DREM':>9} {'exDREM':>9} {'p':>7} "
              f"{'net':>10} {'TIM':>7}")
        for c in valid[:10]:
            real = c["real"]
            print(f"  {c['label'][:39]:<39} "
                  f"{_fmt(real['drem'], '9.4f'):>9} "
                  f"{_fmt(c.get('drem_excess'), '9.4f'):>9} "
                  f"{_fmt(c.get('p_drem_vs_random'), '7.4f'):>7} "
                  f"{_fmt(real['net_total'], '10.2f'):>10} "
                  f"{_fmt(real['tim'], '7.3f'):>7}")

    # The fee reality check.
    covering = [c for c in cells
                if c["real"]["n"] > 0
                and c["real"]["gross_per_leg_over_fee"] >= 1.0]
    print()
    print(f"cells whose gross/leg covers the measured round trip: "
          f"{len(covering)}/{len(cells)}")
    if valid:
        best_net = max(valid, key=lambda c: float(c["real"]["net_total"]))
        print(f"best NET among directionally-valid cells: "
              f"{best_net['label']} = "
              f"${float(best_net['real']['net_total']):,.2f}")
    else:
        print("no directionally-valid cell -> no P&L claim is made")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(_clean(payload), indent=2, default=str),
        encoding="utf-8")
    print()
    print(f"-> {REPORT_PATH}")


if __name__ == "__main__":
    main()
