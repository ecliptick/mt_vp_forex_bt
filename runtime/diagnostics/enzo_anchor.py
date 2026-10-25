"""Enzo § 6a/§ 6b -- resolve the PDF's price anchor, with its NULL.

The PDF (p4) gives a worked example on the H4 chart of the week
19/10 - 23/10 2025:

    "Base on this week 19/10 23/10 H4 chart, downtrend is confirmed
     base on EMA, a trade (SELL LIMIT) will be placed on WHITE ARROW
     area, and then it only triggered (yellow arrow) after more than
     24 hours.  The sell limit should be placed around price
     SELL LIMIT: 4143.50 and BUY/SELL LIMIT should only be placed
     $13 away from YELLOW LINE"

So the yellow line must read 4143.50 - 13 = 4130.50 EXACTLY, and the
order must fill more than 24 h after it was armed.

This script resolves the anchor in three steps, and — the point of
the exercise — measures the NULL of the search before believing any
survivor:

  STEP 1  GEOMETRY.  A SELL LIMIT rests ABOVE the market.  That is a
          hard constraint, independent of any EMA definition.  For
          every arm bar in the PDF's week, the fill is the first later
          bar whose HIGH reaches 4143.50.  We keep the arm bars whose
          fill is >24 h later AND inside 21/10..23/10.

  STEP 2  THE PERIOD, at the geometry-pinned arm bar.  For each price
          source and each EMA period, does the yellow line read
          4130.50?  (The MT4-seeded EMA from enzo_core, because that
          is what the engine uses.)

  STEP 3  THE NULL.  Repeat STEP 1+2 for a set of CONTROL target
          levels around 4130.50.  If the PDF's level does not stand
          out from the controls, the anchor does not identify a
          period and must be reported as such.

Usage:
    python -m runtime.diagnostics.enzo_anchor
"""
from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.diagnostics.enzo_core import ema as mt4_ema

# ── the PDF's facts ──────────────────────────────────────────────
PDF_LIMIT = 4143.50
PDF_OFFSET = 13.0
PDF_WEEK_LO = pd.Timestamp("2025-10-19 00:00", tz="UTC")
PDF_WEEK_HI = pd.Timestamp("2025-10-24 00:00", tz="UTC")
FILL_LO = pd.Timestamp("2025-10-21 00:00", tz="UTC")
FILL_HI = pd.Timestamp("2025-10-24 00:00", tz="UTC")

# the corpus (the canonical XAUUSD 1y file)
DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")

PRICE_SRC = {
    "close": lambda d: d["close"],
    "open": lambda d: d["open"],
    "typical": lambda d: (d["high"] + d["low"] + d["close"]) / 3.0,
    "hl2": lambda d: (d["high"] + d["low"]) / 2.0,
    "ohlc4": lambda d: (d["open"] + d["high"] + d["low"] + d["close"]) / 4.0,
}

H4_OFFSETS = (0, 1, 2, 3)
PERIOD_LO, PERIOD_HI = 10, 140
TOL = 0.50


def load_h4() -> pd.DataFrame:
    df = pd.read_parquet(DATA, columns=["time", "open", "high", "low", "close"])
    df["time"] = pd.to_datetime(df["time"], utc=True)
    return df.set_index("time")


def h4_view(df: pd.DataFrame, off_h: int) -> pd.DataFrame:
    return df.resample("4h", origin="start_day",
                       offset=pd.Timedelta(hours=off_h)) \
            .agg({"open": "first", "high": "max", "low": "min",
                  "close": "last"}).dropna()


def step1_geometry(df: pd.DataFrame) -> list[tuple]:
    """Arm bars forced by geometry alone (a SELL LIMIT rests above)."""
    out = []
    for off_h in H4_OFFSETS:
        h4 = h4_view(df, off_h)
        hi = h4["high"].to_numpy()
        cl = h4["close"].to_numpy()
        idx = h4.index
        for k, t in enumerate(idx):
            if t < PDF_WEEK_LO or t > PDF_WEEK_HI:
                continue
            if not (cl[k] < PDF_LIMIT):
                continue                      # the order cannot rest here
            fill = None
            for j in range(k + 1, min(k + 40, len(idx))):
                if hi[j] >= PDF_LIMIT:
                    fill = j
                    break
            if fill is None:
                continue
            lag = (idx[fill] - (t + pd.Timedelta(hours=4))) \
                .total_seconds() / 3600
            out.append({
                "offset_h": off_h, "arm_bar": t, "arm_close": float(cl[k]),
                "fill_bar": idx[fill], "fill_high": float(hi[fill]),
                "lag_h": float(lag),
                "gt24": lag > 24,
                "in_week": bool(FILL_LO <= idx[fill] < FILL_HI),
            })
    return out


def step2_periods(df: pd.DataFrame, target: float,
                  arm_bars: list[tuple]) -> list[tuple]:
    """Candidates whose yellow line reads `target` at a pinned arm bar."""
    out = []
    for off_h, arm in arm_bars:
        h4 = h4_view(df, off_h)
        idx = h4.index
        k = int(np.searchsorted(idx, arm))
        for pname, fn in PRICE_SRC.items():
            px = fn(h4).to_numpy()
            for n in range(PERIOD_LO, PERIOD_HI + 1):
                v = float(mt4_ema(px, n)[k])
                err = abs(v - target)
                if err > TOL:
                    continue
                out.append((err, off_h, pname, n, arm, v))
    out.sort()
    return out


def coherent_survivors(df: pd.DataFrame, target: float) -> list[tuple]:
    """STEP 1 + STEP 2 combined: value AND the fill rules."""
    survivors = []
    for off_h in H4_OFFSETS:
        h4 = h4_view(df, off_h)
        hi = h4["high"].to_numpy()
        cl = h4["close"].to_numpy()
        idx = h4.index
        sel = np.array([(PDF_WEEK_LO <= t <= PDF_WEEK_HI) for t in idx])
        ks = np.nonzero(sel)[0]
        for pname, fn in PRICE_SRC.items():
            px = fn(h4).to_numpy()
            for n in range(PERIOD_LO, PERIOD_HI + 1):
                e = mt4_ema(px, n)
                for k in ks:
                    err = abs(float(e[k]) - target)
                    if err > TOL:
                        continue
                    if not (cl[k] < PDF_LIMIT):
                        continue
                    fill = None
                    for j in range(k + 1, min(k + 40, len(idx))):
                        if hi[j] >= PDF_LIMIT:
                            fill = j
                            break
                    if fill is None:
                        continue
                    lag = (idx[fill] - (idx[k] + pd.Timedelta(hours=4))) \
                        .total_seconds() / 3600
                    if lag <= 24:
                        continue
                    if not (FILL_LO <= idx[fill] < FILL_HI):
                        continue
                    survivors.append((err, off_h, pname, n, idx[k],
                                      float(e[k]), idx[fill], lag))
    survivors.sort()
    return survivors


def main() -> int:
    print("=" * 100)
    print("ENZO § 6a -- the PDF's price anchor, resolved WITH ITS NULL")
    print("=" * 100)
    print(f"  PDF limit      {PDF_LIMIT}")
    print(f"  PDF offset     ${PDF_OFFSET}")
    print(f"  => yellow must read {PDF_LIMIT - PDF_OFFSET}")
    print()

    df = load_h4()

    # ── STEP 1 ──────────────────────────────────────────────────
    print("=" * 100)
    print("STEP 1 -- GEOMETRY: which arm bars can even hold a resting "
          "SELL LIMIT?")
    print("=" * 100)
    geo = step1_geometry(df)
    pinned = [g for g in geo if g["gt24"] and g["in_week"]]
    print(f"  arm bars in the week where the limit CAN rest: "
          f"{len(geo)}")
    print(f"  ... of which the fill is >24 h later AND inside "
          f"21/10..23/10: {len(pinned)}")
    for g in pinned:
        print(f"    offset {g['offset_h']}h  arm {g['arm_bar']}  "
              f"close {g['arm_close']:.2f}  fill {g['fill_bar']}  "
              f"high {g['fill_high']:.2f}  lag {g['lag_h']:.1f} h")
    print()
    print("  -> the arm bar is pinned by GEOMETRY, with no EMA period "
          "involved.")
    print()

    # ── STEP 2 ──────────────────────────────────────────────────
    print("=" * 100)
    print("STEP 2 -- the yellow line's period at the pinned arm bar")
    print("=" * 100)
    target = PDF_LIMIT - PDF_OFFSET
    hits = step2_periods(df, target, [(g["offset_h"], g["arm_bar"])
                                      for g in pinned])
    print(f"  candidates within ${TOL:.2f} of {target:.2f}: {len(hits)}")
    print(f"  {'err':>6} {'off':>4} {'src':>8} {'n':>4} {'arm bar':>22} "
          f"{'yellow':>9} {'limit':>9}")
    for err, off_h, pname, n, arm, v in hits:
        print(f"  {err:>6.2f} {off_h:>4} {pname:>8} {n:>4} {str(arm):>22} "
              f"{v:>9.2f} {v + PDF_OFFSET:>9.2f}")
    print()

    # the canonical yellow, as a control
    print("  the CANONICAL yellow (EMA(60, close)) at the pinned bar:")
    for g in pinned:
        h4 = h4_view(df, g["offset_h"])
        k = int(np.searchsorted(h4.index, g["arm_bar"]))
        v = float(mt4_ema(PRICE_SRC["close"](h4).to_numpy(), 60)[k])
        print(f"    offset {g['offset_h']}h  {g['arm_bar']}  yellow={v:.2f}  "
              f"limit={v + PDF_OFFSET:.2f}  "
              f"err vs {PDF_LIMIT}: ${abs(v + PDF_OFFSET - PDF_LIMIT):.2f}")
    print()

    # ── STEP 3 ──────────────────────────────────────────────────
    print("=" * 100)
    print("STEP 3 -- THE NULL: is the PDF's LEVEL special?")
    print("=" * 100)
    pdf_n = len(coherent_survivors(df, target))
    print(f"  coherent survivors at the PDF's level {target:.2f}: {pdf_n}")
    print()
    print(f"  {'control target':>18} {'survivors':>11}")
    ctrl_levels = [target + d for d in (-5, -2, -1, -0.5, 0.5, 1, 2, 5)]
    ctrl_counts = []
    for lv in ctrl_levels:
        c = len(coherent_survivors(df, lv))
        ctrl_counts.append(c)
        mark = "   <-- the PDF's level" if abs(lv - target) < 1e-9 else ""
        print(f"  {lv:>18.2f} {c:>11}{mark}")

    rng = np.random.default_rng(20261025)
    rand_counts = []
    for lv in target + rng.uniform(-5, 5, 8):
        c = len(coherent_survivors(df, float(lv)))
        rand_counts.append(c)
        print(f"  {lv:>18.3f} {c:>11}   (random control)")

    allc = ctrl_counts + rand_counts
    print()
    print("=" * 100)
    print("VERDICT")
    print("=" * 100)
    print(f"  PDF level      : {pdf_n} survivors")
    print(f"  control mean   : {np.mean(allc):.2f}   max: {max(allc)}")
    pct = 100.0 * sum(1 for c in allc if c < pdf_n) / len(allc)
    print(f"  PDF percentile among controls: {pct:.1f}%")
    if pdf_n >= max(allc):
        print("  -> the PDF's level IS an outlier -> the anchor identifies "
              "a period.")
    else:
        print("  -> the PDF's level is NOT an outlier -> the anchor does "
              "NOT identify")
        print("     a period.  The geometry finding stands (it is forced by")
        print("     a constraint), but the LEVEL carries no information.")
    print()
    print("  REPORTED, NOT APPLIED: the canonical triple (10/20/60) and the")
    print("  canonical lifecycle (cross-cancels, order rests) are UNCHANGED.")
    print("  See docs/enzo_strategies.md § 6a and § 9.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
