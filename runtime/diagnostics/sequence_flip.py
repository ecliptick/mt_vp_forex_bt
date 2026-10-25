"""Sequence-aware direction FLIP experiment (2026-10-23).

The scalar structure score is a sufficient statistic that explains
nothing: on 2026-01, corr(sequence's scalar score, sequence's realised
EV) = +0.030 over 60 signatures. The SHAPE of the trailing 5 BoS/CHoCH
events carries information the mean discarded.

This harness builds a flip map from the sequences with NEGATIVE pooled
EV, applies it via the engine's ``structure_seq_flip_map``, and re-runs
the corpus.

Method
------
1. Measure per-signature realised EV on a *training* set of slices.
2. Build ``flip_map = {sig: -1 for sig with EV < -threshold}``.
3. Apply on a *held-out* set of slices.

The train/test split matters: fitting a flip map on the same slices you
evaluate it on is how you manufacture a beta proxy. The map is fitted
on trending slices and evaluated on the others.

Usage
-----
    python -m runtime.diagnostics.sequence_flip --stage fit
    python -m runtime.diagnostics.sequence_flip --stage test
    python -m runtime.diagnostics.sequence_flip --stage both
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

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT_DIR = Path("runtime/diagnostics/_reports/sequence_flip")

# Fit on TRENDING slices, test on the rest. A flip map fitted on flat
# slices would learn nothing (no directional information to invert);
# one fitted on the test slices would be cheating.
FIT_SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
]
TEST_SLICES: list[tuple[str, str, str, str]] = [
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
]


def _artifacts(tag: str, start: str, end: str, bars):
    # Scope name carries the cadence: the 1s and 1m zone sets are
    # different artifacts and must not share a cache entry. The
    # fingerprint also covers ``resample_to_n_secs`` (it is part of
    # the detector kwargs), so a stale entry cannot be reused.
    return ArtifactCache(DATA, f"seq1m_{tag}", start, end).build_or_load(bars)


def _run(label: str, start: str, end: str, flip_map: dict | None,
         entry_mode: str = "dual") -> dict[str, Any]:
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        structure_seq_flip_map=flip_map,
        structure_seq_len=5,
        entry_mode=entry_mode,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    wall = time.perf_counter() - t0
    trades = list(res.trades)
    net = float(sum(t.pnl_net_usd for t in trades))
    return {
        "label": label,
        "mkt_ret_pct": float(
            (bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0),
        "legs": len(trades),
        "net": net,
        "gross": float(sum(t.pnl_gross_usd for t in trades)),
        "flipped": int(getattr(res, "n_structure_seq_flipped", 0)),
        "wall_secs": wall,
        "trades": trades,
    }


def _signatures(trades: list, art) -> dict[str, list]:
    """Group trades by their trailing 5-event signature."""
    from bt._engine.market_structure import _BREAK_SIG, _break_bars

    state = art["structure"]
    breaks = state.breaks
    bars_arr = _break_bars(state)
    out: dict[str, list] = {}
    for t in trades:
        bar = getattr(t, "entry_bar", None)
        if bar is None or bars_arr.size == 0:
            continue
        upto = int(np.searchsorted(bars_arr, bar, side="right"))
        if upto < 5:
            continue
        sig = "".join(_BREAK_SIG.get(breaks[j].kind, "?")
                      for j in range(upto - 5, upto))
        if sig:
            out.setdefault(sig, []).append(t)
    return out


def fit_map(threshold: float) -> tuple[dict[str, int], dict[str, Any]]:
    """Measure per-signature EV on the FIT slices; flip the losers."""
    pooled: dict[str, list] = {}
    per_slice: dict[str, dict[str, float]] = {}
    for label, start, end, _regime in FIT_SLICES:
        r = _run(label, start, end, None)
        art = _artifacts(label.split()[0], start, end,
                         load_bars(DATA, start, end))
        sigs = _signatures(r["trades"], art)
        per_slice[label] = {}
        for sig, group in sigs.items():
            pooled.setdefault(sig, []).extend(group)
            net = sum(x.pnl_net_usd for x in group)
            per_slice[label][sig] = net / len(group)
    stats: dict[str, Any] = {}
    flip_map: dict[str, int] = {}
    for sig, group in pooled.items():
        net = sum(x.pnl_net_usd for x in group)
        ev = net / len(group)
        stats[sig] = {"n": len(group), "net": net, "ev": ev}
        if ev < -threshold and len(group) >= 20:
            flip_map[sig] = -1
    stats["_per_slice_ev"] = per_slice
    return flip_map, stats


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--stage", default="both",
                    choices=("fit", "test", "both"))
    ap.add_argument("--threshold", type=float, default=0.05,
                    help="Flip signatures whose pooled EV < -threshold.")
    ap.add_argument("--min-n", type=int, default=20,
                    help="Minimum pooled legs for a signature to be flipped.")
    ap.add_argument("--entry-mode", default="dual")
    args = ap.parse_args()

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print("== sequence-aware direction flip ==")
    print(f"threshold: EV < -{args.threshold}   min legs: {args.min_n}   "
          f"entry_mode: {args.entry_mode}")
    print()

    flip_map: dict[str, int] = {}
    if args.stage in ("fit", "both"):
        print("-- FIT (trending slices) --")
        flip_map, stats = fit_map(args.threshold)
        flip_map = {k: v for k, v in flip_map.items()
                    if stats[k]["n"] >= args.min_n}
        print(f"  {len(flip_map)} signatures flagged for flip")
        for sig, info in sorted(
            stats.items(),
            key=lambda kv: kv[1]["ev"] if isinstance(kv[1], dict) and "ev" in kv[1] else 0,
        ):
            if not isinstance(info, dict) or "ev" not in info:
                continue
            if info["ev"] < 0:
                mark = "FLIP" if sig in flip_map else "    "
                print(f"    {mark} {sig}  n={info['n']:>4}  "
                      f"EV=${info['ev']:+.3f}")
        with open(OUT_DIR / "flip_map.json", "w", encoding="utf-8") as fh:
            json.dump({"flip_map": flip_map,
                       "threshold": args.threshold,
                       "min_n": args.min_n,
                       "stats": {k: v for k, v in stats.items()
                                 if not k.startswith("_")}},
                      fh, indent=2, default=str)
        print(f"  -> {OUT_DIR / 'flip_map.json'}")
        print()

    if args.stage in ("test", "both"):
        if not flip_map:
            with open(OUT_DIR / "flip_map.json", encoding="utf-8") as fh:
                flip_map = json.load(fh)["flip_map"]
        print("-- TEST (held-out slices) --")
        print(f"  {'slice':<14} {'mkt':>8} {'legs':>6} {'flipped':>8} "
              f"{'net (no flip)':>14} {'net (flip)':>12} {'delta':>9}")
        tot_base = tot_flip = 0.0
        for label, start, end, regime in TEST_SLICES:
            base = _run(label, start, end, None, args.entry_mode)
            flp = _run(label, start, end, flip_map, args.entry_mode)
            tot_base += base["net"]
            tot_flip += flp["net"]
            print(f"  {label:<14} {base['mkt_ret_pct']:>+7.2f}% "
                  f"{base['legs']:>6} {flp['flipped']:>8} "
                  f"{base['net']:>14,.0f} {flp['net']:>12,.0f} "
                  f"{flp['net'] - base['net']:>+9,.0f}")
        print()
        print(f"  {'TOTAL':<14} {'':>8} {'':>6} {'':>8} "
              f"{tot_base:>14,.0f} {tot_flip:>12,.0f} "
              f"{tot_flip - tot_base:>+9,.0f}")
        print()
        if tot_flip > tot_base:
            print("  => The flip map IMPROVES held-out P&L. The sequence")
            print("     shape carries real information the scalar score lost.")
        else:
            print("  => The flip map does NOT improve held-out P&L.")
            print("     The per-sequence edge does not generalise.")


if __name__ == "__main__":
    main()
