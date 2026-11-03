# Evaluation — the LEG is the unit, not the month

> Relocated from `AGENTS.md` on 2026-11-03. This is the **evidence** for how
> a run is judged: the methodology, the current verdict, the timing-control
> correction, and the zone-shuffle placebo.
>
> The operating rules live in `AGENTS.md`. The long-form reasoning and the
> measured numbers live here.

## Contents

- [The unit of evaluation is the LEG](#the-unit-of-evaluation-is-the-leg-not-the-month)
- [The current verdict](#the-current-verdict--and-it-is-not-profitable)
- [The entry timing claim was CONFOUNDED](#the-entry-timing-claim-was-confounded--corrected-2026-10-25)
- [The ZONE-SHUFFLE placebo](#the-zone-shuffle-placebo--where-the-gross-really-comes-from)
- [Evaluation methodology](#evaluation-methodology--leg-level-not-month-level)

## The unit of evaluation is the LEG, not the month

**Never evaluate this strategy on monthly slices.** The month is a
**reporting** dimension, never an execution boundary and never a
unit of evaluation.

Why, in one paragraph: this is a high-frequency strategy. After the
vol gate it produces ~40 legs a month, so "8 months" is **n=8 draws**
from a high-variance distribution — and the month's own return tells
us nothing about the in-month volatility distribution the strategy
actually experienced. Worse, the month boundary is an **artefact of
the engine, not of the market**: the p95 cap (7d), the swing cap
(24h) and the VP profile (7d rolling) are all history-dependent, and
in backtest mode the p95 cap returns `inf` until its window fills.
So a cold chunk trades its first ~8 days with **uncapped** targets.

**The replacement** is a corpus-wide, per-leg ledger run over ONE
continuous window, bucketed by `(side, outcome)`:

```bash
python -m runtime.diagnostics.leg_ledger    # build/reuse the ledger
python -m runtime.diagnostics.leg_buckets   # the four buckets + verdicts
```

Four buckets: `long_win`, `long_loss`, `short_win`, `short_loss`.
Each reports `n`, gross, net, mean per leg, and the **standard error
of that mean** — so a reader can see whether a bucket's edge is
distinguishable from zero.

Three rules follow, and they are not optional:

1. **A month is a REPORTING dimension, never an execution
   boundary.** Chunked execution is a harness bug — see § "Chunked vs
   continuous" below, which is not a rounding error.
2. **The "flip" is a tautology.** `gross = (exit − entry) × direction ×
   qty`, so `flip = −gross` **EXACTLY**. It carries zero information.
   The direction question is answered by the **drift/excess**
   decomposition, which depends on the market.
3. **A positive per-leg EV that does not cover the round trip is not
   an edge**, and a near-zero gross makes every gross *ratio*
   meaningless.
4. **A control must be EXCHANGEABLE with what it controls for.**
   Holding the exit fixed is not enough — the entry's *execution
   model* must match too. The random-entry control (§ 2b) failed
   this test; the zone shuffle (§ 2c) is the version that passes.
   **Always run a placebo that destroys ONE ingredient and keeps the
   rest.**
5. **NEVER run a placebo unless the user explicitly prompts for it.**
   A placebo (the zone shuffle, the random-entry control, or any
   other destruction test) costs ~10 minutes on the 1y corpus and
   **rewrites its report artifact**. It is a **confirmation** step
   for a claim that has already been made, not a routine measurement.
   Do not launch one on your own initiative — the answer is already
   recorded in § 2b / § 2c, and re-running it unprompted only
   overwrites the artifact the tests read. **If a claim needs
   re-measurement, ASK FIRST.** The cheap, always-allowed steps are
   the ledger (`leg_ledger` + `leg_buckets`, ~90 s cold / ~1 s
   cached) and the one-month sweep (~6 s).
6. **The trade-count target is 30-70 trades per day.** Measured on
   the continuous corpus, the canonical book must land in that band.
   It is set by `vol_state_min_range_norm`, and that knob is
   **PAIRED with `sl_max_usd`** — the two must always be re-measured
   together. See § "Promotions of 2026-10-25" for the ladder and the
   cost of the target in edge terms.

> ### ⚡ THE ONE-LINE VERDICT (2026-10-25)
>
> **The edge is in the LEVEL, not the TIMING.** Shuffling the FVG
> zones' prices while keeping their trigger bars turns **+$510.58**
> into **−$2,398.07** (mean of 3 seeds, sd $428.29) — a **4.70×**
> collapse, and a **negative** number. The trigger bar tells you
> *when* — and that is worth nothing on its own. The zone's price
> relative to the market carries the information.
>
> So the recipe has a **real level-selection edge** (the long side
> is +5.45pp over break-even, 1.47× the round trip) that is being
> **dragged down by the timing half and by the fee bill** (gross
> $0.3018/leg vs a $0.5029 round trip). The lever is the **exit and
> the trade count**, not the entry signal — see § 2b, § 2c.
>
> ⚠️ **These figures are from the NARROW book** (vol gate 3.0, 1,692
> legs). The canonical recipe is now the WIDE book (vol gate 1.0,
> 51,075 legs) — see § 2. The finding is **robust**: the wide book's
> `excess` is also negative (−$4,080.07, t = −15.63), for every exit
> reason. But the level-not-timing **placebo** was only ever run on
> the narrow book, and it is **prompt-only** — do not re-run it
> unprompted.

## The current verdict — and it is NOT "profitable"

> ### ⚡ PROMOTIONS OF 2026-10-25 — the recipe is now 30-70 trades/day
>
> Three knobs changed together, by instruction:
> **`sl_max_usd` 2.50 → 8.00**, **`contra_close` OFF → ON at `$5.00`**,
> and **`vol_state_min_range_norm` 3.0 → 1.0** (the trade-count
> target). The last two are **PAIRED**: the SL ceiling and the vol
> gate must always be re-measured together.
>
> **The SL ceiling alone took the canonical book from −$340.34 to
> +$54.49 NET** — the first net-positive continuous-corpus result in
> this repo's history. It works because at $2.50 the stop was pinned
> on EVERY leg (1 distinct stop distance across the book), so the HVN
> anchor was inert and the geometry was a fixed 1:$8 bet with no
> structural content. At $8.00 the anchor is live again (**8,445**
> distinct stop distances).
>
> **The trade-count target then costs a lot of edge.** Reaching
> 30-70 trades/day means 51,075 legs instead of 1,692, and gross/leg
> falls from $0.3018 to **$0.1432** — below the $0.3869 round trip.
> **The book is deeply net-negative at the target volume.** That is
> the price of the instruction, stated plainly.

Measured 2026-10-25 on the **continuous** corpus (2024-01-01 →
2026-07-16, canonical sizing 0.01 lots × 100 contract
= **1.00 oz**, `sl_max_usd=8.00`, `contra_close` **ON at $5.00**,
vol gate **1.0**):

| metric | value |
|---|---|
| legs | **51,075** (was 1,692) |
| **trades / calendar-day** | **55.10** (target 30-70 ✅) |
| **trades / trading-day** | **64.65** (target 30-70 ✅) |
| gross | **+$7,312.96** |
| fees | **−$19,758.46** |
| **net** | **−$12,445.50** |
| gross/leg | **$+0.1432** |
| **measured** fee/leg | **$0.3869** |
| **gross/leg ÷ fee/leg** | **0.37×** ❌ |
| hit rate | 49.4% |
| long side | 35,855 legs, gross **+$8,493.75**, hit 50.6% ✅ |
| short side | 15,220 legs, gross **−$1,180.79**, hit 46.4% ❌ |

**The verdict: the book is deeply net-negative at the target volume.**
The fee bill is 2.70× the gross.

> **⚠️ Two DIFFERENT books live here — do not mix their numbers.**
> The narrow book (vol gate 3.0, 1,692 legs, 1.83/day) is the one
> the § 2b / § 2c analyses describe: gross $0.3018/leg, long side
> +5.45pp over break-even, 1.47× the round trip, net −$340.34.
> The wide book (vol gate 1.0, 51,075 legs, 55.10/day) is the one
> that meets the trade-count target: gross $0.1432/leg, 0.37× the
> round trip, net −$12,445.50. **The placebo and drift/excess
> findings were measured on the NARROW book.** Re-measure them on
> the wide book before quoting them as properties of the recipe.

**The verdict on the narrow book (the one § 2b / § 2c measured):**
the LONG side clears the round trip; the SHORT side does not; the
whole book does not. The fee bill is 1.67× the gross.

> **⚠️ "Not profitable" ≠ "no skill" — but the skill is NOT timing.**
> The book is net-negative because the **fee bill** exceeds the gross
> edge. The long side's +5.45pp edge over break-even is a real
> property of the realised P&L. **But it is not entry timing** — the
> drift/excess decomposition shows **drift is 130.4% of gross and
> the timing component (`excess`) is significantly NEGATIVE**
> (−$155.00, t = −2.96), and the **zone-shuffle placebo** confirms
> it: shuffling the zones' prices while keeping the trigger bars
> intact turns +$510.58 into **−$2,398.07** (§ 2c).
>
> **The edge is in the LEVEL, not the TIMING.** The FVG zone's price
> relative to the market carries the information; the trigger bar
> carries none. That is a *level-selection* edge — a real, exploitable
> thing — but it is being harvested by a recipe that also pays for
> the timing, and the fee bill is 1.67× the gross it produces.

Payoff geometry (per side, at the leg level):

| side | hit | avg win | avg loss | payoff | break-even hit | edge | gross/leg ÷ fee | diagnosis |
|---|---|---|---|---|---|---|---|---|
| long | 25.0% | $10.87 | $2.64 | 4.11× | 19.6% | **+5.45pp** | **1.47×** | **profitable** |
| short | 17.0% | $9.86 | $2.52 | 3.91× | 20.4% | **−3.39pp** | −0.83× | **no signal** |

The two failure modes need **different fixes**, and this is the
single most actionable split in the repo:

* **The long side is fee-limited, not signal-less.** It has a real
  +5.45pp edge and 1.47× the round trip per leg. It loses only
  because the *aggregate* is dragged down by the short side. Fix =
  make the short side stop losing.
* **The short side has no signal at all** (−3.39pp edge — it loses
  *before* fees). Fix = better short entries, or a short-side gate.

> **⚠️ But "fee-limited" is now a narrower claim than it looks.**
> The long side's +5.45pp is measured on realised P&L, and the
> zone-shuffle placebo (§ 2c) shows that P&L comes from the **entry
> LEVEL**, not the signal timing. So the long side's edge is a
> **level-selection** edge that happens to be profitable per leg —
> and it is exposed to the market, because a level edge harvested
> by a drift-following position is drift-dependent by construction.
> Do not read "1.47× the round trip" as "this side is safe".

> ### ⚠️ The "profitable" number in the old AGENTS.md was a CHUNKING ARTEFACT
>
> Earlier revisions of this file reported the recipe as **net-positive
> (+$74.51 over 15 months, `contra_close` ON)**. That number came
> from **month-chunked** runs. It does not survive continuous
> execution, and the reason is not a rounding difference.
>
> Measured over the **same 16 calendar months**, same recipe:
>
> | execution | legs | gross | fees | **net** |
> |---|---|---|---|---|
> | chunked (per-month runs) | 904 | +$488.82 | $414.31 | **+$74.51** |
> | **continuous** (one run) | 942 | +$264.37 | $517.73 | **−$253.36** |
> | | | | | |
> | **chunked** (contra OFF) | 899 | +$375.03 | $414.31 | **−$39.28** |
> | **continuous** (contra OFF) | 989 | +$427.35 | $543.57 | **−$116.22** |
>
> Two things are true at once, and both matter:
>
> * The chunked runs **discard legs** (899 chunked vs 989 continuous
>   on the same months) *and* charge less fee — a cold chunk's caps
>   are different caps, so the trades that survive are different
>   trades.
> * The `contra_close` delta **flips sign** between the two views:
>   **+$113.79 chunked** vs **−$137.15 continuous**. The feature was
>   re-picked at `$1.0` *because of* the chunked measurement, so the
>   chunked view is what produced the promotion.
>
> **This is exactly the failure mode the monthly paradigm is
> retired for.** Reproduce:
>
> ```bash
> python -m runtime.diagnostics.chunked_vs_continuous_months
> python -m runtime.diagnostics.chunk_vs_continuous
> ```

## The entry timing claim was CONFOUNDED — corrected 2026-10-25

> **⚠️ SUPERSEDED. This section previously reported "the entry
> timing DOES have skill — 91% of legs beat a random entry, t =
> +18.29". That number was real but it measured the WRONG THING.**
> The control was not exchangeable with the strategy, and the
> diagnostic was fixed at 06:06 on 2026-10-25 to say so.

`runtime/diagnostics/leg_random_entry.py` runs a random-entry
control. It holds the **exit fixed** and randomises only the
**entry**:

```
strategy gross = (exit_price - entry_price) * side * qty
random gross   = (exit_price - close[k])  * side * qty
```

`k` is drawn uniformly from the strategy's OWN decision window
`[signal_bar, exit_bar)` — the window in which it chose to trade.
Same exit price, same side, same size: **the entry bar is the only
difference**. 200 samples per leg, fixed seed.

### The flaw: the two arms are NOT exchangeable

| | strategy | random entrant |
|---|---|---|
| fill type | **RESTING LIMIT** at the FVG zone edge | **MARKET** at `close[k]` |
| fills on | the bar price *touches* the zone edge | the bar it was sampled on |

The strategy's limit order rests for **6,410 bars** on average
before it fills (narrow book; **4,659 bars** on the wide book).
During that time the market has moved *toward* the
zone edge, so the limit fill is systematically at a better price
than the market was at the signal bar. **This is true on 100.0% of
legs** — it is a property of the resting-limit geometry, not of
the FVG signal.

Measured on the continuous corpus (narrow book: 1,692 legs, 200
samples/leg):

| decomposition of the +$11,820 control delta | value | share |
|---|---|---|
| from the **PRE-fill** window `[signal, entry)` | **+$12,181.84** | **103.1%** |
| from the **POST-fill** window `[entry, exit)` | **−$79,402.50** | — |
| total | +$11,800.80 | |

> **The pre-fill window contributes MORE than 100% of the delta.** The
> post-fill window — the part where the timing would have to show
> up — is **negative**. So the control's `t = +18.29` is measuring
> the **limit-order mechanic**, which is already in the backtest,
> not the entry signal.
>
> **On the WIDE book the confound is unchanged and the verdict is the
> same**: the pre-fill window is favourable on **100.0% of legs** and
> contributes **113.17%** of the control's delta ($168,443.35 of
> $148,880.17). The `post_fill_discount_total` is positive on the wide
> book, but that is a **netting artifact** — see the note in § "The
> honest timing number" below. The honest number is `excess`, and it
> is negative.

**A control that compares a resting limit against a market fill is
not measuring skill.** It is measuring the difference between two
execution models, one of which the strategy already has.

### The honest timing number: `excess` = gross − drift

The ledger already computes the correct control — a **passive hold**
of the same side over the same window, with the same execution
model on both sides:

\[
\text{drift} = (close_{\text{exit bar}} - close_{\text{entry bar}})
\cdot \text{direction} \cdot \text{qty}
\qquad
\text{excess} = \text{gross} - \text{drift}
\]

| component | value | mean/leg | t | reading |
|---|---|---|---|---|
| component | narrow book | wide book (canonical) |
|---|---|---|
| legs | 1,692 | **51,075** |
| gross | +$510.58 | **+$7,312.96** |
| **drift** (beta) | +$665.58 (**130.4%** of gross) | **+$11,393.03** (**155.8%** of gross) |
| **excess** (timing) | **−$155.00** (t = −2.96) | **−$4,080.07** (t = **−15.63**) |
| excess per leg | −$0.0916 | **−$0.0799** |

**Drift is 130.4% of gross on the narrow book and 155.8% on the wide
one.** The realised P&L is market drift, and the timing component is
**significantly negative** on BOTH books — the strategy captures
*less* than a passive hold of the same positions.

> **The verdict is ROBUST to the book size.** On the wide book the
> excess is negative for **every exit reason**, not just in
> aggregate:
>
> | exit reason | n | excess |
> |---|---|---|
> | tp | 1,047 | −$49.03 |
> | tp1 | 1,113 | −$13.77 |
> | tp2 | 10,118 | −$1,617.60 |
> | sl | 21,583 | −$1,089.71 |
> | eod | 3 | +$0.01 (noise, n=3) |
> | contra_close | 17,211 | −$1,309.97 |
> | **TOTAL** | **51,075** | **−$4,080.07** (t = −15.63) |
>
> Guard: `tests/test_signal_bar_completeness.py::test_excess_is_negative_for_every_exit_reason`.

> **⚠️ The random-entry control's `post_fill_discount_total` flipped
> POSITIVE on the wide book** (+$3,224,633.82). That is a **metric
> artifact of the netting**, not a change in the finding: a
> `contra_close` leg's "exit" is the netting price — a price the
> market did not independently reach — so the market's drift over that
> window is measured against the wrong reference. The netting accounts
> for +$3,439,943.61 of the total; **excluding the netting legs the
> post-fill discount is −$215,309.79**, still negative. The
> `excess` decomposition above is unaffected (it uses the ledger's own
> drift) and remains negative for every bucket including
> `contra_close`.

**The honest verdict is the opposite of the one this section used to
give.** The entry does **not** demonstrate skill. It demonstrates a
levered drift bet that the fee bill then eats.

```
VERDICT: NO TIMING EDGE - the gross is market drift; the
        timing component (excess) is negative.
```

> **Rule: a control must be EXCHANGEABLE with the thing it controls
> for.** Holding the exit fixed is not enough — the *entry
> execution model* must also match. Comparing a resting limit to a
> market fill measures the order type.
>
> The fix for this class of confound is the **drift/excess**
> decomposition, which is an exact algebraic identity computed with
> the same execution model on both sides. See § "Question 2 — is the
> edge direction-independent?".

### What this does NOT change

The **payoff geometry** in § 2 is unaffected — it is computed from
realised prices only, with no counterfactual:

* **long side: +5.45pp edge over break-even, 1.47× the round trip.**
  That is a real, measurable property of the realised P&L.
* **short side: −3.39pp.** No signal.

What the corrected control removes is the **excuse** for the long
side's fee-limitedness. The long side's gross edge is real, but it
is **drift**, not timing — so it will not survive a market that
stops trending, and "the entry works, the exit caps it" is **not**
a supported reading.

### The three failure modes (corrected)

| failure mode | signature | fix |
|---|---|---|
| no signal | `edge_pp ≤ 0` — loses before fees | better entries |
| fee-limited | `edge_pp > 0` but `gross/leg < fee` | fewer/bigger trades |
| **drift-dependent** | `excess ≤ 0` — the gross IS the market | **a signal that is not drift** |

The long side is **fee-limited AND drift-dependent**. The short
side is **no signal**. There is no third "exit-capped" mode in the
evidence.

Reproduce:

```bash
python -m runtime.diagnostics.leg_random_entry
python -m runtime.diagnostics.leg_buckets --timing
```

## The ZONE-SHUFFLE placebo — where the gross really comes from

> **⚠️ TWO THINGS ABOUT THIS SECTION.**
>
> **1. IT WAS MEASURED ON THE NARROW BOOK** (vol gate 3.0, 1,692
> legs, `sl_max_usd=2.50`, `contra_close` OFF). The current canonical
> recipe is the wide book. The numbers below are correct **for the
> book they were measured on** — do not quote the $510.58 gross as a
> property of the current recipe (whose gross is $7,312.96 on 51,075
> legs).
>
> **2. DO NOT RE-RUN IT UNPROMPTED.** It costs ~10 minutes on the 1y
> corpus and it **overwrites `zone_shuffle_placebo.json`**, which the
> guard tests read. The answer is recorded here. If a claim needs
> re-confirming on the new recipe, **ask the user first** — see rule 5
> in "READ THIS FIRST".

> **This is the decisive test, and it settles the question.** The
> drift/excess decomposition says the timing component is negative;
> that leaves two candidates for the +$510.58 gross: the **exit**
> (a fixed target a drift-following position reaches more often) and
> the **payoff geometry** ($2.50 stop vs a $20 target). The zone
> shuffle separates them.

`runtime/diagnostics/zone_shuffle_placebo.py` takes the canonical
zone set — the **same** zones, trigger bars, widths, directions and
count — and **permutes their PRICES** across the corpus:

```
zone_low[i]  <- the price of some OTHER zone
zone_high[i] <- the matching price of that other zone
(widths, directions and trigger bars are UNCHANGED)
```

**What survives:** the number of zones, the zone-width distribution,
the signal **timing** (trigger bars untouched), the book / conflict /
fill mechanics, the fee model, the SL/TP construction.

**What is destroyed:** the correspondence between a signal and the
price level it points at. The engine still fires on the same bars in
the same direction, but the entry level is now arbitrary.

**If the gross were a timing edge**, the placebo would keep most of
it — the signal still says "go long at this moment", and a long at
an arbitrary level still earns the drift. **If the gross collapses**,
it came from the **level**, and the "signal" never carried timing
information at all.

Measured on the continuous corpus (1,692 canonical legs), with the
**VALID** re-centred shuffle (3 seeds) and a `recentred` control arm:

| arm | legs | gross | drift | excess | gross/leg | \|entry−mkt\| p50 |
|---|---|---|---|---|---|---|
| **canonical** | 1,692 | **+$510.58** | +$665.58 | −$155.00 | **$0.3018** | $0.13 |
| recentred | 2,023 | +$145.14 | +$449.31 | −$304.17 | $0.0717 | $0.13 |
| shuffled (seed 11) | 2,102 | **−$2,068.02** | +$517.46 | −$2,585.47 | −$0.9838 | $0.65 |
| shuffled (seed 23) | 2,121 | **−$2,244.15** | +$168.81 | −$2,412.96 | −$1.0581 | $0.67 |
| shuffled (seed 37) | 2,080 | **−$2,882.06** | −$255.26 | −$2,626.80 | −$1.3856 | $0.73 |

```
VERDICT: the gross COLLAPSES under the shuffle - it came
        from the entry LEVEL (where the zone sat), not from
        the signal's timing. The 'signal' was not a timing edge.

  geometry check: canonical |entry-mkt| p50 = $0.13; placebo max = $0.73  -> VALID
```

**The gross does not merely shrink — it goes NEGATIVE**, to a mean of
**−$2,398.07** (sd $428.29 over 3 seeds) against a canonical
**+$510.58**. That is a **4.70×** collapse and a **−469.7%** swing,
and the placebo's own variance across the three seeds is small
(sd $428 on a ~$2,400 base). So this is not noise.

**The mechanism, per the drift/excess split.** The shuffle destroyed
the **DRIFT**: drift/leg falls from **+$0.3934** to **+$0.0677**
(and to **−$0.1227** on seed 37). The positions no longer sit on the
side of the market that pays. The **excess** collapsed even harder
(−$0.0916 → −$1.2102 per leg), because the payoff geometry was
capturing a drift that no longer existed. So the FVG **level** was
doing **directional** work — it was choosing which side of the market
to sit on.

**The `recentred` arm is the sharpest form of the question.** It
keeps the zone's width, direction and trigger bar, but re-centres the
band on the **signal bar's own close** — i.e. it keeps "a level near
the market" and destroys only "**the FVG's specific level**". It
retains just **28.4%** of the canonical gross ($145.14 vs $510.58),
and its `excess` is **−$304.17**, worse than canonical's −$155.00. So
it is **not** enough for the level to be *near* the market — the
specific FVG level carries most of the information.

> **⚠️ A PRIOR MEASUREMENT OF THIS TEST WAS INVALID — do not quote it.**
> An earlier run of this diagnostic used the **naive** price shuffle
> (permuting the raw `zone_low`/`zone_high` wholesale) and reported
> the gross collapsing to **−$1.24 MILLION**. That number is **not a
> result** — it is an artefact of the broken geometry: the naive
> shuffle puts each entry ~$900 away from the market (measured
> `|entry − market@fill|` p50 = **$910.35**), so the "P&L" is just
> that distance (**−$1,058 per leg**). The script's own docstring
> says so, and the re-centred shuffle was written to fix it.
> **The valid figure is −$2,398.07.** The *verdict* is unchanged
> (the gross still collapses and goes negative), but the effect size
> is **~520× smaller** than the invalid run claimed, and the invalid
> run had **no geometry gate** — it never checked that its entries
> were anywhere near the market.
>
> **Rule: a placebo must clear a GEOMETRY GATE before its P&L is
> read.** `zone_shuffle_placebo.py` now computes the canonical
> arm's `|entry − market@fill|` p50 ($0.13) and refuses to print a
> verdict if any placebo arm's is more than
> `max($2.00, 5 × canonical)` away. Guard:
> `tests/test_level_vs_timing.py::test_zone_shuffle_report_is_geometry_valid`.

**What that means.**

* The edge is **in the LEVEL** — the FVG zone's price relative to
  the market — and **not in the timing**. The trigger bar tells you
  *when*, and that is worth nothing on its own.
* The negative `excess` in § 2b is now explained: the strategy
  captures **less** than a passive hold because its exits truncate
  the drift it is actually harvesting, and the zone level (not the
  signal) is what picks the position.
* **This is not a bug and it is not "no edge" either.** It is a
  **level-selection edge** — "buy the FVG zone, don't buy the
  signal" — being harvested by a strategy that buys *both* the
  level and the timing. The timing half is drag.
* **The lever follows directly:** the entry level is where the
  information is, so the target and stop should be sized to the
  **level's** geometry, not to a fixed $2.50 stop and a $20 target
  chosen for the drift.

> **Rule: a placebo that destroys ONE ingredient and keeps the rest
> is the only way to attribute a result to that ingredient.** The
> random-entry control (§ 2b) was supposed to be that test for the
> entry, but it changed the execution model as well, so it measured
> the order type. The zone shuffle keeps the execution model
> identical and changes only the price level — and that is why it
> gives a clean answer.

> **Rule (added 2026-10-25): the placebo's BASELINE must be the
> SIMPLEST thing that captures the effect you are attributing to the
> rule.** A coin-flip placebo asks "better than chance"; in a
> drifting market "chance + drift" is a high bar a drift-follower can
> clear by luck. A **constant-side** placebo asks "better than
> ALWAYS" — it inherits the drift in the baseline, so the rule has to
> beat the market itself. Measured on the TA episode metric: 11 of
> 64 TA cells beat a coin-flip side placebo, but **0 of 23** beat a
> constant-side baseline on a held-out window, and the two that beat
> it on the fit window collapsed to zero out-of-sample. See
> `docs/ta_episode_metric.md`.

Reproduce:

```bash
python -m runtime.diagnostics.zone_shuffle_placebo
python -m runtime.diagnostics.ta_fixed_side_placebo
```

## Evaluation methodology — LEG-LEVEL, not month-level

### Why monthly slicing was WRONG (measured)

| # | problem | measurement |
|---|---|---|
| 1 | **A month is one draw, not a sample.** The recipe trades ~40 legs/month; the monthly view evaluates n=8. | — |
| 2 | **The month boundary is an engine artefact.** The p95 cap (7d), swing cap (24h) and VP profile (7d rolling) are **history-dependent**, so a cold monthly chunk's first ~8 days are UNCAPPED. | Measured on 2025 Q1 (canonical, 1.00 oz): **continuous run = 126 legs**; monthly chunks = 113 (**89.7%** — ~10% of the universe silently discarded). |
| 3 | **The regime label summarises a distribution we do not have.** "2025-05 FLAT" says the month's close moved +0.50%. It says nothing about whether the strategy's legs were long or short, won or lost, or what the per-leg edge was. | — |

### The replacement: four buckets + three questions

| bucket | meaning |
|---|---|
| `long_win` | long, gross > 0 |
| `long_loss` | long, gross ≤ 0 |
| `short_win` | short, gross > 0 |
| `short_loss` | short, gross ≤ 0 |

Each bucket reports `n`, gross, net, **mean per leg**, and the
**standard error of that mean**.

**Question 1 — is there an edge at all?**
The per-leg gross EV, compared against the **measured** round trip.
The report prints `gross_per_leg / fee_per_leg` so the comparison is
explicit. A gross edge that does not cover the round trip is **not**
an edge.

**Question 2 — is the edge direction-independent?**
The **drift / excess decomposition** — the leg-level replacement for
the monthly beta regression.

The engine computes

\[
\text{gross} = (\text{exit} - \text{entry}) \cdot \text{direction} \cdot \text{qty}
\]

The real counterfactual is the **passive hold**: what would a
position of the same size and side have earned by simply being there
over the same window?

\[
\text{drift} = (\text{close}_{\text{exit bar}} - \text{close}_{\text{entry bar}}) \cdot \text{direction} \cdot \text{qty}
\]

\[
\text{excess} = \text{gross} - \text{drift}
\]

`drift` is the **directional / beta component** — the P&L any
position of that size and side would have earned by being there.
`excess` is the **timing component** — the part attributable to the
strategy's **entry timing**, which a directional proxy cannot have.

This decomposition is an **exact algebraic identity**, not an
estimate. No regression, no monthly aggregation, and no regime label
are required, and it is measured **per leg** with a standard error on
the mean.

**Current reading (continuous corpus, contra OFF):**
drift **+$665.58** (t = +2.10), excess **−$155.00** (t = −2.96).
**The realised P&L is MORE than explained by market drift — the
timing component is negative.** That is the beta-proxy signature,
now measured at the leg level.

**Question 3 — is the loss concentrated?**
The loss buckets' mean and their tail. A strategy whose losses are
all the same size is a **geometry** problem; one with a fat loss tail
is a **risk** problem. Measured: `sl` n=1,319 mean −$2.5955; `tp2`
n=309 mean +$11.7083. The losses are tightly sized (the $2.50
ceiling) — this is a geometry/fee problem, not a tail problem.

### The flip is a tautology — do not use it as a test

The engine computes `gross = (exit − entry) × direction × qty`, so
negating `direction` negates the gross **exactly**:

\[
\text{gross}_{\text{flipped}} = -\text{gross}
\]

**Exact, not an approximation.** So `flip_gross = -gross` by
construction, and it is **arithmetically incapable** of carrying
information about whether the strategy has an edge. The ledger
stores the column (`flip_gross`) and the report flags it
(`flip_gross_total_is_tautology: true`) purely to document the trap.
**Any conclusion drawn from it is vacuous.**

Note this is NOT the same as re-running the strategy with the sign
inverted — a flipped leg would have a different SL/TP geometry and
might never have been taken. The point is narrower and fatal:
**holding entry, exit and size fixed, the flip is forced to be
`-gross`, so it cannot test anything.**

### The chop test at the right granularity

The old decisive test was "does it make money in flat months?".
The per-leg version asks the same question at the leg's own scale:
**do the legs whose LOCAL market hour was flat have a positive
edge?**

`local_drift_1h` is the market's drift over the hour before each
leg's **signal** bar, as a percentage of the entry price. Flat is
defined on that per-leg quantity, not on a month's close-to-close
return. This is the direct replacement for the monthly UP/DOWN/FLAT
split, and it has ~30× the resolution.

Current reading (continuous corpus): flat n=952 gross **+$377.90**
(mean +$0.3970, t=+1.87); up n=376 gross +$518.08; down n=364 gross
**−$385.41** (t=−3.69).

### The payoff geometry — signal problem or fee problem?

For each side the report computes `avg_win`, `avg_loss`, the
`payoff_ratio`, the **break-even win rate**

\[
p_{\text{be}} = \frac{\text{avg\_loss}}{\text{avg\_win} + \text{avg\_loss}}
\]

and the **edge** in percentage points:

\[
\text{edge\_pp} = 100 \cdot (\text{hit\_rate} - p_{\text{be}})
\]

* `edge_pp ≤ 0` → **no signal**. The side loses before fees.
* `edge_pp > 0` but `gross_per_leg < fee` → **fee-limited**. The
  signal is real but too small to pay for itself.
* `gross_per_leg ≥ fee` → **profitable**.

The two failure modes need **different fixes** — a fee-limited side
needs fewer/bigger trades, a signal-less side needs better entries —
and the monthly paradigm could not tell them apart.

### What the leg view does NOT claim

* A positive per-leg EV that does not cover the round trip is **not**
  an edge.
* `excess` is the **timing component of the REALISED P&L**. It does
  not by itself prove the strategy is tradeable — the fee bill still
  has to be paid, and the total has to clear it.
* A **near-zero gross total** makes any gross *ratio* (e.g.
  `drift_share_of_gross`) meaningless — the denominator is near
  zero, so the ratio explodes and flips sign. The report prints
  `n/a` in that regime; read the **absolute totals**.

### The ledger is cached and fingerprinted

The engine run is the expensive part, so the ledger is cached to
`runtime/diagnostics/_reports/leg_ledger.json`, keyed by a
fingerprint of everything that can change the legs: the data file
(size + mtime), the date range, the full engine kwargs, and a SHA1
over the artifact-producing source files.

### ⚠️ The canonical ledger is CANONICAL-ONLY (2026-10-25)

**A variant ledger may not write the canonical path.**

The canonical ledger is the recipe's **evidence record**. Every
consumer — `leg_buckets`, `leg_random_entry`,
`warmup_prefix_effect`, `chunk_vs_continuous`, and the test suite
— reads it as *the canonical recipe*. On 2026-10-25 an ad-hoc
`build_ledger(engine_overrides={...})` call (no explicit `out_path`)
**replaced the canonical ledger with a variant**
(`sl_max_usd=8.0, contra_close=True`), and every consumer went on
analysing the variant while believing it was reading the canonical
strategy.

The only symptom was that the gross totals stopped agreeing with the
timing control ($751.77 vs $510.58) — which is exactly the failure
mode the repo already documents as *"a default silently substituted
for an intent"*, in mirror image: **a variant measured as the
canonical value.**

Two guards now make that structural rather than a convention:

| guard | where | what it does |
|---|---|---|
| **write** | `leg_ledger._guard_canonical_path` | a variant writing to `LEDGER_PATH` raises `SystemExit`. A variant must go to its **own file**. `allow_variant_canonical=True` is the deliberate escape hatch. |
| **read** | `leg_ledger.load_ledger(expect_canonical=True)` | a reader that expects the canonical recipe refuses a ledger whose recorded `engine_overrides` is non-empty. |

The read guard checks the recorded **`engine_overrides`**, not the
fingerprint — the fingerprint changes for benign reasons (the data
file's mtime, a source edit) and would make the check unusable. The
overrides are the thing that actually changes the **strategy**.

`leg_buckets` and `leg_random_entry` now pass
`expect_canonical=True`. `warmup_prefix_effect` asserts it too.

**When you build a variant, give it its own path:**

```python
build_ledger(engine_overrides=ov,
             out_path=OUT_DIR / "leg_ledger_variant.json")
```

Guards:
`tests/test_leg_buckets.py::test_a_variant_may_not_write_the_canonical_ledger`
· `::test_load_ledger_rejects_a_variant_when_the_canonical_is_expected`
· `::test_the_canonical_ledger_on_disk_is_canonical`

> **Rule: an artifact that every consumer reads as "the canonical
> result" must be structurally unable to hold anything else.** A
> fingerprint proves *what* built it; it does not stop the wrong
> thing from being written there.

### Execution is CONTINUOUS — the month is only a reporting dimension

**This is the single most important property of the ledger: the
execution window and the reporting window are decoupled.**

The strategy runs over **one continuous window** spanning the whole
corpus. The ledger's **warmup prefix** (14 days, which exceeds the
longest history-dependent lookback — the p95 cap's 7 days, the swing
cap's 24h, the VP profile's 7d rolling window) is executed but its
**legs are discarded**; only its warmup effect is kept. Every scored
leg therefore has **warm caps**.

Month boundaries then become a *reporting* dimension computed from
the leg timestamps — **never an execution boundary**. Nothing is
lost at a boundary.

> **The signal bar is the causal bar.** Every per-side gate is
> evaluated at the signal, so the ledger keys legs by their signal
> time — not by their fill bar. A bullish BoS invalidates the
> opposing bear FVGs, so a bar whose last structure event is a
> bullish BoS can *never* produce a short signal: the `short|b`
> bucket is **structurally empty**. Measuring at the fill bar
> instead produced a clean-looking, promotable result that was
> measuring the wrong thing.

---
