# NB95 — The canonical recipe is a BETA PROXY, not an edge

**Date:** 2026-10-22
**Status:** Recipe **NOT promoted**. The engine fixes are good; the
strategy is not.

---

## TL;DR

The 2026-10-22 audit fixed three real defects in the backtest (a 2×
fee overcharge, fictional limit-order fills, and a directional
lock-in that made the strategy 100% one-directional). Those fixes made
the backtest **honest**.

The honest backtest then said something uncomfortable:

> **The recipe has no direction-independent edge. Its P&L is the
> market's drift, minus a $0.47/trade fee.**

Every "winner" in the re-sweep matrix was a slice where the market
happened to move the way the strategy's directional lock pointed.

> **Addendum (later the same day).** After `conflict_policy` was
> promoted from `drop` to `stack`, the decisive diagnostic was
> **re-run under the canonical config**. The verdict got stronger,
> not weaker: **7 of 8 slices lose**, total net **−$30,660**, and
> **1/3 flat slices profitable**. The per-side correlations are
> near-perfect in both directions (long **+0.966**, short **−0.961**).
> See § "Canonical-`stack` re-measurement".

---

## The three engine defects (all fixed, all worth keeping)

### D1 — 2× fee overcharge

The engine debits the full entry+exit cost at close:

```python
fee = |exit + entry| * 0.5 * qty * (bps / 10000.0) * 2.0
```

The trailing `* 2.0` models entry + exit. `_derive_taker_fee_bps`
handed the engine a **round-trip dollar amount** instead of a
per-side rate, so every trade was charged **$0.94 against a $0.47
spec**.

```python
# FIXED:
rt = lots * commission_per_lot_usd + oz * spread_per_oz_usd
return rt * 10000.0 / (2.0 * oz * spot)   # the 2.0 undoes the engine's 2.0
```

### D2 — fictional limit-order fills

The pre-D2 engine opened a trade **instantly on the trigger bar** at
the zone-edge price, even though price had never traded there.

Measured on the 1-month 2025-01 slice:

* **95.9%** of trades filled on a *later* bar (median delay 42 bars)
* **4.1%** never filled at all

So 100% of trades were credited a fill at a price that did not exist
on the bar they were booked on.

**Fix:** a signal now **places** a `PendingOrder`; the order **fills**
on a strictly later bar whose range touches the limit level, and is
**cancelled** if the zone dies first.

| Mode | Behaviour |
| --- | --- |
| `touch` | Canonical. Fill on the first LATER bar whose range touches the limit. |
| `next_open` | Fill at the next bar's open. |
| `trigger` | Pre-D2 behaviour. A/B only; not defensible. |

### D3 — directional lock-in

With `conflict_policy='drop'` and an unbounded book, the book **never
emptied** (0 flat bars out of 1,377,169; max 358 open, mean 143.6).
`drop` rejects any signal opposite to anything open — so the *first*
opposite signal was rejected **forever**.

The 1-month canonical run locked **100% short** (5,985 S / 0 L) from
a detector that emitted 5,109 bear and 4,575 bull zones. On a rising
month the same code is 100% long. That is a book artefact, not a
bearish edge.

The same-dir cap also lived only in the `replace` / `stack` branches
of `_conflict_policy_pass`, so under the canonical `drop` policy it
was **structurally unreachable**.

**Fix:** the cap check moved into the `drop` branch, and the
canonical value is now `max_same_dir_open = 20`.

---

## The re-sweep matrix

`sl_zone_mult` × `max_same_dir_open`, 1-month 2025-01 and 3-month
2025-Q1, canonical `fill_model='pessimistic'`:

### 1-month 2025-01 (market **+6.58%**)

| `sl_zone_mult` | cap | trades | L / S | net | EV | hit | maxDD | PF |
|---|---|---|---|---|---|---|---|---|
| 8 | 10 | 1,503 | 470 / 1,033 | −$2,068 | −$1.376 | 0.175 | $2,382 | 0.48 |
| 8 | 20 | 2,655 | 456 / 2,199 | −$4,535 | −$1.708 | 0.153 | $4,934 | 0.39 |
| 20 | 10 | 623 | 623 / 0 | +$975 | +$1.565 | 0.496 | $540 | 1.52 |
| 20 | 20 | 1,167 | 1,167 / 0 | +$1,869 | +$1.602 | 0.500 | $1,034 | 1.51 |
| **40** | **10** | 359 | 359 / 0 | +$1,260 | +$3.509 | 0.657 | $570 | 2.14 |
| **40** | **20** | 735 | 735 / 0 | **+$2,519** | +$3.428 | 0.659 | $1,083 | 2.10 |

### 3-month 2025-Q1 (market **+18.86%**)

| `sl_zone_mult` | cap | trades | L / S | net | EV | hit | maxDD | PF |
|---|---|---|---|---|---|---|---|---|
| 8 | 10 | 6,997 | 3,260 / 3,737 | −$6,333 | −$0.905 | 0.196 | $6,508 | 0.65 |
| 8 | 20 | 11,476 | 5,478 / 5,998 | −$8,784 | −$0.765 | 0.212 | $9,222 | 0.70 |
| 20 | 10 | 3,219 | 2,654 / 565 | −$258 | −$0.080 | 0.418 | $2,434 | 0.98 |
| 20 | 20 | 5,936 | 3,440 / 2,496 | +$490 | +$0.083 | 0.418 | $3,751 | 1.02 |
| **40** | **10** | 1,891 | 1,891 / 0 | +$3,287 | +$1.738 | 0.616 | $1,292 | 1.41 |
| **40** | **20** | 3,611 | 3,611 / 0 | **+$6,736** | +$1.865 | 0.624 | $2,543 | 1.43 |

**The winner on both slices is `sl_zone_mult=40` — and on both slices
the winning cells are 100% LONG, on a market that rose 6.6% and
18.9% respectively.**

That is the smell.

---

## The decisive test

### Test 1 — the `conflict_policy` A/B

Same slice (2025-Q1), same `sl_zone_mult=40`, only the policy
changes:

| policy | trades | L / S | net | exits |
|---|---|---|---|---|
| `drop` | 3,611 | 3,611 / 0 | **+$6,736** | 1,287 SL, 767 TP1, 459 TP, 1,078 TP2 |
| `stack` | 6,910 | 3,611 / 3,299 | **−$3,950** | 3,106 SL, 1,243 TP1, 655 TP, 1,868 TP2 |
| `replace` | 29,378 | 14,103 / 15,275 | **−$15,261** | 28,087 `replaced_by_new_signal` |

`drop` beats `stack` by **$10,686** on the identical strategy. The
only difference is that `drop` refuses to hold the losing side.

**A real edge does not evaporate when you let both sides trade.**

### Test 2 — 8-slice directional-beta regression

`runtime/diagnostics/directional_beta.py`, 8 monthly slices spanning
up / down / flat regimes:

| Slice | Market | Gross P&L | Long n / gross | Short n / gross |
|---|---|---|---|---|
| 2024-06 | −0.11% | −$345 | 0 / $0 | 650 / −$345 |
| 2024-11 | −3.52% | +$645 | 2 / +$0 | 1,184 / +$645 |
| 2025-01 | +6.58% | +$2,793 | 735 / +$2,793 | 0 / $0 |
| 2025-05 | +0.50% | −$3,071 | 0 / $0 | 2,290 / −$3,071 |
| 2025-09 | +12.06% | +$5,004 | 2,131 / +$5,004 | 0 / $0 |
| 2026-03 | −12.59% | +$15,704 | 2,417 / +$1,757 | 3,486 / +$13,947 |
| 2026-04 | −1.48% | −$322 | 0 / $0 | 3,839 / −$322 |
| 2026-01 | +13.21% | −$8,638 | 220 / +$2,328 | 5,256 / −$10,966 |
| **TOTAL** | | **+$11,771** | | |

**The flat-market slices: 0 of 3 profitable. Total −$3,738.**

A strategy with timing skill makes money in a choppy market — that
is what *timing* means. This one loses money in every flat slice.

### Test 3 — per-side correlation with the market

| side | slices | β ($ per 1% market move) | α ($ per slice) | corr |
|---|---|---|---|---|
| long | 4 | +$70.25 | +$2,632.48 | **+0.591** |
| short | 6 | −$937.95 | −$643.12 | **−0.967** |

The short side's P&L is almost perfectly *anti*-correlated with the
market's return. That is the signature of a direction bet: the
strategy is short because the market is going up, and it loses
exactly when the market turns.

**5 of 8 slices had the minority side at exactly zero trades** (and
in 7 of 8 the minority side was under 5% of all trades). The `drop`
policy locks the book, and the lock points whichever way the market
first moved. The one genuinely two-sided slice (2026-03) is the one
where the market fell hardest — and it is also the single most
profitable slice, which is the tell.

### Test 2b — the same test at `sl_zone_mult=6` (independent confirmation)

A tighter stop is a *different* strategy, and it is **not** locked
(both sides trade in every slice). If the diagnosis is right, it
should show the same beta signature — and it does, more sharply:

| slice | market | long gross | short gross |
|---|---|---|---|
| 2024-06 | −0.11% | −$394 (1,014) | −$539 (937) |
| 2024-11 | −3.52% | −$605 (493) | +$192 (3,208) |
| 2025-01 | +6.58% | +$2,059 (1,852) | −$60 (24) |
| 2025-05 | +0.50% | −$677 (4,278) | −$381 (3,361) |
| 2025-09 | +12.06% | +$2,458 (3,914) | −$1,517 (2,130) |
| 2026-03 | −12.59% | −$8,494 (8,776) | +$4,001 (14,917) |
| 2026-04 | −1.48% | −$9,291 (10,095) | −$1,844 (5,268) |
| 2026-01 | +13.21% | +$9,252 (11,658) | +$3,510 (4,840) |

| | `sl=40` | `sl=6` |
|---|---|---|
| β ($ gross per 1% market) | −$506.03 | **+$532.35** |
| α ($ gross per slice, flat market) | +$2,397.65 | **−$1,265.75** |
| r² | 0.376 | 0.461 |
| flat-slice gross total | −$3,738 | **−$13,126** |
| total gross (8 slices) | +$11,771 | **−$2,330** |

Per-side at `sl=6`:

| side | β ($/1%) | α ($/slice) | corr |
|---|---|---|---|
| long | +$585.20 | −$1,782.59 | **+0.834** |
| short | −$52.85 | +$516.84 | −0.207 |

Two independent configurations, two different lock states, the same
conclusion: **the long side's P&L tracks the market (corr +0.83) and
the strategy's alpha — the money it makes when the market does
nothing — is negative.**

A strategy with genuine timing skill has a positive alpha. Both
configurations have a market-explained or negative alpha.

---

## Canonical-`stack` re-measurement (2026-10-22, decisive)

Every table above was measured under `conflict_policy='drop'`. That
policy was subsequently **promoted to `stack`** (both sides trade),
so the whole diagnostic was re-run against the canonical config:

```bash
python -m runtime.diagnostics.directional_beta --conflict-policy stack \
  --out runtime/diagnostics/_reports/directional_beta_stack.json
```

| Slice | Market | Gross | net | Long gross | Short gross |
|---|---|---|---|---|---|
| 2024-06 flat | −0.11% | −$357 | −$1,064 | +$116 (1,015) | −$472 (1,525) |
| 2024-11 down | −3.52% | −$1,732 | −$3,188 | −$2,425 (2,715) | +$693 (2,378) |
| 2025-01 up | +6.58% | −$905 | −$1,870 | +$2,272 (1,552) | −$3,177 (1,866) |
| 2025-05 flat | +0.50% | −$3,239 | −$5,698 | −$537 (4,361) | −$2,702 (4,072) |
| 2025-09 up | +12.06% | −$1,988 | −$4,225 | +$5,283 (3,709) | −$7,271 (4,207) |
| 2026-03 down | −12.59% | −$2,671 | −$6,465 | −$12,994 (7,447) | +$10,323 (5,296) |
| 2026-04 down | −1.48% | +$1,268 | −$1,683 | −$1,782 (4,190) | +$3,050 (5,797) |
| 2026-01 up | +13.21% | −$1,313 | −$6,468 | +$10,416 (7,098) | −$11,728 (8,395) |
| **TOTAL** | | **−$10,937** | **−$30,660** | | |

| metric | `stack` (canonical) | `drop` (legacy) |
|---|---|---|
| β ($ gross per 1% market move) | **+$17.74** | −$506.03 |
| α ($ gross per flat slice) | **−$1,399.58** | +$2,397.65 |
| r² (aggregate) | **0.012** | 0.376 |
| flat-slice gross | **−$2,328** | −$3,738 |
| flat slices positive | 1/3 | 0/3 |
| slices positive gross | **1/8** | 4/8 |
| total gross | **−$10,937** | +$11,771 |
| total fees | **$19,723** | $7,436 |
| **total net** | **−$30,660** | **+$4,335** |
| **long-side corr** | **+0.966** | +0.591 |
| **short-side corr** | **−0.961** | −0.967 |
| **verdict** | no robust edge | no robust edge |

### Why the aggregate beta went to zero — and why that is *not* good news

The aggregate r² collapsed from 0.376 to **0.012** under `stack`.
That looks like the market stopped explaining the P&L. It did not:
the two sides now **cancel**. Read them separately and the
direction bet is if anything *sharper* than under `drop`:

* long gross corr with market: **+0.966** (r² ≈ 0.93)
* short gross corr with market: **−0.961** (r² ≈ 0.92)

So the recipe is, mechanically, a **levered long/short pair that is
long by construction**: the longs make money exactly when the market
rises and lose exactly when it falls, and vice-versa. Averaging the
two produces a flat line and a near-zero beta, which is why the
aggregate test alone would have been misleading here.

**Alpha — the money made when the market does nothing — is negative
in all three views:** aggregate −$1,400/slice, long side −$1,357,
short side −$42. A strategy with genuine timing skill has positive
alpha. This one is negative in every decomposition.

### The `drop` → `stack` promotion cost $35k of "profit"

The only configuration in the entire matrix that showed a positive
total net was `sl=40` + `drop` at **+$4,335**. That number was the
directional lock: `drop` refuses to open the losing side, so on each
slice it kept whichever side the market happened to favour. Flip to
`stack` on the identical config and it is **−$30,660**.

This is the expected and correct behaviour. The promotion made the
backtest honest and the headline worse. The +$4,335 was never an
edge; it was the strategy declining to take the side that lost.

---

## The 1-year run (for the record)

> **SUPERSEDED.** These are the `drop`-policy 1y numbers. The
> canonical-`stack` 1y grid is in § "1-year grid under `stack`"
> below — every value loses, and `sl=40` is gross-**negative**.

With D1 + D2 + D3 + P2–P4, the 1y XAUUSD file (41,233,730 bars,
2024-01-01 → 2026-07-15, market **+96.48%**) under `drop`:

| | |
|---|---|
| wall clock | **586.9 s** (~10 min; was "projected hours") |
| trades | 80,833 (41,007 L / 39,826 S) |
| orders placed / filled / cancelled | 112,368 / 58,017 / 54,346 |
| order fill rate | 51.7% |
| gross P&L | −$362 |
| fees | $32,089 |
| **net P&L** | **−$32,452** |
| EV/trade | −$0.401 |
| hit rate | 0.464 |
| PF | 0.936 |
| maxDD | $54,034 |
| median hold | 4,183 s (~70 min) |

The 1y net is **negative** — and the market rose 96%. That is a
strategy that is *long* (it has 41,007 longs) but whose entries are
badly timed: it buys every FVG in a rising market and gets stopped
out on the noise.

The `signals_unaccounted` and `order_lifecycle.unaccounted`
identities are both **0** — the accounting is conserved.

### The `sl_zone_mult` question, re-measured on the full 1y file

> **STALE — SUPERSEDED.** The numbers originally in this section
> were measured under `conflict_policy='drop'` and have been
> replaced by the `stack` grid below. The old `sl=40` row read
> "gross −$362, essentially breakeven" — that was the lock, not an
> edge. Under `stack` the same config is gross-**−$43,455**.

The 1-month and 3-month slices above were all *rising* months, so
they could not separate "wider stop is better" from "the market
went up". The full-year run can, because it contains 925 days with
both directions:

```bash
python -m runtime.sweeps.cached_sweep --knob sl_zone_mult \
  --grid 6,20,40 --scope xauusd_s1_1y_sl_recheck --notebook nb96_1y_sl_recheck
```

| mult | legs | filled | L / S | gross | fees | net | hit | EV | PF | maxDD | medSL | medTP |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 6 | 645,673 | 511,440 | 322,006 / 323,667 | −$44,394 | $284,983 | **−$329,377** | 21.3% | −$0.510 | 0.74 | $329,632 | $2.34 | $7.32 |
| 20 | 395,668 | 267,094 | 198,532 / 197,136 | −$62,446 | $143,884 | **−$206,331** | 37.2% | −$0.521 | 0.84 | $206,507 | $7.80 | $4.75 |
| **40** (canonical) | 262,156 | 165,253 | 134,394 / 127,762 | −$43,455 | $88,509 | **−$131,963** | 44.9% | −$0.503 | 0.88 | $132,109 | $15.60 | $4.57 |

**Every value is gross-negative** (−$43k to −$62k before fees) and
every value loses $132k–$329k net. `sl=40` is still the least-bad
of the three, but "least-bad" is the whole claim.

Why the tighter stop loses so much is not a subtle edge effect — it
is fee drag with a book-mechanics multiplier:

* `sl=6` produces 645,673 legs vs 262,156 at `sl=40` — 2.5× the
  churn. A tight stop frees the slot immediately, so the same zone
  signal fires again and again.
* Each churn round-trip costs **$0.47**, and the median edge per
  trade is far below that at every setting. `sl=6` pays **$284,983
  in fees** on top of an already gross-negative book.
* Fees are 3.2× higher at `sl=6` than `sl=40` ($285k vs $89k), which
  accounts for most of the net gap.

**The honest reading:** at the best stop width the strategy has
**no edge at all** — and unlike the `drop` measurement, it does not
even reach gross-breakeven. The "profitability" of `sl=40` on the
short rising slices was never an edge; it was (a) the market's
drift and (b) trading less often.

**Kept: `sl_zone_mult = 40.0`** (least-bad), with the strategy
**still not promoted**. The blocker is the missing edge, not the
stop width.

> **Metric naming caveat.** `n_trades` counts **legs**, not
> round-trips: a TP1 partial close logs one leg and reopens a
> remainder leg. On the 1y `sl=40` run, 262,156 legs vs 165,253
> filled orders. Use `n_orders_filled` for the trade count.

---

## What this means going forward

### Keep

* **D1** (fee fix) — the backtest now charges the live $0.47.
* **D2** (resting entry orders) — the backtest now only counts
  trades the market could actually have executed.
* **D3** (bounded book) — the backtest now trades both sides.
* **P2–P4** (engine hot loop) — 1y cell went from "hours" to 10 min.
* The per-cell JSON policy, the artifact cache, the nanosecond
  timebase discipline, the two-sided rolling-window rule.

### Do not

* **Do not promote this recipe to live.** There is no
  direction-independent edge to promote.
* **Do not report a positive P&L from a single slice.** Every
  "winner" was a slice where the market happened to move the way the
  lock pointed.
* **Do not tune `sl_zone_mult` on a rising slice.** `sl=40` beat
  `sl=20` on both rising slices and lost on both flat ones — it is a
  *lower-frequency, higher-conviction* filter, not a better one.

### Next

The question worth asking is no longer "which knob wins" but:

1. **Is there ANY direction-independent edge?** The flat-market
   slices are the honest test: can any configuration make money in
   2025-05 (market +0.50%) and 2024-06 (market −0.11%)?
2. **If the edge is only "hold a direction", is that worth trading?**
   XAUUSD had a +96% move over the 1y window. A strategy that simply
   held long would have made far more than $11,771 gross with no
   risk. The FVG logic is *removing* edge, not adding it.
3. **What would a regime filter look like?** The obvious candidate
   is the NB85 vol-state gate, which is currently INACTIVE
   (`vol_state_min_vel_5m = 0.0`) and has never been re-validated
   after the forward-look fix. A gate that only takes trades in
   *trending* regimes would convert a beta proxy into a
   trend-follower — which is at least a defensible strategy, and
   testable against the flat slices.

---

## Reproducing

```bash
# the full diagnosis
python -m runtime.diagnostics.directional_beta \
  --out runtime/diagnostics/_reports/directional_beta_sl40.json

# the re-sweep matrix (1-month)
python -m runtime.sweeps.cached_sweep \
  --knobs "sl_zone_mult=8,20,40;max_same_dir_open=10,20;fill_model=pessimistic" \
  --data C:/coding/ict_tier_v2/data/XAUUSD_S1_1y.parquet \
  --start 2025-01-01 --end 2025-02-01 \
  --scope xauusd_s1_1m --notebook nb95_matrix_1m

# the 1y canonical run
python -m runtime.sweeps.cached_sweep --knob _canonical \
  --data C:/coding/ict_tier_v2/data/XAUUSD_S1_1y.parquet \
  --scope xauusd_s1_1y_final --notebook nb95_1y_final
```

Per-cell JSON lands in `runtime/sweeps/_results/<knob>/<scope>__cell_<idx>.json`.

---

## Side note: metrics that were lying

Two reporting bugs found while assembling this, both now fixed:

1. **`median_sl_distance_usd` was 0.0.** The engine *mutates*
   `stop_price` as a trade progresses (breakeven after TP1, exit
   price on a stop-out), so `abs(entry - stop_price)` is 0.0 for
   every TP1/TP2 exit. The engine now carries
   `initial_sl_distance_usd` captured at fill time.

2. **`signals_unaccounted` was −51.** A fill-time policy drop
   incremented `n_dropped_policy` *and* `n_orders_cancelled`, so the
   signal identity subtracted it twice. The counters are now
   disjoint: `n_dropped_policy` (signal time) vs
   `n_dropped_policy_at_fill` (fill time).

Both are regression-tested in `tests/test_no_dead_imports.py`.
