"""DECISIVE — is the book actually hedged, and is the hedge "free money"?

The user's premise:

    "if there are 4 trades open i.e. 0.02 lots long and 0.002 lots
     short, both buy low sell high, in theory it is fully hedged and
     free money. but because we didn't close them simultaneously or
     remove SL, we lose money instead."

That premise has FOUR separable claims. This script measures each
one directly:

  C1. The book holds BOTH sides at once, often enough to matter.
      -> overlapping opposite-side position intervals, and the
         number of BARS covered by that overlap.

  C2. When it does, the two legs are LOCKED IN A GAIN ("both buy low
      sell high" -> price sits between the long entry and the short
      entry, so each leg is individually in profit and the pair is
      worth exactly (S - L) per ounce regardless of where price goes).
      -> over every overlapping bar, is price between L and S?

  C3. The locked gain is big enough to be worth taking.
      -> the (S - L) spread distribution and the dollar value of the
         overlapping quantity.

  C4. Is the hedged-and-locked state DIRECTIONAL? If the hedge only
      appears when the market is up, it is a beta proxy, not a
      risk-management feature.
      -> corr(hedged-bar rate, market move) across slices, and the
         UP / DOWN / FLAT split.

Measurement method: POSITION INTERVALS from the trade ledger.
A position occupies the book over [entry_bar, exit_bar] (a TP1
partial close produces two legs for one position -- they are merged
on ``parent_zone_id`` and the interval is the union). This method was
validated against the engine's own live-book counter in
``contra_close_truth.py`` (8/8 slices, exact agreement).

Usage:
    python -m runtime.diagnostics.contra_close_premise
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
OUT = Path("runtime/diagnostics/_reports/contra_close_premise.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2026-03", "2026-04", "2025-10", "2025-09",
    "2026-01", "2025-05", "2025-03", "2024-06",
]

SPREAD_USD = 4.50  # canonical contra_close_spread_usd


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _positions(res) -> list[dict]:
    """One record per POSITION (TP1 legs merged)."""
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
            "qty": float(r0.qty_btc),
        })
    return out


def _hedge_scan(positions: list[dict], close: np.ndarray) -> dict:
    """Scan every bar covered by an overlapping opposite-side pair."""
    longs = [p for p in positions if p["direction"] > 0]
    shorts = [p for p in positions if p["direction"] < 0]

    n_bars_both = 0
    n_locked_gain = 0
    n_locked_loss = 0
    n_eligible = 0
    spreads: list[float] = []
    gain_dollars: list[float] = []
    loss_dollars: list[float] = []
    n_pairs = 0

    for L in longs:
        for S in shorts:
            lo = max(L["entry_bar"], S["entry_bar"])
            hi = min(L["exit_bar"], S["exit_bar"])
            if lo > hi:
                continue
            n_pairs += 1
            spread = S["entry_price"] - L["entry_price"]
            overlap = min(L["qty"], S["qty"])
            for b in range(lo, hi + 1):
                if b >= len(close):
                    break
                n_bars_both += 1
                spreads.append(spread)
                px = float(close[b])
                if L["entry_price"] < px < S["entry_price"]:
                    # price between the two entries -> BOTH legs are
                    # individually in profit; the pair is worth
                    # exactly (S - L) per ounce, locked in.
                    n_locked_gain += 1
                    gain_dollars.append(spread * overlap)
                else:
                    # price outside the band -> one leg is underwater
                    # and the "hedge" is not free money at all.
                    n_locked_loss += 1
                    loss_dollars.append(abs(spread) * overlap)
                if spread >= SPREAD_USD:
                    n_eligible += 1

    def _stats(xs: list[float]) -> dict:
        if not xs:
            return {"n": 0}
        a = np.asarray(xs, dtype=np.float64)
        return {
            "n": int(a.size),
            "p50": float(np.percentile(a, 50)),
            "p90": float(np.percentile(a, 90)),
            "max": float(a.max()),
            "sum": float(a.sum()),
        }

    return {
        "n_opposite_pairs_overlapping": n_pairs,
        "bars_both_sides": n_bars_both,
        "bars_locked_GAIN": n_locked_gain,
        "bars_locked_LOSS": n_locked_loss,
        "bars_eligible_for_contra": n_eligible,
        "spread_stats": _stats(spreads),
        "locked_gain_dollars": _stats(gain_dollars),
        "locked_loss_dollars": _stats(loss_dollars),
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
        h = _hedge_scan(positions, close)
        mkt_ret = (close[-1] / close[0] - 1.0) * 100.0

        n_bars = int(len(close))
        rows[ym] = {
            "bars_total": n_bars,
            "positions": len(positions),
            "market_return_pct": mkt_ret,
            "pct_bars_both_sides": (100.0 * h["bars_both_sides"]
                                    / max(1, n_bars)),
            "eng_opp_fills": int(getattr(res, "n_fills_opposite_open", 0)),
            "eng_both_bars": int(getattr(res, "n_bars_both_sides_open", 0)),
            "eng_max_spread": float(getattr(res, "hedged_spread_max_usd", 0.0)),
            "legs": len(res.trades),
            **h,
        }
        print(f"  {ym}  bars={n_bars:>9,}  pairs={h['n_opposite_pairs_overlapping']:>3}"
              f"  bothbars={h['bars_both_sides']:>6}"
              f"  gain={h['bars_locked_GAIN']:>6}"
              f"  loss={h['bars_locked_LOSS']:>6}"
              f"  elig={h['bars_eligible_for_contra']:>6}"
              f"  mkt={mkt_ret:+.2f}%", flush=True)

        del bars, art
        import gc
        gc.collect()

    print()
    print("=" * 112)
    print("CONTRA CLOSE — IS THE BOOK ACTUALLY HEDGED?  (position-interval scan)")
    print("=" * 112)
    print(f"{'month':<9}{'bars':>11}{'both%':>9}{'gain':>8}{'loss':>8}"
          f"{'elig':>8}{'medSprd':>9}{'maxSprd':>9}{'mkt%':>8}")

    def _get(v, k, d=float("nan")):
        return v.get(k, d)

    for ym, v in rows.items():
        ss = v["spread_stats"]
        print(f"{ym:<9}{v['bars_total']:>11,}"
              f"{v['pct_bars_both_sides']:>8.3f}%"
              f"{v['bars_locked_GAIN']:>8}{v['bars_locked_LOSS']:>8}"
              f"{v['bars_eligible_for_contra']:>8}"
              f"{_get(ss,'p50'):>9.2f}{_get(ss,'max'):>9.2f}"
              f"{v['market_return_pct']:>+8.2f}")

    tot_bars = sum(v["bars_total"] for v in rows.values())
    tot_both = sum(v["bars_both_sides"] for v in rows.values())
    tot_gain = sum(v["bars_locked_GAIN"] for v in rows.values())
    tot_loss = sum(v["bars_locked_LOSS"] for v in rows.values())
    tot_elig = sum(v["bars_eligible_for_contra"] for v in rows.values())
    tot_pairs = sum(v["n_opposite_pairs_overlapping"] for v in rows.values())

    print()
    print(f"TOTAL bars                        : {tot_bars:,}")
    print(f"TOTAL opposite-side pairs overlapping: {tot_pairs:,}")
    print(f"TOTAL bars with BOTH sides open  : {tot_both:,}  "
          f"({100.0 * tot_both / max(1, tot_bars):.4f}% of bars)")
    print(f"TOTAL bars locked in a GAIN       : {tot_gain:,}")
    print(f"TOTAL bars UNDERWATER (one leg)   : {tot_loss:,}")
    print(f"TOTAL bars eligible for contra    : {tot_elig:,}  "
          f"(spread >= ${SPREAD_USD:.2f})")
    print()
    print("C1 (book holds both sides at once) : "
          f"{'YES' if tot_both else 'NO'} — {tot_both:,} bars "
          f"({100.0 * tot_both / max(1, tot_bars):.4f}% of all bars)")
    print("C2 (locked in a GAIN, price between): "
          f"{'YES' if tot_gain else 'NO'} — {tot_gain:,} bars gain vs "
          f"{tot_loss:,} bars underwater")
    if tot_gain + tot_loss:
        print(f"    -> the 'free money' state is "
              f"{100.0 * tot_gain / (tot_gain + tot_loss):.1f}% of hedged bars")
    print("C3 (big enough to matter)          : "
          f"{tot_elig:,} bars eligible at >= ${SPREAD_USD:.2f}")
    if len(rows) > 2:
        rate = [v["pct_bars_both_sides"] for v in rows.values()]
        mkt = [v["market_return_pct"] for v in rows.values()]
        if np.std(rate) > 0 and np.std(mkt) > 0:
            r = float(np.corrcoef(rate, mkt)[0, 1])
            print(f"C4 (directional?)                  : "
                  f"corr(hedged-bar rate, market move) = {r:+.3f}")

    OUT.write_text(json.dumps({"per_month": rows}, indent=2, default=float),
                   encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
