"""Does the SL actually move to breakeven on the TP1 remainder leg?

The user asks: "does SL actually get modified when first leg hits 50% tp
on the second leg?"

Reading the engine (``bt/_engine/vp_multi_target_engine.py``) says YES:

* the TP1 branch closes ``pct`` of the position and reopens the
  remainder with ``stop_price=entry_price`` (breakeven) and
  ``partial_close_pct=0.0``;
* because ``partial_close_pct == 0``, ``tp2_active`` is False on the
  remainder, so it is evaluated by the SINGLE-TARGET path, where
  ``sl_hit = b_low <= ot.stop_price`` (long) / ``b_high >= ot.stop_price``
  (short) -- i.e. the breakeven stop is a real, enforced exit.

This script MEASURES that rather than trusting the read:

1. For every remainder leg (identified by ``sl_struct_ref ==
   "breakeven"``), report the exit reason and the realised
   ``exit_price - entry_price``. A breakeven stop that is genuinely
   enforced should show realised P&L clustered at $0 (minus fees) on
   the sl-exited remainder legs.

2. Measure the SAME-BAR escape: on the bar that fired TP1, did the
   bar already trade back through the breakeven level? The TP1 branch
   ``continue``s, so the fresh remainder is NOT re-scanned against
   that bar -- a bar that hit TP1 and also dipped back to entry lets
   the remainder survive a stop that was already breached.

Usage
-----
    python -m runtime.diagnostics.breakeven_reminder_check
"""
from __future__ import annotations

import sys
from pathlib import Path

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
S, E = "2025-05-01", "2025-06-01"

# The six canonical slices (3 fit + 3 held out), matching
# ``sl_ceiling_sweep8.py``. A SINGLE slice is too thin once the vol
# gate is on: 2025-05 carries only 6 TP1 legs, so a single-slice
# read cannot distinguish "the mechanism works" from "the mechanism
# rarely fires".
SLICES: list[tuple[str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
    ("2025-01 UP", "2025-01-01", "2025-02-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01"),
]


def _run_slice(s: str, e: str):
    """Run one slice and return the bars + trades + raw OHLC."""
    bars = load_bars(DATA, s, e)
    art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    return bars, list(res.trades)


def main() -> None:
    # ── Multi-slice summary ────────────────────────────────────────
    print()
    print("=== breakeven mechanism, CURRENT canonical recipe, 6 slices ===")
    print(f"  {'slice':<14} {'legs':>6} {'tp1':>5} {'rem':>5} "
          f"{'BE sl p50':>10} {'BE sl max':>10} {'esc':>5}")
    tot_tp1 = tot_rem = tot_esc = 0
    sl_p50s: list[float] = []
    sl_maxs: list[float] = []
    for tag, s, e in SLICES:
        bars, tr = _run_slice(s, e)
        lows = bars["low"].to_numpy(dtype=np.float64)
        highs = bars["high"].to_numpy(dtype=np.float64)
        leg1 = [t for t in tr if t.exit_reason == "tp1"]
        rem = [t for t in tr if t.sl_struct_ref == "breakeven"]
        rem_sl = [t for t in rem if t.exit_reason == "sl"]
        # Same-bar escape count.
        esc = 0
        for t1 in leg1:
            b = int(t1.exit_bar)
            if b < 0 or b >= len(lows):
                continue
            hit = (lows[b] <= t1.entry_price) if t1.direction > 0 \
                else (highs[b] >= t1.entry_price)
            if hit:
                esc += 1
        p50 = float(np.median([abs(t.sl_distance_usd) for t in rem_sl])) \
            if rem_sl else float("nan")
        mx = float(np.max([abs(t.sl_distance_usd) for t in rem_sl])) \
            if rem_sl else float("nan")
        if rem_sl:
            sl_p50s.append(p50)
            sl_maxs.append(mx)
        print(f"  {tag:<14} {len(tr):>6} {len(leg1):>5} {len(rem):>5} "
              f"{p50:>10.2f} {mx:>10.2f} {esc:>5}")
        tot_tp1 += len(leg1)
        tot_rem += len(rem)
        tot_esc += esc
        del bars
        import gc
        gc.collect()

    print()
    print(f"  TOTAL tp1 legs        : {tot_tp1}")
    print(f"  TOTAL remainder legs  : {tot_rem}")
    print(f"  same-bar escapes      : {tot_esc} "
          f"({tot_esc / max(tot_tp1, 1):.2%} of tp1 legs)")
    if sl_p50s:
        print(f"  BE stop distance      : p50 max across slices = "
              f"${max(sl_p50s):.2f}  (absolute max = ${max(sl_maxs):.2f})")
        print()
        print("  -> an ENFORCED breakeven stop has sl_distance == 0.00 on")
        print("     every sl-exited remainder leg. Any non-zero value above")
        print("     means the stop moved somewhere else.")
    print()

    # ── Single-slice detail (2025-05) ──────────────────────────────
    bars, tr = _run_slice(S, E)
    lows = bars["low"].to_numpy(dtype=np.float64)
    highs = bars["high"].to_numpy(dtype=np.float64)

    leg1 = [t for t in tr if t.exit_reason == "tp1"]
    rem = [t for t in tr if t.sl_struct_ref == "breakeven"]
    others = [t for t in tr if t.sl_struct_ref != "breakeven" and t.exit_reason != "tp1"]

    print(f"=== {S[:7]} FLAT, canonical recipe (detail) ===")
    print(f"  total legs              : {len(tr)}")
    print(f"  TP1 partial legs        : {len(leg1)}")
    print(f"  remainder legs (BE)     : {len(rem)}")
    print(f"  other legs              : {len(others)}")
    print()

    print("--- 1. remainder-leg exits (sl_struct_ref == 'breakeven') ---")
    by_reason: dict[str, list] = {}
    for t in rem:
        by_reason.setdefault(t.exit_reason, []).append(t)
    for reason in sorted(by_reason):
        sel = by_reason[reason]
        realized = np.array([
            (t.exit_price - t.entry_price) * t.direction for t in sel
        ])
        gross = float(sum(t.pnl_gross_usd for t in sel))
        net = float(sum(t.pnl_net_usd for t in sel))
        sl_dist = np.array([abs(t.sl_distance_usd) for t in sel])
        print(f"  {reason:<8} n={len(sel):>6}  "
              f"realized p50=${np.median(realized):>8.2f} "
              f"p10=${np.percentile(realized, 10):>8.2f} "
              f"p90=${np.percentile(realized, 90):>8.2f}")
        print(f"           gross=${gross:>12,.2f}  net=${net:>12,.2f}  "
              f"sl_distance p50=${np.median(sl_dist):.2f} "
              f"max=${np.max(sl_dist):.2f}")
    print()
    print("  -> an ENFORCED breakeven stop shows realised p50 = $0.00 and")
    print("     sl_distance = 0.00 on the sl-exited remainder legs.")
    print()

    # ── 2. Same-bar escape on the TP1 bar ───────────────────────────
    print("--- 2. same-bar escape: did the TP1 bar already breach BE? ---")
    esc = 0
    checked = 0
    for t in leg1:
        b = int(t.exit_bar)
        if b < 0 or b >= len(lows):
            continue
        checked += 1
        if t.direction > 0:
            if lows[b] <= t.entry_price:
                esc += 1
        else:
            if highs[b] >= t.entry_price:
                esc += 1
    print(f"  TP1 legs with a readable bar : {checked}")
    print(f"  TP1 bar already breached BE   : {esc}  "
          f"({esc / checked:.3%} of TP1 legs)" if checked else "")
    print()
    print("  -> the TP1 branch `continue`s, so the fresh remainder is NOT")
    print("     re-scanned against the TP1 bar. A bar that hit TP1 AND")
    print("     traded back to entry lets the remainder escape a stop that")
    print("     was already breached on that bar.")
    print()

    # ── 3. Distribution of remainder exits overall ──────────────────
    print("--- 3. all legs, split by role ---")
    for label, sel in (("TP1 partial (leg 1)", leg1),
                       ("remainder (leg 2)", rem),
                       ("other (pre-TP1 exits)", others)):
        if not sel:
            continue
        gross = float(sum(t.pnl_gross_usd for t in sel))
        net = float(sum(t.pnl_net_usd for t in sel))
        print(f"  {label:<24} n={len(sel):>6}  "
              f"gross=${gross:>11,.2f}  net=${net:>11,.2f}  "
              f"gross/leg=${gross / len(sel):>8.2f}")
    print()


if __name__ == "__main__":
    main()
