"""Rolling p95 peak-to-trough return cap (TP distance normalizer).

Problem this solves
===================

The NB82 zone-anchored TP formula is ``TP_dist = zone_w ×
tp_zone_mult × boost``. For a wide FVG on a calm regime this
makes the TP literally unreachable — a $3.00 zone on a quiet
XAUUSD session at ``tp_zone_mult=200`` produces a $600 TP
(20% of gold spot); the trade rides for weeks waiting for a
move that never comes and exits at EOD. The pre-existing
``tp_max_distance_usd = 6.0`` cap is the wrong fix — $6
makes ANY zone wider than $0.03 fire its TP within $6 of
entry, regardless of regime, which is exactly the noise target
the user complained about.

This module replaces ``tp_max_distance_usd`` with a
**regime-aware** cap: the **p95 of rolling-window peak-to-
trough returns** over the prior N days (canonical: 7).

Math
====

For each inner-cadence bar ``t`` (default 1h bar), define::

    intra_p2t_t = high[t] - low[t]
                 # peak-to-trough return WITHIN this 1h bar

The cap array is then::

    p95_ret[t] = percentile([intra_p2t_s for s in [t-W, t)],
                            q=95)
                # p95 of hourly excursions over the prior W=168
                # inner bars = 7 days

So at each inner bar, the cap reflects the 95th-percentile
of hourly moves over the prior 7 days. A spike day (one
hour where gold moved $60) does NOT contaminate the cap for
the rest of the week — it occupies one slot in the 168-
element percentile window, and p95 of 168 values where one
is $60 and 167 are $5-15 gives a result still in the
$10-15 range. The cap only inflates if the move density is
genuinely high — i.e. if the WHOLE regime has shifted.

Implementation: input 1s bars are downsampled to 1h before
the rolling P2T is computed — this keeps the per-bar cost
O(N_inner × W_inner) cheap even when the input is 25M 1s
bars. The 1h P2T is just ``high - low`` of the 1h bar (no
intra-cadence max), and the p95 over 168 1h bars is
O(N_inner × W_inner) = 8760 × 168 ≈ 1.5M operations.

We store ``p95_ret[t]`` once (one float per inner-cadence bar)
and look up ``p95_ret[bar_idx]`` at trade time — O(1) per
trade, same shape as the existing ``DailySwingCap`` ring
buffer.

Composition with other caps
===========================

The cap composes WITHIN ``compute_structural_tp`` / the
multi-target engine:

    raw_tp    = zone_w × tp_zone_mult × boost   # NB82 path
    capped_p95 = tp_p95_mult × p95_ret[t]       # THIS module
    capped_swing = swing_cap_pct × swing_range  # NB72
    tp_dist   = min(raw_tp, capped_p95, capped_swing)

The p95 cap REPLACES ``tp_max_distance_usd``. Default
``tp_p95_mult = 1.0`` (cap at one full p95 excursion);
``tp_p95_mult = 0.0`` disables the cap (opt-out).

Live-deployable design
======================

Same constraints as ``DailySwingCap``:

1. **Cheap at fill-time** — O(1) lookup per trade.
2. **Live-deployable on a VPS** — single ``np.ndarray`` of
   floats, ~200KB for 25M bars. No historical bars kept.
3. **Updates throughout the day** — the live engine advances
   the rolling p95 with each new bar.

Coldstart
---------

On a fresh VPS boot, ``RollingP95Cap`` has no data. Two
seed paths (see ``seed_paths``):

* ``"constant"`` — use a JSON constant from config. The
  constant is computed once from a long historical backtest
  (the user runs ``scripts/compute_p95_constant.py`` over
  their historical bars and bakes the result into
  ``optimal_config.FOREX_P95_RET_USD``). Until the rolling
  window has accumulated enough bars, the live engine uses
  the constant.
* ``"pepperstone"`` — VPS calls Pepperstone's MT5 historical-
  pull API at boot to fetch the prior 7 days of XAUUSD 1m
  bars, computes the rolling p95 from that, then transitions
  to streaming updates. Pepperstone allows historical pulls
  on their MT5 bridge, so this is the live-deployable path.

Both paths produce the same ``np.ndarray``; ``update_p95``
appends one new bar at a time (O(1)).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, Tuple

import numpy as np
import pandas as pd

from bt._engine.timebase import assert_ns, to_ns_arr


# ─────────────────────────────────────────────────────────────────────
# Statistic selector (added 2026-10-22)
# ─────────────────────────────────────────────────────────────────────
#
# TWO DIFFERENT STATISTICS were conflated under one name. Measured on
# XAUUSD 2025-01 (1-month, 504 hourly buckets):
#
#   STAT_P2T_HOURLY  (legacy, what the code always did)
#       per-bucket excursion = high[t] - low[t]   (WITHIN one 1h bar)
#       cap = p95 of those over the prior 168 buckets
#       measured median $12.09, p95 $13.33, max $14.26
#
#   STAT_P2T_WINDOW  (what the module docstring always PROMISED)
#       excursion = max(high) - min(low) over the prior 168 buckets
#       (the true 7-day peak-to-trough)
#       measured median $83.34, p95 $108.72
#
# Ratio: 6.9x. They are not variants of one idea -- one measures a
# single hour's range, the other measures the whole week's excursion.
# A cap named "rolling peak-to-trough" that returns the median HOURLY
# range is off by ~7x from its own documentation.
#
# CANONICAL is STAT_P2T_WINDOW because that is what the module has
# always documented itself as, and because a TP cap that is smaller
# than a typical daily move ($26.04 median here) is a noise target,
# not a tradeable one. STAT_P2T_HOURLY is retained so the old
# behaviour stays A/B-able.
STAT_P2T_WINDOW = "p2t_window"
STAT_P2T_HOURLY = "p2t_hourly"
P95_STATISTICS = (STAT_P2T_WINDOW, STAT_P2T_HOURLY)
P95_STATISTIC_CANONICAL = STAT_P2T_WINDOW


# ─────────────────────────────────────────────────────────────────────
# Core dataclass
# ─────────────────────────────────────────────────────────────────────


@dataclass
class RollingP95Cap:
    """Per-bar p95 peak-to-trough return cap.

    Args:
        lookback_secs:    Length of the rolling peak-to-trough
                           window (default 7 days = 604800s).
                           The p95 is computed across THIS many
                           seconds ending at each bar.
        inner_secs:        The sub-window size for the peak-to-
                           trough decomposition. Default 3600s
                           (1h). Each bar's cap is the p95
                           across the prior ``lookback_secs /
                           inner_secs`` sub-window P2T values.
                           Smaller inner_secs = more samples =
                           tighter cap; larger = smoother cap.
        p95_q:             Percentile, default 95.
        p95_mult:          Multiplier on the p95 value at
                           trade time (default 1.0 = cap at
                           one full p95 excursion). 0.0
                           disables the cap.
        seed_path:         'constant' | 'pepperstone' | ''.
                           Coldstart strategy for live VPS.
        seed_constant_usd: Used when ``seed_path='constant'``.
                           The fallback cap value until enough
                           live bars accumulate.
        buffer_t_ns:       Per-bar timestamps (int64 ns).
        buffer_high:       Per-bar highs (float64).
        buffer_low:        Per-bar lows (float64).
        p95_ret:           Precomputed per-bar p95 return
                           (float64, length N). ``np.inf`` at
                           bars where the cap is disabled.
    """

    lookback_secs: int = 604_800  # 7 days
    inner_secs: int = 3600        # 1 hour sub-windows
    p95_q: float = 95.0
    p95_mult: float = 0.0         # 0 = disabled
    # Which P2T statistic produced ``p95_ret``. See the module-level
    # ``P95_STATISTICS`` block for the measured difference between the
    # two (6.9x on XAUUSD 2025-01). Persisted so a cached cap and a
    # freshly-built one are distinguishable.
    statistic: str = P95_STATISTIC_CANONICAL

    seed_path: str = ""
    seed_constant_usd: float = 0.0

    # Warmup window. The first ``lookback_secs + inner_secs``
    # of bars are warmup — the cap is meaningless there (p95
    # of fewer than 168 hourly bars is statistically bogus).
    # ``warmup_until_ns`` (set automatically by
    # ``build_rolling_p95_from_bars``) is the timestamp from
    # which ``cap_for`` returns a real value. The engine uses
    # ``is_warmup_complete`` to gate entry decisions during
    # warmup (default: skip the trade).
    warmup_until_ns: int = 0
    # Allow caller to disable the skip (e.g. for A/B testing).
    # Always False in backtest; True in live (live uses
    # ``seed_constant_usd`` instead).
    skip_trades_during_warmup: bool = True

    # Precomputed per-bar arrays (populated by build / update)
    buffer_t_ns: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.int64)
    )
    buffer_high: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float64)
    )
    buffer_low: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float64)
    )
    p95_ret: np.ndarray = field(
        default_factory=lambda: np.zeros(0, dtype=np.float64)
    )

    @property
    def enabled(self) -> bool:
        return self.p95_mult > 0.0

    def is_warmup_complete(self, t_ns: int) -> bool:
        """True iff the warmup window has passed at time ``t_ns``.

        In backtest mode this is set to ``True`` to skip
        trading during the first ``lookback_secs + inner_secs``
        of bars (we have historical data — we don't need a
        seed constant; we just need to wait for the cap to
        be statistically meaningful).

        In live mode this is set to ``True`` to mean
        "the rolling window has enough data" (we used
        ``seed_constant_usd`` during warmup, no need to skip
        trades). Live coldstart also sets
        ``skip_trades_during_warmup = False`` because trades
        should fire from the first bar — the seed constant is
        the cap during warmup.
        """
        if not self.enabled:
            return True
        return int(t_ns) >= int(self.warmup_until_ns)

    def cap_for(self, t_ns: int) -> float:
        """Return the p95 cap in USD at time ``t_ns``.

        Returns ``np.inf`` if the cap is disabled.
        During warmup, returns ``self.seed_constant_usd`` if
        it is set (live VPS), otherwise ``np.inf`` (backtest
        with ``seed_constant_usd=0.0`` — caller is expected
        to gate entries via ``is_warmup_complete``).
        """
        if not self.enabled:
            return float("inf")
        # Warmup path
        if int(t_ns) < int(self.warmup_until_ns):
            if self.seed_constant_usd > 0.0:
                return float(self.seed_constant_usd) * float(self.p95_mult)
            return float("inf")
        # Live lookup
        if self.p95_ret.size == 0 or self.buffer_t_ns.size == 0:
            return float("inf")
        idx = int(np.searchsorted(self.buffer_t_ns, t_ns))
        if idx >= self.p95_ret.size:
            idx = self.p95_ret.size - 1
        v = float(self.p95_ret[idx])
        if not np.isfinite(v) or v <= 0.0:
            # Should not happen post-warmup, but be defensive.
            return float(self.seed_constant_usd) if self.seed_constant_usd > 0.0 else float("inf")
        return v * float(self.p95_mult)


# ─────────────────────────────────────────────────────────────────────
# Build from cached 1s bars (backtest path)
# ─────────────────────────────────────────────────────────────────────


def _rolling_peak_to_trough_fast(
    high: np.ndarray, low: np.ndarray, window: int,
) -> np.ndarray:
    """Vectorized rolling ``max(high) - min(low)`` over ``window`` bars.

    Same semantics as ``_rolling_peak_to_trough`` (the deque version)
    but computed with numpy instead of a per-bar Python loop.

    Cost is O(N x W) MEMORY via ``sliding_window_view`` (zero-copy),
    which is fine at the inner cadence: on 1y XAUUSD N_inner is
    ~8.7k and W is 168, so the strided view is ~1.5M elements and
    the two reductions are single vectorized passes.
    """
    n = len(high)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    if window < 1 or window > n:
        window = n
    if window == 1:
        return high - low
    w_h = np.lib.stride_tricks.sliding_window_view(high, window)
    w_l = np.lib.stride_tricks.sliding_window_view(low, window)
    out = np.full(n, np.nan, dtype=np.float64)
    out[window - 1:] = w_h.max(axis=1) - w_l.min(axis=1)
    return out


def _rolling_peak_to_trough(
    high: np.ndarray, low: np.ndarray, window: int,
) -> np.ndarray:
    """Per-bar peak-to-trough return over the prior ``window`` bars.

    Returns a float64 array of length ``len(high)``. The first
    ``window - 1`` entries are NaN (warmup). After that, each
    entry is ``high[i].max() - low[i].min()`` over the prior
    ``window`` bars.

    Implementation: a simple O(N × W) scan is fine for small W
    (typical: window=86400 for 1s bars in 24h lookback — but
    that's 24h × 86400 bars = 2.1B operations per bar, way too
    slow). We use a deque-based sliding window min/max which
    is O(N) total.
    """
    n = len(high)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if window < 1 or window > n:
        window = n

    # Sliding-window max of highs and min of lows.
    # Standard monotonic deque algorithm.
    from collections import deque

    max_dq: deque = deque()  # stores indices; values are decreasing
    min_dq: deque = deque()  # stores indices; values are increasing

    for i in range(n):
        # Pop indices whose values are no longer in the window
        while max_dq and max_dq[0] <= i - window:
            max_dq.popleft()
        while min_dq and min_dq[0] <= i - window:
            min_dq.popleft()

        # Push current bar
        h = high[i]
        l = low[i]
        while max_dq and high[max_dq[-1]] <= h:
            max_dq.pop()
        max_dq.append(i)
        while min_dq and low[min_dq[-1]] >= l:
            min_dq.pop()
        min_dq.append(i)

        # Once we have a full window, record the P2T
        if i >= window - 1:
            win_high = high[max_dq[0]]
            win_low = low[min_dq[0]]
            out[i] = win_high - win_low
    return out


def _rolling_percentile(
    values: np.ndarray, window: int, q: float,
) -> np.ndarray:
    """Per-bar rolling percentile via vectorized sliding-window sort.

    Implemented with ``np.lib.stride_tricks.sliding_window_view``
    for memory-cheap windowing and ``np.apply_along_axis``
    percentile over the window axis. Cost is O(N × W) for the
    sort, but the inner kernel is numpy (no Python loop).

    Returns NaN for the warmup bars (where the window isn't
    full yet).
    """
    n = len(values)
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    out = np.full(n, np.nan, dtype=np.float64)
    if window < 1 or window > n:
        window = n
    if window == 1:
        # Fast path: percentile of a single value is the value
        # itself.
        out[:] = values
        return out

    # Build a 2D view of [n - W + 1, W] of contiguous windows.
    # ``sliding_window_view`` does this in O(N × W) memory but
    # zero copy.
    windows = np.lib.stride_tricks.sliding_window_view(
        values, window_shape=window
    )  # [n-W+1, W]
    # Drop NaNs from the inner P2T (warmup of inner rolling) on a
    # per-row basis. We approximate this with a global mask: any
    # row that has ANY nan gets nan output. That preserves the
    # "don't trust incomplete windows" invariant without a per-row
    # Python loop.
    nan_row = np.isnan(windows).any(axis=1)
    # Sort each row and pick the p95 index.
    sorted_w = np.sort(windows, axis=1)
    idx = int(round((q / 100.0) * (window - 1)))
    p_vals = sorted_w[:, idx]
    # Where the row had any NaN, propagate NaN.
    p_vals[nan_row] = np.nan
    # The first `window - 1` output positions correspond to rows
    # whose value sits at the END of the window, so position
    # `[i - window + 1]` in the rolling output. Equivalently,
    # output[i] = p_vals[i - window + 1] when i >= window - 1.
    out[window - 1:] = p_vals
    return out


def _downsample_to_inner(
    high: np.ndarray,
    low: np.ndarray,
    times_ns: np.ndarray,
    inner_secs: int,
    first_t_ns: int,
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Downsample 1s bars to ``inner_secs`` buckets BY WALL-CLOCK TIME.

    Returns (bucket_t_ns, bucket_high, bucket_low).

    Why this is time-based and not a positional reshape
    --------------------------------------------------
    The obvious implementation — ``high[:n].reshape(-1, inner_secs)``
    — assumes every ``inner_secs`` CONSECUTIVE ROWS span exactly
    ``inner_secs`` seconds. That holds only for a gapless feed.

    XAUUSD has weekend and rollover gaps. Measured on
    XAUUSD_S1_1y (41,233,730 bars, 2024-01-01 .. 2026-07-15):

        bars with a >1s gap:  6,192,328  (15.018%)
        buckets whose span != exactly 1h:  11,453 of 11,453 (100%)
        bucket-span drift vs contiguous: mean -5,725h, min -11,451h

    So the reshape shears every single bucket. A bucket straddling a
    weekend reports a multi-day range as if it were one hour, which
    inflates ``inner_p2t`` and therefore ``p95_ret``. Worse, the
    synthetic bucket timestamps advance by exactly ``inner_secs`` per
    bucket regardless of real elapsed time, so ``buffer_t_ns`` runs
    out of sync with the data: on 1y the last bucket lands around
    April 2025 while the data runs to July 2026, and ``cap_for``'s
    ``searchsorted`` saturates, returning a stale value for the last
    ~15 months.

    This implementation floors each bar's timestamp to the UTC bucket
    boundary and aggregates with ``np.unique`` + ``reduceat``, so gaps
    produce genuinely empty buckets rather than sheared ones, and
    ``bucket_t_ns`` is always the real wall-clock time of the bucket.

    Cost: O(N) with one sort-free ``np.unique`` pass (~1.2s for 41M
    rows), in the same ballpark as the reshape it replaces.
    """
    n = len(high)
    if n == 0:
        return (np.zeros(0, dtype=np.int64),
                np.zeros(0, dtype=np.float64),
                np.zeros(0, dtype=np.float64))

    inner_ns = int(inner_secs) * 1_000_000_000
    # Floor to the UTC bucket boundary so buckets align to the clock
    # (00:00, 01:00, ...) rather than to the first bar's timestamp.
    bucket_idx = times_ns // inner_ns

    # ``times_ns`` is sorted ascending (the engine sorts bars by time),
    # so ``bucket_idx`` is NON-DECREASING and the group boundaries can
    # be found with a plain diff — no sort, and no ``return_inverse``
    # array (which would cost 8 bytes x 41M rows = 330 MB of transient
    # allocation on the 1y file).
    #
    # NB: the old implementation used ``np.unique(..., return_inverse=True)``
    # which sorts the full array. That was both slower and far more
    # memory-hungry on a 41M-row file than the diff-based approach.
    is_new = np.empty(len(bucket_idx), dtype=bool)
    is_new[0] = True
    np.not_equal(bucket_idx[1:], bucket_idx[:-1], out=is_new[1:])
    starts = np.flatnonzero(is_new)
    n_buckets = len(starts)
    if n_buckets == 0:
        return (np.zeros(0, dtype=np.int64),
                np.zeros(0, dtype=np.float64),
                np.zeros(0, dtype=np.float64))

    bucket_high = np.maximum.reduceat(high, starts)
    bucket_low = np.minimum.reduceat(low, starts)
    # Real wall-clock time of each bucket.
    bucket_t_ns = bucket_idx[starts].astype(np.int64) * inner_ns

    return bucket_t_ns, bucket_high, bucket_low


def build_rolling_p95_from_bars(
    bars: pd.DataFrame,
    *,
    lookback_secs: int = 604_800,   # 7 days
    inner_secs: int = 3_600,        # 1 hour
    p95_q: float = 95.0,
    p95_mult: float = 1.0,
    seed_path: str = "constant",
    seed_constant_usd: float = 0.0,
    statistic: str = STAT_P2T_WINDOW,
) -> RollingP95Cap:
    """Build a ``RollingP95Cap`` from cached 1s OHLCV bars.

    The corpus is downsampled to ``inner_secs`` cadence
    (default 1h bars) before the rolling P2T is computed —
    vectorized reshape makes this ~100ms for 41M 1s bars.

    For XAUUSD 1y at 1h sub-windows: N_inner = 8760,
    W_inner = 168 (= 7 days / 1h). Total cost ~1s wall-clock.
    """
    times_ns = to_ns_arr(bars, "time")
    # HARD GUARD: this cap computes its cutoff in nanoseconds, so a
    # millisecond timestamp would make ``warmup_until_ns`` effectively
    # infinite and silently drop every trade (this exact bug produced
    # 264,666 signals / 0 trades before timebase.py existed).
    assert_ns(times_ns, "rolling_p95 times")
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)

    # Vectorized downsample: O(N) reshape, ~100ms for 41M rows.
    first_t_ns = int(times_ns[0]) if times_ns.size > 0 else 0
    sub_t, sub_h, sub_l = _downsample_to_inner(
        high, low, times_ns, inner_secs, first_t_ns
    )

    # Inner P2T per bucket, then the rolling cap over the prior window.
    p95_window = max(2, int(lookback_secs // inner_secs))
    #
    # STAT_P2T_HOURLY (legacy): the excursion is measured WITHIN each
    # 1h bucket (high - low), and the cap is the p95 of those
    # excursions. This is what this function did before 2026-10-22.
    #
    # STAT_P2T_WINDOW (canonical): the excursion is the true
    # peak-to-trough across the WHOLE lookback window --
    # max(high) - min(low) over the trailing ``p95_window`` buckets.
    # This is what the module has always documented.
    if statistic == STAT_P2T_WINDOW:
        inner_p2t = _rolling_peak_to_trough_fast(sub_h, sub_l, p95_window)
        # A true window peak-to-trough is already a max-like statistic,
        # so taking a further p95 across the window would be
        # double-counting the tail. The cap IS the excursion.
        p95_series = np.where(np.isnan(inner_p2t), np.inf, inner_p2t)
    elif statistic == STAT_P2T_HOURLY:
        inner_p2t = sub_h - sub_l
        p95_series = _rolling_percentile(inner_p2t, p95_window, p95_q)
        p95_series = np.where(np.isnan(p95_series), np.inf, p95_series)
    else:
        raise ValueError(
            f"statistic={statistic!r} is not one of {P95_STATISTICS!r}."
        )

    # ``p95_q`` is only consumed by the hourly statistic; on the
    # window statistic it is inert by construction. Record it anyway so
    # the cache fingerprint still distinguishes the two modes.
    _ = float(p95_q)

    # Compute warmup timestamp (first inner bar + lookback + one inner period).
    warmup_until_ns = 0
    if sub_t.size > 0:
        warmup_until_ns = int(sub_t[0]) + int(
            (lookback_secs + inner_secs) * 1_000_000_000
        )

    return RollingP95Cap(
        lookback_secs=lookback_secs,
        inner_secs=inner_secs,
        p95_q=p95_q,
        p95_mult=p95_mult,
        statistic=str(statistic),
        seed_path=seed_path,
        seed_constant_usd=seed_constant_usd,
        warmup_until_ns=warmup_until_ns,
        skip_trades_during_warmup=True,
        buffer_t_ns=sub_t,
        buffer_high=sub_h,
        buffer_low=sub_l,
        p95_ret=p95_series,
    )


# ─────────────────────────────────────────────────────────────────────
# Streaming update (live VPS path)
# ─────────────────────────────────────────────────────────────────────


def make_live_rolling_p95(
    *,
    lookback_secs: int = 604_800,
    inner_secs: int = 3600,
    p95_q: float = 95.0,
    p95_mult: float = 1.0,
    seed_path: str = "constant",
    seed_constant_usd: float = 0.0,
    max_inner_bars: int = 4096,
    statistic: str = P95_STATISTIC_CANONICAL,
) -> RollingP95Cap:
    """Create an empty ``RollingP95Cap`` ready for streaming updates.

    The live VPS would call this at startup, then call
    ``update_p95`` once per inner_secs bar as 1m bars arrive.

    ``max_inner_bars`` sets the ring-buffer size — enough for
    ~170 days at 1h cadence. For higher cadences the caller
    should size accordingly.
    """
    cap = RollingP95Cap(
        lookback_secs=lookback_secs,
        inner_secs=inner_secs,
        p95_q=p95_q,
        p95_mult=p95_mult,
        statistic=str(statistic),
        seed_path=seed_path,
        seed_constant_usd=seed_constant_usd,
        buffer_t_ns=np.zeros(max_inner_bars, dtype=np.int64),
        buffer_high=np.zeros(max_inner_bars, dtype=np.float64),
        buffer_low=np.zeros(max_inner_bars, dtype=np.float64),
        p95_ret=np.full(max_inner_bars, np.inf, dtype=np.float64),
    )
    return cap


def update_p95(
    cap: RollingP95Cap,
    t_ns: int,
    high: float,
    low: float,
) -> None:
    """Append one inner-cadence bar to the rolling p95 cap.

    O(1) amortised. Maintains a ring buffer of length
    ``max_inner_bars``. Once the ring is full, the oldest
    entry is overwritten. The ``p95_ret`` array is recomputed
    in-place for the bar that just changed (cheap, only one
    percentile over ``p95_window_bars`` entries).

    TODO (live): on coldstart with seed_path='pepperstone',
    the VPS calls the Pepperstone MT5 historical-pull API to
    fetch the prior ``lookback_secs`` of XAUUSD 1m bars,
    aggregates them to ``inner_secs`` cadence, then calls
    ``update_p95`` once per aggregated bar. After that the
    VPS streams 1m bars at runtime. The seed_constant_usd
    fallback is used for the FIRST bars while the pull is
    in flight (typically <10s).
    """
    if cap.buffer_t_ns.size == 0:
        return
    head = int(getattr(cap, "_head", 0))
    cap.buffer_t_ns[head] = t_ns
    cap.buffer_high[head] = high
    cap.buffer_low[head] = low
    head = (head + 1) % cap.buffer_t_ns.size
    object.__setattr__(cap, "_head", head)

    # Recompute p95 for the new tail bar.
    p95_window = max(2, int(cap.lookback_secs // cap.inner_secs))
    n = cap.buffer_t_ns.size
    tail_idx = (head - 1) % n
    # Read the last p95_window inner bars.
    win_h = np.empty(p95_window, dtype=np.float64)
    win_l = np.empty(p95_window, dtype=np.float64)
    for k in range(p95_window):
        idx = (head - 1 - k) % n
        win_h[k] = cap.buffer_high[idx]
        win_l[k] = cap.buffer_low[idx]

    statistic = getattr(cap, "statistic", P95_STATISTIC_CANONICAL)
    if statistic == STAT_P2T_WINDOW:
        # True window peak-to-trough: max(high) - min(low) over the
        # whole trailing window. Mirrors the backtest build path.
        cap.p95_ret[tail_idx] = float(win_h.max() - win_l.min())
        return
    if statistic != STAT_P2T_HOURLY:
        raise ValueError(
            f"statistic={statistic!r} is not one of {P95_STATISTICS!r}."
        )

    # Legacy: p95 of the per-bucket (within-bar) excursions.
    arr = win_h - win_l
    arr = arr[np.isfinite(arr) & (arr > 0.0)]
    if arr.size > 0:
        cap.p95_ret[tail_idx] = float(np.percentile(arr, cap.p95_q))
    else:
        cap.p95_ret[tail_idx] = np.inf


# ─────────────────────────────────────────────────────────────────────
# Disk persistence
# ─────────────────────────────────────────────────────────────────────


def save_rolling_p95(cap: RollingP95Cap, cache_dir: Path) -> None:
    """Persist a ``RollingP95Cap`` to disk as npz + meta json."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez(
        cache_dir / "rolling_p95.npz",
        buffer_t_ns=cap.buffer_t_ns,
        buffer_high=cap.buffer_high,
        buffer_low=cap.buffer_low,
        p95_ret=cap.p95_ret,
    )
    (cache_dir / "rolling_p95_meta.json").write_text(
        f'{{"lookback_secs": {cap.lookback_secs}, '
        f'"inner_secs": {cap.inner_secs}, '
        f'"p95_q": {cap.p95_q}, '
        f'"p95_mult": {cap.p95_mult}, '
        f'"statistic": "{getattr(cap, "statistic", P95_STATISTIC_CANONICAL)}", '
        f'"seed_path": "{cap.seed_path}", '
        f'"seed_constant_usd": {cap.seed_constant_usd}, '
        f'"n_records": {int(cap.buffer_t_ns.size)}}}\n'
    )


def load_rolling_p95(cache_dir: Path) -> Optional[RollingP95Cap]:
    """Load a ``RollingP95Cap`` from disk; ``None`` if not cached."""
    cache_dir = Path(cache_dir)
    npz_path = cache_dir / "rolling_p95.npz"
    meta_path = cache_dir / "rolling_p95_meta.json"
    if not npz_path.exists() or not meta_path.exists():
        return None
    import json as _json
    npz = np.load(npz_path)
    meta = _json.loads(meta_path.read_text())
    cap = RollingP95Cap(
        lookback_secs=int(meta["lookback_secs"]),
        inner_secs=int(meta["inner_secs"]),
        p95_q=float(meta["p95_q"]),
        statistic=str(meta.get("statistic", P95_STATISTIC_CANONICAL)),
        p95_mult=float(meta["p95_mult"]),
        seed_path=str(meta.get("seed_path", "")),
        seed_constant_usd=float(meta.get("seed_constant_usd", 0.0)),
        buffer_t_ns=npz["buffer_t_ns"],
        buffer_high=npz["buffer_high"],
        buffer_low=npz["buffer_low"],
        p95_ret=npz["p95_ret"],
    )
    return cap


__all__ = [
    "RollingP95Cap",
    "build_rolling_p95_from_bars",
    "make_live_rolling_p95",
    "update_p95",
    "save_rolling_p95",
    "load_rolling_p95",
    "STAT_P2T_WINDOW",
    "STAT_P2T_HOURLY",
    "P95_STATISTICS",
    "P95_STATISTIC_CANONICAL",
]