# The TA episode metric — and the fixed-side placebo that kills the result

**Date:** 2026-10-25
**Status:** measured, negative result, with a new control that the repo did not have

---

## 1. What was built

Two new diagnostics, both driven off the same 1-minute bar builder:

| file | what it is |
|---|---|
| `runtime/diagnostics/ta_signal_episodes.py` | the **EPISODE** metric — a TA rule is a state stream (`+1` / `-1` / `0`), the unit of evaluation is the interval between two signal changes, and the metrics are ACC (plain accuracy), WACC (accuracy weighted by magnitude), SSDR ($/oz per exposure-minute) and TIM (time in market) |
| `runtime/diagnostics/ta_1m_signal_sweep.py` | the scoreboard — runs all 64 TA cells with **two exchangeable placebos** |
| `runtime/diagnostics/ta_fixed_side_placebo.py` | the **fixed-side** placebo — the control that decides the question (see § 4) |

The episode framing has **no stops, no targets and no fees**. It scores
*whether the directional calls were right*, which is the right first
question for a TA rule and separates the entry's skill from the exit's
opinion. It replaces the earlier `ta_1m_core` harness, which scored a
rule by *trading* it and so mixed the two.

Two structural facts are baked into the module and reported in every
output:

* `flip` (negate every side) is a **tautology** on the delta — the same
  trap the canonical ledger documents for `flip_gross`. It is reported
  and labelled, never used as a test.
* `excess vs drift` is **structurally zero** in the episode framing,
  because the signal-to-signal delta *is* the market's move over the
  interval. So the "where did the edge come from" question needs
  placebo arms, not an excess decomposition — hence § 4.

---

## 2. The two placebos, and why they are exchangeable

| arm | what it destroys | what it holds fixed | what it tests |
|---|---|---|---|
| `random_side` | the **SIDE** choice | intervals, prices, abstentions, time in market | "is the SIGN right?" |
| `random_shift` | the **TIMING** (a cyclic rotation by a run boundary) | sides, durations, abstentions, no-signal structure | "is the INTERVAL right?" |

Both use the **same execution model** as the real arm — the same
signal array, the same prices, the same schedule — so the only thing
that changes is the ingredient under test. This is the repo's hard
rule from § 2b, learned the expensive way: `leg_random_entry` compared
a resting limit against a market fill and so measured the **order
type**, not the skill.

---

## 3. The result on the canonical one-month window (2025-01)

64 cells, 100 placebo arms each:

```
cells measured                          : 64
cells with SIGN skill (beats coin flip) : 11
cells with INTERVAL skill (beats shift)  : 14
cells with BOTH                          : 11
```

The leaders were **mean-reversion** families — `rsi_revert` and
`bb_fade` — and nothing else. The trend/momentum families
(`ema_cross`, `macd`, `keltner_break`, `roc_trend`, `donchian`,
`atr_expansion`) all failed to beat the coin-flip side arm.

Cross-checked over six **independent monthly** windows (2025-01 …
2025-06, each a separate run), the per-cell significance flags are
**not stable**: the best cell clears the coin-flip placebo in **3 of 6**
months, and the interval placebo in at most **4 of 6**. This is the
repo's "a gate evaluated on 3-6 slices will find a threshold that
works" lesson, showing up in a new place.

Pooled by family over the six months:

| family | mean WACC | mean ACC | months WACC>0 |
|---|---|---|---|
| `rsi_revert` | **+0.0639** | 0.6850 | 30/36 |
| `bb_fade` | **+0.0459** | 0.6381 | 25/30 |
| `ema_cross` | +0.0266 | 0.2763 | 20/25 |
| `slope_trend` | +0.0112 | 0.3529 | 20/30 |
| `stoch_cross` | −0.0042 | 0.3614 | 7/20 |
| `keltner_break` | −0.0081 | 0.2865 | 14/30 |
| `macd` | −0.0154 | 0.3306 | 6/15 |
| `vwap_revert` | −0.0417 | 0.8487 | 7/20 |
| `bb_break` | −0.0459 | 0.3565 | 5/30 |
| `roc_trend` | −0.0603 | 0.3898 | 8/35 |
| `donchian` | −0.0665 | 0.3859 | 5/25 |
| `atr_expansion` | −0.1392 | 0.4019 | 4/30 |

So the one-month result was **not** a fluke of a single window: the two
mean-reversion families are consistently positive, and the
momentum families are consistently negative. Something real is in
`rsi_revert` / `bb_fade`.

**But it is also not an edge.** Every cell fails the round trip. The
best is `rsi_revert(hi=80,lo=20,n=14)` at **$0.1393 of gross per
episode** against a **$0.47** round trip — **0.30×**. A positive
`SSDR` here is a claim that the directional calls were right, not a
claim of profitability.

---

## 4. THE DECISIVE CONTROL — the fixed-side placebo

The two placebos above leave a third, much more boring explanation
open:

> **THE MARKET DRIFTED, AND THE RULE WAS LONG.**

`random_side` replaces the rule's side with a coin flip. Coin flip
plus market drift is already a strong baseline — a rule can look good
without adding anything. The repo has hit this exact failure mode on
the FVG recipe, where `drift` was **130% of gross** and `excess` was
significantly **negative**.

### The arm

`always_long` / `always_short`: every directional episode's side is
forced to a constant. The **intervals, durations, prices, abstentions
and time in market are all identical** to the real arm. Only the
rule's side *choice* is replaced.

This is the **stronger** baseline, because the constant-side arm
inherits the rule's own timing selection and the market's full drift,
and removes only the side choice. Any excess over it is attributable
to the side choice **alone**.

The edge is

\[
\text{side\_edge} = \text{WACC}_{\text{rule}} - \max(\text{WACC}_{\text{always-long}},\ \text{WACC}_{\text{always-short}})
\]

with a bootstrap CI over **episodes** (the unit of observation).

### Fit period — 2025-01-01 → 2025-07-01 (continuous, 174,726 1m bars)

```
cells measured                          : 23
cells whose SIDE SELECTION beats BOTH    :  2
```

Only two cells clear the constant-side baseline:

| cell | WACC rule | always-L | always-S | edge | 95% CI |
|---|---|---|---|---|---|
| `rsi_revert(hi=80,lo=20,n=14)` | +0.1329 | +0.0512 | −0.0512 | **+0.0817** | [+0.0278, +0.1368] |
| `bb_fade(k=2.5,n=20)` | +0.1274 | +0.0523 | −0.0523 | **+0.0751** | [+0.0197, +0.1344] |

Everything else — including `bb_fade(k=1.5,n=20)`, which had cleared
**both** original placebos in January — is **at or below** a constant
side. In a rising window, "always long" scores better than the rule.

### HELD-OUT period — 2025-07-01 → 2026-01-01 (continuous, 179,722 1m bars)

```
cells measured                          : 23
cells whose SIDE SELECTION beats BOTH    :  0
  a constant-side baseline               : 23
```

**Zero.** Both fit-period winners collapse:

| cell | fit edge | held-out edge | held-out 95% CI |
|---|---|---|---|
| `rsi_revert(hi=80,lo=20,n=14)` | +0.0817 ✅ | **+0.0070** ❌ | [−0.0546, +0.0683] |
| `bb_fade(k=2.5,n=20)` | +0.0751 ✅ | **−0.0138** ❌ | [−0.0701, +0.0432] |

---

## 5. The verdict

```
VERDICT: NO TA CELL DEMONSTRATES SIDE-SELECTION SKILL.

The episode metric's apparent winners (rsi_revert, bb_fade) beat a
COIN-FLIP side placebo, but they do NOT beat a CONSTANT-SIDE
baseline, and the two that did on the fit window do not on the
held-out window.

The episode result is MARKET DRIFT, not rule skill.

And even if it were skill, the economics fail: the best cell earns
$0.1393 per episode against a $0.47 round trip (0.30x).
```

This is the **same verdict as the FVG recipe**, arrived at by a
different route, and it is now measured on the TA side too:

> The directional calls look right because the market paid anyone who
> was on the right side of the drift. The rule's own contribution,
> isolated by the control that removes only the side choice, is zero.

### What the placebo taught

The coin-flip side placebo is **necessary but not sufficient**. It
answers "is the side better than chance", and in a drifting market
"chance + drift" is a high bar that a drift-following rule can clear
by luck. The **constant-side** arm answers the actual question — "is
the side better than *the simplest possible side*" — and it inherits
the drift in the baseline, so the rule has to beat the market itself.

> **Rule: a placebo's baseline must be the SIMPLEST thing that
> captures the effect you are attributing to the rule.**
>
> Randomising the side asks "better than chance". Forcing a constant
> side asks "better than always". If the effect is drift, the constant
> side captures it and the rule shows nothing.

---

## 6. Reproduce

```bash
# the scoreboard (one month by default; the cost gate blocks >31 days)
python -m runtime.diagnostics.ta_1m_signal_sweep

# the fixed-side placebo — fit window (the default)
python -m runtime.diagnostics.ta_fixed_side_placebo

# the fixed-side placebo — held-out window
python -m runtime.diagnostics.ta_fixed_side_placebo \
  --start 2025-07-01 --end 2026-01-01 \
  --out ta_fixed_side_placebo_heldout.json
```

Guards: `tests/test_ta_fixed_side_placebo.py` locks the arm's
**exchangeability** (only the side changes), the **exactness** of the
decomposition, that the arm **detects real skill** on a synthetic
side-picker, and — the important one — that it does **not** fire on an
always-long rule on a rising market.

---

## 7. Open questions

* The `random_shift` arm holds the uncovered tail flat, which is
  conservative but not identical to the real arm's boundary
  treatment. Worth a look if a cell ever survives the fixed-side test.
* `bb_fade` and `rsi_revert` are the *same idea* with different
  parameters (fade an extreme). If any variant of it survives a
  stronger control, the family is worth a proper holdout protocol
  rather than more parameter cells.
* The FVG recipe and the TA rules fail for the **same reason** (drift,
  not skill). That is a meaningful convergence: the repo has now
  measured this on two independent signal families, on two different
  cadences, with two different metrics.
