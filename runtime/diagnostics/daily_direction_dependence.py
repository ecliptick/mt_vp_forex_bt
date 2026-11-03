"""DAILY-FREQUENCY direction-dependence — the replacement for the
monthly market-beta regression.

WHY THIS MODULE EXISTS
======================

The repo used to measure "is the book a directional proxy?" by
regressing **MONTHLY** gross P&L on the **MONTHLY** market return
(``seq5_beta_regression.py``, ``trail_sides_ledger_verdict.py``).
That measurement is WRONG for this strategy, for three independent
reasons:

1. **The sample size is tiny.** The corpus is ~31 months, so the
   regression has n = 13..31 points. The standard error of a
   correlation at n = 13 is ``1/sqrt(n-3) = 0.32`` in Fisher-z
   space, so ``r = +0.459`` (the long-only book's H2 number) is only
   **t = 1.57** -- NOT distinguishable from zero. Every "market
   correlation" number the repo quoted was inside its own noise band.

2. **Monthly aggregation destroys the strategy's own time scale.**
   This is a high-frequency book (~7-40 legs a DAY). A month's gross
   sums thousands of legs whose entry and exit are spread over
   ~30 distinct trading days; the month's return is ONE number. The
   regression is therefore asking "did the book happen to sit on the
   right side of a 30-day trend", which is a statement about the
   BOOK'S COMPOSITION, not about whether the entries have skill.

3. **The month boundary is an ENGINE ARTEFACT.** The p95 cap (7d),
   the swing cap (24h) and the VP profile (7d rolling) are all
   history-dependent, so a cold monthly chunk trades its first ~8
   days with UNCAPPED targets (AGENTS.md § 1.1). The monthly
   aggregation inherits that artefact.

The REPLACEMENT is the **DAILY** aggregation: n ~= 900 points over
the corpus instead of 13-31, each point covering the strategy's
ACTUAL trading horizon, with no month boundary anywhere in the
pipeline.

WHAT THIS MODULE MEASURES
=========================

For each book, over ONE continuous execution (no chunking):

* ``daily_gross``   -- the book's gross, bucketed by SIGNAL day.
* ``mkt_ret_pct``   -- the market's close-to-close return over the
  SAME calendar day.
* ``beta`` / ``alpha`` / ``r2`` / ``corr`` -- the regression of
  daily gross on the daily market return.
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
question, and — unlike the monthly one — its standard error is small
enough to distinguish a real directional book from noise.

WHAT THIS MODULE IS NOT
=======================

It is NOT a placebo, and it does NOT rewrite any existing artifact.
It reads the ledgers that already exist and writes its OWN report.

Usage
-----
    python -m runtime.diagnostics.daily_direction_dependence
    python -m runtime.diagnostics.daily_direction_dependence --json-only
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

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    LEDGER_PATH,
    ledger_arrays,
    load_ledger,
)
from runtime.results import registry  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")
REPORT_PATH = OUT_DIR / "daily_direction_dependence.json"

CORPUS_START = "2024-01-01"
CORPUS_END = "2026-07-16"

# The books this module reads. Each is a ledger ALREADY ON DISK --
# this module never re-runs the engine.
#
# ``leg_ledger_seqwlf_*.json`` are the engine-level walk-forward
# arms; the canonical / trail-short / trail-off books are the ones the
# trail-sides promotion rests on.
BOOKS: tuple[tuple[str, str], ...] = (
    ("canonical", str(LEDGER_PATH)),
    ("trail_short", str(OUT_DIR / "leg_ledger_trail_short.json")),
    ("trail_off", str(OUT_DIR / "leg_ledger_trail_off.json")),
    # The engine-level walk-forward arms (§ 3.10.0). Each is a ledger
    # ALREADY on disk; this module never re-runs the engine.
    ("seqwlf_unfiltered_H1", str(OUT_DIR / "leg_ledger_seqwlf_unfiltered_H1.json")),
    ("seqwlf_unfiltered_H2", str(OUT_DIR / "leg_ledger_seqwlf_unfiltered_H2.json")),
    ("seqwlf_shipped_H1", str(OUT_DIR / "leg_ledger_seqwlf_shipped_on_H1.json")),
    ("seqwlf_shipped_H2", str(OUT_DIR / "leg_ledger_seqwlf_shipped_on_H2.json")),
    ("seqwlf_longonly_H1", str(OUT_DIR / "leg_ledger_seqwlf_longonly_on_H1.json")),
    ("seqwlf_longonly_H2", str(OUT_DIR / "leg_ledger_seqwlf_longonly_on_H2.json")),
    # The two knobs AGENTS.md § 6 lists as NOT PROMOTED. Their old
    # justifications cited a MONTHLY market correlation (n = 13..31);
    # these ledgers let the DAILY test re-ground them.
    ("trade_sides_long", str(OUT_DIR / "leg_ledger_trade_sides_long.json")),
    ("structure_gate_on", str(OUT_DIR / "leg_ledger_structure_gate_on.json")),
)


# ─────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────
def _t(x: np.ndarray) -> float:
    """t-statistic of the mean of ``x`` against zero."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < 2:
        return 0.0
    sd = x.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return 0.0
    return float(x.mean() / (sd / np.sqrt(x.size)))


def _fisher_t(r: float, n: int) -> float:
    """t-statistic of a correlation coefficient against zero.

    Fisher's z transform: ``z = atanh(r)`` with SE ``1/sqrt(n-3)``.
    This is the number that says whether a quoted correlation is
    distinguishable from zero — the number the monthly regression
    never reported.
    """
    if n < 4 or not math.isfinite(r) or abs(r) >= 1.0:
        return 0.0
    z = math.atanh(r)
    return float(z / (1.0 / math.sqrt(n - 3)))


def _regress(mkt: np.ndarray, gross: np.ndarray) -> dict[str, float]:
    """Regress ``gross`` on ``mkt``; report beta/alpha/r2/corr + the t."""
    mkt = np.asarray(mkt, dtype=np.float64)
    gross = np.asarray(gross, dtype=np.float64)
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


# ─────────────────────────────────────────────────────────────────────
# The market's daily return
# ─────────────────────────────────────────────────────────────────────
def _market_daily(start: str, end: str) -> pd.DataFrame:
    """The market's close-to-close return per calendar DAY.

    Cached on disk next to the other report intermediates.
    """
    cache = OUT_DIR / "_market_daily.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        df["mkt_ret_pct"] = df["mkt_ret_pct"].astype(float)
        return df

    from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars

    bars = load_bars(DEFAULT_DATA, start, end)
    ts = pd.Series(pd.to_datetime(bars["time"], unit="ms", utc=True))
    close = bars["close"].to_numpy(dtype=np.float64)
    df = pd.DataFrame({"day": ts.dt.strftime("%Y-%m-%d"), "close": close})
    df = df.groupby("day")["close"].agg(["first", "last"]).reset_index()
    df["mkt_ret_pct"] = (df["last"] / df["first"] - 1.0) * 100.0
    cache.parent.mkdir(parents=True, exist_ok=True)
    df[["day", "mkt_ret_pct"]].to_csv(cache, index=False)
    return df[["day", "mkt_ret_pct"]]


# ─────────────────────────────────────────────────────────────────────
# The per-book analysis
# ─────────────────────────────────────────────────────────────────────
def _daily_buckets(payload: dict[str, Any]) -> pd.DataFrame:
    """The book's gross, bucketed by the leg's SIGNAL day."""
    arr = ledger_arrays(payload)
    ts = arr["signal_time_ns"]
    days = pd.to_datetime(ts, unit="ns", utc=True).strftime("%Y-%m-%d")
    gross = arr["gross"]
    drift = arr["drift_usd"]
    excess = arr["excess_usd"]
    df = pd.DataFrame({
        "day": days,
        "gross": gross,
        "drift": drift,
        "excess": excess,
        "side": arr["side"],
    })
    return df


def analyse_book(payload: dict[str, Any], mkt_map: dict[str, float],
                 ) -> dict[str, Any]:
    """The daily-aggregation view AND the per-leg decomposition."""
    df = _daily_buckets(payload)

    # ── the per-leg decomposition (the exact, primary number) ──
    legs = df
    leg_excess = legs["excess"].to_numpy(dtype=np.float64)
    leg_drift = legs["drift"].to_numpy(dtype=np.float64)
    leg_gross = legs["gross"].to_numpy(dtype=np.float64)
    long_m = legs["side"].to_numpy(dtype=np.int64) > 0
    short_m = ~long_m

    def _leg_block(m: np.ndarray) -> dict[str, Any]:
        if not m.any():
            return {"legs": 0}
        return {
            "legs": int(m.sum()),
            "gross_per_leg": float(leg_gross[m].mean()),
            "drift_per_leg": float(leg_drift[m].mean()),
            "drift_t": _t(leg_drift[m]),
            "excess_per_leg": float(leg_excess[m].mean()),
            "excess_t": _t(leg_excess[m]),
        }

    # ── the daily aggregation (the replacement for the monthly one) ──
    daily = df.groupby("day").agg(
        gross=("gross", "sum"),
        legs=("gross", "size"),
    ).reset_index()
    daily["mkt_ret_pct"] = daily["day"].map(mkt_map)
    daily = daily.dropna(subset=["mkt_ret_pct"])

    reg = _regress(daily["mkt_ret_pct"].to_numpy(dtype=float),
                   daily["gross"].to_numpy(dtype=float))

    return {
        "n_legs": int(legs.shape[0]),
        "n_days": int(daily.shape[0]),
        "daily_regression": reg,
        "per_leg": {
            "gross_per_leg": float(leg_gross.mean()) if leg_gross.size else 0.0,
            "drift_per_leg": float(leg_drift.mean()) if leg_drift.size else 0.0,
            "drift_t": _t(leg_drift),
            "excess_per_leg": float(leg_excess.mean()) if leg_excess.size else 0.0,
            "excess_t": _t(leg_excess),
        },
        "per_leg_long": _leg_block(long_m),
        "per_leg_short": _leg_block(short_m),
        # The verdict reads the PER-LEG number. The daily regression is
        # the aggregate view of the same question.
        "verdict": (
            "TIMING" if (leg_excess.size and leg_excess.mean() > 0
                         and _t(leg_excess) > 2.0)
            else "MARKET-DRIVEN"
        ),
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", type=str, default=CORPUS_START)
    ap.add_argument("--end", type=str, default=CORPUS_END)
    ap.add_argument("--out", type=Path, default=REPORT_PATH)
    ap.add_argument("--json-only", action="store_true")
    args = ap.parse_args()

    mkt = _market_daily(args.start, args.end)
    mkt_map = dict(zip(mkt["day"], mkt["mkt_ret_pct"]))

    report: dict[str, Any] = {
        "window": {"start": args.start, "end": args.end},
        "aggregation": "daily",
        "why_not_monthly": (
            "n=13..31 monthly points give a correlation standard error "
            "of ~0.32 in Fisher-z space, so the repo's quoted monthly "
            "market correlations (r=+0.459, +0.337) were inside their own "
            "noise band (t=1.57, 1.86). A high-frequency book must be "
            "aggregated at the frequency it trades at."
        ),
        "books": {},
    }

    for label, path_str in BOOKS:
        path = Path(path_str)
        if not path.exists():
            continue
        payload = load_ledger(path)
        report["books"][label] = analyse_book(payload, mkt_map)

    if not args.json_only:
        print("=" * 92)
        print("DAILY-FREQUENCY DIRECTION DEPENDENCE "
              "(the replacement for the monthly beta regression)")
        print("=" * 92)
        print()
        print(f"  {'book':<16} {'legs':>7} {'days':>6} {'corr':>8} {'corr t':>8} "
              f"{'r2':>7} {'excess/leg':>12} {'excess t':>10}  verdict")
        print("  " + "-" * 88)
        for label, blk in report["books"].items():
            reg = blk["daily_regression"]
            pl = blk["per_leg"]
            print(f"  {label:<16} {blk['n_legs']:>7} {blk['n_days']:>6} "
                  f"{reg.get('corr', 0.0):>+8.3f} {reg.get('corr_t', 0.0):>+8.2f} "
                  f"{reg.get('r2', 0.0):>7.3f} {pl['excess_per_leg']:>+12.4f} "
                  f"{pl['excess_t']:>+10.2f}  {blk['verdict']}")
        print()
        print("  The VERDICT column reads the PER-LEG excess (n = thousands "
              "of legs).")
        print("  The daily correlation is the aggregate view of the same "
              "question, now")
        print("  with ~900 points instead of 13-31.")
        print()

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                         encoding="utf-8")
    print(f"-> {args.out}")

    registry.record_run(
        kind="daily_direction_dependence",
        params={
            "aggregation": "daily",
            "window": f"{args.start}..{args.end}",
            "books": [label for label, _ in BOOKS],
            "regression": "daily gross ~ alpha + beta * daily market return %",
        },
        summary={
            label: {
                "n_legs": blk["n_legs"],
                "n_days": blk["n_days"],
                "corr": round(blk["daily_regression"].get("corr", 0.0), 4),
                "corr_t": round(blk["daily_regression"].get("corr_t", 0.0), 4),
                "r2": round(blk["daily_regression"].get("r2", 0.0), 4),
                "excess_per_leg": round(blk["per_leg"]["excess_per_leg"], 6),
                "excess_t": round(blk["per_leg"]["excess_t"], 4),
                "verdict": blk["verdict"],
            }
            for label, blk in report["books"].items()
        },
        payload=report,
    )
    print("registry: recorded run 'daily_direction_dependence'")


if __name__ == "__main__":
    main()
