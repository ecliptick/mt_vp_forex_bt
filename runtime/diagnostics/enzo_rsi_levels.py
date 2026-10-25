"""ENZO RULE 1 — the RSI LEVEL VARIANT (18/82 instead of 16/84).

WHY THIS EXISTS
===============

``docs/enzo_strategies.md`` § 1 pins Rule 1's levels at the PDF's
reading: **level set 16 and 84**, SELL when RSI hits 84, BUY when RSI
hits 16, and the post-SL retry admitted only if the RSI is still
"above level 82 / below 16".

The user asked for the same rule run with the level set **18 and 82**
instead. Every other ingredient is held FIXED:

* the same corpus, ONE CONTINUOUS execution with a 14-day warmup
  prefix (the month is a reporting dimension, never an execution
  boundary);
* the same Wilder RSI(14) on the same 1m grid;
* the same episode lifecycle (TP ends the episode, SL licenses exactly
  ONE retry, a 1-hour cooldown after the episode);
* the same PESSIMISTIC fill model (the stop wins an ambiguous bar, a
  gap fills at the open);
* the same canonical fee model (0.01 lots x 100 oz = 1.00 oz, $0.07
  commission + $0.40 spread, scaled by the fill price);
* the same EXCHANGEABLE control (the sign-flip placebo — Rule 1
  enters at the MARKET on the next bar's open, so flipping the side
  keeps the execution model identical).

THE TWO TRIGGER READINGS ARE BOTH RUN
=====================================

The doc reports Rule 1 under two readings of "SELL when hit level 84":

``crossing``
    the signal fires when the RSI CROSSES the level (reaches 84 from
    below / 16 from above). This is the CANONICAL reading.

``level test``
    the signal fires whenever the RSI is at or beyond the level, so a
    persistent extreme re-fires each time the cooldown expires.

Both are measured here, for the canonical levels AND for the new ones,
because the trigger semantics are the thing the user asked about.

THE ASSUMPTION MADE EXPLICIT
=============================

The retry threshold is NOT stated for the new level set, so it is
derived the way the canonical one is: the retry is admitted while the
RSI is still beyond the retry threshold ON THE TRIGGER'S SIDE.

===============  ==============  =====================
arm              trigger         retry admitted while
===============  ==============  =====================
canonical        sell RSI >= 84  RSI > 82  (sell side)
                 buy  RSI <= 16  RSI < 16  (buy side)
NEW (18/82)      sell RSI >= 82  RSI > 80  (sell side)
                 buy  RSI <= 18  RSI < 18  (buy side)
===============  ==============  =====================

The "new levels, retry UNCHANGED (82/16)" arm is also run, so the
trigger change is separated from the retry-threshold change.

Usage
-----
    python -m runtime.diagnostics.enzo_rsi_levels --i-know
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

from runtime.diagnostics.enzo_backtest import (  # noqa: E402
    _clean,
    month_table,
    placebo,
)
from runtime.diagnostics.enzo_core import (  # noqa: E402
    COOLDOWN_MIN,
    M1_EXIT_CELL_LIST,
    RSI_PERIOD,
    EnzoLeg,
    build_enzo_bars,
    drift_excess,
    run_m1_rsi,
    score_legs,
    set_fee_spot,
    wilder_rsi,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars  # noqa: E402
from runtime.sweeps.cost_estimate import estimate_sweep  # noqa: E402

DEFAULT_START = "2024-01-01"
DEFAULT_END = "2026-07-16"
WARMUP_DAYS = 14

REPORT_DIR = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
REPORT_PATH = REPORT_DIR / "enzo_rsi_levels.json"

#: The canonical window for this report. The artifact at
#: ``REPORT_PATH`` is the full-corpus evidence record that
#: ``docs/enzo_strategies.md`` § 1a cites, so a SHORTER run must not
#: silently overwrite it.
#:
#: This is the same guard the repo already uses for the canonical leg
#: ledger (``runtime/diagnostics/leg_ledger._guard_canonical_path``):
#: an artifact that every reader treats as "the canonical result" must
#: be structurally unable to hold anything else. Without it, a 6-second
#: one-month smoke run replaces the 927-day measurement and the only
#: symptom is that the doc and the artifact quietly disagree.
CANONICAL_START = DEFAULT_START
CANONICAL_END = DEFAULT_END


def _guard_canonical_report(*, out_path: Path, start: str, end: str,
                             allow_variant_canonical: bool = False) -> None:
    """Refuse to write a SHORT-WINDOW run over the canonical artifact.

    Two ways out, both explicit:
      * write to your own path (any path other than the canonical one),
        or
      * pass ``allow_variant_canonical=True`` to assert that you are
        deliberately replacing the canonical artifact.
    """
    if out_path.resolve() != REPORT_PATH.resolve():
        return  # a variant writing to its own file is fine
    if (start, end) == (CANONICAL_START, CANONICAL_END):
        return  # the canonical window
    if allow_variant_canonical:
        return
    raise SystemExit(
        f"REFUSING to write a non-canonical window ({start} .. {end}) over\n"
        f"the canonical report:\n  {REPORT_PATH}\n"
        "That artifact is the full-corpus evidence record cited by\n"
        "docs/enzo_strategies.md section 1a.\n"
        "Write to your own --out path, or pass --allow-variant-canonical\n"
        "to assert that you are deliberately replacing it.")


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=CANONICAL_START)
    ap.add_argument("--end", type=str, default=CANONICAL_END)
    ap.add_argument("--n-random", type=int, default=500)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--out", type=Path, default=REPORT_PATH,
                    help="where to write the report JSON")
    ap.add_argument("--allow-variant-canonical", action="store_true",
                    help="assert that you are deliberately replacing the "
                         "canonical report with a non-canonical window")
    ap.add_argument("--i-know", action="store_true",
                    help="APPROVAL GATE: a window longer than one month "
                         "prints its cost estimate and exits without "
                         "running unless this flag is passed.")
    return ap.parse_args(argv)


#: The LEVEL SETS. Each is
#:     (label, sell_level, buy_level, sell_retry, buy_retry, note)
#: with the retry thresholds derived the way the canonical one is: the
#: retry is admitted while the RSI is still beyond the retry threshold
#: ON THE TRIGGER'S SIDE.
LEVEL_SETS: tuple[tuple[str, float, float, float, float, str], ...] = (
    ("canonical 84/16", 84.0, 16.0, 82.0, 16.0,
     "the PDF reading: SELL at RSI>=84, BUY at RSI<=16, retry while "
     "RSI>82 / RSI<16"),
    ("levels 82/18", 82.0, 18.0, 80.0, 18.0,
     "THE REQUESTED LEVEL SET: SELL at RSI>=82, BUY at RSI<=18, retry "
     "while RSI>80 / RSI<18"),
    ("levels 82/18, retry 82/16", 82.0, 18.0, 82.0, 16.0,
     "the new TRIGGER with the retry thresholds UNCHANGED, to separate "
     "the trigger change from the retry-threshold change"),
)

#: The two readings of "SELL when hit level 84".
TRIGGERS: tuple[tuple[str, bool], ...] = (
    ("crossing", True),      # the RSI CROSSES the level (canonical)
    ("level test", False),   # the RSI is AT OR BEYOND the level
)


def evaluate(legs: list[EnzoLeg], bars, n_random: int, seed: int
             ) -> dict[str, Any]:
    """The scorecard for one cell."""
    s = score_legs(legs)
    de = drift_excess(legs, bars)
    pl = placebo(bars, legs, n_random, seed)

    # The drift share of the gross is only meaningful when the gross
    # total is a material fraction of the gross ACTIVITY. When winners
    # and losers nearly cancel (|gross_total| << sum|gross|) the ratio
    # explodes and flips sign, so it is reported as None in that regime
    # -- the ABSOLUTE totals are the numbers to read. (Same rule as
    # runtime/diagnostics/leg_buckets.py.)
    gross_abs = sum(abs(lg.gross_usd) for lg in legs)
    if gross_abs > 0 and abs(s["gross_total"]) >= 0.05 * gross_abs:
        dshare: float | None = de["drift_total"] / s["gross_total"]
    else:
        dshare = None

    return {
        "n": s["n"],
        "gross_total": s["gross_total"],
        "gross_abs_total": gross_abs,
        "fees_total": s["fees_total"],
        "net_total": s["net_total"],
        "gross_per_leg": s["gross_per_leg"],
        "fee_per_leg": s["fee_per_leg"],
        "gross_per_leg_over_fee": s["gross_per_leg_over_fee"],
        "hit_rate": s["hit_rate"],
        "payoff": s["payoff"],
        "break_even_hit": s["break_even_hit"],
        "edge_pp": s["edge_pp"],
        "avg_win": s["avg_win"],
        "avg_loss": s["avg_loss"],
        "n_long": s["n_long"],
        "n_short": s["n_short"],
        "gross_long": s["gross_long"],
        "gross_short": s["gross_short"],
        "n_attempt2": s["n_attempt2"],
        "median_hold_min": s["median_hold_min"],
        "drift_total": de["drift_total"],
        "drift_share_of_gross": dshare,
        "excess_total": de["excess_total"],
        "excess_per_leg": de["excess_per_leg"],
        "t_excess": de["t_excess"],
        "exits": s["exits"],
        "sides": s["sides"],
        "placebo": pl,
        "directionally_valid": bool(pl["p_gross_vs_random"] < 0.05
                                    and pl["gross_excess"] > 0.0),
        "months": month_table(legs),
    }


def main() -> None:
    args = _parse_args()

    if not args.data.exists():
        raise SystemExit(f"data file not found: {args.data}")

    # ── the canonical-path guard, BEFORE any work ───────────────
    _guard_canonical_report(
        out_path=args.out, start=args.start, end=args.end,
        allow_variant_canonical=args.allow_variant_canonical)

    est = estimate_sweep(args.data, args.start, args.end, n_cells=1)
    print("== enzo rule 1 - rsi level variant (18/82) ==")
    print(f"data  : {args.data}")
    print(f"window: {args.start} .. {args.end}")
    print()
    print(est.render())
    print()
    if est.needs_approval and not args.i_know:
        print("ABORTED: window is longer than one month. "
              "Re-run with --i-know to execute.")
        raise SystemExit(2)

    # ── LOAD + BUILD ─────────────────────────────────────────────
    t0 = time.perf_counter()
    load_start = (pd.Timestamp(args.start, tz="UTC")
                  - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    bars_1s = load_bars(args.data, load_start, args.end)
    print(f"loaded {len(bars_1s):,} 1s bars in {time.perf_counter() - t0:.1f}s"
          f"   (warmup from {load_start})", flush=True)

    t1 = time.perf_counter()
    b = build_enzo_bars(bars_1s)
    spot = float(np.median(b.close))
    set_fee_spot(spot)
    rsi = wilder_rsi(b.close, RSI_PERIOD)
    build_secs = time.perf_counter() - t1
    print(f"built {len(b):,} 1m bars in {build_secs:.1f}s   "
          f"spot=${spot:,.2f}   RSI>=84 on {int((rsi >= 84).sum()):,} bars, "
          f"RSI<=16 on {int((rsi <= 16).sum()):,} bars, "
          f"RSI>=82 on {int((rsi >= 82).sum()):,} bars, "
          f"RSI<=18 on {int((rsi <= 18).sum()):,} bars", flush=True)

    score_from_ns = int(pd.Timestamp(args.start, tz="UTC").value)
    score_to_ns = int(pd.Timestamp(args.end, tz="UTC").value)

    def _in_window(legs: list[EnzoLeg]) -> list[EnzoLeg]:
        """Keep only the legs whose EXIT falls in the scored window."""
        return [lg for lg in legs
                if score_from_ns <= lg.exit_time_ns < score_to_ns]

    # ── CELLS ─────────────────────────────────────────────────────
    cells: list[dict[str, Any]] = []
    t2 = time.perf_counter()
    k = 0
    for ls_label, sell_lv, buy_lv, sell_rt, buy_rt, note in LEVEL_SETS:
        for trig_label, cross_trigger in TRIGGERS:
            for ec in M1_EXIT_CELL_LIST:
                legs = _in_window(run_m1_rsi(
                    b, rsi, tp_usd=ec["tp_usd"], sl_usd=ec["sl_usd"],
                    cooldown_min=COOLDOWN_MIN,
                    sell_level=sell_lv, buy_level=buy_lv,
                    sell_retry_level=sell_rt, buy_retry_level=buy_rt,
                    cross_trigger=cross_trigger))
                label = (f"rsi {ls_label} | {trig_label} | "
                         f"tp{ec['tp_usd']:.0f} sl{ec['sl_usd']:.0f}")
                cell = evaluate(legs, b, args.n_random,
                                args.seed + 137 * k)
                cell["label"] = label
                cell["level_set"] = ls_label
                cell["trigger"] = trig_label
                cell["cross_trigger"] = cross_trigger
                cell["sell_level"] = sell_lv
                cell["buy_level"] = buy_lv
                cell["sell_retry_level"] = sell_rt
                cell["buy_retry_level"] = buy_rt
                cell["tp_usd"] = ec["tp_usd"]
                cell["sl_usd"] = ec["sl_usd"]
                cell["note"] = note
                cells.append(cell)
                k += 1
                print(f"  [{k:>2}] {label}: {len(legs):,} legs "
                      f"({time.perf_counter() - t2:.1f}s)", flush=True)

    payload = {
        "ts_utc": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": str(args.data),
        "window": {"start": args.start, "end": args.end},
        "warmup_days": WARMUP_DAYS,
        "warmup_start": load_start,
        "rsi_period": RSI_PERIOD,
        "cooldown_min": COOLDOWN_MIN,
        "spot_median_usd": spot,
        "n_1m_bars": int(len(b)),
        "build_secs": build_secs,
        "sweep_secs": time.perf_counter() - t2,
        "n_random": args.n_random,
        "seed": args.seed,
        "level_sets": [
            {"label": a, "sell_level": b_, "buy_level": c,
             "sell_retry_level": d, "buy_retry_level": e, "note": f}
            for a, b_, c, d, e, f in LEVEL_SETS
        ],
        "triggers": [t for t, _ in TRIGGERS],
        "cells": cells,
    }

    if args.json_only:
        print(json.dumps(_clean(payload), indent=2, default=str))
        return

    # ── TABLE ─────────────────────────────────────────────────────
    print()
    print("=" * 150)
    print("ENZO RULE 1 - RSI LEVEL VARIANT (18/82 vs the canonical 16/84)")
    print("=" * 150)
    print(f"{'level set':<28} {'trigger':<11} {'tp/sl':<9} {'n':>6} "
          f"{'gross':>10} {'fees':>10} {'net':>11} {'g/lg':>8} "
          f"{'x fee':>7} {'hit':>6} {'edge_pp':>8} {'drift%':>7} "
          f"{'excess':>9} {'t_exc':>7} {'p':>7}")
    print("-" * 150)
    for c in cells:
        dshare = c["drift_share_of_gross"]
        dstr = "n/a" if dshare is None else f"{dshare:>6.1%}"
        print(f"{c['level_set']:<28} {c['trigger']:<11} "
              f"{'tp' + str(int(c['tp_usd'])) + '/sl' + str(int(c['sl_usd'])):<9} "
              f"{c['n']:>6,} {c['gross_total']:>10,.2f} "
              f"{c['fees_total']:>10,.2f} {c['net_total']:>11,.2f} "
              f"{c['gross_per_leg']:>8.4f} "
              f"{c['gross_per_leg_over_fee']:>7.2f} "
              f"{c['hit_rate']:>6.3f} {c['edge_pp']:>8.2f} "
              f"{dstr:>7} "
              f"{c['excess_total']:>9,.2f} {c['t_excess']:>7.2f} "
              f"{c['placebo']['p_gross_vs_random']:>7.4f}")
    print("-" * 150)
    print("x fee   = gross/leg divided by the MEASURED fee/leg. Below 1.00 "
          "means the gross")
    print("         does NOT cover the round trip - that is NOT an edge.")
    print("p       = the sign-flip placebo's empirical p-value (the "
          "EXCHANGEABLE control")
    print("         for Rule 1: it enters at the MARKET, so a side flip "
          "is fair).")
    print("drift%  = the drift's share of the gross. Near or above 100% "
          "means the realised")
    print("         P&L IS the market's drift. 'n/a' when the gross total "
          "is near zero")
    print("         (winners and losers nearly cancel) - read the "
          "ABSOLUTE totals then.")

    # ── VERDICT ───────────────────────────────────────────────────
    print()
    print("=" * 150)
    print("VERDICT (per cell)")
    print("=" * 150)
    for c in cells:
        v = []
        if c["n"] == 0:
            v.append("NO LEGS")
        else:
            if not c["directionally_valid"]:
                v.append("NO DIRECTIONAL SKILL (indistinguishable from a "
                         "coin flip)")
            if c["gross_per_leg_over_fee"] < 1.0:
                v.append("gross does NOT cover the round trip")
            if c["t_excess"] < -2.0:
                v.append("the gross IS market beta (excess t<-2)")
            elif c["t_excess"] > 2.0:
                v.append("timing is not beta (excess t>2)")
        print(f"  {c['label']:<58} net ${c['net_total']:>10,.2f}   "
              f"{'; '.join(v) if v else 'gross edge covers fees'}")

    # ── THE HEAD-TO-HEAD: canonical vs the new level set ──────────
    print()
    print("=" * 150)
    print("HEAD-TO-HEAD A: the canonical 84/16 vs the new 82/18 "
          "(same trigger, same exit geometry)")
    print("=" * 150)
    for trig in ("crossing", "level test"):
        for ec in M1_EXIT_CELL_LIST:
            key = f"tp{int(ec['tp_usd'])} sl{int(ec['sl_usd'])}"
            sel = {c["label"]: c for c in cells
                   if c["trigger"] == trig
                   and c["tp_usd"] == ec["tp_usd"]
                   and c["sl_usd"] == ec["sl_usd"]}
            canon = sel.get(f"rsi canonical 84/16 | {trig} | {key}")
            new = sel.get(f"rsi levels 82/18 | {trig} | {key}")
            if canon is None or new is None:
                continue
            d_net = new["net_total"] - canon["net_total"]
            d_gpl = new["gross_per_leg"] - canon["gross_per_leg"]
            print(f"  {trig:<11} {key:<11}  n {canon['n']:>5,} -> "
                  f"{new['n']:>5,}   net ${canon['net_total']:>9,.2f} -> "
                  f"${new['net_total']:>9,.2f}  (delta ${d_net:+,.2f})   "
                  f"g/lg {canon['gross_per_leg']:+.4f} -> "
                  f"{new['gross_per_leg']:+.4f}  (delta {d_gpl:+.4f})")

    # ── THE HEAD-TO-HEAD: crossing vs level test ─────────────────
    print()
    print("=" * 150)
    print("HEAD-TO-HEAD B: the CROSSING trigger vs the LEVEL TEST "
          "(same level set, same exit geometry)")
    print("=" * 150)
    for ls_label in ("canonical 84/16", "levels 82/18",
                     "levels 82/18, retry 82/16"):
        for ec in M1_EXIT_CELL_LIST:
            key = f"tp{int(ec['tp_usd'])} sl{int(ec['sl_usd'])}"
            sel = {c["label"]: c for c in cells
                   if c["level_set"] == ls_label
                   and c["tp_usd"] == ec["tp_usd"]
                   and c["sl_usd"] == ec["sl_usd"]}
            cross = sel.get(f"rsi {ls_label} | crossing | {key}")
            levt = sel.get(f"rsi {ls_label} | level test | {key}")
            if cross is None or levt is None:
                continue
            d_net = levt["net_total"] - cross["net_total"]
            print(f"  {ls_label:<28} {key:<11}  n {cross['n']:>5,} -> "
                  f"{levt['n']:>5,}   net ${cross['net_total']:>9,.2f} -> "
                  f"${levt['net_total']:>9,.2f}  (delta ${d_net:+,.2f})   "
                  f"g/lg {cross['gross_per_leg']:+.4f} -> "
                  f"{levt['gross_per_leg']:+.4f}")

    # ── PER-MONTH for the new level set ───────────────────────────
    for c in cells:
        if c["level_set"] != "levels 82/18" or c["trigger"] != "crossing":
            continue
        if c["tp_usd"] != M1_EXIT_CELL_LIST[0]["tp_usd"]:
            continue
        print()
        print("=" * 150)
        print(f"PER-MONTH (the month is a REPORTING dimension) - {c['label']}")
        print("=" * 150)
        print(f"{'month':<9} {'n':>5} {'gross':>10} {'fees':>9} {'net':>11} "
              f"{'g/lg':>9} {'hit':>7}")
        for m in c["months"]:
            print(f"{m['month']:<9} {m['n']:>5,} {m['gross']:>10,.2f} "
                  f"{m['fees']:>9,.2f} {m['net']:>11,.2f} "
                  f"{m['gross_per_leg']:>9.4f} {m['hit_rate']:>7.3f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(_clean(payload), indent=2, default=str), encoding="utf-8")
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
