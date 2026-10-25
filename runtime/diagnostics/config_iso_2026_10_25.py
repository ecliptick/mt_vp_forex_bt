"""Isolation A/B for the 2026-10-25 config change.

Answers: what does each requested change contribute ON ITS OWN?

    sl_max 2.50 -> 8.00   (with sl_min held at 2.50)
    contra_close OFF -> ON at $5.00
    vol gate 3.0 -> 1.0   (the trade-count target)

Runs the canonical continuous corpus (14-day warmup prefix) for each
cell and reports legs, trades/calendar-day, gross, fees, net and
gross/leg.

This is a MEASUREMENT harness, not a sweep: one continuous window, the
unit of evaluation is the leg.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pandas as pd

from runtime.diagnostics.leg_ledger import build_ledger

OUT_DIR = Path("runtime/diagnostics/_reports")

CELLS: list[tuple[str, dict]] = [
    # name, engine overrides
    ("base_canonical", {}),
    ("sl_max_8", {"sl_min_usd": 2.5, "sl_max_usd": 8.0}),
    ("contra_5", {"contra_close": True, "contra_close_spread_usd": 5.0}),
    ("sl_max_8_contra_5", {
        "sl_min_usd": 2.5, "sl_max_usd": 8.0,
        "contra_close": True, "contra_close_spread_usd": 5.0,
    }),
    ("sl_max_8_contra_5_gate_1", {
        "sl_min_usd": 2.5, "sl_max_usd": 8.0,
        "contra_close": True, "contra_close_spread_usd": 5.0,
        "vol_state_min_range_norm": 1.0,
    }),
]


def summarise(payload: dict) -> dict:
    legs = payload["legs"]
    st = np.array(legs["signal_time_ns"], dtype=np.int64)
    gross = np.array(legs["gross"], dtype=np.float64)
    fee = np.array(legs["fee"], dtype=np.float64)
    n = len(st)

    days = pd.to_datetime(st, unit="ns", utc=True).normalize()
    n_signal_days = int(pd.Series(days).nunique())
    cal = (pd.Timestamp(payload["score_end"], tz="UTC")
           - pd.Timestamp(payload["score_start"], tz="UTC")).days

    return {
        "legs": n,
        "signal_days": n_signal_days,
        "calendar_days": cal,
        "legs_per_calendar_day": n / cal if cal else 0.0,
        "legs_per_signal_day": n / n_signal_days if n_signal_days else 0.0,
        "gross": float(gross.sum()),
        "fees": float(fee.sum()),
        "net": float(gross.sum() - fee.sum()),
        "gross_per_leg": float(gross.mean()) if n else 0.0,
        "fee_per_leg": float(fee.mean()) if n else 0.0,
        "engine_overrides": payload.get("engine_overrides", {}),
    }


def main() -> None:
    rows = []
    for name, ov in CELLS:
        out = OUT_DIR / f"leg_ledger_iso_{name}.json"
        payload = build_ledger(engine_overrides=ov, out_path=out,
                               refresh=True, verbose=False)
        row = summarise(payload)
        row["cell"] = name
        rows.append(row)
        print(f"  {name:<28} legs={row['legs']:>7}  "
              f"/day={row['legs_per_calendar_day']:>7.2f}  "
              f"net=${row['net']:>11,.2f}  g/leg=${row['gross_per_leg']:>7.4f}",
              flush=True)

    print()
    hdr = (f"{'cell':<28}{'legs':>8}{'/day':>8}{'/sig-day':>10}"
           f"{'gross':>12}{'fees':>12}{'net':>13}{'g/leg':>9}")
    print(hdr)
    print("-" * len(hdr))
    for r in rows:
        print(f"{r['cell']:<28}{r['legs']:>8}"
              f"{r['legs_per_calendar_day']:>8.2f}"
              f"{r['legs_per_signal_day']:>10.2f}"
              f"{r['gross']:>12,.2f}{r['fees']:>12,.2f}{r['net']:>13,.2f}"
              f"{r['gross_per_leg']:>9.4f}")

    summary = OUT_DIR / "config_iso_2026_10_25.json"
    summary.write_text(json.dumps({"rows": rows}, indent=2, default=float),
                       encoding="utf-8")
    print(f"\n-> {summary}")


if __name__ == "__main__":
    main()
