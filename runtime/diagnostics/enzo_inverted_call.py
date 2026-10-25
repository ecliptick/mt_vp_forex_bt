"""ENZO — the INVERTED-CALL CONTROL for Rule 2 (the decisive test).

WHY THIS CONTROL EXISTS
=======================

The sign-flip placebo (randomise each leg's side, holding the entry
bar, entry price, exit bar and exit price fixed) is **not exchangeable**
with Rule 2, for exactly the reason AGENTS.md § 2b documents for the
sniper recipe:

* a SELL LIMIT at ``yellow + 13`` fills when price **rises** to it;
* the sign-flipped "BUY LIMIT at the same price" is a **completely
  different order** with a different fill condition and a different
  price path.

So the sign-flip placebo measures the ORDER TYPE, not the trend call.
Its permutation p-value on the canonical cell is **0.061-0.086** — which
is the honest number for a control that does not measure what it
claims to.

THE CONTROL
===========

This control keeps **every** ingredient and destroys exactly **one**:
the DIRECTION CALL.

* the same detector, the same resting order, the same offset from the
  same yellow line, the same fill model, the same TP/SL, the same fee
  model, the same bar loop, the same schedule of arming times;
* the only change: the sign of ``(blue - red)`` is flipped, so a
  downtrend now BUYS at ``yellow - 13`` and an uptrend now SELLS at
  ``yellow + 13``.

If the real call beats the inverted call, the trend call carries
information that the level/geometry does not.

THE SECOND FINDING — THE DRIFT/EXCESS IDENTITY
==============================================

The canonical cell's ``excess`` is **negative** (t = -0.17) and its
``drift`` is **102.2%** of its gross: the realised P&L is market drift,
not skill. Gold rose **+96.7%** over the corpus, so a passive long hold
would have made **+$1,995.81** on 1 oz while Rule 2 made **+$365.25**
net on 124 trades. Rule 2 is a *levered, hedged, low-frequency* way of
capturing some of that drift — with a much better risk profile than
holding, and a much worse return.

Reproduce:

    python -m runtime.diagnostics.enzo_inverted_call
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from runtime.diagnostics.enzo_core import (
    H4_CROSS_CANCELS,
    H4_EMA_SOURCES,
    H4_REPRICE,
    H4_SESSION_OFFSET_H,
    build_enzo_bars,
    drift_excess,
    run_h4_ema_limit,
    score_legs,
    set_fee_spot,
)
from runtime.sweeps.cached_sweep import load_bars

DEFAULT_DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
DEFAULT_START = "2024-01-01"
DEFAULT_END = "2026-07-16"
WARMUP_DAYS = 14
REPORT = Path(__file__).with_name("_reports") / "enzo_inverted_call.json"


def run_inverted(b, *, fast_n: int, mid_n: int, slow_n: int,
                 offset_usd: float, tp_usd: float, sl_usd: float):
    """Run the engine with the DIRECTION CALL inverted.

    The inversion flips the sign of ``(blue - red)`` and NOTHING else:
    the same two EMAs, the same sources, the same yellow line, the
    same offset, the same resting order, the same TP/SL, the same fee
    model and the same schedule of arming times.
    """
    import runtime.diagnostics.enzo_core as ec

    orig = ec.h4_trend_state

    def flipped(bars_, f, m, s, fs="close", ms="open", ss="close"):
        st = orig(bars_, f, m, s, fs, ms, ss)
        st["trend"] = -st["trend"]
        # the cross is SYMMETRIC: a sign flip does not change where the
        # two lines cross, so the INVALID marker is unchanged.
        return st

    ec.h4_trend_state = flipped
    try:
        return run_h4_ema_limit(
            b, fast_n=fast_n, mid_n=mid_n, slow_n=slow_n,
            fast_src=H4_EMA_SOURCES[0], mid_src=H4_EMA_SOURCES[1],
            slow_src=H4_EMA_SOURCES[2],
            offset_usd=offset_usd, tp_usd=tp_usd, sl_usd=sl_usd,
            cross_cancels=H4_CROSS_CANCELS, reprice=H4_REPRICE)
    finally:
        ec.h4_trend_state = orig


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--i-know", action="store_true")
    args = ap.parse_args()

    if not args.data.exists():
        raise SystemExit(f"data file not found: {args.data}")
    if args.start != DEFAULT_START:
        print("NOTE: this diagnostic is defined on the full corpus; the "
              "window changes the numbers.")

    t0 = time.perf_counter()
    load_start = (pd.Timestamp(args.start, tz="UTC")
                  - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    bars = load_bars(args.data, load_start, args.end)
    print(f"loaded {len(bars):,} 1s bars in {time.perf_counter() - t0:.1f}s",
          flush=True)

    b = build_enzo_bars(bars, h4_offset_h=H4_SESSION_OFFSET_H)
    set_fee_spot(float(np.median(b.close)))
    s_from = int(pd.Timestamp(args.start, tz="UTC").value)
    s_to = int(pd.Timestamp(args.end, tz="UTC").value)

    def _win(legs):
        return [lg for lg in legs if s_from <= lg.exit_time_ns < s_to]

    real_legs = _win(run_h4_ema_limit(
        b, fast_n=10, mid_n=20, slow_n=60,
        fast_src=H4_EMA_SOURCES[0], mid_src=H4_EMA_SOURCES[1],
        slow_src=H4_EMA_SOURCES[2], tp_usd=25.0, sl_usd=25.0,
        cross_cancels=H4_CROSS_CANCELS, reprice=H4_REPRICE))
    inv_legs = _win(run_inverted(b, fast_n=10, mid_n=20, slow_n=60,
                                 offset_usd=13.0, tp_usd=25.0, sl_usd=25.0))

    rs = score_legs(real_legs)
    rde = drift_excess(real_legs, b)
    is_ = score_legs(inv_legs)
    ide = drift_excess(inv_legs, b)

    # the market's own drift over the same window
    idx = int(np.searchsorted(b.time_ns, s_to)) - 1
    i0 = int(np.searchsorted(b.time_ns, s_from))
    gold_ret = float(b.close[idx] / b.close[i0] - 1.0)

    print()
    print("=" * 104)
    print("ENZO RULE 2 — THE INVERTED-CALL CONTROL")
    print("=" * 104)
    print(f"{'arm':<26} {'n':>6} {'gross':>11} {'fees':>9} {'net':>11} "
          f"{'g/lg':>8} {'x fee':>7} {'hit':>6} {'drift':>10} {'excess':>9}")
    for label, s, de in (("real call", rs, rde), ("INVERTED call", is_, ide)):
        xf = s["gross_per_leg"] / s["fee_per_leg"] if s["fee_per_leg"] else 0.0
        print(f"{label:<26} {s['n']:>6,} {s['gross_total']:>11,.2f} "
              f"{s['fees_total']:>9,.2f} {s['net_total']:>11,.2f} "
              f"{s['gross_per_leg']:>8.4f} {xf:>7.2f} {s['hit_rate']:>6.3f} "
              f"{de['drift_total']:>10,.2f} {de['excess_total']:>9,.2f}")

    d_gross = rs["gross_per_leg"] - is_["gross_per_leg"]
    print(f"\n  the real call's gross/leg exceeds the inverted call's by "
          f"${d_gross:+.4f}/leg")
    print(f"  the real call's NET exceeds the inverted call's by "
          f"${rs['net_total'] - is_['net_total']:+,.2f}")

    print("\n  the drift/excess identity on the REAL arm:")
    share = (rde["drift_total"] / rs["gross_total"]
             if abs(rs["gross_total"]) > 1e-9 else float("nan"))
    print(f"    gross  = ${rs['gross_total']:,.2f}")
    print(f"    drift  = ${rde['drift_total']:,.2f}   ({share:.1%} of gross)")
    print(f"    excess = ${rde['excess_total']:,.2f}   (t = {rde['t_excess']:+.2f})")
    print("    => the realised P&L is MARKET DRIFT, not timing skill.")

    print(f"\n  the market over the same window: "
          f"${b.close[i0]:,.2f} -> ${b.close[idx]:,.2f}  ({gold_ret:+.1%})")
    print(f"  a passive LONG hold of 1 oz: "
          f"${b.close[idx] - b.close[i0]:,.2f}")
    print(f"  Rule 2's net: ${rs['net_total']:,.2f} on {rs['n']} trades")

    # the permutation placebo, for the record (and its caveat)
    g = np.array([lg.gross_usd for lg in real_legs])
    rng = np.random.default_rng(11)
    signs = rng.choice([-1.0, 1.0], size=(20000, g.size))
    sums = signs @ g
    p_perm = (1.0 + float((sums >= rs["gross_total"]).sum())) / 20001.0
    print(f"\n  the sign-flip permutation p-value (NOT exchangeable — it "
          f"measures the order type): {p_perm:.4f}")
    print("  => the honest read is the INVERTED-CALL comparison above, "
          "not this p-value.")

    payload: dict[str, Any] = {
        "ts_utc": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "window": {"start": args.start, "end": args.end},
        "real": {**{k: v for k, v in rs.items() if not isinstance(v, dict)},
                 **{f"de_{k}": v for k, v in rde.items()}},
        "inverted": {**{k: v for k, v in is_.items() if not isinstance(v, dict)},
                     **{f"de_{k}": v for k, v in ide.items()}},
        "gold_return": gold_ret,
        "passive_long_hold_usd": float(b.close[idx] - b.close[i0]),
        "permutation_p_signflip": float(p_perm),
        "permutation_caveat": "the sign-flip placebo is NOT exchangeable "
                              "with Rule 2: a SELL LIMIT at yellow+13 and "
                              "a BUY LIMIT at the same price are different "
                              "orders. Use the inverted-call arm.",
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, indent=2, default=str))
    print(f"\n-> {REPORT}")


if __name__ == "__main__":
    main()
