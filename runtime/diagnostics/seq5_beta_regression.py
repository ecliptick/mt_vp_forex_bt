"""MARKET-BETA regression for the whitelisted book's per-month gross.

WHY THIS MODULE EXISTS
======================

The engine-level walk-forward (``seq5_engine_walkforward.py``) found
that the LONG half of the 5-event whitelist generalises and the
SHORT half does not:

    long   in-sample +0.2233 g/leg  ->  OUT-OF-SAMPLE +0.2189 g/leg
    short  in-sample +0.0700 g/leg  ->  OUT-OF-SAMPLE -0.1803 g/leg

The tempting conclusion is "drop the short side and ship a long-only
book". That conclusion is DANGEROUS, and this module is the test
that decides it.

A long-only book is, by construction, a **directional bet on gold**.
The repo has already measured that ``trade_sides="long"`` raises the
book's market correlation to **+0.464** and rejected it for that
reason. A long-only WHITELIST might be different -- a 5-event filter
could be selecting *timing* within the up-moves rather than just
riding them -- but that has to be MEASURED, not assumed.

THE TEST
========

Regress each month's gross P&L on that month's market return:

    gross_month = alpha + beta * mkt_return_pct

  * **beta** -- dollars of gross earned per 1% of market move.
  * **alpha** -- the dollars earned in a month where the market goes
    NOWHERE. A timing strategy has a POSITIVE alpha; a directional
    proxy has ~0.
  * **r2** -- how much of the P&L variance the market direction
    explains.

The **drift/excess** decomposition (from the ledger) is the
per-leg version of the same question, and it is already in the
ledger: ``drift_usd`` is what any position of that size and side
would have earned by being there; ``excess_usd`` is the timing
component. If the whitelisted long book's gross is DRIFT and its
excess is NEGATIVE, then the whitelist has found a better place to
stand long -- it has NOT found timing skill, and the "edge" is the
market's drift.

The two tests are complementary: the regression asks whether the
P&L tracks the market across months; the decomposition asks WHY.

Usage
-----
    python -m runtime.diagnostics.seq5_beta_regression
"""
from __future__ import annotations

import argparse
import json
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


def _monthly(legs: dict[str, list]) -> pd.DataFrame:
    """Aggregate the legs to one row per calendar month."""
    st = np.asarray(legs["signal_time_ns"], dtype=np.int64)
    gross = np.asarray(legs["gross"], dtype=np.float64)
    side = np.asarray(legs["side"], dtype=np.int64)
    ts = pd.Series(pd.to_datetime(st, unit="ns", utc=True))
    df = pd.DataFrame({
        "month": ts.dt.strftime("%Y-%m"),
        "gross": gross,
        "side": side,
    })
    out = df.groupby("month").agg(
        gross=("gross", "sum"),
        legs=("gross", "size"),
    ).reset_index()
    return out


def _market_monthly(start: str, end: str) -> pd.DataFrame:
    """The market's return per calendar month over the same window."""
    cache = OUT_DIR / "_market_monthly.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        df["mkt_ret_pct"] = df["mkt_ret_pct"].astype(float)
        return df

    from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars

    bars = load_bars(DEFAULT_DATA, start, end)
    ts = pd.Series(pd.to_datetime(bars["time"], unit="ms", utc=True))
    close = bars["close"].to_numpy(dtype=np.float64)
    df = pd.DataFrame({
        "month": ts.dt.strftime("%Y-%m"),
        "close": close,
    })
    df = df.groupby("month")["close"].agg(["first", "last"]).reset_index()
    df["mkt_ret_pct"] = (df["last"] / df["first"] - 1.0) * 100.0
    cache.parent.mkdir(parents=True, exist_ok=True)
    df[["month", "mkt_ret_pct"]].to_csv(cache, index=False)
    return df[["month", "mkt_ret_pct"]]


def _regress(mkt: np.ndarray, gross: np.ndarray) -> dict[str, float]:
    if mkt.size < 3 or np.std(mkt) <= 0:
        return {}
    slope, intercept = np.polyfit(mkt, gross, 1)
    pred = slope * mkt + intercept
    ss_res = float(np.sum((gross - pred) ** 2))
    ss_tot = float(np.sum((gross - gross.mean()) ** 2))
    return {
        "n_months": int(mkt.size),
        "beta_usd_per_pct": float(slope),
        "alpha_usd_per_month": float(intercept),
        "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0,
        "corr": float(np.corrcoef(mkt, gross)[0, 1]) if np.std(gross) > 0 else 0.0,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--out", type=Path,
                    default=OUT_DIR / "seq5_beta_regression.json")
    args = ap.parse_args()

    # The market's monthly return, once.
    mkt = _market_monthly("2024-01-01", "2026-07-16")
    mkt_map = dict(zip(mkt["month"], mkt["mkt_ret_pct"]))

    report: dict[str, Any] = {"arms": {}}
    print("=" * 88)
    print("MARKET-BETA REGRESSION -- is the whitelisted book a DIRECTIONAL PROXY?")
    print("=" * 88)
    print()
    print(f"  {'arm':<18} {'n mo':>5} {'beta $/pct':>11} {'alpha $/mo':>11} "
          f"{'r2':>7} {'corr':>7}  verdict")
    print("  " + "-" * 84)

    for arm, label in ARMS:
        payload = _load_ledger(arm)
        mo = _monthly(payload["legs"])
        mo["mkt_ret_pct"] = mo["month"].map(mkt_map)
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
                "excess_t": _t(excess.mean() if False else excess),
            },
            "long": _leg_block(long_m),
            "short": _leg_block(short_m),
        }
        report["arms"][arm] = block

        if reg:
            verdict = ("PROXY" if reg["alpha_usd_per_month"] <= 0
                       else "timing")
        else:
            verdict = "n/a"
        print(f"  {arm:<18} {reg.get('n_months', 0):>5} "
              f"{reg.get('beta_usd_per_pct', 0):>+11,.1f} "
              f"{reg.get('alpha_usd_per_month', 0):>+11,.1f} "
              f"{reg.get('r2', 0):>7.3f} {reg.get('corr', 0):>+7.3f}  {verdict}")

    print()
    print("  -- the per-leg decomposition (why) --")
    print()
    print(f"  {'arm':<18} {'side':<6} {'legs':>7} {'g/leg':>8} {'drift/leg':>10} "
          f"{'drift t':>8} {'excess/leg':>11} {'excess t':>9}")
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
            "regression": "monthly gross ~ alpha + beta * market return %",
        },
        summary={
            arm: {
                "beta": round(report["arms"][arm]["regression"].get(
                    "beta_usd_per_pct", 0.0), 2),
                "alpha": round(report["arms"][arm]["regression"].get(
                    "alpha_usd_per_month", 0.0), 2),
                "r2": round(report["arms"][arm]["regression"].get(
                    "r2", 0.0), 4),
                "corr": round(report["arms"][arm]["regression"].get(
                    "corr", 0.0), 4),
            }
            for arm, _ in ARMS
        },
        payload=report,
    )


if __name__ == "__main__":
    main()
