"""PROBE — how do the ledger's entry_bar / exit_bar index into the file?

The ledger's drift is computed on the **1s grid**:

    drift_usd = (close[exit_bar] - close[entry_bar]) * side * qty

so the drift benchmark can only be reproduced if ``entry_bar`` and
``exit_bar`` are indices into the SAME bar array the ledger used.

This probe answers three questions:

1. Is the engine's bar array the file's rows, one-for-one?
2. Does ``close[entry_bar]`` sit near ``entry_price``?
3. What is the bar-to-second ratio (is the file really 1s dense)?

If (2) fails, the ledger's bar indices are NOT file row indices and
the drift must be recomputed from timestamps instead.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pyarrow.parquet as pq  # noqa: E402

from runtime.diagnostics.leg_ledger import LEDGER_PATH  # noqa: E402
from runtime.diagnostics.ta_gate import load_legs  # noqa: E402
from runtime.sweeps.cached_sweep import DEFAULT_DATA  # noqa: E402


def main() -> None:
    legs = load_legs(LEDGER_PATH)
    legs.pop("_meta")
    eb = legs["entry_bar"].astype(np.int64)
    xb = legs["exit_bar"].astype(np.int64)
    ep = legs["entry_price"].astype(np.float64)
    xp = legs["exit_price"].astype(np.float64)
    et = legs["entry_time_ns"].astype(np.int64)
    xt = legs["exit_time_ns"].astype(np.int64)

    pf = pq.ParquetFile(DEFAULT_DATA)
    print(f"file rows: {pf.metadata.num_rows:,}  "
          f"row groups: {pf.metadata.num_row_groups}")

    # Read the first row group's time+close.
    tbl = pf.read_row_group(0, columns=["time", "close"])
    t_ms = tbl.column("time").to_numpy(zero_copy_only=False).astype(np.int64)
    c = tbl.column("close").to_numpy(zero_copy_only=False).astype(np.float64)
    print(f"\nrow group 0: {t_ms.size:,} rows")
    print(f"  first 5 times (ms): {t_ms[:5]}")
    print(f"  time deltas (ms), first 20: {np.diff(t_ms[:21])}")
    print(f"  unique deltas: {np.unique(np.diff(t_ms[:20000]))[:10]}")

    # Is the bar index a file ROW index?
    print("\n[1] IS entry_bar A FILE ROW INDEX?")
    idx = eb[:20]
    print(f"  ledger entry_bar[:20] = {idx}")
    print(f"  row group 0 covers rows 0..{t_ms.size - 1:,}")
    ok = idx.max() < t_ms.size
    print(f"  all of the first 20 entry_bars inside row group 0: {ok}")

    if ok:
        print(f"  file close[entry_bar]   = {np.round(c[idx], 4)}")
        print(f"  ledger entry_price       = {np.round(ep[:20], 4)}")
        d = np.abs(c[idx] - ep[:20])
        print(f"  |close[entry_bar] - entry_price| = {np.round(d, 4)}")
        print(f"  max = {d.max():.4f}   mean = {d.mean():.4f}")
        # The ledger's drift for these legs.
        ld = legs["drift_usd"].astype(np.float64)
        side = legs["side"].astype(np.float64)
        qty = legs["qty"].astype(np.float64)
        drift_from_file = (c[xb[:20]] - c[idx]) * side[:20] * qty[:20]
        print(f"\n  ledger drift_usd[:20]        = {np.round(ld[:20], 4)}")
        print(f"  drift recomputed from file   = {np.round(drift_from_file, 4)}")
        print(f"  max abs difference            = "
              f"{np.abs(drift_from_file - ld[:20]).max():.6e}")

    # The bar-to-second ratio.
    print("\n[2] BAR DENSITY")
    secs = (xt - et) / 1e9
    bars = (xb - eb).astype(np.float64)
    ok2 = bars > 0
    ratio = np.median(secs[ok2] / bars[ok2])
    print(f"  median (exit_time - entry_time) / (exit_bar - entry_bar) "
          f"= {ratio:.4f} s/bar")
    print(f"  (1.0 means the file is dense 1s; >1 means the file has gaps)")

    # Is the ledger's time the file's time at the same index?
    print("\n[3] IS signal_time_ns CONSISTENT WITH entry_bar?")
    # The engine's own array is what matters. Compare the ledger's
    # entry_time_ns to the file's time at entry_bar -- but entry_bar
    # may exceed row group 0.
    need = int(eb.max()) + 1
    print(f"  max entry_bar = {eb.max():,}   "
          f"need {need:,} rows for the check")


if __name__ == "__main__":
    main()
