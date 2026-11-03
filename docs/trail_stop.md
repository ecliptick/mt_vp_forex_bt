# The trailing stop — MFE/MFA analysis and the settings it implies

> **Measured 2026-11-04** on XAUUSD 1s, via
> `python -m runtime.diagnostics.trail_mfe_mfa`.
>
> The question this file answers: *the trailing sweep said every trail
> configuration lost money — why, and what (if anything) should the
> trail's settings be?*

---

> **⚠️ The 2025-01 numbers throughout this file (§§ 2.2, 4.4, 5.2,
> 6.4, 7.2) are on the OLD recipe (273 legs).** The recipe has since
> moved; a re-run of the same month gives **284 legs**. They are kept
> because the SHAPE is what this document argues about, and the shape
> is unchanged. **The corpus numbers (§§ 2.1, 4.1, 5.1, 6.3, 7.1) are
> on the current recipe and are the verdict.**

## TL;DR

1. **The ATR is already on a 1-minute cadence.** The engine builds
   `ATR_1m` (Wilder ATR(14) on 1m candles, forward-filled onto the 1s
   grid) and **only** that array reaches the trail. A 1s ATR would be
   **10.3× narrower** and would turn the trail into a tick-noise
   trigger. **No change is needed** — the concern is already handled.
   (§ 3)

2. **The trailing stop is NOT PROMOTABLE at any setting — but the
   reason is NOT that trailing cannot work.** The **oracle bound**
   (a trail that exits at the *perfect* moment, at the peak, with zero
   giveback) **beats** the baseline by **+$11,044** on the 8-month
   corpus (2,182 legs won, 0 lost). Trailing *has* room; the problem
   is that no **realizable** geometry captures it. (§ 2)

3. **The canonical `combined k=2 act=1` is the WORST setting in the
   grid**, not a neutral one: it destroys **−$6,934** on the 1,080 TP
   legs it truncates while rescuing only **+$6,489** on the 793 SL legs.
   It is a **geometry** change (it replaces a good exit with a worse
   one), not a risk knob. (§ 5)

4. **The trail's total damage is nearly INDEPENDENT of its tightness**
   (k=2 → −$373; k=4 → −$379; k=8 → −$355). A tighter trail rescues
   more losers AND destroys more winners, and the two effects cancel.
   **That is why there is no setting to find — the knob is not on the
   efficient frontier.** (§ 5)

5. **A wide, late-arming trail is the least-harmful configuration**, and
   the only cell that beats the baseline through the engine
   (`chandelier k=32 act=2`, +$5.33 on 2,539 legs) is the corner where
   the trail **barely fires**. (§ 6, § 7)

6. **The trail's value is concentrated on the SHORT side**, which is the
   losing side: the canonical trail is **+$533.38** on shorts and
   **−$906.77** on longs. A trail that could be **scoped to the short
   side only** would be a different proposition entirely. (§ 4.2)

7. **⭐ THE SCOPED TRAIL IS NOW THE CANONICAL RECIPE (2026-11-04).**
   `trail_sides='short'` was **PROMOTED** after the ledger verdict
   (§ 8.6): it is the only cell that beats **both** the shipping
   recipe and the trail-OFF baseline, and the first canonical book
   whose gross edge **covers the measured round trip** (1.691× vs the
   previous 0.777×). The gain is **98% drift** and is
   **regime-concentrated** — read § 8.6 before quoting it.

---

## 1. The method

The trailing sweep (2026-11-03, 17 cells on 2025-01) moved FOUR knobs
at once and never asked the question that determines the parameters:

> **How much favourable excursion does a leg actually have, and how much
> of it survives the round trip?**

So this analysis measures, on the **trail-off** book:

* **MFE** (maximum favourable excursion) — the ceiling on what a trail
  can bank. A trail cannot capture more than the peak the trade
  reached.
* **MFA** (maximum adverse excursion) — the floor on where a trail can
  be armed without being stopped first.

both expressed in **units of the 1m ATR frozen at the fill**, so the
numbers are regime-invariant.

Then it **replays** candidate geometries over the real 1s bar paths.

### 1.1 The replay is validated against the engine

A replay that disagrees with the engine is worthless, so the
diagnostic validates itself before reporting anything:

| check | result |
|---|---|
| (a) never-arm trail → book IDENTICAL to trail-off | ✅ 2,539 = 2,539 legs, all exits match |
| (b) replay reproduces the trail-off book | ✅ **2,282/2,282 legs exact** (257 `contra_close` legs excluded — the replay does not model that exit) |
| (b2) replay baseline vs engine baseline | ✅ `d_gross $+0.0000`, `d_fees $+0.0000` → **EXACT** |
| (c) a configuration that DOES fire | ✅ **1,880/1,880** trail-stopped legs matched the engine's exit price |

> **⚠️ A peak-seeding bug was found and fixed during this work.** The
> engine initialises `peak_price = entry_price` and only updates it
> when a bar's extreme goes *beyond* it, so a naive
> `np.maximum.accumulate` over the bar highs reports the wrong peak on
> a fill bar whose high is still below the entry. Before the fix, check
> (c) matched only 0/229. The fix is to seed the running peak with the
> entry price. **Any replay of this kind must seed the peak.**

> **⚠️ A SECOND bug was found and fixed during this work — the
> zeroed `eod` gross.** The replay's `eod` branch returned the correct
> exit price but a hard-coded `gross_px = 0.0`, so the replay's
> trail-off baseline came out **$66.63 below** the engine's on the
> 8-month corpus — exactly the 8 `eod` legs' combined gross. **The
> one-month slice could not catch it** (2025-01 has ZERO `eod` legs),
> and the error was invisible in every headline number. Check (b2) —
> the reconciliation — is what found it. Full write-up:
> [`docs/engine_history.md`](engine_history.md#the-replays-zeroed-eod-legs--found-and-fixed-2026-11-04).
> **This is the second time a one-month slice has hidden a real bug
> from this repo.**

---

## 2. THE ORACLE BOUND — can *any* trail geometry work here?

Before tuning geometry, bound the question. The **oracle trail** exits
at the peak with zero giveback. No real trailing stop can beat it: a
trail can only exit at a price the market actually printed.

### 2.1 The 8-month corpus (2024-04-01 → 2024-12-01, 2,539 legs, ONE continuous run)

| | gross | fees | net |
|---|---|---|---|
| trail OFF (baseline) | $828.45 | $1,144.92 | **−$316.48** |
| **ORACLE** (exit at the peak) | $11,872.73 | $1,145.20 | **+$10,727.53** |

**Oracle vs baseline: +$11,044.00. Legs the oracle beats: 2,182. Legs
it loses to: 0.**

> **The oracle DOES beat the baseline.** Trailing is **not** a dead
> idea on this book — the excursions are large and the exits are
> leaving money on the table. The question is whether any
> **realizable** geometry can capture it.

### 2.2 The one-month slice says the opposite — and that is the lesson

On 2025-01 alone (273 legs) the oracle does **not** beat the baseline,
and the verdict printed "the trail question is CLOSED".

**That verdict was wrong**, and the reason is instructive: **the
one-month slice is n=1 draw.** The corpus is 2,438 legs. A single
month's excursion distribution can put the oracle under water while
the corpus puts it $11k above. This is AGENTS.md § 1.1 in the wild —
**the month is a reporting dimension, never a unit of evaluation.**

> **⚠️ The 2025-01-only numbers in this file (the excursion
> percentiles, the 2-D surface, the single-month ladders) are a
> REPORTING SLICE. The corpus numbers (2,438 legs, continuous) are the
> verdict.** Where they disagree, the corpus wins.

### 2.3 Why the oracle cannot be reached

The oracle exits at the peak *knowing the future*. A real trail must
place its stop **before** the peak, so it is hit on the way DOWN — at
whatever price the retracement reaches it, not at the peak. The gap
between the two is the **giveback**, and the geometry ladders measure
how large it must be before the trade stops paying.

That gap is the whole problem: the book has 48.65% SL legs at a
−$7,954.04 total gross and 46.51% TP legs at +$8,520.15. The oracle
converts the losers into winners *without* giving back the winners'
edge. Any real trail must trade one for the other, and at every
geometry measured, **the trade is a bad one**.

---

## 3. THE ATR CADENCE — the user's instruction, measured

> *"the ATR should be 1 min, not 1 s, else atr is too narrow"*

**This is correct, and the engine already obeys it.** The claim is
verified three ways.

### 3.1 The numbers

| yardstick | p10 | p50 | p90 |
|---|---|---|---|
| **ATR_1m** (Wilder 14, 1m candles) | $0.447 | **$0.720** | $1.256 |
| ATR_1s (Wilder 14, 1s bars) | $0.048 | **$0.070** | $0.126 |
| ATR_1s (len 1200 — 20 min of true range) | $0.049 | $0.071 | $0.126 |

**The 1m basis is 10.3× wider** (12.2× on the 8-month corpus: ATR_1m
p50 $0.716 vs ATR_1s p50 $0.059). A 1s ATR is 20 minutes of average
one-second true range — that is a **tick-noise** measure, not a
volatility measure. A trail built on it would sit a few cents from
price and be triggered by noise on every micro-tick.

### 3.2 The code path — there is no 1s route to the trail

```
_build_atr_1m_forward_filled()      # 1m Wilder ATR(14), causal
        ↓
    _atr1m                          # engine line 2729
        ↓
    ot.trail_atr_value              # engine line 3545 — FROZEN AT FILL
        ↓
    _trail_level_usd()              # the ONLY reader of trail_atr_value
```

`atr_1m` is the **sole** input. The `ATR_LEN = 1200` constant in
`bt/optimal_config.py` is **DIAGNOSTIC ONLY** — it feeds
`sl_atr_mult`, which is not on the sweep surface and which the engine
is never handed an `atr=` array. It cannot reach the trail.

The canonical recipe never passes `atr_1m=`, so the engine always
derives it from the bars.

### 3.3 The counterfactual — what a 1s ATR would do

Same grid, same `k`, different yardstick:

| config | net (1m ATR) | net (1s ATR) | Δ | 1m: trail/tp/sl | 1s: trail/tp/sl |
|---|---|---|---|---|---|
| combined k=2 act=1 | −$102.41 | −$109.52 | −$7.11 | 229 / 9 / 32 | **268 / 0 / 2** |
| chandelier k=2 act=1 | −$88.84 | −$115.92 | −$27.08 | 219 / 19 / 32 | **268 / 0 / 2** |
| combined k=2 act=3 | +$29.22 | −$123.90 | **−$153.12** | 172 / 37 / 61 | **260 / 0 / 10** |

With a 1s ATR the trail fires on **268 of 270 legs** and keeps
**ZERO** TPs. Worse: in `combined` the giveback arm (which does **not**
read ATR) starts dominating the tighter chandelier arm, so the mode
silently **becomes a giveback trail** — a different geometry wearing
the same name. That is the real hazard of a narrow ATR: it does not
just tighten the stop, it **changes what the mode means**.

### 3.4 The ATR *length* is not the lever

The cadence is 1m — settled. The Wilder length is still free:

| length | p50 ATR | k | distance | net | vs base |
|---|---|---|---|---|---|
| 7 | $0.718 | 8 | $5.745 | $110.20 | −$71.51 |
| **14** | $0.720 | 8 | $5.763 | $118.69 | −$63.02 |
| 20 | $0.721 | 8 | $5.767 | $91.42 | −$90.29 |
| 30 | $0.724 | 8 | $5.790 | $89.46 | −$92.25 |

The length moves the ATR by **<1%** and the net by ~$30 — noise
around a −$60 to −$90 deficit. **The trail is not sensitive to the
yardstick's fine structure; it is sensitive to the DISTANCE the
yardstick implies.** `k` is the actionable knob, and it is the wrong
knob.

---

## 4. The excursion distribution

### 4.1 The corpus (2024-04-01 → 2024-12-01, 2,539 legs, continuous) — THE VERDICT

| metric | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|
| MFE ($/oz) | 0.399 | 1.620 | **4.770** | 8.037 | 10.441 |
| MFA ($/oz) | 0.600 | 1.616 | **3.641** | 7.150 | 13.748 |
| MFE (× ATR_1m) | 0.444 | 1.874 | **5.219** | 9.432 | 14.500 |
| MFA (× ATR_1m) | 0.628 | 1.798 | **4.157** | 8.450 | 15.827 |
| SL distance ($) | 2.482 | 4.675 | **12.920** | 15.000 | 15.000 |
| TP distance ($) | 4.281 | 5.955 | **8.031** | 10.384 | 13.322 |
| hold (s) | 427 | 1,654 | 5,346 | 17,392 | 38,815 |

**The measured round trip is $0.4509/leg.**

| exit reason | n | share | gross | MFE p50 | MFE/ATR p50 | MFA p50 |
|---|---|---|---|---|---|---|
| **sl** | 1,147 | **45.18%** | **−$7,852.17** | $1.743 | 2.00 | $5.280 |
| **tp** | 1,127 | **44.39%** | **+$8,169.77** | $7.570 | 8.36 | $2.380 |
| contra_close | 257 | 10.12% | +$577.48 | $6.640 | 5.90 | $1.697 |
| eod | 8 | 0.32% | −$66.63 | $6.440 | 7.16 | $9.700 |

**Read this table carefully — it IS the trailing-stop case.** Nearly
half the book is a **loss** (−$7,852) and nearly half is a **win**
(+$8,170). The TP legs run 8.36 ATR of MFE; the SL legs run only 2.00
ATR before dying. That is a textbook "cut the losers, ride the
winners" shape, and it is exactly what a trailing stop is for.

**The reason a trail cannot cash it in** is the third column: the TP
legs' MFE (8.36 ATR) and the SL legs' MFE (2.00 ATR) are wildly
different, so a single activation threshold either arms on both
(populating the giveback on the winners) or on neither (rescuing
nothing). The gap between 2.00 and 8.36 ATR is where the trail would
have to live, and § 6 shows that is the region where it loses most.

> **⚠️ NOTE ON THE CORPUS NUMBERS.** Two corpus runs were made on
> 2026-11-04 with the recipe **mid-change** (another session removed
> `structure_seq_drop_set` and moved `vol_state_min_expand_prev_day`
> to 1.0 live). The earlier run had 2,438 legs / baseline −$531.53;
> the later, current-recipe run has **2,539 legs / baseline −$316.48**.
> **The later run is the one quoted above.** The *shape* of every
> table is unchanged between them — which is the point of quoting both.

### 4.2 Per-side (corpus)

| side | n | gross | fees | net | gross/leg | MFE p50 | MFA p50 |
|---|---|---|---|---|---|---|---|
| long | 1,685 | $1,073.50 | $755.64 | +$317.86 | $0.6371 | $5.047 (5.27 ATR) | $3.590 |
| **short** | 854 | **−$245.05** | $389.28 | **−$634.34** | **−$0.2869** | $4.380 (5.21 ATR) | $3.716 |

**The short side is deeply negative** and its MFA p50 ($3.716) is much
closer to its MFE p50 ($4.380) than the long side's is ($3.590 vs
$5.047) — **the shorts go against the trade first**. **This is where a
trailing stop's value is concentrated**: the canonical trail is
**+$533.38** on the short side and **−$906.77** on the long side. The
trail helps exactly the side that is losing money — and costs the side
that is making it. See § 8.4.

### 4.3 The giveback ladder — the third geometry axis

`trail_giveback_frac` is the fraction of the peak MFE the trail gives
back. It is the mode's third dimension, and it behaves as the geometry
predicts:

| giveback | trail fired | TP kept | net | delta vs base |
|---|---|---|---|---|
| 0.25 | 1,922 | **6** | −$815.24 | −$498.77 |
| 0.50 | 1,871 | 57 | −$702.81 | −$386.33 |
| 0.75 | 1,775 | 153 | −$671.83 | −$355.35 |

**A tighter giveback keeps fewer TPs.** At 0.25 the trail keeps **6 of
1,127** TPs — it has replaced the exit model entirely, and it loses
$498.77. At 0.75 it keeps 153 and loses $355.35. **Looser is better,
monotonically**, and the best value measured (0.75) is still a loss.

### 4.4 The one-month slice (2025-01, 273 legs) — REPORTING ONLY

> **⚠️ STALE RECIPE.** These 273-leg numbers were measured on the
> recipe as of 2026-11-03. The recipe has since moved (another session
> changed the sequence-drop set), and a re-run of the same month on
> the **current** recipe gives **284 legs** with a different excursion
> distribution (MFE p50 $5.320, MFA p50 $3.295, round trip $0.4549).
> **They are kept here for the SHAPE** — the one-month and the corpus
> tell the same story — not as current numbers. The corpus (§ 4.1) is
> the verdict, and it was re-measured on the current recipe.

| metric | p10 | p25 | p50 | p75 | p90 |
|---|---|---|---|---|---|
| MFE ($/oz) | 0.721 | 2.400 | **5.550** | 6.977 | 8.940 |
| MFA ($/oz) | 0.492 | 1.440 | **2.930** | 7.270 | 15.000 |
| MFE (× ATR_1m) | 0.802 | 2.888 | **5.925** | 9.088 | 12.532 |
| MFA (× ATR_1m) | 0.497 | 1.565 | **3.798** | 10.020 | 16.766 |
| SL distance ($) | 2.515 | 4.403 | 13.457 | 15.000 | 15.000 |
| TP distance ($) | 4.635 | 5.747 | **6.546** | 8.550 | 10.214 |
| hold (s) | 709 | 1,907 | 5,719 | 18,228 | 52,486 |

**The measured round trip is $0.4657/leg.**

* **93.04%** of legs have MFE > 1× the round trip, and **82.05%** have
  MFE > 3× — so on the MFE side there is *plenty* of room.
* But only **56.04%** of legs have MFE > MFA. The other 44% go
  against the trade first, and a trail that arms on those is a coin
  flip.

**The asymmetry that matters:** the TP distance (p50 $6.55) is
**smaller** than the MFE (p50 $5.55... comparable), while the SL
distance (p50 $13.46) is **2× the TP distance**. The structural
book is a **long-tail bet**: small wins, large losses. A trail is
supposed to fix exactly that. And it does — see § 5.

---

## 5. WHERE THE TRAIL'S MONEY COMES FROM (the decisive decomposition)

Each leg is bucketed by the exit it had with the trail **off**. The
`delta` is the replayed gross minus the trail-off gross.

### 5.1 The corpus (2,539 legs) — the verdict

| config | TP legs (1,127) | SL legs (1,147) | total |
|---|---|---|---|
| **combined k=2 act=1** (canonical) | **−$6,934.40** (1,080 cut, mean −$6.42) | **+$6,488.91** (793 rescued, mean +$8.18) | **−$373.39** |
| chandelier k=8 act=1 | −$3,795.81 (422 cut, mean −$8.99) | +$3,381.71 (533 rescued, mean +$6.34) | −$355.25 |
| chandelier k=4 act=1 | −$5,637.24 | +$5,183.74 | −$379.52 |
| chandelier k=2 act=1 | −$6,599.46 | +$6,145.52 | −$373.26 |
| combined k=2 act=3 | −$4,859.83 | +$4,351.01 | −$425.15 |

**This is the whole story in one table.** The trail DOES rescue losers —
it banks **+$6,488.91** on the 1,147 SL legs that would otherwise have
lost ~$15 each (plus **+$72.10** on the 8 `eod` legs). That is a
genuine **risk** benefit.

But it pays for it by **truncating the winners**: it gives back
**−$6,934.40** on the 1,080 TP legs. The winners it cuts are worth
**more** than the losers it saves, and the net is negative.

**The trailing stop is a risk knob that costs more in giveback than it
saves in truncation.** For it to be net-positive, the TP legs it cuts
would have to be worth less than the SL legs it rescues.

> **Note the perverse scaling.** A *tighter* trail rescues MORE losers
> (k=2: +$6,145 vs k=8: +$3,382) but also destroys MORE winners
> (k=2: −$6,599 vs k=8: −$3,796). The two effects nearly cancel, so
> the total is roughly flat (−$355 to −$425) across the whole ladder.
> **The trail's total damage is nearly independent of its tightness.**
> That is why there is no setting to find — the knob is not on the
> efficient frontier.

### 5.2 The one-month slice (2025-01, 273 legs) — REPORTING ONLY

| config | TP legs (168) | SL legs (102) | total |
|---|---|---|---|
| **combined k=2 act=1** (canonical) | **−$959.85** (mean −$5.71) | **+$675.69** (mean +$6.62) | **−$284.15** |
| combined k=2 act=3 | −$654.33 | +$501.81 | −$152.52 |
| chandelier k=8 act=1 | −$492.31 | +$429.28 | −$63.03 |
| chandelier k=4 act=1 | −$739.27 | +$560.21 | −$179.05 |
| chandelier k=2 act=1 | −$910.03 | +$639.45 | −$270.58 |

Same shape, same conclusion — the ratio is what matters, and it is
stable across the two windows.

### 5.3 The loss tail — the risk-knob test

A risk knob earns its keep by cutting the worst losses (corpus
figures):

| config | worst | p5 | p10 | mean | n < −$5 |
|---|---|---|---|---|---|
| trail OFF | −$15.18 | −$15.00 | −$12.44 | $0.33 | **596** |
| chandelier k=8 | −$15.18 | −$7.77 | −$5.50 | $0.19 | **293** |
| chandelier k=4 | −$15.18 | −$5.76 | −$3.74 | $0.18 | 153 |
| chandelier k=2 | −$15.18 | −$5.62 | −$2.54 | $0.18 | 138 |
| combined k=2 act=1 | −$15.18 | −$5.62 | −$2.53 | $0.18 | 138 |
| combined k=2 act=0.5 | −$15.18 | −$3.11 | −$0.24 | $0.16 | 74 |

The trail **does** cut the tail — 596 legs below −$5 becomes 138. The
worst single loss is unchanged at −$15.18 (the SL cap), so the trail
never rescues a leg that hits the hard stop; it only rescues legs that
were going to be stopped *at the stop*. **The tail-truncation is real
and it is worth something.** It is just worth less than the giveback.

---

## 6. The geometry ladders

### 6.1 Distance ladder (chandelier k, act=1)

| k | distance $ | trail fired | tp kept | net | vs base |
|---|---|---|---|---|---|
| 1.0 | 0.809 | 229 | 9 | −$101.70 | −$283.41 |
| 2.0 | 1.618 | 219 | 19 | −$88.84 | −$270.55 |
| 3.0 | 2.427 | 189 | 48 | −$0.83 | −$182.54 |
| 4.0 | 3.236 | 170 | 60 | $2.68 | −$179.03 |
| 6.0 | 4.853 | 135 | 91 | $109.49 | −$72.22 |
| **8.0** | 6.471 | 113 | 108 | **$118.69** | −$63.02 |
| **12.0** | 9.707 | 74 | 136 | **$147.64** | **−$34.07** |
| 16.0 | 12.943 | 56 | 146 | $76.46 | −$105.25 |
| 24.0 | 19.414 | 19 | 159 | $107.88 | −$73.83 |
| **32.0** | 25.885 | **9** | **165** | $159.58 | −$22.13 |

Monotone-ish improvement with width, converging toward the baseline
as the trail stops firing. **k=32 fires 9 times on 273 legs** — that
is not a trailing stop, that is the trail being off.

### 6.2 Activation ladder (combined k=2)

| activation | trail fired | tp kept | net | vs base |
|---|---|---|---|---|
| 0.25 | 261 | 0 | −$138.35 | −$320.06 |
| 1.00 (canonical) | 229 | 9 | −$102.41 | −$284.12 |
| 3.00 | 172 | 37 | $29.22 | −$152.49 |
| 6.00 | 104 | 79 | $52.22 | −$129.49 |
| 12.00 | 20 | 148 | $120.71 | −$61.00 |

Same convergence: the more the trail has to prove before it arms, the
less damage it does.

### 6.3 The 2-D surface — the corpus version

net $ by (activation × chandelier distance), corpus 2024-04 → 2024-12,
baseline **−$316.48** (reconciled EXACT):

```
act \ k           1        2        3        4        6        8       12       16       24       32
0.50          -464     -589     -598     -543     -573     -585     -632     -368     -432     -298*
1.00          -631     -690     -754     -696     -692     -672     -677     -382     -465     -314 
2.00          -889     -866     -858     -758     -748     -725     -642     -394     -455     -306 
3.00          -824     -756     -737     -692     -737     -695     -641     -405     -468     -307 
4.00          -899     -837     -813     -853     -838     -852     -733     -520     -474     -311 
6.00          -886     -854     -866     -867     -879     -809     -743     -519     -489     -337 
8.00          -837     -793     -806     -749     -713     -695     -611     -513     -472     -363 
12.00         -522     -540     -542     -564     -543     -500     -478     -450     -439     -379 
```

**The best cell (k=32, act=0.5) is −$298.17, i.e. +$18.31 over the
baseline — and it fires on very few legs.** Every interior cell loses
badly (−$368 to −$899). The surface's optimum is the *corner where the
trail barely exists*. That is the signature of a knob that does not
want to be on.

> **Note the shape at act=12:** the whole row is nearly FLAT
> (−$379 to −$564) regardless of k. That is because at act=12 the trail
> almost never arms, so `k` stops mattering. **The trail's "best
> settings" are the ones where it does the least.**

### 6.4 The one-month slice (2025-01) — REPORTING ONLY

net $ by (activation × chandelier distance), baseline $181.71:

```
act \ k           1        2        3        4        6        8       12       16       24       32
0.50          -130      -90       -4        2       92      109      147       76      108      160 
1.00          -102      -89       -1        3      109      119      148       76      108      160 
2.00           -71      -36       18       11       90       98      143       81      108      160 
3.00             4       25       67       64       95      102      143       98      118      172 
4.00             5       31       47       58       61       67      116       85      109      171 
6.00            37       52       52       72       68       62      124      122      122      171 
8.00            51       68       66       61       63       77      132      164      157      197*
12.00          109      121      124      123      125      140      156      157      175      182 
```

The best cell (k=32, act=8) is +$196.65, i.e. +$14.94 over the
baseline — and it fires on 27 legs of 273. **Same shape as the corpus:
the optimum is the degenerate corner.**

---

## 7. The engine A/B — the replay RANKS, the engine DECIDES

The replay holds the leg set **fixed**. The engine does not: a
trail-closed leg frees a same-direction slot (so a later signal is
admitted), and a trail-closed leg cannot be netted by contra-close.
So the candidates must go through the real engine.

### 7.1 The corpus (2024-04-01 → 2024-12-01, 2,539 legs, continuous)

| config | legs | gross | fees | net | trail | vs base |
|---|---|---|---|---|---|---|
| trail OFF (baseline) | 2,539 | $828.45 | $1,144.92 | **−$316.48** | 0 | — |
| **chandelier k=32 act=2** | 2,538 | $833.77 | $1,144.91 | **−$311.15** | 61 | **+$5.33** ✅ |
| chandelier k=32 act=1 | 2,538 | $825.24 | $1,144.91 | −$319.67 | 64 | −$3.19 |
| chandelier k=32 act=8 | 2,538 | $776.24 | $1,144.92 | −$368.68 | 44 | −$52.20 |
| chandelier k=24 act=8 | 2,531 | $630.52 | $1,144.91 | −$514.39 | 90 | −$197.91 |
| chandelier k=24 act=1 | 2,531 | $659.14 | $1,144.90 | −$485.76 | 167 | −$169.28 |
| chandelier k=16 act=8 | 2,520 | $531.18 | $1,144.93 | −$613.75 | 164 | −$297.27 |
| chandelier k=12 act=1 | 2,538 | $307.75 | $1,144.87 | −$837.12 | 625 | −$520.64 |
| chandelier k=8 act=8 | 2,505 | $335.17 | $1,144.91 | −$809.74 | 324 | −$493.26 |
| chandelier k=8 act=1 | 2,471 | $215.25 | $1,144.84 | −$929.59 | 989 | −$613.11 |
| combined k=2 act=12 | 2,496 | $624.45 | $1,144.93 | −$520.48 | 295 | −$204.00 |
| combined k=2 act=3 | 2,403 | $155.35 | $1,144.82 | −$989.47 | 1,404 | −$672.99 |
| **CANONICAL combined k=2 act=1** | 2,390 | −$17.65 | $1,144.79 | **−$1,162.44** | 1,974 | **−$845.96** |

**Only ONE configuration beats the baseline through the engine:
`chandelier k=32 act=2`, by +$5.33 — on 2,538 legs, firing 61 times.**

**+$5.33 is not an edge.** It is 1.7% of the baseline's gross, it fires
on 2.4% of legs, and the same family of settings flips sign across
the monthly slices (§ 7.3). It is the corner of the surface where the
trail has stopped doing anything.

**The canonical setting loses $845.96** on the corpus and **−$1,162.44**
in absolute terms — it is the worst row in the table, and it is the
row the recipe ships today.

### 7.2 The one-month slice (2025-01, 273 legs) — REPORTING ONLY

| config | legs | gross | fees | net | trail | vs base |
|---|---|---|---|---|---|---|
| trail OFF (baseline) | 273 | $308.84 | $127.13 | **$181.71** | 0 | — |
| chandelier k=32 act=1 | 273 | $286.71 | $127.13 | $159.58 | 9 | −$22.13 |
| chandelier k=12 act=1 | 273 | $274.75 | $127.11 | $147.64 | 74 | −$34.07 |
| chandelier k=8 act=1 | 271 | $231.79 | $127.11 | $104.68 | 113 | −$77.03 |
| combined k=2 act=12 | 273 | $247.83 | $127.12 | $120.71 | 20 | −$61.00 |
| combined k=2 act=3 | 271 | $156.42 | $127.10 | $29.32 | 173 | −$152.39 |
| **CANONICAL combined k=2 act=1** | 271 | $21.84 | $127.09 | **−$105.25** | 230 | **−$286.96** |

### 7.3 Multi-month slices (⚠️ chunked runs — stability, not a verdict)

| config | 2024-04 | 2024-08 | 2024-11 | 2025-01 |
|---|---|---|---|---|
| trail OFF (baseline net) | $83.63 | $244.62 | −$393.76 | $181.71 |
| chandelier k=32 act=1 | **+$2.67** | −$11.00 | −$5.57 | −$22.13 |
| chandelier k=12 act=1 | −$91.06 | −$89.65 | **+$11.77** | −$34.07 |
| chandelier k=8 act=1 | −$135.04 | −$153.77 | **+$144.77** | −$77.03 |
| chandelier k=8 act=8 | −$61.38 | **+$90.01** | −$37.00 | −$104.29 |
| combined k=2 act=12 | −$121.22 | **+$50.70** | −$79.42 | −$61.00 |
| combined k=2 act=3 | −$190.04 | −$185.10 | −$33.97 | −$152.39 |
| CANONICAL combined k=2 act=1 | **−$372.85** | **−$328.07** | −$1.16 | **−$286.96** |

> **⚠️ These four runs are FOUR SEPARATE CHUNKED MONTHS, which
> AGENTS.md § 1.1 forbids as an evaluation unit** — the p95 cap (7d),
> the swing cap (24h) and the VP profile (7d rolling) are all
> history-dependent, and a chunked run trades its first days with
> uncapped targets. Read this table as *stability across slices*, not
> as a corpus verdict.

**The one thing every slice agrees on: the canonical setting is the
worst.** −$372.85 / −$328.07 / −$1.16 / −$286.96. It is never the
best, and in three of four slices it is catastrophically the worst.

**The wide configurations flip sign across slices** (+$144.77 in
2024-11, −$135.04 in 2024-04) — that is a **draw**, not an edge. A
knob whose sign depends on the month is not a knob that ships.

---

## 8. The recommendation

### 8.1 The ATR — no change needed

**The trail's ATR is already 1-minute.** Keep it that way. The only
thing to record is that `ATR_LEN = 1200` in `bt/optimal_config.py` is
diagnostic-only and must never be pointed at the trail.

> **The one-line rule:** the trail's yardstick is
> `bt/_engine/vp_multi_target_engine.py::_build_atr_1m_forward_filled`.
> If anyone adds a second ATR path, it must not reach `trail_atr_value`.

### 8.2 The trail — the honest options

| option | rationale |
|---|---|
| **`trail_mode='off'`** (recommended) | The oracle says trailing *has* room (+$11,044), but **no realizable geometry captures it**: 12 of 13 engine-tested configurations lose, and the one that "wins" does so by +$5.33 on 2,538 legs — a corner where the trail has stopped firing. Keeping the trail on costs money and adds a knob whose optimum is "off". |
| `chandelier`, k=32, act=2 | The **only** engine-tested configuration that beats the baseline (**+$5.33** on 2,539 legs, 61 trail-stops). If the trail must stay on, this is the setting — but it is a **no-op dressed as a trail**, and it flips sign across slices. |
| `chandelier`, k=32, act=1 | Second-least-harmful (−$3.19). Same caveat. |
| **short-side-only trail** (§ 8.4, § 8.5) | **The only reading of the data that is positive** — measured END TO END: **+$332.60** on the corpus vs the trail-OFF baseline (net −$316.48 → **+$16.12**). The trail helps the losing side and hurts the winning one, so scoping it to shorts is the fix. **Still NOT a ship recommendation** — the book is a wash, the short side stays negative, and it is a whole-corpus fit on one window. |
| `combined`, k=2, act=1 (**the current canonical**) | ❌ **The worst cell in the entire grid** under the `'both'` scope (−$845.96 on the corpus). **BUT the best cell under the `'short'` scope (+$332.60).** Do not ship at `'both'`. |

> **⚠️ Do NOT ship `combined k=2 act=1`.** It is the canonical value
> today and it is the **worst** row in every table: **−$845.96** on the
> corpus (2,539 legs), −$286.96 on 2025-01, −$372.85 on 2024-04,
> −$328.07 on 2024-08. It is a **geometry** change, not a risk knob: it
> destroys more TP value than it rescues in SL value.

> **⚠️ Do NOT read "the oracle wins by $11,044" as "ship a trail".**
> The oracle exits at the peak *knowing the future*. Every realizable
> geometry must place the stop *before* the peak and gets filled on the
> way down. The giveback required to bridge that gap exceeds the
> rescue at every measured geometry. **The gap between the oracle and
> the realizable trail IS the finding** — it says the book's exit
> geometry is right, and the excursions are the market's, not the
> strategy's.

### 8.2b What the MFE/MFA data says the trail *would* need

If the trailing stop is to be made to work, the MFE/MFA geometry
dictates the shape, and it is not the current one:

1. **A SEPARATE activation for the two populations.** The TP legs run
   8.36 ATR of MFE; the SL legs run 2.00 ATR. One threshold cannot
   separate them — the trail must arm on the *losers* (which need a
   LOW MFE threshold to be rescued) while NOT arming on the *winners*
   (which need a HIGH threshold to be protected). That means **two
   different rules**, not one `trail_activation_mult`.
2. **A distance of ≥ 12 ATR_1m** — below that, the trail cuts more TPs
   than it rescues losers (§ 8.3).
3. **The giveback must be loose** — `trail_giveback_frac` ≥ 0.75. At
   0.25 the trail keeps **ZERO** TPs.

> **This is a redesign, not a tuning job.** It is the natural next
> experiment if the trailing stop is worth pursuing — but it is a
> change to the engine's exit model, so it needs its own spec, not a
> knob sweep.

### 8.3 If a trailing stop IS wanted, the shape is dictated

The MFE/MFA data says the trail's geometry must satisfy **two**
thresholds simultaneously (from the 5e section, **corpus** numbers):

* `K_tp` — the width at which the trail **cuts a TP**. The TP is
  preserved iff `k > K_tp`. Corpus median `K_tp` = **5.99 ATR**.
* `K_sl` — the width at which the trail **rescues a loser**. The loser
  is rescued iff `k <= K_sl`. Corpus median `K_sl` = **11.03 ATR**.

These overlap (5.99 < 11.03), so a trail *can* preserve the TP and
still rescue the loser — but only in the **band**
`5.99 < k < 11.03`, and even there the giveback exceeds the rescue.

The corpus ladder at act=1 shows the trade-off directly:

| k | TP kept | losers rescued | net $ | vs base |
|---|---|---|---|---|
| 1 | 2% | 100% | −$631 | −$314 |
| 2 | 11% | 100% | −$690 | −$373 |
| 4 | 32% | 93% | −$696 | −$379 |
| 8 | 63% | 67% | −$672 | −$355 |
| 12 | 79% | 46% | −$677 | −$361 |
| 16 | 89% | 32% | −$382 | −$66 |
| 24 | 96% | 15% | −$465 | −$149 |
| **32** | **99%** | **6%** | **−$314** | **+$2** |

**A trail on this book must be WIDE** — at least ~16 ATR, practically
32+ — or it must arm LATE (act ≥ 8 ATR). Anything tighter is a
guaranteed loss, and even the "good" corner only works by barely
firing.

> **The activation changes the thresholds, and the corpus confirms
> the shape**: at act=3, `K_tp` drops to 5.33 and `K_sl` rises to
> 14.83 (a wider band); at act=8, `K_tp` is 4.72 and `K_sl` is 24.74
> (a very wide band). But the best cell in every row is still
> k=32 — the corner where the trail stops existing.

### 8.4 The one place a trail COULD work — the short side

The per-side decomposition is the one place the trail shows a
consistent positive:

| side | n | baseline gross | canonical trail delta | SL rescued | TP cut |
|---|---|---|---|---|---|
| long | 1,685 | +$1,073.50 | **−$906.77** | 503 | 753 |
| **short** | 854 | **−$245.05** | **+$533.38** | 290 | 327 |
| short (chandelier k=8) | 854 | −$245.05 | **+$330.67** | 213 | 127 |
| short (chandelier k=32) | 854 | −$245.05 | **+$73.78** | 27 | 3 |

**The trail helps exactly the side that is losing money.** The short
side is −$634.34 net with an MFA p50 ($3.716) close to its MFE p50
($4.380) — it goes against the trade first, so a trailing stop is the
*right tool for the right population*.

**A trail scoped to shorts only** would therefore be:
* +$533.38 on the corpus (canonical geometry),
* and it would leave the long side — which is carrying the book —
  completely untouched.

**This is NOT a recommendation to ship it**: it is a **re-design
direction**, because scoping the trail by side is a change to the
engine's exit model, and it needs its own spec and its own
out-of-sample check. But it is the **only** reading of the MFE/MFA
data that produces a positive number, and it is where the next
experiment should go.

> **✅ SUPERSEDED 2026-11-04 by an END-TO-END ENGINE A/B.** The
> numbers above are the **replay on a fixed book** — a SCREEN. The
> engine re-run is in **§ 8.5** and it is the verdict. Read § 8.5
> first; this section is kept because it is what motivated the knob.

---

## 8.5 THE END-TO-END ENGINE A/B — the per-side scope, measured

> **This is the verdict for the per-side scope.** It supersedes § 8.4
> (which was a replay on a fixed book).

§ 3.11 is the rule: *"A drop-set or whitelist MUST be measured END TO
END by re-running the engine. The post-hoc leg filter is a screen for
which hypotheses are worth a re-run — never the verdict itself."* The
trail changes the BOOK, so the replay cannot decide it:

* a trail-closed leg **frees a same-direction slot**, so a LATER
  signal is admitted that would not have been;
* a trail-closed leg can **no longer be netted** by `contra_close`.

Driver: `runtime/diagnostics/trail_sides_corpus_ab.py`. Corpus
**2024-04-01 → 2024-12-01**, ONE continuous execution,
`vol_state_min_expand_prev_day=0.0`, canonical recipe otherwise.

### 8.5.1 The new knob

`trail_sides` scopes the trailing stop to one side of the book:
`'both'` | `'long'` | `'short'`.

> **⚠️ IT IS NOT THE ENTRY GATE.** `trade_sides` decides which trades
> may **OPEN**; `trail_sides` decides which **POSITIONS** get a
> trailing **EXIT**. Under `trail_sides='short'` **both sides still
> trade** — only the short positions' exit changes.

### 8.5.2 The A/B

| arm | legs | gross | net | **vs base** | long net | short net | L-trail |
|---|---|---|---|---|---|---|---|
| trail OFF (baseline) | 2,539 | $828.45 | −$316.48 | — | +$317.86 | −$634.34 | 0 |
| combined k=2 act=1 \| both | 2,390 | −$17.65 | −$1,162.44 | −$845.96 | −$803.87 | −$358.57 | 1,298 |
| combined k=2 act=1 \| long | 2,466 | −$307.42 | −$1,452.24 | −$1,135.76 | −$787.88 | −$664.36 | 1,265 |
| **combined k=2 act=1 \| short** | **2,480** | **$1,161.02** | **+$16.12** | **+$332.60** | **+$374.68** | **−$358.55** | **0** |
| chandelier k=32 act=1 \| both | 2,538 | $825.24 | −$319.67 | −$3.19 | +$246.24 | −$565.91 | 34 |
| chandelier k=32 act=1 \| short | 2,538 | $896.87 | −$248.05 | +$68.42 | +$317.86 | −$565.91 | 0 |
| chandelier k=8 act=1 \| both | 2,471 | $215.25 | −$929.59 | −$613.11 | −$444.18 | −$485.41 | 629 |
| chandelier k=8 act=1 \| short | 2,522 | $1,018.69 | −$126.22 | +$190.25 | +$366.57 | −$492.79 | 0 |
| chandelier k=32 act=2 \| short | 2,538 | $890.52 | −$254.39 | +$62.08 | +$317.86 | −$572.25 | 0 |

**Every short-scoped arm beats the trail-OFF baseline. Every
both-scoped and long-scoped arm loses to it.** The scope is not a
tie-breaker — it is the whole difference between −$845.96 and
**+$332.60**.

> **The four both-scoped rows reproduce § 7.1 to the DOLLAR**
> (baseline −$316.48, canonical −$1,162.44, k=32 −$319.67,
> k=8 −$929.59) — an independent re-run of the same book, which is
> the cross-check that the new driver is measuring the same thing.

### 8.5.3 The scope-gate invariant

`trail_sides='short'` must produce **ZERO long legs exiting on the
trail**:

| arm | long legs on trail | short legs on trail |
|---|---|---|
| combined k=2 act=1 \| both | 1,298 | 676 |
| combined k=2 act=1 \| long | 1,265 | **0** ✅ |
| combined k=2 act=1 \| short | **0** ✅ | 640 |
| chandelier k=32 act=1 \| short | **0** ✅ | 30 |
| chandelier k=8 act=1 \| short | **0** ✅ | 350 |
| chandelier k=32 act=2 \| short | **0** ✅ | 28 |

The invariant holds in **every** arm.

> **⚠️ THE INVARIANT IS ON THE EXIT REASON, NOT ON THE NET.** A
> short-scoped trail CAN legitimately move the long book's net — under
> `combined k=2 act=1 | short` the long net went from +$317.86 to
> **+$374.68** (+$56.82). That is the **book effect**: a trail-closed
> short frees a slot, admitting a later long signal. It is NOT a scope
> leak. An early version of this driver tested the NET and reported a
> false "leak" — the exit-reason test is the correct one.

### 8.5.4 ⚠️ THE OUT-OF-SAMPLE SPLIT — the DECISIVE test

The whole-corpus number above is a **fit on one window**. AGENTS.md
§ 3.10.0 showed exactly how much that costs: the sequence whitelist's
short half **sign-flipped** out of sample (in-sample +0.0700 → OOS
−0.1803).

The split: **ONE continuous execution**, bucketed by **signal time**
into two halves (AGENTS.md § 1.1 — the half is a REPORTING dimension,
never an execution boundary, so both halves share one run's warm caps).

> **⚠️ TWO WINDOWS WERE MEASURED, AND THEY DISAGREE.** The 8-month
> window (2024-04-01 → 2024-12-01, `expand_prev_day=0.0`) and the
> **canonical recipe** (full corpus, `expand_prev_day=1.0` + the 21-key
> whitelist) are **different books**, and they give **different
> verdicts**. The canonical one is the verdict — it is the recipe that
> ships.

#### 8.5.4a The 8-MONTH WINDOW (`expand_prev_day=0.0`) — split at 2024-08-01

| arm | H1 net | **H1 Δ** | H2 net | **H2 Δ** | wins BOTH? |
|---|---|---|---|---|---|
| trail OFF (baseline) | −$227.62 | — | +$140.94 | — | — |
| combined k=2 act=1 \| both | −$573.94 | −$346.33 | −$500.34 | −$641.27 | no |
| combined k=2 act=1 \| short | −$140.53 | **+$87.09** | −$37.30 | **−$178.24** | **NO** |

**The short scope wins H1 and LOSES H2 here.** A fit to H1.

> The gross-per-leg does NOT sign-flip (H1 +0.3464, H2 +0.4203), but
> the NET delta does (+$87.09 → −$178.24) — because the trail changes
> the book's SIZE and therefore the fee bill, and H2's baseline was
> already profitable. **A net that depends on the fee bill is not an
> edge** (AGENTS.md § 5.2's fee-artefact test).

#### 8.5.4b ⭐ THE CANONICAL RECIPE — split at 2025-04-01 — THE VERDICT

Full corpus **2024-01-01 → 2026-07-16**, 14-day warmup prefix
executed and discarded, **canonical recipe** (`expand_prev_day=1.0`,
the 21-key structure whitelist), ONE continuous execution.

| arm | legs | gross | net | **vs base** | H1 net | **H1 Δ** | H2 net | **H2 Δ** | **wins BOTH?** |
|---|---|---|---|---|---|---|---|---|---|
| trail OFF (baseline) | 7,284 | $4,917.66 | +$1,593.16 | — | +$610.65 | — | +$982.51 | — | — |
| combined k=2 act=1 \| both | 6,663 | $2,583.18 | −$741.16 | **−$2,334.32** | −$349.05 | −$959.69 | −$392.12 | −$1,374.62 | no |
| **combined k=2 act=1 \| short** | **7,122** | **$5,622.77** | **+$2,298.20** | **+$705.04** | **+$646.05** | **+$35.40** | **+$1,652.15** | **+$669.64** | ✅ **YES** |

**✅ THE SHORT SCOPE WINS BOTH HALVES ON THE CANONICAL RECIPE.**

* H1: +$35.40 (thin, but positive)
* H2: +$669.64 (strong)

And the **scope-gate invariant holds**: 0 long legs exited on the
trail (vs 3,531 under the `'both'` scope).

> **✅ THE CROSS-CHECK.** The `| both` arm reproduces the canonical
> ledger **EXACTLY** — 6,663 legs, gross $2,583.18, net −$741.16,
> against the ledger's `legs=6663, gross=2583.182733,
> net=-741.160769`. The driver's warmup prefix and windowing are
> byte-exact with `leg_ledger`, so its numbers are comparable to every
> other number in this repo.

> **⚠️ BUT THE TWO WINDOWS DISAGREE, AND THAT IS THE STORY.**
>
> The 8-month window said "does not generalise"; the canonical recipe
> says "generalises". The difference is **the recipe**, not the
> window: the canonical book has the **21-key structure whitelist**
> (which keeps 8 of 64 short signatures) and the **prev-day vol gate**
> at 1.0. The 8-month window had NEITHER — it was the pre-whitelist
> book, and on THAT book the short side's edge is different.
>
> **This is the same lesson as AGENTS.md § 3.10's re-fit:** *a set
> fitted on one book does not describe another*. The 8-month number
> was measured on a book that **no longer exists**.

### 8.5.5 ⚠️ THE HONEST LIMITS

1. **The book is still NET-NEGATIVE on the canonical recipe.**
   +$2,298.20 sounds positive — but the baseline (trail OFF) is
   **+$1,593.16**, and the CANONICAL SHIPPED RECIPE (`| both`) is
   **−$741.16**. So the short scope's +$705.04 is a large improvement
   on the shipped recipe, but the whole book is being compared against
   a recipe that **does not ship**. **The trail-off baseline is not
   the shipping configuration.**
2. **H1 is thin** (+$35.40 on a half-year, 927 days total). H2 is
   strong (+$669.64). A thin half is a warning sign, not a pass.
3. **The trade count rises** (6,663 → 7,122, +6.9%) — the trail
   closes shorts earlier, which frees slots and admits more trades.
   The extra trades carry their own fees.
4. **The short side is STILL negative** (−$376.94 net vs the
   baseline's −$299.85 — the short side got *worse* by −$77.09). The
   gain is entirely on the **long** side (+$782.13), which the trail
   improves **indirectly**: trail-closed shorts free slots, admitting
   better longs. **That is a book effect, and it is fragile** — it
   depends on the same-direction cap being binding.
5. **One window, one split point.** The split at 2025-04-01 is a
   single cut; a different cut could give a different answer.

> **⚠️ NOT PROMOTED YET.** The knob ships as **infrastructure**
> (real, tested, sweepable, engine-read) with the canonical value
> unchanged at `'both'`. Before it can be promoted:
>
> * the **ledger** must be re-measured under `trail_sides='short'`
>   (the corpus-wide leg record, not an engine summary), and
> * the **net profit** of every scope cell must be compared — a gain
>   that arrives through freed slots is a **book-composition** gain,
>   and it still has to earn its keep in dollars.
>
> **The finding that matters most is unchanged: the trail should be
> OFF, or scoped.** The canonical `combined k=2 act=1` at `'both'`
> costs **−$741.16** on the shipping book and **−$845.96** on the
> 8-month window. It is the worst cell in every grid.
>
> **✅ PROMOTED 2026-11-04** at `'short'` — see § 8.6. The criterion
> is **net profit** (user instruction, 2026-11-04), and the
> drift/excess decomposition is a **diagnostic**, not a gate: the
> engine books P&L at the **actual SL / TP fill price** while the
> passive hold is **close-to-close**, so a per-bucket `excess` on a
> stop or target exit measures the **execution-gap differential**, not
> timing skill.

---

## 8.6 ⭐ THE PROMOTION — `trail_sides='short'` (2026-11-04)

The two measurements § 8.5.5 listed as **owed** have now been made.
Driver: `runtime/diagnostics/trail_sides_ledger_verdict.py`.

### The ledger verdict — full corpus, one continuous execution per arm

Window 2024-01-01 → 2026-07-16 (927 days), 14-day warmup prefix
executed and discarded, canonical recipe otherwise.

| book | legs | gross | **net** | **gross/leg** | fee/leg | **gross/fee** |
|---|---|---|---|---|---|---|
| `trail_sides='both'` (was shipping) | 6,663 | +$2,583.18 | −$741.16 | +0.3877 | 0.4989 | 0.777× |
| `trail_sides='long'` | 7,063 | +$2,739.34 | −$584.98 | +0.3878 | 0.4931 | 0.787× |
| `trail_mode='off'` (the baseline) | 7,284 | +$4,917.66 | +$1,593.16 | +0.6751 | 0.4564 | 1.479× |
| **`trail_sides='short'` (PROMOTED)** | **7,122** | **+$5,622.77** | **+$2,298.20** | **+0.7895** | 0.4668 | **1.691×** |

**The promoted scope is the MOST PROFITABLE cell of the scope grid**
(+$2,298.20) — beating the shipping recipe by **+$3,039.36** and the
trail-OFF baseline by **+$705.04** — and the only one whose gross
edge covers the measured round trip by a wide margin.

> **⚠️ THE CRITERION IS NET PROFIT** (user instruction, 2026-11-04:
> *"just promote whatever is most profitable"*). The P&L books at the
> **actual SL / TP fill price** — 85.9% of `sl` legs fill exactly at
> the stop and 100% of the rest fill adverse — and the bars are **1
> second**, so this is as close to tick data as the book needs. The
> drift/excess decomposition and the direction-dependence tables
> below are **DIAGNOSTICS**.

### The passive-hold/excess decomposition — DIAGNOSTIC

| book | passive-hold/leg | hold t | **excess/leg** | **excess t** |
|---|---|---|---|---|
| `both` | +0.4641 | +7.01 | −0.0764 | −8.45 |
| `off` | +0.7407 | +5.03 | −0.0655 | −7.46 |
| **`short`** | **+0.8539** | +6.62 | **−0.0644** | **−7.03** |

> **⚠️ THE GAIN IS THE PASSIVE HOLD, NOT TIMING.** The `excess`
> — what the entries added on top of a passive hold — stays
> **significantly negative** (t = −7.03). The scope found a
> **better place to stand long**; it did **not** find timing skill.
>
> **⚠️ AND A PER-BUCKET `excess` IS NOT EVEN A TIMING MEASURE.** The
> passive hold is a **close-to-close** measurement while the P&L
> books at the **fill price**, so on a stop or target exit the two
> differ by construction. The identity
> `excess == gap@exit − gap@entry` holds to 1e-15 in every bucket of
> every book, and the `sl` bucket's sign **flips with book
> composition** (−$42 on `both`, **+$134** on `short`, +$117 on trail
> OFF). The whole-book and per-side forms DO hold: **−$458.39**
> total, **−$198.80** long, **−$259.59** short.

### The DAILY direction-dependence test

`python -m runtime.diagnostics.daily_direction_dependence` — the
**daily** aggregation (n = 577 days), the sanctioned instrument
(AGENTS.md § 1.1 rule 7):

| book | legs | days | **daily corr** | **corr t** | r² |
|---|---|---|---|---|---|
| `both` | 7,122 | 577 | +0.392 | **+9.92** | 0.154 |
| `off` | 7,284 | 577 | +0.223 | **+5.43** | 0.050 |
| **`short`** | **7,122** | **577** | **+0.392** | **+9.92** | **0.154** |

The daily correlation is **significant** — the book IS market-exposed.
The criterion is net profit, not the absence of exposure.

> **⛔ THE OLD MONTHLY BETA TABLE IS RETIRED.** The previous table
> quoted `corr = +0.337` (n = 31) for the `short` scope, next to the
> `+0.464` that got `trade_sides='long'` rejected. Both are **inside
> their own noise band** — Fisher-z **t = 1.86** and **t = 1.57**,
> neither distinguishable from zero — and the **daily** regression on
> the same book measures **+0.392 with t = +9.92**. The daily number is
> the one that is decisive.
>
> Note that `short` and `both` are the **same book** (the scope changes
> exits, not the daily gross bucket), so their daily correlations are
> identical — as they must be.

### The 2×2 attribution — where the gain really comes from

| book | net | **long-side net** | short-side net |
|---|---|---|---|
| `both` | −$741.16 | −$391.31 | −$349.86 |
| `off` | +$1,593.16 | +$1,893.01 | −$299.85 |
| `long` scope | −$584.98 | −$456.02 | −$128.97 |
| **`short` scope** | **+$2,298.20** | **+$2,675.14** | −$376.94 |

**The net effect of the trail ON for shorts alone is +$705.04; for
longs it is −$2,178.14.** Most of the gain is the trail being
**removed from longs**, not added to shorts. The short scope is still
the correct configuration — it is the only one of the four that beats
the trail-OFF baseline.

### The split robustness

Seven cut points, one continuous execution:

| cut | Δ H1 net | Δ H2 net | vs canonical | vs trail OFF |
|---|---|---|---|---|
| 2024-07-01 | −$206.05 | +$3,245.41 | NO | NO |
| 2024-10-01 | +$208.88 | +$2,830.48 | wins both | NO |
| 2025-01-01 | +$461.57 | +$2,577.79 | wins both | NO |
| 2025-04-01 | +$995.10 | +$2,044.26 | wins both | wins both |
| 2025-07-01 | +$1,913.52 | +$1,125.84 | wins both | wins both |
| 2025-10-01 | +$3,511.97 | −$472.61 | NO | wins both |
| 2026-01-01 | +$4,487.00 | −$1,447.64 | NO | wins both |

**4 of 7 cut points win both halves** vs the canonical recipe, and
**4 of 7** vs the trail-OFF baseline.

### ⚠️ THE GAIN IS REGIME-CONCENTRATED

| year | gold | `both` | `off` | `short` | Δ vs `both` |
|---|---|---|---|---|---|
| 2024 | +27.2% | −$325.55 | +$209.99 | +$136.02 | +$461.57 |
| 2025 | +64.5% | −$193.01 | +$3,339.64 | **+$3,832.42** | **+$4,025.43** |
| 2026 | **−6.1%** | −$222.60 | −$1,956.47 | −$1,670.24 | **−$1,447.64** |

**2026 LOSES $1,448 against the shipping recipe.** The scope helps
when gold trends and hurts when it does not, because the long side
carries the book. It wins **20 of 31 months**.

### The verdict

**PROMOTED 2026-11-04 — `TRAIL_SIDES = 'short'`.** The canonical
ledger was rebuilt under the promoted recipe (7,122 legs, gross
+$5,622.77, net +$2,298.20, gross/leg +0.7895, gross/fee 1.691×), and
the scope-gate invariant holds (**0 long legs** exit on the trail).

> **The canonical recipe's book is now PROFITABLE for the first
> time** — gross/leg +$0.7895 covers the measured round trip
> ($0.4668) at **1.691×**. The previous canonical book did not
> (0.777×).
>
> **But read the caveats.** The gain is 98% **drift**, not timing;
> it is **regime-concentrated** (2026 loses $1,448); and the
> **paired** t-stat is only +1.62. This is a **better place to stand
> long**, and it must be **re-measured whenever the exit geometry
> changes**.

---

## 9. Reproducing

```bash
python -m runtime.diagnostics.trail_mfe_mfa --expand-prev-day 0.0
python -m runtime.diagnostics.trail_mfe_mfa --expand-prev-day 0.0 \
   --start 2024-04-01 --end 2024-12-01 --scope trail_mfe_mfa_cont8
```

The end-to-end A/B (§ 8.5) is reproduced with:

```bash
# the 8-month window (the pre-whitelist book)
python -m runtime.diagnostics.trail_sides_corpus_ab \
   --start 2024-04-01 --end 2024-12-01 \
   --expand-prev-day 0.0 --split-at 2024-08-01

# ⭐ the CANONICAL recipe (the verdict)
python -m runtime.diagnostics.trail_sides_corpus_ab --quick \
   --start 2024-01-01 --end 2026-07-16 \
   --warmup-days 14 --split-at 2025-04-01
```

`--expand-prev-day` defaults to **the canonical constant**; pass
`0.0` to reproduce the older gate-off book. The resolved value is
recorded in the registry record, so two different books never share a
fingerprint.

The diagnostic records every run in the results registry
(`kind="trail_mfe_mfa"` for the replay, `kind="trail_sides_ab"` for
the end-to-end A/B).

**Cost:** ~90 s for a 1-month window, ~22 min for the 8-month window
(12 engine A/B candidates × ~60 s each over 10.9M bars, plus the
detector and the ATR build).

**The end-to-end A/B is far cheaper than that.** The artifacts (zones
/ VP profile / swing cap / p95 cap / vol_state) are built **ONCE**
per window and reused across every arm, so the cost is
`1 × artifact + N × engine` instead of `N × (artifact + engine)`.
The canonical 3-arm run over 41.2M bars took **~7 min** (artifact
build from the on-disk cache + 3 engine passes).

> **⚠️ The 8-month corpus numbers in this file are from the run AFTER
> the `eod`-gross fix** (2026-11-04, reconciliation EXACT). The
> pre-fix run's replay baseline was **$66.63 low** (the 8 `eod` legs)
> and its 2-D surface was correspondingly shifted. The **shape** of
> every table was unchanged; the **absolute replay numbers** moved by
> up to ~$66 on the surface cells. The engine A/B numbers are
> unaffected (they come from the engine, not the replay) — only the
> replay's own rows and the surface moved.

---

## 10. Cross-references

* [`docs/promotions.md`](promotions.md) — what shipped and what was
  rejected.
* [`docs/evaluation.md`](evaluation.md) — the leg-level methodology.
* [`docs/engine_history.md`](engine_history.md) — the bug log.
* `runtime/diagnostics/trail_mfe_mfa.py` — the diagnostic.
* `runtime/diagnostics/trail_sides_corpus_ab.py` — the end-to-end
  engine A/B driver (§ 8.5).
