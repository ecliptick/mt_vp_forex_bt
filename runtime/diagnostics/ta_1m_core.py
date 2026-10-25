"""1-MINUTE TA CORE — bar builder, TA signals, executor, and the metric.

WHY THIS MODULE EXISTS
======================

The repo's current evidence is about ONE strategy (the v22 SNIPER FVG
recipe) measured on 1s bars. This module answers a different question:

    *Can a SIMPLE, hand-written technical-analysis rule be scored on
    1-MINUTE data, and can the SCORE itself be trusted to mean
    "the direction was right"?*

So there are two deliverables, and the second is the one that matters
most:

1. **A metric** that scores a directional rule on three axes at once:
   *correct direction*, *magnitude of the directional moves*, and *less
   time in market*.
2. **A proof that the metric is directionally correct** — validated
   against GROUND TRUTH (an oracle that is right by construction) and
   against EXCHANGEABLE PLACEBO arms that destroy exactly ONE
   ingredient (the direction) and keep everything else.

THE METRIC
==========

Per leg (a position opened by a rule and closed by the executor), on 1m
bars:

.. code-block:: text

    side      = +1 long / -1 short
    gross_px  = side * (exit_px - entry_px)      # $/oz, signed
    fee_usd   = (entry_px + exit_px) * oz * bps / 10000
    net_usd   = gross_px * oz - fee_usd
    path_px   = total |close-to-close movement| while in the market
    mfe_px    = best favorable excursion while in the market   # signed
    C         = +1 / -1 / 0 by the sign of gross (0 = a FLAT leg)

Four numbers, each with exactly ONE job:

.. code-block:: text

    DAC  = sum(path_px * C) / sum(path_px)     in [-1, +1]
           "correct directionally", WEIGHTED BY THE MAGNITUDE of the
           market movement that happened while the rule was in the
           market. A rule that is right on big moves and wrong on
           small ones scores near zero; a rule that is right on
           everything scores +1; a rule that is wrong on everything
           scores -1.

    DAC_MFE = sum(|mfe_px| * C) / sum(|mfe_px|)   in [-1, +1]
           the same question, but weighted by the size of the FAVORABLE
           move rather than by the market's total activity.

    DREM = sum(gross_usd) / sum(hold_minutes)    [$ per exposure-minute]
           "directional reward per exposure-minute" — the COMPOSITE.
           It rewards MAGNITUDE (the reward is in dollars, so a bigger
           directional move counts more) and it rewards LESS TIME IN
           MARKET (the denominator). It is 0 for a rule with no
           directional skill, + for a good one, - for a consistently
           wrong one.

    TIM  = sum(hold_minutes) / trading_minutes  in [0, 1]
           "time in market" as a fraction of the session that was
           actually tradeable. Reported SEPARATELY because a
           composite that only rewards speed can be gamed by trading
           once.

> **A FLAT LEG IS NOT A LOSS.** ``C = 0`` when ``gross == 0``. The
> first draft of this module scored a zero-P&L leg as a loss, which
> turned a rule that was merely INDISTINGUISHABLE into a rule that
> looked maximally wrong (DAC = -1 on a synthetic zigzag). A metric
> that calls "no edge" "wrong" is not measuring the thing it claims.

The fee test is NOT optional and is carried from the repo's existing
harness: a gross edge that does not cover the measured round trip is
not an edge. So the scorecard always reports ``gross_per_leg``,
``fee_per_leg`` and their ratio.

THE DRIFT / EXCESS DECOMPOSITION IS CARRIED OVER
===============================================

A "correct direction" score is worthless if it just measures market
beta. So every leg also carries the repo's exact algebraic identity:

.. code-block:: text

    drift  = side * (close[exit_bar] - close[entry_bar]) * oz
    excess = gross - drift

``drift`` is what a passive hold of the same side over the same window
would have earned. If ``excess`` is not positive, the rule is not
picking direction — it is riding the market.

WHY THE PLACEBO ARMS ARE THE POINT
==================================

The repo has already been burned by a confounded control
(``leg_random_entry.py`` — it compared a resting LIMIT against a
MARKET fill, so it measured the ORDER TYPE, not the entry skill).
The rule learned here:

    **A control must be EXCHANGEABLE with the thing it controls for.**

So the placebo arms here are built by REPLAYING the real arm's
schedule:

* the same ENTRY bar and the same ENTRY price
* the same EXIT bar and the same EXIT price
* the same TIME IN MARKET, the same PATH, the same fee
* the same execution model, the same fill rules

and the ONLY thing that changes is the SIDE:

``as_is``   the rule's own side (the real arm)
``flip``    the negated side — destroys DIRECTION, keeps everything else
``random``  a coin-flip side per leg — destroys DIRECTION, keeps
            everything else

> **The flip arm is a TAUTOLOGY under a fixed schedule** — its gross is
> exactly ``-gross`` of the real arm, because entry, exit and size are
> held fixed. It is reported as a SIGN/SYMMETRY SANITY CHECK (it
> proves the metric responds to the direction and is not biased), and
> it is explicitly NOT used as the statistical test. The repo has
> already documented this trap.
>
> The **random arm is the real test**. Flipping the sign of each leg's
> realised move at random is a textbook SIGN-FLIP RANDOMISATION TEST
> on the direction call: under the null "the direction adds nothing
> beyond the schedule", the signs are exchangeable. The real arm's
> DREM is then compared against the empirical distribution of 100
> coin-flip arms. Because the schedule is FIXED, every arm shares the
> same denominator, so the comparison is exact.

The ORACLE IS THE GROUND TRUTH
==============================

The strongest validation needs no market at all: build a SYNTHETIC
price series in which the future is knowable, and check that the
metric returns the answer it must return.

* an oracle that always takes the correct side -> ``DAC ~ +1``
* its negation                                    -> ``DAC ~ -1``
* a driftless random walk                          -> ``DAC ~ 0``

``tests/test_ta_1m_metric.py`` locks all three, plus the executor's
pessimistic fill, the fee model, the no-lookahead invariant, and the
bar builder's gap handling.

DATA / TIMEFRAME
================

Everything here runs on **1-MINUTE** bars, built from the canonical 1s
XAUUSD corpus. Two facts about that feed drive the design:

1. **The feed is IRREGULAR.** Median 46 ticks per minute (not 60),
   with weekend and rollover gaps up to 48 hours. So the 1m bars are
   bucketed BY TIMESTAMP (never by row position — that is the NB94
   forward-leak class of bug) and the per-bucket max/min uses
   ``np.maximum.reduceat`` (the bucket index is monotonic, so each
   bucket is a contiguous segment — the same fix the detector uses).

2. **The session is NOT 24h.** Hour 22 UTC is missing entirely and
   hour 21 is partial, so the tradeable day runs 23:00 -> 21:00 UTC.
   "Time in market" is therefore measured against TRADING MINUTES
   (minutes that actually have ticks), never against wall-clock
   minutes. A rule that holds a position across the 22:00 rollover is
   charged the real wall-clock time it was exposed, but it is not
   credited with a session that did not exist.

TIMEBASE
========

Every timestamp is int64 epoch NANOSECONDS, via
:func:`bt._engine.timebase.to_ns_arr`. Never ``.astype("int64")`` on
the datetime column — the parquet stores ``timestamp[ms]``, so that
call silently yields MILLISECONDS (this bit the first draft of this
module: 30,197 minutes collapsed into 1 bucket).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Sequence

import numpy as np
import pandas as pd

from bt._engine.timebase import to_ns_arr
from bt.optimal_config import (
    FOREX_COMMISSION_PER_LOT_USD,
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    FOREX_SPREAD_PER_OZ_USD,
)

# ─────────────────────────────────────────────────────────────────
# Canonical constants (read from the single source of truth)
# ─────────────────────────────────────────────────────────────────

#: The notional unit: 0.01 lots x 100 oz/lot = 1.00 oz.
OZ: float = FOREX_LOTS * FOREX_CONTRACT_SIZE

#: Nanoseconds per minute. Timestamps are ns everywhere.
NS_PER_MIN: int = 60 * 1_000_000_000

#: The live MT4 round trip at 1 oz: $0.07 commission + $0.40 spread.
ROUND_TRIP_USD: float = (
    FOREX_LOTS * FOREX_COMMISSION_PER_LOT_USD
    + OZ * FOREX_SPREAD_PER_OZ_USD
)

#: The feed's session boundary. The XAUUSD day rolls at 22:00 UTC
#: (hour 22 is entirely absent from the corpus), so a "trading day"
#: starts at 23:00 UTC.
SESSION_ROLLOVER_HOUR: int = 22

#: The canonical ATR lookback used to size the stop / target.
DEFAULT_ATR_N: int = 14

#: The canonical exit geometry (multiples of ATR), used as the shared
#: default for every strategy cell. Swept separately via
#: ``--sweep-exit`` so the strategy sweep is not confounded by the
#: exit grid.
DEFAULT_SL_ATR_MULT: float = 1.5
DEFAULT_TP_ATR_MULT: float = 3.0
DEFAULT_MAX_HOLD_BARS: int = 240      # 4 hours on 1m bars


def fee_bps_for(spot: float) -> float:
    """The engine's per-side ``taker_fee_bps`` for a corpus spot.

    Mirrors ``bt.optimal_config._derive_taker_fee_bps`` exactly: the
    engine debits ``(entry + exit) * oz * bps / 10000`` at close, so
    the RATE must be derived from the ROUND TRIP dollar amount.
    Feeding the round trip into a per-side slot is the D1 2x bug.
    """
    if spot <= 0 or OZ <= 0:
        return 0.0
    return ROUND_TRIP_USD * 10000.0 / (2.0 * OZ * spot)


# ─────────────────────────────────────────────────────────────────
# 1-minute bars
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Bars1m:
    """One-minute OHLC bars plus the derived series the TAs need."""

    time_ns: np.ndarray      # int64, bucket START (ns, UTC)
    open: np.ndarray         # float64
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray       # tick volume summed into the bucket
    n_ticks: np.ndarray      # int64, ticks in the bucket
    atr: np.ndarray          # float64, Wilder ATR (per bar)
    atr_n: int

    def __len__(self) -> int:
        return int(self.close.size)

    @property
    def trading_minutes(self) -> int:
        """Minutes that actually had ticks — the session-size proxy."""
        return int(self.close.size)


def build_bars_1m(bars: pd.DataFrame, *, atr_n: int = DEFAULT_ATR_N
                  ) -> Bars1m:
    """Aggregate 1s bars into 1-minute bars, WITHOUT forward leaks.

    The aggregation is bucketed BY TIMESTAMP, never by row position.
    The canonical parquet stores ``timestamp[ms]``, so the bucket index
    is computed from the ns conversion (:func:`to_ns_arr`) and anchored
    to the UTC epoch so a bucket is a real wall-clock minute.

    ``np.maximum.reduceat`` / ``np.minimum.reduceat`` are valid
    because the bucket index is MONOTONIC on a real feed: every
    bucket is a contiguous run of rows. The alternative (one full-length
    mask per bucket) is O(n^2) — on the 1y corpus that was measured at
    ~8 hours.

    Minutes with NO ticks are DROPPED, never forward-filled. A
    forward-filled bar would have a zero range, which would poison
    the ATR and would let a rule "trade" during a session that did
    not exist.
    """
    if "time" not in bars.columns:
        raise ValueError(f"bars is missing the 'time' column "
                         f"(have: {list(bars.columns)})")
    t_ns = to_ns_arr(bars)
    n = int(t_ns.size)
    if n == 0:
        z = np.zeros(0)
        return Bars1m(np.zeros(0, np.int64), z, z, z, z, z,
                      np.zeros(0, np.int64), z, atr_n)

    bucket = t_ns // NS_PER_MIN
    uniq, starts = np.unique(bucket, return_index=True)
    ends = np.append(starts[1:], n) - 1

    o = bars["open"].to_numpy(dtype=np.float64)
    h = bars["high"].to_numpy(dtype=np.float64)
    lo = bars["low"].to_numpy(dtype=np.float64)
    c = bars["close"].to_numpy(dtype=np.float64)

    if "volume" in bars.columns:
        v = bars["volume"].to_numpy(dtype=np.float64)
    elif "tickv" in bars.columns:
        v = bars["tickv"].to_numpy(dtype=np.float64)
    else:
        v = np.zeros(n, dtype=np.float64)

    m_open = o[starts]
    m_high = np.maximum.reduceat(h, starts)
    m_low = np.minimum.reduceat(lo, starts)
    m_close = c[ends]
    m_vol = np.add.reduceat(v, starts)
    m_ticks = (ends - starts + 1).astype(np.int64)

    atr = _atr(m_high, m_low, m_close, atr_n)
    return Bars1m(
        time_ns=(uniq * NS_PER_MIN).astype(np.int64),
        open=m_open, high=m_high, low=m_low, close=m_close,
        volume=m_vol, n_ticks=m_ticks, atr=atr, atr_n=atr_n,
    )


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         n: int) -> np.ndarray:
    """Wilder's ATR via an exponential moving average of true range."""
    if close.size == 0:
        return np.zeros(0)
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev), np.abs(low - prev)))
    alpha = 1.0 / float(max(1, n))
    s = pd.Series(tr).ewm(alpha=alpha, adjust=False).mean()
    return s.to_numpy(dtype=np.float64)


# ─────────────────────────────────────────────────────────────────
# Indicators (vectorised, backward-only)
# ─────────────────────────────────────────────────────────────────

def _ema(x: np.ndarray, n: int) -> np.ndarray:
    if x.size == 0:
        return x
    return pd.Series(x).ewm(span=max(1, int(n)), adjust=False).mean().to_numpy()


def _rolling(x: np.ndarray, n: int, fn: str) -> np.ndarray:
    if x.size == 0:
        return x
    s = pd.Series(x)
    r = getattr(s.rolling(max(1, int(n))), fn)()
    return r.to_numpy(dtype=np.float64)


def _rsi(close: np.ndarray, n: int) -> np.ndarray:
    if close.size == 0:
        return close
    d = np.diff(close, prepend=close[0])
    up = np.maximum(d, 0.0)
    dn = np.maximum(-d, 0.0)
    au = _ema(up, n)
    ad = _ema(dn, n)
    rs = np.divide(au, ad, out=np.full_like(au, np.inf), where=ad > 0)
    out = 100.0 - 100.0 / (1.0 + rs)
    return np.where(np.isfinite(out), out, 100.0)


def _bbands(close: np.ndarray, n: int, k: float
            ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mid = _rolling(close, n, "mean")
    sd = _rolling(close, n, "std")
    return mid, mid + k * sd, mid - k * sd


def _stoch(high: np.ndarray, low: np.ndarray, close: np.ndarray,
           n: int) -> np.ndarray:
    hh = _rolling(high, n, "max")
    ll = _rolling(low, n, "min")
    rng = hh - ll
    out = np.divide(close - ll, rng, out=np.zeros_like(close), where=rng > 0)
    return 100.0 * out


def _session_vwap(bars1m: Bars1m) -> tuple[np.ndarray, np.ndarray]:
    """Session VWAP and its rolling sigma, per trading session.

    The session boundary is 22:00 UTC (the feed's daily rollover), so
    a session label is the UTC date of ``time - 22h``.
    """
    n = len(bars1m)
    if n == 0:
        return np.zeros(0), np.zeros(0)
    t = bars1m.time_ns
    shifted = t - SESSION_ROLLOVER_HOUR * 3600 * 1_000_000_000
    session_id = shifted // (24 * 3600 * 1_000_000_000)

    vwap = np.zeros(n)
    sigma = np.zeros(n)
    i = 0
    while i < n:
        j = i
        while j + 1 < n and session_id[j + 1] == session_id[i]:
            j += 1
        seg_p = bars1m.close[i:j + 1]
        seg_v = bars1m.volume[i:j + 1]
        if seg_v.sum() > 0:
            pv = np.cumsum(seg_p * seg_v)
            vv = np.cumsum(seg_v)
            w = pv / vv
            sd = np.sqrt(np.maximum(_rolling(seg_p, 60, "std"), 0.0))
        else:
            w = seg_p
            sd = np.zeros_like(seg_p)
        vwap[i:j + 1] = w
        sigma[i:j + 1] = sd
        i = j + 1
    return vwap, sigma


def _rolling_slope(close: np.ndarray, n: int) -> tuple[np.ndarray, np.ndarray]:
    """Rolling OLS slope of close vs bar index, and the R^2 of the fit.

    Vectorised with a sliding window: the O(n x n) Python loop this
    replaced was the slowest thing in the sweep.
    """
    m = len(close)
    slope = np.zeros(m)
    r2 = np.zeros(m)
    if m < n or n < 2:
        return slope, r2
    x = np.arange(n, dtype=np.float64)
    x -= x.mean()
    sxx = float((x * x).sum())
    win = np.lib.stride_tricks.sliding_window_view(close, n)
    dy = win - win.mean(axis=1, keepdims=True)
    sxy = dy @ x
    syy = (dy * dy).sum(axis=1)
    slope[n - 1:] = sxy / sxx
    with np.errstate(divide="ignore", invalid="ignore"):
        r2[n - 1:] = np.where(syy > 0, (sxy * sxy) / (sxx * syy), 0.0)
    return slope, r2


# ─────────────────────────────────────────────────────────────────
# TA signal generators
# ─────────────────────────────────────────────────────────────────
#
# Every generator returns an array `s` of shape (n_bars,) with values
# in {-1, 0, +1}:
#     +1  the rule wants to be LONG
#      0  the rule wants to be FLAT
#     -1  the rule wants to be SHORT
#
# `s[i]` is computed from bars UP TO AND INCLUDING bar i, so the
# earliest possible entry is the OPEN of bar i+1. The executor honours
# that, which is what makes the whole harness look-ahead free.

SignalFn = Callable[[Bars1m, dict], np.ndarray]


def _sig_ema_cross(b: Bars1m, p: dict) -> np.ndarray:
    fast = _ema(b.close, int(p["fast"]))
    slow = _ema(b.close, int(p["slow"]))
    out = np.zeros(len(b))
    out[fast > slow] = 1
    out[fast < slow] = -1
    return out


def _sig_donchian(b: Bars1m, p: dict) -> np.ndarray:
    n = int(p["n"])
    hh = _rolling(b.high, n, "max")
    ll = _rolling(b.low, n, "min")
    # Shift by one bar: the breakout must be measured against the
    # PREVIOUS n bars, not against the bar that is breaking out.
    hh = np.roll(hh, 1)
    ll = np.roll(ll, 1)
    hh[:1] = np.nan
    ll[:1] = np.nan
    out = np.zeros(len(b))
    out[b.close > hh] = 1
    out[b.close < ll] = -1
    return out


def _sig_rsi_revert(b: Bars1m, p: dict) -> np.ndarray:
    r = _rsi(b.close, int(p["n"]))
    lo = float(p["lo"])
    hi = float(p["hi"])
    out = np.zeros(len(b))
    out[r < lo] = 1
    out[r > hi] = -1
    return out


def _sig_bb_fade(b: Bars1m, p: dict) -> np.ndarray:
    _, up, dn = _bbands(b.close, int(p["n"]), float(p["k"]))
    out = np.zeros(len(b))
    out[b.close > up] = -1
    out[b.close < dn] = 1
    return out


def _sig_bb_break(b: Bars1m, p: dict) -> np.ndarray:
    _, up, dn = _bbands(b.close, int(p["n"]), float(p["k"]))
    out = np.zeros(len(b))
    out[b.close > up] = 1
    out[b.close < dn] = -1
    return out


def _sig_macd(b: Bars1m, p: dict) -> np.ndarray:
    macd = (_ema(b.close, int(p["fast"]))
            - _ema(b.close, int(p["slow"])))
    sig = _ema(macd, int(p["signal"]))
    out = np.zeros(len(b))
    out[macd > sig] = 1
    out[macd < sig] = -1
    return out


def _sig_roc_trend(b: Bars1m, p: dict) -> np.ndarray:
    n = int(p["n"])
    thr = float(p["thresh"])
    prev = np.roll(b.close, n)
    prev[:n] = b.close[0]
    with np.errstate(divide="ignore", invalid="ignore"):
        roc = (b.close - prev) / prev
    denom = (b.atr * math.sqrt(max(1, n))) / np.maximum(b.close, 1e-9)
    z = np.divide(roc, denom, out=np.zeros_like(roc), where=denom > 0)
    out = np.zeros(len(b))
    out[z > thr] = 1
    out[z < -thr] = -1
    return out


def _sig_vwap_revert(b: Bars1m, p: dict) -> np.ndarray:
    vwap, sd = _session_vwap(b)
    k = float(p["k"])
    band = k * np.maximum(sd, 0.05)
    out = np.zeros(len(b))
    out[b.close < vwap - band] = 1
    out[b.close > vwap + band] = -1
    return out


def _sig_stoch_cross(b: Bars1m, p: dict) -> np.ndarray:
    k = _stoch(b.high, b.low, b.close, int(p["n"]))
    d = _rolling(k, int(p["smooth"]), "mean")
    out = np.zeros(len(b))
    out[k > d] = 1
    out[k < d] = -1
    return out


def _sig_atr_expansion(b: Bars1m, p: dict) -> np.ndarray:
    n = int(p["n"])
    k = float(p["k"])
    med = _rolling(b.atr, 60, "median")
    hh = _rolling(b.high, n, "max")
    ll = _rolling(b.low, n, "min")
    hh = np.roll(hh, 1)
    ll = np.roll(ll, 1)
    hh[:1] = np.nan
    ll[:1] = np.nan
    expand = b.atr > k * np.maximum(med, 1e-9)
    out = np.zeros(len(b))
    out[expand & (b.close > hh)] = 1
    out[expand & (b.close < ll)] = -1
    return out


def _sig_slope_trend(b: Bars1m, p: dict) -> np.ndarray:
    n = int(p["n"])
    min_r2 = float(p["min_r2"])
    slope, r2 = _rolling_slope(b.close, n)
    denom = (b.atr * n) / np.maximum(b.close, 1e-9)
    z = np.divide(slope, denom, out=np.zeros_like(slope), where=denom > 0)
    ok = r2 >= min_r2
    out = np.zeros(len(b))
    out[ok & (z > 0)] = 1
    out[ok & (z < 0)] = -1
    return out


def _sig_keltner_break(b: Bars1m, p: dict) -> np.ndarray:
    n = int(p["n"])
    k = float(p["k"])
    mid = _ema(b.close, n)
    band = k * b.atr
    out = np.zeros(len(b))
    out[b.close > mid + band] = 1
    out[b.close < mid - band] = -1
    return out


# ─────────────────────────────────────────────────────────────────
# MOMENTUM and TREND-FOLLOWING families (2026-10-25)
# ─────────────────────────────────────────────────────────────────
#
# These are the "momentum and trend following" TAs the user asked to
# try. They are added ALONGSIDE the existing brainstorm, not
# replacing it, and every one of them is a STATE STREAM: +1 wants to
# be long, -1 wants to be short, 0 wants to be flat.
#
# The families are deliberately built on DIFFERENT ingredients, so a
# sweep can tell them apart:
#
#   adx_trend      ADX/DI directional strength   (trend STRENGTH)
#   supertrend     ATR-banded trailing stop      (trend, self-reversing)
#   ts_ma_cross    close vs a trend MA           (trend, price vs MA)
#   momentum_rsi   RSI momentum thrust           (momentum, bounded)
#   cci            CCI deviation from the mean   (momentum, unbounded)
#   willr          Williams %R                   (momentum, bounded)
#   roc_zero       rate-of-change sign           (momentum, simplest)
#   kama           Kaufman adaptive MA          (trend, adaptive)
#   squeeze        BB inside Keltner             (momentum, expansion)
#
# Every one is BACKWARD-LOOKING ONLY: it reads bars up to and
# including bar i, so the earliest possible entry is the OPEN of bar
# i+1. See the module docstring.


def _adx(b: Bars1m, n: int) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Wilder's ADX with +DI / -DI.

    Returns ``(adx, plus_di, minus_di)``. ADX is NaN during warmup.
    """
    h = b.high
    low = b.low
    c = b.close
    if c.size < 2:
        z = np.full(c.size, np.nan)
        return z, z.copy(), z.copy()

    up = np.diff(h, prepend=h[0])
    dn = -np.diff(low, prepend=low[0])
    plus_dm = np.where((up > dn) & (up > 0), up, 0.0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0.0)

    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - low,
                    np.maximum(np.abs(h - prev_c), np.abs(low - prev_c)))
    tr[0] = h[0] - low[0]

    def _wilder(x: np.ndarray) -> np.ndarray:
        s = pd.Series(x)
        # Wilder's smoothing == an EMA with alpha = 1/n.
        return s.ewm(alpha=1.0 / float(max(1, n)), adjust=False).mean().to_numpy()

    atr = _wilder(tr)
    pdi = 100.0 * _wilder(plus_dm) / np.maximum(atr, 1e-9)
    mdi = 100.0 * _wilder(minus_dm) / np.maximum(atr, 1e-9)
    dx = 100.0 * np.abs(pdi - mdi) / np.maximum(pdi + mdi, 1e-9)
    adx = _wilder(np.nan_to_num(dx))
    # Warmup: ADX is meaningless until the DI spread has stabilised.
    warm = 2 * int(n)
    if adx.size > warm:
        adx[:warm] = np.nan
    return adx, pdi, mdi


def _sig_adx_trend(b: Bars1m, p: dict) -> np.ndarray:
    """ADX above a threshold: follow the dominant DI direction."""
    n = int(p["n"])
    thr = float(p["thr"])
    adx, pdi, mdi = _adx(b, n)
    out = np.zeros(len(b))
    strong = np.nan_to_num(adx, nan=0.0) >= thr
    out[strong & (pdi > mdi)] = 1
    out[strong & (mdi > pdi)] = -1
    return out


def _sig_supertrend(b: Bars1m, p: dict) -> np.ndarray:
    """SuperTrend: an ATR band that flips the trend when it breaks.

    A self-reversing trend follower: the state changes exactly when
    the close crosses the band, so it is the cleanest "close on signal
    change" rule in the family.

    THE BAND IS TRAILING, AND THAT IS THE WHOLE POINT. The final
    upper band only ratchets UP (it keeps the tightest value it has
    seen) until the close breaks ABOVE it, at which point the band
    resets to the current basic band and the trend flips long.

    An earlier draft used "the trend is short" as a proxy for "reset
    the upper band", which reset it on EVERY bar of a downtrend and
    made the band so wide that a long flip could never trigger — the
    rule degenerated to "always short". The correct reset condition
    is the previous close breaking the band.
    """
    n = int(p["n"])
    mult = float(p["mult"])
    atr = b.atr
    hl2 = (b.high + b.low) * 0.5
    basic_upper = hl2 + mult * atr
    basic_lower = hl2 - mult * atr

    n_bars = len(b)
    close = b.close
    trend = np.zeros(n_bars, dtype=np.int8)
    if n_bars == 0:
        return trend

    fu = float(basic_upper[0])
    fl = float(basic_lower[0])
    direction = 0
    for i in range(n_bars):
        if i == 0:
            fu = float(basic_upper[0])
            fl = float(basic_lower[0])
        else:
            prev_c = float(close[i - 1])
            # The band resets when the PREVIOUS close broke it.
            fu = (float(basic_upper[i])
                  if (float(basic_upper[i]) < fu or prev_c > fu)
                  else fu)
            fl = (float(basic_lower[i])
                  if (float(basic_lower[i]) > fl or prev_c < fl)
                  else fl)
        c = float(close[i])
        if c > fu:
            direction = 1
        elif c < fl:
            direction = -1
        trend[i] = direction
    return trend


def _sig_ts_ma_cross(b: Bars1m, p: dict) -> np.ndarray:
    """Close vs a single trend MA (no cross event needed).

    The state is the sign of ``close - ma``, so the signal flips
    exactly when the price crosses the MA.
    """
    n = int(p["n"])
    ma = _ema(b.close, n)
    out = np.zeros(len(b))
    out[b.close > ma] = 1
    out[b.close < ma] = -1
    return out


def _sig_momentum_rsi(b: Bars1m, p: dict) -> np.ndarray:
    """RSI momentum thrust: long above ``hi``, short below ``lo``.

    This is the MOMENTUM reading of RSI — the mirror of
    ``rsi_revert``, which fades the same oscillator. Both are kept so
    a sweep can measure which reading of the SAME indicator survives.
    """
    r = _rsi(b.close, int(p["n"]))
    lo = float(p["lo"])
    hi = float(p["hi"])
    out = np.zeros(len(b))
    out[r > hi] = 1
    out[r < lo] = -1
    return out


def _sig_cci(b: Bars1m, p: dict) -> np.ndarray:
    """Commodity Channel Index: deviation from the typical price."""
    n = int(p["n"])
    thr = float(p["thr"])
    tp = (b.high + b.low + b.close) / 3.0
    ma = _rolling(tp, n, "mean")
    md = _rolling(np.abs(tp - ma), n, "mean")
    denom = np.maximum(0.015 * md, 1e-9)
    cci = (tp - ma) / denom
    out = np.zeros(len(b))
    out[cci > thr] = 1
    out[cci < -thr] = -1
    return out


def _sig_willr(b: Bars1m, p: dict) -> np.ndarray:
    """Williams %R, read as MOMENTUM (oversold = long).

    ``rsi_revert`` is the same idea via RSI. Keeping both means the
    sweep measures the oscillator, not the implementation.
    """
    n = int(p["n"])
    lo = float(p["lo"])
    hi = float(p["hi"])
    hh = _rolling(b.high, n, "max")
    ll = _rolling(b.low, n, "min")
    rng = hh - ll
    wr = np.divide(hh - b.close, rng, out=np.zeros_like(b.close), where=rng > 0)
    wr = wr * -100.0
    out = np.zeros(len(b))
    out[wr < lo] = 1
    out[wr > hi] = -1
    return out


def _sig_roc_zero(b: Bars1m, p: dict) -> np.ndarray:
    """The sign of the rate of change — the simplest momentum there is."""
    n = int(p["n"])
    prev = np.roll(b.close, n)
    prev[:n] = b.close[0]
    out = np.zeros(len(b))
    out[b.close > prev] = 1
    out[b.close < prev] = -1
    return out


def _sig_kama(b: Bars1m, p: dict) -> np.ndarray:
    """Kaufman's Adaptive MA: an efficiency-ratio-weighted MA.

    The ER is the ratio of net displacement to path length, so it
    scales the MA toward the price in a trending market and toward
    the price's own mean in a choppy one. The signal is the sign of
    ``close - kama``.
    """
    n = int(p["n"])
    fast = int(p.get("fast", 2))
    slow = int(p.get("slow", 30))

    # The path length is the sum of |close-to-close| moves over the
    # window. It is a CUMULATIVE sum, so it must be trimmed to the
    # SAME length as the price series before the two are subtracted.
    diff = np.abs(np.diff(b.close, prepend=b.close[0]))
    path = np.concatenate(([0.0], np.cumsum(diff)))[:-1]   # length n
    disp = np.abs(b.close - np.roll(b.close, n))
    disp[:n] = 0.0
    travelled = path - np.roll(path, n)
    travelled[:n] = 0.0
    er = np.divide(disp, travelled, out=np.zeros_like(disp), where=travelled > 0)
    sc = (er * (2.0 / (fast + 1) - 2.0 / (slow + 1))
          + 2.0 / (slow + 1)) ** 2

    kama = np.zeros(len(b))
    kama[0] = float(b.close[0])
    for i in range(1, len(b)):
        kama[i] = kama[i - 1] + sc[i] * (float(b.close[i]) - kama[i - 1])
    out = np.zeros(len(b))
    out[b.close > kama] = 1
    out[b.close < kama] = -1
    return out


def _sig_squeeze(b: Bars1m, p: dict) -> np.ndarray:
    """Momentum expansion: a Bollinger band inside a Keltner channel.

    While the bands are SQUEEZED the rule is flat; the breakout bar
    takes the direction. This is the "compression then expansion" idea
    — the state is 0 for most of the time and directional only on the
    expansion, which is exactly the shape a "gate open" wants.
    """
    n = int(p["n"])
    kk = float(p["k_bband"])
    kk2 = float(p["k_keltner"])
    _, up, dn = _bbands(b.close, n, kk)
    mid = _ema(b.close, n)
    band = kk2 * b.atr
    ku = mid + band
    kd = mid - band
    squeeze = (up < ku) & (dn > kd)

    out = np.zeros(len(b))
    # The expansion bar: the close leaves the Bollinger band.
    out[~squeeze & (b.close > up)] = 1
    out[~squeeze & (b.close < dn)] = -1
    return out


def _sig_zscore_revert(b: Bars1m, p: dict) -> np.ndarray:
    """Mean reversion on the price's own z-score.

    Long when the price is ``thr`` standard deviations BELOW its
    rolling mean, short when it is that far ABOVE. This is the
    cleanest statement of "fade the stretch" that does not depend on
    an oscillator, so it measures the MR IDEA rather than any
    particular indicator.
    """
    n = int(p["n"])
    thr = float(p["thr"])
    mu = _rolling(b.close, n, "mean")
    sd = _rolling(b.close, n, "std")
    z = np.divide(b.close - mu, sd, out=np.zeros_like(b.close), where=sd > 0)
    out = np.zeros(len(b))
    out[z < -thr] = 1
    out[z > thr] = -1
    return out


def _sig_range_fade(b: Bars1m, p: dict) -> np.ndarray:
    """Fade the extremes of the recent range.

    Short at the top of the rolling range, long at the bottom. It
    differs from ``bb_fade`` in that the band is a RANGE (a high/low
    channel) rather than a statistical one, so it does not depend on
    the close's own standard deviation.
    """
    n = int(p["n"])
    k = float(p["k"])
    hh = _rolling(b.high, n, "max")
    ll = _rolling(b.low, n, "min")
    rng = np.maximum(hh - ll, 1e-9)
    pos = (b.close - ll) / rng
    out = np.zeros(len(b))
    out[pos < (1.0 - k)] = 1
    out[pos > k] = -1
    return out


@dataclass(frozen=True)
class TAStrategy:
    """One strategy cell: a name, its parameters, and its signal."""

    name: str
    params: dict[str, Any]
    signal: np.ndarray

    @property
    def label(self) -> str:
        if not self.params:
            return self.name
        inner = ",".join(f"{k}={v}" for k, v in sorted(self.params.items()))
        return f"{self.name}({inner})"


#: The brainstorm. Each entry is (name, signal fn, parameter grid).
#: The grid is deliberately small — this is a one-month screen.
STRATEGY_GRIDS: dict[str, tuple[SignalFn, list[dict[str, Any]]]] = {
    "ema_cross": (_sig_ema_cross, [
        {"fast": 5, "slow": 20}, {"fast": 8, "slow": 34},
        {"fast": 13, "slow": 55}, {"fast": 21, "slow": 89},
        {"fast": 34, "slow": 144},
    ]),
    "donchian": (_sig_donchian, [
        {"n": 10}, {"n": 20}, {"n": 40}, {"n": 60}, {"n": 120},
    ]),
    "rsi_revert": (_sig_rsi_revert, [
        {"n": 7, "lo": 30, "hi": 70}, {"n": 14, "lo": 30, "hi": 70},
        {"n": 21, "lo": 30, "hi": 70}, {"n": 14, "lo": 25, "hi": 75},
        {"n": 14, "lo": 20, "hi": 80}, {"n": 14, "lo": 35, "hi": 65},
    ]),
    "bb_fade": (_sig_bb_fade, [
        {"n": 20, "k": 1.5}, {"n": 20, "k": 2.0}, {"n": 20, "k": 2.5},
        {"n": 40, "k": 1.5}, {"n": 40, "k": 2.0}, {"n": 40, "k": 2.5},
    ]),
    "bb_break": (_sig_bb_break, [
        {"n": 20, "k": 1.5}, {"n": 20, "k": 2.0}, {"n": 20, "k": 2.5},
        {"n": 40, "k": 1.5}, {"n": 40, "k": 2.0}, {"n": 40, "k": 2.5},
    ]),
    "macd": (_sig_macd, [
        {"fast": 12, "slow": 26, "signal": 9},
        {"fast": 5, "slow": 35, "signal": 5},
        {"fast": 8, "slow": 21, "signal": 5},
    ]),
    "roc_trend": (_sig_roc_trend, [
        {"n": 5, "thresh": 0.5}, {"n": 5, "thresh": 1.0},
        {"n": 5, "thresh": 2.0}, {"n": 10, "thresh": 0.5},
        {"n": 10, "thresh": 1.0}, {"n": 10, "thresh": 2.0},
        {"n": 20, "thresh": 1.0},
    ]),
    "vwap_revert": (_sig_vwap_revert, [
        {"k": 1.0}, {"k": 1.5}, {"k": 2.0}, {"k": 2.5},
    ]),
    "stoch_cross": (_sig_stoch_cross, [
        {"n": 14, "smooth": 3}, {"n": 14, "smooth": 5},
        {"n": 21, "smooth": 3}, {"n": 21, "smooth": 5},
    ]),
    "atr_expansion": (_sig_atr_expansion, [
        {"n": 20, "k": 1.2}, {"n": 20, "k": 1.5}, {"n": 20, "k": 2.0},
        {"n": 40, "k": 1.2}, {"n": 40, "k": 1.5}, {"n": 40, "k": 2.0},
    ]),
    "slope_trend": (_sig_slope_trend, [
        {"n": 10, "min_r2": 0.0}, {"n": 10, "min_r2": 0.3},
        {"n": 20, "min_r2": 0.0}, {"n": 20, "min_r2": 0.3},
        {"n": 20, "min_r2": 0.6}, {"n": 40, "min_r2": 0.3},
    ]),
    "keltner_break": (_sig_keltner_break, [
        {"n": 20, "k": 1.0}, {"n": 20, "k": 1.5}, {"n": 20, "k": 2.0},
        {"n": 40, "k": 1.0}, {"n": 40, "k": 1.5}, {"n": 40, "k": 2.0},
    ]),
    # ── MOMENTUM and TREND-FOLLOWING (2026-10-25) ────────────────
    "adx_trend": (_sig_adx_trend, [
        {"n": 14, "thr": 15.0}, {"n": 14, "thr": 20.0},
        {"n": 14, "thr": 25.0}, {"n": 28, "thr": 15.0},
        {"n": 28, "thr": 20.0}, {"n": 28, "thr": 25.0},
    ]),
    "supertrend": (_sig_supertrend, [
        {"n": 14, "mult": 1.5}, {"n": 14, "mult": 2.0},
        {"n": 14, "mult": 2.5}, {"n": 28, "mult": 1.5},
        {"n": 28, "mult": 2.0}, {"n": 28, "mult": 2.5},
    ]),
    "ts_ma_cross": (_sig_ts_ma_cross, [
        {"n": 20}, {"n": 50}, {"n": 100}, {"n": 200},
    ]),
    "momentum_rsi": (_sig_momentum_rsi, [
        {"n": 14, "lo": 30, "hi": 70}, {"n": 14, "lo": 20, "hi": 80},
        {"n": 14, "lo": 40, "hi": 60}, {"n": 7, "lo": 30, "hi": 70},
        {"n": 21, "lo": 30, "hi": 70}, {"n": 21, "lo": 20, "hi": 80},
    ]),
    "cci": (_sig_cci, [
        {"n": 14, "thr": 100.0}, {"n": 14, "thr": 150.0},
        {"n": 14, "thr": 200.0}, {"n": 20, "thr": 100.0},
        {"n": 20, "thr": 150.0}, {"n": 20, "thr": 200.0},
    ]),
    "willr": (_sig_willr, [
        {"n": 14, "lo": -80, "hi": -20}, {"n": 14, "lo": -70, "hi": -30},
        {"n": 14, "lo": -90, "hi": -10}, {"n": 21, "lo": -80, "hi": -20},
        {"n": 21, "lo": -70, "hi": -30},
    ]),
    "roc_zero": (_sig_roc_zero, [
        {"n": 5}, {"n": 10}, {"n": 20}, {"n": 40}, {"n": 60},
    ]),
    "kama": (_sig_kama, [
        {"n": 10, "fast": 2, "slow": 30},
        {"n": 20, "fast": 2, "slow": 30},
        {"n": 20, "fast": 3, "slow": 20},
        {"n": 30, "fast": 2, "slow": 30},
    ]),
    "squeeze": (_sig_squeeze, [
        {"n": 20, "k_bband": 2.0, "k_keltner": 1.5},
        {"n": 20, "k_bband": 2.0, "k_keltner": 1.0},
        {"n": 20, "k_bband": 1.5, "k_keltner": 1.5},
        {"n": 40, "k_bband": 2.0, "k_keltner": 1.5},
        {"n": 40, "k_bband": 2.0, "k_keltner": 1.0},
    ]),
    # ── MEAN REVERSION (2026-10-25) ──────────────────────────────
    # The MR half of the sweep. ``rsi_revert`` / ``bb_fade`` /
    # ``vwap_revert`` already exist; these two add the "idea" forms
    # that do not depend on a named oscillator.
    "zscore_revert": (_sig_zscore_revert, [
        {"n": 20, "thr": 1.0}, {"n": 20, "thr": 1.5},
        {"n": 20, "thr": 2.0}, {"n": 60, "thr": 1.0},
        {"n": 60, "thr": 1.5}, {"n": 60, "thr": 2.0},
    ]),
    "range_fade": (_sig_range_fade, [
        {"n": 20, "k": 0.1}, {"n": 20, "k": 0.2},
        {"n": 60, "k": 0.1}, {"n": 60, "k": 0.2},
        {"n": 120, "k": 0.1},
    ]),
}


def build_strategy_signals(bars1m: Bars1m, *, names: Iterable[str] | None = None
                           ) -> list[TAStrategy]:
    """Materialise every strategy cell into a signal array."""
    out: list[TAStrategy] = []
    wanted = set(names) if names else set(STRATEGY_GRIDS)
    for name in sorted(wanted):
        if name not in STRATEGY_GRIDS:
            raise KeyError(f"unknown strategy {name!r}; known: "
                           f"{sorted(STRATEGY_GRIDS)}")
        fn, grid = STRATEGY_GRIDS[name]
        for params in grid:
            sig = np.asarray(fn(bars1m, params), dtype=np.int8)
            if sig.size != len(bars1m):
                raise ValueError(
                    f"{name}{params}: signal length {sig.size} != "
                    f"{len(bars1m)} bars")
            out.append(TAStrategy(name, dict(params), sig))
    return out


# ─────────────────────────────────────────────────────────────────
# The executor
# ─────────────────────────────────────────────────────────────────

@dataclass
class Leg:
    """One closed position."""

    signal_bar: int
    entry_bar: int
    exit_bar: int
    entry_time_ns: int
    exit_time_ns: int
    entry_price: float
    exit_price: float
    direction: int
    gross_usd: float
    fee_usd: float
    net_usd: float
    hold_min: float
    mfe_usd: float       # best favorable excursion, signed, $
    mae_usd: float       # worst adverse excursion, signed, $
    path_usd: float      # total |movement| while in the market, $
    exit_reason: str
    sl_price: float
    tp_price: float


def _segments(signal: np.ndarray) -> list[tuple[int, int, int]]:
    """Maximal runs of constant non-zero signal.

    Returns ``(entry_bar, exit_due_bar, side)`` per segment.

    * ``entry_bar``     = the bar AFTER the first bar of the run (the
      signal is known at the close of the first bar).
    * ``exit_due_bar``  = the bar AFTER the last bar of the run (the
      signal changed at the close of the last bar, so the exit is at
      the next open).
    """
    n = int(signal.size)
    segs: list[tuple[int, int, int]] = []
    i = 0
    while i < n:
        if signal[i] == 0:
            i += 1
            continue
        j = i
        while j + 1 < n and signal[j + 1] == signal[i]:
            j += 1
        entry_bar = i + 1
        exit_due = min(j + 1, n)
        segs.append((entry_bar, exit_due, int(signal[i])))
        i = j + 1
    return segs


def run_ta(bars1m: Bars1m, signal: np.ndarray, *,
           sl_mult: float = DEFAULT_SL_ATR_MULT,
           tp_mult: float = DEFAULT_TP_ATR_MULT,
           max_hold_bars: int = DEFAULT_MAX_HOLD_BARS,
           side_mode: str = "as_is",
           seed: int = 0) -> list[Leg]:
    """Execute a signal array on the 1m bars.

    Parameters
    ----------
    side_mode:
        ``as_is``  the rule's own side (the real arm)
        ``flip``   the negated side — destroys DIRECTION only
        ``random`` a coin-flip side — destroys DIRECTION only

        With ``side_mode != 'as_is'`` this function re-derives the
        EXIT from the new side, so the arms have DIFFERENT schedules.
        That is NOT exchangeable, so this mode is a DIAGNOSTIC ONLY —
        the statistical control is :func:`replay_legs`, which holds the
        schedule fixed.

    Execution model (mirrors the canonical engine):
        * entry at the OPEN of the bar after the signal
        * stop / target sized on the ATR at the SIGNAL bar
        * **pessimistic** fill: a bar touching both levels books the
          STOP
        * gap-through fills at the bar's OPEN
    """
    n = len(bars1m)
    if n == 0:
        return []
    rng = np.random.default_rng(seed)
    legs: list[Leg] = []
    o = bars1m.open
    h = bars1m.high
    lo = bars1m.low
    c = bars1m.close
    atr = bars1m.atr

    for entry_bar, exit_due, sig_side in _segments(signal):
        if entry_bar >= n:
            break
        if side_mode == "as_is":
            side = sig_side
        elif side_mode == "flip":
            side = -sig_side
        elif side_mode == "random":
            side = 1 if rng.random() < 0.5 else -1
        else:
            raise ValueError(f"unknown side_mode {side_mode!r}")

        sig_bar = entry_bar - 1
        a = float(atr[sig_bar]) if sig_bar >= 0 else float(atr[0])
        if not math.isfinite(a) or a <= 0:
            med = float(np.nanmedian(atr)) if atr.size else 0.0
            a = med if math.isfinite(med) and med > 0 else 0.0
        if a <= 0:
            # No volatility to size a stop against: the trade is not
            # expressible, so it is not a leg.
            continue

        entry_px = float(o[entry_bar])
        sl_dist = sl_mult * a
        tp_dist = tp_mult * a
        sl_px = entry_px - sl_dist if side > 0 else entry_px + sl_dist
        tp_px = entry_px + tp_dist if side > 0 else entry_px - tp_dist

        hold_exit = entry_bar + int(max_hold_bars)
        exit_bar = None
        exit_px = 0.0
        reason = ""

        for t in range(entry_bar, n):
            # (1) Signal-driven or time-driven exit: both happen at the
            # OPEN of this bar, before its range.
            if t == exit_due:
                exit_bar, exit_px, reason = t, float(o[t]), "signal"
                break
            if t == hold_exit:
                exit_bar, exit_px, reason = t, float(o[t]), "time"
                break
            # (2) Gap through the stop -> fill at the open.
            if side > 0 and float(o[t]) <= sl_px:
                exit_bar, exit_px, reason = t, float(o[t]), "sl"
                break
            if side < 0 and float(o[t]) >= sl_px:
                exit_bar, exit_px, reason = t, float(o[t]), "sl"
                break
            # (3) Gap through the target -> fill at the open (better).
            if side > 0 and float(o[t]) >= tp_px:
                exit_bar, exit_px, reason = t, float(o[t]), "tp"
                break
            if side < 0 and float(o[t]) <= tp_px:
                exit_bar, exit_px, reason = t, float(o[t]), "tp"
                break
            # (4) Intrabar. PESSIMISTIC: the stop wins the ambiguity.
            if side > 0:
                sl_hit = float(lo[t]) <= sl_px
                tp_hit = float(h[t]) >= tp_px
            else:
                sl_hit = float(h[t]) >= sl_px
                tp_hit = float(lo[t]) <= tp_px
            if sl_hit:
                exit_bar, exit_px, reason = t, sl_px, "sl"
                break
            if tp_hit:
                exit_bar, exit_px, reason = t, tp_px, "tp"
                break

        if exit_bar is None:
            # Still open at the end of the data: close at the last
            # close (a forced flat, so the leg is still scored).
            exit_bar = n - 1
            exit_px = float(c[exit_bar])
            reason = "eod"

        legs.append(_make_leg(
            bars1m, sig_bar, entry_bar, exit_bar, entry_px, exit_px,
            side, sl_px, tp_px, reason))
    return legs


def _make_leg(bars1m: Bars1m, sig_bar: int, entry_bar: int, exit_bar: int,
              entry_px: float, exit_px: float, side: int,
              sl_px: float, tp_px: float, reason: str) -> Leg:
    """Assemble a Leg, including the excursions over the hold."""
    mfe, mae, path = _excursions(
        bars1m, entry_bar, exit_bar, entry_px, side, sl_px, tp_px, reason)
    gross_px = side * (exit_px - entry_px)
    gross_usd = gross_px * OZ
    fee_usd = _leg_fee(entry_px, exit_px)
    return Leg(
        signal_bar=sig_bar,
        entry_bar=entry_bar,
        exit_bar=exit_bar,
        entry_time_ns=int(bars1m.time_ns[entry_bar]),
        exit_time_ns=int(bars1m.time_ns[exit_bar]),
        entry_price=entry_px,
        exit_price=exit_px,
        direction=int(side),
        gross_usd=float(gross_usd),
        fee_usd=float(fee_usd),
        net_usd=float(gross_usd - fee_usd),
        hold_min=float(exit_bar - entry_bar),
        mfe_usd=float(mfe),
        mae_usd=float(mae),
        path_usd=float(path),
        exit_reason=reason,
        sl_price=float(sl_px),
        tp_price=float(tp_px),
    )


def _leg_fee(entry_px: float, exit_px: float) -> float:
    """The engine's close-time fee: ``(entry + exit) * oz * bps / 10000``.

    The bps come from :func:`fee_bps_for` with the corpus spot, so the
    realised cost SCALES WITH THE FILL PRICE (the repo's measured fee
    is $0.5029/leg on the 1y corpus vs the $0.47 spec at $3,000).
    """
    spot = _SPOT[0]
    if spot <= 0:
        return 0.0
    bps = fee_bps_for(spot)
    return (entry_px + exit_px) * OZ * (bps / 10000.0)


#: The corpus spot used to derive the fee rate. Set by
#: :func:`set_fee_spot` before any execution.
_SPOT: list[float] = [3000.0]


def set_fee_spot(spot: float) -> None:
    """Set the corpus spot used to derive the per-side fee rate."""
    if spot > 0:
        _SPOT[0] = float(spot)


def _excursions(bars1m: Bars1m, entry_bar: int, exit_bar: int,
                entry_px: float, side: int, sl_px: float, tp_px: float,
                exit_reason: str) -> tuple[float, float, float]:
    """Favourable excursion, adverse excursion and path over the hold.

    The EXIT bar's excursion is CAPPED BY THE TRADE'S OWN STOP AND
    TARGET: on the bar we exited we could not have booked anything
    beyond our target (favourable) and our loss was capped at our
    stop (adverse). Without the cap, a long stopped out on a bar whose
    high was $3 above entry would report a $3 favourable excursion it
    never got.
    """
    hi = bars1m.high
    low = bars1m.low
    cl = bars1m.close
    mfe = -math.inf
    mae = -math.inf
    path = abs(float(cl[entry_bar]) - entry_px)
    for t in range(entry_bar, exit_bar + 1):
        if t == exit_bar:
            if side > 0:
                fav_px = min(float(hi[t]), tp_px)
                adv_px = max(float(low[t]), sl_px)
            else:
                fav_px = min(float(low[t]), tp_px)
                adv_px = max(float(hi[t]), sl_px)
        else:
            if side > 0:
                fav_px = float(hi[t])
                adv_px = float(low[t])
            else:
                fav_px = float(low[t])
                adv_px = float(hi[t])
        if side > 0:
            mfe = max(mfe, fav_px - entry_px)
            mae = max(mae, entry_px - adv_px)
        else:
            mfe = max(mfe, entry_px - fav_px)
            mae = max(mae, adv_px - entry_px)
        if t > entry_bar:
            path += abs(float(cl[t]) - float(cl[t - 1]))
    if mfe == -math.inf:
        mfe = 0.0
    if mae == -math.inf:
        mae = 0.0
    return mfe * OZ, mae * OZ, path * OZ


# ─────────────────────────────────────────────────────────────────
# The EXCHANGEABLE placebo: replay the schedule, change the side
# ─────────────────────────────────────────────────────────────────

def replay_legs(bars1m: Bars1m, legs: Sequence[Leg], *,
                side_mode: str = "flip",
                seed: int = 0) -> list[Leg]:
    """Replay ``legs`` with a different SIDE, holding everything else.

    THIS is the control. The entry bar, entry price, exit bar, exit
    price, time in market, path and fee are ALL held fixed. The only
    thing that changes is the direction of each leg.

    Why this design: the first draft recomputed the EXIT for the
    flipped side, so the placebo arms had different denominators and
    different time in market. That made the comparison measure the
    SCHEDULE as much as the DIRECTION — the same class of confound the
    repo hit in ``leg_random_entry.py``. With the schedule fixed, the
    arms are exactly exchangeable and the comparison is a clean
    sign-flip randomisation test.

    The stop/target are MIRRORED for the new side (so the excursion
    caps stay meaningful), but they never move an exit — the exit is
    inherited.
    """
    if side_mode not in ("as_is", "flip", "random"):
        raise ValueError(f"unknown side_mode {side_mode!r}")
    rng = np.random.default_rng(seed)
    out: list[Leg] = []
    for lg in legs:
        if side_mode == "as_is":
            side = lg.direction
        elif side_mode == "flip":
            side = -lg.direction
        else:
            side = 1 if rng.random() < 0.5 else -1
        if side == lg.direction:
            sl_px, tp_px = lg.sl_price, lg.tp_price
        else:
            # Mirror the geometry about the entry price.
            sl_px = 2.0 * lg.entry_price - lg.tp_price
            tp_px = 2.0 * lg.entry_price - lg.sl_price
        out.append(_make_leg(
            bars1m, lg.signal_bar, lg.entry_bar, lg.exit_bar,
            lg.entry_price, lg.exit_price, side, sl_px, tp_px,
            lg.exit_reason))
    return out


# ─────────────────────────────────────────────────────────────────
# The scorecard
# ─────────────────────────────────────────────────────────────────

def _mean_se(x: np.ndarray) -> tuple[float, float]:
    n = int(x.size)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return float(x[0]), float("nan")
    return float(x.mean()), float(x.std(ddof=1) / math.sqrt(n))


def score_legs(legs: Sequence[Leg], bars1m: Bars1m, *,
               trading_minutes: int | None = None) -> dict[str, Any]:
    """The metric suite for one arm."""
    n = len(legs)
    tm = int(trading_minutes if trading_minutes is not None
             else bars1m.trading_minutes)
    if n == 0:
        return {
            "n": 0, "dac_path": 0.0, "dac_mfe": 0.0, "drem": 0.0,
            "tim": 0.0, "gross_total": 0.0, "fees_total": 0.0,
            "net_total": 0.0, "gross_per_leg": 0.0, "fee_per_leg": 0.0,
            "gross_per_leg_over_fee": 0.0, "hit_rate": 0.0,
            "drift_total": 0.0, "excess_total": 0.0,
            "excess_per_leg": 0.0, "t_excess": 0.0,
            "median_hold_min": 0.0, "path_total": 0.0,
            "n_flat_legs": 0, "n_long": 0, "n_short": 0, "payoff": 0.0,
        }

    path = np.array([lg.path_usd for lg in legs], dtype=np.float64)
    mfe = np.array([lg.mfe_usd for lg in legs], dtype=np.float64)
    gross = np.array([lg.gross_usd for lg in legs], dtype=np.float64)
    fee = np.array([lg.fee_usd for lg in legs], dtype=np.float64)
    net = np.array([lg.net_usd for lg in legs], dtype=np.float64)
    hold = np.array([lg.hold_min for lg in legs], dtype=np.float64)
    side = np.array([lg.direction for lg in legs], dtype=np.int64)

    # Direction correctness: did the rule's SIDE match the outcome?
    # A FLAT leg (gross == 0) is NEITHER right nor wrong -> C = 0.
    c = np.where(gross > 0, 1.0, np.where(gross < 0, -1.0, 0.0))

    path_sum = float(path.sum())
    mfe_sum = float(np.abs(mfe).sum())
    dac_path = (float((path * c).sum()) / path_sum) if path_sum > 0 else 0.0
    dac_mfe = (float((np.abs(mfe) * c).sum()) / mfe_sum) if mfe_sum > 0 else 0.0
    hold_sum = float(hold.sum())
    drem = (float(gross.sum()) / hold_sum) if hold_sum > 0 else 0.0

    # The repo's drift / excess decomposition, on 1m closes.
    close = bars1m.close
    drift = np.array([
        side[i] * (float(close[lg.exit_bar]) - float(close[lg.entry_bar]))
        * OZ
        for i, lg in enumerate(legs)
    ], dtype=np.float64)
    excess = gross - drift
    me, se = _mean_se(excess)

    fee_total = float(fee.sum())
    gross_total = float(gross.sum())
    gpl = gross_total / n
    fpl = fee_total / n

    return {
        "n": n,
        # ── the metric ───────────────────────────────────────────
        "dac_path": float(dac_path),
        "dac_mfe": float(dac_mfe),
        "drem": float(drem),
        "tim": float(hold_sum / tm) if tm > 0 else 0.0,
        # ── the money ────────────────────────────────────────────
        "gross_total": gross_total,
        "fees_total": fee_total,
        "net_total": float(net.sum()),
        "gross_per_leg": float(gpl),
        "fee_per_leg": float(fpl),
        "gross_per_leg_over_fee": float(gpl / fpl) if fpl > 0 else 0.0,
        "hit_rate": float((net > 0).mean()),
        "payoff": _payoff(gross),
        # ── is it just beta? ──────────────────────────────────────
        "drift_total": float(drift.sum()),
        "excess_total": float(excess.sum()),
        "excess_per_leg": float(me),
        "t_excess": float(me / se) if (se and math.isfinite(se) and se > 0) else 0.0,
        # ── exposure ─────────────────────────────────────────────
        "median_hold_min": float(np.median(hold)),
        "mean_hold_min": float(hold.mean()),
        "path_total": path_sum,
        "n_flat_legs": int((c == 0).sum()),
        "n_long": int((side > 0).sum()),
        "n_short": int((side < 0).sum()),
    }


def _payoff(gross: np.ndarray) -> float:
    wins = gross[gross > 0]
    losses = gross[gross <= 0]
    if wins.size == 0 or losses.size == 0:
        return 0.0
    aw = float(wins.mean())
    al = abs(float(losses.mean()))
    return (aw / al) if al > 0 else 0.0


def score_with_placebos(bars1m: Bars1m, signal: np.ndarray, *,
                        sl_mult: float, tp_mult: float,
                        max_hold_bars: int,
                        n_random: int = 100, seed: int = 0,
                        trading_minutes: int | None = None
                        ) -> dict[str, Any]:
    """Score the real arm and its exchangeable placebos.

    ``as_is``   the rule's own side (the REAL arm)
    ``flip``    the negated side — schedule FIXED, so its gross is
                exactly ``-gross`` of the real arm. This is a SIGN /
                SYMMETRY SANITY CHECK, NOT a test of skill.
    ``random``  N coin-flip sides — schedule FIXED. THIS is the
                statistical test: a sign-flip randomisation test on
                the direction call.

    ``drem_excess`` is the headline: the part of the directional
    reward that the rule's DIRECTION CALL actually produced, with the
    market-beta part removed by the placebo rather than by an
    assumption.
    """
    real = run_ta(bars1m, signal, sl_mult=sl_mult, tp_mult=tp_mult,
                  max_hold_bars=max_hold_bars, side_mode="as_is")
    s_real = score_legs(real, bars1m, trading_minutes=trading_minutes)

    # ── the exchangeable controls: SAME SCHEDULE, different side ──
    flip = replay_legs(bars1m, real, side_mode="flip")
    s_flip = score_legs(flip, bars1m, trading_minutes=trading_minutes)

    randoms: list[dict[str, Any]] = []
    for k in range(n_random):
        arms = replay_legs(bars1m, real, side_mode="random",
                           seed=seed + 1000 * (k + 1))
        randoms.append(score_legs(arms, bars1m,
                                  trading_minutes=trading_minutes))

    # ── the NON-exchangeable diagnostic (own geometry) ───────────
    flip_own = run_ta(bars1m, signal, sl_mult=sl_mult, tp_mult=tp_mult,
                      max_hold_bars=max_hold_bars, side_mode="flip")
    s_flip_own = score_legs(flip_own, bars1m,
                             trading_minutes=trading_minutes)

    def _col(key: str, src: Sequence[dict[str, Any]]) -> np.ndarray:
        return np.array([r[key] for r in src], dtype=np.float64)

    r_drem = _col("drem", randoms)
    r_dac = _col("dac_path", randoms)
    r_net = _col("net_total", randoms)

    drem_real = float(s_real["drem"])
    # Empirical p-value: how often does a coin flip beat the real one?
    p_drem = (1.0 + float((r_drem >= drem_real).sum())) / (n_random + 1.0)
    p_net = (1.0 + float((r_net >= float(s_real["net_total"])).sum())) \
        / (n_random + 1.0)

    out = {
        "real": s_real,
        "flip": s_flip,
        "flip_own_geometry": s_flip_own,
        "random_mean": {
            "drem": float(r_drem.mean()) if r_drem.size else 0.0,
            "dac_path": float(r_dac.mean()) if r_dac.size else 0.0,
            "net_total": float(r_net.mean()) if r_net.size else 0.0,
            "drem_sd": float(r_drem.std(ddof=1)) if r_drem.size > 1 else 0.0,
            "net_sd": float(r_net.std(ddof=1)) if r_net.size > 1 else 0.0,
        },
        "n_random": n_random,
        "p_drem_vs_random": float(p_drem),
        "p_net_vs_random": float(p_net),
        # The flip arm is a TAUTOLOGY under a fixed schedule. Recorded
        # so nobody quotes it as a test.
        "flip_is_tautology_under_fixed_schedule": True,
    }
    out["drem_excess"] = float(drem_real - float(out["random_mean"]["drem"]))
    out["dac_excess"] = float(float(s_real["dac_path"])
                              - float(out["random_mean"]["dac_path"]))
    out["net_excess"] = float(float(s_real["net_total"])
                              - float(out["random_mean"]["net_total"]))
    out["metric_is_directionally_valid"] = bool(
        out["drem_excess"] > 0.0 and p_drem < 0.05)
    out["verdict"] = _verdict(out)
    return out


def _verdict(res: dict[str, Any]) -> str:
    """The per-cell verdict, in the repo's language."""
    real = res["real"]
    if real.get("n", 0) == 0:
        return "no trades: the rule never fired on this window"
    parts: list[str] = []
    if not res.get("metric_is_directionally_valid"):
        parts.append(
            "METRIC INVALID: the rule's direction call is not "
            f"distinguishable from a coin flip "
            f"(DREM excess {res['drem_excess']:+.4f}, "
            f"p={res['p_drem_vs_random']:.3f})")
    else:
        parts.append(
            f"directional edge: DREM excess {res['drem_excess']:+.4f} "
            f"/min, p={res['p_drem_vs_random']:.4f}")
    if real["gross_per_leg_over_fee"] >= 1.0:
        parts.append(
            f"gross/leg covers the fee ({real['gross_per_leg_over_fee']:.2f}x)")
    else:
        parts.append(
            f"gross/leg does NOT cover the fee "
            f"({real['gross_per_leg_over_fee']:.2f}x)")
    if real["t_excess"] > 2.0:
        parts.append("timing is not beta (excess t>2)")
    elif real["t_excess"] < -2.0:
        parts.append("gross IS beta (excess t<-2)")
    return "; ".join(parts)


__all__ = [
    "Bars1m", "Leg", "TAStrategy",
    "OZ", "ROUND_TRIP_USD", "NS_PER_MIN",
    "DEFAULT_SL_ATR_MULT", "DEFAULT_TP_ATR_MULT", "DEFAULT_MAX_HOLD_BARS",
    "build_bars_1m", "build_strategy_signals", "fee_bps_for",
    "run_ta", "replay_legs", "score_legs", "score_with_placebos",
    "set_fee_spot", "STRATEGY_GRIDS",
]
