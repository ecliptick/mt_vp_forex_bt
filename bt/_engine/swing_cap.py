"""Daily-swing TP cap — limits `tp_zone_mult * zone_width` to a
multiple of recent BTC peak-to-trough daily swing.

Why this exists
===============

The canonical sniper uses ``tp_zone_mult=60``: a $50 zone gets
TP = $3000; a $200 zone gets TP = $12,000. BTC's p90 daily
peak-to-trough swing on the 6mo corpus is ~$6,700 (8% of price).
A $12,000 TP is **179% of BTC's p90 daily swing** — structurally
unfillable in a 1-2 day hold.

Without a cap, the wider zones produce trades that ride for
weeks waiting for an unreachable TP, then exit at EOD with a
"win" that's actually a fee-bite artifact. The cap converts
this into a sensible per-day TP that scales with recent
volatility.

Live-deployable design
======================

Three constraints drove the design:

1. **Cheap at fill-time**. The cap is queried at signal-submit
   time on every trade, so the lookup must be O(1) per trade.
2. **Live-deployable on a VPS**. Cannot recompute from raw ticks
   every minute — the VPS doesn't have 18 GB of historical ticks.
3. **Updates throughout the day**. The cap must reflect today's
   intra-day excursion, not just yesterday's close.

The solution: a **rolling high/low window** stored in a flat
numpy array. The backtest code populates this from cached 1s
bars (one-time cost); the live engine updates it from streaming
1s bars as they arrive.

Public API
==========

* ``DailySwingCap`` — dataclass holding the rolling window state
  plus a query method ``cap_for(entry_ts_ns, mult)``.
* ``build_daily_swing(bars, cap_pct)`` — precompute the cap from
  cached 1s bars. Returns a ``DailySwingCap`` ready for use.
* ``update_swing(cap, t_ns, price)`` — live-deployable update
  path: as each new bar arrives, advance the rolling high/low.

The cap is wired into ``notebooks/nb71_volume_profile_sniper.py``
and ``src/backtest/ict_backtest.py`` as an *optional* knob
(default off). When ``tp_zone_swing_cap_pct > 0``, the cap is
applied; when ``tp_zone_swing_cap_pct == 0`` (the default), the
behaviour is bit-identical to the prior canonical.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import numpy as np
import pandas as pd

from bt._engine.timebase import assert_ns, to_ns_arr


# ─────────────────────────────────────────────────────────────────
# Core dataclass
# ─────────────────────────────────────────────────────────────────


@dataclass
class DailySwingCap:
    """Rolling-window daily-swing cap.

    Maintains a buffer of (timestamp_ns, high, low) triples for
    the last ``lookback_secs`` seconds. ``cap_for(t_ns)`` returns
    the rolling peak-to-trough swing — which is the absolute
    maximum distance price could have moved in either direction
    over that window.

    The cap is applied as:
        effective_tp = min(zone_mult * zone_width,
                           cap_pct * rolling_peak_to_trough)

    Args:
        lookback_secs:  Window length for the rolling high/low.
                        Default 24h captures one full UTC day.
        cap_pct:        Multiplier on the rolling swing (e.g. 0.5
                        means TP cap is half the recent swing).
                        0.0 disables the cap.
        buffer_t_ns:    Ring buffer of timestamps (int64 ns).
        buffer_high:    Ring buffer of per-bar highs.
        buffer_low:     Ring buffer of per-bar lows.
        head:           Next write index into the ring buffer.
        count:          Number of populated entries.
    """

    lookback_secs: int = 86_400  # 24h default
    cap_pct: float = 0.0  # 0 = disabled

    # Ring-buffer state (populated by ``build_daily_swing`` or
    # streaming ``update_swing`` calls).
    buffer_t_ns: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.int64))
    buffer_high: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    buffer_low: np.ndarray = field(default_factory=lambda: np.zeros(0, dtype=np.float64))
    head: int = 0
    count: int = 0

    @property
    def enabled(self) -> bool:
        return self.cap_pct > 0.0 and self.count > 0

    def cap_for(self, t_ns: int) -> float:
        """Rolling peak-to-trough swing at time ``t_ns`` × ``cap_pct``.

        Returns the absolute TP cap in USD. If the cap is disabled
        (``cap_pct == 0``) or no data has been loaded, returns
        ``np.inf`` (no cap).

        Performance: **O(log n + w)** where ``w`` is the number of
        entries inside the lookback window (~60 for a 24h window at
        the 24-min sampling cadence). The window is located with
        ``np.searchsorted`` on the chronologically sorted timestamps,
        then only that slice is scanned for the max/min.

        This used to be O(buffer) per call: it built a full-length
        boolean mask over every entry ever loaded. On XAUUSD_S1_1y
        the buffer holds 37,835 entries and the engine calls
        ``cap_for`` once per signal (~264k signals) — roughly 10
        billion element-ops, which dominated the bar loop and made
        the run decelerate from 26,459 bars/s to 1,597 bars/s.
        """
        if not self.enabled:
            return float("inf")
        cutoff = t_ns - int(self.lookback_secs * 1e9)
        if self.count < self.buffer_t_ns.size:
            # Ring not yet full — slots [0, head) are chronological.
            ts = self.buffer_t_ns[: self.head]
            hs = self.buffer_high[: self.head]
            ls = self.buffer_low[: self.head]
        else:
            # Ring is full — unwrap into chronological order.
            tail = np.arange(self.head, self.head + self.count) % self.count
            ts = self.buffer_t_ns[tail]
            hs = self.buffer_high[tail]
            ls = self.buffer_low[tail]
        # Filter to entries within the lookback window.
        #
        # BOTH bounds are required. The lower bound alone leaks
        # FORWARD: without ``ts <= t_ns`` the window runs from
        # ``t_ns - lookback`` all the way to the END of the buffer,
        # which on a multi-month file is months of future bars.
        #
        # This is why the cap looked correct on a 1-week file and
        # wildly wrong on 1 year. Measured on XAUUSD_S1_1y:
        #   probe at file midpoint -> 17,833 entries passed the
        #   one-sided mask (spanning 435 days) where the correct
        #   two-sided window holds 58; ``cap_for`` returned
        #   $1,733.62 instead of $92.66 (18.7x). Early-file and
        #   end-of-file probes are unaffected because the forward
        # leak is bounded by the file edges — the bug only shows
        # up once there IS a future.
        #
        # ``ts`` is chronologically sorted, so the window is a
        # CONTIGUOUS SLICE located by binary search. The previous
        # implementation built a full-length boolean mask over every
        # entry ever loaded — O(buffer) per call. With 37,835 buffer
        # entries and ~264k signals on the 1y file that is ~10 billion
        # element-ops, and it dominated the bar loop (the run
        # decelerated from 26,459 bars/s to 1,597 bars/s).
        lo = int(np.searchsorted(ts, cutoff, side="left"))
        hi = int(np.searchsorted(ts, t_ns, side="right"))
        if hi <= lo:
            return float("inf")
        win_high = float(hs[lo:hi].max())
        win_low = float(ls[lo:hi].min())
        swing = win_high - win_low
        return swing * float(self.cap_pct)


# ─────────────────────────────────────────────────────────────────
# Build from cached 1s bars (backtest path)
# ─────────────────────────────────────────────────────────────────


def build_daily_swing(
    bars: pd.DataFrame,
    *,
    lookback_secs: int = 86_400,
    cap_pct: float = 0.5,
) -> DailySwingCap:
    """Build a ``DailySwingCap`` from 1s OHLCV bars.

    Resamples to a 60-bar-per-lookback cadence (one record every
    ``lookback_secs / 60`` seconds, default ~24 minutes) so the
    ring buffer stays small (~60 entries for a 24h lookback).
    """
    times_ns = to_ns_arr(bars, "time")
    # HARD GUARD: cap_for() subtracts lookback_secs in NANOSECONDS.
    # A millisecond timestamp here would make the cutoff ~10^6x too
    # large, so no bar ever falls inside the window and the cap
    # silently returns inf (disabled) for every query.
    assert_ns(times_ns, "swing_cap times")
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)

    # Subsample so the ring is ~60 records per lookback window.
    target_records = 60
    cadence_secs = max(1, lookback_secs // target_records)
    cadence_ns = int(cadence_secs * 1e9)

    if len(times_ns) > 0:
        first_t = int(times_ns[0])
        # Bucket index = floor((t - first_t) / cadence_ns)
        bucket = (times_ns - first_t) // cadence_ns
        # Per-bucket high/low.
        df = pd.DataFrame({"bucket": bucket, "high": high, "low": low,
                           "t": times_ns})
        agg = (
            df.groupby("bucket", sort=True)
            .agg(high=("high", "max"),
                 low=("low", "min"),
                 t=("t", "min"))
            .reset_index()
        )
        sub_t = agg["t"].to_numpy(dtype=np.int64)
        sub_h = agg["high"].to_numpy(dtype=np.float64)
        sub_l = agg["low"].to_numpy(dtype=np.float64)
    else:
        sub_t = np.zeros(0, dtype=np.int64)
        sub_h = np.zeros(0, dtype=np.float64)
        sub_l = np.zeros(0, dtype=np.float64)

    cap = DailySwingCap(
        lookback_secs=lookback_secs,
        cap_pct=cap_pct,
        buffer_t_ns=sub_t,
        buffer_high=sub_h,
        buffer_low=sub_l,
        head=len(sub_t),
        count=len(sub_t),
    )
    return cap


def save_daily_swing(cap: DailySwingCap, cache_dir: Path) -> None:
    """Persist a DailySwingCap to disk as npz + meta json."""
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    np.savez(
        cache_dir / "daily_swing.npz",
        buffer_t_ns=cap.buffer_t_ns,
        buffer_high=cap.buffer_high,
        buffer_low=cap.buffer_low,
    )
    (cache_dir / "daily_swing_meta.json").write_text(
        f'{{"lookback_secs": {cap.lookback_secs}, "cap_pct": {cap.cap_pct}, '
        f'"n_records": {cap.count}}}\n'
    )


def load_daily_swing(cache_dir: Path) -> Optional[DailySwingCap]:
    """Load a DailySwingCap from disk; ``None`` if not cached."""
    cache_dir = Path(cache_dir)
    npz_path = cache_dir / "daily_swing.npz"
    meta_path = cache_dir / "daily_swing_meta.json"
    if not npz_path.exists() or not meta_path.exists():
        return None
    npz = np.load(npz_path)
    import json as _json
    meta = _json.loads(meta_path.read_text())
    return DailySwingCap(
        lookback_secs=int(meta["lookback_secs"]),
        cap_pct=float(meta["cap_pct"]),
        buffer_t_ns=npz["buffer_t_ns"],
        buffer_high=npz["buffer_high"],
        buffer_low=npz["buffer_low"],
        head=int(meta["n_records"]),
        count=int(meta["n_records"]),
    )


# ─────────────────────────────────────────────────────────────────
# Streaming update (live VPS path)
# ─────────────────────────────────────────────────────────────────


def make_live_swing_cap(
    *, lookback_secs: int = 86_400, cap_pct: float = 0.5,
    max_records: int = 4096,
) -> DailySwingCap:
    """Create an empty DailySwingCap ready for streaming updates.

    The live VPS would call this at startup, then call
    ``update_swing`` once per bar (or per minute) as 1s/1m bars
    arrive. The ring buffer holds ``max_records`` entries — enough
    for ~24h at 1-min cadence or ~68 minutes at 1s cadence.
    """
    return DailySwingCap(
        lookback_secs=lookback_secs,
        cap_pct=cap_pct,
        buffer_t_ns=np.zeros(max_records, dtype=np.int64),
        buffer_high=np.zeros(max_records, dtype=np.float64),
        buffer_low=np.zeros(max_records, dtype=np.float64),
        head=0,
        count=0,
    )


def update_swing(
    cap: DailySwingCap, t_ns: int, high: float, low: float,
) -> None:
    """Append one bar to the rolling swing cap.

    O(1) per call. Maintains a ring buffer of length ``max_records``
    (the buffer's allocated size). Once the ring is full, the
    oldest entry is overwritten.

    Args:
        cap:  The DailySwingCap (mutated in place).
        t_ns: Bar timestamp in nanoseconds since epoch.
        high: Bar high price.
        low:  Bar low price.
    """
    if cap.buffer_t_ns.size == 0:
        return
    cap.buffer_t_ns[cap.head] = t_ns
    cap.buffer_high[cap.head] = high
    cap.buffer_low[cap.head] = low
    cap.head = (cap.head + 1) % cap.buffer_t_ns.size
    if cap.count < cap.buffer_t_ns.size:
        cap.count += 1
