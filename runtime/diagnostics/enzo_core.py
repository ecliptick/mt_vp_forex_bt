"""ENZO STRATEGY — M1 RSI(14) reversion + H4 EMA limit-order system.

WHAT THIS IS
============

A backtest of the strategy written up in ``Enzo input.pdf`` (XAUUSD,
MT4/MT5 convention). The PDF describes **TWO** rules:

RULE 1 — M1 RSI REVERSION
    * M1 chart, RSI (default 14 period), levels 16 and 84.
    * SELL when RSI hits 84; BUY when RSI hits 16.
    * TP and SL $5 (50 pips) — and a second cell with TP and SL $10.
    * "If TP, no second trades. Cool down period 1 hour."
    * "If SL, open second trades with same TP SL provided M1 still
      above level 82 / below 16."
      -> on a STOP-OUT, one extra attempt in the SAME direction with
      the SAME TP/SL, admitted only if the RSI is still beyond the
      tighter threshold (a sell needs RSI > 82, a buy needs RSI < 16).
      A TP ends the episode with no retry.

RULE 2 — H4 EMA LIMIT ORDERS
    * H4 chart, EMA. TP and SL $25.
    * BUY LIMIT / SELL LIMIT ("wait for price to come and hit a
      better price").
    * Blue and red lines determine the trend. If both cross -> INVALID.
    * If the lines show a trend (blue below red = DOWNTREND, vice
      versa), focus the YELLOW line and put BUY LIMIT / SELL LIMIT
      $13 away from it.
    * Worked example (PDF p4): downtrend week 19/10-23/10, SELL LIMIT
      placed at 4143.50, which is "$13 away from YELLOW LINE" -> the
      yellow line was at 4130.50. The order only triggered "after more
      than 24 hours".

The EMA periods are NOT stated in the PDF, so they are a SWEPT axis
(see :mod:`runtime.diagnostics.enzo_backtest`).

WHY IT IS BUILT LIKE THIS
=========================

The repo's rules are followed exactly:

* **Execution is CONTINUOUS.** One run over one window with a warmup
  prefix. A month is a REPORTING dimension, never an execution
  boundary.
* **The unit of evaluation is the LEG**, not the month.
* **The fee model is the canonical one** (0.01 lots x 100 oz = 1.00 oz,
  $0.07 commission + $0.40 spread per round trip), scaled by the fill
  price exactly as the engine does.
* **The fill is PESSIMISTIC**: on a bar that touches both the stop and
  the target, the STOP wins; a gap through a level fills at the bar's
  OPEN.
* **The control is EXCHANGEABLE.** The placebo arms hold the entry bar,
  the entry price, the exit bar and the exit price FIXED and change
  only the SIDE. That is a sign-flip randomisation test on the
  direction call — the same construction the repo's 1m TA harness
  uses. The "flip" arm is a tautology (``gross -> -gross``) and is
  reported only as a symmetry sanity check.
* **The drift / excess decomposition** is carried over: if
  ``excess = gross - drift`` is not positive, the gross is market
  beta, not skill.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Sequence

import numpy as np
import pandas as pd

from bt.optimal_config import (
    FOREX_COMMISSION_PER_LOT_USD,
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    FOREX_SPREAD_PER_OZ_USD,
)

# ─────────────────────────────────────────────────────────────────
# Canonical constants
# ─────────────────────────────────────────────────────────────────

#: 0.01 lots x 100 oz/lot = 1.00 oz.
OZ: float = FOREX_LOTS * FOREX_CONTRACT_SIZE

#: The live MT4 round trip at 1 oz: $0.07 commission + $0.40 spread.
ROUND_TRIP_USD: float = (
    FOREX_LOTS * FOREX_COMMISSION_PER_LOT_USD + OZ * FOREX_SPREAD_PER_OZ_USD
)

#: Nanoseconds per minute.
NS_PER_MIN: int = 60 * 1_000_000_000

#: Rule 1 parameters (from the PDF).
RSI_PERIOD: int = 14
RSI_SELL_LEVEL: float = 84.0     # SELL when RSI hits 84
RSI_BUY_LEVEL: float = 16.0      # BUY when RSI hits 16
RSI_SELL_RETRY_LEVEL: float = 82.0   # "still above level 82"
RSI_BUY_RETRY_LEVEL: float = 16.0    # "below 16"
COOLDOWN_MIN: int = 60           # "Cool down period 1 hour"

#: Rule 1 exit geometries, in dollars of price move at 1 oz.
#: "$5 (50 pips)" and "$10 (100 pips)". The DOLLAR amount is
#: authoritative — the PDF's pip counts are internally inconsistent
#: (it calls $5 "50 pips" but $13 "1300 pips"), so the prices are used.
M1_EXIT_CELL_LIST: tuple[dict[str, float], ...] = (
    {"tp_usd": 5.0, "sl_usd": 5.0},
    {"tp_usd": 10.0, "sl_usd": 10.0},
)

#: Rule 2 parameters (from the PDF).
H4_TP_USD: float = 25.0
H4_SL_USD: float = 25.0
H4_LIMIT_OFFSET_USD: float = 13.0   # "$13 away from YELLOW LINE"

#: The CANONICAL H4 EMA triple, specified by the user (2026-10-25):
#:
#:     blue   = EMA(10) of CLOSE
#:     red    = EMA(20) of OPEN
#:     yellow = EMA(60) of CLOSE
#:
#: all with shift 0 and the MT4/MT5 "Exponential" MA method. The red
#: line being an EMA of the H4 OPEN (not the close) is not a detail:
#: it is a different series, and it is what the user specified.
H4_EMA_PERIODS: tuple[int, int, int] = (10, 20, 60)
H4_EMA_SOURCES: tuple[str, str, str] = ("close", "open", "close")

#: The canonical H4 grid alignment: bars close on the 00/04/08/12/16/20
#: UTC grid. Swept, because a broker's H4 bars need not close on the
#: UTC grid -- and the choice moves the result materially (see
#: ``docs/enzo_strategies.md``).
H4_SESSION_OFFSET_H: int = 0

#: The canonical order lifecycle:
#:
#: * a CROSS of the blue and red lines CANCELS a resting order (the
#:   PDF: "if both line cross then INVALID"), and
#: * the resting order is NOT re-priced at every H4 close -- it rests
#:   at the price it was armed at (the PDF: the order "only triggered
#:   after more than 24 hours").
H4_CROSS_CANCELS: bool = True
H4_REPRICE: bool = False

#: A sensitivity grid around the canonical triple (the sweep surface).
H4_EMA_PERIOD_GRID: tuple[tuple[int, int, int], ...] = (
    (10, 20, 60), (9, 26, 89), (12, 26, 100), (13, 34, 100),
    (13, 55, 200), (20, 50, 100), (21, 55, 200),
)


def fee_bps_for(spot: float) -> float:
    """The engine's per-side ``taker_fee_bps`` for a corpus spot.

    Mirrors ``bt.optimal_config._derive_taker_fee_bps``: the engine
    debits ``(entry + exit) * oz * bps / 10000`` at close, so the RATE
    must be derived from the ROUND TRIP dollar amount. Feeding the
    round trip into a per-side slot is the D1 2x bug.
    """
    if spot <= 0 or OZ <= 0:
        return 0.0
    return ROUND_TRIP_USD * 10000.0 / (2.0 * OZ * spot)


# ─────────────────────────────────────────────────────────────────
# Indicators
# ─────────────────────────────────────────────────────────────────

def wilder_rsi(close: np.ndarray, n: int = RSI_PERIOD) -> np.ndarray:
    """Wilder's RSI — the EXACT MT4 ``iRSI`` construction.

    MT4 seeds the average gain / loss with the SIMPLE mean of the first
    ``n`` changes and then recurses with ``avg = (avg*(n-1) + x)/n``.
    ``pandas.ewm(alpha=1/n, adjust=False)`` has the same recursion but
    seeds with ``x[0]``, so the first bars differ. The difference is
    immaterial after ~200 bars, but the first bars are exactly where a
    "hit level 84" would be least reliable anyway, so this uses the
    exact seed.
    """
    c = np.asarray(close, dtype=np.float64)
    m = c.size
    if m < 2:
        return np.zeros(m)
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = np.zeros(m)
    ad = np.zeros(m)
    au[n] = float(up[:n].mean())      # average over changes 1..n
    ad[n] = float(dn[:n].mean())
    for i in range(n + 1, m):
        au[i] = (au[i - 1] * (n - 1) + up[i - 1]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i - 1]) / n
    out = np.full(m, 50.0)
    valid = np.arange(m) >= n
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(au, ad, out=np.full(m, np.inf), where=ad > 0)
        out = 100.0 - 100.0 / (1.0 + rs)
    out[~valid] = 50.0
    out[~np.isfinite(out)] = 100.0
    return out


def ema(x: np.ndarray, n: int) -> np.ndarray:
    """The MT4/MT5 "Exponential MA" — ``alpha = 2/(n+1)``, shift 0.

    MT4 seeds an MA with a SIMPLE average of the first ``n`` values and
    then recurses ``ema = (x*alpha + ema_prev*(1-alpha))``. pandas'
    ``ewm(span=n, adjust=False)`` has the SAME recursion but seeds with
    ``x[0]`` instead of the SMA of the first ``n`` values.

    The difference decays as ``(1-alpha)^k``, which for ``n = 60`` is
    still ~25% after 30 bars — i.e. still material on the first H4
    bars of a window. So the seed is done the MT4 way here.

    The recursion is written out explicitly rather than using pandas so
    the seeding is visible and testable.
    """
    a = np.asarray(x, dtype=np.float64)
    m = a.size
    n = max(1, int(n))
    out = np.empty(m, dtype=np.float64)
    if m == 0:
        return out
    if m < n:
        # Not enough bars to seed: fall back to the running mean.
        csum = np.cumsum(a)
        out[:] = csum / np.arange(1, m + 1)
        return out
    alpha = 2.0 / (n + 1.0)
    prev = float(a[:n].mean())      # MT4 seeds with the SMA of the first n
    out[:n] = prev                  # the first n bars all read the seed
    for i in range(n, m):
        prev = alpha * a[i] + (1.0 - alpha) * prev
        out[i] = prev
    return out


# ─────────────────────────────────────────────────────────────────
# Bars
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EnzoBars:
    """1-minute bars (the execution grid) plus the H4 view."""

    time_ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    h4_time_ns: np.ndarray
    h4_open: np.ndarray
    h4_high: np.ndarray
    h4_low: np.ndarray
    h4_close: np.ndarray
    atr: np.ndarray

    def __len__(self) -> int:
        return int(self.close.size)


def build_enzo_bars(bars_1s: pd.DataFrame,
                    h4_offset_h: int = 0) -> EnzoBars:
    """Aggregate the canonical 1s corpus into 1m and H4 bars.

    The 1m aggregation is bucketed BY TIMESTAMP (never by row
    position — that is the NB94 forward-leak class of bug) and the
    per-bucket max/min uses ``np.maximum.reduceat`` (the bucket index
    is monotonic, so each bucket is a contiguous run of rows).

    The H4 view is resampled FROM THE 1m BARS, not from the 1s feed,
    so the two timeframes are guaranteed consistent.

    ``h4_offset_h`` shifts the H4 grid off the 00/04/08/... UTC
    boundary, so the BROKER's session alignment can be swept (a
    broker's H4 bars do not necessarily close on the UTC grid). The
    offset is applied to the resample, not to the data.
    """
    from bt._engine.timebase import to_ns_arr

    t_ns = to_ns_arr(bars_1s)
    n = int(t_ns.size)
    if n == 0:
        z = np.zeros(0)
        return EnzoBars(np.zeros(0, np.int64), z, z, z, z,
                        np.zeros(0, np.int64), z, z, z, z, z)

    o = bars_1s["open"].to_numpy(dtype=np.float64)
    h = bars_1s["high"].to_numpy(dtype=np.float64)
    lo = bars_1s["low"].to_numpy(dtype=np.float64)
    c = bars_1s["close"].to_numpy(dtype=np.float64)

    bucket = t_ns // NS_PER_MIN
    uniq, starts = np.unique(bucket, return_index=True)
    ends = np.append(starts[1:], n) - 1

    m_open = o[starts]
    m_high = np.maximum.reduceat(h, starts)
    m_low = np.minimum.reduceat(lo, starts)
    m_close = c[ends]
    m_time = (uniq * NS_PER_MIN).astype(np.int64)

    # ── H4, resampled from the 1m bars ──────────────────────────
    m1 = pd.DataFrame({
        "time": pd.to_datetime(m_time, unit="ns", utc=True),
        "open": m_open, "high": m_high, "low": m_low, "close": m_close,
    }).set_index("time")
    off = pd.Timedelta(hours=int(h4_offset_h) % 4)
    h4 = m1.resample("4h", offset=off).agg({"open": "first", "high": "max",
                                            "low": "min", "close": "last"}
                                           ).dropna()

    return EnzoBars(
        time_ns=m_time, open=m_open, high=m_high, low=m_low, close=m_close,
        h4_time_ns=h4.index.astype("int64"),
        h4_open=h4["open"].to_numpy(), h4_high=h4["high"].to_numpy(),
        h4_low=h4["low"].to_numpy(), h4_close=h4["close"].to_numpy(),
        atr=_atr(m_high, m_low, m_close, 14),
    )


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         n: int) -> np.ndarray:
    if close.size == 0:
        return np.zeros(0)
    prev = np.empty_like(close)
    prev[0] = close[0]
    prev[1:] = close[:-1]
    tr = np.maximum(high - low,
                    np.maximum(np.abs(high - prev), np.abs(low - prev)))
    return pd.Series(tr).ewm(alpha=1.0 / n, adjust=False).mean().to_numpy()


# ─────────────────────────────────────────────────────────────────
# Legs
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class EnzoLeg:
    """One closed position."""

    strat: str
    side: int                 # +1 long, -1 short
    attempt: int              # 1 = first entry, 2 = the post-SL retry
    signal_time_ns: int
    entry_time_ns: int
    exit_time_ns: int
    entry_price: float
    exit_price: float
    sl_price: float
    tp_price: float
    gross_usd: float
    fee_usd: float
    net_usd: float
    exit_reason: str          # tp | sl | eod
    hold_min: float
    mfe_usd: float = 0.0
    mae_usd: float = 0.0
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def dir_label(self) -> str:
        return "long" if self.side > 0 else "short"


# ─────────────────────────────────────────────────────────────────
# RULE 1 — M1 RSI reversion
# ─────────────────────────────────────────────────────────────────

def run_m1_rsi(b: EnzoBars, rsi: np.ndarray, *, tp_usd: float,
               sl_usd: float, cooldown_min: int = COOLDOWN_MIN,
               sell_level: float = RSI_SELL_LEVEL,
               buy_level: float = RSI_BUY_LEVEL,
               sell_retry_level: float = RSI_SELL_RETRY_LEVEL,
               buy_retry_level: float = RSI_BUY_RETRY_LEVEL,
               cross_trigger: bool = True,
               strat_name: str = "m1_rsi") -> list[EnzoLeg]:
    """Execute Rule 1 on the 1m grid.

    Timeline of one episode (the PDF's wording, made explicit):

    * A signal is a RSI CROSS into the extreme: the bar's RSI is at or
      beyond 84 (sell) / 16 (buy). The signal is known at the CLOSE of
      that bar; the entry is the OPEN of the next bar.
    * TP and SL are fixed dollar distances from the entry price.
    * If the trade hits the **TP** -> the episode ends. No second
      trade.
    * If the trade hits the **SL** -> a second trade is opened in the
      SAME direction with the SAME TP/SL, but only if the RSI is still
      beyond the tighter threshold (sell: RSI > 82, buy: RSI < 16).
      The retry is admitted at the close of the SL bar and fills at the
      open of the next bar.
    * After the episode ends (TP, or the final SL) there is a **one
      hour cooldown** in which no new signal is taken.

    The retry is part of the SAME episode, so it is exempt from the
    cooldown — otherwise the cooldown would make it unreachable.

    The fill model is PESSIMISTIC: on a bar that touches both the stop
    and the target the STOP wins, and a gap through a level fills at
    the bar's OPEN.
    """
    n = len(b)
    if n < 3:
        return []
    o, h, lo = b.open, b.high, b.low
    t_ns = b.time_ns
    legs: list[EnzoLeg] = []
    cooldown_ns = cooldown_min * 60 * 1_000_000_000

    pending_side = 0
    pending_sig_time = 0
    pending_attempt = 0
    cooldown_until_ns = -1

    pos_side = 0
    pos_entry_px = 0.0
    pos_entry_bar = -1
    pos_entry_time = 0
    pos_sl = 0.0
    pos_tp = 0.0
    pos_attempt = 0
    pos_sig_time = 0

    for i in range(1, n):
        # ── 1. fill a pending order at this bar's OPEN ──────────
        if pending_side != 0:
            pos_side = pending_side
            pos_entry_px = float(o[i])
            pos_entry_bar = i
            pos_entry_time = int(t_ns[i])
            pos_sig_time = pending_sig_time
            pos_attempt = pending_attempt
            if pos_side > 0:
                pos_tp = pos_entry_px + tp_usd
                pos_sl = pos_entry_px - sl_usd
            else:
                pos_tp = pos_entry_px - tp_usd
                pos_sl = pos_entry_px + sl_usd
            pending_side = 0

        # ── 2. resolve an open position on this bar ─────────────
        if pos_side != 0:
            exit_px = 0.0
            reason = ""
            if pos_side > 0:
                gap_sl = float(o[i]) <= pos_sl
                gap_tp = float(o[i]) >= pos_tp
            else:
                gap_sl = float(o[i]) >= pos_sl
                gap_tp = float(o[i]) <= pos_tp
            if gap_sl:
                exit_px, reason = float(o[i]), "sl"
            elif gap_tp:
                exit_px, reason = float(o[i]), "tp"
            else:
                if pos_side > 0:
                    hit_sl = float(lo[i]) <= pos_sl
                    hit_tp = float(h[i]) >= pos_tp
                else:
                    hit_sl = float(h[i]) >= pos_sl
                    hit_tp = float(lo[i]) <= pos_tp
                if hit_sl:            # PESSIMISTIC: stop wins ambiguity
                    exit_px, reason = pos_sl, "sl"
                elif hit_tp:
                    exit_px, reason = pos_tp, "tp"
            if reason:
                mfe, mae = _excursions(b, pos_entry_bar, i, pos_entry_px,
                                       pos_side, pos_sl, pos_tp)
                legs.append(_make_leg(
                    strat_name, pos_side, pos_attempt, pos_sig_time,
                    pos_entry_time, int(t_ns[i]), pos_entry_px, exit_px,
                    pos_sl, pos_tp, reason, float(i - pos_entry_bar),
                    mfe, mae))
                # ── 3. the retry / the cooldown ────────────────
                if reason == "sl" and pos_attempt == 1:
                    # The PDF allows exactly ONE second trade: "If SL,
                    # open second trades with same TP SL provided M1
                    # still above level 82 / below 16." A second SL
                    # does NOT license a third attempt.
                    r = float(rsi[i])
                    retry_ok = ((pos_side > 0 and r < buy_retry_level)
                                or (pos_side < 0 and r > sell_retry_level))
                    if retry_ok:
                        pending_side = pos_side
                        pending_sig_time = int(t_ns[i])
                        pending_attempt = 2
                    else:
                        pending_side = 0
                        cooldown_until_ns = int(t_ns[i]) + cooldown_ns
                else:   # TP -> no second trade; or attempt 2's SL -> stop
                    pending_side = 0
                    cooldown_until_ns = int(t_ns[i]) + cooldown_ns
                pos_side = 0

        # ── 4. a NEW signal, evaluated at this bar's close ─────
        if pending_side == 0 and pos_side == 0 and i < n - 1:
            if int(t_ns[i]) >= cooldown_until_ns:
                r = float(rsi[i])
                r_prev = float(rsi[i - 1])
                side = 0
                # "SELL when hit level 84, BUY when hit level 16" is a
                # CROSSING, not a level test: the RSI reaches 84 from
                # below (and 16 from above) and the trade fires. A level
                # test would re-fire every cooldown period for as long
                # as the RSI stayed beyond the level.
                if cross_trigger:
                    if r >= sell_level and r_prev < sell_level:
                        side = -1
                    elif r <= buy_level and r_prev > buy_level:
                        side = 1
                else:
                    if r >= sell_level:
                        side = -1
                    elif r <= buy_level:
                        side = 1
                if side != 0:
                    pending_side = side
                    pending_sig_time = int(t_ns[i])
                    pending_attempt = 1

    # ── close anything still open at the end of the data ────────
    if pos_side != 0:
        j = n - 1
        exit_px = float(b.close[j])
        mfe, mae = _excursions(b, pos_entry_bar, j, pos_entry_px,
                               pos_side, pos_sl, pos_tp)
        legs.append(_make_leg(
            strat_name, pos_side, pos_attempt, pos_sig_time,
            pos_entry_time, int(t_ns[j]), pos_entry_px, exit_px,
            pos_sl, pos_tp, "eod", float(j - pos_entry_bar), mfe, mae))
    return legs


# ─────────────────────────────────────────────────────────────────
# RULE 2 — H4 EMA limit orders
# ─────────────────────────────────────────────────────────────────

def h4_trend_state(b: EnzoBars, fast_n: int, mid_n: int, slow_n: int,
                   fast_src: str = "close", mid_src: str = "open",
                   slow_src: str = "close") -> dict[str, Any]:
    """The H4 EMA geometry: the trend, the cross flag and the yellow line.

    The canonical spec (user, 2026-10-25) is::

        blue   = EMA(fast_n)  of the H4 CLOSE
        red    = EMA(mid_n)   of the H4 OPEN
        yellow = EMA(slow_n)  of the H4 CLOSE

    The red line being an EMA of the OPEN is a DIFFERENT SERIES from an
    EMA of the close, so it changes the trend/cross logic materially.
    """
    src = {"open": b.h4_open, "high": b.h4_high, "low": b.h4_low,
           "close": b.h4_close}
    ef = ema(src[fast_src], fast_n)
    em = ema(src[mid_src], mid_n)
    ey = ema(src[slow_src], slow_n)
    diff = ef - em
    trend = np.zeros(len(b.h4_close), dtype=np.int8)
    trend[diff < 0] = -1      # blue below red -> DOWNTREND
    trend[diff > 0] = 1
    cross = np.zeros(len(b.h4_close), dtype=bool)
    cross[1:] = (np.sign(diff[1:]) != np.sign(diff[:-1]))
    return {"fast": ef, "mid": em, "slow": ey, "diff": diff,
            "trend": trend, "cross": cross,
            "fast_n": int(fast_n), "mid_n": int(mid_n), "slow_n": int(slow_n),
            "fast_src": fast_src, "mid_src": mid_src, "slow_src": slow_src}


def run_h4_ema_limit(b: EnzoBars, *, fast_n: int, mid_n: int, slow_n: int,
                     offset_usd: float = H4_LIMIT_OFFSET_USD,
                     tp_usd: float = H4_TP_USD, sl_usd: float = H4_SL_USD,
                     fast_src: str = "close", mid_src: str = "open",
                     slow_src: str = "close",
                     cross_cancels: bool = True,
                     reprice: bool = False,
                     strat_name: str = "h4_ema_limit",
                     stats: dict[str, Any] | None = None) -> list[EnzoLeg]:
    """Execute Rule 2 on the 1m grid.

    The H4 chart decides; the 1m grid executes.

    * At the CLOSE of each H4 bar the trend is read:
      - a CROSS of the blue and red lines makes the setup INVALID: any
        resting order is cancelled and no new order is armed;
      - otherwise, if there is no open position and no resting order,
        an order is armed:
          DOWNTREND (blue below red) -> SELL LIMIT at ``yellow + 13``
          UPTREND   (blue above red) -> BUY  LIMIT at ``yellow - 13``
    * The armed order is a RESTING LIMIT. It fills on the first 1m bar
      after the H4 close whose range touches the limit price. A gap
      through the limit fills at the bar's OPEN (pessimistic).
    * From the fill, TP and SL are $25 either side of the FILL price.
    * After a position closes, the next H4 bar may arm a new order.

    The order is armed ONCE and then RESTS: the PDF's worked example
    says the order was placed in the week of 19/10 and "only triggered
    after more than 24 hours", i.e. the resting limit is NOT re-priced
    at every H4 close while it waits.
    """
    n = len(b)
    if n < 3 or len(b.h4_close) < 3:
        return []
    st = h4_trend_state(b, fast_n, mid_n, slow_n, fast_src, mid_src,
                         slow_src)
    trend, cross, ey = st["trend"], st["cross"], st["slow"]
    o, h, lo = b.open, b.high, b.low
    t_ns = b.time_ns
    legs: list[EnzoLeg] = []

    # For each H4 bar k, the index of the LAST 1m bar inside it, and the
    # index of the FIRST 1m bar of the NEXT H4 bar (the moment the H4
    # bar closes and the new setup is read).
    h4_bar_end_1m = np.searchsorted(t_ns, b.h4_time_ns, side="right") - 1
    h4_bar_start_1m = np.searchsorted(t_ns, b.h4_time_ns, side="left")

    order_side = 0
    order_limit = 0.0
    order_sig_time = 0
    order_live_from = -1

    pos_side = 0
    pos_entry_px = 0.0
    pos_entry_bar = -1
    pos_entry_time = 0
    pos_sl = 0.0
    pos_tp = 0.0
    pos_sig_time = 0

    # The H4 close index -> the 1m bar index at which it happens.
    h4_close_1m = {}
    for k in range(len(b.h4_close)):
        j = int(h4_bar_start_1m[k + 1]) - 1 if k + 1 < len(b.h4_close) \
            else int(h4_bar_end_1m[k])
        h4_close_1m[j] = k

    # The funnel: where do the H4 setups die? The counters are the
    # ONLY way to tell "the strategy is selective" from "the code
    # silently drops the order".
    S = {"h4_bars": 0, "n_cross": 0, "n_armed": 0, "n_blocked_pos": 0,
         "n_blocked_order": 0, "n_cancel_cross": 0, "n_fill": 0,
         "n_cancel_expired": 0, "n_order_survived_cross": 0,
         "n_repriced": 0}

    for i in range(1, n):
        # ── 1. does a resting order fill on THIS 1m bar? ───────
        if order_side != 0 and i >= order_live_from:
            fill_px = None
            if order_side > 0:
                if float(o[i]) <= order_limit:
                    fill_px = float(o[i])       # gapped through -> open
                elif float(lo[i]) <= order_limit:
                    fill_px = order_limit
            else:
                if float(o[i]) >= order_limit:
                    fill_px = float(o[i])
                elif float(h[i]) >= order_limit:
                    fill_px = order_limit
            if fill_px is not None:
                S["n_fill"] += 1
                pos_side = order_side
                pos_entry_px = fill_px
                pos_entry_bar = i
                pos_entry_time = int(t_ns[i])
                pos_sig_time = order_sig_time
                if pos_side > 0:
                    pos_tp = pos_entry_px + tp_usd
                    pos_sl = pos_entry_px - sl_usd
                else:
                    pos_tp = pos_entry_px - tp_usd
                    pos_sl = pos_entry_px + sl_usd
                order_side = 0

        # ── 2. resolve an open position on this bar ─────────────
        if pos_side != 0:
            exit_px = 0.0
            reason = ""
            if pos_side > 0:
                gap_sl = float(o[i]) <= pos_sl
                gap_tp = float(o[i]) >= pos_tp
            else:
                gap_sl = float(o[i]) >= pos_sl
                gap_tp = float(o[i]) <= pos_tp
            if gap_sl:
                exit_px, reason = float(o[i]), "sl"
            elif gap_tp:
                exit_px, reason = float(o[i]), "tp"
            else:
                if pos_side > 0:
                    hit_sl = float(lo[i]) <= pos_sl
                    hit_tp = float(h[i]) >= pos_tp
                else:
                    hit_sl = float(h[i]) >= pos_sl
                    hit_tp = float(lo[i]) <= pos_tp
                if hit_sl:
                    exit_px, reason = pos_sl, "sl"
                elif hit_tp:
                    exit_px, reason = pos_tp, "tp"
            if reason:
                mfe, mae = _excursions(b, pos_entry_bar, i, pos_entry_px,
                                       pos_side, pos_sl, pos_tp)
                legs.append(_make_leg(
                    strat_name, pos_side, 1, pos_sig_time, pos_entry_time,
                    int(t_ns[i]), pos_entry_px, exit_px, pos_sl, pos_tp,
                    reason, float(i - pos_entry_bar), mfe, mae,
                    meta={"ema_fast": st["fast_n"], "ema_mid": st["mid_n"],
                          "ema_slow": st["slow_n"],
                          "ema_fast_src": st["fast_src"],
                          "ema_mid_src": st["mid_src"],
                          "ema_slow_src": st["slow_src"]}))
                pos_side = 0

        # ── 3. at an H4 CLOSE: arm / cancel the next order ──────
        k = h4_close_1m.get(i)
        if k is not None:
            S["h4_bars"] += 1
            if cross[k]:
                S["n_cross"] += 1
                if cross_cancels:
                    if order_side != 0:
                        S["n_cancel_cross"] += 1
                    order_side = 0         # INVALID -> cancel the order
                    if pos_side == 0 and trend[k] != 0 and not reprice:
                        # No cross-cancel semantics: the resting order
                        # survives, so nothing new is armed either.
                        S["n_blocked_order"] += 1
                else:
                    # The cross is INVALID for a NEW setup, but a
                    # RESTING order that was armed before the cross
                    # keeps working (the PDF's example is exactly this:
                    # the order was placed in the week of 19/10 and only
                    # filled "after more than 24 hours", i.e. it
                    # survived several H4 closes).
                    if order_side != 0:
                        S["n_order_survived_cross"] += 1
            if not cross[k]:
                if pos_side == 0 and order_side == 0 and trend[k] != 0:
                    side = 1 if trend[k] > 0 else -1
                    limit = (float(ey[k]) - offset_usd if side > 0
                             else float(ey[k]) + offset_usd)
                    order_side = side
                    order_limit = limit
                    order_sig_time = int(t_ns[i])
                    order_live_from = i + 1
                    S["n_armed"] += 1
                elif pos_side != 0:
                    S["n_blocked_pos"] += 1
                else:
                    S["n_blocked_order"] += 1
            elif cross_cancels:
                pass
            elif pos_side == 0 and order_side == 0 and trend[k] != 0:
                # cross_cancels is False and there is no resting order:
                # the cross bar itself cannot arm one (INVALID), but the
                # NEXT H4 bar can.
                S["n_blocked_order"] += 1

            if reprice and order_side != 0 and pos_side == 0:
                # Re-price the resting order at every H4 close. This is
                # the ALTERNATIVE lifecycle: the limit tracks the
                # yellow line instead of resting at the price it was
                # armed at.
                side = order_side
                new_limit = (float(ey[k]) - offset_usd if side > 0
                             else float(ey[k]) + offset_usd)
                if abs(new_limit - order_limit) > 1e-9:
                    S["n_repriced"] += 1
                order_limit = new_limit

    if pos_side != 0:
        j = n - 1
        exit_px = float(b.close[j])
        mfe, mae = _excursions(b, pos_entry_bar, j, pos_entry_px,
                               pos_side, pos_sl, pos_tp)
        legs.append(_make_leg(
            strat_name, pos_side, 1, pos_sig_time, pos_entry_time,
            int(t_ns[j]), pos_entry_px, exit_px, pos_sl, pos_tp, "eod",
            float(j - pos_entry_bar), mfe, mae,
            meta={"ema_fast": st["fast_n"], "ema_mid": st["mid_n"],
                  "ema_slow": st["slow_n"],
                  "ema_fast_src": st["fast_src"],
                  "ema_mid_src": st["mid_src"],
                  "ema_slow_src": st["slow_src"]}))
    if stats is not None:
        stats.update(S)
    return legs


# ─────────────────────────────────────────────────────────────────
# Leg construction
# ─────────────────────────────────────────────────────────────────

#: The per-side fee rate in bps, set by :func:`set_fee_spot`.
_BPS: list[float] = [0.0]


def set_fee_spot(spot: float) -> None:
    """Set the corpus spot used to derive the per-side fee rate."""
    if spot > 0:
        _BPS[0] = fee_bps_for(float(spot))


def _make_leg(strat: str, side: int, attempt: int, sig_time: int,
              entry_time: int, exit_time: int, entry_px: float,
              exit_px: float, sl_px: float, tp_px: float, reason: str,
              hold_min: float, mfe: float, mae: float,
              meta: dict[str, Any] | None = None) -> EnzoLeg:
    gross = side * (exit_px - entry_px) * OZ
    fee = (entry_px + exit_px) * OZ * (_BPS[0] / 10000.0)
    return EnzoLeg(
        strat=strat, side=int(side), attempt=int(attempt),
        signal_time_ns=int(sig_time), entry_time_ns=int(entry_time),
        exit_time_ns=int(exit_time), entry_price=float(entry_px),
        exit_price=float(exit_px), sl_price=float(sl_px),
        tp_price=float(tp_px), gross_usd=float(gross), fee_usd=float(fee),
        net_usd=float(gross - fee), exit_reason=reason,
        hold_min=float(hold_min), mfe_usd=float(mfe), mae_usd=float(mae),
        meta=dict(meta or {}),
    )


def _excursions(b: EnzoBars, entry_bar: int, exit_bar: int, entry_px: float,
                side: int, sl_px: float, tp_px: float
                ) -> tuple[float, float]:
    """MFE and MAE over the hold, capped by the trade's own stop / target.

    The EXIT bar's excursion is CAPPED by the trade's own stop and
    target: on the bar we exited we could not have booked anything
    beyond our target (favourable) and our loss was capped at our stop
    (adverse).

    VECTORISED: this is called once per leg, and the corpus produces
    tens of thousands of legs, so a Python loop over the hold would
    dominate the run time.
    """
    hi = b.high[entry_bar:exit_bar + 1]
    lo = b.low[entry_bar:exit_bar + 1]
    if hi.size == 0:
        return 0.0, 0.0
    if side > 0:
        fav = hi.copy()
        adv = lo.copy()
        fav[-1] = min(float(hi[-1]), tp_px)
        adv[-1] = max(float(lo[-1]), sl_px)
        mfe = float(fav.max()) - entry_px
        mae = entry_px - float(adv.min())
    else:
        fav = lo.copy()
        adv = hi.copy()
        fav[-1] = min(float(lo[-1]), tp_px)
        adv[-1] = max(float(hi[-1]), sl_px)
        mfe = entry_px - float(fav.min())
        mae = float(adv.max()) - entry_px
    return max(mfe, 0.0) * OZ, max(mae, 0.0) * OZ


# ─────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────

def _mean_se(x: np.ndarray) -> tuple[float, float]:
    n = int(x.size)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return float(x[0]), float("nan")
    return float(x.mean()), float(x.std(ddof=1) / math.sqrt(n))


def score_legs(legs: Sequence[EnzoLeg], b: EnzoBars | None = None, *,
               trading_minutes: int | None = None) -> dict[str, Any]:
    """The scorecard for one arm of one strategy."""
    n = len(legs)
    empty: dict[str, Any] = {
        "n": 0, "gross_total": 0.0, "fees_total": 0.0, "net_total": 0.0,
        "gross_per_leg": 0.0, "fee_per_leg": 0.0,
        "gross_per_leg_over_fee": 0.0, "hit_rate": 0.0, "payoff": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0, "break_even_hit": 0.0,
        "edge_pp": 0.0, "drift_total": 0.0, "excess_total": 0.0,
        "excess_per_leg": 0.0, "t_excess": 0.0, "n_long": 0, "n_short": 0,
        "gross_long": 0.0, "gross_short": 0.0, "n_attempt2": 0,
        "median_hold_min": 0.0, "mean_hold_min": 0.0,
        "exits": {}, "sides": {},
    }
    if n == 0:
        return empty

    gross = np.array([lg.gross_usd for lg in legs])
    fee = np.array([lg.fee_usd for lg in legs])
    side = np.array([lg.side for lg in legs], dtype=np.int64)
    hold = np.array([lg.hold_min for lg in legs])

    wins = gross[gross > 0]
    losses = gross[gross <= 0]
    aw = float(wins.mean()) if wins.size else 0.0
    al = abs(float(losses.mean())) if losses.size else 0.0
    payoff = (aw / al) if al > 0 else 0.0
    p_be = (al / (aw + al)) if (aw + al) > 0 else 0.0
    hit = float((gross > 0).mean())

    exits: dict[str, int] = {}
    for lg in legs:
        exits[lg.exit_reason] = exits.get(lg.exit_reason, 0) + 1

    sides: dict[str, dict[str, Any]] = {}
    for label, sel in (("long", side > 0), ("short", side < 0)):
        if not sel.any():
            continue
        g = gross[sel]
        sides[label] = {
            "n": int(sel.sum()),
            "gross": float(g.sum()),
            "gross_per_leg": float(g.mean()),
            "hit_rate": float((g > 0).mean()),
        }

    return {
        "n": n,
        "gross_total": float(gross.sum()),
        "fees_total": float(fee.sum()),
        "net_total": float((gross - fee).sum()),
        "gross_per_leg": float(gross.mean()),
        "fee_per_leg": float(fee.mean()),
        "gross_per_leg_over_fee": float(gross.mean() / fee.mean())
        if fee.mean() > 0 else 0.0,
        "hit_rate": hit,
        "payoff": payoff,
        "avg_win": aw,
        "avg_loss": al,
        "break_even_hit": p_be,
        "edge_pp": 100.0 * (hit - p_be),
        "n_long": int((side > 0).sum()),
        "n_short": int((side < 0).sum()),
        "gross_long": float(gross[side > 0].sum()),
        "gross_short": float(gross[side < 0].sum()),
        "n_attempt2": int(sum(1 for lg in legs if lg.attempt == 2)),
        "median_hold_min": float(np.median(hold)),
        "mean_hold_min": float(hold.mean()),
        "exits": exits,
        "sides": sides,
        "drift_total": 0.0,
        "excess_total": 0.0,
        "excess_per_leg": 0.0,
        "t_excess": 0.0,
    }


def drift_excess(legs: Sequence[EnzoLeg], b: EnzoBars) -> dict[str, Any]:
    """The repo's exact algebraic identity, on the 1m closes.

    ``drift``  what a passive hold of the same side over the same
               window would have earned.
    ``excess`` the part attributable to the strategy's own decisions.
    """
    if not legs:
        return {"drift_total": 0.0, "excess_total": 0.0,
                "excess_per_leg": 0.0, "t_excess": 0.0}
    drift = np.empty(len(legs), dtype=np.float64)
    for i, lg in enumerate(legs):
        j = _bar_of(b, lg.exit_time_ns)
        k = _bar_of(b, lg.entry_time_ns)
        drift[i] = lg.side * (float(b.close[j]) - float(b.close[k])) * OZ
    gross = np.array([lg.gross_usd for lg in legs])
    excess = gross - drift
    me, se = _mean_se(excess)
    return {
        "drift_total": float(drift.sum()),
        "excess_total": float(excess.sum()),
        "excess_per_leg": float(me),
        "t_excess": float(me / se) if (se and math.isfinite(se) and se > 0)
        else 0.0,
    }


def _bar_of(b: EnzoBars, t_ns: int) -> int:
    """The index of the 1m bar whose bucket contains ``t_ns``."""
    i = int(np.searchsorted(b.time_ns, t_ns, side="right")) - 1
    return max(0, min(len(b) - 1, i))


def randomize_sides(legs: Sequence[EnzoLeg], seed: int) -> list[EnzoLeg]:
    """The EXCHANGEABLE placebo: same schedule, random side.

    The entry bar, entry price, exit bar, exit price, time in market and
    fee are ALL held fixed; only the direction of each leg changes.
    Under the null "the direction call adds nothing beyond the
    schedule", the sign-flipped legs are exchangeable with the real
    ones. This is the textbook sign-flip randomisation test, and it is
    the repo's hard rule for a control: the execution model must match.
    """
    rng = np.random.default_rng(seed)
    out: list[EnzoLeg] = []
    for lg in legs:
        s = 1 if rng.random() < 0.5 else -1
        gross = s * (lg.exit_price - lg.entry_price) * OZ
        out.append(EnzoLeg(
            strat=lg.strat, side=s, attempt=lg.attempt,
            signal_time_ns=lg.signal_time_ns, entry_time_ns=lg.entry_time_ns,
            exit_time_ns=lg.exit_time_ns, entry_price=lg.entry_price,
            exit_price=lg.exit_price, sl_price=lg.sl_price,
            tp_price=lg.tp_price, gross_usd=gross, fee_usd=lg.fee_usd,
            net_usd=gross - lg.fee_usd, exit_reason=lg.exit_reason,
            hold_min=lg.hold_min, mfe_usd=lg.mfe_usd, mae_usd=lg.mae_usd,
            meta=dict(lg.meta)))
    return out


__all__ = [
    "OZ", "ROUND_TRIP_USD", "RSI_PERIOD", "RSI_SELL_LEVEL", "RSI_BUY_LEVEL",
    "COOLDOWN_MIN", "M1_EXIT_CELL_LIST", "H4_TP_USD", "H4_SL_USD",
    "H4_LIMIT_OFFSET_USD", "H4_EMA_PERIOD_GRID", "H4_EMA_PERIODS",
    "H4_EMA_SOURCES",
    "EnzoBars", "EnzoLeg", "build_enzo_bars", "wilder_rsi", "ema",
    "fee_bps_for", "set_fee_spot", "run_m1_rsi", "run_h4_ema_limit",
    "h4_trend_state", "score_legs", "drift_excess", "randomize_sides",
]
