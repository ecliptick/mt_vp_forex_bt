"""VALIDATE the TA gate's re-pricing against the canonical ledger.

THE IDENTITIES
==============

The gate re-prices a leg's gross and fee from the entry price and the
exit price, using the engine's own formulas:

    gross = (exit - entry) * side * qty
    fee   = |exit + entry| * 0.5 * qty * (bps / 10000) * 2

So on the legs the gate lets through, the re-priced gross and fee
must reproduce the ledger's ``gross`` and ``fee`` EXACTLY.

THE TEST
========

An all-LONG signal (``+1`` on every 1m bar) passes every LONG leg and
rejects every SHORT leg. So:

    * the passed set must be exactly the long legs,
    * the re-priced gross/fee must equal the ledger's, to the bit,
    * the 1m-grid drift must AGREE with the ledger's 1s-grid drift
      (correlation + sign), and the 1m-grid excess must agree in
      SIGN with the ledger's.

If any of these fail, the sweep's drift/excess columns are garbage.
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from runtime.diagnostics.leg_ledger import LEDGER_PATH  # noqa: E402
from runtime.diagnostics.ta_gate import (  # noqa: E402
    apply_gate,
    build_bars_1m_streaming,
    fee_bps_from_legs,
    load_legs,
)
from runtime.sweeps.cached_sweep import DEFAULT_DATA  # noqa: E402


def main() -> None:
    legs = load_legs(LEDGER_PATH)
    meta = legs.pop("_meta")
    n = int(legs["side"].size)
    bps = fee_bps_from_legs(legs)
    print(f"ledger: {n:,} legs  built {meta.get('built_utc')}  bps={bps:.10f}")

    grid = build_bars_1m_streaming(DEFAULT_DATA, with_row_map=True)
    bars = grid.bars
    print(f"1m bars: {len(bars):,}   1s closes: {grid.closes_1s.size:,}")

    side = legs["side"].astype(np.int64)
    n_long = int((side > 0).sum())

    # An all-LONG signal: passes every long leg, rejects every short.
    always_long = np.ones(int(len(bars)), dtype=np.int8)

    res = apply_gate(legs, always_long, bars, bps=bps, grid=grid)
    filled = res.filled
    n_passed = int(filled.sum())
    print(f"\ngate: {n_passed:,} legs passed (expected {n_long:,} longs), "
          f"{int(res.n_cancelled_at_fill):,} cancelled at fill")

    lg = legs["gross"].astype(np.float64)
    lf = legs["fee"].astype(np.float64)
    ld = legs["drift_usd"].astype(np.float64)
    le = legs["excess_usd"].astype(np.float64)

    g = res.gross_gate_only[filled]
    f = res.fee_gate_only[filled]
    d = res.drift_gate_only[filled]
    ex = res.excess_gate_only[filled]

    ok = True

    # ── 1. the PASSED SET must be exactly the long legs ─────────
    print("\n[1] THE PASSED SET == THE LONG LEGS")
    all_long = bool(np.all(side[filled] > 0))
    n_missed = n_long - n_passed
    print(f"    passed {n_passed:,}  longs {n_long:,}  "
          f"missed {n_missed:,}  (a long can be cancelled at fill if "
          f"the signal flips, but the signal is constant here)")
    print(f"    every passed leg is long: {all_long}   "
          f"{'PASS' if all_long else 'FAIL'}")
    ok &= all_long

    # ── 2. the gross identity ──────────────────────────────────
    print("\n[2] GROSS IDENTITY (bit-exact)")
    gross_err = float(np.max(np.abs(g - lg[filled])))
    print(f"    ledger gross total: ${lg[filled].sum():,.6f}")
    print(f"    gate   gross total: ${g.sum():,.6f}")
    print(f"    max abs error     : {gross_err:.3e}")
    print(f"    {'PASS' if gross_err < 1e-9 else 'FAIL'}")
    ok &= gross_err < 1e-9

    # ── 3. the fee identity ────────────────────────────────────
    print("\n[3] FEE IDENTITY (bit-exact)")
    fee_err = float(np.max(np.abs(f - lf[filled])))
    print(f"    ledger fee total : ${lf[filled].sum():,.6f}")
    print(f"    gate   fee total : ${f.sum():,.6f}")
    print(f"    max abs error    : {fee_err:.3e}")
    print(f"    {'PASS' if fee_err < 1e-9 else 'FAIL'}")
    ok &= fee_err < 1e-9

    # ── 4. the drift agreement (1s grid vs the ledger's 1s grid) ──
    print("\n[4] DRIFT AGREEMENT (both on the ledger's 1s grid)")
    corr = float(np.corrcoef(d, ld[filled])[0, 1])
    ldt = float(ld[filled].sum())
    gdt = float(d.sum())
    drift_err = float(np.max(np.abs(d - ld[filled])))
    print(f"    ledger drift total: ${ldt:,.6f}")
    print(f"    gate   drift total: ${gdt:,.6f}")
    print(f"    max abs error     : {drift_err:.3e}   "
          f"{'PASS (bit-exact)' if drift_err < 1e-9 else 'FAIL'}")
    print(f"    correlation       : {corr:.6f}")
    ok &= drift_err < 1e-9

    # ── 5. the excess agreement ────────────────────────────────
    print("\n[5] EXCESS AGREEMENT")
    ecorr = float(np.corrcoef(ex, le[filled])[0, 1])
    let = float(le[filled].sum())
    get = float(ex.sum())
    ex_err = float(np.max(np.abs(ex - le[filled])))
    print(f"    ledger excess total: ${let:,.6f}")
    print(f"    gate   excess total: ${get:,.6f}")
    print(f"    max abs error      : {ex_err:.3e}   "
          f"{'PASS (bit-exact)' if ex_err < 1e-9 else 'FAIL'}")
    print(f"    correlation        : {ecorr:.6f}")
    ok &= ex_err < 1e-9

    # ── 6. the ledger's own identity must hold ─────────────────
    print("\n[6] LEDGER IDENTITY (gross - drift == excess)")
    id_err = float(np.max(np.abs((lg - ld) - le)))
    print(f"    max abs error: {id_err:.3e}   "
          f"{'PASS' if id_err < 1e-8 else 'FAIL'}")
    ok &= id_err < 1e-8

    print("\n" + "=" * 60)
    print("OVERALL: " + ("PASS" if ok else "FAIL"))
    print("=" * 60)
    if not ok:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
