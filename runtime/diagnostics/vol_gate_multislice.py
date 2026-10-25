"""VOL-STATE ``range_norm_1m`` GATE — multi-slice validation (2026-10-24).

WHY THIS EXISTS
---------------
A single-slice probe (``2026-01``) showed the ``vol_state_min_range_norm``
gate flipping the canonical run from NEGATIVE net to POSITIVE net:

    thr=0.0 (off)  legs=4136  gross=$-5.0  net=$-24.8
    thr=3.0         legs= 112  gross=$ 1.8  net=$ 1.3

That is EXACTLY the shape of a beta proxy, and this repo's own rules
(AGENTS.md, "READ THIS FIRST") forbid promoting a single-slice winner.
A gate that only helps on a BULL slice is a directional filter wearing
a volatility filter's clothes. So this diagnostic re-measures the gate
on six slices -- three fit, three held out -- and reports the FLAT
slice separately, because the flat slice is the decisive test:
a strategy with genuine timing skill makes money in a choppy market.

SCALE
-----
This runs at CANONICAL scale: ``lots=0.01``, ``contract_size=100``
(1 oz). P&L is exactly linear in qty, so the diagnostic scale
(``contract_size=1``) used by the other sweeps gives the same SIGNS
and the same ratios -- but the absolute dollar figures here are the
real ones, including the real $0.47 round trip.

    python -m runtime.diagnostics.vol_gate_multislice
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
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/vol_gate_multislice.json")

# (tag, start, end, regime)
FIT = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
]
HELD = [
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

# ``vol_state_min_range_norm`` thresholds. 0.0 = gate OFF (canonical).
# The measure is the 1m range normalised by its trailing median, so
# 1.0 == "this minute is normal for this instrument", 2.0 == "twice
# normal", 3.0 == "three times normal".
THRESHOLDS = [0.0, 1.0, 1.5, 2.0, 2.5, 3.0]


def _prep(tag: str, s: str, e: str):
    bars = load_bars(DATA, s, e)
    art = ArtifactCache(DATA, f"c1m_{s[:7]}", s, e).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    return bars, art, spot


def _run(bars, art, spot, thr: float) -> dict:
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        vol_state_min_range_norm=thr,
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    tr = list(res.trades)
    gross = sum(t.pnl_gross_usd for t in tr)
    net = sum(t.pnl_net_usd for t in tr)
    fees = sum(t.fee_usd for t in tr)
    wins = [t for t in tr if t.pnl_gross_usd > 0]
    losses = [t for t in tr if t.pnl_gross_usd <= 0]
    aw = float(np.mean([t.pnl_gross_usd for t in wins])) if wins else 0.0
    al = abs(float(np.mean([t.pnl_gross_usd for t in losses]))) if losses else 0.0
    payoff = aw / al if al > 0 else float("nan")
    return {
        "legs": len(tr),
        "gross": gross,
        "net": net,
        "fees": fees,
        "dropped_vol": int(res.n_dropped_vol_state),
        "win_rate": 100.0 * len(wins) / max(len(tr), 1),
        "payoff": payoff,
        "breakeven_win_pct": (100.0 / (1.0 + payoff)) if payoff == payoff else float("nan"),
        "wall": time.perf_counter() - t0,
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)

    # Per-slice results: rows[thr][slice_tag]. Only the small summary
    # dicts are retained -- the bars and the artifact bundle are freed
    # after every slice. Holding all six months at once is ~1.5-2.5 GB
    # of resident arrays, and AGENTS.md warns that a retained working
    # set is what turns a run into a paging death spiral.
    rows: dict[float, dict[str, dict]] = {thr: {} for thr in THRESHOLDS}
    for tag, s, e, regime in FIT + HELD:
        print(f"--- {tag} ---", flush=True)
        bars, art, spot = _prep(tag, s, e)
        for thr in THRESHOLDS:
            r = _run(bars, art, spot, thr)
            r["regime"] = regime
            rows[thr][tag] = r
            print(
                f"  {tag:<13} thr={thr:<4} legs={r['legs']:>6} "
                f"dropped={r['dropped_vol']:>6} gross=${r['gross']:>10,.2f} "
                f"net=${r['net']:>10,.2f} hit={r['win_rate']:>5.1f}%",
                flush=True,
            )
        del bars, art
        import gc
        gc.collect()

    # ── Report ────────────────────────────────────────────────────
    print()
    print("=" * 108)
    print("VOL GATE (range_norm_1m) — CANONICAL SCALE (0.01 lots x 100 cs = 1 oz, $0.47 round trip)")
    print("=" * 108)
    header = (f"{'thr':>5} {'legs':>7} {'gross':>11} {'fees':>10} {'net':>11} "
              f"{'EVnet':>8} | {'FIT net':>10} {'HELD net':>10} "
              f"{'FLAT net':>10} {'slices+':>7}")
    print(header)
    summary = {}
    for thr in THRESHOLDS:
        per = rows[thr]
        fit = [v for k, v in per.items() if k in {t for t, _, _, _ in FIT}]
        held = [v for k, v in per.items() if k in {t for t, _, _, _ in HELD}]
        flat = [v for v in per.values() if v["regime"] == "flat"]
        legs = sum(v["legs"] for v in per.values())
        gross = sum(v["gross"] for v in per.values())
        fees = sum(v["fees"] for v in per.values())
        net = sum(v["net"] for v in per.values())
        pos = sum(1 for v in per.values() if v["net"] > 0)
        summary[thr] = {
            "legs": legs, "gross": gross, "fees": fees, "net": net,
            "ev_net": net / max(legs, 1),
            "fit_net": sum(v["net"] for v in fit),
            "held_net": sum(v["net"] for v in held),
            "flat_net": sum(v["net"] for v in flat),
            "slices_positive_net": pos,
            "slices": len(per),
            "per_slice": per,
        }
        print(f"{thr:>5.1f} {legs:>7} {gross:>11,.2f} {fees:>10,.2f} {net:>11,.2f} "
              f"{net/max(legs,1):>8.4f} | {summary[thr]['fit_net']:>10,.2f} "
              f"{summary[thr]['held_net']:>10,.2f} {summary[thr]['flat_net']:>10,.2f} "
              f"{pos:>4}/{len(per)}")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
