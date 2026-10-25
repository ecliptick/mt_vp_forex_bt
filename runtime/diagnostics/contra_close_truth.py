"""DECISIVE TEST — which opposite-fill counter is correct?

`contra_close_firing.json` contains TWO mutually exclusive measurements
of "was the book hedged when this fill landed":

  * `n_incoming_opposite_fills`  — reconstructed by replaying the
    closed-trade LEDGER (a reimplementation).
  * `eng_fills_opposite_open`     — counted by the ENGINE, inside the
    bar loop, against the real live book.

On 2026-03 they disagree catastrophically: ledger says 48 of 95 fills
(50.5%) were incoming-opposite; engine says 0, and 0 bars ever had
both sides open.

This script computes a THIRD, fully independent measurement: pairwise
INTERVAL OVERLAP on the trade ledger. Two trades on opposite sides
were simultaneously open iff their [entry_bar, exit_bar] intervals
overlap. No replay, no ordering assumptions — just interval geometry.

Then it compares all three.

Usage:
    python -m runtime.diagnostics.contra_close_truth
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
OUT = Path("runtime/diagnostics/_reports/contra_close_truth.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2026-03", "2026-04", "2025-10", "2025-09", "2026-01",
    "2025-05", "2025-03", "2024-06",
]


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _interval_overlap_count(trades: list) -> tuple[int, int, int]:
    """Independent measure: how many fills landed while an opposite
    position was open, by PAIRWISE INTERVAL OVERLAP.

    A trade occupies the book over [entry_bar, exit_bar]. An incoming
    opposite fill is one whose entry_bar falls inside some
    opposite-side trade's interval.

    Returns (n_opposite_fills, n_same_side_fills, n_bars_both_sides).
    """
    longs = [(int(t["entry_bar"]), int(t["exit_bar"]))
             for t in trades if int(t["direction"]) > 0]
    shorts = [(int(t["entry_bar"]), int(t["exit_bar"]))
              for t in trades if int(t["direction"]) < 0]

    n_opp = 0
    n_same = 0
    for t in trades:
        entry_bar = int(t["entry_bar"])
        opp = shorts if int(t["direction"]) > 0 else longs
        hit = any(lo <= entry_bar <= hi for lo, hi in opp)
        if hit:
            n_opp += 1
        else:
            n_same += 1

    # Bars on which both sides were open, from the union of intervals.
    def _union(iv: list[tuple[int, int]]) -> list[tuple[int, int]]:
        if not iv:
            return []
        iv = sorted(iv)
        out = [list(iv[0])]
        for lo, hi in iv[1:]:
            if lo <= out[-1][1] + 1:
                out[-1][1] = max(out[-1][1], hi)
            else:
                out.append([lo, hi])
        return [(a, b) for a, b in out]

    lu = _union(longs)
    su = _union(shorts)
    both = 0
    for lo, hi in lu:
        for lo2, hi2 in su:
            a, b = max(lo, lo2), min(hi, hi2)
            if a <= b:
                both += b - a + 1
    return n_opp, n_same, both


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
            contra_hedge_stats=True,
        )

        trades = list(res.trades)
        # One record per POSITION: a TP1 partial close produces two
        # records for one position. Deduplicate on parent_zone_id.
        by_parent: dict[int, list] = {}
        for t in trades:
            by_parent.setdefault(int(t.parent_zone_id), []).append(t)
        positions = []
        for pid, recs in by_parent.items():
            r0 = recs[0]
            positions.append({
                "parent": pid,
                "entry_bar": int(r0.entry_bar),
                "exit_bar": max(int(r.exit_bar) for r in recs),
                "direction": int(r0.direction),
                "entry_price": float(r0.entry_price),
                "exit_price": float(recs[-1].exit_price),
                "qty": sum(float(r.qty_btc) for r in recs),
                "exit_reasons": [r.exit_reason for r in recs],
            })

        n_opp_iv, n_same_iv, both_bars_iv = _interval_overlap_count(
            [{"entry_bar": p["entry_bar"], "exit_bar": p["exit_bar"],
              "direction": p["direction"]} for p in positions])

        eng_opp = int(getattr(res, "n_fills_opposite_open", 0))
        eng_fills = int(getattr(res, "n_fills_total", 0))
        eng_both = int(getattr(res, "n_bars_both_sides_open", 0))
        eng_max_spread = float(getattr(res, "hedged_spread_max_usd", 0.0))

        rows[ym] = {
            "legs": len(trades),
            "positions": len(positions),
            "fills_engine": eng_fills,
            "opp_ENGINE": eng_opp,
            "opp_INTERVAL": n_opp_iv,
            "same_INTERVAL": n_same_iv,
            "both_bars_ENGINE": eng_both,
            "both_bars_INTERVAL": both_bars_iv,
            "max_spread_ENGINE": eng_max_spread,
        }
        print(f"  {ym}  positions={len(positions):>3}  "
              f"opp ENGINE={eng_opp:>3}  opp INTERVAL={n_opp_iv:>3}  "
              f"bothbars ENGINE={eng_both:>6}  INTERVAL={both_bars_iv:>6}",
              flush=True)

        del bars, art
        import gc
        gc.collect()

    print()
    print("=" * 96)
    print("CONTRA CLOSE — WHICH COUNTER IS TRUE?")
    print("=" * 96)
    print(f"{'month':<9}{'pos':>5}{'opp_ENG':>9}{'opp_IVL':>9}"
          f"{'both_ENG':>11}{'both_IVL':>10}")
    for ym, v in rows.items():
        print(f"{ym:<9}{v['positions']:>5}{v['opp_ENGINE']:>9}"
              f"{v['opp_INTERVAL']:>9}{v['both_bars_ENGINE']:>11}"
              f"{v['both_bars_INTERVAL']:>10}")

    print()
    agree_ivl = sum(v["opp_ENGINE"] == v["opp_INTERVAL"]
                    for v in rows.values())
    agree_eng_bars = sum(abs(v["both_bars_ENGINE"]
                             - v["both_bars_INTERVAL"])
                         <= 0.02 * max(1, v["both_bars_INTERVAL"])
                         for v in rows.values())
    print(f"months where ENGINE opposite == INTERVAL opposite: "
          f"{agree_ivl}/{len(rows)}")
    print(f"months where ENGINE hedged-bars ~= INTERVAL hedged-bars: "
          f"{agree_eng_bars}/{len(rows)}")

    tot_eng = sum(v["opp_ENGINE"] for v in rows.values())
    tot_ivl = sum(v["opp_INTERVAL"] for v in rows.values())
    tot_fills = sum(v["fills_engine"] for v in rows.values())
    print()
    print(f"TOTAL fills            : {tot_fills}")
    print(f"TOTAL opposite (ENGINE): {tot_eng}")
    print(f"TOTAL opposite (INTERV): {tot_ivl}")

    OUT.write_text(json.dumps({"per_month": rows}, indent=2, default=float),
                   encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
