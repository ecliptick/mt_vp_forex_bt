# ENZO — the two-rule XAUUSD strategy from `Enzo input.pdf`

Measured 2026-10-25 on the **continuous** corpus
(`XAUUSD_S1_1y.parquet`, 2024-01-01 → 2026-07-16, a 14-day warmup
prefix, ONE execution — not month chunks).

| | |
|---|---|
| legs (Rule 2) | **124** (0.134 trades/calendar-day) |
| legs (Rule 1) | 949 (tp5/sl5) · 785 (tp10/sl10) |
| legs (Rule 1, 18/82 variant — § 1a) | 1,524 (tp5/sl5) · 1,195 (tp10/sl10) — **REJECTED, worse** |
| canonical sizing | 0.01 lots × 100 oz/lot = **1.00 oz** |
| round trip | **$0.47** at $3,000 spot; **$0.5743/leg** measured |
| fill model | pessimistic (the stop wins an ambiguous bar; a gap fills at the open) |

---

## 1. The two rules, as implemented

### Rule 1 — M1 RSI reversion

```
M1 chart, RSI (default 14 period), level set 16 and 84
Strategy: SELL when hit level 84, BUY when hit level 16.
TP and SL $5 (50 pips) and TP and SL $10 (100 pips), same strategy
but different TP SL.
If TP, no second trades. Cool down period 1 hour.
If SL, open second trades with same TP SL provided M1 still above
level 82 / below 16.
```

The episode, made explicit:

* the signal is a **crossing** into the extreme (the RSI reaches 84
  from below / 16 from above), known at the bar's CLOSE; the entry is
  the **open of the next bar**;
* a **TP** ends the episode — no second trade;
* an **SL** opens **one** second trade in the same direction with the
  same TP/SL, admitted only if the RSI is still beyond the tighter
  threshold (sell: RSI > 82, buy: RSI < 16). **A second SL does not
  license a third attempt** — the PDF says "second trades", singular;
* a **one-hour cooldown** after the episode ends. The retry is part of
  the SAME episode, so it is exempt from the cooldown (otherwise the
  cooldown would make it unreachable).

Two readings were measured and the crossing is used:

| trigger | tp/sl | n | gross | fees | net | gross/leg | × fee |
|---|---|---|---|---|---|---|---|
| crossing | $5/$5 | 949 | $88.04 | $487.64 | **−$399.60** | $0.0928 | **0.18×** |
| crossing | $10/$10 | 785 | $78.53 | $413.44 | **−$334.91** | $0.1000 | **0.19×** |
| level test | $5/$5 | 949 | $88.04 | $487.64 | −$399.60 | $0.0928 | 0.18× |
| level test | $10/$10 | 787 | $98.53 | $414.63 | −$316.10 | $0.1252 | 0.24× |

**The trigger semantics barely matter — Rule 1 loses either way.** The
gross is 0.18–0.24× the round trip. It is a fee-limited rule with a
gross too small to pay for itself, on 949 legs.

The RSI's `excess` is **positive** on the tp5 cell (+$118.42,
t = +1.29) but not significantly, and the gross is 2 orders of
magnitude below the fee bill.

**The level set is load-bearing — the wider 18/82 band is much worse**
(§ 1a). Rule 1's edge, such as it is, lives in how *extreme* the RSI
reading is.

#### 1a. The level-set variant — 18/82 instead of 16/84

**Requested 2026-10-25:** the same rule with the level set **18 and 82**
instead of the PDF's 16 and 84. Measured on the identical corpus,
the identical RSI(14), the identical episode lifecycle and the
identical fee model, by
`runtime/diagnostics/enzo_rsi_levels.py` (report:
`runtime/diagnostics/_reports/enzo_rsi_levels.json`).

The retry threshold is not stated for the new level set, so it is
derived the way the canonical one is — the retry is admitted while the
RSI is still beyond the retry threshold **on the trigger's side**:

| arm | trigger | retry admitted while |
|---|---|---|
| canonical 84/16 | sell RSI ≥ 84 / buy RSI ≤ 16 | RSI > 82 / RSI < 16 |
| **new 82/18** | sell RSI ≥ 82 / buy RSI ≤ 18 | RSI > 80 / RSI < 18 |
| new 82/18, retry 82/16 | *(as above)* | RSI > 82 / RSI < 16 *(unchanged)* |

Both readings of "hit level 84" (crossing / level test) were run for
both level sets, so the trigger change is separated from the
retry-threshold change.

| level set | trigger | tp/sl | n | gross | fees | **net** | gross/leg | × fee | hit | edge_pp | excess | t_exc | placebo p |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| canonical 84/16 | crossing | $5/$5 | 949 | $88.04 | $487.64 | **−$399.60** | $0.0928 | 0.18× | 50.7% | +0.92 | +$118.42 | +1.29 | 0.295 |
| canonical 84/16 | crossing | $10/$10 | 785 | $78.53 | $413.44 | **−$334.91** | $0.1000 | 0.19× | 51.1% | +0.49 | +$27.21 | +0.24 | 0.435 |
| **82/18** | crossing | $5/$5 | 1,524 | **−$31.92** | $781.15 | **−$813.07** | **−$0.0209** | **−0.04×** | 49.6% | −0.21 | +$225.24 | +2.05 | 0.555 |
| **82/18** | crossing | $10/$10 | 1,195 | **−$236.44** | $632.03 | **−$868.48** | **−$0.1979** | **−0.37×** | 49.5% | −0.98 | +$241.39 | +1.81 | 0.721 |
| 82/18, retry 82/16 | crossing | $5/$5 | 1,481 | −$26.92 | $761.15 | −$788.07 | −$0.0182 | −0.04× | 49.6% | −0.18 | +$208.42 | +1.92 | 0.543 |
| 82/18, retry 82/16 | crossing | $10/$10 | 1,169 | −$216.44 | $617.77 | −$834.21 | −$0.1852 | −0.35× | 49.5% | −0.92 | +$201.32 | +1.52 | 0.735 |

> **THE 18/82 LEVEL SET IS STRICTLY WORSE — AND IT TURNS THE GROSS
> NEGATIVE.**

| comparison | tp5/sl5 | tp10/sl10 |
|---|---|---|
| legs (84/16 → 82/18) | 949 → **1,524** | 785 → **1,195** |
| net (84/16 → 82/18) | −$399.60 → **−$813.07** | −$334.91 → **−$868.48** |
| **delta net** | **−$413.47** | **−$533.56** |
| gross/leg (84/16 → 82/18) | +$0.0928 → **−$0.0209** | +$0.1000 → **−$0.1979** |
| **delta gross/leg** | **−$0.1137** | **−$0.2979** |

Three things are going on, and they all point the same way:

1. **The wider band fires much more often** — 1,524 legs vs 949
   (+61%). RSI ≥ 82 hits 2,437 bars vs 1,312 for RSI ≥ 84; RSI ≤ 18
   hits 1,908 vs 1,026 for RSI ≤ 16. The extra trades are the ones
   the PDF's tighter band deliberately excluded.
2. **Those extra trades are the LOSING ones.** The gross/leg goes from
   +$0.0928 to **−$0.0209**. The canonical band's gross is small but
   *positive*; the 82/18 band's gross is *negative*. The wider band
   trades the milder, less-extreme RSI readings — and they do not
   revert.
3. **The fee bill scales with the leg count, so the net damage grows
   faster than the fee bill.** Fees rise 60% ($487.64 → $781.15 on
   tp5) but the net damage rises **103%** ($399.60 → $813.07) —
   because the gross itself has turned negative on top of the extra
   fees.

The hit rate confirms it: the canonical band hits 50.7–51.1% (a coin
flip, but a coin flip at the RIGHT price), the 82/18 band hits
49.5–49.6% — a coin flip at the WRONG price. **The `edge_pp` column
is the cleanest read:** the canonical band carries a small positive
edge (+0.92pp on tp5, +0.49pp on tp10); the 82/18 band carries a
**negative** edge (−0.21pp, −0.98pp). The rule loses *before* fees,
not after.

**Where the damage is: the SELL side.** Splitting the gross by side
shows the wider band is not uniformly bad — it is specifically the
loosened **sell** trigger (RSI ≥ 82 instead of ≥ 84) that bleeds:

| level set | tp/sl | n_long | gross_long | n_short | gross_short |
|---|---|---|---|---|---|
| canonical 84/16 | $5/$5 | 450 | **+$43.46** | 499 | **+$44.58** |
| **82/18** | $5/$5 | 726 | **+$58.31** | 798 | **−$90.24** |
| canonical 84/16 | $10/$10 | 381 | **+$411.50** | 404 | **−$332.97** |
| **82/18** | $10/$10 | 563 | **+$296.22** | 632 | **−$532.67** |

On tp5 the **long** side actually *improves* with the wider band
(+$43.46 → +$58.31) — it is the short side that flips sign
(+$44.58 → **−$90.24**, and its per-leg gross goes from **+$0.0893**
to **−$0.1131**). On tp10 both sides degrade, but the short side
again takes the bigger hit: **299 extra short legs** on tp5 cost
**−$134.82** of gross, and on tp10 the short side gives up
**−$199.70** against the long side's **−$115.28**.

> **The asymmetry has a clean reading.** The buy side (RSI ≤ 18) and
> the sell side (RSI ≥ 82) are NOT symmetric in this market — and
> they were not symmetric in the canonical band either, where the
> canonical long side earns **+$411.50** on tp10 against the short
> side's **−$332.97**. Loosening the sell band by 2 RSI points
> admits 228 extra short legs on tp10 whose gross is **−$199.70**. The
> extreme's mean-reversion on the sell side lives in the last two
> points of the range, and 82 is outside it.

**The placebo confirms it.** The sign-flip placebo — the exchangeable
control for Rule 1, since its entry is a market fill — gives
p = 0.555 / 0.721 for the 82/18 crossing cells against
p = 0.295 / 0.435 for the canonical. No cell on either level set is
distinguishable from a coin flip. **Neither level set has directional
skill; the 82/18 set has a negative one.**

**The trigger semantics do not rescue it.** The level-test reading adds
at most 6 legs and +$9–$17 of net (it fires marginally more often
after a cooldown expires). The canonical 84/16 level test is the only
cell in the whole matrix that is *less* bad than its crossing twin on
the tp10 geometry (787 legs, −$316.10 vs 785 legs, −$334.91) — and
even that is still a 0.24× fee ratio.

**The retry threshold is not the driver.** The "82/18 with the retry
unchanged at 82/16" arm sits between the other two on every cell
(net −$788.07 / −$834.21 vs −$813.07 / −$868.48), and the spread is
$25–$34 on a ~$800 loss. The level set is what matters, not the
retry.

> **Verdict on 18/82: REJECTED. Keep the PDF's 16/84.**
>
> The wider band is a strictly worse version of an already-dead rule.
> It roughly doubles the loss at tp10/sl10 (−$334.91 → −$868.48),
> and it does so by turning a small POSITIVE gross into a NEGATIVE
> one. The mechanism is legible: **the RSI extreme's EDGE lives in how
> extreme it is.** The canonical band (84/16) selects only the most
> extreme readings, which is where the mean-reversion actually is;
> the wider band (82/18) reaches into the milder readings, which
> trend, and pays a round trip for each one.
>
> Rule 1's overall verdict is unchanged: **it is dead as specified**,
> and it is now dead by a wider margin under the variant.

### Rule 2 — H4 EMA limit orders

```
H4 chart, EMA (exponential moving average indicator, TP and SL $25
(2500 pips)
Strategy: BUY LIMIT/ SELL LIMIT (buy at lower / sell at higher
theory, wait for price to come and hit better price)
Blue and Red lines determine trend, if both line cross then INVALID.
If both lines showing trend (blue below red, DOWNTREND, vice versa),
then FOCUS YELLOW LINE, and put BUY LIMIT/ SELL LIMIT $13 (1300 pips
away)
```

**The EMA spec (given by the user, 2026-10-25):**

| line | period | source | shift | MA method |
|---|---|---|---|---|
| **blue** | 10 | **close** | 0 | exponential |
| **red** | 20 | **open** | 0 | exponential |
| **yellow** | 60 | **close** | 0 | exponential |

The red line being an EMA of the H4 **OPEN** is not a detail — it is a
different series from an EMA of the close, and it changes the
trend/cross logic materially. It is implemented as specified.

Two implementation details that matter:

* **the MT4 seed.** MT4/MT5 seeds an MA with the *simple* average of
  the first `n` values and then recurses `ema = x*alpha + ema_prev*(1-alpha)`.
  pandas' `ewm(span=n, adjust=False)` has the same recursion but
  seeds with `x[0]`. The difference decays as `(1-alpha)^k`, which for
  `n = 60` is still ~25% after 30 H4 bars — material on the first bars
  of a window. `enzo_core.ema()` implements the MT4 seed explicitly.
* **the H4 grid.** Bars close on the 00/04/08/12/16/20 UTC grid
  (canonical), with the session offset as a swept axis.

The order lifecycle, as implemented:

* at the **close of each H4 bar** the trend is read from the blue/red
  pair;
* a **cross** makes the setup INVALID: a resting order is **cancelled**
  and no new order is armed;
* otherwise, if there is no open position and no resting order, an
  order is armed:
  * DOWNTREND (blue below red) → **SELL LIMIT** at `yellow + 13`
  * UPTREND (blue above red) → **BUY LIMIT** at `yellow − 13`
* the armed order is a **resting limit**. It fills on the first 1m bar
  after the H4 close whose range touches the limit price; a gap
  through the limit fills at the bar's OPEN (pessimistic);
* from the fill, TP and SL are **$25** either side of the FILL price;
* the order **rests** — it is not re-priced at every H4 close.

---

## 2. The canonical cell

`blue=EMA(10,close)`, `red=EMA(20,open)`, `yellow=EMA(60,close)`,
offset $13, TP/SL $25, cross-cancels, order rests, UTC H4 grid.

| metric | value |
|---|---|
| legs | **124** |
| trades/calendar-day | 0.134 |
| gross | **+$436.47** |
| fees | −$71.21 |
| **net** | **+$365.25** |
| gross/leg | **$3.5199** |
| measured fee/leg | $0.5743 |
| **gross/leg ÷ fee/leg** | **6.13×** ✅ |
| hit rate | 56.5% |
| avg win / avg loss | $25.52 / $25.00 |
| payoff | 1.02× |
| break-even hit | 49.5% |
| **edge** | **+6.97pp** |
| long side | 52 legs, +$100.40 |
| short side | 72 legs, +$336.07 |
| exits | tp 70, sl 54 |
| median hold | 296 min |

**This is the only cell in either rule whose gross covers the round
trip** — by a factor of 6.13, against Rule 1's 0.18×.

---

## 3. ⚠️ The gross is MARKET DRIFT — the drift/excess identity

This is the finding that decides how Rule 2 should be read.

| component | value |
|---|---|
| gross | +$436.47 |
| **drift** (a passive hold of the same side over the same window) | **+$445.93** |
| **excess** (the strategy's own contribution) | **−$9.46** (t = −0.17) |
| drift as a share of gross | **102.2%** |

**The drift is 102.2% of the gross.** The realised P&L is the market's
drift, and the strategy's own contribution is slightly negative —
indistinguishable from zero.

The context that makes this concrete: **gold rose +96.7%** over the
corpus ($2,064.24 → $4,060.05).

| | |
|---|---|
| a passive LONG hold of 1 oz for the whole corpus | **+$1,995.81** |
| Rule 2's net | **+$365.25** on 124 trades |
| the monthly correlation between Rule 2's net and gold's monthly return | **−0.391** |

The −0.391 correlation is **not** a "no beta" reading — it is the
signature of a rule that is **short in a rising market** (its short
side is 72 of 124 legs) and therefore fights the drift at the monthly
scale while still capturing it at the leg scale.

So the honest description of Rule 2 is:

> **A levered, market-neutral-ish, low-frequency way of capturing part
> of gold's drift, at a much better risk profile than holding, and at a
> much worse return.** The trend call is not adding timing skill over
> a passive hold.

### The sign-flip placebo is NOT exchangeable here

The sign-flip placebo (randomise each leg's side, holding the entry
bar, entry price, exit bar and exit price fixed) gives a permutation
p of **0.061–0.086** — but it is measuring the wrong thing, for
exactly the reason AGENTS.md § 2b documents for the sniper recipe:

* a SELL LIMIT at `yellow + 13` fills when price **rises** to it;
* the sign-flipped "BUY LIMIT at the same price" is a **completely
  different order** with a different fill condition.

The placebo's null distribution is `sum(±g_i)` over random sign
assignments, whose sd is `sd(g)·sqrt(n) = $25.36 × 11.1 = $282`. The
observed gross of $436 is only **1.55 sd** above the null mean. So the
empirical p is ~0.06 — and a **paired t-statistic on the same data
gives t = 31.9**, which is an artefact (the "pairing" carries no
information: each arm is a different sign assignment on the same
legs, so the paired test has no valid null). The permutation p-value
is the honest one for that test, and the test itself is the wrong one.

### The control that IS exchangeable: the inverted call

`runtime/diagnostics/enzo_inverted_call.py` keeps **every** ingredient
and destroys exactly **one** — the **direction call**. The sign of
`(blue − red)` is flipped, so a downtrend BUYS at `yellow − 13` and an
uptrend SELLS at `yellow + 13`. Same EMAs, same yellow line, same
offset, same resting order, same fill model, same TP/SL, same fees,
same arming schedule.

| arm | n | gross | fees | net | gross/leg | × fee | hit | drift | excess |
|---|---|---|---|---|---|---|---|---|---|
| **real call** | 124 | **+$436.47** | $71.21 | **+$365.25** | **+$3.5199** | **6.13×** | 56.5% | +$445.93 | −$9.46 |
| **INVERTED call** | 1,078 | **−$1,255.32** | $642.20 | **−$1,897.52** | **−$1.1645** | −1.95× | 47.4% | −$1,649.06 | +$393.74 |

**The real call beats the inverted call by $4.6844/leg of gross and
$2,262.78 of net.** The trend call carries real information — it is
not decoration.

The inverted arm's *positive* excess (+$393.74) is the mirror image
of the real arm's negative one: the inverted arm is systematically
short a rising market, so its loss is drift, and what is left over is
positive. The two arms together say: **the call picks the side, and
the side picks the drift.** It does not say the call times anything.

---

## 4. Statistical power — the honest weakness

124 legs is a small sample.

| measure | value |
|---|---|
| months with a leg | 29 / 31 |
| months net > 0 | 16 (55.2%) |
| median month | +$17.11 |
| best month | +$94.44 (2026-04) |
| worst month | −$51.11 (2025-09) |
| **share of the net from the best 3 months** | **59.9%** |
| share of the net from the best 3 *trades* | 30.1% |
| net with the best 3 trades dropped | +$255.30 |
| **bootstrap 95% CI on the mean net/leg** | **[−$1.4908, +$7.3748]** |
| P(mean net/leg > 0) | **0.9034** |

**The bootstrap CI includes zero.** The point estimate is strongly
positive and the placebo arms are decisively beaten, but the CI does
not exclude zero. The result is *suggestive*, not established.

---

## 5. The ambiguities, and how each was settled

Every cell below is measured on the **same** continuous corpus with
the **same** schedule, so the comparisons are exchangeable.

### 5a. The order lifecycle

| cell | n | gross | net | gross/leg | × fee | armed | filled | survived a cross |
|---|---|---|---|---|---|---|---|---|
| **cross-cancels, order rests** (canonical) | 124 | **$436.47** | **$365.25** | **3.5199** | **6.13×** | 208 | 124 | 0 |
| cross-cancels, order re-priced | 164 | $345.03 | $249.57 | 2.1038 | 3.61× | 224 | 164 | 0 |
| order survives a cross, rests | **7** | $75.00 | $72.85 | 10.7143 | 34.93× | 8 | 7 | **173** |
| order survives a cross, re-priced | 154 | $159.19 | $70.36 | 1.0337 | 1.79× | 155 | 154 | 99 |

The **"survives a cross"** cell looks spectacular on its headline
numbers (34.93× the fee) and is **worth nothing**: it has **7 legs on
the whole corpus**. The reason is visible in the last column — 173
orders survive a cross, but with no cross-cancel the armed order is
never replaced, so the book effectively arms once and then stops. A
7-leg cell is not a result; it is a broken schedule. The canonical
cross-cancel reading is the one that trades.

The **re-price** cell is the interesting one: it produces 40% more
legs and a *lower* gross/leg, and its `excess` turns **positive**
(+$50.12 vs −$9.46) with a much lower drift share (**85.5%** vs
102.2%). The `cancel=0, reprice=1` cell has an even lower drift share
(**20.3%**) — it is the least drift-dependent cell in the table.

> **The trade-off is explicit: the canonical cell's edge is drift;
> re-pricing the limit trades some of that drift for a smaller, more
> timing-dependent book.** Whether that is better is a risk question,
> not an edge question, and it is a decision for the trader.

### 5b. The H4 session alignment

The broker's H4 bars need not close on the UTC grid, and the choice
moves the result:

| H4 offset | n | gross | net | gross/leg | × fee | hit | drift share |
|---|---|---|---|---|---|---|---|
| **0h (canonical, UTC grid)** | 124 | $436.47 | $365.25 | 3.5199 | 6.13× | 56.5% | 102.2% |
| 1h | 129 | $561.87 | $488.08 | 4.3556 | 7.61× | 58.1% | 105.5% |
| 2h | 116 | $386.97 | $320.42 | 3.3360 | 5.81× | 56.0% | 122.9% |
| 3h | 130 | $539.02 | $464.36 | 4.1463 | 7.22× | 57.7% | 102.8% |

All four are the same strategy on a different bar grid, and all four
are net-positive with a gross/leg above $3.3. **The result is not an
artefact of the alignment** — that is the useful reading. The spread
($320–$488) is the size of the alignment uncertainty.

### 5c. The EMA triple

The triple is the user's, so it is canonical. The grid around it
shows how much of the result depends on it:

| triple | n | gross | net | gross/leg | × fee | drift share |
|---|---|---|---|---|---|---|
| **10/20/60 (canonical)** | 124 | $436.47 | $365.25 | 3.5199 | 6.13× | 102.2% |
| 9/26/89 | 187 | $527.65 | $415.86 | 2.8217 | 4.72× | 108.1% |
| 12/26/100 | 195 | $47.46 | −$72.00 | 0.2434 | 0.40× | 19.4% |
| 13/34/100 | 180 | $222.19 | $112.03 | 1.2344 | 2.02× | 48.5% |
| 13/55/200 | 277 | −$580.04 | −$748.46 | −2.0940 | −3.44× | 96.5% |
| 20/50/100 | 153 | −$110.11 | −$204.58 | −0.7196 | −1.17× | 129.2% |
| 21/55/200 | 257 | −$880.04 | −$1,037.23 | −3.4243 | −5.60× | 102.5% |

**The triple is load-bearing.** Neighbouring triples lose money. This
is a genuine sensitivity: the user's 10/20/60 is in the winning
region, and the result is not a plateau.

### 5d. The offset and the pip arithmetic

The PDF says "$13 (1300 pips)" and "$25 (2500 pips)". For XAUUSD the
pip size is ambiguous — 1 pip = $0.01 (then 1300 pips = $13) or
1 pip = $0.10 (then 1300 pips = $130). **The dollar reading is
confirmed by measurement:**

| offset | tp/sl | n | gross | net | gross/leg | × fee |
|---|---|---|---|---|---|---|
| $13 | $25 | 124 | $436.47 | $365.25 | 3.5199 | 6.13× |
| $13 | $250 | 19 | −$503.98 | −$515.33 | −26.5253 | −44.41× |
| **$130** | $25 | **0** | — | — | — | — |
| **$130** | $250 | **0** | — | — | — | — |

**At a $130 offset nothing ever fills** (156 orders armed, 0 fills) —
the limit is farther away than the market travels between the arm and
the next arm. And a $250 TP/SL turns the cell sharply negative. So
the PDF's "$" figures are the authoritative ones.

The offset sweep at the canonical TP/SL:

| offset | n | gross | net | gross/leg | × fee | hit | drift share |
|---|---|---|---|---|---|---|---|
| $5 | 166 | $534.21 | $440.89 | 3.2181 | 5.72× | 56.0% | 90.7% |
| $8 | 149 | $410.84 | $327.53 | 2.7573 | 4.93× | 55.0% | 91.4% |
| **$13 (canonical)** | 124 | $436.47 | $365.25 | 3.5199 | 6.13× | 56.5% | 102.2% |
| $18 | 101 | $425.28 | $367.19 | 4.2107 | 7.32× | 57.4% | 94.9% |
| $25 | 72 | $298.07 | $255.15 | 4.1398 | 6.95× | 56.9% | 99.7% |

The offset is **not** a knife edge — every value from $5 to $25 is
net-positive with a gross/leg above $2.75. The canonical $13 is
comfortable inside the plateau, which is reassuring: the result does
not depend on hitting the PDF's number exactly.

---

## 6. The PDF's worked example — a partial reproduction

PDF p4:

> Base on this week 19/10 – 23/10 H4 chart, downtrend is confirmed
> base on EMA, a trade (SELL LIMIT) will be placed on WHITE ARROW area,
> and then it only triggered (yellow arrow) after more than 24 hours.
> The sell limit should be placed around price SELL LIMIT: 4143.50 and
> BUY/SELL LIMIT should only be placed $13 away from YELLOW LINE

**Reproduced:**

* the **direction** — the downtrend is confirmed by the EMA pair, and
  the cross to DOWNTREND happens on 2025-10-21 12:00 UTC (the blue EMA
  crosses below the red EMA);
* the **fill timing** — the arm→fill lag has a **p75 of 22.7 hours**
  and **25% of the fills take more than 24 hours** to trigger, which is
  exactly the PDF's description. The median lag is 2.4 hours.

**Not reproduced:**

* the **price anchor**. The PDF's SELL LIMIT was at **4143.50**, which
  means its yellow line was at **4130.50**. Under the canonical spec
  the yellow line in that week reads:

  | H4 close (UTC) | blue | red | yellow | limit |
  |---|---|---|---|---|
  | 2025-10-22 08:00 | 4158.08 | 4217.46 | 4155.38 | 4168.38 |
  | 2025-10-23 08:00 | 4111.81 | 4154.01 | 4143.10 | 4156.10 |
  | 2025-10-24 20:00 | 4107.28 | 4126.02 | 4134.16 | 4147.16 |

  The yellow line crosses **4130.50** on **2025-10-26 23:00** — three
  days after the PDF's window closes. Across all four H4 session
  alignments the best match to 4143.50 is **$3.66** away (the UTC
  grid, 2025-10-24 20:00). The PDF's own chart is a different
  rendering of the same week, and the difference is worth ~$3.66 of
  yellow-line level.

  **This is a one-anchor discrepancy and it is reported rather than
  tuned away.** It is the single most likely place where the PDF's
  author's broker, chart feed and "close/open" handling differ from
  this corpus.

### 6a. ⚠️ The anchor RESOLVED — by geometry, not by the chart pixels

Measured 2026-10-25. The chart-pixel route was tried first and
**failed on its own terms**; the corpus-side route succeeded. Both
are reported, because the failure is instructive.

#### The pixel route is not usable on this chart

The p4 chart was read directly (`enzo_emb_p4_0.png`, 1230×575). The
candle-body detector is *correct* — it finds **17** bodies, and the best
matching H4 window contains exactly 17 H4 bars — but **no window in
the corpus fits the chart's y→price map well.** Scanning **all 4,060**
H4 windows and fitting on the body→body pairs, the best is:

| window | R² | implied span | corpus span |
|---|---|---|---|
| 2024-10-04 08:00 → 2024-10-08 20:00 | **0.8048** | $30.92 | $48.40 |

> **Definitions, so the table is reproducible.** The detector is
> "≥6 columns carrying candle ink, in a run of ≥8 columns"; it yields
> **17** bodies. The *implied span* is `|slope| × (the pixel range of the
> body TOPS)`, i.e. the price the fitted line assigns across the chart's
> own body-top band. The *corpus span* is `max(max(o,c)) − min(min(o,c))`
> over the window (the **body-only** span — the high/low span is $65.49).

A chart's price axis is **linear**, so the right window must give
R² > 0.95. The best is **0.80**, with per-candle body-edge errors up to
**$13.28**. (The *worst* pairing — body top against the corpus LOW — puts
the error at **$39.31**, and that pairing also collapses R² to 0.05.)
**The chart is not an H4 rendering of this corpus** (different broker
feed / session alignment / bar construction), so the pixels cannot
resolve the anchor. *Reported, not tuned away.*

> **The body count is detector-threshold-sensitive, and the window
> moves with it.** Sweeping the detector over (ink-column threshold
> 3–8) × (min run width 4–10) gives 16–19 bodies, and R² moves between
> 0.760 and 0.827. The **18-body** variants (`thr≥4`/`thr≥5`) shift the
> best window to **2024-10-04 04:00** with R² **0.7927**, and the
> 19-body variants to **R² 0.7599**. **No variant reproduces the whole
> table at once** — a fact that strengthens the conclusion rather than
> weakening it: the pixel route does not merely fail to find a *good*
> window, it fails to find a *stable* one. The verdict (the pixels cannot
> resolve the anchor) is threshold-independent.

#### The corpus-side route: GEOMETRY pins the arm bar

A **SELL LIMIT rests above the market** — that is a hard geometric
constraint, independent of any EMA definition. On the PDF's H4 week
(the corpus, UTC grid):

| H4 bar (UTC) | close | can a SELL LIMIT at 4143.50 rest? |
|---|---|---|
| 2025-10-19 20:00 → 2025-10-21 08:00 | 4237.60 … 4253.15 | **no** — the market is above it |
| **2025-10-21 12:00** | 4138.39 | yes (first bar it can rest on) |
| 2025-10-22 04:00 | 4132.72 | yes |
| 2025-10-23 12:00 | 4141.56 | yes |

For each arm bar, the fill is the first later bar whose **high**
reaches 4143.50. **Exactly one** arm bar in the whole week yields a
fill **more than 24 h later** *and* inside the PDF's stated week
(21/10 → 23/10):

```
arm  2025-10-22 04:00 UTC   (close 4132.72)
fill 2025-10-23 12:00 UTC   (bar high 4154.44)
lag  28.0 h        <- the PDF's "more than 24 hours"
```

> **The PDF's "triggered after more than 24 hours" is REPRODUCED, and
> it is forced by geometry — no EMA period is involved.** The arm bar
> is pinned to 2025-10-22 04:00 UTC.
>
> **Across the four H4 session alignments the pin is the same moment.**
> The 1h-offset grid has its own single pinned arm bar, one hour later
> on the clock but the **same 4h bar of price action**:
>
> ```
> arm  2025-10-22 05:00 UTC (offset 1h)  (close 4069.89)
> fill 2025-10-23 13:00 UTC             (bar high 4154.44)
> lag  28.0 h
> ```
>
> The 2h- and 3h-offset grids pin **nothing** — no arm bar on those
> grids satisfies both the >24 h lag and the fill-inside-the-week
> constraint. So the geometry selects **one market moment** out of the
> week, and the session alignment moves only *which clock hour* names
> it.

#### The yellow line's period at the pinned bar

At that bar the PDF's yellow line must read **4130.50** (because
4143.50 − 13). Using the engine's own **MT4-seeded** EMA
(`enzo_core.ema`), across 5 price sources × 131 periods = 655
candidates **per pinned arm bar**, only **5** land within $0.50 of
4130.50:

| source | period | yellow | error | limit (yellow+13) |
|---|---|---|---|---|
| **hl2** | **72** | **4130.53** | **$0.03** | **4143.53** |
| ohlc4 | 73 | 4130.30 | $0.20 | 4143.30 |
| typical | 70 | 4130.24 | $0.26 | 4143.24 |
| close | 74 | 4130.18 | $0.32 | 4143.18 |
| close | 71 | 4130.11 | $0.39 | 4143.11 |

(`hl2/72`, `ohlc4/73` and `close/74` are on the **UTC** grid, at the
arm bar 2025-10-22 04:00; `typical/70` and `close/71` are on the
**1h-offset** grid, at its arm bar 2025-10-22 05:00. Both arm bars are
the same market moment.)

**The canonical yellow (`EMA(60, close)`) reads 4159.71 at that bar →
a limit of 4172.71, i.e. $29.21 away from the PDF's 4143.50.** So
the canonical triple **cannot** produce the PDF's example. The triple
is the user's, so this is **reported, not changed**.

#### ⚠️ But the LEVEL 4130.50 is not statistically special

The 5-of-1310 result looks decisive and is not. Repeating the whole
search for **control target levels** shows 4130.50 sits in the middle
of the null. (16 controls: 8 fixed offsets around the PDF's level, 8
random ones drawn from a fixed seed.)

| target level | coherent survivors |
|---|---|
| **4130.50 (the PDF's)** | **5** |
| 4130.00 | 7 |
| 4132.50 | 6 |
| 4129.50 | 5 |
| 4125.50 | 5 |
| 4131.50 | 4 |
| 4128.50 | 4 |
| 4131.00 | 3 |
| 4135.50 | 2 |
| 4132.195 (random) | 8 |
| 4126.318 (random) | 5 |
| 4127.102 (random) | 3 |
| … 16 controls … | mean **4.12**, max **8** |

**The PDF's level produces 5 survivors; the control mean is 4.12 and
the control maximum is 8.** The PDF's level is at the **62.5th
percentile** of its own controls — 10 of the 16 controls score *below*
it, and one random control (4132.195) scores *above* it.

> **A search that returns 5 hits out of 1,310 candidates has told you
> the base rate, not the answer.** (1,310 = 2 pinned arm bars × 5 price
> sources × 131 periods; the wider scan over the whole week and all four
> H4 offsets considers 66,810.) The anchor is *constrained* to the
> ~70–74 period family by the geometry, but the **level itself carries
> no identifying information** — it is what any level in that
> neighbourhood would produce.

#### The honest verdict on the anchor

| claim | status |
|---|---|
| the fill is >24 h later, inside the PDF's week | ✅ **reproduced**, forced by geometry |
| the arm bar is 2025-10-22 04:00 UTC | ✅ **pinned** by geometry |
| the yellow line reads 4130.50 there | ✅ **true for a ~70–74 period family** |
| the level 4130.50 identifies the period | ❌ **no** — it is at the 62.5th percentile of the null |
| the canonical `EMA(60, close)` reproduces the PDF's price | ❌ **no** — it is $29.21 away |

**So § 6's "one-anchor discrepancy" is now characterised rather than
open:** the discrepancy is **$29.21 of yellow-line level**, and it is
a **period** discrepancy (60 vs ~72), not a feed discrepancy — the
geometry reproduces the PDF's example exactly on this corpus at
2025-10-22 04:00 UTC.

**What this does NOT license.** Re-fitting the canonical yellow period
to 72 would make the PDF's example reproduce — and would be fitting a
single anchor from a search whose null says the level is not
identifying. **The canonical triple stays 10/20/60.** If the period is
ever revisited, the honest measurement is the § 5c grid (where
neighbouring triples lose money) and **not** this one anchor.

### 6b. The REPRICE vs REST lifecycle, re-tested at the anchor

The PDF's example distinguishes the two semantics, because the arm bar
and the fill bar are 28 h apart with **six H4 closes in between**:

| semantics | limit at the arm close | limit at the fill | PDF's 4143.50? |
|---|---|---|---|
| **REST** (one order, not re-priced) | 4145.64 | 4145.64 | off by $2.14 |
| **REPRICE** (limit re-priced every close) | 4143.53 | 4143.53 | **off by $0.03** ✅ |

**Under the geometry-pinned arm bar, the PDF's price is matched by
REPRICING, not by resting** — the opposite of the § 5a reading, which
was taken from the canonical triple (whose yellow line never reached
the PDF's level in that week, so the two semantics were
indistinguishable there).

This is a **conflict with the canonical lifecycle** (`H4_REPRICE =
False`), and it is reported, not applied. Note the § 5a grid is
measured on the canonical triple; on the ~72-period yellow the two
semantics have not been re-scored over the full corpus, so the trade-off
between "more legs / lower drift share" (§ 5a) and "matches the PDF"
has not been re-measured. **Do not change the canonical lifecycle on
the strength of one anchor** — the same null caveat applies.

---

## 7. Rule 1 and Rule 2 together

| book | n | gross | fees | net | gross/leg | × fee | hit |
|---|---|---|---|---|---|---|---|
| Rule 1 alone (tp10/sl10) | 785 | $78.53 | $413.44 | −$334.91 | $0.1000 | 0.19× | 51.1% |
| **Rule 2 alone** | 124 | $436.47 | $71.21 | **+$365.25** | $3.5199 | **6.13×** | 56.5% |
| Rule 1 + Rule 2 (union) | 909 | $515.00 | $907.07 | +$30.34 | $0.5666 | 1.06× | 51.8% |

**The union barely beats Rule 1's loss.** Rule 1's 785 fee-hungry
legs consume Rule 2's edge. Run Rule 2 alone.

---

## 8. Verdict

The driver's verdict now uses the **exchangeable** control for each rule:

```
OK  = the direction call beats its EXCHANGEABLE control
     Rule 2 (h4): the control is the INVERTED CALL — a SELL
       LIMIT at yellow+13 and a BUY LIMIT at the same price
       are DIFFERENT orders, so the sign-flip placebo is not
       exchangeable there and its p-value is not the verdict.
     Rule 1 (m1): the control is the sign-flip placebo
       (the entry is a market fill, so a sign flip is fair).
drift% = the drift's share of the gross. A value near or above
        100% means the realised P&L IS the market's drift.
```

| cell | verdict |
|---|---|
| Rule 1, tp5/sl5 | **NO DIRECTIONAL SKILL** (indistinguishable from a coin flip); gross does NOT cover the round trip |
| Rule 1, tp10/sl10 | **NO DIRECTIONAL SKILL** (indistinguishable from a coin flip); gross does NOT cover the round trip |
| **Rule 1, 18/82 variant (§ 1a)** | **REJECTED — WORSE.** The gross turns NEGATIVE (0.18× → −0.04× fee); net −$399.60 → −$813.07 (tp5) and −$334.91 → −$868.48 (tp10) |
| **Rule 2, 10/20/60** | **OK** — beats the inverted call; gross covers the round trip 6.13×; **the gross is 102% MARKET DRIFT** |

### The one-line version

> **Rule 2 works and Rule 1 does not. Rule 2's edge is in the TREND
> CALL (it beats the inverted call decisively, +$4.68/leg of gross),
> but its realised P&L is 102% market drift — gold rose 96.7% over the
> corpus — and the result is carried by 124 legs and 3 months, with a
> bootstrap CI that includes zero. Tradeable, not established.**

### What each rule would need to become a real edge

* **Rule 1** needs a **smaller fee bill or a bigger edge**. Its gross
  is $0.09–0.13/leg against a $0.51–0.53 round trip. It has a real
  signal (the RSI's `excess` is positive on the tp5 cell, t = +1.29)
  but the fee is 4–5× the gross. There is no knob that closes a 4×
  gap on the fee side; the signal itself would have to be 4× stronger
  or the trades 4× bigger. **Dead as specified.**
  **The level set is the one knob that moves the edge, and it moves it
  in the WRONG direction when loosened** (§ 1a): 16/84 → 18/82 turns
  the gross negative. The mechanism implies a TIGHTER band (e.g.
  86/14) is the direction to test if Rule 1 is ever revived — but
  that is an untested extrapolation, not a measurement, and it is not
  what was run here.
* **Rule 2** needs **more legs and a less drift-dependent schedule**.
  At 0.134 trades/day the book is too small to be robust, and at
  102% drift share it is a leveraged bet on gold's direction. The
  re-priced lifecycle cell (§ 5a) is the interesting one to develop:
  it trades 32% more legs and drops the drift share to 85.5%.

---

## 9. A note on what was NOT tuned

Two things were deliberately **not** fitted to the corpus:

1. **The EMA triple** (10/20/60) came from the user. The grid in § 5c
   shows the neighbouring triples lose money — so the result is
   sensitive to it and the triple is load-bearing. It was NOT chosen
   by picking the best cell from the grid, and the fact that the grid
   contains several losing neighbours is reported rather than hidden.
   **§ 6a strengthens this.** The PDF's own worked example points at a
   yellow period of ~**72** (geometry-pinned arm bar), not 60 — so the
   canonical 60 is *not* the PDF's period either. It was still not
   changed: the level 4130.50 sits at the **62.5th percentile** of its
   own null (§ 6a), so one anchor cannot identify a period, and the
   § 5c grid — not the anchor — is the honest measurement of a
   period's worth.
2. **The order lifecycle** (cross-cancels, order rests) came from the
   PDF's wording. The alternatives are in § 5a with their leg counts.
   **§ 6b notes a conflict** with the PDF's price anchor (which matches
   the re-priced reading), but a single anchor cannot overturn a
   full-corpus measurement, so the canonical lifecycle stands.
3. **Rule 1's RSI level set** (16/84) came from the PDF, and it is
   NOT the best cell of a searched grid. One alternative was measured
   — the user-requested **18/82** (§ 1a) — and it is **strictly
   worse** on every cell: the gross turns negative and the net
   roughly doubles. So the PDF's 16/84 is **vindicated by
   measurement, not merely inherited**: the one variant that was
   tested loses. A tighter band (86/14) was NOT tested and remains
   untested.

The place where a **measurement contradicted the PDF** is the
yellow-line anchor in § 6. It is now **characterised** (§ 6a): the
discrepancy is **$29.21 of yellow-line level** — a *period*
discrepancy (60 vs ~72), not a feed discrepancy, because the PDF's
"triggered after more than 24 hours" is reproduced **exactly** on this
corpus at the geometry-pinned arm bar 2025-10-22 04:00 UTC.

> **The rule that produced this:** a search over many candidates must
> be compared against its **own null** before any survivor is called a
> result. The 5-of-1310 period hit looked decisive; the 16 control
> levels showed it was the **62.5th percentile** of noise. The geometry
> finding survived because it was **forced by a constraint** (a sell
> limit rests above the market), not by a search.


---

## Reproducing

```bash
# the full-corpus scorecard for both rules (continuous, 14-day warmup)
python -m runtime.diagnostics.enzo_backtest \
  --start 2024-01-01 --end 2026-07-16 --ema 10,20,60 --n-random 500 --i-know

# the decisive control (the inverted call)
python -m runtime.diagnostics.enzo_inverted_call

# the Rule 1 level-set variant (16/84 vs 18/82), § 1a
python -m runtime.diagnostics.enzo_rsi_levels \
  --start 2024-01-01 --end 2026-07-16 --n-random 500 --i-know
```

> **The `enzo_rsi_levels.json` report is a CANONICAL artifact.** A
> run with a shorter window will be **REFUSED** rather than silently
> replacing the full-corpus measurement that § 1a cites — the same
> protection `leg_ledger` applies to the canonical ledger. To run a
> short window, write to your own path
> (`--out .../enzo_rsi_levels_variant.json`), or pass
> `--allow-variant-canonical` to assert that you are deliberately
> replacing the evidence record. Guard:
> `tests/test_enzo_rsi_levels.py`.

The engine is `runtime/diagnostics/enzo_core.py`; the driver is
`runtime/diagnostics/enzo_backtest.py`. Both follow the repo's rules:
continuous execution with a warmup prefix, pessimistic fills, the
canonical fee model (0.01 lots × 100 oz, $0.07 commission + $0.40
spread), the leg as the unit of evaluation, and the drift/excess
identity carried through every scorecard.

### The canonical spec, in one block

```python
H4_EMA_PERIODS     = (10, 20, 60)
H4_EMA_SOURCES     = ("close", "open", "close")   # blue, red, yellow
H4_SESSION_OFFSET_H = 0                           # bars close on the UTC grid
H4_LIMIT_OFFSET_USD = 13.0                        # "$13 away from YELLOW LINE"
H4_TP_USD = 25.0
H4_SL_USD = 25.0
H4_CROSS_CANCELS = True                           # "if both line cross then INVALID"
H4_REPRICE = False                                # the order rests
RSI_PERIOD = 14
RSI_SELL_LEVEL, RSI_BUY_LEVEL = 84.0, 16.0
RSI_SELL_RETRY_LEVEL, RSI_BUY_RETRY_LEVEL = 82.0, 16.0
COOLDOWN_MIN = 60
M1_EXIT_CELL_LIST = ({"tp_usd": 5.0, "sl_usd": 5.0},
                     {"tp_usd": 10.0, "sl_usd": 10.0})
```
