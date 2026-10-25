"""Guards for the enzo RSI level-set report artifact.

The artifact at ``runtime/diagnostics/_reports/enzo_rsi_levels.json``
is the FULL-CORPUS evidence record that ``docs/enzo_strategies.md``
section 1a cites. It must not be silently replaced by a short-window
run.

This is the same class of protection the repo already applies to the
canonical leg ledger (``leg_ledger._guard_canonical_path``): an
artifact that every reader treats as "the canonical result" must be
structurally unable to hold anything else.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from runtime.diagnostics import enzo_rsi_levels


REPORT_PATH = enzo_rsi_levels.REPORT_PATH


def test_the_canonical_window_may_write_the_canonical_report():
    """The guard does not obstruct the canonical full-corpus build."""
    enzo_rsi_levels._guard_canonical_report(
        out_path=REPORT_PATH,
        start=enzo_rsi_levels.CANONICAL_START,
        end=enzo_rsi_levels.CANONICAL_END,
    )


def test_a_short_window_may_not_write_the_canonical_report():
    """A one-month run must not overwrite the 927-day measurement."""
    with pytest.raises(SystemExit) as e:
        enzo_rsi_levels._guard_canonical_report(
            out_path=REPORT_PATH,
            start="2025-01-01",
            end="2025-02-01",
        )
    msg = str(e.value)
    assert "REFUSING" in msg
    # The message must say how to proceed, not just complain.
    assert "--out" in msg
    assert "--allow-variant-canonical" in msg


def test_a_short_window_may_write_its_own_file():
    """A variant report to its own path is the NORMAL case."""
    enzo_rsi_levels._guard_canonical_report(
        out_path=REPORT_PATH.with_name("enzo_rsi_levels_variant.json"),
        start="2025-01-01",
        end="2025-02-01",
    )


def test_the_escape_hatch_is_explicit():
    """The deliberate replacement must be asserted, never inferred."""
    enzo_rsi_levels._guard_canonical_report(
        out_path=REPORT_PATH,
        start="2025-01-01",
        end="2025-02-01",
        allow_variant_canonical=True,
    )


def test_the_canonical_report_on_disk_is_the_canonical_window():
    """The artifact the doc cites must BE the canonical measurement.

    Without this, a short run that bypassed the guard (e.g. written
    by an older version of the script) would leave the doc and the
    artifact quietly disagreeing.
    """
    if not REPORT_PATH.exists():
        pytest.skip("canonical report not built yet")
    payload = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    window = payload["window"]
    assert window["start"] == enzo_rsi_levels.CANONICAL_START
    assert window["end"] == enzo_rsi_levels.CANONICAL_END


def test_the_canonical_window_is_the_full_corpus():
    """Guard against the canonical window itself drifting to a slice."""
    from datetime import date

    start = date.fromisoformat(enzo_rsi_levels.CANONICAL_START)
    end = date.fromisoformat(enzo_rsi_levels.CANONICAL_END)
    assert (end - start).days > 700, (
        "the canonical enzo window must span the full corpus; a short "
        "window would make the report a monthly draw dressed up as an "
        "evidence record")
