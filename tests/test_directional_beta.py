"""Tests for the directional-beta diagnostic.

The diagnostic is what proved the canonical recipe is a market-beta
proxy rather than a timing edge (see ``docs/nb95_beta_proxy.md``).

These tests guard the *analysis*, not the strategy. A bug in the
analysis would produce a wrong verdict about whether the recipe is
tradeable, which is exactly the class of bug that must not be silent.
"""
from __future__ import annotations

import numpy as np
import pytest


# ─────────────────────────────────────────────────────────────────────
# 1. The analysis functions
# ─────────────────────────────────────────────────────────────────────
def _mk(label: str, mkt_ret: float, gross: float, ln: int = 0, sn: int = 0,
        lgross: float = 0.0, sgross: float = 0.0) -> dict:
    return {
        "label": label,
        "mkt": {"mkt_ret_pct": mkt_ret, "mkt_start": 100.0, "mkt_end": 100.0},
        "n_trades": ln + sn,
        "gross": gross,
        "net": gross,
        "fees": 0.0,
        "long": {"n": ln, "gross": lgross, "net": lgross, "fees": 0.0,
                 "hit": 0.5, "mean_hold": 0.0, "median_hold": 0.0},
        "short": {"n": sn, "gross": sgross, "net": sgross, "fees": 0.0,
                  "hit": 0.5, "mean_hold": 0.0, "median_hold": 0.0},
    }


def test_beta_analysis_detects_a_clean_beta_proxy():
    """A pure direction bet must show a high r2 and a ~zero alpha."""
    from runtime.diagnostics.directional_beta import _beta_analysis

    # Gross P&L is a perfect multiple of the market's return.
    results = [
        _mk("up1", +10.0, +5000.0),
        _mk("up2", +5.0, +2500.0),
        _mk("dn1", -10.0, -5000.0),
        _mk("dn2", -5.0, -2500.0),
    ]
    b = _beta_analysis(results)
    assert b["beta_usd_per_pct"] == pytest.approx(500.0, rel=1e-6)
    assert b["alpha_usd_per_slice"] == pytest.approx(0.0, abs=1e-6)
    assert b["r2"] == pytest.approx(1.0, abs=1e-9)


def test_beta_analysis_detects_a_pure_alpha():
    """A strategy that makes a constant $ regardless of the market."""
    from runtime.diagnostics.directional_beta import _beta_analysis

    results = [
        _mk("a", +10.0, +1000.0),
        _mk("b", -8.0, +1000.0),
        _mk("c", +2.0, +1000.0),
    ]
    b = _beta_analysis(results)
    assert b["beta_usd_per_pct"] == pytest.approx(0.0, abs=1e-6)
    assert b["alpha_usd_per_slice"] == pytest.approx(1000.0, rel=1e-6)
    # Constant P&L -> zero variance explained.
    assert b["r2"] == pytest.approx(0.0, abs=1e-9)


def test_beta_analysis_flags_unprofitable_flat_slices():
    """The flat-market test must actually fire on flat losers."""
    from runtime.diagnostics.directional_beta import _beta_analysis

    results = [
        _mk("flat1", -0.1, -300.0),
        _mk("flat2", +0.5, -3000.0),
        _mk("flat3", -1.5, -400.0),
        _mk("trend", +12.0, +6000.0),
    ]
    b = _beta_analysis(results)
    assert b["flat_n"] == 3
    assert b["flat_gross_total"] == pytest.approx(-3700.0)
    assert b["flat_positive_count"] == 0
    # The trending slice must NOT be in the flat set.
    assert "trend" not in b["flat_slices"]


def test_side_analysis_measures_each_side_separately():
    """The per-side regression must use only the slices where it traded."""
    from runtime.diagnostics.directional_beta import _side_analysis

    results = [
        # Short trades only, and they lose when the market rises.
        _mk("s1", +10.0, -1000.0, ln=0, sn=100, sgross=-1000.0),
        _mk("s2", -10.0, +1000.0, ln=0, sn=100, sgross=+1000.0),
        _mk("s3", +5.0, -500.0, ln=0, sn=100, sgross=-500.0),
    ]
    s = _side_analysis(results)
    assert s["short"]["n_slices"] == 3
    # Short gross falls as the market rises -> NEGATIVE beta.
    assert s["short"]["beta_usd_per_pct"] < 0
    # The long side never traded -> no regression.
    assert "beta_usd_per_pct" not in s["long"]


def test_side_analysis_ignores_slices_with_trivial_activity():
    """A side with < 20 trades in a slice must be excluded from the fit."""
    from runtime.diagnostics.directional_beta import _side_analysis

    results = [
        _mk("big", +10.0, -1000.0, ln=0, sn=500, sgross=-1000.0),
        _mk("tiny", -10.0, +9999.0, ln=0, sn=3, sgross=+9999.0),
    ]
    s = _side_analysis(results)
    assert s["short"]["n_slices"] == 1, (
        "the slice with 3 short trades must be excluded"
    )


# ─────────────────────────────────────────────────────────────────────
# 2. The measured verdict (a regression on the diagnosis itself)
# ─────────────────────────────────────────────────────────────────────
def test_measured_canonical_recipe_is_a_beta_proxy():
    """The canonical recipe's measured slices must show the beta signature.

    This is a regression on the *diagnosis*. If a future engine change
    flips these numbers, the conclusion "the recipe is not tradeable"
    needs to be re-examined — so the test should fail loudly rather
    than let the conclusion rot.

    The measured data (2026-10-22, 8 monthly slices of the 1y corpus):
    """
    slices = [
        ("2024-06", -0.11, -345.38, 0, 650),
        ("2024-11", -3.52, +645.45, 2, 1184),
        ("2025-01", +6.58, +2793.33, 735, 0),
        ("2025-05", +0.50, -3071.21, 0, 2290),
        ("2025-09", +12.06, +5004.43, 2131, 0),
        ("2026-03", -12.59, +15704.22, 2417, 3486),
        ("2026-04", -1.48, -321.66, 0, 3839),
        ("2026-01", +13.21, -8638.22, 220, 5256),
    ]
    from runtime.diagnostics.directional_beta import _beta_analysis

    results = [
        _mk(label, mkt, gross, ln=ln, sn=sn)
        for (label, mkt, gross, ln, sn) in slices
    ]
    b = _beta_analysis(results)

    # 1. The short side is ANTI-correlated with the market.
    assert b["beta_usd_per_pct"] < 0, (
        "the canonical recipe's gross P&L must fall as the market "
        "rises — that is the beta-proxy signature"
    )
    # 2. It is NOT profitable in flat markets.
    assert b["flat_n"] == 3
    assert b["flat_positive_count"] == 0, (
        "the canonical recipe must NOT be profitable in flat markets; "
        "if this now passes, re-verify the whole diagnosis"
    )
    assert b["flat_gross_total"] < 0
    # 3. Most slices are effectively one-directional (the lock).
    #    Measured: 5 of 8 have the minority side at ZERO trades; with
    #    a 5% tolerance (the minority side is < 5% of all trades) it
    #    is 7 of 8. The one genuinely two-sided slice (2026-03) is
    #    the one where the market fell hardest.
    def _minority_share(r: dict) -> float:
        lo, sh = r["long"]["n"], r["short"]["n"]
        if lo + sh == 0:
            return 0.0
        return min(lo, sh) / (lo + sh)

    strictly_one = sum(
        1 for r in results
        if r["long"]["n"] == 0 or r["short"]["n"] == 0)
    effectively_one = sum(
        1 for r in results if _minority_share(r) < 0.05)
    assert strictly_one >= 5, (
        f"expected >=5 strictly one-directional slices, got {strictly_one}"
    )
    assert effectively_one >= 7, (
        f"expected >=7 effectively one-directional slices (<5% minority "
        f"side), got {effectively_one}"
    )


def test_directional_beta_module_imports_cleanly():
    import runtime.diagnostics.directional_beta as db

    assert callable(db.run_slice)
    assert callable(db._beta_analysis)
    assert callable(db._side_analysis)


def test_default_slots_cover_up_down_and_flat_regimes():
    """The diagnostic's default slice list must span the regimes."""
    from runtime.diagnostics.directional_beta import DEFAULT_SLOTS

    assert len(DEFAULT_SLOTS) >= 6
    # The labels should name the regime so a reader can see the design.
    labels = " ".join(s[0] for s in DEFAULT_SLOTS).lower()
    assert "up" in labels
    assert "down" in labels
    assert "flat" in labels
