"""DECISIVE — actual P&L vs. simultaneous-close P&L for every hedged pair.

``contra_close_sl_suppression.py`` proved the MECHANISM: at 35 exits
across 15 months, an opposite-side position was open, and 33 of those
pairs were locked in a GAIN worth $269.58 in total.

This script asks the only question that matters for P&L:

    Did the two legs ACTUALLY lose that money, or did they get it
    back some other way?

For every opposite-side overlapping pair it computes:

  ACTUAL     -- the combined REALISED net P&L of both positions, as
                 the engine booked them (each leg's own exit).

  SIMULTANEOUS -- the counterfactual the user describes: close BOTH
                 legs' overlapping quantity together at ONE price the
                 moment the first of them closes. The combined gross
                 of the matched quantity is (S - L) * overlap, and the
                 fees are the two round trips the engine would pay
                 anyway.

  DELTA     -- SIMULTANEOUS - ACTUAL, on the OVERLAPPING quantity
                 only (the non-overlapping residual of the bigger leg
                 is left alone in both arms).

If DELTA is materially positive, the user's premise is confirmed in
P&L terms and a simultaneous-close (or SL-suppression) feature is
worth shipping. If DELTA is ~0 or negative, the legs were getting
the money back on their own and the premise is a misreading of the
ledger.

LEG-LEVEL FRAMING (2026-10-25)
==============================

The monthly/regime paradigm was retired on 2026-10-25 (AGENTS.md
§ "Evaluation methodology — LEG-LEVEL, not month-level"). This
script is the **pair-level** view of the leg ledger: the population
of interest for ``contra_close`` is the set of overlapping
long/short PAIRS, not the set of months, because the feature does
not change which signals are taken — it changes how a pair that IS
taken is resolved.

The report therefore now carries the **leg-level statistics** the
new methodology requires, alongside the monthly table:

* the **paired delta's** mean, standard error and t-stat — the
  decisive number, at n=34 instead of n=3 flat months;
* the **improved / worsened** split;
* the **tail concentration** — what the aggregate becomes without
  its single best pair;
* the **directional split**, asked per pair rather than per month,
  which is the OLD monthly test at the right unit.

The direction split is the one that decides the promotion: a
feature whose paired delta is positive ONLY when the market rose is
a beta proxy, and the pair-level view measures that with far more
power than 3 monthly totals.

Usage:
    python -m runtime.diagnostics.contra_close_actual_vs_simul
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT = Path("runtime/diagnostics/_reports/contra_close_actual_vs_simul.json")
CACHE_ROOT = Path("notebooks/.cache")

MONTHS = [
    "2024-06", "2024-08", "2024-11", "2024-12",
    "2025-01", "2025-03", "2025-05", "2025-06", "2025-09", "2025-10",
    "2025-12", "2026-01", "2026-03", "2026-04", "2026-06",
]

# The engine's per-side fee: one round trip per LEG, charged with that
# leg's own entry price. Derived from the engine's own formula so this
# script cannot drift from it:
#   fee = |exit + entry| * 0.5 * qty * (bps/10000) * 2
def _fee(entry: float, exit_: float, qty: float, bps: float) -> float:
    return abs(exit_ + entry) * 0.5 * qty * (bps / 10000.0) * 2.0


def _month_bounds(ym: str) -> tuple[str, str]:
    y, m = int(ym[:4]), int(ym[5:7])
    ny, nm = (y + 1, 1) if m == 12 else (y, m + 1)
    return f"{ym}-01", f"{ny:04d}-{nm:02d}-01"


def _positions(res) -> list[dict]:
    """One record per POSITION (TP1 legs merged on parent_zone_id).

    The position's SIZE is the SUM of its legs' quantities. A TP1
    position books TWO legs -- the partial close (50%) and the
    remainder (50%) -- so taking the FIRST leg's ``qty_btc`` halves
    the position's size and makes every qty comparison wrong.
    """
    by_parent: dict[int, list] = {}
    for t in res.trades:
        by_parent.setdefault(int(t.parent_zone_id), []).append(t)
    out = []
    for pid, recs in by_parent.items():
        r0 = recs[0]
        out.append({
            "parent": pid,
            "entry_bar": int(r0.entry_bar),
            "exit_bar": max(int(r.exit_bar) for r in recs),
            "direction": int(r0.direction),
            "entry_price": float(r0.entry_price),
            "qty": float(sum(float(r.qty_btc) for r in recs)),
            "exit_reasons": [r.exit_reason for r in recs],
            "gross": sum(float(r.pnl_gross_usd) for r in recs),
            "net": sum(float(r.pnl_net_usd) for r in recs),
            "fee": sum(float(r.fee_usd) for r in recs),
        })
    return out


def _pair_analysis(positions: list[dict], close: np.ndarray,
                    bps: float) -> dict:
    longs = [p for p in positions if p["direction"] > 0]
    shorts = [p for p in positions if p["direction"] < 0]

    rows: list[dict] = []
    for L in longs:
        for S in shorts:
            lo = max(L["entry_bar"], S["entry_bar"])
            hi = min(L["exit_bar"], S["exit_bar"])
            if lo > hi:
                continue
            # The pair lives [lo, hi]. The FIRST exit is the moment
            # the hedge would have been broken.
            if L["exit_bar"] <= S["exit_bar"]:
                first, second = L, S
            else:
                first, second = S, L

            overlap = min(L["qty"], S["qty"])
            if overlap <= 0:
                continue

            # exit price of the FIRST closer
            b = first["exit_bar"]
            px = float(close[b]) if b < len(close) else first["exit_price"]

            # ── ACTUAL: realised net P&L of the overlapping portion.
            # Each leg's realised gross is linear in its own qty, so
            # the overlapping portion's gross is:
            #     leg_gross * (overlap / leg_qty)
            def _portion(p):
                frac = overlap / p["qty"] if p["qty"] > 0 else 0.0
                return p["gross"] * frac, p["fee"] * frac, frac

            gL, fL, fracL = _portion(L)
            gS, fS, fracS = _portion(S)
            actual_net = (gL + gS) - (fL + fS)

            # ── SIMULTANEOUS: close both overlapping quantities at
            # ONE price (px), at the moment the first leg closed.
            #
            # long @ L_entry, short @ S_entry, both qty `overlap`:
            #   gross = (px - L_entry)*overlap + (S_entry - px)*overlap
            #         = (S_entry - L_entry) * overlap      <- CONSTANT
            gross_sim = (S["entry_price"] - L["entry_price"]) * overlap
            # Two round trips -- the same two the status quo pays.
            fee_sim = (_fee(L["entry_price"], px, overlap, bps)
                       + _fee(S["entry_price"], px, overlap, bps))
            sim_net = gross_sim - fee_sim

            delta = sim_net - actual_net
            rows.append({
                "long_entry": L["entry_price"],
                "short_entry": S["entry_price"],
                "spread": S["entry_price"] - L["entry_price"],
                "qty_long": L["qty"],
                "qty_short": S["qty"],
                "overlap": overlap,
                "balanced": abs(L["qty"] - S["qty"]) <= 1e-9,
                "first_exit_bar": b,
                "first_exit_reasons": first["exit_reasons"],
                "actual_net": actual_net,
                "sim_net": sim_net,
                "delta": delta,
                "market_price_at_exit": px,
            })

    def _s(key: str) -> dict:
        if not rows:
            return {"n": 0}
        a = np.asarray([r[key] for r in rows], dtype=np.float64)
        return {
            "n": int(a.size),
            "p50": float(np.percentile(a, 50)),
            "sum": float(a.sum()),
            "min": float(a.min()),
            "max": float(a.max()),
        }

    return {
        "n_pairs": len(rows),
        "actual_net": _s("actual_net"),
        "sim_net": _s("sim_net"),
        "delta": _s("delta"),
        "n_balanced": sum(1 for r in rows if r["balanced"]),
        "n_positive_spread": sum(1 for r in rows if r["spread"] > 0),
        "n_negative_spread": sum(1 for r in rows if r["spread"] < 0),
        "n_delta_positive": sum(1 for r in rows if r["delta"] > 0),
        "n_delta_negative": sum(1 for r in rows if r["delta"] < 0),
        "rows": rows,
    }


def main() -> int:
    OUT.parent.mkdir(parents=True, exist_ok=True)
    rows: dict[str, dict] = {}

    for ym in MONTHS:
        scope = f"c1m_{ym}"
        if not (CACHE_ROOT / scope).exists():
            print(f"  skip {ym}: no cache", flush=True)
            continue
        s, e = _month_bounds(ym)
        bars = load_bars(DATA, s, e)
        if bars.empty:
            continue
        art = ArtifactCache(DATA, scope, s, e).build_or_load(bars)
        spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

        ek = build_engine_kwargs(
            spot=spot, swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=False,
            contra_close=False, contra_close_spread_usd=0.0,
        )
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek,
        )

        close = bars["close"].to_numpy(dtype=np.float64)
        bps = float(ek.get("taker_fee_bps", 0.0))
        positions = _positions(res)
        a = _pair_analysis(positions, close, bps)
        mkt = (close[-1] / close[0] - 1.0) * 100.0
        rows[ym] = {"market_pct": mkt, "positions": len(positions),
                    "legs": len(res.trades), **a}
        print(f"  {ym} mkt={mkt:+6.2f}%  pairs={a['n_pairs']:>3}  "
              f"actual=${a['actual_net'].get('sum',0.0):>9.2f}  "
              f"sim=${a['sim_net'].get('sum',0.0):>9.2f}  "
              f"DELTA=${a['delta'].get('sum',0.0):>9.2f}", flush=True)

        del bars, art
        import gc
        gc.collect()

    print()
    print("=" * 100)
    print("CONTRA CLOSE — ACTUAL vs SIMULTANEOUS-CLOSE (overlapping qty only)")
    print("=" * 100)
    print(f"{'month':<9}{'mkt%':>8}{'pairs':>6}{'actual$':>11}{'simul$':>11}"
          f"{'DELTA$':>11}{'+d':>5}{'-d':>5}")

    for ym, v in rows.items():
        print(f"{ym:<9}{v['market_pct']:>+8.2f}{v['n_pairs']:>6}"
              f"{v['actual_net'].get('sum',0.0):>11.2f}"
              f"{v['sim_net'].get('sum',0.0):>11.2f}"
              f"{v['delta'].get('sum',0.0):>11.2f}"
              f"{v['n_delta_positive']:>5}{v['n_delta_negative']:>5}")

    n_pairs = sum(v["n_pairs"] for v in rows.values())
    act = sum(v["actual_net"].get("sum", 0.0) for v in rows.values())
    sim = sum(v["sim_net"].get("sum", 0.0) for v in rows.values())
    dlt = sum(v["delta"].get("sum", 0.0) for v in rows.values())
    ndp = sum(v["n_delta_positive"] for v in rows.values())
    ndn = sum(v["n_delta_negative"] for v in rows.values())
    nbal = sum(v["n_balanced"] for v in rows.values())
    npos = sum(v["n_positive_spread"] for v in rows.values())
    nneg = sum(v["n_negative_spread"] for v in rows.values())

    print()
    print(f"TOTAL opposite-side overlapping pairs : {n_pairs:,}")
    print(f"    balanced (equal size)                : {nbal:,}")
    print(f"    positive spread (profitable lock)    : {npos:,}")
    print(f"    negative spread (locked loss)        : {nneg:,}")
    print(f"ACTUAL   realised net (overlapping qty) : ${act:,.2f}")
    print(f"SIMUL    net if closed together         : ${sim:,.2f}")
    print(f"DELTA    (simul - actual)               : ${dlt:,.2f}")
    print(f"    pairs improved                      : {ndp}")
    print(f"    pairs worsened                      : {ndn}")
    print()

    # ── LEG-LEVEL (PAIR-LEVEL) STATISTICS ───────────────────────
    # The monthly table above has n=15 rows of totals. The statistics
    # below have n=34 PAIRS, which is the unit the feature acts on.
    pair_rows: list[dict] = []
    for ym, v in rows.items():
        for r in v["rows"]:
            r = dict(r)
            r["ym"] = ym
            r["mkt"] = v["market_pct"]
            pair_rows.append(r)

    leg_stats: dict = {}
    if pair_rows:
        d = np.asarray([r["delta"] for r in pair_rows], dtype=np.float64)
        mk = np.asarray([r["mkt"] for r in pair_rows], dtype=np.float64)
        sp = np.asarray([r["spread"] for r in pair_rows], dtype=np.float64)
        n_p = int(d.size)
        mean_d = float(d.mean())
        se_d = float(d.std(ddof=1) / np.sqrt(n_p)) if n_p > 1 else float("nan")
        t_d = mean_d / se_d if se_d and np.isfinite(se_d) and se_d > 0 else 0.0
        order = np.argsort(-d)
        top1 = float(d[order[0]])
        corr = (float(np.corrcoef(mk, d)[0, 1])
                if np.std(mk) > 0 and np.std(d) > 0 else float("nan"))

        print("-" * 100)
        print("PAIR-LEVEL (LEG-LEVEL) STATISTICS — n = pairs, not months")
        print("-" * 100)
        print(f"  n pairs                  : {n_p}")
        print(f"  paired delta   mean       : ${mean_d:+,.4f}  "
              f"(se ${se_d:,.4f})   t = {t_d:+.2f}")
        print(f"  paired delta   total      : ${d.sum():+,.2f}")
        print(f"  improved / worsened       : {int((d > 0).sum())} / "
              f"{int((d < 0).sum())}")
        print(f"  total WITHOUT best pair   : ${d.sum() - top1:+,.2f}")
        print(f"  corr(delta, market move)  : {corr:+.4f}")
        print()

        print("  DIRECTIONAL SPLIT (per PAIR, by the month's market move):")
        for label, pred in (("UP   (mkt > +1%)", lambda m: m > 1.0),
                            ("DOWN (mkt < -1%)", lambda m: m < -1.0),
                            ("FLAT (|mkt| <= 1%)", lambda m: -1.0 <= m <= 1.0)):
            sel = [r for r in pair_rows if pred(r["mkt"])]
            if not sel:
                print(f"    {label:<22} n=0")
                continue
            dd = np.asarray([r["delta"] for r in sel], dtype=np.float64)
            print(f"    {label:<22} n={len(sel):>3}  "
                  f"total=${dd.sum():>8.2f}  mean=${dd.mean():>7.3f}  "
                  f"improved={int((dd > 0).sum())}/{len(dd)}")
        print()

        # The spread is the trigger variable for the shipped feature.
        print("  DELTA BY LOCKED SPREAD (the trigger variable):")
        for lo, hi in ((0.0, 1.0), (1.0, 2.0), (2.0, 5.0),
                       (5.0, 10.0), (10.0, 1e9)):
            sel = [r for r in pair_rows if lo <= r["spread"] < hi]
            if not sel:
                continue
            dd = np.asarray([r["delta"] for r in sel], dtype=np.float64)
            rng = f"${lo:g}-${hi:g}" if hi < 1e8 else f"${lo:g}+"
            print(f"    spread {rng:<12} n={len(sel):>3}  "
                  f"total=${dd.sum():>8.2f}  mean=${dd.mean():>7.3f}")
        print()

        leg_stats = {
            "n_pairs": n_p,
            "mean_delta": mean_d,
            "se_delta": se_d,
            "t_delta": t_d,
            "total_delta": float(d.sum()),
            "total_delta_ex_top1": float(d.sum() - top1),
            "n_improved": int((d > 0).sum()),
            "n_worsened": int((d < 0).sum()),
            "corr_delta_market": corr,
            "mean_spread": float(sp.mean()),
        }

    print("-" * 100)
    if dlt > 0:
        print(f"VERDICT: closing simultaneously would have banked "
              f"${dlt:,.2f} MORE across {ndp} pairs.")
    else:
        print(f"VERDICT: closing simultaneously would have LOST "
              f"${abs(dlt):,.2f} vs the status quo.")

    OUT.write_text(json.dumps(
        {"per_month": rows, "pair_level": leg_stats},
        indent=2, default=float), encoding="utf-8")
    print(f"\nwrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
