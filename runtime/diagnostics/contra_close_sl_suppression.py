"""DECISIVE — does an SL exit ever DESTROY a locked-in hedge?

The user's claim:

    "if there are 4 trades open i.e. 0.02 lots long and 0.002 lots
     short, both buy low sell high, in theory it is fully hedged and
     free money. but because we didn't close them simultaneously or
     remove SL, we lose money instead."

This script tests the loss mechanism directly.

For every trade that exited on ``sl`` (or any exit), it asks:

    WAS THERE AN OPPOSITE-SIDE POSITION OPEN AT THE EXIT BAR?

If yes, the book WAS hedged at the moment the stop fired, and the
stop "broke the hedge". Then the decisive question is what the
COMBINED pair was worth:

  * long @ L, short @ S, both qty q.
    Combined unrealised P&L at price p = (p - L)*q + (S - p)*q
                                   = (S - L)*q      <-- CONSTANT.

  So the pair is worth EXACTLY (S - L) per ounce REGARDLESS of where
  price goes. If (S - L) > 0 the pair is LOCKED IN A GAIN and the
  SL exit destroyed money. If (S - L) < 0 the pair was locked in a
  LOSS and the SL exit was CORRECT (it cut a loser early).

That single comparison decides the feature:

  * ``sl_exit_with_locked_gain``  -> the user's premise is RIGHT.
    Closing simultaneously (or suppressing the SL on the hedged leg)
    would have banked money the stop threw away.
  * ``sl_exit_with_locked_loss``   -> the premise is WRONG for that
    trade. The stop saved money; removing it would have made the
    loss WORSE.

Note the user's own example is NOT a hedge: 0.02 lots long against
0.002 lots short is a 10:1 net-long book. The combined P&L of an
UNEQUAL pair is (p - L)*q_L + (S - p)*q_S, which is only constant
when q_L == q_S. This script therefore reports the quantity ratio
as well, and splits the result by whether the pair was BALANCED.

Measurement method: position intervals from the trade ledger
(validated against the engine's live-book counter in
``contra_close_truth.py`` -- 8/8 slices, exact agreement).

Usage:
    python -m runtime.diagnostics.contra_close_sl_suppression
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/contra_close_sl_suppression.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _positions(res) -> list[dict]:
    """One record per POSITION (TP1 legs merged on parent_zone_id).

    The position's SIZE is the SUM of its legs' quantities. A TP1
    position books TWO legs -- the partial close (50%) and the
    remainder (50%) -- so taking the FIRST leg's ``qty_btc`` halves
    the position's size and makes every qty comparison wrong.
    """
    by_parent: dict[int, list] = {}
    for t in res.trades:
        by_parent.setdefault(int(t.parent_zone_id), []).append(t)
    out = []
    for pid, recs in by_parent.items():
        r0 = recs[0]
        out.append({
            "parent": pid,
            "entry_bar": int(r0.entry_bar),
            "exit_bar": max(int(r.exit_bar) for r in recs),
            "direction": int(r0.direction),
            "entry_price": float(r0.entry_price),
            "qty": float(sum(float(r.qty_btc) for r in recs)),
            "exit_reasons": [r.exit_reason for r in recs],
            "gross": sum(float(r.pnl_gross_usd) for r in recs),
            "net": sum(float(r.pnl_net_usd) for r in recs),
        })
    return out


def _analyse(positions: list[dict], close: np.ndarray) -> dict:
    """For every position, find opposing positions open at its EXIT."""
    longs = [p for p in positions if p["direction"] > 0]
    shorts = [p for p in positions if p["direction"] < 0]

    # ── 1. the "SL broke the hedge" test ────────────────────────
    n_exit_hedged = 0
    n_exit_hedged_gain = 0
    n_exit_hedged_loss = 0
    n_balanced_gain = 0
    n_unbalanced_gain = 0
    locked_gain_usd: list[float] = []
    locked_loss_usd: list[float] = []
    ratios: list[float] = []
    sl_exit_hedged = 0

    for p in positions:
        opp = shorts if p["direction"] > 0 else longs
        live = [o for o in opp
                if o["entry_bar"] <= p["exit_bar"] <= o["exit_bar"]]
        if not live:
            continue
        n_exit_hedged += 1
        # quantity-weighted opposing entry
        q_o = sum(o["qty"] for o in live)
        px_o = sum(o["qty"] * o["entry_price"] for o in live) / q_o
        q_s = p["qty"]
        px_s = p["entry_price"]

        # Combined P&L of the pair, per unit of the SMALLER side.
        # long p (px_s) + short o (px_o):
        #   value(p) = (p - px_s)*q_s + (px_o - p)*q_o
        # With q_s == q_o it is constant = (px_o - px_s)*q.
        # With unequal sizes it is LINEAR in p -- slope (q_s - q_o).
        def _pair_value(price: float) -> float:
            if p["direction"] > 0:
                return (price - px_s) * q_s + (px_o - price) * q_o
            return (px_s - price) * q_s + (price - px_o) * q_o

        # The LOCKED component is the part that cannot be lost: for a
        # long/short pair the pair's value at the SHORT's entry and at
        # the LONG's entry bracket the extremes. The constant floor
        # (when balanced) is (px_o - px_s) * min(q_s, q_o) with the
        # sign of the direction of the price move.
        overlap = min(q_s, q_o)
        balanced = abs(q_s - q_o) <= 1e-9
        ratio = (q_s / q_o) if q_o > 0 else 0.0
        ratios.append(ratio)

        if balanced:
            # value is CONSTANT = (px_o - px_s) * q for a long p.
            if p["direction"] > 0:
                locked = (px_o - px_s) * overlap
            else:
                locked = (px_s - px_o) * overlap
            if locked > 0:
                n_exit_hedged_gain += 1
                n_balanced_gain += 1
                locked_gain_usd.append(locked)
            else:
                n_exit_hedged_loss += 1
                locked_loss_usd.append(locked)
        else:
            # Unequal pair -> not locked; evaluate at the exit price.
            b = p["exit_bar"]
            px = float(close[b]) if b < len(close) else px_s
            val = _pair_value(px)
            if val > 0:
                n_exit_hedged_gain += 1
                n_unbalanced_gain += 1
                locked_gain_usd.append(val)
            else:
                n_exit_hedged_loss += 1
                locked_loss_usd.append(val)

        if "sl" in p["exit_reasons"]:
            sl_exit_hedged += 1

    # ── 2. the balance ratio the user's example implies ─────────
    # What does the book look like when it IS hedged? Ratio of long
    # qty to short qty at overlapping moments.
    n_balanced_pairs = 0
    for L in longs:
        for S in shorts:
            lo = max(L["entry_bar"], S["entry_bar"])
            hi = min(L["exit_bar"], S["exit_bar"])
            if lo > hi:
                continue
            n_balanced_pairs += 1

    def _stats(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        a = np.asarray(xs, dtype=np.float64)
        return {
            "n": int(a.size),
            "p50": float(np.percentile(a, 50)),
            "p90": float(np.percentile(a, 90)),
            "min": float(a.min()),
            "max": float(a.max()),
            "sum": float(a.sum()),
        }

    return {
        "n_exits_with_opposite_open": n_exit_hedged,
        "n_sl_exits_with_opposite_open": sl_exit_hedged,
        "n_exit_hedged_LOCKED_GAIN": n_exit_hedged_gain,
        "n_exit_hedged_LOCKED_LOSS": n_exit_hedged_loss,
        "n_balanced_gain": n_balanced_gain,
        "n_unbalanced_gain": n_unbalanced_gain,
        "n_opposite_pairs_overlapping": n_balanced_pairs,
        "locked_gain_usd": _stats(locked_gain_usd),
        "locked_loss_usd": _stats(locked_loss_usd),
        "qty_ratio_stats": _stats(ratios),
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            print(f"  skip {ym}: no cache", flush=True)
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=False,
            contra_close=False, contra_close_spread_usd=0.0,
        )
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek,
        )

        close = bars["close"].to_numpy(dtype=np.float64)
        positions = _positions(res)
        a = _analyse(positions, close)
        mkt = (close[-1] / close[0] - 1.0) * 100.0
        rows[ym] = {
            "market_pct": mkt,
            "positions": len(positions),
            "legs": len(res.trades),
            **a,
        }
        print(f"  {ym} mkt={mkt:+6.2f}%  pos={len(positions):>3}  "
              f"exitHedged={a['n_exits_with_opposite_open']:>3}  "
              f"GAIN={a['n_exit_hedged_LOCKED_GAIN']:>3}  "
              f"LOSS={a['n_exit_hedged_LOCKED_LOSS']:>3}  "
              f"slHedged={a['n_sl_exits_with_opposite_open']:>3}",
              flush=True)

        del bars, art
        import gc
        gc.collect()

    print()
    print("=" * 104)
    print("CONTRA CLOSE — DID AN SL EXIT DESTROY A LOCKED HEDGE?")
    print("=" * 104)
    print(f"{'month':<9}{'mkt%':>8}{'pos':>5}{'exitHedg':>10}{'GAIN':>6}"
          f"{'LOSS':>6}{'slHedg':>8}{'gain$':>10}{'loss$':>10}")

    for ym, v in rows.items():
        g = v["locked_gain_usd"]
        l = v["locked_loss_usd"]
        print(f"{ym:<9}{v['market_pct']:>+8.2f}{v['positions']:>5}"
              f"{v['n_exits_with_opposite_open']:>10}"
              f"{v['n_exit_hedged_LOCKED_GAIN']:>6}"
              f"{v['n_exit_hedged_LOCKED_LOSS']:>6}"
              f"{v['n_sl_exits_with_opposite_open']:>8}"
              f"{g.get('sum', 0.0):>10.2f}{l.get('sum', 0.0):>10.2f}")

    n_exit = sum(v["n_exits_with_opposite_open"] for v in rows.values())
    n_gain = sum(v["n_exit_hedged_LOCKED_GAIN"] for v in rows.values())
    n_loss = sum(v["n_exit_hedged_LOCKED_LOSS"] for v in rows.values())
    n_bal = sum(v["n_balanced_gain"] for v in rows.values())
    n_unbal = sum(v["n_unbalanced_gain"] for v in rows.values())
    n_sl = sum(v["n_sl_exits_with_opposite_open"] for v in rows.values())
    n_pairs = sum(v["n_opposite_pairs_overlapping"] for v in rows.values())
    gain_sum = sum(v["locked_gain_usd"].get("sum", 0.0) for v in rows.values())
    loss_sum = sum(v["locked_loss_usd"].get("sum", 0.0) for v in rows.values())

    print()
    print(f"TOTAL opposite-side pairs overlapping : {n_pairs:,}")
    print(f"TOTAL exits that HAD an opposite open  : {n_exit:,}")
    print(f"    ... locked in a GAIN (money burned)  : {n_gain:,}")
    print(f"    ... locked in a LOSS (stop was right): {n_loss:,}")
    print(f"    ... of the GAIN, BALANCED pairs       : {n_bal:,}")
    print(f"    ... of the GAIN, UNBALANCED pairs     : {n_unbal:,}")
    print(f"TOTAL SL exits with an opposite open    : {n_sl:,}")
    print(f"TOTAL locked-in GAIN at those exits     : ${gain_sum:,.2f}")
    print(f"TOTAL locked-in LOSS at those exits     : ${loss_sum:,.2f}")
    print()
    if n_exit == 0:
        print("VERDICT: the book is NEVER hedged at an exit. The premise's "
              "mechanism does not occur.")
    else:
        print(f"VERDICT: of {n_exit:,} hedged exits, {n_gain:,} had a locked "
              f"GAIN and {n_loss:,} had a locked LOSS.")
        if n_gain and n_loss:
            print(f"  -> {100.0 * n_gain / n_exit:.1f}% gain / "
                  f"{100.0 * n_loss / n_exit:.1f}% loss.")
        if gain_sum > abs(loss_sum):
            print(f"  -> NET ${gain_sum - loss_sum:,.2f} would have been "
                  f"banked by closing simultaneously.")
        else:
            print(f"  -> NET ${gain_sum - loss_sum:,.2f} -> closing "
                  f"simultaneously would have LOST money.")

    OUT.write_text(json.dumps({"per_month": rows}, indent=2, default=float),
                   encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
