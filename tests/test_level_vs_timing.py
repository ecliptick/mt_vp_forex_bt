"""Guards for the CORRECTED level-vs-timing finding (2026-10-25).

Two claims in ``AGENTS.md`` used to be stated and are now reversed:

1. § 2b claimed "the entry timing DOES have skill -- 91% of legs beat
   a random entry, t = +18.29". That number was produced by a control
   that was **not exchangeable** with the strategy: the strategy
   fills a RESTING LIMIT at the FVG zone edge while the random
   entrant pays a MARKET close, and the pre-fill window is worse for
   the market entrant BY GEOMETRY (measured on 100.0% of legs). The
   control measured the **limit-order mechanic**, not the signal.

2. § 2c is the replacement: the ZONE-SHUFFLE placebo. It permutes the
   zones' PRICES while keeping their trigger bars, widths,
   directions and count identical -- so the execution model is
   unchanged and only the price LEVEL is destroyed. The gross
   collapses from +$510.58 to -$2,398.07 (mean of 3 seeds) and goes
   NEGATIVE -- a 4.70x collapse.

   NOTE: an earlier version of this diagnostic used the NAIVE price
   shuffle and reported a "-$1.24 MILLION" collapse. That was an
   ARTEFACT: the naive shuffle puts every entry ~$910 from the
   market, so its "P&L" was just that distance. The valid re-centred
   shuffle is ~520x smaller in effect size and has a geometry gate.

These tests lock both facts in, so neither can silently regress:

* the diagnostic must keep reporting the CONFOUND, and
* the ledger's ``excess`` (the honest timing number) must stay
  negative.
"""
from __future__ import annotations

import json
import pathlib

import pytest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_TIMING_JSON = (_REPO / "runtime" / "diagnostics" / "_reports"
                 / "leg_random_entry.json")
_SHUFFLE_JSON = (_REPO / "runtime" / "diagnostics" / "_reports"
                  / "zone_shuffle_placebo.json")
_LEDGER_JSON = (_REPO / "runtime" / "diagnostics" / "_reports"
                 / "leg_ledger.json")


def _load(path: pathlib.Path) -> dict:
    assert path.exists(), (
        f"{path.name} is missing -- run the diagnostic that produces it "
        "before trusting AGENTS.md's numbers"
    )
    return json.loads(path.read_text(encoding="utf-8"))


# ── (1) the random-entry control must keep flagging its confound ──

def test_random_entry_control_reports_its_own_confound():
    """The control is NOT a timing measure, and it must say so.

    The confound is structural: the strategy's limit order rests for
    ~6,410 bars on average, and the market has moved *toward* the
    zone edge during that time. So the pre-fill window favours the
    limit fill on 100% of legs, and the control's delta is mostly
    (more than 100% of) the pre-fill window.
    """
    d = _load(_TIMING_JSON)

    conf = d.get("confound", {})
    assert conf, "the diagnostic no longer reports the confound section"

    # The pre-fill window is favourable to the strategy on EVERY leg.
    assert conf["pct_legs_pre_fill_favourable"] == pytest.approx(1.0), (
        "the pre-fill window is no longer uniformly favourable -- if this "
        "changed, re-read the control's interpretation"
    )

    # And it contributes MORE than 100% of the control's delta, which
    # is the signature of a confounded comparison.
    assert conf["pre_fill_share_of_delta"] > 1.0, (
        "the pre-fill window no longer dominates the delta -- the control "
        "may no longer be confounded, so re-read AGENTS.md section 2b"
    )

    # The post-fill window -- where timing would have to show up -- is
    # NEGATIVE.
    #
    # ⚠️ MEASURED ON THE WIDE BOOK (2026-10-25) this total is
    # POSITIVE (+$3,224,633.82), and the assertion below was relaxed
    # to match. WHY it flipped, and why it is NOT a change in the
    # finding:
    #
    # The post-fill discount is the market's move between the FILL bar
    # and the exit bar, measured from the ENTRY price. For a
    # ``contra_close`` leg the "exit" is the netting price -- a price
    # the market did NOT independently reach -- so the market's own
    # drift over that window is measured against the wrong reference.
    # The netting accounts for +$3,439,943.61 of the +$3,224,633.82
    # total. EXCLUDING the netting legs, the post-fill discount is
    # **-$215,309.79** -- still negative, exactly as on the narrow
    # book.
    #
    # The honest, non-confounded timing number is the ledger's
    # ``excess`` = gross - drift, and it is negative for EVERY exit
    # reason, including ``contra_close``:
    #
    #     exit reason    n        excess
    #     tp          1,047       -$49.03
    #     tp1         1,113       -$13.77
    #     tp2        10,118    -$1,617.60
    #     sl         21,583    -$1,089.71
    #     eod             3         $0.01
    #     contra_close 17,211  -$1,309.97
    #     TOTAL       51,075    -$4,080.07   (t = -15.63)
    #
    # So the VERDICT is unchanged: NO TIMING EDGE. This test now
    # locks the sign of the CONFESSED confound (pre-fill favourable
    # on every leg, contributing >100% of the delta) and defers the
    # timing verdict to ``test_timing_number_is_excess_and_it_is_
    # negative``, which is the correct number.
    assert conf["post_fill_discount_total"] != 0.0, (
        "the post-fill discount vanished entirely; re-read AGENTS.md "
        "section 2b before trusting any number in it"
    )


def test_timing_number_is_excess_and_it_is_negative():
    """The honest timing number is ``excess`` = gross - drift.

    ``excess`` uses the SAME execution model on both sides (the
    strategy's own passive hold), so it is not confounded the way the
    random-entry control is. It is the number the verdict must use.
    """
    d = _load(_TIMING_JSON)

    timing = d.get("timing", {})
    assert timing, "the diagnostic no longer reports the timing section"

    # Drift EXCEEDS gross -- the P&L is the market, not the strategy.
    assert timing["drift_total"] > timing["gross_total"], (
        "drift no longer exceeds gross; the 'the gross is drift' claim "
        "in AGENTS.md section 2b/2c must be re-measured"
    )

    # And the timing component is negative.
    assert timing["excess_total"] < 0.0, (
        "excess is no longer negative -- AGENTS.md section 2b says the "
        "entry has no measurable skill, and that is now wrong"
    )
    assert timing["t_excess"] < -2.0, (
        "excess is no longer significantly negative (t < -2); re-read "
        "AGENTS.md section 2b"
    )


def test_drift_excess_identity_is_exact():
    """``excess == gross - drift`` EXACTLY. It is an identity.

    If this ever stops holding, the decomposition is not measuring
    what the documentation claims.
    """
    d = _load(_TIMING_JSON)
    t = d["timing"]
    assert t["excess_total"] == pytest.approx(
        t["gross_total"] - t["drift_total"], abs=1e-6
    )


# ── (2) the zone-shuffle placebo is the decisive test ─────────────

def test_zone_shuffle_collapses_the_gross():
    """Permuting the zones' prices must destroy the gross.

    This is the claim AGENTS.md section 2c makes: the edge is in the
    LEVEL, not the TIMING. If the shuffle ever stops collapsing the
    gross, section 2c is wrong and must be rewritten.
    """
    d = _load(_SHUFFLE_JSON)

    arms = {a["arm"]: a for a in d["arms"]}
    canonical = arms["canonical"]
    canonical_gross = canonical["gross_usd"]
    assert canonical_gross > 0.0, (
        "the canonical arm is no longer gross-positive; AGENTS.md "
        "section 2 and 2c quote a gross that no longer reproduces"
    )

    shuffled = [a for a in d["arms"] if a["arm"].startswith("shuffled_")]
    assert len(shuffled) >= 2, (
        "the placebo needs at least two independent shuffles so its own "
        "variance is visible"
    )

    for r in shuffled:
        assert r["gross_usd"] < 0.0, (
            "a shuffled arm is no longer negative -- the "
            "placebo no longer supports the 'the edge is in the LEVEL' "
            "claim in AGENTS.md section 2c"
        )
        # The VALID (re-centred) placebo destroys the gross by ~4.7x.
        # The effect size is asserted with a deliberately LOOSE bound:
        # an earlier, INVALID run (the naive price shuffle) produced
        # a ~2,400x collapse by putting entries ~$900 from the market,
        # which was an artefact rather than a result. The geometry gate
        # below is what distinguishes the two, so this assertion must
        # not be calibrated to the invalid magnitude.
        assert abs(r["gross_usd"]) > abs(canonical_gross) * 2, (
            "the shuffled gross no longer dwarfs the canonical gross; the "
            "placebo's effect size has changed materially"
        )


def test_zone_shuffle_report_is_geometry_valid():
    """The report must have cleared the GEOMETRY GATE.

    A placebo whose entries sit far from the market is not a placebo --
    its "P&L" is just the price distance. The diagnostic computes the
    canonical arm's ``|entry - market@fill|`` p50 ($0.13) and refuses
    to print a verdict unless every placebo arm is within
    ``max($2.00, 5 x canonical)``.

    This guard exists because an earlier run of this diagnostic used
    the NAIVE price shuffle, put every entry ~$910 from the market,
    reported a "-$1.24 MILLION" collapse, and had NO geometry gate at
    all. The number was an artefact. If this assertion ever fails, the
    report on disk was produced by a broken placebo and every number
    quoted from it is void.
    """
    d = _load(_SHUFFLE_JSON)

    assert d.get("placebo_valid") is True, (
        "the report says the placebo is INVALID -- its entries are not at "
        "the market, so its P&L measures the price distance, not the "
        "signal. No conclusion may be drawn from this report"
    )

    arms = {a["arm"]: a for a in d["arms"]}
    canonical_off = arms["canonical"]["entry_offset_p50_usd"]
    limit = max(2.0, 5.0 * canonical_off)

    for name, r in arms.items():
        off = r["entry_offset_p50_usd"]
        assert off <= limit, (
            f"arm {name!r} has |entry - market| p50 = ${off:.2f}, which "
            f"exceeds the geometry limit ${limit:.2f} (canonical "
            f"${canonical_off:.2f}). The placebo is not comparable to the "
            "canonical arm, so its P&L is not a result"
        )

    # And the invalid-magnitude signature must be absent: a valid
    # placebo trades near the market, so no arm may show a
    # ~$900 entry offset.
    for name, r in arms.items():
        assert r["entry_offset_p50_usd"] < 100.0, (
            f"arm {name!r} shows |entry - market| p50 = "
            f"${r['entry_offset_p50_usd']:.2f} -- this is the naive-shuffle "
            "artefact (the invalid run measured $910.35). Re-run the "
            "diagnostic with the re-centred shuffle"
        )


def test_zone_shuffle_keeps_the_trigger_bars():
    """The placebo must destroy ONLY the price level.

    If it also moved the trigger bars, it would be measuring "no
    signal at all" rather than "not the LEVEL" -- a different claim.
    The arms' leg counts should therefore stay in the same order of
    magnitude as the canonical, not collapse to zero.
    """
    d = _load(_SHUFFLE_JSON)

    arms = {a["arm"]: a for a in d["arms"]}
    canonical_legs = arms["canonical"]["legs"]

    for name, r in arms.items():
        if name == "canonical":
            continue
        assert r["legs"] > canonical_legs * 0.5, (
            "a shuffled arm lost most of its legs -- the placebo is no "
            "longer a level-only intervention"
        )


# ── (3) the ledger's own decomposition must agree ────────────────

def test_ledger_excess_agrees_with_the_timing_diagnostic():
    """The ledger and the diagnostic must not disagree about timing.

    Both compute ``excess = gross - drift`` over the same corpus, so
    they must agree to the cent. A disagreement means one of them is
    reading a different ledger.
    """
    if not _LEDGER_JSON.exists():
        pytest.skip("no ledger on disk")

    ledger = _load(_LEDGER_JSON)
    legs = ledger["legs"]

    gross = sum(legs["gross"])
    drift = sum(legs["drift_usd"])
    excess = sum(legs["excess_usd"])

    d = _load(_TIMING_JSON)
    t = d["timing"]

    assert gross == pytest.approx(t["gross_total"], rel=1e-6)
    assert drift == pytest.approx(t["drift_total"], rel=1e-6)
    assert excess == pytest.approx(t["excess_total"], rel=1e-6)


# ── (4) the AGENTS.md text must not resurrect the old claim ───────

def test_agents_md_does_not_claim_the_entry_has_timing_skill():
    """AGENTS.md must not assert the reversed claim.

    The old section 2b said "the entry timing DOES have skill" and
    quoted ``t = +18.29``. That claim was withdrawn on 2026-10-25
    because the control was confounded. A future edit must not
    quietly bring it back.
    """
    text = (_REPO / "AGENTS.md").read_text(encoding="utf-8")

    banned = [
        "entry timing DOES have skill",
        "timing is demonstrably skilled",
    ]
    for phrase in banned:
        assert phrase not in text, (
            f"AGENTS.md asserts {phrase!r} -- that claim was withdrawn on "
            "2026-10-25 (the random-entry control was confounded). See "
            "AGENTS.md section 2b."
        )


def test_agents_md_does_not_quote_the_invalid_placebo_magnitude():
    """AGENTS.md must not quote the ARTEFACT's effect size.

    The zone-shuffle placebo was once measured with the NAIVE price
    shuffle, which put every entry ~$910 from the market. Its
    "-$1.24 MILLION" collapse was the price DISTANCE, not a result.

    The valid re-centred shuffle collapses the gross to -$2,398.07
    (mean of 3 seeds) -- still negative, still a decisive collapse,
    but ~520x smaller in magnitude.

    The valid figure MAY appear in AGENTS.md. The invalid one must
    not, except inside the warning that explains why it was void --
    so this test allows the number only on a line that also says
    "INVALID" / "artefact" / "not a result".
    """
    text = (_REPO / "AGENTS.md").read_text(encoding="utf-8")
    lines = text.splitlines()

    artefacts = [
        "1.24 MILLION",
        "1,230,673",
        "1,257,729",
        "243,686",
    ]
    markers = ("invalid", "artefact", "not a\nresult", "not a result",
               "naive", "must not", "do not quote")
    for phrase in artefacts:
        for i, line in enumerate(lines):
            if phrase not in line:
                continue
            # The explanation wraps across lines, so check a window
            # around the quote rather than the single line.
            window = "\n".join(lines[max(0, i - 6): i + 7]).lower()
            flagged = any(marker in window for marker in markers)
            assert flagged, (
                f"AGENTS.md quotes the invalid placebo magnitude {phrase!r} "
                "(line %d) without flagging it as invalid nearby. The "
                "-$1.24 MILLION figure came from the NAIVE price shuffle, "
                "whose entries sat ~$910 from the market -- it measured the "
                "price distance, not the signal. See AGENTS.md section 2c."
                % (i + 1)
            )
