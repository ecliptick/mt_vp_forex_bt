"""Verify the engine-side 5-EVENT WHITELIST matches post-hoc.

The whitelist was measured post-hoc (discard the matching trades
after the engine ran) in ``seq5_table.py``. This module proves the
ENGINE implementation produces the identical trade set, so the
measurement transfers to the in-engine knob.

The reason they should agree
----------------------------
The whitelist is applied AFTER the sequence-direction flip and BEFORE
the conflict-policy check. Under ``conflict_policy="stack"`` an
opposite-direction signal never blocks admission and
``max_same_dir_open`` is PER-DIRECTION, so removing a trade cannot
change which other trades are admitted (the cap counts only
same-direction opens).

This script checks that empirically rather than trusting the
reasoning.

Usage
-----
    python -m runtime.diagnostics.seq_whitelist_equivalence
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt._engine.vp_multi_target_engine import _sequence_signature  # noqa: E402
from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    STRUCTURE_SEQ_WHITELIST,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/seq_whitelist_equivalence.json")

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
    return ArtifactCache(DATA, f"seqwl_{tag}", start, end).build_or_load(bars)


def _signature(art: dict, signal_bar: int, length: int) -> str:
    """The trailing ``length`` BoS/CHoCH signature at the SIGNAL bar.

    The engine evaluates the whitelist at the SIGNAL bar (when the
    decision is made), not the fill bar. Under the D2 resting-order
    model the fill bar is later (median 42 bars), so using
    ``entry_bar`` here would tag a different history than the engine
    gated on and the comparison would spuriously mismatch.
    """
    if not signal_bar:
        return ""
    return _sequence_signature(art["structure"], int(signal_bar), length)


def _run(label: str, start: str, end: str, whitelist) -> dict:
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
    )
    if whitelist is not None:
        ek["structure_seq_whitelist"] = whitelist
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
        "n_dropped_seq_whitelist": int(
            getattr(res, "n_dropped_seq_whitelist", 0)),
    }


def main() -> None:
    print("== 5-event sequence whitelist equivalence ==")
    print(f"  whitelist: {len(STRUCTURE_SEQ_WHITELIST)} keys")
    print()
    all_ok = True
    rows = []
    for label, s, e in SLICES:
        both = _run(label, s, e, None)
        gated = _run(label, s, e, STRUCTURE_SEQ_WHITELIST)

        # The signature length the engine uses: ``structure_seq_len``
        # when set, else ``structure_score_lookback`` (canonical 5).
        ek = build_engine_kwargs()
        length = int(ek.get("structure_seq_len")
                     or ek.get("structure_score_lookback") or 5)

        # Post-hoc reference from the CONTROL run.
        ph = []
        for t in both["trades"]:
            side = "long" if int(t.direction) > 0 else "short"
            sig = _signature(both["art"], getattr(t, "signal_bar", 0), length)
            if f"{side}|{sig}" not in STRUCTURE_SEQ_WHITELIST:
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
            "n_dropped_seq_whitelist": gated["n_dropped_seq_whitelist"],
            "equivalent": ok,
        })
        print(f"  {label:<12} ctrl={both['legs']:>4} "
              f"gate={gated['legs']:>4} posthoc={ph_legs:>4}  "
              f"dropped={gated['n_dropped_seq_whitelist']:>4}  "
              f"gross {gated['gross']:>8,.2f} vs {ph_gross:>8,.2f}  "
              f"{'OK' if ok else 'MISMATCH'}", flush=True)

    print()
    print(f"  ALL SLICES EQUIVALENT: {all_ok}")
    if not all_ok:
        print("  => The engine knob does NOT match the post-hoc filter.")
    else:
        print("  => The engine-side whitelist is EXACTLY equivalent to the")
        print("     post-hoc filter, so the measurement transfers.")

    with open(OUT, "w", encoding="utf-8") as fh:
        json.dump({"rows": rows, "all_equivalent": all_ok,
                   "whitelist": sorted(STRUCTURE_SEQ_WHITELIST)},
                  fh, indent=2, default=str)
    print()
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
