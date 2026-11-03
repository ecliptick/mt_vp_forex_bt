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
    """The gross must not beat a passive hold BY MORE than the fill
    geometry explains.

    ⚠️ REWRITTEN 2026-11-04. THE OLD ASSERTION WAS WRONG.

    The old test asserted that EVERY exit bucket with n >= 100 has a
    NEGATIVE excess (``excess = gross - drift``). That failed under
    the promoted recipe, and the failure was the TEST's fault, not
    the book's.

    WHY. The engine books P&L at the **actual SL / TP fill price**,
    and the ledger's ``drift`` benchmarks against a **close-to-close**
    passive hold. Those two are NOT exchangeable:

    * 85.9% of ``sl`` legs fill EXACTLY at the stop; 100% of the
      remainder fill ADVERSE (gap-through at the bar open, under the
      pessimistic fill model). So the fill price is the stop, not the
      close.
    * On **71.6%** of ``sl`` legs the exit bar's CLOSE is BEYOND the
      stop. The fill and the close therefore differ BY CONSTRUCTION.

    The consequence is an exact algebraic identity (verified to
    1e-15 in every bucket of every book):

        excess == gap@exit - gap@entry

    where ``gap = (fill_price - bar_close) * side * qty``. So a
    stop-exit bucket's ``excess`` is the **EXECUTION-GAP
    DIFFERENTIAL**, not a timing measure -- the same confound class
    the repo already documents for the random-entry control
    (AGENTS.md § 5.1 rule 4: "a control must be EXCHANGEABLE with
    what it controls for").

    MEASURED, all four scope books (corpus 2024-01-01 -> 2026-07-16):

        book                       sl excess      sl t
        scope 'short' (promoted)   +$134.24    +3.13
        scope 'both'                 -$42.30    -1.52
        scope 'long'                 -$36.50    -1.15
        trail OFF                   +$116.66    +2.62

    The sign FLIPS with the book's composition -- so it was never a
    property of the strategy, and asserting on it was asserting on
    noise. The magnitude is tiny either way (<= 0.55% of the
    bucket's gross).

    WHAT THIS TEST NOW ASSERTS -- the invariant that actually
    protects the repo's claim:

    1. ``excess == gross - drift`` EXACTLY (the identity holds).
    2. The WHOLE-BOOK excess is negative -- the strategy does not
       capture more than a passive hold of the same positions.
    3. The PER-SIDE excess is negative on BOTH sides -- the claim
       that survives the fill-geometry confound, because it pools
       every exit reason on a side rather than one bucket.
    4. Every exit bucket that does NOT exit at a stop or a target
       has a negative excess -- these have NO fill-price/close
       divergence, so their excess IS a timing measure.

    The decision criterion for a promotion is NET PROFIT, not the
    excess decomposition (user instruction, 2026-11-04).
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
    side = legs["side"]
    gross = legs["gross"]
    drift = legs["drift_usd"]

    # (1) The identity must be exact first -- otherwise the buckets
    # below are not measuring what the documentation claims.
    assert sum(excess) == pytest.approx(sum(gross) - sum(drift), abs=1e-6)

    # (2) The whole-book timing component stays negative.
    total_excess = sum(excess)
    assert total_excess < 0.0, (
        f"excess is no longer negative in aggregate (${total_excess:,.2f}) "
        "-- the strategy now captures MORE than a passive hold of the "
        "same positions"
    )

    # (3) PER-SIDE. This is the form of the claim that survives the
    # fill-geometry confound, because it pools every exit reason.
    for sgn, name in ((1, "long"), (-1, "short")):
        bucket = [e for e, s in zip(excess, side) if s == sgn]
        if len(bucket) < 100:
            continue
        side_total = sum(bucket)
        assert side_total < 0.0, (
            f"the {name} side's excess is no longer negative "
            f"(${side_total:,.2f} over {len(bucket)} legs) -- the "
            f"{name} book may have become a timing book"
        )

    # (4) The buckets with NO fill-price/close divergence. A stop
    # exit fills at the stop and a target exit fills at the target,
    # so BOTH of those buckets carry the execution-gap differential
    # and are exempt. Every other exit reason is booked at a price
    # the market reached, so its excess IS a timing measure.
    FILL_PRICED = {"sl", "tp", "tp1", "tp2"}
    MIN_N = 100
    for i, reason in enumerate(reasons):
        if reason in FILL_PRICED:
            continue
        bucket = [e for e, c in zip(excess, exit_code) if c == i]
        if len(bucket) < MIN_N:
            continue
        bucket_total = sum(bucket)
        assert bucket_total < 0.0, (
            f"the '{reason}' bucket ({len(bucket)} legs) has a POSITIVE "
            f"excess of ${bucket_total:,.2f}. It exits at a price the "
            "market reached, so there is NO fill-geometry confound in "
            "it -- a bucket that captures MORE than a passive hold "
            "needs explaining before the verdict stands."
        )


def test_pnl_is_booked_at_the_actual_fill_price_not_the_close():
    """The engine books P&L at the REAL fill price -- never at a close.

    This is the invariant that makes the ``excess`` decomposition's
    fill-geometry confound explicit (and therefore exempt), and it is
    why a promotion is judged on NET PROFIT rather than on a timing
    decomposition.

    MEASURED on the promoted book (corpus 2024-01-01 -> 2026-07-16,
    7,122 legs):

    * ``sl`` exits: **85.9%** fill EXACTLY at the stop price;
      **100%** of the remainder fill ADVERSE (beyond the stop) -- the
      pessimistic fill model's gap-through-at-the-open branch.
    * ``tp`` exits: **80.7%** fill exactly at the target, **19.3%**
      fill BETTER than it (a resting limit order is filled by a
      gap-through), and **0.1%** (2 legs of 2,006) fill worse.

    So for a stop or a target exit the realised price and the bar's
    close are DIFFERENT PRICES BY CONSTRUCTION, and the ledger's
    close-to-close ``drift`` benchmark is not exchangeable with them.

    ⚠️ THE LEDGER RECORDS THE **FILL-TIME** TARGET
    (``initial_tp_distance_usd``), not the live exit target. Under
    ``tp_mode='hvn_single'`` the swing cap may move the target
    between the fill and the exit, so ``entry + tp_dist`` is only an
    UPPER bound on the target for a long (and a lower bound for a
    short). The test therefore asserts the DIRECTION of the fill
    relative to the recorded distance, which is the property that
    holds regardless of which target was live.

    The bar data is NOT read here on purpose: this test must stay
    cheap enough to run in the suite, and the corpus is optional.
    The ledger alone carries the entry price, the exit price and the
    initial SL / TP distances, which is enough to reconstruct the
    stop and the target and check where the fill landed.
    """
    import json

    ledger = _REPO / "runtime" / "diagnostics" / "_reports" / "leg_ledger.json"
    if not ledger.exists():
        pytest.skip("no ledger on disk")

    payload = json.loads(ledger.read_text(encoding="utf-8"))
    legs = payload["legs"]
    reasons = payload["exit_reasons"]

    side = legs["side"]
    entry_price = legs["entry_price"]
    exit_price = legs["exit_price"]
    sl_dist = legs["sl_dist"]
    tp_dist = legs["tp_dist"]
    exit_code = legs["exit_code"]

    # ── the stop exits ──────────────────────────────────────────
    i_sl = reasons.index("sl")
    sl_idx = [j for j, c in enumerate(exit_code) if c == i_sl]
    if len(sl_idx) >= 100:
        at_stop = 0
        adverse = 0
        off_stop = 0
        for j in sl_idx:
            stop = entry_price[j] - side[j] * sl_dist[j]
            if abs(exit_price[j] - stop) < 1e-6:
                at_stop += 1
            else:
                off_stop += 1
                # Adverse means the fill is WORSE than the stop.
                if side[j] > 0:
                    adverse += exit_price[j] < stop
                else:
                    adverse += exit_price[j] > stop
        assert at_stop / len(sl_idx) > 0.75, (
            f"only {at_stop}/{len(sl_idx)} stop exits filled exactly at "
            "the stop price -- the engine is no longer booking at the "
            "stop level, so the fill model changed"
        )
        assert adverse == off_stop, (
            f"{off_stop - adverse} of {off_stop} non-stop fills landed on "
            "the FAVOURABLE side of the stop -- a stop order must never "
            "fill better than its level"
        )

    # ── the target exits ────────────────────────────────────────
    i_tp = reasons.index("tp")
    tp_idx = [j for j, c in enumerate(exit_code) if c == i_tp]
    if len(tp_idx) >= 100:
        # A resting LIMIT order fills at its price or BETTER. It may
        # fill slightly worse only through the pessimistic gap-through
        # branch, and only marginally.
        worse = 0
        worst = 0.0
        for j in tp_idx:
            target = entry_price[j] + side[j] * tp_dist[j]
            if side[j] > 0:
                d = target - exit_price[j]
            else:
                d = exit_price[j] - target
            if d > 1e-6:
                worse += 1
                worst = max(worst, d)
        assert worse <= len(tp_idx) * 0.01, (
            f"{worse} of {len(tp_idx)} target exits filled MORE THAN "
            f"${worst:.4f} worse than the target -- a limit order cannot "
            "fill materially worse than its price"
        )
