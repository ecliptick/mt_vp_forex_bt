"""DAILY direction-dependence for the whitelisted book's gross.

⚠️ THIS MODULE WAS RENAMED IN SPIRIT, NOT IN FILE.
========================================================

It used to regress each **MONTH's** gross on that month's market
return. **That measurement is invalid for this strategy** (AGENTS.md
§ 1.1 rule 7):

1. **The sample size is tiny.** The corpus is ~31 months, so the
   regression has n = 13..31 points. The standard error of a
   correlation at n = 13 is ``1/sqrt(n-3) = 0.32`` in Fisher-z
   space, so ``r = +0.459`` (the long-only book's H2 number) is only
   **t = 1.57** -- NOT distinguishable from zero. Every "market
   correlation" number the repo quoted was inside its own noise band.

2. **Monthly aggregation destroys the strategy's own time scale.**
   This is a high-frequency book (~7-40 legs a DAY). A month's gross
   sums thousands of legs whose entry and exit are spread over ~30
   distinct trading days; the month's return is ONE number. The
   regression therefore asks "did the book happen to sit on the right
   side of a 30-day trend", which is a statement about the BOOK'S
   COMPOSITION, not about whether the entries have skill.

3. **The month boundary is an ENGINE ARTEFACT.** The p95 cap (7d),
   the swing cap (24h) and the VP profile (7d rolling) are all
   history-dependent, so a cold monthly chunk trades its first ~8
   days with UNCAPPED targets (AGENTS.md § 1.1). The monthly
   aggregation inherits that artefact.

It was also **sign-unstable**: the monthly table called
``shipped_on_H2`` **-0.373**, while the daily regression measures the
same book at **+0.187** (Fisher-z t = +2.75). A number whose sign
flips with the aggregation window cannot decide anything.

THE REPLACEMENT
===============

The **DAILY** aggregation: n ~= 900 points over the corpus instead of
13-31, each point covering the strategy's ACTUAL trading horizon, with
no month boundary anywhere in the pipeline.

WHAT THIS MODULE MEASURES
=========================

For each book, over ONE continuous execution (no chunking):

* ``daily_gross``   -- the book's gross, bucketed by SIGNAL day.
* ``mkt_ret_pct``   -- the market's close-to-close return over the
  SAME calendar day.
* ``beta`` / ``alpha`` / ``r2`` / ``corr`` / ``corr_t`` -- the
  regression of daily gross on the daily market return.
* ``excess_per_leg`` -- the ledger's PER-LEG decomposition
  (``gross - passive hold``), which is the exact, per-leg
  alternative and is the number the verdict should read.

The per-leg decomposition does NOT need any aggregation at all: it
is an exact algebraic identity on every leg, so its t-statistic uses
n = thousands of legs, not n = 31 months.

THE VERDICT
============

The question is NOT "does the book correlate with the market?" -- it
is "**is the gross explained by the market's move, or by the
entries?**". The honest answer is the per-leg ``excess``:

* ``excess_per_leg > 0`` and significant -> the entries have skill.
* ``excess_per_leg <= 0`` -> the gross is the market's move.

The daily regression is reported as the AGGREGATE view of the same
question, and -- unlike the monthly one -- its standard error is small
enough to distinguish a real directional book from noise.

    python -m runtime.diagnostics.seq5_beta_regression
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.results import registry  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")
LEDGER_DIR = OUT_DIR

ARMS = (
    ("unfiltered_H1", "H1 (2024-01-01..2025-07-09)"),
    ("unfiltered_H2", "H2 (2025-07-09..2026-07-16)"),
    ("fitH2_on_H1", "OOS: H1 fit on H2"),
    ("fitH1_on_H2", "OOS: H2 fit on H1"),
    ("shipped_on_H1", "shipped set on H1"),
    ("shipped_on_H2", "shipped set on H2"),
    ("longonly_on_H1", "LONG-ONLY on H1"),
    ("longonly_on_H2", "LONG-ONLY on H2"),
)


def _load_ledger(arm: str) -> dict[str, Any]:
    path = LEDGER_DIR / f"leg_ledger_seqwlf_{arm}.json"
    if not path.exists():
        raise SystemExit(f"missing ledger for arm {arm}: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _daily(legs: dict[str, list]) -> pd.DataFrame:
    """Aggregate the legs to one row per calendar DAY."""
    st = np.asarray(legs["signal_time_ns"], dtype=np.int64)
    gross = np.asarray(legs["gross"], dtype=np.float64)
    side = np.asarray(legs["side"], dtype=np.int64)
    ts = pd.Series(pd.to_datetime(st, unit="ns", utc=True))
    df = pd.DataFrame({
        "day": ts.dt.strftime("%Y-%m-%d"),
        "gross": gross,
        "side": side,
    })
    out = df.groupby("day").agg(
        gross=("gross", "sum"),
        legs=("gross", "size"),
    ).reset_index()
    return out


def _market_daily(start: str, end: str) -> pd.DataFrame:
    """The market's return per calendar DAY over the same window."""
    cache = OUT_DIR / "_market_daily.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        df["mkt_ret_pct"] = df["mkt_ret_pct"].astype(float)
        return df

    from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars

    bars = load_bars(DEFAULT_DATA, start, end)
    ts = pd.Series(pd.to_datetime(bars["time"], unit="ms", utc=True))
    close = bars["close"].to_numpy(dtype=np.float64)
    df = pd.DataFrame({
        "day": ts.dt.strftime("%Y-%m-%d"),
        "close": close,
    })
    df = df.groupby("day")["close"].agg(["first", "last"]).reset_index()
    df["mkt_ret_pct"] = (df["last"] / df["first"] - 1.0) * 100.0
    cache.parent.mkdir(parents=True, exist_ok=True)
    df[["day", "mkt_ret_pct"]].to_csv(cache, index=False)
    return df[["day", "mkt_ret_pct"]]


def _fisher_t(r: float, n: int) -> float:
    """t-statistic of a correlation coefficient against zero.

    Fisher's z transform: ``z = atanh(r)`` with SE ``1/sqrt(n-3)``.
    This is the number that says whether a quoted correlation is
    distinguishable from zero -- the number the monthly regression
    never reported.
    """
    if n < 4 or not math.isfinite(r) or abs(r) >= 1.0:
        return 0.0
    z = math.atanh(r)
    return float(z / (1.0 / math.sqrt(n - 3)))


def _regress(mkt: np.ndarray, gross: np.ndarray) -> dict[str, float]:
    if mkt.size < 3 or np.std(mkt) <= 0:
        return {}
    slope, intercept = np.polyfit(mkt, gross, 1)
    pred = slope * mkt + intercept
    ss_res = float(np.sum((gross - pred) ** 2))
    ss_tot = float(np.sum((gross - gross.mean()) ** 2))
    corr = float(np.corrcoef(mkt, gross)[0, 1]) if np.std(gross) > 0 else 0.0
    return {
        "n_days": int(mkt.size),
        "beta_usd_per_pct": float(slope),
        "alpha_usd_per_day": float(intercept),
        "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0,
        "corr": corr,
        # The number the monthly regression omitted.
        "corr_t": _fisher_t(corr, int(mkt.size)),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=OUT_DIR / "seq5_beta_regression.json")
    args = ap.parse_args()

    # The market's daily return, once.
    mkt = _market_daily("2024-01-01", "2026-07-16")
    mkt_map = dict(zip(mkt["day"], mkt["mkt_ret_pct"]))

    report: dict[str, Any] = {"arms": {}, "aggregation": "daily"}
    print("=" * 92)
    print("DAILY DIRECTION DEPENDENCE -- is the whitelisted book a "
          "DIRECTIONAL BET?")
    print("=" * 92)
    print()
    print(f"  {'arm':<18} {'n days':>7} {'beta $/pct':>11} {'alpha $/day':>12} "
          f"{'r2':>7} {'corr':>8} {'corr t':>8}  verdict")
    print("  " + "-" * 88)

    for arm, label in ARMS:
        payload = _load_ledger(arm)
        mo = _daily(payload["legs"])
        mo["mkt_ret_pct"] = mo["day"].map(mkt_map)
        mo = mo.dropna(subset=["mkt_ret_pct"])

        reg = _regress(mo["mkt_ret_pct"].to_numpy(dtype=float),
                       mo["gross"].to_numpy(dtype=float))

        # The per-leg decomposition.
        legs = payload["legs"]
        gross = np.asarray(legs["gross"], dtype=np.float64)
        drift = np.asarray(legs["drift_usd"], dtype=np.float64)
        excess = np.asarray(legs["excess_usd"], dtype=np.float64)
        side = np.asarray(legs["side"], dtype=np.int64)

        def _t(x: np.ndarray) -> float:
            x = np.asarray(x, dtype=float)
            if x.size < 2 or x.std(ddof=1) <= 0:
                return 0.0
            return float(x.mean() / (x.std(ddof=1) / np.sqrt(x.size)))

        long_m = side > 0
        short_m = side < 0

        def _leg_block(m: np.ndarray) -> dict[str, Any]:
            if not m.any():
                return {}
            return {
                "legs": int(m.sum()),
                "gross": float(gross[m].sum()),
                "gross_per_leg": float(gross[m].mean()),
                # The ledger's field name is ``drift_usd``; the
                # quantity is the close-to-close PASSIVE HOLD.
                "drift_per_leg": float(drift[m].mean()),
                "drift_t": _t(drift[m]),
                "excess_per_leg": float(excess[m].mean()),
                "excess_t": _t(excess[m]),
            }

        block = {
            "label": label,
            "regression": reg,
            "total": {
                "legs": int(gross.size),
                "gross": float(gross.sum()),
                "gross_per_leg": float(gross.mean()),
                "drift_per_leg": float(drift.mean()),
                "drift_t": _t(drift),
                "excess_per_leg": float(excess.mean()),
                "excess_t": _t(excess),
            },
            "long": _leg_block(long_m),
            "short": _leg_block(short_m),
        }
        report["arms"][arm] = block

        if reg:
            # The verdict reads the PER-LEG excess. The daily
            # correlation is the aggregate view of the same question.
            total = block["total"]
            verdict = ("TIMING" if total["excess_per_leg"] > 0
                       and total["excess_t"] > 2.0 else "MARKET-DRIVEN")
        else:
            verdict = "n/a"
        print(f"  {arm:<18} {reg.get('n_days', 0):>7} "
              f"{reg.get('beta_usd_per_pct', 0):>+11,.1f} "
              f"{reg.get('alpha_usd_per_day', 0):>+12,.1f} "
              f"{reg.get('r2', 0):>7.3f} {reg.get('corr', 0):>+8.3f} "
              f"{reg.get('corr_t', 0):>+8.2f}  {verdict}")

    print()
    print("  -- the per-leg decomposition (why) --")
    print()
    print(f"  {'arm':<18} {'side':<6} {'legs':>7} {'g/leg':>8} {'hold/leg':>10} "
          f"{'hold t':>8} {'excess/leg':>11} {'excess t':>9}")
    print("  " + "-" * 84)
    for arm, _ in ARMS:
        b = report["arms"][arm]
        for side_key in ("long", "short"):
            sb = b.get(side_key) or {}
            if not sb:
                continue
            print(f"  {arm:<18} {side_key:<6} {sb['legs']:>7} "
                  f"{sb['gross_per_leg']:>+8.4f} {sb['drift_per_leg']:>+10.4f} "
                  f"{sb['drift_t']:>+8.2f} {sb['excess_per_leg']:>+11.4f} "
                  f"{sb['excess_t']:>+9.2f}")

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                        encoding="utf-8")
    print()
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq5_beta_regression",
        params={
            "window": "2024-01-01..2026-07-16",
            "arms": [a for a, _ in ARMS],
            "aggregation": "daily",
            "regression": "daily gross ~ alpha + beta * daily market return %",
        },
        summary={
            arm: {
                "beta": round(report["arms"][arm]["regression"].get(
                    "beta_usd_per_pct", 0.0), 2),
                "alpha": round(report["arms"][arm]["regression"].get(
                    "alpha_usd_per_day", 0.0), 2),
                "r2": round(report["arms"][arm]["regression"].get(
                    "r2", 0.0), 4),
                "corr": round(report["arms"][arm]["regression"].get(
                    "corr", 0.0), 4),
                "corr_t": round(report["arms"][arm]["regression"].get(
                    "corr_t", 0.0), 4),
            }
            for arm, _ in ARMS
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
