# The 5-event BoS/CHoCH sequence table (per side)

> **Status: MEASURED and PROMOTED 2026-11-04.** Corpus-wide, one
> continuous execution over 2024-01-01 → 2026-07-16 (14-day warmup
> prefix). Canonical recipe, `structure_seq_drop_set` DISABLED (so the
> table sees the unfiltered book) — 64,978 legs.
>
> **This document supersedes the 2026-11-03 edition.** The recipe
> changed at 20:59 local on 2026-11-03 (the trailing stop became the
> canonical exit), which moved every number. The earlier edition's
> figures were measured on the PRE-trailing-stop book and do not
> reconcile with this one.

## The question

> "tabulate bos/choch 5-event sequence and the subsequent entries.
> split between longs and shorts. then, we know which 5-seq events to
> whitelist before each long/short"

## What was measured

| item | value |
|---|---|
| corpus | 2024-01-01 → 2026-07-16, one continuous run, 14d warmup |
| legs (unfiltered) | **64,978** |
| signature | trailing **5** BoS/CHoCH events at the leg's **SIGNAL bar** |
| alphabet | `b`=BoS bull, `B`=BoS bear, `c`=CHoCH bull, `C`=CHoCH bear |
| consistency test | each signature reported on the corpus's first half (H1) and second half (H2) |

Diagnostics:
`runtime/diagnostics/seq5_table.py` (the table),
`runtime/diagnostics/seq5_walkforward.py` (out-of-sample + null),
`runtime/diagnostics/seq_whitelist_corpus_ab.py` (the engine-side A/B),
`runtime/diagnostics/seq_drop_set_corpus_ab.py` (the drop-set A/B).

Artifacts: `runtime/diagnostics/_reports/seq5_table_none.json`,
`seq5_walkforward.json`, `seq_whitelist_corpus_ab.json`,
`seq_drop_set_corpus_ab.json`.

---

## 1. The 1-event buckets (the engine's 1-event granularity)

Every leg's last structure event, on the unfiltered wide book.

| side | last event | n | gross | gross/leg | hit | H1 g/leg | H2 g/leg | consistent? |
|---|---|---|---|---|---|---|---|---|
| long | `b` BoS bull | 8,642 | −$425 | −0.049 | 80.1% | — | — | no |
| long | `B` BoS bear | 7,237 | −$897 | −0.124 | 79.0% | — | — | no |
| long | `c` CHoCH bull | 8,781 | −$1,024 | −0.117 | 79.9% | — | — | no |
| long | `C` CHoCH bear | 8,558 | −$518 | −0.061 | 79.9% | — | — | no |
| short | `b` BoS bull | 8,327 | −$1,453 | −0.174 | 78.3% | — | — | no |
| short | `B` BoS bear | 6,951 | −$2,072 | −0.298 | 77.4% | — | — | no |
| short | `c` CHoCH bull | 8,369 | −$1,428 | −0.171 | 78.0% | — | — | no |
| short | `C` CHoCH bear | 8,103 | −$1,658 | −0.205 | 78.1% | — | — | no |

**On the current recipe NO 1-event bucket clears the "positive on both
halves" bar** — the 1-event granularity carries no signal by itself.
The signal lives in the 5-event SHAPE.

---

## 2. The 5-event signature table — LONG side

18 of 64 signatures have positive gross/leg on BOTH halves.

| signature | n | gross | gross/leg | hit | H1 g/leg | H2 g/leg |
|---|---|---|---|---|---|---|
| `Ccbbb` | 586 | +$331 | **+0.565** | 84.3% | +0.199 | +0.952 |
| `cCBcC` | 517 | +$227 | +0.440 | 82.0% | +0.118 | +0.762 |
| `cCcCc` | 590 | +$216 | +0.366 | 81.2% | +0.061 | +0.617 |
| `cbCcC` | 479 | +$146 | +0.305 | 80.6% | +0.365 | +0.239 |
| `bCcbb` | 658 | +$200 | +0.305 | 81.0% | +0.222 | +0.382 |
| `CcbCc` | 619 | +$165 | +0.266 | 80.3% | +0.257 | +0.277 |
| `cCcCB` | 530 | +$133 | +0.252 | 80.6% | +0.486 | +0.038 |
| `CBcbC` | 529 | +$126 | +0.237 | 81.1% | +0.090 | +0.388 |
| `cCcbb` | 568 | +$132 | +0.232 | 80.8% | +0.049 | +0.418 |
| `BcCcC` | 533 | +$120 | +0.225 | 80.9% | +0.187 | +0.253 |
| `CcbbC` | 561 | +$124 | +0.221 | 80.9% | +0.267 | +0.173 |
| `bbbbC` | 615 | +$99 | +0.161 | 80.3% | +0.034 | +0.290 |
| `CcbCB` | 495 | +$70 | +0.140 | 80.2% | +0.144 | +0.137 |
| `bCcCc` | 582 | +$68 | +0.117 | 80.4% | +0.264 | +0.003 |
| `bbCBB` | 475 | +$47 | +0.099 | 79.8% | +0.057 | +0.131 |
| `CcCcb` | 547 | +$42 | +0.078 | 79.5% | +0.135 | +0.019 |
| `bbCcb` | 631 | +$38 | +0.059 | 81.6% | +0.098 | +0.015 |
| `CBBcb` | 570 | +$28 | +0.049 | 80.4% | +0.001 | +0.094 |

The bottom of the long table (all rejected): `BBcbC` +0.048 (H1
**−0.459**), `BcCBc` +0.028 (H1 −0.297), `CcCcC` −0.020.

## 3. The 5-event signature table — SHORT side

7 of 64 signatures qualify.

| signature | n | gross | gross/leg | hit | H1 g/leg | H2 g/leg |
|---|---|---|---|---|---|---|
| `BcCcC` | 592 | +$284 | **+0.480** | 80.4% | +0.390 | +0.570 |
| `CBBcC` | 435 | +$180 | +0.415 | 83.0% | +0.633 | +0.153 |
| `cbbCB` | 487 | +$177 | +0.364 | 81.7% | +0.167 | +0.539 |
| `BBcbC` | 442 | +$161 | +0.364 | 82.4% | +0.028 | +0.735 |
| `cCBBB` | 430 | +$95 | +0.221 | 80.9% | +0.019 | +0.417 |
| `BBcbb` | 529 | +$87 | +0.164 | 82.6% | +0.078 | +0.271 |
| `BBBcC` | 440 | +$65 | +0.149 | 79.8% | +0.012 | +0.301 |

The worst short signatures: `bbbbb` −0.121, `CBBBB` −0.116,
`BBBcb` −0.101, `BcCBB` −0.084.

---

## 4. THE WALK-FORWARD — the test that separates mining from signal

Fit the whitelist on ONE half of the corpus, score it on the OTHER,
both ways round. The null is **2000 random whitelists of the same
size** — "trade less at random" — because a random whitelist also
produces a positive delta by saving fees.

### LONG

| fold | fitted | test legs | kept | d gross | d net | null p95 | percentile |
|---|---|---|---|---|---|---|---|
| fit H1 → test H2 | 34 sigs | 16,854 | 9,400 | +$1,159 | +$5,688 | +$1,725 | 69.3% |
| fit H2 → test H1 | 28 sigs | 16,364 | 7,061 | +$1,069 | +$4,702 | +$973 | **97.9%** |

Pooled out-of-sample: gross −$2,863 → −$634 (d +$2,229),
gross/leg −$0.0862 → −$0.0385, dropped-trade t −2.16 / −2.95.

### SHORT

| fold | fitted | test legs | kept | d gross | d net | null p95 | percentile |
|---|---|---|---|---|---|---|---|
| fit H1 → test H2 | 15 sigs | 15,635 | 3,519 | +$3,504 | +$10,860 | +$3,506 | **95.0%** |
| fit H2 → test H1 | 22 sigs | 16,115 | 5,722 | +$2,258 | +$6,301 | +$2,386 | **87.0%** |

Pooled out-of-sample: gross −$6,611 → −$848 (d +$5,762),
gross/leg −$0.2082 → −$0.0918, dropped-trade t −5.07 / −6.01.

**Read.** The SHORT side is the one that earns the slot: both folds
beat the random-whitelist null (95%, 87%). The long side's folds
**disagree** (69% vs 98%), so the long set is the weaker half of the
promotion.

---

## 5. WHAT WAS PROMOTED

`structure_seq_whitelist` — a set of **25** `<side>|<5-char signature>`
keys (**18 long, 7 short**), the union of the tables above.

**It is a WHITELIST: a key PRESENT means TAKE the trade, ABSENT means
DROP it.** It is a SEPARATE knob from `structure_seq_drop_set` (which
is a 1-event BLACKLIST), because they answer different questions at
different granularity.

### The honest limits

1. **The kept book is still gross-NEGATIVE** (long −$634, short −$848
   out-of-sample) and its per-leg edge is **fee-limited**: kept
   gross/leg −$0.039 against a measured fee/leg of $0.515. **This is a
   QUALITY knob, not an EDGE knob** — the same shape of verdict as
   the promoted `vol_state_min_expand_prev_day` gate (AGENTS.md
   § 3.7.2).
2. **The shipped set is a whole-corpus fit.** The walk-forward
   validates the SHAPE of the rule (fit on one half, score on the
   other). The **engine-level** walk-forward — fit the set on H1,
   rebuild the H2 ledger with it, and vice versa — is the remaining
   test.
3. **Post-hoc is not the book** — see § 6.

---

## 6. THE RULE THIS EXPOSED: a drop-set must be measured END TO END

`contra_close` nets opposing legs **pairwise**, so removing legs
removes the netting's **counterparties**. A post-hoc filter cannot see
that. Measured on the corpus (`seq_drop_set_corpus_ab.py`):

| variant | legs | legs/day | gross | fees | net | gross/leg |
|---|---|---|---|---|---|---|
| unfiltered (`drop_set=None`) | 64,978 | 70.09 | −$9,478 | $32,419 | −$41,897 | −0.1459 |
| canonical `{short\|c,short\|C}` | 48,188 | 51.98 | −$6,476 | $24,231 | −$30,707 | −0.1344 |
| `{short\|B}` | 57,883 | 62.44 | −$7,563 | $28,936 | −$36,499 | −0.1307 |
| `{short\|B,short\|b}` | 49,452 | 53.35 | −$5,973 | $24,823 | −$30,796 | −0.1208 |
| `{short\|B,short\|c,short\|C}` | 41,102 | 44.34 | −$4,592 | $20,748 | −$25,340 | −0.1117 |

> **RULE: a drop-set / whitelist must be measured END TO END by
> re-running the engine.** The post-hoc leg filter is a *screen* for
> which hypotheses are worth a re-run — never the verdict itself. The
> contra-close netting makes any pairwise-coupled feature
> non-decomposable.

---

## 7. RECOMMENDATION

1. **SHIPPED: the 5-event per-side whitelist** (25 keys). It is a
   QUALITY knob — the kept trades are much better, the book is still
   fee-limited.
2. **The next test is the ENGINE-LEVEL walk-forward**: fit the set on
   H1, rebuild the H2 ledger with it, and vice versa. That is the
   only test that validates the specific 25 keys rather than the
   shape of the rule.
3. **The 1-event drop set is now WEAKER than the whitelist** — no
   1-event bucket clears the both-halves bar on the current recipe.
   Whether the two should coexist is an open question; the engine
   applies the drop set FIRST, so a short leg dropped by the drop set
   never reaches the whitelist gate.
4. **Do not promote the coarse POLARITY filter** — it is dead
   (held-out aggregate t = −2.15). The signal is in the specific
   5-event shape, not in the bull/bear/mixed bucket.

---

## Reproduce

```bash
# the unfiltered corpus ledger (must run with BOTH filters disabled)
python -m runtime.diagnostics.leg_ledger \
  --out runtime/diagnostics/_reports/leg_ledger_seqdrop_none.json

python -m runtime.diagnostics.seq5_table \
  --ledger runtime/diagnostics/_reports/leg_ledger_seqdrop_none.json \
  --min-n 100 --out runtime/diagnostics/_reports/seq5_table_none.json

python -m runtime.diagnostics.seq5_walkforward \
  --ledger runtime/diagnostics/_reports/leg_ledger_seqdrop_none.json

# the engine-side A/B (three full corpus re-runs)
python -m runtime.diagnostics.seq_whitelist_corpus_ab
```
