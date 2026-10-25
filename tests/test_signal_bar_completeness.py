"""Guards for the ``signal_bar`` completeness invariant (2026-10-25).

Every ``ClosedTrade`` the engine produces must carry the bar at which
the SIGNAL fired — the causal decision point. Entry-side gates are
evaluated there, the ledger keys legs by their SIGNAL time, and the
per-side sequence filter is attributed at that bar.

``ClosedTrade.signal_bar`` defaults to ``0``. So a construction site
that FORGETS to pass it produces trades that claim to have signalled
at the very start of the corpus — silently, with no error.

That is exactly what happened in the ``contra_close`` netting branch:
it built a ``ClosedTrade`` without ``signal_bar``, so every netted leg
reported ``signal_bar == 0``. It was invisible while the netting was
a rare event (~93 legs on the narrow book). On the WIDE book
(vol gate 1.0, 51,075 legs) the netting fires on 17,211 legs, and
the ledger's signal-time index was corrupted for 34% of the book:
17,200 legs showed a pre-fill window of up to 41M bars (the whole
corpus) instead of a few thousand.

These tests walk the source, so they fail at the SOURCE of the
defect rather than on a symptom.
"""
from __future__ import annotations

import pathlib
import re

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_ENGINE = _REPO / "bt" / "_engine" / "vp_multi_target_engine.py"


def _closed_trade_sites(src: str) -> list[tuple[int, str]]:
    """Every ``closed_trades.append(ClosedTrade(`` call site.

    Returns ``(line_no, call_block)`` pairs. The block is the call
    text up to its closing ``))`` -- found by paren balance, so a
    long argument list is captured in full.
    """
    sites: list[tuple[int, str]] = []
    lines = src.splitlines()
    for i, line in enumerate(lines):
        if "closed_trades.append(ClosedTrade(" not in line:
            continue
        depth = 0
        block: list[str] = []
        started = False
        for j in range(i, len(lines)):
            ln = lines[j]
            block.append(ln)
            for ch in ln:
                if ch == "(":
                    depth += 1
                    started = True
                elif ch == ")":
                    depth -= 1
            if started and depth <= 0:
                break
        sites.append((i + 1, "\n".join(block)))
    return sites


def test_every_closed_trade_site_sets_signal_bar():
    """No ``ClosedTrade`` construction may omit ``signal_bar``.

    The field defaults to 0, so omitting it is a SILENT corruption of
    the signal-time index -- not a crash.
    """
    src = _ENGINE.read_text(encoding="utf-8")
    sites = _closed_trade_sites(src)

    assert len(sites) >= 7, (
        f"expected to find the engine's ClosedTrade construction sites, "
        f"found only {len(sites)} -- the source layout changed, so this "
        "guard is no longer looking at the right thing"
    )

    missing = [
        line_no for line_no, block in sites
        if not re.search(r"signal_bar=", block)
    ]
    assert not missing, (
        f"ClosedTrade construction at line(s) {missing} does not pass "
        "signal_bar. It defaults to 0, so those trades claim to have "
        "signalled at the start of the corpus. This corrupted the "
        "ledger's signal-time index for 34% of the wide book "
        "(17,200 legs with a pre-fill window of up to 41M bars). "
        "Pass the leg's own signal_bar."
    )


def test_closed_trade_signal_bar_defaults_to_zero_is_known():
    """Lock the default so the guard above stays meaningful.

    If the default ever becomes non-zero (e.g. a sentinel like -1),
    the omission would silently become a different bug and the
    source-walking guard would need to change.
    """
    src = _ENGINE.read_text(encoding="utf-8")
    assert re.search(r"signal_bar:\s*int\s*=\s*0", src), (
        "ClosedTrade.signal_bar no longer defaults to 0 -- if it now "
        "uses a sentinel, update this guard and the source-walking "
        "test above accordingly"
    )


def test_contra_close_leg_carries_its_own_signal_bar():
    """The netting branch must pass the NETTED LEG's own signal bar.

    Not the counterparty's, and not the default. The netting closes a
    slice of a specific open position, and that position's signal bar
    is the causal bar for that slice.
    """
    src = _ENGINE.read_text(encoding="utf-8")
    sites = _closed_trade_sites(src)

    netting = [(n, b) for n, b in sites if "contra_close" in b]
    assert netting, (
        "no ClosedTrade site mentions contra_close -- the netting branch "
        "moved or was removed; re-point this guard"
    )

    for line_no, block in netting:
        assert "signal_bar=_ct.signal_bar" in block, (
            f"the contra_close netting branch (line {line_no}) must pass "
            "signal_bar=_ct.signal_bar -- the netted leg's OWN signal bar"
        )


def test_ledger_pre_fill_window_is_sane_on_the_current_book():
    """The signal-time index must not be corrupted on disk.

    A leg whose pre-fill window spans a large fraction of the corpus
    is the signature of a ``signal_bar`` that defaulted to 0. This is
    the end-to-end symptom check on the built ledger.
    """
    import json

    ledger = _REPO / "runtime" / "diagnostics" / "_reports" / "leg_ledger.json"
    if not ledger.exists():
        pytest.skip("no ledger on disk")

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    legs = payload["legs"]

    signal_bar = legs["signal_bar"]
    entry_bar = legs["entry_bar"]
    n = len(signal_bar)
    if not n:
        pytest.skip("empty ledger")

    # The corpus is ~41M bars. A leg whose pre-fill window spans
    # HALF the corpus is not a resting limit order, it is a corrupted
    # signal_bar.
    #
    # Why 50% and not 10%: the engine has NO order expiry (the
    # ``n_orders_cancelled_expired`` counter is declared but never
    # incremented), so a resting order lives until its zone dies. The
    # longest MEASURED legitimate rest on the current book is 9,856,308
    # bars — 198 days, on a zone that outlived the whole gold rally.
    # That is 23.9% of the corpus, so a 10% threshold would flag a
    # real trade.
    #
    # The corruption signature is much sharper and is caught by
    # ``test_no_leg_claims_to_have_signalled_at_bar_zero`` (every one
    # of the 17,211 netted legs reported signal_bar == 0). This test
    # is the coarse backstop for a partial corruption.
    corpus_bars = 41_233_730
    suspicious = sum(
        1 for sb, eb in zip(signal_bar, entry_bar)
        if (eb - sb) > 0.50 * corpus_bars
    )
    assert suspicious == 0, (
        f"{suspicious} of {n} legs have a pre-fill window longer than "
        "HALF the corpus. That is the signal_bar-defaults-to-0 "
        "signature -- those legs are claiming to have signalled at "
        "the start of the corpus."
    )


def test_no_leg_claims_to_have_signalled_at_bar_zero():
    """``signal_bar == 0`` means "signalled at the corpus start".

    The execution window starts with a 14-day warmup prefix, so bar 0
    is INSIDE the warmup and no SCORED leg may legitimately sit
    there. A non-zero count is the exact corruption signature: the
    ``contra_close`` branch used to omit ``signal_bar``, so all
    17,211 netted legs reported 0.
    """
    import json

    ledger = _REPO / "runtime" / "diagnostics" / "_reports" / "leg_ledger.json"
    if not ledger.exists():
        pytest.skip("no ledger on disk")

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    legs = payload["legs"]

    signal_bar = legs["signal_bar"]
    n = len(signal_bar)
    if not n:
        pytest.skip("empty ledger")

    at_zero = sum(1 for sb in signal_bar if sb == 0)
    assert at_zero == 0, (
        f"{at_zero} of {n} legs report signal_bar == 0. The execution "
        "window opens with a 14-day warmup prefix, so no scored leg "
        "can legitimately have signalled at bar 0. A ClosedTrade "
        "construction site is omitting signal_bar."
    )


def test_excess_is_negative_for_every_exit_reason():
    """The timing verdict must hold leg-by-leg, not just in aggregate.

    ``excess = gross - drift`` is the honest, non-confounded timing
    number (the same execution model on both sides). AGENTS.md 2b's
    verdict is "NO TIMING EDGE", and that claim is only as strong as
    its weakest exit bucket.

    MEASURED on the current wide book (51,075 legs):

        exit reason    n        excess
        tp          1,047       -$49.03
        tp1         1,113       -$13.77
        tp2        10,118    -$1,617.60
        sl         21,583    -$1,089.71
        eod             3         $0.01
        contra_close 17,211  -$1,309.97
        TOTAL       51,075    -$4,080.07   (t = -15.63)

    Every bucket with real weight is negative. ``eod`` has n=3 and is
    noise.

    This is the ROBUST form of the claim. The random-entry control's
    ``post_fill_discount_total`` flipped positive on the wide book
    purely because the netting legs' "exit" is a netting price the
    market did not independently reach -- but their ``excess`` is
    still negative, because ``excess`` uses the ledger's own drift.
    """
    import json

    ledger = _REPO / "runtime" / "diagnostics" / "_reports" / "leg_ledger.json"
    if not ledger.exists():
        pytest.skip("no ledger on disk")

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    legs = payload["legs"]
    reasons = payload["exit_reasons"]

    exit_code = legs["exit_code"]
    excess = legs["excess_usd"]

    # The identity must be exact first -- otherwise the buckets below
    # are not measuring what the documentation claims.
    gross = legs["gross"]
    drift = legs["drift_usd"]
    assert sum(excess) == pytest.approx(sum(gross) - sum(drift), abs=1e-6)

    total_excess = sum(excess)
    assert total_excess < 0.0, (
        f"excess is no longer negative in aggregate (${total_excess:,.2f}) "
        "-- AGENTS.md section 2b says the entry has no measurable skill, "
        "and that is now wrong"
    )

    # Per-bucket, ignoring the 3-leg eod bucket.
    MIN_N = 100
    for i, reason in enumerate(reasons):
        bucket = [e for e, c in zip(excess, exit_code) if c == i]
        if len(bucket) < MIN_N:
            continue
        bucket_total = sum(bucket)
        assert bucket_total < 0.0, (
            f"the '{reason}' bucket ({len(bucket)} legs) has a POSITIVE "
            f"excess of ${bucket_total:,.2f}. The 'no timing edge' verdict "
            "is per-leg -- a bucket where the strategy captures MORE than "
            "a passive hold needs explaining before the verdict stands."
        )
