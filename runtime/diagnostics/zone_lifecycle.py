"""Zone lifecycle diagnostic on the 1m zone set (2026-10-23).

Answers: what actually limits the sniper (iFVG) count at 1m?

Hypothesis from the user: "sniper fvg firing is still too few, I expect
around 30 snipers a day. Is the superseding too fast? Note that
invalidation still checks at 1 second bodies."

This measures, per zone:

* the zone's age at death (in 1m bars) and how it died
* whether it reached PIERCED but failed to invert
* whether it was superseded BEFORE it could have inverted

The last one is the important one, because the supersession scan runs
during DETECTION (phase 2) while the inversion walk runs AFTER all
detection (phase 3). So ``z.inverted_bar`` is always -1 during the
supersession scan -- the ``z.inverted_bar >= 0`` escape hatch at
``ict_signals.py:1243`` can never fire during detection. Every zone
that overlaps a later zone dies, regardless of whether it was about
to invert.

Usage
-----
    python -m runtime.diagnostics.zone_lifecycle
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache, detector_kwargs_from_recipe, load_bars,
)

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/zone_lifecycle.json")

SLICES = [
    ("2026-01", "2026-01-01", "2026-02-01"),
    ("2026-03", "2026-03-01", "2026-04-01"),
    ("2025-05", "2025-05-01", "2025-06-01"),
]


def main():
    rows = []
    for tag, s, e in SLICES:
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        z = art["zones"]
        n = len(z)
        days = (np.datetime64(e) - np.datetime64(s)).astype(int)
        n_days = max(int(days), 1)

        # How many bars? used to convert 1s ages to minutes.
        n_1s = len(bars)

        c = Counter()
        age_at_death_min = []
        age_at_inversion_min = []
        for x in z:
            if x.inverted_bar >= 0:
                c["inverted"] += 1
                age_at_inversion_min.append((x.inverted_bar - x.trigger_bar) / 60.0)
            elif x.superseded_bar >= 0:
                c["superseded"] += 1
                age_at_death_min.append(
                    (x.superseded_bar - x.trigger_bar) / 60.0)
            elif x.expired_bar >= 0:
                c["expired"] += 1
            else:
                c["alive"] += 1
            if x.pierced_bar >= 0 and x.inverted_bar < 0:
                c["pierced_but_not_inverted"] += 1
            if x.mitigated_bar >= 0:
                c["mitigated"] += 1
            if x.played_out_bar >= 0:
                c["played_out"] += 1

        inv = c["inverted"]
        print()
        print(f"=== {tag}  ({n_days} days, {n_1s:,} 1s bars) ===")
        print(f"  zones            {n:>7}   ({n/n_days:.0f}/day)")
        print(f"  inverted         {inv:>7}   ({inv/n_days:.1f}/day)  "
              f"<-- SNIPERS")
        print(f"  superseded       {c['superseded']:>7}   "
              f"({c['superseded']/n:.1%})")
        print(f"  expired          {c['expired']:>7}")
        print(f"  alive at EOD     {c['alive']:>7}")
        print(f"  mitigated        {c['mitigated']:>7}   "
              f"({c['mitigated']/n:.1%})")
        print(f"  pierced          {c['pierced_but_not_inverted'] + inv:>7}")
        print(f"  PIERCED but NOT inverted {c['pierced_but_not_inverted']:>7}   "
              f"<-- failed on the retest leg")
        if age_at_death_min:
            print(f"  age at supersession (min): p10 {np.percentile(age_at_death_min,10):.1f} "
                  f"p50 {np.percentile(age_at_death_min,50):.1f} "
                  f"p90 {np.percentile(age_at_death_min,90):.1f}")
        if age_at_inversion_min:
            print(f"  age at inversion   (min): p10 {np.percentile(age_at_inversion_min,10):.1f} "
                  f"p50 {np.percentile(age_at_inversion_min,50):.1f} "
                  f"p90 {np.percentile(age_at_inversion_min,90):.1f}")

        # The 30/day target.
        need = 30 * n_days
        print(f"  target 30/day = {need} inversions; actual {inv} "
              f"({inv/max(need,1):.0%} of target)")

        rows.append({
            "slice": tag, "days": n_days, "zones": n,
            "zones_per_day": n / n_days,
            "inverted": inv, "inverted_per_day": inv / n_days,
            "superseded_pct": c["superseded"] / n,
            "pierced_not_inverted": c["pierced_but_not_inverted"],
            "counts": dict(c),
            "age_at_death_min_p": (
                list(np.percentile(age_at_death_min, [10, 50, 90]))
                if age_at_death_min else None),
            "age_at_inversion_min_p": (
                list(np.percentile(age_at_inversion_min, [10, 50, 90]))
                if age_at_inversion_min else None),
        })

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(rows, fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
