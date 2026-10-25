"""Instrument the TP chain on a real slice: which clamp actually binds?

Answers "did the peak-to-trough clamp fail?" directly rather than by
inference from aggregate distributions.

Wraps ``_apply_swing_cap`` / ``_apply_p95_cap`` / ``_separate_tp_legs``
and records, per call:

    raw_tp      TP1 straight out of compute_structural_tp
    after_swing after the NB72 swing clamp
    after_p95   after the NB91 p95 clamp
    final       after _separate_tp_legs

and the ``tp_ref`` suffix at each stage, which is the engine's own
record of WHICH clamp tightened it.

Run: python -m runtime.diagnostics.tp_chain_trace
"""
from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

import bt._engine.vp_multi_target_engine as eng  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
START, END = "2025-01-01", "2025-02-01"

RECORDS: list[dict] = []

_orig_swing = eng._apply_swing_cap
_orig_p95 = eng._apply_p95_cap
_orig_sep = eng._separate_tp_legs
_orig_tp = eng.compute_structural_tp


def tp_spy(**kw):
    out = _orig_tp(**kw)
    raw, ref, tp2, ref2, pct = out
    RECORDS.append({
        "raw": abs(float(raw) - float(kw["entry_price"])),
        "raw_ref": ref,
        "raw_tp2": abs(float(tp2) - float(kw["entry_price"])),
        "entry": float(kw["entry_price"]),
        "dir": int(kw["direction"]),
        "zw": float(kw["zone_width"]),
    })
    return out


def swing_spy(*, tp_price, tp_ref, entry_price, direction, swing_cap, t_ns):
    out = _orig_swing(tp_price=tp_price, tp_ref=tp_ref, entry_price=entry_price,
                      direction=direction, swing_cap=swing_cap, t_ns=t_ns)
    if RECORDS:
        rec = RECORDS[-1]
        rec["after_swing"] = abs(out[0] - entry_price)
        rec["swing_ref"] = out[1]
    return out


def p95_spy(*, tp_price, tp_ref, entry_price, direction, p95_cap, t_ns,
            tp_p95_mult=None):
    out = _orig_p95(tp_price=tp_price, tp_ref=tp_ref, entry_price=entry_price,
                    direction=direction, p95_cap=p95_cap, t_ns=t_ns,
                    tp_p95_mult=tp_p95_mult)
    # Only the FIRST p95 call per record is TP1; the second is TP2.
    if RECORDS and "after_p95" not in RECORDS[-1]:
        rec = RECORDS[-1]
        rec["after_p95"] = abs(out[0] - entry_price)
        rec["p95_ref"] = out[1]
        rec["p95_cap"] = float(p95_cap.cap_for(t_ns)) if p95_cap else float("inf")
        rec["swing_cap"] = (
            float(swing_cache[0].cap_for(t_ns)) if swing_cache else float("inf"))
    return out


def sep_spy(*, tp1, tp2, tp1_ref, tp2_ref, entry_price, direction,
            min_leg_usd=0.01):
    out = _orig_sep(tp1=tp1, tp2=tp2, tp1_ref=tp1_ref, tp2_ref=tp2_ref,
                    entry_price=entry_price, direction=direction,
                    min_leg_usd=min_leg_usd)
    if RECORDS and "after_p95" in RECORDS[-1]:
        rec = RECORDS[-1]
        rec["final"] = abs(out[0] - entry_price)
        rec["final_ref"] = out[2]
        rec["final_tp2"] = abs(out[1] - entry_price)
    return out


swing_cache: list = []


def install_spies() -> None:
    """Monkeypatch the clamp helpers. MUST only be called from main().

    ``tests/test_no_dead_imports.py::test_every_module_imports_cleanly``
    imports every module under ``runtime/``. Patching the engine at
    MODULE SCOPE therefore leaks: the later test then sees the
    ``**kw``-stub spy as ``compute_structural_tp`` and its signature
    no longer advertises ``partial_close_pct``. Importing this file
    must have no side effects.
    """
    eng.compute_structural_tp = tp_spy
    eng._apply_swing_cap = swing_spy
    eng._apply_p95_cap = p95_spy
    eng._separate_tp_legs = sep_spy


def pct(a, label):
    a = np.asarray([v for v in a if v is not None], dtype=float)
    a = a[np.isfinite(a)]
    if a.size == 0:
        print(f"  {label:38s} (empty)")
        return
    print(f"  {label:38s} n={a.size:>8,}  p10={np.percentile(a,10):7.2f}  "
          f"p50={np.percentile(a,50):7.2f}  p90={np.percentile(a,90):8.2f}  "
          f"max={a.max():9.2f}")


def main() -> None:
    from bt.optimal_config import build_engine_kwargs, FOREX_LOTS, FOREX_CONTRACT_SIZE
    from bt.vp_engine import run_vp_sniper_backtest_forex
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    bars = load_bars(DATA, start=START, end=END)
    print(f"{len(bars):,} bars  {START}..{END}")

    art = ArtifactCache(DATA, "tpchain", START, END).build_or_load(bars)
    swing_cache.append(art["swing_cap"])
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    install_spies()

    ek = build_engine_kwargs(spot=spot, swing_cap=art["swing_cap"],
                             p95_cap=art["p95_cap"],
                             sl_zone_mult=6.0, conflict_policy="stack")
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )

    n = len(RECORDS)
    print(f"\n== TP CHAIN over {n:,} entries ==")
    print("  (each row is the SAME trade measured at a later stage)\n")
    pct([r["raw"] for r in RECORDS], "1. raw TP1 (no clamp)")
    pct([r.get("after_swing") for r in RECORDS], "2. after swing cap (NB72)")
    pct([r.get("after_p95") for r in RECORDS], "3. after p95 cap (NB91)")
    pct([r.get("final") for r in RECORDS], "4. final TP1 (after separate)")
    print()
    pct([r["raw_tp2"] for r in RECORDS], "   TP2 raw (200 x zone_width)")
    pct([r.get("final_tp2") for r in RECORDS], "   TP2 final")
    print()
    pct([r.get("swing_cap") for r in RECORDS], "   swing cap VALUE at entry")
    pct([r.get("p95_cap") for r in RECORDS], "   p95 cap VALUE at entry")

    # Which clamp actually tightened the leg?
    def frac(pred):
        return 100.0 * sum(1 for r in RECORDS if pred(r)) / max(n, 1)

    print("\n== WHICH CLAMP BINDS ==")
    print(f"  TP1 raw was BELOW swing cap (swing inert): "
          f"{frac(lambda r: r['raw'] <= r.get('swing_cap', 0) + 1e-9):5.1f}%")
    print(f"  TP1 tightened by swing           : "
          f"{frac(lambda r: 'swing' in str(r.get('swing_ref', ''))):5.1f}%")
    print(f"  TP1 tightened by p95             : "
          f"{frac(lambda r: 'p95' in str(r.get('p95_ref', ''))):5.1f}%")
    print(f"  TP1 untouched by both            : "
          f"{frac(lambda r: 'swing' not in str(r.get('swing_ref', '')) and 'p95' not in str(r.get('p95_ref', ''))):5.1f}%")
    print(f"  TP1 == TP2 after separate (degenerate multi-target): "
          f"{frac(lambda r: 'pinned' in str(r.get('final_ref', ''))):5.1f}%")

    refs: dict[str, int] = {}
    for r in RECORDS:
        k = str(r.get("final_ref", "?"))
        refs[k] = refs.get(k, 0) + 1
    print("\n  final TP1 ref breakdown:")
    for k, v in sorted(refs.items(), key=lambda x: -x[1]):
        print(f"    {k:34s} {v:>8,}  {100.0 * v / max(n, 1):5.1f}%")


if __name__ == "__main__":
    main()