"""Measure the SUPERSESSION look-ahead in the FVG retest path.

THE CLAIM UNDER TEST
---------------------
``detect_fvg`` resolves supersession in a DEFERRED pass, at the very
END of detection, over the WHOLE window::

    for _z in zones:
        if _z.superseded_bar < 0:
            continue
        if _z.inverted_bar >= 0 and _z.inverted_bar <= _z.superseded_bar:
            _z.live = True
        else:
            _z.live = False        # <-- superseded and never inverted

``_z.live`` is a SINGLE SCALAR for the zone's whole life.  It is set
using an event that may happen LONG AFTER the zone's retest bar.

Then ``fvg_retest_signals`` does::

    for z in zones:
        if not z.live:
            continue

=> A zone that was superseded at bar S and never inverted is DEAD for
   its ENTIRE life, including the retest it would have produced at bar
   K < S.  At bar K, a live engine does not yet know that a newer
   zone will form at bar S.

THIS IS A LOOK-AHEAD: the FVG book is a selection on the outcome.

WHAT THIS PROBE MEASURES
-------------------------
Counterfactual: re-run the FVG retest scan with the supersession-killed
zones temporarily revived (``live=True``), and count the retests that
appear ONLY in the counterfactual.  For each, record
``lead = superseded_bar - retest_bar``:

  * ``lead > 0``  -> the supersession happened AFTER the retest bar.
    The trade was fully CAUSAL at the retest bar and was removed
    because of the future.  **This is the look-ahead.**
  * ``lead <= 0`` -> supersession already happened; excluding it is
    correct.

Run:
    python -m runtime.diagnostics.probe_fvg_supersession_lookahead
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
       / "fvg_supersession_lookahead.json")


def _scan(zones, close, high, low, min_pct: float):
    from bt._engine.ict_signals import fvg_retest_signals
    return fvg_retest_signals(
        zones, close, high, low,
        only_inverted=False, skip_inverted=True,
        min_mitigation_pct=min_pct,
    )


def main() -> None:
    from bt.optimal_config import build_detector_kwargs
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    scope = "XAUUSD_S1_1y__lookahead"
    print(f"[probe] loading bars {START} -> {END} ...", flush=True)
    bars = load_bars(DATA, START, END)
    art = ArtifactCache(DATA, scope, START, END).build_or_load(bars)
    zones = art["zones"]
    print(f"[probe] bars={len(bars)} zones={len(zones)}", flush=True)

    close = bars["close"].to_numpy(np.float64)
    high = bars["high"].to_numpy(np.float64)
    low = bars["low"].to_numpy(np.float64)
    min_pct = float(build_detector_kwargs().get("fvg_min_mitigation_pct", 0.0) or 0.0)

    # ── Snapshot the flags the scan will mutate ─────────────────────
    live0 = [bool(z.live) for z in zones]
    consumed0 = [int(z.consumed_bar) for z in zones]

    def restore() -> None:
        for z, lv, cb in zip(zones, live0, consumed0):
            z.live = lv
            z.consumed_bar = cb

    # ── A: ACTUAL (as shipped) ──────────────────────────────────────
    restore()
    actual = _scan(zones, close, high, low, min_pct)
    actual_ids = {(bar, id(z)) for bar, _d, z in actual}
    print(f"[probe] ACTUAL FVG retests: {len(actual)}", flush=True)

    # ── B: COUNTERFACTUAL — revive the supersession-killed zones ────
    # A zone is supersession-killed iff it was superseded and did NOT
    # invert at-or-before the superseding bar.  Those are exactly the
    # zones the deferred pass set live=False.
    killed = [
        z for z in zones
        if z.superseded_bar >= 0
        and not (z.inverted_bar >= 0 and z.inverted_bar <= z.superseded_bar)
    ]
    print(f"[probe] supersession-killed zones: {len(killed)}", flush=True)

    restore()
    for z in killed:
        z.live = True
    cf = _scan(zones, close, high, low, min_pct)
    cf_ids = {(bar, id(z)) for bar, _d, z in cf}
    print(f"[probe] COUNTERFACTUAL FVG retests: {len(cf)}", flush=True)

    extra = [(b, d, z) for b, d, z in cf if (b, id(z)) not in actual_ids]
    lost = [(b, d, z) for b, d, z in actual if (b, id(z)) not in cf_ids]

    leads = []
    for b, _d, z in extra:
        sb = z.superseded_bar
        if sb >= 0:
            leads.append(sb - b)
    leads_arr = np.array(leads, dtype=np.int64) if leads else np.zeros(0, dtype=np.int64)

    n_after = int((leads_arr > 0).sum())
    n_before = int((leads_arr <= 0).sum())

    out = {
        "scope": scope,
        "start": START,
        "end": END,
        "bars": int(len(bars)),
        "n_zones": int(len(zones)),
        "n_superseded": int(sum(1 for z in zones if z.superseded_bar >= 0)),
        "n_supersession_killed": len(killed),
        "actual_fvg_retests": len(actual),
        "counterfactual_fvg_retests": len(cf),
        "retests_only_in_counterfactual": len(extra),
        "retests_only_in_actual": len(lost),
        "LOOKAHEAD_extra_lead_gt_0": n_after,
        "legit_extra_lead_le_0": n_before,
        "lookahead_share_of_extra": round(n_after / max(1, n_after + n_before), 4),
        "lookahead_share_of_actual": round(n_after / max(1, len(actual)), 4),
        "lead_min": int(leads_arr.min()) if leads_arr.size else None,
        "lead_p25": float(np.percentile(leads_arr, 25)) if leads_arr.size else None,
        "lead_median": float(np.median(leads_arr)) if leads_arr.size else None,
        "lead_p75": float(np.percentile(leads_arr, 75)) if leads_arr.size else None,
        "lead_max": int(leads_arr.max()) if leads_arr.size else None,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    print("\n=== FVG supersession look-ahead ===")
    for k, v in out.items():
        print(f"  {k}: {v}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[probe] wrote {OUT}")


if __name__ == "__main__":
    main()
