"""VOLATILITY-AT-ENTRY verification — is the market moving enough
to reach the target? (2026-10-24)

The user: "can we have a verification that upon opening, the
volatility is enough to hit our tps and we aren't opening at random?"

``runtime/diagnostics/tp_reachability.py`` answered this AFTER the
fact: it measured whether the market's forward range covered the
target distance, and found that trades whose target was BARELY
reachable did much better than trades whose target was unreachable.

That is a label, not a filter. This diagnostic turns it into
something LIVE-USABLE: a gate computed ONLY from information
available at the entry bar.

The candidate measure
---------------------
For each trade, compute at the ENTRY bar:

    expected_move(H) = typical range the market covers over H seconds

using a BACKWARD-looking measure of recent volatility, then compare
it to the target distance ``D``:

    cover(H) = expected_move(H) / D

If ``cover(H) < 1.0``, the target is further away than the market
normally travels in H seconds -- the trade is asking for something
unusual.

Two backward measures are tested:

* ``atr_H``  -- the mean of the last H 1s bars' (high - low).
  A simple realised-range measure.
* ``vel_5m`` -- the vol_state velocity measure (already computed
  and cached on the artifact bundle), as a ratio.

Why this is the RIGHT shape of filter
-------------------------------------
The reachability result said: unreachable trades have EVgross
-0.0022 and barely-reachable have EVgross +0.0116 (at the 1800s
horizon). The DIRECTION is right. The question is whether a
backward-looking proxy for "is this target reachable" captures
enough of that separation to be worth gating on.

THE HONEST TEST
---------------
The gate is only worth having if it survives the fee bill. At the
canonical $0.47 round trip, a filter that improves gross by
$0.0008/leg is worthless. So the report states, for every
threshold: the gross EV, the net EV, the legs retained, and
whether the FLAT slice (the decisive test) improves.

A gate that only helps on BULL slices is the beta proxy again.

Usage
-----
    python -m runtime.diagnostics.vol_entry_gate
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
OUT = Path("runtime/diagnostics/_reports/vol_entry_gate.json")

# (tag, start, end, regime)
SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
]

# Backward window (seconds) for the expected-move estimate.
# 900 = 15m, 1800 = 30m, 3600 = 1h.
WINDOWS = [900, 1800, 3600]

# cover thresholds to test (keep trades with cover >= threshold).
THRESHOLDS = [0.0, 0.5, 0.75, 1.0, 1.25, 1.5, 2.0]


def _atr_series(high: np.ndarray, low: np.ndarray, window: int) -> np.ndarray:
    """Backward-looking mean of (high - low) over ``window`` bars.

    At bar ``i`` this uses bars ``[i-window, i]`` -- the RANGE THAT
    HAS ALREADY HAPPENED. No forward data.
    """
    tr = high - low
    n = len(tr)
    out = np.zeros(n, dtype=np.float64)
    if n == 0:
        return out
    c = np.concatenate([[0.0], np.cumsum(tr)])
    idx = np.arange(n)
    lo = np.maximum(0, idx - window + 1)
    cnt = (idx - lo + 1).astype(np.float64)
    out = (c[idx + 1] - c[lo]) / cnt
    return out


def _expected_move(high: np.ndarray, low: np.ndarray, window: int) -> np.ndarray:
    """Backward-looking EXPECTED RANGE over the next ``window`` seconds.

    The naive ATR over the last H seconds UNDERSTATES the forward
    range badly, because it averages 1-second bars. The market's
    1-second bar range is ~$0.10; a 15-minute move covers $20-165.
    So "ATR(900) / D" is ~0 for every trade and the gate is
    degenerate (it kept 0.2% of trades).

    The right measure is the EXPECTED RANGE over the horizon:

        expected_range(H) = ATR(H) * sqrt(H)

    where ATR(H) is the mean 1-second range and the sqrt(H) factor is
    the Brownian scaling of a random walk (a sum of H independent
    1-second ranges grows like sqrt(H), not H).

    This is computed BACKWARD: at bar ``i`` it uses ATR over
    ``[i-window, i]`` and projects it forward. Only past data enters.
    """
    atr = _atr_series(high, low, window)
    return atr * float(np.sqrt(window))


def main():
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # rows[window][threshold] = list of per-trade dicts
    per_trade: dict[int, list] = {w: [] for w in WINDOWS}
    slice_regime: list[tuple[str, str]] = []

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
        tr = list(res.trades)
        high = bars["high"].to_numpy(dtype=np.float64)
        low = bars["low"].to_numpy(dtype=np.float64)
        print(f"    legs={len(tr)}  wall={time.perf_counter()-t0:.1f}s", flush=True)

        atr = {w: _expected_move(high, low, w) for w in WINDOWS}

        for t in tr:
            d = abs(float(t.initial_tp_distance_usd))
            eb = int(t.entry_bar)
            if d <= 0.0 or eb < 0 or eb >= len(high):
                continue
            row = {
                "tag": tag, "regime": regime,
                "gross": float(t.pnl_gross_usd),
                "net": float(t.pnl_net_usd),
                "d": d,
                "dir": int(t.direction),
            }
            for w in WINDOWS:
                row[f"cover_{w}"] = float(atr[w][eb]) / d
                # BUG FIX (2026-10-24): this used to be
                # ``per_trade[w].append(row)`` AFTER the loop, so
                # every row landed in the LAST window's list and the
                # 900s / 1800s windows reported 0 kept trades. The
                # row must be appended once PER window, because the
                # cover value differs per window.
                per_trade[w].append(dict(row))
            slice_regime.append((tag, regime))

        del bars, art, res, tr, high, low
        import gc
        gc.collect()

    # ── Report ────────────────────────────────────────────────────
    report = {"windows": WINDOWS, "thresholds": THRESHOLDS, "results": {}}
    print()
    print("=" * 104)
    print("VOLATILITY-AT-ENTRY GATE — 'expected move over H' vs 'target distance D'")
    print("  cover = expected_move(H) / D.  Keep trades with cover >= threshold.")
    print("  expected_move(H) = ATR(H) * sqrt(H)  (Brownian scaling, backward-looking)")
    print("=" * 104)

    for w in WINDOWS:
        rows = per_trade[w]
        print(f"\n--- window {w}s ---")
        print(f"{'thresh':>7} {'kept':>7} {'kept%':>7} {'gross':>9} {'EVgross':>9} "
              f"{'net':>9} {'EVnet':>8} | {'FLAT gross':>11} {'FLAT net':>9}")
        wres = {}
        for th in THRESHOLDS:
            sel = [r for r in rows if r[f"cover_{w}"] >= th]
            flat = [r for r in sel if r["regime"] == "flat"]
            g = sum(r["gross"] for r in sel)
            n = sum(r["net"] for r in sel)
            fg = sum(r["gross"] for r in flat)
            fn = sum(r["net"] for r in flat)
            L = len(sel)
            wres[th] = {
                "kept": L, "kept_pct": 100.0 * L / max(len(rows), 1),
                "gross": g, "net": n,
                "ev_gross": g / max(L, 1), "ev_net": n / max(L, 1),
                "flat_gross": fg, "flat_net": fn,
                "flat_legs": len(flat),
            }
            print(f"{th:>7.2f} {L:>7} {100.0*L/max(len(rows),1):>6.1f}% "
                  f"{g:>9,.1f} {g/max(L,1):>9.4f} {n:>9,.1f} {n/max(L,1):>8.4f} | "
                  f"{fg:>11,.1f} {fn:>9,.1f}")
        report["results"][w] = wres

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print(f"\n-> {OUT}")


if __name__ == "__main__":
    main()
