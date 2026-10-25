"""Is the strategy donating to the spread on sub-minute trades?

The concern: at 1s XAUUSD the live spread is $0.40/oz round-trip and
the median 1s ATR is $0.098. The canonical FVG floor is
``fvg_min_zone_usd = $0.30`` -- NARROWER THAN THE SPREAD. A zone the
market cannot trade through is not a level, it is a rounding error,
and any "trade" resolved inside it is a pure spread donation.

This probe answers, on REAL canonical trades:
  1. hold-time distribution -- what share exit within 1s/5s/30s/60s?
  2. zone width vs spread vs 1s ATR
  3. TP/SL distance vs spread (a target inside the spread is unreachable
     without paying the fee first)
  4. net P&L of the fast trades vs the slow ones
  5. fee as a share of gross, bucketed by hold time

Run:
    python -m runtime.diagnostics.probe_hold_time
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    DETECTOR_FVG_MIN_ZONE_USD,
    FOREX_LOTS,
    FOREX_SPREAD_PER_OZ_USD,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import (  # noqa: E402
    ArtifactCache,
    compute_metrics,
    load_bars,
)

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
START, END = "2025-01-01", "2025-02-01"

# round trip: commission + spread on the canonical 1 oz position
RT_USD = FOREX_LOTS * 7.0 + FOREX_LOTS * 100.0 * FOREX_SPREAD_PER_OZ_USD


def bucket_report(holds: np.ndarray, gross: np.ndarray, net: np.ndarray) -> None:
    edges = [0, 1, 5, 15, 30, 60, 300, 1800, np.inf]
    names = ["<1s", "1-5s", "5-15s", "15-30s", "30-60s", "1-5m", "5-30m", ">30m"]
    print(f"{'bucket':>8} {'n':>7} {'share':>8} {'gross':>12} {'fees':>10} {'net':>12} {'EV':>9}")
    for lo, hi, nm in zip(edges[:-1], edges[1:], names):
        m = (holds > lo) & (holds <= hi) if lo > 0 else holds <= hi
        n = int(m.sum())
        if n == 0:
            print(f"{nm:>8} {0:>7} {0:>8.2%} {'-':>12} {'-':>10} {'-':>12} {'-':>9}")
            continue
        g = float(gross[m].sum())
        fe = float(-net[m].sum()) - g  # fees = gross - net
        nt = float(net[m].sum())
        print(f"{nm:>8} {n:>7} {n / len(holds):>8.2%} {g:>12,.2f} {fe:>10,.2f} {nt:>12,.2f} {nt / n:>9.3f}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--scope", default="hold_time")
    args = ap.parse_args()

    print(f"=== hold-time / spread-donation probe ({args.scope}) ===", flush=True)
    print(f"spread=${FOREX_SPREAD_PER_OZ_USD:.2f}/oz round-trip  "
          f"commission=0.01*7=${FOREX_LOTS * 7.0:.2f}  TOTAL=${RT_USD:.2f}/trade")
    print(f"canonical fvg_min_zone_usd=${DETECTOR_FVG_MIN_ZONE_USD:.2f}"
          f"   <-- vs spread ${FOREX_SPREAD_PER_OZ_USD:.2f}"
          f"  {'*** NARROWER THAN SPREAD ***' if DETECTOR_FVG_MIN_ZONE_USD < FOREX_SPREAD_PER_OZ_USD else 'ok'}\n", flush=True)

    bars = load_bars(DATA, START, END)
    art = ArtifactCache(DATA, args.scope, START, END).build_or_load(bars)

    # CRITICAL: swing_cap / p95_cap are ARGS TO build_engine_kwargs --
    # mutating the returned dict afterwards silently does nothing.
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"],
        p95_cap=art["p95_cap"],
    )
    print(f"spot=${spot:,.2f}  swing_cap={'yes' if ek.get('swing_cap') else 'NO'}  "
          f"p95_cap={'yes' if ek.get('p95_cap') else 'NO'}", flush=True)

    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_LOTS * 100.0,
        engine_kwargs=ek,
    )
    metrics = compute_metrics(res)
    tr = res.trades
    if not tr:
        print("no trades")
        return

    holds = np.array([t.hold_secs for t in tr], dtype=np.float64)
    gross = np.array([t.pnl_gross_usd for t in tr], dtype=np.float64)
    net = np.array([t.pnl_net_usd for t in tr], dtype=np.float64)
    # METRIC NAMING TRAP -- two of them, both silent.
    #
    # (1) ``target_price`` is NOT the leg's own target. On a TP1 partial
    #     exit the engine logs ``target_price=tp2_price``
    #     (vp_multi_target_engine.py:2307) and on a runner exit it logs
    #     the relabelled runner level (line 2466). So ``target_price``
    #     is the RUNNER target on every leg, and reading it as "the
    #     take-profit" yields a median of ~$70 when the real TP1
    #     geometry is ~$20.
    #
    # (2) ``stop_price`` on a TP1 leg is the post-breakeven entry, and
    #     ``sl_distance_usd`` there is 0.0 for the same reason.
    #
    # The honest per-leg distances are ``initial_sl_distance_usd`` and
    # ``tp_distance_usd`` (captured at fill / at the leg's own TP).
    zw = np.array([t.zone_width for t in tr], dtype=np.float64)
    sl = np.array([abs(t.initial_sl_distance_usd) for t in tr], dtype=np.float64)
    tp = np.array([abs(t.tp_distance_usd) for t in tr], dtype=np.float64)
    # ``target_price_2`` is 0.0 on 96.7% of legs (only the two-target
    # branch sets it). A naive ``|target_price_2 - entry_price|`` on a
    # 0.0 sentinel yields ~SPOT ($2,693 = $2,706 spot), which looks
    # exactly like an absurd target. Always mask the sentinel.
    e = np.array([t.entry_price for t in tr], dtype=np.float64)
    t2 = np.array([t.target_price_2 for t in tr], dtype=np.float64)
    runner = np.where(t2 > 0.0, np.abs(t2 - e), np.nan)
    tp1_leg = np.array([abs(t.tp_distance_usd) for t in tr if t.exit_reason == "tp1"], dtype=np.float64)

    print(f"trades={len(tr)}  net=${net.sum():,.2f}  gross=${gross.sum():,.2f}\n", flush=True)

    print("--- 1. hold-time distribution ---", flush=True)
    print(f"  p1={np.percentile(holds, 1):.2f}s  p10={np.percentile(holds, 10):.2f}s  "
          f"p50={np.percentile(holds, 50):.2f}s  p90={np.percentile(holds, 90):.2f}s  "
          f"max={holds.max():.0f}s")
    for e in (1, 5, 15, 30, 60, 300):
        print(f"  share holding <= {e:>3}s : {np.mean(holds <= e):>7.2%}")
    print(flush=True)

    print("--- 2. bucket economics ---", flush=True)
    bucket_report(holds, gross, net)
    print(flush=True)

    print("--- 3. geometry vs spread ---", flush=True)
    def d(name, a):
        a = a[np.isfinite(a)]
        print(f"  {name:<22} p5={np.percentile(a, 5):>8.3f}  p25={np.percentile(a, 25):>8.3f}  "
              f"p50={np.percentile(a, 50):>8.3f}  p75={np.percentile(a, 75):>8.3f}  p95={np.percentile(a, 95):>8.3f}")
    d("zone_width ($)", zw)
    d("SL initial ($)", sl)
    d("leg TP ($)", tp)
    if np.isfinite(runner).any():
        d("runner TP2 ($)", runner)
        print(f"  (target_price_2 unset on {np.mean(~np.isfinite(runner)):.1%} of legs -- "
              "0.0 sentinel masked, not a target)")
    if tp1_leg.size:
        d("TP1 partial ($)", tp1_leg)
    print("\n  Uses initial_sl_distance_usd / tp_distance_usd, NOT")
    print("  target_price (which logs the RUNNER level on every leg).")
    print(f"\n  spread (round-trip) = ${FOREX_SPREAD_PER_OZ_USD:.2f}")
    print(f"  p(zone_width  < spread) = {np.mean(zw < FOREX_SPREAD_PER_OZ_USD):.2%}")
    print(f"  p(SL distance < spread) = {np.mean(sl < FOREX_SPREAD_PER_OZ_USD):.2%}")
    print(f"  p(TP distance < spread) = {np.mean(tp < FOREX_SPREAD_PER_OZ_USD):.2%}")
    print(f"  p(TP distance < 1 spread) = {np.mean(tp < 2 * FOREX_SPREAD_PER_OZ_USD):.2%}")
    print(f"  p(SL distance < 1 spread) = {np.mean(sl < 2 * FOREX_SPREAD_PER_OZ_USD):.2%}")
    print(f"\n  legTP/SL median ratio = {np.median(tp) / max(np.median(sl), 1e-9):.3f}")
    print(f"  breakeven win rate = {np.median(sl) / max(np.median(sl) + np.median(tp), 1e-9):.2%}"
          "   (vs $0.47 fee per side-pair)")
    print(flush=True)

    print("--- 4. fast vs slow trades ---", flush=True)
    for cut in (5, 15, 60):
        f = holds <= cut
        s = holds > cut
        if f.sum() == 0 or s.sum() == 0:
            continue
        print(f"  hold <= {cut:>3}s : n={int(f.sum()):>6}  net=${net[f].sum():>12,.2f}  "
              f"EV=${net[f].sum() / f.sum():>8.3f}  hit={np.mean(net[f] > 0):.2%}")
        print(f"  hold >  {cut:>3}s : n={int(s.sum()):>6}  net=${net[s].sum():>12,.2f}  "
              f"EV=${net[s].sum() / s.sum():>8.3f}  hit={np.mean(net[s] > 0):.2%}")
    print(flush=True)

    print("--- 5. what actually ends the trade ---", flush=True)
    reasons: dict[str, int] = {}
    for t in tr:
        reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
    for k, v in sorted(reasons.items(), key=lambda kv: -kv[1]):
        print(f"  {k:<10} {v:>7}  {v / len(tr):>7.2%}")
    print(flush=True)

    print("--- 6. verdict inputs ---", flush=True)
    sub60 = holds <= 60
    print(f"  trades held <= 60s      : {sub60.sum():>7} ({sub60.mean():.2%})")
    print(f"  their net               : ${net[sub60].sum():,.2f}")
    print(f"  their fees              : ${float((gross[sub60] - net[sub60]).sum()):,.2f}")
    print(f"  net if they were removed : ${net[~sub60].sum():,.2f}")
    tp_too_close = tp < FOREX_SPREAD_PER_OZ_USD
    print(f"\n  trades with TP inside the spread: {tp_too_close.sum()} "
          f"({tp_too_close.mean():.2%}), net=${net[tp_too_close].sum():,.2f}")


if __name__ == "__main__":
    main()