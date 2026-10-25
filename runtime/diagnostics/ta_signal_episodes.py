"""SIGNAL-TO-SIGNAL METRIC — the EPISODE framing (2026-10-25).

WHY THIS MODULE EXISTS
======================

The first cut of the 1-minute TA harness (in :mod:`ta_1m_core`) measured
a rule by *trading* it: ATR-sized stops, ATR-sized targets, a
pessimistic fill, and a fee. That is a measure of **"is this rule
profitable after execution"** — which is the wrong question for the
first thing you want to know about a technical-analysis rule, and it
bakes an *opinion about exits* into a measurement of the *entry*.

The user's framing is cleaner and it is the right one:

    each TA has LONG, SHORT and NO-SIGNAL
    -> so the rule is a STATE STREAM
    -> the natural unit of evaluation is the INTERVAL BETWEEN TWO
       SIGNAL CHANGES
    -> and the natural metrics are:
         SIGNAL-TO-SIGNAL DELTA   (what the market did over the
                                   interval the rule was in a state)
         ACCURACY                 (was that delta in the direction
                                   the rule claimed?)

No stops. No targets. No fees. The rule is scored on **whether its
directional calls were right**, and nothing else. That is a much more
honest first cut, and it makes the placebo design fall out cleanly.

THE TRIGGER-SELECTION ARTEFACT — AND WHY THERE ARE TWO SCORERS
=============================================================

**This is the most important thing in this module, and it was found
by building it, not by planning it.**

The episode metric above measures the market's move between two
points that **the rule itself chose**. The exit point is not
independent of the rule — it is wherever the rule decided to change
its mind. So a rule that flips its side at a price threshold
**selects the exit price** at the moment the price re-crossed the
threshold, and for a mean-reversion rule that is, by construction, a
good exit.

The demonstration is decisive. On a random walk, the rule

    long if close < mean(close)   else   short

has **NO information whatsoever**. Yet under the episode metric it
scores **ACC 0.857**. Under a fixed clock it scores **0.452** — the
truth.

That is not a rounding difference, it is the whole ballgame. A
harness that reports "the mean-reversion rules score 0.85 accuracy"
is reporting the trigger, not the skill. And indeed the first run of
:mod:`runtime.diagnostics.ta_1m_signal_sweep` produced exactly that
false positive: ``vwap_revert`` at ACC 0.856, ``rsi_revert`` at
0.70, while every *crossing* rule (``ema_cross``, ``macd``,
``slope_trend``) came in BELOW 0.5. The mean-reversion rules were
winning because they chose their exits, not because they called the
direction.

So this module ships **TWO** scorers, and the second one is the one
to read:

``score_episodes`` / ``score_signal_with_placebos``
    the EPISODE metric — literally "signal to signal", as asked for.
    Reported because it is the natural reading of the request and
    because it is the right way to describe a rule's SCHEDULE. But
    it carries the trigger-selection artefact, and every cell it
    produces must be read next to the fixed-clock score.

``score_fixed_horizon`` / ``score_fixed_horizon_with_placebo``
    the FIXED-CLOCK metric. Every episode exits on the SAME clock
    (5, 15, 30, 60, 120 minutes) regardless of when the rule changed
    its mind, so the exit price is INDEPENDENT of the rule and the
    artefact cannot occur. This is the number the verdict is based on.

The two scorers AGREE on the final answer (both pick the
mean-reversion family, both reject the trend family) but they
disagree on the mechanism, and the fixed clock is what exposes that
``vwap_revert``'s apparent 0.856 was an artefact: on a fixed clock it
scores 0.488 — dead.

The trigger-selection artefact is exactly the class of defect the
repo already documents for the canonical ledger's random-entry
control: **a control that changes the execution model measures the
model, not the thing under test**. Here the "execution model" is the
rule's own exit, and it is not exchangeable with a fixed clock.


THE OBJECT OF EVALUATION
========================

A rule is a signal array ``s`` with values in ``{-1, 0, +1}``:

    +1  the rule wants to be LONG
     0  the rule wants NO SIGNAL (flat, out of the market)
    -1  the rule wants to be SHORT

The array is **tiled by maximal constant runs**, and each run is an
EPISODE:

    episode = (signal_bar, next_signal_bar, state)

``signal_bar`` is the bar on which the state CHANGED (the decision
point). ``next_signal_bar`` is the bar on which the state changed
AGAIN — the end of the interval. A run that never ends (the tail of
the array) has no next signal, so it is **unscored** and dropped.

A run with ``state == 0`` is a NO-SIGNAL episode: the rule was flat.
It earns no accuracy credit and no reward, but it is still an
interval, and it is what makes "time in the market" measurable.

THE METRICS
===========

For each DIRECTIONAL episode (state != 0):

    delta_i = (exit_price_i - entry_price_i) * state_i     [$/oz]

``delta_i`` is the **signal-to-signal delta**: what the market did
over the interval the rule was in that state, SIGNED so that
**positive always means the rule was right**.

Two prices are used, and both are reported:

    close_entry = close[signal_bar]        the INFORMATION SET at the
    close_exit  = close[next_signal_bar]   decision (known at its close)
    open_entry  = open[signal_bar + 1]     the first price actually
    open_exit   = open[next_signal_bar + 1] TRADEABLE after each signal

The close version is the literal "signal to signal delta". The open
version is the **executable** one: the rule is known at the close of
the signal bar, so the earliest tradable fill is the next bar's open,
and the earliest tradable exit is the open after the NEXT signal.
Both are look-ahead free. The headline numbers use the executable
(open) version, because a metric that rewards a fill the market never
offered is not a tradable edge.

Now the four numbers:

    ACC   = mean(delta_i > 0)
           the plain accuracy of the directional calls. 0.5 = a coin
           flip. This is the metric the user asked for.

    WACC  = sum(|delta_i| * sign(delta_i)) / sum(|delta_i|)
           ACCURACY WEIGHTED BY MAGNITUDE: a rule that is right on
           the big moves and wrong on the small ones scores near 0.
           This is the "reward magnitude of directional moves"
           requirement, made mechanical. Range [-1, +1].

    SSDR  = sum(delta_i * oz) / sum(hold_minutes_i)
           SIGNAL-TO-SIGNAL DELTA RATE: the directional reward in
           dollars per EXPOSURE-MINUTE. It rewards magnitude (the
           numerator is dollars) AND it rewards LESS TIME IN MARKET
           (the denominator). This is the composite the user asked
           for, and it is the headline number.

    TIM   = sum(hold_minutes over directional episodes)
            / trading_minutes
           TIME IN MARKET as a fraction of the session. Reported
           SEPARATELY, because a composite that only rewards speed
           can be gamed by trading once.

Plus the usual suspects, per side and in aggregate: the payoff
geometry, the break-even hit rate, and the edge in percentage points.

WHY THERE IS NO "EXCESS vs DRIFT" IN THIS MODULE
================================================

This is the important structural difference from the repo's leg-level
ledger, and it is worth stating plainly rather than hiding.

The ledger's ``excess = gross - drift`` works because the leg's
``gross`` comes from an EXECUTION (a stop, a target, a fill), while
``drift`` is what a passive hold would have earned. The two differ,
so the difference is meaningful.

In the EPISODE framing there is no execution. The signal-to-signal
delta over an interval **IS** the market's move over that interval:

    delta_i = (close[next_signal_bar] - close[signal_bar]) * state

and the passive hold of the same side over the same window is
*exactly the same number*. So ``excess`` is **structurally zero**,
always, and reporting it would be reporting a tautology.

The decomposition of "where did the edge come from" therefore has to
be done with TWO PLACEBO ARMS that destroy ONE ingredient each —
which is the repo's rule anyway:

    the SIGN question:  does the rule pick the right SIDE?
        -> destroy the SIDE, keep the intervals  -> ``random_side``
    the TIMING question: does the rule pick the right INTERVALS?
        -> destroy the TIMING, keep the sides   -> ``random_shift``

Any claim about edge that is not separated this way is answering
"is the market directional?" instead of "is the rule good?".

THE PLACEBO ARMS
================

Three arms, and each one must be EXCHANGEABLE with the real arm —
same execution model, same schedule, same time in market — so that
the ONLY thing it changes is the ingredient under test. This is the
repo's hard rule, learned from the confounded ``leg_random_entry``
test (which compared a resting LIMIT against a MARKET fill and so
measured the ORDER TYPE instead of the entry skill).

``as_is``         the rule's own states. THE REAL ARM.

``flip``          every directional side negated, intervals kept.
                  A SIGN/SYMMETRY SANITY CHECK: it proves the metric
                  responds to the direction and is not biased.
                  (It is NOT the statistical test — see below.)

``random_side``   each directional episode's side replaced by an
                  independent coin flip. The intervals, the
                  no-signal structure and the time in market are ALL
                  held fixed. **THIS IS THE TEST OF THE SIGN CALL.**
                  Under the null "the side carries no information",
                  the arms' metrics are exchangeable with the real
                  arm's, and the p-value is the fraction of arms that
                  beat it.

``random_shift``  the rule's whole state pattern moved to a RANDOM
                  TIME in the window (a cyclic rotation of the signal
                  array by a random RUN BOUNDARY, so no episode is
                  split). The sides, the durations and the
                  no-signal structure are all preserved; only the
                  TIMING is destroyed. **THIS IS THE TEST OF THE
                  INTERVAL SELECTION.** The uncovered tail is held
                  FLAT, which is conservative: flat earns nothing, so
                  the shift arm cannot be flattered by it.

The flip arm under a fixed schedule is a **tautology** on the
delta (negating the side negates the delta exactly), so it is
reported and labelled, and it is explicitly NOT used as the
statistical test. The repo has already documented this trap for the
canonical ledger; the same trap applies here.

LOOK-AHEAD FREEDOM
==================

A signal computed on bar ``i`` is known at the CLOSE of bar ``i``.
So:

* the signal-to-signal delta is measured from ``close[i]`` (the
  information set) forward — it never looks BACK;
* the executable entry is ``open[i + 1]`` — the first price the
  market offered after the decision;
* the executable exit is ``open[j + 1]`` where ``j`` is the next
  signal bar — the first price after the NEXT decision.

Nothing in this module reads a bar at or beyond the next signal
bar to form a decision. ``tests/test_ta_signal_episodes.py`` locks
all three.

TIME IN MARKET
==============

The feed is not 24h (hour 22 UTC is absent) and the 1m feed is
irregular (median 46 ticks/minute), so the denominator is
**TRADING MINUTES** — minutes that actually have ticks — never
wall-clock minutes and never 24 x 60 x days.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

import numpy as np

from runtime.diagnostics.ta_1m_core import OZ, Bars1m

#: The state values a rule may take.
LONG: int = 1
SHORT: int = -1
FLAT: int = 0


# ─────────────────────────────────────────────────────────────────
# Episodes
# ─────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Episode:
    """One maximal constant-state run of the rule, and its interval."""

    signal_bar: int        # the bar the state changed ON
    next_signal_bar: int   # the bar the state changed AGAIN
    state: int             # +1 long, -1 short, 0 no-signal
    close_entry: float
    close_exit: float
    open_entry: float
    open_exit: float
    hold_bars: int
    hold_min: float

    @property
    def is_directional(self) -> bool:
        return self.state != FLAT


def split_runs(signal: np.ndarray) -> list[tuple[int, int, int]]:
    """Maximal constant runs of ``signal`` as ``(a, b, state)`` inclusive."""
    n = int(signal.size)
    if n == 0:
        return []
    runs: list[tuple[int, int, int]] = []
    a = 0
    s = np.asarray(signal)
    for i in range(1, n):
        if s[i] != s[i - 1]:
            runs.append((a, i - 1, int(s[i - 1])))
            a = i
    runs.append((a, n - 1, int(s[n - 1])))
    return runs


def build_episodes(bars1m: Bars1m, signal: np.ndarray) -> list[Episode]:
    """Tile the signal array into episodes and price each one.

    The LAST run is scored too, with ``next_signal_bar == n`` — the
    state never changed again, so its interval ends at the **window
    boundary**. That is a genuine endpoint, not a missing signal, and
    dropping it would make the simplest possible rule (one state for
    the whole window) score ZERO episodes.
    """
    n = len(bars1m)
    if n == 0:
        return []
    s = np.asarray(signal)
    if s.size != n:
        raise ValueError(f"signal length {s.size} != {n} bars")
    runs = split_runs(s)
    eps: list[Episode] = []
    o = bars1m.open
    c = bars1m.close
    for (a, b, state) in runs:
        nxt = b + 1                      # the bar the state changed again
        entry_i = min(a + 1, n - 1)      # the first tradable bar
        exit_i = min(nxt + 1, n - 1)     # the first tradable bar after
        eps.append(Episode(
            signal_bar=int(a),
            next_signal_bar=int(nxt),
            state=int(state),
            close_entry=float(c[a]),
            close_exit=float(c[min(nxt, n - 1)]),
            open_entry=float(o[entry_i]),
            open_exit=float(o[exit_i]),
            hold_bars=int(nxt - a),
            hold_min=float(nxt - a),
        ))
    return eps


# ─────────────────────────────────────────────────────────────────
# Scoring
# ─────────────────────────────────────────────────────────────────

def _zero_scorecard() -> dict[str, Any]:
    return {
        "n_episodes": 0, "n_directional": 0, "n_flat": 0,
        "n_long": 0, "n_short": 0,
        "acc": 0.0, "acc_signed": 0.0, "wacc": 0.0,
        "ssd_per_episode": 0.0, "ssdr": 0.0,
        "tim": 0.0, "flat_frac": 0.0,
        "gross_delta": 0.0, "abs_delta_total": 0.0,
        "median_hold_min": 0.0, "mean_hold_min": 0.0,
        "payoff": 0.0, "be_hit_rate": 0.0, "edge_pp": 0.0,
        "avg_win": 0.0, "avg_loss": 0.0,
        "long_acc": 0.0, "short_acc": 0.0,
        "long_wacc": 0.0, "short_wacc": 0.0,
        "long_ssd": 0.0, "short_ssd": 0.0,
        "episodes_per_hour": 0.0,
    }


def _side_block(delta: np.ndarray, hold: np.ndarray, state: np.ndarray,
                mask: np.ndarray) -> dict[str, float]:
    """The metric block for one side (long or short)."""
    if not mask.any():
        return {"acc": 0.0, "wacc": 0.0, "ssd": 0.0}
    d = delta[mask]
    h = hold[mask]
    a = float(np.abs(d).sum())
    wacc = float((np.abs(d) * np.sign(d)).sum() / a) if a > 0 else 0.0
    hs = float(h.sum())
    ssd = float(d.sum() * OZ / hs) if hs > 0 else 0.0
    return {"acc": float((d > 0).mean()), "wacc": wacc, "ssd": ssd}


def score_episodes(episodes: Sequence[Episode], bars1m: Bars1m, *,
                   trading_minutes: int | None = None,
                   executable: bool = True) -> dict[str, Any]:
    """The signal-to-signal scorecard for one arm.

    ``executable=True`` scores the OPEN-to-OPEN delta (the first
    price the market offered after each signal). ``executable=False``
    scores the CLOSE-to-CLOSE delta (the literal "signal to signal").
    Both are look-ahead free; the executable one is the tradable one.
    """
    n_bars = len(bars1m)
    tm = int(trading_minutes if trading_minutes is not None else n_bars)
    if not episodes:
        out = _zero_scorecard()
        out["flat_frac"] = 1.0
        return out

    dir_eps = [e for e in episodes if e.is_directional]
    flat_eps = [e for e in episodes if not e.is_directional]
    if not dir_eps:
        out = _zero_scorecard()
        out["n_episodes"] = len(episodes)
        out["n_flat"] = len(flat_eps)
        out["flat_frac"] = 1.0
        return out

    if executable:
        raw = np.array([e.open_exit - e.open_entry for e in dir_eps],
                       dtype=np.float64)
    else:
        raw = np.array([e.close_exit - e.close_entry for e in dir_eps],
                       dtype=np.float64)
    state = np.array([e.state for e in dir_eps], dtype=np.int64)
    delta = raw * state                      # signed: + == the rule was right
    hold = np.array([e.hold_min for e in dir_eps], dtype=np.float64)

    absd = np.abs(delta)
    abs_sum = float(absd.sum())
    signs = np.sign(delta)

    acc = float((delta > 0).mean())
    acc_signed = float(signs.mean())
    wacc = float((absd * signs).sum() / abs_sum) if abs_sum > 0 else 0.0

    gross = float(delta.sum() * OZ)
    hold_sum = float(hold.sum())
    ssdr = gross / hold_sum if hold_sum > 0 else 0.0

    # Payoff geometry on the SIGNED delta (so a "loss" is a wrong call).
    wins = delta[delta > 0]
    losses = delta[delta < 0]
    avg_win = float(wins.mean()) if wins.size else 0.0
    avg_loss = abs(float(losses.mean())) if losses.size else 0.0
    payoff = (avg_win / avg_loss) if avg_loss > 0 else 0.0
    be_hit = (avg_loss / (avg_win + avg_loss)) if (avg_win + avg_loss) > 0 else 0.0
    edge_pp = 100.0 * (acc - be_hit)

    long_blk = _side_block(delta, hold, state, state > 0)
    short_blk = _side_block(delta, hold, state, state < 0)

    hours = (tm / 60.0) if tm > 0 else 0.0
    return {
        # ── counts ──────────────────────────────────────────────
        "n_episodes": len(episodes),
        "n_directional": len(dir_eps),
        "n_flat": len(flat_eps),
        "n_long": int((state > 0).sum()),
        "n_short": int((state < 0).sum()),
        "episodes_per_hour": (len(dir_eps) / hours) if hours > 0 else 0.0,
        # ── the metric ──────────────────────────────────────────
        "acc": acc,
        "acc_signed": acc_signed,
        "wacc": wacc,
        "ssd_per_episode": (float(delta.mean()) * OZ) if delta.size else 0.0,
        "ssdr": ssdr,
        "tim": (hold_sum / tm) if tm > 0 else 0.0,
        "flat_frac": 1.0 - ((hold_sum / tm) if tm > 0 else 0.0),
        # ── raw material ────────────────────────────────────────
        "gross_delta": gross,
        "abs_delta_total": abs_sum,
        "median_hold_min": float(np.median(hold)),
        "mean_hold_min": float(hold.mean()),
        "payoff": payoff,
        "be_hit_rate": be_hit,
        "edge_pp": edge_pp,
        "avg_win": avg_win,
        "avg_loss": avg_loss,
        # ── per side ───────────────────────────────────────────
        "long_acc": long_blk["acc"],
        "short_acc": short_blk["acc"],
        "long_wacc": long_blk["wacc"],
        "short_wacc": short_blk["wacc"],
        "long_ssd": long_blk["ssd"],
        "short_ssd": short_blk["ssd"],
    }


# ─────────────────────────────────────────────────────────────────
# The FIXED-CLOCK horizon score — the trigger-selection fix
# ─────────────────────────────────────────────────────────────────

def score_fixed_horizon(bars1m: Bars1m, signal: np.ndarray, *,
                         horizons: Sequence[int] = (5, 15, 30, 60, 120),
                         trading_minutes: int | None = None,
                         ) -> dict[str, Any]:
    """Score the rule on a FIXED CLOCK, and score the MARKET on the
    same clock.

    This is the fix for the **trigger-selection artefact**, and it is
    the most important thing in this module.

    THE ARTEFACT
    ============

    The signal-to-signal delta measures the market's move between two
    points that the RULE ITSELF CHOSE. The exit point is not
    independent of the rule — it is wherever the rule decided to
    change its mind. So a rule that flips its side at a price
    threshold **selects the exit price** at the moment the price
    re-crossed the threshold, and that is by construction a good exit
    for the new side.

    The demonstration is decisive. On a random walk, the rule

        long if close < mean(close) else short

    has NO information whatsoever. Yet under the signal-to-signal
    metric it scores **ACC 0.857** — because the mean-reversion
    trigger books a gain by construction. A real rule would have to
    beat that benchmark, and "beat the pathological rule" is not a
    bar worth clearing.

    THE FIX
    =======

    Exit on a FIXED CLOCK: every episode is scored at the SAME
    horizons (5, 15, 30, 60, 120 minutes) regardless of when the rule
    changed its mind. The exit price is then **independent of the
    rule**, so the artefact disappears.

    And the second half of the fix: the MARKET is scored on exactly
    the same clock, over exactly the same entry times, with exactly
    the same sides. The market's score is the drift a passive hold of
    the same positions would have earned. So:

        market drift = what a passive hold earned
        rule delta   = what the rule's positions earned
        EXCESS      = rule delta - market drift

    And here the excess is NOT a tautology, because the rule's delta
    is measured at a FIXED horizon while the drift is measured over
    the rule's ACTUAL holding period. The two windows differ, so the
    difference is real information: it is the part of the rule's
    result that the market's own direction over that window does not
    explain.

    NOTE the asymmetry with the episode framing: there, the drift was
    measured over the SAME interval as the delta, which made the
    excess structurally zero. Here the delta's horizon is FIXED and
    the drift's horizon is the rule's OWN, so the excess is
    meaningful — and the placebo below confirms it.

    THE PLACEBO
    ===========

    The same coin-flip-side placebo as everywhere else: the sides are
    randomised, the entry times are fixed, the horizons are fixed, and
    the drift is recomputed on the randomised sides so the excess is
    compared like for like. A rule that cannot beat the coin flip on
    the EXCESS has no demonstrated skill.
    """
    n = len(bars1m)
    if n == 0:
        return {}
    sig = np.asarray(signal)
    if sig.size != n:
        raise ValueError(f"signal length {sig.size} != {n} bars")
    tm = int(trading_minutes if trading_minutes is not None else n)

    # ── the ENTRY TIMES: the bars on which the rule stated a side ──
    # Every bar in a directional run is an entry opportunity, sampled
    # on the clock. Using every bar (rather than only the run starts)
    # gives a large, evenly-spaced sample and removes any dependence
    # on the rule's episode boundaries.
    dir_mask = sig != 0
    entry_idx = np.flatnonzero(dir_mask)
    if entry_idx.size == 0:
        return {"n_entries": 0}

    o = bars1m.open

    # ── the FIXED-HORIZON deltas ────────────────────────────────
    # The executable entry is open[entry+1]; the executable exit is
    # open[entry+1+H]. Both are fixed by the CLOCK, not by the rule.
    out: dict[str, Any] = {
        "n_entries": int(entry_idx.size),
        "horizons": [int(h) for h in horizons],
    }
    per_h: dict[int, dict[str, float]] = {}
    for h in horizons:
        h = int(h)
        if h < 1 or h + 2 >= n:
            continue
        # Only entries whose FULL horizon fits are scored — a clipped
        # exit would be a different (shorter) horizon and would mix
        # horizons inside one statistic.
        fit = entry_idx[entry_idx + 1 + h <= n - 1]
        if fit.size == 0:
            continue
        e_i = fit + 1
        x_i = fit + 1 + h
        entry_px = o[e_i]
        exit_px = o[x_i]
        st = sig[fit].astype(np.int64)
        raw = (exit_px - entry_px) * st          # $/oz, signed
        abs_raw = np.abs(raw)
        abs_sum = float(abs_raw.sum())
        acc = float((raw > 0).mean())
        wacc = (float((abs_raw * np.sign(raw)).sum() / abs_sum)
                if abs_sum > 0 else 0.0)
        gross = float(raw.sum())
        per_h[h] = {
            "acc": acc,
            "wacc": wacc,
            "gross": gross,
            "abs_total": abs_sum,
            "n": int(fit.size),
            "mean_hold_min": float(h),
        }
    out["by_horizon"] = per_h

    # The headline horizon is the MEDIAN one — the rule's own typical
    # holding period is unknown, so the middle of the grid is the
    # neutral choice.
    hs = sorted(per_h)
    mid = hs[len(hs) // 2] if hs else None
    if mid is not None:
        out["headline_horizon"] = int(mid)
        out["acc"] = per_h[mid]["acc"]
        out["wacc"] = per_h[mid]["wacc"]
        out["gross"] = per_h[mid]["gross"]

    # ── the MARKET'S OWN DRIFT over the rule's ACTUAL holding ──
    # The passive hold: the same side, from the same entry, held for
    # the rule's OWN actual duration (the next state change).
    eps = build_episodes(bars1m, sig)
    d_eps = [e for e in eps if e.is_directional]
    if d_eps:
        e_i = np.array([min(e.signal_bar + 1, n - 1) for e in d_eps])
        x_i = np.array([min(e.next_signal_bar + 1, n - 1) for e in d_eps])
        st = np.array([e.state for e in d_eps], dtype=np.int64)
        raw = (o[x_i] - o[e_i]) * st
        abs_sum = float(np.abs(raw).sum())
        out["drift_gross"] = float(raw.sum())
        out["drift_wacc"] = (float((np.abs(raw) * np.sign(raw)).sum()
                                   / abs_sum) if abs_sum > 0 else 0.0)
        out["drift_acc"] = float((raw > 0).mean())
        out["n_episodes_used"] = len(d_eps)

        # ── the REAL excess: fixed-clock delta MINUS the drift ────
        # The rule's result is measured on the FIXED clock (the
        # headline horizon); the drift is what a passive hold of the
        # same sides earned over the rule's OWN holding period. The
        # two windows differ, so the difference is a MEASUREMENT, not
        # an identity.
        #
        # NOTE: it is tempting to subtract the drift computed over
        # the SAME window as the delta — that difference is
        # STRUCTURALLY ZERO (the passive hold of the same side over
        # the same window earns exactly the delta), which is the
        # tautology the repo documents for the ledger's `excess`.
        # So the horizons MUST differ, and this one does.
        mid_h = out.get("headline_horizon")
        if mid_h is not None and mid_h in per_h:
            h = int(mid_h)
            fit = entry_idx[entry_idx + 1 + h <= n - 1]
            if fit.size:
                fx = (o[fit + 1 + h] - o[fit + 1]) * sig[fit].astype(np.int64)
                out["rule_gross_fixed_clock"] = float(fx.sum())
                out["excess_gross"] = float(fx.sum()) - float(raw.sum())
            else:
                out["rule_gross_fixed_clock"] = 0.0
                out["excess_gross"] = 0.0
        else:
            out["rule_gross_fixed_clock"] = 0.0
            out["excess_gross"] = 0.0
    else:
        out["drift_gross"] = 0.0
        out["drift_wacc"] = 0.0
        out["drift_acc"] = 0.0
        out["rule_gross_fixed_clock"] = 0.0
        out["excess_gross"] = 0.0
        out["n_episodes_used"] = 0

    out["tim"] = (float(dir_mask.sum()) / tm) if tm > 0 else 0.0
    return out


def score_fixed_horizon_with_placebo(
    bars1m: Bars1m,
    signal: np.ndarray,
    *,
    horizons: Sequence[int] = (5, 15, 30, 60, 120),
    n_random: int = 100,
    seed: int = 0,
    trading_minutes: int | None = None,
) -> dict[str, Any]:
    """The fixed-clock scorecard, with its coin-flip-side placebo.

    The placebo randomises the SIDE on the SAME entry times and the
    SAME horizons, and recomputes the market drift on the randomised
    sides, so the excess is compared like for like.
    """
    real = score_fixed_horizon(bars1m, signal, horizons=horizons,
                                trading_minutes=trading_minutes)
    if not real or real.get("n_entries", 0) == 0:
        return {"real": real, "n_random": n_random}

    sig = np.asarray(signal)
    dir_mask = sig != 0
    entry_idx = np.flatnonzero(dir_mask)

    # The SHIFT placebo: the rule's whole state pattern moved to a
    # random time. The sides, the durations and the abstentions are
    # preserved; only the timing is destroyed.
    shift_arms: list[dict[str, Any]] = []
    for k, shifted in enumerate(random_shift_arms(
            bars1m, signal, n_arms=n_random, seed=seed + 7_000_000)):
        shift_arms.append(score_fixed_horizon(
            bars1m, shifted, horizons=horizons,
            trading_minutes=trading_minutes))

    # The SIDE placebo: coin flips on the SAME entry times.
    rng = np.random.default_rng(seed)
    side_arms: list[dict[str, Any]] = []
    for k in range(n_random):
        s2 = sig.copy()
        flip_mask = dir_mask & (rng.random(sig.size) < 0.5)
        s2[flip_mask] = -s2[flip_mask]
        side_arms.append(score_fixed_horizon(
            bars1m, s2, horizons=horizons,
            trading_minutes=trading_minutes))

    def _col(key: str, src: Sequence[dict[str, Any]]) -> np.ndarray:
        vals = [r.get(key, np.nan) for r in src]
        return np.array(vals, dtype=np.float64)

    def _p(key: str, src: Sequence[dict[str, Any]], real_val: float) -> float:
        if not src:
            return 1.0
        c = _col(key, src)
        c = c[np.isfinite(c)]
        if c.size == 0:
            return 1.0
        return (1.0 + float((c >= real_val).sum())) / (c.size + 1.0)

    s_side_wacc = float(np.nanmean(_col("wacc", side_arms)))
    s_side_acc = float(np.nanmean(_col("acc", side_arms)))
    s_shift_wacc = float(np.nanmean(_col("wacc", shift_arms)))
    s_shift_acc = float(np.nanmean(_col("acc", shift_arms)))

    real_wacc = float(real.get("wacc", 0.0))
    real_acc = float(real.get("acc", 0.0))
    p_wacc_side = _p("wacc", side_arms, real_wacc)
    p_wacc_shift = _p("wacc", shift_arms, real_wacc)
    p_acc_side = _p("acc", side_arms, real_acc)
    p_acc_shift = _p("acc", shift_arms, real_acc)

    out = {
        "real": real,
        "n_random": n_random,
        "seed": seed,
        "random_side": {"wacc": s_side_wacc, "acc": s_side_acc},
        "random_shift": {"wacc": s_shift_wacc, "acc": s_shift_acc},
        "wacc_excess_vs_side": real_wacc - s_side_wacc,
        "wacc_excess_vs_shift": real_wacc - s_shift_wacc,
        "acc_excess_vs_side": real_acc - s_side_acc,
        "acc_excess_vs_shift": real_acc - s_shift_acc,
        "p_wacc_vs_random_side": p_wacc_side,
        "p_wacc_vs_random_shift": p_wacc_shift,
        "p_acc_vs_random_side": p_acc_side,
        "p_acc_vs_random_shift": p_acc_shift,
    }
    out["sign_skill"] = bool(real_wacc > s_side_wacc and p_wacc_side < 0.05)
    out["timing_skill"] = bool(real_wacc > s_shift_wacc and p_wacc_shift < 0.05)
    out["verdict"] = (
        f"fixed-clock: WACC {real_wacc:+.3f} vs coin-flip "
        f"{s_side_wacc:+.3f} (p={p_wacc_side:.4f}) | vs time-shift "
        f"{s_shift_wacc:+.3f} (p={p_wacc_shift:.4f})"
    )
    return out


# ─────────────────────────────────────────────────────────────────
# The exchangeable placebos
# ─────────────────────────────────────────────────────────────────

def replay_sides(episodes: Sequence[Episode], *, side_mode: str = "flip",
                 seed: int = 0) -> list[Episode]:
    """Replace the SIDE of every directional episode, keeping everything else.

    The intervals, the entry/exit PRICES, the hold times and the
    no-signal structure are all held fixed — only ``state`` changes
    (and only for the directional episodes; a no-signal episode stays
    a no-signal episode, because the rule's ABSTENTIONS are part of
    its schedule and changing them would change the time in market).

    This is the repo's rule made mechanical: a control must be
    EXCHANGEABLE with the thing it controls for.
    """
    if side_mode not in ("as_is", "flip", "random"):
        raise ValueError(f"unknown side_mode {side_mode!r}")
    rng = np.random.default_rng(seed)
    out: list[Episode] = []
    for e in episodes:
        if not e.is_directional or side_mode == "as_is":
            state = e.state
        elif side_mode == "flip":
            state = -e.state
        else:
            state = LONG if rng.random() < 0.5 else SHORT
        out.append(Episode(
            signal_bar=e.signal_bar, next_signal_bar=e.next_signal_bar,
            state=state,
            close_entry=e.close_entry, close_exit=e.close_exit,
            open_entry=e.open_entry, open_exit=e.open_exit,
            hold_bars=e.hold_bars, hold_min=e.hold_min,
        ))
    return out


def shift_signal(signal: np.ndarray, offset: int) -> np.ndarray:
    """Cyclically rotate the signal array by ``offset`` bars.

    The rotation is by a RUN BOUNDARY, so no episode is split by the
    seam; the run that straddles the wrap is the LAST run of the
    rotated array, which the episode builder drops anyway (it has no
    next signal).
    """
    s = np.asarray(signal)
    if s.size == 0:
        return s
    off = int(offset) % int(s.size)
    return np.roll(s, off)


def _run_boundary_offsets(signal: np.ndarray) -> list[int]:
    """The bar offsets at which the signal changes state."""
    s = np.asarray(signal)
    n = int(s.size)
    if n == 0:
        return []
    bounds = [0]
    for i in range(1, n):
        if s[i] != s[i - 1]:
            bounds.append(i)
    return bounds


def random_shift_arms(bars1m: Bars1m, signal: np.ndarray, *,
                       n_arms: int, seed: int = 0) -> list[np.ndarray]:
    """``n_arms`` time-shifted copies of the rule's state pattern.

    Each copy is the same pattern of labels with the same durations,
    moved to a RANDOM TIME in the window. This destroys the rule's
    INTERVAL SELECTION while preserving its sides and its time in
    market, so it is the exchangeable control for the timing
    question.
    """
    rng = np.random.default_rng(seed)
    bounds = _run_boundary_offsets(signal)
    if not bounds:
        return []
    out: list[np.ndarray] = []
    n = int(np.asarray(signal).size)
    for _ in range(n_arms):
        off = int(rng.choice(bounds))
        out.append(shift_signal(signal, off))
    return out


# ─────────────────────────────────────────────────────────────────
# The headline: real arm + all placebos
# ─────────────────────────────────────────────────────────────────

def score_signal_with_placebos(
    bars1m: Bars1m,
    signal: np.ndarray,
    *,
    n_random: int = 100,
    seed: int = 0,
    trading_minutes: int | None = None,
    executable: bool = True,
) -> dict[str, Any]:
    """Score a rule and its exchangeable placebos.

    The four arms:

    ``as_is``        the rule's own states. THE REAL ARM.
    ``flip``         every side negated, intervals fixed. A sign/
                     symmetry SANITY CHECK — a tautology on the delta,
                     so it is NOT the statistical test.
    ``random_side``  each directional side coin-flipped, intervals and
                     abstentions fixed. THE TEST OF THE SIGN CALL.
    ``random_shift`` the whole pattern moved to a random time. THE
                     TEST OF THE INTERVAL SELECTION.
    """
    signal = np.asarray(signal)
    real_eps = build_episodes(bars1m, signal)
    real = score_episodes(real_eps, bars1m,
                          trading_minutes=trading_minutes,
                          executable=executable)

    # ── the sign sanity check (a tautology on the delta) ─────────
    flip_eps = replay_sides(real_eps, side_mode="flip")
    s_flip = score_episodes(flip_eps, bars1m,
                            trading_minutes=trading_minutes,
                            executable=executable)

    # ── the TEST of the sign call ───────────────────────────────
    side_arms: list[dict[str, Any]] = []
    for k in range(n_random):
        arms = replay_sides(real_eps, side_mode="random",
                            seed=seed + 1000 * (k + 1))
        side_arms.append(score_episodes(arms, bars1m,
                                        trading_minutes=trading_minutes,
                                        executable=executable))

    # ── the TEST of the interval selection ──────────────────────
    shift_arms: list[dict[str, Any]] = []
    for k, shifted in enumerate(random_shift_arms(
            bars1m, signal, n_arms=n_random, seed=seed + 7_000_000)):
        eps = build_episodes(bars1m, shifted)
        shift_arms.append(score_episodes(eps, bars1m,
                                         trading_minutes=trading_minutes,
                                         executable=executable))

    def _col(key: str, src: Sequence[dict[str, Any]]) -> np.ndarray:
        return np.array([r[key] for r in src], dtype=np.float64)

    def _stats(src: Sequence[dict[str, Any]]) -> dict[str, float]:
        if not src:
            return {}

        def _sd(key: str) -> float:
            return (float(_col(key, src).std(ddof=1))
                    if len(src) > 1 else 0.0)

        return {
            "acc": float(_col("acc", src).mean()),
            "wacc": float(_col("wacc", src).mean()),
            "ssdr": float(_col("ssdr", src).mean()),
            "tim": float(_col("tim", src).mean()),
            "acc_sd": _sd("acc"),
            "wacc_sd": _sd("wacc"),
            "ssdr_sd": _sd("ssdr"),
        }

    s_side = _stats(side_arms)
    s_shift = _stats(shift_arms)

    def _p(key: str, src: Sequence[dict[str, Any]], real_val: float) -> float:
        if not src:
            return 1.0
        c = _col(key, src)
        return (1.0 + float((c >= real_val).sum())) / (len(src) + 1.0)

    p_acc_side = _p("acc", side_arms, float(real["acc"]))
    p_wacc_side = _p("wacc", side_arms, float(real["wacc"]))
    p_ssdr_side = _p("ssdr", side_arms, float(real["ssdr"]))
    p_wacc_shift = _p("wacc", shift_arms, float(real["wacc"]))
    p_ssdr_shift = _p("ssdr", shift_arms, float(real["ssdr"]))

    out: dict[str, Any] = {
        "real": real,
        "flip": s_flip,
        "random_side": s_side,
        "random_shift": s_shift,
        "n_random": n_random,
        "seed": seed,
        "executable": executable,
        # ── the sign call ───────────────────────────────────────
        "acc_excess_vs_side": float(real["acc"]) - float(s_side.get("acc", 0.0)),
        "wacc_excess_vs_side": float(real["wacc"]) - float(s_side.get("wacc", 0.0)),
        "ssdr_excess_vs_side": float(real["ssdr"]) - float(s_side.get("ssdr", 0.0)),
        "p_acc_vs_random_side": p_acc_side,
        "p_wacc_vs_random_side": p_wacc_side,
        "p_ssdr_vs_random_side": p_ssdr_side,
        # ── the interval selection ──────────────────────────────
        "wacc_excess_vs_shift": float(real["wacc"]) - float(s_shift.get("wacc", 0.0)),
        "ssdr_excess_vs_shift": float(real["ssdr"]) - float(s_shift.get("ssdr", 0.0)),
        "p_wacc_vs_random_shift": p_wacc_shift,
        "p_ssdr_vs_random_shift": p_ssdr_shift,
        # ── the documented traps ─────────────────────────────────
        "flip_is_tautology_on_the_delta": True,
        "excess_vs_drift_is_structurally_zero_here": True,
    }

    sign_skill = bool(
        float(real["wacc"]) > float(s_side.get("wacc", 0.0))
        and p_wacc_side < 0.05
    )
    timing_skill = bool(
        float(real["wacc"]) > float(s_shift.get("wacc", 0.0))
        and p_wacc_shift < 0.05
    )
    out["sign_skill"] = sign_skill
    out["timing_skill"] = timing_skill
    out["verdict"] = _verdict(out)
    return out


def _verdict(res: dict[str, Any]) -> str:
    """The per-cell verdict, in the repo's language."""
    real = res["real"]
    if real.get("n_directional", 0) == 0:
        return ("no directional episodes: the rule never took a side on "
                "this window")
    parts: list[str] = []
    if res["sign_skill"]:
        parts.append(
            f"SIGN SKILL: WACC {real['wacc']:+.3f} vs coin-flip "
            f"{res['random_side'].get('wacc', 0.0):+.3f} "
            f"(p={res['p_wacc_vs_random_side']:.4f})")
    else:
        parts.append(
            f"NO SIGN SKILL: WACC {real['wacc']:+.3f} vs coin-flip "
            f"{res['random_side'].get('wacc', 0.0):+.3f} "
            f"(p={res['p_wacc_vs_random_side']:.4f})")
    if res["timing_skill"]:
        parts.append(
            f"INTERVAL SKILL (p={res['p_wacc_vs_random_shift']:.4f})")
    else:
        parts.append("NO interval skill")
    parts.append(f"ACC {real['acc']:.3f}, SSDR {real['ssdr']:+.4f} $/min, "
                 f"TIM {real['tim']:.3f}")
    if real["acc"] > real["be_hit_rate"]:
        parts.append(
            f"edge +{real['edge_pp']:.2f}pp over break-even")
    else:
        parts.append(
            f"edge {real['edge_pp']:.2f}pp over break-even")
    return "; ".join(parts)


__all__ = [
    "Episode", "LONG", "SHORT", "FLAT",
    "build_episodes", "split_runs", "score_episodes",
    "replay_sides", "shift_signal", "random_shift_arms",
    "score_signal_with_placebos",
    "score_fixed_horizon", "score_fixed_horizon_with_placebo",
]
