"""TA GATE — align the canonical v22 book with a technical-analysis signal.

THE SPEC THIS MODULE IMPLEMENTS
================================

    * a trade may only be OPENED if the TA signal agrees with the
      trade's side at the trade's SIGNAL bar  ("gate open"), and
    * an open trade is CLOSED at the first minute boundary at which
      the TA signal stops agreeing with its side  ("close on signal
      change").

WHY THE GATE IS EVALUATED ON 1-MINUTE BARS
===========================================

The TA state is computed on 1-minute bars (the repo's TA harness
already lives there: :mod:`runtime.diagnostics.ta_1m_core`). But the
gate must not look into the future, and a 1m bar is only KNOWN at its
CLOSE. So the signal that is in force at a real time ``T`` is the
signal of the last 1m bar whose close is ``<= T``:

    idx(T) = max { k : bucket_start[k] + 60s <= T }
    A(T)   = signal[idx(T)]

A 1m bar labelled ``bucket_start`` covers ``[bucket_start,
bucket_start + 60s)`` and is only complete at ``bucket_start + 60s``.
Using the bar that CONTAINS ``T`` would leak up to 60 seconds of
future price into the gate decision — the same class of defect the
repo already documents for the NB94 forward-leak bugs and for the
"signal bar vs fill bar" trap.

The consequence is visible and correct: a leg whose SIGNAL is at
``10:00:37`` is gated on the 1m bar that closed at ``10:00:00``, not
on the bar that closes at ``10:01:00``.

THE THREE OUTCOMES OF A GATE
============================

For every leg in the book:

``GATED OUT AT SIGNAL``
    The TA signal at the signal bar was flat, or it pointed the OTHER
    way. The trade never existed.

``CANCELLED AT FILL``
    The signal agreed at the signal bar but had flipped by the time
    the resting limit order filled. The trade never existed. This is
    the D2 resting-order model's own ``policy_at_fill`` semantics: the
    engine re-checks the book at fill time, and so does the gate.

``FILLED``
    The trade exists. It is then closed early if the signal stops
    agreeing with its side.

WHY THE EARLY CLOSE FILLS AT THE NEXT 1m BAR'S OPEN
===================================================

The signal change becomes KNOWN at the close of 1m bar ``k``, which is
exactly the start of 1m bar ``k+1`` (or later, if the feed has a
gap). The first tradeable price after that instant is the OPEN of the
next 1m bar. That is the same fill the engine uses for a
gap-through exit, and the 1m open IS the 1s open of that minute's
first bar — so the price is exact, not an approximation.

WHY A POST-HOC RE-PRICING IS SAFE ON THIS BOOK
==============================================

The gate is applied to the canonical ledger's legs, after the fact.
Two things make that a faithful measurement rather than a guess:

1. **The book never binds.** The canonical recipe caps the book at
   ``max_same_dir_open = 20`` per direction, and the MEASURED max
   open depth on the wide book is **2**. A book that never holds more
   than 2 positions cannot have its admissions decided by slot
   contention, so removing trades cannot unblock a trade that the
   gated run would have admitted. The TRADE COUNT is therefore exact,
   not an estimate.

2. **The gate is evaluated at the causal bar.** The gate reads the
   signal at the leg's SIGNAL bar — the same bar the engine's own
   per-side gates use. The repo's rule ("a gate that the engine
   evaluates at bar S must be measured at bar S") is satisfied.

What this does NOT do: it does not re-run the engine with the gate
inside it. A re-run would also change the conflict policy, the
contra-close netting and the fills of the SURVIVING legs. The trade
COUNT is exact; the P&L of the surviving legs is a close estimate.

Usage
-----
    from runtime.diagnostics.ta_gate import (
        apply_gate, build_bars_1m_streaming, load_legs)
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from runtime.diagnostics.ta_1m_core import NS_PER_MIN, Bars1m, _atr

__all__ = [
    "GateResult",
    "apply_gate",
    "build_bars_1m_streaming",
    "load_legs",
    "signal_at",
]


# ─────────────────────────────────────────────────────────────────
# The 1-minute bar builder — STREAMING
# ─────────────────────────────────────────────────────────────────

def build_bars_1m_streaming(path: Path, *, atr_n: int = 14,
                            with_row_map: bool = False,
                            ) -> Bars1m | Bars1mGrid:
    """Build 1-minute bars straight from the parquet, streaming.

    WHY NOT ``build_bars_1m(load_bars(path))``: the 1y file is 41.2M
    rows. ``load_bars`` materialises a ~2 GB pandas frame to produce
    ~700K 1m bars. Streaming the row groups keeps the peak memory at
    the size of ONE row group plus the 1m output.

    The aggregation is the SAME technique the repo already uses in the
    detector (the A2 fix): the bucket index comes from the TIMESTAMP,
    never from the row position, and the per-bucket max/min/sum use
    ``reduceat`` because the bucket index is monotonic.

    A minute can straddle a row-group boundary (a row group holds
    ~1M rows ~= 21,700 minutes, so only the LAST minute of a group is
    at risk). That one bucket is carried across the boundary and
    merged.

    ``with_row_map`` also returns the FIRST and LAST file row of each
    1m bucket. That map is what lets the drift benchmark be computed
    on the LEDGER'S OWN 1s grid: the canonical ledger's drift is
    ``(close[exit_bar] - close[entry_bar]) * side * qty`` on 1s rows,
    and a 1m-grid drift is a DIFFERENT number (measured: the two
    grids disagree in SIGN on the whole long book).
    """
    import pyarrow as pa
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    cols = ["time", "open", "high", "low", "close"]
    names = set(pf.schema_arrow.names)
    vol_col = "tickv" if "tickv" in names else ("volume" if "volume" in names else None)
    if vol_col:
        cols.append(vol_col)

    b_list: list[np.ndarray] = []
    o_list: list[np.ndarray] = []
    h_list: list[np.ndarray] = []
    l_list: list[np.ndarray] = []
    c_list: list[np.ndarray] = []
    v_list: list[np.ndarray] = []
    n_list: list[np.ndarray] = []
    #: the FIRST and LAST FILE ROW of each 1m bucket — the map that
    #: lets the drift benchmark run on the LEDGER'S OWN 1s grid.
    rs_list: list[np.ndarray] = []
    re_list: list[np.ndarray] = []

    # The pending (possibly incomplete) minute at the group boundary.
    carry: dict[str, Any] | None = None
    #: the global file row offset of the group being read.
    row_off = 0

    def _flush(carry: dict[str, Any]) -> None:
        b_list.append(np.array([carry["bucket"]], dtype=np.int64))
        o_list.append(np.array([carry["open"]], dtype=np.float64))
        h_list.append(np.array([carry["high"]], dtype=np.float64))
        l_list.append(np.array([carry["low"]], dtype=np.float64))
        c_list.append(np.array([carry["close"]], dtype=np.float64))
        v_list.append(np.array([carry["vol"]], dtype=np.float64))
        n_list.append(np.array([carry["ticks"]], dtype=np.int64))
        rs_list.append(np.array([carry["row_start"]], dtype=np.int64))
        re_list.append(np.array([carry["row_end"]], dtype=np.int64))

    for rg in range(pf.metadata.num_row_groups):
        tbl = pf.read_row_group(rg, columns=cols)
        t_ms = tbl.column("time").cast(pa.int64()).to_numpy(zero_copy_only=False)
        ns = t_ms.astype(np.int64) * 1_000_000
        o = tbl.column("open").to_numpy(zero_copy_only=False).astype(np.float64)
        h = tbl.column("high").to_numpy(zero_copy_only=False).astype(np.float64)
        low = tbl.column("low").to_numpy(zero_copy_only=False).astype(np.float64)
        c = tbl.column("close").to_numpy(zero_copy_only=False).astype(np.float64)
        if vol_col:
            v = tbl.column(vol_col).to_numpy(zero_copy_only=False).astype(np.float64)
        else:
            v = np.zeros(o.size, dtype=np.float64)
        if o.size == 0:
            continue

        bucket = ns // NS_PER_MIN
        starts = np.unique(np.concatenate(
            ([0], np.flatnonzero(np.diff(bucket)) + 1)))
        ends = np.append(starts[1:], bucket.size)

        r_open = o[starts]
        r_high = np.maximum.reduceat(h, starts)
        r_low = np.minimum.reduceat(low, starts)
        r_close = c[ends - 1]
        r_vol = np.add.reduceat(v, starts)
        r_cnt = (ends - starts).astype(np.int64)
        r_bucket = bucket[starts]
        # The file row index of each run's FIRST and LAST row.
        r_row_start = starts.astype(np.int64) + row_off
        r_row_end = (ends - 1).astype(np.int64) + row_off

        # Merge the carried minute into this group's FIRST run.
        merged = False
        if carry is not None:
            if carry["bucket"] == r_bucket[0]:
                carry["high"] = max(carry["high"], float(r_high[0]))
                carry["low"] = min(carry["low"], float(r_low[0]))
                carry["close"] = float(r_close[0])
                carry["vol"] = carry["vol"] + float(r_vol[0])
                carry["ticks"] = carry["ticks"] + int(r_cnt[0])
                carry["row_end"] = int(r_row_end[0])
                r_bucket = r_bucket[1:]
                r_open = r_open[1:]
                r_high = r_high[1:]
                r_low = r_low[1:]
                r_close = r_close[1:]
                r_vol = r_vol[1:]
                r_cnt = r_cnt[1:]
                r_row_start = r_row_start[1:]
                r_row_end = r_row_end[1:]
                merged = True
            else:
                # The carried minute is now COMPLETE (its group ended).
                _flush(carry)
                carry = None

        if r_bucket.size == 0:
            # The whole group was the carried minute — it was merged
            # into ``carry`` above and stays there for the next group.
            row_off += int(o.size)
            continue

        # Everything but the LAST run is complete: append it.
        if r_bucket.size > 1:
            if merged:
                # The merged boundary minute is COMPLETE now — it must
                # be FLUSHED BEFORE the later buckets, or the output
                # would be out of order. An earlier draft appended the
                # later buckets first and flushed the boundary minute
                # after, which broke the monotonicity of the bucket
                # index AND silently dropped every minute that
                # straddled a row-group boundary (3 on the 2025Q1 file).
                _flush(carry)
            b_list.append(r_bucket[:-1])
            o_list.append(r_open[:-1])
            h_list.append(r_high[:-1])
            l_list.append(r_low[:-1])
            c_list.append(r_close[:-1])
            v_list.append(r_vol[:-1])
            n_list.append(r_cnt[:-1])
            rs_list.append(r_row_start[:-1])
            re_list.append(r_row_end[:-1])
            last = r_bucket.size - 1
            carry = {
                "bucket": int(r_bucket[last]),
                "open": float(r_open[last]),
                "high": float(r_high[last]),
                "low": float(r_low[last]),
                "close": float(r_close[last]),
                "vol": float(r_vol[last]),
                "ticks": int(r_cnt[last]),
                "row_start": int(r_row_start[last]),
                "row_end": int(r_row_end[last]),
            }
        elif merged:
            # The group contributed ONLY the carried minute, which was
            # already merged into ``carry``. Do NOT overwrite it.
            pass
        else:
            carry = {
                "bucket": int(r_bucket[0]),
                "open": float(r_open[0]),
                "high": float(r_high[0]),
                "low": float(r_low[0]),
                "close": float(r_close[0]),
                "vol": float(r_vol[0]),
                "ticks": int(r_cnt[0]),
                "row_start": int(r_row_start[0]),
                "row_end": int(r_row_end[0]),
            }
        row_off += int(o.size)

    if carry is not None:
        _flush(carry)

    if not b_list:
        z = np.zeros(0)
        return Bars1m(np.zeros(0, np.int64), z, z, z, z, z,
                      np.zeros(0, np.int64), z, atr_n)

    bucket_all = np.concatenate(b_list)
    time_ns = (bucket_all * NS_PER_MIN).astype(np.int64)
    open_ = np.concatenate(o_list)
    high_ = np.concatenate(h_list)
    low_ = np.concatenate(l_list)
    close_ = np.concatenate(c_list)
    vol_ = np.concatenate(v_list)
    ticks_ = np.concatenate(n_list)
    row_start = np.concatenate(rs_list)
    row_end = np.concatenate(re_list)

    # Sanity: the buckets must be strictly increasing (monotonic), which
    # is what makes the reduceat aggregation valid in the first place.
    if bucket_all.size > 1 and not np.all(np.diff(bucket_all) > 0):
        raise RuntimeError("1m bucket index is not strictly increasing")

    atr = _atr(high_, low_, close_, atr_n)
    bars = Bars1m(
        time_ns=time_ns, open=open_, high=high_, low=low_, close=close_,
        volume=vol_, n_ticks=ticks_, atr=atr, atr_n=atr_n,
    )
    if not with_row_map:
        return bars
    return Bars1mGrid(bars=bars, row_start=row_start, row_end=row_end,
                      path=path, closes_1s=load_1s_closes(path))


# ─────────────────────────────────────────────────────────────────
# The ledger
# ─────────────────────────────────────────────────────────────────

def load_legs(path: Path) -> dict[str, np.ndarray]:
    """Load the canonical ledger's legs as parallel arrays."""
    import json

    payload = json.loads(path.read_text(encoding="utf-8"))
    legs = payload["legs"]
    out: dict[str, np.ndarray] = {}
    for key, values in legs.items():
        arr = np.asarray(values)
        if arr.dtype.kind in "iub":
            out[key] = arr.astype(np.int64)
        else:
            out[key] = arr.astype(np.float64)
    out["_meta"] = payload  # type: ignore[assignment]
    return out


@dataclass(frozen=True)
class Bars1mGrid:
    """The 1-minute bars plus the map back to the 1s FILE ROWS.

    WHY THE MAP IS NEEDED
    =====================

    The canonical ledger computes its DRIFT on the **1s grid**:

        drift_usd = (close[exit_bar] - close[entry_bar]) * side * qty

    where ``entry_bar`` / ``exit_bar`` are FILE ROW indices (verified:
    the ledger's drift reproduces bit-exactly from the parquet's
    ``close`` column).

    A 1m-grid drift is a DIFFERENT number — measured, the two grids
    disagree in SIGN on the whole long book (the 1m grid says
    -$9,715 of drift; the 1s grid says +$11,098). The 1m close is the
    close of the LAST second of a minute, and shifting both window
    endpoints by up to a minute moves a short-horizon drift by as
    much as the drift itself.

    So the drift benchmark has to run on the ledger's own grid, which
    means the map from a 1m bar back to the 1s rows it contains.

    Every field is REQUIRED — no defaults. A missing map silently
    produces a drift number on the wrong grid.
    """

    bars: Bars1m
    #: the FIRST file row of each 1m bar
    row_start: np.ndarray
    #: the LAST file row of each 1m bar
    row_end: np.ndarray
    #: the parquet the rows index into — needed to read the 1s closes
    path: Path
    #: the 1s ``close`` column, in FILE ROW order. 330 MB for the 1y
    #: corpus. Loaded ONCE and shared by every cell — a per-cell
    #: parquet read would be 3,000 reads of the whole column.
    closes_1s: np.ndarray

    def __len__(self) -> int:
        return len(self.bars)


def gather_1s_closes(path: Path, rows: np.ndarray) -> np.ndarray:
    """The 1s ``close`` at each of ``rows`` (FILE ROW indices).

    Reads the parquet one row group at a time and keeps ONLY the
    requested rows, so the peak memory is one row group's close
    column (~8 MB) plus the answer — not the 330 MB a full column
    would need.
    """
    import pyarrow.parquet as pq

    rows = np.asarray(rows, dtype=np.int64)
    if rows.size == 0:
        return np.zeros(0, dtype=np.float64)
    order = np.argsort(rows, kind="stable")
    sorted_rows = rows[order]
    out = np.zeros(rows.size, dtype=np.float64)

    pf = pq.ParquetFile(path)
    rg_start = 0
    for rg in range(pf.metadata.num_row_groups):
        n_rg = int(pf.metadata.row_group(rg).num_rows)
        rg_end = rg_start + n_rg
        # The rows that fall inside this group.
        lo = int(np.searchsorted(sorted_rows, rg_start, side="left"))
        hi = int(np.searchsorted(sorted_rows, rg_end, side="left"))
        if hi > lo:
            arr = (pf.read_row_group(rg, columns=["close"])
                   .column("close").to_numpy(zero_copy_only=False))
            out[order[lo:hi]] = arr.astype(np.float64)[
                sorted_rows[lo:hi] - rg_start]
        rg_start = rg_end
    return out


def fee_bps_from_legs(legs: dict[str, np.ndarray]) -> float:
    """Derive the engine's per-side ``taker_fee_bps`` from the ledger.

    The engine's fee is ``|exit + entry| * 0.5 * qty * (bps/10000) * 2``.
    So ``bps`` can be recovered from any leg. It is asserted to be
    CONSTANT across the book — if it is not, the re-pricing below
    would be using a rate that does not match the artifact.
    """
    fee = legs["fee"]
    ep = legs["entry_price"]
    xp = legs["exit_price"]
    qty = legs["qty"]
    den = np.abs(xp + ep) * 0.5 * qty * 2.0 / 10000.0
    ok = den > 0
    if not np.any(ok):
        raise ValueError("no leg has a non-zero fee denominator")
    bps = fee[ok] / den[ok]
    med = float(np.median(bps))
    spread = float(np.max(np.abs(bps - med)))
    if spread > 1e-9:
        raise ValueError(
            f"the ledger's fee rate is not constant (median {med!r}, "
            f"max deviation {spread:.3e}). The re-pricing assumes one "
            f"rate for the whole book."
        )
    return med


def load_1s_closes(path: Path) -> np.ndarray:
    """Stream the 1s ``close`` column into a flat array.

    ⚠️ THIS IS THE 330 MB VERSION. The gate does NOT use it — the
    gate only needs the close at a handful of specific rows, and
    :func:`gather_1s_closes` reads only those. Kept for the probes.
    """
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(path)
    n_rows = int(pf.metadata.num_rows)
    out = np.empty(n_rows, dtype=np.float64)
    off = 0
    for rg in range(pf.metadata.num_row_groups):
        arr = (pf.read_row_group(rg, columns=["close"])
               .column("close").to_numpy(zero_copy_only=False))
        out[off:off + arr.size] = arr.astype(np.float64)
        off += int(arr.size)
    if off != n_rows:
        raise RuntimeError(f"read {off} closes, expected {n_rows}")
    return out


# ─────────────────────────────────────────────────────────────────
# The gate
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class GateResult:
    """The outcome of gating a book with one TA signal."""

    n_legs: int
    #: the TA signal agreed with the leg's side at the SIGNAL bar
    gate_pass: np.ndarray
    #: the signal still agreed at the FILL (the trade really opened)
    filled: np.ndarray
    #: the signal stopped agreeing -> the trade was closed early
    early_closed: np.ndarray
    #: the exit price under each policy
    exit_price_gate_only: np.ndarray
    exit_price_gate_close: np.ndarray
    exit_time_gate_close: np.ndarray
    #: the P&L under each policy (zero for legs that never opened)
    gross_gate_only: np.ndarray
    gross_gate_close: np.ndarray
    fee_gate_only: np.ndarray
    fee_gate_close: np.ndarray
    net_gate_only: np.ndarray
    net_gate_close: np.ndarray
    #: the DRIFT / EXCESS decomposition, on the 1m grid, for both arms.
    #:
    #: ``drift`` is what a passive hold of the SAME side over the SAME
    #: window would have earned. ``excess = gross - drift`` is the
    #: TIMING component. This is the repo's decisive test: if the
    #: gate's improvement in gross/leg is ALL drift, then the gate is
    #: a BETA filter (it selects the trades that happen to point with
    #: the market) and not an alpha source.
    drift_gate_only: np.ndarray
    drift_gate_close: np.ndarray
    excess_gate_only: np.ndarray
    excess_gate_close: np.ndarray
    #: the number of legs affected
    n_gate_pass: int
    n_filled: int
    n_early_closed: int
    n_gated_out_signal: int
    n_cancelled_at_fill: int


def signal_at(signal: np.ndarray, bars: Bars1m,
              times_ns: np.ndarray) -> np.ndarray:
    """The TA signal in force at each of ``times_ns`` (no lookahead).

    The signal of the last 1m bar whose CLOSE is ``<= t``.
    """
    closed_at = bars.time_ns + NS_PER_MIN
    idx = np.searchsorted(closed_at, np.asarray(times_ns, dtype=np.int64),
                          side="right") - 1
    safe = np.where(idx >= 0, idx, 0)
    out = np.where(idx >= 0, np.asarray(signal)[safe], 0)
    return out.astype(np.int8), idx


def _next_differing(signal: np.ndarray) -> dict[int, np.ndarray]:
    """For each value v, ``r[i]`` = the first index ``j >= i`` with ``signal[j] != v``.

    Length ``n + 1`` (the sentinel ``n`` means "no such index"), so the
    lookup ``r[i + 1]`` is always in range.
    """
    n = int(signal.size)
    pos = np.arange(n, dtype=np.int64)
    out: dict[int, np.ndarray] = {}
    for v in (-1, 0, 1):
        idxs = np.where(signal != v, pos, n)
        rev = np.minimum.accumulate(idxs[::-1])[::-1]
        out[v] = np.append(rev, n)
    return out


def apply_gate(legs: dict[str, np.ndarray], signal: np.ndarray,
               bars: Bars1m, *, bps: float,
               grid: Bars1mGrid | None = None) -> GateResult:
    """Apply the TA gate to a book of legs.

    Parameters
    ----------
    legs:
        The ledger's parallel arrays (``side``, ``qty``, ``entry_price``,
        ``exit_price``, ``signal_time_ns``, ``entry_time_ns``,
        ``exit_time_ns``, ``gross``, ``fee``).
    signal:
        The TA signal on the 1m bars, values in ``{-1, 0, +1}``.
    bars:
        The 1m bars the signal was computed on.
    bps:
        The engine's per-side fee rate.
    grid:
        The 1m bars PLUS the map back to the 1s file rows. REQUIRED for
        the drift/excess decomposition — the drift benchmark has to
        run on the ledger's own 1s grid, and a 1m-grid drift is a
        different number (it disagrees in SIGN on the whole book).

    Returns
    -------
    GateResult
    """
    signal = np.asarray(signal, dtype=np.int8)
    n_m = int(signal.size)
    side = legs["side"].astype(np.int64)
    qty = legs["qty"].astype(np.float64)
    ep = legs["entry_price"].astype(np.float64)
    xp = legs["exit_price"].astype(np.float64)
    st = legs["signal_time_ns"].astype(np.int64)
    et = legs["entry_time_ns"].astype(np.int64)
    xt = legs["exit_time_ns"].astype(np.int64)
    n = int(side.size)

    closed_at = bars.time_ns + NS_PER_MIN

    # ── 1. the gate at the SIGNAL bar ────────────────────────────
    j_sig = np.searchsorted(closed_at, st, side="right") - 1
    safe_sig = np.where(j_sig >= 0, j_sig, 0)
    gate_pass = (j_sig >= 0) & (signal[safe_sig].astype(np.int64) == side)

    # ── 2. the gate at the FILL (the resting order's own re-check) ─
    j_ent = np.searchsorted(closed_at, et, side="right") - 1
    safe_ent = np.where(j_ent >= 0, j_ent, 0)
    filled = gate_pass & (j_ent >= 0) & (
        signal[safe_ent].astype(np.int64) == side)

    # ── 3. the early close ───────────────────────────────────────
    # The first 1m bar AFTER the fill whose signal differs from the
    # leg's side. That bar's CLOSE is when the change becomes known, so
    # the fill is the NEXT bar's open.
    nxt = _next_differing(signal)
    k = np.where(side > 0, nxt[1][safe_ent + 1], nxt[-1][safe_ent + 1])
    k_next = k + 1
    valid = filled & (k < n_m) & (k_next < n_m)
    kc = np.where(valid, k_next, 0)
    early_px = bars.open[kc]
    early_time = bars.time_ns[kc]
    early_wins = valid & (early_time < xt)

    # ── 4. re-price ──────────────────────────────────────────────
    def _px(early: bool) -> np.ndarray:
        return np.where(early, early_px, xp)

    def _gross(px: np.ndarray) -> np.ndarray:
        return (px - ep) * side * qty

    def _fee(px: np.ndarray) -> np.ndarray:
        return np.abs(px + ep) * 0.5 * qty * (bps / 10000.0) * 2.0

    px_only = _px(np.zeros(n, dtype=bool))
    px_close = _px(early_wins)

    gross_only = _gross(px_only)
    gross_close = _gross(px_close)
    fee_only = _fee(px_only)
    fee_close = _fee(px_close)

    # ── 5. the DRIFT / EXCESS decomposition ──────────────────────
    # The repo's decisive test: is the gate's improvement in gross
    # SKILL, or is it just MARKET DRIFT that the gate happens to
    # select?
    #
    # ``drift`` = what a passive hold of the SAME side over the SAME
    # window would have earned. ``excess = gross - drift`` is the
    # TIMING component.
    #
    # The window is [entry, exit] ON THE 1s GRID, which is the
    # ledger's own definition (the ledger's ``drift_usd`` reproduces
    # bit-exactly from the parquet's close column, verified).
    #
    # ⚠️ A 1m-grid drift is NOT the same number. Measured: the two
    # grids disagree in SIGN on the whole long book (1m grid says
    # -$9,715 of drift; 1s grid says +$11,098). The 1m close is the
    # close of the LAST second of a minute, and shifting both window
    # endpoints by up to a minute moves a short-horizon drift by as
    # much as the drift itself. So the benchmark MUST be on the 1s
    # grid.
    #
    # The two arms have DIFFERENT windows, because the early close
    # changes the exit: the gate-only arm holds to the natural exit,
    # the gate+close arm holds to the early exit.
    if grid is None:
        raise ValueError(
            "apply_gate needs a Bars1mGrid (with the 1s row map) to "
            "compute the drift on the ledger's own grid."
        )

    # The 1s FILE ROW of each leg's entry and exit.
    #   - the entry row is the ledger's own ``entry_bar`` (a file row
    #     index), so the gate-only drift reproduces the ledger's
    #     drift EXACTLY on an ungated book;
    #   - the exit row is the ledger's own ``exit_bar``.
    eb_row = legs["entry_bar"].astype(np.int64)
    xb_row = legs["exit_bar"].astype(np.int64)

    # The early close's exit row: the NEXT 1m bar's OPEN is the fill,
    # so the 1s row is that bar's FIRST row.
    early_rows = grid.row_start[np.clip(k_next, 0, grid.row_start.size - 1)]

    # The 1s closes at the four endpoints we need.
    c_entry = grid.closes_1s[eb_row]
    c_exit = grid.closes_1s[xb_row]
    c_early = grid.closes_1s[early_rows]

    def _drift(exit_close: np.ndarray) -> np.ndarray:
        """The passive hold over [entry_bar, exit_bar] on the 1s grid."""
        d = (exit_close - c_entry) * side * qty
        return np.where(filled, d, 0.0)

    drift_only = _drift(c_exit)
    drift_close = _drift(np.where(early_wins, c_early, c_exit))
    excess_only = np.where(filled, gross_only - drift_only, 0.0)
    excess_close = np.where(filled, gross_close - drift_close, 0.0)

    # Legs that never opened contribute ZERO, not their original P&L.
    gross_only = np.where(filled, gross_only, 0.0)
    gross_close = np.where(filled, gross_close, 0.0)
    fee_only = np.where(filled, fee_only, 0.0)
    fee_close = np.where(filled, fee_close, 0.0)

    exit_time_close = np.where(early_wins, early_time, xt)

    n_gate_pass = int(gate_pass.sum())
    n_filled = int(filled.sum())
    n_early = int(early_wins.sum())
    return GateResult(
        n_legs=n,
        gate_pass=gate_pass,
        filled=filled,
        early_closed=early_wins,
        exit_price_gate_only=px_only,
        exit_price_gate_close=px_close,
        exit_time_gate_close=exit_time_close,
        gross_gate_only=gross_only,
        gross_gate_close=gross_close,
        fee_gate_only=fee_only,
        fee_gate_close=fee_close,
        net_gate_only=gross_only - fee_only,
        net_gate_close=gross_close - fee_close,
        drift_gate_only=drift_only,
        drift_gate_close=drift_close,
        excess_gate_only=excess_only,
        excess_gate_close=excess_close,
        n_gate_pass=n_gate_pass,
        n_filled=n_filled,
        n_early_closed=n_early,
        n_gated_out_signal=n - n_gate_pass,
        n_cancelled_at_fill=n_gate_pass - n_filled,
    )
