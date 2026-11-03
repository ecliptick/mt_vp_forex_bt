"""Measure the LOOK-AHEAD EXCLUSION in the FVG retest path.

THE CLAIM UNDER TEST
---------------------
``fvg_retest_signals`` is called TWICE over the SAME zone list:

  * the FVG path  -> ``skip_inverted=True``  -> SKIPS any zone with
    ``z.inverted == True``
  * the iFVG path -> ``only_inverted=True``   -> takes only inverted zones

The zone list is built by ``detect_fvg`` over the WHOLE window, so
``z.inverted`` / ``z.inverted_bar`` are properties of the zone's
FUTURE.  A zone that inverted at bar J is excluded from the FVG path
even when the FVG retest would have fired at bar K < J.

=> The FVG path is a SELECTION ON THE OUTCOME: it only keeps zones
   that NEVER inverted (or that inverted after the retest).  A zone
   that inverted LATER is silently removed from the FVG book.

THIS IS A LOOK-AHEAD.  A live engine, at bar K, does not know whether
the zone will invert at bar J > K.

WHAT THIS PROBE MEASURES
-------------------------
For every zone that the FVG path WOULD have produced a retest for, if
we ignore the ``skip_inverted`` exclusion:

  * ``lead = z.inverted_bar - retest_bar``
  * ``lead > 0``  -> the zone inverted AFTER the retest bar.  The
    exclusion removed a trade that was fully CAUSAL at the retest bar.
    **This is the look-ahead.**
  * ``lead <= 0`` -> the zone had already inverted BEFORE the retest
    bar.  Excluding it is correct (it belongs to the iFVG path).

Run:
    python -m runtime.diagnostics.probe_fvg_lookahead_exclusion
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
OUT = _REPO_ROOT / "runtime" / "diagnostics" / "_reports" / "fvg_lookahead_exclusion.json"


def main() -> None:
    from bt._engine.ict_signals import fvg_retest_signals
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    scope = "XAUUSD_S1_1y__lookahead"
    print(f"[probe] loading bars {START} -> {END} ...", flush=True)
    bars = load_bars(DATA, START, END)
    print(f"[probe] bars={len(bars)}", flush=True)

    t0 = time.perf_counter()
    art = ArtifactCache(DATA, scope, START, END).build_or_load(bars)
    zones = art["zones"]
    print(f"[probe] zones={len(zones)} in {time.perf_counter()-t0:.1f}s", flush=True)

    close = bars["close"].to_numpy(np.float64)
    high = bars["high"].to_numpy(np.float64)
    low = bars["low"].to_numpy(np.float64)

    # The detector's own retest depth filter -- mirror the engine's call.
    from bt.optimal_config import build_detector_kwargs
    dk = build_detector_kwargs()
    min_pct = float(dk.get("fvg_min_mitigation_pct", 0.0) or 0.0)

    # ── A: the ACTUAL FVG path (skip_inverted=True) ─────────────────
    actual = fvg_retest_signals(
        zones, close, high, low,
        only_inverted=False, skip_inverted=True,
        min_mitigation_pct=min_pct,
    )
    actual_bars = {bar for bar, _d, _z in actual}
    print(f"[probe] ACTUAL FVG retests: {len(actual)}", flush=True)

    # ── B: the COUNTERFACTUAL FVG path (no inverted-exclusion) ──────
    # Only inverted zones are added back; every other condition is
    # identical.  `only_inverted=False, skip_inverted=False`.
    cf = fvg_retest_signals(
        zones, close, high, low,
        only_inverted=False, skip_inverted=False,
        min_mitigation_pct=min_pct,
    )
    print(f"[probe] COUNTERFACTUAL FVG retests: {len(cf)}", flush=True)

    # The counterfactual scan CONSUMES zones (z.live=False), so the
    # actual set may be a subset of the counterfactual set.  Compute the
    # difference on BAR identity, which is what matters for the book.
    cf_bars = {bar for bar, _d, _z in cf}

    # For each counterfactual retest on an INVERTED zone, was the
    # inversion already known at the retest bar?
    inverted_retests = [(b, d, z) for b, d, z in cf if z.inverted]
    leads = []
    for b, _d, z in inverted_retests:
        ib = z.inverted_bar
        if ib >= 0:
            leads.append(ib - b)

    leads_arr = np.array(leads, dtype=np.int64) if leads else np.zeros(0, dtype=np.int64)

    n_after = int((leads_arr > 0).sum())      # inverted AFTER the retest -> LOOK-AHEAD
    n_before = int((leads_arr <= 0).sum())    # inverted BEFORE -> legitimate exclusion
    n_noinv = int(sum(1 for b, _d, z in inverted_retests if z.inverted_bar < 0))

    out = {
        "scope": scope,
        "start": START,
        "end": END,
        "bars": int(len(bars)),
        "n_zones": int(len(zones)),
        "n_zones_inverted": int(sum(1 for z in zones if z.inverted)),
        "actual_fvg_retests": len(actual),
        "counterfactual_fvg_retests": len(cf),
        "counterfactual_on_inverted_zones": len(inverted_retests),
        "LOOKAHEAD_excluded_after_retest": n_after,
        "legit_excluded_before_retest": n_before,
        "excluded_no_inverted_bar": n_noinv,
        "lookahead_share_of_excluded": (
            round(n_after / max(1, n_after + n_before), 4)
        ),
        "lookahead_share_of_actual": (
            round(n_after / max(1, len(actual)), 4)
        ),
        "lead_min": int(leads_arr.min()) if leads_arr.size else None,
        "lead_p25": float(np.percentile(leads_arr, 25)) if leads_arr.size else None,
        "lead_median": float(np.median(leads_arr)) if leads_arr.size else None,
        "lead_p75": float(np.percentile(leads_arr, 75)) if leads_arr.size else None,
        "lead_max": int(leads_arr.max()) if leads_arr.size else None,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    print("\n=== FVG look-ahead exclusion ===")
    for k, v in out.items():
        print(f"  {k}: {v}")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"\n[probe] wrote {OUT}")


if __name__ == "__main__":
    main()
