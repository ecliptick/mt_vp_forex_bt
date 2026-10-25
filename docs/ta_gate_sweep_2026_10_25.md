# TA gate sweep — momentum/trend + mean-reversion gates (2026-10-25)

**Question asked:** *"try some momentum and trend following TAs. do a
quick sweep on the MR TAs, where trades must align with the TA's
signal (gate open) and upon signal change close open trades. tell me
the trade number decay based on this"*

**Answer in one line:** the trade-count decay is **48–100% for the
momentum/trend families** and **67–100% for the mean-reversion
families**; the MR gates are far more destructive, and **no gate in
either family produced a book that pays for itself** (the widest
gate that covers the round trip keeps only 653 legs — a 98.7% decay).

---

## 1. What was built

| file | what it is |
|---|---|
| `runtime/diagnostics/ta_gate.py` | the gate engine: gate at the signal bar, gate at the fill, early close on signal change, and the drift/excess decomposition |
| `runtime/diagnostics/ta_gate_sweep.py` | the sweep driver — 74 cells across 14 TA families, with two placebos per cell |
| `runtime/diagnostics/ta_gate_validate.py` | the **proof** that the gate reproduces the canonical ledger bit-exactly |
| `runtime/diagnostics/ta_gate_summary.py` | the read-out (decay by family, MR vs trend, placebo, excess) |
| `runtime/diagnostics/ta_gate_probe_index.py` | the probe that established the ledger's bar-index semantics |
| `tests/test_ta_gate.py` | 8 guard tests |

The gate is applied to the **canonical wide book** (51,075 legs, vol
gate 1.0, `sl_max_usd=8.00`, `contra_close` ON at $5.00) on the
continuous corpus (2024-01-01 → 2026-07-16).

### The gate semantics

1. **Gate at the SIGNAL bar.** A leg survives only if the TA signal at
   the leg's SIGNAL bar equals the leg's side. The signal bar is the
   causal bar — the engine's own per-side gates are evaluated there
   too, and the repo's rule is *"a gate that the engine evaluates at
   bar S must be measured at bar S"*.
2. **Gate at the FILL.** The engine's D2 resting-limit order re-checks
   the book at fill time (`policy_at_fill`); the gate does the same.
   A signal that flipped between signal and fill means the trade
   never existed.
3. **Early close.** A surviving trade is closed at the first 1m
   boundary where the signal stops agreeing with its side. The fill is
   the **next 1m bar's OPEN** — the first tradeable price after the
   change is known.

### No lookahead

The TA state is computed on 1m bars, but a 1m bar is only known at its
CLOSE. So the signal in force at time `T` is the signal of the last
1m bar whose close is `<= T`:

```
idx(T) = max { k : bucket_start[k] + 60s <= T }
A(T)   = signal[idx(T)]
```

Using the bar that *contains* `T` would leak up to 60 s of future
price — the same class of defect as the NB94 forward-leak bugs and
the "signal bar vs fill bar" trap. Guard:
`tests/test_ta_gate.py::test_the_signal_in_force_is_the_last_bar_that_HAS_CLOSED`.

---

## 2. THE HARNESS IS PROVEN, NOT ASSUMED

`ta_gate_validate.py` runs the gate with a constant `+1` signal (which
passes every LONG leg and rejects every SHORT one) and checks that
the gate reproduces the canonical ledger **bit-exactly**:

| check | result |
|---|---|
| the passed set == the long legs | **PASS** (35,855 / 35,855) |
| gross identity | **PASS** — max abs error `0.000e+00` |
| fee identity | **PASS** — max abs error `0.000e+00` |
| drift identity | **PASS** — max abs error `0.000e+00` |
| excess identity | **PASS** — max abs error `0.000e+00` |

### The grid trap that had to be fixed

The **first** implementation computed the drift on the 1m grid. It
failed the validation: the 1m grid said **−$9,715** of drift on the
long legs while the ledger's 1s grid said **+$11,098** — the two
**disagree in sign** on the whole book, even though they correlate at
0.92.

The cause is structural: the 1m close is the close of the **last
second** of a minute, so shifting both window endpoints by up to a
minute moves a short-horizon drift by as much as the drift itself.

The probe (`ta_gate_probe_index.py`) established that the ledger's
`entry_bar` / `exit_bar` are **file row indices**, and that the
ledger's drift reproduces bit-exactly from the parquet's `close`
column. So the fix was to carry a **1m → 1s row map** through the bar
builder and to compute the drift on the ledger's own grid.

> **Rule: a benchmark must run on the grid the benchmark was defined
> on.** A drift computed on a coarser grid is a *different number*,
> not an approximation — and here it had the wrong sign.

---

## 3. THE TRADE-NUMBER DECAY

Canonical book: **51,075 legs**. Decay = `1 - kept / 51075`.

### By family (the widest cell in each family)

| family | type | kept% (lo–hi) | decay (lo–hi) | widest cell |
|---|---|---|---|---|
| `ts_ma_cross` | trend | 47.1–52.0% | **48.0–52.9%** | `n=200` |
| `supertrend` | trend | 46.3–47.3% | **52.7–53.7%** | `mult=1.5` |
| `roc_zero` | trend | 41.9–47.3% | **52.7–58.1%** | `n=60` |
| `kama` | trend | 46.4–49.3% | **50.7–53.6%** | `fast=3,n=20` |
| `adx_trend` | trend | 7.5–41.1% | 58.9–92.5% | `n=14,thr=15` |
| `momentum_rsi` | trend | 0.9–24.0% | 76.0–99.1% | `hi=60,lo=40,n=14` |
| `cci` | trend | 0.9–12.6% | 87.4–99.1% | `n=20,thr=100` |
| `squeeze` | trend | 2.9–8.5% | 91.5–97.1% | `k_bb=1.5,n=20` |
| `willr` | trend | 0.0–3.2% | 96.8–100.0% | `n=21` |
| **`vwap_revert`** | **MR** | **26.6–33.4%** | **66.6–73.4%** | `k=1.0` |
| **`range_fade`** | **MR** | **12.9–22.3%** | **77.7–87.1%** | `k=0.1,n=120` |
| **`zscore_revert`** | **MR** | **0.0–10.1%** | **89.9–100.0%** | `n=60,thr=1.0` |
| **`rsi_revert`** | **MR** | **0.0–0.5%** | **99.5–100.0%** | `hi=65,lo=35,n=14` |
| **`bb_fade`** | **MR** | **0.0–1.2%** | **98.8–100.0%** | `k=1.5,n=40` |

### MR vs TREND — the comparison asked for

| | cells | kept% (median) | kept% (range) | decay (median) | decay (range) |
|---|---|---|---|---|---|
| **momentum / trend** | 47 | **18.6%** | 0.0–52.0% | **81.4%** | 48.0–100.0% |
| **mean reversion** | 27 | **0.5%** | 0.0–33.4% | **99.5%** | 66.6–100.0% |

**The MR gates are far more destructive.** The median MR gate kills
**99.5%** of the book against the trend family's 81.4%.

> **Why, structurally.** The MR oscillators **abstain** in the middle
> of their range (`rsi_revert` only signals outside 30/70, `bb_fade`
> only outside the band). Abstention is a *flat* signal, and the gate
> requires the signal to equal the trade's side — so every abstention
> is a rejection. The trend families are **directional** on almost
> every bar (a 200-bar MA cross is long or short ~99% of the time),
> so they reject only on the flips. `vwap_revert` is the MR exception
> because it is signed rather than abstaining, and it is also the
> MR family with the smallest decay (66.6%).

---

## 4. Does the surviving book pay for itself?

**9 of 74 cells** have a surviving book whose gross/leg covers the
measured round trip ($0.3869/leg) — and every one of them is a
**decay ≥ 95.7%**. The two net columns matter: `netOnly` is the gate
alone, `netCls` is the gate **plus the early close** (the rule as
specified).

| cell | kept | decay | gross/leg | x | netOnly | **netCls** | excess/leg | t | pSub |
|---|---|---|---|---|---|---|---|---|---|
| `cci(n=20,thr=150)` | 2,178 | 95.7% | $0.4077 | 1.06× | +$51 | **−$1,028** | −0.0530 | −3.30 | 0.951 |
| `cci(n=20,thr=200)` | 653 | 98.7% | $0.4302 | 1.12× | +$30 | **−$297** | −0.0664 | −1.58 | 0.780 |
| `rsi_revert(65/35,n=14)` | 245 | 99.5% | $0.4891 | 1.22× | +$22 | **−$121** | −0.0147 | −0.27 | 0.780 |
| `willr(-80/-20,n=14)` | 102 | 99.8% | $0.5921 | 1.62× | +$23 | **−$35** | −0.0264 | −0.44 | 0.537 |
| `rsi_revert(30/70,n=14)` | 61 | 99.9% | $0.8215 | 1.94× | +$24 | **−$68** | +0.0848 | 0.67 | 0.829 |
| `bb_fade(k=2.0,n=40)` | 58 | 99.9% | $0.5689 | 1.44× | +$10 | **−$44** | +0.0983 | 0.56 | 0.585 |
| `rsi_revert(30/70,n=7)` | 45 | 99.9% | $1.4701 | 3.67× | +$48 | **−$10** | +0.0882 | 0.68 | 0.634 |
| `zscore_revert(n=20,thr=1.5)` | 42 | 99.9% | $0.7365 | 1.63× | +$12 | **−$54** | +0.2361 | 0.99 | 0.780 |
| `bb_fade(k=1.5,n=20)` | 42 | 99.9% | $0.7365 | 1.63× | +$12 | **−$54** | +0.2361 | 0.99 | 0.805 |

> ### ⚡ The rule AS SPECIFIED never produces a positive book.
>
> **Every one of the 9 cells that survives the round trip WITHOUT the
> early close turns NET-NEGATIVE WITH it.** The best
> gate+close net across all 74 cells is **−$9.77** (`rsi_revert`
> 30/70, n=7 — 45 legs out of 51,075, a 99.9% decay).
>
> This is the single sharpest result in the sweep: the early close is
> not a neutral addition, it is **the thing that destroys the
> survivors' profitability**.

**The widest gate that covers the round trip keeps 653 legs out of
51,075 — a 98.7% decay.** Against the canonical book's −$12,445.50
this is noise, and it is bought by throwing away 96% of the trades.

> **A 98.7% decay is not a strategy.** A book that trades 653 times
> in 2.7 years (≈ 0.7/day) is not the 30-70/day book the canonical
> recipe targets, and it is not what produced the +$51.

---

## 5. THE EARLY CLOSE — it HURTS

The rule asked for "upon signal change close open trades". Measured:

| | value |
|---|---|
| median gross/leg, gate ONLY | **$0.1002** |
| median gross/leg, gate + early close | **−$0.0572** |
| cells where the early close HELPED (>5% better) | **14** of 74 |
| cells where the early close HURT (<5% worse) | **59** of 74 |
| cells where it made no difference | 1 |

**The early close turns the surviving book's gross negative.** It is
a **fee multiplier**: closing early does not save the round trip, it
pays a second one on a position that was going to reach its target.
The median early close fires on **55–85% of the survivors**, so the
rule is mostly cutting winners short.

Why it hurts *structurally*: the canonical book's gross is already
**drift** (§ 6). The early close **truncates the drift** — it closes
the position at the moment the TA disagrees, which is precisely the
moment the drift stops paying. So the early close removes the only
component of the gross that was working.

> **The early close is the same mechanism as the swing/p95 TP caps,
> which the repo already documents as truncating a drift harvest.**

---

## 6. THE EXCESS — the gates select DRIFT, not skill

`excess = gross − drift`, on the ledger's own 1s grid (bit-exact,
validated). A negative excess means the gated book captured **less**
than a passive hold of the same positions.

**Of the 55 cells with ≥ 500 surviving legs, 50 have a significantly
negative excess.**

| cell | kept | gross/leg | drift/leg | excess/leg | t |
|---|---|---|---|---|---|
| `ts_ma_cross(n=200)` | 26,542 | $0.1375 | $0.1959 | **−0.0584** | −13.82 |
| `ts_ma_cross(n=100)` | 26,033 | $0.1002 | $0.1565 | **−0.0562** | −13.25 |
| `kama(fast=3,n=20,slow=20)` | 25,180 | $0.0999 | $0.1579 | **−0.0580** | −13.02 |
| `supertrend(mult=1.5)` | 24,157 | $0.1274 | $0.1844 | **−0.0570** | −12.10 |
| `adx_trend(n=28,thr=15)` | 13,645 | $0.1337 | $0.1934 | **−0.0597** | −12.22 |
| `vwap_revert(k=1.0)` | 17,071 | −0.0108 | $0.0679 | **−0.0787** | −7.58 |

The canonical book's own excess is **−0.0799/leg (t = −15.63)** — so
the gates do not *create* the drift dependence, but they also do not
remove it. They are **beta filters**: they select the trades that
happen to point with the market.

---

## 7. THE PLACEBO — the gates pick the WRONG trades

Two controls per cell, 40 arms each:

**Placebo A — a random subset of the same size.** The sharpest
control: same trade count, different selection.

| | value |
|---|---|
| cells with p < 0.05 (better than a random subset) | **0 of 74** |
| cells with p > 0.95 (worse than a random subset) | **43 of 74** |
| median p | 0.976 |

**No gate beat a random subset of the same size. Not one.** And 43
of 74 were *significantly worse* than picking trades at random.

**Placebo B — the rule moved to a random TIME.** This destroys only
the timing, keeping the rule's structure (its state durations, its
abstentions, its flips).

| | value |
|---|---|
| cells with p < 0.05 (better than the rule at a random time) | 9 of 74 |
| median p | 0.683 |

Nine cells did beat the time-shifted control. Those are the only
cells with any claim to *timing* information — and they are all in
the tight-gate regime with tiny books, and their excess is negative
anyway.

---

## 8. Verdict

> **VERDICT: NO TA GATE IS PROMOTABLE.**
>
> 1. **The trade-number decay is the headline.** Momentum/trend gates
>    decay **48–100%** (median 81.4%); mean-reversion gates decay
>    **67–100%** (median 99.5%). The MR gates are the more
>    destructive because they abstain, and abstention is a rejection.
> 2. **Nothing survives as a book.** The widest gate that covers the
>    round trip keeps **653 of 51,075 legs (98.7% decay)** and earns
>    **+$51** over 2.7 years — **without** the early close.
> 3. **⚡ WITH the early close — the rule AS SPECIFIED — every one of
>    those 9 cells turns NET-NEGATIVE.** The best gate+close net
>    across all 74 cells is **−$9.77**. The early close turns the
>    median surviving gross from **+$0.1002/leg to −$0.0572/leg**
>    and hurts in **59 of 74** cells. It truncates the drift, and the
>    drift is the only thing paying.
> 4. **The gates are beta filters, not alpha.** 50 of the 55
>    book-sized cells have a significantly negative excess — the
>    survivors captured *less* than a passive hold.
> 5. **The placebo is decisive.** **Zero of 74** cells beat a random
>    subset of the same size; 43 were significantly worse.
>
> **The one thing the sweep does establish** is that the canonical
> recipe's trades are *already* where the trend is — the gates
> confirm it and cannot improve on it.

---

## Reproduce

```bash
# the harness proof (bit-exact vs the canonical ledger)
python -m runtime.diagnostics.ta_gate_validate

# the sweep (~165 s; 74 cells, 40 placebo arms each)
python -m runtime.diagnostics.ta_gate_sweep

# the read-out
python -m runtime.diagnostics.ta_gate_summary

# the guards
python -m pytest tests/test_ta_gate.py -q
```

Artifacts: `runtime/diagnostics/_reports/ta_gate_sweep.json`,
`runtime/diagnostics/_reports/ta_gate_bars_1m.npz`.

## Caveats

* **The gate is POST-HOC.** It re-prices the canonical ledger's legs;
  it does not re-run the engine with the gate inside it. The trade
  COUNT is exact (the measured max open depth on the wide book is 2,
  so the book never binds and removing trades cannot unblock an
  admitted one); the P&L of the surviving legs is a close estimate,
  because a re-run would also change the conflict policy, the
  contra-close netting and the fills of the survivors.
* **The MR family is under-sampled.** `rsi_revert` and `bb_fade` keep
  under 1.2% of the book, so their cells are too small to score —
  their p-values are reported but the books are not tradeable.
* **The placebo arms are 40 per cell.** That is enough to separate
  the 43 cells that are worse than random (p > 0.95) but thin for the
  9 cells at p < 0.05 against the time-shift control. Those 9 would
  need more arms before they could be believed.
