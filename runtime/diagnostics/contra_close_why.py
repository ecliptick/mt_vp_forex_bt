"""WHY does the contra close help? (2026-10-24)

The A/B (`runtime/diagnostics/contra_close_ab.py`) measured a total
gain of ~+$83 at a $4.50 threshold — but the gain is CONCENTRATED:

    2026-01 BULL  +$85.71   (2 events, max spread $78.44)
    2025-09 BULL  +$9.20    (5 events, max spread $10.39)
    2025-05 FLAT  -$11.78   (2 events, max spread $19.24)
    2026-03 BEAR   $0.00    (0 events)
    2024-11 BEAR   $0.00    (0 events)

Two questions decide whether this is a real improvement or another
beta proxy:

1. WHAT does the netting replace? If the netted legs were about to be
   STOPPED OUT on the same bar, the netting avoided a loss and the
   gain is a genuine risk reduction. If the legs were going to run to
   their targets, the netting crystallised a small certain gain and
   gave up a larger contingent one — and the "gain" is an accounting
   choice, not an edge.

2. Is the DELTA correlated with the market's direction? The netting
   can only fire on a POSITIVE spread (the code requires
   ``spread >= threshold``), and a positive spread exists when the
   longs entered BELOW the shorts — which is a BULL-shaped book. A
   bear-shaped book holds a locked LOSS, so the feature never fires
   there. That asymmetry means the feature's benefit is structurally
   tilted toward up-moves, and the flat slice (the decisive test) is
   where it has to prove itself.

This diagnostic answers both.

Usage
-----
    python -m runtime.diagnostics.contra_close_why
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter
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
OUT = Path("runtime/diagnostics/_reports/contra_close_why.json")

SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP", "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

THRESHOLD = CANON_SPREAD_USD


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {"ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "threshold": THRESHOLD, "slices": []}

    for tag, s, e, regime in SLICES:
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

        def _run(**extra):
            ek = build_engine_kwargs(
                spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
                structure=art["structure"], vol_state=art["vol_state"],
                structure_gate=False, **extra,
            )
            res, _ = run_vp_sniper_backtest_forex(
                bars=bars, zones=art["zones"], vp_set=art["vp_set"],
                lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
                engine_kwargs=ek,
            )
            return res, list(res.trades)

        res_off, off = _run()
        res_on, on = _run(contra_close=True,
                          contra_close_spread_usd=THRESHOLD)

        # ── Q1: what did the netting REPLACE? ─────────────────────
        #
        # Match the OFF run's closed trades against the ON run's
        # contra-closed trades by (entry_time_ns, direction). For
        # each netted leg, find what the OFF run did with the SAME
        # position: did it stop out, hit its target, or close at EOD?
        off_by_key = {}
        for t in off:
            off_by_key.setdefault(
                (int(t.entry_time_ns), int(t.direction)), []).append(t)

        replaced: list[dict] = []
        for t in on:
            if t.exit_reason != "contra_close":
                continue
            cands = off_by_key.get((int(t.entry_time_ns), int(t.direction)), [])
            # The OFF run may have split this position into several
            # legs (TP1 partial + remainder). Attribute the whole
            # position's OFF outcome to the netted slice.
            entry = t.entry_price
            outcome = "no_match"
            for c in cands:
                if abs(float(c.entry_price) - entry) > 1e-9:
                    continue
                outcome = c.exit_reason
                break
            replaced.append({
                "direction": int(t.direction),
                "entry": float(t.entry_price),
                "exit": float(t.exit_price),
                "qty": float(t.qty_btc),
                "contra_gross": float(t.pnl_gross_usd),
                "off_outcome": outcome,
            })

        outcomes = Counter(r["off_outcome"] for r in replaced)

        # ── The gross the OFF run made on those SAME positions ──
        off_gross_same = 0.0
        for r in replaced:
            for c in off_by_key.get(
                    (int(c["entry_time_ns"]) if False else 0,), []):
                pass
        # Simpler and exact: recompute the OFF gross for the netted
        # entries by matching entry_time + entry price.
        off_gross_same = 0.0
        off_qty_same = 0.0
        netted_keys = {(int(t.entry_time_ns), int(t.direction),
                        float(t.entry_price))
                       for t in on if t.exit_reason == "contra_close"}
        for c in off:
            k = (int(c.entry_time_ns), int(c.direction), float(c.entry_price))
            if k in netted_keys:
                off_gross_same += float(c.pnl_gross_usd)
                off_qty_same += float(c.qty_btc)

        contra_gross = sum(float(t.pnl_gross_usd) for t in on
                            if t.exit_reason == "contra_close")
        contra_qty = sum(float(t.qty_btc) for t in on
                         if t.exit_reason == "contra_close")

        mkt = float((bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0)
        d_gross = (sum(float(t.pnl_gross_usd) for t in on)
                   - sum(float(t.pnl_gross_usd) for t in off))

        row = {
            "tag": tag, "regime": regime, "mkt_ret_pct": mkt,
            "off_gross": sum(float(t.pnl_gross_usd) for t in off),
            "on_gross": sum(float(t.pnl_gross_usd) for t in on),
            "delta_gross": d_gross,
            "n_contra": int(getattr(res_on, "n_contra_close_events", 0)),
            "contra_gross": contra_gross,
            "contra_qty": contra_qty,
            "off_gross_on_same_positions": off_gross_same,
            "off_qty_on_same_positions": off_qty_same,
            "banked_minus_given_up": contra_gross - off_gross_same,
            "off_outcome_of_netted_legs": dict(outcomes),
            "legs": replaced,
        }
        report["slices"].append(row)

        print(f"\n{tag}  (mkt {mkt:+.2f}%, {regime})")
        print(f"  delta gross          {d_gross:>12,.2f}")
        print(f"  contra events        {row['n_contra']:>12}")
        print(f"  contra gross banked  {contra_gross:>12,.2f}  "
              f"(qty {contra_qty:.2f})")
        print(f"  OFF gross on the SAME positions  {off_gross_same:>12,.2f}  "
              f"(qty {off_qty_same:.2f})")
        print(f"  banked - given up    {row['banked_minus_given_up']:>12,.2f}")
        print(f"  what the OFF run did with those legs: {dict(outcomes)}")

        del bars, art, res_off, res_on, off, on
        import gc
        gc.collect()

    # ── Q2: is the DELTA correlated with the market direction? ────
    xs = [r["mkt_ret_pct"] for r in report["slices"]]
    ys = [r["delta_gross"] for r in report["slices"]]
    corr = float(np.corrcoef(xs, ys)[0, 1]) if len(xs) > 2 else float("nan")
    report["delta_vs_market_corr"] = corr
    report["delta_total"] = float(sum(ys))
    flat = [r for r in report["slices"] if r["regime"] == "flat"]
    report["flat_delta_total"] = float(sum(r["delta_gross"] for r in flat))

    print("\n" + "=" * 96)
    print(f"total delta gross      {report['delta_total']:>14,.2f}")
    print(f"delta on FLAT slices   {report['flat_delta_total']:>14,.2f}  "
          f"(the decisive test)")
    print(f"corr(delta, mkt move)  {corr:>14.4f}")
    print("=" * 96)

    OUT.write_text(json.dumps(report, indent=2, default=float), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
