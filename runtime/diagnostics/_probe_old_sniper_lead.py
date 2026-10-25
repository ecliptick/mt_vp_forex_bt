"""DECISIVE: in the OLD repo, when does a SNIPER trade actually enter,
relative to the bar at which its zone truly inverted?

`zone.inverted` is a flag computed by a forward walk over the WHOLE
dataset. The sniper queue checks that flag on EVERY bar. If the flag is
already True on the bar after the signal (because the zone will invert
10000 bars later), the trade is selected with knowledge of the future.
"""
import sys, time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(r"C:\coding\ict_forex_research")))

from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest

p = optimal_params(mode="sniper")
df = pd.read_parquet(Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet"))

t0 = time.time()
res = run_ict_backtest(df, p, strategy_label="sniper_v22")
print("backtest %.1fs" % (time.time() - t0))

trades = res.trades
print("trades:", len(trades))
fee = sum(t.fee_usd for t in trades)
gross = sum(t.pnl_usd for t in trades)
print("gross $%.2f  fee $%.2f  NET $%.2f" % (gross, fee, gross - fee))

import collections
print("by source:", dict(collections.Counter(t.entry_triggered_by for t in trades)))
print("by exit:", dict(collections.Counter(t.exit_reason for t in trades)))

# ── THE MEASUREMENT ────────────────────────────────────────────────────
# For each sniper trade: lead = zone.inverted_bar - trade.entry_bar.
# lead > 0  => the trade entered BEFORE the zone actually inverted
#             => the entry used FUTURE information.
lead = []
for t in trades:
    z = getattr(t, "fvg_zone", None)
    if z is None:
        continue
    ib = int(getattr(z, "inverted_bar", -1))
    if ib < 0:
        continue
    lead.append(ib - int(t.entry_bar))

lead = np.array(lead)
print("\nsniper trades with a known inverted_bar: %d" % len(lead))
if len(lead):
    print("  inverted_bar - entry_bar: min %d p10 %d median %d p90 %d max %d"
          % (lead.min(), np.percentile(lead, 10), np.median(lead),
             np.percentile(lead, 90), lead.max()))
    n_future = int((lead > 0).sum())
    print("  entered BEFORE the zone inverted (LOOK-AHEAD): %d / %d = %.1f%%"
          % (n_future, len(lead), 100.0 * n_future / len(lead)))
    print("  entered AFTER the zone inverted (honest): %d / %d = %.1f%%"
          % (len(lead) - n_future, len(lead),
             100.0 * (len(lead) - n_future) / len(lead)))
    fut = lead[lead > 0]
    if len(fut):
        print("  median look-ahead distance: %.0f bars (%.1f hours)"
              % (np.median(fut), np.median(fut) / 3600.0))
        print("  max look-ahead: %d bars (%.2f days)"
              % (fut.max(), fut.max() / 86400.0))

    # P&L split: does the look-ahead leg carry the profit?
    fut_set = set()
    k = 0
    for t in trades:
        z = getattr(t, "fvg_zone", None)
        if z is None:
            continue
        ib = int(getattr(z, "inverted_bar", -1))
        if ib < 0:
            continue
        fut_set.add(ib - int(t.entry_bar) > 0)
        k += 1
    la_pnl = [t.pnl_usd for t in trades
              if getattr(t, "fvg_zone", None) is not None
              and int(getattr(t.fvg_zone, "inverted_bar", -1)) >= 0
              and int(getattr(t.fvg_zone, "inverted_bar", -1)) - int(t.entry_bar) > 0]
    ho_pnl = [t.pnl_usd for t in trades
              if getattr(t, "fvg_zone", None) is not None
              and int(getattr(t.fvg_zone, "inverted_bar", -1)) >= 0
              and int(getattr(t.fvg_zone, "inverted_bar", -1)) - int(t.entry_bar) <= 0]
    print("  P&L of LOOK-AHEAD legs: n=%d gross $%.2f (%.1f%% of trades, %.1f%% of gross)"
          % (len(la_pnl), sum(la_pnl),
             100.0 * len(la_pnl) / max(1, len(trades)),
             100.0 * sum(la_pnl) / gross if gross else 0.0))
    print("  P&L of HONEST legs:       n=%d gross $%.2f"
          % (len(ho_pnl), sum(ho_pnl)))
