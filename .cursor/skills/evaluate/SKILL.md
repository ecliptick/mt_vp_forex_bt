---
name: evaluate
description: >-
  Measure and judge the SNIPER XAUUSD strategy at the LEG level using the
  corpus-wide ledger, the four (side x outcome) buckets, and the
  drift/excess decomposition. Use when asked to evaluate the recipe, score a
  variant, check whether a change helped, read the buckets, compute the payoff
  geometry, or answer "is there an edge?" — and before promoting any knob.
disable-model-invocation: true
---

# Evaluate — the leg-level verdict

## The one rule that governs everything

**The unit of evaluation is the LEG, not the month.** A month is a reporting
dimension, never an execution boundary and never a unit of evaluation.

Why: the recipe produces ~55 legs a day. "8 months" is n=8 draws from a
high-variance distribution, and the month's own return says nothing about the
in-month distribution the strategy experienced. Worse, the month boundary is an
**engine artefact**: the p95 cap (7d), the swing cap (24h) and the VP profile
(7d rolling) are history-dependent, so a cold chunk trades its first ~8 days
with UNCAPPED targets. Chunked execution silently discards ~10% of the trade
universe.

## The workflow

### Step 1 — check the registry FIRST

Before running anything, ask whether this measurement already exists:

```bash
python -m runtime.results.cli find --kind leg_ledger
python -m runtime.results.cli stale     # runs on an older commit
```

If the params AND the commit match, the number is already known. Reuse it.

### Step 2 — build/reuse the ledger

```bash
python -m runtime.diagnostics.leg_ledger
```

One continuous run over the corpus with a 14-day warmup prefix (the prefix's
legs are DISCARDED; only its warmup effect is kept). Cached by fingerprint —
~1 s on a hit, ~5 min cold on the wide book.

### Step 3 — read the buckets

```bash
python -m runtime.diagnostics.leg_buckets
```

Four buckets: `long_win`, `long_loss`, `short_win`, `short_loss`. Each reports
`n`, gross, net, mean per leg, and the **standard error of that mean**.

### Step 4 — answer the three questions

**Q1 — is there an edge at all?**
Compare per-leg gross against the **MEASURED** round trip (`fee_per_leg`, not
the flat $0.47 spec — the measured fee on the full corpus is $0.5029). The
report prints `gross_per_leg / fee_per_leg` so the comparison is explicit.

> A gross edge that does not cover the round trip is **not an edge**.

**Q2 — is the edge direction-independent?**
The drift/excess decomposition:

```
drift  = (close[exit_bar] - close[entry_bar]) * direction * qty
excess = gross - drift
```

`drift` is the directional/beta component — what any position of that size and
side would have earned by being there. `excess` is the timing component, which
a directional proxy cannot have.

This is an **exact algebraic identity**, not an estimate. No regression, no
monthly aggregation, no regime label.

Current reading: excess is **negative on both books** (narrow: −$155.00,
t = −2.96; wide: −$4,080.07, t = −15.63) and negative for **every** exit
reason. The realised P&L is market drift.

**Q3 — is the loss concentrated?**
A strategy whose losses are all the same size is a **geometry** problem; a fat
loss tail is a **risk** problem.

### Step 5 — the payoff geometry

For each side: `avg_win`, `avg_loss`, `payoff_ratio`, break-even win rate, and
the edge in percentage points:

```
edge_pp = 100 * (hit_rate - p_be)
```

| condition | diagnosis | fix |
|---|---|---|
| `edge_pp <= 0` | **no signal** — loses before fees | better entries |
| `edge_pp > 0` but `gross_per_leg < fee` | **fee-limited** | fewer/bigger trades |
| `gross_per_leg >= fee` | profitable | — |

The two failure modes need **different fixes**. Never collapse them.

## Traps that have already cost this repo a result

**The flip is a tautology.** `gross = (exit - entry) * direction * qty`, so
`flip_gross = -gross` EXACTLY. It carries zero information. Any conclusion
drawn from it is vacuous.

**The signal bar is the causal bar.** Every per-side gate is evaluated at the
signal, so the ledger keys legs by SIGNAL time, not fill time. A bullish BoS
invalidates the opposing bear FVGs, so `short|b` is **structurally empty** —
not a weak bucket, an empty one. Measuring at the fill bar produces a
clean-looking promotable result that measures the wrong thing.

**The canonical ledger is CANONICAL-ONLY.** `build_ledger` raises
`SystemExit` if a variant tries to write the canonical path. Give a variant
its own `out_path`.

**Use the measured fee.** The report gives both `fee_per_leg` (measured) and
`fee_per_leg_spec` ($0.47). The verdict uses the measured one; the flat spec
overstates the edge by ~7%.

## What the leg view does NOT claim

- A positive per-leg EV that does not cover the round trip is not an edge.
- `excess` is the timing component of the **realised** P&L. It does not by
  itself make the strategy tradeable — the fee bill still has to be paid.
- A near-zero gross total makes any gross *ratio* (e.g. `drift_share_of_gross`)
  meaningless. Read the **absolute totals**.

## Reference

- [`docs/evaluation.md`](../../../docs/evaluation.md) — the full methodology, the
  current verdict, the timing-control correction, the zone-shuffle placebo.
- [`AGENTS.md`](../../../AGENTS.md) § 5 — the harness commands.
