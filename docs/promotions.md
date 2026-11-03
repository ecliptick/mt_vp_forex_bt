# Promotions, dead features, and the NB lineage

> Relocated from `AGENTS.md` on 2026-11-03. What shipped, what was measured
> and rejected, and the numbers behind each promotion.
>
> The **current canonical values** are in `AGENTS.md`. This is the history.

## Contents

- [The 5-event per-side sequence whitelist](#the-5-event-per-side-sequence-whitelist--2026-11-04)
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

## The 5-event per-side sequence whitelist — 2026-11-04

**PROMOTED** as `structure_seq_whitelist` — a set of **25
`<side>|<5-char signature>` keys** (18 long, 7 short).

> **The user asked (2026-11-03):** *"tabulate bos/choch 5-event
> sequence and the subsequent entries. split between longs and shorts.
> then, we know which 5-seq events to whitelist before each
> long/short"*.

### What it is

A **whitelist**: a key PRESENT means **TAKE** that trade, ABSENT means
**DROP** it. The signature is the trailing **5** BoS/CHoCH events at
the **signal** bar, oldest first (`b`=BoS bull, `B`=BoS bear,
`c`=CHoCH bull, `C`=CHoCH bear) — e.g. `long|Ccbbb`.

It is a **separate knob** from `structure_seq_drop_set`: the drop set
is 1-event granularity (*"what is the last break?"*), the whitelist is
5-event (*"what are the last FIVE breaks?"*). Different questions,
different evidence, so they move independently.

### The measurement — corpus-wide, one continuous execution

`runtime/diagnostics/seq5_table.py`, full corpus 2024-01-01 →
2026-07-16 (14d warmup prefix executed and discarded), 64,978 legs on
the unfiltered book. A signature qualifies when gross/leg is positive
on **both halves** of the corpus.

**LONG — 18 of 64 qualify:**

| signature | n | gross/leg | H1 | H2 |
|---|---|---|---|---|
| `Ccbbb` | 586 | +0.565 | +0.199 | +0.952 |
| `cCBcC` | 517 | +0.440 | +0.118 | +0.762 |
| `cCcCc` | 590 | +0.366 | +0.061 | +0.617 |
| `cbCcC` | 479 | +0.305 | +0.365 | +0.239 |
| `bCcbb` | 658 | +0.305 | +0.222 | +0.382 |
| `CcbCc` | 619 | +0.266 | +0.257 | +0.277 |
| `cCcCB` | 530 | +0.252 | +0.486 | +0.038 |
| `CBcbC` | 529 | +0.237 | +0.090 | +0.388 |
| `cCcbb` | 568 | +0.232 | +0.049 | +0.418 |
| `BcCcC` | 533 | +0.225 | +0.187 | +0.253 |
| `CcbbC` | 561 | +0.221 | +0.267 | +0.173 |
| `bbbbC` | 615 | +0.161 | +0.034 | +0.290 |
| `CcbCB` | 495 | +0.140 | +0.144 | +0.137 |
| `bCcCc` | 582 | +0.117 | +0.264 | +0.003 |
| `bbCBB` | 475 | +0.099 | +0.057 | +0.131 |
| `CcCcb` | 547 | +0.078 | +0.135 | +0.019 |
| `bbCcb` | 631 | +0.059 | +0.098 | +0.015 |
| `CBBcb` | 570 | +0.049 | +0.001 | +0.094 |

**SHORT — 7 of 64 qualify:**

| signature | n | gross/leg | H1 | H2 |
|---|---|---|---|---|
| `BcCcC` | 592 | +0.480 | +0.390 | +0.570 |
| `CBBcC` | 435 | +0.415 | +0.633 | +0.153 |
| `cbbCB` | 487 | +0.364 | +0.167 | +0.539 |
| `BBcbC` | 442 | +0.364 | +0.028 | +0.735 |
| `cCBBB` | 430 | +0.221 | +0.019 | +0.417 |
| `BBcbb` | 529 | +0.164 | +0.078 | +0.271 |
| `BBBcC` | 440 | +0.149 | +0.012 | +0.301 |

### The walk-forward — the test that separates mining from signal

`runtime/diagnostics/seq5_walkforward.py`: fit the whitelist on ONE
half of the corpus, score it on the OTHER, both ways round. The null
is **2000 random whitelists of the same size** — "trade less at
random" — because a random whitelist also produces a positive delta
by saving fees.

| side | fold | real Δgross | null p95 | percentile |
|---|---|---|---|---|
| long | H1 → H2 | +$1,159 | +$1,725 | 69.3% |
| long | H2 → H1 | +$1,069 | +$973 | **97.9%** |
| short | H1 → H2 | +$3,504 | +$3,506 | **95.0%** |
| short | H2 → H1 | +$2,258 | +$2,386 | **87.0%** |

**The short side is the one that earns the slot** — both of its folds
beat the random-whitelist null. The long side's folds disagree
(69% vs 98%), so the long set is the weaker half.

Pooled out-of-sample: long gross −$2,863 → −$634 (Δ +$2,229), short
gross −$6,611 → −$848 (Δ +$5,762). The dropped trades carry a
decisively negative t (long −2.16 / −2.95, short −5.07 / −6.01).

### ⚠️ What this promotion does NOT claim

1. **The kept book is still gross-NEGATIVE** and its per-leg edge is
   **fee-limited**. This is a **QUALITY** knob, not an **EDGE** knob —
   the same shape of verdict as the promoted
   `vol_state_min_expand_prev_day` gate (AGENTS.md § 3.7.2): it makes
   the surviving trades much better; it does not make them profitable.
2. **The shipped set is a whole-corpus fit.** The walk-forward
   validates the *shape* of the rule, not the specific 25 keys. The
   **engine-level** walk-forward (fit the set on H1, rebuild the H2
   ledger with it) is still open.
3. **Post-hoc is not the book.** The table above is a leg filter. The
   engine-side whitelist re-runs the book, so the conflict policy, the
   same-direction cap and the `contra_close` netting are all
   re-evaluated.

### The methodological rule this promotion exposed

> **A drop-set or whitelist MUST be measured END TO END by re-running
> the engine.** The post-hoc leg filter is a *screen* for which
> hypotheses are worth a re-run — never the verdict itself.

`contra_close` nets opposing legs **pairwise**, so removing legs
removes the netting's counterparties. Measured on the corpus: the
post-hoc table said the `short|B` drop set was best (positive on both
halves); the engine-side re-run said it was the **worst** of the
three tested, because it destroyed the netting's counterparties.

Driver: `runtime/diagnostics/seq_whitelist_corpus_ab.py`.

### How this SUPERSEDES the 2026-10-24 "whitelist is DEAD" finding

The 2026-10-24 finding (`sequence_whitelist.py`) reported the
whitelist as **empty**. That emptiness was a **sample-size artefact,
not an EV verdict**: after the vol gate the mean was 5.5 legs per
signature against the report's own `min_n` floor of 15, so *no*
signature could ever qualify. On the full corpus each signature has
~500 legs and 25 of the 64 clear both halves.

The `test_structure_whitelist_monthly_report_is_empty_and_superseded`
guard now locks that historical report as-is, and the live promotion is
locked by `test_five_event_sequence_whitelist_is_promoted`.

**What did NOT change:** the coarse 3-bucket **polarity** filter is
still dead (`test_structure_whitelist_powered_heldout_is_not_significantly_profitable`).
**A coarse polarity bucket does not carry the signal — the specific
5-event shape does.** That is precisely why the promotion is
per-signature and not per-polarity.

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

| threshold | legs | legs/day | gross | fees | net | **EV (gross/leg)** | fee/leg | excess/leg |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 0.0 (off) | 11,361 | 12.26 | $2,842.28 | $5,763.29 | −$2,921.01 | 0.2502 | 0.5073 | −0.0617 |
| 0.7 | 9,158 | 9.88 | $2,909.64 | $4,606.76 | −$1,697.13 | 0.3177 | 0.5030 | −0.0654 |
| 0.85 | 7,951 | 8.58 | $2,535.63 | $4,000.24 | −$1,464.61 | 0.3189 | 0.5031 | −0.0565 |
| **1.0 (SHIPPED)** | 6,753 | 7.28 | $2,318.84 | $3,393.04 | **−$1,074.20** | **0.3434** | 0.5024 | −0.0589 |

**EV rises monotonically with the threshold and peaks at `1.0`**:
**+0.2502 → +0.3434 (+37%)**. The net improves in step (−$2,921 →
−$1,074) because the fee bill falls faster than the gross.

### Why it promotes

1. **EV is up at every threshold**, and the maximum is at `1.0`.
2. **Net improves monotonically** alongside it.
3. **The knob is LIVE** — it is not a no-op that merely reshuffles
   the book.

### The honest limits (recorded, not hidden)

* **The EV lift is NOT statistically separable** from its
  neighbours: 1.0 vs gate-off is t = **+1.18**; 1.0 vs 0.85 is
  t = **+0.29**. The EV curve is **flat across 0.85–1.0** — 1.0 is
  the argmax of a flat region, not a sharp optimum.
* **The book is still NEGATIVE** (−$1,074). The gate **reduces a
  loss**; it does not make the book profitable. EV/leg ($0.3434)
  stays **below** the measured round trip ($0.5024) — the strategy
  remains **fee-limited** by the `AGENTS.md` § 5.2 test.
* **`excess` (the timing component, as opposed to drift) stays
  NEGATIVE** at every threshold (t = −6.41 at 1.0). The gross is
  still dominated by drift.
* **Trade count is far below the target band**: 7.28
  legs/calendar-day, and **94.6% of days trade fewer than 30 legs**.
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
