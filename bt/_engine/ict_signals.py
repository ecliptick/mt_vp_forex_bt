"""ICT and Wyckoff signal primitives.

Four pure-function detectors for instant / structural signals that
complement the slow GMMA trend indicator (see
``src/core/gmma_indicators.py``):

* **FVG** (Fair Value Gap) — 3-bar imbalance where the middle bar's
  high/low don't overlap with bars 1 and 3. Fires on the FIRST bar of
  a displacement, not 60 bars later (the GMMA's structural lag).
* **iFVG** (Inversion FVG) — a previously-respected FVG that has been
  *violated* by a full candle body close through it. The zone flips
  polarity; the first retest is the entry.
* **ORB** (Opening Range Breakout) — session-open range (London 07:00
  UTC, NY 13:30 UTC) — break above opens a long, break below opens a
  short. Highest-vol windows on XAUUSD.
* **Wyckoff phase tagger** — coarse regime classifier: accumulation /
  distribution / spring / UTAD. Used as a confidence multiplier.

All detectors are pure functions of OHLC + tz-naive int64 ns timestamps.
They emit boolean arrays and ``PendingSignal``-compatible intent
objects, so the strategy layer can mix and match them with the existing
GMMA signal pipeline.

**No Numba, no I/O, no trading math.** All SL/TP/bookkeeping lives in
``src/backtest/trend_backtest.py`` and the live executors — this module
just emits signal intents.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from bt._engine.ict_strategy import PendingSignal


@dataclass
class IctSeries:
    """Lightweight per-bar arrays used by ``generate_ict_pending_signals``.

    Replaces the GMMA's ``GmmaSeries`` for the ICT-only fork. The only
    fields the ICT signal generators consume are:

    * ``trend`` — int8 per-bar trend classifier (used as the bias gate).
      In this fork the trend is sourced from
      ``market_structure.StructureState.trend`` (i.e. the BoS/CHoCH
      classifier), not from a moving-average average. ``+1`` = bull,
      ``-1`` = bear, ``0`` = unknown.
    * ``atr`` — float64 per-bar rolling ATR. Used by ATR-scaled SL/TP.
      Computed with a simple rolling mean of true range.
    """
    trend: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int8))
    atr: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    # Structure-driven FVG invalidation counter (added 2026-09-05):
    # how many live FVGs the detector flipped to iFVGs because an
    # opposing BoS/CHoCH fired after the zone formed. ``0`` if the
    # feature is off (the detector only flips zones on price-side
    # events). Populated by ``generate_ict_pending_signals`` after the
    # ``detect_fvg`` call; consumed by ``IctBacktestResult.summary``
    # via the backtest driver.
    n_structure_invalidations: int = 0
    # iFVG min-age filter (added 2026-09-15): how many iFVG
    # retest entries were dropped because their source zone was
    # inverted within ``fvg_ifvg_min_inversion_age_secs`` of
    # trigger (the D-tier "immediate inversion" pattern).
    n_ifvg_age_dropped: int = 0
    # Body-only invalidation counters (added 2026-09-15): how many
    # FVGs got mitigated / inverted under the body-only semantics.
    # Effectively n_mitigations/n_inversions under
    # ``fvg_body_only_mitigation=True`` and ``fvg_body_only_invalidation=True``.
    # Useful for sanity-checking the knob's effect size.
    n_body_mitigations: int = 0
    n_body_inversions: int = 0
    # Trade-the-D-inversion edge (added 2026-09-15): how many
    # inverse-direction trades were submitted via the
    # ``fvg_inv_trade_enabled`` path (when an inversion fires
    # the soft-stop, a new trade opens in the opposite direction).
    n_inv_trades_submitted: int = 0
    n_inv_trades_filled: int = 0
    n_inv_trades_closed: int = 0
    # Rolling FVG percentile ranker (added 2026-09-17): causal
    # rank of each zone vs the past N same-direction zones. Lives
    # on IctSeries so the detector can reach it at signal-emission
    # time. Lazily initialised by the caller; ``reset()`` is
    # called when the backtest resets.
    ranker: RollingFvgRanker | None = None


# ────────────────────────────────────────────────────────────────────────────
# Rolling FVG percentile ranker (2026-09-17)
# ────────────────────────────────────────────────────────────────────────────

import bisect
from collections import deque
from dataclasses import dataclass, field
import sys as _sys
import time as _time


@dataclass
class RollingFvgRanker:
    """O(log N) rolling percentile ranker for same-direction FVG zones.

    Causal: only past same-direction zones are used. Each zone's rank
    is computed at signal-emission time using a bounded deque of past
    anchor prices.

    Parameters
    ----------
    window : int
        Maximum number of past zones to rank against. Default 20.
        Must be >= 2 for a meaningful percentile.
    """
    window: int = 20

    # Per-direction rolling deques of anchor prices. Anchors are:
    #   bull  → zone_high  (the top of the displacement gap)
    #   bear  → zone_low   (the bottom of the displacement gap)
    # Use a deque so append/pop-left are O(1).
    _bull: deque[float] = field(default_factory=lambda: deque(maxlen=20))
    _bear: deque[float] = field(default_factory=lambda: deque(maxlen=20))

    def reset(self, window: int = 20) -> None:
        """Re-initialise with a new window size (called on backtest reset)."""
        self.window = window
        self._bull = deque(maxlen=window)
        self._bear = deque(maxlen=window)

    def add(self, direction: int, anchor_price: float) -> None:
        """Record a new zone's anchor price in the rolling window."""
        if direction > 0:
            self._bull.append(float(anchor_price))
        else:
            self._bear.append(float(anchor_price))

    def percentile(self, direction: int, anchor_price: float) -> float:
        """Return this zone's percentile rank within its direction window.

        Returns a float in [0.0, 1.0]:
            0.0 = highest anchor in the window (most extreme)
            1.0 = lowest  anchor in the window (least extreme)
            0.5 = exactly median

        When the window has < 2 zones, returns 0.0 (degenerate —
        the zone is effectively the only reference point).
        """
        if direction > 0:
            buf = self._bull
        else:
            buf = self._bear
        n = len(buf)
        if n < 2:
            return 0.0
        # bisect_left gives the number of elements < anchor_price.
        # For bull FVGs (ranked descending): anchor_price == sorted buf
        # means anchor is the LOWEST of the set, so rank = n-1 → pct=1.0.
        # bisect_left gives count of strictly-less-than → anchor
        # IS in the list (bisect finds it) → idx where anchor sits.
        # Since we rank descending for bull, "higher price = better
        # extreme = lower pct": rank = n-1 - bisect_left(sorted_desc, anchor)
        # For ascending (bear): rank = bisect_left(sorted_asc, anchor)
        # We implement this without sorting by keeping a sorted copy.
        # ── Naive (sort on every call, O(N log N)): acceptable for
        # small windows (N <= 200) and N_signal << N_bars.
        # ── Faster (bisect on a sorted mirror): O(log N) per signal.
        #
        # Use the sorted-mirror approach: maintain a sorted list alongside
        # the deque. Insertion is O(N) for the list (shifts), but
        # deque append is O(1). For N=20 this is ~20 ops/signal.
        # Net: O(N) insert + O(log N) percentile ≈ O(N) ≈ 20 ops/signal.
        # ── Even faster (keep only the deque, percentile via bisect on
        # a sorted copy): O(N log N) on every signal.
        # We use the sorted-copy approach: sort once, bisect_left.
        # Cost: sort O(N log N) on every signal, but N ≤ 200 so this
        # is ~200*log2(200) ≈ 1,500 ops/signal, vs 700k bars/day
        # and 50 signals/day → 75k ops/day. Negligible.
        prices = sorted(buf)
        n = len(prices)
        idx = bisect.bisect_left(prices, float(anchor_price))
        # idx ∈ [0, n]; 0 = anchor is smallest price → worst pct.
        # n = anchor is largest price → best pct (0.0 for bull, 1.0 for bear).
        # For bull: higher anchor → smaller percentile (more extreme).
        # For bear:  lower anchor → smaller percentile (more extreme).
        if direction > 0:
            # Bull: larger anchor = more extreme = lower percentile.
            # idx=n → pct=0.0 (anchor is max); idx=0 → pct=1.0 (min).
            pct = 1.0 - float(idx) / float(n - 1) if n > 1 else 0.0
        else:
            # Bear: smaller anchor = more extreme = lower percentile.
            # idx=0 → pct=0.0 (anchor is min); idx=n → pct=1.0 (max).
            pct = float(idx) / float(n - 1) if n > 1 else 0.0
        return pct

    def clear(self) -> None:
        self._bull.clear()
        self._bear.clear()


# ────────────────────────────────────────────────────────────────────────────
# Renko bricks (2026-09-05)
# ────────────────────────────────────────────────────────────────────────────
#
# Renko bars are a time-independent price series. A new brick forms ONLY
# when price has moved at least ``brick_size_usd`` from the previous brick's
# close — the brick direction is +1 (up-brick) or -1 (down-brick), never
# both, and consecutive same-direction bricks are stacked. The resulting
# brick series collapses noise: a 1s price series that wiggles inside a
# $0.10 band produces zero bricks, while a sustained $0.50 move produces
# 5 bricks all in the same direction.
#
# Why renko is useful for FVG invalidation:
#   The current detector flips an FVG on a single 1s pierce bar — that's
#   noisy (SL-hunt false positives). Renko gives a STRUCTURAL commitment
#   signal: the market has only "decided" the zone was violated when a
#   fresh directional brick forms on the OTHER side and holds for K
#   consecutive bricks. A 1s bar wick past the zone produces no brick;
#   a real sweep that holds produces a multi-brick streak.
#
# The output ``RenkoBars`` is mapped back to the ORIGINAL 1s bar index so
# the bar loop can query "what's the renko state at 1s bar N?" with O(1)
# lookups. We also carry per-brick arrays for charting (each brick has a
# start/end 1s bar so the visualizer can draw a brick rectangle).

@dataclass
class RenkoBars:
    """A renko brick series derived from a 1s OHLC array.

    The per-bar arrays are indexed by the ORIGINAL 1s bar so the bar
    loop can query renko state with O(1) lookups. The per-brick arrays
    are indexed by brick number (0, 1, 2, ...) and used for charting.

    Attributes
    ----------
    brick_size_usd : float
        The fixed brick height. ``0`` = invalid (raises at construction
        only when used; the function checks upfront).
    direction_per_bar : np.ndarray (int8)
        ``+1`` / ``-1`` / ``0`` (no brick yet) at each 1s bar.
    close_per_bar : np.ndarray (float64)
        Renko close at each 1s bar (last brick's close). Useful for
        "is the renko close past the zone?" checks.
    brick_count_per_bar : np.ndarray (int32)
        How many bricks have formed by 1s bar N. Monotonically
        non-decreasing; ``-1`` = no bricks yet at bar N.
    n_bricks : int
        Total number of bricks in the series.
    brick_open : np.ndarray (float64)
        Per-brick open price (the close of the previous brick).
    brick_close : np.ndarray (float64)
        Per-brick close price.
    brick_direction : np.ndarray (int8)
        Per-brick direction (``+1`` / ``-1``).
    brick_start_bar : np.ndarray (int32)
        1s bar index where this brick was created.
    brick_end_bar : np.ndarray (int32)
        Last 1s bar that contributed to this brick (same as start_bar
        for single-bar bricks; later if the brick "held" across several
        bars).
    """
    brick_size_usd: float
    direction_per_bar: np.ndarray
    close_per_bar: np.ndarray
    brick_count_per_bar: np.ndarray
    n_bricks: int
    brick_open: np.ndarray
    brick_close: np.ndarray
    brick_direction: np.ndarray
    brick_start_bar: np.ndarray
    brick_end_bar: np.ndarray


def compute_renko_bars(
    close: np.ndarray,
    brick_size_usd: float,
) -> RenkoBars:
    """Build a renko brick series from a 1s close array.

    Standard renko rules:

    * The first brick is anchored at ``close[0]`` (rounded down/up to
      the nearest brick boundary; the first bar can form a brick only
      if price moves ≥ ``brick_size_usd`` from the anchor, otherwise
      ``direction_per_bar[i] = 0`` until enough movement accumulates).
    * Consecutive same-direction bricks are stacked: the brick close of
      brick N is the brick open of brick N+1.
    * A direction reversal requires the close to move ``brick_size_usd``
      BEYOND the previous brick's close in the opposite direction
      (standard "Wicks ignored — only closes drive new bricks" rule).
    * Each brick is assigned the 1s bar index where it was created
      (the FIRST bar whose close reached the brick's level).

    Parameters
    ----------
    close : np.ndarray (float64)
        1-second close prices. Length N.
    brick_size_usd : float
        Brick height in USD. ``<= 0`` raises ``ValueError``. Typical
        for XAUUSD 1s: ``0.10``–``0.50``.

    Returns
    -------
    RenkoBars
        See class docstring.
    """
    n = close.shape[0]
    if brick_size_usd <= 0:
        raise ValueError(f"brick_size_usd must be > 0, got {brick_size_usd}")
    if n == 0:
        return RenkoBars(
            brick_size_usd=float(brick_size_usd),
            direction_per_bar=np.zeros(0, dtype=np.int8),
            close_per_bar=np.zeros(0, dtype=np.float64),
            brick_count_per_bar=np.zeros(0, dtype=np.int32),
            n_bricks=0,
            brick_open=np.zeros(0, dtype=np.float64),
            brick_close=np.zeros(0, dtype=np.float64),
            brick_direction=np.zeros(0, dtype=np.int8),
            brick_start_bar=np.zeros(0, dtype=np.int32),
            brick_end_bar=np.zeros(0, dtype=np.int32),
        )

    # Per-bar outputs (filled in by the brick walk).
    direction_per_bar = np.zeros(n, dtype=np.int8)
    close_per_bar = np.zeros(n, dtype=np.float64)
    brick_count_per_bar = np.full(n, -1, dtype=np.int32)

    # Per-brick logs (filled as bricks form).
    brick_open_list: list[float] = []
    brick_close_list: list[float] = []
    brick_dir_list: list[int] = []
    brick_start_list: list[int] = []
    brick_end_list: list[int] = []

    # Anchor: the FIRST 1s bar starts a brick-tracking state from
    # ``close[0]`` with no committed direction yet. The first brick
    # forms once price moves ≥ brick_size_usd in either direction.
    anchor = float(close[0])
    # Current brick's committed close (once formed). ``None`` = no
    # brick yet, awaiting first move.
    brick_close: float | None = None
    brick_dir: int = 0
    n_bricks = 0
    current_brick_start_bar = -1

    for i in range(n):
        c = float(close[i])
        # Per-bar forward-fill: until a brick forms, the close stays at
        # the anchor and direction is 0.
        if brick_close is None:
            close_per_bar[i] = anchor
            direction_per_bar[i] = 0
            brick_count_per_bar[i] = -1
            # First-bar gate: require ≥ brick_size_usd from anchor.
            if c - anchor >= brick_size_usd:
                # First up-brick.
                brick_dir = 1
                brick_close = anchor + brick_size_usd
                current_brick_start_bar = i
                n_bricks = 1
                brick_open_list.append(anchor)
                brick_close_list.append(brick_close)
                brick_dir_list.append(1)
                brick_start_list.append(i)
                brick_end_list.append(i)
            elif anchor - c >= brick_size_usd:
                # First down-brick.
                brick_dir = -1
                brick_close = anchor - brick_size_usd
                current_brick_start_bar = i
                n_bricks = 1
                brick_open_list.append(anchor)
                brick_close_list.append(brick_close)
                brick_dir_list.append(-1)
                brick_start_list.append(i)
                brick_end_list.append(i)
            continue

        # Already have a committed brick. Check for extension or reversal.
        if brick_dir > 0:
            # Up-brick active. New up-brick if c >= brick_close + brick_size_usd.
            if c >= brick_close + brick_size_usd:
                brick_close = brick_close + brick_size_usd
                brick_open_list.append(brick_close - brick_size_usd)
                brick_close_list.append(brick_close)
                brick_dir_list.append(1)
                brick_start_list.append(i)
                brick_end_list.append(i)
                n_bricks += 1
            elif c <= brick_close - brick_size_usd:
                # Reversal: new down-brick.
                brick_dir = -1
                brick_close = brick_close - brick_size_usd
                brick_open_list.append(brick_close + brick_size_usd)
                brick_close_list.append(brick_close)
                brick_dir_list.append(-1)
                brick_start_list.append(i)
                brick_end_list.append(i)
                n_bricks += 1
                current_brick_start_bar = i
        else:  # brick_dir < 0
            if c <= brick_close - brick_size_usd:
                brick_close = brick_close - brick_size_usd
                brick_open_list.append(brick_close + brick_size_usd)
                brick_close_list.append(brick_close)
                brick_dir_list.append(-1)
                brick_start_list.append(i)
                brick_end_list.append(i)
                n_bricks += 1
            elif c >= brick_close + brick_size_usd:
                # Reversal.
                brick_dir = 1
                brick_close = brick_close + brick_size_usd
                brick_open_list.append(brick_close - brick_size_usd)
                brick_close_list.append(brick_close)
                brick_dir_list.append(1)
                brick_start_list.append(i)
                brick_end_list.append(i)
                n_bricks += 1
                current_brick_start_bar = i

        close_per_bar[i] = brick_close
        direction_per_bar[i] = brick_dir
        brick_count_per_bar[i] = n_bricks - 1
        # Update end_bar of the current brick (it "held" through bar i).
        if n_bricks > 0:
            brick_end_list[-1] = i

    return RenkoBars(
        brick_size_usd=float(brick_size_usd),
        direction_per_bar=direction_per_bar,
        close_per_bar=close_per_bar,
        brick_count_per_bar=brick_count_per_bar,
        n_bricks=n_bricks,
        brick_open=np.asarray(brick_open_list, dtype=np.float64),
        brick_close=np.asarray(brick_close_list, dtype=np.float64),
        brick_direction=np.asarray(brick_dir_list, dtype=np.int8),
        brick_start_bar=np.asarray(brick_start_list, dtype=np.int32),
        brick_end_bar=np.asarray(brick_end_list, dtype=np.int32),
    )


def _rolling_tier_label(pct: float, a_pct: float, c_pct: float) -> str:
    """Map a rolling percentile [0,1] to a tier label (A / B / C).

    Tier thresholds are configurable via ``fvg_rolling_a_pct`` and
    ``fvg_rolling_c_pct`` on ``TrendStrategyParams``.

    * A tier: ``pct <= a_pct`` — the most extreme anchor in the window
      (highest bull zone_high / lowest bear zone_low). These are the
      strongest displacement levels and get the best TP.
    * C tier: ``pct >= 1 - c_pct`` — the least extreme anchors.
      Weakest zones, tightest SL/TP, outermost layer may be dropped.
    * B tier: everything in between. Neutral scales.

    Returns ``""`` when ``pct`` is outside [0, 1] (degenerate).
    """
    if not (0.0 <= pct <= 1.0):
        return ""
    if pct <= a_pct:
        return "A"
    if pct >= 1.0 - c_pct:
        return "C"
    return "B"


def compute_simple_atr(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 1200
) -> np.ndarray:
    """Rolling ATR — true range averaged over the last ``length`` bars.

    Uses the same TR definition as the parent repo's ``_rolling_atr``
    helper but exposed as a public utility for the ICT fork.
    """
    n = close.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    out = np.zeros(n, dtype=np.float64)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    idx = np.arange(n)
    win_start = np.maximum(0, idx - length + 1)
    counts = (idx + 1 - win_start).astype(np.float64)
    out = (csum[idx + 1] - csum[win_start]) / counts
    return out


def compute_dynamic_wick_floor(
    close: np.ndarray,
    *,
    price_pct: float = 0.00025,
    recompute_secs: int = 3600,
    times_utc_ns: np.ndarray | None = None,
) -> np.ndarray:
    """Per-bar USD wick floor scaled to a rolling mid-price.

    Ported from the sibling BTC repo (``bt/signals.py``). The floor is
    recomputed at ``recompute_secs`` boundaries (hourly by default) so
    the per-bar cost is amortised: each bar looks its floor up from a
    small bucket array — no per-bar percentile call.

    An absolute-USD wick floor is scale-blind: $25 is a meaningful
    "this candle has a real wick" threshold on BTC at $100k and an
    impossible one on gold at $3,000. Scaling the floor by the bucket's
    median close keeps the filter scale-invariant, which is the same
    argument that promoted the NB83 scale-invariant stop.

    Parameters
    ----------
    close : np.ndarray
        Per-bar close price (1s on the canonical corpus).
    price_pct : float
        Fraction of the bucket median close used as the USD floor.
        ``0.00025`` = 0.025% → $0.75 at gold $3,000, $25 at BTC $100k.
    recompute_secs : int
        Bucket size in seconds. Default 3600 (1 hour).
    times_utc_ns : np.ndarray | None
        Per-bar UTC nanosecond timestamp. When ``None`` the bucket index
        assumes 1 detector-bar per second (correct on the canonical 1s
        XAUUSD corpus).

    Returns
    -------
    np.ndarray
        Float64 array of shape ``(N,)`` with the per-bar wick floor in
        USD. Entries are 0.0 for invalid input.
    """
    n = int(close.shape[0])
    out = np.zeros(n, dtype=np.float64)
    if n == 0 or price_pct <= 0.0:
        return out

    if times_utc_ns is not None and np.asarray(times_utc_ns).shape[0] == n:
        bucket_secs = np.asarray(times_utc_ns, dtype=np.int64) // 1_000_000_000
        # Anchor every bar to the FIRST bar's bucket so the grid does
        # not drift with float rounding or intraday session boundaries.
        origin = int(bucket_secs[0])
        bucket_idx = (bucket_secs - origin) // int(recompute_secs)
    else:
        bucket_idx = np.arange(n, dtype=np.int64) // int(recompute_secs)

    n_buckets = int(bucket_idx.max()) + 1 if bucket_idx.size else 0
    if n_buckets == 0:
        return out

    bucket_medians = np.zeros(n_buckets, dtype=np.float64)
    for b in range(n_buckets):
        mask = bucket_idx == b
        if not mask.any():
            continue
        bucket_medians[b] = float(np.median(close[mask]))

    floor_per_bar = bucket_medians[bucket_idx] * float(price_pct)
    out[:] = np.where(
        np.isfinite(floor_per_bar) & (floor_per_bar > 0.0), floor_per_bar, 0.0
    )
    return out


# ────────────────────────────────────────────────────────────────────────────
# Fair Value Gap (FVG)
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class FvgZone:
    """A single detected FVG / iFVG zone.

    ``zone_low`` is the bottom of the gap (lower of the two non-touching
    candle extremes); ``zone_high`` is the top. For a bull FVG, the gap
    sits ABOVE the middle bar — the convention is ``zone_low > middle_high``
    but the zone itself is bracketed by the bar-1 high and bar-3 low (i.e.
    ``zone_low = bar_3_low``, ``zone_high = bar_1_high`` for the bullish
    case). See ``detect_fvg`` for the precise detection logic.
    """
    trigger_bar: int        # bar index where the 3-bar pattern closes
    direction: int          # +1 bull FVG, -1 bear FVG
    zone_low: float         # bottom of the gap
    zone_high: float        # top of the gap
    mitigated_bar: int = -1 # bar where the zone was first filled (-1 = unmitigated)
    mitigated_depth_pct: float = 0.0  # 0.0–1.0: fraction of the zone's height covered by
                                     # the mitigation bar's range. A wick-only touch is
                                     # ≈0; a full fill (bar range covers the entire zone)
                                     # is 1.0. ``fvg_min_mitigation_pct`` filters on this.
    pierced_bar: int = -1   # bar where close committed past the zone edge
                              # (suspicious — could be an SL hunt OR a real iFVG).
                              # Added 2026-08-20 to split "probe past zone" from
                              # "polarity flipped": a single wick through the zone
                              # is not an iFVG; only a follow-up RETEST from the
                              # OTHER side is. ``-1`` = never pierced.
    inverted_bar: int = -1  # bar where polarity flipped (-1 = not inverted).
                              # Now only set after the pierce + retest is
                              # confirmed (see ``fvg_require_retest_to_invert``
                              # on ``detect_fvg`` / ``TrendStrategyParams``).
    inverted: bool = False  # True once inverted
    consumed_bar: int = -1  # bar where the zone FIRED its retest signal (-1 = unused)
    expired_bar: int = -1   # bar where the zone exceeded its lifetime (-1 = not expired)
    superseded_bar: int = -1  # bar where a NEWER zone formed inside the same
                              # price range and superseded this one (-1 = still
                              # authoritative). Added 2026-08-20: when a new
                              # FVG overlaps an existing one, the older zone
                              # is no longer the "freshest level in this
                              # region" and shouldn't fire retest entries.
                              # ``fvg_supersede_on_new`` toggles this behavior.
    played_out_bar: int = -1  # bar where price moved PAST the zone in the
                              # FAVORABLE direction by a meaningful distance
                              # (≥ ``played_out_min_extension_usd`` USD beyond
                              # the zone edge). Added 2026-08-20: once the
                              # direction has "played out", re-entering on
                              # this zone is re-entering on a stale level —
                              # the gap-fill thesis is gone. The detector
                              # marks the zone dead at ``played_out_bar`` so
                              # neither the FVG nor the iFVG path can reopen
                              # a new position on it. (-1 = not played out).
    live: bool = True       # False once consumed, expired, superseded, OR played out
    # ── PIVOT F+I (added 2026-09-17) ─────────────────────────────────────
    # Track how many bars have had their range overlap the zone (i.e. real
    # touches, not just the final retest bar). Used by fvg_retest_signals
    # to enforce fvg_min_retest_count: the zone must have been touched N times
    # before the retest signal fires. Persisted on the zone so both FVG and
    # iFVG passes see the same touch count (they share the same zone list).
    n_touches: int = 0


def mitigation_touch_price(
    zone_low: float,
    zone_high: float,
    direction: int,
    bar_low: float,
    bar_high: float,
    bar_close: float,
    position_in_bar: float = 0.5,
) -> float | None:
    """Return the price at which a bar's range FIRST intersects an FVG zone.

    This is the canonical "mitigation touch" point — the price the chart
    annotation should plot on the zone rectangle when drawing the
    mitigation marker. The semantic: a **best-estimate of the actual
    fill price**, NOT the bar's deepest penetration.

    Two cases:

    * ``bar_close`` inside the zone (the standard mitigation semantic):
      the touch price is interpolated between the **zone's entry-side
      edge** (``zone_high`` for a bull FVG dip — price came from above;
      ``zone_low`` for a bear FVG spike — price came from below) and
      the bar's close at ``position_in_bar``. The entry-side edge is
      the FIRST tick that crossed into the zone; the close is the
      FINAL tick before the bar settled. The fill happened somewhere
      between these two prices.
    * bar's range straddles the zone but close is outside (a wick-only
      touch OR an overshoot): the touch price is interpolated between
      the **zone's entry-side edge** and the bar's close (which is
      outside the zone on the entry side — i.e. the bar pulled back
      out before settling).

    ``position_in_bar`` lets the user tune the fill-price assumption:

    * ``position_in_bar=0.0`` → returns the zone's entry-side edge
      (the FIRST tick that crossed into the zone — the canonical
      "mitigation touch" price for a bar whose sub-bar time series
      we don't have).
    * ``position_in_bar=0.5`` → midpoint between zone edge and bar
      close. A neutral default for the fill price.
    * ``position_in_bar=1.0`` → returns the bar's close (the FINAL
      tick before the bar settled).

    For close-inside-zone fills, the marker always sits INSIDE the
    zone rectangle by construction (interpolation between two
    zone-edge prices yields an in-zone result). For wick-only
    touches, the marker may sit outside the zone on the entry side —
    use a thin dashed connecting line to keep the visual link to the
    rectangle.

    Returns ``None`` when the bar's range misses the zone entirely.

    Added 2026-08-20 so the viz can plot mitigation markers at a
    best-estimate of the actual mitigation price instead of plotting
    ``bar_close`` at a price that's often well outside the gap (because
    the close that "filled" the gap is from the bar AFTER the gap was
    opened, and the bar can continue moving past the gap before
    settling). Updated 2026-08-20 to interpolate between the ZONE
    EDGE and the bar close (not the bar's range extremes), so the
    marker always sits inside the zone rectangle for close-inside
    fills — the previous version interpolated between bar_low/bar_high
    which produced markers OUTSIDE the zone when the bar's range
    extended far past the zone edges (the common case for a 1s bar).
    """
    if position_in_bar < 0.0 or position_in_bar > 1.0:
        raise ValueError(
            f"position_in_bar must be in [0, 1], got {position_in_bar}"
        )
    if bar_low > zone_high or bar_high < zone_low:
        return None  # bar missed the zone entirely
    # The touch price is interpolated between the ZONE'S ENTRY-SIDE
    # EDGE (the first tick that crossed into the zone) and the bar's
    # CLOSE (the final tick before the bar settled). This guarantees
    # the marker sits INSIDE the zone rectangle for close-inside-zone
    # fills (the detector's definition of mitigated).
    if direction > 0:  # bull FVG, zone above trigger, price dipping down from above
        entry_edge = float(zone_high)
    else:  # bear FVG, zone below trigger, price rising up from below
        entry_edge = float(zone_low)
    return entry_edge + (float(bar_close) - entry_edge) * position_in_bar


def _resample_bucket_index(
    times_utc_ns: np.ndarray,
    n: int,
    resample_to_n_secs: int,
) -> np.ndarray:
    """Bucket each 1s bar into its resample window.

    2026-10-23: BUCKET BY TIMESTAMP, NOT BY ROW POSITION.

    The original code used ``np.arange(n) // resample_to_n_secs``,
    which assumes a gapless feed: bar i is at second i. Real FX data
    has weekend/rollover gaps, so row position drifts away from
    wall-clock time and every bucket boundary is wrong by the
    accumulated gap.

    This is the same class of bug as the NB94 forward-leak in
    ``rolling_p95._downsample_to_inner`` (``reshape(-1, 3600)``
    assumes a gapless feed). Measured on XAUUSD_S1_1y: 923,993 of
    the 924k gaps are NOT multiples of 60s (e.g. 172,812s,
    176,401s, 190,863s), totalling 7,886,596 seconds of drift. A
    gap that IS a multiple of 60s does not change the grouping -- a
    gap that is not shifts every subsequent bucket boundary.

    ``times_utc_ns`` is REQUIRED for a correct resample. If it is not
    supplied we fall back to the positional bucketing and warn,
    because a silently-wrong bucket boundary corrupts every zone the
    detector emits.

    Extracted as a module-level function so it is directly testable
    without running the whole detector.
    """
    if times_utc_ns is not None and len(times_utc_ns) >= n:
        _tns = np.asarray(times_utc_ns, dtype=np.int64)[:n]
        # Bucket = floor((t - t0) / window). Timestamps are
        # non-decreasing on a real feed, so this is monotonic.
        _t0 = int(_tns[0])
        return ((_tns - _t0) // int(resample_to_n_secs * 1_000_000_000)
                ).astype(np.int64)
    import warnings as _warnings
    _warnings.warn(
        "detect_fvg: resample_to_n_secs>0 without times_utc_ns; "
        "falling back to POSITIONAL bucketing. This is wrong on "
        "any feed with gaps (weekends/rollovers) because row "
        "position drifts from wall-clock time.",
        RuntimeWarning, stacklevel=3)
    return (np.arange(n) // resample_to_n_secs).astype(np.int64)


def detect_fvg(
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    warmup: int = 0,
    max_active_zones: int = 0,
    resample_to_n_secs: int = 0,
    fvg_min_zone_usd: float = 0.0,
    max_zone_age_bars: int = 0,
    fvg_displacement_ratio: float = 0.0,
    fvg_body_definition: str = "body",
    fvg_min_zone_atr_mult: float = 0.0,
    atr_for_min_zone: float = 0.0,
    supersede_on_new: bool = False,
    invalidation_min_pierce_usd: float = 0.0,
    invalidation_min_consecutive_bars: int = 1,
    require_retest_to_invert: bool = False,
    played_out_min_extension_usd: float = 0.0,
    # ── 2026-09-15: body-only mitigation / invalidation knobs ───────
    # When ``body_only_mitigation=True``, mitigation fires only when
    # the candle BODY (open↔close range) crosses the zone edge — NOT
    # a wick-only touch. The bar's close-inside-zone check is
    # replaced by "open OR close on the wrong side AND the other end
    # on the original side", which is a body-cross.
    #
    # When ``body_only_invalidation=True``, a pierce requires the
    # bar's BODY to close past the zone edge, not just the wick. A
    # bar that opens past the zone but closes inside (or vice
    # versa) does NOT count as a pierce.
    body_only_mitigation: bool = False,
    body_only_invalidation: bool = False,
    # ── 2026-09-05: structure-driven FVG invalidation ───────────────
    # When provided, the detector ALSO invalidates an FVG when a
    # market-structure event in the OPPOSING direction fires after
    # the zone was formed. Concretely:
    #   * A BEAR structure event (BOS_BEAR, CHOCH_BEAR) invalidates
    #     bull FVGs (turns them into iFVGs).
    #   * A BULL structure event (BOS_BULL, CHOCH_BULL) invalidates
    #     bear FVGs.
    # This is a STRUCTURAL invalidation — no price-side pierce is
    # required. The rationale: a bearish structure event rejects the
    # structural thesis of any still-live bull FVG; the zone is
    # unlikely to fill profitably now that the trend has flipped.
    #
    # ``structure_events_per_bar`` is an int8 array of length N where
    # each entry is a ``StructureEvent`` value (or 0 = NONE). The
    # detector walks forward from each new zone's trigger_bar to
    # the end-of-data, marking the zone ``inverted=True`` at the
    # FIRST opposing-direction structure event.
    #
    # ``structure_invalidation_age_secs``: if > 0, the zone must be
    # YOUNGER than this (in wall-clock seconds from the trigger bar)
    # for the rule to fire. ``0`` (default) = unlimited age. We
    # convert to detector-bar count via the same per_bar_secs logic
    # as the age cap (see age_secs handling above).
    structure_events_per_bar: np.ndarray | None = None,
    structure_invalidation_age_secs: int = 0,
    times_utc_ns: np.ndarray | None = None,
    # ── 2026-09-17: minimum FVG lifetime filter ─────────────────────────
    # Empirically, ~9% of FVGs on 1s XAUUSD get mitigated or inverted on
    # bar+1 (the very next 1s bar after the consequent candle). These
    # "born-dead" zones have no tradeable follow-through — the market
    # filled the gap faster than any human or algorithmic entry could
    # react. When ``fvg_min_lifetime_secs > 0``, the detector drops any
    # zone whose FIRST end event (mitigated, inverted, superseded,
    # played-out, or struct-invalidated) happens at a wall-clock
    # delta smaller than this many seconds from the zone's trigger bar.
    # Zones that survive past the lifetime threshold pass through
    # unchanged — this only filters out the noise tail.
    #
    # NOTE: on 1s data (the only cadence this repo uses), 1 detector
    # bar == 1 wall-clock second. Identical to a bar-count — no
    # need to pass ``times_utc_ns`` unless using a higher timeframe.
    #
    # Default ``0`` = disabled (legacy behaviour, every zone returned).
    fvg_min_lifetime_secs: int = 0,
    # ── NB52 c-delta: strict-wick filter ──────────────────────────
    # When ``strict_wick_required=True`` AND the effective floor is
    # > 0, a candidate zone survives only if BOTH outer candles (c1
    # and c3) show a visible wick on the side AWAY from the gap.
    #
    # Rationale: a gap whose outer candles are pure bodies (no
    # rejection wick) was driven by a single impulse with no
    # opposing participation — historically the lowest-quality
    # imbalances. Requiring the wick keeps only gaps the market
    # actually rejected.
    #
    # The floor is SCALE-INVARIANT by default: pass the per-bar array
    # from :func:`compute_dynamic_wick_floor` and it wins over the
    # scalar. An absolute-USD scalar is scale-blind, which is why the
    # array form is the one the sweep driver uses.
    #
    # ``False`` / floor ``0.0`` = no filter (legacy behaviour).
    strict_wick_required: bool = False,
    strict_wick_min_wick_usd: float = 0.0,
    strict_wick_floor_usd_per_bar: np.ndarray | None = None,
) -> list[FvgZone]:
    """Detect all 3-bar Fair Value Gaps.

    A bull FVG forms at bar ``i`` when ``high[i-2] < low[i]`` — candle 1's
    high is below candle 3's low. The zone is ``[c1.H, c3.L]``. Per ICT
    (2026-08-19 user clarification), "c1 and c3's colors are irrelevant"
    — the bull/bear designation comes purely from the gap position, NOT
    from the middle candle's direction. (Earlier iterations required the
    middle candle to be bullish/bearish; that constraint was removed because
    it filtered out valid FVGs where the displacement candle happened to
    be a doji or wick-dominated bar.)

    A bear FVG is the mirror: ``high[i] < low[i-2]``, zone = ``[c3.H, c1.L]``.

    Parameters
    ----------
    open_, high, low, close : np.ndarray
        Raw OHLC arrays (any frequency — typically 1s).
    warmup : int
        Skip this many bars at the start of the array.
    max_active_zones : int
        **Cache cap (added 2026-08-18).** Maximum number of zones to
        keep alive simultaneously. When the live zone count exceeds
        this number, the OLDEST zones are dropped (LRU eviction by
        ``trigger_bar``). This bounds the mitigation-scan complexity
        at O(N × max_active_zones) instead of O(N × total_zones).
        Set to ``0`` (default) for unlimited (legacy behaviour).
        On 1s data this can drop 99%+ of stale zones; the live
        ``fvg_max_cache_size`` field on ``TrendStrategyParams``
        forwards to this argument.
    resample_to_n_secs : int
        **2026-08-18: FVG timeframe knob.** If > 0, internally
        aggregates OHLC into N-second bars BEFORE scanning for FVGs.
        This lets you detect "1-minute FVGs" (``resample_to_n_secs=60``)
        or "5-minute FVGs" (``resample_to_n_secs=300``) on a 1s
        data feed without pre-aggregating to a different
        parquet. The retest logic in ``fvg_retest_signals`` still
        operates on the original 1s bars — only the *zone
        detection* is at the higher timeframe. Useful because
        a 1m FVG is structurally different from a 1s FVG:
        the 1s FVG is often noise (a 5-cent gap that closes
        within 2 seconds) while a 1m FVG is a real displacement.
        ``0`` (default) keeps the legacy 1s behaviour.
    fvg_min_zone_usd : float
        **2026-08-18: minimum zone size.** Skip FVGs whose zone
        width (``zone_high - zone_low``) is below this threshold.
        Default ``0.0`` = no filter. Useful for cleaning up
        1s-bar noise FVGs that have width < ``TICK_VALUE_USD``
        = $0.01. Pass ``0.05`` (= 5 cents) for a "real" FVG
        filter.
    max_zone_age_bars : int
        **2026-08-18: zone lifetime cap.** Mark a zone as
        ``expired_bar`` (= ``trigger_bar + max_zone_age_bars``)
        once it has been alive longer than this many bars
        without being retested. ``0`` (default) = unlimited
        lifetime (legacy). For 1m FVG a sensible default is
        ``180`` (= 3 hours) — most 1m FVGs are retested within
        30 min; capping at 3h keeps the live zone list bounded.
Expired zones are NOT drawn by the visualizer and the
        retest scanner skips them (``live=False``).
    fvg_displacement_ratio : float
        **2026-08-19: displacement quality filter.** Require the
        middle candle's size to be at least this many times the
        LARGER of c1.size and c3.size. ``0.0`` (default) disables.
        ``2.0`` is the typical ICT "real displacement" threshold
        (c2 must be ≥2× the larger of c1/c3).
    fvg_body_definition : str
        **2026-08-19: how to measure candle size.**
        ``"body"`` (default) = abs(close - open); ``"range"``
        = high - low.
    fvg_min_zone_atr_mult : float
        **2026-08-20: ATR-relative zone-width filter.** If > 0,
        drop zones whose width (``zone_high - zone_low``) is below
        ``fvg_min_zone_atr_mult * atr_for_min_zone``. The
        motivation is that ``fvg_min_zone_usd`` is regime-blind —
        a $0.30 zone is thin during a 2-USD ATR session but
        noise during a 20-cent ATR session. The ATR multiplier
        makes the threshold adaptive. ``0.0`` (default) keeps
        the legacy absolute filter (``fvg_min_zone_usd``) only.
    atr_for_min_zone : float
        **2026-08-20: ATR scalar used by the ATR-relative filter.**
        Pass the recent regime ATR (e.g. ``compute_simple_atr(...)``
        at the dataset midpoint). Ignored when
        ``fvg_min_zone_atr_mult == 0``.
    supersede_on_new : bool
        **2026-08-20: supersede older zones when a new FVG overlaps.**
        When ``True`` (default at the strategy level), any live zone
        whose ``[zone_low, zone_high]`` is overlapped (by any amount)
        by a newly-formed zone's ``[zone_low, zone_high]`` is marked
        ``superseded_bar`` (and ``live=False``). Superseded zones do
        NOT fire retest entries — only the newest zone in a price
        region is structurally interesting. ``False`` (default at
        the detector level to keep the function deterministic) keeps
        the legacy "every live zone can fire" semantic. The
        ``fvg_supersede_on_new`` field on ``TrendStrategyParams``
        forwards to this argument.
    invalidation_min_pierce_usd : float
        **2026-08-20: minimum close-past-edge for pierce.** Require the
        close to extend BEYOND the zone edge by at least N USD before
        the bar counts as a pierce. A wick past the zone (where the
        close stays inside) is NOT a pierce. ``0.0`` (default) = any
        close on the wrong side counts. For 1s XAUUSD a sensible value
        is ``0.05``–``0.10``. See ``fvg_invalidation_min_pierce_usd``
        on ``TrendStrategyParams``.
    invalidation_min_consecutive_bars : int
        **2026-08-20: sustained pierce before inversion.** Require K
        consecutive closes on the wrong side before declaring the zone
        inverted. ``1`` (default) = legacy single-tick behaviour.
        ``3``–``5`` is a sensible 1s XAUUSD setting (kills SL-hunt
        false positives where one tick past the zone gets followed by
        a tick back inside). See
        ``fvg_invalidation_min_consecutive_bars`` on
        ``TrendStrategyParams``.
    require_retest_to_invert : bool
        **2026-08-20: distinguish pierce from iFVG.** When ``True``
        (recommended default at the strategy level), the zone's
        ``inverted_bar`` is NOT set on the pierce bar — it's set on
        the bar where price RETURNS to retest the zone from the OTHER
        side. Concretely: a bull FVG with a pierce at bar J is only
        confirmed inverted at the first bar ≥ J+1 whose close is back
        on the ORIGINAL side of the zone (``c > zone_high``). The
        pierce is recorded on ``FvgZone.pierced_bar`` either way.

        Why this matters: SL hunts on 1s XAUUSD often probe the zone
        edge by many ticks but never retest — the move is one-way
        through the zone. The previous detector (single-tick pierce
        = inversion) flipped polarity on these probes and forced a
        soft-stop on every open position sourced from the zone,
        right at the worst price in the move. With retest
        confirmation the probe is recorded but the inversion only
        fires when the market actually comes back to retest the
        invalidated zone — the iFVG semantic the textbook describes.

        ``False`` (default at the detector level, deterministic for
        callers that don't opt in) keeps the legacy single-tick
        pierce semantic. The ``fvg_require_retest_to_invert`` field
        on ``TrendStrategyParams`` (default ``True``) forwards to
        this argument.
    played_out_min_extension_usd : float
        **2026-08-20: played-out mark.** The minimum USD the close
        must extend BEYOND the zone edge in the FAVORABLE direction
        before the zone is marked played out (and dead forever).
        ``0.0`` (default) disables the played-out semantic.

    fvg_min_lifetime_secs : int
        **2026-09-17: minimum FVG lifetime filter.** Drop any zone
        whose first end event (mitigated / inverted / superseded /
        played-out / structure-invalidated) occurs within
        ``fvg_min_lifetime_secs`` detector bars of the zone's
        ``trigger_bar``. On 1s data this is a wall-clock second;
        the filter is purely bar-count. Default ``0`` = disabled.

    The output is a list of ``FvgZone`` objects with mitigation and
    inversion tracked forward through the rest of the data.
    ``max_active_zones`` bounds the live-zone bookkeeping so memory
    stays O(1) on long windows.
    """
    # ── Optional resample to a higher timeframe (2026-08-18) ─────────────
    # If ``resample_to_n_secs > 0``, aggregate OHLC to N-second bars
    # before scanning. The output zones' ``trigger_bar`` refers to
    # the *aggregated* bar index in the 1s timeline (i.e. the last
    # 1s bar in the N-bar group that produced the FVG). Mitigation
    # and inversion still scan the 1s close array for retest accuracy.
    n = close.shape[0]
    scan_low = low
    scan_high = high
    scan_open = open_
    scan_close = close
    scan_index_map: np.ndarray | None = None  # aggregated_idx -> last 1s idx
    if resample_to_n_secs > 0 and n > 0:
        # 2026-10-23: bucket by TIMESTAMP, not by row position. See
        # ``_resample_bucket_index`` for why (and for the measured
        # drift on the real corpus).
        bucket = _resample_bucket_index(times_utc_ns, n, resample_to_n_secs)
        # For each bucket, take the first open, max high, min low, last close
        # and remember the LAST 1s index in each bucket (so FVG trigger_bar
        # points at the right 1s bar).
        unique_buckets, bucket_starts = np.unique(bucket, return_index=True)
        # Last 1s index per bucket = bucket_starts + count - 1
        bucket_ends = np.empty_like(bucket_starts)
        for ub_idx in range(len(unique_buckets) - 1, -1, -1):
            end = bucket_starts[ub_idx + 1] if ub_idx + 1 < len(unique_buckets) else n
            bucket_ends[ub_idx] = end - 1
        scan_open = open_[bucket_starts]
        scan_close = close_[bucket_ends] if False else close[bucket_ends]  # noqa
        # 2026-10-25 — PERF FIX. This used to be:
        #
        #     for ub_idx, ub in enumerate(unique_buckets):
        #         mask = bucket == ub
        #         scan_high[ub_idx] = float(high[mask].max())
        #         scan_low[ub_idx]  = float(low[mask].min())
        #
        # i.e. ONE full-length comparison PER BUCKET. On the 1y corpus
        # that is 687,228 buckets x 41,233,730 bars = **28.3 TRILLION**
        # element-ops — roughly 8 hours for the 1y detector.
        #
        # ``_resample_bucket_index`` returns ``floor((t - t0) / window)``
        # on a non-decreasing timestamp series, so the bucket index is
        # MONOTONIC and the buckets are CONTIGUOUS runs of rows. That
        # makes the per-bucket max/min a SEGMENT REDUCTION, which numpy
        # does in O(n) via ``reduceat``.
        #
        # ``reduceat`` needs the segment START indices, and it reduces
        # ``arr[start[i] : start[i+1]]`` — so the last segment runs to
        # the end of the array. ``bucket_starts`` is exactly that, and
        # ``bucket_ends`` is the last row of each segment.
        #
        # EQUIVALENCE: the old loop computed, for each bucket, the max
        # and min over the rows whose bucket index equals that bucket.
        # Because the index is monotonic, those rows are exactly the
        # contiguous segment ``[bucket_starts[i], bucket_ends[i]]``.
        # ``reduceat`` computes the same max/min over the same rows.
        # Guarded by
        # ``tests/test_detector_cadence.py::test_resample_aggregation_matches_the_loop``
        # which compares the vectorised form against the original loop
        # on synthetic data with gaps.
        if len(unique_buckets) > 0:
            scan_high = np.maximum.reduceat(high, bucket_starts)
            scan_low = np.minimum.reduceat(low, bucket_starts)
        else:
            scan_high = np.zeros(0, dtype=np.float64)
            scan_low = np.zeros(0, dtype=np.float64)
        scan_index_map = bucket_ends
        n = len(unique_buckets)

    zones: list[FvgZone] = []
    # ── Live-candidate list for the supersession scan (2026-10-21) ──
    # The supersession rule asks "which LIVE zones overlap this new
    # zone's price range?". Scanning every zone ever created is O(Z²) —
    # on 1y XAUUSD (117k zones) that is ~10^10 comparisons and the
    # detector never terminates.
    #
    # Supersession kills ~99% of zones during the detection loop (see
    # the ``superseded`` counters), so a candidate list compacted to
    # LIVE zones only is O(live) per new zone, which is tiny. The
    # compaction runs every ``_SUP_COMPACT_EVERY`` inserts.
    _live_candidates: list[FvgZone] = []
    if n < 3:
        return zones

    # LRU eviction helper (added 2026-08-18). Bounds the live candidate
    # list when ``max_active_zones`` is set; no-op when it is 0 (the
    # compaction below is the effective bound in that case).
    def _evict_oldest() -> None:
        if max_active_zones <= 0 or len(_live_candidates) <= max_active_zones:
            return
        oldest_idx = min(range(len(_live_candidates)),
                         key=lambda i: _live_candidates[i].trigger_bar)
        del _live_candidates[oldest_idx]

    # Compaction cadence for the supersession candidate list.
    #
    # The scan tests every candidate for ``z.live``; nearly all are dead
    # by the time the next zone forms. Compacting every ``_SUP_COMPACT_EVERY``
    # inserts drops the dead entries so the scan cost tracks the LIVE zone
    # count rather than the cumulative zone count.
    _SUP_COMPACT_EVERY = 512
    _since_compact = 0
    _det_n = 0
    _det_t0 = _time.perf_counter()
    # ── Price-bucket spatial index for supersession (2026-10-21) ───
    # The supersession test is a PRICE-RANGE OVERLAP test. Indexing
    # live candidates by price bucket lets the scan touch only the
    # candidates whose price range can possibly overlap the new zone,
    # instead of every live candidate ever created. Semantically
    # identical (the overlap predicate is unchanged) but O(overlaps)
    # instead of O(all live candidates).
    #
    # Bucket width: gold's intraday range on 1s bars is well under
    # $100, and the median FVG zone is ~$0.36 wide. A $5.00 bucket
    # puts ~14 median zones in a bucket — small enough to be selective,
    # large enough that a typical zone spans 1-2 buckets.
    _SUP_BUCKET_USD = 5.0
    _sup_buckets: dict[int, list] = {}

    # ── Vectorised candidate pre-pass (2026-10-21) ─────────────
    # The per-bar loop below evaluated the gap test and the three
    # static filters on EVERY bar in Python. On 1s XAUUSD that is
    # 41M iterations of pure interpreter overhead — the 1y sweep's
    # "non-termination" wall, independent of the supersession scan.
    #
    # Every filter in this loop is elementwise, so the whole candidate
    # set can be computed with numpy in a single pass (~0.06s for
    # 41M bars) and the loop then iterates only over SURVIVING
    # candidates (~1.8M on 1y, 9.7k on 1 month).
    _start = max(2, warmup)
    # For trigger bar ``i``: bull iff high[i-2] < low[i]; bear iff
    # high[i] < low[i-2]. Both masks are indexed at ``i - _start``.
    _i = np.arange(_start, n, dtype=np.int64)
    _h2 = scan_high[_i - 2]
    _l0 = scan_low[_i]
    _h0 = scan_high[_i]
    _l2 = scan_low[_i - 2]
    _is_bull = _h2 < _l0
    _is_bear = _h0 < _l2
    # Zone bounds per direction.
    _zl_bull, _zh_bull = _h2, _l0
    _zl_bear, _zh_bear = _h0, _l2
    # Direction: bull wins when both are true (matches the original
    # ``if bull_gap: ... elif bear_gap: ...`` precedence).
    _dir = np.where(_is_bull, 1, -1).astype(np.int8)
    _keep = _is_bull | _is_bear
    _zl = np.where(_is_bull, _zl_bull, _zl_bear)
    _zh = np.where(_is_bull, _zh_bull, _zh_bear)

    # ── Displacement quality filter (vectorised) ────────────────
    if fvg_displacement_ratio > 0.0:
        if fvg_body_definition == "range":
            _c1 = scan_high[_i - 2] - scan_low[_i - 2]
            _c2 = scan_high[_i - 1] - scan_low[_i - 1]
            _c3 = scan_high[_i] - scan_low[_i]
        else:
            _c1 = np.abs(scan_close[_i - 2] - scan_open[_i - 2])
            _c2 = np.abs(scan_close[_i - 1] - scan_open[_i - 1])
            _c3 = np.abs(scan_close[_i] - scan_open[_i])
        _max_outer = np.maximum(_c1, _c3)
        # ``max_outer <= 0`` (both outer candles doji) drops the zone.
        _disp_pass = (_max_outer > 0.0) & (
            _c2 >= fvg_displacement_ratio * _max_outer)
        _keep &= _disp_pass

    # Absolute minimum zone width.
    if fvg_min_zone_usd > 0:
        _keep &= (_zh - _zl) >= fvg_min_zone_usd
    # ATR-relative minimum zone width (absolute ATR scalar).
    if fvg_min_zone_atr_mult > 0 and atr_for_min_zone > 0:
        _keep &= (_zh - _zl) >= (fvg_min_zone_atr_mult * atr_for_min_zone)

    # ── NB52 strict-wick filter (vectorised) ─────────────────────
    # Both OUTER candles must show a visible wick on the side AWAY
    # from the gap. For a bull gap (c1 above, c3 below) that is c1's
    # DOWN wick and c3's UP wick; a bear gap is the mirror.
    #
    # ``_c1_body_lo`` is min(open, close) of bar i-2 and
    # ``_c3_body_hi`` is max(open, close) of bar i, so the wick
    # lengths are body-edge to bar extreme — a body-only candle
    # yields 0.0 on the relevant side and is rejected.
    #
    # This runs in the pre-pass (not the candidate loop) for the
    # same reason the other filters do: it is elementwise, so
    # evaluating it on all 41M bars in numpy costs ~0.06s whereas
    # adding it to the candidate loop would re-derive the wick
    # arithmetic per surviving candidate.
    if strict_wick_required:
        if strict_wick_floor_usd_per_bar is not None:
            _wfloor_arr = np.asarray(strict_wick_floor_usd_per_bar)
            # Index the per-bar floor at the SAME 1s bar indices the
            # candidate loop uses (``scan_index_map``), so a resampled
            # scan looks up the right bar's floor.
            if scan_index_map is not None:
                _wfloor = _wfloor_arr[
                    np.clip(scan_index_map[_i], 0, _wfloor_arr.shape[0] - 1)
                ]
            else:
                _wfloor = _wfloor_arr[np.clip(_i, 0, _wfloor_arr.shape[0] - 1)]
        else:
            _wfloor = np.full(_i.shape, float(strict_wick_min_wick_usd))
        _wfloor = np.where(np.isfinite(_wfloor), _wfloor, 0.0)

        _c1_body_lo = np.minimum(scan_open[_i - 2], scan_close[_i - 2])
        _c1_body_hi = np.maximum(scan_open[_i - 2], scan_close[_i - 2])
        _c3_body_lo = np.minimum(scan_open[_i], scan_close[_i])
        _c3_body_hi = np.maximum(scan_open[_i], scan_close[_i])

        # Bull: c1 wick DOWN, c3 wick UP. Bear: c1 wick UP, c3 wick DOWN.
        _c1_wick_bull = _c1_body_lo - scan_low[_i - 2]
        _c3_wick_bull = scan_high[_i] - _c3_body_hi
        _c1_wick_bear = scan_high[_i - 2] - _c1_body_hi
        _c3_wick_bear = _c3_body_lo - scan_low[_i]

        _c1_wick = np.where(_is_bull, _c1_wick_bull, _c1_wick_bear)
        _c3_wick = np.where(_is_bull, _c3_wick_bull, _c3_wick_bear)

        # A floor of 0.0 disables the test entirely (matches the
        # scalar ``0 = no filter`` contract).
        _active_wick = _wfloor > 0.0
        if _active_wick.any():
            _keep &= (
                ~_active_wick
                | ((_c1_wick >= _wfloor) & (_c3_wick >= _wfloor))
            )

    _cand = np.flatnonzero(_keep)
    for _ci in _cand:
        i = int(_i[_ci])
        direction = int(_dir[_ci])
        zl, zh = float(_zl[_ci]), float(_zh[_ci])
        # NOTE: the displacement, absolute-min-width and ATR-relative
        # filters were all applied in the vectorised pre-pass above and
        # are NOT re-checked here (that would be a second pass).
        trigger_bar_1s = int(scan_index_map[i]) if scan_index_map is not None else i
        new_zone = FvgZone(
            trigger_bar=trigger_bar_1s,
            direction=direction,
            zone_low=zl,
            zone_high=zh,
        )
        # ── Supersession on price-range overlap (added 2026-08-20) ─────
        # When a NEW zone's [zone_low, zone_high] overlaps an existing
        # LIVE zone's [zone_low, zone_high] by any amount, mark the older
        # zone as superseded (live=False, superseded_bar=new_trigger_bar).
        # The hypothesis: only the freshest level in a price region is
        # structurally interesting — older zones in the same region have
        # been overwritten by the new displacement. ``supersede_on_new``
        # defaults to ``False`` at the detector level (deterministic for
        # callers that don't opt in); the strategy forwards
        # ``fvg_supersede_on_new`` (default ``True``) to enable.
        # The overlap test is on PRICE RANGE only — any direction
        # overlap counts. We do NOT supersede zones that are already
        # consumed/expired/inverted (they're already dead) and we do
        # NOT supersede zones whose trigger_bar is >= the new zone's
        # (which would only happen for same-bar multi-zone cases; the
        # detector only emits one zone per bar in practice).
        # 2026-08-20: the time component of supersession. A new zone
        # only supersedes the old one if the old zone was STILL ALIVE
        # at the moment the new zone formed. If the old zone had
        # already played out / expired / inverted / been superseded
        # by an earlier zone, the new zone is just an unrelated event
        # landing in the same price region — it doesn't "kill" the
        # old zone. The natural-death timestamps:
        #
        #   played_out_bar  ≥ 0 → old zone played out (direction ran)
        #   expired_bar     > 0 → old zone hit max lifetime
        #   superseded_bar  ≥ 0 → old zone already superseded (skip)
        #   inverted_bar    ≥ 0 → old zone flipped polarity
        #   mitigated_bar   ≥ 0 → old zone was filled (note: NOT a
        #     "natural death" — mitigation is shallow enough that the
        #     zone is still watchable for retest. Mitigation alone
        #     doesn't block supersession.)
        #
        # Without the time check, every overlapping zone within the
        # cache's lifetime could recolor a dead zone as superseded.
        # Price-bucket range for the new zone — used by the supersession
        # scan below AND to register the zone in the spatial index.
        _b_lo = int(new_zone.zone_low // _SUP_BUCKET_USD)
        _b_hi = int(new_zone.zone_high // _SUP_BUCKET_USD)
        if supersede_on_new:
            new_bar = new_zone.trigger_bar
            # PERF (2026-10-21): the candidate list is bounded by a
            # PRICE-BUCKET spatial index, not by compaction alone.
            # Compaction only drops DEAD (superseded) zones; zones that
            # are never overlapped by a later zone survive forever and
            # accumulate (1y XAUUSD reached ~2,900 by zone 650k,
            # degrading the scan rate from 7,669/s to ~1,500/s).
            #
            # A zone can only be superseded by a NEWER zone whose price
            # range OVERLAPS its own. So instead of scanning every live
            # candidate, index live candidates by PRICE BUCKET and only
            # test the buckets the new zone actually spans. This is
            # EXACTLY equivalent (the overlap test is unchanged) and
            # reduces the scan from O(all live) to O(overlapping price
            # buckets).
            candidates = []
            for _b in range(_b_lo, _b_hi + 1):
                candidates.extend(_sup_buckets.get(_b, ()))

            for z in candidates:
                if not z.live:
                    continue
                # Skip zones that have already played out, expired,
                # or been superseded before the new zone formed.
                if z.played_out_bar >= 0 and new_bar >= z.played_out_bar:
                    continue
                if z.expired_bar > 0 and new_bar >= z.expired_bar:
                    continue
                if z.superseded_bar >= 0:
                    continue  # already dead via earlier supersession
                if z.inverted_bar >= 0 and new_bar >= z.inverted_bar:
                    continue
                # Price-range overlap test: any interval overlap counts.
                # Two intervals [a, b] and [c, d] overlap iff a <= d AND c <= b.
                if z.zone_low <= new_zone.zone_high and new_zone.zone_low <= z.zone_high:
                    # 2026-10-23: RECORD, DO NOT KILL. The supersession
                    # DECISION is deferred to after the mitigation /
                    # inversion walk (see the walk loop). Killing the
                    # zone here made the inversion escape hatch
                    # unreachable, because ``z.inverted_bar`` is always
                    # -1 during detection.
                    z.superseded_bar = new_bar
        zones.append(new_zone)
        _live_candidates.append(new_zone)
        # Register the new zone in the price-bucket spatial index so the
        # next zone's supersession scan can find it. A zone is inserted
        # into every bucket its price range spans.
        for _b in range(_b_lo, _b_hi + 1):
            _sup_buckets.setdefault(_b, []).append(new_zone)
        _evict_oldest()
        _since_compact += 1
        if _since_compact >= _SUP_COMPACT_EVERY:
            _since_compact = 0
            _live_candidates[:] = [z for z in _live_candidates if z.live]
        # Progress (2026-10-21 diagnostic): report detection-loop rate and
        # the live-candidate list size (the supersession scan cost).
        _det_n += 1
        if _det_n % 50_000 == 0:
            _el = _time.perf_counter() - _det_t0
            _sys.stderr.write(
                f"[detect_fvg] detect: zones={_det_n} elapsed={_el:.1f}s "
                f"rate={_det_n/_el:.0f}/s live_cands={len(_live_candidates)}\n")
            _sys.stderr.flush()

    # Track mitigation (any close inside zone → filled) and inversion
    # (close fully through opposite side → polarity flipped).
    # ALSO mark zones as expired when they exceed ``max_zone_age_bars``
    # of lifetime without being retested (added 2026-08-18).
    #
    # 2026-08-19 (user clarification): a zone can be BOTH mitigated AND
    # inverted. The user's scenario: "the first bar is an fvg, and is
    # converted to an ifvg about 20 bars away". The price first fills
    # the zone (mitigation, often a wick touch that closes inside), then
    # later pushes THROUGH it (inversion — close ends on the opposite
    # side of the zone). The detector previously broke on first mitigation
    # so it never saw the inversion. We now scan for both events
    # independently and record whichever happens first.
    #
    # 2026-08-20 BUGFIX: the mitigation scan was using the resampled
    # walk length (``n`` was reassigned to ``len(unique_buckets)`` when
    # ``resample_to_n_secs > 0``) but the ``trigger_bar`` on each zone
    # is in 1S space. The loop ``for j in range(z.trigger_bar + 1, n)``
    # therefore terminated early (after a few thousand 1s bars) and
    # missed most of the mitigation/inversion events the user could
    # see on the chart. The fix is to scan in 1s space using the
    # original ``close/high/low`` arrays and ``close.shape[0]`` as the
    # walk limit.
    n_1s = close.shape[0]
    _zonestep_t0 = _time.perf_counter()
    # Count live zones ONCE. Recomputing it inside the progress tick
    # would be O(n) per tick and O(n^2 / 50k) across the walk — that is
    # what stretched a ~15-minute detector step into hours.
    _live_zone_count = sum(
        1 for zz in zones if zz.live and zz.superseded_bar < 0
    )
    for z_idx, z in enumerate(zones):
        # ── DEFERRED SUPERSESSION (2026-10-23) ──────────────────────
        # THE STRUCTURAL BUG. Supersession used to be applied DURING
        # detection (phase 2), and the mitigation/inversion walk runs
        # AFTER all detection (phase 3). So during the supersession
        # scan ``z.inverted_bar`` was ALWAYS -1, which made the escape
        # hatch at the supersession check (``if z.inverted_bar >= 0``)
        # structurally unreachable: a zone that had ALREADY inverted
        # was still killed by the next overlapping zone.
        #
        # Worse, the walk SKIPPED superseded zones entirely, so a zone
        # that was about to invert never got the chance. Measured on
        # 2026-01 (1m cadence):
        #
        #     zones 4,767  superseded 94.2%  inverted 130 (4.2/day)
        #
        # The user expects ~30 snipers/day. Turning supersession OFF
        # gives 4,189 inversions (135/day) -- but that also keeps every
        # stale zone alive, which is the opposite extreme.
        #
        # THE FIX: supersession is recorded during detection (so the
        # spatial index and the audit trail stay intact), but the
        # DECISION is deferred to the walk: a zone that inverted
        # before it was superseded stays alive as a valid iFVG
        # source. A zone that was superseded and never inverted dies.
        #
        # ``superseded_bar`` records WHEN the newer zone formed.
        # ``inverted_bar`` records WHEN polarity flipped. If
        # ``inverted_bar >= 0`` and ``inverted_bar <= superseded_bar``,
        # the zone inverted FIRST and is a legitimate iFVG.
        if not z.live:
            continue
        # NOTE: superseded zones ARE walked. A superseded zone that
        # inverted BEFORE the superseding zone formed is a legitimate
        # iFVG (the "sniper" signal), and we cannot know that until
        # the walk has run. Skipping them here is what made the
        # inversion escape hatch unreachable. The supersession
        # DECISION is made after the walk, in the deferred-resolution
        # pass.
        # Set the expiry bar upfront if a lifetime cap is configured.
        # ``max_zone_age_bars`` is in the resampled cadence (the
        # detector's bar space), so convert to 1s bars when resampling
        # is active so the wall-clock lifetime is consistent.
        if max_zone_age_bars > 0:
            rs = int(resample_to_n_secs) if resample_to_n_secs > 0 else 1
            z.expired_bar = z.trigger_bar + max_zone_age_bars * rs
        # Track the deepest mitigation bar (deepest = largest fraction
        # of the zone's height covered by the bar's range). A wick-only
        # touch gets depth ≈ 0; a full fill (bar range covers the entire
        # zone) gets depth == 1. ``fvg_min_mitigation_pct`` filters on
        # this so the retest scanner can skip zones that were only
        # wick-touched (a wick is not a real mitigation).
        deepest_pct = 0.0
        deepest_bar = -1
        # ── Vectorised mitigation/inversion scan (2026-09-15) ─────
        # The original implementation iterated per-bar in Python and
        # called ``min()``/``max()`` on scalars 4× per bar per zone
        # (~892k calls/day per backtest). On 1s data with 50 live
        # zones × 67k bars/day, that's the dominant cost (~0.78s/day
        # = ~6h for a year). Vectorise: slice the bars from
        # ``trigger_bar+1`` to ``n_1s`` (or expiry) and use numpy
        # boolean masks + ``np.argmax`` to find the FIRST bar where
        # each event fires. ``n_1s`` is ~67k for a 1-day slice; one
        # vectorised pass costs ~0.5–1ms — 100-1000× faster than the
        # Python loop. The semantics are preserved exactly: same
        # ``mitigated_bar`` (first qualifying bar), same
        # ``mitigated_depth_pct`` (deepest overlap), same
        # ``pierced_bar`` / ``inverted_bar`` / ``played_out_bar``
        # logic (including the consecutive-bars streak for inversion).
        # ── Walk bound (2026-10-21 perf fix) ─────────────────────────
        # A zone's economic lifetime is the detector's own cadence window.
        # Walking past that bound cannot change any tradeable signal (the
        # strategy won't touch a zone older than its own detector cadence).
        # When max_zone_age_bars IS set, the explicit expiry is
        # authoritative. Otherwise cap at the canonical 3600-bar window.
        _cap = (
            int(max_zone_age_bars) * (int(resample_to_n_secs) or 1)
            if max_zone_age_bars > 0 else 3_600
        )
        start_bar = z.trigger_bar + 1
        end_bar = (
            z.expired_bar if z.expired_bar > 0
            else min(n_1s, start_bar + _cap)
        )
        # 2026-10-23: A zone's life ends at the FIRST of (expiry,
        # supersession, walk cap). The supersession bar is the bar on
        # which a newer overlapping zone formed -- after that the old
        # zone is no longer the authoritative one, so it cannot invert
        # any more. Capping the walk here is what makes the deferred
        # supersession decision meaningful: a zone that inverted
        # BEFORE this bar keeps its signal; one that did not, dies.
        if z.superseded_bar >= 0:
            end_bar = min(end_bar, z.superseded_bar + 1)
        if start_bar < end_bar:
            # ── CADENCE-CONSISTENT INVALIDATION (2026-10-23) ──────
            # The zone is DETECTED on the resampled cadence (1m), so
            # its invalidation must be evaluated on the SAME cadence.
            # The walk used to slice the RAW 1s arrays
            # (``close[sl]``, ``high[sl]``, ``low[sl]``), which meant:
            #
            #   * ``invalidation_min_consecutive_bars=2`` counted TWO
            #     1-SECOND bars, not two 1-MINUTE bars;
            #   * ``body_only_invalidation`` tested 1s candle bodies,
            #     so a single 1s wick through the zone edge counted
            #     as a full-body pierce;
            #   * a zone could be "invalidated" by a fraction of a
            #     cent of 1s noise before the 1m candle had even
            #     closed.
            #
            # The user caught this: "invalidation still checks at 1
            # second bodies". The fix is to walk the RESAMPLED arrays
            # and map the result back to 1s indices.
            if scan_index_map is not None:
                # Aggregate bar index of the zone's trigger, and the
                # aggregate bar index one past the walk end.
                _agg_start = int(np.searchsorted(scan_index_map, start_bar,
                                                 side="right"))
                _agg_end = int(np.searchsorted(scan_index_map, end_bar,
                                               side="right"))
                _agg_start = max(0, min(_agg_start, scan_index_map.size))
                _agg_end = max(_agg_start, min(_agg_end, scan_index_map.size))
                _walk_close = scan_close
                _walk_high = scan_high
                _walk_low = scan_low
                _walk_open = scan_open
                _walk_from = _agg_start
                _walk_to = _agg_end
            else:
                _walk_close = close
                _walk_high = high
                _walk_low = low
                _walk_open = open_
                _walk_from = start_bar
                _walk_to = end_bar
            if _walk_from < _walk_to:
                # Slice views (no copy) — numpy ufuncs on these slices
                # are the hot path.
                sl = slice(_walk_from, _walk_to)
                seg_close = _walk_close[sl]
                seg_high = _walk_high[sl]
                seg_low = _walk_low[sl]
                seg_open = _walk_open[sl]
                # Map an aggregate-space local index back to 1s space.
                if scan_index_map is not None:
                    def _to_1s(_local: int) -> int:
                        # Clamp: ``_walk_to`` can be one past the last
                        # aggregate bar, and the last local index can
                        # point at the final 1s bar.
                        _k = _walk_from + _local
                        if _k < 0:
                            _k = 0
                        elif _k >= scan_index_map.size:
                            _k = scan_index_map.size - 1
                        return int(scan_index_map[_k])
                else:
                    def _to_1s(_local: int) -> int:
                        return int(_walk_from + _local)
            zl = float(z.zone_low)
            zh = float(z.zone_high)
            zone_h = zh - zl
            # ── Depth (vectorised across all bars in segment) ──
            if zone_h > 0:
                # overlap = max(0, min(bar_high, zh) - max(bar_low, zl))
                # All numpy primitives — no Python min/max per bar.
                overlap = np.clip(
                    np.minimum(seg_high, zh) - np.maximum(seg_low, zl),
                    0.0, None,
                )
                seg_depth_pct = overlap / zone_h
            else:
                seg_depth_pct = np.zeros(end_bar - start_bar, dtype=np.float64)
            # ── Mitigation ──
            # Legacy: close inside zone. Body-only: body crosses
            # the zone edge. We compute BOTH and select via the
            # ``body_only_mitigation`` flag below.
            in_zone_close = (seg_close >= zl) & (seg_close <= zh)
            if body_only_mitigation:
                if z.direction == 1:
                    # Bull: body crosses zone from above.
                    body_crossed = (
                        ((seg_open <= zl) & (seg_close >= zl)) |
                        ((seg_open >= zl) & (seg_close <= zl)) |
                        ((seg_close >= zl) & (seg_open >= zl) & ((seg_open <= zh) | (seg_close <= zh)))
                    )
                else:
                    # Bear: body crosses zone from below.
                    body_crossed = (
                        ((seg_open >= zh) & (seg_close <= zh)) |
                        ((seg_open <= zh) & (seg_close >= zh)) |
                        ((seg_close <= zh) & (seg_open <= zh) & ((seg_open >= zl) | (seg_close >= zl)))
                    )
                mit_mask = body_crossed
            else:
                mit_mask = in_zone_close
            # First bar where mitigation fires (relative-to-segment index).
            mit_idx_local = -1
            if mit_mask.any():
                mit_idx_local = int(np.argmax(mit_mask))
                # argmax returns the FIRST True — that's exactly the
                # first qualifying bar in the Python loop.
            # ── Deepest depth bar ──
            # Python loop tracks ``if depth_pct > deepest_pct`` —
            # the FIRST bar where depth is maximum. Numpy equivalent:
            #
            # GUARD: ``seg_depth_pct`` is empty when the zone triggers
            # on the final bar(s) of the file, so there is no segment
            # to scan. ``np.argmax`` on an empty array raises, which
            # crashed detect_fvg outright on short inputs.
            if seg_depth_pct.size > 0:
                deepest_local = int(np.argmax(seg_depth_pct))
                deepest_pct_val = float(seg_depth_pct[deepest_local])
                if deepest_pct_val > deepest_pct:
                    deepest_pct = deepest_pct_val
                    deepest_bar = _to_1s(deepest_local)
            # ── PIVOT F+I (2026-09-17): touch count ─────────────────
            # Count how many bars had their range OVERLAP the zone (any
            # overlap, not just close-inside-zone). A bar's range touches
            # the zone when high >= zone_low AND low <= zone_high.
            # This feeds fvg_min_retest_count in the retest scanner.
            if zone_h > 0:
                range_overlaps = (
                    (seg_high >= zl) & (seg_low <= zh)
                )
                z.n_touches = int(np.count_nonzero(range_overlaps))
            # ── Pierce / inversion (consecutive-bars streak) ──
            # Vectorise the per-bar pierce boolean, then find the
            # FIRST run of K consecutive True bars via numpy stride
            # tricks (or argmax of a rolling-streak counter).
            pierce = float(invalidation_min_pierce_usd)
            if body_only_invalidation:
                if z.direction == 1:
                    pierce_mask = (seg_open <= zl - pierce) & (seg_close <= zl - pierce)
                else:
                    pierce_mask = (seg_open >= zh + pierce) & (seg_close >= zh + pierce)
            else:
                if z.direction == 1:
                    pierce_mask = seg_close < zl - pierce
                else:
                    pierce_mask = seg_close > zh + pierce
            needed = max(1, int(invalidation_min_consecutive_bars))
            pierced_bar_local = -1
            inverted_bar_local = -1
            if pierce_mask.any():
                # Compute the streak of consecutive True values via
                # cumsum + last-false-index trick (vectorised).
                false_idx = np.where(~pierce_mask)[0]
                k = np.arange(pierce_mask.size, dtype=np.int64)
                if false_idx.size == 0:
                    # All True: streak at k = k + 1.
                    streak_at = (k + 1).astype(np.int32)
                else:
                    # For each k, find the position in false_idx of
                    # the LAST False ≤ k. ``searchsorted(side='left')``
                    # returns the position where ``k`` would be
                    # inserted to keep ``false_idx`` sorted, which is
                    # the count of False values ≤ k. The last False ≤
                    # k is at index ``count - 1`` if count > 0,
                    # otherwise there's no False ≤ k and streak_at[k]
                    # should be k+1.
                    count_le_k = np.searchsorted(false_idx, k, side='left')
                    has_false_le = count_le_k > 0
                    # Where has_false_le, last_false_idx = false_idx[count_le_k - 1].
                    # Where not, last_false_idx = -1.
                    last_false_idx = np.where(
                        has_false_le,
                        false_idx[np.maximum(count_le_k - 1, 0)],
                        -1,
                    )
                    streak_at = (k - last_false_idx).astype(np.int32)
                    # streak_at is the count of consecutive Trues
                    # ending at k (0 when pierce_mask[k] is False,
                    # ≥1 when True).
                # First k where streak_at[k] >= needed AND pierce_mask[k].
                qual = (streak_at >= needed) & pierce_mask
                if qual.any():
                    # Local index of the streak-completing bar.
                    streak_complete_local = int(np.argmax(qual))
                    # ``pierced_bar`` is the FIRST bar that COMPLETED
                    # the streak — i.e., ``needed - 1`` bars back from
                    # here (the Python loop set
                    # ``z.pierced_bar = j - needed + 1``).
                    pierced_bar_local = max(0, streak_complete_local - needed + 1)
                    # Inversion under ``require_retest_to_invert``:
                    # find the FIRST bar AFTER the streak-completing
                    # bar where price returns to the original side.
                    if require_retest_to_invert:
                        if z.direction == 1:
                            retest_mask = seg_close > zh
                        else:
                            retest_mask = seg_close < zl
                        retest_search_start = streak_complete_local + 1
                        if retest_search_start < retest_mask.size:
                            retest_seg = retest_mask[retest_search_start:]
                            if retest_seg.any():
                                inverted_bar_local = retest_search_start + int(np.argmax(retest_seg))
                    else:
                        # Legacy: sustained pierce alone flips the
                        # zone (use the streak-completing bar).
                        inverted_bar_local = streak_complete_local
            # ── Played-out check (vectorised) ──
            played_out_bar_local = -1
            ext = float(played_out_min_extension_usd)
            if ext > 0:
                if z.direction == 1:
                    po_mask = seg_close > zh + ext
                else:
                    po_mask = seg_close < zl - ext
                if po_mask.any():
                    played_out_bar_local = int(np.argmax(po_mask))
            # ── Write results back to the zone dataclass ──
            # Map aggregate-space local indices back to 1s space so the
            # rest of the engine (which indexes 1s bars) stays correct.
            if mit_idx_local >= 0:
                z.mitigated_bar = _to_1s(mit_idx_local)
            if pierced_bar_local >= 0:
                z.pierced_bar = _to_1s(pierced_bar_local)
            if inverted_bar_local >= 0:
                z.inverted_bar = _to_1s(inverted_bar_local)
                z.inverted = True
            if played_out_bar_local >= 0:
                z.played_out_bar = _to_1s(played_out_bar_local)
                z.live = False
        # Persist the deepest mitigation depth so the retest scanner
        # can filter on ``fvg_min_mitigation_pct``.
        z.mitigated_depth_pct = float(deepest_pct)
        # Promote the deepest-mitigation bar to ``mitigated_bar`` if the
        # close-inside-zone trigger never fired. Otherwise most zones
        # would have ``mitigated_bar=-1`` because price typically
        # PIERCES through the zone (on its way to inverting it) without
        # ever closing inside it — and the chart would only show a
        # mitigation marker for the rare bar that settled in the zone.
        # A wick-overlap of ≥ 50% of the zone's height is the user's
        # threshold for "the zone was actually filled". For visual
        # purposes we use 0.5 here (matches the swatch colour for the
        # "deep mitigation" tier in the chart legend). The retest
        # scanner's ``fvg_min_mitigation_pct`` knob still controls
        # which zones actually generate entries — this only ensures the
        # CHART can mark every meaningful fill.
        if z.mitigated_bar < 0 and deepest_pct >= 0.5 and deepest_bar >= 0:
            z.mitigated_bar = int(deepest_bar)
        # Periodic progress (2026-10-21 diagnostic): emit to stderr every
        # 50,000 zones during the mitigation walk so the operator can
        # confirm progress vs. stall. The print is unconditionally cheap
        # (one sys call per 50k zones) and does not affect results.
        if z_idx and z_idx % 50_000 == 0:
            _elapsed = _time.perf_counter() - _zonestep_t0
            _rate = z_idx / _elapsed if _elapsed > 0 else 0.0
            _live_so_far = _live_zone_count
            _sys.stderr.write(
                f"[detect_fvg] zones={z_idx}/{len(zones)} "
                f"elapsed={_elapsed:.1f}s rate={_rate:.0f}/s "
                f"live={_live_so_far}\n")
            _sys.stderr.flush()
        # If the loop finished naturally without mitigation OR inversion
        # OR expiry, the zone is still live at end-of-data. If a lifetime
        # cap is set and the zone was never retested, mark it expired
        # at the dataset's end-of-data bar.
        if z.live and max_zone_age_bars > 0 and z.expired_bar > 0 and z.expired_bar < n:
            # Zone never got mitigated/inverted AND never hit expiry
            # inside the dataset → it's still alive at end-of-data.
            # Leave ``live=True`` (we don't know if it would expire
            # with more data). Mark ``expired_bar`` as the cap.
            pass

    # ── DEFERRED SUPERSESSION RESOLUTION (2026-10-23) ───────────────
    # The supersession DECISION happens here, after every zone has
    # been walked. The rule:
    #
    #   * Zone was superseded AND never inverted -> DEAD. A newer zone
    #     replaced it and it never became an iFVG, so it must never
    #     generate a signal.
    #   * Zone was superseded BUT inverted BEFORE the superseding zone
    #     formed -> ALIVE. It is a legitimate iFVG: price committed
    #     through it and flipped its polarity before the replacement
    #     appeared. This is exactly the "sniper" signal.
    #   * Zone was never superseded -> unchanged.
    #
    # Before this, supersession was applied during detection, so the
    # inversion escape hatch was unreachable and 94% of zones died
    # without ever being given the chance to invert.
    for _z in zones:
        if _z.superseded_bar < 0:
            continue
        if _z.inverted_bar >= 0 and _z.inverted_bar <= _z.superseded_bar:
            # Inverted first -> it earned the signal. Keep it live.
            _z.live = True
        else:
            # Superseded and never (or later) inverted -> dead.
            _z.live = False

    # ── Structure-driven invalidation (added 2026-09-05) ─────────────
    # When ``structure_events_per_bar`` is provided, walk each
    # still-live zone forward in time and invalidate it at the FIRST
    # opposing-direction structure event. The rule:
    #
    #   * Bear structure event (BOS_BEAR, CHOCH_BEAR) on bar j →
    #     invalidate any bull FVG with ``trigger_bar < j``.
    #   * Bull structure event (BOS_BULL, CHOCH_BULL) on bar j →
    #     invalidate any bear FVG with ``trigger_bar < j``.
    #
    # We process the structure events in chronological order so each
    # zone is invalidated at the FIRST applicable event. Once a zone
    # is invalidated, the per-zone walk terminates early.
    #
    # This is a STRUCTURAL invalidation — no price-side pierce is
    # required. The zone flips to iFVG at the structure event bar
    # (the bar where the new swing level was broken / the trend
    # flipped). We do NOT mark ``pierced_bar`` here because there was
    # no pierce — the iFVG event is purely a structural-rejection
    # decision. ``retest scanner + bar-loop soft-stop logic`` will
    # see ``inverted=True`` and ``inverted_bar >= 0`` and treat the
    # zone as an iFVG from that bar forward.
    if structure_events_per_bar is not None and len(structure_events_per_bar) >= n_1s:
        # Compute max-age cutoff in 1s bars (if specified).
        age_cap_1s = 0  # 0 = unlimited
        if structure_invalidation_age_secs > 0 and times_utc_ns is not None and len(times_utc_ns) >= n_1s:
            age_cap_1s = max(1, int(structure_invalidation_age_secs))

        # ── Bucketed index for O(N_events × bucket_size) invalidation ──────
        # Raw loop: for ev_bar in range(n_1s): for z in zones:
        # → O(N × Z) = 41M × 290k ≈ 1.2e13 comparisons → hours.
        # Fix: bucket zones by trigger_bar so each event_bar only scans
        # the zones that COULD be invalidated by it (those with trigger_bar
        # before the event bar).
        #
        # Bucketing: the range [0, n_1s) is divided into equal-width
        # buckets (~10k zones per bucket at 290k zones). For each event
        # bar we binary-search to the first bucket that could contain
        # valid zones (trigger_bar < ev_bar), then scan candidate zones
        # and check the trigger_bar condition explicitly.
        n_buckets = max(1, len(zones) // 5000)  # ~5k zones/bucket target
        bucket_edges = np.linspace(0, n_1s, n_buckets + 1, dtype=np.int64)
        buckets: list[list[tuple[int, FvgZone]]] = [[] for _ in range(n_buckets)]
        for zi, z in enumerate(zones):
            bi = int(z.trigger_bar * n_buckets // n_1s)
            bi = max(0, min(bi, n_buckets - 1))
            buckets[bi].append((zi, z))

        # Walk structure events in chronological order. For each
        # opposing-direction event, invalidate matching live zones
        # whose trigger_bar is BEFORE the event bar (no look-ahead).
        #
        # Only scan bars that have events (most of 41M bars are empty).
        event_bar_indices = np.where(structure_events_per_bar[:n_1s] != 0)[0]
        for ev_bar in event_bar_indices:
            ev = int(structure_events_per_bar[ev_bar])
            # Determine which FVG direction this event invalidates.
            invalidates_bull_fvg = (ev < 0)  # BOS_BEAR, CHOCH_BEAR
            invalidates_bear_fvg = (ev > 0 and ev in (1, 2))  # BOS_BULL, CHOCH_BULL
            if not (invalidates_bull_fvg or invalidates_bear_fvg):
                continue  # liquidity-sweep or other event — skip

            # Binary-search: find the last bucket whose upper edge is
            # < ev_bar. Only zones in buckets up to bi_end can have
            # trigger_bar < ev_bar.
            bi_end = int(ev_bar * n_buckets // n_1s)
            if bi_end <= 0:
                continue  # no buckets with trigger_bar < ev_bar
            bi_end = min(bi_end, n_buckets - 1)

            for bi in range(bi_end + 1):
                for zi, z in buckets[bi]:
                    if not z.live:
                        continue
                    tb = z.trigger_bar
                    if tb >= ev_bar:
                        continue  # zone formed at or after the event — not invalidated
                    # Direction must match: bear event invalidates bull FVG.
                    if invalidates_bull_fvg and z.direction != 1:
                        continue
                    if invalidates_bear_fvg and z.direction != -1:
                        continue
                    # Age check (if configured).
                    if age_cap_1s > 0 and times_utc_ns is not None:
                        if tb < 1:
                            continue
                        trig_ns = int(times_utc_ns[tb])
                        ev_ns = int(times_utc_ns[ev_bar])
                        age_s = (ev_ns - trig_ns) / 1e9
                        if age_s > structure_invalidation_age_secs:
                            continue
                    # Fire the structural invalidation. We do NOT set
                    # ``pierced_bar`` (no pierce happened — the iFVG is
                    # structural). We set ``inverted_bar`` to the event
                    # bar and ``inverted=True``.
                    z.inverted_bar = ev_bar
                    z.inverted = True
                    z.live = False

    # ── Minimum lifetime filter (2026-09-17) ─────────────────────────
    # Drop zones whose first end event happened within
    # ``fvg_min_lifetime_secs`` detector bars of the zone's
    # ``trigger_bar``. These "born-dead" zones never had a chance to be
    # retested by a tradeable participant.
    #
    # NOTE: on 1s data (the only cadence this repo uses), 1 detector
    # bar == 1 wall-clock second. The filter is purely bar-count.
    #
    # End events considered (first wins):
    #   1. mitigated_bar  (if ≥ 0)
    #   2. inverted_bar  (if ≥ 0 — covers price-side and structure-driven)
    #   3. superseded_bar (if ≥ 0)
    #   4. played_out_bar (if ≥ 0)
    if fvg_min_lifetime_secs > 0:
        kept: list[FvgZone] = []
        n_dropped_lifetime = 0
        for z in zones:
            candidates: list[int] = []
            if z.mitigated_bar >= 0:
                candidates.append(z.mitigated_bar)
            if z.inverted_bar >= 0:
                candidates.append(z.inverted_bar)
            if z.superseded_bar >= 0:
                candidates.append(z.superseded_bar)
            if z.played_out_bar >= 0:
                candidates.append(z.played_out_bar)
            if not candidates:
                # Still-live zone — always passes through.
                kept.append(z)
                continue
            first_end_bar = min(candidates)
            delta_bars = first_end_bar - int(z.trigger_bar)
            if delta_bars < fvg_min_lifetime_secs:
                n_dropped_lifetime += 1
            else:
                kept.append(z)
        zones = kept
        _last_lifetime_dropped = n_dropped_lifetime  # noqa: F841

    return zones


# NOTE (2026-09-17): ``compute_fvg_price_ranks`` and its helper
# ``_tier_for_percentile`` have been REMOVED. The price-rank tier
# system is look-forward biased: it requires future zones in the
# same UTC day to rank against, so the percentile assigned to a
# zone at detection time depends on zones that haven't been
# detected yet. The ``PendingSignal.rank_tier`` and
# ``PendingSignal.rank_percentile`` fields are kept as
# empty-default fields for backward compat with old Trade rows
# but are never populated.


def fvg_retest_signals(
    zones: list[FvgZone],
    close: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    *,
    require_unmitigated: bool = False,
    only_inverted: bool = False,
    max_age_bars: int = 5000,
    min_mitigation_pct: float = 0.0,
    # 2026-09-15 v2: the FVG and iFVG paths both call this function
    # and both need to claim a zone. Previously a zone claimed by
    # the FVG path was marked ``live=False`` so the iFVG path could
    # not see it on a follow-up call. The fix: when
    # ``only_inverted=False`` (the FVG path), SKIP inverted zones
    # instead of consuming them — the iFVG path owns those. When
    # ``only_inverted=True`` (the iFVG path), SKIP non-inverted
    # zones for the same reason. The two paths now produce disjoint
    # retest sets and never race to consume the same zone.
    skip_inverted: bool = False,
    skip_non_inverted: bool = False,
) -> list[tuple[int, int, FvgZone]]:
    """Walk every FVG zone and emit retest-entry signals.

    A "retest" is the first bar AFTER the mitigation bar (or after the
    inversion bar, for iFVG entries) where the bar's range crosses the
    zone. This is the canonical ICT retest semantic — the entry fires
    when price RETURNS to the zone after first filling it, NOT on the
    mitigation bar itself.

    Without this offset the scanner would fire on the same bar as the
    mitigation, which is structurally the same as entering on the FVG
    bar (no actual retest happens — the first touch IS the mitigation).

    Mitigation = first touch (price filling the gap).
    Retest     = second touch (price returning to the zone after the
                 initial fill) — this is the entry.

    For un-mitigated zones (a fresh FVG with no fill yet) the scan
    falls back to ``trigger_bar + 1`` so live FVGs that have never been
    touched still get entries when price first arrives.

    Entry direction:
      - long on bull FVG retest, short on bear FVG retest
      - for inverted FVGs the direction flips (entering long on a
        violated-bear-FVG, now bull iFVG, is the canonical ICT reversal).

    Returns a list of ``(bar_index, direction, zone)`` tuples.

    Mitigation-depth filter (added 2026-08-20):
        ``min_mitigation_pct`` is the minimum fraction of the zone's
        height that must have been covered by a bar's range before
        the retest signal can fire. ``0.0`` (default) accepts any
        touch (legacy). ``0.5`` requires a bar whose range covered
        at least 50% of the zone's height. ``1.0`` requires a full
        fill (bar range fully covered the zone). The motivation:
        a wick-only touch (≈0% depth) is not a real mitigation —
        price wicked into the zone but the close stayed outside.
        The first-touch entry fallback for un-mitigated zones
        (when ``z.mitigated_bar < 0``) is now also gated on depth:
        if no bar has reached ``min_mitigation_pct``, the fallback
        does NOT fire either.
    """
    out: list[tuple[int, int, FvgZone]] = []
    n = close.shape[0]
    for z in zones:
        # Skip zones the detector has marked dead (consumed or expired).
        if not z.live:
            continue
        if only_inverted and not z.inverted:
            continue
        if skip_inverted and z.inverted:
            # FVG path skips inverted zones — the iFVG path owns them.
            continue
        if skip_non_inverted and not z.inverted:
            # iFVG path skips non-inverted zones — the FVG path owns them.
            continue
        if require_unmitigated and z.mitigated_bar >= 0 and not z.inverted:
            continue

        # Mitigation-depth filter (added 2026-08-20): if the deepest
        # mitigation we've seen is shallower than ``min_mitigation_pct``
        # of the zone's height, skip the zone entirely. A wick-only
        # touch is not a real mitigation; a shallow entry that only
        # filled 30% of the zone is too thin to act as a confirmation.
        if min_mitigation_pct > 0.0:
            if z.inverted:
                # Inverted zones have a polarity flip, which is a
                # deeper confirmation than a mitigation alone. Allow
                # the retest to fire regardless of mitigation depth.
                pass
            elif z.mitigated_depth_pct < min_mitigation_pct:
                z.live = False
                continue

        # Determine entry trigger bar — start AFTER the mitigation (or
        # inversion, for iFVG) so we don't fire on the first touch.
        if z.inverted:
            anchor = z.inverted_bar if z.inverted_bar >= 0 else z.trigger_bar
            start = anchor + 1  # post-inversion retest
        elif z.mitigated_bar >= 0:
            start = z.mitigated_bar + 1  # post-mitigation retest
        else:
            # Zone was never mitigated in this dataset. The legacy
            # fallback ``start = z.trigger_bar + 1`` lets live FVGs
            # fire on the first touch, but with ``min_mitigation_pct``
            # enforcement above that first touch must have reached
            # the depth threshold (otherwise the zone was skipped
            # above). This preserves the "live FVGs still get entries"
            # semantic while enforcing proper depth.
            start = z.trigger_bar + 1

        # Cap at the zone's expiry bar if the detector set one.
        expiry_cap = z.expired_bar if z.expired_bar > 0 else n
        if max_age_bars > 0:
            end = min(n, min(start + max_age_bars, expiry_cap))
        else:
            end = min(n, expiry_cap)
        if start >= end:
            z.live = False  # dead on arrival — no window to fire
            continue

        for j in range(start, end):
            if z.expired_bar > 0 and j >= z.expired_bar:
                z.live = False
                break
            # Bar enters zone if its range straddles the zone bounds
            if low[j] <= z.zone_high and high[j] >= z.zone_low:
                d = -z.direction if z.inverted else z.direction
                # Mark the zone as consumed — it can only fire ONE
                # retest signal in its lifetime.
                z.consumed_bar = j
                z.live = False
                out.append((j, d, z))
                break
    return out


# ────────────────────────────────────────────────────────────────────────────
# Opening Range Breakout (ORB)
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class OrbSession:
    """One session's opening range and breakout state.

    Times are tz-naive int64 nanoseconds since epoch UTC.
    """
    session_start_ns: int
    or_high: float
    or_low: float
    or_end_ns: int        # last bar of the opening range
    broke_high_bar: int = -1
    broke_low_bar: int = -1
    bull_break: bool = False
    bear_break: bool = False


def detect_orb(
    times_utc_ns: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    session_start_hour_utc: int,
    or_duration_mins: int = 15,
) -> list[OrbSession]:
    """Detect all sessions and their opening range breakouts.

    For each calendar day in ``times_utc_ns``:
      1. Find the OR window = [session_start_hour_utc, +or_duration_mins).
      2. Track the high/low of all bars in that window (the "range").
      3. After the OR window ends, watch for the first bar where
         ``close > or_high`` (bull breakout) or ``close < or_low`` (bear
         breakout). The first such bar is the breakout bar.

    Returns one ``OrbSession`` per day with the breakout state.
    """
    NS_PER_MIN = 60_000_000_000
    NS_PER_HOUR = 3_600_000_000_000
    NS_PER_DAY = 86_400_000_000_000
    window_ns = or_duration_mins * NS_PER_MIN
    session_start_offset_ns = session_start_hour_utc * NS_PER_HOUR

    n = times_utc_ns.shape[0]
    if n == 0:
        return []

    days = (times_utc_ns // NS_PER_DAY).astype(np.int64)
    out: list[OrbSession] = []

    # Walk days
    unique_days = np.unique(days)
    for d in unique_days:
        day_start_ns = int(d) * NS_PER_DAY
        or_start_ns = day_start_ns + session_start_offset_ns
        or_end_ns = or_start_ns + window_ns
        day_end_ns = day_start_ns + NS_PER_DAY

        # Find OR bars
        or_mask = (times_utc_ns >= or_start_ns) & (times_utc_ns < or_end_ns)
        if not or_mask.any():
            continue
        or_high = float(high[or_mask].max())
        or_low = float(low[or_mask].min())

        # Watch for breakout
        broke_high_bar = -1
        broke_low_bar = -1
        bull_break = False
        bear_break = False

        post_mask = (times_utc_ns >= or_end_ns) & (times_utc_ns < day_end_ns)
        post_idx = np.where(post_mask)[0]
        for j in post_idx:
            c = close[j]
            if broke_high_bar < 0 and c > or_high:
                broke_high_bar = j
                bull_break = True
                # Don't break — both can fire in different orders
            if broke_low_bar < 0 and c < or_low:
                broke_low_bar = j
                bear_break = True
            if bull_break and bear_break:
                break

        out.append(OrbSession(
            session_start_ns=or_start_ns,
            or_high=or_high,
            or_low=or_low,
            or_end_ns=or_end_ns,
            broke_high_bar=broke_high_bar,
            broke_low_bar=broke_low_bar,
            bull_break=bull_break,
            bear_break=bear_break,
        ))
    return out


def orb_signals(
    sessions: list[OrbSession],
    *,
    require_both_breaks: bool = False,
) -> list[tuple[int, int, OrbSession]]:
    """Emit entry signals on the breakout bar.

    A bull breakout (close > OR high) emits a long signal at the
    breakout bar; a bear breakout emits a short signal. With
    ``require_both_breaks=True``, only sessions where both sides
    broke (i.e. the range failed completely — a "whipsaw" signal) emit
    anything.
    """
    out: list[tuple[int, int, OrbSession]] = []
    for s in sessions:
        if require_both_breaks and not (s.bull_break and s.bear_break):
            continue
        if s.bull_break and s.broke_high_bar >= 0:
            out.append((s.broke_high_bar, 1, s))
        if s.bear_break and s.broke_low_bar >= 0:
            out.append((s.broke_low_bar, -1, s))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Wyckoff phase tagger
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class WyckoffState:
    """Per-bar Wyckoff tag (lightweight — intended as a confidence multiplier)."""
    is_in_range: bool          # ATR(20) < ATR(100) × 0.7 → squeezing
    is_accumulating: bool      # is_in_range AND prior trend was bear
    is_distributing: bool      # is_in_range AND prior trend was bull
    is_spring: bool            # bar broke range low then closed back inside
    is_utad: bool              # bar broke range high then closed back inside
    range_low: float = np.nan
    range_high: float = np.nan


def _rolling_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    prev_close = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    out = np.zeros_like(tr, dtype=np.float64)
    csum = np.concatenate([[0.0], np.cumsum(tr)])
    idx = np.arange(tr.shape[0])
    win_start = np.maximum(0, idx - n + 1)
    counts = (idx + 1 - win_start).astype(np.float64)
    out = (csum[idx + 1] - csum[win_start]) / counts
    return out


def detect_wyckoff(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    ict: IctSeries,
    *,
    atr_fast: int = 20,
    atr_slow: int = 100,
    squeeze_ratio: float = 0.7,
    range_lookback: int = 100,
    trend_lookback: int = 100,
    spring_break_usd: float = 0.05,
) -> list[WyckoffState]:
    """Tag each bar with coarse Wyckoff phase state.

    Rules (lightweight, intended as a multiplier, not a primary signal):
      * **is_in_range**: ATR(atr_fast) < squeeze_ratio × ATR(atr_slow)
      * **is_accumulating**: in_range AND prior trend was bear for trend_lookback bars
      * **is_distributing**: in_range AND prior trend was bull for trend_lookback bars
      * **is_spring**: in_range AND bar low < recent range low - spring_break_usd
        AND bar close back inside the range (failed breakdown)
      * **is_utad**: in_range AND bar high > recent range high + spring_break_usd
        AND bar close back inside the range (failed breakout)

    Returns a list (one ``WyckoffState`` per bar) — same length as input.
    """
    n = close.shape[0]
    out: list[WyckoffState] = []
    if n == 0:
        return out

    atr_f = _rolling_atr(high, low, close, atr_fast)
    atr_s = _rolling_atr(high, low, close, atr_slow)
    is_squeeze = atr_f < squeeze_ratio * atr_s

    # Rolling range high/low (for spring/UTAD detection)
    range_high = np.full(n, np.nan)
    range_low = np.full(n, np.nan)
    for i in range(range_lookback, n):
        window_high = high[i - range_lookback:i].max()
        window_low = low[i - range_lookback:i].min()
        range_high[i] = window_high
        range_low[i] = window_low

    # Prior trend: was the structure-state trend bear for trend_lookback bars ending here?
    prior_trend = ict.trend.astype(np.int8) if hasattr(ict, "trend") and len(ict.trend) == n else np.zeros(n, dtype=np.int8)
    # Need at least trend_lookback bars of same sign
    is_long_trend = np.zeros(n, dtype=np.bool_)
    is_short_trend = np.zeros(n, dtype=np.bool_)
    for i in range(trend_lookback, n):
        recent = prior_trend[i - trend_lookback:i]
        is_long_trend[i] = bool(np.all(recent == 1))
        is_short_trend[i] = bool(np.all(recent == -1))

    for i in range(n):
        in_range = bool(is_squeeze[i])
        accum = in_range and bool(is_short_trend[i])
        distrib = in_range and bool(is_long_trend[i])
        spring = False
        utad = False
        if i >= range_lookback:
            rh = range_high[i]
            rl = range_low[i]
            if np.isfinite(rh) and np.isfinite(rl):
                if low[i] < rl - spring_break_usd and close[i] > rl:
                    spring = True
                if high[i] > rh + spring_break_usd and close[i] < rh:
                    utad = True
        out.append(WyckoffState(
            is_in_range=in_range,
            is_accumulating=accum,
            is_distributing=distrib,
            is_spring=spring,
            is_utad=utad,
            range_low=float(range_low[i]) if np.isfinite(range_low[i]) else np.nan,
            range_high=float(range_high[i]) if np.isfinite(range_high[i]) else np.nan,
        ))
    return out


# ────────────────────────────────────────────────────────────────────────────
# Convenience: emit unified signal intents compatible with the
# TrendStrategyParams / PendingSignal pipeline.
# ────────────────────────────────────────────────────────────────────────────

@dataclass
class IctSignal:
    """A signal intent from one of the ICT detectors. The strategy
    layer can convert this to a ``PendingSignal`` and pass it through
    the existing trade-bookkeeping path."""
    trigger_bar: int
    direction: int                # +1 long, -1 short
    source: str                   # "fvg" | "ifvg" | "orb" | "wyckoff"
    stop_usd: float
    target_usd: float
    confidence: float = 1.0       # 1.0 default; Wyckoff tags can adjust
    metadata: dict = None         # arbitrary (zone bounds, session, etc.)


__all__ = [
    "FvgZone",
    "detect_fvg",
    "fvg_retest_signals",
    "OrbSession",
    "detect_orb",
    "orb_signals",
    "WyckoffState",
    "detect_wyckoff",
    "RenkoBars",
    "compute_renko_bars",
    "IctSignal",
    # NOTE (2026-09-17): compute_fvg_price_ranks removed (look-forward bias).
    # classify_candle_quality / annotate_candle_quality /
    # classify_fvg_tier / annotate_fvg_tiers removed earlier (2026-09-16).
]


def generate_ict_pending_signals(
    close: np.ndarray,
    open_: np.ndarray,
    high: np.ndarray,
    low: np.ndarray,
    times_utc_ns: np.ndarray,
    ict: IctSeries,
    p,  # TrendStrategyParams (avoid circular import on type hint)
    src: str | None = None,
    precomputed_zones: list | None = None,
) -> list:
    """Generate pending signals from FVG / iFVG / ORB / Wyckoff detectors.

    By default the source is read from ``p.signal_source``. To emit
    signals from MORE THAN ONE source (the multi-source compositing
    feature added 2026-08-18), call this function once per source
    and concatenate the lists. The TrendStrategyParams will hold the
    ``additional_sources`` list and the backtest driver iterates.

    Each source:
      * ``"fvg"``     — first-bar entries on FVG displacements.
                        Gated on GMMA bias by default (long in bull,
                        short in bear).
      * ``"ifvg"``    — entries on the FIRST retest of an INVERTED FVG
                        zone. Gating is inverted: long on bull-inverted-
                        FVG, short on bear-inverted-FVG. Bias gate is
                        typically disabled (``gate_on_gmma_bias=False``)
                        because the inversion IS the reversal signal.
      * ``"orb"``     — breakouts of the London/NY opening range.
      * ``"wyckoff"`` — only spring/UTAD bars (rare).

    **Retest lookback** (added 2026-08-18): when
    ``p.fvg_retest_lookback > 0``, the function only emits signals
    for FVG/iFVG zones whose ``trigger_bar`` (FVG) or
    ``inverted_bar`` (iFVG) is within the last
    ``fvg_retest_lookback`` bars of the signal bar. This caps the
    lookback so we don't pile up ancient zones.

    **Breadth-scaled SL/TP** (added 2026-08-18): when
    ``p.fvg_sl_per_breadth > 0`` or ``p.fvg_tp_per_breadth > 0``,
    the per-signal ``stop_usd`` / ``target_usd`` are computed as
    ``k * zone_width`` instead of the global ``strat.sl_usd`` /
    ``strat.tp_usd``. The zone width = ``zone_high - zone_low``.
    This makes wide FVGs ride further and narrow FVGs cut tighter.

    All signals carry the canonic ``PendingSignal`` fields:
    ``trigger_bar``, ``direction``, ``triggered_by`` (set to the
    source name), ``stop_usd``, ``target_usd``.
    """
    from bt._engine.ict_strategy import PendingSignal

    src = (src or str(p.signal_source)).lower()
    if src == "gmma":
        return []  # Not our source; the backtester will use the GMMA path.

    out: list[PendingSignal] = []
    # 2026-09-15 v2: default flipped to False on TrendStrategyParams.
    # The bias gate rejects signals whose direction disagrees with the
    # current BoS/CHoCH trend. The empirical finding (nb40): this
    # UNDERPERFORMS the counterfactual because the trend classifier
    # is reactive (it changes AFTER the move has happened) and the
    # rejection often fires on the SAME bar as the signal — the FVG
    # was structurally valid but the trend hadn't caught up yet.
    # BoS/CHoCH remains available via the conviction scoring (TP
    # boosting) without being used as a hard reject.
    bias_gate = bool(getattr(p, "gate_on_gmma_bias", False))
    sl_usd = float(p.sl_usd)
    tp_usd = float(p.tp_usd)
    # ATR-scaled SL/TP when enabled
    if bool(getattr(p, "use_atr_scaling", False)) and ict.atr.shape[0] > 0:
        # Use ATR at the last bar as a proxy for the recent regime
        atr_v = float(ict.atr[-1]) if ict.atr[-1] > 0 else 0.0
        if atr_v > 0:
            sl_usd = float(p.sl_atr_mult) * atr_v
            tp_usd = float(p.tp_atr_mult) * atr_v

    # ── Market-structure conviction (added 2026-08-19) ─────────────────────
    # When ``use_market_structure=True``, build a StructureState once
    # and apply the per-bar conviction classifier to every retest.
    # The CHoCH+ annotation is also computed when we have FVG zones
    # available (so we annotate right after ``detect_fvg`` below).
    structure_state = None
    use_ms = bool(getattr(p, "use_market_structure", False))
    # Per-source opt-in flags (default all on when use_ms is on).
    src_apply_ms = {
        "fvg": bool(getattr(p, "ms_apply_to_fvg", True)),
        "ifvg": bool(getattr(p, "ms_apply_to_ifvg", True)),
        "orb": bool(getattr(p, "ms_apply_to_orb", True)),
        "wyckoff": bool(getattr(p, "ms_apply_to_wyckoff", True)),
    }
    ms_min_conv = float(getattr(p, "ms_min_conviction", 0.0))
    ms_boost = float(getattr(p, "ms_boost_conviction", 1.0))
    ms_max_boost_age = int(getattr(p, "ms_max_boost_age_bars", 60))
    ms_choch_caution_age = int(getattr(p, "ms_choch_caution_age_bars", 60))
    if use_ms:
        from .market_structure import structure_conviction
        # 2026-09-15: pass the structure state from ``ict_series``
        # instead of recomputing. ``ict_backtest.run_ict_backtest``
        # already calls ``detect_market_structure`` once and stores
        # it on ``ict_series.trend`` (with the full StructureState
        # accessible via ``ict_series._structure_state``). This
        # eliminates a redundant ~1s-per-call structure detection
        # that was firing for every source in
        # ``generate_ict_pending_signals`` — i.e. 2x for fvg+ifvg.
        # When ``ict_series`` is None (ad-hoc caller like a notebook
        # calling ``generate_ict_pending_signals`` directly), fall
        # back to building the state locally.
        existing_struct = getattr(ict, "_structure_state", None) if ict is not None else None
        if existing_struct is not None:
            structure_state = existing_struct
        else:
            from .market_structure import detect_market_structure
            structure_state = detect_market_structure(
                high, low, close,
                pivot_len=int(getattr(p, "ms_pivot_len", 9)),
                liquidity_len=int(getattr(p, "ms_liquidity_len", 30)),
                detect_order_blocks=bool(getattr(p, "ms_draw_order_blocks", True)),
                detect_liquidity=bool(getattr(p, "ms_draw_liquidity_sweeps", True)),
            )

    def _bias(i: int) -> int:
        """Return current structure-state trend at bar i (-1/0/+1).

        In the ICT-only fork, the bias comes from the BoS/CHoCH
        classifier (``StructureState.trend``) rather than from a
        moving-average average. ``+1`` = bull, ``-1`` = bear,
        ``0`` = unknown / no signal yet.
        """
        if i < 0 or i >= len(ict.trend):
            return 0
        return int(ict.trend[i])

    if src in ("fvg", "ifvg"):
        only_inv = src == "ifvg"
        rs = int(p.ifvg_resample_secs if only_inv else p.fvg_resample_secs)
        min_z = float(p.ifvg_min_zone_usd if only_inv else p.fvg_min_zone_usd)
        # Cache cap (added 2026-08-18): keep at most N zones alive.
        # ``fvg_max_cache_size`` for faster 1s-native FVG runs.
        max_zones = int(p.ifvg_max_cache_size if only_inv else p.fvg_max_cache_size)
        # Detector lifetime cap (added 2026-08-18): a zone is marked
        # ``expired_bar`` after ``fvg_max_age_secs`` (wall-clock) of
        # life without a retest. We convert seconds → detector-bar
        # count using ``resample_to_n_secs`` so the same wall-clock
        # window means the same thing at 1s, 1m, or 5m detection.
        # ``0`` disables (legacy unlimited). The legacy
        # ``fvg_max_age_bars`` (raw bar count) is honoured as a
        # fallback when ``fvg_max_age_secs == 0``.
        age_secs = int(p.fvg_max_age_secs if not only_inv else p.ifvg_max_age_secs)
        age_bars_legacy = int(p.fvg_max_age_bars if not only_inv else p.ifvg_max_age_bars)
        if age_secs > 0:
            # Convert wall-clock seconds to bars at the resampled cadence.
            # rs > 0 means we're detecting on aggregated bars; rs == 0 means
            # native 1s bars. Either way, divide seconds by the per-bar span.
            per_bar_secs = rs if rs > 0 else 1
            detector_max_age = max(1, age_secs // per_bar_secs)
        elif age_bars_legacy > 0:
            detector_max_age = age_bars_legacy  # legacy bar-count path
        else:
            detector_max_age = 0  # unlimited
        # ── Structure-driven FVG invalidation (added 2026-09-05) ─────
        # When ``fvg_invalidate_on_structure=True``, compute a
        # StructureState (if not already computed above) and pass its
        # per-bar events array to ``detect_fvg`` so bearish structure
        # events (BoS/CHoCH BEAR) invalidate bull FVGs and bullish
        # events invalidate bear FVGs. This is a STRUCTURAL rule —
        # the detector sees no price-side pierce, only the structure
        # event flipping the trend. The retest scanner and bar-loop
        # soft-stop logic honor ``inverted=True`` set this way.
        structure_events_for_detector = None
        if bool(getattr(p, "fvg_invalidate_on_structure", False)):
            from .market_structure import detect_market_structure
            struct_state_for_inv = detect_market_structure(
                high, low, close,
                pivot_len=int(getattr(p, "ms_pivot_len", 9)),
                liquidity_len=int(getattr(p, "ms_liquidity_len", 30)),
                detect_order_blocks=False,  # events only — no OBs needed
                detect_liquidity=False,      # events only — no sweeps needed
                resample_to_n_secs=int(getattr(p, "ms_resample_secs", 0)),
            )
            structure_events_for_detector = struct_state_for_inv.events

        # When the caller precomputed the zones (added 2026-09-26,
        # forex HF-strategy perf refactor), reuse them instead of
        # re-running the expensive detect_fvg walk. The precomputed
        # zones were built with the same detector-side params
        # (zone min width, displacement ratio, etc), so the
        # retest/inversion pipeline downstream sees IDENTICAL
        # inputs. Only the call to detect_fvg is skipped — every
        # filter, gate, and signal post-processing still runs.
        #
        # IMPORTANT (added 2026-09-26): ``fvg_retest_signals``
        # mutates zones on fire (``z.consumed_bar``, ``z.live = False``).
        # When the zones are shared with a precomputed bundle, those
        # mutations poison the bundle for the next call. We deep-copy
        # here so the retest scanner operates on its OWN zone objects,
        # not the cached ones. The cost is O(N) shallow copies of
        # ~15k FvgZone objects — sub-second on 2025Q1.
        if precomputed_zones is not None:
            import copy as _copy
            zones = [_copy.copy(z) for z in precomputed_zones]
        else:
            zones = detect_fvg(
                open_, high, low, close,
                resample_to_n_secs=rs if rs > 0 else 0,
                fvg_min_zone_usd=min_z,
                max_active_zones=max_zones,
                max_zone_age_bars=detector_max_age,
                fvg_displacement_ratio=float(p.fvg_displacement_ratio),
                fvg_body_definition=str(p.fvg_body_definition),
                fvg_min_zone_atr_mult=float(getattr(p, "fvg_min_zone_atr_mult", 0.0)),
                atr_for_min_zone=float(getattr(p, "fvg_min_zone_atr", 0.0)),
                # Supersession rule (added 2026-08-20): when a new FVG
                # overlaps an older live zone's price range, the older zone
                # is marked superseded and won't fire a retest. Only the
                # freshest level in a region is structurally interesting.
                # ``fvg_supersede_on_new`` defaults True in the strategy
                # params; the detector's own ``supersede_on_new`` defaults
                # False (deterministic for ad-hoc callers).
                supersede_on_new=bool(getattr(p, "fvg_supersede_on_new", True)),
                invalidation_min_pierce_usd=float(getattr(p, "fvg_invalidation_min_pierce_usd", 0.0)),
                invalidation_min_consecutive_bars=int(getattr(p, "fvg_invalidation_min_consecutive_bars", 1)),
                require_retest_to_invert=bool(getattr(p, "fvg_require_retest_to_invert", True)),
                played_out_min_extension_usd=float(getattr(p, "played_out_min_extension_usd", 0.0)),
                # 2026-09-15: body-only mitigation / invalidation knobs.
                # Forwarded from the strategy params. Default False (legacy
                # close-only semantics) so existing callers are unaffected.
                body_only_mitigation=bool(getattr(p, "fvg_body_only_mitigation", False)),
                body_only_invalidation=bool(getattr(p, "fvg_body_only_invalidation", False)),
                # 2026-09-05: structure-driven invalidation (optional).
                structure_events_per_bar=structure_events_for_detector,
                structure_invalidation_age_secs=int(getattr(p, "fvg_structure_invalidation_age_secs", 0)),
                times_utc_ns=times_utc_ns,
                # 2026-09-17: minimum FVG lifetime filter. When > 0, drops
                # "born-dead" zones (those whose first end event fires within
                # N seconds of trigger). Reduces noise on 1s XAUUSD — the
                # detector still emits every zone, but only the ones that
                # survived a meaningful window make it to the retest scanner.
                fvg_min_lifetime_secs=int(getattr(p, "fvg_min_lifetime_secs", 0)),
            )
        # ── Structure-driven invalidation counter (added 2026-09-05) ─
        # A zone was structurally invalidated (vs price-side) when:
        #   * ``inverted=True``
        #   * ``inverted_bar >= 0``
        #   * ``pierced_bar < 0`` (no price-side pierce preceded it)
        # This counter is what the backtest reports as
        # ``n_structure_invalidations``. We sum over every source's
        # run because the detector is called fresh per source.
        struct_inv = 0
        for _z in zones:
            if _z.inverted and _z.inverted_bar >= 0 and _z.pierced_bar < 0:
                struct_inv += 1
        ict.n_structure_invalidations += struct_inv
        # ── Body-only counters (added 2026-09-15) ──────────────────────
        # When ``fvg_body_only_mitigation=True`` /
        # ``fvg_body_only_invalidation=True`` the detector ran with
        # stricter semantics — surface the resulting mitigation /
        # inversion counts so the user can audit how many zones
        # were flagged. ``n_body_mitigations`` includes all zones
        # whose ``mitigated_bar`` was set (regardless of inversion).
        # ``n_body_inversions`` is zones with ``inverted=True``.
        if bool(getattr(p, "fvg_body_only_mitigation", False)):
            ict.n_body_mitigations += sum(
                1 for _z in zones if _z.mitigated_bar >= 0
            )
        if bool(getattr(p, "fvg_body_only_invalidation", False)):
            ict.n_body_inversions += sum(
                1 for _z in zones if _z.inverted and _z.inverted_bar >= 0
            )
        # NOTE (2026-09-17): the FVG price-rank treatment block and
        # the breadth-percentile filter block have been REMOVED.
        # Both required future information (intra-day ranking
        # against future zones; post-hoc zone-width distribution)
        # — pure look-forward bias. The
        # ``PendingSignal.rank_tier`` / ``PendingSignal.rank_percentile``
        # fields remain on the dataclass (empty defaults) for
        # backward compat with old Trade rows but are never
        # populated here.

        # ── Rolling FVG percentile tier (added 2026-09-17) ─────────────
        # Causal alternative: rank each zone vs the past N same-direction
        # zones. Only past prices are used — no future information.
        # The ranker lives on IctSeries so it persists across bars.
        rolling_enabled = bool(getattr(p, "fvg_rolling_treatment_enabled", False))
        if rolling_enabled:
            rolling_window = max(2, int(getattr(p, "fvg_rolling_window_n", 20)))
            if ict.ranker is None:
                ict.ranker = RollingFvgRanker(window=rolling_window)
            # Add every newly-detected zone to the ranker so that
            # subsequent zones in the same bar (possible with multiple
            # FVGs) are ranked correctly. We add all zones regardless
            # of whether they'll produce a retest (dead zones still
            # count as price levels for the rolling window).
            for _z in zones:
                if _z.direction > 0:
                    _anchor = float(_z.zone_high)
                else:
                    _anchor = float(_z.zone_low)
                ict.ranker.add(_z.direction, _anchor)

        # Retest window cap (added 2026-08-18): mirror the detector's
        # age cap, in wall-clock seconds. Convert to detector-bar count
        # via the same per_bar_secs logic as above. Legacy bar-count
        # fields are honoured as fallback when the seconds field is 0.
        retest_secs = int(p.fvg_max_age_secs if not only_inv else p.ifvg_max_age_secs)
        retest_bars_legacy = int(p.fvg_max_age_bars if not only_inv else p.ifvg_max_age_bars)
        if retest_secs > 0:
            per_bar_secs = rs if rs > 0 else 1
            max_age = max(1, retest_secs // per_bar_secs)
        elif retest_bars_legacy > 0:
            max_age = retest_bars_legacy
        else:
            max_age = 5000  # legacy default
        retest_lookback = int(p.fvg_retest_lookback)

        # ── Liquidity-sweep stop-order signals (added 2026-09-05) ───────
        # The sweep signal fires for each LIVE FVG zone: a stop order
        # placed past the zone edge that fills when price sweeps THROUGH
        # the zone (taking out retail stops), then the trade profits
        # from the reversal back through the zone.
        #
        # We emit the sweep signal BEFORE the retest scanner consumes
        # the zone (so the zone is still ``live`` when we look at it).
        # If the regular retest scanner ALSO fires on this zone (price
        # enters the zone instead of sweeping past it), both signals
        # exist independently — the bar loop will fill whichever
        # condition is met first.
        #
        # Mechanics:
        #   * Bull FVG (long thesis): stop BUY at
        #     ``zone_low - fvg_sweep_distance_usd``. Bar loop checks
        #     ``b_low <= fill_price`` each bar; when price dips to
        #     that level, the stop fills.
        #   * Bear FVG (short thesis): mirror — stop SELL at
        #     ``zone_high + fvg_sweep_distance_usd``.
        #
        # The sweep signal is emitted with `trigger_bar = z.trigger_bar + 1`
        # (anti-look-ahead: the bar AFTER the zone is born). The bar
        # loop reads ``fill_price = b_close + offset`` (where the
        # offset is computed against the trigger bar's close) and
        # checks the bar's range against it.
        #
        # The user opts in by adding "sweep" to ``additional_sources``
        # AND setting ``fvg_sweep_enabled=True``. When both are true,
        # the FVG pass will emit sweep signals on top of the regular
        # FVG retests.
        if (
            src in ("fvg", "ifvg")
            and bool(getattr(p, "fvg_sweep_enabled", False))
        ):
            sweep_distance = float(getattr(p, "fvg_sweep_distance_usd", 0.10))
            sweep_min_zone = float(getattr(p, "fvg_sweep_min_zone_usd", 0.20))
            sweep_min_dist = float(getattr(p, "fvg_sweep_min_distance_from_zone", 0.05))
            sweep_atr_mult = float(getattr(p, "fvg_sweep_atr_mult", 0.5))
            # Current ATR for SL sizing.
            atr_now_for_sweep = (
                float(ict.atr[-1]) if (ict.atr.shape[0] > 0 and ict.atr[-1] > 0) else 0.0
            )
            for z in zones:
                if not z.live:
                    continue
                if z.played_out_bar >= 0 or z.expired_bar > 0:
                    continue
                # Width filter.
                z_width = float(z.zone_high - z.zone_low)
                if z_width < sweep_min_zone:
                    continue
                # Distance-from-zone floor.
                if sweep_min_dist > 0 and sweep_distance < sweep_min_dist:
                    continue
                # Determine sweep stop-order price + direction.
                if z.direction > 0:
                    sweep_price = float(z.zone_low) - sweep_distance
                    sweep_dir = 1
                    sl_price = (
                        float(z.zone_low) - sweep_atr_mult * atr_now_for_sweep
                        if atr_now_for_sweep > 0
                        else float(z.zone_low) - 0.50
                    )
                    tp_price = float(z.zone_high)
                else:
                    sweep_price = float(z.zone_high) + sweep_distance
                    sweep_dir = -1
                    sl_price = (
                        float(z.zone_high) + sweep_atr_mult * atr_now_for_sweep
                        if atr_now_for_sweep > 0
                        else float(z.zone_high) + 0.50
                    )
                    tp_price = float(z.zone_low)
                # The signal fires on the bar AFTER the zone is born
                # (anti-look-ahead). The bar loop checks every bar for
                # a fill from that point forward.
                trigger_bar = min(z.trigger_bar + 1, close.shape[0] - 1)
                # Bias gate (same as FVG).
                if bias_gate and _bias(trigger_bar) != 0:
                    effective_dir = _bias(trigger_bar)
                    if only_inv and getattr(p, "ifvg_reverses_bias", True):
                        effective_dir = -effective_dir
                    if sweep_dir != effective_dir:
                        continue
                # BoS/CHoCH alignment-based gating was REMOVED 2026-09-16.
                # The new design uses ``BosChochMemory`` in the bar loop
                # to suppress the inversion soft-stop for aligned trades
                # — see ``_bos_choch_entry_alignment`` in
                # ``src/backtest/ict_backtest.py``. Signals always submit;
                # the alignment only affects whether the soft-stop fires.
                # SL/TP distances from the sweep stop price.
                scaled_sl = abs(sweep_price - sl_price)
                scaled_tp = abs(tp_price - sweep_price)
                scaled_sl = max(scaled_sl, float(p.dynamic_sl_floor_usd))
                scaled_tp = max(scaled_tp, float(p.dynamic_tp_floor_usd))
                # Conviction gate.
                conviction = 1.0
                if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                    conviction = structure_conviction(
                        structure_state, trigger_bar, sweep_dir,
                        bos_boost_max_age_bars=ms_max_boost_age,
                        choch_plus_boost_max_age_bars=ms_max_boost_age,
                        choch_caution_max_age_bars=ms_choch_caution_age,
                    )
                    if conviction < ms_min_conv:
                        continue
                    if conviction > 1.0 and ms_boost > 1.0:
                        scaled_tp = scaled_tp * (
                            1.0 + (ms_boost - 1.0) * (conviction - 1.0) / 0.5
                        )
                # NOTE (2026-09-17): the look-forward price-rank tier
                # has been replaced with a causal rolling percentile
                # tier. Rank only past same-direction zones — no future
                # information. ``rank_tier_s`` / ``rank_pct_s`` are
                # now computed from the RollingFvgRanker if enabled.
                if rolling_enabled and ict.ranker is not None:
                    _sweep_anchor = float(z.zone_low) if sweep_dir > 0 else float(z.zone_high)
                    rank_pct_s = ict.ranker.percentile(sweep_dir, _sweep_anchor)
                    rank_tier_s = _rolling_tier_label(
                        rank_pct_s,
                        float(getattr(p, "fvg_rolling_a_pct", 0.20)),
                        float(getattr(p, "fvg_rolling_c_pct", 0.20)),
                    )
                else:
                    rank_tier_s = ""
                    rank_pct_s = 0.0
                # Encode the sweep price as the layer's fill_price via
                # the offset from the trigger bar's close. The bar
                # loop reads ``fill_price = b_close + offset`` and
                # checks the bar's range against it. For a long sweep,
                # sweep_price is BELOW the trigger close → offset is
                # negative. The bar loop's existing long-fill rule
                # ``b_low <= target_price`` handles stop-order fills
                # correctly (the bar's low must reach the stop level).
                # For a short sweep, sweep_price is ABOVE the trigger
                # close → offset is positive. The bar loop's existing
                # short-fill rule ``b_high >= target_price`` handles
                # the fill.
                # NOTE: we do NOT mark the zone consumed here. If the
                # sweep fails to fill AND price later comes back into
                # the zone, the retest scanner can still fire on it.
                # We use the new ``fill_price`` field on PendingSignal
                # (added 2026-09-05) to encode the sweep price directly
                # instead of computing an offset from the trigger close.
                # The bar loop sees ``fill_price`` and uses it directly.
                out.append(PendingSignal(
                    trigger_bar=trigger_bar,
                    direction=sweep_dir,
                    triggered_by="sweep",
                    stop_usd=scaled_sl,
                    target_usd=scaled_tp,
                    conviction=conviction,
                    fvg_zone=z if src in ("fvg", "ifvg") else None,
                    is_ifvg=(src == "ifvg"),
                    rank_tier=rank_tier_s,
                    rank_percentile=rank_pct_s,
                    fill_price=float(sweep_price),
                ))

        retests = fvg_retest_signals(
            zones, close, high, low,
            only_inverted=only_inv,
            max_age_bars=max_age,
            min_mitigation_pct=float(getattr(
                p, "fvg_min_mitigation_pct",
                0.0 if only_inv else 0.0,
            )),
            # 2026-09-15 v2: the FVG and iFVG paths must claim disjoint
            # subsets of zones. The FVG path skips inverted zones;
            # the iFVG path skips non-inverted zones. This avoids
            # the previous "FVG consumes zone first, iFVG sees nothing"
            # race that produced only ~10 iFVG signals per day.
            skip_inverted=not only_inv,
            skip_non_inverted=only_inv,
        )

        # ── PIVOT F+I (2026-09-17): post-scan retest filters ─────────────────
        # Apply fvg_entry_confirm_bars and fvg_min_retest_count AFTER the scan
        # so the n_touches field is populated (detector pre-computes it).
        # These are causal: n_touches is the count of bars whose range
        # overlapped the zone — known at the retest bar.
        n_pre_filter = len(retests)
        if (
            int(getattr(p, "fvg_entry_confirm_bars", 0)) > 0
            or int(getattr(p, "fvg_min_retest_count", 0)) > 0
        ):
            confirm_bars = max(0, int(getattr(p, "fvg_entry_confirm_bars", 0)))
            min_retests = max(0, int(getattr(p, "fvg_min_retest_count", 0)))
            kept_retests_f: list[tuple[int, int, FvgZone]] = []
            for r_bar, r_dir, r_zone in retests:
                if r_zone is None:
                    kept_retests_f.append((r_bar, r_dir, r_zone))
                    continue
                if confirm_bars > 0 and r_zone.n_touches < confirm_bars:
                    continue  # skip: not enough consecutive-confirmation bars
                if min_retests > 0 and r_zone.n_touches < min_retests:
                    continue  # skip: not enough total touches
                kept_retests_f.append((r_bar, r_dir, r_zone))
            retests = kept_retests_f
        # iFVG min-inversion-age filter (added 2026-09-15).
        # Skip iFVG retest entries on zones that flipped within
        # ``fvg_ifvg_min_inversion_age_secs`` seconds of trigger. The
        # nb38/nb40 study showed D-tier zones invert almost
        # immediately (often < 5s) and those inversions carry
        # negligible edge. Filtering them at the SIGNAL level
        # (separate from the soft-stop path) drops garbage trades
        # while keeping the soft-stop defense intact for all zones.
        if (
            only_inv
            and times_utc_ns is not None
            and int(getattr(p, "fvg_ifvg_min_inversion_age_secs", 0)) > 0
        ):
            min_age_secs = int(p.fvg_ifvg_min_inversion_age_secs)
            n_pre_ifvg = len(retests)
            kept_retests: list[tuple[int, int, FvgZone]] = []
            for r_bar, r_dir, r_zone in retests:
                if r_zone is None or r_zone.inverted_bar < 0:
                    kept_retests.append((r_bar, r_dir, r_zone))
                    continue
                if r_zone.trigger_bar < 0 or r_zone.trigger_bar >= len(times_utc_ns):
                    kept_retests.append((r_bar, r_dir, r_zone))
                    continue
                trig_ns = int(times_utc_ns[r_zone.trigger_bar])
                inv_ns = int(times_utc_ns[r_zone.inverted_bar]) if r_zone.inverted_bar < len(times_utc_ns) else trig_ns
                age_secs = (inv_ns - trig_ns) / 1_000_000_000
                if age_secs >= min_age_secs:
                    kept_retests.append((r_bar, r_dir, r_zone))
                # else: drop — immediate inversion, no edge.
            n_dropped_ifvg = n_pre_ifvg - len(kept_retests)
            if n_dropped_ifvg > 0:
                ict.n_ifvg_age_dropped += n_dropped_ifvg
            retests = kept_retests
        # Annotate CHoCH+ on the structure state now that we have the
        # FVG zones (CHoCH+ = a CHoCH that was caused by a real
        # displacement, i.e. the break leg contained an FVG).
        if use_ms and structure_state is not None:
            from .market_structure import annotate_choch_plus
            annotate_choch_plus(structure_state, zones)
        # Breadth-scaled SL/TP knobs (added 2026-08-18)
        sl_per_b = float(p.fvg_sl_per_breadth)
        tp_per_b = float(p.fvg_tp_per_breadth)
        # Inverted-zone suppression (added 2026-08-19): when running
        # the FVG pipeline (only_inv=False), skip any retest whose
        # zone has been inverted. iFVG owns inverted zones — letting
        # both fire creates a duplicate signal on the same bar that
        # the bar loop's stable sort resolves non-deterministically.
        # This is the structural implementation of "iFVG overrides FVG":
        # the two sources produce disjoint retest sets and cannot
        # conflict on the same bar. Toggle via
        # ``TrendStrategyParams.drop_inverted_fvg`` (default True).
        drop_inverted = (not only_inv) and bool(
            getattr(p, "drop_inverted_fvg", True)
        )
        for bar, direction, zone in retests:
            if drop_inverted and zone is not None and zone.inverted:
                continue
            # Retest lookback cap (added 2026-08-18): only accept the
            # retest if the zone was MITIGATED (FVG) or INVERTED (iFVG)
            # in the last ``fvg_retest_lookback`` bars before ``bar``.
            # The mitigation/inversion is the structural "price just
            # broke this level" event we want recency on.
            if retest_lookback > 0 and zone is not None:
                anchor = zone.inverted_bar if only_inv else zone.mitigated_bar
                if anchor < 0 or (bar - anchor) > retest_lookback:
                    continue
            # Bias gate (added 2026-08-18 inversion handling).
            # Default semantic: signal direction must equal the
            # current GMMA trend direction. For iFVG with
            # ``ifvg_reverses_bias=True`` (default), we INVERT the
            # effective bias — the inversion IS the reversal, so we
            # accept counter-trend signals (long in bear trend, short
            # in bull trend). Set ``ifvg_reverses_bias=False`` to
            # require iFVG alignment with the bias (more conservative).
            if bias_gate and _bias(bar) != 0:
                effective_dir = _bias(bar)
                if only_inv and getattr(p, "ifvg_reverses_bias", True):
                    # Inversion reversal: trade against the bias
                    effective_dir = -effective_dir
                if direction != effective_dir:
                    continue
            # BoS/CHoCH directional gate (legacy v1, removed 2026-09-16).
            # The new design lets signals always submit and uses
            # ``BosChochMemory`` in the bar loop to suppress the
            # inversion soft-stop for aligned trades. See
            # ``_bos_choch_entry_alignment`` in
            # ``src/backtest/ict_backtest.py`` and the
            # ``bos_choch_ignore_invert_when_aligned`` parameter on
            # ``TrendStrategyParams``.
            #
            # Compute breadth-scaled SL/TP if enabled.
            # Floor uses ``fvg_breadth_floor_usd`` (default $0.10) to
            # prevent $0.01 stops on tiny zones, NOT the user's GMMA
            # ``sl_usd`` (which is calibrated for the slow trend path
            # and would over-floor the ICT signals).
            #
            # Inversion semantics (added 2026-08-18):
            #   normal:  scaled = k * zone_width
            #   invert:  scaled = min(cap, 1 / (k * zone_width))
            # where ``cap`` is the user-set ``sl_usd`` / ``tp_usd``
            # (the GMMA baseline). The inversion makes wide zones
            # produce tight SL/TP — the user's hypothesis that
            # displacement is itself the move, so we fade the remainder.
            sl_invert = bool(getattr(p, "fvg_sl_breadth_invert", False))
            tp_invert = bool(getattr(p, "fvg_tp_breadth_invert", False))
            if (sl_per_b > 0 or tp_per_b > 0) and zone is not None:
                zone_width = float(zone.zone_high - zone.zone_low)
                if sl_per_b > 0:
                    scaled_sl = sl_per_b * zone_width
                    if sl_invert and scaled_sl > 0:
                        scaled_sl = min(sl_usd, 1.0 / scaled_sl)
                else:
                    scaled_sl = sl_usd
                if tp_per_b > 0:
                    scaled_tp = tp_per_b * zone_width
                    if tp_invert and scaled_tp > 0:
                        scaled_tp = min(tp_usd, 1.0 / scaled_tp)
                else:
                    scaled_tp = tp_usd
                breadth_floor = float(p.fvg_breadth_floor_usd)
                scaled_sl = max(scaled_sl, breadth_floor)
                scaled_tp = max(scaled_tp, breadth_floor)
            else:
                scaled_sl = sl_usd
                scaled_tp = tp_usd
            # ── Conviction gate (added 2026-08-19) ─────────────────────
            # Apply market-structure conviction only if this source is
            # opted in. When conviction < ms_min_conviction we drop the
            # signal entirely (e.g. ms_min_conviction=0.5 drops every
            # signal that fires into a recent CHoCH-against-the-trade).
            # When conviction > 1.0 we widen the TP by ``ms_boost`` (so
            # 1.5 = TP is 50% wider on boosted entries).
            conviction = 1.0
            if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                conviction = structure_conviction(
                    structure_state, bar, direction,
                    bos_boost_max_age_bars=ms_max_boost_age,
                    choch_plus_boost_max_age_bars=ms_max_boost_age,
                    choch_caution_max_age_bars=ms_choch_caution_age,
                )
                if conviction < ms_min_conv:
                    continue
                if conviction > 1.0 and ms_boost > 1.0:
                    scaled_tp = scaled_tp * (1.0 + (ms_boost - 1.0) * (conviction - 1.0) / 0.5)
                    # Linear interpolation: conviction 1.0 → no widening,
                    # conviction 1.5 → fully applied widening.
            # NOTE (2026-09-17): the look-forward price-rank tier
            # has been replaced with a causal rolling percentile
            # tier. Rank only past same-direction zones — no future
            # information. ``rank_tier`` / ``rank_percentile`` are
            # now computed from the RollingFvgRanker if enabled.
            rank_tier = ""
            rank_percentile = 0.0
            if rolling_enabled and ict.ranker is not None and zone is not None:
                _anchor = float(zone.zone_high) if direction > 0 else float(zone.zone_low)
                rank_percentile = ict.ranker.percentile(direction, _anchor)
                rank_tier = _rolling_tier_label(
                    rank_percentile,
                    float(getattr(p, "fvg_rolling_a_pct", 0.20)),
                    float(getattr(p, "fvg_rolling_c_pct", 0.20)),
                )
            out.append(PendingSignal(
                trigger_bar=bar,
                direction=direction,
                triggered_by=src,
                stop_usd=scaled_sl,
                target_usd=scaled_tp,
                conviction=conviction,
                # Attach the FVG zone so the backtester can track
                # inversion events for the open position. ``zone`` is
                # a live reference; the detector mutates ``inverted``
                # forward in time, so the backtester sees the
                # inversion when it happens.
                fvg_zone=zone if src in ("fvg", "ifvg") else None,
                is_ifvg=(src == "ifvg"),
                # Rank-tier metadata (price-rank percentile system,
                # added 2026-08-20). When ``fvg_rank_treatment_enabled``
                # is on, every FVG/iFVG signal carries a tier
                # ("A" / "B" / "C") and a percentile within its
                # day+direction group. The bar loop reads these
                # fields to scale SL/TP and skip the deepest layers
                # on C-tier trades.
                rank_tier=rank_tier,
                rank_percentile=rank_percentile,
            ))

    elif src == "sweep":
        # The "sweep" source is a side-effect of the FVG/iFVG pass
        # (see the liquidity-sweep block above). When the user lists
        # "sweep" in ``additional_sources``, the sweep signals are
        # already emitted from the FVG pass — running an explicit
        # sweep pass would double-count. We treat "sweep" as a
        # no-op here; the FVG/iFVG pass is the canonical emitter.
        pass

    elif src == "orb":
        sessions = detect_orb(
            times_utc_ns, open_, high, low, close,
            session_start_hour_utc=int(p.orb_session_hour_utc),
            or_duration_mins=int(p.orb_duration_mins),
        )
        signals = orb_signals(sessions)
        for bar, direction, _sess in signals:
            if bias_gate and _bias(bar) != 0 and direction != _bias(bar):
                continue
            # BoS/CHoCH directional gate (legacy) REMOVED 2026-09-16 —
            # signals always submit; the new memory-based alignment
            # in the bar loop suppresses the inversion soft-stop for
            # aligned trades.
            conviction = 1.0
            if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                conviction = structure_conviction(
                    structure_state, bar, direction,
                    bos_boost_max_age_bars=ms_max_boost_age,
                    choch_plus_boost_max_age_bars=ms_max_boost_age,
                    choch_caution_max_age_bars=ms_choch_caution_age,
                )
                if conviction < ms_min_conv:
                    continue
            out.append(PendingSignal(
                trigger_bar=bar,
                direction=direction,
                triggered_by="orb",
                stop_usd=sl_usd,
                target_usd=tp_usd,
                conviction=conviction,
            ))

    elif src == "wyckoff":
        states = detect_wyckoff(high, low, close, ict)
        for i, st in enumerate(states):
            d = 0
            if st.is_spring:
                d = 1  # Failed breakdown → buy (Wyckoff: spring marks the end of accumulation)
            elif st.is_utad:
                d = -1  # Failed breakout → sell (UTAD marks the end of distribution)
            if d == 0:
                continue
            if bias_gate and _bias(i) != 0 and d != _bias(i):
                continue
            # BoS/CHoCH directional gate (legacy) REMOVED 2026-09-16.
            conviction = 1.0
            if use_ms and structure_state is not None and src_apply_ms.get(src, True):
                conviction = structure_conviction(
                    structure_state, i, d,
                    bos_boost_max_age_bars=ms_max_boost_age,
                    choch_plus_boost_max_age_bars=ms_max_boost_age,
                    choch_caution_max_age_bars=ms_choch_caution_age,
                )
                if conviction < ms_min_conv:
                    continue
            out.append(PendingSignal(
                trigger_bar=i,
                direction=d,
                triggered_by="wyckoff",
                stop_usd=sl_usd,
                target_usd=tp_usd,
                conviction=conviction,
            ))

    return out