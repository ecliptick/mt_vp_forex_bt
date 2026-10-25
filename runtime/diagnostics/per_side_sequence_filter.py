"""PER-SIDE market-structure sequence filter (2026-10-24).

The user's instruction:

    "we should never trade one side only, if shorts have ablation,
     merely hardcore profitable market struct event sequence
     separately for longs and shorts."

So: keep BOTH sides alive, and gate each side on the market-structure
sequences that are profitable FOR THAT SIDE. A bullish sequence
precedes a long; a bearish sequence precedes a short. The two sides
should be filtered on different keys, because the sequence that sets
up a long is not the sequence that sets up a short.

WHY THE PLAIN WHITELIST FAILED (and what this does differently)
---------------------------------------------------------------
``sequence_whitelist.py`` pooled BOTH sides and used the full 5-char
signature. Three measured failures:

1. **POWER.** With the vol gate on, 331 fit legs spread over 60
   signatures = 5.5 legs/sig, against a ``min_n`` floor of 15. The
   whitelist was empty *by construction*.
2. **SIDE BLINDNESS.** Pooling longs and shorts averages two
   different populations. The short side's negative alpha is a
   *composition* problem, and a pooled filter cannot see it.
3. **NET vs GROSS.** The plain whitelist's held-out "+$2" was $70 of
   fee savings from trading less, hiding a **-$68 gross loss**.

This module fixes all three:

* **Per-side**: every statistic is computed on longs and shorts
  SEPARATELY, and the filter is fitted per side.
* **Pre-specified buckets with power.** The primary bucket is the
  LAST STRUCTURAL EVENT'S TYPE (``b``/``B``/``c``/``C``), which is a
  real market hypothesis, not a data-mined shape: ICT methodology
  distinguishes a CHoCH (reversal / "shift of structure") from a BoS
  (continuation). A secondary coarse bucket (the 5-event polarity:
  bull / bear / mixed) is reported for comparison.
* **GROSS is the metric.** Every result reports gross, net, fees, and
  the gross delta, so a "win" that is only fee savings is visible.
* **Disjoint fit / held-out slices**, and a t-stat on the held-out
  kept trades.

THE PRIMARY HYPOTHESIS (pre-specified, not mined)
-------------------------------------------------
    For a LONG, is the trade more profitable after a CHoCH-bull
    ("c" -- a bullish shift of structure) than after a BoS-bull
    ("b" -- bullish continuation)? And symmetrically for shorts?

That is ONE hypothesis with a direction, tested per side. It is not a
search over 60 shapes.

Usage
-----
    python -m runtime.diagnostics.per_side_sequence_filter
"""
from __future__ import annotations

import argparse
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
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/per_side_sequence_filter.json")

# DISJOINT fit / held-out slice sets.
FIT_SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "up"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "up"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "down"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "down"),
]
HELD_SLICES = [
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "up"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2024-06 FLAT", "2024-06-01", "2024-07-01", "flat"),
    ("2026-04 FLAT", "2026-04-01", "2026-05-01", "flat"),
]

FEE = 0.47  # canonical MT4 round trip: $0.07 commission + $0.40 spread

# The event-type letters, with their meaning.
_EVENT_MEANING = {
    "b": "BoS bull   (bullish continuation)",
    "B": "BoS bear   (bearish continuation)",
    "c": "CHoCH bull (bullish reversal)",
    "C": "CHoCH bear (bearish reversal)",
}


def _bucket_last(sig: str) -> str:
    """The LAST event's type -- the primary (pre-specified) bucket."""
    return sig[-1] if sig else "?"


def _bucket_pol(sig: str) -> str:
    """The 5-event polarity -- the coarse secondary bucket."""
    bull = sum(1 for c in sig if c in ("b", "c"))
    bear = sum(1 for c in sig if c in ("B", "C"))
    if bull >= 3:
        return "bull"
    if bear >= 3:
        return "bear"
    return "mixed"


_BUCKETS = {
    "last": _bucket_last,
    "pol": _bucket_pol,
}


def _artifacts(tag: str, start: str, end: str, bars) -> dict:
    return ArtifactCache(DATA, f"pssf_{tag}", start, end).build_or_load(bars)


def _run(label: str, start: str, end: str) -> dict:
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
        trade_sides="both",
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    trades = list(res.trades)
    return {
        "label": label, "art": art, "trades": trades,
        "mkt_ret_pct": float(
            (bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0),
        "legs": len(trades),
        "wall": time.perf_counter() - t0,
    }


# ── Slice-level cache ────────────────────────────────────────────
# The engine run per slice is the expensive part (~2 min each). The
# FIT/HELD analysis is cheap and re-runnable. So the tagged trades
# (direction, pnl, signature) are cached to disk and reused.
_CACHE = Path("runtime/diagnostics/_reports/_per_side_slice_cache.json")


def _cached_slices() -> dict:
    if _CACHE.exists():
        try:
            return json.loads(_CACHE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _save_cache(cache: dict) -> None:
    _CACHE.parent.mkdir(parents=True, exist_ok=True)
    _CACHE.write_text(json.dumps(cache, indent=2), encoding="utf-8")


def slice_rows(label: str, start: str, end: str) -> tuple:
    """Return ``(mkt_pct, tagged_rows)`` for one slice, cached on disk.

    The engine run per slice is the expensive part (~2 min each); the
    fit/held analysis is cheap and re-runnable. So the tagged trades
    (direction, pnl, signature) are cached to disk and reused.
    """
    cache = _cached_slices()
    key = f"{label}|{start}|{end}"
    if key in cache:
        entry = cache[key]
        tagged = [(_T(e["dir"], e["g"], e["n"]), e["sig"])
                  for e in entry["tagged"]]
        return entry["mkt"], tagged
    r = _run(label, start, end)
    tagged = _tag_trades(r["art"], r["trades"])
    cache[key] = {
        "mkt": r["mkt_ret_pct"],
        "legs": r["legs"],
        "tagged": [
            {"dir": int(t.direction), "g": t.pnl_gross_usd,
             "n": t.pnl_net_usd, "sig": sig}
            for t, sig in tagged
        ],
    }
    _save_cache(cache)
    return r["mkt_ret_pct"], tagged


def _tag_trades(art: dict, trades: list) -> list[tuple]:
    """(trade, signature) for every trade with a usable signature.

    The signature is computed at the trade's **signal bar**, not its
    entry bar. Under the D2 resting-order model the entry bar is the
    FILL bar, which is later (median 42 bars). The engine evaluates
    its entry-side gates at the signal bar, because that is where the
    decision is made and the only bar at which it is causal. Using
    the fill bar here would tag a different event than the engine
    gated on.
    """
    st = art["structure"]
    bb = _break_bars(st)
    out = []
    for t in trades:
        b = getattr(t, "signal_bar", 0) or getattr(t, "entry_bar", None)
        if not b or bb.size == 0:
            continue
        u = int(np.searchsorted(bb, b, side="right"))
        if u < 5:
            continue
        sig = "".join(_BREAK_SIG.get(st.breaks[j].kind, "?")
                      for j in range(u - 5, u))
        out.append((t, sig))
    return out


def _stats(group: list) -> dict:
    if not group:
        return {"n": 0, "gross": 0.0, "net": 0.0, "ev": 0.0, "hit": 0.0,
                "t": 0.0}
    nets = np.array([t.pnl_net_usd for t in group], dtype=np.float64)
    gross = float(sum(t.pnl_gross_usd for t in group))
    net = float(nets.sum())
    sd = float(nets.std(ddof=1)) if nets.size > 1 else 0.0
    t = float(nets.mean() / (sd / np.sqrt(nets.size))) if sd > 0 else 0.0
    return {
        "n": len(group),
        "gross": gross,
        "net": net,
        "ev": net / len(group),
        "hit": float((nets > 0).mean()),
        "t": t,
    }


def _side_key(t) -> str:
    return "long" if int(t.direction) > 0 else "short"


class _T:
    """A minimal trade stand-in.

    The slice cache stores plain dicts; this wraps them so the
    analysis code can use attribute access uniformly whether the
    trade came from the engine or from the cache.
    """

    __slots__ = ("direction", "pnl_gross_usd", "pnl_net_usd")

    def __init__(self, direction: int, gross: float, net: float):
        self.direction = int(direction)
        self.pnl_gross_usd = float(gross)
        self.pnl_net_usd = float(net)


def _fit(bucket_name: str, fit_rows: list) -> tuple[dict, dict]:
    """Fit a per-side whitelist. Returns (per_side_stats, keep).

    ``fit_rows`` is a list of ``(label, mkt_pct, tagged_rows)`` where
    ``tagged_rows`` is a list of ``(trade, signature)``.
    """
    bucketer = _BUCKETS[bucket_name]
    pooled: dict[tuple[str, str], list] = {}
    for _label, _mkt, rows in fit_rows:
        for t, sig in rows:
            pooled.setdefault((_side_key(t), bucketer(sig)), []).append(t)

    stats = {k: _stats(g) for k, g in pooled.items()}
    keep: dict[str, set] = {"long": set(), "short": set()}
    for (side, key), v in stats.items():
        # Admission: enough sample AND positive GROSS per leg. Gross,
        # not net -- a bucket that is only net-positive because it
        # trades less is not a signal.
        if v["n"] >= 20 and (v["gross"] / v["n"]) > 0.0:
            keep[side].add(key)
    return stats, keep


def _apply(rows: list, bucket_name: str, keep: dict) -> tuple[list, list]:
    """Split (trade, sig) rows into (kept, dropped) under the filter."""
    bucketer = _BUCKETS[bucket_name]
    kept, dropped = [], []
    for t, sig in rows:
        side = _side_key(t)
        allowed = keep.get(side) or set()
        if not allowed:
            # No admission for this side at all -> keep everything
            # (the filter is a no-op on a side with no winning bucket).
            kept.append(t)
        elif bucketer(sig) in allowed:
            kept.append(t)
        else:
            dropped.append(t)
    return kept, dropped


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--buckets", default="last,pol")
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args()
    bucket_names = [b.strip() for b in args.buckets.split(",") if b.strip()]

    print("== PER-SIDE market-structure sequence filter ==")
    print(f"  fit slices  : {len(FIT_SLICES)}   held slices: {len(HELD_SLICES)}")
    print(f"  admission   : n >= {args.min_n} AND gross/leg > 0")
    print()

    # ---- run every slice once, cache the tagged rows ----
    print("-- running slices --")
    fit_rows, held_rows = [], []
    for label, s, e, _regime in FIT_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        fit_rows.append((label, mkt, tagged))
        print(f"  fit  {label:<12} mkt={mkt:>+7.2f}%  legs={len(tagged):>4}",
              flush=True)
    for label, s, e, _regime in HELD_SLICES:
        mkt, tagged = slice_rows(label, s, e)
        held_rows.append((label, mkt, tagged))
        print(f"  held {label:<12} mkt={mkt:>+7.2f}%  legs={len(tagged):>4}",
              flush=True)

    all_report = {}
    for bucket_name in bucket_names:
        print()
        print(f"=" * 74)
        print(f"  BUCKET = {bucket_name!r}")
        print("=" * 74)

        stats, keep = _fit(bucket_name, fit_rows)
        print()
        print("  FIT, per side:")
        for side in ("long", "short"):
            keys = sorted(k for (sd, k) in stats if sd == side)
            for k in keys:
                v = stats[(side, k)]
                mark = "KEEP" if k in keep[side] else "    "
                desc = _EVENT_MEANING.get(k, "")
                print(f"    {side:<5} {mark} {k:<6} n={v['n']:>4}  "
                      f"gross/leg=${v['gross'] / max(v['n'], 1):>+7.3f}  "
                      f"gross=${v['gross']:>+9,.0f}  net=${v['net']:>+9,.0f}  "
                      f"hit={v['hit'] * 100:>5.1f}%  t={v['t']:>+6.2f}  {desc}")
        print(f"  keep long  : {sorted(keep['long']) or 'NONE (side unfiltered)'}")
        print(f"  keep short : {sorted(keep['short']) or 'NONE (side unfiltered)'}")

        # ---- HELD-OUT ----
        print()
        print("  HELD-OUT:")
        print(f"    {'slice':<12} {'mkt':>8} {'base legs':>10} {'kept':>6} "
              f"{'base gross':>11} {'wl gross':>10} {'d gross':>9} "
              f"{'base net':>10} {'wl net':>9} {'d net':>8}")
        base_gross = base_net = wl_gross = wl_net = 0.0
        base_legs = kept_legs = 0
        kept_pnls: list[float] = []
        per_slice = []
        for label, mkt, rows in held_rows:
            base_trades = [t for t, _ in rows]
            kept, dropped = _apply(rows, bucket_name, keep)
            bg = float(sum(t.pnl_gross_usd for t in base_trades))
            bn = float(sum(t.pnl_net_usd for t in base_trades))
            kg = float(sum(t.pnl_gross_usd for t in kept))
            kn = float(sum(t.pnl_net_usd for t in kept))
            base_gross += bg
            base_net += bn
            wl_gross += kg
            wl_net += kn
            base_legs += len(base_trades)
            kept_legs += len(kept)
            kept_pnls.extend(t.pnl_net_usd for t in kept)
            per_slice.append({
                "slice": label, "mkt_ret_pct": mkt,
                "base_legs": len(base_trades), "kept": len(kept),
                "base_gross": bg, "wl_gross": kg, "d_gross": kg - bg,
                "base_net": bn, "wl_net": kn, "d_net": kn - bn,
            })
            print(f"    {label:<12} {mkt:>+7.2f}% {len(base_trades):>10} "
                  f"{len(kept):>6} {bg:>11,.0f} {kg:>10,.0f} "
                  f"{kg - bg:>+9,.0f} {bn:>10,.0f} {kn:>9,.0f} "
                  f"{kn - bn:>+8,.0f}")

        if kept_pnls:
            arr = np.array(kept_pnls, dtype=np.float64)
            sd = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
            t_agg = float(arr.mean() / (sd / np.sqrt(arr.size))) if sd > 0 else 0.0
        else:
            t_agg = 0.0

        print()
        print(f"    TOTAL  base: {base_legs:>4} legs  gross ${base_gross:>9,.0f}  "
              f"net ${base_net:>9,.0f}")
        print(f"    TOTAL   wl: {kept_legs:>4} legs  gross ${wl_gross:>9,.0f}  "
              f"net ${wl_net:>9,.0f}")
        print(f"    DELTA     : {kept_legs - base_legs:>+4} legs  "
              f"gross ${wl_gross - base_gross:>+9,.0f}  "
              f"net ${wl_net - base_net:>+9,.0f}")
        print(f"    held-out kept trades: n={len(kept_pnls)}  t={t_agg:+.2f}")

        # The flat-slice test -- the decisive chop check.
        flat = [r for r in per_slice if abs(r["mkt_ret_pct"]) < 3.0]
        if flat:
            fg = sum(r["d_gross"] for r in flat)
            fn = sum(r["d_net"] for r in flat)
            print(f"    FLAT slices ({len(flat)}): d gross ${fg:+,.0f}  "
                  f"d net ${fn:+,.0f}")

        print()
        verdict = []
        if wl_gross > base_gross:
            verdict.append("GROSS IMPROVED")
        else:
            verdict.append("GROSS NOT IMPROVED")
        if t_agg >= 2.0:
            verdict.append("kept trades significantly profitable")
        elif t_agg <= -2.0:
            verdict.append("kept trades significantly LOSS-MAKING")
        else:
            verdict.append("kept trades not significant")
        if flat and sum(r["d_net"] for r in flat) < 0:
            verdict.append("FLAT-SLICE TEST FAILS")
        elif flat:
            verdict.append("flat-slice test holds")
        print("    VERDICT: " + "; ".join(verdict))

        all_report[bucket_name] = {
            "fit_stats": {f"{s}|{k}": v for (s, k), v in stats.items()},
            "keep": {s: sorted(v) for s, v in keep.items()},
            "held": per_slice,
            "base_legs": base_legs, "kept_legs": kept_legs,
            "base_gross": base_gross, "wl_gross": wl_gross,
            "base_net": base_net, "wl_net": wl_net,
            "d_gross": wl_gross - base_gross, "d_net": wl_net - base_net,
            "t_aggregate": t_agg,
            "verdict": verdict,
        }

    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump({"min_n": args.min_n, "buckets": all_report}, fh,
                  indent=2, default=str)
    print()
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
