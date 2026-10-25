"""ENZO STRATEGY BACKTEST — the driver.

Runs the two rules from ``Enzo input.pdf`` on the canonical XAUUSD 1s
corpus, aggregates to 1-minute bars, executes CONTINUOUSLY, and scores
every leg.

USAGE
-----
    # the canonical one-month screen (~6 s)
    python -m runtime.diagnostics.enzo_backtest

    # a specific window
    python -m runtime.diagnostics.enzo_backtest \\
        --start 2025-10-01 --end 2025-11-01

    # the full corpus (needs the approval gate)
    python -m runtime.diagnostics.enzo_backtest \\
        --start 2024-01-01 --end 2026-07-16 --i-know

    # a different EMA triple
    python -m runtime.diagnostics.enzo_backtest --ema 8,21,55

WHAT IT REPORTS
---------------
Per cell (one exit geometry for Rule 1, one EMA triple for Rule 2):

* legs, gross, fees, net, gross/leg, fee/leg, gross/leg ÷ fee/leg
* the payoff geometry: hit rate, payoff, break-even hit rate, edge_pp
* the LONG and SHORT splits
* the drift / excess decomposition (is the gross just market beta?)
* the EXCHANGEABLE placebo: the real arm vs 100 coin-flip arms on the
  SAME schedule, with the empirical p-value
* a per-month table (the month is a REPORTING dimension, never an
  execution boundary)

The repo's rules are respected:
* one CONTINUOUS execution over the window (never month chunks)
* the leg is the unit of evaluation
* the canonical fee model, scaled by the fill price
* a PESSIMISTIC fill (the stop wins an ambiguous bar; a gap fills at
  the open)
* a control that is EXCHANGEABLE with the thing it controls for
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

from runtime.diagnostics.enzo_core import (  # noqa: E402
    H4_CROSS_CANCELS,
    H4_EMA_PERIOD_GRID,
    H4_EMA_PERIODS,
    H4_EMA_SOURCES,
    H4_LIMIT_OFFSET_USD,
    H4_REPRICE,
    H4_SESSION_OFFSET_H,
    H4_SL_USD,
    H4_TP_USD,
    M1_EXIT_CELL_LIST,
    EnzoLeg,
    build_enzo_bars,
    drift_excess,
    randomize_sides,
    run_h4_ema_limit,
    run_m1_rsi,
    score_legs,
    set_fee_spot,
    wilder_rsi,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars  # noqa: E402
from runtime.sweeps.cost_estimate import (  # noqa: E402
    DEFAULT_END,
    DEFAULT_START,
    estimate_sweep,
)

REPORT_DIR = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
REPORT_PATH = REPORT_DIR / "enzo_backtest.json"


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


def _fmt(x: Any, spec: str = "9.4f") -> str:
    if x is None:
        return "n/a"
    try:
        return format(float(x), spec)
    except (TypeError, ValueError):
        return str(x)


def placebo(bars, legs: list[EnzoLeg], n_random: int, seed: int
            ) -> dict[str, Any]:
    """The EXCHANGEABLE control: same schedule, random side.

    The entry bar, entry price, exit bar, exit price, time in market and
    fee are held FIXED; only the side changes. The empirical p-value is
    how often a coin flip beat the real arm.
    """
    if not legs:
        return {"n_random": n_random, "p_net_vs_random": 1.0,
                "p_gross_vs_random": 1.0,
                "random_net_mean": 0.0, "random_net_sd": 0.0,
                "random_gross_mean": 0.0, "random_gross_sd": 0.0,
                "net_excess": 0.0, "gross_excess": 0.0}
    real = score_legs(legs)
    nets = []
    grosses = []
    for k in range(n_random):
        arm = score_legs(randomize_sides(legs, seed + 1000 * (k + 1)))
        nets.append(arm["net_total"])
        grosses.append(arm["gross_total"])
    nets_a = np.array(nets)
    grosses_a = np.array(grosses)
    p_net = (1.0 + float((nets_a >= real["net_total"]).sum())) / (n_random + 1.0)
    p_gross = (1.0 + float((grosses_a >= real["gross_total"]).sum())) \
        / (n_random + 1.0)
    # The PAIRED test. Each random arm is a sign flip of the SAME leg
    # schedule, so the arms are not independent samples -- they are
    # paired observations of the same leg. The paired t-statistic on
    # (real - random) is therefore the correct statistic, and it is
    # MORE powerful than the empirical p-value because it uses the
    # within-leg correlation.
    d_gross = np.array([real["gross_total"] - g for g in grosses])
    d_net = np.array([real["net_total"] - n for n in nets])
    t_gross = (float(d_gross.mean()) / (float(d_gross.std(ddof=1))
                                        / math.sqrt(n_random))
               if n_random > 1 and d_gross.std(ddof=1) > 0 else 0.0)
    t_net = (float(d_net.mean()) / (float(d_net.std(ddof=1))
                                    / math.sqrt(n_random))
             if n_random > 1 and d_net.std(ddof=1) > 0 else 0.0)
    return {
        "n_random": n_random,
        "p_net_vs_random": float(p_net),
        "p_gross_vs_random": float(p_gross),
        "t_gross_paired": t_gross,
        "t_net_paired": t_net,
        "n_random_beating_real": int((grosses_a >= real["gross_total"]).sum()),
        "random_net_mean": float(nets_a.mean()),
        "random_net_sd": float(nets_a.std(ddof=1)) if n_random > 1 else 0.0,
        "random_gross_mean": float(grosses_a.mean()),
        "random_gross_sd": float(grosses_a.std(ddof=1)) if n_random > 1 else 0.0,
        "net_excess": float(real["net_total"] - nets_a.mean()),
        "gross_excess": float(real["gross_total"] - grosses_a.mean()),
    }


def month_table(legs: list[EnzoLeg]) -> list[dict[str, Any]]:
    """The month as a REPORTING dimension (never an execution boundary)."""
    if not legs:
        return []
    df = pd.DataFrame({
        "t": pd.to_datetime([lg.exit_time_ns for lg in legs], unit="ns",
                            utc=True),
        "gross": [lg.gross_usd for lg in legs],
        "fee": [lg.fee_usd for lg in legs],
        "net": [lg.net_usd for lg in legs],
        "side": [lg.side for lg in legs],
    })
    df["month"] = df["t"].dt.strftime("%Y-%m")
    out = []
    for m, g in df.groupby("month"):
        out.append({
            "month": m,
            "n": int(len(g)),
            "gross": float(g["gross"].sum()),
            "fees": float(g["fee"].sum()),
            "net": float(g["net"].sum()),
            "gross_per_leg": float(g["gross"].mean()),
            "hit_rate": float((g["gross"] > 0).mean()),
            "n_long": int((g["side"] > 0).sum()),
            "n_short": int((g["side"] < 0).sum()),
        })
    return out


def inverted_call_legs(bars, *, fast_n: int, mid_n: int, slow_n: int,
                       offset_usd: float = H4_LIMIT_OFFSET_USD,
                       tp_usd: float = H4_TP_USD, sl_usd: float = H4_SL_USD,
                       ) -> list[EnzoLeg]:
    """Rule 2 with the DIRECTION CALL inverted.

    This is the EXCHANGEABLE control for Rule 2. The sign-flip placebo
    is not: a SELL LIMIT at ``yellow + 13`` fills when price RISES to
    it, so flipping the leg's side turns it into a different order with
    a different fill condition (AGENTS.md § 2b's lesson).

    The inversion flips the sign of ``(blue - red)`` and changes
    NOTHING else: the same two EMAs, the same sources, the same yellow
    line, the same offset, the same resting order, the same fill model,
    the same TP/SL, the same fee model and the same arming schedule.
    """
    import runtime.diagnostics.enzo_core as ec

    orig = ec.h4_trend_state

    def flipped(bars_, f, m, s, fs="close", ms="open", ss="close"):
        st = orig(bars_, f, m, s, fs, ms, ss)
        st["trend"] = -st["trend"]
        # The cross is SYMMETRIC under a sign flip, so the INVALID
        # marker is unchanged.
        return st

    ec.h4_trend_state = flipped
    try:
        return run_h4_ema_limit(
            bars, fast_n=fast_n, mid_n=mid_n, slow_n=slow_n,
            fast_src=H4_EMA_SOURCES[0], mid_src=H4_EMA_SOURCES[1],
            slow_src=H4_EMA_SOURCES[2],
            offset_usd=offset_usd, tp_usd=tp_usd, sl_usd=sl_usd,
            cross_cancels=H4_CROSS_CANCELS, reprice=H4_REPRICE)
    finally:
        ec.h4_trend_state = orig


def evaluate_cell(name: str, legs: list[EnzoLeg], bars, n_random: int,
                  seed: int) -> dict[str, Any]:
    """The full scorecard for one cell."""
    s = score_legs(legs)
    de = drift_excess(legs, bars)
    s.update(de)
    pl = placebo(bars, legs, n_random, seed)
    return {
        "label": name,
        "n": s["n"],
        "gross_total": s["gross_total"],
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
        "drift_total": s["drift_total"],
        "excess_total": s["excess_total"],
        "excess_per_leg": s["excess_per_leg"],
        "t_excess": s["t_excess"],
        "exits": s["exits"],
        "sides": s["sides"],
        "placebo": pl,
        "directionally_valid": bool(pl["p_gross_vs_random"] < 0.05
                                    and pl["gross_excess"] > 0.0),
        "months": month_table(legs),
    }


#: A cell with fewer legs than this cannot be judged. The canonical
#: Rule 2 cell has 124 legs on the full corpus but only 1-2 on a
#: one-month window, and a verdict on 1 leg is noise.
MIN_LEGS_FOR_VERDICT = 30


def _ctrl_ok(cell: dict[str, Any]) -> bool:
    """Did the direction call beat its EXCHANGEABLE control?

    Rule 2 (h4) enters with a RESTING LIMIT order, so the exchangeable
    control is the INVERTED CALL — flipping the leg's side would turn
    a SELL LIMIT at ``yellow + 13`` into a BUY LIMIT at the same price,
    which is a different order with a different fill condition (the
    repo's § 2b lesson).

    Rule 1 (m1) enters at the MARKET on the next bar's open, so a
    sign flip IS exchangeable there and the placebo is the right test.
    """
    inv = cell.get("inverted_call")
    if inv is not None:
        return bool(inv["beats_inverted"])
    return bool(cell["directionally_valid"])


def print_table(cells: list[dict[str, Any]]) -> None:
    print()
    print("=" * 132)
    print("ENZO STRATEGY - LEG-LEVEL SCOREBOARD")
    print("=" * 132)
    print(f"{'cell':<26} {'n':>6} {'gross':>11} {'fees':>10} {'net':>11} "
          f"{'g/lg':>8} {'fee/lg':>8} {'x fee':>7} {'hit':>6} {'edge_pp':>8} "
          f"{'exc':>10} {'t_exc':>7} {'drift%':>7} {'ctrl':>6}")
    print("-" * 132)
    for c in cells:
        # The VERDICT flag. For Rule 2 the correct control is the
        # INVERTED CALL (the sign-flip placebo is not exchangeable with
        # a resting limit order). For Rule 1 the sign-flip placebo IS
        # the right control, because Rule 1 enters at the market on the
        # next bar's open — there is no resting order whose fill
        # condition a sign flip would change.
        inv = c.get("inverted_call")
        if inv is not None:
            flag = "OK " if inv["beats_inverted"] else "INV"
            ctrl = "inv" if inv["beats_inverted"] else "LOSE"
            dshare = (c["drift_total"] / c["gross_total"]
                      if abs(c["gross_total"]) > 1e-9 else float("nan"))
        else:
            flag = "OK " if c["directionally_valid"] else "INV"
            ctrl = f"{c['placebo']['p_gross_vs_random']:.4f}"
            dshare = (c["drift_total"] / c["gross_total"]
                      if abs(c["gross_total"]) > 1e-9 else float("nan"))
        print(f"{flag} {c['label'][:23]:<23} {c['n']:>6} "
              f"{c['gross_total']:>11,.2f} {c['fees_total']:>10,.2f} "
              f"{c['net_total']:>11,.2f} {c['gross_per_leg']:>8.4f} "
              f"{c['fee_per_leg']:>8.4f} {c['gross_per_leg_over_fee']:>7.2f} "
              f"{c['hit_rate']:>6.3f} {c['edge_pp']:>8.2f} "
              f"{c['excess_total']:>10,.2f} {c['t_excess']:>7.2f} "
              f"{dshare:>6.1%} {ctrl:>6}")
    print("-" * 132)
    print("OK  = the direction call beats its EXCHANGEABLE control")
    print("     Rule 2 (h4): the control is the INVERTED CALL — a SELL")
    print("       LIMIT at yellow+13 and a BUY LIMIT at the same price")
    print("       are DIFFERENT orders, so the sign-flip placebo is not")
    print("       exchangeable there and its p-value is not the verdict.")
    print("     Rule 1 (m1): the control is the sign-flip placebo")
    print("       (the entry is a market fill, so a sign flip is fair).")
    print("INV = the direction call is NOT distinguishable from its "
          "control")
    print("drift% = the drift's share of the gross. A value near or above")
    print("        100% means the realised P&L IS the market's drift.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--ema", type=str, default="",
                    help="One EMA triple 'fast,mid,slow'. Default: the "
                         "whole grid.")
    ap.add_argument("--n-random", type=int, default=100)
    ap.add_argument("--seed", type=int, default=11)
    ap.add_argument("--warmup-days", type=int, default=14,
                    help="Warmup prefix in days. Executed but its legs are "
                         "DISCARDED, so every scored leg has warm EMA / "
                         "RSI state. 14 days covers the longest lookback "
                         "here: the H4 EMA(60) needs 10 days of H4 bars.")
    ap.add_argument("--json-only", action="store_true")
    ap.add_argument("--i-know", action="store_true",
                    help="APPROVAL GATE: a window longer than one month "
                         "prints its cost estimate and exits without "
                         "running unless this flag is passed.")
    args = ap.parse_args()

    if not args.data.exists():
        raise SystemExit(f"data file not found: {args.data}")

    est = estimate_sweep(args.data, args.start, args.end, n_cells=1)
    print("== enzo backtest ==")
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
    # The warmup prefix is EXECUTED but its legs are DISCARDED. It
    # gives every scored leg warm EMA / RSI state, so the window's
    # first bars are not scored against a cold indicator. This is the
    # repo's continuous-execution rule (AGENTS.md § "Chunked vs
    # continuous"): the month is a REPORTING dimension, never an
    # execution boundary.
    t0 = time.perf_counter()
    load_start = args.start
    if args.warmup_days > 0:
        load_start = (pd.Timestamp(args.start, tz="UTC")
                      - pd.Timedelta(days=args.warmup_days)).strftime(
                          "%Y-%m-%d")
    bars_1s = load_bars(args.data, load_start, args.end)
    print(f"loaded {len(bars_1s):,} 1s bars in {time.perf_counter() - t0:.1f}s"
          f"   (warmup from {load_start})", flush=True)

    t1 = time.perf_counter()
    b = build_enzo_bars(bars_1s, h4_offset_h=H4_SESSION_OFFSET_H)
    spot = float(np.median(b.close))
    set_fee_spot(spot)
    rsi = wilder_rsi(b.close)
    build_secs = time.perf_counter() - t1
    print(f"built {len(b):,} 1m bars + {len(b.h4_close):,} H4 bars in "
          f"{build_secs:.1f}s   spot=${spot:,.2f}   "
          f"RSI>=84 on {int((rsi >= 84).sum()):,} bars, "
          f"RSI<=16 on {int((rsi <= 16).sum()):,} bars", flush=True)
    if len(b) < 100:
        raise SystemExit("not enough 1m bars in the window")

    # The scored window boundary (a 1m bar timestamp).
    score_from_ns = int(pd.Timestamp(args.start, tz="UTC").value)
    score_to_ns = int(pd.Timestamp(args.end, tz="UTC").value)

    def _in_window(legs: list[EnzoLeg]) -> list[EnzoLeg]:
        """Keep only the legs whose EXIT falls in the scored window."""
        return [lg for lg in legs
                if score_from_ns <= lg.exit_time_ns < score_to_ns]

    # ── CELLS ─────────────────────────────────────────────────────
    cells: list[dict[str, Any]] = []
    t2 = time.perf_counter()

    for i, ec in enumerate(M1_EXIT_CELL_LIST):
        legs = _in_window(run_m1_rsi(b, rsi, tp_usd=ec["tp_usd"],
                                     sl_usd=ec["sl_usd"]))
        name = f"m1_rsi tp{ec['tp_usd']:.0f} sl{ec['sl_usd']:.0f}"
        cells.append(evaluate_cell(name, legs, b, args.n_random,
                                    args.seed + i * 977))
        print(f"  [{i + 1}] {name}: {len(legs):,} legs "
              f"({time.perf_counter() - t2:.1f}s)", flush=True)

    if args.ema:
        grid = [tuple(int(x) for x in args.ema.split(","))]
    else:
        # The canonical triple FIRST, then the sensitivity grid. The
        # canonical cell is the answer; the grid is context.
        grid = [H4_EMA_PERIODS] + [g for g in H4_EMA_PERIOD_GRID
                                   if g != H4_EMA_PERIODS]
    for j, (f_, m_, s_) in enumerate(grid):
        funnel: dict[str, Any] = {}
        legs = _in_window(run_h4_ema_limit(
            b, fast_n=f_, mid_n=m_, slow_n=s_,
            fast_src=H4_EMA_SOURCES[0],
            mid_src=H4_EMA_SOURCES[1],
            slow_src=H4_EMA_SOURCES[2],
            cross_cancels=H4_CROSS_CANCELS, reprice=H4_REPRICE,
            stats=funnel))
        name = f"h4 ema {f_}/{m_}/{s_}"
        cells.append(evaluate_cell(name, legs, b, args.n_random,
                                    args.seed + 100 * (j + 1)))
        cells[-1]["funnel"] = dict(funnel)

        # The EXCHANGEABLE control for Rule 2: the inverted call.
        # The sign-flip placebo (in `placebo`) is NOT exchangeable for
        # Rule 2 — it measures the order type, not the trend call.
        inv_legs = _in_window(inverted_call_legs(
            b, fast_n=f_, mid_n=m_, slow_n=s_))
        inv_s = score_legs(inv_legs)
        cells[-1]["inverted_call"] = {
            "n": inv_s["n"],
            "gross_total": inv_s["gross_total"],
            "net_total": inv_s["net_total"],
            "gross_per_leg": inv_s["gross_per_leg"],
            "hit_rate": inv_s["hit_rate"],
            "gross_per_leg_advantage": (cells[-1]["gross_per_leg"]
                                        - inv_s["gross_per_leg"]),
            "net_advantage": (cells[-1]["net_total"]
                              - inv_s["net_total"]),
            "beats_inverted": bool(
                cells[-1]["gross_per_leg"] > inv_s["gross_per_leg"]),
        }
        print(f"  [{j + 1}] {name}: {len(legs):,} legs "
              f"({time.perf_counter() - t2:.1f}s)   funnel={funnel}",
              flush=True)
        print(f"        inverted-call control: "
              f"{inv_s['n']:,} legs, gross/leg "
              f"{inv_s['gross_per_leg']:+.4f} vs real "
              f"{cells[-1]['gross_per_leg']:+.4f}  ->  "
              f"{'BEATS' if cells[-1]['inverted_call']['beats_inverted'] else 'LOSES TO'}"
              f" the inverted call", flush=True)

    # ── REPORT ────────────────────────────────────────────────────
    payload = {
        "ts_utc": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": str(args.data),
        "window": {"start": args.start, "end": args.end},
        "warmup_days": args.warmup_days,
        "warmup_start": load_start,
        "ema_sources": {"fast": H4_EMA_SOURCES[0],
                        "mid": H4_EMA_SOURCES[1],
                        "slow": H4_EMA_SOURCES[2]},
        "n_1s_bars": int(len(bars_1s)),
        "n_1m_bars": int(len(b)),
        "n_h4_bars": int(len(b.h4_close)),
        "spot_median_usd": spot,
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

    # ── VERDICT ───────────────────────────────────────────────────
    print()
    print("=" * 132)
    print("VERDICT")
    print("=" * 132)
    if not cells:
        print("no cells measured")
        return
    valid = [c for c in cells if _ctrl_ok(c)]
    print(f"cells measured               : {len(cells)}")
    print(f"cells with a valid direction : {len(valid)}")
    print()

    # The fee reality check - a gross edge that does not cover the
    # round trip is NOT an edge.
    covering = [c for c in cells
                if c["n"] > 0 and c["gross_per_leg_over_fee"] >= 1.0]
    print(f"cells whose gross/leg covers the measured round trip: "
          f"{len(covering)}/{len(cells)}")
    for c in cells:
        if c["n"] == 0:
            continue
        verdict = []
        if c["n"] < MIN_LEGS_FOR_VERDICT:
            verdict.append(f"TOO FEW LEGS to judge (n={c['n']} < "
                           f"{MIN_LEGS_FOR_VERDICT})")
        elif not _ctrl_ok(c):
            if c.get("inverted_call") is not None:
                verdict.append("NO DIRECTIONAL SKILL (loses to the "
                               "inverted-call control)")
            else:
                verdict.append("NO DIRECTIONAL SKILL (indistinguishable "
                               "from a coin flip)")
        if c["gross_per_leg_over_fee"] < 1.0:
            verdict.append("gross does NOT cover the round trip")
        dshare = (c["drift_total"] / c["gross_total"]
                  if abs(c["gross_total"]) > 1e-9 else float("nan"))
        if c["t_excess"] < -2.0:
            verdict.append("the gross IS market beta (excess t<-2)")
        elif c["t_excess"] > 2.0:
            verdict.append("timing is not beta (excess t>2)")
        elif abs(dshare) > 0.95 and dshare > 0:
            verdict.append(f"the gross is {dshare:.0%} MARKET DRIFT")
        print(f"  {c['label']:<26} net ${c['net_total']:>10,.2f}   "
              f"{'; '.join(verdict) if verdict else 'gross edge covers fees'}")

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_PATH.write_text(
        json.dumps(_clean(payload), indent=2, default=str), encoding="utf-8")
    print()
    print(f"-> {REPORT_PATH}")


if __name__ == "__main__":
    main()
