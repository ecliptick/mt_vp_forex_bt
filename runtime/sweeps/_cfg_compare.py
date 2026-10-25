"""Ad-hoc config comparison on the 1-month slice.

Reuses the cached artifacts so each config costs only the engine cell.
Not a sweep driver — a scratch tool for picking the canonical config.
"""
from __future__ import annotations

import pickle
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402

SCOPE = "XAUUSD_S1_1y__20250101_20250201"
CACHE = Path("notebooks/.cache") / SCOPE
DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")


def load():
    art = pickle.load(open(
        CACHE / "artifacts_c6bcf7ba898b5527.pkl", "rb"))
    df = pd.read_parquet(DATA)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df[
        (df["time"] >= pd.Timestamp("2025-01-01", tz="UTC"))
        & (df["time"] < pd.Timestamp("2025-02-01", tz="UTC"))
    ].sort_values("time").reset_index(drop=True)
    if "volume" not in df.columns:
        df["volume"] = df["tickv"].astype(float)
    return df, art


def run(df, art, label, **over):
    spot = float(np.median(df["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"], **over)
    t0 = time.perf_counter()
    res, diag = run_vp_sniper_backtest_forex(
        bars=df, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek, record_equity_curve=True)
    wall = time.perf_counter() - t0
    tr = res.trades
    n = len(tr)
    net = sum(t.pnl_net_usd for t in tr)
    longs = sum(1 for t in tr if t.direction > 0)
    shorts = n - longs
    wins = sum(1 for t in tr if t.pnl_net_usd > 0)
    fees = sum(t.fee_usd for t in tr)
    med_sl = np.median([abs(t.entry_price - t.stop_price) for t in tr]) if tr else 0
    med_tp = np.median([abs(t.target_price - t.entry_price) for t in tr]) if tr else 0
    print(
        f"{label:52s} n={n:>5} net=${net:>10.0f} EV=${(net/n if n else 0):>7.2f} "
        f"L/S={longs}/{shorts} hit={wins/n if n else 0:.3f} "
        f"medSL=${med_sl:>6.2f} medTP=${med_tp:>6.2f} "
        f"fees=${fees:>8.0f} {wall:>6.1f}s")
    return res, diag, net


if __name__ == "__main__":
    df, art = load()
    print(f"bars={len(df):,}")
    # Policy comparison at the canonical sl_zone_mult=40, cap=20.
    for pol in ("drop", "stack", "replace"):
        run(df, art, f"sl=40 cap=20 policy={pol}",
            conflict_policy=pol, max_same_dir_open=20)
    # sl_zone_mult sweep under stack.
    for sl in (4, 8, 12, 20, 40):
        run(df, art, f"sl={sl} cap=20 policy=stack",
            conflict_policy="stack", max_same_dir_open=20,
            sl_zone_mult=float(sl))
