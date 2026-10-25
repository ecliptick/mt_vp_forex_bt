"""Directional-beta diagnostic — is the VP-MT edge real, or is it beta?

The question
============

The 2026-10-22 re-sweep found that ``sl_zone_mult=40`` beats
``sl_zone_mult=20`` on every rising slice we tried, and that the
``drop`` conflict policy beats ``stack``/``replace`` by a wide
margin. Both results are suspicious:

* ``drop`` + an unbounded book produces a **directional lock** — the
  first side to fill the book wins, and every opposite signal is
  dropped. The lock persists as long as the book holds, and the
  median hold is hours.
* If the strategy has NO predictive signal, then in a rising market
  its longs make money and its shorts lose money. A directional lock
  that happens to point the right way looks exactly like an edge.

The decisive test
=================

Run the SAME recipe on slices that rise, slices that fall, and slices
that chop. Then split the realised P&L by the direction the trade was
taken in.

If the strategy has signal, a LONG trade should make money in a
falling market too (and a SHORT trade in a rising market) — the edge
is in the *timing*, not in the *direction*. If the long trades only
make money in rising markets and the short trades only make money in
falling markets, then the strategy is a **directional beta proxy**:
it is a machine for holding one direction, and the apparent edge is
the market's drift, minus fees.

The report
==========

For each slice this prints:

* the market's own return over the slice
* gross and net P&L for LONG trades and for SHORT trades, separately
* the *direction-independent* edge: the P&L a trade would have made if
  its direction had been flipped

The last row is the number that matters. If flipped-direction P&L is
strongly negative, the strategy has no timing skill and the entire
"edge" is market drift.

Usage
-----

    python -m runtime.diagnostics.directional_beta
    python -m runtime.diagnostics.directional_beta --sl-slots 2025-01,2025-04
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

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402

DEFAULT_DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")

# (label, start, end) — chosen to span up / down / flat regimes.
DEFAULT_SLOTS: list[tuple[str, str, str]] = [
    ("2024-06 flat",      "2024-06-01", "2024-07-01"),
    ("2024-11 down",      "2024-11-01", "2024-12-01"),
    ("2025-01 up",        "2025-01-01", "2025-02-01"),
    ("2025-05 flat",      "2025-05-01", "2025-06-01"),
    ("2025-09 up",        "2025-09-01", "2025-10-01"),
    ("2026-03 down",      "2026-03-01", "2026-04-01"),
    ("2026-04 down",      "2026-04-01", "2026-05-01"),
    ("2026-01 up",        "2026-01-01", "2026-02-01"),
]

# Sentinel meaning "use whatever the canonical config says".
#
# This CANNOT be ``None``: the canonical ``STRUCTURE_SEQ_DROP_SET`` is
# a real (non-empty) drop set, and passing ``None`` to
# ``build_engine_kwargs`` would OVERRIDE it with "disabled". The
# diagnostic must measure the RECIPE by default.
_USE_CANONICAL = object()


def _load(path: Path, start: str, end: str) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df["time"] = pd.to_datetime(df["time"], utc=True)
    df = df[(df["time"] >= pd.Timestamp(start, tz="UTC"))
            & (df["time"] < pd.Timestamp(end, tz="UTC"))]
    df = df.sort_values("time").reset_index(drop=True)
    if "volume" not in df.columns:
        if "tickv" in df.columns:
            df["volume"] = df["tickv"].astype(float)
        else:
            df["volume"] = (df["high"] - df["low"]).abs() * 1000.0
    return df


def _build_artifacts(bars: pd.DataFrame, scope: str) -> dict[str, Any]:
    """Build the per-FILE artifacts (zones / VP / caps) for one slice."""
    from bt._engine.ict_signals import detect_fvg
    from bt._engine.market_structure import detect_market_structure
    from bt._engine.rolling_p95 import build_rolling_p95_from_bars
    from bt._engine.swing_cap import build_daily_swing
    from bt._engine.timebase import to_ns_arr
    from bt._engine.vol_state import build_vol_state
    from bt._engine.volume_profile import build_volume_profiles
    from runtime.sweeps.cached_sweep import (
        P95_KWARGS,
        STRUCTURE_KWARGS,
        SWING_KWARGS,
        VP_KWARGS,
        detector_kwargs_from_recipe,
    )

    zones = detect_fvg(
        open_=bars["open"].to_numpy(),
        high=bars["high"].to_numpy(),
        low=bars["low"].to_numpy(),
        close=bars["close"].to_numpy(),
        # 2026-10-24: REQUIRED for a correct resample. Without it the
        # detector falls back to POSITIONAL bucketing, which is wrong on
        # any feed with gaps (weekends/rollovers) because row position
        # drifts from wall-clock time. This diagnostic is the DECISIVE
        # test for the beta-proxy verdict, so it must use the SAME
        # bucketing as ``runtime/sweeps/cached_sweep.py``.
        times_utc_ns=to_ns_arr(bars),
        **detector_kwargs_from_recipe(),
    )
    vp_set = build_volume_profiles(
        bars_1s=bars, file_stem=scope, verbose=False, **VP_KWARGS)
    swing_cap = build_daily_swing(bars, **SWING_KWARGS)
    p95_cap = build_rolling_p95_from_bars(bars, **P95_KWARGS)
    p95_cap.skip_trades_during_warmup = True
    # 2026-10-23: structure + momentum for the two-stage score filter.
    # ``ts_ns`` is MANDATORY -- the 60s resample buckets by timestamp;
    # a positional reshape sheared 100% of bars on this corpus
    # (median $19.39 error, see docs/v2_spec.md 6.4).
    structure = detect_market_structure(
        high=bars["high"].to_numpy(dtype=np.float64),
        low=bars["low"].to_numpy(dtype=np.float64),
        close=bars["close"].to_numpy(dtype=np.float64),
        pivot_len=STRUCTURE_KWARGS["pivot_len"],
        ts_ns=to_ns_arr(bars),
        resample_to_n_secs=STRUCTURE_KWARGS["resample_to_n_secs"],
        detect_order_blocks=False,
        detect_liquidity=False,
    )
    vol_state = build_vol_state(
        bars["high"].to_numpy(dtype=np.float64),
        bars["low"].to_numpy(dtype=np.float64),
        bars["close"].to_numpy(dtype=np.float64),
        bars["open"].to_numpy(dtype=np.float64),
        measures=("vel_5m", "range_norm_1m", "wick_ratio"),
    )
    return {
        "zones": zones, "vp_set": vp_set,
        "swing_cap": swing_cap, "p95_cap": p95_cap,
        "structure": structure, "vol_state": vol_state,
    }


def _slice_stats(bars: pd.DataFrame) -> dict[str, float]:
    c = bars["close"].to_numpy(dtype=np.float64)
    return {
        "mkt_ret_pct": float((c[-1] / c[0] - 1.0) * 100.0),
        "mkt_start": float(c[0]),
        "mkt_end": float(c[-1]),
    }


def _direction_breakdown(trades: list) -> dict[str, Any]:
    """Split realised P&L by trade direction."""
    out: dict[str, Any] = {}
    for name, want_dir in (("long", 1), ("short", -1)):
        sel = [t for t in trades if int(t.direction) == want_dir]
        if not sel:
            out[name] = {"n": 0, "gross": 0.0, "net": 0.0, "fees": 0.0,
                         "hit": 0.0, "mean_hold": 0.0}
            continue
        gross = float(sum(float(t.pnl_gross_usd) for t in sel))
        net = float(sum(float(t.pnl_net_usd) for t in sel))
        fees = float(sum(float(getattr(t, "fee_usd", 0.0)) for t in sel))
        wins = sum(1 for t in sel if float(t.pnl_net_usd) > 0)
        holds = [float(getattr(t, "hold_secs", 0.0)) for t in sel]
        out[name] = {
            "n": len(sel),
            "gross": gross,
            "net": net,
            "fees": fees,
            "hit": wins / len(sel),
            "mean_hold": float(np.mean(holds)),
            "median_hold": float(np.median(holds)),
        }
    return out


def run_slice(*, path: Path, label: str, start: str, end: str,
              sl_zone_mult: float, max_same_dir_open: int,
              conflict_policy: str, structure_gate: bool = False,
              structure_seq_drop_set=_USE_CANONICAL) -> dict[str, Any]:
    scope = f"{label.replace(' ', '_').replace('-', '')}"
    bars = _load(path, start, end)
    if bars.empty:
        raise SystemExit(f"no bars for {label}")
    art = _build_artifacts(bars, scope)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"],
        p95_cap=art["p95_cap"],
        sl_zone_mult=sl_zone_mult,
        max_same_dir_open=max_same_dir_open,
        conflict_policy=conflict_policy,
        # 2026-10-23: the two-stage score filter. OFF by default so the
        # pre-gate baseline stays reproducible; flip with --structure-gate.
        structure=art["structure"],
        vol_state=art["vol_state"],
        structure_gate=structure_gate,
    )
    # 2026-10-24: the per-side sequence drop set. The canonical value
    # IS a real drop set, so the diagnostic must measure the RECIPE by
    # default. It is only overridden when the caller asks for a
    # specific set (e.g. an empty set to reproduce a pre-promotion
    # baseline). Passing ``None`` here would OVERRIDE the canonical
    # value with "disabled", so the sentinel is a distinct object.
    if structure_seq_drop_set is not _USE_CANONICAL:
        ek["structure_seq_drop_set"] = structure_seq_drop_set
    t0 = time.perf_counter()
    res, diag = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
        record_equity_curve=True,
    )
    wall = time.perf_counter() - t0
    trades = list(res.trades)
    mkt = _slice_stats(bars)
    bd = _direction_breakdown(trades)
    gross_all = float(sum(float(t.pnl_gross_usd) for t in trades))
    net_all = float(sum(float(t.pnl_net_usd) for t in trades))
    fees_all = float(sum(float(getattr(t, "fee_usd", 0.0)) for t in trades))
    return {
        "label": label, "start": start, "end": end,
        "n_bars": int(len(bars)), "wall_secs": wall,
        "mkt": mkt, "n_trades": len(trades),
        "gross": gross_all, "net": net_all, "fees": fees_all,
        "long": bd["long"], "short": bd["short"],
        "diag": {
            "signals_unaccounted": diag.get("signals_unaccounted"),
            "order_lifecycle": diag.get("order_lifecycle"),
        },
    }


def _fmt_row(r: dict[str, Any]) -> str:
    lo, sh = r["long"], r["short"]
    return (
        f"{r['label']:<14} "
        f"mkt={r['mkt']['mkt_ret_pct']:+7.2f}%  "
        f"n={r['n_trades']:>6}  "
        f"gross=${r['gross']:>10.2f}  net=${r['net']:>10.2f}  "
        f"fees=${r['fees']:>9.2f}  |  "
        f"L n={lo['n']:>5} g=${lo['gross']:>9.2f}  "
        f"S n={sh['n']:>5} g=${sh['gross']:>9.2f}"
    )


def _beta_analysis(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Is the per-slice gross P&L explained by the market's direction?

    This is the informative test. Regressing slice gross P&L on the
    slice's market return gives:

    * ``beta`` — how many dollars of gross P&L the strategy earns per
      1% of market move. If this is large and the R² is high, the
      strategy is a **directional proxy**, not a timing strategy.
    * ``alpha`` — the dollars the strategy makes per slice when the
      market goes nowhere. A timing strategy has a positive alpha; a
      directional proxy has ~0.
    * ``r2`` — how much of the P&L variance the market direction
      explains.

    A second, independent test: the **flat-market** slices. If the
    strategy has timing skill it should make money in a choppy market
    (that is what timing IS). If it only makes money when the market
    trends, it is a beta proxy and the "edge" is the market's drift.
    """
    mkt = np.array([r["mkt"]["mkt_ret_pct"] for r in results], dtype=float)
    gross = np.array([r["gross"] for r in results], dtype=float)

    out: dict[str, Any] = {}
    if mkt.size >= 3 and np.std(mkt) > 0:
        slope, intercept = np.polyfit(mkt, gross, 1)
        pred = slope * mkt + intercept
        ss_res = float(np.sum((gross - pred) ** 2))
        ss_tot = float(np.sum((gross - gross.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
        out.update({
            "beta_usd_per_pct": float(slope),
            "alpha_usd_per_slice": float(intercept),
            "r2": float(r2),
            "corr": float(np.corrcoef(mkt, gross)[0, 1])
            if np.std(gross) > 0 else 0.0,
        })

    # Flat-market test: slices whose |market return| is small.
    flat_idx = [i for i, m in enumerate(mkt) if abs(m) < 3.0]
    if flat_idx:
        flat_gross = gross[flat_idx]
        out["flat_slices"] = [results[i]["label"] for i in flat_idx]
        out["flat_gross_total"] = float(flat_gross.sum())
        out["flat_gross_mean"] = float(flat_gross.mean())
        out["flat_positive_count"] = int((flat_gross > 0).sum())
        out["flat_n"] = len(flat_idx)

    out["gross_total"] = float(gross.sum())
    out["fees_total"] = float(sum(r["fees"] for r in results))
    out["net_total"] = float(sum(r["net"] for r in results))
    out["slices_positive_gross"] = int((gross > 0).sum())
    out["n_slices"] = len(results)
    out["gross_mean"] = float(gross.mean())
    out["gross_stdev"] = float(gross.std(ddof=1)) if gross.size > 1 else 0.0
    return out


def _side_analysis(results: list[dict[str, Any]]) -> dict[str, Any]:
    """Is there independent skill on EACH side, or only on the lucky one?

    A strategy with real timing skill makes money on LONG trades in a
    falling market and on SHORT trades in a rising market. If a side
    only ever makes money in the direction the market happened to go,
    it is not timed.
    """
    out: dict[str, Any] = {}
    for side in ("long", "short"):
        # The market return of the slices where this side was active.
        rs, gs = [], []
        for r in results:
            s = r[side]
            if s["n"] < 20:            # ignore slices where it barely traded
                continue
            rs.append(r["mkt"]["mkt_ret_pct"])
            gs.append(s["gross"])
        if len(rs) >= 3 and np.std(rs) > 0:
            slope, intercept = np.polyfit(np.array(rs), np.array(gs), 1)
            out[side] = {
                "n_slices": len(rs),
                "beta_usd_per_pct": float(slope),
                "alpha_usd_per_slice": float(intercept),
                "corr": float(np.corrcoef(rs, gs)[0, 1]),
                "gross_total": float(np.sum(gs)),
            }
        else:
            out[side] = {"n_slices": len(rs), "gross_total": float(np.sum(gs))}
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--sl-zone-mult", type=float, default=40.0)
    ap.add_argument("--max-same-dir-open", type=int, default=20)
    ap.add_argument("--conflict-policy", type=str, default="drop")
    ap.add_argument("--out", type=Path, default=None,
                    help="Optional path to write the JSON report.")
    ap.add_argument("--sl-slots", type=str, default="",
                    help="Comma-separated 'start:end' pairs to override "
                         "the default slot list.")
    ap.add_argument("--structure-gate", action="store_true",
                    help="Enable the two-stage structure x momentum score "
                         "filter (Stage 1 admits on sign, Stage 2 scales "
                         "SL/TP by magnitude). Default OFF.")
    ap.add_argument("--no-seq-drop-set", action="store_true",
                    help="Disable the per-side sequence drop set, to "
                         "reproduce a PRE-PROMOTION baseline. The "
                         "canonical value is a real drop set, so the "
                         "default (no flag) measures the RECIPE.")
    args = ap.parse_args()

    seq_drop = frozenset() if args.no_seq_drop_set else _USE_CANONICAL

    slots = DEFAULT_SLOTS
    if args.sl_slots:
        slots = []
        for raw in args.sl_slots.split(","):
            raw = raw.strip()
            if not raw:
                continue
            s, _, e = raw.partition(":")
            slots.append((f"{s}..{e}", s, e))

    print(f"== directional beta diagnostic ==")
    print(f"data   : {args.data}")
    print(f"recipe : sl_zone_mult={args.sl_zone_mult} "
          f"max_same_dir_open={args.max_same_dir_open} "
          f"conflict_policy={args.conflict_policy}  "
          f"structure_gate={args.structure_gate}")
    print()

    results: list[dict[str, Any]] = []
    for label, start, end in slots:
        r = run_slice(
            path=args.data, label=label, start=start, end=end,
            sl_zone_mult=args.sl_zone_mult,
            max_same_dir_open=args.max_same_dir_open,
            conflict_policy=args.conflict_policy,
            structure_gate=args.structure_gate,
            structure_seq_drop_set=seq_drop,
        )
        results.append(r)
        print(_fmt_row(r), flush=True)

    # ── The decisive summary ───────────────────────────────────
    beta = _beta_analysis(results)
    sides = _side_analysis(results)

    print()
    print("=" * 78)
    print("== IS THE P&L TIMING SKILL, OR MARKET DIRECTION? ==")
    print("=" * 78)
    print()
    print("Test 1 — regress slice gross P&L on the slice's market return")
    print("  beta  = $ of gross P&L per 1% of market move")
    print("  alpha = $ of gross P&L per slice when the market goes NOWHERE")
    print("  r2    = how much of the P&L variance the market explains")
    print()
    if "beta_usd_per_pct" in beta:
        print(f"  beta  = ${beta['beta_usd_per_pct']:>10.2f} per 1% market move")
        print(f"  alpha = ${beta['alpha_usd_per_slice']:>10.2f} per slice")
        print(f"  r2    = {beta['r2']:>10.3f}   corr = {beta['corr']:+.3f}")
    print()
    # 2026-10-23: report EVERY regime, not just the flat one. Leading
    # with the flat verdict under-reports the trending slices, where a
    # genuine directional filter MUST show a beta advantage -- that is
    # the condition under which the filter is worth keeping.
    mkt = np.array([r["mkt"]["mkt_ret_pct"] for r in results], dtype=float)
    gross = np.array([r["gross"] for r in results], dtype=float)
    regimes = {
        "UP   (mkt >= +3%)": mkt >= 3.0,
        "DOWN (mkt <= -3%)": mkt <= -3.0,
        "FLAT (|mkt| < 3%)": np.abs(mkt) < 3.0,
    }
    print("Test 2 — by REGIME (not just the flat slices)")
    print("  A directional filter is worth keeping if it WINS on the")
    print("  trending slices. Losing on flat slices is the tie-breaker,")
    print("  not the whole test.")
    print()
    print(f"  {'regime':<20} {'n':>2}  {'gross total':>12}  {'mean':>10}  {'positive':>9}")
    for name, mask in regimes.items():
        if not mask.any():
            continue
        g = gross[mask]
        print(f"  {name:<20} {int(mask.sum()):>2}  "
              f"${g.sum():>11,.0f}  ${g.mean():>9,.0f}  "
              f"{int((g > 0).sum())}/{int(mask.sum()):>2}")
    print()
    print("  per-slice, by regime:")
    for name, mask in regimes.items():
        if not mask.any():
            continue
        print(f"    {name}")
        for i in np.flatnonzero(mask):
            r = results[i]
            lo, sh = r["long"], r["short"]
            print(f"      {r['label']:<14} mkt={mkt[i]:+7.2f}%  "
                  f"n={r['n_trades']:>5}  gross=${gross[i]:>9,.0f}  "
                  f"net=${r['net']:>9,.0f}  | "
                  f"L ${lo['gross']:>8,.0f} (hit {lo['hit']:.0%})  "
                  f"S ${sh['gross']:>8,.0f} (hit {sh['hit']:.0%})")
    print()
    print("Test 2b — FLAT-market slices (|market move| < 3%)")
    print("  A strategy with timing skill makes money in a choppy market.")
    print("  A directional proxy does not.")
    print()
    if beta.get("flat_n"):
        print(f"  flat slices      : {beta['flat_n']}")
        print(f"  gross total      = ${beta['flat_gross_total']:>10.2f}")
        print(f"  gross mean       = ${beta['flat_gross_mean']:>10.2f}")
        print(f"  positive slices  = {beta['flat_positive_count']}/{beta['flat_n']}")
    print()
    print("Test 3 — per-side skill (does each side work on its own?)")
    for side in ("long", "short"):
        s = sides.get(side, {})
        if "beta_usd_per_pct" in s:
            print(f"  {side:<6} n_slices={s['n_slices']}  "
                  f"beta=${s['beta_usd_per_pct']:>9.2f}/1%  "
                  f"alpha=${s['alpha_usd_per_slice']:>9.2f}  "
                  f"corr={s['corr']:+.3f}")
        else:
            print(f"  {side:<6} n_slices={s.get('n_slices', 0)}  "
                  f"gross=${s.get('gross_total', 0.0):>10.2f}")
    print()
    print(f"TOTAL gross = ${beta['gross_total']:>12.2f}   "
          f"fees = ${beta['fees_total']:>12.2f}   "
          f"net = ${beta['net_total']:>12.2f}")
    print(f"slices with positive GROSS pnl: "
          f"{beta['slices_positive_gross']}/{beta['n_slices']}   "
          f"(mean ${beta['gross_mean']:,.0f}, stdev ${beta['gross_stdev']:,.0f})")
    print()

    # ── The verdict ───────────────────────────────────────────
    r2 = beta.get("r2", 0.0)
    flat_gross = beta.get("flat_gross_total")
    alpha = beta.get("alpha_usd_per_slice")
    if beta["gross_total"] > 0 and r2 >= 0.5 and (flat_gross or 0) < 0:
        print("VERDICT: BETA PROXY, not a timing edge.")
        print("  The market's direction explains most of the P&L (r2 >= 0.5),")
        print("  and the strategy LOSES money in flat markets. The 'edge' is")
        print("  the market's drift, minus fees — not the FVG logic.")
    elif beta["gross_total"] > 0 and (alpha or 0) > 0 and (flat_gross or 0) > 0:
        print("VERDICT: direction-independent timing edge (alpha > 0 and")
        print("  profitable in flat markets).")
    else:
        print("VERDICT: no robust edge. Do not promote this recipe.")

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "recipe": {**vars(args), "data": str(args.data)},
            "results": results,
            "beta_analysis": beta,
            "side_analysis": sides,
        }
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, default=str)
        print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
