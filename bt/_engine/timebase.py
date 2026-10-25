"""Canonical timebase conversion — ONE place that decides timestamp units.

Why this module exists
======================

The canonical XAUUSD parquet files store ``time`` as
``timestamp[ms, tz=UTC]``. pandas preserves that resolution, so::

    df["time"].astype("int64")   # -> 1_784_143_874_000   (MILLISECONDS)

but three of the four engine builders (``rolling_p95``,
``volume_profile``, ``swing_cap``) and the engine's own bar loop
all reason in **nanoseconds**::

    cutoff = t_ns - lookback_secs * 1_000_000_000   # NANOSECONDS

Mixing the two is silent and catastrophic: a nanosecond-magnitude
cutoff is ~10^6 times larger than a millisecond-magnitude
timestamp, so every "is this bar inside my window?" test fails in
the same direction forever.

This is not hypothetical — it is the reason the canonical run
emitted 264,666 signals and closed **0 trades**: the NB91 p95 cap
computed ``warmup_until_ns`` in real nanoseconds while the engine
handed it millisecond timestamps, so ``is_warmup_complete`` was
``False`` on every bar of the file and
``skip_trades_during_warmup`` discarded every single entry.

The convention going forward
----------------------------

**Every timestamp that crosses a module boundary in this repo is
int64 NANOSECONDS since the UNIX epoch.** ``to_ns_arr`` is the
only sanctioned way to get one from a bars DataFrame. Never call
``.astype("int64")`` on a datetime column directly — that is what
made the bug invisible in the first place.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

__all__ = ["to_ns_arr", "to_ns_int", "assert_ns"]


def to_ns_int(value) -> int:
    """Coerce one scalar (datetime | Timestamp | int) to epoch nanoseconds."""
    if isinstance(value, (int, np.integer)):
        # Already an integer. Interpret as NANOSECONDS (repo convention).
        return int(value)
    ts = pd.Timestamp(value)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    return int(ts.value)


def to_ns_arr(bars: pd.DataFrame, column: str = "time") -> np.ndarray:
    """Return ``bars[column]`` as an int64 array of epoch NANOSECONDS.

    This is the single sanctioned conversion. It is unit-safe
    regardless of whether the source column is ``datetime64[s]``,
    ``[ms]``, ``[us]`` or ``[ns]`` — pandas carries the resolution
    in the dtype and ``as_unit``/``astype`` normalizes it up to ns.

    Parameters
    ----------
    bars : pd.DataFrame
        Bars frame with a tz-aware datetime ``column``.
    column : str
        The time column name. Default ``"time"``.

    Returns
    -------
    np.ndarray
        int64 array of epoch nanoseconds, UTC.

    Raises
    ------
    ValueError
        If the column is missing, or holds non-datetime values.
    """
    if column not in bars.columns:
        raise ValueError(
            f"bars is missing the {column!r} column "
            f"(have: {list(bars.columns)})"
        )
    col = bars[column]
    if not pd.api.types.is_datetime64_any_dtype(col):
        raise ValueError(
            f"bars[{column!r}] must be datetime64, got {col.dtype}. "
            "Parse it with pd.to_datetime(..., utc=True) first."
        )
    # Normalize to ns regardless of the stored resolution. `as_unit` is
    # the cheap path; `astype` is the fallback for older pandas where
    # DatetimeArray.as_unit is unavailable.
    if col.dtype.kind == "M":
        try:
            return np.asarray(col.array.as_unit("ns").astype("int64"),
                              dtype=np.int64)
        except AttributeError:
            return np.asarray(
                col.astype("datetime64[ns, UTC]").astype("int64"),
                dtype=np.int64,
            )
    # Already integer (ns by repo convention).
    return np.asarray(col.to_numpy(), dtype=np.int64)


def assert_ns(arr: np.ndarray, label: str = "timestamps") -> None:
    """Sanity-check that ``arr`` really holds nanosecond timestamps.

    Cheap guard against the ms/ns mix-up recurring: any sane
    nanosecond timestamp for a real bar file falls in
    ``[1e18, 1e19)`` (2001-09-09 .. ~2286-11-20). Milliseconds
    land around ``1e12`` and would trip this.
    """
    if arr.size == 0:
        return
    probe = int(arr[0])
    if not (1e18 <= abs(probe) < 1e19):
        raise ValueError(
            f"{label} does not look like epoch NANOSECONDS "
            f"(first value {probe} = {probe / 1e12:.1f}e12). "
            "Use bt._engine.timebase.to_ns_arr() to convert."
        )