"""Where does the 1y memory actually go?

Answers: "why does memory grow so much if backtests are sequential and
zones expire?" by sizing each concrete allocation.

Run:
  python runtime/sweeps/diag_memory_breakdown.py
"""
from __future__ import annotations

import gc
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")


def deep_size(obj, seen=None) -> int:
    """Recursive deep size in bytes (numpy arrays, lists, dicts, objects)."""
    if seen is None:
        seen = set()
    oid = id(obj)
    if oid in seen:
        return 0
    seen.add(oid)
    if isinstance(obj, np.ndarray):
        return int(obj.nbytes)
    size = sys.getsizeof(obj, 0)
    if isinstance(obj, (str, bytes, bytearray)):
        return size
    if isinstance(obj, dict):
        for k, v in obj.items():
            size += deep_size(k, seen) + deep_size(v, seen)
        return size
    if isinstance(obj, (list, tuple, set, frozenset)):
        for v in obj:
            size += deep_size(v, seen)
        return size
    d = getattr(obj, "__dict__", None)
    if isinstance(d, dict):
        for k, v in d.items():
            size += deep_size(v, seen)
    else:
        for cls in type(obj).__mro__:
            for name in (getattr(cls, "__slots__", ()) or ()):
                if hasattr(obj, name):
                    size += deep_size(getattr(obj, name), seen)
    return size


def mb(n: float) -> str:
    return f"{n / 1024 / 1024:>10,.1f} MB"


def main() -> None:
    print("=" * 74)
    print("STEP 1 — the parquet's own dtypes (does to_numpy(dtype=float64) COPY?)")
    print("=" * 74)
    schema = pd.read_parquet(DATA).dtypes
    for col, dt in schema.items():
        print(f"  {col:<16} {dt}")
    print()

    from runtime.sweeps.cached_sweep import load_bars, SWING_KWARGS
    from bt._engine.timebase import to_ns_arr

    bars = load_bars(DATA)
    n = len(bars)
    print(f"bars: {n:,} rows")
    print()
    print("  bars frame (per column, deep):")
    total_frame = 0
    for col in bars.columns:
        s = bars[col]
        nb = int(s.memory_usage(index=False, deep=True))
        total_frame += nb
        print(f"    {col:<16} {mb(nb)}  dtype={s.dtype}")
    print(f"    {'TOTAL':<16} {mb(total_frame)}")
    print()

    print("  what the ENGINE allocates from those columns:")
    eng = 0
    for col in ("open", "high", "low", "close"):
        arr = bars[col].to_numpy(dtype=np.float64)
        copied = arr.base is None
        nb = arr.nbytes
        eng += nb
        print(f"    {col:<16} {mb(nb)}  copied={copied}")
    times = to_ns_arr(bars, "time")
    eng += times.nbytes
    print(f"    {'time(ns)':<16} {mb(times.nbytes)}  copied={times.base is None}")
    print(f"    {'SUBTOTAL':<16} {mb(eng)}")
    print()

    print("=" * 74)
    print("STEP 2 — the zone list (1.46M dataclass objects)")
    print("=" * 74)
    from bt._engine.ict_signals import detect_fvg
    zones = detect_fvg(
        bars["open"].to_numpy(), bars["high"].to_numpy(),
        bars["low"].to_numpy(), bars["close"].to_numpy(),
        fvg_min_zone_usd=0.30, max_zone_age_bars=3600,
        fvg_body_definition="body", fvg_displacement_ratio=0.0,
    )
    one = deep_size(zones[0])
    print(f"  zones                    {len(zones):,}")
    print(f"  deep size of ONE zone    {one:,} bytes")
    print(f"  x {len(zones):,} zones          {mb(one * len(zones))}")
    print()
    print("  -> the zone list is STATIC. It does not grow during the run.")
    print("     Zones are built once, before the bar loop, and never freed.")
    print()

    print("=" * 74)
    print("STEP 3 — per-trade objects (grow DURING the run)")
    print("=" * 74)
    from bt._engine.vp_multi_target_engine import OpenTrade, ClosedTrade
    ot = OpenTrade(
        trade_id=1, parent_zone_id=1, entry_bar=0, entry_time_ns=0,
        entry_price=3000.0, direction=1, stop_price=2999.0,
        target_price=3001.0, fvg_direction=1, source="immediate",
        sl_mode="fvg_only", tp_mode="multi_target",
        sl_struct_ref="zone_edge", tp_struct_ref="hvn",
    )
    ct = ClosedTrade(
        trade_id=1, parent_zone_id=1, entry_bar=0, exit_bar=10,
        entry_time_ns=0, exit_time_ns=0, entry_price=3000.0,
        exit_price=3001.0, direction=1, stop_price=2999.0,
        target_price=3001.0, exit_reason="tp", hold_secs=1.0,
        pnl_gross_usd=1.0, fee_usd=0.5, pnl_net_usd=0.5, qty_btc=1.0,
        fvg_direction=1, source="immediate", sl_mode="fvg_only",
        tp_mode="multi_target", sl_struct_ref="zone_edge",
        tp_struct_ref="hvn",
    )
    print(f"  deep size of ONE OpenTrade   {deep_size(ot):,} bytes")
    print(f"  deep size of ONE ClosedTrade  {deep_size(ct):,} bytes")
    print(f"  80,833 closed trades           {mb(deep_size(ct) * 80833)}")
    print(f"  1,320 open trades (peak)       {mb(deep_size(ot) * 1320)}")
    print()
    print("  -> the CLOSED-TRADE LIST is the only structure that grows")
    print("     without bound during a run. It is never freed.")
    print()

    print("=" * 74)
    print("STEP 4 — the equity curve (a single big array)")
    print("=" * 74)
    print(f"  equity_curve float64 on 1y    {mb(n * 8)}")
    print("  (disabled above 8M bars in the sweep driver)")
    print()

    print("=" * 74)
    print("SUMMARY — the 6.1 GB working set")
    print("=" * 74)
    rows = [
        ("bars frame (parquet columns)", total_frame),
        ("engine float64 copies (o/h/l/c + time)", eng),
        ("zone list (1.46M FvgZone objects)", one * len(zones)),
        ("closed trades (80,833 ClosedTrade)", deep_size(ct) * 80833),
        ("equity curve (float64, 41M)", n * 8),
    ]
    grand = 0
    for label, nb in rows:
        grand += nb
        print(f"  {label:<44} {mb(nb)}")
    print(f"  {'-' * 44} {'-' * 11}")
    print(f"  {'TOTAL (accounted)':<44} {mb(grand)}")
    print()
    print("  NOTE: the artifacts PICKLE (171 MB on disk) is ALSO unpickled")
    print("        into RAM, duplicating zones + vp_set. Pickling a list of")
    print("        1.46M dataclass instances writes them field-by-field,")
    print("        so the on-disk size UNDERSTATES the in-memory size.")


if __name__ == "__main__":
    main()
