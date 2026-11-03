---
name: promote
description: >-
  Promote a knob or feature into the SNIPER XAUUSD canonical recipe, or
  reject one. Use when a change has been measured and needs a promotion
  decision, when asked to promote a config value, to update the canonical
  config, to decide whether a feature ships, or to record why something was
  rejected.
disable-model-invocation: true
---

# Promote — decide, then change one thing

## Promotion is a DECISION with an evidence trail, not a code edit

A promotion changes what every future run measures. That makes it the
highest-stakes edit in the repo. The order is fixed:

**measure -> judge on the ledger -> decide -> change ONE thing -> re-measure
-> record -> document.**

## The gate: five questions

Answer all five. "Yes" to 1-3 and "No" to 4-5 is a promotion. Anything else
is not.

1. **Was it measured on the CONTINUOUS corpus?** A monthly slice is one draw
   from a high-variance distribution. If the only evidence is monthly, it is
   not evidence.
2. **Was it judged on the LEDGER, not the month's net?** See the `evaluate`
   skill.
3. **Does the per-leg edge cover the MEASURED round trip?** A gross edge
   below the fee bill is not an edge.
4. **Is the improvement larger than the noise?** The buckets report a
   standard error on the mean. A t-stat near zero is not a finding.
5. **Does it survive the held-out check?** A filter fitted and scored on the
   same slices proves nothing. The per-side sequence filter shipped as a
   **fixed, pre-registered pair of buckets** — not a general learner —
   precisely because walk-forward re-fitting was 2/7 out-of-sample.

## Then: change ONE thing

**Never bundle promotions.** The 2026-10-25 promotions changed three knobs at
once and the only reason the attribution is knowable is that a separate
isolation A/B harness (`config_iso_2026_10_25.py`) re-measured each one alone.

When you must change several, measure each in isolation too. The isolation
A/B is the evidence that the combination is not just the sum of the parts.

## PAIRED knobs — the exception

Some knobs are not independent, and they are the exception to "one thing":

| Pair | Why |
|---|---|
| `sl_max_usd` + `vol_state_min_range_norm` | The SL ceiling was measured with the vol gate OFF (optimum $5.00) and with it ON (optimum $2.50-$3.00). They must always be re-measured together. |

The pairing does not excuse a blind bundle — it means the pair is the unit of
measurement.

## The steps

### 1. Record the "before" in the registry

```bash
python -m runtime.results.cli list --kind leg_ledger
```

The canonical ledger must exist and be current. If it is not, build it:
`python -m runtime.diagnostics.leg_ledger`.

### 2. Change the value in `bt/optimal_config.py`

**One place only.** `bt/optimal_config.py` is the single source of truth for
every canonical value. There is no recipe dataclass, no second copy.

If the knob is not already on the right allowlist, you must add it there — a
knob that is not on an allowlist does not exist, and cannot be swept.

### 3. Re-measure on the ledger

```bash
python -m runtime.diagnostics.leg_ledger --refresh
python -m runtime.diagnostics.leg_buckets
```

Check the trade count too, if the change could affect it:
`python -m runtime.diagnostics.volgate_trade_count`

### 4. Diff the two runs

```bash
python -m runtime.results.cli history --kind leg_ledger --params '{...}'
```

Same `config_fp`, different `commit` — that is the "what did the promotion do"
view.

### 5. Update the guards

The repo's guard tests lock the important invariants. If your promotion
changes a canonical value, check whether a guard asserts it:

```
tests/test_config_single_source.py   # no second source of truth, no dead knob
tests/test_parity.py                 # knob allowlist split
tests/test_smoke.py                  # every builder rejects illegal overrides
tests/test_regressions.py            # all engine kwargs reach the engine
```

### 6. Document it

- **`AGENTS.md` § 3.5** — the canonical config table. If the value changed,
  this is the row that changed.
- **`AGENTS.md` § 6** — if a feature was rejected, it goes in the DEAD table
  with the reason, so it is not re-litigated.
- **`docs/promotions.md`** — the history: what shipped, the numbers behind it.
- **`results/index.json`** — committed automatically by `record_run`.

## Rejecting a feature

A rejection is a **result** and gets recorded like one. The DEAD table in
`AGENTS.md` § 6 must say:

1. What it was.
2. The measurement that killed it, with the number.
3. **Why it failed** — "it did not help" is not a reason. "It kept zero
   trades, and the kept trades were significantly loss-making" is.

Dead means DEAD. Record it so nobody spends the money again.

## The traps

**The chunked-measurement promotion trap.** The `contra_close` $1.0 threshold
was chosen by a pair-level analysis and shipped OFF, then re-picked. The
`contra_close` delta **flips sign** between the chunked view (+$113.79) and
the continuous view (−$137.15) over the same months. **The chunked view is
what produced the promotion.** If the promotion was driven by a chunked
measurement, re-measure it continuously before shipping.

**The one-month sweep trap.** The one-month sweep is a liveness probe, not a
verdict. The caps are history-dependent, so a cold chunk's first ~8 days
trade UNCAPPED and the sign of the gross can flip. Use the sweep to ask "is
this knob live?", then judge on the ledger.

**The vol gate's sample-size trap.** A gate evaluated on 3-6 slices will find
a threshold that "works". The 3.0 threshold was `+$69 net` on 6 slices and
`−$364` on 15 months. Both numbers were correct for their samples.

**The fee-limited trap.** "Not profitable" != "no skill". A side can have a
real edge and still lose to the fee bill. Report which side, and why.

## Reference

- [`AGENTS.md`](../../../AGENTS.md) § 1.1, § 3.5, § 6.
- [`docs/promotions.md`](../../../docs/promotions.md) — the promotion history and
  the isolation A/B tables.
- The `evaluate` skill — the judging step.
