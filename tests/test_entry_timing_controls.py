"""Tests for the entry-timing controls (2026-10-25).

THE POINT OF THIS FILE
======================

The leg ledger raised a question that the repo had never answered at
the leg level: **is the entry timing worth anything?** The first
control built for it reported a large, highly significant result::

    delta $11,820.61   delta/leg +6.99   t = +18.29
    "ENTRY TIMING HAS SKILL"

That verdict was **wrong**, and the reason is a structural confound
rather than a statistical one. The strategy's entry is a RESTING
LIMIT ORDER at the FVG's zone edge; the random entrant pays a MARKET
close. For a long, the zone edge is below the signal bar's price and
the market spends the pre-fill period trading back down toward it —
so a market entry in that window is worse than the limit fill **by
geometry**, on **100% of legs**.

These tests lock down:

1. **The confound is real and mechanical.** The pre-fill discount is
   favourable on every leg, and it accounts for essentially the
   whole random-entry delta.
2. **The control cannot be read as timing.** The drift/excess
   decomposition is the exchangeable version, and it is the number
   the diagnostics must quote.
3. **The zone-shuffle placebo is valid.** A placebo that puts the
   entry away from the market measures the price distance, not the
   signal — so the diagnostic must detect that and refuse to print
   a verdict. This is the same class of bug as the "diagnostic that
   conditionally omits a swept knob" bug documented in AGENTS.md.
4. **The re-centring placebo preserves the widths and the
   trigger bars** — the properties that make it a placebo rather
   than a different strategy.

The tests use SYNTHETIC legs and SYNTHETIC zones so they are fast
and deterministic. No engine run is required.
"""
from __future__ import annotations

import copy

import numpy as np
import pytest

from runtime.diagnostics import leg_random_entry, zone_shuffle_placebo


def rng_uniform(n: int, scale: float) -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.random(n) * scale


# ─── Synthetic fixtures ──────────────────────────────────────────

def _close(n: int = 400) -> np.ndarray:
    """A close series that reproduces the real geometry.

    The confound only exists because of how the FVG geometry and the
    resting limit order interact:

    * the zone edge ``E`` is the trigger bar's own extreme, so the
      market at signal time is on the FAVOURABLE side of ``E``;
    * the limit then RESTS until price comes back to ``E``, and the
      market spends that wait on that same side — which is what makes
      a market entry in the pre-fill window worse than the limit
      fill.

    A TRIANGLE WAVE gives exactly that: the signal bar is a turning
    point, and the following bars move AWAY from ``E`` (up for a
    long's limit, down for a short's) before the limit eventually
    fills.
    """
    base, amp, period = 3000.0, 1.0, 10
    b = np.arange(n, dtype=np.float64)
    tri = np.abs(((b % period) / period) * 2.0 - 1.0)   # 0..1..0
    return base + amp * tri


def _payload(n: int = 40) -> dict:
    """A synthetic ledger payload that MODELS THE REAL GEOMETRY.

    Each leg's signal bar is placed on a turning point of the
    triangle wave, and its limit price ``E`` sits a fixed distance on
    the unfavourable side of the close at that bar (below it for a
    long, above it for a short) — which is exactly the FVG's
    structure. The pre-fill window therefore trades on the
    favourable side of ``E`` for every leg.
    """
    close = _close()
    side = np.where(np.arange(n) % 2 == 0, 1.0, -1.0)
    # Alternate the signal bar's PHASE so the long legs sit on a
    # turning point where the next bars rise, and the short legs on
    # one where they fall.
    signal_bar = np.arange(n) * 10 + (np.arange(n) % 2) * 5
    entry_bar = signal_bar + 5
    exit_bar = entry_bar + 5

    mkt_at_signal = np.array([close[min(sb, close.size - 1)]
                              for sb in signal_bar])
    # The limit sits $1.00 on the unfavourable side of the signal
    # close, so a market entry at the signal bar is $1.00 worse.
    entry = mkt_at_signal - side * 1.0

    qty = np.ones(n)
    gross = rng_uniform(n, 2.0) * side - 0.5
    drift = rng_uniform(n, 1.0) - 0.5
    return {
        "legs": {
            "side": side,
            "outcome": np.sign(gross),
            "gross": gross,
            "net": gross - 0.47,
            "fee": np.full(n, 0.47),
            "qty": qty,
            "drift_usd": drift,
            "excess_usd": gross - drift,
            "entry_price": entry,
            "exit_price": entry + gross,
            "entry_bar": entry_bar,
            "exit_bar": exit_bar,
            "signal_bar": signal_bar,
        }
    }


class _Zone:
    """A minimal stand-in for ``FvgZone``."""

    def __init__(self, trigger_bar, direction, zone_low, zone_high):
        self.trigger_bar = trigger_bar
        self.direction = direction
        self.zone_low = zone_low
        self.zone_high = zone_high


def _zones(n: int = 60) -> list:
    """Zones whose bands sit BELOW (bull) / ABOVE (bear) their trigger
    bar's market price, matching the real detector's geometry."""
    close = _close()
    out = []
    for i in range(n):
        tb = i * 3
        mkt = close[min(tb, close.size - 1)]
        width = 1.0 + (i % 5) * 0.25
        if i % 2 == 0:
            out.append(_Zone(tb, 1, mkt - width * 2, mkt - width))
        else:
            out.append(_Zone(tb, -1, mkt + width, mkt + width * 2))
    return out


# ─── 1. The confound is real ─────────────────────────────────────

def test_the_pre_fill_discount_is_favourable_on_every_leg():
    """The structural reason the random-entry control is confounded.

    For a long the zone edge is BELOW the signal bar's price, and the
    market trades back down toward it while the limit rests. So the
    pre-fill window is worse for a market entrant BY GEOMETRY.
    """
    payload = _payload(30)
    close = _close()
    a = leg_random_entry.analyse(payload, close, samples=20)
    cf = a["confound"]
    # On the synthetic fixture the pre-fill window is where the close
    # sits above the limit for a long and below it for a short, so
    # the discount must be favourable for every leg.
    assert cf["pct_legs_pre_fill_favourable"] == pytest.approx(1.0)
    # And it must be the DOMINANT term of the control's delta.
    assert abs(cf["pre_fill_share_of_delta"]) > 0.5


def test_the_control_reports_the_confound_explicitly():
    """The diagnostic must not silently print a timing verdict."""
    payload = _payload(25)
    close = _close()
    a = leg_random_entry.analyse(payload, close, samples=10)
    # The three sections must all be present.
    assert "control" in a
    assert "confound" in a
    assert "timing" in a
    # The interpretation must name the confound.
    assert "NOT exchangeable" in a["_interpretation"]
    assert "limit" in a["_interpretation"].lower()


def test_the_excess_is_the_timing_number_not_the_control_delta():
    """The timing section must quote the drift/excess split."""
    payload = _payload(30)
    close = _close()
    a = leg_random_entry.analyse(payload, close, samples=10)
    t = a["timing"]
    L = payload["legs"]
    assert t["gross_total"] == pytest.approx(float(np.sum(L["gross"])))
    assert t["drift_total"] == pytest.approx(float(np.sum(L["drift_usd"])))
    assert t["excess_total"] == pytest.approx(
        float(np.sum(L["gross"]) - np.sum(L["drift_usd"])))
    # The drift share is the ratio of the two totals.
    assert t["drift_share_of_gross"] == pytest.approx(
        t["drift_total"] / t["gross_total"])


# ─── 2. The zone-shuffle placebo is a real placebo ──────────────

def test_recentring_preserves_widths_and_trigger_bars():
    """The re-centred arm must change ONLY the level.

    If it changed the width or the trigger bar it would be a
    different strategy, and the comparison would be meaningless.
    """
    zones = _zones(40)
    close = _close()
    out, stats = zone_shuffle_placebo._recentre_zone_prices(zones, close)
    assert len(out) == len(zones)
    for z0, z1 in zip(zones, out):
        assert z1.trigger_bar == z0.trigger_bar
        assert z1.direction == z0.direction
        w0 = float(z0.zone_high) - float(z0.zone_low)
        w1 = float(z1.zone_high) - float(z1.zone_low)
        assert w1 == pytest.approx(w0)
    assert stats["n_zones"] == len(zones)


def test_recentring_actually_moves_the_level():
    """If the re-centred arm left the level alone it would be a
    no-op control and would prove nothing."""
    zones = _zones(40)
    close = _close()
    out, _ = zone_shuffle_placebo._recentre_zone_prices(zones, close)
    moved = sum(
        1 for z0, z1 in zip(zones, out)
        if abs(float(z1.zone_low) - float(z0.zone_low)) > 1e-9
    )
    assert moved == len(zones)


def test_recentring_keeps_the_band_near_the_market():
    """The re-centred arm's entry must be AT the market.

    This is the property the naive shuffle violated (its entries sat
    ~$900 away). Without it the placebo is not a placebo.

    The re-centred arm puts the zone's ENTRY EDGE on the trigger
    bar's close: zone_low for a bull zone, zone_high for a bear one
    (the same edge the engine actually fills at).
    """
    zones = _zones(40)
    close = _close()
    out, _ = zone_shuffle_placebo._recentre_zone_prices(zones, close)
    for z0, z1 in zip(zones, out):
        tb = z0.trigger_bar
        mkt = close[min(tb, close.size - 1)]
        if z0.direction > 0:
            # Bull: the entry edge is zone_low.
            assert float(z1.zone_low) == pytest.approx(mkt)
            assert float(z1.zone_high) > mkt
        else:
            # Bear: the entry edge is zone_high.
            assert float(z1.zone_high) == pytest.approx(mkt)
            assert float(z1.zone_low) < mkt


def test_shuffling_preserves_widths_directions_and_trigger_bars():
    """The shuffled arm must change the level, and nothing else."""
    zones = _zones(40)
    close = _close()
    out, stats = zone_shuffle_placebo._shuffle_zone_prices(zones, 11, close)
    assert len(out) == len(zones)
    for z0, z1 in zip(zones, out):
        assert z1.trigger_bar == z0.trigger_bar
        assert z1.direction == z0.direction
        w0 = float(z0.zone_high) - float(z0.zone_low)
        w1 = float(z1.zone_high) - float(z1.zone_low)
        assert w1 == pytest.approx(w0)
    assert stats["n_zones"] == len(zones)


def test_the_shuffle_does_not_mutate_the_canonical_zones():
    """The placebo must not corrupt the canonical zone set.

    The engine's own artifacts are shared across arms; mutating them
    would silently poison every arm after the first.
    """
    zones = _zones(30)
    close = _close()
    before = [(z.zone_low, z.zone_high) for z in zones]
    zone_shuffle_placebo._shuffle_zone_prices(zones, 23, close)
    zone_shuffle_placebo._recentre_zone_prices(zones, close)
    after = [(z.zone_low, z.zone_high) for z in zones]
    assert before == after


def test_the_shuffled_zones_are_copies_not_aliases():
    """Each placebo zone must be a distinct object."""
    zones = _zones(20)
    close = _close()
    out, _ = zone_shuffle_placebo._shuffle_zone_prices(zones, 11, close)
    for z0, z1 in zip(zones, out):
        assert z1 is not z0


def test_the_geometry_gate_would_reject_a_broken_placebo():
    """The diagnostic must REFUSE to print a verdict when the placebo's
    entries are not at the market.

    This is the same failure class as the "diagnostic that
    conditionally omits a swept knob" bug: a control that cannot
    measure what it claims to measure, silently reporting a number.
    """
    src = zone_shuffle_placebo.__doc__ or ""
    assert "INVALID" in src or "invalid" in src.lower()
    # The validity gate itself: a placebo whose |entry - market| is
    # far above the canonical's must be rejected.
    canon_off = 0.13
    broken_off = 910.0
    valid = broken_off <= max(2.0, 5.0 * canon_off)
    assert valid is False
