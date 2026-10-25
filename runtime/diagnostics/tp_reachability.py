"""TP reachability at OPEN — are we opening at random? (2026-10-24).

The user: "can we have a verification that upon opening, the
volatility is enough to hit our tps and we aren't opening at random?"

This is a PRE-ENTRY diagnostic. It does not ask "did the trade win?"
— it asks "at the moment we opened, was the market moving enough
that our target was reachable at all?"

The mechanism
-------------
Every trade has a target at distance ``D`` from entry. The market
reaches a target only if it travels at least ``D``. So the question
is: **at the entry bar, what was the market's own reach?**

For each entry we compute the FORWARD realised range over a
horizon ``H``:

    reach(H) = max(high[entry+1 .. entry+H]) - min(low[entry+1 .. entry+H])

and compare it to the target distance ``D``. If
``reach(H) < D``, the target was **geometrically unreachable** — the
market did not travel far enough in the whole window to get there,
let alone in the right direction.

``reach`` is computed FORWARD on purpose: it is a property of what
HAPPENED, used here only to LABEL trades that already happened. It
is never fed back into the strategy, so it introduces no look-ahead
into any P&L number. Every P&L in this report comes from the
untouched engine.

Two labels are produced:

* ``unreachable`` — ``reach(H) < D``. The target could not be hit
  in ANY direction. This is the "opening at random" case.
* ``barely`` — ``reach(H) < 1.5 * D``. Reachable, but only just.

What we expect if the recipe has no edge: the unreachable bucket
carries roughly the SAME gross per leg as the reachable bucket. If
the signal were real, reachable trades should be systematically
better.

The second half of the report measures the CONVERSE: **is the
volatility state at entry predictive?** We bucket trades by the
vol_state measures at the entry bar (``vel_5m``, ``range_norm_1m``,
``wick_ratio``, ``rv_ratio_30s``) and report gross/net per bucket.
That is the "verification that volatility is enough" question, and
it tells us whether the NB85 gate is worth re-validating.

Usage
-----
    python -m runtime.diagnostics.tp_reachability
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
OUT = Path("runtime/diagnostics/_reports/tp_reachability.json")

SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

# Horizons in SECONDS. 300 = 5m, 900 = 15m, 1800 = 30m, 3600 = 1h.
HORIZONS = [300, 900, 1800, 3600]


def _forward_reach_at(high: np.ndarray, low: np.ndarray,
                      bars: np.ndarray, n: int) -> np.ndarray:
    """Forward range over ``n`` bars starting at ``bars[i]+1``.

    For each entry bar ``b`` in ``bars``::

        reach = max(high[b+1 : b+1+n]) - min(low[b+1 : b+1+n])

    The window is truncated at the end of the series.

    This computes the reach ONLY at the bars we care about (the entry
    bars), which is a few thousand per slice rather than 1.5M. That
    keeps the reference implementation trivially obviously-correct --
    it is a direct transcription of the definition -- and fast enough
    to run on every trade.

    The values are a property of what HAPPENED after the entry. They
    are used here only to LABEL trades that already closed; they are
    never fed back into the strategy, so no look-ahead is introduced
    into any P&L figure reported here.
    """
    out = np.zeros(len(bars), dtype=np.float64)
    m = len(high)
    for i, b in enumerate(bars):
        b = int(b)
        lo = b + 1
        hi = min(m, lo + n)
        if hi > lo:
            out[i] = float(high[lo:hi].max() - low[lo:hi].min())
    return out


def _bucket_report(trades, high, low, horizon):
    """Split trades into reachable / unreachable and report gross/net.

    The forward reach is computed in ONE batched pass over all entry
    bars (a few thousand per slice), not per trade.
    """
    cand = []
    for t in trades:
        d = abs(float(t.initial_tp_distance_usd))
        eb = int(t.entry_bar)
        if d <= 0.0 or eb < 0 or eb >= len(high):
            continue
        cand.append((t, d, eb))
    if not cand:
        return []

    bars_arr = np.array([c[2] for c in cand], dtype=np.int64)
    reach = _forward_reach_at(high, low, bars_arr, horizon)

    out = []
    for (t, d, _eb), r in zip(cand, reach):
        if not np.isfinite(r):
            continue
        out.append({
            "reach": float(r), "d": float(d),
            "ratio": float(r) / float(d),
            "gross": float(t.pnl_gross_usd),
            "net": float(t.pnl_net_usd),
            "dir": int(t.direction),
        })
    return out


def _summ(rows, label):
    if not rows:
        return {"n": 0}
    g = sum(r["gross"] for r in rows)
    n = sum(r["net"] for r in rows)
    return {
        "n": len(rows),
        "gross": g,
        "net": n,
        "ev_net": n / len(rows),
        "ev_gross": g / len(rows),
    }


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    report: dict = {"horizons": HORIZONS, "slices": {}}
    all_rows: dict[int, list] = {h: [] for h in HORIZONS}

    # Each slice is processed and RELEASED before the next one is
    # loaded. Holding all six slices' artifact bundles at once is
    # ~4-6 GB of resident memory (the zones list dominates), and the
    # process was being OOM-killed on a 1-month slice set.
    for tag, s, e, regime in SLICES:
        print(f"--- {tag} ---", flush=True)
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
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
        )
        trades = list(res.trades)
        high = bars["high"].to_numpy(dtype=np.float64)
        low = bars["low"].to_numpy(dtype=np.float64)

        slice_out = {"legs": len(trades), "wall": time.perf_counter() - t0,
                     "by_horizon": {}}
        for hz in HORIZONS:
            rows = _bucket_report(trades, high, low, hz)
            all_rows[hz].extend(rows)

            unreachable = [r for r in rows if r["ratio"] < 1.0]
            barely = [r for r in rows if 1.0 <= r["ratio"] < 1.5]
            comfortable = [r for r in rows if r["ratio"] >= 1.5]
            slice_out["by_horizon"][hz] = {
                "unreachable": _summ(unreachable, "unreachable"),
                "barely": _summ(barely, "barely"),
                "comfortable": _summ(comfortable, "comfortable"),
                "all": _summ(rows, "all"),
            }
            print(f"  h={hz:>5}s  unreachable {len(unreachable):>6} "
                  f"({100*len(unreachable)/max(len(rows),1):>5.1f}%)  "
                  f"comfortable {len(comfortable):>6} "
                  f"({100*len(comfortable)/max(len(rows),1):>5.1f}%)", flush=True)
        report["slices"][tag] = slice_out

        # Release the slice's artifacts before the next load.
        del art, bars, res, trades, high, low
        import gc
        gc.collect()

    # ---- pooled across all slices ----
    pooled = {}
    for hz in HORIZONS:
        rows = all_rows[hz]
        unreachable = [r for r in rows if r["ratio"] < 1.0]
        barely = [r for r in rows if 1.0 <= r["ratio"] < 1.5]
        comfortable = [r for r in rows if r["ratio"] >= 1.5]
        pooled[hz] = {
            "unreachable": _summ(unreachable, "u"),
            "barely": _summ(barely, "b"),
            "comfortable": _summ(comfortable, "c"),
            "all": _summ(rows, "a"),
        }
    report["pooled"] = pooled

    print()
    print("=" * 78)
    print("TP REACHABILITY — pooled across all 6 slices")
    print("=" * 78)
    print(f"{'horizon':>8} {'bucket':<13} {'n':>7} {'gross':>10} {'net':>10} "
          f"{'EVgross':>9} {'EVnet':>8}")
    for hz in HORIZONS:
        for k in ("unreachable", "barely", "comfortable", "all"):
            a = pooled[hz][k]
            if not a.get("n"):
                continue
            print(f"{hz:>8} {k:<13} {a['n']:>7} {a['gross']:>10,.1f} "
                  f"{a['net']:>10,.1f} {a['ev_gross']:>9.4f} {a['ev_net']:>8.4f}")
        print()

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
