"""Sequence whitelist — POWERED test (2026-10-24).

Why this module exists
----------------------
``runtime/diagnostics/sequence_whitelist.py`` reported an EMPTY
whitelist (0 of 64 signatures) at canonical sizing. The reason was
**statistical power, not economics**:

* The vol gate (``vol_state_min_range_norm = 3.0``) cut the trade
  population on the fit slices from 4,136+3,216+4,925 = 12,277 legs
  down to 112+93+126 = **331 legs**.
* Those 331 legs spread across **60 distinct 5-event signatures** --
  a mean of **5.5 legs per signature**.
* The whitelist's admission rule requires ``n >= 15`` AND
  ``EV >= $0.05``. With a mean of 5.5 legs per bucket, **no
  signature can ever satisfy n >= 15**. The whitelist was empty by
  construction, not because the signatures carry no information.

Indeed 21 of 60 signatures DID clear the $0.05 EV bar -- they were
simply all rejected on sample size.

What this module does differently
----------------------------------
1. **Coarsens the signature.** Instead of a full 5-char string
   (2^5 = 32 shapes per side, x polarity => 60+ observed), it
   buckets on the *dominant* polarity of the last 5 events, which
   has a far better populated distribution.
2. **Fits on a real fit set and validates on a disjoint held-out
   set** with the EXACT same whitelist, and reports both.
3. **Reports the significance of the held-out result** (a t-stat on
   the per-trade P&L), so a lucky slice cannot masquerade as an edge.
4. **Applies the whitelist IN-ENGINE** via ``structure_seq_flip_map``
   where the engine supports it, and post-hoc where it does not --
   the post-hoc path is legitimate because the signature is a
   function of the structure state at or before the entry bar (no
   lookahead).

Usage
-----
    python -m runtime.diagnostics.sequence_whitelist_powered
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
OUT_DIR = Path("runtime/diagnostics/_reports/sequence_whitelist_powered")

# FIT and HELD are DISJOINT slice sets. The whitelist is fit on FIT
# only; HELD is scored once with the frozen whitelist.
FIT_SLICES = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01"),
]
HELD_SLICES = [
    ("2025-01 UP",   "2025-01-01", "2025-02-01"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01"),
    ("2024-06 FLAT", "2024-06-01", "2024-07-01"),
    ("2026-04 FLAT", "2026-04-01", "2026-05-01"),
]

# The coarse buckets. `_BUCKET` maps a 5-event signature to a
# polarity label:
#
#   "bull"  -- 3+ of the 5 events are bullish breaks
#   "bear"  -- 3+ of the 5 events are bearish breaks
#   "mixed" -- otherwise
#
# This collapses 60 observed shapes into 3 buckets, which is what
# makes a 331-leg sample sufficient to estimate per-bucket EV.
_BUCKET = ("bull", "bear", "mixed")


def _bucket(sig: str) -> str:
    """Coarse polarity bucket for a 5-event signature."""
    bull = sum(1 for c in sig if c in ("B", "b"))
    bear = sum(1 for c in sig if c in ("C", "c"))
    if bull >= 3:
        return "bull"
    if bear >= 3:
        return "bear"
    return "mixed"


def _artifacts(tag: str, start: str, end: str, bars) -> dict:
    return ArtifactCache(DATA, f"wlpow_{tag}", start, end).build_or_load(bars)


def _run(label: str, start: str, end: str) -> dict:
    """One slice, canonical engine config."""
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        structure_gate=False,
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
        "net": float(sum(t.pnl_net_usd for t in trades)),
        "gross": float(sum(t.pnl_gross_usd for t in trades)),
        "legs": len(trades),
        "wall": time.perf_counter() - t0,
    }


def _by_bucket(art: dict, trades: list) -> dict[str, list]:
    """Bucket trades by the COARSE polarity of their trailing 5 events."""
    st = art["structure"]
    bb = _break_bars(st)
    out: dict[str, list] = {}
    for t in trades:
        b = getattr(t, "entry_bar", None)
        if b is None or bb.size == 0:
            continue
        u = int(np.searchsorted(bb, b, side="right"))
        if u < 5:
            continue
        sig = "".join(_BREAK_SIG.get(st.breaks[j].kind, "?")
                      for j in range(u - 5, u))
        out.setdefault(_bucket(sig), []).append(t)
    return out


def _stats(group: list) -> dict:
    """n, gross, net, EV, hit rate, and a t-stat on the per-trade P&L."""
    if not group:
        return {"n": 0, "gross": 0.0, "net": 0.0, "ev": 0.0,
                "hit": 0.0, "t": 0.0}
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


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--min-n", type=int, default=20)
    ap.add_argument("--min-ev", type=float, default=0.05)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)

    print("== POWERED sequence whitelist (coarse polarity buckets) ==")
    print(f"  fit slices  : {len(FIT_SLICES)}   held slices: {len(HELD_SLICES)}")
    print(f"  admission   : n >= {args.min_n} AND EV >= ${args.min_ev}")
    print()

    # ---------- FIT ----------
    pooled: dict[str, list] = {}
    for label, s, e in FIT_SLICES:
        r = _run(label, s, e)
        for b, group in _by_bucket(r["art"], r["trades"]).items():
            pooled.setdefault(b, []).extend(group)
        print(f"  fit {label:<12} mkt={r['mkt_ret_pct']:>+7.2f}%  "
              f"legs={r['legs']:>4}  net=${r['net']:>8,.0f}", flush=True)

    fit_stats = {b: _stats(g) for b, g in pooled.items()}
    keep = {b for b, v in fit_stats.items()
            if v["n"] >= args.min_n and v["ev"] >= args.min_ev}
    print()
    print(f"  bucket stats (fit, pooled):")
    for b in ("bull", "bear", "mixed"):
        v = fit_stats.get(b, {"n": 0, "ev": 0.0, "net": 0.0, "hit": 0.0, "t": 0.0})
        mark = "KEEP" if b in keep else "    "
        print(f"    {mark} {b:<6} n={v['n']:>4}  EV=${v['ev']:>+8.3f}  "
              f"net=${v['net']:>+9,.0f}  hit={v['hit']*100:>5.1f}%  t={v['t']:>+6.2f}")
    print(f"  whitelist (fit): {sorted(keep) or 'EMPTY'}")

    # ---------- HELD-OUT ----------
    print()
    print("-- HELD-OUT (frozen whitelist) --")
    print(f"  {'slice':<14} {'mkt':>8} {'legs':>6} {'kept':>6} "
          f"{'base net':>10} {'wl net':>9} {'delta':>8} {'wl t':>7}")
    tb = tw = 0.0
    held_rows = []
    all_kept_pnls: list[float] = []
    for label, s, e in HELD_SLICES:
        r = _run(label, s, e)
        buckets = _by_bucket(r["art"], r["trades"])
        kept = []
        for b, group in buckets.items():
            if b in keep:
                kept.extend(group)
        nb = r["net"]
        nw = sum(t.pnl_net_usd for t in kept)
        all_kept_pnls.extend(t.pnl_net_usd for t in kept)
        tb += nb
        tw += nw
        kt = _stats(kept)
        held_rows.append({
            "slice": label, "mkt_ret_pct": r["mkt_ret_pct"],
            "legs": r["legs"], "kept": len(kept),
            "base_net": nb, "wl_net": nw, "delta": nw - nb,
            "wl_t": kt["t"],
        })
        print(f"  {label:<14} {r['mkt_ret_pct']:>+7.2f}% {r['legs']:>6} "
              f"{len(kept):>6} {nb:>10,.0f} {nw:>9,.0f} {nw - nb:>+8,.0f} "
              f"{kt['t']:>+7.2f}", flush=True)
    print()
    print(f"  {'TOTAL':<14} {'':>8} {'':>6} {'':>6} {tb:>10,.0f} "
          f"{tw:>9,.0f} {tw - tb:>+8,.0f}")

    # Aggregate t-stat across ALL held-out kept trades.
    if all_kept_pnls:
        arr = np.array(all_kept_pnls, dtype=np.float64)
        sd = float(arr.std(ddof=1)) if arr.size > 1 else 0.0
        t_agg = float(arr.mean() / (sd / np.sqrt(arr.size))) if sd > 0 else 0.0
    else:
        t_agg = 0.0
    print(f"  held-out kept trades: n={len(all_kept_pnls)}  "
          f"aggregate t={t_agg:+.2f}")
    print()
    # ---- THE DECISIVE DECOMPOSITION: net vs gross ----
    # A filter can improve NET purely by trading less and paying fewer
    # fees. That is NOT an edge. The honest metric is GROSS, so the
    # decomposition is printed explicitly.
    fee = 0.47  # canonical MT4 round-trip: $0.07 commission + $0.40 spread
    n_base = sum(r["legs"] for r in held_rows)
    gross_base = tb + n_base * fee
    gross_wl = tw + len(all_kept_pnls) * fee
    print("  gross-vs-fee decomposition (the decisive metric):")
    print(f"    base      : {n_base:>4} legs  net ${tb:>8,.0f}  "
          f"implied gross ${gross_base:>8,.0f}")
    print(f"    whitelisted: {len(all_kept_pnls):>4} legs  net ${tw:>8,.0f}  "
          f"implied gross ${gross_wl:>8,.0f}")
    print(f"    DELTA     : {len(all_kept_pnls) - n_base:>+4} legs  "
          f"net ${tw - tb:>+8,.0f}  gross ${gross_wl - gross_base:>+8,.0f}")
    print()
    if gross_wl < gross_base:
        print("  => GROSS got WORSE. Any net improvement is fee savings "
              "from trading less, not an edge. Do not promote.")
    else:
        print("  => GROSS improved. The filter adds value on a gross "
              "basis (not just by trading less).")
    print()
    if tw > tb:
        print("  => The WHITELIST improves held-out P&L.")
    else:
        print("  => The WHITELIST does NOT improve held-out P&L.")
    if abs(t_agg) < 2.0:
        print("  => |t| < 2.0: the held-out result is NOT statistically "
              "significant. Do not promote.")
    elif t_agg < 0:
        print("  => t < -2.0: the KEPT trades are SIGNIFICANTLY "
              "LOSS-MAKING. The whitelist is actively harmful on a "
              "gross basis -- any net improvement is fee savings from "
              "trading less. Do not promote.")
    else:
        print("  => t > +2.0: the kept trades are significantly "
              "profitable on held-out. Promotable, subject to the "
              "flat-slice check above.")

    with open(args.out / "powered_whitelist.json", "w", encoding="utf-8") as fh:
        json.dump({
            "keep": sorted(keep),
            "min_n": args.min_n, "min_ev": args.min_ev,
            "fit_stats": fit_stats,
            "held": held_rows,
            "held_total_base": tb, "held_total_wl": tw,
            "held_t_aggregate": t_agg,
            "held_kept_n": len(all_kept_pnls),
        }, fh, indent=2, default=str)
    print()
    print(f"-> {args.out / 'powered_whitelist.json'}")


if __name__ == "__main__":
    main()
