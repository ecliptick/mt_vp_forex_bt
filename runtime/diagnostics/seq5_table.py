"""THE 5-EVENT BoS/CHoCH SEQUENCE TABLE, PER SIDE (corpus-wide).

WHAT THIS ANSWERS
==================

The user asked:

    "tabulate bos/choch 5-event sequence and the subsequent entries.
     split between longs and shorts. then, we know which 5-seq
     events to whitelist before each long/short"

So: for every distinct trailing 5-event BoS/CHoCH signature that
precedes an entry, tabulate the REALISED outcome of the entries that
followed -- separately for longs and for shorts -- and then say
which signatures are worth whitelisting on each side.

WHY THE CORPUS LEDGER, NOT MONTHS
==================================

AGENTS.md § 1.1: the unit of evaluation is the LEG, not the month.
A 5-event signature has up to 4**5 = 1024 shapes. On one month
(~500 legs) each shape gets <1 leg, so a per-signature table on a
month is noise. On the corpus-wide continuous run (51k legs) the
table is estimable.

WHY THE SIGNATURE IS TAKEN AT THE **SIGNAL** BAR
================================================

The engine's entry-side gates are evaluated at the SIGNAL bar,
because that is the causal decision point: the signature is a
function of the structure state at that bar, and nothing after it
is knowable. (The fill bar is later -- median 42 bars -- and using
it would tag a different event than the engine gated on.)

CAUSALITY
=========

The signature is built from the break events at or BEFORE the
signal bar:

    upto = searchsorted(break_bars, signal_bar, side="right")

``side="right"`` includes a break ON the signal bar, which is
knowable at that bar. It cannot include a later break. So the
table is causal -- no look-ahead.

THE SPLIT-HALF CONSISTENCY CHECK
=================================

A whitelist fitted and read on the same data is a mining artefact
until it survives on data it was not fitted on. So every signature
is reported on the FIRST HALF and the SECOND HALF of the corpus
(both by time), and a candidate must be positive on BOTH to be
called a candidate. This is the same test
``per_side_sequence_paired.py`` used for the 1-event buckets.

WHAT THIS DOES NOT CLAIM
========================

* A post-hoc whitelist changes which legs EXIST. Filtering the
  measured legs after the fact measures the legs' own P&L under a
  different entry population; it does NOT re-run the book, so the
  conflict policy, the same-direction cap and the contra-close
  netting are NOT re-evaluated. The engine-side implementation
  must re-measure end to end before shipping.
* The canonical ledger is read-only here. No engine change.

Usage
-----
    python -m runtime.diagnostics.seq5_table
    python -m runtime.diagnostics.seq5_table --min-n 50
    python -m runtime.diagnostics.seq5_table --top 25
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from bt._engine.market_structure import _BREAK_SIG, _break_bars  # noqa: E402
from runtime.diagnostics.leg_ledger import (  # noqa: E402
    LEDGER_PATH,
    DEFAULT_DATA,
    DEFAULT_END,
    DEFAULT_START,
    WARMUP_DAYS,
    load_ledger,
    ledger_arrays,
)
from runtime.results import registry  # noqa: E402

OUT = Path("runtime/diagnostics/_reports/seq5_table.json")

# The canonical corpus window's EXECUTION window (the ledger runs
# with a warmup prefix and discards the prefix's legs).
EXEC_START = (
    pd.Timestamp(DEFAULT_START, tz="UTC")
    - pd.Timedelta(days=WARMUP_DAYS)
).strftime("%Y-%m-%d")
EXEC_END = DEFAULT_END
SCOPE = f"{DEFAULT_DATA.stem}__{DEFAULT_START.replace('-', '')}_" \
        f"{DEFAULT_END.replace('-', '')}"

# The event letters, with their meaning.
EVENT_MEANING = {
    "b": "BoS bull   (bull continuation)",
    "B": "BoS bear   (bear continuation)",
    "c": "CHoCH bull (bull reversal)",
    "C": "CHoCH bear (bear reversal)",
}

SIG_LEN = 5


def _tstat(vals: np.ndarray) -> float:
    """t-stat of the mean of ``vals`` (0.0 when undefined)."""
    if vals.size < 2:
        return 0.0
    sd = float(vals.std(ddof=1))
    if sd <= 0.0:
        return 0.0
    return float(vals.mean() / (sd / np.sqrt(vals.size)))


def _structure_for_corpus() -> Any:
    """The StructureState for the ledger's EXECUTION window.

    Rebuilt through the SAME ArtifactCache the ledger used, so the
    bar indices are identical (both index into the bars loaded for
    ``EXEC_START .. EXEC_END``). The artifact pickle is ~2 GB and
    the structure object is the only thing needed from it.
    """
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    cache = ArtifactCache(DEFAULT_DATA, SCOPE, EXEC_START, EXEC_END)
    path = cache.path
    if not path.exists():
        raise SystemExit(
            f"artifact cache MISS at {path}\n"
            "Build it first:\n"
            "    python -m runtime.diagnostics.leg_ledger"
        )
    print(f"  loading artifacts: {path.name}", flush=True)
    t0 = time.perf_counter()
    import pickle
    with open(path, "rb") as fh:
        art = pickle.load(fh)
    print(f"  artifacts loaded in {time.perf_counter() - t0:.1f}s",
          flush=True)
    return art["structure"]


def _signatures(structure: Any, signal_bars: np.ndarray) -> np.ndarray:
    """The trailing 5-event signature at each signal bar.

    Returns an array of strings; an empty string means "fewer than 5
    structure events had occurred by that bar" (the leg has no
    signature and is excluded from the table).
    """
    bb = _break_bars(structure)
    out = np.full(len(signal_bars), "", dtype=object)
    if bb.size == 0:
        return out
    breaks = structure.breaks
    for i, bar in enumerate(signal_bars):
        upto = int(np.searchsorted(bb, int(bar), side="right"))
        if upto < SIG_LEN:
            continue
        out[i] = "".join(_BREAK_SIG.get(breaks[j].kind, "?")
                          for j in range(upto - SIG_LEN, upto))
    return out


def _bucket_table(
    sig: np.ndarray,
    gross: np.ndarray,
    net: np.ndarray,
    fee: np.ndarray,
    drift: np.ndarray,
    excess: np.ndarray,
    mask: np.ndarray,
) -> dict[str, dict[str, Any]]:
    """Per-signature statistics for one population."""
    out: dict[str, dict[str, Any]] = {}
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return out
    for s in np.unique(sig[idx]):
        m = idx[sig[idx] == s]
        g = gross[m]
        n = net[m]
        out[str(s)] = {
            "n": int(m.size),
            "gross": float(g.sum()),
            "net": float(n.sum()),
            "fees": float(fee[m].sum()),
            "gross_per_leg": float(g.sum()) / m.size,
            "net_per_leg": float(n.sum()) / m.size,
            "hit": float((g > 0).mean()),
            "t_gross": _tstat(g),
            "drift": float(drift[m].sum()),
            "excess": float(excess[m].sum()),
        }
    return out


def _half_masks(signal_time_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Split the legs into the FIRST and SECOND half of the corpus.

    Split by TIME (the signal timestamp), so the two halves are
    chronological and the structures are not independent but the
    regimes are separated.
    """
    if signal_time_ns.size == 0:
        return np.zeros(0, bool), np.zeros(0, bool)
    median = float(np.median(signal_time_ns))
    first = signal_time_ns <= median
    return first, ~first


def _last_event(sig: str) -> str:
    return sig[-1] if sig else "?"


def _polarity(sig: str) -> str:
    bull = sum(1 for c in sig if c in ("b", "c"))
    bear = sum(1 for c in sig if c in ("B", "C"))
    if bull >= 3:
        return "bull"
    if bear >= 3:
        return "bear"
    return "mixed"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--ledger", type=Path, default=LEDGER_PATH)
    ap.add_argument("--min-n", type=int, default=50,
                    help="Minimum legs per signature to be tabulated.")
    ap.add_argument("--top", type=int, default=25,
                    help="How many signatures to print per side.")
    ap.add_argument("--out", type=Path, default=OUT)
    ap.add_argument("--expect-canonical", action="store_true",
                    help="Assert the ledger was built with the CANONICAL "
                         "recipe (no engine overrides).")
    args = ap.parse_args()

    print("=" * 78)
    print("5-EVENT BoS/CHoCH SEQUENCE TABLE  (corpus-wide, per side)")
    print("=" * 78)

    payload = load_ledger(args.ledger,
                          expect_canonical=args.expect_canonical)
    arr = ledger_arrays(payload)
    n_legs = int(arr["gross"].size)
    print(f"  ledger legs : {n_legs}")
    print(f"  window      : {payload['score_start']} .. {payload['score_end']}")
    print(f"  overrides   : {payload.get('engine_overrides') or '{}'}")
    print()

    structure = _structure_for_corpus()
    print()
    print("-- tagging legs with their trailing 5-event signature --")
    t0 = time.perf_counter()
    sig = _signatures(structure, arr["signal_bar"])
    print(f"  tagged in {time.perf_counter() - t0:.1f}s; "
          f"{(sig == '').sum()} legs have no signature "
          f"(fewer than 5 structure events yet)")

    has = sig != ""
    long_m = has & (arr["side"] > 0)
    short_m = has & (arr["side"] < 0)
    print(f"  long legs tagged  : {int(long_m.sum())}")
    print(f"  short legs tagged : {int(short_m.sum())}")
    print()

    first, second = _half_masks(arr["signal_time_ns"])

    common = dict(
        gross=arr["gross"], net=arr["net"], fee=arr["fee"],
        drift=arr["drift_usd"], excess=arr["excess_usd"],
    )

    long_all = _bucket_table(sig, mask=long_m, **common)
    long_h1 = _bucket_table(sig, mask=long_m & first, **common)
    long_h2 = _bucket_table(sig, mask=long_m & second, **common)
    short_all = _bucket_table(sig, mask=short_m, **common)
    short_h1 = _bucket_table(sig, mask=short_m & first, **common)
    short_h2 = _bucket_table(sig, mask=short_m & second, **common)

    # The one-event buckets (the granularity the engine's
    # ``structure_seq_drop_set`` uses today) -- reported for
    # comparison, because a 1-event rule cannot express a 5-event
    # whitelist.
    def _by_last(table: dict, side: str) -> dict:
        out: dict[str, dict[str, Any]] = {}
        for s, v in table.items():
            k = _last_event(s)
            acc = out.setdefault(k, {"n": 0, "gross": 0.0, "net": 0.0,
                                     "hits": 0.0, "fees": 0.0})
            acc["n"] += v["n"]
            acc["gross"] += v["gross"]
            acc["net"] += v["net"]
            acc["fees"] += v["fees"]
            acc["hits"] += v["hit"] * v["n"]
        for k, acc in out.items():
            acc["hit"] = acc["hits"] / acc["n"] if acc["n"] else 0.0
            acc["gross_per_leg"] = acc["gross"] / acc["n"] if acc["n"] else 0.0
            acc.pop("hits")
        return out

    def _print_side(side: str, all_t, h1, h2, top: int) -> None:
        print("=" * 78)
        print(f"{side.upper()}  --  5-event signature table")
        print("=" * 78)
        rows = []
        for s, v in all_t.items():
            a = h1.get(s)
            b = h2.get(s)
            rows.append((s, v, a, b))
        rows.sort(key=lambda r: -r[1]["gross_per_leg"])
        print(f"{'signature':<9} {'n':>6} {'gross':>11} {'net':>10} "
              f"{'g/leg':>8} {'hit':>6} {'t':>6} | "
              f"{'H1 n':>5} {'H1 g/leg':>9} | {'H2 n':>5} {'H2 g/leg':>9} "
              f"{'consist':>8}")
        shown = 0
        for s, v, a, b in rows:
            if v["n"] < args.min_n:
                continue
            shown += 1
            if shown > top:
                break
            a_txt = (f"{a['gross_per_leg']:>+9.3f}" if a else f"{'-':>9}")
            b_txt = (f"{b['gross_per_leg']:>+9.3f}" if b else f"{'-':>9}")
            an = f"{a['n']:>5}" if a else f"{'-':>5}"
            bn = f"{b['n']:>5}" if b else f"{'-':>5}"
            if a and b:
                consist = "YES" if (a["gross_per_leg"] > 0
                                    and b["gross_per_leg"] > 0) else "no"
            else:
                consist = "-"
            print(f"{s:<9} {v['n']:>6} {v['gross']:>11,.0f} "
                  f"{v['net']:>10,.0f} {v['gross_per_leg']:>+8.3f} "
                  f"{v['hit'] * 100:>5.1f}% {v['t_gross']:>+6.2f} | "
                  f"{an} {a_txt} | {bn} {b_txt} {consist:>8}")
        print()
        print(f"  -- {side.upper()}: 1-event buckets (the engine's current "
              f"granularity) --")
        last_t = _by_last(all_t, side)
        print(f"  {'evt':<5} {'n':>6} {'gross':>11} {'net':>10} "
              f"{'g/leg':>8} {'hit':>6}  meaning")
        for k in ("b", "B", "c", "C"):
            v = last_t.get(k)
            if not v:
                continue
            print(f"  {k:<5} {v['n']:>6} {v['gross']:>11,.0f} "
                  f"{v['net']:>10,.0f} {v['gross_per_leg']:>+8.3f} "
                  f"{v['hit'] * 100:>5.1f}%  {EVENT_MEANING[k]}")
        print()

    _print_side("long", long_all, long_h1, long_h2, args.top)
    _print_side("short", short_all, short_h1, short_h2, args.top)

    # ── The whitelist candidates ────────────────────────────────
    print("=" * 78)
    print("WHITELIST CANDIDATES  (positive gross/leg on BOTH halves)")
    print("=" * 78)
    candidates: dict[str, list[str]] = {}
    for side, all_t, h1, h2 in (("long", long_all, long_h1, long_h2),
                                ("short", short_all, short_h1, short_h2)):
        keep: list[str] = []
        for s, v in all_t.items():
            if v["n"] < args.min_n:
                continue
            a, b = h1.get(s), h2.get(s)
            if not a or not b:
                continue
            if a["gross_per_leg"] > 0 and b["gross_per_leg"] > 0:
                keep.append(s)
        keep.sort(key=lambda s: -all_t[s]["gross_per_leg"])
        candidates[side] = keep
        print(f"  {side}: {len(keep)} of {len(all_t)} signatures")
        for s in keep:
            v = all_t[s]
            print(f"    + {s}  n={v['n']:>5}  gross/leg=${v['gross_per_leg']:+.3f}  "
                  f"gross=${v['gross']:>+10,.0f}  net=${v['net']:>+9,.0f}  "
                  f"H1=${h1[s]['gross_per_leg']:+.3f} H2=${h2[s]['gross_per_leg']:+.3f}")
    print()

    # ── What the whitelist would do (POST-HOC, legs only) ───────
    print("=" * 78)
    print("POST-HOC EFFECT OF THE WHITELIST  (legs only, NOT a re-run)")
    print("=" * 78)
    effect: dict[str, Any] = {}
    for side, all_t, mask in (("long", long_all, long_m),
                              ("short", short_all, short_m)):
        keep = set(candidates[side])
        idx = np.flatnonzero(mask)
        kept = [i for i in idx if sig[i] in keep]
        dropped = [i for i in idx if sig[i] not in keep]
        base_gross = float(arr["gross"][idx].sum())
        base_net = float(arr["net"][idx].sum())
        k_gross = float(arr["gross"][kept].sum()) if kept else 0.0
        k_net = float(arr["net"][kept].sum()) if kept else 0.0
        d_gross = float(arr["gross"][dropped].sum()) if dropped else 0.0
        effect[side] = {
            "base_legs": len(idx), "kept_legs": len(kept),
            "base_gross": base_gross, "kept_gross": k_gross,
            "d_gross": k_gross - base_gross,
            "base_net": base_net, "kept_net": k_net,
            "d_net": k_net - base_net,
            "dropped_gross": d_gross,
            "dropped_t": _tstat(arr["gross"][dropped])
            if dropped else 0.0,
            "dropped_n": len(dropped),
        }
        e = effect[side]
        print(f"  {side:<6} legs {e['base_legs']:>6} -> {e['kept_legs']:>6}  "
              f"gross ${e['base_gross']:>10,.0f} -> ${e['kept_gross']:>10,.0f} "
              f"({e['d_gross']:>+9,.0f})  "
              f"net ${e['base_net']:>10,.0f} -> ${e['kept_net']:>10,.0f} "
              f"({e['d_net']:>+9,.0f})")
        print(f"         dropped n={e['dropped_n']:>6}  "
              f"dropped gross ${e['dropped_gross']:>+10,.0f}  "
              f"t={e['dropped_t']:+.2f}")
    print()

    # ── Save ───────────────────────────────────────────────────
    payload_out = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "ledger": {
            "path": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "n_legs": n_legs,
        },
        "sig_len": SIG_LEN,
        "min_n": args.min_n,
        "long": {"all": long_all, "h1": long_h1, "h2": long_h2},
        "short": {"all": short_all, "h1": short_h1, "h2": short_h2},
        "candidates": candidates,
        "effect": effect,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload_out, indent=2, default=str),
                        encoding="utf-8")
    print(f"-> {args.out}")

    registry.record_run(
        kind="seq5_table",
        params={
            "ledger": str(args.ledger),
            "score_start": payload["score_start"],
            "score_end": payload["score_end"],
            "engine_overrides": payload.get("engine_overrides"),
            "sig_len": SIG_LEN,
            "min_n": args.min_n,
        },
        summary={
            "n_legs": n_legs,
            "n_long_candidates": len(candidates["long"]),
            "n_short_candidates": len(candidates["short"]),
            "long_d_gross": round(effect["long"]["d_gross"], 6),
            "short_d_gross": round(effect["short"]["d_gross"], 6),
            "long_kept_legs": effect["long"]["kept_legs"],
            "short_kept_legs": effect["short"]["kept_legs"],
        },
        payload=payload_out,
    )


if __name__ == "__main__":
    main()
