"""Analyse the saved OLD-repo decomposition: per-exit-reason drift vs gross,
the loss geometry, and what actually carries the profit."""
import json
from pathlib import Path
import numpy as np
import pandas as pd

P = Path(__file__).parent / "_reports" / "old_repo_decomposition.json"


def main() -> None:
    # NOTE: this read is INSIDE the main guard on purpose.
    # ``runtime/diagnostics/_reports/`` is GITIGNORED, so on a fresh
    # clone this file does not exist and reading it at module scope
    # makes the module raise on IMPORT -- which is what broke the
    # repo-wide import test. See
    # ``tests/test_no_dead_imports.py::test_no_module_does_work_at_import_time``.
    if not P.exists():
        raise SystemExit(
            "missing input: %s\n"
            "(the _reports/ directory is gitignored -- regenerate the "
            "decomposition before running this analysis)" % P
        )
    d = json.loads(P.read_text(encoding="utf-8"))
    tr = d["trades"]
    g = np.array([t["gross"] for t in tr])
    dr = np.array([t["drift"] for t in tr])
    ex = g - dr
    reason = np.array([t["exit_reason"] for t in tr])
    qty = np.array([t["qty"] for t in tr])

    print("=" * 74)
    print("PER EXIT REASON — where the gross and the drift come from")
    print("=" * 74)
    print("%-6s %5s %11s %11s %11s %9s" % ("exit", "n", "gross", "drift", "excess", "avg_g"))
    for r in sorted(set(reason)):
        m = reason == r
        print("%-6s %5d %11.2f %11.2f %11.2f %9.2f"
              % (r, m.sum(), g[m].sum(), dr[m].sum(), ex[m].sum(), g[m].mean()))
    print("-" * 74)
    print("%-6s %5s %11s %11s %11s" % ("TOTAL", len(tr), g.sum(), dr.sum(), ex.sum()))

    print("\n" + "=" * 74)
    print("THE GEOMETRY — what actually caps the loss?")
    print("=" * 74)
    for r in sorted(set(reason)):
        m = reason == r
        loss = g[m] <= 0
        if loss.sum() == 0:
            print("  %-6s (no losses)" % r)
            continue
        print("  %-6s losses n=%3d  avg $%7.3f  median $%7.3f  max $%7.3f"
              % (r, loss.sum(), -g[m][loss].mean(),
                 -np.median(g[m][loss]), -g[m][loss].min()))
    print("  overall avg loss $%.3f" % (-g[g <= 0].mean()))

    print("\n" + "=" * 74)
    print("SIDE DECOMPOSITION")
    print("=" * 74)
    for side, lab in ((1, "LONG"), (-1, "SHORT")):
        m = np.array([t["direction"] == side for t in tr])
        print("  %-5s n=%3d  gross $%9.2f  drift $%9.2f  excess $%9.2f  avg_gross $%7.2f"
              % (lab, m.sum(), g[m].sum(), dr[m].sum(), ex[m].sum(), g[m].mean()))
        for r in sorted(set(reason)):
            mm = m & (reason == r)
            if mm.sum():
                print("        %-6s n=%3d gross $%9.2f" % (r, mm.sum(), g[mm].sum()))

    print("\n" + "=" * 74)
    print("DOES A RANDOM LONG OVER THE SAME WINDOWS DO AS WELL?")
    print("=" * 74)
    # For each trade, the drift a PASSIVE hold of the same side over the same
    # window would have earned. Compare with the strategy's realised gross.
    print("  strategy gross            = $%9.2f" % g.sum())
    print("  passive hold (same side)  = $%9.2f" % dr.sum())
    print("  difference (excess)       = $%9.2f" % ex.sum())
    # what if we had done nothing but been long for the same total time?
    long_only = dr[np.array([t["direction"] == 1 for t in tr])].sum()
    print("  long-leg drift alone      = $%9.2f  (%.1f%% of gross)"
          % (long_only, 100.0 * long_only / g.sum()))

    print("\n" + "=" * 74)
    print("TOP-10 TRADES — the profit is in a handful of multi-day runners")
    print("=" * 74)
    order = np.argsort(-g)
    for i in order[:10]:
        t = tr[i]
        print("  %-6s %s dir=%+d entry $%8.2f exit $%8.2f pnl $%8.2f hold %6.1f h  drift $%8.2f"
              % (t["exit_reason"],
                 pd.Timestamp(t["entry_ts"], unit="ns", tz="UTC").strftime("%Y-%m-%d %H:%M"),
                 t["direction"], t["entry_price"], t["exit_price"], t["pnl"],
                 t["hold_s"] / 3600, t["drift"]))

    print("\n" + "=" * 74)
    print("WHAT IF THE BIGGEST WINNER HAD NOT HAPPENED?")
    print("=" * 74)
    for k in (1, 2, 3, 5):
        s = g.sum() - g[order[:k]].sum()
        print("  drop top %d: gross $%8.2f  -> net $%8.2f" % (k, s, s - d["summary"]["fee"]))


if __name__ == "__main__":
    main()
