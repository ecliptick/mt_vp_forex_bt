"""DECISIVE TEST — TRUNCATION INVARIANCE.

If a backtest's result on bars [0, N) depends on data AFTER bar N, it is
using future information.

The old repo's `detect_fvg` runs a FORWARD walk over the whole dataset and
sets `zone.inverted` / `zone.superseded_bar` on each zone. The retest
scanner then uses those flags to decide WHICH zones are eligible. A zone
that will invert 10,000 bars later is already flagged when the scanner
runs on bar 100.

This script:
  1. Runs the old repo's SNIPER on the FULL 2025Q1 file.
  2. Runs it on the file TRUNCATED to the first 2,000,000 bars.
  3. Compares the trades that occur in bars [0, 2M) of each run.

If run 1 and run 2 disagree on the shared bars, the engine is reading the
future.
"""
import sys, time, json
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(r"C:\coding\ict_forex_research")))

from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest

CUT = 2_000_000
SRC = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
p = optimal_params(mode="sniper")

df_full = pd.read_parquet(SRC)
df_trunc = df_full.iloc[:CUT].copy()
print("full bars %d   truncated bars %d" % (len(df_full), len(df_trunc)))

out = {}

def run(tag, d):
    t0 = time.time()
    r = run_ict_backtest(d, p, strategy_label=tag)
    fee = sum(t.fee_usd for t in r.trades)
    gross = sum(t.pnl_usd for t in r.trades)
    print("%s: %.1fs  trades=%d gross=$%.2f fee=$%.2f NET=$%.2f"
          % (tag, time.time() - t0, len(r.trades), gross, fee, gross - fee))
    recs = []
    for t in r.trades:
        z = getattr(t, "fvg_zone", None)
        recs.append(dict(
            entry_bar=int(t.entry_bar), entry_ts=int(t.entry_time),
            direction=int(t.direction), entry_price=float(t.entry_price),
            exit_bar=int(t.exit_bar), exit_price=float(t.exit_price),
            pnl=float(t.pnl_usd), fee=float(t.fee_usd),
            exit_reason=str(t.exit_reason),
            inverted_bar=int(getattr(z, "inverted_bar", -1)) if z is not None else -1,
            zone_low=float(getattr(z, "zone_low", 0.0)) if z is not None else 0.0,
            zone_high=float(getattr(z, "zone_high", 0.0)) if z is not None else 0.0,
        ))
    return recs

out["full"] = run("FULL", df_full)
out["trunc"] = run("TRUNC", df_trunc)

with open(runtime_reports := Path(__file__).parent / "_reports" / "old_truncation_probe.json", "w",
           encoding="utf-8") as f:
    json.dump(out, f)

# ── COMPARE the shared window ─────────────────────────────────────────
full_shared = [r for r in out["full"] if r["entry_bar"] < CUT]
trunc_all = out["trunc"]

def key(r):
    return (r["entry_bar"], r["direction"], round(r["entry_price"], 4))

fk = {key(r) for r in full_shared}
tk = {key(r) for r in trunc_all}

print("\n" + "=" * 66)
print("TRUNCATION INVARIANCE over bars [0, %d)" % CUT)
print("=" * 66)
print("  full-run trades in window : %d" % len(full_shared))
print("  truncated-run trades      : %d" % len(trunc_all))
print("  IDENTICAL trade sets      : %s" % (fk == tk))
only_full = fk - tk
only_trunc = tk - fk
print("  only in FULL              : %d" % len(only_full))
print("  only in TRUNC             : %d" % len(only_trunc))
if only_full:
    print("    e.g.", list(only_full)[:5])
if only_trunc:
    print("    e.g.", list(only_trunc)[:5])

fg = sum(r["pnl"] for r in full_shared)
tg = sum(r["pnl"] for r in trunc_all)
print("\n  gross in window  FULL = $%.2f" % fg)
print("  gross in window TRUNC = $%.2f" % tg)
print("  DELTA                 = $%.2f" % (tg - fg))
