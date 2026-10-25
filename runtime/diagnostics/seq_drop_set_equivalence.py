"""Verify the engine-side PER-SIDE SEQUENCE DROP SET matches post-hoc.

The drop set was measured post-hoc (discard the matching trades
after the engine ran) in ``per_side_sequence_pooled.py``. This module
proves the ENGINE implementation produces the identical trade set, so
the measurement transfers to the in-engine knob.

The reason they should agree
----------------------------
The drop set is applied AFTER the sequence-direction flip and BEFORE
the conflict-policy check. Under ``conflict_policy="stack"`` an
opposite-direction signal never blocks admission and
``max_same_dir_open`` is PER-DIRECTION, so removing a short cannot
change which longs are admitted, nor which other shorts are admitted
(the cap counts only same-direction opens).

This script checks that empirically rather than trusting the
reasoning.

Usage
-----
    python -m runtime.diagnostics.seq_drop_set_equivalence
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

from bt._engine.market_structure import _BREAK_SIG, _break_bars  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    STRUCTURE_SEQ_DROP_SET,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/seq_drop_set_equivalence.json")

SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
    ("2024-06 FLAT", "2024-06-01", "2024-07-01"),
    ("2026-04 FLAT", "2026-04-01", "2026-05-01"),
]


def _artifacts(tag: str, start: str, end: str, bars) -> dict:
    return ArtifactCache(DATA, f"pssf_{tag}", start, end).build_or_load(bars)


def _last_event(art: dict, signal_bar) -> str:
    """The signature letter of the LAST structure break at the signal bar.

    The engine evaluates the per-side drop set at the SIGNAL bar (when
    the decision is made), not the fill bar. Under the D2 resting-order
    model the fill bar is later (median 42 bars), so using
    ``entry_bar`` here would tag a different event than the engine
    gated on and the comparison would spuriously mismatch.
    """
    st = art["structure"]
    bb = _break_bars(st)
    if not signal_bar or bb.size == 0:
        return ""
    u = int(np.searchsorted(bb, signal_bar, side="right"))
    if u < 5:
        return ""
    return _BREAK_SIG.get(st.breaks[u - 1].kind, "")


def _run(label: str, start: str, end: str, drop_set) -> dict:
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        structure_seq_drop_set=drop_set,
    )
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    trades = list(res.trades)
    return {
        "trades": trades, "art": art,
        "legs": len(trades),
        "long_legs": sum(1 for t in trades if int(t.direction) > 0),
        "short_legs": sum(1 for t in trades if int(t.direction) < 0),
        "gross": float(sum(t.pnl_gross_usd for t in trades)),
        "net": float(sum(t.pnl_net_usd for t in trades)),
        "n_dropped_seq_side": int(getattr(res, "n_dropped_seq_side", 0)),
    }


def main() -> None:
    print("== per-side sequence drop-set equivalence ==")
    print(f"  drop set: {sorted(STRUCTURE_SEQ_DROP_SET)}")
    print()
    all_ok = True
    rows = []
    for label, s, e in SLICES:
        both = _run(label, s, e, None)
        gated = _run(label, s, e, STRUCTURE_SEQ_DROP_SET)

        # Post-hoc reference from the CONTROL run.
        ph = []
        for t in both["trades"]:
            side = "long" if int(t.direction) > 0 else "short"
            ev = _last_event(both["art"], getattr(t, "signal_bar", 0))
            if f"{side}|{ev}" in STRUCTURE_SEQ_DROP_SET:
                continue
            ph.append(t)
        ph_gross = float(sum(t.pnl_gross_usd for t in ph))
        ph_legs = len(ph)

        legs_ok = gated["legs"] == ph_legs
        gross_ok = abs(gated["gross"] - ph_gross) < 1e-9
        ok = legs_ok and gross_ok
        all_ok = all_ok and ok
        rows.append({
            "slice": label,
            "control_legs": both["legs"], "control_gross": both["gross"],
            "engine_legs": gated["legs"], "engine_gross": gated["gross"],
            "posthoc_legs": ph_legs, "posthoc_gross": ph_gross,
            "n_dropped_seq_side": gated["n_dropped_seq_side"],
            "equivalent": ok,
        })
        print(f"  {label:<12} ctrl={both['legs']:>4} "
              f"gate={gated['legs']:>4} posthoc={ph_legs:>4}  "
              f"dropped={gated['n_dropped_seq_side']:>4}  "
              f"gross {gated['gross']:>8,.2f} vs {ph_gross:>8,.2f}  "
              f"{'OK' if ok else 'MISMATCH'}", flush=True)

    print()
    print(f"  ALL SLICES EQUIVALENT: {all_ok}")
    if not all_ok:
        print("  => The engine knob does NOT match the post-hoc filter.")
    else:
        print("  => The engine-side drop set is EXACTLY equivalent to the")
        print("     post-hoc filter, so the measurement transfers.")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "all_equivalent": all_ok,
                   "drop_set": sorted(STRUCTURE_SEQ_DROP_SET)},
                  fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
