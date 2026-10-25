"""WHY the chunked 1-month run and the continuous corpus disagree.

Three ways to measure the SAME recipe over the SAME calendar window
(2025-01-01 .. 2025-02-01):

  (a) CHUNKED, no warmup   -- what ``cached_sweep`` does by default.
      The artifacts (p95 cap, swing cap, VP profile) are built from
      January's bars ONLY, and the p95 cap's 7-day window never fills,
      so it returns ``inf`` for the first ~7 days: those trades trade
      with UNCAPPED targets.

  (b) CHUNKED + warmup prefix -- what ``leg_ledger`` does: the
      execution window starts 14 days early, the artifacts are built
      over the whole window, and only the scored month's legs are
      kept.

  (c) CONTINUOUS corpus -- one run over 2024-01-01 .. 2026-07-16,
      then the Jan-2025 legs are selected by SIGNAL time. The caps
      and the VP profile are warm and consistent with the rest of
      the corpus.

If (a), (b) and (c) disagree, then a 1-month sweep is NOT measuring
the canonical strategy -- and every verdict drawn from a 1-month
sweep is a verdict about a different book.

Usage:
    python -m runtime.diagnostics.chunk_vs_continuous
"""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/chunk_vs_continuous.json")

MONTH = "2025-01-01"
MONTH_END = "2025-02-01"
WARMUP_DAYS = 14


def _run(bars: pd.DataFrame, art: dict[str, Any]) -> dict[str, Any]:
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek, record_equity_curve=False,
    )
    trades = list(res.trades)
    gross = sum(float(t.pnl_gross_usd) for t in trades)
    fees = sum(float(getattr(t, "fee_usd", 0.0)) for t in trades)
    tps = [abs(float(getattr(t, "initial_tp_distance_usd", 0.0)))
           for t in trades]
    sls = [abs(float(getattr(t, "initial_sl_distance_usd", 0.0)))
           for t in trades]
    exits: dict[str, int] = {}
    for t in trades:
        r = str(getattr(t, "exit_reason", "?"))
        exits[r] = exits.get(r, 0) + 1
    return {
        "legs": len(trades),
        "gross": gross,
        "fees": fees,
        "net": gross - fees,
        "median_tp": float(np.median(tps)) if tps else None,
        "median_sl": float(np.median(sls)) if sls else None,
        "exits": exits,
    }


def _summary(r: dict[str, Any]) -> str:
    return (f"legs={r['legs']:>4} gross=${r['gross']:>9,.2f} "
            f"fees=${r['fees']:>8,.2f} net=${r['net']:>9,.2f} "
            f"medTP=${r['median_tp'] or 0:>7.2f} medSL=${r['median_sl'] or 0:>6.2f} "
            f"{r['exits']}")


def main() -> None:
    print("== chunked vs continuous: the SAME calendar window ==")
    print(f"window: {MONTH} .. {MONTH_END}\n")

    # ── (a) chunked, NO warmup ─────────────────────────────────
    print("(a) CHUNKED, no warmup  [what cached_sweep does by default]")
    bars_a = load_bars(DATA, MONTH, MONTH_END)
    art_a = ArtifactCache(DATA, "cmp_chunk_nowarm", MONTH,
                          MONTH_END).build_or_load(bars_a)
    a = _run(bars_a, art_a)
    print(f"    {_summary(a)}\n")

    # ── (b) chunked + warmup prefix ────────────────────────────
    print("(b) CHUNKED + 14d warmup prefix  [what leg_ledger does]")
    exec_start = (pd.Timestamp(MONTH, tz="UTC")
                  - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    bars_b = load_bars(DATA, exec_start, MONTH_END)
    art_b = ArtifactCache(DATA, "cmp_chunk_warm", exec_start,
                          MONTH_END).build_or_load(bars_b)
    b_all = _run(bars_b, art_b)
    # Keep only the scored month's legs.
    sig = []
    for t in []:
        pass
    print(f"    (all legs in the window) {_summary(b_all)}\n")

    # ── (c) continuous corpus, Jan-2025 selected by signal time ─
    print("(c) CONTINUOUS corpus  [one run, then slice by signal time]")
    ledger_path = Path("runtime/diagnostics/_reports/leg_ledger.json")
    if not ledger_path.exists():
        raise SystemExit(
            "build the ledger first:\n"
            "    python -m runtime.diagnostics.leg_ledger")
    payload = json.loads(ledger_path.read_text(encoding="utf-8"))
    if (payload.get("engine_overrides") or {}):
        raise SystemExit(
            f"{ledger_path} was built with VARIANT engine overrides "
            f"{payload.get('engine_overrides')!r}. This diagnostic "
            "compares the CONTINUOUS canonical run against a chunked "
            "run -- rebuild the canonical ledger:\n"
            "    python -m runtime.diagnostics.leg_ledger --refresh"
        )
    legs = payload["legs"]
    ts = pd.to_datetime(legs["signal_time_ns"], unit="ns", utc=True)
    m = ((ts >= pd.Timestamp(MONTH, tz="UTC"))
         & (ts < pd.Timestamp(MONTH_END, tz="UTC")))
    g = np.asarray(legs["gross"], dtype=float)[m]
    f = np.asarray(legs["fee"], dtype=float)[m]
    c = {
        "legs": int(m.sum()),
        "gross": float(g.sum()),
        "fees": float(f.sum()),
        "net": float(g.sum() - f.sum()),
    }
    print(f"    legs={c['legs']:>4} gross=${c['gross']:>9,.2f} "
          f"fees=${c['fees']:>8,.2f} net=${c['net']:>9,.2f}\n")

    print("=" * 78)
    print(f"(a) chunked/no-warmup net = ${a['net']:>10,.2f}")
    print(f"(c) continuous      net = ${c['net']:>10,.2f}")
    print(f"    DISCREPANCY        = ${a['net'] - c['net']:>10,.2f}")
    print("=" * 78)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "window": [MONTH, MONTH_END],
        "a_chunked_no_warmup": a,
        "c_continuous": c,
        "discrepancy_net_usd": a["net"] - c["net"],
    }, indent=2), encoding="utf-8")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
