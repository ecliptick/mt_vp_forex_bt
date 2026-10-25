"""Reference (unbucketed) supersession scan, for A/B validation.

This mirrors the ORIGINAL O(Z) ``for z in zones`` loop that lived in
``detect_fvg`` before the price-bucket index was added. It exists only
so ``tests/test_detector_bucketing.py`` can prove the bucketed version
is behaviourally identical, not merely faster.
"""
from __future__ import annotations


def supersede_reference(zones, new_zone, supersede_on_new=True):
    """Original O(Z) supersession scan. Mutates ``zones`` in place."""
    if not supersede_on_new:
        return zones
    new_bar = new_zone.trigger_bar
    for z in zones:
        if not z.live:
            continue
        if z.played_out_bar >= 0 and new_bar >= z.played_out_bar:
            continue
        if z.expired_bar > 0 and new_bar >= z.expired_bar:
            continue
        if z.superseded_bar >= 0:
            continue
        if z.inverted_bar >= 0 and new_bar >= z.inverted_bar:
            continue
        if z.zone_low <= new_zone.zone_high and new_zone.zone_low <= z.zone_high:
            z.superseded_bar = new_bar
            z.live = False
    zones.append(new_zone)
    return zones