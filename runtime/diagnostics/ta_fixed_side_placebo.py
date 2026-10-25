"""THE FIXED-SIDE placebo — is the TA skill real, or a directional bet?

WHY THIS EXISTS
===============

The 6-month episode scoreboard (``ta_1m_signal_sweep``) found that two
mean-reversion families -- ``rsi_revert`` and ``bb_fade`` -- beat BOTH
placebos: the coin-flip-SIDE arm and the random-TIME-SHIFT arm.

But there is a third, much more boring explanation that neither placebo
can rule out:

    THE MARKET ITSELF WAS DRIFTING, AND THE RULE WAS LONG.

A rule that is always long, on a market that went up, "beats" a
coin-flip side arm -- because the coin flip only gets the drift half
the time. The coin-flip placebo destroys the rule's SIDE *selection*
but it CONTAINS the market's drift in BOTH arms, so it cannot
distinguish "the rule picked the right side" from "the market paid
anyone who was long".

The repo's own verdict on the FVG recipe is exactly this failure mode:
``drift`` was 130% of gross and ``excess`` was significantly NEGATIVE.
So this harness needs the SAME decomposition, and the same discipline:

    a control must be EXCHANGEABLE with what it controls for.

THE ARM
=======

``always_long``   every directional episode's side forced to LONG.
                  The INTERVALS, the durations, the abstentions and
                  the time-in-market are ALL held fixed. Only the
                  rule's side CHOICE is replaced by a constant.

If the rule's WACC is no better than always-long's WACC over the SAME
intervals, then the rule's side selection is worth nothing and the
whole result is market drift.

``always_short``  the mirror image. Between the two, any rule whose
side selection has content must beat BOTH constants.

WHY THIS IS THE RIGHT CONTROL (and not the coin flip)
======================================================

The coin-flip arm is exchangeable in the sense that it preserves the
schedule and the time in market, and it IS a valid test of "is the
side choice better than chance". But chance is not the right baseline
when the market has a directional bias: chance + drift is a strong
baseline, and a rule can look good without adding anything.

The constant-side arms are the STRONGER baseline: they keep the exact
same intervals the rule chose, so they inherit the rule's own timing
selection, and they remove only the side choice. Any excess over them
is attributable to the side choice ALONE, with the market's drift
fully in the baseline.

THE VERDICT
===========

For each rule:

    wacc_rule      the rule's own magnitude-weighted accuracy
    wacc_always_long / wacc_always_short
    side_edge      = wacc_rule - max(wacc_always_long, wacc_always_short)

``side_edge > 0`` with a bootstrap CI above zero means the rule's SIDE
SELECTION carries information. ``side_edge <= 0`` means the rule is a
directional bet and the episode result is drift.

This is the repo's ``drift`` / ``excess`` decomposition, applied to a
metric that has no stops, no targets and no fees.

RUN
===
    python -m runtime.diagnostics.ta_fixed_side_placebo
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from runtime.diagnostics.ta_1m_core import (
    build_bars_1m,
    build_strategy_signals,
)
from runtime.diagnostics.ta_signal_episodes import (
    FLAT,
    LONG,
    SHORT,
    build_episodes,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars

OUT_DIR = Path("runtime/diagnostics/_reports")

DEFAULT_START = "2025-01-01"
DEFAULT_END = "2025-07-01"          # 6 months, CONTINUOUS

# The two families the episode scoreboard flagged, plus their nearest
# siblings as controls.
FAMILIES = ("rsi_revert", "bb_fade", "ema_cross", "bb_break")

N_BOOT = 2000
SEED = 11


def _force_side(eps, side: int):
    """Replace every directional episode's side, keeping the schedule."""
    from dataclasses import replace

    out = []
    for e in eps:
        if e.state == FLAT:
            out.append(e)          # the abstentions are untouched
        else:
            out.append(replace(e, state=side))
    return out


def _bootstrap_ci(wacc: float, deltas: np.ndarray, n_boot: int = N_BOOT,
                  seed: int = SEED) -> tuple[float, float, float]:
    """Bootstrap the mean of the per-episode signed deltas.

    ``deltas`` is the per-episode ``|delta| * sign(delta)`` contribution,
    so its mean IS the WACC. The resample is over EPISODES (the unit of
    observation), which is the honest population for this question.
    """
    rng = np.random.default_rng(seed)
    n = len(deltas)
    if n == 0:
        return (float("nan"), float("nan"), float("nan"))
    idx = rng.integers(0, n, size=(n_boot, n))
    means = deltas[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return (float(wacc), float(lo), float(hi))


def _episode_deltas(eps, bars1m, executable: bool = True) -> np.ndarray:
    """Per-episode signed |delta| contributions (the WACC numerator)."""
    out = []
    o, c = bars1m.open, bars1m.close
    for e in eps:
        if e.state == FLAT:
            continue
        if executable:
            d = (e.open_exit - e.open_entry) * e.state
        else:
            d = (e.close_exit - e.close_entry) * e.state
        out.append(abs(d) * np.sign(d) if d != 0 else 0.0)
    return np.asarray(out, dtype=np.float64)


def _wacc(deltas: np.ndarray) -> float:
    if len(deltas) == 0:
        return float("nan")
    tot = np.abs(deltas).sum()
    # deltas already carry the sign; WACC = sum(|d| * sign(d)) / sum(|d|)
    # and |d| * sign(d) == d, so this is sum(d) / sum(|d|).
    return float(deltas.sum() / tot) if tot > 0 else 0.0


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--n-bootstrap", type=int, default=N_BOOT)
    ap.add_argument("--seed", type=int, default=SEED)
    ap.add_argument("--out", type=str, default="ta_fixed_side_placebo.json")
    args = ap.parse_args()

    data = args.data
    if not data.exists():
        raise SystemExit(f"data file not found: {data}")

    print("== TA FIXED-SIDE placebo ==")
    print(f"data  : {data}")
    print(f"window: {args.start} .. {args.end}  (continuous)")
    print()

    bars = load_bars(data, args.start, args.end)
    b1 = build_bars_1m(bars)
    print(f"loaded {len(bars):,} 1s bars -> {len(b1):,} 1m bars   "
          f"spot=${float(np.median(b1.close)):,.2f}", flush=True)

    strategies = build_strategy_signals(b1, names=FAMILIES)
    rows: list[dict[str, Any]] = []

    for strat in strategies:
        eps = build_episodes(b1, strat.signal)
        rule_d = _episode_deltas(eps, b1)
        w_rule = _wacc(rule_d)

        al_d = _episode_deltas(_force_side(eps, LONG), b1)
        as_d = _episode_deltas(_force_side(eps, SHORT), b1)
        w_al = _wacc(al_d)
        w_as = _wacc(as_d)

        best_const = max(w_al, w_as)
        edge = w_rule - best_const

        # The bootstrap resamples EPISODES (the unit of observation) and
        # recomputes the paired difference against the SAME constant
        # baseline, so the CI is on the edge, not on the raw WACC.
        rng = np.random.default_rng(args.seed)
        n = len(rule_d)
        boot_edges = []
        for _ in range(args.n_bootstrap):
            i = rng.integers(0, n, size=n)
            den = np.abs(rule_d[i]).sum()
            wr = rule_d[i].sum() / den if den else 0.0
            boot_edges.append(wr - best_const)
        lo, hi = np.percentile(boot_edges, [2.5, 97.5])

        rows.append({
            "label": strat.label,
            "n_directional": int(len(rule_d)),
            "wacc_rule": w_rule,
            "wacc_always_long": w_al,
            "wacc_always_short": w_as,
            "side_edge": float(edge),
            "side_edge_ci_lo": float(lo),
            "side_edge_ci_hi": float(hi),
            "side_edge_significant": bool(edge > 0 and lo > 0),
            "always_long_is_the_better_constant": bool(w_al >= w_as),
        })

        flag = "SIGNIFICANT" if rows[-1]["side_edge_significant"] else ""
        print(f"{strat.label[:36]:<38} n={len(rule_d):>6} "
              f"rule={w_rule:+.4f}  alwaysL={w_al:+.4f}  alwaysS={w_as:+.4f}  "
              f"edge={edge:+.4f} [{lo:+.4f},{hi:+.4f}] {flag}", flush=True)

    out = {
        "ts_utc": pd.Timestamp.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
        "data": str(data),
        "window": {"start": args.start, "end": args.end},
        "n_bootstrap": args.n_bootstrap,
        "seed": args.seed,
        "rows": rows,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    path = OUT_DIR / args.out
    path.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")

    print()
    print("=" * 96)
    print("VERDICT")
    print("=" * 96)
    sig = [r for r in rows if r["side_edge_significant"]]
    print(f"cells measured                          : {len(rows)}")
    print(f"cells whose SIDE SELECTION beats BOTH    : {len(sig)}")
    print(f"  a constant-side baseline               : {len(rows) - len(sig)}")
    print()
    if sig:
        print("The side selection carries information:")
        for r in sorted(sig, key=lambda r: -r["side_edge"])[:12]:
            print(f"  {r['label'][:36]:<38} edge={r['side_edge']:+.4f} "
                  f"[{r['side_edge_ci_lo']:+.4f},{r['side_edge_ci_hi']:+.4f}]")
    else:
        print("NO cell's side selection beats a constant-side baseline.")
        print("The episode result is MARKET DRIFT, not rule skill.")
    print()
    print(f"-> {path}")


if __name__ == "__main__":
    main()
