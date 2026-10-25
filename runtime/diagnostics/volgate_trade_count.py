"""Find the vol-gate threshold that hits the 30-70 trades/day target.

Runs the CANONICAL continuous-corpus ledger (14-day warmup prefix, one
continuous execution) at several ``vol_state_min_range_norm`` values,
with the requested SL / contra-close settings, and reports the trade
count per calendar day and per trading day.

Writes one ledger per threshold so each is auditable, and prints a
summary table. This is a MEASUREMENT harness, not a sweep: the unit is
the leg, over one continuous window.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from runtime.diagnostics.leg_ledger import build_ledger

OUT_DIR = Path("runtime/diagnostics/_reports")

# The requested config (2026-10-25, user instruction).
BASE_OVERRIDES = {
    "sl_min_usd": 2.5,
    "sl_max_usd": 8.0,
    "contra_close": True,
    "contra_close_spread_usd": 5.0,
}

THRESHOLDS = [0.0, 0.5, 1.0, 1.25, 1.5, 2.0]


def summarise(payload: dict) -> dict:
    legs = payload["legs"]
    st = np.array(legs["signal_time_ns"], dtype=np.int64)
    gross = np.array(legs["gross"], dtype=np.float64)
    fee = np.array(legs["fee"], dtype=np.float64)
    n = len(st)

    days = pd.to_datetime(st, unit="ns", utc=True).normalize()
    n_signal_days = int(pd.Series(days).nunique())

    score_start = pd.Timestamp(payload["score_start"], tz="UTC")
    score_end = pd.Timestamp(payload["score_end"], tz="UTC")
    calendar_days = (score_end - score_start).days

    return {
        "threshold": float(payload["engine_overrides"]
                            ["vol_state_min_range_norm"]
                            .split("=")[-1].strip("'")),
        "legs": n,
        "signal_days": n_signal_days,
        "calendar_days": calendar_days,
        "legs_per_calendar_day": n / calendar_days if calendar_days else 0.0,
        "legs_per_signal_day": n / n_signal_days if n_signal_days else 0.0,
        "gross": float(gross.sum()),
        "fees": float(fee.sum()),
        "net": float(gross.sum() - fee.sum()),
        "gross_per_leg": float(gross.mean()) if n else 0.0,
    }


def main() -> None:
    rows = []
    for thr in THRESHOLDS:
        ov = dict(BASE_OVERRIDES)
        ov["vol_state_min_range_norm"] = thr
        out = OUT_DIR / f"leg_ledger_volgate_{thr:g}.json"
        payload = build_ledger(engine_overrides=ov, out_path=out,
                               refresh=True, verbose=False)
        row = summarise(payload)
        rows.append(row)
        print(f"  thr={thr:<5g} legs={row['legs']:>6}  "
              f"/cal-day={row['legs_per_calendar_day']:>6.2f}  "
              f"/signal-day={row['legs_per_signal_day']:>6.2f}  "
              f"net=${row['net']:>10,.2f}  g/leg=${row['gross_per_leg']:>7.4f}",
              flush=True)

    print()
    hdr = (f"{'thr':>6}{'legs':>8}{'/cal-day':>10}{'/sig-day':>10}"
           f"{'gross':>12}{'fees':>12}{'net':>12}{'g/leg':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['threshold']:>6g}{r['legs']:>8}"
              f"{r['legs_per_calendar_day']:>10.2f}"
              f"{r['legs_per_signal_day']:>10.2f}"
              f"{r['gross']:>12,.2f}{r['fees']:>12,.2f}{r['net']:>12,.2f}"
              f"{r['gross_per_leg']:>9.4f}")

    summary = OUT_DIR / "volgate_trade_count_target.json"
    summary.write_text(json.dumps({"rows": rows}, indent=2, default=float),
                       encoding="utf-8")
    print(f"\n-> {summary}")


if __name__ == "__main__":
    main()
