# sniper_mt_vp_forex_bt_only

Standalone backtest repo for the v22 SNIPER recipe on
**XAUUSD (MT4 MT5 convention)**, 100× leverage on the 0.01-lot
base, with the **live 40¢/oz spread** modelled directly (no
backtest-vs-live split).

This repo is **self-contained**. The engine source from
`ict_forex_research/src/` and `ict_crypto_research/src/` is
**duplicated** into `bt/_engine/`. There are no `sys.modules['src']`
shims, no shadow context managers, and no runtime dependency on
either upstream repo. To refresh the engine after upstream changes,
re-copy the files in `bt/_engine/` and rewrite `from src.X` to
`from bt._engine.X`.

---

## ⚡ READ THIS FIRST — how to evaluate, and how to sweep

### 1. The unit of evaluation is the LEG, not the month

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

### 2. The current verdict — and it is NOT "profitable"

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

### 2b. The entry timing claim was CONFOUNDED — corrected 2026-10-25

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

#### The flaw: the two arms are NOT exchangeable

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

#### The honest timing number: `excess` = gross − drift

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

#### What this does NOT change

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

#### The three failure modes (corrected)

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

### 2c. The ZONE-SHUFFLE placebo — where the gross really comes from

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

### 3. Sweeping: 1 month by default, always ask before a full run

**Never launch a multi-month or full-corpus sweep without asking.**
The policy is in § "Sweep policy" below. In short:

* Default to **one month** (~6 s end to end).
* **The driver ENFORCES the ask.** A window longer than 31 days
  prints its cost estimate and **exits with code 2** unless
  `--i-know` is passed. The default window is one month, so the
  cheap thing is the default thing.
* Even so, **state the window, the cell count and the cost estimate
  in chat before launching a long run**, and get the user's
  go-ahead. The gate protects against an *accidental* long run; it
  does not replace telling the user what you are about to do.
* Every sweep is judged on the **leg ledger**, not on the month's
  net.

### 4. The measurement WORKFLOW — cheap first, expensive last

**This is the routine. Follow it in this order and do not skip
ahead.**

| step | command | cost | what it tells you |
|---|---|---|---|
| **1. one month** | `python -m runtime.sweeps.cached_sweep --knob X --grid ...` | **~6 s** | is the knob **live**? does the metric move at all? |
| **2. the ledger** | `python -m runtime.diagnostics.leg_ledger` then `leg_buckets` | **~90 s** cold, **~1 s** cached | the **real** verdict: buckets, drift/excess, payoff geometry |
| **3. the trade count** | `python -m runtime.diagnostics.volgate_trade_count` | ~15 min (6 cells) | does the book land in the **30-70 trades/day** band? |
| **4. the placebo** | `python -m runtime.diagnostics.zone_shuffle_placebo` | ~10 min | is the edge in the **level** or the **timing**? **⚠️ ONLY WHEN THE USER PROMPTS FOR IT** — see rule 5 |
| **5. the full corpus** | `--i-know` + explicit go-ahead | **~3 min/cell** | only for a candidate that survived 1–4 |

> **⚠️ Step 4 (the placebo) is PROMPT-ONLY.** Never launch it
> unprompted: it costs ~10 minutes, it **rewrites
> `zone_shuffle_placebo.json`**, and the guard tests read that
> artifact. The answer is already recorded in § 2b / § 2c. If a
> claim needs re-confirming, **ask the user first**. Steps 1-3 are
> always allowed.

> **The ledger is cheap, and it is cached by fingerprint.** The engine
> walk is **82.3 s** on the 1y corpus (measured 2026-10-25); the
> artifact build is cached alongside it, so a repeat run is **~1 s**.
> The ledger's fingerprint covers the data file, the window and the
> engine kwargs — so it rebuilds automatically when any of those
> change, and never goes stale silently.
>
> **⚠️ The ledger is no longer ~90 s on the WIDE book.** With the
> canonical recipe at vol gate 1.0 (51,075 legs), the engine walk is
> **~300 s** — measured 2026-10-25. The artifact build dominates the
> rest.

**Rules that keep it fast.**

* **A month is enough to answer "is this knob live?"** — a knob
  that does not move a single metric on 1 month will not move it on
  12. Do not spend 3 minutes/cell proving a null.
* **Never sweep a knob you have not read the canonical config for.**
  The canonical config table says which knobs are live, which are
  inert (`sl_mode` at the $2.50 ceiling), and which are dead. A
  sweep of an inert knob is a sweep that measures nothing — the
  exact failure the repo keeps documenting.
* **Do not re-measure the same cell twice.** Every cell writes its
  own JSON under `runtime/sweeps/_results/`. Check the directory
  before running.
* **One 1y process at a time.** Two concurrent 1y runs cost a
  **9×** slowdown each (memory contention), not a 2× — so
  "parallelise the sweep" is a *pessimisation* at this scale.
* **The `--i-know` gate is not permission.** It is a guard against
  accident. A long run still needs a **message to the user** with
  the window, the cell count and the estimate.

> **Why the workflow is ordered this way.** The corpus-wide ledger
> run costs ~90 s and gives a *better* answer than a 12-month sweep
> cell, because it pools every leg instead of reading one month. So
> the full corpus is not "the thorough version of the monthly
> sweep" — it is a **different measurement** with a different cost.
> Use the ledger.

---

## Architecture

**Flat `bt/` module — no nested subpackages.** Every Python file lives
directly under `bt/`. Mirrors `sniper_mt_vp_bt_only`.

**Engine source is DUPLICATED, not re-exported.** The detectors
(`FvgZone`, `detect_fvg`, `detect_market_structure`) and the
multi-target engine (`run_vp_sniper_backtest`) all live in
`bt/_engine/`. The files are verbatim copies of upstream, with only
the cross-file imports rewritten to `from bt._engine.X`. To find an
algorithm's home, grep `bt/_engine/` — it has no upstream
dependencies at all.

**Multi-target wrapper (`bt/vp_engine.py`).** Adds the XAUUSD sizing
convention on top of the duplicated engine:
`qty_btc = lots × contract_size` (= 1.0 at canonical 0.01 lots ×
100 contract_size). Strips the forex fee knobs
(`commission_per_lot_usd`, `spread_per_oz_usd`) before forwarding to
the engine — the engine reads `taker_fee_bps` only.

## Sizing (MT4 XAUUSD, 100× leverage)

| Field | Canonical | Source |
| --- | --- | --- |
| `lots` | `0.01` | MT4 minimum |
| `contract_size` | `100.0` | 100 oz / lot → 1 oz at 0.01 |
| leverage | `100×` | user-specified |
| spot | `~$3,000/oz` | live mid |
| notional | `$3,000` | `lots × contract_size × spot` |
| margin | `$30` | `notional / leverage` |

**P&L per $1 favorable move at 1 oz:** `+$1.00` (long) / `−$1.00`
(short). Same as 1 BTC at 1 BTC qty.

## Fees (single source of truth)

| Component | Round-trip | Per-trade @ 0.01 lots × 100 |
| --- | --- | --- |
| Commission / lot | `$7.00` | `0.01 × $7 = $0.07` |
| Spread / oz | `$0.40` | `1 × $0.40 = $0.40` |
| **Total** | | **$0.47** at $3,000 spot |

There is exactly ONE fee model. The backtest IS the live quote. No
"baseline $0.19 + overlay $0.21" split — the upstream backtest's
$0.19/oz baseline is an artifact of the backtest using
tighter-than-live quotes; we model the live MT4 quote directly.
`spread_per_der_usd = 0.40` is the single source of truth in
`bt/optimal_config.FOREX_SPREAD_PER_OZ_USD`.

> ### ⚠️ The measured fee is NOT always $0.47
>
> The engine's fee is `avg_price × qty × bps × 2`, and `bps` is
> derived from the corpus **median spot**. So the realised round trip
> scales with the price the trade actually filled at. The corpus
> trades between **$2,331 and $4,871**, so the measured fee on the
> full corpus is **$0.5029/leg**, not $0.47.
>
> `leg_buckets` therefore reports **both**: `fee_per_leg` (measured,
> used for the verdict) and `fee_per_leg_spec` ($0.47, the contract
> value at $3,000). **The verdict uses the measured fee** — using the
> flat $0.47 overstates the edge by ~7%.
>
> Guard: `tests/test_leg_buckets.py` locks the measured-fee path.

### D1 fix (2026-10-22) — the ×2 factor

The engine debits the **full entry+exit** cost at close time:

```python
fee = |exit + entry| * 0.5 * qty * (bps / 10000.0) * 2.0
```

The trailing `* 2.0` models entry + exit. So `_derive_taker_fee_bps`
must hand the engine a **per-side** rate. It used to hand over the
**round-trip dollar amount**, so every trade was charged **$0.94
against a $0.47 spec** — a silent 2× overcharge on every single
trade.

```python
# FIXED:
rt = lots * commission_per_lot_usd + oz * spread_per_oz_usd
return rt * 10000.0 / (2.0 * oz * spot)  # the 2.0 undoes the engine's 2.0
```

Guard: `tests/test_regressions.py::test_round_trip_fee_is_not_double_charged`
and `tests/test_no_dead_imports.py::test_live_round_trip_fee_is_47_cents`.

## No-defaults policy

Every field on every local dataclass is REQUIRED:

* `MarginAndCost` (output of `margin_and_cost`)
* `ExposureSnapshot` (output of `account_exposure`)
* `KellySizing` (output of `size_lots`)

Construction without all fields raises `TypeError`.

> **Update 2026-10-22.** `FocusedParams` and `VPEngineConfig` were
> DELETED as phantom surfaces (never constructed in production).
>
> **Update 2026-10-24.** `build_focused_params()` was DELETED too.
> The recipe dataclass it returned was never read by the VP engine,
> so every knob on it was a silent no-op. `build_engine_kwargs()`
> is now one of **seven** materialisers — see § "ONE source of truth".

## ONE source of truth (2026-10-24 — config consolidation)

`bt/optimal_config.py` is the **single** place a canonical value
lives. There is no recipe dataclass, no upstream re-export, and no
second copy of any knob.

> **Update 2026-10-24.** `build_focused_params()` and the whole
> `TrendStrategyParams` recipe are **DELETED**. The recipe was
> never read by the VP engine (`run_vp_sniper_backtest` has no
> `params` argument), so every `_OPTIMAL_OVERRIDES_ALLOWED` key was
> a **silent no-op** — a sweep of `fvg_inv_trade_sl_zone_mult`
> reported a "canonical" cell that measured nothing.

### What was deleted

| Deleted | Why |
| --- | --- |
| `bt/_engine/optimal_config.py` | The `TrendStrategyParams` recipe: TWO recipes (`SNIPER` + `HF_TIGHT`) and an `optimal_params()` override factory. `HF_TIGHT` had **zero** callers anywhere. |
| `bt/_engine/ict_backtest.py` | The **bar engine** (`run_ict_backtest`). Zero production callers — the VP multi-target engine is the only engine. |
| `bt/_engine/precomputed.py` | The bar engine's precompute cache. Zero callers; the VP engine has no precompute cache (the sweep driver owns its own artifact cache). |
| `TrendStrategyParams` (in `bt/_engine/ict_strategy.py`) | The ~1000-field recipe dataclass. Only the dead bar engine read it. `PendingSignal` survives — `ict_signals.py` uses it. |
| `run_vp_sniper_backtest_forex(params=...)` | A silent-drop trap: the parameter was accepted and **never forwarded**. |

### What replaced it

**Seven** **materialisers** in `bt/optimal_config.py`, each reading
the module constants and each enforcing its own allowlist. There is
no override *layering* — a builder either emits the canonical value
or an explicitly-allowed override, and anything else raises
`KeyError`.

| Builder | Surface | Allowlist |
| --- | --- | --- |
| `build_engine_kwargs(**over)` | engine kwargs | `_ENGINE_OVERRIDES_ALLOWED` |
| `build_vp_kwargs(**over)` | `build_volume_profiles` | `_VP_OVERRIDES_ALLOWED` |
| `build_detector_kwargs(**over)` | `detect_fvg` | `_DETECTOR_OVERRIDES_ALLOWED` |
| `build_swing_kwargs(**over)` | `build_daily_swing` | `_SWING_OVERRIDES_ALLOWED` |
| `build_p95_kwargs(**over)` | `build_rolling_p95_from_bars` | `_P95_OVERRIDES_ALLOWED` |
| `build_live_p95_kwargs(**over)` | `make_live_rolling_p95` | `_LIVE_P95_OVERRIDES_ALLOWED` |
| `build_structure_kwargs(**over)` | `detect_market_structure` | `_STRUCTURE_OVERRIDES_ALLOWED` |

**Seven surfaces, not two.** The swing cap, the p95 cap and the
structure state are **cached objects** built from the bars — the
engine receives the finished object, not the build parameters. They
used to be duplicated as literal dicts inside
`runtime/sweeps/cached_sweep.py`; they now come from the config.

### The rule

> **A knob that is not on an allowlist does not exist.** Passing one
> raises `KeyError` — it can never become a silent no-op. That is
> the whole point of the consolidation: the old failure mode was a
> sweep that *looked* like it measured something and measured
> nothing.

Guards:
* `tests/test_smoke.py::test_every_builder_rejects_illegal_overrides`
* `tests/test_parity.py::test_knob_allowlist_split`
* `tests/test_no_dead_imports.py::test_no_module_imports_bt_twice_under_different_names`
* `tests/test_config_single_source.py` — the AST guard suite:
  no second source of truth, no unread (dead) canonical knob, no
  duplicated knob→parameter map outside `bt/optimal_config.py`.
* `runtime/diagnostics/audit_config_surface.py` — the same audit as
  a runnable report (duplicate constants / dead imports / dead knobs).
* `runtime/diagnostics/audit_constant_reachability.py` — the STRICT
  version: a constant must be passed by name onto a target surface,
  not merely mentioned in a docstring.

### The two p95 builders are NOT interchangeable

`build_p95_kwargs()` is the **backtest** path: it pins
`seed_constant_usd=0.0` ("the corpus already has the history the cap
needs"). `build_live_p95_kwargs()` is the **live VPS** path: it uses
the baked-in `FOREX_P95_RET_USD` as the coldstart fallback.

Keeping them separate is what guarantees the backtest can never
accidentally pick up the live seed constant and silently move every
measured number.

### The four translated builders

`build_swing_kwargs`, `build_p95_kwargs`, `build_live_p95_kwargs`
and `build_structure_kwargs` use **canonical knob names** that
differ from their target's parameter names. The translation maps
(`_SWING_KNOB_TO_PARAM` etc.) live in `bt/optimal_config.py` and
nowhere else.

> **The bug this fixes.** `base.update(overrides)` APPENDED the
> canonical key instead of replacing the target-named one, so
> `build_swing_kwargs(swing_cap_pct=0.5)` emitted
> `{'cap_pct': 0.25, 'swing_cap_pct': 0.5}`. Splatted into
> `build_daily_swing` that is a `TypeError`; worse, it was silently
> overridden by a caller's own copy of the map. **The override was
> measured as nothing.**

## Canonical config

All values live in `bt/optimal_config.py`.

| Knob | Value | Surface | Note |
| --- | --- | --- | --- |
| `lots` | `0.01` | sizing | MT4 minimum |
| `contract_size` | `100.0` | sizing | 100 oz/lot → 1 oz at 0.01 |
| `taker_fee_bps` | derived | engine | Per-SIDE rate — see D1 |
| `sl_zone_mult` | `40.0` | engine | **The FALLBACK stop only** — applies when `sl_mode='hvn_lvn'` cannot resolve the Nth shelf behind entry |
| `sl_buffer_usd` | `0.05` | engine | |
| `sl_mode` | `hvn_lvn` | engine | Stop anchors on the Nth HVN shelf behind entry — the mirror of the TP |
| `sl_nth_hvn_n` | `1` | engine | Which shelf behind entry anchors the stop. Deliberately **separate** from the TP's `nth_hvn_n` |
| `sl_min_usd` | `2.50` | engine | Hard floor on stop distance from entry. Can only ever **loosen** a too-tight stop |
| `sl_max_usd` | `8.00` | engine | Hard CEILING on stop distance — can only ever **tighten**. **RAISED from $2.50 on 2026-10-25** — at $2.50 it pinned EVERY stop (1 distinct stop distance across 18,608 legs) and made the HVN SL anchor **inert**; at $8.00 there are **8,445** distinct distances, so the anchor is live again. ⚠️ **PAIRED with the vol gate** — re-measure them together |
| `sl_hvn_max_distance_usd` | `0.0` | engine | `0.0` = **DERIVE** the shelf-search radius per bar from `min(swing_cap, p95_cap)` |
| `nth_hvn_n` | `3` | engine | TP reaches for the 3rd shelf ahead |
| `tp_zone_mult_fallback` | `200.0` | engine | NB75 remainder leg |
| `tp_mode` | `multi_target` | engine | NB74 HVN1 partial |
| `partial_close_pct` | `0.50` | engine | |
| `tp_max_distance_usd` | `0.0` | engine | **DERIVED** — it is only the HVN search radius, never a TP ceiling |
| `tp_hvn_proxy_frac` | `0.7` | engine | The `multi_hvn_proxy` literal, promoted so it is sweepable |
| `conflict_policy` | `stack` | engine | Both sides trade freely; the only signal-level gate is `max_same_dir_open=20`. The prior `drop` value produced a permanent directional lock |
| `max_same_dir_open` | `20` | engine | D3 fix — was `0` (uncapped) |
| `entry_fill_model` | `touch` | engine | D2 fix — resting limit order |
| `fill_model` | `pessimistic` | engine | SL precedence + gap-through at open |
| `tp_p95_mult` | `1.0` | engine | NB91 rolling p95 TP cap |
| `entry_mode` | `dual` | engine | Immediate + sniper co-fire |
| `vol_state_min_range_norm` | `1.0` | engine | **RE-PAIRED 2026-10-25: 3.0 → 1.0 for the TRADE-COUNT TARGET.** Admit a signal only when the current 1m candle's range is ≥N× the trailing-60m median. At 3.0 it admitted only **1.83 trades/calendar-day**; at 1.0 it gives **55.10/day** (64.65/trading-day) — inside the **30-70/day** target. Non-directional. ⚠️ **PAIRED with the SL ceiling** — re-measure them together |
| `structure_seq_drop_set` | `{"short\|c", "short\|C"}` | engine | **PROMOTED 2026-10-24.** Do not take a short when the last structure event was a **reversal** (CHoCH bull / CHoCH bear) |
| `structure_gate` | `False` | engine | **NOT PROMOTED** — improves gross but leaves the market correlation untouched |
| `trade_sides` | `"both"` | engine | **NOT PROMOTED — deliberately two-sided.** A long-only account is a directional bet |
| `contra_close` | `True` | engine | **PROMOTED 2026-10-25** (user instruction "contra 5USD"). Was OFF. See § "Contra close / hedge netting" |
| `contra_close_spread_usd` | `5.0` | engine | Price spread threshold for `contra_close`. Moved $1.0 → $5.0 on 2026-10-25 — **more conservative** than the pair-level pick, so it fires only on WIDE locks |
| `swing_cap_pct` | `0.25` | engine | NB72 daily-swing TP cap (1-day window) |
| `fvg_min_zone_usd` | `0.30` | detector | ⚠️ MUST match the canonical recipe — the old driver used `0.10`, which detected **44×** more zones and traded a different strategy |
| `resample_to_n_secs` | `60` | detector | FVG detection cadence is **1m**, matching the structure cadence. At 1s the supersession cascade starves the inversion path (7 inversions/month vs 130 at 1m) |
| `vp_rows` | `200` | VP | Re-scaled from the BTC-era 100 — a $0.13/row profile is ~3× finer than the median FVG |
| `vp_min_zone_usd` | `0.50` | VP | Re-scaled from the BTC-era $5.00 |
| `structure_pivot_len` | `9` | structure | At the 1m resample cadence |
| `strict_wick_required` | `False` | detector | **Ships DISABLED, deliberately.** The sibling's canonical floor ($0.68) matches **0.00%** of XAUUSD 1s windows |

> **The vol gate and the SL ceiling are PAIRED, not independent.**
> `sl_max_usd` was measured with the gate OFF (optimum $5.00) and
> with the gate ON (optimum $2.50–$3.00). They must always be
> re-measured together.

## Timestamps are nanoseconds — always

Every timestamp crossing a module boundary is **int64 epoch
nanoseconds**. The canonical parquet stores `timestamp[ms, UTC]`,
and pandas preserves that resolution, so:

```python
# WRONG — yields MILLISECONDS (1_784_143_874_000)
bars["time"].astype("int64")

# RIGHT
from bt._engine.timebase import to_ns_arr
times = to_ns_arr(bars)
```

Mixing the two is silent and catastrophic. The original canonical
run emitted **264,666 signals and closed 0 trades** because the NB91
p95 cap computed `warmup_until_ns` in real nanoseconds while the
engine handed it millisecond timestamps — so `is_warmup_complete`
was `False` on every bar and `skip_trades_during_warmup` discarded
every single entry.

`to_ns_arr` / `assert_ns` are applied in `rolling_p95.py`,
`swing_cap.py`, `volume_profile.py` and the engine bar loop. The
`assert_ns` guard raises loudly if a millisecond array ever slips
through again.

---

## Sweep policy — 1 month by default, ask before the full run

**This is the operating rule for this repo.** The recipe is
high-frequency and the measurement unit is the leg, so the sweep
policy has to match the strategy, not the habit of a monthly
backtest.

### The rules

1. **Default to ONE month.** A 1-month canonical sweep costs
   **~6 s** end to end (measured 2026-10-25: 1.6 s bars + 2.0 s
   artifacts + 1.6 s engine). There is no excuse for running more
   than you need.
2. **Report the full-run cost before running it — AND THE DRIVER
   ENFORCES IT.** The `--i-know` approval gate is **implemented**
   (2026-10-25, `runtime/sweeps/cost_estimate.py`):
   * a window longer than **31 days** prints its estimated cost and
     **exits with code 2 without running anything**, unless
     `--i-know` is passed;
   * the default window is **one month** (`2025-01-01` →
     `2025-02-01`), so the cheap thing is the default thing;
   * the estimate is computed from the parquet **time column only**
     (~0.5 s, exact bar count), so the pre-flight is essentially
     free.

   > **⚠️ This gate did NOT exist until 2026-10-25.** For several
   > revisions this file documented an `--i-know` flag and an ETA
   > prompt that were **never implemented** — the safety was
   > *imagined*. A rule written only in prose is a rule nobody
   > enforces. If the gate ever misbehaves, check
   > `runtime/sweeps/cost_estimate.py` and
   > `tests/test_sweep_policy.py` — not this file.
   >
   > The old default was `--start None --end None`, which silently
   > loaded the **entire 1y corpus** (41M bars, ~2 GB) for a sweep
   > that was almost always read off one month. That default is
   > gone.
3. **Never run two 1y processes concurrently.** They exhaust memory
   and the bar loop stalls for hours (see § "Operational gotcha").
   Measured 2026-10-25: the 1-month artifact build took **18–25 s**
   with a competing process running and **2.0 s** without — a **9×**
   difference on identical code.
4. **Judge the sweep on the LEDGER, not on the month's net.** A
   month's net is one draw. Re-run `leg_ledger` + `leg_buckets` on
   the continuous corpus before believing any cell.

### The one-month window

```bash
# canonical recipe, one month, ~6 s  (the dates are the DEFAULT)
python -m runtime.sweeps.cached_sweep --knob _canonical

# a single knob, warm cache
python -m runtime.sweeps.cached_sweep \
  --knob sl_zone_mult --grid 10,20,40,80 \
  --start 2025-01-01 --end 2025-02-01

# MULTI-knob grid (cartesian product) — the LAST knob varies fastest.
# Each cell writes its own JSON under
# runtime/sweeps/_results/_multi/<scope>__cell_<idx>.json
python -m runtime.sweeps.cached_sweep \
  --knobs "sl_zone_mult=4,8,12,20,40;max_same_dir_open=10,20" \
  --start 2025-01-01 --end 2025-02-01

# force a rebuild
python -m runtime.sweeps.cached_sweep --knob _canonical --no-cache
```

### The confirmation prompt — IT IS NOW REAL

A window longer than one month **prints the estimate and exits**:

```bash
# Prints the estimate, then ABORTS with exit code 2.
python -m runtime.sweeps.cached_sweep --knob _canonical \
  --start 2024-01-01 --end 2026-07-16

# Already approved by the human — this one RUNS.
python -m runtime.sweeps.cached_sweep --knob _canonical \
  --start 2024-01-01 --end 2026-07-16 --i-know
```

The abort prints:

```
  window      : 2024-01-01 .. 2026-07-16   (927 days)
  bars        : ~41,233,730
  artifacts   : COLD (cache MISS)
  load        : ~7.0 s
  artifact    : ~97.2 s
  engine/cell : ~58.5 s
  cells       : 1
  ---------------------------------------------------------------
  ESTIMATED   : ~2.7 min total
  ---------------------------------------------------------------
  NOTE        : this window is longer than 31 days.
                The monthly chunking is an ENGINE artefact --
                the p95 cap (7d), swing cap (24h) and VP profile
                (7d rolling) are history-dependent, so a cold
                chunk trades its first ~8 days UNCAPPED and
                discards part of the trade universe.
                Verify on the continuous corpus with leg_ledger
                + leg_buckets before believing a cell.
  ---------------------------------------------------------------
  re-run with --i-know to execute.

ABORTED: window is longer than one month. Re-run with --i-know to execute.
```

> **Why the estimate is trustworthy.** The bar count is read from
> the parquet **time column**, so it is exact. The rates are
> MEASURED on this machine, not guessed: engine **705,000 bars/s**
> (from the 1y run's own progress log: `bar=41000000/41233730
> elapsed=56.3s rate=728335/s`), artifacts **424,000 bars/s**
> (97.3 s for 41.2M bars), load **7.0 s** for the 41.2M-bar file.
>
> An earlier version of the estimator interpolated within the
> parquet footer's row-group statistics. That was **rejected**: FX
> bars are not uniform in wall-clock time (weekend gaps), so the
> interpolation was off by **−6% to +8%** depending on the window.
> Reading the time column costs 0.5 s and is exact.

**Even with the gate, state the window and the estimate in chat
before launching.** The gate protects against an *accidental* long
run; it does not replace telling the user what you are about to do.

### Measured cost model (XAUUSD 1s, 1y file)

| scope | bars | artifact build (cold) | engine cell | total, 1 cell |
| --- | --- | --- | --- | --- |
| 1 month (2025-01) | 1,377,190 | **2.0 s** | **1.6 s** | **~6 s** |
| 1y (2024-01 → 2026-07) | 41,233,730 | **86.5–97.3 s** | **51.0–58.5 s** (narrow book) | **~2.3–2.7 min cold, ~1 min warm** |
| 1y, **WIDE book** (vol gate 1.0) | 41,233,730 | cached | **~300 s** (51,075 legs) | **~5 min** |

> **⚠️ The engine cell cost depends on the BOOK SIZE, not just the
> bar count.** On the narrow book (vol gate 3.0, 1,692 legs) the 1y
> engine cell is ~51 s. On the **wide book** (vol gate 1.0, 51,075
> legs — the 2026-10-25 canonical recipe) it is **~300 s**, measured
> 2026-10-25. The per-bar open-book scan is `O(n_bars × n_open)` and
> the wide book holds far more positions. **Any 1y measurement on the
> current canonical recipe costs ~5 minutes per cell, not ~1.**
> `runtime/diagnostics/volgate_trade_count.py` (6 cells) therefore
> takes ~15-30 minutes, not ~10.

Cold 1y artifact build breakdown (measured 2026-10-25, from
`build_secs` in the artifact cache; two independent builds):

```
artifacts MISS in 86.5s (114,479 zones at the 1m cadence)
 zones      13.5s   <- detect_fvg, 1m cadence
 vp_set     25.1s
 swing_cap   3.7s
 p95_cap     2.2s
 structure  31.1s
 vol_state  10.9s
```

> **Two corrections to earlier drafts of this table.** An earlier
> version claimed **~190 s** (its stage numbers did not sum to it)
> and another claimed **~10 s** (it read the `detect_fvg` stage
> alone as the whole build). The measured total is **86.5 s**, and
> the **largest single stage is `structure` (31.1 s)**, not
> `detect_fvg` (13.5 s).
>
> A second cold build measured **97.3 s** with `structure` at
> **42.7 s** — the extra came from a concurrent process competing
> for memory bandwidth. **Treat 86.5–97.3 s as the range.**
>
> **The artifact build is now the DOMINANT cost of a cold 1y cell,
> not the engine.** ~87–97 s of artifacts vs. 51.0 s of engine.
> `structure` and `vp_set` are the two stages worth optimising if
> cold-build time ever matters — and both are **cached**, so this
> is paid once per fingerprint, not once per sweep cell.

Engine bar rate on the 1y run: **~730,000–790,000 bars/s**, with a
**measured max open depth of 2** — the vol gate drops 56% of
signals, so the book is nearly always empty and the
O(n_bars × open) scan is effectively free.

> **⚠️ Never run two heavy cells concurrently.** AGENTS.md's
> "Operational gotcha: memory contention kills 1y runs silently"
> applies. The 1-month build measured **18–25 s** while a second
> process was running, and **2.0 s** when it was not — a **9×**
> difference on the same code.

The artifact cache is keyed by the file (size + mtime), the date
slice, the resolved detector / VP / swing / p95 / structure kwargs,
and a SHA1 over the artifact-producing source files. So **a knob
that only touches engine kwargs reuses the cache on every cell**;
a detector / VP / structure knob rebuilds it.

### Per-element JSON output (sweep output policy)

**Rule:** each sweep cell writes its own JSON the moment that cell
finishes. **No full-sweep JSON written at the end.**

The point is fault tolerance — a 60-cell sweep that crashes on cell
57 should still have 57 readable result files on disk, not 0.

```
runtime/sweeps/_results/<knob>/<scope>__cell_<idx>.json
```

Each file is a single JSON object:

```json
{
 "ts_utc": "2026-10-20T19:42:00Z",
 "notebook": "nb86_sl_tp_zone_mult_sweep",
 "knob": "sl_zone_mult",
 "value": 40.0,
 "cell_idx": 3,
 "scope": ["xauusd_s1_1y"],
 "engine": "vp_multi_target",
 "wall_secs": 142.7,
 "cache_hit": true,
 "metrics": { "n_trades": 1842, "hit_rate": 0.512, "...": "..." },
 "overrides_vs_canonical": {"sl_zone_mult": 40.0}
}
```

### Anti-pattern (do NOT do this)

```python
# WRONG — one file at the end of the loop, no per-cell recovery
results = []
for value in grid:
    res, _ = run_vp(...)
    results.append((value, res))
with open("sweep.json", "w") as f:
    json.dump(results, f)  # crash here = total loss
```

### The sweep driver: `runtime/sweeps/cached_sweep.py`

This is the ONLY sweep driver in the repo. It replaces the old
`single_sweep.py` (which reported empty metrics — it read
`result.calmar` / `result.n_trades`, none of which exist on
`BacktestResult`) and `run_dual_sweep.py` (which hardcoded detector
knobs that disagreed with the canonical recipe).

**Detector config must match the canonical config.**
`detector_kwargs_from_recipe()` reads the canonical constants via
`build_detector_kwargs()`. The old driver hardcoded
`fvg_min_zone_usd=0.10` while the canonical says `0.30`. That 3×
threshold difference produced **425,094 zones per month instead of
9,684** (44×), which flooded the open book and made the bar loop's
`O(n_bars × n_open_positions)` check run for hours. It was also
silently trading a different, far higher-frequency strategy than the
recipe specifies.

Guard: `tests/test_regressions.py::test_detector_kwargs_match_the_canonical_recipe`.

### The one-month sweep is NOT the canonical measurement

This is the most important caveat in the file, and it is a
**correctness** issue, not a preference.

The engine's caps are history-dependent:

| cap | lookback | cold-start behaviour |
| --- | --- | --- |
| p95 TP cap (NB91) | 7 days | returns `inf` → **uncapped TP** |
| swing TP cap (NB72) | 24h | degenerate on day 1 |
| VP profile | 7d rolling | cold shelves |

So a chunked run's first ~8 days trade with **different targets**
than the same days in a continuous run.

Measured on **2025-01** (canonical recipe, 1.00 oz):

| execution | legs | gross | fees | net | median TP |
| --- | --- | --- | --- | --- | --- |
| chunked, no warmup (what `cached_sweep` does) | 41 | **+$11.12** | $19.13 | **−$8.02** | $6.15 |
| chunked + 14d warmup prefix | 60 | −$22.82 | $27.28 | −$50.11 | $5.60 |
| **continuous corpus, sliced by signal time** | 41 | **−$27.27** | $15.73 | **−$43.00** | $6.08 |

Same leg count, same calendar window, **$35 apart in net** — and the
sign of the gross flips. Reproduce:

```bash
python -m runtime.diagnostics.chunk_vs_continuous
python -m runtime.diagnostics.warmup_prefix_effect
```

**The fix for the driver** is a warmup prefix: execute from
`start − 14 days` (14 days exceeds the longest lookback: p95's 7
days, the swing cap's 24h, the VP profile's 7d rolling) and keep
only the scored window's legs. `leg_ledger` already does this
(`WARMUP_DAYS = 14`).

---

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

## The engine fixes that made the backtest honest

These are real, measured, and worth keeping. They are not edge work.

| # | Defect | Fix | Effect |
|---|---|---|---|
| **D1** | The engine debits `|exit+entry| × 0.5 × qty × bps × 2`, but `_derive_taker_fee_bps` handed it the **round-trip** dollar amount — a 2× overcharge on every trade. | Hand the engine a **per-side** rate. | $0.94 → **$0.47** per trade |
| **D2** | Entries opened **instantly on the trigger bar** at a price the market never traded. Measured: **95.9%** of pre-D2 fills happened on a later bar (median +42 bars), 4.1% never filled. | A signal now **places** a `PendingOrder`; it fills on a strictly later bar whose range touches the limit level. If the zone dies first, the order is **cancelled**. | trades 5,985 → **717** |
| **D3** | With `conflict_policy='drop'` and an unbounded book, the book **never emptied** and the recipe locked to one side (4,335 shorts / 0 longs) — a book artefact, not a bearish edge. | The same-dir cap check moved into the `drop` branch; canonical `max_same_dir_open = 20`. | two-sided trading; book depth capped at 20 |
| **P2–P4** | The per-bar book scan computed state nobody read, made ~198M method calls, and allocated ~198M lists. | Gate the scan behind its consumers; inline `qty_of`; compact in place. | engine rate 14,700 → **~71,000 bars/s** |

### Pessimistic execution (2026-10-22)

On 1s bars a single bar can touch BOTH the stop and the target, and
can gap through a level entirely. The canonical `fill_model`:

| Mode | Ambiguous bar | Gap-through |
| --- | --- | --- |
| `mid` | TARGET wins (legacy, optimistic) | fills at the level |
| `pessimistic` | **STOP wins** | fills at the **bar's open** |
| `next_open` | STOP wins | always the bar's open |

Measured on 1-month 2025-01, 0.00% of trades have a bar touching
both levels — so this knob is not the binding constraint on *this*
corpus. It **must** be pessimistic anyway, because it starts biting
the moment `sl_zone_mult` is tightened and the stop sits close to
the entry.

### The multi-target lifecycle — TP1 partial → breakeven → TP2

**Verified by measurement, not by reading the code.**
`runtime/diagnostics/breakeven_reminder_check.py` and
`runtime/diagnostics/breakeven_escape_impact.py`.

**Yes — the SL actually moves when the first leg takes 50%.** The TP1
branch closes `pct` of the position and reopens the remainder as a
**new `OpenTrade`** with `stop_price = entry_price` (breakeven),
`sl_struct_ref = "breakeven"`, `partial_close_pct = 0.0` (so TP1
cannot re-fire), `target_price = tp2_price`, and `qty = pos_qty ×
(1 − pct)`.

Because `partial_close_pct == 0`, `tp2_active` is **False** on the
remainder, so it is evaluated by the **single-target** path, where
`sl_hit = b_low <= ot.stop_price` (long) — i.e. the breakeven stop is
a **real, enforced exit**, not a field that is set and ignored.

Measured across six canonical slices: `sl_distance_usd` is **$0.00**
on EVERY sl-exited remainder leg, with an absolute max of **$0.00**
— the signature of a breakeven stop firing precisely at entry.

**The one inconsistency: same-bar breakeven escape.** The TP1 branch
ends in `continue`, so the freshly-created remainder is **not
re-scanned** against the bar that fired TP1. A bar that reached TP1
and also traded back through the entry price therefore lets the
remainder escape a breakeven stop that was already breached.

Measured: **1 of 12 TP1 legs (8.3%)** had such a bar. This is
**FAVOURABLE** to the strategy and is **inconsistent with
`fill_model='pessimistic'`**. **Do not "fix" it silently** — it moves
P&L, and every number in this repo was measured with it in place. If
it is ever changed, re-measure the canonical slices. Guard:
`tests/test_regressions.py::test_tp1_remainder_is_not_rescanned_against_the_tp1_bar`.

### The per-side sequence filter — filter each side on its OWN sequences

The short side's negative alpha is a **composition** problem: some
short sequences make money and others do not. The fix is a
**per-side** filter that keeps both sides alive.

**Design constraints this must satisfy:**

| constraint | why |
|---|---|
| **Per-side, not pooled** | longs and shorts have different sequences |
| **Sufficient sample per cell** | the per-side split is coarse on purpose |
| **Fit / held-out disjoint** | a filter fitted and scored on the same slices proves nothing |
| **Gross, not net, is the metric** | a net "win" from trading less is not an edge |
| **Flat-slice test must hold** | the decisive chop test |

##### The statistical correction that made it work

A filter does not claim "the trades I keep are profitable". It
claims "the trades I DROP are worse than the ones I keep". The
correct statistic is the **paired delta**:

\[
\text{delta\_gross} = \text{gross(kept)} - \text{gross(all)} = -\text{gross(dropped)}
\]

So "drop bucket X" is justified exactly when `gross(X) < 0` with a
significantly negative mean — **not** when the kept trades happen to
look good.

##### The measurement trap: SIGNAL bar vs FILL bar

Under the D2 resting-order model a signal is **placed** on bar `S`
and the order **fills** on a later bar `F` (median 42 bars later).
The engine's entry-side gates are evaluated at `S` — that is when
the decision is made, and the only bar at which the decision is
causal. So the analysis must tag on `S` too.

The first measurement tagged the FILL bar. It found `short|b` with
105 trades, a paired t-stat of −2.10, and a +$104 gross improvement
— a clean-looking, promotable result. **It was measuring the wrong
thing.** Re-measured at the SIGNAL bar, `short|b` is
**STRUCTURALLY EMPTY**: a bullish BoS invalidates the opposing bear
FVGs, so no short *can* be signalled there.

> **Rule: a gate that the engine evaluates at bar `S` must be
> measured at bar `S`. The fill bar is a different bar.**

`ClosedTrade.signal_bar` was added (2026-10-24) so this cannot be
gotten wrong silently.

##### The rule this yields

**Do not take a SHORT when the last structure event was a REVERSAL.**
Keep shorts after a bearish **continuation** (`B`).

The market reading: the FVG that triggers a short formed *before* the
reversal, and the reversal event is precisely what invalidates it. A
short after a bearish continuation is *with* the trend — and that
bucket is positive on both fit and held-out halves.

Note also that the **long** side's best bucket is `long|C` — a long
after a **bearish** reversal. The same asymmetry holds on both sides:
**fade the structure, don't join it.** That symmetry is why the
filter has to be per-side.

##### PROMOTED — `structure_seq_drop_set = {"short|c", "short|C"}`

Pooled over all slices: gross **+$276 → +$379** (+$103), net **+$9 →
+$197**, short-side gross/leg **−$0.081 → +$0.996** (12× better), and
the **long side is completely untouched** (same legs, same gross,
same correlation). Both sides still trade — **this is not a
directional bet**.

**Honest weakness.** Walk-forward re-fitting is **2/7**
out-of-sample slices positive, so this ships as a **fixed,
pre-registered pair of buckets** chosen on the fit/held-out
consistency check, NOT as a general sequence-learning feature.

**Implementation.** `structure_seq_drop_set` is an engine kwarg. It
is applied **AFTER** the sequence-direction flip (so it sees the
FINAL side), **AFTER** the side gate, and **BEFORE** the
conflict-policy check.

**Equivalence to the post-hoc measurement is proven, not assumed.**
`runtime/diagnostics/seq_drop_set_equivalence.py` runs the engine with
the drop set and compares it against the post-hoc filter on the same
run, on all slices. `runtime/diagnostics/side_gate_equivalence.py`
does the same for the `trade_sides` gate. Both print
`ALL SLICES EQUIVALENT: True`.

---

## Contra close / hedge netting — MEASURED, ships ON at $5.00 (2026-10-25)

The mechanism is real: the book can hold opposing positions, and
those legs are a **locked spread**. Left alone, the engine closes
them **independently** — one leg's stop fires while its counterparty
sits on an equal-and-opposite gain that is never taken.

The knob: `contra_close` (bool, canonical **ON**) and
`contra_close_spread_usd` (price, canonical **`$5.00`**).

**Mechanism.** On each bar, BEFORE step 3 (SL/TP resolution), the
engine computes the quantity-weighted-average entry of each side:

\[
\text{spread} = \overline{P}_{\text{short}} - \overline{P}_{\text{long}}
\]

If `spread >= contra_close_spread_usd`, the overlapping quantity is
netted out **simultaneously at one market price**, pro-rata on both
sides. A leg that is fully netted is closed; a partially-netted leg
keeps a residual with its **full multi-target geometry intact**.

**Why the price cannot manufacture a gain.** Both legs are booked at
the SAME exit price, so the price terms cancel exactly and the
pair's gross P&L is the locked spread, independent of where the
netting happened. Guard:
`tests/test_contra_close.py::test_netted_pair_pnl_is_independent_of_the_exit_price`.

**The fee model — two round trips, deliberately.** On a hedging
account each leg is its own position with its own entry and exit
spread, so closing both pays **two** round trips. The status quo
also pays two, so the netting does **not** change the fee count.
Charging one would be a per-trade windfall that flatters the feature.

> ### ⚠️ The threshold was re-picked from a CHUNKED measurement
>
> The `$1.0` threshold was chosen by the pair-level analysis, which
> found the monthly view unable to see whether the feature fires in a
> bear-shaped book. It is a legitimate, well-argued threshold.
>
> **But the continuous corpus disagrees with the chunked one about
> whether the feature helps at all** (see § 2 above): +$113.79
> chunked vs **−$137.15** continuous over the same months.
>
> **On that evidence the feature shipped OFF** (2026-10-24). On the
> continuous corpus with it ON: gross **+$570.78**, net **−$280.13**
> — better than OFF (−$340.34) in aggregate, but the *per-month*
> deltas disagree with the chunked ones, and the flat slice is
> negative in both views.
>
> ### ⚡ PROMOTED 2026-10-25 — ON at a $5.00 threshold
>
> (user instruction, 2026-10-25: "contra 5USD")
>
> The feature is now **ON** and the threshold moved **$1.0 → $5.00**.
> The threshold change is the *measured* part; the ON/OFF flip is the
> user's call.
>
> **Isolation A/B on the continuous corpus** (2024-01-01 →
> 2026-07-16, 14-day warmup prefix, one continuous execution;
> `runtime/diagnostics/config_iso_2026_10_25.py`):
>
> | cell | legs | /day | gross | fees | net | gross/leg |
> |---|---|---|---|---|---|---|
> | base_canonical | 1,692 | 1.83 | $510.58 | $850.92 | −$340.34 | $0.3018 |
> | **contra_5** | 1,696 | 1.83 | **$582.71** | $850.91 | **−$268.20** | **$0.3436** |
> | sl_max_8 | 1,705 | 1.84 | $905.43 | $850.94 | **+$54.49** | $0.5310 |
> | sl_max_8_contra_5 | 1,713 | 1.85 | $751.77 | $850.92 | −$99.14 | $0.4389 |
>
> **The feature HELPS in isolation: +$72.13 of gross and +$72.14 of
> net at a constant leg count (+4 legs).** It is a risk-management
> feature, not an alpha source — it converts a probable stop-out into
> a certain small gain.
>
> A `$5.00` threshold is **MORE CONSERVATIVE** than the `$1.0` the
> pair-level ladder picked: it fires only on WIDE locks, so it banks
> fewer, larger spreads. The ladder ($0.5 / $1.0 / $2.0 / $4.5 /
> $6.5) is in `bt/optimal_config.py`; $5.0 sits between the $4.5 and
> $6.5 rungs.
>
> ⚠️ **The threshold was set BY INSTRUCTION, not re-picked from a
> fresh pair-level analysis.** If it is ever revisited, re-run
> `contra_close_actual_vs_simul` and `contra_close_stats` first, and
> check the FLAT slice — it was −$11.78 at the old $4.50 threshold.
>
> ⚠️ **On the WIDE book the feature fires a LOT.** With
> `sl_max_usd=8.00` the stop is 3.2× further away, so positions
> overlap far more often: on the current 51,075-leg book
> `contra_close` exits **17,211 legs (34%)** for **+$38,763.86** of
> gross. It is now a core exit path, not a rare event — which is also
> why the wide book's short side is still net-negative.

**The requested "incoming-opposite" semantics are NOT implemented, and
the measurement says they should not be.** Under that design the book
would have seen **30 incoming-opposite events** in 15 months — 2.7×
more than the passive scan — but **73% of those events land in UP
months**, i.e. it is *more* direction-tilted per event, and it would
routinely pay two round trips ($0.94) to bank a median **$1.93**
lock. The threshold is precisely the thing that keeps the feature on
only the wide locks — the ones worth two round trips. Removing it is
a loss of the only filter the feature has.

Reproduce:

```bash
python -m runtime.diagnostics.contra_close_actual_vs_simul
python -m runtime.diagnostics.contra_close_stats
python -m runtime.diagnostics.contra_close_risk
python -m runtime.diagnostics.contra_close_firing
```

---

## Things that were measured and found DEAD

Recording these so they are not re-litigated. Each was measured, not
assumed.

### Market structure event whitelist — DEAD

The trailing-5-event **signature whitelist** asked "is this exact
BoS/CHoCH history a trade I should take at all". **No.**

* The full 60-signature whitelist kept **ZERO trades** on every
  slice. The script prints "the whitelist improves held-out P&L"
  because `kept = 0` — the "+$23 improvement" is the P&L of *not
  trading*.
* The whitelist was empty **by construction**: the admission rule
  requires `n >= 15`, and after the vol gate the mean is **5.5 legs
  per signature**.
* A properly-powered rerun with disjoint fit/held-out slices showed
  the held-out kept trades have **aggregate t = −2.15** — they are
  significantly *loss-making*. The net improved by **+$2** while the
  **gross got $68 WORSE**: it dropped 150 legs, saved $70 of fees,
  and destroyed $68 of gross.
* It **destroys the decisive flat slice**: 2025-05 goes from **+$62
  net to −$22 net**.

### The sequence flip — DEAD

`runtime/diagnostics/sequence_flip.py` records its own verdict:
*"Tested: does NOT generalise, −$8 on held-out slices."*

### `structure_gate` — MEASURED, NOT PROMOTED

It improves gross but leaves the market-beta correlation untouched
(long +0.968 → +0.970), and promoting it would break 33 call sites
that build no `StructureState`. Locked by
`tests/test_regressions.py::test_structure_gate_stays_off_until_its_plumbing_is_safe`.

### `trade_sides = "long"` — MEASURED, NOT PROMOTED

Long-only turns −$74 into +$161 — but raises the market correlation
to **+0.464**, higher than the two-sided recipe's aggregate. A
long-only gold account has no defence in a sell-off. The negative
short alpha is a REAL finding and it is attacked where it is safe to
attack it — by gating the short side on market structure sequences,
so the side stays alive but only trades its own profitable sequences.

### HVN/LVN — the volume profile is INERT in the canonical recipe

Harness: `runtime/diagnostics/hvn_lvn_proof.py`. 6 monthly slices.

* The **placebo** test is decisive: shuffle the volume across price
  rows and destroy the volume↔price correspondence. If the profile
  carried information, the placebo must fall outside the real
  profiles' band. **It does not** — placebo gross $2,254 sits
  **inside** the $2,123–$2,396 band of the four real profiles.
* With the clamp removed, the real profile **loses to the placebo** on
  4 of 6 slices, and loses on **both flat-market slices**.
* The daily profile **reads the future**: `current_daily_vp()` returns
  the profile of the containing UTC day, built from that day's
  *entire* volume. Truncating the same day at noon shifts the POC by
  **$18.19**.
* Why it never reaches the P&L — three blockers: the **SL ceiling**
  clamps every stop to $2.50 (1 distinct stop distance across 18,608
  legs, so the HVN anchor is inert); the **TP is the swing cap**, not
  an HVN shelf (`multi_swing_edge` = 89.0% of legs); and **LVN is
  never read at all** (`lvn_zones_near` → 0 call sites).

> **✅ THE SL CEILING IS NO LONGER A BLOCKER (2026-10-25).**
> `sl_max_usd` was raised from $2.50 to $8.00, so the HVN anchor is
> LIVE again — the current canonical book has **8,445 distinct stop
> distances** (was 1). **The third blocker listed above is gone.**
> The other two stand: the TP is still the swing cap rather than an
> HVN shelf (`multi_swing_edge` = 89.0% of legs), and LVN is still
> never read (`lvn_zones_near` → 0 call sites).
>
> The VP-inertness finding was measured on the narrow book and has NOT
> been re-measured on the wide one. **Do not promote any VP knob on
> the strength of it** — and if you need to, the isolation A/B harness
> (`runtime/diagnostics/config_iso_2026_10_25.py`) shows how to
> measure one change at a time.

A leak-free profile is the **WORST** of the three nocap arms
(−$10,916 vs −$9,524). A causal profile is not a free upgrade here;
that is an honest signal, not a bug.

**Do not promote any VP knob on the strength of this.** The
prescription is the opposite of "tune the VP": either make the
profile causal AND let it actually set the stop, or accept that the
profile is a no-op and delete the HVN anchor from the SL path.

### The vol gate — a REAL improvement, and a lesson about sample size

`vol_state_min_range_norm` is the **first change in this
repo's history to move the verdict at all**. The gate admits a signal
only when the current 1m candle's range is ≥N× the trailing-60m
median — a volatility *expansion*, which is a directional commitment.
It removes the chop whipsaws that were absorbing the entire fee bill.

At **3.0**, measured on 15 monthly slices: the fee bill falls **97.5%**
($23,596 → $579) and gross **rises** ($1,830 → $205 on a 40×
smaller book — gross/leg rises 4.6×). That is the opposite of the
`sl_zone_mult` mechanism, where the win comes from paying fewer
fees.

> **⚠️ But 3.0 was ALSO the binding constraint on the whole recipe.**
> It admitted only **1.83 trades per calendar day** on the continuous
> corpus — one trade every ~13 hours. The 2026-10-25 trade-count
> target (30-70/day) re-paired it to **1.0**, which gives
> **55.10/day** and still keeps gross/leg at 1.49× the gate-off
> book's. The gate is still a QUALITY filter at 1.0; it is just no
> longer a filter that admits only the top ~2% of bars. See §
> "Promotions of 2026-10-25".

> **The lesson, and the reason the diagnostic exists.** A 6-slice
> sweep put threshold 3.0 at **+$69 net**. The 15-month sweep put it
> at **−$364**. Both numbers are correct for their samples — the
> single best month in the 6-slice sample was doing all the work.
>
> **A gate evaluated on 3–6 slices will find a threshold that
> "works". Validate on the whole corpus before believing it.**
>
> ⚠️ **And a threshold optimised for PER-LEG EDGE can quietly destroy
> the book's ACTIVITY.** The 3.0 threshold was picked on a per-leg
> gross metric, which improved monotonically as the book shrank — so
> "optimise gross/leg" and "produce a usable book" are **different
> objectives**. Measure the trade count alongside the edge, every
> time.

---

## Forward-leak bugs — fixed 2026-10-21 (NB94)

**Symptom:** 1-week / 1-month backtests looked fine; the 1-year run
emitted 264,666 signals and closed **0 trades**.

**Root cause:** two rolling-window statistics whose filters were
**missing their upper bound**, so they read FUTURE bars.

| Where | The bug | Impact |
| --- | --- | --- |
| `bt/_engine/swing_cap.py` `cap_for` | `mask = ts >= cutoff` (no `ts <= t_ns`) | the "24h window" ran to the end of the buffer — up to 435 days. TP cap inflated **18.7×** ($92.66 → $1,733.62), putting every TP out of reach. |
| `bt/_engine/rolling_p95.py` `_downsample_to_inner` | positional `reshape(-1, 3600)` assumes a gapless feed | 15.018% of 1y bars are >1s apart (weekend gaps), so 100% of "1h" buckets sheared. |

### The rule

> **Any rolling-window statistic that reads a buffer needs a TWO-SIDED
> mask.** A one-sided mask is a look-ahead whose severity scales with
> file length — invisible in unit tests, invisible on short validation
> slices, maximal on the long run.

Three corollaries:

1. **Rolling windows** → `lo <= t <= hi`. Never `t >= lo` alone.
2. **Bar aggregation** → bucket by **timestamp**, never by row
   position. Positional reshape assumes no gaps; real FX data has
   weekend/rollover gaps.
3. **Window statistics** → assert the result is **independent of the
   file truncation point**. See
   `tests/test_regressions.py::test_swing_cap_is_stable_across_file_length`.
4. **A fallback that silently degrades correctness is a bug, not a
   convenience.** The detector's resample fallback (positional
   bucketing) was reachable from three call sites — including the
   decisive beta diagnostic. It emitted a `RuntimeWarning` on every
   call and **nothing failed**. **A guard must live at the call site,
   not only in the callee.**

### Why short validation runs hid it

The leak is *position*-dependent: early-in-file and end-of-file probes
are accidentally correct. Only probes in the middle of a long file see
it. A 1-week file is short enough that every probe is an "edge" probe.

### Ruled out (measured, not assumed)

* p95 warmup gate — `is_warmup_complete` is True for **99.3786%** of
  bars; only the first 8 days are gated.
* ms/ns mixing — the NB91 timebase fix is intact, `assert_ns` passes.
* conflict policy — never reached (`n_dropped_opposite == 0`).

### Per-gate attribution counters

The engine counts where each signal dies, so this never needs
guessing again:

```
n_signals_emitted
 - n_dropped_vol_state
 - n_dropped_p95_warmup
 - n_dropped_p95_warmup_sniper
 - n_dropped_policy
== n_immediate_filled + n_sniper_filled
```

`diag["gate_breakdown"]` has the breakdown; `diag["signals_unaccounted"]`
must be **0**.

**The p95 cap is a TP-ONLY clamp.** It must never gate entries — only
tighten TP1/TP2. The warmup gate is a separate, explicit concern.

Full write-up: `docs/nb94_1w_vs_1y.md`.

---

## The 1m detector — three bugs fixed (2026-10-23/24)

The FVG detector moved from 1s to 1m on 2026-10-23, which changed the
median zone width from $0.47 to $1.04 (2.2×) and exposed three
defects that were invisible at 1s.

### Bug 1 — the resample bucketed by ROW POSITION, not by timestamp

```python
# WRONG — assumes a gapless feed: bar i is at second i
bucket = (np.arange(n) // resample_to_n_secs).astype(np.int64)
```

Real FX data has weekend/rollover gaps, so row position drifts away
from wall-clock time and every bucket boundary is wrong by the
accumulated gap. **15.018% of the 1y file's bars are >1s apart.**

The fix buckets by `floor((t − t0) / window)` on the actual
timestamps, and `times_utc_ns` is now **required** for a resample —
falling back to positional bucketing emits a `RuntimeWarning` rather
than silently corrupting every zone.

### Bug 2 — the invalidation walk ran at 1s while detection ran at 1m

The zone is DETECTED on the resampled cadence, but its invalidation
was evaluated on the RAW 1s arrays. Consequences:

* `invalidation_min_consecutive_bars=2` counted TWO **1-second** bars,
  not two 1-minute bars;
* `body_only_invalidation` tested 1s candle bodies, so a single 1s
  wick through the zone edge counted as a full-body pierce.

The walk now slices the **resampled** arrays and maps the result back
to 1s indices via `scan_index_map`.

### Bug 3 — supersession preempted inversion (THE BIG ONE)

**This is why the sniper count was 4.2/day against a ~30/day
expectation.**

Supersession was applied DURING detection (phase 2), and the
mitigation/inversion walk runs AFTER all detection (phase 3). So
during the supersession scan `z.inverted_bar` was **always −1**,
which made the escape hatch (`if z.inverted_bar >= 0`) **structurally
unreachable**.

**The fix defers the DECISION, not the record:**

* During detection, supersession **records** `superseded_bar` but does
  **not** kill the zone.
* The walk runs on every zone (superseded included), capped at
  `superseded_bar + 1`.
* **After** the walk, a zone is DEAD if it was superseded and never
  inverted; it stays **LIVE** if it inverted *before* the superseding
  zone formed. That is a legitimate iFVG — the sniper.

| slice | zones | superseded | inverted | snipers/day | vs target |
|---|---|---|---|---|---|
| 2026-01 | 5,189 | 94.4% | 1,270 | **41.0** | 137% |
| 2026-03 | 6,112 | — | 1,442 | **46.5** | 155% |
| 2025-05 | 4,583 | — | 1,119 | **36.1** | 120% |
| 2025-01 | 2,475 | — | 723 | **23.3** | 78% |

### Bug 1 was only HALF fixed

The 2026-10-23 fix changed `bt/_engine/ict_signals._resample_bucket_index`
to bucket by timestamp, and made `times_utc_ns` **required** — but
**three callers were never updated** to pass it:

| Caller | Impact |
| --- | --- |
| `runtime/diagnostics/directional_beta.py` | **The DECISIVE diagnostic** — computed on a DIFFERENT zone set than the sweep driver produced. |
| `runtime/diagnostics/cap_and_atr_audit.py` | The raw-TP decomposition. |
| `runtime/diagnostics/probe_vp_scale_inference.py` | The VP shape coverage sweep. |

The detector warned on every slice, but **nothing failed**. A warning
is not a guard.

**How badly the zone sets diverge** (1-month 2025-01, same bars, same
detector kwargs, only the bucketing differs):

| bucketing | zones |
| --- | --- |
| positional (wrong) | 2,136 |
| timestamp (correct) | **2,475** |

| overlap | count |
| --- | --- |
| shared (same price band + direction) | **194** |
| only in the positional set | 1,942 |
| only in the timestamp set | 2,280 |

**Only 194 of 2,475 zones (7.8%) are common to both sets.** The two
universes are effectively disjoint. The verdict did not change, but
the numbers moved materially.

> **Rule: when a warning fires in a diagnostic that produces a
> PROMOTION VERDICT, the diagnostic is not trustworthy until the
> warning is gone.** A verdict computed on a differently-bucketed
> universe than the sweep driver is not a verdict about the recipe.

**Guard:** `tests/test_regressions.py::test_every_detect_fvg_call_site_passes_times_utc_ns`
walks every non-test `detect_fvg` call site via AST.

---

## Dead knobs — verified 2026-10-20

Knobs that look live but cannot change the result. **Do not sweep
these; they will silently no-op.**

| Knob | Why it is dead |
| --- | --- |
| `commission_per_lot_usd`, `spread_per_oz_usd` (engine surface) | `bt.vp_engine._FOREX_ONLY_KWARGS` strips them before the engine call. Their effect is folded into `taker_fee_bps` at build time. |
| `sl_atr_mult` | **REMOVED from the sweep surface 2026-10-22.** Doubly dead: canonical `0.0` *and* no `atr=` array is ever passed. A 1s-cadence ATR is **$0.098 median**, so expressing a $5 stop needs `mult ≈ 51` — a constant with no cross-regime meaning. |
| `poc_buffer_usd` | Only read by `compute_structural_sl` when `sl_mode == 'fvg_or_poc'`. Canonical is `hvn_lvn`. |
| `hvn_buffer_usd` | **LIVE** — canonical `sl_mode='hvn_lvn'` reads it (the offset past the shelf edge). |
| `partial_close_pct` | Was hardcoded to 0.5 inside `compute_structural_tp`. **Fixed** — now a real engine parameter. |
| `strict_wick_required` | Ships **OFF**. The BTC-calibrated floor ($0.68) matches **0.00%** of XAUUSD 1s windows — enabling it would delete the whole strategy. It is **real and sweepable**, just off. |
| `fvg_body_only_*` | Was silently inert (resolved to the dataclass default `False` and the gate never ran). Now live and wired from the canonical constants. |

`tests/test_regressions.py::test_all_engine_kwargs_reach_the_engine`
and `::test_engine_accepts_every_allowlisted_knob` guard the engine
surface against re-drift.

---

## Known engine bugs and their fixes (chronological)

### Two `sl_mode` branches ignored the SL floor and/or ceiling (2026-10-24)

| branch | floor (`sl_min_usd`) | ceiling (`sl_max_usd`) | before |
|---|---|---|---|
| `hvn_lvn` (canonical) | applied | applied | OK |
| `fvg_only` | **MISSING** | applied | partial |
| `fvg_or_poc` | **MISSING** | **MISSING** | **broken** |

`fvg_or_poc` returned its raw anchor level directly, so it ignored
BOTH bounds — a `sl_mode` sweep would have shown a `$19.50` stop in
that cell while its two siblings sat at `$2.50`.

> **Rule: a bound that is enforced in one branch of a switch must be
> enforced in every branch.**

Guard: `tests/test_smoke.py::test_every_sl_mode_honours_the_floor_and_the_ceiling`.

### The callback path crashed on `VolState` (2026-10-25)

`on_bar_callback` called `vol_state.items()`, but the canonical config
hands the engine a **`VolState` object** (which has `__getitem__` /
`get` but no `items()`). The callback raised `AttributeError` on the
FIRST bar.

It went unnoticed because `on_bar_callback` had **zero production
callers** — the branch was dead code. Same class of defect as § "Bug
1 was only HALF fixed": **a guard that lives in the callee but was
never exercised by any caller.**

Guard: `tests/test_contra_close.py::test_on_bar_callback_survives_a_vol_state_object`.

### The `contra_close` branch dropped `signal_bar` (2026-10-25)

**This one is a silent data-corruption bug, and it only became
visible when the book got wide.**

`ClosedTrade.signal_bar` **defaults to `0`**. Every exit path in the
engine passes its leg's own signal bar — except the **`contra_close`
netting branch**, which built a `ClosedTrade` without the field. So
every netted leg reported `signal_bar == 0`, i.e. "this trade signalled
at the very start of the corpus".

| book | legs | `contra_close` legs | legs with `signal_bar == 0` |
|---|---|---|---|
| narrow (vol gate 3.0) | 1,692 | ~93 | ~93 (5.5%) |
| **wide (vol gate 1.0)** | **51,075** | **17,211** | **17,211 (34%)** |

On the narrow book it was a rounding error. On the wide book it
corrupted the ledger's **signal-time index** for a third of the book:

* the mean pre-fill window read **9,490,055 bars** (23% of the whole
  corpus) instead of ~4,000;
* 17,200 legs showed a pre-fill window of up to 41M bars — the entire
  corpus — when their real wait was a few thousand bars;
* the random-entry control's confound measurement was computed off
  the corrupted index.

The fix is one line — pass `signal_bar=_ct.signal_bar` — plus the
guards. After the fix the mean pre-fill window is **4,659 bars**, and
the netted legs' pre-fill mean (5,574) matches the non-netted
baseline (4,194).

> **Rule: a field with a DEFAULT is a silent-corruption field.**
> `ClosedTrade` has `signal_bar: int = 0`, `zone_width: float = 0.0`,
> `zone_low`/`zone_high: float = 0.0`. Any construction site that
> forgets one produces plausible-looking numbers that are simply
> wrong — no crash, no warning. **Walk every construction site and
> assert the required fields are passed.**
>
> The second half of the rule: **a bug's blast radius scales with
> how often its code path runs.** The same omission was harmless at
> 93 legs and corrupted 17,211. When a knob widens the book, re-run
> the invariants — the bug was always there, only the exposure
> changed.

Guards: `tests/test_signal_bar_completeness.py` — a source-walking
test that every `ClosedTrade` construction site passes `signal_bar`,
plus two ledger-level end-to-end checks (`signal_bar == 0` count, and
the pre-fill-window backstop).

---

## Performance — the 1y detector and engine

### Bottleneck A2 — the 1m resample aggregation O(buckets × bars) (FIXED 2026-10-25)

The detector's resample step aggregated the 1s bars into 1m bars
with one **full-length mask per bucket**:

```python
for ub_idx, ub in enumerate(unique_buckets):
    mask = bucket == ub   # <-- full-length comparison, PER BUCKET
    scan_high[ub_idx] = float(high[mask].max())
```

On the 1y corpus: 687,228 buckets × 41,233,730 bars = **28.3
TRILLION** element-ops → **~8 hours**. It was invisible on the
monthly slices (the 1-month corpus is 1/30th the size) and
**completely invisible to the byte-identity checks**, which all ran on
1-month data. The 1y detector had never actually been run end-to-end
at 1m cadence.

**The fix** — `np.maximum.reduceat` / `np.minimum.reduceat`. This is
only valid because `_resample_bucket_index` returns
`floor((t - t0) / window)` on a **non-decreasing** timestamp series,
so the bucket index is **monotonic** and every bucket is a
**contiguous segment** of rows.

**Measured:** 4M bars aggregate in 0.037s (108M bars/s) → the 1y
corpus's aggregation step is **0.4 s** instead of **~8 hours**.

**Equivalence is proven, not assumed:**
`tests/test_detector_cadence.py::test_resample_aggregation_matches_the_loop`
compares the vectorised form against the ORIGINAL loop on both gapped
and gapless synthetic data, and
`::test_resample_bucket_index_is_monotonic_so_reduceat_is_valid`
asserts the monotonicity/contiguity property the fix depends on.

> **Rule: a per-bucket mask over a full-length array is O(n²).** When
> the bucket index is monotonic, use `reduceat`. When it is not, sort
> first — never mask per bucket.

### The other detector bottlenecks (all fixed 2026-10-21)

| # | Phase | Root cause | Before | After |
|---|---|---|---|---|
| A | Python bar loop | 41M Python iterations | 100+ s | **0.06 s** |
| B | Supersession O(Z²) | scan all prior zones per new zone | 15.7 h | ~0.3 s |
| B2 | Unbounded live-candidate list | never-overlapped zones accumulate | ~20 min | **145.2 s** (1y) |
| C | Mitigation walk to EOF | non-sup zones walk to `n_1s` | 10+ min | ~25 s |
| D | Structure invalidation O(N×Z) | all bars × all zones | unknown | <1 s |
| F | VP engine O(n_bars × open) | unbounded book (mean 124 open) | ~hours on 1y | **~51 s** (1y) |

**B2's fix** was a **price-bucket spatial index**: the supersession
predicate is a *price-range overlap* test, so a zone can only be
superseded by a candidate whose price range overlaps it. This is
**exactly equivalent** — the overlap test is untouched, only the set
of zones considered is narrowed. Verified byte-identical on 1-month
data.

**F's fix** was three changes together: gate the per-bar book scan
behind its actual consumers (P2), inline `ot.qty_of(qty_btc)` (P3),
and compact `open_trades` in place (P4). The book is now capped at 20
by D3, and the engine runs at ~71,000 bars/s.

> **Re-measured 2026-10-25 on the current recipe (1m detector + vol
> gate): ~730,000–790,000 bars/s, max open depth 2, 1y engine cell
> 51.0 s.** The ~71,000 bars/s above is the 1s-detector figure.

### Memory: where the 1y working set actually goes (2026-10-22)

| Structure | Size | Grows during the run? |
| --- | --- | --- |
| bars frame (parquet columns) | 2,202.1 MB | no — static |
| engine float64 arrays (o/h/l/c + time ns) | 1,572.9 MB | no — static |
| zone list (1.82M `FvgZone` objects) | 647.3 MB | no — static |
| closed trades (80,833 `ClosedTrade`) | 46.7 MB | **yes** — the only unbounded one |
| equity curve (float64, 41M) | 314.6 MB | no — disabled above 8M bars |
| **total accounted** | **4,783.7 MB** | |

**The answer to "why does memory grow if backtests are sequential and
zones expire?"** — it mostly *doesn't*, and zone expiry is irrelevant
to it:

1. **Zones do NOT expire from memory.** `detect_fvg` returns a list
   of 1.82M `FvgZone` dataclass objects. The engine never deletes
   them; a zone is "expired" logically but the object stays alive.
2. **The bars frame is the floor, not the growth.** 1.98 GB is
   irreducible at 41M rows × 6 columns × 8 bytes.
3. **Only the closed-trade list grows without bound** — 606 bytes
   per trade.
4. **The cache does not help memory at all.** It helps *time*, but the
   artifacts are **unpickled into RAM** for every cell.

`load_bars` used to build a redundant 330 MB `volume` column.
Pandas column assignment always copies, so it is now a **rename**
(`df.rename(columns={"tickv": "volume"})`) — a label-only change,
zero extra bytes.

### Numba: considered, not needed (yet)

A Numba kernel for the O(n_bars × n_open) loop was the original
plan. It is not the right lever any more. After D2 + D3 + P2–P4 the
book depth is capped at 20 and the scan runs at ~71k bars/s on 1y.

> **Re-measured 2026-10-25 — Numba is even less needed.** The 1m
> detector plus the vol gate moved the 1y bar rate to
> **~730,000–790,000 bars/s** and the **measured max open depth to
> 2**. A 1y engine cell is **51.0 s** and a 60-cell 1y sweep is
> **~51 min** cold (or **~60 min** with the artifact cache warm).
> Numba would optimise a loop that is already ~99% idle.

If the sweep matrix grows past ~60 cells, revisit Numba. Below that,
the effort is better spent on the knobs.

---

## Operational gotchas

### Diagnostic logging

The engine's bar loop reports progress to **stderr** every 250,000
bars:

```
[vp_engine] bar=1250000/41233730 elapsed=51.9s rate=24091/s open=24
```

The `open=` field is the key diagnostic — it is the multiplier on the
total runtime, because the per-bar book scan is
`O(n_bars × n_open)`. A run whose `open=` count is flat and low is
fast; a run whose `open=` grows into the hundreds is the wall.

Run with `python -u` so the output is unbuffered.

### Memory contention kills 1y runs silently

**Observed 2026-10-21.** Two `cached_sweep` 1y runs were accidentally
left running at the same time. Each holds ~6.4 GB resident. Together
they exhaust physical memory and the effect is two-fold:

1. **The bar loop stalled for 2.3 hours.** The `elapsed` counter
   jumped discontinuously:

```
bar=14000000 elapsed= 1036.6s rate=13506/s  <- healthy
bar=14250000 elapsed= 9341.1s rate= 1526/s  <- 2.3 h stall
```

2. **The run was then killed outright** before finishing, with no
   error in the log.

**Rule: never run two 1y `cached_sweep` processes concurrently.**
Check with:

```bash
powershell -NoProfile -Command \
 "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" |
 Select-Object ProcessId,CommandLine | Format-List"
```

**Distinguishing the failure modes** by the progress logger:

| Symptom | Cause |
| --- | --- |
| No `[detect_fvg] detect:` lines | still in `load_bars` / parquet read |
| `detect:` lines stop mid-way | detector OOM or a `detect_fvg` hot spot |
| `elapsed` jumps discontinuously | machine-level memory contention |
| `vp_engine` lines advancing, slow | book depth — expected, not a bug |
| Process gone, log truncated | OOM kill — check for a duplicate run first |

---

## Files

```
bt/
 __init__.py top-level imports of duplicated engine
 _engine/ duplicated engine source (standalone)
 ict_signals.py FVG / iFVG detectors
 ict_strategy.py PendingSignal (the ONLY survivor of the recipe file)
 market_structure.py BoS / CHoCH / order blocks
 run_report.py run-log writer
 vp_multi_target_engine.py NB74/NB75/NB83 multi-target + D2 + D3
 volume_profile.py daily VP rolling aggregator (NB80)
 vol_state.py NB85 vel gate (with look-ahead fix)
 swing_cap.py NB72 daily-swing TP cap
 rolling_p95.py NB91 rolling p95 TP cap
 timebase.py NANOSECONDS conversion (single source)
 optimal_config.py THE canonical config + the 7 builders
 rolling_p95.py wrapper over bt._engine.rolling_p95
 volume_profile.py wrapper over bt._engine.volume_profile
 vol_state.py wrapper over bt._engine.vol_state
 swing_cap.py wrapper over bt._engine.swing_cap
 vp_engine.py Multi-target VP engine wrapper
 risk.py Margin + cost + pnl math
 kelly.py Fractional Kelly sizing (NB81)
 leverage.py 100x leverage exposure math
runtime/sweeps/
 cached_sweep.py THE sweep driver (single + multi-knob grid)
 cost_estimate.py The PRE-FLIGHT cost estimate + the --i-know gate
runtime/diagnostics/
 ── THE EVALUATION HARNESS (use these) ───────────────────────────
 leg_ledger.py THE corpus-wide per-LEG record (continuous run)
 leg_buckets.py THE leg-level evaluation (side x outcome buckets)
 leg_random_entry.py RANDOM-ENTRY control (CONFOUNDED — see § 2b)
 zone_shuffle_placebo.py ZONE-SHUFFLE placebo — the decisive test
                          ⚠️ PROMPT-ONLY — never run unprompted (§ rule 5)
 volgate_trade_count.py THE TRADE-COUNT LADDER (30-70/day target)
 config_iso_2026_10_25.py Isolation A/B for the 2026-10-25 promotions
 chunk_vs_continuous.py Chunked vs continuous on the same window
 chunked_vs_continuous_months.py Per-month chunked vs continuous
 warmup_prefix_effect.py What the warmup prefix changes, per leg
 payoff_breakdown.py Payoff geometry
 validate_candidate.py Candidate validation harness
 ── THE TA EVALUATION HARNESS (the episode metric) ────────────────
 ta_signal_episodes.py The EPISODE metric (signal-to-signal) + its 2 placebos
 ta_1m_signal_sweep.py The TA scoreboard over all 64 TA cells
 ta_fixed_side_placebo.py THE FIXED-SIDE placebo — the decisive control
 ta_1m_core.py The TA signal builders (all 12 families) + the old traded metric
 ── THE TA GATE (post-hoc gate on the canonical book) ─────────────
 ta_gate.py THE GATE — gate open at the signal bar, close on signal change
  2360| ta_gate_sweep.py The TA gate sweep — 74 cells, 14 families, 2 placebos each
 ta_gate_validate.py PROVES the gate reproduces the canonical ledger BIT-EXACTLY
 ta_gate_summary.py The read-out: decay by family, MR vs trend, placebo, excess
 ta_gate_probe_index.py The probe that fixed the bar-index (1s grid) semantics
 ta_gate_patch_baseline.py Fills the baseline's drift/excess from the ledger
 ── DIAGNOSTICS (per-feature, mostly historical) ─────────────────
 directional_beta.py Is the P&L timing skill or market direction?
 short_side_ablation.py Is the short side a pure drag?
 side_gate_equivalence.py PROVES the engine side gate == post-hoc filter
 per_side_sequence_filter.py Per-side (side, last-event) buckets
 per_side_sequence_paired.py The paired delta (drop-bucket scoring)
 per_side_sequence_pooled.py The pooled control
 per_side_sequence_hypotheses.py Hypothesis table
 seq_signal_vs_fill_bar.py SIGNAL bar vs FILL bar (the trap)
 seq_drop_set_validate.py Walk-forward validation of the drop set
 seq_drop_set_equivalence.py PROVES the engine drop set == post-hoc filter
 contra_close_ab.py Hedge netting A/B
 contra_close_why.py WHY the netting helps (leg-level decomposition)
 contra_close_broad.py Multi-month broad sweep
 contra_close_firing.py Is the feature FIRING when it should?
 contra_close_actual_vs_simul.py Pair-level counterfactual
 contra_close_stats.py Sign test / Wilcoxon / bootstrap on the pairs
 contra_close_risk.py Risk view (maxDD, gross/leg vs fee/leg)
 contra_close_threshold_fine.py Fine threshold ladder for the netting
 hvn_lvn_proof.py Is the volume profile inert?
 zone_lifecycle.py Zone detection lifecycle
 zone_geometry.py Zone shape geometry
 zone_knob_sweep.py Zone detector knob sweep
 sl_ceiling_sweep.py / sl_ceiling_sweep8.py The SL ceiling
 sl_width_breakdown.py Where the stop width comes from
 sl_anchor_breakdown.py Which anchor set the stop
 capture_scaling.py The capture-scaling grid
 combined_screen.py The joint (gate x ceiling) screen
 vol_gate_broad.py / vol_gate_multislice.py The vol gate
 vol_entry_gate.py The vol gate, entry-side only
 cadence_sweep.py The detector cadence grid
 tp_reachability.py / tp_chain_trace.py / tp_metric_identity.py TP geometry
 probe_*.py One-off probes (knob liveness, VP scales, SL A/B, hold time)
 sequence_whitelist*.py DEAD — the signature whitelist
 sequence_flip.py DEAD — the sequence flip
 structure_sequences.py DEAD — structure sequence scoring
 audit_config_surface.py The config audit (duplicate knobs, dead knobs)
 audit_constant_reachability.py STRICT constant-reachability audit
 verify_diag_scale.py Proves the diagnostics run at canonical size
```

> **The Files list is a MAP, not an inventory.** The evaluation
> harness is the top block — `leg_ledger` + `leg_buckets` + the
> placebos. Everything below it is per-feature history. A feature's
> diagnostic staying in the tree does **not** mean the feature is
> live; the canonical config table is the authority on that.

### Deleted in the 2026-10-22 dead-code sweep

Nothing may import these again; `tests/test_no_dead_imports.py`
enforces that.

| Deleted file | Why |
| --- | --- |
| `bt/params.py` | `FocusedParams` — a phantom "no-defaults dataclass" that was **never constructed in production**. |
| `bt/vp_config.py` | Duplicate of the engine-kwargs surface in `bt/optimal_config.py`. |
| `bt/structural_sl_tp.py` | Duplicate of the engine's own `compute_structural_sl` / `compute_structural_tp`. Its geometry had already **diverged** from the engine's. |
| `runtime/sweeps/single_sweep.py` | Reported empty metrics. |
| `runtime/sweeps/run_dual_sweep.py` | Hardcoded detector knobs that disagreed with the canonical recipe. |
| `runtime/sweeps/diag_swing_cap.py` | One-off diagnostic, superseded by the regression test. |
| `runtime/sweeps/diag_engine_memory.py` | One-off diagnostic, superseded by the memory note above. |

---

## What gets swept

The sweep surface is **the seven builders**. Sweep drivers vary ONE
field at a time and re-call the builder that owns that surface:
`build_engine_kwargs`, `build_vp_kwargs`, `build_detector_kwargs`,
`build_swing_kwargs`, `build_p95_kwargs`, `build_live_p95_kwargs`,
`build_structure_kwargs`.

The override allowlists are the complete sweep surface. A name on
none of them raises `KeyError` **before** any cell runs — it can
never become a silent no-op.

| Builder | Target | Vocabulary |
| --- | --- | --- |
| `build_engine_kwargs(**over)` | `run_vp_sniper_backtest` | the engine's own parameter names |
| `build_vp_kwargs(**over)` | `build_volume_profiles` | the engine's own parameter names |
| `build_detector_kwargs(**over)` | `detect_fvg` | the detector's own parameter names |
| `build_swing_kwargs(**over)` | `build_daily_swing` | **canonical** names → translated |
| `build_p95_kwargs(**over)` | `build_rolling_p95_from_bars` | **canonical** names → translated |
| `build_live_p95_kwargs(**over)` | `make_live_rolling_p95` | **canonical** names → translated |
| `build_structure_kwargs(**over)` | `detect_market_structure` | **canonical** names → translated |

The canonical SL/TP geometry that the VP engine actually uses is:
SL = the `sl_mode='hvn_lvn'` HVN shelf behind entry, clamped into
`[sl_min_usd, sl_max_usd]`; TP = the NB74 multi-target pair, then
capped by `swing_cap` and `p95_cap`.

> **⚠️ CHANGED 2026-10-25 — the SL anchor is LIVE again.**
> This section used to read "because `sl_min_usd == sl_max_usd ==
> 2.50`, the HVN SL anchor is INERT". That was true at the old
> ceiling. `sl_max_usd` is now **$8.00** (with `sl_min_usd` still
> $2.50), so the effective stop distance ranges over
> **[$2.50, $8.00]** and the current book has **8,445 distinct stop
> distances**.
>
> **Consequence: sweeping `sl_mode` / `sl_nth_hvn_n` /
> `hvn_buffer_usd` will now MEASURE SOMETHING.** That is new — those
> sweeps were previously dead at the canonical ceiling. Re-measure
> them, and re-measure them **together with the vol gate**, which was
> re-paired from 3.0 to 1.0 on the same day.

---

## NB history (the lineage of what's been promoted)

| NB | What | Promoted on forex? |
| --- | --- | --- |
| NB72 | Daily-swing TP cap (`swing_cap_pct=0.25`, 1-day window) | YES (engine surface) |
| NB74 | HVN1 partial TP (`partial_close_pct=0.50`) | YES (engine surface) |
| NB75 | Zone-mult remainder TP | YES (NB82 path) |
| NB76 | Fee standardisation | YES (forex baseline) |
| NB82 | Zone-anchored SL/TP | partial |
| NB83 | Scale-invariant structural SL | engine surface YES |
| NB52 | c-delta (`fvg_body_only_*`, `strict_wick`) | **PARTIAL** — body-only gates now live; strict-wick is real but ships DISABLED |
| NB85 | vol_state gate | **PROMOTED** as `vol_state_min_range_norm=3.0` → **re-paired to 1.0 on 2026-10-25** for the 30-70 trades/day target |
| NB86 | `conflict_policy` | YES (canonical `stack`) |
| NB89 | `max_same_dir_open=20` | PROMOTED 2026-10-22 (D3 fix) |
| NB91 | Rolling p95 TP cap | IMPLEMENTED — **but SHADOWED by NB72** on 100% of trade probes, so it currently measures nothing |
| NB94 | Forward-leak fixes (swing cap + p95 downsample) | FIXED 2026-10-21 |
| **NB95** | **D1 fee 2× fix, D2 resting entry orders, D3 bounded book, P2–P4 engine perf** | **ENGINE FIXES PROMOTED.** The recipe's P&L was explained by market direction — see `docs/nb95_beta_proxy.md` |
| **NB96** | **Per-side sequence drop set, vol gate, contra close / hedge netting** | **PROMOTED:** vol gate + `{"short\|c","short\|C"}` drop set. **`contra_close` SHIPPED OFF — then PROMOTED ON 2026-10-25 at a $5.00 threshold** |

---

## Promotions of 2026-10-25 — the trade-count target (30-70 trades/day)

> **The instruction:** *"the vol gate reduces trade count too much. we
> aim for 30-70 trades a day"*, plus *"run sl min 2.5, sl max 8.0 and
> contra 5USD"*.

Three knobs changed together. Two of them are **PAIRED** and must
always be re-measured as a pair: `sl_max_usd` and
`vol_state_min_range_norm`.

### The trade-count ladder

Measured on the **continuous** corpus (2024-01-01 → 2026-07-16,
14-day warmup prefix, ONE continuous execution — not month chunks),
with the requested `sl_min=2.50 / sl_max=8.00 / contra=$5.00`
settings. `runtime/diagnostics/volgate_trade_count.py`:

| thr | legs | /calendar-day | /trading-day | gross | fees | net | gross/leg | in band? |
|---|---|---|---|---|---|---|---|---|
| 0.0 | 100,008 | 107.88 | 126.43 | $9,612.27 | $34,233.51 | −$24,621.24 | $0.0961 | ❌ over |
| 0.5 | 95,338 | 102.85 | 120.68 | $8,197.54 | $32,995.30 | −$24,797.76 | $0.0860 | ❌ over |
| **1.0** | **51,075** | **55.10** | **64.65** | **$7,312.96** | $19,758.46 | −$12,445.50 | **$0.1432** | ✅ **mid-band** |
| 1.25 | 30,923 | 33.36 | 39.54 | $5,284.96 | $13,017.27 | −$7,732.31 | $0.1709 | ✅ bottom edge |
| 1.5 | 18,981 | 20.48 | 24.59 | $5,267.46 | $8,448.18 | −$3,180.72 | $0.2775 | ❌ under |
| 2.0 | 7,373 | 7.95 | 9.74 | $1,647.84 | $3,536.14 | −$1,888.31 | $0.2235 | ❌ under |

**`1.0` is the canonical value** — the only threshold that lands
mid-band on BOTH measures (55.10/calendar-day, 64.65/trading-day).

### The isolation A/B

Each requested change measured on its own, same corpus, same
warmup. `runtime/diagnostics/config_iso_2026_10_25.py`:

| cell | legs | /day | gross | fees | net | gross/leg |
|---|---|---|---|---|---|---|
| base_canonical | 1,692 | 1.83 | $510.58 | $850.92 | −$340.34 | $0.3018 |
| sl_max_8 | 1,705 | 1.84 | $905.43 | $850.94 | **+$54.49** | $0.5310 |
| contra_5 | 1,696 | 1.83 | $582.71 | $850.91 | −$268.20 | $0.3436 |
| sl_max_8_contra_5 | 1,713 | 1.85 | $751.77 | $850.92 | −$99.14 | $0.4389 |
| sl_max_8_contra_5_gate_1 | 51,075 | 55.10 | $7,312.96 | $19,758.46 | −$12,445.50 | $0.1432 |

**Two findings worth keeping:**

1. **The SL ceiling alone takes the canonical book from −$340.34 to
   +$54.49 NET** — the first net-positive continuous-corpus result
   in this repo's history. At $2.50 the stop was pinned on EVERY leg
   (1 distinct stop distance across the book), so the HVN SL anchor
   was **inert** and the geometry was a fixed 1:$8 bet with no
   structural content. At $8.00 there are **8,445** distinct stop
   distances and the anchor is live again.

2. **The trade-count target costs a lot of edge.** 51,075 legs is
   30× the book, and gross/leg falls from $0.3018 to $0.1432 —
   below the $0.3869 measured round trip. **The book is deeply
   net-negative at the target volume.** That is the price of the
   instruction, stated plainly rather than buried.

### What this means for the rest of the file

**§ 2b (drift/excess) and § 2c (the zone shuffle) were measured on
the NARROW book** — vol gate 3.0, 1,692 legs, `sl_max_usd=2.50`,
`contra_close` OFF. Their numbers ($510.58 gross, −$155.00 excess,
−$2,398.07 placebo) are **still correct for that book**, and they
still describe the recipe's structure. But the CANONICAL book is now
the wide one, and it has a different gross/leg, a different fee
ratio, and a much larger `contra_close` contribution.

> **Rule: when a change to the canonical recipe lands, every number
> in this file that was measured on the OLD recipe must be labelled
> with the recipe it belongs to.** The two books live side by side
> and quoting them interchangeably is the fastest way to make this
> file wrong.

### The new canonical book, scored

`python -m runtime.diagnostics.leg_buckets` on the current canonical
ledger (51,075 legs):

| metric | value |
|---|---|
| legs | 51,075 |
| trades/calendar-day | **55.10** ✅ |
| gross | +$7,312.96 |
| fees | −$19,758.46 |
| **net** | **−$12,445.50** |
| gross/leg | $0.1432 |
| fee/leg | $0.3869 |
| gross/leg ÷ fee/leg | **0.37×** ❌ |
| hit rate | 49.4% |
| long side | 35,855 legs, +$8,493.75, hit 50.6% |
| short side | 15,220 legs, −$1,180.79, hit 46.4% |
| drift | +$11,393.03 |
| **excess** | **−$4,080.07 (t = −15.63)** |
| `contra_close` exits | 17,211 legs (34%), +$38,763.86 |

The timing component is **still significantly negative** on the wide
book — the level-not-timing finding survives the change in scale.

---

## The TA GATE — MEASURED 2026-10-25, NOT PROMOTABLE

> **The rule:** *"trades must align with the TA's signal (gate open),
> and upon signal change close open trades."* Measured on the
> canonical wide book (51,075 legs) across **74 TA cells / 14
> families**, with **two placebos per cell**. Full write-up:
> `docs/ta_gate_sweep_2026_10_25.md`.

### The TRADE-NUMBER DECAY — the headline

| family | kept% (range) | decay (range) |
|---|---|---|
| **momentum / trend** (47 cells) | 0.0–52.0% | **48.0–100%** (median 81.4%) |
| **mean reversion** (27 cells) | 0.0–33.4% | **66.6–100%** (median 99.5%) |

**The MR gates are far more destructive** — the median MR gate kills
**99.5%** of the book against the trend family's 81.4%. The reason is
structural: the MR oscillators **abstain** in the middle of their
range, and **abstention is a rejection** (the gate needs the signal to
equal the trade's side). The trend families are directional on almost
every bar, so they reject only on the flips. `vwap_revert` is the MR
exception — it is *signed* rather than abstaining, and it has the
smallest MR decay (66.6%).

### Three findings that kill the feature

1. **Nothing survives as a book.** Only **9 of 74** cells have a
   surviving book that covers the round trip — and every one of them
   has a **decay ≥ 95.7%**. The widest gate that pays keeps **653 of
   51,075 legs (98.7% decay)** and earns **+$51** over 2.7 years.

2. **⚡ The EARLY CLOSE DESTROYS THE SURVIVORS.** **Every one of
   those 9 cells turns NET-NEGATIVE once the early close is applied**
   (the best gate+close net across all 74 cells is **−$9.77**).
   Median surviving gross/leg goes from **+$0.1002 (gate only) to
   −$0.0572 (gate + close)**, and it hurts in **59 of 74** cells
   (helps in 14). The early close fires on 55–85% of the survivors
   and it **truncates the drift** — the drift is the only thing
   paying. It is a fee multiplier that cuts winners.

3. **The gates are BETA filters, not alpha.** **50 of the 55**
   book-sized cells have a significantly **negative excess** — the
   survivors captured *less* than a passive hold of the same
   positions. And the placebo is decisive: **ZERO of 74** cells beat
   a **random subset of the same size**; **43 of 74** were
   significantly *worse*.

> **Rule: a gate that removes 95% of the book and leaves a book that
> still cannot cover the round trip has not found anything — it has
> just found a smaller book.** And a gate whose surviving gross is
> *worse* than a random subset of the same size is worse than random.

### The harness is PROVEN, not assumed

`python -m runtime.diagnostics.ta_gate_validate` runs the gate with a
constant `+1` signal (which passes every LONG leg) and proves it
reproduces the canonical ledger **bit-exactly** — gross, fee, drift
and excess, all at max abs error `0.000e+00`.

> **⚠️ The drift benchmark MUST run on the ledger's own 1s grid.** The
> first implementation used the 1m grid and it **disagreed in SIGN**
> with the ledger (−$9,715 vs +$11,098) even though the two
> correlated at 0.92. The 1m close is the close of the LAST second
> of a minute, so shifting both window endpoints by up to a minute
> moves a short-horizon drift by as much as the drift itself. The
> ledger's `entry_bar` / `exit_bar` are **file row indices**, so the
> fix was to carry a **1m → 1s row map** through the bar builder.
>
> **Rule: a benchmark must run on the grid the benchmark was defined
> on.** A drift computed on a coarser grid is a *different number*,
> not an approximation — and here it had the wrong sign.

Guards: `tests/test_ta_gate.py` (8 tests) — the no-op identity, the
1s-grid drift, the exact decay count, and the no-lookahead clock.

---

## Documentation map

| Doc | Contents |
| --- | --- |
| `AGENTS.md` (this file) | The operating rules: how to evaluate, how to sweep, what is true now |
| `docs/nb94_1w_vs_1y.md` | The forward-leak diagnosis |
| `docs/nb95_beta_proxy.md` | The beta-proxy diagnosis and the engine-defect audit |
| `docs/ta_episode_metric.md` | The TA episode (signal-to-signal) metric + the fixed-side placebo that kills the result |
| `docs/ta_gate_sweep_2026_10_25.md` | The TA GATE sweep — the trade-number decay, MR vs trend, the early close, the placebos |
| `docs/v2_spec.md` | The v2 rebuild logic spec |
| `docs/analysis.md` | The NB promotion / sweep-priority analysis (historical) |
| `docs/config_consolidation_plan.md` | The config consolidation plan (historical) |

> **Rule: measured evidence lives in `docs/`, not in this file.**
> This file states the current truth and the rules. When a diagnostic
> produces a table that matters, write it to `docs/` and link it.
> AGENTS.md is read on every session — it must stay short enough to
> be read.
