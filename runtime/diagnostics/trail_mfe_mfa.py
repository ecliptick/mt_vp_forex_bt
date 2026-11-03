"""MFE / MFA analysis for the trailing stop — what SHOULD the trail do?

WHY THIS EXISTS
===============

The trailing-stop sweep (2026-11-03, 17 cells on 2025-01) measured the
trail and found that **every** trailing configuration lost money versus
``trail_mode='off'``:

    off        n=1058  gross $248.96  net  -$240.40  PF 0.936
    combined   n=1053  gross $124.28  net  -$370.19  PF 0.527
    chandelier n=1053  gross  $74.58  net  -$419.90  PF 0.632
    giveback   n=1053  gross $115.01  net  -$379.46  PF 0.515

Read naively that says "the trailing stop is harmful — turn it off". But
the sweep moved FOUR knobs at once and never asked the question that
actually determines the parameters:

    **How much favourable excursion does a leg actually have, and how
    much of it survives the round trip?**

MFE (maximum favourable excursion) is the ceiling on what a trailing
stop can bank: a trail cannot capture more than the peak the trade
reached. MFA (maximum adverse excursion) is the floor on where a trail
can be armed without being stopped first. The two together say whether
a trailing stop has any room to work at all — and if it does, which
activation/distance geometry captures the most of it.

THE METHOD
==========

1. Run the recipe with ``trail_mode='off'`` so the EXIT is decided by
   the structural SL/TP alone. That gives a clean leg list.
2. For every leg, measure MFE / MFA on the ACTUAL bar path the engine
   walked between the fill and the exit — the same path, including the
   intrabar geometry the fill model saw.
3. Express MFE and MFA in units of the **1m ATR frozen at the fill** —
   the same yardstick the trail itself uses. That makes the numbers
   regime-invariant: "this trade ran 5.9 ATRs in my favour" transfers
   across volatility regimes; "$6.09 of MFE" does not.
4. REPLAY the candidate trail rules over the SAME path and report what
   each configuration would have banked, WITHOUT re-running the engine.

Step 4 is the point. It is a **replay**, not a new backtest, so it is
exact for the trail's own arithmetic (the trail reads only the price
path, the peak and the ATR) and it costs milliseconds per candidate.

THE REPLAY IS VALIDATED
=======================

A replay that disagrees with the engine is worthless, so the diagnostic
validates itself before reporting anything:

  * **The no-fire check.** The engine is re-run with an activation of
    1e9 ATR, so the trail can never arm. The resulting book must be
    IDENTICAL to the trail-off book, and the replay must reproduce
    every leg's exit price exactly. This validates the path handling,
    the SL/TP geometry and the exit precedence.
  * **The arithmetic check.** For a configuration that DOES fire, the
    replay's per-leg exit price is compared with the engine's for the
    legs that both books share.

WHAT THE REPLAY DOES *NOT* CLAIM
================================

The replay holds the leg set FIXED. The engine does not:

  * a trail-closed leg frees a same-direction slot, so a LATER signal
    is admitted that would not have been;
  * a trail-closed leg is no longer in the book, so it cannot be netted
    by contra-close.

So the replay answers "given these legs, which trail geometry banks the
most?" and the engine answers "given a live book, what does the whole
recipe earn?". **The replay RANKS; the engine DECIDES.** The output
prints the book-level delta so the gap is never invisible.

THE ATR CADENCE (2026-11-04)
=============================

The trail's ATR is **1m Wilder ATR(14)** — NOT 1s. Measured on the
2025-01 corpus:

    ATR_1m (Wilder 14)  p10 $0.447  p50 $0.720  p90 $1.256
    ATR_1s (len 1200)    p10 $0.049  p50 $0.071  p90 $0.126

The 1m figure is **10.2x** wider. A 1s ATR is 20 minutes of average
one-second true range, which is not a volatility measure — it is a
tick-noise measure. A trail built on it would sit a few cents from
price and be triggered by noise on every micro-tick.

The engine already uses the 1m basis
(``bt/_engine/vp_multi_target_engine.py::_build_atr_1m_forward_filled``)
and this diagnostic calls the SAME function, so the numbers are
comparable to what the engine does by construction.

Run:
    python -m runtime.diagnostics.trail_mfe_mfa
    python -m runtime.diagnostics.trail_mfe_mfa --expand-prev-day 1.0
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.timebase import to_ns_arr  # noqa: E402
from bt._engine.vp_multi_target_engine import (  # noqa: E402
    _build_atr_1m_forward_filled,
    _resolve_exit_price,
)
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.results import registry  # noqa: E402
from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache,
    load_bars,
)

DEFAULT_DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
DEFAULT_START = "2025-01-01"
DEFAULT_END = "2025-02-01"

# The ATR length the trail's yardstick uses: 1m Wilder ATR(14).
ATR_1M_LENGTH = 14

# The canonical book's notional size in ounces (0.01 lots x 100).
OZ = FOREX_LOTS * FOREX_CONTRACT_SIZE  # 1.0 oz

# The exit reasons this diagnostic understands.
_KNOWN_REASONS = ("tp", "sl", "trail_stop", "eod", "contra_close",
                  "replaced_by_new_signal", "inv", "soft_sl")


# ─────────────────────────────────────────────────────────────────────
# The leg record
# ─────────────────────────────────────────────────────────────────────
class Leg:
    """One closed trade, with the path it walked."""

    __slots__ = (
        "trade_id", "side", "entry_px", "exit_px", "entry_bar", "exit_bar",
        "gross", "fee", "exit_reason", "mfe", "mfa", "atr_1m",
        "sl_px", "tp_px", "hold_secs", "qty_oz",
    )

    def __init__(self, **kw: object) -> None:
        for k in self.__slots__:
            setattr(self, k, kw.get(k))


# ─────────────────────────────────────────────────────────────────────
# Excursion measurement on the ACTUAL bar path
# ─────────────────────────────────────────────────────────────────────
def _leg_path(
    high: np.ndarray,
    low: np.ndarray,
    open_: np.ndarray,
    entry_bar: int,
    exit_bar: int,
    side: int,
    entry_px: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """The bars from the fill to the exit, plus the running peak.

    ``peak`` is the running max (long) / min (short) of the bar
    extremes since the fill — EXACTLY what the engine's trail tracks.

    The peak is SEEDED WITH THE ENTRY PRICE. The engine initialises
    ``OpenTrade.peak_price = entry_price`` and only updates it when a
    bar's extreme goes BEYOND it:

        if ot.direction > 0:
            if b_high > ot.peak_price:
                ot.peak_price = float(b_high)

    so on a fill bar whose high is still below the entry (a long
    entered on a touch fill at the zone's LOW edge), the engine's peak
    stays at the ENTRY PRICE while a naive ``maximum.accumulate`` would
    report the bar's high. Seeding with the entry price reproduces the
    engine exactly.
    """
    n = len(high)
    b0 = max(0, int(entry_bar))
    b1 = min(n - 1, int(exit_bar))
    if b1 < b0:
        empty = np.zeros(0, dtype=np.float64)
        return empty, empty, empty, empty
    hi = high[b0:b1 + 1]
    lo = low[b0:b1 + 1]
    op = open_[b0:b1 + 1]
    if side > 0:
        peak = np.maximum(np.maximum.accumulate(hi), entry_px)
    else:
        peak = np.minimum(np.minimum.accumulate(lo), entry_px)
    return hi, lo, op, peak


def _excursions(
    hi: np.ndarray,
    lo: np.ndarray,
    entry_px: float,
    side: int,
    peak: np.ndarray,
) -> tuple[float, float]:
    """MFE and MFA in $/oz, signed positive."""
    if hi.size == 0:
        return 0.0, 0.0
    if side > 0:
        mfe = float(np.max(hi)) - entry_px
        mfa = entry_px - float(np.min(lo))
    else:
        mfe = entry_px - float(np.min(lo))
        mfa = float(np.max(hi)) - entry_px
    return max(0.0, mfe), max(0.0, mfa)


# ─────────────────────────────────────────────────────────────────────
# The vectorised trail replay
# ─────────────────────────────────────────────────────────────────────
def _replay_trail(
    *,
    hi: np.ndarray,
    lo: np.ndarray,
    op: np.ndarray,
    peak: np.ndarray,
    entry_px: float,
    side: int,
    atr_1m: float,
    sl_px: float,
    tp_px: float,
    mode: str,
    atr_mult: float,
    activation_mult: float,
    giveback_frac: float,
    fill_model: str,
    eod_px: float,
) -> tuple[str, float, float]:
    """Replay one trail configuration over one leg's path.

    Returns ``(exit_reason, exit_price, gross_px)``.

    This mirrors the engine's single-target path EXACTLY for the trail's
    own arithmetic:

      * the peak tracks the bar extremes since the fill,
      * the trail arms once MFE >= activation_mult * ATR_1m,
      * the exit level is the TIGHTER of chandelier and giveback
        (higher for a long, lower for a short),
      * the level RATCHETS — it never loosens,
      * the STOP wins a tie against the target and against the trail,
      * a gap through the level books at the bar's OPEN.

    The RATCHET is exact without extra work: for a long both the
    chandelier level (``peak - k*ATR``) and the giveback level
    (``entry + (peak-entry)*(1-f)``) are MONOTONE NON-DECREASING in the
    peak, and the peak is monotone non-decreasing. So the level at bar
    i is already the ratcheted value. (For a short both are monotone
    non-increasing and the tighter one is the lower — the engine's own
    rule, and the same argument applies in mirror.)

    The per-bar precedence is resolved by taking the FIRST bar at which
    anything fires, with the priority sl > trail > tp — which is what
    the engine's ``if sl_hit: ... elif trail_hit: ... elif tp_hit:``
    does, one bar at a time.
    """
    n = hi.size
    if n == 0:
        # No bars on this path — the leg ends where it ended, at the
        # engine's own exit price. The gross MUST be the engine's, not
        # zero: the reconciliation check (b2) compares the replay's
        # baseline against the engine's, and a zeroed eod leg shows up
        # there as a gross mismatch.
        return "eod", eod_px, (eod_px - entry_px) * side

    # ── SL / TP hit masks ──
    if side > 0:
        sl_hit = lo <= sl_px
        tp_hit = hi >= tp_px
    else:
        sl_hit = hi >= sl_px
        tp_hit = lo <= tp_px

    # ── The trail ──
    trail_hit = np.zeros(n, dtype=bool)
    trail_lvl = np.zeros(n, dtype=np.float64)
    if atr_1m > 0.0 and mode != "off":
        mfe = (peak - entry_px) if side > 0 else (entry_px - peak)
        armed = np.flatnonzero(mfe >= activation_mult * atr_1m)
        if armed.size:
            a0 = int(armed[0])
            if mode in ("chandelier", "combined"):
                chand = peak - side * atr_mult * atr_1m
            else:
                chand = None
            if mode in ("giveback", "combined"):
                give = entry_px + (peak - entry_px) * (1.0 - giveback_frac)
            else:
                give = None
            if mode == "chandelier":
                lvl = chand
            elif mode == "giveback":
                lvl = give
            else:
                if chand is None:
                    lvl = give
                elif give is None:
                    lvl = chand
                else:
                    lvl = (np.maximum(chand, give) if side > 0
                           else np.minimum(chand, give))
            trail_lvl[a0:] = lvl[a0:]
            if side > 0:
                trail_hit[a0:] = lo[a0:] <= lvl[a0:]
            else:
                trail_hit[a0:] = hi[a0:] >= lvl[a0:]

    def _first(mask: np.ndarray) -> int:
        idx = np.flatnonzero(mask)
        return int(idx[0]) if idx.size else -1

    i_sl = _first(sl_hit)
    i_tr = _first(trail_hit)
    i_tp = _first(tp_hit)

    # First bar wins; ties go to the stop (sl), then the trail, then tp.
    best = min([x for x in (i_sl, i_tr, i_tp) if x >= 0], default=-1)
    if best < 0:
        # Nothing fired — the leg ran to the end of the path at the
        # engine's exit price. Same rule as the empty-path branch: the
        # gross is the engine's, not zero.
        return "eod", eod_px, (eod_px - entry_px) * side

    if i_sl == best:
        px = _resolve_exit_price(
            level=sl_px, b_open=float(op[best]), b_low=float(lo[best]),
            b_high=float(hi[best]), direction=side, is_stop=True,
            fill_model=fill_model,
        )
        return "sl", px, (px - entry_px) * side
    if i_tr == best:
        px = _resolve_exit_price(
            level=float(trail_lvl[best]), b_open=float(op[best]),
            b_low=float(lo[best]), b_high=float(hi[best]), direction=side,
            is_stop=True, fill_model=fill_model,
        )
        return "trail_stop", px, (px - entry_px) * side
    px = _resolve_exit_price(
        level=tp_px, b_open=float(op[best]), b_low=float(lo[best]),
        b_high=float(hi[best]), direction=side, is_stop=False,
        fill_model=fill_model,
    )
    return "tp", px, (px - entry_px) * side


def _atr_1s_wilder(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    length: int = 14,
) -> np.ndarray:
    """Wilder ATR(14) computed on the RAW 1s grid.

    This is the COUNTERFACTUAL yardstick — what the trail would use if
    it were fed 1s bars instead of 1m candles. It is NOT what the
    engine does; it exists so the "1m vs 1s" question is measured
    rather than asserted.

    Causal: the ATR at bar ``i`` uses only bars ``<= i``.
    """
    n = int(close.shape[0])
    out = np.zeros(n, dtype=np.float64)
    if n == 0:
        return out
    prev = np.concatenate([[close[0]], close[:-1]])
    tr = np.maximum.reduce([
        high - low,
        np.abs(high - prev),
        np.abs(low - prev),
    ])
    if n < length:
        return out
    out[length - 1] = float(np.mean(tr[:length]))
    for i in range(length, n):
        out[i] = (out[i - 1] * (length - 1) + tr[i]) / length
    return out


def _replay_net(
    legs: list["Leg"],
    paths: list[tuple],
    is_modelled: np.ndarray,
    atr_array: np.ndarray,
    *,
    mode: str,
    k: float,
    act: float,
    gb: float,
    fill_model: str,
    leg_fee,
    want_breakdown: bool = False,
):
    """Replay the grid against a DIFFERENT ATR yardstick.

    Same legs, same paths, same arithmetic — only the ATR array
    changes. Returns the net P&L so the two bases can be compared
    directly.

    With ``want_breakdown`` it also returns the exit-reason counts and
    the per-leg delta vs the baseline, which is what reveals what a
    different yardstick actually DOES to the book.
    """
    tot_gross = 0.0
    tot_fee = 0.0
    n_trail = n_tp = n_sl = 0
    delta = 0.0
    for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
        if not modelled:
            tot_gross += l.gross
            tot_fee += l.fee
            continue
        eb = int(l.entry_bar)
        atr_v = float(atr_array[eb]) if 0 <= eb < len(atr_array) else 0.0
        reason, px, gross_px = _replay_trail(
            hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
            side=l.side, atr_1m=atr_v, sl_px=l.sl_px, tp_px=l.tp_px,
            mode=mode, atr_mult=k, activation_mult=act,
            giveback_frac=gb, fill_model=fill_model, eod_px=l.exit_px,
        )
        tot_gross += gross_px * l.qty_oz
        tot_fee += leg_fee(l.entry_px, px) * (l.qty_oz / OZ)
        if reason == "trail_stop":
            n_trail += 1
        elif reason == "tp":
            n_tp += 1
        elif reason == "sl":
            n_sl += 1
        if want_breakdown:
            delta += gross_px * l.qty_oz - l.gross
    net = tot_gross - tot_fee
    if want_breakdown:
        return net, {"n_trail": n_trail, "n_tp": n_tp, "n_sl": n_sl,
                     "delta": delta}
    return net


def _width_thresholds(
    legs: list["Leg"],
    paths: list[tuple],
    is_modelled: np.ndarray,
    act: float,
) -> list[dict[str, object]]:
    """Per-leg geometric thresholds for the trail's WIDTH.

    For every bar ``b`` the trail fires only if the width is at most
    ``need[b]``:

      * chandelier (long): the level is ``peak - k*ATR`` and it fires
        when ``low <= peak - k*ATR``, i.e. when
        ``k <= (peak - low)/ATR``;
      * giveback (long): the level is ``entry + mfe*(1-f)`` and it fires
        when ``low <=`` that, i.e. when
        ``f <= (peak - low)/mfe``.

    So ``need[b]`` is the WIDEST width at which bar ``b`` still fires.
    A leg's threshold is the MAXIMUM ``need`` over the bars it lives
    through — i.e. the trail only fires on that leg once the width
    drops to (or below) that value.

    Two thresholds come out of it:

      * **k_preserve** (TP legs): the width must be STRICTLY GREATER
        than this or the trail cuts the TP.
      * **k_rescue** (SL legs): the width must be LESS THAN OR EQUAL to
        this or the trail cannot rescue the loser.

    The armed mask makes the bars a SUFFIX (MFE is monotone), so the
    running maximum is taken over the suffix only.
    """
    out: list[dict[str, object]] = []
    for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
        if not modelled or hi.size == 0:
            continue
        atr = float(l.atr_1m)
        if atr <= 0.0:
            continue
        if l.side > 0:
            mfe = peak - l.entry_px
            retrace = peak - lo
        else:
            mfe = l.entry_px - peak
            retrace = hi - peak
        armed = mfe >= act * atr
        idx = np.flatnonzero(armed)
        if idx.size == 0:
            # Never arms -> the trail cannot touch this leg at any width.
            out.append({"leg": l, "k_preserve": None, "k_rescue": None,
                        "f_preserve": None, "f_rescue": None})
            continue
        b0 = int(idx[0])
        n = hi.size
        # The width at which each bar fires.
        need_k = (retrace / atr)[b0:]
        with np.errstate(divide="ignore", invalid="ignore"):
            need_f = np.where(
                mfe[b0:] > 1e-12,
                retrace[b0:] / mfe[b0:],
                np.inf,
            )
        cmax_k = np.maximum.accumulate(need_k)
        cmax_f = np.maximum.accumulate(need_f)
        rec: dict[str, object] = {"leg": l}
        if l.exit_reason == "tp":
            # Cut iff the trail fires on or before the TP bar.
            rec["k_preserve"] = float(cmax_k[-1])
            rec["f_preserve"] = float(cmax_f[-1])
            rec["k_rescue"] = None
            rec["f_rescue"] = None
        elif l.exit_reason == "sl":
            # Rescue iff the trail fires STRICTLY before the SL bar.
            rec["k_rescue"] = float(cmax_k[-2]) if cmax_k.size >= 2 else 0.0
            rec["f_rescue"] = float(cmax_f[-2]) if cmax_f.size >= 2 else 0.0
            rec["k_preserve"] = None
            rec["f_preserve"] = None
        else:
            continue
        rec["armed_at"] = b0
        out.append(rec)
    return out


# ─────────────────────────────────────────────────────────────────────
# The run
# ─────────────────────────────────────────────────────────────────────
def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--scope", type=str, default="trail_mfe_mfa")
    ap.add_argument(
        "--expand-prev-day", type=float, default=0.0,
        help="vol_state_min_expand_prev_day for the base recipe. "
             "0.0 reproduces the book the 2026-11-03 trail sweep "
             "measured (n=1058 legs on 2025-01).")
    args = ap.parse_args()

    print("=" * 78)
    print("TRAIL MFE/MFA ANALYSIS")
    print("=" * 78)
    print(f"window {args.start} -> {args.end}   data {args.data.name}")
    print(f"base recipe: trail_mode=off, "
          f"vol_state_min_expand_prev_day={args.expand_prev_day}")
    print("ATR basis: 1m Wilder ATR(14), forward-filled onto the 1s grid")
    print("           (the SAME function the engine's trail uses)")
    print()

    bars = load_bars(args.data, args.start, args.end)
    if bars.empty:
        raise SystemExit("no bars in window")
    art = ArtifactCache(args.data, args.scope, args.start, args.end).build_or_load(bars)

    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    open_ = bars["open"].to_numpy(dtype=np.float64)
    close = bars["close"].to_numpy(dtype=np.float64)
    times = to_ns_arr(bars, "time")

    t0 = time.perf_counter()
    atr_1m = _build_atr_1m_forward_filled(
        high, low, close, times, length=ATR_1M_LENGTH)
    a = atr_1m[atr_1m > 0]
    print(f"ATR_1m built in {time.perf_counter() - t0:.1f}s  "
          f"(bars={len(bars):,})")
    if a.size:
        print(f"  ATR_1m distribution: p10 ${np.percentile(a, 10):.3f}  "
              f"p50 ${np.percentile(a, 50):.3f}  p90 ${np.percentile(a, 90):.3f}")
    from bt._engine.ict_signals import compute_simple_atr
    atr_1s = compute_simple_atr(high, low, close, length=1200)
    b = atr_1s[np.isfinite(atr_1s)]
    if b.size and a.size:
        print(f"  ATR_1s (len 1200):  p10 ${np.percentile(b, 10):.3f}  "
              f"p50 ${np.percentile(b, 50):.3f}  p90 ${np.percentile(b, 90):.3f}")
        print(f"  -> the 1m basis is {np.median(a) / np.median(b):.1f}x wider. "
              "The trail already uses the 1m basis.")
    print()

    spot = float(np.median(close))
    base_over: dict[str, object] = {
        "trail_mode": "off",
        "vol_state_min_expand_prev_day": float(args.expand_prev_day),
    }

    def _run(over: dict[str, object]):
        ek = build_engine_kwargs(
            spot=spot,
            swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art.get("structure"), vol_state=art.get("vol_state"),
            **over,
        )
        t = time.perf_counter()
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek, record_equity_curve=False,
        )
        return list(res.trades), ek, time.perf_counter() - t

    trades, ek, wall = _run(base_over)
    fee_bps = float(ek.get("taker_fee_bps", 0.0))
    fill_model = str(ek.get("fill_model", "pessimistic"))
    print(f"engine (trail OFF): {len(trades)} legs in {wall:.1f}s   "
          f"taker_fee_bps={fee_bps:.4f}  fill_model={fill_model}")

    reasons: dict[str, int] = {}
    for t in trades:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    print(f"  exit reasons: {reasons}")
    print()

    # ── The engine's own fee model, reused exactly ──
    def leg_fee(entry_px: float, exit_px: float) -> float:
        """The engine's fee formula, for a FULL 1-oz leg.

        ``qty`` scales it, and the engine's fee uses the leg's OWN
        quantity — a leg that was partially netted by contra-close
        carries ``qty_oz = 0.5`` and is charged on half.
        """
        return (abs(exit_px + entry_px) * 0.5 * OZ
                * (fee_bps / 10000.0) * 2.0)

    # ── Build the leg list with excursions ──
    legs: list[Leg] = []
    for t in trades:
        eb = int(t.entry_bar)
        xb = int(t.exit_bar)
        side = int(t.direction)
        entry_px = float(t.entry_price)
        hi, lo, op, peak = _leg_path(high, low, open_, eb, xb, side, entry_px)
        mfe, mfa = _excursions(hi, lo, entry_px, side, peak)
        atr_v = float(atr_1m[eb]) if 0 <= eb < len(atr_1m) else 0.0
        legs.append(Leg(
            trade_id=int(t.trade_id),
            side=side,
            qty_oz=float(t.qty_oz),
            entry_px=entry_px,
            exit_px=float(t.exit_price),
            entry_bar=eb,
            exit_bar=xb,
            gross=float(t.pnl_gross_usd),
            fee=float(t.fee_usd),
            exit_reason=str(t.exit_reason),
            mfe=mfe,
            mfa=mfa,
            atr_1m=atr_v,
            sl_px=float(t.stop_price),
            tp_px=float(t.target_price),
            hold_secs=float(t.hold_secs),
        ))

    if not legs:
        raise SystemExit("no legs")

    n = len(legs)
    gross_off = sum(l.gross for l in legs)
    fee_off = sum(l.fee for l in legs)

    # ── The paths, cached for the replay ──
    paths: list[tuple] = []
    for l in legs:
        hi, lo, op, peak = _leg_path(high, low, open_, l.entry_bar,
                                      l.exit_bar, l.side, l.entry_px)
        paths.append((hi, lo, op, peak))

    mfe_usd = np.array([l.mfe for l in legs], dtype=np.float64)
    mfa_usd = np.array([l.mfa for l in legs], dtype=np.float64)
    atr_arr = np.array([l.atr_1m for l in legs], dtype=np.float64)
    side_arr = np.array([l.side for l in legs], dtype=np.int64)
    gross_arr = np.array([l.gross for l in legs], dtype=np.float64)

    with np.errstate(divide="ignore", invalid="ignore"):
        mfe_atr = np.where(atr_arr > 0, mfe_usd / atr_arr, np.nan)
        mfa_atr = np.where(atr_arr > 0, mfa_usd / atr_arr, np.nan)

    # ================================================================
    # VALIDATION — the replay must agree with the engine
    # ================================================================
    print("=" * 78)
    print("0. VALIDATION  (the replay must agree with the engine)")
    print("=" * 78)

    # (a) A trail that can NEVER arm -> the book must be identical.
    nofire_trades, _, _ = _run({**base_over, "trail_mode": "combined",
                                "trail_activation_mult": 1e9})
    same_len = len(nofire_trades) == len(trades)
    same_ids = same_len and all(
        a.trade_id == b.trade_id and a.exit_bar == b.exit_bar
        and abs(a.exit_price - b.exit_price) < 1e-9
        for a, b in zip(nofire_trades, trades))
    print(f"  (a) never-arm trail: legs {len(nofire_trades)} vs "
          f"{len(trades)} (trail off) -> "
          f"{'IDENTICAL BOOK' if same_ids else 'MISMATCH'}")
    if not same_ids:
        print("      !! the book differs -> the replay's book-level numbers "
              "cannot be compared to the engine's")

    # (b) The replay must reproduce the trail-off book exactly.
    #
    # EXCEPTION: legs closed by a mechanism the replay does not model.
    # ``contra_close`` books the exit at the MARKET price (it nets two
    # opposing positions), and ``replaced_by_new_signal`` books it at
    # the replacing signal's price. Neither is an SL or a TP, so the
    # replay's path logic cannot reproduce them — and neither can the
    # trail change them (they were closed by something else). They are
    # excluded from the exact-match test and HELD AT THE ENGINE'S
    # ACTUAL P&L in the replay grid, which is the only honest
    # treatment: the trail did not touch those legs.
    unmodelled = ("contra_close", "replaced_by_new_signal",
                  "soft_sl", "inv")
    is_modelled = np.array([l.exit_reason not in unmodelled for l in legs])
    n_unmodelled = int((~is_modelled).sum())

    mism = 0
    mism_reasons: dict[str, int] = {}
    for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
        if not modelled:
            continue
        reason, px, _g = _replay_trail(
            hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
            side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px, tp_px=l.tp_px,
            mode="off", atr_mult=0.0, activation_mult=0.0,
            giveback_frac=0.0, fill_model=fill_model, eod_px=l.exit_px,
        )
        if reason != l.exit_reason or abs(px - l.exit_px) > 1e-9:
            mism += 1
            mism_reasons[l.exit_reason] = mism_reasons.get(l.exit_reason, 0) + 1
    n_modelled = n - n_unmodelled
    print(f"  (b) replay reproduces the trail-off book: "
          f"{n_modelled - mism}/{n_modelled} legs exact  "
          f"{'OK' if mism == 0 else '*** MISMATCH ***'}")
    if n_unmodelled:
        print(f"      ({n_unmodelled} legs excluded — closed by a mechanism "
              "the replay does not model: "
              + ", ".join(f"{r} x{sum(1 for l in legs if l.exit_reason == r)}"
                          for r in unmodelled
                          if any(l.exit_reason == r for l in legs)) + ")")
    if mism:
        print(f"      !! mismatched exit reasons: {mism_reasons} — "
              "every number below is suspect")

    # (b2) The REPLAY's baseline (gross/fees) must equal the ENGINE's
    # baseline. This is the end-to-end check: it catches the qty and
    # fee mistakes that a per-leg price match can hide.
    print("  (b2) replay baseline vs engine baseline:")
    print(f"      engine : gross ${gross_off:>10,.4f}  fees ${fee_off:>9,.4f}  "
          f"net ${gross_off - fee_off:>10,.4f}")
    print(f"      replay : (printed in the grid as the 'off' row)")

    # (c) A configuration that DOES fire: compare the shared legs.
    #
    # NOTE: ``trade_id`` is NOT unique in the engine's output — a
    # contra-close split emits TWO legs with the SAME id (one for the
    # netted part, one for the remainder), and the remainder's exit is
    # a different bar. So the legs are matched by their FULL identity
    # (entry_bar, exit_bar, side, entry_price), not by id alone.
    fire_cfg = {"trail_mode": "combined", "trail_atr_mult": 2.0,
                "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5}
    fire_trades, _, _ = _run({**base_over, **fire_cfg})

    def _key(t: object) -> tuple:
        # NOT the exit bar: a trail-stopped leg exits EARLIER than it did
        # with the trail off, so the exit bar is exactly what differs.
        # Match on the ENTRY identity.
        return (int(getattr(t, "entry_bar")),
                int(getattr(t, "direction")),
                round(float(getattr(t, "entry_price")), 6))

    fire_by_key: dict[tuple, list] = {}
    for t in fire_trades:
        fire_by_key.setdefault(_key(t), []).append(t)

    shared = 0
    trail_legs = 0
    px_match = 0
    for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
        if not modelled:
            continue
        cand = fire_by_key.get((l.entry_bar, l.side, round(l.entry_px, 6)))
        et = cand[0] if cand else None
        if et is None:
            continue
        shared += 1
        reason, px, _g = _replay_trail(
            hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
            side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px, tp_px=l.tp_px,
            mode="combined", atr_mult=2.0, activation_mult=1.0,
            giveback_frac=0.5, fill_model=fill_model, eod_px=l.exit_px,
        )
        if et.exit_reason == "trail_stop":
            trail_legs += 1
            if reason == "trail_stop" and abs(px - et.exit_price) < 1e-6:
                px_match += 1
    print(f"  (c) firing config: {shared} shared legs, {trail_legs} of them "
          f"trail-stopped in the engine, "
          f"{px_match} matched the replay's exit price")
    print("      (legs the trail closed early are NOT in the shared set — "
          "the book changed)")
    print()

    # ================================================================
    # 1. The excursion distribution
    # ================================================================
    print("=" * 78)
    print("1. THE EXCURSION DISTRIBUTION  (trail OFF book)")
    print("=" * 78)
    print(f"legs = {n}   gross ${gross_off:,.2f}   fees ${fee_off:,.2f}   "
          f"net ${gross_off - fee_off:,.2f}")
    print()
    print(f"{'metric':<26}{'p10':>9}{'p25':>9}{'p50':>9}{'p75':>9}{'p90':>9}")
    for name, arr in (
            ("MFE ($/oz)", mfe_usd),
            ("MFA ($/oz)", mfa_usd),
            ("MFE (x ATR_1m)", mfe_atr),
            ("MFA (x ATR_1m)", mfa_atr),
            ("SL distance ($)", np.array([abs(l.entry_px - l.sl_px) for l in legs])),
            ("TP distance ($)", np.array([abs(l.tp_px - l.entry_px) for l in legs])),
            ("hold (s)", np.array([l.hold_secs for l in legs]))):
        fin = arr[np.isfinite(arr)]
        if fin.size == 0:
            continue
        print(f"{name:<26}"
              f"{np.percentile(fin, 10):>9.3f}"
              f"{np.percentile(fin, 25):>9.3f}"
              f"{np.percentile(fin, 50):>9.3f}"
              f"{np.percentile(fin, 75):>9.3f}"
              f"{np.percentile(fin, 90):>9.3f}")
    print()

    # ================================================================
    # 2. Is there room for a trail at all?
    # ================================================================
    print("=" * 78)
    print("2. IS THERE ROOM FOR A TRAIL?")
    print("=" * 78)
    fee_per_leg = fee_off / n
    print(f"measured round trip: ${fee_per_leg:.4f}/leg")
    print()
    print("A trail can only bank the MFE it survives to. The share of legs")
    print("whose MFE clears the round trip is the CEILING on the trail's")
    print("value:")
    for mult in (0.0, 0.5, 1.0, 1.5, 2.0, 3.0):
        thresh = mult * fee_per_leg
        share = float(np.mean(mfe_usd > thresh))
        print(f"  MFE > {mult:.1f} x round trip (${thresh:.3f}): "
              f"{share:>7.2%} of legs")
    print()
    print("MFE relative to MFA — the trade went further in my favour than")
    print("against me. A trail can only work on these legs:")
    good = np.isfinite(mfe_atr) & np.isfinite(mfa_atr) & (mfe_atr > mfa_atr)
    print(f"  MFE > MFA: {float(np.mean(good)):>7.2%} of legs")
    for mult in (1.0, 2.0, 3.0):
        sel = good & (mfe_atr > mult)
        print(f"    and MFE > {mult:.1f} x ATR_1m: {float(np.mean(sel)):>7.2%}")
    print()

    # ================================================================
    # 2b. THE ORACLE BOUND  (can ANY trail geometry work here?)
    # ================================================================
    #
    # Before tuning the geometry, bound the question. The ORACLE trail
    # exits at the PERFECT moment: at the peak, with zero giveback.
    # No real trailing stop can beat it — it is the ceiling on what
    # trailing can bank, because a trail can only exit at a price the
    # market actually printed.
    #
    # If the oracle loses, the question is closed: no geometry helps.
    # If the oracle wins by a lot, the question is OPEN and the
    # geometry ladder is worth reading.
    print("=" * 78)
    print("2b. THE ORACLE BOUND  (can ANY trail geometry work here?)")
    print("=" * 78)
    print("The oracle exits at the PEAK with zero giveback — the best")
    print("any trailing stop could possibly do on these paths. It is a")
    print("bound, not a candidate.")
    print()

    orc_gross = 0.0
    orc_fee = 0.0
    n_orc_beats = 0
    n_orc_loses = 0
    for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
        if not modelled:
            orc_gross += l.gross
            orc_fee += l.fee
            continue
        # The oracle exits at the peak, but only if the SL is not hit
        # FIRST (the SL is a hard stop the trail cannot override).
        if l.side > 0:
            sl_hit = lo <= l.sl_px
            peak_bar = int(np.argmax(hi))
        else:
            sl_hit = hi >= l.sl_px
            peak_bar = int(np.argmin(lo))
        sl_first = bool(sl_hit[:peak_bar + 1].any())
        if sl_first:
            px = l.exit_px
        else:
            px = float(peak[peak_bar])
        g_px = (px - l.entry_px) * l.side
        orc_gross += g_px * l.qty_oz
        orc_fee += leg_fee(l.entry_px, px) * (l.qty_oz / OZ)
        base_g = (l.exit_px - l.entry_px) * l.side * l.qty_oz
        if g_px * l.qty_oz > base_g + 1e-9:
            n_orc_beats += 1
        elif g_px * l.qty_oz < base_g - 1e-9:
            n_orc_loses += 1
    orc_net = orc_gross - orc_fee
    print(f"  {'trail OFF (baseline)':<34} gross ${gross_off:>10,.2f}  "
          f"fees ${fee_off:>9,.2f}  net ${gross_off - fee_off:>10,.2f}")
    print(f"  {'ORACLE (exit at the peak)':<34} gross ${orc_gross:>10,.2f}  "
          f"fees ${orc_fee:>9,.2f}  net ${orc_net:>10,.2f}")
    print(f"  {'oracle vs baseline':<34} "
          f"${orc_net - (gross_off - fee_off):>+10,.2f}")
    print()
    print(f"  legs the oracle BEATS: {n_orc_beats}   "
          f"legs it LOSES to: {n_orc_loses}")
    print()
    if orc_net <= (gross_off - fee_off):
        print("  VERDICT: the oracle does NOT beat the baseline. No")
        print("  trailing geometry can win on this book — the TP is")
        print("  worth more than the peak a trail could bank. The trail")
        print("  question is CLOSED, and the geometry ladder below is")
        print("  measuring the size of the loss, not a way to win.")
    else:
        print("  VERDICT: the oracle DOES beat the baseline. Trailing has")
        print("  room; the geometry ladder below is the way to find it.")
    print()

    # ================================================================
    # 3. Per side
    # ================================================================
    print("=" * 78)
    print("3. PER-SIDE")
    print("=" * 78)
    for lbl, sel in (("long", side_arr > 0), ("short", side_arr < 0)):
        if sel.sum() == 0:
            continue
        g = float(gross_arr[sel].sum())
        f = float(sum(l.fee for l, s in zip(legs, sel) if s))
        print(f"  {lbl:<6} n={int(sel.sum()):<6} gross ${g:>10,.2f}  "
              f"fees ${f:>9,.2f}  net ${g - f:>10,.2f}  "
              f"gross/leg ${g / sel.sum():>7.4f}  "
              f"fee/leg ${f / sel.sum():>7.4f}")
        print(f"         MFE p50 ${np.percentile(mfe_usd[sel], 50):.3f}  "
              f"MFA p50 ${np.percentile(mfa_usd[sel], 50):.3f}  "
              f"MFE p50 {np.percentile(mfe_atr[sel], 50):.2f} ATR")
    print()

    # ================================================================
    # 4. What ends a leg
    # ================================================================
    print("=" * 78)
    print("4. WHAT ENDS A LEG (and what the trail would replace)")
    print("=" * 78)
    print(f"{'exit reason':<16}{'n':>7}{'share':>9}{'gross':>12}"
          f"{'MFE p50':>10}{'MFE/ATR p50':>14}{'MFA p50':>10}")
    for r in sorted(reasons, key=lambda k: -reasons[k]):
        sel = np.array([l.exit_reason == r for l in legs])
        if sel.sum() == 0:
            continue
        print(f"{r:<16}{int(sel.sum()):>7}{sel.mean():>9.2%}"
              f"{float(gross_arr[sel].sum()):>12,.2f}"
              f"{np.percentile(mfe_usd[sel], 50):>10.3f}"
              f"{np.percentile(mfe_atr[sel], 50):>14.2f}"
              f"{np.percentile(mfa_usd[sel], 50):>10.3f}")
    print()
    print("READ: the 'tp' bucket is where a trail could substitute. The 'sl'")
    print("bucket is where the trail would have to NOT fire.")
    print()

    # ================================================================
    # 5. The replay grid
    # ================================================================
    print("=" * 78)
    print("5. TRAIL REPLAY  (same legs, candidate geometry)")
    print("=" * 78)
    print("Replay holds the leg set FIXED — it does not re-run the book.")
    print("It answers: given these legs, which geometry banks the most?")
    print()

    grid: list[tuple[str, float, float, float]] = []
    for mode in ("chandelier", "giveback", "combined"):
        for k in (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0):
            grid.append((mode, k, 1.0, 0.5))
    for act in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0):
        grid.append(("combined", 2.0, act, 0.5))
    for gb in (0.25, 0.5, 0.75):
        grid.append(("giveback", 2.0, 1.0, gb))
        grid.append(("combined", 2.0, 1.0, gb))
    grid.append(("off", 0.0, 0.0, 0.0))

    rows: list[dict[str, object]] = []
    for mode, k, act, gb in grid:
        tot_gross = 0.0
        tot_fee = 0.0
        n_trail = n_tp = n_sl = n_eod = n_unmod = 0
        for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
            if not modelled:
                # The trail never touched this leg — hold it at the
                # engine's actual P&L. It is not a candidate.
                tot_gross += l.gross
                tot_fee += l.fee
                n_unmod += 1
                continue
            reason, px, gross_px = _replay_trail(
                hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
                side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px, tp_px=l.tp_px,
                mode=mode, atr_mult=k, activation_mult=act,
                giveback_frac=gb, fill_model=fill_model, eod_px=l.exit_px,
            )
            tot_gross += gross_px * l.qty_oz
            tot_fee += leg_fee(l.entry_px, px) * (l.qty_oz / OZ)
            if reason == "trail_stop":
                n_trail += 1
            elif reason == "tp":
                n_tp += 1
            elif reason == "sl":
                n_sl += 1
            else:
                n_eod += 1
        rows.append({
            "mode": mode, "k": k, "act": act, "gb": gb,
            "gross": tot_gross, "fees": tot_fee, "net": tot_gross - tot_fee,
            "n_trail": n_trail, "n_tp": n_tp, "n_sl": n_sl, "n_eod": n_eod,
            "n_unmodelled": n_unmod,
            "gross_per_leg": tot_gross / n,
        })

    def _fmt(r: dict[str, object]) -> str:
        return (f"{str(r['mode']):<11}{float(r['k']):>5.1f}"
                f"{float(r['act']):>6.2f}{float(r['gb']):>6.2f}"
                f"{float(r['gross']):>11,.2f}{float(r['fees']):>10,.2f}"
                f"{float(r['net']):>11,.2f}"
                f"{float(r['gross_per_leg']):>9.4f}"
                f"{int(r['n_trail']):>8}{int(r['n_tp']):>7}"
                f"{int(r['n_sl']):>7}{int(r['n_eod']):>6}")

    hdr = (f"{'mode':<11}{'k':>5}{'act':>6}{'gb':>6}{'gross':>11}"
           f"{'fees':>10}{'net':>11}{'g/leg':>9}{'trail':>8}{'tp':>7}"
           f"{'sl':>7}{'eod':>6}")
    print(hdr)
    print("-" * len(hdr))
    off_row = next(r for r in rows if r["mode"] == "off")
    print(f"{_fmt(off_row)}   <- REPLAY baseline")
    print(f"{'ENGINE baseline:':<40} gross {gross_off:>10,.2f}  "
          f"fees {fee_off:>9,.2f}  net {gross_off - fee_off:>10,.2f}")
    d_gross = float(off_row["gross"]) - gross_off
    d_fee = float(off_row["fees"]) - fee_off
    ok_recon = abs(d_gross) < 0.01 and abs(d_fee) < 0.01
    print(f"  RECONCILIATION: d_gross ${d_gross:+.4f}  d_fees ${d_fee:+.4f}  "
          f"-> {'EXACT' if ok_recon else '*** MISMATCH ***'}")
    if not ok_recon:
        print("  !! the replay's baseline does not equal the engine's — "
              "every delta below is suspect")
    print()
    rows_sorted = sorted([r for r in rows if r["mode"] != "off"],
                         key=lambda r: -float(r["net"]))
    for r in rows_sorted:
        print(_fmt(r))
    print()
    best = rows_sorted[0]
    print(f"BEST REPLAY: {best['mode']} k={best['k']} act={best['act']} "
          f"gb={best['gb']}  net ${float(best['net']):,.2f}")
    print(f"  vs baseline net ${gross_off - fee_off:,.2f}  "
          f"(delta ${float(best['net']) - (gross_off - fee_off):+,.2f})")
    print(f"  gross/leg ${float(best['gross_per_leg']):.4f} vs baseline "
          f"${gross_off / n:.4f}   (fee/leg ${fee_per_leg:.4f})")
    print()

    # ================================================================
    # 5b. PER-LEG DECOMPOSITION — who wins, who loses, and why
    # ================================================================
    #
    # The book-level number hides the mechanism. This section takes
    # the CANONICAL candidate and the BEST replay candidate and splits
    # the legs into the four classes that decide the verdict:
    #
    #   * TP legs the trail let run further  -> the trail CAPTURED more
    #   * TP legs the trail cut early        -> the trail LOST upside
    #   * SL legs the trail cut early        -> the trail SAVED loss
    #   * SL legs the trail did not touch    -> untouched
    #
    # A trailing stop is only worth having if class 1 + class 3 beats
    # class 2. That is the whole trade.
    print("=" * 78)
    print("5b. PER-LEG DECOMPOSITION  (what the trail wins and loses)")
    print("=" * 78)

    def _decompose(mode: str, k: float, act: float, gb: float) -> dict:
        """Replay one config and bucket the legs by (original exit x new exit)."""
        buckets: dict[tuple[str, str], list[float]] = {}
        deltas: list[float] = []
        for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
            if not modelled:
                continue
            reason, px, gross_px = _replay_trail(
                hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
                side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px, tp_px=l.tp_px,
                mode=mode, atr_mult=k, activation_mult=act,
                giveback_frac=gb, fill_model=fill_model, eod_px=l.exit_px,
            )
            d = gross_px * l.qty_oz - l.gross
            buckets.setdefault((l.exit_reason, reason), []).append(d)
            deltas.append(d)
        return {"buckets": buckets, "total": sum(deltas)}

    for label, cfg in (
            ("CANONICAL  combined k=2.0 act=1.0 gb=0.5",
             ("combined", 2.0, 1.0, 0.5)),
            ("WIDEST    chandelier k=8.0 act=1.0 gb=0.5",
             ("chandelier", 8.0, 1.0, 0.5))):
        mode, k, act, gb = cfg
        dec = _decompose(mode, k, act, gb)
        print(f"  {label}")
        print(f"    {'original':<14}{'new exit':<14}{'n':>6}{'total $':>13}"
              f"{'mean $':>11}")
        for (orig, new), ds in sorted(dec["buckets"].items(),
                                     key=lambda kv: -sum(kv[1])):
            if not ds:
                continue
            print(f"    {orig:<14}{new:<14}{len(ds):>6}"
                  f"{sum(ds):>13,.2f}{sum(ds) / len(ds):>11,.2f}")
        print(f"    {'TOTAL':<28}{'':>6}{dec['total']:>13,.2f}")
        print()

    # The two sub-totals that decide the verdict.
    mode, k, act, gb = "combined", 2.0, 1.0, 0.5
    dec = _decompose(mode, k, act, gb)
    gained = sum(v for (o, nw), vs in dec["buckets"].items()
                 for v in vs if o == "tp" and nw == "tp" and v > 0)
    lost = sum(v for (o, nw), vs in dec["buckets"].items()
               for v in vs if o == "tp" and nw == "tp" and v < 0)
    cut_loss = sum(v for (o, nw), vs in dec["buckets"].items()
                   for v in vs if o == "sl" and nw == "trail_stop")
    print(f"  CANONICAL DECOMPOSITION (combined k=2.0 act=1.0 gb=0.5):")
    print(f"    TP legs that kept their TP and banked MORE : "
          f"${gained:>10,.2f}")
    print(f"    TP legs that kept their TP and banked LESS : "
          f"${lost:>10,.2f}")
    print(f"    SL legs the trail rescued before the stop   : "
          f"${cut_loss:>10,.2f}")
    print()

    # ================================================================
    # 5c. THE 2-D SURFACE  (activation x distance) — the real answer
    # ================================================================
    #
    # The 1-D ladders each vary ONE knob with the others pinned at the
    # canonical values. But the trade-off is JOINT: a wide activation
    # lets the trade run before the trail arms, and a wide distance
    # lets it give back room. The surface is where the answer lives.
    print("=" * 78)
    print("5c. THE 2-D SURFACE  (activation x chandelier distance)")
    print("=" * 78)
    print("net $ by (activation in ATR_1m) x (chandelier distance in ATR_1m)")
    print("baseline (trail OFF) net "
          f"${gross_off - fee_off:,.2f}   "
          f"gross/leg ${gross_off / n:.4f}   fee/leg ${fee_per_leg:.4f}")
    print()

    k_grid = (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0)
    act_grid = (0.5, 1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0)
    surface: list[dict[str, object]] = []
    for act in act_grid:
        for k in k_grid:
            surface.append({
                "mode": "chandelier", "k": k, "act": act, "gb": 0.5,
                "net": _replay_net(
                    legs, paths, is_modelled, atr_1m, mode="chandelier",
                    k=k, act=act, gb=0.5, fill_model=fill_model,
                    leg_fee=leg_fee),
            })

    hdr3 = f"{'act \\ k':<10}" + "".join(f"{k:>9.0f}" for k in k_grid)
    print(hdr3)
    print("-" * len(hdr3))
    best_surface = max(surface, key=lambda r: float(r["net"]))
    for act in act_grid:
        cells = []
        for k in k_grid:
            r = next(x for x in surface
                     if x["act"] == act and x["k"] == k)
            net = float(r["net"])
            mark = "*" if r is best_surface else " "
            cells.append(f"{net:>8,.0f}{mark}")
        print(f"{act:<10.2f}" + "".join(cells))
    print()
    print(f"best on the surface: chandelier k={best_surface['k']} "
          f"act={best_surface['act']}  net ${float(best_surface['net']):,.2f}  "
          f"(baseline ${gross_off - fee_off:,.2f}, "
          f"delta ${float(best_surface['net']) - (gross_off - fee_off):+,.2f})")
    print()

    # ================================================================
    # 5d. PER-SIDE DECOMPOSITION
    # ================================================================
    print("=" * 78)
    print("5d. PER-SIDE DECOMPOSITION  (does the trail help one side?)")
    print("=" * 78)
    for side_lbl, side_sel in (("LONG", 1), ("SHORT", -1)):
        sub_legs = [l for l in legs if l.side == side_sel]
        if not sub_legs:
            continue
        idx = [i for i, l in enumerate(legs) if l.side == side_sel]
        sub_paths = [paths[i] for i in idx]
        sub_mod = is_modelled[idx]
        sub_legs_arr = np.array([sub_legs])
        print(f"  {side_lbl}  n={len(sub_legs)}  "
              f"baseline gross ${sum(l.gross for l in sub_legs):,.2f}")
        for label, cfg in (
                ("combined k=2 act=1 gb=0.5", ("combined", 2.0, 1.0, 0.5)),
                ("chandelier k=8 act=1 gb=0.5", ("chandelier", 8.0, 1.0, 0.5)),
                ("chandelier k=32 act=1 gb=0.5", ("chandelier", 32.0, 1.0, 0.5))):
            mode, k, act, gb = cfg
            tot = 0.0
            n_sl_resc = 0
            n_tp_cut = 0
            for l, (hi, lo, op, peak), modelled in zip(
                    sub_legs, sub_paths, sub_mod):
                if not modelled:
                    continue
                reason, px, gross_px = _replay_trail(
                    hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
                    side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px,
                    tp_px=l.tp_px, mode=mode, atr_mult=k,
                    activation_mult=act, giveback_frac=gb,
                    fill_model=fill_model, eod_px=l.exit_px,
                )
                tot += gross_px * l.qty_oz - l.gross
                if l.exit_reason == "sl" and reason == "trail_stop":
                    n_sl_resc += 1
                if l.exit_reason == "tp" and reason == "trail_stop":
                    n_tp_cut += 1
            print(f"    {label:<32} delta ${tot:>10,.2f}   "
                  f"SL rescued {n_sl_resc:>3}   TP cut {n_tp_cut:>3}")
        print()

    # ================================================================
    # 5e. THE GEOMETRY — how wide must the trail be?
    # ================================================================
    #
    # The 2-D surface shows the SHAPE but not the CAUSE. The cause is
    # geometric and it is decidable per leg: on the bars a leg lives
    # through, how wide can the trail be before it cuts the TP, and
    # how tight must it be to rescue the loser?
    #
    # This section computes BOTH thresholds directly from the path, so
    # the recommendation is a GEOMETRIC fact about the book rather
    # than a search result.
    print("=" * 78)
    print("5e. THE GEOMETRY  (how wide can the trail be?)")
    print("=" * 78)
    print("A trail fires at bar b iff the width is SMALL ENOUGH that the")
    print("retrace reaches it. So a WIDER trail fires LESS:")
    print("    chandelier (long): fires iff  k <= (peak - low)/ATR_1m")
    print("    giveback  (long): fires iff  f <= (peak - low)/MFE")
    print("Per leg this gives two thresholds:")
    print("    K_tp = the width at which the trail CUTS the TP")
    print("          -> the TP is preserved iff  k > K_tp")
    print("    K_sl = the width at which the trail RESCUES the loser")
    print("          -> the loser is rescued iff  k <= K_sl")
    print()

    for act in (1.0, 3.0, 8.0):
        recs = _width_thresholds(legs, paths, is_modelled, act)
        tp_recs = [r for r in recs if r["k_preserve"] is not None]
        sl_recs = [r for r in recs if r["k_rescue"] is not None]
        if not tp_recs or not sl_recs:
            continue
        kp = np.array([float(r["k_preserve"]) for r in tp_recs])
        kr = np.array([float(r["k_rescue"]) for r in sl_recs])
        print(f"  activation = {act:.1f} x ATR_1m   "
              f"(TP legs {len(tp_recs)}, SL legs {len(sl_recs)})")
        print(f"    K_tp (cut a TP at or below this width)   "
              f"p10 {np.percentile(kp, 10):6.2f}  p25 {np.percentile(kp, 25):6.2f}  "
              f"p50 {np.percentile(kp, 50):6.2f}  p90 {np.percentile(kp, 90):6.2f}")
        print(f"    K_sl (rescue a loser at or below this width) "
              f"p10 {np.percentile(kr, 10):6.2f}  p25 {np.percentile(kr, 25):6.2f}  "
              f"p50 {np.percentile(kr, 50):6.2f}  p90 {np.percentile(kr, 90):6.2f}")
        print()
        print(f"    {'k':>6}{'TP kept':>10}{'losers rescued':>17}"
              f"{'net $':>12}{'vs base':>11}")
        base_net = gross_off - fee_off
        for k in (1.0, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0):
            keep = float((kp < k).mean())
            resc = float((kr >= k).mean())
            r = next((x for x in surface
                      if x["act"] == act and x["k"] == k), None)
            net_s = f"{float(r['net']):>12,.0f}" if r is not None else f"{'':>12}"
            d_s = (f"{float(r['net']) - base_net:>+11,.0f}"
                   if r is not None else f"{'':>11}")
            print(f"    {k:>6.0f}{keep:>9.0%}{resc:>17.0%}{net_s}{d_s}")
        print()

    # ================================================================
    # 6. The activation ladder
    # ================================================================
    print("=" * 78)
    print("6. THE ACTIVATION LADDER  (the MFE the trade must reach)")
    print("=" * 78)
    print("Activation is the ONE knob that decides whether the trail fires")
    print("at all. Below the activation MFE the leg exits exactly as it")
    print("did with the trail off — so activation cannot hurt a leg that")
    print("never armed.")
    print()
    print(f"{'activation':>12}{'trail fired':>14}{'tp kept':>10}"
          f"{'net':>12}{'delta vs base':>15}")
    for act in (0.25, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0):
        r = next(x for x in rows
                 if x["mode"] == "combined" and x["k"] == 2.0
                 and x["act"] == act and x["gb"] == 0.5)
        print(f"{act:>12.2f}{int(r['n_trail']):>14}{int(r['n_tp']):>10}"
              f"{float(r['net']):>12,.2f}"
              f"{float(r['net']) - (gross_off - fee_off):>15,.2f}")
    print()

    # ================================================================
    # 7. The distance ladder
    # ================================================================
    print("=" * 78)
    print("7. THE DISTANCE LADDER  (chandelier k)")
    print("=" * 78)
    med_atr = float(np.median(atr_arr[atr_arr > 0])) if (atr_arr > 0).any() else 0.0
    print(f"The chandelier distance is k x ATR_1m. At this book's median")
    print(f"ATR_1m of ${med_atr:.3f}:")
    print()
    print(f"{'k':>6}{'distance $':>12}{'trail fired':>14}{'tp kept':>10}"
          f"{'net':>12}{'delta vs base':>15}")
    for k in (1.0, 1.5, 2.0, 3.0, 4.0, 6.0, 8.0, 12.0, 16.0, 24.0, 32.0):
        r = next(x for x in rows
                 if x["mode"] == "chandelier" and x["k"] == k
                 and x["act"] == 1.0 and x["gb"] == 0.5)
        print(f"{k:>6.1f}{k * med_atr:>12.3f}{int(r['n_trail']):>14}"
              f"{int(r['n_tp']):>10}{float(r['net']):>12,.2f}"
              f"{float(r['net']) - (gross_off - fee_off):>15,.2f}")
    print()

    # ================================================================
    # 7b. THE ATR BASIS — 1m vs 1s (the counterfactual)
    # ================================================================
    #
    # The user's instruction: "the ATR should be 1 min, not 1 s, else
    # ATR is too narrow." This is CORRECT and the engine already obeys
    # it — but the claim needs a measurement, not an assertion.
    #
    # The counterfactual: rebuild the ATR yardstick on the 1s grid
    # (Wilder ATR(14) over 1s bars) and replay the SAME grid against
    # it. A 1s ATR is ~10x narrower, so the same ``k`` means a
    # 10x TIGHTER chandelier — the trail would sit a few cents from
    # price and be triggered by tick noise.
    print("=" * 78)
    print("7b. THE ATR BASIS — 1m vs 1s  (the counterfactual)")
    print("=" * 78)
    atr_1s_wilder = _atr_1s_wilder(high, low, close, length=ATR_1M_LENGTH)
    a1s = atr_1s_wilder[atr_1s_wilder > 0]
    if a1s.size and a.size:
        print(f"  ATR_1m (Wilder 14, 1m candles)   p50 ${np.percentile(a, 50):.4f}")
        print(f"  ATR_1s (Wilder 14, 1s bars)      p50 ${np.percentile(a1s, 50):.4f}")
        print(f"  ratio                            {np.median(a) / np.median(a1s):.1f}x")
        print()
        print("  Same grid, same k, DIFFERENT yardstick:")
        print()
        hdr2 = (f"{'mode':<11}{'k':>5}{'act':>6}{'net (1m ATR)':>15}"
                f"{'net (1s ATR)':>15}{'delta':>12}"
                f"{'1m: trail/tp/sl':>20}{'1s: trail/tp/sl':>20}")
        print(hdr2)
        print("-" * len(hdr2))
        base_net = gross_off - fee_off
        for mode, k, act, gb in (
                ("combined", 2.0, 1.0, 0.5),
                ("chandelier", 2.0, 1.0, 0.5),
                ("giveback", 2.0, 1.0, 0.5),
                ("combined", 4.0, 1.0, 0.5),
                ("combined", 2.0, 3.0, 0.5)):
            r1m = next(x for x in rows
                       if x["mode"] == mode and x["k"] == k
                       and x["act"] == act and x["gb"] == gb)
            net_1s, bd_1s = _replay_net(
                legs, paths, is_modelled, atr_1s_wilder, mode=mode, k=k,
                act=act, gb=gb, fill_model=fill_model, leg_fee=leg_fee,
                want_breakdown=True)
            bd_1m = {"n_trail": int(r1m["n_trail"]), "n_tp": int(r1m["n_tp"]),
                     "n_sl": int(r1m["n_sl"])}
            print(f"{mode:<11}{k:>5.1f}{act:>6.2f}"
                  f"{float(r1m['net']):>15,.2f}{net_1s:>15,.2f}"
                  f"{net_1s - float(r1m['net']):>12,.2f}"
                  f"{bd_1m['n_trail']:>8}/{bd_1m['n_tp']:>5}/{bd_1m['n_sl']:>4}"
                  f"{bd_1s['n_trail']:>8}/{bd_1s['n_tp']:>5}/{bd_1s['n_sl']:>4}")
        print()
        print(f"  baseline (trail off) net ${base_net:,.2f} — identical under")
        print("  both bases, because the trail is OFF in the baseline.")
        print()
        print("  READ: a 1s ATR is ~10x narrower, so the SAME k is a 10x")
        print("  TIGHTER stop. The trail fires on more legs and cuts more")
        print("  TPs. In 'combined' the giveback arm (which does NOT read")
        print("  ATR) starts dominating the tighter chandelier arm, so the")
        print("  mode silently becomes a giveback trail — a different")
        print("  geometry wearing the same name.")
        print()

    # ================================================================
    # 7b2. THE ATR LENGTH  (1m cadence, but WHICH 1m length?)
    # ================================================================
    #
    # The cadence is 1m — that is settled. But the LENGTH is still a
    # free choice: Wilder ATR(14) on 1m candles is 14 minutes of true
    # range, and ATR(20) is 20 minutes. A longer length is a smoother,
    # slower yardstick. This ladder measures whether the length matters
    # at the SETTINGS THE ANALYSIS POINTS TO, so the final recipe does
    # not bake in an arbitrary 14.
    print("=" * 78)
    print("7b2. THE ATR LENGTH  (1m cadence — but which length?)")
    print("=" * 78)
    print("Same 1m cadence, different Wilder length. The yardstick")
    print("changes, so the SAME k means a different distance in $.")
    print()
    hdr_len = (f"{'ATR length':>11}{'p50 ATR $':>11}{'k':>6}"
               f"{'distance $':>12}{'net $':>11}{'vs base':>11}")
    print(hdr_len)
    print("-" * len(hdr_len))
    base_net_len = gross_off - fee_off
    for length in (7, 14, 20, 30):
        atr_len = _build_atr_1m_forward_filled(
            high, low, close, times, length=length)
        al = atr_len[atr_len > 0]
        med = float(np.median(al)) if al.size else 0.0
        for k in (8.0, 12.0):
            net_len, _bd = _replay_net(
                legs, paths, is_modelled, atr_len, mode="chandelier", k=k,
                act=1.0, gb=0.5, fill_model=fill_model, leg_fee=leg_fee,
                want_breakdown=True)
            print(f"{length:>11}{med:>11.3f}{k:>6.1f}{k * med:>12.3f}"
                  f"{net_len:>11,.2f}{net_len - base_net_len:>11,.2f}")
    print()
    print("READ: if the length barely moves the net, the trail is NOT")
    print("sensitive to the yardstick's fine structure — it is sensitive")
    print("to the DISTANCE it implies, which is the actionable knob.")
    print()

    # ================================================================
    # 7c. WHERE THE TRAIL'S MONEY COMES FROM
    # ================================================================
    #
    # The grid says the trail loses. But a trail can lose on the TP
    # bucket and still be worth having if it truncates the SL bucket.
    # This decomposition splits the net delta by the leg's ORIGINAL
    # exit reason (the trail-OFF book), which is the only way to tell
    # a RISK knob (truncates the loss tail) from a GEOMETRY change
    # (replaces a good exit with a worse one).
    print("=" * 78)
    print("7c. WHERE THE TRAIL'S MONEY COMES FROM  (by ORIGINAL exit)")
    print("=" * 78)
    print("Each leg is bucketed by the exit it had with the trail OFF.")
    print("'delta' is the replayed gross minus the trail-off gross, for")
    print("the legs in that bucket.")
    print()

    for mode, k, act, gb in (("chandelier", 8.0, 1.0, 0.5),
                              ("chandelier", 4.0, 1.0, 0.5),
                              ("chandelier", 2.0, 1.0, 0.5),
                              ("combined", 2.0, 3.0, 0.5),
                              ("combined", 2.0, 6.0, 0.5),
                              ("combined", 2.0, 1.0, 0.5)):
        buckets: dict[str, list[float]] = {}
        for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
            if not modelled:
                continue
            reason, px, gross_px = _replay_trail(
                hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
                side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px, tp_px=l.tp_px,
                mode=mode, atr_mult=k, activation_mult=act,
                giveback_frac=gb, fill_model=fill_model, eod_px=l.exit_px,
            )
            d = (gross_px - (l.exit_px - l.entry_px) * l.side) * l.qty_oz
            buckets.setdefault(l.exit_reason, []).append(d)
        tot = sum(sum(v) for v in buckets.values())
        print(f"  {mode} k={k} act={act} gb={gb}   total gross delta "
              f"${tot:+,.2f}")
        print(f"    {'orig exit':<12}{'n':>5}{'delta $':>12}{'per leg':>11}")
        for r in sorted(buckets, key=lambda x: -len(buckets[x])):
            v = buckets[r]
            print(f"    {r:<12}{len(v):>5}{sum(v):>12,.2f}"
                  f"{sum(v) / len(v):>11,.4f}")
        print()

    # ================================================================
    # 7d. THE LOSS TAIL — does the trail truncate it?
    # ================================================================
    print("=" * 78)
    print("7d. THE LOSS TAIL  (the risk-knob test)")
    print("=" * 78)
    print("A risk knob earns its keep by cutting the WORST losses. The")
    print("measure is the p-tail of the per-leg gross, trail-off vs")
    print("trail-on, on the SAME legs.")
    print()
    off_px = np.array([(l.exit_px - l.entry_px) * l.side * l.qty_oz
                        for l in legs])
    print(f"{'config':<28}{'worst':>9}{'p5':>9}{'p10':>9}"
          f"{'mean':>9}{'n<-$5':>9}")
    print(f"{'trail OFF':<28}"
          f"{off_px.min():>9,.2f}"
          f"{np.percentile(off_px, 5):>9,.2f}"
          f"{np.percentile(off_px, 10):>9,.2f}"
          f"{off_px.mean():>9,.2f}"
          f"{int((off_px < -5).sum()):>9}")
    for mode, k, act, gb in (("chandelier", 8.0, 1.0, 0.5),
                              ("chandelier", 4.0, 1.0, 0.5),
                              ("chandelier", 2.0, 1.0, 0.5),
                              ("combined", 2.0, 1.0, 0.5),
                              ("combined", 2.0, 0.5, 0.5)):
        vals = []
        for l, (hi, lo, op, peak), modelled in zip(legs, paths, is_modelled):
            if not modelled:
                vals.append(l.gross)
                continue
            _reason, px, gross_px = _replay_trail(
                hi=hi, lo=lo, op=op, peak=peak, entry_px=l.entry_px,
                side=l.side, atr_1m=l.atr_1m, sl_px=l.sl_px, tp_px=l.tp_px,
                mode=mode, atr_mult=k, activation_mult=act,
                giveback_frac=gb, fill_model=fill_model, eod_px=l.exit_px,
            )
            vals.append(gross_px * l.qty_oz)
        v = np.array(vals)
        print(f"{mode + ' k=' + str(k) + ' act=' + str(act):<28}"
              f"{v.min():>9,.2f}"
              f"{np.percentile(v, 5):>9,.2f}"
              f"{np.percentile(v, 10):>9,.2f}"
              f"{v.mean():>9,.2f}"
              f"{int((v < -5).sum()):>9}")
    print()

    # ================================================================
    # 7e. THE ENGINE A/B  (the replay RANKS; the engine DECIDES)
    # ================================================================
    #
    # The replay holds the leg set fixed. The engine does not: a
    # trail-closed leg frees a same-direction slot, so a LATER signal
    # is admitted; and a trail-closed leg cannot be netted by
    # contra-close. So the top replay candidates must be re-measured
    # through the real engine before anything is believed.
    print("=" * 78)
    print("7e. THE ENGINE A/B  (the replay RANKS; the engine DECIDES)")
    print("=" * 78)
    print("Each candidate is run through the real engine on the same")
    print("window. The engine's book is allowed to CHANGE — that is the")
    print("whole point of the A/B.")
    print()
    hdr3 = (f"{'config':<28}{'legs':>6}{'gross':>11}{'fees':>10}"
            f"{'net':>11}{'trail':>7}{'vs base':>11}")
    print(hdr3)
    print("-" * len(hdr3))
    base_net_engine = gross_off - fee_off
    print(f"{'trail OFF (baseline)':<28}{len(trades):>6}{gross_off:>11,.2f}"
          f"{fee_off:>10,.2f}{base_net_engine:>11,.2f}{0:>7}"
          f"{0.0:>11,.2f}")

    # The candidates the replay liked, plus the canonical.
    cands = [
        ("chandelier k=32 act=1 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 32.0,
          "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=24 act=1 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 24.0,
          "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=16 act=8 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 16.0,
          "trail_activation_mult": 8.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=32 act=8 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 32.0,
          "trail_activation_mult": 8.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=24 act=8 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 24.0,
          "trail_activation_mult": 8.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=32 act=2 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 32.0,
          "trail_activation_mult": 2.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=12 act=1 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 12.0,
          "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=8 act=1 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 8.0,
          "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5}),
        ("chandelier k=8 act=8 gb=0.5",
         {"trail_mode": "chandelier", "trail_atr_mult": 8.0,
          "trail_activation_mult": 8.0, "trail_giveback_frac": 0.5}),
        ("combined k=2 act=12 gb=0.5",
         {"trail_mode": "combined", "trail_atr_mult": 2.0,
          "trail_activation_mult": 12.0, "trail_giveback_frac": 0.5}),
        ("combined k=2 act=3 gb=0.5",
         {"trail_mode": "combined", "trail_atr_mult": 2.0,
          "trail_activation_mult": 3.0, "trail_giveback_frac": 0.5}),
        ("CANONICAL combined k=2 act=1",
         {"trail_mode": "combined", "trail_atr_mult": 2.0,
          "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5}),
    ]
    engine_rows: list[dict[str, object]] = []
    for label, over in cands:
        tr, _, _ = _run({**base_over, **over})
        g = sum(t.pnl_gross_usd for t in tr)
        f = sum(t.fee_usd for t in tr)
        nt = sum(1 for t in tr if t.exit_reason == "trail_stop")
        print(f"{label:<28}{len(tr):>6}{g:>11,.2f}{f:>10,.2f}"
              f"{g - f:>11,.2f}{nt:>7}{(g - f) - base_net_engine:>11,.2f}")
        engine_rows.append({
            "label": label, "legs": len(tr), "gross": g, "fees": f,
            "net": g - f, "n_trail": nt,
            "delta": (g - f) - base_net_engine,
        })
    print()
    print("READ: compare each row's 'vs base' with the SAME config's")
    print("replay delta. Where the engine is WORSE than the replay, the")
    print("trail changed the BOOK for the worse (freed slots admitted")
    print("later, worse signals; or lost the contra-close netting).")
    print()

    # ================================================================
    # 8. The giveback ladder
    # ================================================================
    print("=" * 78)
    print("8. THE GIVEBACK LADDER  (fraction of peak MFE given back)")
    print("=" * 78)
    print(f"{'giveback':>10}{'trail fired':>14}{'tp kept':>10}"
          f"{'net':>12}{'delta vs base':>15}")
    for gb in (0.25, 0.5, 0.75):
        r = next(x for x in rows
                 if x["mode"] == "giveback" and x["k"] == 2.0
                 and x["act"] == 1.0 and x["gb"] == gb)
        print(f"{gb:>10.2f}{int(r['n_trail']):>14}{int(r['n_tp']):>10}"
              f"{float(r['net']):>12,.2f}"
              f"{float(r['net']) - (gross_off - fee_off):>15,.2f}")
    print()

    # ================================================================
    # 9. Why the engine sweep said the trail hurts
    # ================================================================
    print("=" * 78)
    print("9. WHY THE ENGINE SWEEP SAID THE TRAIL HURTS")
    print("=" * 78)
    print("The replay and the engine disagree because they measure")
    print("different things. The replay isolates the TRAIL'S OWN arithmetic")
    print("on a FIXED book. The engine lets the trail change the BOOK:")
    print()
    print("  * a trail-closed leg frees a same-direction slot, so a LATER")
    print("    signal is admitted that would not have been;")
    print("  * a trail-closed leg is no longer in the book, so it cannot")
    print("    be netted by contra-close;")
    print("  * the engine's 'combined' mode takes the TIGHTER of the two")
    print("    levels, so a tight chandelier dominates the giveback and")
    print("    the giveback leg of the mode is inert.")
    print()
    print(f"The engine's own numbers for the SAME config (from the engine run")
    print(f"above, {fire_cfg}):")
    f_gross = sum(t.pnl_gross_usd for t in fire_trades)
    f_fee = sum(t.fee_usd for t in fire_trades)
    f_trail = sum(1 for t in fire_trades if t.exit_reason == "trail_stop")
    print(f"    legs {len(fire_trades)} (vs {n} trail-off)  "
          f"gross ${f_gross:,.2f}  fees ${f_fee:,.2f}  net ${f_gross - f_fee:,.2f}")
    print(f"    trail-stopped legs: {f_trail}")
    print()
    print("A trail that fires on FEW legs and keeps the rest's exits intact")
    print("is a RISK knob (it truncates the loss tail). A trail that fires")
    print("on MANY legs and replaces the TP is a GEOMETRY change (and here")
    print("it loses, because the TP is worth more than the trail captures).")
    print()

    # ================================================================
    # Record the run
    # ================================================================
    out_dir = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / "trail_mfe_mfa.json"
    payload = {
        "window": {"start": args.start, "end": args.end},
        "data": str(args.data),
        "n_legs": n,
        "base_recipe": {
            "trail_mode": "off",
            "vol_state_min_expand_prev_day": float(args.expand_prev_day),
        },
        "baseline": {
            "gross": gross_off, "fees": fee_off,
            "net": gross_off - fee_off,
            "fee_per_leg": fee_per_leg,
            "gross_per_leg": gross_off / n,
        },
        "validation": {
            "never_arm_book_identical": bool(same_ids),
            "replay_exact_legs": int(n - mism),
            "replay_total_legs": int(n),
            "firing_shared_legs": int(shared),
            "firing_trail_legs": int(trail_legs),
            "firing_px_match": int(px_match),
        },
        "atr_1m": {
            "p10": float(np.percentile(a, 10)) if a.size else None,
            "p50": float(np.percentile(a, 50)) if a.size else None,
            "p90": float(np.percentile(a, 90)) if a.size else None,
        },
        "mfe_usd": {"p50": float(np.percentile(mfe_usd, 50)),
                    "p90": float(np.percentile(mfe_usd, 90))},
        "mfe_atr": {"p50": float(np.percentile(mfe_atr[np.isfinite(mfe_atr)], 50)),
                    "p90": float(np.percentile(mfe_atr[np.isfinite(mfe_atr)], 90))},
        "mfa_atr": {"p50": float(np.percentile(mfa_atr[np.isfinite(mfa_atr)], 50)),
                    "p90": float(np.percentile(mfa_atr[np.isfinite(mfa_atr)], 90))},
        "replay": rows,
        "best": rows_sorted[0],
    }
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"report -> {out_path}")

    registry.record_run(
        kind="trail_mfe_mfa",
        params={
            "start": args.start,
            "end": args.end,
            "data": str(args.data),
            "scope": args.scope,
            "atr_basis": "1m_wilder_14",
            "base_recipe": "trail_mode=off",
            "vol_state_min_expand_prev_day": float(args.expand_prev_day),
        },
        summary={
            "n_legs": n,
            "baseline_net": round(gross_off - fee_off, 6),
            "baseline_gross_per_leg": round(gross_off / n, 6),
            "fee_per_leg": round(fee_per_leg, 6),
            "mfe_atr_p50": round(
                float(np.percentile(mfe_atr[np.isfinite(mfe_atr)], 50)), 4),
            "mfa_atr_p50": round(
                float(np.percentile(mfa_atr[np.isfinite(mfa_atr)], 50)), 4),
            "best_replay_net": round(float(best["net"]), 6),
            "best_replay_cfg": (
                f"{best['mode']} k={best['k']} "
                f"act={best['act']} gb={best['gb']}"),
            "atr_1m_p50": round(float(np.percentile(a, 50)), 4) if a.size else None,
            "replay_exact_legs": int(n - mism),
        },
        payload=payload,
        src_fp=registry.source_fingerprint([
            "bt/_engine/vp_multi_target_engine.py",
            "bt/_engine/ict_signals.py",
            "bt/_engine/vol_state.py",
            "bt/_engine/volume_profile.py",
            "bt/_engine/swing_cap.py",
            "bt/_engine/market_structure.py",
            "bt/_engine/rolling_p95.py",
            "bt/_engine/timebase.py",
            "bt/optimal_config.py",
        ]),
    )
    print("registry: recorded run 'trail_mfe_mfa'")


if __name__ == "__main__":
    main()
