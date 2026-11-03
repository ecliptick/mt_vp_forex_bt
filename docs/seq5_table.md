# The 5-event BoS/CHoCH sequence table (per side)

> **Status: MEASURED and PROMOTED 2026-11-04.** Corpus-wide, one
> continuous execution over 2024-01-01 → 2026-07-16 (14-day warmup
> prefix). Canonical recipe, **both** the 1-event drop set and the
> 5-event whitelist DISABLED (so the table sees the unfiltered book)
> — **37,159 legs**.
>
> **This document supersedes the 2026-11-03 edition TWICE.**
> 1. The recipe changed at 20:59 local on 2026-11-03 (the trailing
>    stop became the canonical exit, `tp_mode` moved to `hvn_single`,
>    the SL ceiling moved, and the `vol_state_min_expand_prev_day`
>    gate went live). The earlier edition's figures were measured on
>    the PRE-trailing-stop book (64,978 legs) and do not reconcile.
> 2. **The set was RE-FITTED on 2026-11-04** — 25 keys → 21 keys.
>    Only 14 of the old 25 survived the re-fit, because the book
>    itself changed. See § 8 for the cache-fingerprint bug that
>    allowed two books to coexist under one nominal config.

## The question

> "tabulate bos/choch 5-event sequence and the subsequent entries.
> split between longs and shorts. then, we know which 5-seq events to
> whitelist before each long/short"

## What was measured

| item | value |
|---|---|
| corpus | 2024-01-01 → 2026-07-16, one continuous run, 14d warmup |
| legs (unfiltered) | **37,159** |
| signature | trailing **5** BoS/CHoCH events at the leg's **SIGNAL bar** |
| alphabet | `b`=BoS bull, `B`=BoS bear, `c`=CHoCH bull, `C`=CHoCH bear |
| consistency test | each signature reported on the corpus's first half (H1) and second half (H2) |
| min n | 50 per signature |

Diagnostics:
`runtime/diagnostics/seq5_table.py` (the table),
`runtime/diagnostics/seq5_walkforward.py` (out-of-sample + null),
`runtime/diagnostics/seq5_ksweep.py` (how many signatures to keep),
`runtime/diagnostics/seq5_kwalkforward.py` (ranked-K out-of-sample),
`runtime/diagnostics/seq_whitelist_corpus_ab2.py` (the engine-side A/B),
`runtime/diagnostics/seq_whitelist_equivalence.py` (engine ≡ post-hoc).

Artifacts: `runtime/diagnostics/_reports/seq5_table_fresh.json`,
`seq5_walkforward_fresh.json`, `seq5_ksweep.json`,
`seq5_kwalkforward.json`, `seq_whitelist_corpus_ab2.json`,
`seq_whitelist_equivalence.json`.

---

## 1. The 1-event buckets (the engine's 1-event granularity)

Every leg's last structure event, on the unfiltered wide book.

| side | last event | verdict |
|---|---|---|
| long | `b` / `B` / `c` / `C` | no 1-event bucket clears the "positive on both halves" bar |
| short | `b` / `B` / `c` / `C` | ditto |

**On the current recipe NO 1-event bucket clears the bar.** The signal
lives in the 5-event SHAPE, not in the last break. This is why the
1-event drop set was **removed** on 2026-11-04 (§ 7).

---

## 2. The 5-event signature table — LONG side

**13 of 64** signatures have positive gross/leg on BOTH halves. These
are the **shipped** long keys.

| signature | n | gross | gross/leg | hit | H1 g/leg | H2 g/leg |
|---|---|---|---|---|---|---|
| `Ccbbb` | 345 | +$268 | **+0.776** | 85.8% | +0.473 | +1.035 |
| `bCcbb` | 385 | +$216 | +0.561 | 82.1% | +0.584 | +0.542 |
| `cCcCB` | 313 | +$175 | +0.558 | 83.1% | +1.055 | +0.161 |
| `CcbCc` | 384 | +$203 | +0.529 | 83.3% | +0.621 | +0.441 |
| `cbCcC` | 298 | +$157 | +0.525 | 84.9% | +0.669 | +0.363 |
| `BcCcC` | 330 | +$131 | +0.396 | 82.1% | +0.342 | +0.433 |
| `bbCcb` | 362 | +$141 | +0.390 | 85.1% | +0.060 | +0.873 |
| `cCcCc` | 318 | +$123 | +0.386 | 79.2% | +0.095 | +0.636 |
| `bbbbC` | 365 | +$135 | +0.370 | 82.5% | +0.249 | +0.481 |
| `cCcbb` | 327 | +$89 | +0.273 | 81.0% | +0.113 | +0.473 |
| `CcCBc` | 359 | +$79 | +0.220 | 83.3% | +0.358 | +0.072 |
| `CcbbC` | 356 | +$55 | +0.154 | 82.6% | +0.111 | +0.199 |
| `CcbCB` | 291 | +$24 | +0.081 | 78.4% | +0.169 | +0.011 |

Near-misses (positive in-sample, but NOT positive on both halves, so
**rejected**): `bCBBB` +0.582 (H1 **−0.045**), `CBcbC` +0.554 (H1
**−0.088**), `BBcbC` +0.301 (H1 −0.034), `CcCcC` +0.249 (H1 −0.408).

The bottom of the long table: `CBBcC` −0.851, `cCBBc` −0.799,
`CBBBB` −0.698, `BcCBB` −0.624.

**The whitelist's effect on the long side (post-hoc):**

| | value |
|---|---|
| base legs | 19,064 |
| kept legs | 4,433 |
| base gross | −$522 |
| kept gross | **+$1,794** |
| Δ gross | **+$2,316** |
| dropped-trade t | **−3.64** |

---

## 3. The 5-event signature table — SHORT side

**8 of 64** signatures qualify. These are the **shipped** short keys.

| signature | n | gross | gross/leg | hit | H1 g/leg | H2 g/leg |
|---|---|---|---|---|---|---|
| `BBcbb` | 271 | +$194 | **+0.716** | 84.1% | +0.624 | +0.824 |
| `BcCcC` | 378 | +$201 | +0.532 | 78.8% | +0.229 | +0.819 |
| `cCcbb` | 294 | +$117 | +0.397 | 81.0% | +0.244 | +0.539 |
| `BBBcb` | 253 | +$88 | +0.349 | 76.7% | +0.372 | +0.325 |
| `Bcbbb` | 283 | +$92 | +0.325 | 80.9% | +0.175 | +0.487 |
| `CBBBB` | 202 | +$55 | +0.272 | 78.7% | +0.231 | +0.315 |
| `cCcbC` | 340 | +$66 | +0.195 | 78.8% | +0.155 | +0.239 |
| `CBcCc` | 291 | +$34 | +0.119 | 80.1% | +0.107 | +0.129 |

Near-misses: `CBBBc` +0.446 (H1 **−0.187**), `BBcbC` +0.276 (H1
−0.155), `CBBcC` +0.254 (H2 **−0.323**), `CcbCB` +0.223 (H1 −0.151).

The worst short signatures: `BcbCB` −1.520, `BcCBc` −1.414,
`CBBcb` −1.203, `bCcCB` −0.884.

**The whitelist's effect on the short side (post-hoc):**

| | value |
|---|---|
| base legs | 18,085 |
| kept legs | 2,312 |
| base gross | −$4,216 |
| kept gross | **+$848** |
| Δ gross | **+$5,063** |
| dropped-trade t | **−7.45** |

---

## 4. THE WALK-FORWARD — the test that separates mining from signal

Fit the whitelist on ONE half of the corpus, score it on the OTHER,
both ways round. The null is **2000 random whitelists of the same
size** — "trade less at random" — because a random whitelist also
produces a positive delta by saving fees.

### LONG

| fold | real Δgross | null p95 | percentile |
|---|---|---|---|
| fit H1 → test H2 | +$593 | +$888 | 77.0% |
| fit H2 → test H1 | +$449 | +$381 | **97.9%** |

### SHORT

| fold | real Δgross | null p95 | percentile |
|---|---|---|---|
| fit H1 → test H2 | +$1,605 | +$2,121 | 56.3% |
| fit H2 → test H1 | +$1,495 | +$1,550 | **91.7%** |

**Read.** The **H2→H1 fold** is the one that earns the slot on each
side (98% and 92%). The other two folds — 77.0% on long and **56.3%**
on short — are barely better than chance. That is recorded here
rather than smoothed over: the read is weaker than the pre-re-fit
edition's (which showed 69/98 long and 95/87 short).

---

## 5. THE TRADE-COUNT CONSTRAINT (the K sweep)

`runtime/diagnostics/seq5_ksweep.py` answers "how many signatures do
you have to keep to stay inside the 30–70 legs/day band?".

| K (of 64/side) | legs | legs/day | gross | gross/leg | in band? |
|---|---|---|---|---|---|
| 21 (the shipped set) | 7,343 | 7.92 | +$3,167 | +0.4314 | no |
| 25 | 14,947 | 16.12 | +$3,874 | +0.2592 | no |
| 48 | 28,042 | 30.25 | +$1,095 | +0.0391 | **YES** |
| 53 | 30,756 | 33.18 | −$78 | −0.0025 | YES |
| 64 (no filter) | 37,149 | 40.07 | −$4,738 | −0.1275 | YES |

**To reach 30 legs/day the whitelist must keep 48 of 64 signatures per
side** — i.e. it stops being a whitelist. The full-strength set is the
one that carries the signal, and the user explicitly waived the band
for it (*"dont need to be too strict on trade count for this"*).

---

## 6. THE END-TO-END A/B — the verdict

`runtime/diagnostics/seq_whitelist_corpus_ab2.py`: **7 arms**, each a
full corpus continuous re-run. A post-hoc filter cannot see
`contra_close`'s pairwise hedge netting, so this is the only
measurement that counts.

| arm | wl | drop set | legs | legs/day | gross | fees | net | gross/leg |
|---|---|---|---|---|---|---|---|---|
| unfiltered | 0 | 0 | 37,159 | 40.09 | −$4,742 | $18,349 | −$23,091 | −0.1276 |
| old (25 keys) | 25 | 0 | 7,926 | 8.55 | +$2,599 | $3,966 | −$1,367 | +0.3279 |
| old + drop set | 25 | 2 | 6,753 | 7.28 | +$2,319 | $3,393 | −$1,074 | +0.3434 |
| **split (21 keys) — SHIPPED** | **21** | **0** | **6,663** | **7.19** | **+$2,583** | $3,324 | **−$741** | **+0.3877** |
| split + drop set | 21 | 2 | 5,664 | 6.11 | +$2,357 | $2,824 | −$467 | +0.4161 |
| ranked-21/side | 42 | 0 | 12,655 | 13.65 | +$3,917 | $6,312 | −$2,395 | +0.3095 |
| ranked-21/side + drop set | 42 | 2 | 9,377 | 10.12 | +$3,414 | $4,694 | −$1,281 | +0.3640 |

The shipped set beats the old set on **both** gross per leg (+0.3877
vs +0.3279) and net (−$741 vs −$1,367).

**Engine ≡ post-hoc is proven, not assumed.**
`runtime/diagnostics/seq_whitelist_equivalence.py` runs the engine
with the whitelist and compares it against the post-hoc filter on the
same run, on all 8 benchmark slices: leg count AND gross P&L match to
the cent, and the gate fires on every slice. Output:
`ALL SLICES EQUIVALENT: True`.

---

## 7. WHAT WAS PROMOTED, AND WHAT WAS DELETED

**SHIPPED: `structure_seq_whitelist`** — a set of **21**
`<side>|<5-char signature>` keys (13 long, 8 short).

**DELETED: `structure_seq_drop_set`** (the 1-event blacklist). It was
not measured wrong; it was **redundant and contradictory** to the
whitelist. It vetoed 3 of the 8 whitelisted short shapes — `BcCcC`
(the best short shape), `CBcCc`, `cCcbC` — because their last event
is a reversal. Its isolated effect on the same 21-key whitelist:

| | gross | net | legs |
|---|---|---|---|
| drop set OFF | +$2,583 | −$741 | 6,663 |
| drop set ON | +$2,357 | −$467 | 5,664 |
| **Δ (ON − OFF)** | **−$226** | **+$274** | **−999** |

It costs 999 legs and **$226 of GROSS** to save **$274 of NET**. A
net-only win that *loses* gross is a **fee artefact, not a signal**.

---

## 8. THE CACHE-FINGERPRINT BUG (why the re-fit was needed)

> `bt/_engine/vol_state.py` produced the `vol_state` artifact, which
> **IS pickled into the artifact cache** — but the module was in
> **neither** `runtime/sweeps/cached_sweep.py::_ARTIFACT_SOURCES`
> **nor** `runtime/diagnostics/leg_ledger.py::_source_hash()`'s file
> list. So editing it never invalidated the cache, and a stale
> `vol_state` kept being served.

The symptom was silent and large: **one nominal config produced two
different books** — 64,978 legs (53,568 vol-gate drops) on one
ledger and 37,159 legs (90,620 drops) on another. The first set was
fitted on the stale book and did not describe the second.

**FIXED 2026-11-04** in both places. The rule, now written into the
code: *if a module's output is pickled into the artifact, that module
belongs in the fingerprint tuple.*

---

## 9. RECOMMENDATION

1. **SHIPPED: the 5-event per-side whitelist** (21 keys), and the
   1-event drop set is **REMOVED**.
2. **The next test is the ENGINE-LEVEL walk-forward**: fit the set on
   H1, rebuild the H2 ledger with it, and vice versa. That is the
   only test that validates the specific 21 keys rather than the
   shape of the rule.
3. **Do not promote the coarse POLARITY filter** — it is dead
   (held-out aggregate t = −2.15). The signal is in the specific
   5-event shape, not in the bull/bear/mixed bucket.

---

## Reproduce

```bash
# the unfiltered corpus ledger (both filters disabled)
python -m runtime.diagnostics.leg_ledger \
  --out runtime/diagnostics/_reports/leg_ledger_seqwl_unfiltered.json

python -m runtime.diagnostics.seq5_table \
  --ledger runtime/diagnostics/_reports/leg_ledger_seqwl_unfiltered.json \
  --min-n 50 --out runtime/diagnostics/_reports/seq5_table_fresh.json

python -m runtime.diagnostics.seq5_walkforward \
  --ledger runtime/diagnostics/_reports/leg_ledger_seqwl_unfiltered.json

python -m runtime.diagnostics.seq5_ksweep \
  --ledger runtime/diagnostics/_reports/leg_ledger_seqwl_unfiltered.json --min-n 50

# the engine-side A/B (SEVEN full corpus re-runs)
python -m runtime.diagnostics.seq_whitelist_corpus_ab2

# engine == post-hoc, on all 8 benchmark slices
python -m runtime.diagnostics.seq_whitelist_equivalence
```
