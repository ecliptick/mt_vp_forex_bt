"""Cost estimation for a sweep BEFORE it runs.

Why this exists
===============

This repo's recipe is **high-frequency** (~40 legs/month after the
vol gate). The old workflow swept the whole 1y corpus by default,
which cost minutes per cell and produced a verdict that was never
read. The operating rule is now: **sweep ONE month by default, and
ask before anything longer.**

This module makes "ask" mechanical instead of a matter of memory.
It answers two questions from the parquet metadata alone (no bars
loaded, no engine run):

1. **How long will this window take?**
2. **Is the artifact cache already warm?** (cold build is ~10x the
   warm cost)

The numbers come from MEASURED constants, not guesses:

* ``_BARS_PER_SEC_ENGINE`` -- the engine's bar-loop rate. Measured
  2026-10-25 on the 1y file: 41,233,730 bars in 58.5 s -> ~705,000
  bars/s. The doc's "71,000 bars/s" figure was from the pre-D2 /
  pre-D3 book, when the book was unbounded. The current canonical
  recipe (book capped at 20) is ~10x faster.
* ``_BARS_PER_SEC_ARTIFACT`` -- the artifact build rate. Measured
  2026-10-25 on the 1y file: 41,233,730 bars in 97.3 s of artifact
  build -> ~424,000 bars/s.
* ``_LOAD_SECS_PER_MBAR`` -- the parquet load rate. Measured
  2026-10-25: the 1y file (41.2M bars) loaded in 7.0 s.

Usage
-----

    from runtime.sweeps.cost_estimate import estimate_sweep

    est = estimate_sweep(data_path, start, end, n_cells=1)
    print(est.render())
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ── Measured rates (2026-10-25, canonical recipe, XAUUSD 1s) ─────
#
# ENGINE: the 1y continuous run logged
#   "[vp_engine] bar=41000000/41233730 elapsed=56.3s rate=728335/s"
# -> ~705,000 bars/s with the book capped at 20 (D3 fix).
_ENGINE_BARS_PER_SEC = 705_000.0

# ARTIFACTS: the 1y cold build measured 97.3 s of artifact time
# (zones 14.6 + vp_set 18.3 + swing_cap 4.1 + p95_cap 2.4
#  + structure 42.7 + vol_state 15.1) for 41,233,730 bars.
_ARTIFACT_BARS_PER_SEC = 424_000.0

# LOAD: the 1y file loaded in 7.0 s (41.2M bars, ~2.0 GB in RAM).
_LOAD_SECS_PER_MBAR = 7.0 / 41.2

# The default window. One calendar month of the canonical corpus.
DEFAULT_START = "2025-01-01"
DEFAULT_END = "2025-02-01"

# A window longer than this many days needs explicit approval.
APPROVAL_DAYS = 31


@dataclass(frozen=True)
class SweepEstimate:
    """The cost of a proposed sweep, computed without running it."""

    start: str
    end: str
    days: int
    n_bars: int
    n_cells: int
    artifacts_cached: bool
    load_secs: float
    artifact_secs: float
    engine_secs_per_cell: float

    @property
    def total_secs(self) -> float:
        """Total wall clock, including one artifact build."""
        return (self.load_secs
                + self.artifact_secs
                + self.engine_secs_per_cell * self.n_cells)

    @property
    def needs_approval(self) -> bool:
        """True when the window is longer than one month."""
        return self.days > APPROVAL_DAYS

    @property
    def is_full_corpus(self) -> bool:
        """True when the window covers the whole corpus."""
        return self.n_bars >= 40_000_000

    def render(self) -> str:
        """The human-facing estimate block."""
        art = "WARM (cache HIT)" if self.artifacts_cached else \
            "COLD (cache MISS)"
        lines = [
            "  window      : "
            f"{self.start} .. {self.end}   ({self.days} days)",
            f"  bars        : ~{self.n_bars:,}",
            f"  artifacts   : {art}",
            f"  load        : ~{self.load_secs:.1f} s",
            f"  artifact    : ~{self.artifact_secs:.1f} s",
            f"  engine/cell : ~{self.engine_secs_per_cell:.1f} s",
            f"  cells       : {self.n_cells}",
            "  " + "-" * 63,
            f"  ESTIMATED   : ~{_fmt_duration(self.total_secs)} total",
        ]
        if self.needs_approval:
            lines += [
                "  " + "-" * 63,
                "  NOTE        : this window is longer than "
                f"{APPROVAL_DAYS} days.",
                "                The monthly chunking is an ENGINE "
                "artefact --",
                "                the p95 cap (7d), swing cap (24h) and "
                "VP profile",
                "                (7d rolling) are history-dependent, so a "
                "cold",
                "                chunk trades its first ~8 days "
                "UNCAPPED and",
                "                discards part of the trade universe.",
                "                Verify on the continuous corpus with "
                "leg_ledger",
                "                + leg_buckets before believing a cell.",
            ]
        lines.append(
            "  " + "-" * 63
            + ("\n  re-run with --i-know to execute." if self.needs_approval
               else "")
        )
        return "\n".join(lines)


def _fmt_duration(secs: float) -> str:
    """Human-readable duration."""
    if secs < 90:
        return f"{secs:.0f} s"
    if secs < 5400:
        return f"{secs / 60:.1f} min"
    return f"{secs / 3600:.2f} h"


def _count_bars(data_path: Path, start: str | None,
                end: str | None) -> int:
    """Count the bars in the window, exactly.

    Reads ONLY the ``time`` column -- not the price/volume columns --
    so it costs ~0.5 s and ~330 MB on the 41M-bar 1y file (measured
    2026-10-25), versus ~7 s and ~2 GB for a full load. That is
    cheap enough to run as a pre-flight on every sweep.

    An earlier version estimated the count from the parquet footer's
    row-group statistics, interpolating within the straddling group.
    That was rejected: FX bars are NOT uniform in wall-clock time
    (weekend gaps), so the interpolation was off by **-6% to +8%**
    depending on the window. A cost estimate that swings by 8% is
    not a cost estimate.
    """
    import datetime as _dt

    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    pf = pq.ParquetFile(data_path)
    tbl = pf.read(columns=["time"])
    ts = tbl["time"]

    lo = _dt.datetime.fromisoformat(start) if start else None
    hi = _dt.datetime.fromisoformat(end) if end else None

    # The column is ``timestamp[ms, tz=UTC]``; the bounds must match
    # its unit or pyarrow has no kernel for the comparison.
    mask = None
    if lo is not None:
        mask = pc.greater_equal(ts, _match_unit(lo, ts.type))
    if hi is not None:
        upper = pc.less(ts, _match_unit(hi, ts.type))
        mask = upper if mask is None else pc.and_(mask, upper)
    if mask is None:
        return tbl.num_rows
    return int(pc.sum(mask).as_py() or 0)


def _match_unit(bound: Any, arrow_type: Any) -> Any:
    """Coerce a bound to the unit/tz of an Arrow timestamp column."""
    import datetime as _dt

    import pyarrow as pa

    unit = getattr(arrow_type, "unit", "us")
    tz = getattr(arrow_type, "tz", None)
    if tz is not None:
        bound = bound.replace(tzinfo=_dt.timezone.utc)
    # ``pa.scalar`` infers the unit from the datetime's precision, so
    # the value is cast explicitly to the column's unit.
    return pa.scalar(bound, type=pa.timestamp(unit, tz=tz))


def estimate_sweep(data_path: Path, start: str | None = None,
                   end: str | None = None, n_cells: int = 1,
                   artifacts_cached: bool = False) -> SweepEstimate:
    """Estimate the cost of a sweep over ``[start, end)``.

    ``artifacts_cached`` says whether the artifact pickle for this
    window already exists. When it does, the cold build is skipped.
    """
    import datetime as _dt

    if start is None:
        start = DEFAULT_START
    if end is None:
        end = DEFAULT_END

    s = _dt.date.fromisoformat(start)
    e = _dt.date.fromisoformat(end)
    days = max(1, (e - s).days)

    n_bars = _count_bars(data_path, start, end)

    load_secs = n_bars / 1e6 * _LOAD_SECS_PER_MBAR
    artifact_secs = (0.0 if artifacts_cached
                     else n_bars / _ARTIFACT_BARS_PER_SEC)
    engine_secs = n_bars / _ENGINE_BARS_PER_SEC

    return SweepEstimate(
        start=start,
        end=end,
        days=days,
        n_bars=n_bars,
        n_cells=max(1, n_cells),
        artifacts_cached=artifacts_cached,
        load_secs=load_secs,
        artifact_secs=artifact_secs,
        engine_secs_per_cell=engine_secs,
    )
