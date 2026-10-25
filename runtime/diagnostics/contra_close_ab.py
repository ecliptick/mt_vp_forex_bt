"""CONTRA CLOSE / HEDGE NETTING — A/B on the historical slices (2026-10-24).

The user: *"if there are 4 trades open i.e 0.02 lots long and 0.002
lots short, both buy low sell high, in theory it is fully hedged and
free money. but because we didn't close them simultaneously or remove
SL, we lose money instead."*

The diagnosis is right and it is MECHANICAL, not statistical. The book
can hold opposing positions. Those legs are a LOCKED SPREAD:

    Q * (E_short - E_long)

Left alone, the engine closes them INDEPENDENTLY. One leg's stop
fires while its counterparty sits on an equal-and-opposite gain that
is never taken. The hedge is broken for a LOSS, and the surviving
leg is then a naked directional bet that also pays a round trip.

The contra-close feature nets the overlapping quantity out
SIMULTANEOUSLY at one market price, banking the spread.

This diagnostic answers four questions:

1. How OFTEN does the book actually hold both sides at once? (If the
   answer is "never", the whole premise is wrong.)
2. What is the largest spread the book reaches? (This is what sets
   the threshold; a $4.50 threshold is a guess until it is measured.)
3. Does the netting help or hurt, and by how much — on the FLAT
   slices, which are the decisive test per the beta-proxy diagnosis?
4. Does it help on the BULL/BEAR slices, or only on the flat ones?

The honest caveat up front: the feature banks a spread that is REAL
but SMALL. A $4.50 spread on 1.00 oz banks $4.50 against a $0.47
round trip per leg ($0.94 for the pair). So the netting has to be
worth roughly 20% of a round trip per event to break even. The
question is how many events there are and how big they are.

Usage
-----
    python -m runtime.diagnostics.contra_close_ab
    python -m runtime.diagnostics.contra_close_ab --spreads 1,2.5,4.5,7.5,12
"""
from __future__ import annotations

import argparse
import json
import sys
import time
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
OUT = Path("runtime/diagnostics/_reports/contra_close_ab.json")

# (tag, start, end, regime)
SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP", "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

DEFAULT_SPREADS = [1.0, 2.5, 4.5, 5.5, 6.5, 7.5, 12.0]


def _row(res, bars, tag, regime, s, e, spread, wall) -> dict:
    tr = list(res.trades)
    longs = [t for t in tr if t.direction > 0]
    shorts = [t for t in tr if t.direction < 0]
    mkt = float((bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0)
    contra = [t for t in tr if t.exit_reason == "contra_close"]
    return {
        "tag": tag, "regime": regime, "start": s, "end": e,
        "spread_usd": spread,
        "mkt_ret_pct": mkt,
        "legs": len(tr),
        "n_long": len(longs), "n_short": len(shorts),
        "gross": float(sum(t.pnl_gross_usd for t in tr)),
        "fees": float(sum(t.fee_usd for t in tr)),
        "net": float(sum(t.pnl_net_usd for t in tr)),
        "n_contra_events": int(getattr(res, "n_contra_close_events", 0)),
        "n_contra_legs": int(getattr(res, "n_contra_legs_closed", 0)),
        "n_contra_qty": float(getattr(res, "n_contra_qty_netted", 0.0)),
        "contra_gross": float(getattr(res, "contra_gross_usd", 0.0)),
        "contra_fees": float(getattr(res, "contra_fees_usd", 0.0)),
        "contra_spread_max": float(getattr(res, "contra_spread_usd_max", 0.0)),
        "n_contra_legs_in_trades": len(contra),
        "wall": wall,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spreads", default=",".join(
        f"{v:g}" for v in DEFAULT_SPREADS))
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()
    spreads = [float(x) for x in args.spreads.split(",") if x.strip()]

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []

    # ── 1. The BASELINE (feature off) ──────────────────────────────
    print("=" * 118)
    print("CONTRA CLOSE A/B — canonical sizing "
          f"({FOREX_LOTS} lots x {FOREX_CONTRACT_SIZE} cs = 1.00 oz)")
    print("=" * 118)
    print("\nBASELINE (contra_close OFF)\n")

    for tag, s, e, regime in SLICES:
        bars = load_bars(DATA, s, e)
        art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=False,
        )
        t0 = time.perf_counter()
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek,
        )
        wall = time.perf_counter() - t0
        m = _row(res, bars, tag, regime, s, e, 0.0, wall)
        rows.append(m)
        print(f"  {tag:<14} mkt={m['mkt_ret_pct']:>+6.2f}%  "
              f"legs={m['legs']:>6}  L/S={m['n_long']:>5}/{m['n_short']:>5}  "
              f"gross={m['gross']:>10,.2f}  fees={m['fees']:>9,.2f}  "
              f"net={m['net']:>10,.2f}  wall={wall:>6.1f}s", flush=True)
        del bars, art, res
        import gc
        gc.collect()

    # ── 2. The A/B ─────────────────────────────────────────────────
    for spread in spreads:
        print(f"\nCONTRA CLOSE ON — threshold ${spread:g}\n")
        for tag, s, e, regime in SLICES:
            bars = load_bars(DATA, s, e)
            art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
            spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
            ek = build_engine_kwargs(
                spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
                structure=art["structure"], vol_state=art["vol_state"],
                structure_gate=False,
                contra_close=True,
                contra_close_spread_usd=spread,
            )
            t0 = time.perf_counter()
            res, _ = run_vp_sniper_backtest_forex(
                bars=bars, zones=art["zones"], vp_set=art["vp_set"],
                lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
                engine_kwargs=ek,
            )
            wall = time.perf_counter() - t0
            m = _row(res, bars, tag, regime, s, e, spread, wall)
            rows.append(m)
            print(f"  {tag:<14} mkt={m['mkt_ret_pct']:>+6.2f}%  "
                  f"legs={m['legs']:>6}  "
                  f"contra={m['n_contra_events']:>4}ev/"
                  f"{m['n_contra_legs']:>4}leg  "
                  f"maxSpr={m['contra_spread_max']:>7.2f}  "
                  f"cGross={m['contra_gross']:>9,.2f}  "
                  f"gross={m['gross']:>10,.2f}  net={m['net']:>10,.2f}  "
                  f"wall={wall:>6.1f}s", flush=True)
            del bars, art, res
            import gc
            gc.collect()

    # ── 3. Summary ─────────────────────────────────────────────────
    print("\n" + "=" * 118)
    print("SUMMARY — delta vs the OFF baseline")
    print("=" * 118)
    base = {(r["tag"], r["spread_usd"]): r for r in rows if r["spread_usd"] == 0.0}
    summary: dict[str, dict] = {}
    for spread in spreads:
        d_gross = d_net = 0.0
        flat_d_net = 0.0
        n_flat = 0
        n_ev = 0
        c_gross = 0.0
        for tag, _s, _e, regime in SLICES:
            m = next(r for r in rows
                     if r["tag"] == tag and r["spread_usd"] == spread)
            b = base[(tag, 0.0)]
            d_gross += m["gross"] - b["gross"]
            d_net += m["net"] - b["net"]
            n_ev += m["n_contra_events"]
            c_gross += m["contra_gross"]
            if regime == "flat":
                flat_d_net += m["net"] - b["net"]
                n_flat += 1
        summary[f"{spread:g}"] = {
            "delta_gross": d_gross,
            "delta_net": d_net,
            "delta_net_flat_slices": flat_d_net,
            "n_flat_slices": n_flat,
            "total_contra_events": n_ev,
            "total_contra_gross": c_gross,
            "contra_gross_per_event": (c_gross / n_ev) if n_ev else 0.0,
        }
        print(f"  spread=${spread:<6g} events={n_ev:>4}  "
              f"dGross={d_gross:>+10,.2f}  dNet={d_net:>+10,.2f}  "
              f"dNet(flat)={flat_d_net:>+9,.2f}  "
              f"cGross/ev={summary[f'{spread:g}']['contra_gross_per_event']:>8,.2f}")

    out = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": str(DATA),
        "slices": [list(x) for x in SLICES],
        "spreads": spreads,
        "rows": rows,
        "summary": summary,
    }
    out_path.write_text(json.dumps(out, indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
