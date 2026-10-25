"""LEG BUCKETS — the replacement for the monthly/regime evaluation.

WHY (2026-10-25)
================

The repo's evaluation paradigm used to be:

    slice the corpus into MONTHS, label each month UP / DOWN / FLAT by
    its own return, and ask "did the strategy make money in the flat
    months?"

For a high-frequency strategy that is the wrong unit:

* A month is ONE draw from the strategy's leg distribution, not a
  sample of it. After the vol gate the recipe produces ~40 legs a
  month, so "8 months" is n=8 draws of a high-variance process.
* The month boundary is an ARTEFACT. The market does not reset on the
  1st, but the engine's history-dependent caps (p95 7d, swing 24h,
  VP 7d rolling) do — and in backtest mode the p95 cap returns
  ``inf`` until its window fills, so a cold chunk trades with
  UNCAPPED targets for its first ~8 days. Measured on 2025 Q1,
  monthly chunks discard **~10% of the trade universe** versus a
  continuous run (113 legs vs 126).
* The regime label summarises a market distribution we do not have,
  at a granularity that has nothing to do with the trade.

THE REPLACEMENT
===============

**Bucket the legs.** The unit of evaluation is the trade, and the
buckets are the properties that actually determine P&L:

    (side, outcome)  ->  long-win, long-loss, short-win, short-loss

Each bucket reports the count, the gross/net, the mean per leg and
the **standard error of that mean**, so a reader can see whether the
bucket's edge is distinguishable from zero. This is the unit at
which the sample size is large enough to say anything.

THE THREE QUESTIONS
===================

The old paradigm asked one question ("is it profitable in chop?").
The bucket view answers three, each on the leg population.

1. **Is there an edge at all?** -> the per-leg gross EV, and whether
   it beats the round trip ($0.47). This is the primary number.

2. **Is the edge direction-independent?** -> the **drift/excess
   decomposition**, which is the leg-level replacement for the
   monthly beta regression:

       drift  = passive hold of the same side, same window
       excess = gross - drift

   ``drift`` is the P&L any position of that size and side would
   have earned by being there — the **directional/beta component**.
   ``excess`` is the part attributable to the strategy's **entry
   timing** — the component a directional proxy cannot have.

   The decomposition is an exact algebraic identity, so no
   regression, no monthly aggregation and no regime label is needed.

   > **Do not use the "flip" as a counterfactual.** The engine's
   > ``gross = (exit - entry) * direction * qty``, so negating
   > ``direction`` negates ``gross`` EXACTLY. A "flip" is therefore
   > ``-gross``: a tautology carrying zero information. The ledger
   > records it only to document the trap.

3. **Is the loss concentrated?** -> the loss buckets' mean and tail.
   A strategy whose losses are all the same size is a geometry
   problem; one with a fat loss tail is a risk problem.

THE CHOP TEST, AT THE RIGHT GRANULARITY
========================================

The old decisive test was "does it make money in flat months?". The
per-leg version asks the same question at the leg's own scale:
**do the legs whose local market hour was FLAT have a positive
edge?**

``local_drift_1h`` is the market's drift over the hour before each
leg's SIGNAL bar. Flat is defined on that per-leg quantity, not on
a month's close-to-close return. This is the direct replacement for
the monthly UP/DOWN/FLAT split, and it has ~30x the resolution.

WHAT THIS DOES NOT CLAIM
========================

* A positive per-leg EV that does not cover the round trip is NOT an
  edge. The report always prints ``gross_per_leg / fee_per_leg`` so
  the comparison is explicit.
* ``excess`` is the timing component of the REALISED P&L. It does
  not by itself prove the strategy is tradeable — the fee bill still
  has to be paid, and the total has to clear it.

THE FEE IS A MEASUREMENT, NOT THE SPEC
========================================

``FEE_PER_LEG = 0.47`` is the **spec** — $0.07 commission + $0.40
spread at 1.00 oz, **at $3,000/oz**. The engine charges::

    |exit + entry| * 0.5 * qty * (bps / 10000) * 2

so the fee **scales with the price at fill time**. On the 1y corpus
spot ranges **$2,331-$4,871** and the MEASURED fee per leg is
**$0.5029** — 7% above the spec.

Every verdict and diagnosis in this module is therefore computed
against the **measured** fee, read from the ledger. The spec is
reported alongside it for reference. Using the flat constant
OVERSTATES the edge by ~7%.

Usage
-----
    python -m runtime.diagnostics.leg_buckets
    python -m runtime.diagnostics.leg_buckets --json-only
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from runtime.diagnostics.leg_ledger import (  # noqa: E402
    LEDGER_PATH,
    load_ledger,
    ledger_arrays,
)

# The canonical round trip: $0.07 commission + $0.40 spread, at
# 0.01 lots x 100 contract_size = 1.00 oz, AT spot = $3,000.
#
# ⚠️ This is a SPEC, not a measurement. The engine charges
# ``|exit + entry| * 0.5 * qty * (bps / 10000) * 2``, so the fee
# SCALES WITH THE PRICE at fill time. On the 1y corpus spot ranges
# $2,331-$4,871, and the MEASURED fee per leg is **$0.5029** — 7%
# above the $0.47 spec.
#
# The verdict must therefore be computed against the MEASURED fee
# (read from the ledger), not this constant. The constant is kept
# for reference and for the "what would this cost at $3,000"
# comparison. Using the flat constant OVERSTATES the edge by ~7%.
FEE_PER_LEG = 0.47

# The bucket names, in report order.
BUCKETS: tuple[tuple[str, str], ...] = (
    ("long", "win"),
    ("long", "loss"),
    ("short", "win"),
    ("short", "loss"),
)

# The per-leg "local market regime" split, replacing the monthly
# UP/DOWN/FLAT split. The thresholds are in PERCENT of the local
# hour's drift.
LOCAL_FLAT_PCT = 0.30   # |local_drift_1h| < 0.30% of the entry price
LOCAL_UP_PCT = 0.30
LOCAL_DOWN_PCT = 0.30

# Where the random-entry timing control writes its report.
TIMING_CONTROL_PATH = (
    _REPO_ROOT / "runtime" / "diagnostics" / "_reports"
    / "leg_random_entry.json"
)


def _load_timing_control(path: Path | None = None) -> dict[str, Any] | None:
    """Load the random-entry timing control report, if it exists.

    Returns ``None`` when the report is absent -- the caller then
    reports the verdict WITHOUT the timing evidence rather than
    silently implying the control was run.
    """
    p = path or TIMING_CONTROL_PATH
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _mean_se(values: np.ndarray) -> tuple[float, float]:
    """Mean and the standard ERROR of the mean (not the std dev).

    The standard error is what says whether a bucket's edge is
    distinguishable from zero. With n legs in a bucket, the SE of the
    mean is ``std / sqrt(n)``.
    """
    n = int(values.size)
    if n == 0:
        return 0.0, 0.0
    if n == 1:
        return float(values[0]), float("nan")
    m = float(values.mean())
    se = float(values.std(ddof=1) / np.sqrt(n))
    return m, se


def _stats(gross: np.ndarray, net: np.ndarray, fee: np.ndarray,
           drift: np.ndarray, excess: np.ndarray) -> dict[str, Any]:
    """The per-bucket statistic block."""
    n = int(gross.size)
    if n == 0:
        return {
            "n": 0, "gross": 0.0, "net": 0.0, "fees": 0.0,
            "drift": 0.0, "excess": 0.0,
            "mean_gross": 0.0, "se_gross": 0.0, "t_gross": 0.0,
            "mean_net": 0.0, "se_net": 0.0, "t_net": 0.0,
            "mean_drift": 0.0, "mean_excess": 0.0,
            "gross_per_leg": 0.0,
        }
    mg, sg = _mean_se(gross)
    mn, sn = _mean_se(net)
    md, _ = _mean_se(drift)
    me, se_ex = _mean_se(excess)
    return {
        "n": n,
        "gross": float(gross.sum()),
        "net": float(net.sum()),
        "fees": float(fee.sum()),
        # The passive/beta component and the timing component.
        "drift": float(drift.sum()),
        "excess": float(excess.sum()),
        "mean_gross": mg,
        "se_gross": sg,
        "t_gross": (mg / sg) if (sg and np.isfinite(sg) and sg > 0) else 0.0,
        "mean_net": mn,
        "se_net": sn,
        "t_net": (mn / sn) if (sn and np.isfinite(sn) and sn > 0) else 0.0,
        "mean_drift": md,
        "mean_excess": me,
        "se_excess": se_ex,
        "t_excess": (me / se_ex) if (se_ex and np.isfinite(se_ex)
                                    and se_ex > 0) else 0.0,
        "gross_per_leg": float(gross.sum()) / n,
        # The MEASURED fee per leg in this bucket, and the edge
        # expressed in round trips. The measured fee is used because
        # the engine's fee scales with the fill price, so the flat
        # $0.47 spec is only right at spot = $3,000.
        "fee_per_leg": float(fee.sum()) / n,
        "gross_per_leg_over_fee": (
            (float(gross.sum()) / n) / (float(fee.sum()) / n)
            if fee.sum() > 0 else 0.0
        ),
        # The same ratio against the FLAT $0.47 spec, for comparison.
        "gross_per_leg_over_spec_fee": (float(gross.sum()) / n) / FEE_PER_LEG,
    }


def bucket_legs(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """Split the legs into the four (side, outcome) buckets."""
    side = arr["side"]
    outcome = arr["outcome"]
    out: dict[str, Any] = {}
    for s_name, o_name in BUCKETS:
        s = 1 if s_name == "long" else -1
        o = 1 if o_name == "win" else -1
        m = (side == s) & (outcome == o)
        out[f"{s_name}_{o_name}"] = _stats(
            arr["gross"][m], arr["net"][m], arr["fee"][m],
            arr["drift_usd"][m], arr["excess_usd"][m])
    return out


def overall_stats(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """Corpus-level per-leg statistics."""
    n = int(arr["gross"].size)
    if n == 0:
        return {"n": 0}
    st = _stats(arr["gross"], arr["net"], arr["fee"],
                arr["drift_usd"], arr["excess_usd"])
    st["hit_rate"] = float((arr["outcome"] > 0).mean())
    # The MEASURED fee per leg (the engine's fee scales with the
    # fill price, so the flat $0.47 spec is only right at $3,000).
    st["fee_per_leg"] = (float(arr["fee"].sum()) / n) if n else 0.0
    st["gross_per_leg_over_fee"] = (
        (st["gross_per_leg"] / st["fee_per_leg"])
        if st["fee_per_leg"] > 0 else 0.0
    )
    st["gross_per_leg_over_spec_fee"] = st["gross_per_leg"] / FEE_PER_LEG
    # The TAUTOLOGY is reported so nobody mistakes it for a test.
    st["flip_gross_total_is_tautology"] = True
    st["flip_gross_total"] = float(arr["flip_gross"].sum())
    return st


def per_side_stats(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """Does each side work on its own?"""
    out: dict[str, Any] = {}
    for s_name, s in (("long", 1), ("short", -1)):
        m = arr["side"] == s
        if not m.any():
            out[s_name] = {"n": 0}
            continue
        st = _stats(arr["gross"][m], arr["net"][m], arr["fee"][m],
                    arr["drift_usd"][m], arr["excess_usd"][m])
        st["hit_rate"] = float((arr["outcome"][m] > 0).mean())
        out[s_name] = st
    return out


def local_regime_stats(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """The chop test, at the LEG's own granularity.

    Replaces the monthly UP/DOWN/FLAT split. ``local_drift_1h`` is the
    market's drift over the hour before the leg's SIGNAL bar, as a
    percentage of the entry price.
    """
    entry = arr["entry_price"]
    local_pct = np.where(entry > 0, 100.0 * arr["local_drift_1h"] / entry, 0.0)
    regimes = {
        "flat": np.abs(local_pct) < LOCAL_FLAT_PCT,
        "up": local_pct >= LOCAL_UP_PCT,
        "down": local_pct <= -LOCAL_DOWN_PCT,
    }
    out: dict[str, Any] = {"thresholds_pct": {
        "flat": LOCAL_FLAT_PCT, "up": LOCAL_UP_PCT, "down": LOCAL_DOWN_PCT}}
    for name, m in regimes.items():
        if not m.any():
            out[name] = {"n": 0}
            continue
        out[name] = _stats(arr["gross"][m], arr["net"][m], arr["fee"][m],
                           arr["drift_usd"][m], arr["excess_usd"][m])
        out[name]["mean_local_pct"] = float(local_pct[m].mean())
    out["n_legs"] = int(arr["gross"].size)
    return out


def directional_stats(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """The direction dependence, decomposed.

    ``drift`` is the beta component; ``excess`` is the timing
    component. Their ratio says how much of the P&L is explained by
    the market's direction rather than by the strategy's entries.
    """
    gross = arr["gross"]
    drift = arr["drift_usd"]
    excess = arr["excess_usd"]
    n = int(gross.size)
    if n == 0:
        return {"n": 0}

    gross_sum = float(gross.sum())
    drift_sum = float(drift.sum())
    excess_sum = float(excess.sum())
    # The gross ACTIVITY: the sum of |gross| over the legs. This is
    # the denominator-free measure of how much P&L the book turned
    # over, and it is what the gross total is measured against.
    gross_abs_sum = float(np.abs(gross).sum())
    out: dict[str, Any] = {
        "n": n,
        "gross_total": gross_sum,
        "gross_abs_total": gross_abs_sum,
        "drift_total": drift_sum,
        "excess_total": excess_sum,
        # The share of the realised P&L that is pure market drift.
        #
        # NOTE: this ratio is only meaningful when the gross total is
        # a material fraction of the gross ACTIVITY. When winners and
        # losers nearly cancel (|gross_total| << sum|gross|) the ratio
        # explodes and flips sign, so it is reported as None in that
        # regime -- the ABSOLUTE totals are the numbers to read.
        "drift_share_of_gross": (
            (drift_sum / gross_sum)
            if (gross_sum and gross_abs_sum > 0
                and abs(gross_sum) >= 0.05 * gross_abs_sum)
            else None
        ),
        "excess_share_of_gross": (
            (excess_sum / gross_sum)
            if (gross_sum and gross_abs_sum > 0
                and abs(gross_sum) >= 0.05 * gross_abs_sum)
            else None
        ),
        # The direction-independent reading, which does NOT depend on
        # a near-zero denominator: is the TIMING component positive
        # per leg, and is it distinguishable from zero?
        "timing_edge_per_leg": excess_sum / n,
    }

    # The sign test: does the EXCESS (the timing component) have a
    # consistent sign? A direction-independent timing edge has a
    # positive mean excess and a positive t-stat.
    me, se = _mean_se(excess)
    out["mean_excess"] = me
    out["se_excess"] = se
    out["t_excess"] = (me / se) if (se and np.isfinite(se) and se > 0) else 0.0
    md, sd = _mean_se(drift)
    out["mean_drift"] = md
    out["se_drift"] = sd
    out["t_drift"] = (md / sd) if (sd and np.isfinite(sd) and sd > 0) else 0.0

    # Per-side excess — the per-side version of the old beta test.
    for s_name, s in (("long", 1), ("short", -1)):
        m = arr["side"] == s
        if not m.any():
            out[s_name] = {"n": 0}
            continue
        me_s, se_s = _mean_se(excess[m])
        md_s, sd_s = _mean_se(drift[m])
        out[s_name] = {
            "n": int(m.sum()),
            "gross": float(gross[m].sum()),
            "drift": float(drift[m].sum()),
            "excess": float(excess[m].sum()),
            "mean_excess": me_s,
            "se_excess": se_s,
            "t_excess": (me_s / se_s) if (se_s and np.isfinite(se_s)
                                         and se_s > 0) else 0.0,
            "mean_drift": md_s,
            "se_drift": sd_s,
            "t_drift": (md_s / sd_s) if (sd_s and np.isfinite(sd_s)
                                         and sd_s > 0) else 0.0,
        }
    return out


def exit_breakdown(arr: dict[str, np.ndarray], exit_reasons: list[str]
                   ) -> dict[str, Any]:
    """Per exit reason: count, gross, mean."""
    code = arr["exit_code"].astype(np.int64)
    gross = arr["gross"]
    out: dict[str, Any] = {}
    for i, reason in enumerate(exit_reasons):
        m = code == i
        n = int(m.sum())
        if n == 0:
            continue
        g = gross[m]
        mg, sg = _mean_se(g)
        out[reason] = {
            "n": n,
            "gross": float(g.sum()),
            "mean_gross": mg,
            "se_gross": sg,
        }
    return out


def monthly_view(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """Month as a REPORTING dimension only.

    The month is NOT an execution boundary and NOT a unit of
    evaluation — it is a slice of the leg population for reporting.
    Nothing is lost at a boundary because the strategy ran
    continuously.
    """
    ts = arr["signal_time_ns"]
    if ts.size == 0:
        return {}
    months = pd.to_datetime(ts, unit="ns", utc=True).strftime("%Y-%m")
    out: dict[str, Any] = {}
    for ym in sorted(set(months)):
        m = months == ym
        n = int(m.sum())
        if n == 0:
            continue
        g = arr["gross"][m]
        mg, sg = _mean_se(g)
        out[ym] = {
            "n": n,
            "gross": float(g.sum()),
            "net": float(arr["net"][m].sum()),
            "mean_gross": mg,
            "se_gross": sg,
            "drift": float(arr["drift_usd"][m].sum()),
            "excess": float(arr["excess_usd"][m].sum()),
        }
    return out


def payoff_geometry(arr: dict[str, np.ndarray]) -> dict[str, Any]:
    """The per-side payoff geometry, at the leg level.

    This is the leg-level replacement for the monthly "payoff
    breakdown". It answers the question the bucket view makes
    visible: **is the loss caused by a bad signal, or by the fee
    bill?**

    For each side:

    * ``avg_win`` / ``avg_loss`` — the realised magnitudes
    * ``payoff_ratio`` — avg_win / avg_loss
    * ``breakeven_win_rate`` — the win rate at which the side breaks
      even BEFORE fees
    * ``edge_pp`` — hit_rate - breakeven_win_rate, in percentage
      points. Positive means the signal has an edge; the question is
      then only whether it is big enough to cover the round trip.
    * ``gross_per_leg_over_fee`` — the edge expressed in round trips

    A strategy with a large positive ``edge_pp`` and a negative net is
    a **fee problem** (trade less, or make the edge bigger). A
    strategy with a non-positive ``edge_pp`` has **no signal**.
    """
    out: dict[str, Any] = {}
    for s_name, s in (("long", 1), ("short", -1)):
        m = arr["side"] == s
        if not m.any():
            out[s_name] = {"n": 0}
            continue
        gross = arr["gross"][m]
        wins = gross[gross > 0]
        losses = gross[gross <= 0]
        n = int(gross.size)
        avg_win = float(wins.mean()) if wins.size else 0.0
        avg_loss = abs(float(losses.mean())) if losses.size else 0.0
        payoff = (avg_win / avg_loss) if avg_loss > 0 else float("inf")
        hit_rate = float((gross > 0).mean())
        # Break-even win rate before fees: p*avg_win - (1-p)*avg_loss = 0
        breakeven = (avg_loss / (avg_win + avg_loss)) if (avg_win + avg_loss) > 0 else 0.0
        edge_pp = 100.0 * (hit_rate - breakeven)
        gross_per_leg = float(gross.sum()) / n
        # The MEASURED fee for this side. The engine's fee scales
        # with the fill price, so it differs between the sides and
        # from the flat $0.47 spec.
        fee_per_leg = float(arr["fee"][m].sum()) / n
        over_fee = (gross_per_leg / fee_per_leg) if fee_per_leg > 0 else 0.0
        out[s_name] = {
            "n": n,
            "hit_rate": hit_rate,
            "avg_win": avg_win,
            "avg_loss": avg_loss,
            "payoff_ratio": payoff,
            "breakeven_win_rate": breakeven,
            "edge_pp": edge_pp,
            "gross_per_leg": gross_per_leg,
            "fee_per_leg": fee_per_leg,
            "net_per_leg": float(arr["net"][m].sum()) / n,
            "gross_per_leg_over_fee": over_fee,
            "gross_per_leg_over_spec_fee": gross_per_leg / FEE_PER_LEG,
            # The two failure modes, made explicit.
            "diagnosis": (
                "no signal" if edge_pp <= 0
                else ("fee-limited" if over_fee < 1.0
                      else "profitable")
            ),
        }
    return out


def analyse(payload: dict[str, Any],
             timing_control: dict[str, Any] | None = None) -> dict[str, Any]:
    """The full leg-level analysis.

    ``timing_control`` is the optional RANDOM-ENTRY control (see
    ``leg_random_entry.py``). When supplied, the verdict distinguishes
    "the entry has no skill" from "the entry has skill but the exit
    caps the winner" -- two problems with opposite fixes.
    """
    arr = ledger_arrays(payload)
    buckets = bucket_legs(arr)
    overall = overall_stats(arr)
    per_side = per_side_stats(arr)
    local = local_regime_stats(arr)
    directional = directional_stats(arr)
    exits = exit_breakdown(arr, payload.get("exit_reasons", []))
    months = monthly_view(arr)
    payoff = payoff_geometry(arr)

    # The verdict. The threshold is the round trip: a gross edge that
    # does not cover it is not an edge.
    #
    # The fee used is the MEASURED one (the engine's fee scales with
    # the fill price), not the flat $0.47 spec. The spec is the
    # contract value at $3,000/oz; the corpus trades at $2,331-$4,871.
    gpl = overall.get("gross_per_leg", 0.0)
    fee = overall.get("fee_per_leg", FEE_PER_LEG)
    if overall.get("n", 0) == 0:
        verdict = "no edge: no legs measured"
    elif gpl < fee:
        verdict = (
            f"no edge: gross ${gpl:.4f}/leg does not cover the "
            f"${fee:.4f} measured round trip "
            f"({gpl / fee:.2f}x)"
        )
    else:
        verdict = (
            f"gross edge ${gpl:.4f}/leg covers the ${fee:.4f} "
            f"measured round trip ({gpl / fee:.2f}x)"
        )

    # The RANDOM-ENTRY control separates the three failure modes.
    #
    # A gross/leg below the fee line is ambiguous on its own: it
    # could mean "the entry has no skill" OR "the entry has skill but
    # the exit caps the winner". Those need OPPOSITE fixes, so the
    # verdict must not conflate them.
    #
    # ⚠️ The random-entry control is CONFOUNDED. The strategy fills a
    # RESTING LIMIT at the FVG zone edge while the random entrant pays
    # a MARKET close, and the pre-fill window is worse for the market
    # entrant BY GEOMETRY (100% of legs). So the control's t-stat
    # measures the limit-order mechanic, NOT timing.
    #
    # The HONEST timing number is the ledger's ``excess`` = gross −
    # drift, which uses the SAME execution model on both sides. So the
    # verdict reads ``timing`` (excess), and reports the control's
    # confound so the t-stat is never quoted as a timing result.
    timing = None
    if timing_control is not None:
        ctl = timing_control.get("control", {})
        conf = timing_control.get("confound", {})
        tim = timing_control.get("timing", {})
        timing = {
            "control": ctl,
            "confound": conf,
            "timing": tim,
            # The control is NOT a timing measure. Flag it so no
            # downstream reader can quote its t-stat as skill.
            "control_is_confounded": True,
            "pre_fill_share_of_delta": conf.get("pre_fill_share_of_delta"),
        }
        excess_total = float(tim.get("excess_total", 0.0))
        t_excess = float(tim.get("t_excess", 0.0))
        excess_per_leg = float(tim.get("excess_per_leg", 0.0))
        has_skill = excess_total > 0 and t_excess > 2.0
        if gpl >= fee:
            verdict += " [PROFITABLE]"
        elif has_skill:
            verdict += (
                f" -- but the ENTRY HAS SKILL: excess (gross - drift) is "
                f"${excess_total:,.2f} total, {excess_per_leg:+.4f}/leg, "
                f"t={t_excess:+.2f}. The blocker is the EXIT CAP or the "
                f"fee, not the entry."
            )
        else:
            verdict += (
                f" -- and the ENTRY shows no measurable skill: excess "
                f"(gross - drift) is ${excess_total:,.2f} total, "
                f"{excess_per_leg:+.4f}/leg, t={t_excess:+.2f}. The gross "
                f"is MARKET DRIFT, not timing."
            )

    return {
        "n_legs": overall.get("n", 0),
        "overall": overall,
        "buckets": buckets,
        "per_side": per_side,
        "local_regime": local,
        "directional": directional,
        "exit_breakdown": exits,
        "payoff_geometry": payoff,
        "monthly_reporting_view": months,
        "fee_per_leg": fee,
        "fee_per_leg_spec": FEE_PER_LEG,
        "timing_control": timing,
        "verdict": verdict,
    }


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:+.1f}%"


def print_report(payload: dict[str, Any], analysis: dict[str, Any]) -> None:
    ov = analysis["overall"]
    b = analysis["buckets"]
    fee = analysis.get("fee_per_leg", FEE_PER_LEG)

    print("=" * 78)
    print("LEG-LEVEL EVALUATION - (side x outcome) buckets")
    print("=" * 78)
    print(f"legs={ov.get('n', 0)}   fee/leg=${fee:.4f} measured "
          f"(spec ${FEE_PER_LEG:.2f})   "
          f"gross/leg=${ov.get('gross_per_leg', 0.0):+.4f}   "
          f"gross/leg/fee={ov.get('gross_per_leg_over_fee', 0.0):.3f}x")
    print(f"hit rate {_fmt_pct(ov.get('hit_rate', 0.0))}   "
          f"gross=${ov.get('gross', 0.0):,.2f}   "
          f"fees=${ov.get('fees', 0.0):,.2f}   "
          f"net=${ov.get('net', 0.0):,.2f}")
    print()

    print("-" * 78)
    print("BUCKETS   (drift = passive hold; excess = gross - drift = timing)")
    print("-" * 78)
    print(f"{'bucket':<14} {'n':>6} {'gross':>12} {'mean':>9} {'se':>8} "
          f"{'t':>7} {'drift':>11} {'excess':>11}")
    for name in ("long_win", "long_loss", "short_win", "short_loss"):
        d = b[name]
        print(f"{name:<14} {d['n']:>6} {d['gross']:>12,.2f} "
              f"{d['mean_gross']:>9.4f} {d['se_gross']:>8.4f} "
              f"{d['t_gross']:>7.2f} {d['drift']:>11,.2f} "
              f"{d['excess']:>11,.2f}")
    print()

    print("-" * 78)
    print("PER SIDE")
    print("-" * 78)
    for s in ("long", "short"):
        d = analysis["per_side"][s]
        if not d.get("n"):
            print(f"{s:<6} no legs")
            continue
        print(f"{s:<6} n={d['n']:>6}  gross=${d['gross']:>11,.2f}  "
              f"mean=${d['mean_gross']:>8.4f} +/-{d['se_gross']:.4f}  "
              f"t={d['t_gross']:>6.2f}  hit={_fmt_pct(d['hit_rate'])}  "
              f"excess=${d['excess']:>10,.2f}")
    print()

    print("-" * 78)
    print("DIRECTION DEPENDENCE (drift vs excess)")
    print("-" * 78)
    d = analysis["directional"]
    print(f"  total gross    ${d['gross_total']:>12,.2f}")
    print(f"  drift (beta)   ${d['drift_total']:>12,.2f}   "
          f"mean=${d['mean_drift']:>8.4f} +/-{d['se_drift']:.4f}  "
          f"t={d['t_drift']:>6.2f}")
    print(f"  excess (timing)${d['excess_total']:>12,.2f}   "
          f"mean=${d['mean_excess']:>8.4f} +/-{d['se_excess']:.4f}  "
          f"t={d['t_excess']:>6.2f}")
    if d.get("drift_share_of_gross") is not None:
        print(f"  drift share of gross: {_fmt_pct(d['drift_share_of_gross'])}")
    else:
        print("  drift share of gross: n/a (gross is near zero - read the "
              "absolute totals)")
    print(f"  timing edge per leg: ${d.get('timing_edge_per_leg', 0.0):+.4f}")
    print()

    print("-" * 78)
    print("CHOP TEST - the PER-LEG local regime (replaces monthly UP/DOWN/FLAT)")
    print("-" * 78)
    lr = analysis["local_regime"]
    print(f"  (thresholds: |local drift| < {LOCAL_FLAT_PCT}% is FLAT)")
    for name in ("flat", "up", "down"):
        v = lr.get(name, {})
        if not v.get("n"):
            print(f"  {name:<6} no legs")
            continue
        print(f"  {name:<6} n={v['n']:>6}  gross=${v['gross']:>11,.2f}  "
              f"mean=${v['mean_gross']:>8.4f} +/-{v['se_gross']:.4f}  "
              f"t={v['t_gross']:>6.2f}  excess=${v['excess']:>10,.2f}")
    print()

    print("-" * 78)
    print("PAYOFF GEOMETRY - is it a signal problem or a fee problem?")
    print("-" * 78)
    for s in ("long", "short"):
        d = analysis["payoff_geometry"][s]
        if not d.get("n"):
            print(f"  {s:<6} no legs")
            continue
        print(f"  {s:<6} hit={_fmt_pct(d['hit_rate'])}  "
              f"avg_win=${d['avg_win']:>8,.2f}  avg_loss=${d['avg_loss']:>7,.2f}  "
              f"payoff={d['payoff_ratio']:>6.2f}x")
        print(f"         break-even win rate={_fmt_pct(d['breakeven_win_rate'])}  "
              f"edge={d['edge_pp']:+.2f}pp  "
              f"gross/leg=${d['gross_per_leg']:+.4f}  "
              f"fee/leg=${d['fee_per_leg']:.4f}  "
              f"gross/leg/fee={d['gross_per_leg_over_fee']:.3f}x  "
              f"-> {d['diagnosis']}")
    print()

    print("-" * 78)
    print("EXIT REASONS")
    print("-" * 78)
    for reason, v in sorted(analysis["exit_breakdown"].items(),
                             key=lambda kv: -kv[1]["n"]):
        print(f"  {reason:<12} n={v['n']:>6}  gross=${v['gross']:>11,.2f}  "
              f"mean=${v['mean_gross']:>8.4f}")
    print()
    print(f"VERDICT: {analysis['verdict']}")
    print()


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--out", type=Path, default=None,
                    help="Optional JSON report path.")
    ap.add_argument("--json-only", action="store_true",
                    help="Print JSON to stdout instead of the table.")
    ap.add_argument("--timing", action="store_true",
                    help="Load the random-entry timing control and "
                         "fold it into the verdict.")
    args = ap.parse_args()

    payload = load_ledger(args.ledger, expect_canonical=True)

    timing = None
    if args.timing:
        timing = _load_timing_control()
        if timing is None:
            print("! timing control not found -- run "
                  "`python -m runtime.diagnostics.leg_random_entry` first. "
                  "Continuing without it.")

    analysis = analyse(payload, timing_control=timing)

    if args.json_only:
        print(json.dumps(analysis, indent=2, default=str))
        return

    print_report(payload, analysis)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(
            json.dumps({"analysis": analysis,
                        "ledger_meta": {k: v for k, v in payload.items()
                                        if k != "legs"}},
                       indent=2, default=str),
            encoding="utf-8")
        print(f"-> {args.out}")


if __name__ == "__main__":
    main()
