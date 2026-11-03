---
name: evaluate
description: >-
  Measure and judge the SNIPER XAUUSD strategy at the LEG level using the
  corpus-wide ledger, the four (side x outcome) buckets, and the
  per-leg passive-hold/excess decomposition. Use when asked to evaluate the
  recipe, score a variant, check whether a change helped, read the buckets,
  compute the payoff geometry, or answer "is there an edge?" — and before
  promoting any knob.
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

## The promotion criterion is NET PROFIT

> **A promotion is judged on the NET.** (User instruction, 2026-11-04:
> *"just promote whatever is most profitable. monthly aggs are meaningless for
> this high freq strat"*.)
>
> The passive-hold/excess decomposition (Q2) and the market-beta regression
> are **DIAGNOSTICS** — they explain WHY a book makes money. Neither may
> **block** a promotion on a positive net.
>
> Two measured reasons:
>
> 1. **The `excess` benchmark is not exchangeable with the execution model.**
>    The engine books P&L at the **actual SL / TP fill price** (85.9% of `sl`
>    legs fill exactly at the stop; 100% of the rest fill adverse), while
>    `passive_hold` is a **close-to-close** measurement. On a stop or target
>    exit the two differ BY CONSTRUCTION, so a per-bucket `excess` there is
>    the **execution-gap differential**, not a timing measure — and its sign
>    flips with book composition. Read the WHOLE-BOOK and PER-SIDE excess
>    (which do hold) for the diagnostic, never the per-bucket sign.
> 2. **A month is n=31 draws** at ~7 legs/day. The month is a REPORTING
>    dimension, never an evaluation boundary.

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
The per-leg passive-hold/excess decomposition:

```
passive_hold = (close[exit_bar] - close[entry_bar]) * direction * qty
excess       = gross - passive_hold
```

`passive_hold` is what any position of that size and side would have earned by
merely BEING there, over the same bars. `excess` is what the entries added on
top of that — the part a purely directional book cannot have.

This is an **exact algebraic identity**, not an estimate. No regression, no
monthly aggregation, no regime label, and its standard error uses
n = thousands of legs rather than n = 13..31 months.

> **⚠️ `excess` is a DIAGNOSTIC, not a promotion gate.** The ledger
> benchmarks a leg's realised P&L against a **close-to-close** passive hold,
> but the engine books P&L at the **actual SL / TP fill price**. Those are
> not exchangeable, so a stop or target bucket's `excess` is the
> **execution-gap differential**, not a timing measure — and its sign FLIPS
> with book composition. Read the **whole-book** and **per-side** excess for
> the diagnostic. See `tests/test_signal_bar_completeness.py` and AGENTS.md
> § 5.1.

**Q3 — is the loss concentrated?**
A strategy whose losses are all the same size is a **geometry** problem; a fat
loss tail is a **risk** problem.

### Step 4b — the daily direction-dependence view

When the question is specifically "is this book a directional bet on gold?",
use the **daily** aggregation — never a monthly one:

```bash
python -m runtime.diagnostics.daily_direction_dependence
```

It regresses **daily** gross on the **daily** market return (n ~= 577 days,
so the correlation's t-statistic is decisive) and reports the per-leg
decomposition alongside it. The verdict reads the **per-leg** `excess`; the
daily correlation is the aggregate view of the same question.

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
- `excess` is what the entries added on top of a passive hold. It does not by
  itself make the strategy tradeable — the fee bill still has to be paid, and
  it is not exchangeable with the engine's fill-price P&L.
- A near-zero gross total makes any gross *ratio* (e.g. the passive-hold
  share of gross) meaningless. Read the **absolute totals**.
- **A monthly correlation is never a verdict.** A monthly regression of gross
  on market return has n = 13..31, whose correlation standard error is ~0.32
  in Fisher-z space — every quoted monthly correlation in this repo's history
  sat inside its own noise band. Use the per-leg decomposition, or the
  **daily** aggregation (`daily_direction_dependence`).

## Reference

- [`docs/evaluation.md`](../../../docs/evaluation.md) — the full methodology, the
  current verdict, the timing-control correction, the zone-shuffle placebo.
- [`AGENTS.md`](../../../AGENTS.md) § 5 — the harness commands.
