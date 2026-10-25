"""Tests for the SWEEP POLICY (2026-10-25).

The repo's operating rule is:

* **Sweep ONE month by default.** The recipe is high-frequency
  (~40 legs/month after the vol gate), so a multi-month sweep is
  many draws of a high-variance distribution and costs minutes per
  cell to produce a verdict nobody reads.
* **Always report the full-run cost before running it**, and
  **always ask before a long run.**

The second rule is only real if it is MECHANICAL. A rule that lives
only in prose is a rule that gets skipped the first time somebody is
in a hurry. So the driver implements an **approval gate**:

* the default window is one month;
* a window longer than one month prints its estimated cost and
  **exits without running**, unless ``--i-know`` is passed;
* the estimate is computed from the parquet FOOTER alone, so the
  pre-flight costs ~0 s even on the 41M-bar 1y file.

These tests lock all three properties, plus the failure mode that
motivated them: the old default was ``--start None --end None``,
which silently loaded the **entire 1y corpus** (41M bars, ~2 GB) for
a sweep that was almost always read off one month.

Run with::

    python -m pytest tests/test_sweep_policy.py -v
"""
from __future__ import annotations

import datetime as _dt
import inspect
from pathlib import Path

import pytest

from runtime.sweeps import cached_sweep, cost_estimate

REPO_ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────────
# 1. The default window is ONE MONTH, not the whole corpus.
# ─────────────────────────────────────────────────────────────────
def test_default_window_is_one_month_not_the_whole_corpus():
    """The sweep driver must default to a one-month window.

    The old default (``--start None --end None``) loaded the entire
    1y corpus: 41,233,730 bars and ~2 GB of RAM, for a sweep that
    was almost always read off one month. That is the mechanism
    behind "sweeps happily, progress takes too long".
    """
    src = inspect.getsource(cached_sweep.main)
    assert "DEFAULT_START" in src, (
        "the sweep driver must import the one-month default window"
    )
    assert "DEFAULT_END" in src

    # The defaults themselves must be a ONE-MONTH window.
    s = _dt.date.fromisoformat(cost_estimate.DEFAULT_START)
    e = _dt.date.fromisoformat(cost_estimate.DEFAULT_END)
    days = (e - s).days
    assert 28 <= days <= 31, (
        f"the default window must be one month, got {days} days"
    )


# ─────────────────────────────────────────────────────────────────
# 2. The approval gate exists and is wired to the CLI.
# ─────────────────────────────────────────────────────────────────
def test_the_driver_has_an_approval_gate_flag():
    """``--i-know`` must exist and must gate long windows."""
    src = inspect.getsource(cached_sweep.main)
    assert '"--i-know"' in src, (
        "the sweep driver must expose the --i-know approval flag"
    )
    assert "args.i_know" in src, (
        "the --i-know flag must actually be consulted, not just declared"
    )
    # And the gate must ABORT rather than merely warn.
    assert "raise SystemExit(2)" in src, (
        "a long window without --i-know must exit non-zero, not just "
        "print a warning"
    )


def test_the_gate_cannot_be_bypassed_by_omitting_the_dates():
    """The gate is keyed on the WINDOW LENGTH, not on the flags used.

    A gate that only triggers when the user passes explicit dates
    would be trivially bypassed by omitting them -- which is exactly
    what the old default did.
    """
    src = inspect.getsource(cached_sweep.main)
    # The gate must consult the estimator, which measures the window.
    assert "estimate_sweep" in src
    assert "needs_approval" in src


# ─────────────────────────────────────────────────────────────────
# 3. The estimator's arithmetic.
# ─────────────────────────────────────────────────────────────────
def test_the_estimator_flags_a_long_window_and_not_a_short_one():
    """A one-month window needs no approval; a 2.5-year window does."""
    short = cost_estimate.SweepEstimate(
        start="2025-01-01", end="2025-02-01", days=31, n_bars=1_377_190,
        n_cells=1, artifacts_cached=True, load_secs=0.2,
        artifact_secs=0.0, engine_secs_per_cell=2.0,
    )
    assert not short.needs_approval
    assert not short.is_full_corpus

    long = cost_estimate.SweepEstimate(
        start="2024-01-01", end="2026-07-16", days=927, n_bars=41_233_730,
        n_cells=1, artifacts_cached=False, load_secs=7.0,
        artifact_secs=97.2, engine_secs_per_cell=58.5,
    )
    assert long.needs_approval
    assert long.is_full_corpus


def test_the_estimator_scales_with_the_number_of_cells():
    """A 60-cell sweep must not be quoted the price of a 1-cell sweep."""
    one = cost_estimate.SweepEstimate(
        start="2025-01-01", end="2025-02-01", days=31, n_bars=1_377_190,
        n_cells=1, artifacts_cached=True, load_secs=0.2,
        artifact_secs=0.0, engine_secs_per_cell=2.0,
    )
    sixty = cost_estimate.SweepEstimate(
        start="2025-01-01", end="2025-02-01", days=31, n_bars=1_377_190,
        n_cells=60, artifacts_cached=True, load_secs=0.2,
        artifact_secs=0.0, engine_secs_per_cell=2.0,
    )
    assert sixty.total_secs > one.total_secs * 50


def test_a_warm_cache_must_not_charge_for_the_cold_build():
    """The artifact build is the dominant cold cost -- it must show up."""
    cold = cost_estimate.SweepEstimate(
        start="2024-01-01", end="2026-07-16", days=927, n_bars=41_233_730,
        n_cells=1, artifacts_cached=False, load_secs=7.0,
        artifact_secs=97.2, engine_secs_per_cell=58.5,
    )
    warm = cost_estimate.SweepEstimate(
        start="2024-01-01", end="2026-07-16", days=927, n_bars=41_233_730,
        n_cells=1, artifacts_cached=True, load_secs=7.0,
        artifact_secs=0.0, engine_secs_per_cell=58.5,
    )
    assert cold.total_secs > warm.total_secs
    assert "COLD" in cold.render()
    assert "WARM" in warm.render()


def test_the_render_block_mentions_the_approval_flag():
    """The pre-flight must tell the user how to proceed."""
    est = cost_estimate.SweepEstimate(
        start="2024-01-01", end="2026-07-16", days=927, n_bars=41_233_730,
        n_cells=1, artifacts_cached=False, load_secs=7.0,
        artifact_secs=97.2, engine_secs_per_cell=58.5,
    )
    text = est.render()
    assert "--i-know" in text
    assert "ESTIMATED" in text


# ─────────────────────────────────────────────────────────────────
# 4. The estimator must be exact AND cheap.
# ─────────────────────────────────────────────────────────────────
def test_the_bar_count_comes_from_the_time_column_not_the_footer():
    """The pre-flight must be cheap enough to run every time.

    An earlier version estimated the count from the parquet footer's
    row-group statistics, interpolating within the straddling group.
    That was rejected: FX bars are NOT uniform in wall-clock time
    (weekend gaps), so the interpolation was off by -6% to +8%
    depending on the window. The count must be EXACT.
    """
    src = inspect.getsource(cost_estimate._count_bars)
    assert 'read(columns=["time"])' in src, (
        "the bar count must read only the time column, not the "
        "footer and not the price columns"
    )
    # It must NOT read the price/volume columns -- that is the 2 GB.
    assert 'read()' not in src
    assert "read(columns=[\"time\"])" in src


def test_the_bar_count_matches_the_real_bar_count_on_a_real_file():
    """The estimate must be EXACT, not approximate."""
    data = cached_sweep.DEFAULT_DATA
    if not data.exists():
        pytest.skip(f"corpus not present: {data}")

    from runtime.sweeps.cached_sweep import load_bars

    for start, end in [("2025-01-01", "2025-02-01"),
                       ("2025-06-01", "2025-07-01"),
                       ("2024-01-01", "2026-07-16")]:
        est = cost_estimate._count_bars(data, start, end)
        real = len(load_bars(data, start, end))
        assert est == real, (
            f"bar count must be exact for {start}..{end}: "
            f"estimated {est:,} vs real {real:,}"
        )


def test_the_bar_count_is_cheap_on_the_full_corpus():
    """Reading the time column must cost well under the full load.

    The full 1y load costs ~7 s and ~2 GB. The time column alone is
    ~0.5 s, so the pre-flight is essentially free next to the run it
    is guarding.
    """
    data = cached_sweep.DEFAULT_DATA
    if not data.exists():
        pytest.skip(f"corpus not present: {data}")

    import time

    t0 = time.perf_counter()
    cost_estimate._count_bars(data, "2024-01-01", "2026-07-16")
    elapsed = time.perf_counter() - t0
    assert elapsed < 3.0, (
        f"the pre-flight bar count took {elapsed:.1f} s -- it must be "
        f"much cheaper than the ~7 s full load it replaces"
    )
