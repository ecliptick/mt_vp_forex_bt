"""PROBE: does the old repo's SNIPER fire at the zone's real inverted_bar,
or at a bar chosen with knowledge of a FUTURE inversion?

`zone.inverted` is a globally precomputed flag set by a forward walk over
the WHOLE dataset. If the sniper queue checks that flag on the bar after
the signal, the trade is selected using information that only exists later.
"""
import sys, time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(r"C:\coding\ict_forex_research")))

from src.core.optimal_config import optimal_params
from src.core.ict_signals import detect_fvg, generate_ict_pending_signals

p = optimal_params(mode="sniper")

df = pd.read_parquet(Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet"))
ts = df["time"].astype("int64").to_numpy()
if ts.max() < 1e15:
    ts = ts * 1_000_000
o = df["open"].to_numpy(float); h = df["high"].to_numpy(float)
l = df["low"].to_numpy(float); c = df["close"].to_numpy(float)
print("bars", len(df))

t0 = time.time()
zones = detect_fvg(
    o, h, l, c,
    resample_to_n_secs=p.fvg_resample_secs,
    fvg_min_zone_usd=p.fvg_min_zone_usd,
    max_active_zones=int(getattr(p, "fvg_max_active_zones", 0)),
    max_zone_age_bars=int(getattr(p, "fvg_max_zone_age_bars", 0)),
    supersede_on_new=bool(p.fvg_supersede_on_new),
    invalidation_min_consecutive_bars=int(p.fvg_invalidation_min_consecutive_bars),
    body_only_mitigation=bool(getattr(p, "fvg_body_only_mitigation", False)),
    body_only_invalidation=bool(getattr(p, "fvg_body_only_inversion", False)),
    fvg_min_lifetime_secs=int(p.fvg_min_lifetime_secs),
    times_utc_ns=ts,
)
print("zones %.1fs n=%d" % (time.time() - t0, len(zones)))
print("inverted=%d superseded=%d" % (
    sum(1 for z in zones if z.inverted),
    sum(1 for z in zones if z.superseded_bar >= 0)))

t0 = time.time()
sigs = generate_ict_pending_signals(
    zones, o, h, l, c, ts, params=p, structure=None,
)
print("signals %.1fs n=%d" % (time.time() - t0, len(sigs)))

import collections
print("sources:", dict(collections.Counter(
    str(getattr(s, "triggered_by", "")) for s in sigs)))

rows = []
for s in sigs:
    z = getattr(s, "fvg_zone", None)
    if z is None:
        continue
    rows.append((int(s.trigger_bar), int(getattr(z, "inverted_bar", -1)),
                 int(getattr(z, "trigger_bar", -1)),
                 str(getattr(s, "triggered_by", ""))))

for src in ("fvg", "ifvg"):
    pre = [r for r in rows if r[3] == src and r[1] >= 0]
    if not pre:
        print("%s: no signals with an inverted zone" % src)
        continue
    lead = np.array([r[1] - r[0] for r in pre])
    print("\n%s-path signals whose zone inverted: n=%d" % (src, len(pre)))
    print("  inverted_bar - signal_bar: min %d p25 %d median %d p75 %d max %d"
          % (lead.min(), np.percentile(lead, 25), np.median(lead),
             np.percentile(lead, 75), lead.max()))
    print("  %% inverted AFTER the signal bar: %.1f%%" % (100.0 * float((lead > 0).mean())))
    print("  %% inverted >1800 bars later (sniper_max_age): %.1f%%"
          % (100.0 * float((lead > 1800).mean())))
    print("  %% inverted >1 day later: %.1f%%" % (100.0 * float((lead > 86400).mean())))
    # THE LOOK-AHEAD: the sniper fires when the flag is seen. If the flag
    # is set at inverted_bar but the sniper only checks it on bars after
    # signal_bar, a signal whose inversion is 10000 bars later is still
    # "live" at signal_bar+1 -- so the queue would either fire it early
    # (look-ahead) or wait forever.
    print("  %% inverted BEFORE the signal bar (already true): %.1f%%"
          % (100.0 * float((lead <= 0).mean())))
