"""Measure the DUAL-MODE look-ahead in the shipped engine.

THE BUG
-------
``bt/_engine/vp_multi_target_engine.py``, the "new zone triggers"
(immediate co-fire) step::

    if entry_mode == "dual" and int(zone.inverted_bar) >= 0:
        res.n_skipped_dual_inverted += 1
        continue

The canonical recipe runs ``entry_mode == "dual"``.  The intent is
correct: a zone that has ALREADY inverted belongs to the sniper path,
so the immediate path must not double-fire it.

But ``zone.inverted_bar`` is a **whole-file verdict**.  The engine
computes it over the ENTIRE dataset and then reads it at the zone's
``trigger_bar``.  If the zone inverted at bar J, the immediate path
skips the zone -- even when J is FAR in the future of the trigger bar.

At the trigger bar, a live engine does not yet know that the zone will
invert at bar J.  So the immediate book is a **selection on the
outcome**: zones that eventually invert are silently removed from it.

Compare with the sniper path, which DOES restore causality::

    zones_inverted_at_bar.setdefault(inv_b, []).append(z)
    # gated by: not _zone_dead_by(z, inv_b)

i.e. the sniper path keys on the bar the zone fires at and checks
death causally.  The immediate path has no such check.

WHAT THIS PROBE MEASURES
------------------------
For every zone the engine buckets as a trigger-bar zone, classify it:

  * ``inverted_bar < 0``            -> kept (never inverted)  OK
  * ``0 <= inverted_bar <= trig``   -> skipped, CORRECT (it had
    already inverted by the time it fired)
  * ``inverted_bar > trig``         -> skipped, **LOOK-AHEAD**.  The
    inversion is in the future of the trigger bar.

The third bucket is the bug.  Report its size, and the share of the
whole trigger-bar population it represents.

Run:
    python -m runtime.diagnostics.probe_dual_mode_lookahead
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
START = "2025-01-01"
END = "2025-02-01"
OUT = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
       / "dual_mode_lookahead.json")


def main() -> None:
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    scope = "XAUUSD_S1_1y__lookahead"
    print(f"[probe] loading bars {START} -> {END} ...", flush=True)
    bars = load_bars(DATA, START, END)
    art = ArtifactCache(DATA, scope, START, END).build_or_load(bars)
    zones = art["zones"]
    print(f"[probe] bars={len(bars)} zones={len(zones)}", flush=True)

    n = len(bars)

    # Mirror the engine's bucketing exactly (vp_multi_target_engine.py
    # ~line 2592).  Note the engine does NOT filter trigger zones by
    # ``live`` -- only the sniper bucket is gated on _zone_dead_by.
    trigger_zones = [z for z in zones if 0 <= int(z.trigger_bar) < n]

    kept = 0            # inverted_bar < 0
    skipped_correct = 0  # 0 <= inverted_bar <= trigger_bar
    skipped_lookahead = 0  # inverted_bar > trigger_bar

    leads = []
    for z in trigger_zones:
        ib = int(getattr(z, "inverted_bar", -1))
        tb = int(z.trigger_bar)
        if ib < 0:
            kept += 1
        elif ib <= tb:
            skipped_correct += 1
        else:
            skipped_lookahead += 1
            leads.append(ib - tb)

    leads_arr = np.array(leads, dtype=np.int64) if leads else np.zeros(0, dtype=np.int64)
    n_trig = len(trigger_zones)

    out = {
        "scope": scope,
        "start": START,
        "end": END,
        "bars": int(n),
        "n_zones": int(len(zones)),
        "n_trigger_zones": n_trig,
        "kept_never_inverted": kept,
        "skipped_already_inverted_correct": skipped_correct,
        "LOOKAHEAD_skipped_future_inversion": skipped_lookahead,
        "lookahead_share_of_trigger_zones": round(skipped_lookahead / max(1, n_trig), 4),
        "lookahead_share_of_all_skips": round(
            skipped_lookahead / max(1, skipped_lookahead + skipped_correct), 4),
        "lead_min": int(leads_arr.min()) if leads_arr.size else None,
        "lead_p25": float(np.percentile(leads_arr, 25)) if leads_arr.size else None,
        "lead_median": float(np.median(leads_arr)) if leads_arr.size else None,
        "lead_p75": float(np.percentile(leads_arr, 75)) if leads_arr.size else None,
        "lead_p90": float(np.percentile(leads_arr, 90)) if leads_arr.size else None,
        "lead_max": int(leads_arr.max()) if leads_arr.size else None,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    print("\n=== dual-mode look-ahead (inverted_bar vs trigger_bar) ===")
    for k, v in out.items():
        print(f"  {k}: {v}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[probe] wrote {OUT}")


if __name__ == "__main__":
    main()
