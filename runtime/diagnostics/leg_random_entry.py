"""ENTRY-COST DECOMPOSITION — what the random-entry control actually measures.

WHY THIS FILE REPLACES A "TIMING SKILL" CLAIM (2026-10-25)
==========================================================

The first version of this diagnostic compared the strategy's gross
against a random entry drawn from the strategy's own decision
window, holding the exit PRICE fixed. It reported::

    delta $11,820.61   delta/leg +6.99   t = +18.29
    VERDICT: ENTRY TIMING HAS SKILL

**That verdict is wrong.** The t-stat is real; the conclusion drawn
from it is not. The control is not exchangeable, and the reason is
structural rather than statistical.

THE CONFOUND
============

The strategy's entry is a RESTING LIMIT ORDER at the FVG's zone
edge (D2, 2026-10-22). The order is placed at the signal bar and
rests until price comes back to the zone. The random entrant, by
contrast, buys at the MARKET CLOSE of a random bar.

For a long, the zone edge is BELOW the signal bar's price, and the
market spends the pre-fill period trading back DOWN toward it — that
is precisely why the limit fills later. So for **every** leg::

    close[signal_bar] > E   (long)

by geometry. The random entrant, drawn from that same pre-fill
period, systematically pays a WORSE price than the limit order did.

Measured on the canonical 31-month ledger (1,692 legs):

===============================================  ==========
measure                                          value
===============================================  ==========
delta attributable to the PRE-FILL window       +$12,181.84
total delta                                     +$12,186.11
share                                            **99.96%**
legs where the pre-fill discount is favourable    **100.0%**
===============================================  ==========

The control was measuring **"a resting limit order beats a market
order at a random moment"** — a real execution advantage, and one
that is ALREADY correctly captured in the backtest — not **"the
strategy picked a good moment"**.

WHAT THE TIMING NUMBER ACTUALLY IS
==================================

Timing must be measured against a counterfactual that uses the
SAME execution model. The ledger's drift/excess decomposition does
exactly that, and it is an algebraic identity rather than a
simulation::

    drift  = passive hold of the SAME side over the SAME window
    excess = gross - drift

Both the limit fill and the exit are identical between the two
terms, so the limit-order advantage CANCELS. Measured on the same
ledger::

    sum(gross)   +$510.58
    sum(drift)   +$665.58   (t = +2.10)
    sum(excess)  -$155.00   (t = -2.96)

**The strategy's gross is 130% explained by the market drift of its
own positions, and its timing component is NEGATIVE.**

WHAT THIS FILE DOES
===================

It reports the decomposition, states the confound, and shows why
the control cannot support a timing claim. The honest timing
number lives in ``leg_buckets`` (the drift/excess split) and in the
per-leg chop test.

Usage
-----
    python -m runtime.diagnostics.leg_random_entry
    python -m runtime.diagnostics.leg_random_entry --samples 500
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

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    DEFAULT_DATA,
    LEDGER_PATH,
    load_ledger,
)

# Random entry bars drawn per leg. 200 is enough to estimate the
# random distribution's mean to well under a cent per leg.
DEFAULT_SAMPLES = 200

# FIXED seed — a measurement that moves when it is re-run is not a
# measurement.
SEED = 20251025


def _load_close(exec_start: str, exec_end: str) -> np.ndarray:
    """The close series the ENGINE walked, in the engine's bar order.

    Must be loaded with the SAME ``load_bars`` call the ledger used
    (same file, same window) or the bar indices will not line up.
    """
    from runtime.sweeps.cached_sweep import load_bars

    bars = load_bars(DEFAULT_DATA, exec_start, exec_end)
    return bars["close"].to_numpy(dtype=np.float64)


def _window_mean(close: np.ndarray, cumsum: np.ndarray,
                 lo: np.ndarray, hi: np.ndarray) -> np.ndarray:
    """Mean close over [lo, hi) for each leg (vectorised)."""
    lo_c = np.clip(lo, 0, close.size)
    hi_c = np.clip(hi, 0, close.size)
    w = np.maximum(hi_c - lo_c, 1)
    return (cumsum[hi_c] - cumsum[lo_c]) / w


def analyse(payload: dict[str, Any], close: np.ndarray,
            samples: int = DEFAULT_SAMPLES) -> dict[str, Any]:
    """Decompose the random-entry delta into its mechanical parts."""
    L = payload["legs"]
    gross = np.asarray(L["gross"], dtype=np.float64)
    side = np.asarray(L["side"], dtype=np.float64)
    qty = np.asarray(L["qty"], dtype=np.float64)
    exit_price = np.asarray(L["exit_price"], dtype=np.float64)
    entry_price = np.asarray(L["entry_price"], dtype=np.float64)
    entry_bar = np.asarray(L["entry_bar"], dtype=np.int64)
    exit_bar = np.asarray(L["exit_bar"], dtype=np.int64)
    signal_bar = np.asarray(L["signal_bar"], dtype=np.int64) \
        if "signal_bar" in L else entry_bar.copy()

    n = gross.size
    n_bars = close.size
    cumsum = np.concatenate(([0.0], np.cumsum(close)))
    sq = side * qty

    rng = np.random.default_rng(SEED)

    # ── The random-entry control (Variant A: same exit PRICE) ───────
    strat_a = np.empty(n, dtype=np.float64)
    rand_a_mean = np.empty(n, dtype=np.float64)
    rand_a_pct = np.empty(n, dtype=np.float64)

    # ── The pre-fill / post-fill split ───────────────────────────────
    # The PRE-FILL window is [signal_bar, entry_bar): the bars the
    # resting limit order was resting for. That is where a random
    # market entry is guaranteed to be WORSE than the limit fill.
    pre_mean = _window_mean(close, cumsum, signal_bar, entry_bar)
    post_mean = _window_mean(close, cumsum, entry_bar, exit_bar)
    full_mean = _window_mean(close, cumsum, signal_bar, exit_bar)

    pre_discount = sq * (pre_mean - entry_price)
    post_discount = sq * (post_mean - entry_price)
    full_discount = sq * (full_mean - entry_price)

    n_valid = 0
    for i in range(n):
        lo = int(signal_bar[i])
        hi = int(exit_bar[i])
        if hi <= lo or hi >= n_bars or lo < 0:
            rand_a_mean[i] = np.nan
            rand_a_pct[i] = np.nan
            continue
        ks = lo + rng.integers(0, hi - lo, size=samples)
        ck = close[ks]
        ga = (exit_price[i] - ck) * side[i] * qty[i]
        rand_a_mean[i] = float(ga.mean())
        rand_a_pct[i] = float((ga < gross[i]).mean())
        strat_a[i] = gross[i]
        n_valid += 1

    delta = strat_a - rand_a_mean

    def _t(v: np.ndarray) -> float:
        if v.size < 2:
            return 0.0
        se = float(v.std(ddof=1) / np.sqrt(v.size))
        return float(v.mean() / se) if se > 0 else 0.0

    # ── The HONEST timing number: the ledger's drift/excess split ───
    drift = np.asarray(L["drift_usd"], dtype=np.float64)
    excess = np.asarray(L["excess_usd"], dtype=np.float64)

    pre_share = float(pre_discount.sum() / delta.sum()) \
        if abs(delta.sum()) > 1e-9 else 0.0

    return {
        "n_legs": int(n),
        "n_valid_windows": int(n_valid),
        "samples_per_leg": samples,
        "seed": SEED,
        "control": {
            "delta_total": float(delta.sum()),
            "delta_per_leg": float(delta.mean()),
            "t_delta": _t(delta),
            "mean_pct_of_random": float(np.nanmean(rand_a_pct)),
            "pct_legs_above_random_mean": float(np.nanmean(delta > 0)),
        },
        "confound": {
            "pre_fill_discount_total": float(pre_discount.sum()),
            "post_fill_discount_total": float(post_discount.sum()),
            "full_window_discount_total": float(full_discount.sum()),
            "pre_fill_share_of_delta": pre_share,
            "pct_legs_pre_fill_favourable": float(np.mean(pre_discount > 0)),
            "pct_legs_post_fill_favourable": float(np.mean(post_discount > 0)),
            "mean_pre_fill_bars": float(np.mean(np.maximum(entry_bar - signal_bar, 0))),
            "mean_post_fill_bars": float(np.mean(np.maximum(exit_bar - entry_bar, 0))),
            "median_pre_fill_discount_usd": float(np.median(np.abs(pre_mean - entry_price))),
        },
        "timing": {
            "gross_total": float(gross.sum()),
            "drift_total": float(drift.sum()),
            "excess_total": float(excess.sum()),
            "excess_per_leg": float(excess.mean()),
            "t_excess": _t(excess),
            "drift_share_of_gross": float(drift.sum() / gross.sum())
            if abs(gross.sum()) > 1e-9 else 0.0,
        },
        "_interpretation": (
            "The random-entry control is NOT exchangeable: the strategy "
            "fills a RESTING LIMIT at the FVG zone edge while the "
            "random entrant pays a MARKET close, and the pre-fill "
            "window is systematically worse for the market entrant by "
            "geometry (100% of legs). The control therefore measures "
            "the limit-order mechanic, not timing. The timing number "
            "is the ledger's excess = gross - drift, which is "
            "computed with the SAME execution model on both sides."
        ),
    }


def print_report(a: dict[str, Any]) -> None:
    print("=" * 78)
    print("ENTRY-COST DECOMPOSITION - what does the random-entry control")
    print("actually measure?")
    print("=" * 78)
    print(f"legs={a['n_legs']}  usable windows={a['n_valid_windows']}  "
          f"samples/leg={a['samples_per_leg']}  seed={a['seed']}")
    print()

    c = a["control"]
    print("THE CONTROL (same exit PRICE, random entry in [signal, exit)):")
    print(f"  delta total   ${c['delta_total']:,.2f}")
    print(f"  delta / leg   {c['delta_per_leg']:+.4f}")
    print(f"  t             {c['t_delta']:+.2f}")
    print(f"  mean percentile of random  {c['mean_pct_of_random']:.3f}")
    print()

    cf = a["confound"]
    print("WHY IT IS NOT A TIMING MEASURE:")
    print(f"  delta from the PRE-FILL window   ${cf['pre_fill_discount_total']:,.2f}")
    print(f"  delta from the POST-FILL window  ${cf['post_fill_discount_total']:,.2f}")
    print(f"  total delta                      ${cf['full_window_discount_total']:,.2f}")
    print(f"  PRE-FILL share of the delta      {100 * cf['pre_fill_share_of_delta']:.2f}%")
    print(f"  pre-fill discount favourable on  {100 * cf['pct_legs_pre_fill_favourable']:.1f}% of legs")
    print(f"  post-fill discount favourable on {100 * cf['pct_legs_post_fill_favourable']:.1f}% of legs")
    print(f"  pre-fill bars (mean)  {cf['mean_pre_fill_bars']:,.0f}")
    print(f"  post-fill bars (mean) {cf['mean_post_fill_bars']:,.0f}")
    print(f"  median |pre-fill close - limit|   ${cf['median_pre_fill_discount_usd']:.4f}")
    print()
    print("  -> the control compares a RESTING LIMIT fill against a MARKET")
    print("     fill. The pre-fill window is worse for the market entrant")
    print("     BY GEOMETRY, so the t-stat measures the limit-order")
    print("     mechanic (already in the backtest), not timing.")

    print()
    t = a["timing"]
    print("THE TIMING NUMBER (same execution model on both sides):")
    print(f"  sum(gross)   ${t['gross_total']:,.2f}")
    print(f"  sum(drift)   ${t['drift_total']:,.2f}   <- passive hold, same positions")
    print(f"  sum(excess)  ${t['excess_total']:,.2f}   <- gross - drift")
    print(f"  excess/leg   {t['excess_per_leg']:+.4f}   t = {t['t_excess']:+.2f}")
    print(f"  drift share of gross  {100 * t['drift_share_of_gross']:.1f}%")
    print()
    if t["t_excess"] < 0:
        print("VERDICT: NO TIMING EDGE - the gross is market drift; the")
        print("        timing component (excess) is negative.")
    else:
        print("VERDICT: the timing component (excess) is positive;")
        print("        see leg_buckets for the per-leg breakdown.")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--samples", type=int, default=DEFAULT_SAMPLES)
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    payload = load_ledger(args.ledger, expect_canonical=True)
    exec_start = payload.get("exec_start")
    exec_end = payload.get("exec_end")
    if not exec_start or not exec_end:
        raise SystemExit("ledger has no exec_start/exec_end - rebuild it")

    t0 = time.perf_counter()
    close = _load_close(exec_start, exec_end)
    print(f"loaded {close.size} bars in {time.perf_counter() - t0:.1f}s")
    a = analyse(payload, close, samples=args.samples)
    print_report(a)

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(a, indent=2, default=float),
                            encoding="utf-8")
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
