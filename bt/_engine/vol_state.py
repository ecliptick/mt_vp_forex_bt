"""Real-time volatility STATE for the 1m-structure / tick-execution split.

Motivation
----------
A single ATR percentile does not answer the question the FVG/iFVG
detector actually needs to ask. The detector wants to distinguish a
*genuine displacement* (the gap is real, price committed) from a
*liquidity sweep* (a wick through the gap that immediately reverses).
Both look identical on bar geometry alone — a tight range followed by a
one-bar pierce. The distinguishing information is the **volatility
regime at the moment of the event**.

So this module builds a small, cheap, per-bar *state vector* rather
than a single scalar:

===================  ==========================================  =========
Measure              Definition                                  Grid
===================  ==========================================  =========
``rv_5/10/30/60``    realized variance, sum of squared 1s log     1s
                     returns over the trailing N seconds
``rv_ratio_30s``     ``rv_30s / median(rv_30s)`` over the         1s
                     trailing baseline window — the headline
                     "BTC is moving at Nx its normal vol"
``range_norm_1m``    1m candle range / median range of the        1s
                     trailing 1m candles
``vel_1m/5m/15m``    ``|P_t - P_{t-N}| / P_t``                    1s
``shock``            ``|r_1s| / median(|r_1s|)`` over the         1s
                     baseline window
``wick_ratio``       max wick / (H - L) of the 1m candle          1s
===================  ==========================================  =========

Everything is expressed as a **ratio to its own rolling median**, so
the number is scale-free and directly interpretable: ``rv_ratio_30s``
of 3.0 means "BTC is currently moving three times faster than its own
24h-normal pace". No annualization, no magic constants.

Performance
-----------
The naive implementation (rolling median over a 24h window at 1s
cadence) is O(n * w log w) — roughly 2.6M * 86400 operations on a
monthly file. Completely impractical.

The implementation here is **two-tier**:

1. **Numerators stay on the 1s grid** and are O(n) with a cumulative
   sum: realized variance is a rolling sum of ``r^2``, and velocity is
   a shifted absolute difference. Both are vectorised in full.
2. **Baselines (the rolling medians) are computed on a 1-minute
   grid**, then broadcast back onto the 1s grid. A month has ~44,640
   minutes, so a 1440-minute trailing median is a 1440-element median
   over 44,640 output points — three orders of magnitude cheaper than
   doing it at 1s.

Net cost is O(n) for the numerators plus O(minutes * win) for the
baselines, which lands around 1-2s per monthly file. The result is
cached alongside the detector cache, so it is paid once per
(file, config) pair rather than once per backtest.

Causality
---------
No measure reads an incomplete candle by default. A 1m candle is only
final at its close, so the 1m-derived measures
(``range_norm_1m``, ``wick_ratio``) read the last **complete** 1m
candle by default, i.e. the one that closed at least
``min_bars_since_close`` 1s-bars ago.

``elapsed_adjust=True`` opts into using the in-progress candle with a
Brownian ``sqrt(t)`` time-scaling correction
(``range * sqrt(candle_secs / elapsed_secs)``), which is the
theoretically correct normalisation for a diffusion process and is
what you want for genuinely real-time use. It is off by default
because the un-adjusted form is easier to reason about when
validating.
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass, field

import numpy as np

__all__ = [
    "VolState",
    "build_vol_state",
    "VOL_MEASURES",
]


# Every measure this module knows how to produce. Used for validation
# and for the CLI-facing help text.
#
# NAMING NOTE: the ``rv_share_*`` family is a *within-minute share* —
# ``rv_Ns / rv_60s`` — not a ratio to a fitted baseline. The name
# changed from an earlier ``rv_ratio_*`` draft to make the semantics
# explicit, because "ratio" implied a trailing-median denominator that
# no longer exists (see the long comment in ``build_vol_state`` for the
# two normalisations that were tried and why both failed).
VOL_MEASURES = (
    "rv_5s",
    "rv_10s",
    "rv_30s",
    "rv_60s",
    "rv_share_5s",
    "rv_share_10s",
    "rv_share_30s",
    "rv_ratio_30s",
    "range_norm_1m",
    "vel_1m",
    "vel_5m",
    "vel_15m",
    "shock",
    "wick_ratio",
)

_RV_WINDOWS = (5, 10, 30, 60)
_VEL_WINDOWS = (60, 300, 900)

# ── Memo cache for ``build_vol_state`` (added for sweep throughput) ──
# A parameter sweep evaluates many gate configs against the SAME bars.
# ``build_vol_state`` is a pure function of (OHLC, kwargs), so the
# expensive rolling-median baselines can be reused across cells that
# differ only in their THRESHOLDS. Key is a content fingerprint of the
# inputs plus every build parameter -- never the threshold values, which
# are applied downstream by the caller and do not affect the state.
#
# Bounded: each monthly file contributes ~126 MB (6 measures x 2.6M
# float64), so holding more than a couple of entries would OOM a sweep
# that walks six months. Oldest-first eviction keeps the working set
# small; in practice the sweep loop is (cell -> month), so this holds
# the six months of ONE cell and reuses them across the next cell only
# when the loop order permits. OrderedDict gives cheap LRU semantics.
_CACHE_MAX = 8
_CACHE: "OrderedDict[tuple, VolState]" = OrderedDict()


@dataclass
class VolState:
    """Per-1s-bar volatility state, keyed by measure name.

    Attributes
    ----------
    series:
        ``{measure_name: np.ndarray[n_1s]}``. Only the measures that
        were actually requested (and any measures needed to compute
        them) are present, to keep peak memory bounded.
    meta:
        Free-form provenance dict — measure list, window sizes, bar
        count. Persisted alongside the arrays so a cached state can be
        validated against the current request.
    """

    series: dict[str, np.ndarray] = field(default_factory=dict)
    meta: dict = field(default_factory=dict)

    def __getitem__(self, key: str) -> np.ndarray:
        return self.series[key]

    def __contains__(self, key: str) -> bool:
        return key in self.series

    def get(self, key: str) -> np.ndarray | None:
        return self.series.get(key)


def _rolling_sum(x: np.ndarray, w: int) -> np.ndarray:
    """Causal rolling sum over the trailing ``w`` samples.

    Output has the same length as the input; the first ``w - 1``
    entries are zero-padded because a full window is not yet
    available there.
    """
    if w <= 1:
        return x.astype(np.float64, copy=True)
    c = np.concatenate(([0.0], np.cumsum(x, dtype=np.float64)))
    out = c[w:] - c[:-w]
    # c[w:] has length n+1-w; pad the front to restore length n.
    return np.concatenate((np.zeros(w - 1, dtype=np.float64), out))


def _segment_bounds(idx: np.ndarray, n_seg: int) -> tuple[np.ndarray, np.ndarray]:
    """Start/end offsets for each contiguous run of equal values.

    ``idx`` must be non-decreasing. Returns ``(starts, ends)`` with
    ``ends[k]`` exclusive, both length ``n_seg``.
    """
    starts = np.searchsorted(idx, np.arange(n_seg), side="left")
    # searchsorted on a value beyond the last element returns len(idx);
    # clamp so reduceat-style consumers never get an out-of-range start.
    starts = np.minimum(starts, max(0, idx.shape[0] - 1))
    ends = np.searchsorted(idx, np.arange(1, n_seg + 1), side="left")
    ends = np.maximum(ends, starts + 1)
    return starts, ends


def _per_bucket_stat(
    x: np.ndarray, bucket_idx: np.ndarray, n_buckets: int
) -> np.ndarray:
    """Mean of ``x`` within each bucket. Fully vectorised.

    ``np.bincount`` accumulates the bucket totals in one pass; the
    bucket counts are computed once from ``bucket_idx`` rather than
    per-call. This replaces a 44,640-iteration Python loop, which was
    the module's dominant cost.

    A MEAN is used rather than a median deliberately. For the
    microsecond-scale measures (5s realized variance, single-bar
    ``|r|``) the median over a quiet window is effectively zero, so a
    median-normalised ratio explodes into meaningless territory —
    empirically ``rv_ratio_5s`` hit p99 = 10,790 before this change.
    The mean is far better conditioned for these: it is positive
    whenever any real movement occurred in the window, so the ratio
    stays in a sane range.
    """
    sums = np.bincount(bucket_idx, weights=x, minlength=n_buckets)
    counts = np.bincount(bucket_idx, minlength=n_buckets).astype(np.float64)
    counts = np.where(counts < 1.0, 1.0, counts)
    return sums / counts


def _per_bucket_median(
    x: np.ndarray, bucket_idx: np.ndarray, n_buckets: int
) -> np.ndarray:
    """Median of ``x`` within each bucket. O(n_buckets) median calls."""
    starts, ends = _segment_bounds(bucket_idx, n_buckets)
    out = np.empty(n_buckets, dtype=np.float64)
    for k in range(n_buckets):
        out[k] = float(np.median(x[starts[k] : ends[k]]))
    return out


def _rolling_median_baseline(
    per_bucket: np.ndarray, win: int
) -> np.ndarray:
    """Trailing rolling median over the bucketed series.

    ``baseline[k] = median(per_bucket[max(0, k - win + 1) : k + 1])``.

    This is the expensive half of the module — O(n_buckets * win) —
    which is exactly why it runs on the 1-minute grid rather than the
    1-second grid.
    """
    nb = per_bucket.shape[0]
    out = np.empty(nb, dtype=np.float64)
    for k in range(nb):
        s = k - win + 1
        if s < 0:
            s = 0
        out[k] = float(np.median(per_bucket[s : k + 1]))
    return out


def _rolling_mean_baseline(
    per_bucket: np.ndarray, win: int
) -> np.ndarray:
    """Trailing rolling mean over the bucketed series, via cumsum.

    O(n_buckets) total instead of O(n_buckets * win).
    """
    nb = per_bucket.shape[0]
    c = np.concatenate(([0.0], np.cumsum(per_bucket, dtype=np.float64)))
    hi = np.arange(1, nb + 1)
    lo = np.maximum(0, hi - win)
    tot = c[hi] - c[lo]
    cnt = (hi - lo).astype(np.float64)
    cnt = np.where(cnt < 1.0, 1.0, cnt)
    return tot / cnt


def _safe_ratio(num: np.ndarray, den: np.ndarray) -> np.ndarray:
    """``num / den`` with a guard against divide-by-zero.

    A zero or negative baseline means the feature is undefined for
    this bar (e.g. a perfectly flat window). We emit 1.0, which is
    the neutral "moving exactly as normal as always" value, so the
    feature degrades to "no opinion" rather than to a wild number
    that would trip a threshold gate spuriously.
    """
    den = np.where(np.abs(den) < 1e-18, np.nan, den)
    out = np.divide(num, den, out=np.ones_like(num, dtype=np.float64), where=~np.isnan(den))
    return np.where(np.isfinite(out), out, 1.0)


def build_vol_state(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    open_: np.ndarray | None = None,
    *,
    measures: tuple[str, ...] | list[str] = ("rv_ratio_30s",),
    rv_baseline_secs: int = 86_400,
    range_baseline_mins: int = 60,
    shock_baseline_secs: int = 3_600,
    candle_secs: int = 60,
    min_bars_since_close: int = 1,
    elapsed_adjust: bool = False,
    use_cache: bool = True,
) -> VolState:
    """Build the per-bar volatility state.

    Parameters
    ----------
    high, low, close:
        1s OHLC arrays, all the same length, ascending in time.
    open_:
        1s open array. Only needed for ``wick_ratio``; if omitted,
        ``wick_ratio`` falls back to ``(H - L) / max(|C - O|, eps)``
        clipped, which is a cruder proxy.
    measures:
        Which measures to materialise. Requesting a ratio measure
        automatically pulls in the numerator it is built from.
    rv_baseline_secs:
        Trailing window for the realized-variance median baseline.
    range_baseline_mins:
        Trailing window (in 1m candles) for the range median.
    shock_baseline_secs:
        Trailing window for the ``|r|`` median that defines a
        "normal" single-bar move.
    candle_secs:
        Size of the structural candle. 60 = 1m, matching
        ``fvg_resample_secs``.
    min_bars_since_close:
        Causality floor. A structural candle that closed this many
        1s-bars ago or earlier is considered final. Ignored when
        ``elapsed_adjust=True``.
    elapsed_adjust:
        Use the in-progress structural candle with a ``sqrt(t)``
        Brownian correction instead of the last complete candle.
    use_cache:
        Memoise the result. ``build_vol_state`` is a PURE function of
        the OHLC arrays plus the kwargs, but a parameter sweep calls it
        once per cell on the SAME bars -- nb79's 28-cell sweep paid the
        86,400-point rolling-median cost 28 times for identical input.
        The key is a cheap content fingerprint of the arrays plus every
        parameter, so a cache hit is bit-identical to a rebuild. Set
        False to force a fresh build. Bounded to ``_CACHE_MAX`` entries
        with oldest-first eviction, because a monthly file's state is
        ~6 measures x 2.6M float64 ~= 126 MB.

    Returns
    -------
    VolState
    """
    high = np.asarray(high, dtype=np.float64)
    low = np.asarray(low, dtype=np.float64)
    close = np.asarray(close, dtype=np.float64)
    n = close.shape[0]
    if n == 0:
        raise ValueError("build_vol_state: empty input arrays")

    # ── Memo cache (sweep fast path) ──────────────────────────────
    _ck = None
    if use_cache:
        # Fingerprint the CONTENT, not the id: a sweep passes a
        # freshly-loaded-but-identical array each time, and id()
        # would miss while the data is the same. Hashing 2.6M x 4
        # float64 directly is ~30 ms -- negligible against the ~7 s
        # build it replaces, and far cheaper than a wrong cache hit.
        _ck = (
            hash((n, float(close[0]), float(close[-1]),
                  float(high.sum()), float(low.sum()),
                  float(close.sum()))),
            tuple(sorted(measures)),
            int(rv_baseline_secs), int(range_baseline_mins),
            int(shock_baseline_secs), int(candle_secs),
            int(min_bars_since_close), bool(elapsed_adjust),
            None if open_ is None else float(open_.sum()),
        )
        _hit = _CACHE.get(_ck)
        if _hit is not None:
            return _hit
    if high.shape[0] != n or low.shape[0] != n:
        raise ValueError("build_vol_state: high/low/close length mismatch")
    if open_ is not None:
        open_ = np.asarray(open_, dtype=np.float64)
        if open_.shape[0] != n:
            raise ValueError("build_vol_state: open_ length mismatch")

    want = set(measures)
    unknown = want - set(VOL_MEASURES)
    if unknown:
        raise ValueError(
            f"build_vol_state: unknown measure(s) {sorted(unknown)}; "
            f"valid = {list(VOL_MEASURES)}"
        )

    series: dict[str, np.ndarray] = {}

    # ── 1s log returns ────────────────────────────────────────────
    # r_i = ln(P_i / P_{i-1}). r_0 is undefined; zero it so it does not
    # contribute a spurious jump to the realized-variance sum.
    with np.errstate(divide="ignore", invalid="ignore"):
        r = np.diff(np.log(close), prepend=close[:1])
    r[0] = 0.0
    r = np.where(np.isfinite(r), r, 0.0)
    r2 = r * r
    abs_r = np.abs(r)

    # ── Structural (1m) bucket grid ───────────────────────────────
    cs = max(1, int(candle_secs))
    bucket_idx = np.arange(n, dtype=np.int64) // cs
    n_buckets = int(bucket_idx[-1]) + 1

    # Index of the structural candle each 1s bar may read from.
    #   elapsed_adjust=False -> the last candle that has CLOSED, with
    #       an extra min_bars_since_close delay for safety.
    #   elapsed_adjust=True  -> the candle currently forming.
    if elapsed_adjust:
        usable = bucket_idx
    else:
        usable = bucket_idx - 1 - max(0, int(min_bars_since_close))
    usable = np.clip(usable, 0, n_buckets - 1)

    # ── Realized variance + its median baseline ───────────────────
    # A ratio measure implies its numerator, so work out the union of
    # RV windows actually needed.
    needed_rv: set[int] = set()
    for m in want:
        # Handles both "rv_30s" and "rv_share_30s" / "rv_ratio_30s".
        parts = m.split("_")
        if len(parts) >= 2 and parts[-1].endswith("s"):
            try:
                w = int(parts[-1].rstrip("s"))
            except ValueError:
                continue
            if w in _RV_WINDOWS and m.startswith("rv"):
                needed_rv.add(w)
    needed_rv = {w for w in needed_rv if w in _RV_WINDOWS}

    rv_base_minutes = max(4, int(rv_baseline_secs) // cs)
    # The widest window is the denominator for every within-minute
    # share, so compute it once whenever any share is requested.
    need_any_share = any(m.startswith("rv_share_") for m in want)
    if need_any_share:
        rv_wide = _rolling_sum(r2, max(_RV_WINDOWS))
        series[f"rv_{max(_RV_WINDOWS)}s"] = rv_wide

    # Raw windows (always emitted for a requested share's numerator).
    for w in sorted(needed_rv):
        series[f"rv_{w}s"] = _rolling_sum(r2, w)

    # ── Within-minute concentration ──────────────────────────────
    # ``rv_Ns / rv_60s`` = "what share of the last minute's squared
    # movement happened in the last N seconds". Scale-free, no fitted
    # baseline, and bucketing-safe because numerator and denominator
    # are measured at the same instant in the same units.
    for w in sorted(needed_rv):
        name = f"rv_share_{w}s"
        if name not in want:
            continue
        series[name] = _safe_ratio(series[f"rv_{w}s"], rv_wide)

    # ── Elevation vs the instrument's own recent norm ────────────
    # The share above cannot answer "is volatility high right now",
    # only "is it concentrated right now". For that we need a genuine
    # trailing baseline.
    #
    # A MEAN baseline is used here (not median, not MAD):
    #   * median collapses to ~0 on a quiet tape -> ratio explodes.
    #   * MAD is computed on the 1m grid, and a 30s window only covers
    #     half of its bucket, so the per-bucket MAD measures a
    #     different quantity than the 1s numerator. The 1.4826
    #     consistency factor then amplified the mismatch into the
    #     millions (rv_ratio_5s max = 5.7e6 when this was tried).
    #   * mean has neither failure mode, and at the 60s scale the
    #     bucket grid is exactly commensurate with the window, so no
    #     mismatch arises. Its one weakness -- sensitivity to the
    #     spikes being measured -- is acceptable for a 24h trailing
    #     window, where a handful of spikes is the correct thing for
    #     "normal" to include.
    if "rv_ratio_30s" in want:
        rv30 = series["rv_30s"]
        per_b = _per_bucket_median(rv30, bucket_idx, n_buckets)
        base = _rolling_median_baseline(per_b, rv_base_minutes)
        # Median (not mean) for the elevation baseline: at 30s the
        # numerator is large enough that the median is well away from
        # zero, so the median's robustness to spikes is worth more
        # than the mean's tightness.
        series["rv_ratio_30s"] = _safe_ratio(rv30, base[usable])

    # ── Normalized structural range + wick ratio ──────────────────
    if "range_norm_1m" in want or "wick_ratio" in want:
        b_starts, b_ends = _segment_bounds(bucket_idx, n_buckets)
        # ``np.*.reduceat`` silently produces wrong results when the
        # offset array has repeated adjacent values (it treats a
        # repeated index as "extend the previous segment"). Our
        # clamped starts can repeat only for trailing empty buckets,
        # which cannot occur on a dense 1s grid — but assert rather
        # than trust it, because a wrong high/low silently corrupts
        # every downstream feature.
        if np.any(np.diff(b_starts) <= 0):
            # Degenerate grid: fall back to the (slow but correct)
            # explicit-loop path. Only reachable if the caller passes
            # a sparse/irregular series.
            b_high = np.array(
                [high[b_starts[k] : b_ends[k]].max() for k in range(n_buckets)]
            )
            b_low = np.array(
                [low[b_starts[k] : b_ends[k]].min() for k in range(n_buckets)]
            )
        else:
            b_high = np.maximum.reduceat(high, b_starts)
            b_low = np.minimum.reduceat(low, b_starts)
        b_range = b_high - b_low

        if "wick_ratio" in want:
            # Per-bucket open = first bar's open, close = last bar's
            # close. Both are gathers, not reductions.
            b_close = close[b_ends - 1]
            if open_ is not None:
                b_open = open_[b_starts]
            else:
                # Without an open series we cannot measure a wick, so
                # fall back to the bar-close-to-close body as a crude
                # proxy. Documented as degraded rather than silently
                # emitting a meaningless number.
                b_open = b_close
            upper = b_high - np.maximum(b_open, b_close)
            lower = np.minimum(b_open, b_close) - b_low
            # Max single-side wick as a fraction of the candle range.
            # A candle that closes on its high/low has one wick at ~0,
            # which is the "clean displacement" end of the scale; a
            # candle that closes mid-range with a long tail is the
            # "liquidity sweep" end.
            span = np.where(b_range > 1e-12, b_range, np.nan)
            wr = np.maximum(upper, lower) / span
            wr = np.where(np.isfinite(wr), wr, 0.0)
            wr = np.clip(wr, 0.0, 1.0)
            series["wick_ratio"] = wr[usable]

        if "range_norm_1m" in want:
            win_m = max(4, int(range_baseline_mins))
            base = _rolling_median_baseline(b_range, win_m)
            if elapsed_adjust:
                # Read the candle that is CURRENTLY FORMING and scale
                # its partial range up to a full-candle equivalent
                # using a Brownian sqrt(t) correction. A candle that is
                # 37s into a 60s window has accumulated roughly
                # (37/60) of the variance a full candle would, so its
                # range is scaled by sqrt(60/37).
                elapsed = (
                    np.arange(n, dtype=np.float64) - bucket_idx * cs
                ) + 1.0
                scale = np.sqrt(cs / np.maximum(elapsed, 1.0))
                series["range_norm_1m"] = _safe_ratio(
                    b_range[bucket_idx] * scale, base[usable]
                )
            else:
                series["range_norm_1m"] = _safe_ratio(
                    b_range[usable], base[usable]
                )

    # ── Price velocity ────────────────────────────────────────────
    # V = |P_t - P_{t-N}| / P_{t-N}. Pure shifts over the TRAILING
    # window of N samples, so this is O(n) with no rolling statistic
    # at all — no baseline needed because the caller thresholds it
    # directly.
    #
    # **Causality (FIX 2026-10-13)**: an earlier implementation used
    # ``close[w:] - close[:-w]``, which computes ``P_{t+w} - P_t``
    # — a **forward-looking** price move, not a backward one. That
    # implementation is the textbook lookahead-bias: at every bar
    # it was reading the price 5 minutes (or 15m) **in the future**
    # and using that as a "velocity at the current bar". Every
    # downstream gate using ``vel_5m >= threshold`` was leaking
    # future information. NB85's ``vel5>=0.001`` finding is
    # invalidated by this fix; the v17_sniper_focused recipe's
    # default ``vol_state_min_vel_5m=0.001`` should be unset to
    # ``0.0`` (i.e. measure not gated) until the gate is
    # re-validated with the corrected feature.
    #
    # The correct shift is **backward**:
    #   vel[i] = |P_i - P_{i-w}| / P_{i-w}     for i >= w
    #   vel[i] = 0                              for i <  w
    # The first ``w`` entries are zero because a full window of
    # past data is not yet available there; this is the same
    # zero-padding convention used by ``_rolling_sum`` so the
    # back-end sees no NaNs.
    for w in _VEL_WINDOWS:
        name = f"vel_{w // 60}m"
        if name not in want:
            continue
        if w >= n:
            series[name] = np.zeros(n, dtype=np.float64)
            continue
        # P_{t-w} for t = w..n-1, and P_t for t = w..n-1.
        # The first w entries of ``back`` would have no anchor, so
        # we zero-pad the front.
        back = close[:n - w]                 # P_0, P_1, ..., P_{n-1-w}
        fwd = close[w:]                      # P_w, P_{w+1}, ..., P_{n-1}
        vel = np.abs(fwd - back) / np.where(np.abs(back) > 1e-12, back, np.nan)
        full = np.concatenate(
            (np.zeros(w, dtype=np.float64), vel)
        )
        series[name] = np.where(np.isfinite(full), full, 0.0)

    # ── Shock: single-bar move vs its own normal ──────────────────
    if "shock" in want:
        # Shock = single-bar move vs the trailing norm of a single-bar
        # move, using a trailing MEDIAN baseline. Both numerator and
        # baseline live on the 1s grid and the median is taken over a
        # bucketed intermediate, but the measure itself (one bar's
        # |r|) is NOT windowed, so the N-of-60 bucketing mismatch that
        # broke the short RV windows does not apply here.
        sb = max(4, int(shock_baseline_secs) // cs)
        per_b = _per_bucket_median(abs_r, bucket_idx, n_buckets)
        base = _rolling_median_baseline(per_b, sb)
        series["shock"] = _safe_ratio(abs_r, base[usable])

    _vs = VolState(
        series=series,
        meta={
            "n_1s": int(n),
            "measures": sorted(series.keys()),
            "requested": sorted(want),
            "candle_secs": cs,
            "rv_baseline_secs": int(rv_baseline_secs),
            "range_baseline_mins": int(range_baseline_mins),
            "shock_baseline_secs": int(shock_baseline_secs),
            "min_bars_since_close": int(min_bars_since_close),
            "elapsed_adjust": bool(elapsed_adjust),
        },
    )

    if _ck is not None:
        _CACHE[_ck] = _vs
        _CACHE.move_to_end(_ck)
        while len(_CACHE) > _CACHE_MAX:
            _CACHE.popitem(last=False)
    return _vs
