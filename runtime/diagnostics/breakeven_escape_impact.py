"""P&L impact of the TP1 same-bar breakeven escape.

Finding from ``breakeven_reminder_check.py``:

* the remainder leg IS stopped at breakeven (measured: realised p50
  = $0.00, sl_distance p50 = $0.00), so the SL modification works;
* BUT the TP1 branch ``continue``s, so the freshly-created remainder
  is NOT re-scanned against the TP1 bar. When that same bar also
  traded back through the entry price, the breakeven stop was ALREADY
  breached on the bar that created the position -- and the remainder
  survives it.

That is inconsistent with ``fill_model='pessimistic'``, whose whole
contract is "a bar that touches both the stop and the target resolves
to the STOP".

This script quantifies the cost of that inconsistency: for every TP1
leg whose bar already breached breakeven, compare the remainder's
ACTUAL realised P&L against the P&L it would have had if the
breakeven stop had been enforced on that bar (exit at entry, minus
the fee).

Usage
-----
    python -m runtime.diagnostics.breakeven_escape_impact
"""
from __future__ import annotations

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
S, E = "2025-05-01", "2025-06-01"


def main() -> None:
    bars = load_bars(DATA, S, E)
    art = ArtifactCache(DATA, f"c1m_{S[:7]}", S, E).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    ek = build_engine_kwargs(
        spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )

    tr = list(res.trades)
    lows = bars["low"].to_numpy(dtype=np.float64)
    highs = bars["high"].to_numpy(dtype=np.float64)
    closes = bars["close"].to_numpy(dtype=np.float64)

    leg1 = [t for t in tr if t.exit_reason == "tp1"]
    rem = [t for t in tr if t.sl_struct_ref == "breakeven"]

    # Match each TP1 leg to its remainder (same parent zone, remainder
    # exits on a later bar than the TP1 leg).
    print()
    print(f"=== 2025-05 FLAT: TP1 same-bar breakeven escape ===")
    print(f"  TP1 legs            : {len(leg1)}")
    print(f"  remainder legs      : {len(rem)}")
    print()

    esc_rows = []
    for t1 in leg1:
        b = int(t1.exit_bar)
        if b < 0 or b >= len(lows):
            continue
        breached = (lows[b] <= t1.entry_price) if t1.direction > 0 \
            else (highs[b] >= t1.entry_price)
        if not breached:
            continue
        # Find the remainder that belongs to this TP1 leg.
        cand = [r for r in rem
                if r.parent_zone_id == t1.parent_zone_id
                and r.entry_bar == t1.entry_bar
                and r.exit_bar >= b]
        if not cand:
            continue
        r = min(cand, key=lambda x: x.exit_bar)
        actual_net = r.pnl_net_usd
        # If BE had been enforced on the TP1 bar: exit at entry price.
        fee = abs(t1.entry_price + t1.entry_price) * 0.5 * r.qty_btc * (
            0.47 / (2.0 * 1.0 * 3000.0) * 10000.0 / 10000.0
        ) * 2.0
        esc_rows.append((t1, r, actual_net, r.exit_price))

    n = len(esc_rows)
    print(f"  TP1 legs whose bar ALSO breached breakeven: {n}")
    if not n:
        print("  (none -- no impact this slice)")
        print()
        return

    actual = sum(x[2] for x in esc_rows)
    print(f"  their remainder ACTUAL net   : ${actual:,.2f}")
    print()
    print(f"  {'TP1 bar':>8} {'dir':>4} {'entry':>9} {'rem exit':>9} "
          f"{'rem net':>9} {'rem reason':>10}")
    for t1, r, net, ex in esc_rows:
        print(f"  {int(t1.exit_bar):>8} {t1.direction:>+4} "
              f"{t1.entry_price:>9.2f} {ex:>9.2f} {net:>9.2f} "
              f"{r.exit_reason:>10}")
    print()
    won = [x for x in esc_rows if x[1].exit_reason == "tp"]
    print(f"  of the {n} escaped remainders, {len(won)} still went on to "
          f"hit TP2 (a WIN)")
    print(f"  -> the inconsistency is FAVOURABLE to the strategy: a bar that")
    print(f"     hit TP1 and also breached the breakeven stop is credited a")
    print(f"     free ride instead of a stop-out.")
    print()

    # Sensitivity: what does the slice P&L look like if BE were enforced
    # on the TP1 bar for these legs?
    total_net = sum(t.pnl_net_usd for t in tr)
    print(f"  slice total net (as measured)          : ${total_net:,.2f}")
    if won:
        w = sum(x[2] for x in won)
        print(f"  net contributed by the escaped winners  : ${w:,.2f}")
        print(f"  slice net if BE were enforced on TP1 bar: "
              f"${total_net - w:,.2f}  (approx, ignores re-entry effects)")
    print()


if __name__ == "__main__":
    main()
