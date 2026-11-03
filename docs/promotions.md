# Promotions, dead features, and the NB lineage

> Relocated from `AGENTS.md` on 2026-11-03. What shipped, what was measured
> and rejected, and the numbers behind each promotion.
>
> The **current canonical values** are in `AGENTS.md`. This is the history.

## Contents

- [The 5-event per-side sequence whitelist](#the-5-event-per-side-sequence-whitelist--2026-11-04)
- [The engine-level walk-forward](#the-engine-level-walk-forward--the-decisive-test-2026-11-04)
- [`trail_sides` — the per-side trailing scope](#trail_sides--the-per-side-trailing-scope-promoted-at-short-2026-11-04)
- [The per-side sequence filter](#the-per-side-sequence-filter--filter-each-side-on-its-own-sequences)
- [Contra close / hedge netting](#contra-close--hedge-netting--measured-ships-on-at-500-2026-10-25)
- [`tp_mode='hvn_single'` — the single-target exit](#tp_modehvn_single--the-single-target-exit-2026-11-03)
- [`vol_state_min_expand_prev_day` — the day-shifted vol gate](#vol_state_min_expand_prev_day--the-day-shifted-vol-gate-promoted-2026-11-04)
- [The `vol_state` measure-set crash](#the-vol_state-measure-set-crash--a-dead-knob-that-looked-like-a-bug)
- [Things that were measured and found DEAD](#things-that-were-measured-and-found-dead)
- [NB history](#nb-history-the-lineage-of-whats-been-promoted)
- [Promotions of 2026-10-25](#promotions-of-2026-10-25--the-trade-count-target-30-70-tradesday)
- [The TA GATE](#the-ta-gate--measured-2026-10-25-not-promotable)

## The per-side sequence filter — filter each side on its OWN sequences

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

##### REMOVED — `structure_seq_drop_set = {"short|c", "short|C"}` (2026-11-04)

**REMOVED 2026-11-04.** The knob, the constant, the engine gate (both
the immediate and the sniper path), the `n_dropped_seq_side` counter
and five diagnostics are **deleted**. It was not measured wrong — it
was **REDUNDANT with, and CONTRADICTORY to, the 5-event whitelist**
that replaced it.

**The original promotion (2026-10-24), for the record.** Pooled over
all slices: gross **+$276 → +$379** (+$103), net **+$9 → +$197**,
short-side gross/leg **−$0.081 → +$0.996** (12× better), and the
**long side is completely untouched** (same legs, same gross, same
correlation). Both sides still trade — **this is not a directional
bet**. Its own recorded weakness: walk-forward re-fitting was **2/7**
out-of-sample slices positive, so it shipped as a **fixed,
pre-registered pair of buckets**, NOT as a general
sequence-learning feature.

**Why it was removed.** The 5-event whitelist answers the same
question at a strictly finer granularity, and the two gates
**contradicted each other**:

* The drop set said *"never short after a structure reversal
  ('c'/'C')"*.
* The whitelist said *"short after THIS specific 5-event history is
  good"* — and **3 of the 8 whitelisted short shapes end in a
  reversal**, including the single best one (`short|BcCcC`,
  +0.532 gross/leg).

So the drop set **vetoed the whitelist's best short shape**. The two
rules could never both be satisfied.

**The end-to-end proof.** 7-arm corpus A/B
(`runtime/diagnostics/seq_whitelist_corpus_ab2.py`), each arm a full
corpus continuous re-run. The drop set's isolated effect on the SAME
21-key whitelist:

| | gross | net | legs |
|---|---|---|---|
| drop set OFF | +$2,583 | −$741 | 6,663 |
| drop set ON | +$2,357 | −$467 | 5,664 |
| **Δ (ON − OFF)** | **−$226** | **+$274** | **−999** |

It costs **999 legs** and **$226 of GROSS** to save **$274 of NET**.
A net-only win that *loses* gross is a **fee artefact, not a signal**
(AGENTS.md § 5.2). The whitelist alone is the better book.

Locked by `tests/test_regressions.py::test_per_side_sequence_drop_set_is_removed`.

---

## The 5-event per-side sequence whitelist — 2026-11-04

**PROMOTED** as `structure_seq_whitelist` — a set of **21
`<side>|<5-char signature>` keys** (13 long, 8 short).

> **The user asked (2026-11-03):** *"tabulate bos/choch 5-event
> sequence and the subsequent entries. split between longs and shorts.
> then, we know which 5-seq events to whitelist before each
> long/short"*.

> **⚠️ THE SET WAS RE-FITTED ON 2026-11-04** (25 keys → 21 keys).
> The first version was fitted on a **64,978-leg** ledger that was
> built BEFORE the recipe changed. The current book is **37,159
> legs** and **only 14 of the 25 keys survived** — so the set was
> re-fitted from scratch. See the cache-fingerprint bug note below.

### What it is

A **whitelist**: a key PRESENT means **TAKE** that trade, ABSENT means
**DROP** it. The signature is the trailing **5** BoS/CHoCH events at
the **signal** bar, oldest first (`b`=BoS bull, `B`=BoS bear,
`c`=CHoCH bull, `C`=CHoCH bear) — e.g. `long|Ccbbb`.

It **REPLACED** `structure_seq_drop_set` (removed 2026-11-04 — see
the entry above).

### The measurement — corpus-wide, one continuous execution

`runtime/diagnostics/seq5_table.py`, full corpus 2024-01-01 →
2026-07-16 (14d warmup prefix executed and discarded), **37,159 legs**
on the unfiltered book. A signature qualifies when gross/leg is
positive on **both halves** of the corpus.

**LONG — 13 of 64 qualify:**

| signature | n | gross/leg | H1 | H2 |
|---|---|---|---|---|
| `Ccbbb` | 345 | +0.776 | +0.473 | +1.035 |
| `bCcbb` | 385 | +0.561 | +0.584 | +0.542 |
| `cCcCB` | 313 | +0.558 | +1.055 | +0.161 |
| `CcbCc` | 384 | +0.529 | +0.621 | +0.441 |
| `cbCcC` | 298 | +0.525 | +0.669 | +0.363 |
| `BcCcC` | 330 | +0.396 | +0.342 | +0.433 |
| `bbCcb` | 362 | +0.390 | +0.060 | +0.873 |
| `cCcCc` | 318 | +0.386 | +0.095 | +0.636 |
| `bbbbC` | 365 | +0.370 | +0.249 | +0.481 |
| `cCcbb` | 327 | +0.273 | +0.113 | +0.473 |
| `CcCBc` | 359 | +0.220 | +0.358 | +0.072 |
| `CcbbC` | 356 | +0.154 | +0.111 | +0.199 |
| `CcbCB` | 291 | +0.081 | +0.169 | +0.011 |

**SHORT — 8 of 64 qualify:**

| signature | n | gross/leg | H1 | H2 |
|---|---|---|---|---|
| `BBcbb` | 271 | +0.716 | +0.624 | +0.824 |
| `BcCcC` | 378 | +0.532 | +0.229 | +0.819 |
| `cCcbb` | 294 | +0.397 | +0.244 | +0.539 |
| `BBBcb` | 253 | +0.349 | +0.372 | +0.325 |
| `Bcbbb` | 283 | +0.325 | +0.175 | +0.487 |
| `CBBBB` | 202 | +0.272 | +0.231 | +0.315 |
| `cCcbC` | 340 | +0.195 | +0.155 | +0.239 |
| `CBcCc` | 291 | +0.119 | +0.107 | +0.129 |

### The walk-forward — the test that separates mining from signal

`runtime/diagnostics/seq5_walkforward.py`: fit the whitelist on ONE
half of the corpus, score it on the OTHER, both ways round. The null
is **2000 random whitelists of the same size** — "trade less at
random" — because a random whitelist also produces a positive delta
by saving fees.

| side | fold | real Δgross | null p95 | percentile |
|---|---|---|---|---|
| long | H1 → H2 | +$593 | +$888 | 77.0% |
| long | H2 → H1 | +$449 | +$381 | **97.9%** |
| short | H1 → H2 | +$1,605 | +$2,121 | 56.3% |
| short | H2 → H1 | +$1,495 | +$1,550 | **91.7%** |

**The H2→H1 fold is the one that earns the slot on each side** (98%
and 92%). The other two folds (77% and 56.3%) are barely better than
chance — recorded rather than smoothed over.

### The END-TO-END A/B — the verdict

`runtime/diagnostics/seq_whitelist_corpus_ab2.py`: **7 arms**, each a
full corpus continuous re-run (not a post-hoc filter).

| arm | wl | drop set | legs | legs/day | gross | net | gross/leg |
|---|---|---|---|---|---|---|---|
| unfiltered | 0 | 0 | 37,159 | 40.09 | −$4,742 | −$23,091 | −0.1276 |
| old (25 keys) | 25 | 0 | 7,926 | 8.55 | +$2,599 | −$1,367 | +0.3279 |
| old + drop set | 25 | 2 | 6,753 | 7.28 | +$2,319 | −$1,074 | +0.3434 |
| **split (21 keys) — SHIPPED** | **21** | **0** | **6,663** | **7.19** | **+$2,583** | **−$741** | **+0.3877** |
| split + drop set | 21 | 2 | 5,664 | 6.11 | +$2,357 | −$467 | +0.4161 |
| ranked-21/side | 42 | 0 | 12,655 | 13.65 | +$3,917 | −$2,395 | +0.3095 |
| ranked-21/side + drop set | 42 | 2 | 9,377 | 10.12 | +$3,414 | −$1,281 | +0.3640 |

The shipped set beats the old set on **both** gross per leg (+0.3877
vs +0.3279) and net (−$741 vs −$1,367).

### The ENGINE-LEVEL walk-forward — the decisive test (2026-11-04)

`runtime/diagnostics/seq5_engine_walkforward.py` — the test that was
OPEN when the set was promoted, and the one that decides whether the
**specific 21 keys** or only the **shape** of the rule generalise.

Each half is a **separate continuous execution** with its own warmup
prefix, so the rolling caps are warm. The whitelist is **fixed before
the run starts** — which is exactly what the post-hoc walk-forward
cannot do, because the post-hoc filter never re-evaluates the
`contra_close` netting or the same-direction cap.

| arm | legs | gross | net | gross/leg | long g/leg | short g/leg |
|---|---|---|---|---|---|---|
| unfiltered H1 | 18,640 | −$1,792 | −$10,805 | −0.0961 | +0.0065 | −0.2000 |
| unfiltered H2 | 18,350 | −$1,982 | −$10,505 | −0.1080 | +0.0141 | −0.2405 |
| **fit H1 → score H2** | 3,024 | −$351 | −$1,783 | −0.1159 | **+0.0635** | **−0.4664** |
| **fit H2 → score H1** | 2,645 | +$936 | −$350 | **+0.3540** | **+0.3721** | **+0.2945** |
| *in-sample (fit H1 → H1)* | 3,329 | +$1,085 | −$531 | +0.3260 | +0.3616 | +0.2605 |
| *in-sample (fit H2 → H2)* | 2,412 | −$69 | −$1,214 | −0.0287 | +0.0620 | −0.3260 |
| shipped set on H1 | 3,329 | +$1,085 | −$531 | +0.3260 | +0.3616 | +0.2605 |
| shipped set on H2 | 3,024 | −$351 | −$1,783 | −0.1159 | +0.0635 | −0.4664 |
| **long-only on H1** | 2,157 | +$770 | −$281 | **+0.3569** | +0.3569 | — |
| **long-only on H2** | 1,997 | +$110 | −$843 | **+0.0553** | +0.0553 | — |

> **`fit H1` produced EXACTLY the shipped 21-key set** — the H1 fit
> and the whole-corpus fit agree key-for-key. So `fit H1 → score H2`
> IS the shipped set scored out of sample, and the shipped set's H2
> number is an **out-of-sample** number.

**Pooled verdict:**

| book | legs | gross | net | gross/leg | fee/leg | gross/fee |
|---|---|---|---|---|---|---|
| control (unfiltered) | 36,990 | −$3,774 | −$21,310 | −0.1020 | — | — |
| **OUT-OF-SAMPLE** (each half scored by the OTHER's fit) | 5,669 | **+$586** | −$2,133 | **+0.1033** | 0.4796 | 0.215× |
| *in-sample* (each half scored by its OWN fit) | 5,741 | +$1,016 | −$1,745 | +0.1770 | 0.4810 | 0.368× |
| shipped (whole-corpus fit) | 6,353 | +$735 | −$2,314 | +0.1156 | 0.4799 | 0.241× |
| **long-only** | 4,154 | **+$880** | −$1,124 | **+0.2119** | 0.4825 | 0.439× |

**The OOS gross/leg (+0.1033) is 58% of the in-sample gross/leg
(+0.1770).** The rule generalises, with real decay.

#### ⚠️ THE SHORT SIDE DOES NOT GENERALISE — a SIGN FLIP

| side | in-sample g/leg | **out-of-sample g/leg** | verdict |
|---|---|---|---|
| **long** | +0.2233 | **+0.2189** | **generalises** (no decay) |
| **short** | +0.0700 | **−0.1803** | **sign flip — fit noise** |

The short side's whole-corpus fit says +0.0700; scored on the half it
never saw, it is **−0.1803**.

#### ⚠️ BUT THE LONG-ONLY BOOK IS A DIRECTIONAL BET

The **per-leg** decomposition — the exact algebraic identity, with
n = thousands of legs — is the decisive number:

| arm | side | gross/leg | passive-hold/leg | hold t | **excess/leg** | **excess t** |
|---|---|---|---|---|---|---|
| long-only H1 | long | +0.3569 | +0.3884 | +5.32 | **−0.0315** | **−2.87** |
| long-only H2 | long | +0.0553 | +0.1569 | +1.00 | **−0.1016** | **−3.47** |

**The long-only book's gross is the market's passive hold, and its
`excess` — what the entries added on top of it — is significantly
NEGATIVE in both halves.** The whitelist has found a better place to
stand long — it has **not** found timing skill.

The **daily** direction-dependence regression agrees, and it is the
sanctioned instrument — `python -m
runtime.diagnostics.daily_direction_dependence`:

| arm | legs | days | **daily corr** | **corr t** | r² | excess/leg | excess t |
|---|---|---|---|---|---|---|---|
| unfiltered H1 | 18,640 | 418 | +0.040 | +0.82 | 0.002 | −0.0574 | −12.17 |
| unfiltered H2 | 18,350 | 275 | −0.003 | −0.06 | 0.000 | −0.1415 | −15.86 |
| shipped on H1 | 3,329 | 344 | +0.047 | +0.87 | 0.002 | −0.0441 | −4.84 |
| shipped on H2 | 3,024 | 214 | +0.187 | **+2.75** | 0.035 | −0.1299 | −5.62 |
| **long-only H1** | 2,157 | 309 | +0.208 | **+3.69** | 0.043 | −0.0315 | **−2.87** |
| **long-only H2** | 1,997 | 198 | +0.247 | **+3.53** | 0.061 | −0.1016 | **−3.47** |

The long-only book is **significantly long-correlated in BOTH halves**
(t = +3.69 and +3.53); the unfiltered book is **not correlated at all**
(t = +0.82 / −0.06). The whitelist does not create the market exposure
— **concentrating** it does.

> **⛔ THE OLD MONTHLY BETA TABLE IS RETIRED.** The repo used to
> regress **monthly** gross on the **monthly** market return
> (`runtime/diagnostics/seq5_beta_regression.py`). That measurement is
> invalid for this book: n = 13..31 points, so the correlation's
> standard error is ~0.32 in Fisher-z space and every quoted monthly
> `r` sat inside its own noise band (**r = +0.459** at n = 13 →
> **t = 1.57**; **r = +0.337** at n = 31 → **t = 1.86**). It was also
> **sign-unstable**: the monthly table called `shipped on H2`
> **−0.373**, while the daily regression measures the same book at
> **+0.187** (t = +2.75). A number whose sign flips with the
> aggregation window cannot decide anything.
>
> The monthly table and its driver are **superseded by
> `runtime/diagnostics/daily_direction_dependence.py`**.

> **⚠️ THIS IS WHY ALL 21 KEYS SHIP.** The long-only variant has the
> better gross/leg (+0.2119 vs +0.1156) and the better net (−$1,124
> vs −$2,314) — and it is **not** shipped, because it is a directional
> bet on gold wearing a sequence filter as a hat. Dropping the short
> side does not remove the market exposure; it **concentrates** it.

### ⚠️ What this promotion does NOT claim

1. **The book is still NET-negative** (−$741 over 927 days). Its
   gross is **positive** (+$2,583) but the per-leg edge (+$0.3877) is
   still **below the measured round trip** ($0.4989). This is a
   **QUALITY** knob, not an **EDGE** knob — the same shape of verdict
   as the promoted `vol_state_min_expand_prev_day` gate (AGENTS.md
   § 3.7.2): it makes the surviving trades much better; it does not
   make them profitable.
2. **The trade count is far below the 30–70/day band** — 7.19
   legs/calendar-day, 93.6% of days under 30 legs. The user
   explicitly waived the band for this knob (*"dont need to be too
   strict on trade count for this"*). To stay inside the band the
   whitelist would have to keep **48 of 64** signatures per side —
   i.e. barely filter at all (`runtime/diagnostics/seq5_ksweep.py`).
   The full-strength set is the one that carries the signal.
3. **The shipped set is a whole-corpus fit, and the engine-level
   walk-forward now says exactly how much that costs.** The OOS
   gross/leg is **+0.1033** against the in-sample **+0.1770** — a 58%
   retention. The **long** half generalises cleanly; the **short**
   half **sign-flips** and is fit noise. And the long-only variant
   that the short half would have enabled is a **directional bet**
   (per-leg excess significantly negative in both halves; daily
   correlation significant in both halves: t = +3.69 and +3.53) — the
   same market exposure that got `trade_sides="long"` rejected. So all
   21 keys ship: the short keys are the honest cost of keeping the
   book two-sided, and removing them would not remove the market
   exposure, it would concentrate it.
4. **Post-hoc is not the book.** The table above is a leg filter. The
   engine-side whitelist re-runs the book, so the conflict policy, the
   same-direction cap and the `contra_close` netting are all
   re-evaluated — and the engine-level walk-forward above is that
   re-run.

### The TRADE-COUNT BAND is unreachable — measured (2026-11-04)

The "48 of 64 signatures per side to stay in the band" claim was
POST-HOC. It is now **measured END TO END**
(`runtime/diagnostics/seq5_ksweep_engine_check.py`):

| book | keys | legs | legs/day | gross | net | gross/leg | gross/fee | excess t |
|---|---|---|---|---|---|---|---|---|
| **shipped (21 keys)** | 21 | 6,663 | 7.19 | +$2,583 | **−$741** | **+0.3877** | **0.777×** | −8.45 |
| **ranked top-48** | 96 | 27,926 | **30.13** | +$1,279 | −$12,561 | **+0.0458** | **0.092×** | **−17.37** |

The K=48 variant **does** land in the band (30.13/day), but gross/leg
collapses to **12%** of the shipped set, net worsens by **−$11,820**,
gross/fee falls from 0.777× to 0.092×, its timing component is
**strongly negative** (t = −17.37), and its short side turns
negative (−0.0477 g/leg over 13,524 legs).

**The trade-count band and the edge are in DIRECT conflict.** The band
is reachable only by admitting signatures that destroy the per-leg
edge — so the user's waiver of the band is not a compromise, it is
the only admissible choice.

### The methodological rule this promotion exposed

> **A drop-set or whitelist MUST be measured END TO END by re-running
> the engine.** The post-hoc leg filter is a *screen* for which
> hypotheses are worth a re-run — never the verdict itself.

`contra_close` nets opposing legs **pairwise**, so removing legs
removes the netting's counterparties. Measured on the corpus: the
post-hoc table said the `short|B` drop set was best (positive on both
halves); the engine-side re-run said it was the **worst** of the
three tested, because it destroyed the netting's counterparties.

Driver: `runtime/diagnostics/seq_whitelist_corpus_ab2.py`.

### How this SUPERSEDES the 2026-10-24 "whitelist is DEAD" finding

The 2026-10-24 finding (`sequence_whitelist.py`) reported the
whitelist as **empty**. That emptiness was a **sample-size artefact,
not an EV verdict**: after the vol gate the mean was 5.5 legs per
signature against the report's own `min_n` floor of 15, so *no*
signature could ever qualify. On the full corpus each signature has
~300 legs and 21 of the 64 clear both halves.

The `test_structure_whitelist_monthly_report_is_empty_and_superseded`
guard now locks that historical report as-is, and the live promotion is
locked by `test_five_event_sequence_whitelist_is_promoted`.

**What did NOT change:** the coarse 3-bucket **polarity** filter is
still dead (`test_structure_whitelist_powered_heldout_is_not_significantly_profitable`).
**A coarse polarity bucket does not carry the signal — the specific
5-event shape does.** That is precisely why the promotion is
per-signature and not per-polarity.

### The cache-fingerprint bug behind the re-fit

The reason the first fit was stale is worth recording, because it is
a **general** trap:

> `bt/_engine/vol_state.py` produced the `vol_state` artifact, which
> IS pickled into the artifact cache — but the module was in **neither**
> `runtime/sweeps/cached_sweep.py::_ARTIFACT_SOURCES` **nor**
> `runtime/diagnostics/leg_ledger.py::_source_hash()`'s file list.
> So editing it never invalidated the cache, and a stale `vol_state`
> kept being served. One nominal config then produced **two different
> books** (64,978 legs vs 37,159 legs).

**FIXED 2026-11-04** in both places. The rule, now written into the
code: *if a module's output is pickled into the artifact, that module
belongs in the fingerprint tuple.*

---

## `trail_sides` — the per-side trailing scope (PROMOTED at `'short'` 2026-11-04)

**PROMOTED. The canonical value is `'short'`.**

### What it is

`trail_sides` scopes the **trailing stop** to one side of the book:
`'both'` | `'long'` | `'short'`.

> **⚠️ IT IS NOT THE ENTRY GATE.** `trade_sides` decides which trades
> may **OPEN**; `trail_sides` decides which **POSITIONS** get a
> trailing **EXIT**. Under `trail_sides='short'` both sides still
> trade. Wiring it into the entry path would silently re-create the
> `trade_sides='long'` bet that this file already records as rejected.

### Why it was built

The trail's MFE/MFA analysis (`docs/trail_stop.md` § 8.4) measured the
trail's effect **per side** and found it **helps the losing side and
hurts the winning side**: longs −$906.77 (753 TPs cut), shorts
+$533.38 (290 SLs rescued). The short side's MFA p50 ($3.716) sits
close to its MFE p50 ($4.380) — shorts go against the trade *first*,
which is the population a trailing stop is the right tool for.

### THE LEDGER VERDICT — the promotion-gate measurement

`runtime/diagnostics/trail_sides_ledger_verdict.py`. **Full corpus
(2024-01-01 → 2026-07-16, 927 days), 14-day warmup prefix, ONE
continuous execution per arm**, canonical recipe otherwise.

| book | legs | gross | **net** | **gross/leg** | fee/leg | **gross/fee** |
|---|---|---|---|---|---|---|
| `trail_sides='both'` (was shipping) | 6,663 | +$2,583.18 | −$741.16 | +0.3877 | 0.4989 | 0.777× |
| `trail_sides='long'` | 7,063 | +$2,739.34 | −$584.98 | +0.3878 | 0.4931 | 0.787× |
| **`trail_mode='off'` (the baseline)** | 7,284 | +$4,917.66 | +$1,593.16 | +0.6751 | 0.4564 | 1.479× |
| **`trail_sides='short'` (PROMOTED)** | **7,122** | **+$5,622.77** | **+$2,298.20** | **+0.7895** | 0.4668 | **1.691×** |

**The promoted scope is the MOST PROFITABLE cell of the scope grid**
(+$2,298.20). It beats the shipping recipe by **+$3,039.36** and the
trail-OFF baseline by **+$705.04**, and it is the only arm whose
gross edge **covers the measured round trip** by a wide margin
(1.691× vs the canonical book's 0.777×).

> **⚠️ THE PROMOTION CRITERION IS NET PROFIT** (user instruction,
> 2026-11-04: *"just promote whatever is most profitable"*). The
> `excess` decomposition, the daily direction-dependence regression
> and the daily aggregation are **DIAGNOSTICS** — they explain the
> result, they do not gate it.
>
> **⚠️ THE DRIVER'S ARMS ARE EXPLICIT OVERRIDES.** After the
> promotion the canonical recipe IS `trail_sides='short'`, so a
> driver that took its "canonical" arm from the no-overrides path
> would compare the book against **itself**. Every arm in
> `trail_sides_ledger_verdict.py` now names its scope explicitly.

The **measured** fee is the one used for the verdict — the engine's
fee scales with the fill price, so the flat $0.47 spec overstates the
edge by ~7% (AGENTS.md § 3.2).

#### The passive-hold/excess decomposition (AGENTS.md § 5.1 Q2) — DIAGNOSTIC

| book | passive-hold/leg | hold t | **excess/leg** | **excess t** |
|---|---|---|---|---|
| `both` | +0.4641 | +7.01 | −0.0764 | −8.45 |
| `off` | +0.7407 | +5.03 | −0.0655 | −7.46 |
| **`short`** | **+0.8539** | +6.62 | **−0.0644** | **−7.03** |

> **⚠️ READ THIS BEFORE QUOTING A PER-BUCKET `excess`.**
> The engine books P&L at the **actual SL / TP fill price**;
> `drift` (the passive hold) is a **close-to-close** measurement.
> Those are **not exchangeable** — 85.9% of `sl` legs fill exactly at
> the stop, 100% of the rest fill adverse, and on **71.6%** of `sl`
> legs the exit bar's close is BEYOND the stop. The identity
>
> ```text
> excess == gap@exit − gap@entry      (gap = (fill − bar_close)·side·qty)
> ```
>
> holds to **1e-15** in every bucket of every book. So a stop or
> target bucket's `excess` is the **EXECUTION-GAP DIFFERENTIAL**, not
> a timing measure, and its sign **flips with book composition**
> (the `sl` bucket: −$42 on `both`, **+$134** on `short`, +$117 on
> trail OFF).
>
> The forms that DO hold are the **whole-book** and **per-side**
> excess: **−$458.39** total, **−$198.80** long, **−$259.59**
> short. The per-bucket assertion was replaced by those forms plus
> the buckets with no fill-price/close divergence
> (`contra_close`, `trail_stop`, `replaced_by_new_signal`).
>
> **THE HONEST LIMIT #1 STANDS:** the book captures **less** than a
> passive hold of the same positions, and the short scope is a
> **better place to stand**, not a timing edge. But that is a
> **limitation on the book's characterisation**, not a bar on
> shipping it — it earns its keep by the NET.

#### The DAILY direction-dependence test

`python -m runtime.diagnostics.daily_direction_dependence` — the
**daily** aggregation (n = 577 days), the sanctioned instrument
(AGENTS.md § 1.1 rule 7):

| book | legs | days | **daily corr** | **corr t** | r² |
|---|---|---|---|---|---|
| `both` | 7,122 | 577 | +0.392 | **+9.92** | 0.154 |
| `off` | 7,284 | 577 | +0.223 | **+5.43** | 0.050 |
| **`short`** | **7,122** | **577** | **+0.392** | **+9.92** | **0.154** |

**The daily correlation is significant** — the book IS market-exposed,
and the criterion is net profit, not the absence of exposure.

> **⛔ THE OLD MONTHLY BETA REGRESSION IS RETIRED.** The repo previously
> regressed gross on the **monthly** market return and quoted
> `corr = +0.337` (n = 31) for the `short` scope, next to the
> `+0.464` that got `trade_sides='long'` rejected. Those numbers are
> **inside their own noise band** — Fisher-z **t = 1.86** and
> **t = 1.57** respectively, neither distinguishable from zero — and
> the **daily** regression on the same book measures **+0.392 with
> t = +9.92**. The daily number is the one that is decisive.
>
> Note that `short` and `both` are the **same book** (the scope changes
> exits, not the daily gross bucket), so their daily correlations are
> identical — as they must be.

#### The PAIRED test

Joining the books on `(signal_bar, side)` and pairing by entry bar
(the key is **not** unique — 275 groups carry a different number of
legs in each book):

| comparison | paired legs | gross delta | t |
|---|---|---|---|
| `short` vs `both` | 6,659 | **+$1,289.78** | +1.62 |
| `short` vs `off` | 6,947 | **+$1,236.01** | +1.91 |

The paired t-stat is **below 2.0 on both comparisons** — the
per-trade improvement is not individually significant. The gain is
carried by the **book composition** (the 463 unmatched variant legs
average **+$3.77 gross each**, 455 of them LONG).

### ⚠️ THE 2×2 ATTRIBUTION — where the gain actually comes from

| book | net | **long-side net** | short-side net |
|---|---|---|---|
| `both` | −$741.16 | −$391.31 | −$349.86 |
| `off` | +$1,593.16 | +$1,893.01 | −$299.85 |
| `long` scope | −$584.98 | −$456.02 | −$128.97 |
| **`short` scope** | **+$2,298.20** | **+$2,675.14** | −$376.94 |

The **net effect of turning the trail ON for shorts alone** is
**+$705.04**. The net effect of turning it on for **longs** is
**−$2,178.14**.

> **⚠️ THE HONEST LIMIT #2: MOST OF THE GAIN IS THE TRAIL BEING
> *REMOVED* FROM LONGS, NOT ADDED TO SHORTS.** The long side's net
> goes from −$391 (`both`) to +$2,675 (`short`) — a **+$3,066** swing
> on a side whose trail was simply switched OFF. The short side, whose
> trail was switched ON, gets **−$27 WORSE** (−$349.86 → −$376.94).
>
> The short scope is nevertheless the **correct** configuration: it is
> the only one of the four that beats the trail-OFF baseline, because
> the trail's damage on longs outweighs its benefit on shorts. The
> mechanism is "the trail is a bad exit for this book", and scoping it
> is the least-bad way to express that.

### ⚠️ THE OUT-OF-SAMPLE SPLIT — and the honest limits

ONE continuous execution, bucketed by **signal time** (AGENTS.md § 1.1
— the half is a REPORTING dimension, never an execution boundary):

| book | H1 net | H2 net | H1 g/leg | H2 g/leg |
|---|---|---|---|---|
| `both` | −$349.05 | −$392.12 | +0.2221 | +0.4883 |
| `off` | +$610.65 | +$982.51 | +0.5813 | +0.7276 |
| **`short`** | **+$646.05** | **+$1,652.15** | **+0.5963** | **+0.9011** |

The promoted scope beats the trail-OFF baseline on **gross/leg in BOTH
halves** — which is the fee-artefact-free comparison (AGENTS.md § 5.2).

#### ⚠️ THE SPLIT ROBUSTNESS — one split is not enough

AGENTS.md § 3.9b named "one split point" as an honest limit of the
single-split verdict. It is now **measured** across **7 cut points**
(the same one continuous execution, re-bucketed):

| cut | Δ H1 net | Δ H2 net | vs canonical | vs trail OFF |
|---|---|---|---|---|
| 2024-07-01 | −$206.05 | +$3,245.41 | NO | NO |
| 2024-10-01 | +$208.88 | +$2,830.48 | wins both | NO |
| 2025-01-01 | +$461.57 | +$2,577.79 | wins both | NO |
| 2025-04-01 | +$995.10 | +$2,044.26 | wins both | wins both |
| 2025-07-01 | +$1,913.52 | +$1,125.84 | wins both | wins both |
| 2025-10-01 | +$3,511.97 | −$472.61 | NO | wins both |
| 2026-01-01 | +$4,487.00 | −$1,447.64 | NO | wins both |

**The scope wins both halves at 4 of 7 cut points vs the canonical
recipe, and at 4 of 7 vs the trail-OFF baseline.** It is NOT uniformly
better — and the failures are not random: they are the two extreme
cuts, where one half is very short.

> **⚠️ THE HONEST LIMIT #3: THE GAIN IS REGIME-CONCENTRATED.**
> Splitting by year (gold: 2024 **+27.2%**, 2025 **+64.5%**, 2026
> **−6.1%**):
>
> | year | `both` | `off` | `short` | Δ vs `both` |
> |---|---|---|---|---|
> | 2024 | −$325.55 | +$209.99 | +$136.02 | +$461.57 |
> | 2025 | −$193.01 | +$3,339.64 | **+$3,832.42** | **+$4,025.43** |
> | 2026 | −$222.60 | −$1,956.47 | −$1,670.24 | **−$1,447.64** |
>
> **2026 LOSES $1,448 against the shipping recipe.** The long book's
> net in 2026H1 (gold −7.4%) is **−$2,232.88** under the promoted
> scope. The scope helps when gold trends and hurts when it does not,
> because the long side carries the book.
>
> The variant wins **20 of 31 months** against the shipping recipe.

### The verdict — PROMOTED

The promote gate (`.cursor/skills/promote/SKILL.md`):

| # | question | answer |
|---|---|---|
| 1 | Measured on the CONTINUOUS corpus? | **YES** — one continuous run per arm, 927 days, 14d warmup |
| 2 | Judged on the LEDGER, not the month's net? | **YES** — `leg_buckets` on the corpus-wide per-leg record |
| 3 | Does the per-leg edge cover the MEASURED round trip? | **YES** — 1.691× (the canonical book was 0.777×) |
| 4 | Is the improvement larger than the noise? | **PARTIAL** — the unpaired t is +2.80; the **paired** t is only +1.62 |
| 5 | Does it survive the held-out check? | **PARTIAL** — wins both halves at 4/7 cut points, not 7/7 |

**Yes to 1–3, partial on 4–5.** Promoted, with the limits above
recorded rather than smoothed over. The corpus-wide result is large
(+$3,039 over the shipping recipe, +$705 over the trail-OFF
baseline) and the direction is consistent across the ledger, the
buckets, the halves and the per-leg excess decomposition.

### What shipped

- `bt/optimal_config.py`: `TRAIL_SIDES = 'short'`.
- The **canonical ledger** was rebuilt under the promoted recipe
  (7,122 legs, gross +$5,622.77, net +$2,298.20, gross/leg +0.7895,
  gross/fee 1.691×).
- The knob's four regression guards (`tests/test_regressions.py`) are
  unchanged and still green — they lock the *mechanics* (the knob
  reaches the engine, it rejects unknown modes, it is scoped per
  position direction, it is not the entry gate), not the value.

### The scope-gate invariant

`trail_sides='short'` must produce **ZERO long legs exiting on the
trail**. Measured: **0** in the promoted canonical ledger (the exit
histogram shows `trail_stop` n=1,640, all of them shorts) ✅.

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

## `tp_mode='hvn_single'` — the single-target exit (2026-11-03)

**PROMOTED** (user instruction, 2026-11-03), with the one-month A/B
below.

### What changed

The engine gained a third TP mode. `compute_structural_tp` now
implements `hvn_single`: **ONE** target on the `nth_hvn_n`-th HVN
shelf ahead of entry, with **no partial close, no runner leg and no
breakeven re-injection**. The whole position rides to the shelf.

### Why

The `multi_target` shape splits the position 50/50 between a partial
at TP1 and a remainder at TP2. On the canonical book the two legs
**collapse onto the SAME price** — the swing cap — on effectively
every trade, because **88.7%** of HVN lookups miss (the
`multi_hvn_proxy` fallback puts TP1 beyond the cap) and TP2's raw
200× zone width is also beyond it.

The result is: a partial close at the cap, followed by a remainder
that runs the **same distance** for **half the size**. That is a
**fee-paying shape, not an edge** — it pays two round trips to make
one move.

### The one-month A/B (2025-01)

`vol_state_min_expand_prev_day=0.0`, canonical everything else.

| mode | legs | gross | fees | net | EV/leg | hit | maxDD | PF | exit buckets |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `multi_target` | 904 | $526.94 | $386.15 | $140.79 | $0.156 | 0.551 | $283.49 | 1.06 | `tp2` 367, `tp1` 46 |
| **`hvn_single`** | 858 | $524.96 | $386.15 | $138.81 | $0.162 | 0.527 | $283.71 | 1.06 | **`tp` 413 (single)** |
| `fvg_mult` | 872 | $514.21 | $386.15 | $128.06 | $0.147 | 0.530 | $283.71 | 1.05 | `tp` 409 (single) |

**The exit-reason histogram is the real result.** `hvn_single` has a
single `tp` bucket; `multi_target` has two (`tp1` + `tp2`). The net
difference between the three modes is **~$13 on one month** — that
is ONE DRAW, not a verdict.

> **⚠️ NOT YET LEDGER-JUDGED.** The three modes are within $13 of
> each other on a month's net. The corpus-wide ledger
> (`leg_ledger` → `leg_buckets`) has not been run on the `hvn_single`
> book, so the promotion rests on the **geometry** argument (two legs
> on the same price is fee drag) and the histogram, not on a measured
> per-leg EV. Run the ledger before treating this as settled.

### Collateral effect — `partial_close_pct` is now INACTIVE

`hvn_single` returns `partial_close_pct=0.0`, which the engine reads
as "TP2 is inactive", so the full position closes at TP1. The knob
remains on the sweep surface for the `multi_target` A/B, but it does
**nothing** under the canonical mode.

---

## `vol_state_min_expand_prev_day` — the day-shifted vol gate (PROMOTED 2026-11-04)

**IMPLEMENTED, CORPUS-LEVEL A/B'd, and PROMOTED at `1.0`.**

> **The decision criterion is EV (gross per leg)** — *"always analyze
> based on EV, any EV increase we promote"* (user instruction,
> 2026-11-04). This section records the measurement, the promotion,
> and the honest limits.

### What it is

`vol_expand_prev_day` = **RV(today so far) ÷ RV(yesterday at the
same elapsed fraction of the day)**.

This is the "vol gate expansion" of
[`v2_spec.md`](v2_spec.md) § 4.3. The hypothesis: volatility is
autocorrelated **across days**, so yesterday's regime is a usable
prior for today. The existing `vol_state_min_range_norm` gate has a
**60-minute** memory and therefore **cannot distinguish** "the whole
day is slow" from "the whole day is fast".

The evidence for the hypothesis is in `v2_spec.md` § 4.3: on the 1y
corpus, the lag-1 correlation of consecutive daily ranges is
**+0.611 Pearson / +0.461 log-log**, and the volatility
autocorrelation holds at **every** horizon including 1s (**+0.403**).

### Why "same elapsed fraction"

Raw "today so far vs yesterday's full day" is mechanically biased low
every morning (a partial day always accumulates less RV than a whole
one) and the bias shrinks as the day progresses. Comparing at the
**same elapsed fraction** removes it, so `1.0` means "today is
tracking yesterday exactly" **at any hour**.

Causal by construction: the previous calendar day is entirely in the
past, and the numerator is a cumulative sum from midnight over
returns up to the current bar only.

### THE PROMOTING MEASUREMENT (2026-11-04)

One continuous run over the **full corpus** (2024-01-01 → 2026-07-16,
927 calendar days, 14-day warmup prefix executed and discarded), the
canonical recipe otherwise, via
`runtime/diagnostics/expand_prev_day_corpus_ab.py`:

| threshold | legs | legs/day | gross | fees | net | **EV (gross/leg)** | passive-hold/leg | excess/leg | fee/leg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.0 (off) | 11,305 | 12.20 | $1,944.53 | $5,690.49 | −$3,745.96 | 0.1720 | +0.2704 | −0.0984 | 0.5034 |
| 0.7 | 9,117 | 9.83 | $2,430.97 | $4,556.08 | −$2,125.11 | 0.2666 | +0.3512 | −0.0846 | 0.4997 |
| 0.85 | 7,822 | 8.44 | $2,329.55 | $3,910.68 | −$1,581.14 | 0.2978 | +0.3740 | −0.0762 | 0.5000 |
| **1.0 (SHIPPED)** | 6,663 | 7.19 | $2,583.18 | $3,324.34 | **−$741.16** | **0.3877** | **+0.4641** | −0.0764 | 0.4989 |
| 1.1 | 5,677 | 6.12 | $1,808.74 | $2,838.53 | −$1,029.78 | 0.3186 | +0.3866 | −0.0680 | 0.5000 |
| 1.2 | 5,088 | 5.49 | $1,541.59 | $2,560.87 | −$1,019.29 | 0.3030 | +0.3740 | −0.0710 | 0.5033 |
| 1.3 | 4,610 | 4.97 | $1,410.23 | $2,327.22 | −$916.99 | 0.3059 | +0.3798 | −0.0739 | 0.5048 |

**EV rises to a peak at `1.0` and then FALLS AWAY.** The full
7-point curve was measured (2026-11-04); `1.1`, `1.2` and `1.3` were
run specifically to confirm `1.0` is a **peak** and not a point on a
still-rising slope:

* `1.0` beats every higher threshold — dEV = **+0.0691** (vs 1.1),
  **+0.0847** (vs 1.2), **+0.0818** (vs 1.3).
* The gain over gate-off is dEV = **+0.2157**, **t = +2.60** — the
  lift is **statistically significant**. The net improves in step
  (−$3,746 → −$741) because the fee bill falls faster than the gross.

> **The peak is a PASSIVE-HOLD peak, not a timing peak.** At `1.0` the
> passive-hold component peaks too (+0.4641) while `excess` (what the
> entries added on top of it) is flat at −0.07 to −0.10 across the
> whole range. The higher thresholds have a *slightly better* excess
> (−0.0680 at 1.1) but a **worse** passive-hold component
> (+0.3866), and EV is the sum. **The gate selects on the market's
> move, not on timing skill** — consistent with the repo-wide finding
> that the gross is passive-hold-dominated.

### Why it promotes

1. **EV is up at every threshold**, and the maximum is at `1.0`.
2. **Net improves monotonically** alongside it.
3. **The knob is LIVE** — it is not a no-op that merely reshuffles
   the book.

### The honest limits (recorded, not hidden)

* The EV lift over gate-off **is statistically significant**
  (dEV = **+0.2157**, **t = +2.60**), but the lift over the NEAR
  neighbours is not: 1.0 vs 0.85 is t = **+1.00**, 1.0 vs 1.1 is
  t = **+0.71**. The peak is real and sits at `1.0`, but the plateau
  0.85–1.0 is not sharply separated from it.
* **The book is still NEGATIVE** (−$741). The gate **reduces a
  loss**; it does not make the book profitable. EV/leg ($0.3877)
  stays **below** the measured round trip ($0.4989) — the strategy
  remains **fee-limited** by the `AGENTS.md` § 5.2 test.
* **`excess` (the entries' contribution, as opposed to the passive
  hold) stays NEGATIVE** at every threshold (t = −8.45 at 1.0). The
  gross is still dominated by the passive hold.
* **Trade count is far below the target band**: 7.19
  legs/calendar-day, and **93.6% of days trade fewer than 30 legs**.
  The 30–70/day band (`AGENTS.md` § 1.1 rule 6) is **not** met.
  The instruction to promote on EV **overrides** the trade-count
  preference — a deliberate trade-off, not an oversight.

### The superseded 2026-11-03 measurement (kept for the record)

On the **pre-`trail_mode`** recipe the same knob was measured as
**NOT** promotable:

| threshold | legs | legs/day | gross/leg | net |
| --- | --- | --- | --- | --- |
| 0.0 (off) | 66,562 | 71.80 | 0.0404 | −$21,340.00 |
| 0.9 | 42,012 | 45.32 | 0.0813 | −$12,024.50 |
| 1.1 | 31,423 | 33.90 | 0.0799 | −$9,187.74 |

Both recipes show the same *shape* (EV up, fewer trades, net up),
but the **levels are not comparable** — the trailing stop changed the
book from 27,619 legs to 6,753 and flipped the EV sign from negative
to positive. **A number from one recipe must never be quoted against
the other.** The 2026-11-04 table above is the live one.

> **The lesson.** The trailing stop (`trail_mode='combined'`) landed
> between the two measurements and changed the payoff geometry. The
> knob's verdict moved with it. That is why this gate is now
> recorded as **paired with the exit geometry** and must be
> re-measured whenever the exit shape changes.

### Two implementation traps, both fixed

* **The measure needs timestamps.** `build_vol_state` gained a
  `times_ns` parameter (int64 epoch nanoseconds, via `to_ns_arr`).
  Requesting `vol_expand_prev_day` without it **raises** rather than
  silently returning 1.0.
* **Partial days must not be the denominator.** Forex sessions break
  daily and weekend days are partial (Sunday's session is ~1h). The
  measure emits `1.0` ("no opinion") when the previous day did not
  trade up to the same elapsed fraction — otherwise Monday 10:00
  would be compared against Sunday 23:59's tiny cumulative RV and
  the ratio would explode to ~30× on no real signal.

---

## The `vol_state` measure-set crash — a dead knob that looked like a bug

**FIXED 2026-11-03.**

`cached_sweep.py` and `directional_beta.py` built only three
`vol_state` measures:

```python
measures=("vel_5m", "range_norm_1m", "wick_ratio")
```

The engine's `_vol_state_pass` reads the measure it needs **by key**.
Any threshold for a measure that was never materialised raised
`KeyError` deep inside the bar loop. So sweeping
`vol_state_min_vel_15m` — a knob that is on the allowlist, is
forwarded by the builder, and is listed in the canonical config —
**crashed the run** rather than measuring anything.

This is worse than a dead knob: a dead knob returns a plausible
number, whereas this one looked like a **bug in the sweep driver**,
so the natural response was to distrust the driver rather than to
notice the missing measure.

Both call sites now build the full set (`vel_5m`, `vel_15m`,
`range_norm_1m`, `wick_ratio`, `rv_ratio_30s`, `shock`,
`vol_expand_prev_day`), at a cost of **~0.7 s per month**.

> **Rule it exposes.** A knob is live only if the engine actually
> reads it **and** the measure it gates on is materialised. The
> allowlist checks the first half; nothing checked the second.

---

## Things that were measured and found DEAD

Recording these so they are not re-litigated. Each was measured, not
assumed.

### The 2026-10-24 monthly whitelist artifact — DEAD as measured, SUPERSEDED 2026-11-04

The trailing-5-event **signature whitelist** asked "is this exact
BoS/CHoCH history a trade I should take at all". As measured on **8
monthly slices** on 2026-10-24: **No.**

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

> **⚠️ WHY IT WAS SUPERSEDED.** The emptiness was a **sample-size
> artefact, not an EV verdict**. `min_n = 15` against a mean of 5.5
> legs/signature means *no* signature could ever qualify — the report
> could not have found an edge at any EV. On the **full corpus**
> (64,978 legs, one continuous execution) each signature has ~500
> legs, 25 of the 64 clear both halves, and the 2-fold walk-forward
> puts the short side at the 95th/87th percentile of the
> random-whitelist null. That is the promoted feature —
> [`structure_seq_whitelist`](#the-5-event-per-side-sequence-whitelist--2026-11-04).
>
> **The coarse-polarity variant is still DEAD** — see below.

### The coarse POLARITY whitelist — still DEAD

`sequence_whitelist_powered.py` coarsened the signature into 3
polarity buckets (bull / bear / mixed). Fit picks the `bear` bucket
(EV +$0.485, t +0.52 — not significant) and on held-out the kept
trades have **aggregate t = −2.15**.

**A coarse polarity bucket does NOT carry the signal — the specific
5-event shape does.** That is exactly why the 2026-11-04 promotion is
per-signature and not per-polarity. Locked by
`test_structure_whitelist_powered_heldout_is_not_significantly_profitable`.

### The sequence flip — DEAD

`runtime/diagnostics/sequence_flip.py` records its own verdict:
*"Tested: does NOT generalise, −$8 on held-out slices."*

### `structure_gate` — MEASURED, NOT PROMOTED

Measured end to end on the current recipe (corpus, one continuous
execution, `runtime/diagnostics/rejected_knob_ledgers.py`):

| book | legs | gross | fees | net | gross/leg |
|---|---|---|---|---|---|
| canonical | 7,122 | +$5,622.77 | $3,324.56 | **+$2,298.20** | +0.7895 |
| `structure_gate=True` | 3,731 | +$2,900.87 | $1,818.01 | **+$1,082.85** | +0.7775 |

It **loses $1,215 of net** against the canonical book. Its per-leg
`excess` is significantly negative on both sides (long −0.0345,
t = −2.19; short −0.1130, t = −5.41), so it does not buy timing skill
with the trades it removes. Promoting it would also break 33 call
sites that build no `StructureState`. Locked by
`tests/test_regressions.py::test_structure_gate_stays_off_until_its_plumbing_is_safe`.

### `trade_sides = "long"` — MEASURED, NOT PROMOTED (⚠️ SEE THE FLAG BELOW)

A long-only gold account has **no defence in a sell-off** — there is
nothing on the other side to net the loss against, and the book cannot
be flat in a downtrend by construction. That is a **structural** risk
argument, not a statistical one.

On the current recipe it is **more market-exposed** than the two-sided
book, and the DAILY test measures that decisively:

| book | legs | days | **daily corr** | **corr t** | r² | long excess/leg | short excess/leg |
|---|---|---|---|---|---|---|---|
| canonical (`both`) | 7,122 | 577 | +0.392 | +9.92 | 0.154 | −0.0412 (t = −3.60) | −0.1132 (t = −7.51) |
| **`trade_sides='long'`** | 4,371 | 528 | **+0.437** | **+10.74** | **0.191** | −0.0418 (t = −3.17) | — |
| `structure_gate=True` | 3,731 | 511 | +0.305 | +7.11 | 0.093 | −0.0345 (t = −2.19) | −0.1130 (t = −5.41) |

> **⚠️ FLAG: THE NET-PROFIT NUMBER NOW FAVOURS `trade_sides='long'`.**
> Measured on the corpus with the current recipe: **net +$2,731.05**
> (gross +$4,917.39, fees $2,186.33, 4,371 legs) versus the canonical
> book's **+$2,298.20**. It is better on net **and** on gross/leg
> (+1.1250 vs +0.7895) — the short side is net-negative on the current
> book, so removing it raises the total.
>
> The knob therefore stays OFF as a **deliberate risk choice** (a
> concentrated long-only book with no hedge, and a *higher* daily
> market correlation than the two-sided recipe), **not** because it
> measured worse. Its old justification — *"raises market correlation
> to +0.464"* — is **RETIRED**: that was a MONTHLY regression (n = 13..31,
> Fisher-z **t = 1.86**, inside its own noise band) and it does not
> survive the daily test. See AGENTS.md § 1.1 rule 7.
>
> The negative short alpha is a REAL finding and it is attacked where
> it is safe to attack it — by gating the short side on market
> structure sequences, so the side stays alive but only trades its own
> profitable sequences.

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
