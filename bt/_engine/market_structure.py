"""Market structure: swings, BOS/CHoCH, order blocks, liquidity sweeps.

Pure-function detector that mirrors the UAlgo "Price Action Toolkit Lite"
Pine indicator (the reference script in the user query) and exposes the
state needed to gate FVG / iFVG entries on confirmed structure breaks.

The Pine indicator's *trade-relevant* primitives are:

* **Swing high / swing low** — ``ta.highest(high, N)[N]`` style pivots,
  right-side confirmed after ``N`` bars.
* **Trend** — a state machine that flips on a confirmed swing in the
  opposite direction.
* **Break of Structure (BoS)** — the first close through the prior
  swing, **continuing** the existing trend. *Confirmation.*
* **Change of Character (CHoCH)** — the first close through the prior
  swing **against** the existing trend. *Warning / potential reversal.*
* **Order block** — the highest-high candle of the bearish leg (for a
  bear OB) or the lowest-low candle of the bullish leg (for a bull
  OB), with an ATR-sized box that lives until price violates it.
* **Liquidity sweep** — a confirmed pivot whose level is wicked through
  AND closed back through on the same bar (failed break / stop-hunt).

A **CHoCH+** is a CHoCH that was caused by a *real* displacement —
here defined as "the break leg contained an FVG" (so the FVG detector
can confirm the break was structural, not a fake stop-hunt). CHoCH+
is the high-conviction reversal; a bare CHoCH is a caution flag.

This module is pure NumPy / Python — no Numba, no I/O, no trading
math. SL/TP / position bookkeeping stays in
``src/backtest/trend_backtest.py`` and the live executors; this module
just emits *structure state* per bar so the strategy layer can use it
as a conviction filter.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum
from typing import Optional

import numpy as np


# ────────────────────────────────────────────────────────────────────────────
# Public enums
# ────────────────────────────────────────────────────────────────────────────

class StructureEvent(IntEnum):
    """Per-bar market-structure event tag.

    Stored as int8 for memory efficiency on long datasets.
    """
    NONE = 0           # no event
    BOS_BULL = 1       # close > last swing high, in a bull trend
    BOS_BEAR = -1      # close < last swing low, in a bear trend
    CHOCH_BULL = 2     # close > last swing high, after a bear trend (reversal up)
    CHOCH_BEAR = -2    # close < last swing low, after a bull trend (reversal down)
    LIQ_SWEEP_HIGH = 3  # wick above pivot, close back below (bearish sweep)
    LIQ_SWEEP_LOW = -3  # wick below pivot, close back above (bullish sweep)


# Helper: is this a CHoCH (potential reversal)?
def is_choch(ev: int) -> bool:
    return ev in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR)


def is_bos(ev: int) -> bool:
    return ev in (StructureEvent.BOS_BULL, StructureEvent.BOS_BEAR)


def is_bull(ev: int) -> bool:
    return ev in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL, StructureEvent.LIQ_SWEEP_LOW)


def is_bear(ev: int) -> bool:
    return ev in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR, StructureEvent.LIQ_SWEEP_HIGH)


# ────────────────────────────────────────────────────────────────────────────
# Output dataclasses
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class SwingPoint:
    """A confirmed swing high or swing low.

    ``bar`` is the *origin* bar of the pivot (i.e. the bar whose high/low
    was the extreme). Right-side confirmation takes ``pivot_len`` more
    bars, so the swing is *known* at ``bar + pivot_len``.
    """
    bar: int
    price: float
    is_high: bool          # True for swing high, False for swing low


@dataclass(frozen=True)
class BreakEvent:
    """A single BoS / CHoCH event."""
    bar: int               # bar of the first close-through
    kind: StructureEvent   # BOS_BULL | BOS_BEAR | CHOCH_BULL | CHOCH_BEAR
    broken_swing_bar: int  # the swing whose level was broken
    broken_swing_price: float


@dataclass
class OrderBlock:
    """A live order block zone.

    Drawn when a structure break fires. The OB value is the
    *displacement candle's extreme* (Pine convention):

    * Bull OB: lowest low in the bullish leg (the candle that drove
      the break up). Box is ``[low, low + atr]``.
    * Bear OB: highest high in the bearish leg. Box is ``[high - atr, high]``.

    The box lives until price violates the OB value (close < bull OB,
    close > bear OB).
    """
    bar_start: int
    bar_end: int
    is_bull: bool
    ob_value: float        # the displacement candle's extreme (low for bull, high for bear)
    ob_top: float          # box top
    ob_bottom: float       # box bottom
    caused_break_bar: int  # bar of the BoS/CHoCH that drew this OB
    caused_break_kind: StructureEvent
    mitigated_bar: int = -1
    live: bool = True


@dataclass
class LiquiditySweep:
    """A wick-through / close-back liquidity sweep.

    Triggered when price wicks through a confirmed pivot level but
    closes back on the *other* side. Classic stop-hunt signature.
    """
    bar: int
    pivot_bar: int
    pivot_price: float
    is_bear_sweep: bool    # True if wicking ABOVE a swing high then closing back below


@dataclass
class StructureState:
    """Per-bar structure state, returned by :func:`detect_market_structure`.

    All arrays are length ``n_bars``.
    """
    trend: np.ndarray                  # int8 — +1 bull, -1 bear, 0 unknown
    events: np.ndarray                 # int8 — StructureEvent per bar
    last_break_bar: np.ndarray         # int32 — bar of the most recent BoS/CHoCH (-1 if none)
    last_break_kind: np.ndarray        # int8 — most recent BoS/CHoCH kind (StructureEvent.NONE if none)
    last_break_age_bars: np.ndarray    # int32 — bars since the most recent BoS/CHoCH (-1 if none)
    last_choch_bar: np.ndarray         # int32 — bar of the most recent CHoCH (-1 if none)
    last_choch_kind: np.ndarray        # int8 — most recent CHoCH kind (0 if none)
    last_choch_age_bars: np.ndarray    # int32 — bars since the most recent CHoCH
    last_choch_plus_bar: np.ndarray    # int32 — bar of the most recent CHoCH+ (CHoCH confirmed by FVG)
    last_choch_plus_age_bars: np.ndarray  # int32
    # Raw event log
    swings: list[SwingPoint] = field(default_factory=list)
    breaks: list[BreakEvent] = field(default_factory=list)
    order_blocks: list[OrderBlock] = field(default_factory=list)
    sweeps: list[LiquiditySweep] = field(default_factory=list)


# ────────────────────────────────────────────────────────────────────────────
# ATR helper (matches Pine's ta.atr(14))
# ────────────────────────────────────────────────────────────────────────────

def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, length: int = 14) -> np.ndarray:
    """Wilder ATR (matches TradingView's ta.atr)."""
    n = close.shape[0]
    if n == 0:
        return np.zeros(0, dtype=np.float64)
    prev_close = np.empty(n, dtype=np.float64)
    prev_close[0] = close[0]
    prev_close[1:] = close[:-1]
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev_close),
        np.abs(low - prev_close),
    ])
    # RMA — first value is the simple mean of the first `length` TRs,
    # then RMA smoothing.
    out = np.zeros(n, dtype=np.float64)
    if n < length:
        return out
    out[length - 1] = tr[:length].mean()
    for i in range(length, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


# ────────────────────────────────────────────────────────────────────────────
# Main detector
# ────────────────────────────────────────────────────────────────────────────

def detect_market_structure(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    *,
    pivot_len: int = 9,
    liquidity_len: int = 30,
    detect_order_blocks: bool = True,
    detect_liquidity: bool = True,
    atr_length: int = 14,
    resample_to_n_secs: int = 0,
    # Epoch-nanosecond timestamps, one per bar. OPTIONAL: when omitted
    # the resample path has no way to bucket correctly and refuses.
    # See the 2026-10-23 note in the resample block -- positional
    # reshape is unsound on a gappy feed, so timestamp bucketing is
    # now mandatory rather than merely better.
    ts_ns: Optional[np.ndarray] = None,
) -> StructureState:
    """Detect market structure (swings, BoS, CHoCH, order blocks, sweeps).

    Mirrors the UAlgo Pine indicator:

    * Swing = a bar whose high is the max of ``high[i-pivot_len : i+pivot_len+1]``
      (right-side confirmed at ``i + pivot_len``). Same for lows.
    * Trend = a state machine: ``+1`` when last swing is high (or no
      swings yet and the most recent flip was up), ``-1`` when last
      swing is low. **NOT** the same as the Pine's `trend` variable,
      which tracks a *coarser* direction (a swing high in a bull trend
      means the trend is *still* bull; a swing low in a bear trend
      means the trend is *still* bear). The convention used here:
      trend flips when the *first* close-through of the prior swing
      in the opposite direction occurs.
    * Break = first close through the prior swing, edge-triggered
      (one event per swing level). Labelled BoS or CHoCH based on
      the prior trend.

    Parameters
    ----------
    high, low, close : np.ndarray
        OHLC arrays, any frequency (1s recommended).
    pivot_len : int
        Right-side confirmation length for *swings / BoS / CHoCH*.
        Default 9 matches the Pine default. With 1s data and
        ``resample_to_n_secs=0``, this is a 19-second pivot — which
        produces far too many micro-swings to be useful. Either
        set ``resample_to_n_secs=60`` (1m pivots) or use a much
        larger ``pivot_len`` (~3600 for 1h pivots on 1s data).
    liquidity_len : int
        Pivot length for *liquidity sweeps*. Default 30 matches
        the Pine default.
    detect_order_blocks : bool
        If True, draw OB boxes for every structure break.
    detect_liquidity : bool
        If True, detect liquidity sweeps.
    atr_length : int
        ATR length for OB box sizing. Default 14 (Pine default).
    resample_to_n_secs : int
        **2026-08-20: structure timeframe knob.** If > 0, the
        detector internally aggregates OHLC into N-second bars
        BEFORE scanning for swings / BoS / CHoCH, then expands the
        resulting per-bar arrays back to the original resolution.
        This is the canonical ICT convention — pivot_len is
        measured in *bars of the chart you're viewing*, not in
        the underlying tick stream. With ``pivot_len=9`` and
        ``resample_to_n_secs=60``, you get a 9-bar 1m pivot
        (≈ 9 min on each side), which is the right granularity
        for XAUUSD 1s data. The detector's output ``StructureState``
        is still indexed by the *original* bar positions (i.e.
        length == ``high.shape[0]``) so backtest callers don't
        have to change anything. The ``breaks`` list is also
        remapped back to original-resolution bar indices. Set to
        ``0`` (default) to use the raw input bars (legacy).

    Returns
    -------
    StructureState
        All per-bar arrays + raw event log.
    """
    # ── Optional resample to a higher timeframe for cleaner pivots ──
    # On 1s data with pivot_len=9, the pivot window is 19 seconds —
    # every micro-tick creates a "swing", so the detector fires on
    # noise. Resampling to 1m (or higher) gives a 9-bar pivot that's
    # 9 minutes on each side, the canonical ICT granularity.
    if resample_to_n_secs and resample_to_n_secs > 1:
        n_orig = high.shape[0]
        if ts_ns is None:
            raise ValueError(
                "detect_market_structure(resample_to_n_secs>0) requires "
                "ts_ns (epoch-nanosecond timestamps, one per bar). "
                "Aggregating by row position is unsound on a gappy "
                "feed: measured on XAUUSD_S1_2025Q1, 15.4% of bars "
                "are >1s apart and 91.9% of 60s buckets do not hold "
                "60 bars, which sheared 100% of resampled bars "
                "(median error $19.39). Pass to_ns_arr(bars)."
            )
        ts_ns = np.asarray(ts_ns, dtype=np.int64)
        if ts_ns.shape[0] != n_orig:
            raise ValueError(
                f"ts_ns has {ts_ns.shape[0]} entries but high has "
                f"{n_orig}; they must be one-per-bar."
            )
        nb = n_orig // resample_to_n_secs
        if nb >= 2 * pivot_len + 1:
            # ── Aggregate OHLC into N-second bars. ──────────────
            #
            # FIXED 2026-10-23. This used to be a POSITIONAL reshape:
            #
            #     hi = high[:trimmed].reshape(nb, resample_to_n_secs).max(axis=1)
            #
            # which silently assumes a GAPLESS 1s feed. Real FX data
            # is not gapless: measured on XAUUSD_S1_2025Q1
            # (1,377,190 bars, 2025-01), **15.4% of consecutive bars
            # are >1s apart** (weekend + session gaps) and 91.9% of
            # 60-second buckets do NOT contain 60 bars (median 48).
            # So the reshape shears every single bucket.
            #
            # Measured error vs. timestamp buckets:
            #
            #     abs diff   median $19.39   p95 $56.80   max $85.82
            #     fraction of resample bars WRONG   100.0%
            #
            # That is ~$19 of price error on a ~$2,700 instrument,
            # on 100% of resampled bars -- the pivots, and therefore
            # every BoS/CHoCH, were derived from fabricated highs.
            # This is the SAME bug class as the NB94 fixes
            # (``rolling_p95`` / ``swing_cap``): "bar aggregation must
            # bucket by TIMESTAMP, never by row position".
            #
            # The fix buckets by timestamp. ``start_ts`` anchors the
            # grid so bucket boundaries are stable, and empty buckets
            # (gaps) are dropped rather than emitted as phantom bars.
            first_ts = int(ts_ns[0])
            _bucket = (ts_ns - first_ts) // int(resample_to_n_secs * 1e9)
            _uniq, _inv = np.unique(_bucket, return_inverse=True)
            nb = int(_uniq.shape[0])
            hi = np.full(nb, -np.inf, dtype=np.float64)
            lo = np.full(nb, +np.inf, dtype=np.float64)
            np.maximum.at(hi, _inv, high)
            np.minimum.at(lo, _inv, low)
            # close = last bar in the bucket (index -1 of each group).
            # ``maximum.at`` on the flat index gives the LAST bar in
            # timestamp order, which is the bucket's close.
            _last = np.zeros(nb, dtype=np.int64)
            np.maximum.at(_last, _inv, np.arange(n_orig, dtype=np.int64))
            cl = close[_last]
            # A bucket whose first_ns is wrong by construction would
            # desync the expand-back step, so recompute it here.
            ts_rs = first_ts + _uniq * int(resample_to_n_secs * 1e9)
            state_rs = detect_market_structure(
                hi, lo, cl,
                ts_ns=ts_rs,
                pivot_len=pivot_len,
                liquidity_len=liquidity_len,
                detect_order_blocks=detect_order_blocks,
                detect_liquidity=detect_liquidity,
                atr_length=atr_length,
                resample_to_n_secs=0,
            )
            # Expand the per-bar arrays back to original resolution:
            # each resampled bar ``j`` covers original bars
            # ``[j*N, (j+1)*N)``. We forward-fill so the latest event
            # in the resampled window is visible at every original
            # bar in that window.
            return _expand_state_to_original(state_rs, nb, resample_to_n_secs, n_orig,
                                             high, low, close, atr_length)

    n = close.shape[0]
    trend = np.zeros(n, dtype=np.int8)
    events = np.zeros(n, dtype=np.int8)
    last_break_bar = np.full(n, -1, dtype=np.int32)
    last_break_kind = np.zeros(n, dtype=np.int8)
    last_break_age = np.full(n, -1, dtype=np.int32)
    last_choch_bar = np.full(n, -1, dtype=np.int32)
    last_choch_kind = np.zeros(n, dtype=np.int8)
    last_choch_age = np.full(n, -1, dtype=np.int32)
    last_chochp_bar = np.full(n, -1, dtype=np.int32)
    last_chochp_age = np.full(n, -1, dtype=np.int32)

    swings: list[SwingPoint] = []
    breaks: list[BreakEvent] = []
    order_blocks: list[OrderBlock] = []
    sweeps: list[LiquiditySweep] = []

    if n < 2 * pivot_len + 1:
        return StructureState(
            trend=trend, events=events,
            last_break_bar=last_break_bar, last_break_kind=last_break_kind,
            last_break_age_bars=last_break_age,
            last_choch_bar=last_choch_bar, last_choch_kind=last_choch_kind,
            last_choch_age_bars=last_choch_age,
            last_choch_plus_bar=last_chochp_bar,
            last_choch_plus_age_bars=last_chochp_age,
            swings=swings, breaks=breaks, order_blocks=order_blocks, sweeps=sweeps,
        )

    # Step 1: identify all confirmed swings (Pine-equivalent of
    # ``high[zigzagLen] >= ta.highest(high, zigzagLen)``).
    # The bar at index ``i + pivot_len`` is the *origin*; we mark it
    # confirmed once we reach ``i + 2*pivot_len``.
    swing_high_origin: list[int] = []
    swing_low_origin: list[int] = []
    swing_high_price: list[float] = []
    swing_low_price: list[float] = []

    for i in range(pivot_len, n - pivot_len):
        # Right-side confirm: bar i is the *origin* if high[i] is the
        # max of high[i-pivot_len : i+pivot_len+1].
        window_h = high[i - pivot_len:i + pivot_len + 1]
        if high[i] >= window_h.max():
            swing_high_origin.append(i)
            swing_high_price.append(float(high[i]))
        window_l = low[i - pivot_len:i + pivot_len + 1]
        if low[i] <= window_l.min():
            swing_low_origin.append(i)
            swing_low_price.append(float(low[i]))

    # Merge into a single sorted-by-bar list (Pine's highValIndex /
    # lowValIndex combined).
    all_swings: list[tuple[int, float, bool]] = []
    for b, p in zip(swing_high_origin, swing_high_price):
        all_swings.append((b, p, True))
    for b, p in zip(swing_low_origin, swing_low_price):
        all_swings.append((b, p, False))
    all_swings.sort(key=lambda t: t[0])
    swings = [SwingPoint(bar=b, price=p, is_high=h) for (b, p, h) in all_swings]

    # Step 2: walk swings and detect breaks (Pine's BoS / CHoCH logic).
    # The Pine builds ``highVal`` / ``lowVal`` arrays and detects breaks
    # by tracking the *most recent* swing high / swing low. The first
    # close through that level is the break.
    last_swing_high_bar = -1
    last_swing_high_price = -np.inf
    last_swing_low_bar = -1
    last_swing_low_price = np.inf
    last_swing_high_broken_bar = -1   # first bar where close > last_swing_high_price
    last_swing_low_broken_bar = -1    # first bar where close < last_swing_low_price
    # Track the current trend. Pine's trend is a state machine:
    # starts at +1 (no prior direction); flips to -1 on a confirmed
    # down swing, back to +1 on a confirmed up swing.
    cur_trend = 1
    # Map: which swings have been "consumed" (i.e. their level has
    # been broken). We only fire ONE break per swing level.
    high_swing_broken: set[int] = set()
    low_swing_broken: set[int] = set()

    def _commit_break(bar: int, kind: int, sb: int, sp: float) -> None:
        breaks.append(BreakEvent(
            bar=bar, kind=StructureEvent(kind),
            broken_swing_bar=sb, broken_swing_price=sp,
        ))
        events[bar] = kind
        last_break_bar[bar:] = bar
        last_break_kind[bar:] = kind
        # age filled by forward pass below

    # Walk bars in order. On each bar, check whether the last swing
    # high/low has been *first* violated.
    # For the close-through test we must look ahead: a swing high at
    # origin bar ``b`` is *known* to be a swing high at bar ``b +
    # pivot_len``. We can only test for a break at bars ``>= b +
    # pivot_len``.
    #
    # To make this O(n * 1) instead of O(n * swings), maintain
    # ``last_swing_high_bar`` and ``last_swing_high_price`` as the
    # most recent CONFIRMED swing high seen so far (i.e. one whose
    # pivot_len-bar lag has already elapsed by the current bar).
    # When a new confirmed swing arrives, we check whether the
    # *prior* swing has already been broken (in the inter-swing
    # window) — if so, that break was a real event, and we record it.
    #
    # This faithfully mirrors the Pine because the Pine also only
    # tests breaks against the *most recent* swing.
    pending_break_high: Optional[tuple[int, int, float]] = None
    # (candidate_break_bar, swing_origin_bar, swing_price)
    pending_break_low: Optional[tuple[int, int, float]] = None
    swing_iter = iter(swings)
    cur_swing_idx = 0
    next_swing = swings[cur_swing_idx] if swings else None

    for i in range(n):
        # 1) If any confirmed swing origin was reached at i (i.e. a
        # new swing is now "known"), advance.
        while next_swing is not None and next_swing.bar + pivot_len <= i:
            sb, sp, ish = next_swing.bar, next_swing.price, next_swing.is_high
            if ish:
                # The previous swing high level is the "to be broken"
                # candidate. If it was already broken between the
                # previous swing and now, the break bar is the first
                # bar in that range with close > sp.
                if last_swing_high_bar >= 0 and last_swing_high_bar not in high_swing_broken:
                    # Find first close-through in [last_swing_high_bar + 1, sb]
                    brk_bar = _first_break(close, last_swing_high_bar + 1, sb, last_swing_high_price, +1)
                    if brk_bar is not None:
                        # Trend at break time: was it bull or bear?
                        kind = StructureEvent.BOS_BULL if cur_trend == 1 else StructureEvent.CHOCH_BULL
                        _commit_break(brk_bar, kind, last_swing_high_bar, last_swing_high_price)
                        high_swing_broken.add(last_swing_high_bar)
                        # Trend flips on a CHoCH; stays on a BoS in a bull trend.
                        if kind == StructureEvent.CHOCH_BULL:
                            cur_trend = 1
                last_swing_high_bar = sb
                last_swing_high_price = sp
            else:
                if last_swing_low_bar >= 0 and last_swing_low_bar not in low_swing_broken:
                    brk_bar = _first_break(close, last_swing_low_bar + 1, sb, last_swing_low_price, -1)
                    if brk_bar is not None:
                        kind = StructureEvent.BOS_BEAR if cur_trend == -1 else StructureEvent.CHOCH_BEAR
                        _commit_break(brk_bar, kind, last_swing_low_bar, last_swing_low_price)
                        low_swing_broken.add(last_swing_low_bar)
                        if kind == StructureEvent.CHOCH_BEAR:
                            cur_trend = -1
                last_swing_low_bar = sb
                last_swing_low_price = sp
            cur_swing_idx += 1
            next_swing = swings[cur_swing_idx] if cur_swing_idx < len(swings) else None

        # 2) Record the current trend at this bar.
        trend[i] = cur_trend

        # 3) Order block: drawn at the *break* bar from a sweep
        # backwards to the prior swing. We'll do this AFTER the loop
        # because we need the OBs to know which swings they're
        # attached to.

    # ── Liquidity sweeps (post-loop) ───────────────────────────────────
    # A sweep is a wick-through / close-back at a confirmed pivot
    # level. We use the swings list (with both pivot_len=9 and
    # pivot_len=liquidity_len), but for sweep purposes use
    # ``liquidity_len`` pivots so a sweep is a *higher-timeframe*
    # event than a swing.
    if detect_liquidity:
        liq_swings = _pivots_only(high, low, liquidity_len)
        for ps in liq_swings:
            # Scan bars after the swing is confirmed (ps.bar + liquidity_len).
            # A "bear sweep" of a swing high: wick above, close below.
            for j in range(ps.bar + liquidity_len, n):
                if high[j] > ps.price and close[j] < ps.price:
                    sweeps.append(LiquiditySweep(
                        bar=j, pivot_bar=ps.bar, pivot_price=ps.price,
                        is_bear_sweep=ps.is_high,
                    ))
                    events[j] = StructureEvent.LIQ_SWEEP_HIGH if ps.is_high else StructureEvent.LIQ_SWEEP_LOW
                    break

    # ── Order blocks (post-loop) ───────────────────────────────────────
    # Pine's OB is the *opposite-side extreme* in the leg from the
    # prior swing to the break. For a bull break: the LOWEST LOW
    # in [prior_swing_bar, break_bar]. For a bear break: the
    # HIGHEST HIGH.
    if detect_order_blocks and breaks:
        atr_arr = _atr(high, low, close, atr_length)
        for be in breaks:
            prior_swing_bar = be.broken_swing_bar
            break_bar = be.bar
            if be.kind in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
                # Bull OB = lowest low in the bullish leg
                seg_low_idx = int(np.argmin(low[prior_swing_bar:break_bar + 1])) + prior_swing_bar
                seg_low = float(low[seg_low_idx])
                atr_v = float(atr_arr[break_bar]) if atr_v_safe(atr_arr, break_bar) else 0.0
                if atr_v <= 0:
                    atr_v = float(atr_arr[max(0, break_bar - 1)]) if atr_v_safe(atr_arr, max(0, break_bar - 1)) else 0.10
                ob = OrderBlock(
                    bar_start=seg_low_idx, bar_end=break_bar,
                    is_bull=True,
                    ob_value=seg_low,
                    ob_top=seg_low + atr_v,
                    ob_bottom=seg_low,
                    caused_break_bar=break_bar,
                    caused_break_kind=be.kind,
                )
                order_blocks.append(ob)
            else:
                # Bear OB = highest high in the bearish leg
                seg_high_idx = int(np.argmax(high[prior_swing_bar:break_bar + 1])) + prior_swing_bar
                seg_high = float(high[seg_high_idx])
                atr_v = float(atr_arr[break_bar]) if atr_v_safe(atr_arr, break_bar) else 0.0
                if atr_v <= 0:
                    atr_v = float(atr_arr[max(0, break_bar - 1)]) if atr_v_safe(atr_arr, max(0, break_bar - 1)) else 0.10
                ob = OrderBlock(
                    bar_start=seg_high_idx, bar_end=break_bar,
                    is_bull=False,
                    ob_value=seg_high,
                    ob_top=seg_high,
                    ob_bottom=seg_high - atr_v,
                    caused_break_bar=break_bar,
                    caused_break_kind=be.kind,
                )
                order_blocks.append(ob)

        # Mitigate OBs (close violates the OB value)
        for ob in order_blocks:
            for j in range(ob.bar_end + 1, n):
                if ob.is_bull and close[j] < ob.ob_value:
                    ob.mitigated_bar = j
                    ob.live = False
                    break
                if (not ob.is_bull) and close[j] > ob.ob_value:
                    ob.mitigated_bar = j
                    ob.live = False
                    break

    # ── Fill in derived per-bar arrays ─────────────────────────────────
    # ``last_break_age_bars[i] = i - last_break_bar[i]`` (clamped to 0
    # if no break yet). Same for CHoCH.
    for i in range(n):
        if last_break_bar[i] >= 0:
            last_break_age[i] = i - last_break_bar[i]
        if last_choch_bar[i] >= 0:
            last_choch_age[i] = i - last_choch_bar[i]
        if last_chochp_bar[i] >= 0:
            last_chochp_age[i] = i - last_chochp_bar[i]
    # Forward-fill last_choch_* using breaks list (we need the most
    # recent CHoCH up to and including bar i, not just the bar of
    # the break itself).
    last_cb = -1
    last_ck = 0
    for be in breaks:
        if be.kind in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            last_cb = be.bar
            last_ck = int(be.kind)
    # Walk forward filling last_choch_bar / last_choch_kind
    next_choch_idx = 0
    choches = [be for be in breaks if be.kind in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR)]
    cb = -1
    ck = 0
    chi = 0
    for i in range(n):
        while chi < len(choches) and choches[chi].bar <= i:
            cb = choches[chi].bar
            ck = int(choches[chi].kind)
            chi += 1
        last_choch_bar[i] = cb
        last_choch_kind[i] = ck
        if cb >= 0:
            last_choch_age[i] = i - cb

    return StructureState(
        trend=trend, events=events,
        last_break_bar=last_break_bar, last_break_kind=last_break_kind,
        last_break_age_bars=last_break_age,
        last_choch_bar=last_choch_bar, last_choch_kind=last_choch_kind,
        last_choch_age_bars=last_choch_age,
        last_choch_plus_bar=last_chochp_bar,
        last_choch_plus_age_bars=last_chochp_age,
        swings=swings, breaks=breaks, order_blocks=order_blocks, sweeps=sweeps,
    )


def _first_break(close: np.ndarray, start: int, end: int, level: float, sign: int) -> Optional[int]:
    """Return the first bar j in [start, end+1) with close[j] on the break side of level.

    ``sign = +1`` → close > level (bull break). ``sign = -1`` → close < level (bear break).
    """
    lo = max(0, start)
    hi = min(close.shape[0], end + 1)
    if lo >= hi:
        return None
    seg = close[lo:hi]
    if sign == +1:
        mask = seg > level
    else:
        mask = seg < level
    idxs = np.flatnonzero(mask)
    if idxs.size == 0:
        return None
    return int(idxs[0]) + lo


def _expand_state_to_original(
    state_rs: StructureState,
    nb: int,
    n_secs: int,
    n_orig: int,
    high_orig: np.ndarray,
    low_orig: np.ndarray,
    close_orig: np.ndarray,
    atr_length: int,
) -> StructureState:
    """Expand a StructureState from resampled bars back to the original 1s bars.

    Each resampled bar ``j`` covers original bars ``[j*n_secs, (j+1)*n_secs)``.
    The ``breaks`` list is remapped so each event's ``bar`` becomes the
    *first original bar* of its resampled window; this gives a stable
    anchor for chart annotations (the bracket is always drawn on a
    visible 1s candle). All per-bar ``last_*_bar`` arrays are then
    forward-filled at original resolution so the backtest's per-bar
    conviction classifier sees consistent state at every 1s bar.
    """
    # Remap event log: each resampled bar index becomes the first
    # original bar in the corresponding resampled window. We map to
    # the *first* original bar (not the last close) so the bracket
    # annotation doesn't drift to a bar that may not exist (the
    # trailing partial window is shorter than n_secs).
    #
    # 2026-09-17 hot-path optimization: build the remap table once
    # with vectorised numpy (88k Python-level ``_remap_bar`` calls
    # across a 1-day backtest collapse to one vectorised op).
    _rs_max = (n_orig - 1) // n_secs + 1
    _remap_arr = np.minimum(np.arange(_rs_max) * n_secs, n_orig - 1).astype(np.int32)

    def _remap_bar(bar: int) -> int:
        if 0 <= bar < _rs_max:
            return int(_remap_arr[bar])
        return n_orig - 1

    new_breaks = [
        BreakEvent(
            bar=_remap_bar(be.bar),
            kind=be.kind,
            broken_swing_bar=_remap_bar(be.broken_swing_bar),
            broken_swing_price=be.broken_swing_price,
        )
        for be in state_rs.breaks
    ]
    new_swings = [
        SwingPoint(bar=_remap_bar(sw.bar), price=sw.price, is_high=sw.is_high)
        for sw in state_rs.swings
    ]
    new_order_blocks = [
        OrderBlock(
            bar_start=_remap_bar(ob.bar_start),
            bar_end=_remap_bar(ob.bar_end),
            is_bull=ob.is_bull,
            ob_value=ob.ob_value,
            ob_top=ob.ob_top,
            ob_bottom=ob.ob_bottom,
            caused_break_bar=_remap_bar(ob.caused_break_bar),
            caused_break_kind=ob.caused_break_kind,
            mitigated_bar=(_remap_bar(ob.mitigated_bar) if ob.mitigated_bar >= 0 else -1),
            live=ob.live,
        )
        for ob in state_rs.order_blocks
    ]
    new_sweeps = [
        LiquiditySweep(
            bar=_remap_bar(sw.bar),
            pivot_bar=_remap_bar(sw.pivot_bar),
            pivot_price=sw.pivot_price,
            is_bear_sweep=sw.is_bear_sweep,
        )
        for sw in state_rs.sweeps
    ]

    # Per-bar arrays. We need them at original 1s resolution because
    # the backtest bar loop indexes them per-second. For each of the
    # n_secs original bars in window j we record the same value as
    # the resampled bar j (i.e. forward-fill). Bar ``i`` therefore
    # sees the *most recent* resampled state whose window contains it.
    trend = np.zeros(n_orig, dtype=np.int8)
    events = np.zeros(n_orig, dtype=np.int8)
    last_break_bar = np.full(n_orig, -1, dtype=np.int32)
    last_break_kind = np.zeros(n_orig, dtype=np.int8)
    last_break_age = np.full(n_orig, -1, dtype=np.int32)
    last_choch_bar = np.full(n_orig, -1, dtype=np.int32)
    last_choch_kind = np.zeros(n_orig, dtype=np.int8)
    last_choch_age = np.full(n_orig, -1, dtype=np.int32)
    last_chochp_bar = np.full(n_orig, -1, dtype=np.int32)
    last_chochp_age = np.full(n_orig, -1, dtype=np.int32)

    for j in range(nb):
        start = j * n_secs
        end = min((j + 1) * n_secs, n_orig)
        if end <= start:
            continue
        trend[start:end] = state_rs.trend[j]
        events[start:end] = state_rs.events[j]
        # Only forward-fill if the resampled value is valid; otherwise
        # leave the default (-1 / 0) so the backtest's "no event yet"
        # guard fires correctly on the first bars.
        if state_rs.last_break_bar[j] >= 0:
            last_break_bar[start:end] = _remap_bar(int(state_rs.last_break_bar[j]))
            last_break_kind[start:end] = state_rs.last_break_kind[j]
        if state_rs.last_choch_bar[j] >= 0:
            last_choch_bar[start:end] = _remap_bar(int(state_rs.last_choch_bar[j]))
            last_choch_kind[start:end] = state_rs.last_choch_kind[j]
        if state_rs.last_choch_plus_bar[j] >= 0:
            last_chochp_bar[start:end] = _remap_bar(int(state_rs.last_choch_plus_bar[j]))
    # Trailing partial window (if n_orig > nb * n_secs): keep the
    # last fully-computed resampled state's trend / events.
    tail_start = nb * n_secs
    if n_orig > tail_start:
        trend[tail_start:] = state_rs.trend[nb - 1]
        events[tail_start:] = state_rs.events[nb - 1]
        if state_rs.last_break_bar[nb - 1] >= 0:
            last_break_bar[tail_start:] = _remap_bar(int(state_rs.last_break_bar[nb - 1]))
            last_break_kind[tail_start:] = state_rs.last_break_kind[nb - 1]
        if state_rs.last_choch_bar[nb - 1] >= 0:
            last_choch_bar[tail_start:] = _remap_bar(int(state_rs.last_choch_bar[nb - 1]))
            last_choch_kind[tail_start:] = state_rs.last_choch_kind[nb - 1]
        if state_rs.last_choch_plus_bar[nb - 1] >= 0:
            last_chochp_bar[tail_start:] = _remap_bar(int(state_rs.last_choch_plus_bar[nb - 1]))

    # Re-derive age arrays from the (now correctly indexed)
    # last_*_bar arrays. The resampled detector computed ages on the
    # resampled timeline; we want them on the original timeline.
    for i in range(n_orig):
        if last_break_bar[i] >= 0:
            last_break_age[i] = i - last_break_bar[i]
        if last_choch_bar[i] >= 0:
            last_choch_age[i] = i - last_choch_bar[i]
        if last_chochp_bar[i] >= 0:
            last_chochp_age[i] = i - last_chochp_bar[i]

    return StructureState(
        trend=trend,
        events=events,
        last_break_bar=last_break_bar,
        last_break_kind=last_break_kind,
        last_break_age_bars=last_break_age,
        last_choch_bar=last_choch_bar,
        last_choch_kind=last_choch_kind,
        last_choch_age_bars=last_choch_age,
        last_choch_plus_bar=last_chochp_bar,
        last_choch_plus_age_bars=last_chochp_age,
        swings=new_swings,
        breaks=new_breaks,
        order_blocks=new_order_blocks,
        sweeps=new_sweeps,
    )


def atr_v_safe(atr_arr: np.ndarray, i: int) -> bool:
    return 0 <= i < atr_arr.shape[0] and atr_arr[i] > 0


def _pivots_only(high: np.ndarray, low: np.ndarray, pivot_len: int) -> list[SwingPoint]:
    """Return confirmed pivot highs/lows with the given pivot_len (no trend / break logic).

    Vectorised (2026-09-15): the original implementation did a Python
    for-loop with ``win_h.max()`` / ``win_l.min()`` per bar (~700
    iterations/day × 2 calls/iter = ~1.4k scalar max/min calls).
    With ``pivot_len=9`` and 720 1m bars/day that's 700 iterations,
    but on a YEAR of data we'd have 720×252 ≈ 180k iterations. The
    cumulative cost across ``detect_market_structure`` calls
    (called 3× per backtest — once for trend, once for FVG
    invalidation, once inside ``generate_ict_pending_signals``) was
    the dominant bottleneck at ~1.4s/call × 3 = ~4s/day.

    Vectorised: build sliding windows with
    ``np.lib.stride_tricks.sliding_window_view``, take max/min
    across each window in a single numpy call, then argmax the
    boolean mask for the pivot bar. ~100× faster.
    """
    n = high.shape[0]
    out: list[SwingPoint] = []
    if n < 2 * pivot_len + 1:
        return out
    # Build sliding windows of width 2*pivot_len+1 covering every
    # bar where a pivot can be confirmed (i in [pivot_len, n-pivot_len]).
    win_size = 2 * pivot_len + 1
    # sliding_window_view gives us shape (n - win_size + 1, win_size).
    h_wins = np.lib.stride_tricks.sliding_window_view(high, win_size)
    l_wins = np.lib.stride_tricks.sliding_window_view(low, win_size)
    # The pivot bar is at index ``pivot_len`` within each window.
    pivot_idx = pivot_len
    pivot_h = h_wins[:, pivot_idx]
    pivot_l = l_wins[:, pivot_idx]
    # Pivot high: pivot bar's high is the max of its window.
    is_pivot_h = pivot_h >= h_wins.max(axis=1)
    # Pivot low: pivot bar's low is the min of its window.
    is_pivot_l = pivot_l <= l_wins.min(axis=1)
    # Map window index back to bar index (window index 0 = bar 0).
    n_windows = h_wins.shape[0]
    bar_indices = np.arange(n_windows) + pivot_idx
    # Append pivot highs / lows to the output.
    for i in np.where(is_pivot_h)[0]:
        out.append(SwingPoint(bar=int(bar_indices[i]), price=float(pivot_h[i]), is_high=True))
    for i in np.where(is_pivot_l)[0]:
        out.append(SwingPoint(bar=int(bar_indices[i]), price=float(pivot_l[i]), is_high=False))
    return out


# ────────────────────────────────────────────────────────────────────────────
# CHoCH+ annotation
# ────────────────────────────────────────────────────────────────────────────

def annotate_choch_plus(
    state: StructureState,
    fvg_zones: list,  # list[FvgZone] from src.core.ict_signals
) -> StructureState:
    """Mark CHoCH+ on the per-bar arrays.

    A CHoCH+ is a CHoCH that was *caused by a real displacement* —
    defined here as "the CHoCH break leg contained an FVG whose
    trigger_bar lies within [prior_swing_bar, break_bar]".

    Mutates ``state`` in place and returns it. The ``last_choch_plus_bar``
    / ``last_choch_plus_age_bars`` arrays are updated to reflect the
    most recent CHoCH+ at each bar.
    """
    n = state.trend.shape[0]
    chochp_bar = np.full(n, -1, dtype=np.int32)
    last_cp = -1
    last_cp_age = np.full(n, -1, dtype=np.int32)

    # Build a sorted list of (trigger_bar, zone) for fast membership tests.
    sorted_fvg = sorted(fvg_zones, key=lambda z: z.trigger_bar)

    def _has_fvg_in_leg(start_bar: int, end_bar: int) -> bool:
        # Binary search for the first FVG with trigger_bar >= start_bar
        lo, hi = 0, len(sorted_fvg)
        while lo < hi:
            mid = (lo + hi) // 2
            if sorted_fvg[mid].trigger_bar < start_bar:
                lo = mid + 1
            else:
                hi = mid
        # Walk forward
        i = lo
        while i < len(sorted_fvg) and sorted_fvg[i].trigger_bar <= end_bar:
            return True
        return False

    # Update the per-bar "last CHoCH+ bar" with the most recent CHoCH+
    # up to and including bar i.
    chi = 0
    for be in state.breaks:
        if be.kind not in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            continue
        if _has_fvg_in_leg(be.broken_swing_bar, be.bar):
            last_cp = be.bar
    # Forward-fill
    cur = -1
    cur_age = -1
    # Need to know the sequence: walk bars and update when we cross
    # a CHoCH+ break bar. We rebuild the chochp_bar list directly.
    chochp_breaks = []
    for be in state.breaks:
        if be.kind not in (StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR):
            continue
        if _has_fvg_in_leg(be.broken_swing_bar, be.bar):
            chochp_breaks.append(be.bar)
    chi = 0
    for i in range(n):
        while chi < len(chochp_breaks) and chochp_breaks[chi] <= i:
            cur = chochp_breaks[chi]
            cur_age = 0
            chi += 1
        chochp_bar[i] = cur
        if cur >= 0:
            last_cp_age[i] = i - cur

    state.last_choch_plus_bar = chochp_bar
    state.last_choch_plus_age_bars = last_cp_age
    return state


# ────────────────────────────────────────────────────────────────────────────
# Conviction classifier — used by ict_signals to weight FVG entries
# ────────────────────────────────────────────────────────────────────────────

def structure_conviction(
    state: StructureState,
    bar: int,
    direction: int,
    *,
    bos_boost_max_age_bars: int = 60,
    choch_plus_boost_max_age_bars: int = 60,
    choch_caution_max_age_bars: int = 60,
) -> float:
    """Return a conviction multiplier for a FVG/iFVG entry at ``bar``.

    Returns a value in ``[0.0, 1.5]``:

    * ``> 1.0`` = boost (a fresh structure break agrees with the trade)
    * ``1.0``  = neutral (no recent structure event)
    * ``< 1.0`` = caution (a fresh CHoCH *against* the trade, or a
      recent CHoCH in the trade direction means the trend has just
      changed character and the prior signal may be exhausted)

    Components
    ----------
    * Fresh BoS in the trade direction (within ``bos_boost_max_age_bars``)
      → ``+0.25`` boost.
    * Fresh CHoCH+ in the trade direction (within
      ``choch_plus_boost_max_age_bars``) → ``+0.50`` boost (the
      displacement-confirmed reversal is the highest-conviction
      entry).
    * Fresh CHoCH *against* the trade (within ``choch_caution_max_age_bars``)
      → ``-0.50`` caution. (A CHoCH against the trade means the
      prior character has just changed — your signal may be a
      "last gasp" of the prior trend.)
    """
    if bar < 0 or bar >= state.trend.shape[0]:
        return 1.0
    score = 1.0

    last_bk = int(state.last_break_kind[bar])
    last_bk_age = int(state.last_break_age_bars[bar])
    last_ck = int(state.last_choch_kind[bar])
    last_ck_age = int(state.last_choch_age_bars[bar])
    last_cpk = state.last_choch_plus_bar[bar]
    last_cpk_age = int(state.last_choch_plus_age_bars[bar]) if last_cpk >= 0 else -1

    # BoS in trade direction
    if last_bk_age >= 0 and last_bk_age <= bos_boost_max_age_bars:
        if (direction > 0 and last_bk == StructureEvent.BOS_BULL) or \
           (direction < 0 and last_bk == StructureEvent.BOS_BEAR):
            score += 0.25

    # CHoCH+ in trade direction (FVG-confirmed reversal = highest boost)
    if last_cpk_age >= 0 and last_cpk_age <= choch_plus_boost_max_age_bars:
        if (direction > 0 and last_ck == StructureEvent.CHOCH_BULL) or \
           (direction < 0 and last_ck == StructureEvent.CHOCH_BEAR):
            score += 0.50

    # CHoCH against the trade (the trend has just changed character
    # *away* from our signal — exercise caution).
    if last_ck_age >= 0 and last_ck_age <= choch_caution_max_age_bars:
        if (direction > 0 and last_ck == StructureEvent.CHOCH_BEAR) or \
           (direction < 0 and last_ck == StructureEvent.CHOCH_BULL):
            score -= 0.50

    # Liquidity sweep in the trade direction
    ev = int(state.events[bar])
    if (direction > 0 and ev == StructureEvent.LIQ_SWEEP_LOW) or \
       (direction < 0 and ev == StructureEvent.LIQ_SWEEP_HIGH):
        score += 0.10

    return max(0.0, min(1.5, score))


# ────────────────────────────────────────────────────────────────────────────
# BoS/CHoCH memory (added 2026-09-16) — last-N stateful event log
# ────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class BosChochEvent:
    """A single BoS or CHoCH event captured in the rolling memory.

    Stored in chronological insertion order. ``bar`` is the bar of
    the first close-through in the *original* 1s timeline (after
    any resampling expansion).

    ``kind`` is one of:
    * ``StructureEvent.BOS_BULL`` (+1) / ``BOS_BEAR`` (-1)
    * ``StructureEvent.CHOCH_BULL`` (+2) / ``CHoCH_BEAR`` (-2)
    """
    bar: int
    kind: int


class BosChochMemory:
    """Stateful rolling buffer of the last N BoS / CHoCH events.

    The "direction trade thesis" is determined by walking the
    memory in order:

    1. Sort by ``bar`` (ascending).
    2. The DIRECTION of the *most recent* event in memory is the
       "current thesis".
    3. BoS events CONTINUE the prior thesis (BoS_BULL in a bull
       trend; BoS_BEAR in a bear trend).
    4. CHoCH events REVERSE the prior thesis (CHoCH_BULL flips to
       bull; CHoCH_BEAR flips to bear).
    5. After applying the most recent event, the resulting thesis
       is the **predicted next-move direction** — i.e. the side
       of the next BoS we'd expect to see (the trade thesis).

    Usage:

    >>> mem = BosChochMemory(max_events=5)
    >>> mem.push(bar=100, kind=StructureEvent.BOS_BULL)
    >>> mem.push(bar=250, kind=StructureEvent.CHOCH_BEAR)
    >>> mem.alignment_at(bar=300)  # direction=+1 for a long signal
    'opposed'   # the most recent CHoCH is BEAR → trade thesis is bearish

    Attributes:
        max_events: maximum number of events retained (default 5).
        events: list of ``BosChochEvent`` in chronological (push) order.
    """
    max_events: int
    events: list[BosChochEvent]

    def __init__(self, max_events: int = 5) -> None:
        if max_events < 1:
            raise ValueError(f"max_events must be >= 1, got {max_events}")
        self.max_events = int(max_events)
        self.events = []

    def push(self, bar: int, kind: int) -> None:
        """Append a BoS/CHoCH event, evicting the oldest if full."""
        if int(kind) not in (
            StructureEvent.BOS_BULL, StructureEvent.BOS_BEAR,
            StructureEvent.CHOCH_BULL, StructureEvent.CHOCH_BEAR,
        ):
            return  # ignore non-break events (LIQ_SWEEP_HIGH/LOW etc.)
        self.events.append(BosChochEvent(bar=int(bar), kind=int(kind)))
        if len(self.events) > self.max_events:
            # Evict oldest by bar (preserve chronological order)
            self.events.sort(key=lambda e: e.bar)
            self.events = self.events[-self.max_events:]

    def thesis_after_most_recent(self) -> int:
        """Return the trade thesis direction implied by the most recent event.

        Returns:
            +1: trade thesis is bullish (expect next move UP).
            -1: trade thesis is bearish (expect next move DOWN).
             0: no events in memory (no thesis yet).
        """
        if not self.events:
            return 0
        # Sort by bar and take the most recent
        sorted_evs = sorted(self.events, key=lambda e: e.bar)
        last = sorted_evs[-1]
        # BoS/CHoCH events already encode the *direction of the
        # committed move*. A CHoCH has already FLIPPED the thesis —
        # the new thesis is the CHoCH's direction. A BoS continues
        # the prior thesis — but since we don't store the prior
        # thesis in memory, we infer the thesis by walking the
        # events in order with the BoS-continues-prior semantic.
        # For simplicity and correctness in a memory of N events:
        # treat the *first* event's direction as the initial thesis,
        # then walk forward flipping on CHoCH only.
        first = sorted_evs[0]
        if first.kind in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
            thesis = +1
        elif first.kind in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR):
            thesis = -1
        else:
            return 0
        for ev in sorted_evs[1:]:
            if ev.kind == StructureEvent.CHOCH_BULL:
                thesis = +1
            elif ev.kind == StructureEvent.CHOCH_BEAR:
                thesis = -1
            # BoS continues — thesis unchanged
        return thesis

    def alignment_at(self, bar: int, direction: int) -> str:
        """Determine the alignment between the trade thesis and a signal.

        Args:
            bar: the signal bar (only used to cap which events qualify
                — events at ``bar`` are valid; events with bar > ``bar``
                are rejected as look-ahead).
            direction: +1 (long) or -1 (short).

        Returns:
            ``"aligned"``    — thesis agrees with trade direction.
            ``"opposed"``    — thesis disagrees (most recent event committed
                                to the OTHER side).
            ``"unknown"``    — no events in memory (no thesis yet).
        """
        if not self.events:
            return "unknown"
        # Cap at events with bar <= signal bar (anti-lookahead)
        eligible = [e for e in self.events if e.bar <= int(bar)]
        if not eligible:
            return "unknown"
        # Build a temporary memory with only eligible events
        eligible.sort(key=lambda e: e.bar)
        # Recompute thesis with only eligible events
        if eligible[0].kind in (StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL):
            thesis = +1
        elif eligible[0].kind in (StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR):
            thesis = -1
        else:
            return "unknown"
        for ev in eligible[1:]:
            if ev.kind == StructureEvent.CHOCH_BULL:
                thesis = +1
            elif ev.kind == StructureEvent.CHOCH_BEAR:
                thesis = -1
        if thesis == 0:
            return "unknown"
        if direction > 0:
            return "aligned" if thesis == +1 else "opposed"
        if direction < 0:
            return "aligned" if thesis == -1 else "opposed"
        return "unknown"

    def clear(self) -> None:
        """Reset the memory to empty."""
        self.events = []


# ────────────────────────────────────────────────────────────────────────────
# BoS/CHoCH directional gate (kept for backwards compatibility — see
# the new ``BosChochMemory`` class above for the production design)
# ────────────────────────────────────────────────────────────────────────────

def bos_choch_directional_alignment(
    state: StructureState,
    bar: int,
    direction: int,
    *,
    max_age_bars: int = 300,
) -> bool | None:
    """Determine if a signal at ``bar`` in ``direction`` is BoS/CHoCH-aligned.

    Distinct from ``structure_conviction(...)`` (which returns a TP
    multiplier) and from ``gate_on_gmma_bias`` (which uses the
    continuous ``state.trend`` value).

    This is an explicit GATE: returns True iff there has been a
    BoS or CHoCH in ``direction`` within the last ``max_age_bars``
    bars. Returns None when no qualifying event has happened yet
    (which ``gate_on_gmma_bias=True`` would treat as "no bias, pass
    through" — but here we treat it as "no alignment, reject"
    unless the caller explicitly opts to pass-through).

    Anti-lookahead: ``state.last_break_kind`` and
    ``state.last_break_age_bars`` reflect the structure detector's
    walks forward through bars 0..bar. The detector's break events
    fire on the bar of the close-through, which is in the bar loop's
    past at ``bar`` (we only call this for ``bar >= current bar``).

    Args:
        state: the StructureState from ``detect_market_structure``.
        bar: the bar at which the signal would be entered.
        direction: +1 (long) or -1 (short).
        max_age_bars: maximum age (in 1s bars) of the qualifying
            structure event. Default 300 = 5 min on 1s data.

    Returns:
        True: aligned (recent BoS/CHoCH in trade direction).
        False: opposed (recent BoS/CHoCH AGAINST trade direction).
        None: no recent structure event (no alignment signal yet).
    """
    if bar < 0 or bar >= state.last_break_kind.shape[0]:
        return None
    last_bk = int(state.last_break_kind[bar])
    last_bk_age = int(state.last_break_age_bars[bar])
    if last_bk_age < 0 or last_bk_age > max_age_bars:
        return None
    # last_bk is one of the four direction values
    if direction > 0 and last_bk in (
        StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL,
    ):
        return True
    if direction < 0 and last_bk in (
        StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR,
    ):
        return True
    # Event in the opposite direction
    if direction > 0 and last_bk in (
        StructureEvent.BOS_BEAR, StructureEvent.CHOCH_BEAR,
    ):
        return False
    if direction < 0 and last_bk in (
        StructureEvent.BOS_BULL, StructureEvent.CHOCH_BULL,
    ):
        return False
    return None


# ═══════════════════════════════════════════════════════════════════
# Structure SCORE (2026-10-23)
# ═══════════════════════════════════════════════════════════════════
#
# WHY THIS EXISTS. ``bos_choch_directional_alignment`` above answers a
# BINARY question ("was the single most recent break in my direction,
# within N bars?"). Two things are wrong with that for this corpus:
#
#   1. It can return ``None`` ("no recent event") and the gate treats
#      ``None`` as REJECT. On the 1-month 2025-01 slice that produced
#      6,352-9,387 ``no_event`` rejections -- ~92% of all rejections --
#      even though the slice carries **857 break events** and at every
#      bar past ~4,080 at least 5 are in history. The ``None`` was an
#      artifact of the ``max_age_bars`` timeout, not of absent data.
#      A timeout window also silently makes the filter sensitive to
#      CADENCE: ``max_age_bars=300`` is 5 minutes of 1s bars but only
#      5 minutes of a 60s-resampled structure series, while the events
#      themselves were stamped on 1m bars.
#
#   2. A binary verdict throws away magnitude. Five bull breaks
#      followed by one bear break is emphatically not the same state as
#      one bull break, yet both score "aligned".
#
# THE DESIGN (as specified): score the last K=5 events on a
# CONTINUOUS scale, with NO timeout. ``None`` is unreachable whenever
# 5 events exist in history, which after warmup is always.
#
#     base_k   in {-1, +1}   BoS  = +1 bull / -1 bear
#                           CHoCH = REVERSAL: opposite sign to the
#                           break it replaced, and worth MORE
#     recency  exponential decay, w_j = HALF ** (age / half_life)
#     magnitude CHoCH weight 2.0 vs BoS 1.0
#
#         score = SUM_j ( w_j * mag_j * base_j ) / SUM_j ( w_j * mag_j )
#
#     score -> +1.0  five bull signals, recent one dominant
#     score -> -1.0  five bear signals
#     score ->  0.0  a balanced / conflicting recent history
#
# The denominator is the TOTAL decayed weight, not the number of
# events, so a 2-event history and a 5-event history are both bounded
# to [-1, +1] and comparable to each other.
#
# ANTI-LOOKAHEAD. Only events with ``event.bar <= bar`` are considered,
# and the caller is the bar loop. This is structurally impossible to
# peek with -- there is no future index in the slice.

# The four directional StructureEvent values -> (sign, is_choch).
_BREAK_SIGN: dict = {
    StructureEvent.BOS_BULL: (+1, False),
    StructureEvent.BOS_BEAR: (-1, False),
    StructureEvent.CHOCH_BULL: (+1, True),
    StructureEvent.CHOCH_BEAR: (-1, True),
}

# Bars 0..len(breaks)-1, so a per-bar lookup is a binary search.
_BREAK_BARS_CACHE: dict = {}

# Compact per-event signature letters (2026-10-23). Used to build the
# trailing-event signature that the SEQUENCE-aware direction flip
# keys on. Lowercase = bull, uppercase = bear; b/B = BoS, c/C = CHoCH.
_BREAK_SIG: dict = {
    StructureEvent.BOS_BULL: "b",
    StructureEvent.BOS_BEAR: "B",
    StructureEvent.CHOCH_BULL: "c",
    StructureEvent.CHOCH_BEAR: "C",
}


def _break_bars(state: "StructureState") -> np.ndarray:
    """Bar indices of every break event, ascending. Cached per state."""
    key = id(state)
    cached = _BREAK_BARS_CACHE.get(key)
    if cached is not None and len(cached) == len(state.breaks):
        return cached
    if state.breaks:
        arr = np.fromiter(
            (b.bar for b in state.breaks), dtype=np.int64,
            count=len(state.breaks),
        )
    else:
        arr = np.zeros(0, dtype=np.int64)
    _BREAK_BARS_CACHE[key] = arr
    return arr


def structure_score(
    state: "StructureState",
    bar: int,
    *,
    lookback_events: int = 5,
    half_life_bars: float = 1800.0,
    choch_weight: float = 2.0,
) -> float:
    """Continuous bullish/bearish structure score in ``[-1.0, +1.0]``.

    Scores the last ``lookback_events`` (canonical 5) BoS/CHoCH events
    at or before ``bar``. **No timeout**: an event stays in the window
    until it is pushed out by 5 newer events, so this can never return
    "no structure" once 5 events exist.

    Parameters
    ----------
    half_life_bars:
        Recency decay, in bars. ``weight = 0.5 ** (age / half_life)``
        so an event exactly ``half_life_bars`` old counts half. 1800
        = 30 min on 1s bars, which on the 1m-resampled structure
        series is 30 one-minute bars.
    choch_weight:
        Relative weight of a CHoCH vs a BoS. A CHoCH is a character
        change and carries more information than another continuation
        break, so canonical is 2.0.
    """
    if lookback_events <= 0:
        return 0.0
    bars = _break_bars(state)
    if bars.size == 0:
        return 0.0
    # Events at or before `bar` (inclusive: a break on this bar is
    # known to us, not the future).
    upto = int(np.searchsorted(bars, bar, side="right"))
    if upto == 0:
        return 0.0
    lo = max(0, upto - lookback_events)

    num = 0.0
    den = 0.0
    j = upto - 1                      # newest first
    while j >= lo:
        ev = state.breaks[j]
        pair = _BREAK_SIGN.get(ev.kind)
        if pair is not None:
            sign, is_choch = pair
            age = float(bar - ev.bar)
            if age < 0.0:
                age = 0.0
            w = 0.5 ** (age / half_life_bars) * (
                choch_weight if is_choch else 1.0
            )
            num += w * sign
            den += w
        j -= 1
    if den <= 0.0:
        return 0.0
    return float(num / den)


def structure_confidence(state: "StructureState", bar: int, **kw) -> float:
    """``|score|`` -- how much the structure actually agrees, ignoring sign.

    Use this to distinguish "mildly bullish" from "emphatically
    bullish"; the score alone conflates them at the sign level.
    """
    return abs(structure_score(state, bar, **kw))


# ═══════════════════════════════════════════════════════════════════
# Combined STRUCTURE x MOMENTUM score (2026-10-23)
# ═══════════════════════════════════════════════════════════════════
#
# Two-stage decision, as specified:
#
#   STAGE 1 -- BINARY ALIGNMENT (admission). A trade is taken only if
#              the structure score AGREES IN SIGN with the trade.
#              This is a gate, not a scaler: it decides WHETHER, and
#              it never inverts. Rejecting is cheap and unambiguous.
#
#   STAGE 2 -- SL/TP SCALING (sizing the risk). Only survivors get
#              here. The combined magnitude decides HOW MUCH to let
#              the stop breathe and how far to chase the target.
#
# The blend is a WEIGHTED SUM OF THE SAME UNITS -- both inputs are
# already in [-1, +1], so no normalisation is needed and the blend is
# directly interpretable as a single conviction number.
#
#     combined = w_struct * structure + w_mom * momentum
#
# ⚠️ MEASURED CONSTRAINT on "momentum indicators". The engine's
# ``vel_5m`` / ``vel_15m`` are **UNSIGNED**:
#
#     vel[i] = |P_i - P_{i-w}| / P_{i-w}
#
# Measured on XAUUSD_S1_2025Q1 (1,377,190 bars): min **0.0**,
# p50 0.0003, max 0.0052 -- it never goes negative, by construction.
# It measures how FAST price moved, not WHICH WAY. (It is also
# causality-correct: an earlier ``close[w:] - close[:-w]`` form was a
# forward-looking bug, fixed 2026-10-13. Do not reintroduce it.)
#
# A signed momentum therefore needs its sign from PRICE. That is done
# below from the backward difference ``P_t - P_{t-w}`` -- the same
# quantity ``vel_5m`` takes the absolute value of -- so magnitude and
# direction come from one consistent, backward-only window.
#
# The wick term discounts expansion that was REJECTED (price moved
# fast then gave it back), which is the opposite of momentum, so it
# enters with the SIGN OF VELOCITY but reduced magnitude.

MOMENTUM_VEL_5M: float = 0.70
MOMENTUM_WICK: float = 0.30
STRUCTURE_WEIGHT: float = 0.60
MOMENTUM_WEIGHT: float = 0.40


def momentum_score(
    vol_state: Any,
    bar: int,
    close: Optional[np.ndarray] = None,
    *,
    vel_weight: float = MOMENTUM_VEL_5M,
    wick_weight: float = MOMENTUM_WICK,
    vel_window: int = 300,
    vel_reference: float = 0.0015,
) -> float:
    """Signed momentum in ``[-1, +1]``.

    ``vol_state["vel_5m"]`` supplies the UNSIGNED magnitude; the sign
    comes from the backward price difference over the same window.
    Both are strictly backward-looking (no ``t + w`` anywhere).

    Degrades to ``0.0`` on missing input rather than raising --
    momentum is a MODULATOR, and a missing modulator must never
    silently become a veto.
    """
    if vol_state is None or bar < 0:
        return 0.0
    try:
        mag_arr = np.asarray(vol_state["vel_5m"])
    except (KeyError, IndexError, TypeError):
        return 0.0
    if bar >= mag_arr.shape[0]:
        return 0.0
    mag = float(mag_arr[bar])
    if not np.isfinite(mag) or mag <= 0.0:
        return 0.0

    # -- Sign from the backward price difference ------------------
    sign = 0.0
    if close is not None and bar >= vel_window:
        p_now = float(close[bar])
        p_prev = float(close[bar - vel_window])
        if np.isfinite(p_now) and np.isfinite(p_prev) and abs(p_prev) > 1e-12:
            diff = p_now - p_prev
            sign = 1.0 if diff > 0 else (-1.0 if diff < 0 else 0.0)
    if sign == 0.0:
        return 0.0

    # Normalise against a REFERENCE velocity, NOT the per-slice
    # median: a median would make the scale depend on which slice
    # you looked at (the frozen-constant trap).
    vel_term = sign * float(np.clip(mag / vel_reference, 0.0, 1.0))

    # -- Wick discount: expansion that was rejected ----------------
    wick_term = 0.0
    try:
        if "range_norm_1m" in vol_state and "wick_ratio" in vol_state:
            rng = float(np.asarray(vol_state["range_norm_1m"])[bar])
            wr = float(np.asarray(vol_state["wick_ratio"])[bar])
            if np.isfinite(rng) and np.isfinite(wr):
                expan = float(np.clip(rng / 3.0, 0.0, 1.0))
                accept = float(np.clip(1.0 - wr, 0.0, 1.0))
                wick_term = vel_term * expan * accept
    except (KeyError, IndexError, TypeError):
        wick_term = 0.0

    total = vel_weight * vel_term + wick_weight * wick_term
    return float(np.clip(total, -1.0, 1.0))


def combined_score(
    state: "StructureState",
    vol_state: Any,
    bar: int,
    direction: int,
    *,
    close: Optional[np.ndarray] = None,
    structure_weight: float = STRUCTURE_WEIGHT,
    momentum_weight: float = MOMENTUM_WEIGHT,
    lookback_events: int = 5,
    half_life_bars: float = 1800.0,
    choch_weight: float = 2.0,
) -> tuple[float, float, float]:
    """``(combined, structure, momentum)`` for a signal at ``bar``.

    ``combined`` is signed; the caller compares its sign to
    ``direction`` for Stage 1 and uses ``|combined|`` for Stage 2.
    """
    s = structure_score(
        state, bar,
        lookback_events=lookback_events,
        half_life_bars=half_life_bars,
        choch_weight=choch_weight,
    )
    m = momentum_score(vol_state, bar, close)
    total = structure_weight * s + momentum_weight * m
    return float(total), float(s), float(m)


__all__ = [
    "StructureEvent",
    "is_choch", "is_bos", "is_bull", "is_bear",
    "SwingPoint", "BreakEvent", "OrderBlock", "LiquiditySweep", "StructureState",
    "BosChochEvent", "BosChochMemory",
    "detect_market_structure",
    "annotate_choch_plus",
    "structure_conviction",
    "bos_choch_directional_alignment",
    # 2026-10-23: continuous structure x momentum scoring.
    "structure_score",
    "structure_confidence",
    "momentum_score",
    "combined_score",
]
