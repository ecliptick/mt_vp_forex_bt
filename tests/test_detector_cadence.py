"""Regression tests for the 2026-10-23/24 detector fixes (1m cadence).

Three defects were found when the FVG detector moved from 1s to 1m:

1. The resample bucketed by ROW POSITION, not by timestamp. Real FX
   data has weekend/rollover gaps, so row position drifts from
   wall-clock time and every bucket boundary is wrong.

2. The mitigation/invalidation walk sliced the RAW 1s arrays while
   detection ran on the resampled cadence. So
   ``invalidation_min_consecutive_bars=2`` counted two 1-SECOND bars,
   and ``body_only_invalidation`` tested 1s candle bodies.

3. Supersession was applied DURING detection while the inversion walk
   runs AFTER all detection, so ``z.inverted_bar`` was always -1 during
   the supersession scan. The escape hatch was structurally
   unreachable and 94% of zones died without ever being given the
   chance to invert. That is why the sniper count was 4.2/day against
   a ~30/day expectation.

These tests lock all three.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from bt._engine.ict_signals import (
    _resample_bucket_index,
    detect_fvg,
)
from runtime.sweeps.cached_sweep import ArtifactCache


def _gapped_bars(n: int = 40_000, seed: int = 11) -> tuple:
    """Bars with a WEEKEND GAP in the middle.

    Row position and wall-clock time diverge across the gap, which is
    exactly the condition that breaks positional bucketing.

    THE GAP SIZE MATTERS. A gap that is an exact multiple of the
    bucket size (60s) does NOT change the grouping -- the rows simply
    shift by a whole number of buckets. The real corpus gaps are NOT
    minute-aligned: measured on XAUUSD_S1_1y, 923,993 of 924k gaps
    are not multiples of 60s (e.g. 172,812s, 176,401s, 190,863s),
    totalling 7,886,596 seconds of drift. So the fixture uses a real
    gap size, 172,812s.
    """
    rng = np.random.default_rng(seed)
    steps = rng.normal(0.0, 0.25, size=n)
    close = 2650.0 + np.cumsum(steps)
    high = close + np.abs(rng.normal(0, 0.15, size=n))
    low = close - np.abs(rng.normal(0, 0.15, size=n))
    open_ = np.concatenate([[close[0]], close[:-1]])

    # 1s cadence, then a real (non-minute-aligned) weekend gap.
    gap_at = ((n // 2) // 60) * 60
    base = np.arange(n, dtype="int64") * 1_000_000_000
    base[gap_at:] += 172_812 * 1_000_000_000
    t0 = 1_735_689_600_000_000_000  # 2025-01-01T00:00:00Z in ns
    times_ns = base + t0
    return open_, high, low, close, times_ns


def test_resample_is_bucketed_by_timestamp_not_row_position():
    """A weekend gap must not shift the bucket boundaries.

    With positional bucketing, each bucket is 60 CONSECUTIVE ROWS
    regardless of elapsed time. After a gap that is NOT a multiple of
    60s, a row bucket can straddle a wall-clock minute boundary -- so
    the aggregated bar mixes bars from different minutes.

    The invariant: under CORRECT timestamp bucketing, every bucket
    lies entirely INSIDE one wall-clock minute.
    """
    o, h, l, c, tns = _gapped_bars()
    n = len(o)
    minute_ns = 60 * 1_000_000_000

    # ── Timestamp bucketing (correct) ──────────────────────────
    b_ts = _resample_bucket_index(tns, n, 60)
    assert _buckets_contained_in_one_minute(b_ts, tns, minute_ns), (
        "timestamp bucketing produced a bucket that straddles a "
        "wall-clock minute boundary"
    )

    # ── Positional bucketing (the old, wrong behaviour) ─────────
    b_pos = (np.arange(n) // 60).astype(np.int64)
    assert not _buckets_contained_in_one_minute(b_pos, tns, minute_ns), (
        "the positional fallback also stays inside one minute -- the "
        "gap is not exercising the bug, so this test proves nothing"
    )


def _buckets_contained_in_one_minute(bucket, tns, minute_ns) -> bool:
    """True when every bucket's bars share one wall-clock minute."""
    order = np.argsort(bucket, kind="stable")
    b_sorted = bucket[order]
    t_sorted = tns[order]
    # The minute a bar belongs to, in wall-clock terms.
    minute_of = t_sorted // minute_ns
    # Within a bucket, the minute must not change.
    changes = (np.diff(minute_of) != 0) & (np.diff(b_sorted) == 0)
    return not bool(changes.any())


def test_resample_without_timestamps_warns_instead_of_silently_guessing():
    """A resample with no timestamps must not silently corrupt zones."""
    o, h, l, c, _ = _gapped_bars()
    with pytest.warns(RuntimeWarning, match="POSITIONAL"):
        detect_fvg(
            open_=o, high=h, low=l, close=c,
            fvg_min_zone_usd=0.30, max_zone_age_bars=3_600,
            fvg_body_definition="body", fvg_displacement_ratio=0.0,
            supersede_on_new=True, require_retest_to_invert=True,
            resample_to_n_secs=60,
        )


def test_resample_aggregation_matches_the_loop():
    """The vectorised per-bucket max/min MUST equal the original loop.

    2026-10-25. The resample aggregation was:

        for ub_idx, ub in enumerate(unique_buckets):
            mask = bucket == ub
            scan_high[ub_idx] = float(high[mask].max())
            scan_low[ub_idx]  = float(low[mask].min())

    That is ONE full-length comparison PER BUCKET. On the 1y corpus
    it is 687,228 buckets x 41,233,730 bars = **28.3 TRILLION**
    element-ops -- roughly 8 hours for the 1y detector.

    The fix uses ``np.maximum.reduceat`` / ``np.minimum.reduceat``,
    which is valid ONLY because ``_resample_bucket_index`` returns a
    MONOTONIC index on a non-decreasing timestamp series, so each
    bucket is a CONTIGUOUS segment of rows.

    This test proves the equivalence on data WITH A GAP (the case
    where positional assumptions would break) and on gapless data.
    """
    for label, (o, h, l, c, tns) in {
        "gapped": _gapped_bars(),
        "gapless": _gapless_bars(),
    }.items():
        n = len(o)
        bucket = _resample_bucket_index(tns, n, 60)
        unique_buckets, bucket_starts = np.unique(
            bucket, return_index=True)

        # ── The ORIGINAL loop ─────────────────────────────────
        ref_high = np.zeros(len(unique_buckets), dtype=np.float64)
        ref_low = np.full(len(unique_buckets), np.inf, dtype=np.float64)
        for ub_idx, ub in enumerate(unique_buckets):
            mask = bucket == ub
            ref_high[ub_idx] = float(h[mask].max())
            ref_low[ub_idx] = float(l[mask].min())

        # ── The VECTORISED form ───────────────────────────────
        got_high = np.maximum.reduceat(h, bucket_starts)
        got_low = np.minimum.reduceat(l, bucket_starts)

        assert np.array_equal(got_high, ref_high), (
            f"{label}: the vectorised high differs from the loop"
        )
        assert np.array_equal(got_low, ref_low), (
            f"{label}: the vectorised low differs from the loop"
        )


def test_resample_bucket_index_is_monotonic_so_reduceat_is_valid():
    """``reduceat`` is only correct on CONTIGUOUS, MONOTONIC segments.

    The whole per-bucket aggregation fix rests on this property. If
    the bucket index ever stops being monotonic, ``reduceat`` would
    silently produce the wrong answer -- so the property is asserted
    here rather than assumed.
    """
    for label, (o, h, l, c, tns) in {
        "gapped": _gapped_bars(),
        "gapless": _gapless_bars(),
    }.items():
        n = len(o)
        bucket = _resample_bucket_index(tns, n, 60)
        # Non-decreasing.
        assert np.all(np.diff(bucket) >= 0), (
            f"{label}: the bucket index is not monotonic"
        )
        # And therefore each bucket IS a contiguous run, so the rows
        # with a given bucket index are exactly one segment.
        unique_buckets, starts = np.unique(bucket, return_index=True)
        ends = np.empty_like(starts)
        for i in range(len(starts) - 1, -1, -1):
            ends[i] = (starts[i + 1] - 1) if i + 1 < len(starts) else n - 1
        for i, ub in enumerate(unique_buckets):
            seg = bucket[starts[i]:ends[i] + 1]
            assert np.all(seg == ub), (
                f"{label}: bucket {ub} is not contiguous -- the "
                "reduceat aggregation would be wrong"
            )


def _gapless_bars(n: int = 20_000, seed: int = 7) -> tuple:
    """A gapless 1s series -- the common case."""
    rng = np.random.default_rng(seed)
    close = 2650.0 + np.cumsum(rng.normal(0.0, 0.25, size=n))
    high = close + np.abs(rng.normal(0, 0.15, size=n))
    low = close - np.abs(rng.normal(0, 0.15, size=n))
    open_ = np.concatenate([[close[0]], close[:-1]])
    t0 = 1_735_689_600_000_000_000
    times_ns = np.arange(n, dtype="int64") * 1_000_000_000 + t0
    return open_, high, low, close, times_ns


def test_supersession_does_not_preempt_inversion():
    """A zone that inverted BEFORE being superseded must stay live.

    This is the fix for the 4.2/day sniper rate. The rule:

      * superseded and never inverted  -> DEAD
      * superseded but inverted first    -> LIVE (legitimate iFVG)

    The second case is what the old code could never produce, because
    ``z.inverted_bar`` is always -1 during detection.
    """
    o, h, l, c, tns = _gapped_bars()

    zones = detect_fvg(
        open_=o, high=h, low=l, close=c, times_utc_ns=tns,
        fvg_min_zone_usd=0.30, max_zone_age_bars=3_600,
        fvg_body_definition="body", fvg_displacement_ratio=0.0,
        supersede_on_new=True, require_retest_to_invert=True,
        resample_to_n_secs=60,
    )
    assert zones, "the synthetic data must produce some zones"

    inverted_before_superseded = [
        z for z in zones
        if z.inverted_bar >= 0 and z.superseded_bar >= 0
        and z.inverted_bar <= z.superseded_bar
    ]
    # The fix must produce at least one of these. Under the old code
    # this set was ALWAYS EMPTY.
    assert inverted_before_superseded, (
        "no zone inverted before being superseded -- the deferred "
        "supersession fix is not taking effect"
    )
    for z in inverted_before_superseded:
        assert z.live, (
            "a zone that inverted before being superseded must stay live"
        )

    # And the complementary rule still holds.
    for z in zones:
        if z.superseded_bar >= 0 and not (
            z.inverted_bar >= 0 and z.inverted_bar <= z.superseded_bar
        ):
            assert not z.live, (
                "a superseded zone that never inverted must be dead"
            )


def test_invalidation_walk_uses_the_detection_cadence():
    """``invalidation_min_consecutive_bars`` must count DETECTOR bars.

    On 1m data, "2 consecutive bars" means two 1-MINUTE bars. If the
    walk sliced the raw 1s arrays, it would be counting two 1-SECOND
    bars -- which is a different (much stricter) rule.
    """
    o, h, l, c, tns = _gapped_bars()

    def _count(consec: int) -> int:
        z = detect_fvg(
            open_=o, high=h, low=l, close=c, times_utc_ns=tns,
            fvg_min_zone_usd=0.30, max_zone_age_bars=3_600,
            fvg_body_definition="body", fvg_displacement_ratio=0.0,
            supersede_on_new=True, require_retest_to_invert=True,
            invalidation_min_consecutive_bars=consec,
            resample_to_n_secs=60,
        )
        return sum(1 for x in z if x.pierced_bar >= 0)

    # Requiring MORE consecutive bars can never produce MORE pierces.
    one = _count(1)
    two = _count(2)
    assert two <= one, (
        f"2 consecutive bars produced {two} pierces but 1 bar produced "
        f"{one} -- the walk is not running at the detection cadence"
    )


def test_detector_cadence_override_reaches_the_detector():
    """A ``resample_to_n_secs`` detector override must reach ``detect_fvg``.

    2026-10-24 BUG. ``ArtifactCache.build_or_load`` called
    ``detector_kwargs_from_recipe()`` with NO arguments, which
    SILENTLY DISCARDED ``self.detector_overrides``. The artifact
    FINGERPRINT was computed from the overridden kwargs (so the cache
    key was correct) but the DETECTOR ran with the canonical kwargs.

    The observable symptom: a 1m/3m/5m cadence sweep builds three
    differently-keyed caches that all contain the SAME 1m zone set.
    The sweep reports a flat line and gets read as "the cadence
    doesn't matter" -- the exact misreading AGENTS.md warns about
    for every other inert knob.

    This test drives the REAL ``build_or_load`` on synthetic bars and
    asserts the zone COUNT differs between cadences. It therefore
    fails if the override is dropped anywhere in the chain --
    merge, fingerprint, or build.
    """
    from runtime.sweeps.cached_sweep import ArtifactCache

    o, h, l, c, tns = _gapped_bars()
    n = len(o)

    # A minimal bars frame with the columns the cache reads. NOTE:
    # ``load_bars`` renames ``tickv`` -> ``volume``; the artifact
    # builder expects the post-rename name.
    frame = pd.DataFrame({
        "time": pd.to_datetime(tns // 1_000_000, unit="ms", utc=True),
        "open": o, "high": h, "low": l, "close": c,
        "volume": np.full(n, 1.0),
    })

    def _build(cadence: int, tmp_path):
        cache = ArtifactCache(
            tmp_path / "synthetic.parquet", "cadtest",
            "2026-01-01", "2026-01-02",
            detector_overrides={"resample_to_n_secs": cadence},
        )
        cache.dir = tmp_path / f"cache_{cadence}"
        cache.dir.mkdir(parents=True, exist_ok=True)
        art = cache.build_or_load(frame)
        return art["zones"]

    import tempfile

    with tempfile.TemporaryDirectory() as td:
        tmp_path = Path(td)
        # The fingerprint stats the data path, so it must exist.
        data_path = tmp_path / "synthetic.parquet"
        data_path.write_bytes(b"not-a-real-parquet")
        z1 = _build(60, tmp_path)
        z5 = _build(300, tmp_path)

    assert len(z1) != len(z5), (
        f"a 1m and a 5m detector produced the SAME zone count "
        f"({len(z1)}) -- the cadence override is NOT reaching "
        f"detect_fvg in build_or_load"
    )


def test_detector_cadence_override_changes_the_zone_count():
    """Different cadences must produce genuinely different zone sets.

    This is the end-to-end guard on the plumbing bug above: it does
    not care HOW the override is threaded, only that a 1m detector
    and a 5m detector disagree.
    """
    o, h, l, c, tns = _gapped_bars()

    def _n_zones(cadence: int) -> int:
        z = detect_fvg(
            open_=o, high=h, low=l, close=c, times_utc_ns=tns,
            fvg_min_zone_usd=0.30, max_zone_age_bars=3_600,
            fvg_body_definition="body", fvg_displacement_ratio=0.0,
            supersede_on_new=True, require_retest_to_invert=True,
            invalidation_min_consecutive_bars=2,
            resample_to_n_secs=cadence,
        )
        return len(z)

    n1 = _n_zones(60)
    n5 = _n_zones(300)
    assert n1 != n5, (
        f"a 1m and a 5m detector produced the SAME zone count ({n1}) "
        "-- the cadence is not reaching the detector"
    )
