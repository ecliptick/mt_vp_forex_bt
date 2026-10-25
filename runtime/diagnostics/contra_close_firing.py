"""CONTRA CLOSE — is the feature even FIRING when it should?

WHY THIS EXISTS.

The 15-month broad sweep (`contra_close_broad.py`) reported only **11
contra events** at the canonical $6.50 threshold, over 899 legs. The
feature is the repo's answer to a specific user complaint:

    "if there are 4 trades open i.e. 0.02 lots long and 0.002 lots
     short, both buy low sell high, in theory it is fully hedged and
     free money. but because we didn't close them simultaneously or
     remove SL, we lose money instead."

The complaint describes an **INCOMING opposite position** closing an
existing one. The implementation is something else entirely: a
**passive per-bar scan** of the open book that fires when the
weighted-average entry spread between the two sides EXCEEDS a
threshold.

Those are different things, and the difference matters:

  * INCOMING-OPPOSITE (what the user described): every time an
    opposite-side position is opened, it should immediately close the
    overlapping quantity of the other side. This is a **per-FILL**
    event. The count should scale with the number of FILLS, not with
    the number of BARS.

  * PASSIVE-SPREAD-SCAN (what is implemented): on every bar, look at
    the book; if the two sides' weighted-average entries are more than
    $X apart, net them. This is a **per-BAR** event gated on a PRICE
    SPREAD threshold.

So "11 events in 15 months" is a question about the SECOND design.
This diagnostic answers it directly: it reconstructs the book over
time (with the feature OFF, so the book is unmutated) and measures

  1. How often do BOTH sides hold positions at the same time?
  2. What is the distribution of the weighted-average entry spread?
  3. How many bars would the passive scan have fired at $0.00 (any
     spread) vs at the canonical $6.50?
  4. How many FILLS are opposite-direction — i.e. how many
     incoming-opposite events did the book actually see?

That last number is the one the user's description implies.

Usage
-----
    python -m runtime.diagnostics.contra_close_firing
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    CONTRA_CLOSE_SPREAD_USD as CANON_SPREAD_USD,
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/contra_close_firing.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]

# The spread ladder the passive scan would fire at.
LADDER = [0.0, 0.5, 1.0, 2.5, 4.5, 5.5, 6.5, 7.5, 10.0, 12.0, 20.0]


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _book_spread(open_trades: list, qty_btc: float) -> tuple[float, float, float]:
    """(long_qty, short_qty, spread) for a book snapshot.

    ``spread`` is ``wavg(short_entry) - wavg(long_entry)`` -- the SAME
    definition the engine's ``_contra_spread_usd`` uses. Positive means
    the shorts entered above the longs (the profitable lock).
    """
    lq = 0.0
    ls = 0.0
    sq = 0.0
    ss = 0.0
    for ot in open_trades:
        q = ot.qty if ot.qty > 0.0 else qty_btc
        if ot.direction > 0:
            lq += q
            ls += q * float(ot.entry_price)
        else:
            sq += q
            ss += q * float(ot.entry_price)
    if lq <= 0.0 or sq <= 0.0:
        return lq, sq, 0.0
    return lq, sq, (ss / sq) - (ls / lq)


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    market: dict[str, float] = {}
    per_month: dict[str, dict] = {}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            print(f"  skip {ym}: no cache", flush=True)
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        c0 = float(bars["close"].iloc[0])
        c1 = float(bars["close"].iloc[-1])
        market[ym] = 100.0 * (c1 - c0) / c0

        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

        # ── Book trace, feature OFF ──────────────────────────────
        #
        # The snapshot callback fires POST-resolution with the live
        # book. We record, per bar, whether BOTH sides are open and
        # what the weighted-average entry spread is.
        trace: list[tuple[float, float]] = []  # (long_qty, short_qty, spread)

        def _cb(_i, _t, _c, _h, _l, book, _vs):
            lq, sq, sp = _book_spread(book, 1.0)
            trace.append((lq, sq, sp))

        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=False,
            contra_close=False,
            contra_close_spread_usd=0.0,
        )
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek,
            on_bar_callback=_cb,
            on_bar_every_k=1,
            # Authoritative engine-side counters: how many fills landed
            # opposite to an already-open position, and how wide the
            # hedged weighted-average entry spread ever got. These come
            # from the engine, not from a replay of the trade ledger.
            contra_hedge_stats=True,
        )

        eng_fills = int(getattr(res, "n_fills_total", 0))
        eng_opp = int(getattr(res, "n_fills_opposite_open", 0))
        eng_hedged_bars = int(getattr(res, "n_bars_both_sides_open", 0))
        eng_spread_max = float(getattr(res, "hedged_spread_max_usd", 0.0))

        spreads = np.array([t[2] for t in trace], dtype=np.float64)
        both = [t for t in trace if t[0] > 0.0 and t[1] > 0.0]
        # Spread stats over HEDGED bars ONLY. Averaging over ALL bars
        # would make the median trivially $0.00 (the spread helper
        # returns 0.0 whenever one side is empty), which would be a
        # meaningless headline.
        hedged_spreads = np.array([t[2] for t in both], dtype=np.float64)

        # How many bars would the passive scan fire at each threshold?
        fires = {}
        episodes = {}
        for thr in LADDER:
            if thr <= 0.0:
                # "$0.00" means ANY positive spread -- i.e. fire
                # whenever the book is hedged with a positive lock.
                hit = spreads > 0.0
            else:
                hit = spreads >= thr
            fires[f"{thr:g}"] = int(np.sum(hit))
            # EPISODES: contiguous runs of qualifying bars. The engine
            # nets the book on the FIRST qualifying bar, which destroys
            # the spread -- so the feature can only fire ONCE per
            # episode, not once per bar. This is why "3,000 qualifying
            # bars" and "11 events" are both true at the same time.
            n_ep = 0
            prev = False
            for h in hit:
                if h and not prev:
                    n_ep += 1
                prev = bool(h)
            episodes[f"{thr:g}"] = n_ep

        # The USER'S DESCRIPTION: incoming opposite positions.
        #
        # Reconstruct the EXACT fill/exit timeline from the closed
        # trade ledger.
        #
        # GROUPING BY ``parent_zone_id``, not by (bar, price, side):
        # a TP1 partial close produces TWO records for one original
        # position (the partial leg + the remainder), both carrying the
        # same parent_zone_id. And two DIFFERENT positions can share
        # the same (entry_bar, entry_price, direction) -- grouping on
        # that triple would merge them and make the merged group live
        # until the LATER exit, which fabricates hedging.
        #
        # ``zone_already_fired`` guarantees one position per zone, so
        # ``parent_zone_id`` is a unique position identity.
        groups: dict[int, list] = {}
        for t in res.trades:
            groups.setdefault(int(t.parent_zone_id), []).append(t)

        fill_events: list[tuple[int, int, float]] = []  # (bar, side, qty)
        exit_events: list[tuple[int, int, float]] = []  # (bar, side, qty)
        for recs in groups.values():
            rec0 = recs[0]
            q = sum(float(r.qty_btc) for r in recs)
            side = int(rec0.direction)
            fill_events.append((int(rec0.entry_bar), side, q))
            exit_events.append((max(int(r.exit_bar) for r in recs),
                                side, q))

        # Replay the book bar by bar.
        lq = 0.0
        sq = 0.0
        n_incoming_opposite = 0
        n_incoming_qty = 0.0
        n_same_side = 0
        _fills_by_bar: dict[int, list] = {}
        for bar, side, q in fill_events:
            _fills_by_bar.setdefault(bar, []).append((side, q))
        _exits_by_bar: dict[int, list] = {}
        for bar, side, q in exit_events:
            _exits_by_bar.setdefault(bar, []).append((side, q))

        all_bars = sorted(set(_fills_by_bar) | set(_exits_by_bar))
        for bar in all_bars:
            # Exits at this bar happen BEFORE the fills (a position
            # that closed on this bar is not "open" when the new
            # position arrives).
            for side, q in _exits_by_bar.get(bar, ()):
                if side > 0:
                    lq -= q
                else:
                    sq -= q
            lq = max(lq, 0.0)
            sq = max(sq, 0.0)
            for side, q in _fills_by_bar.get(bar, ()):
                if side > 0:
                    if sq > 1e-12:
                        n_incoming_opposite += 1
                        n_incoming_qty += min(q, sq)
                    else:
                        n_same_side += 1
                    lq += q
                else:
                    if lq > 1e-12:
                        n_incoming_opposite += 1
                        n_incoming_qty += min(q, lq)
                    else:
                        n_same_side += 1
                    sq += q

        per_month[ym] = {
            "market_pct": market[ym],
            "legs": len(res.trades),
            "orders_filled": int(getattr(res, "n_orders_filled", 0)),
            "n_contra_events": int(getattr(res, "n_contra_close_events", 0)),
            "bars_traced": len(trace),
            "bars_both_sides_open": len(both),
            "pct_bars_hedged": (100.0 * len(both) / len(trace))
            if trace else 0.0,
            "spread_p50": float(np.percentile(spreads, 50))
            if len(spreads) else 0.0,
            "spread_p90": float(np.percentile(spreads, 90))
            if len(spreads) else 0.0,
            "spread_p99": float(np.percentile(spreads, 99))
            if len(spreads) else 0.0,
            "spread_max": float(spreads.max()) if len(spreads) else 0.0,
            "spread_pct_positive": float(np.mean(spreads > 0.0) * 100.0)
            if len(spreads) else 0.0,
            # Hedged-bars-only spread distribution. This is the
            # meaningful one: the all-bars median is $0.00 by
            # construction (one side empty -> spread 0.0).
            "hedged_spread_p50": float(np.percentile(hedged_spreads, 50))
            if len(hedged_spreads) else 0.0,
            "hedged_spread_p90": float(np.percentile(hedged_spreads, 90))
            if len(hedged_spreads) else 0.0,
            "hedged_spread_p99": float(np.percentile(hedged_spreads, 99))
            if len(hedged_spreads) else 0.0,
            "hedged_spread_max": float(hedged_spreads.max())
            if len(hedged_spreads) else 0.0,
            "hedged_spread_pct_positive": float(
                np.mean(hedged_spreads > 0.0) * 100.0)
            if len(hedged_spreads) else 0.0,
            "passive_scan_fires_by_threshold": fires,
            "passive_scan_episodes_by_threshold": episodes,
            "n_incoming_opposite_fills": n_incoming_opposite,
            "n_incoming_opposite_qty": n_incoming_qty,
            "n_same_side_fills": n_same_side,
            "pct_fills_incoming_opposite": (
                100.0 * n_incoming_opposite
                / max(1, n_incoming_opposite + n_same_side)),
            # ── ENGINE-AUTHORITATIVE counters ────────────────────
            "eng_fills_total": eng_fills,
            "eng_fills_opposite_open": eng_opp,
            "eng_pct_fills_opposite_open": (
                100.0 * eng_opp / max(1, eng_fills)),
            "eng_bars_both_sides_open": eng_hedged_bars,
            "eng_hedged_spread_max_usd": eng_spread_max,
        }

        m = per_month[ym]
        print(
            f"  {ym} mkt={market[ym]:+6.2f}%  legs={m['legs']:>4}  "
            f"fills={eng_fills:>4} opp={eng_opp:>3} "
            f"({m['eng_pct_fills_opposite_open']:4.1f}%)  "
            f"hedged_bars={eng_hedged_bars:>6}  "
            f"max_spread=${eng_spread_max:8.2f}  "
            f"episodes@${CANON_SPREAD_USD:g}="
            f"{episodes[f'{CANON_SPREAD_USD:g}']:>2}",
            flush=True,
        )

        del bars, art
        import gc
        gc.collect()

    # ── Aggregate ────────────────────────────────────────────────
    print()
    print("=" * 100)
    print("CONTRA CLOSE — FIRING ANALYSIS (feature OFF, book reconstructed)")
    print("=" * 100)

    tot_legs = sum(v["legs"] for v in per_month.values())
    tot_hedged = sum(v["eng_bars_both_sides_open"]
                      for v in per_month.values())
    tot_bars = sum(v["bars_traced"] for v in per_month.values())
    tot_incoming = sum(v["eng_fills_opposite_open"]
                       for v in per_month.values())
    tot_fills = sum(v["eng_fills_total"] for v in per_month.values())
    tot_events = sum(v["n_contra_events"] for v in per_month.values())
    spread_max = max(v["eng_hedged_spread_max_usd"]
                      for v in per_month.values())

    # The hedged-bars-only spread distribution. This is the meaningful
    # one -- the all-bars median is $0.00 by construction (one side
    # empty -> the helper returns 0.0).
    med_of_meds = float(np.median(
        [v["hedged_spread_p50"] for v in per_month.values()]))
    max_p90 = max(v["hedged_spread_p90"] for v in per_month.values())
    max_p99 = max(v["hedged_spread_p99"] for v in per_month.values())

    print(f"months               : {len(per_month)}")
    print(f"legs                 : {tot_legs}")
    print(f"bars traced          : {tot_bars}")
    print(f"fills (engine)       : {tot_fills}")
    print(f"bars with BOTH sides : {tot_hedged} "
          f"({100.0 * tot_hedged / tot_bars:.1f}%)")
    print(f"INCOMING-OPPOSITE fills (engine, user's description): "
          f"{tot_incoming} "
          f"({100.0 * tot_incoming / max(1, tot_fills):.1f}% of fills)")
    print(f"widest hedged spread EVER: ${spread_max:.2f}")
    print(f"hedged-spread median of monthly medians: ${med_of_meds:.2f}")
    print(f"worst monthly hedged p90: ${max_p90:.2f}  "
          f"worst monthly hedged p99: ${max_p99:.2f}")
    print(f"canonical threshold   : ${CANON_SPREAD_USD:.2f}")
    print(f"canonical events      : {tot_events}")
    if spread_max < CANON_SPREAD_USD:
        print()
        print(f"  *** THE CANONICAL THRESHOLD IS ABOVE THE WIDEST SPREAD "
              f"THE BOOK EVER SHOWED.")
        print(f"  *** ${CANON_SPREAD_USD:.2f} > ${spread_max:.2f} -> the "
              f"feature CANNOT fire on this corpus.")

    print()
    print("Passive-scan fires per month, by threshold:")
    hdr = "  thr  " + "".join(f"{thr:>7}" for thr in LADDER)
    print(hdr)
    for thr in LADDER:
        key = f"{thr:g}"
        row = "".join(
            f"{v['passive_scan_fires_by_threshold'][key]:>7}"
            for v in per_month.values())
        print(f"  {key:>4}{row}")

    print()
    totals = {
        key: sum(v["passive_scan_fires_by_threshold"][key]
                 for v in per_month.values())
        for key in (f"{t:g}" for t in LADDER)
    }
    ep_totals = {
        key: sum(v["passive_scan_episodes_by_threshold"][key]
                 for v in per_month.values())
        for key in (f"{t:g}" for t in LADDER)
    }
    print("  TOT " + "".join(f"{totals[f'{t:g}']:>7}" for t in LADDER))
    print("  EPI " + "".join(f"{ep_totals[f'{t:g}']:>7}" for t in LADDER))
    print()
    print(f"  canonical threshold ${CANON_SPREAD_USD:.2f} -> "
          f"{totals[f'{CANON_SPREAD_USD:g}']} qualifying bars, "
          f"{ep_totals[f'{CANON_SPREAD_USD:g}']} EPISODES")
    print(f"  $0.00 (any positive lock) -> {totals['0']} qualifying bars, "
          f"{ep_totals['0']} EPISODES")
    print(f"  incoming-opposite fills -> {tot_incoming}")

    out = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "canonical_threshold": CANON_SPREAD_USD,
        "ladder": LADDER,
        "total_legs": tot_legs,
        "total_bars_traced": tot_bars,
        "total_bars_both_sides": tot_hedged,
        "total_fills": tot_fills,
        "total_incoming_opposite_fills": tot_incoming,
        "widest_hedged_spread_ever_usd": spread_max,
        "total_canonical_events": tot_events,
        "passive_fires_totals": totals,
        "passive_episodes_totals": ep_totals,
        "per_month": per_month,
    }
    OUT.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
