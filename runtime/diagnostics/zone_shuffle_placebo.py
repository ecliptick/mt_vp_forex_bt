"""ZONE-SHUFFLE PLACEBO — the decisive test of whether the FVG signal
carries any information about WHERE to enter.

WHY (2026-10-25)
=================

The leg ledger produced two findings that have to be reconciled:

* **The gross P&L is real** (+$510.58 over 1,692 legs, 31 months).
* **The timing component is NEGATIVE** (excess = gross − drift =
  −$155.00, t = −2.96). The gross is 130% explained by the market
  drift of the strategy's own positions.

If the timing component is negative, then something OTHER than
entry timing is producing the gross. The candidates are:

  (a) the **exit** (a fixed target that a drift-following position
      reaches more often than a random one), and
  (b) the **payoff geometry** (a $2.50 stop against a $20 target).

This diagnostic isolates (a) and (b) by DESTROYING the FVG signal
while keeping every other mechanic identical.

THE PLACEBO
============

Take the canonical zone set — the SAME zones, the SAME trigger
bars, the SAME widths, the SAME directions, the SAME count — and
move each zone's PRICE BAND to a different price level, while
keeping it **anchored to its own trigger bar's market price**.

    the zone's price band is RE-CENTRED on the market price at its
    own trigger bar (offset by a randomly chosen OTHER zone's
    relative offset), so:

      * the zone's WIDTH is unchanged,
      * the zone's RELATIVE position to the market at signal time is
        changed (that is the signal's price information),
      * the zone still sits at a price the market actually visits, so
        the resting limit order can fill and the SL/TP geometry is
        well-posed.

    (trigger_bar, direction, width, lifecycle) are ALL unchanged.

What survives: the number of zones, the zone-width distribution, the
signal TIMING (the trigger bars are untouched), the engine's
book/conflict/fill mechanics, the fee model, the SL/TP construction,
and the fact that every entry is at a price the market reached.

What is destroyed: the correspondence between a signal and the
price level it points at. The engine still fires on the same bars,
in the same direction, with the same resting limit order, the same
$2.50 stop and the same TP — but the entry level is now arbitrary.

WHY NOT A NAIVE SHUFFLE
=======================

The obvious version — permute the raw ``zone_low``/``zone_high``
across the corpus — is INVALID and was measured to be so. It puts
each zone's price band at a price the market is nowhere near, so:

    |entry - market at fill|   canonical p50   $0.13
                               naive shuffle   $910.35

The trades are then entered ~$900 away from the market and the
realised "P&L" is just that distance. Measured: **−$1,058 per leg**,
which is not a result, it is an artefact of the broken geometry.

So the price band is RE-CENTRED on the market instead of being
relocated wholesale.

If the strategy's edge is a genuine **timing** edge, the placebo
should keep most of the gross: the signal still says "go long at
this moment", and a long at an arbitrary level still earns the
drift.

If the gross collapses, the gross was coming from the **level** — the
entry price relative to the market — and the "signal" was never
carrying timing information at all.

THE THREE ARMS
===============

  canonical    the recipe, unmodified
  shuffled     the same zones with their prices permuted
  shuffled_b   a SECOND independent shuffle (a different seed), so
               the placebo's own variance is visible

Usage
-----
    python -m runtime.diagnostics.zone_shuffle_placebo
    python -m runtime.diagnostics.zone_shuffle_placebo --seeds 3
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

from bt.optimal_config import (  # noqa: E402
    FOREX_CONTRACT_SIZE,
    FOREX_LOTS,
    build_engine_kwargs,
)
from bt.vp_engine import run_vp_sniper_backtest_forex  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")

# The corpus proper, with a 14-day warmup prefix (see leg_ledger).
WARMUP_DAYS = 14
START = "2024-01-01"
END = "2026-07-16"

SEEDS = (11, 23, 37)


def _recentre_zone_prices(zones: list, close: np.ndarray
                          ) -> tuple[list, dict[str, Any]]:
    """Re-centre each zone's band on the signal bar's CLOSE.

    THE GEOMETRY BEING MIMICRED
    ===========================

    In the real detector, a bull FVG's ``zone_low`` is bar-3's LOW
    and a bear FVG's ``zone_high`` is bar-3's HIGH — and the trigger
    bar IS bar 3. The engine's entry price is exactly that edge::

        bull  ->  entry = zone_low  = the trigger bar's LOW
        bear  ->  entry = zone_high = the trigger bar's HIGH

    So the canonical entry is the trigger bar's own EXTREME, and the
    trigger bar's CLOSE is a few cents inside it.

    This placebo replaces that extreme with the trigger bar's CLOSE:

        bull  ->  zone_low  = trigger close,  zone_high = close + width
        bear  ->  zone_high = trigger close,  zone_low  = close - width

    The zone's WIDTH, DIRECTION and TRIGGER BAR are unchanged. The
    band still extends in the correct direction (a bull zone's band
    is ABOVE its entry edge, a bear zone's BELOW), so the entry is
    a price the market has just visited and the resting limit can
    fill on a later bar.

    This is the sharpest version of the question: it separates

        "the strategy needs a level NEAR the market"    (any level works)
    from
        "the strategy needs the FVG's specific level"   (only the gap works)

    Both the canonical and the re-centred arms fill a resting limit
    at a price the market visits, so the execution model is
    identical; only the LEVEL's origin differs.
    """
    import copy

    n = len(zones)
    if n == 0:
        return [], {}

    lows = np.array([float(z.zone_low) for z in zones], dtype=np.float64)
    highs = np.array([float(z.zone_high) for z in zones], dtype=np.float64)
    widths = highs - lows
    trig = np.array([int(z.trigger_bar) for z in zones], dtype=np.int64)
    mkt = close[np.clip(trig, 0, close.size - 1)]

    # The entry edge is zone_low for a bull zone and zone_high for a
    # bear zone. Re-centre THAT edge on the trigger bar's close.
    is_bull = lows < mkt
    new_low = np.where(is_bull, mkt, mkt - widths)
    new_high = np.where(is_bull, mkt + widths, mkt)

    out = []
    for z, lo, hi in zip(zones, new_low, new_high):
        zc = copy.copy(z)
        zc.zone_low = float(lo)
        zc.zone_high = float(hi)
        out.append(zc)

    stats = {
        "seed": 0,
        "n_zones": n,
        "width_p50_usd": float(np.median(widths)),
        "shift_p50_usd": float(np.median(np.abs(new_low - lows))),
    }
    return out, stats


def _shuffle_zone_prices(zones: list, seed: int, close: np.ndarray
                         ) -> tuple[list, dict[str, Any]]:
    """Give each zone a DIFFERENT relative offset from its trigger bar.

    THE GEOMETRY BEING MIMICRED
    ===========================

    The canonical entry is the trigger bar's own extreme (bar-3's low
    for a bull FVG, bar-3's high for a bear FVG), so each zone has a
    characteristic OFFSET from the trigger bar's close:

        offset = zone_low  - close  (bull)
        offset = zone_high - close  (bear)

    This placebo keeps each zone's WIDTH, DIRECTION and TRIGGER BAR
    and re-places its band at a DIFFERENT offset, drawn from the pool
    of offsets observed across the corpus. The band therefore still
    sits just on the far side of the trigger bar's price — a level
    the market visits — so the resting limit can fill and the SL/TP
    geometry is well-posed.

    What is destroyed is the correspondence between a signal and the
    SPECIFIC level it points at.

    WHY NOT A NAIVE SHUFFLE
    ======================

    The obvious version — permute the raw ``zone_low``/``zone_high``
    across the corpus — is INVALID and was measured to be so. It puts
    each zone's band at a price the market is nowhere near, so::

        |entry - market at fill|   canonical p50   $0.13
                                   naive shuffle   $910.35

    The trades are then entered ~$900 away from the market and the
    realised "P&L" is just that distance. Measured: **−$1,058 per
    leg**, which is not a result, it is an artefact of broken
    geometry. The diagnostic now carries a geometry gate that
    refuses to print a verdict if this happens.

    Returns the new zones plus the offset-pool statistics, so the
    report can state what the placebo actually did.
    """
    import copy

    rng = np.random.default_rng(seed)
    n = len(zones)
    if n == 0:
        return [], {}

    lows = np.array([float(z.zone_low) for z in zones], dtype=np.float64)
    highs = np.array([float(z.zone_high) for z in zones], dtype=np.float64)
    widths = highs - lows
    trig = np.array([int(z.trigger_bar) for z in zones], dtype=np.int64)

    # The market price at each zone's own trigger bar.
    mkt = close[np.clip(trig, 0, close.size - 1)]

    # The observed offset pool: each zone's distance from its own
    # trigger-bar market price to its band. For a bull zone the
    # band sits BELOW the market (the gap is under the middle bar),
    # for a bear zone ABOVE it. Keep the SIGN so the placebo zones
    # are still geometrically plausible.
    is_bull = lows < mkt
    offs = np.where(is_bull, lows - mkt, highs - mkt)

    # Draw each zone's replacement offset from the pool (with
    # replacement) and re-place the band.
    pick = rng.integers(0, n, size=n)
    new_offs = offs[pick]

    # Re-derive the band, preserving the sign convention: a bull
    # zone's band is below the market at signal time, a bear zone's
    # above it.
    new_low = np.where(is_bull, mkt + new_offs, mkt + new_offs - widths)
    new_high = new_low + widths

    out = []
    for z, lo, hi in zip(zones, new_low, new_high):
        zc = copy.copy(z)
        zc.zone_low = float(lo)
        zc.zone_high = float(hi)
        out.append(zc)

    stats = {
        "seed": seed,
        "n_zones": n,
        "offset_pool_p50_usd": float(np.median(np.abs(offs))),
        "new_offset_p50_usd": float(np.median(np.abs(new_offs))),
        "width_p50_usd": float(np.median(widths)),
        "moved_beyond_1us": int(np.sum(np.abs(offs - new_offs) > 1e-9)),
    }
    return out, stats


def _run(bars, art, zones, label: str) -> dict[str, Any]:
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=zones, vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek, record_equity_curve=False,
    )
    wall = time.perf_counter() - t0
    trades = list(res.trades)
    gross = float(sum(t.pnl_gross_usd for t in trades))
    fees = float(sum(getattr(t, "fee_usd", 0.0) for t in trades))

    # The drift/excess split, per arm. This is what EXPLAINS the
    # result: the shuffle can destroy the gross either by making the
    # positions directionally wrong (the DRIFT turns negative) or by
    # wrecking the payoff geometry (the EXCESS turns negative).
    # Those are very different findings, so the arm reports both.
    close = bars["close"].to_numpy(dtype=np.float64)
    drift = 0.0
    excess = 0.0
    if trades:
        sb = np.array([int(getattr(t, "signal_bar", t.entry_bar))
                       for t in trades], dtype=np.int64)
        eb = np.array([int(t.entry_bar) for t in trades], dtype=np.int64)
        xb = np.array([int(t.exit_bar) for t in trades], dtype=np.int64)
        sd = np.array([float(t.direction) for t in trades], dtype=np.float64)
        qt = np.array([float(t.qty_btc) for t in trades], dtype=np.float64)
        c_fill = close[np.clip(eb, 0, close.size - 1)]
        c_exit = close[np.clip(xb, 0, close.size - 1)]
        drift = float(np.sum((c_exit - c_fill) * sd * qt))
        excess = gross - drift

    # The GEOMETRY SANITY CHECK. A placebo that puts the entry
    # thousands of dollars away from the market is not a placebo --
    # it is a broken backtest, and its P&L is meaningless. The
    # canonical arm's |entry - market@fill| is ~$0.13 (a resting
    # limit fills AT the level), so the placebo must be in the same
    # regime or the comparison is invalid.
    if trades:
        eb2 = np.array([int(t.entry_bar) for t in trades], dtype=np.int64)
        ep = np.array([float(t.entry_price) for t in trades], dtype=np.float64)
        mkt = close[np.clip(eb2, 0, close.size - 1)]
        offset_p50 = float(np.median(np.abs(ep - mkt)))
        offset_p90 = float(np.percentile(np.abs(ep - mkt), 90))
    else:
        offset_p50 = offset_p90 = float("nan")
    return {
        "arm": label,
        "legs": len(trades),
        "gross_usd": gross,
        "fees_usd": fees,
        "net_usd": gross - fees,
        "gross_per_leg": gross / len(trades) if trades else 0.0,
        "drift_usd": drift,
        "excess_usd": excess,
        "drift_per_leg": drift / len(trades) if trades else 0.0,
        "excess_per_leg": excess / len(trades) if trades else 0.0,
        "entry_offset_p50_usd": offset_p50,
        "entry_offset_p90_usd": offset_p90,
        "wall_secs": wall,
    }


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DATA)
    ap.add_argument("--start", type=str, default=START)
    ap.add_argument("--end", type=str, default=END)
    ap.add_argument("--seeds", type=int, default=len(SEEDS))
    ap.add_argument("--out", type=Path, default=None)
    args = ap.parse_args()

    import pandas as pd
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    exec_start = (pd.Timestamp(args.start, tz="UTC")
                  - pd.Timedelta(days=WARMUP_DAYS)).strftime("%Y-%m-%d")
    scope = (f"{args.data.stem}__{args.start.replace('-', '')}"
             f"_{args.end.replace('-', '')}")

    print("== zone-shuffle placebo ==")
    print(f"data   : {args.data}")
    print(f"window : {args.start} .. {args.end} (warmup {WARMUP_DAYS}d)")
    print()

    bars = load_bars(args.data, exec_start, args.end)
    art = ArtifactCache(args.data, scope, exec_start, args.end).build_or_load(bars)
    print(f"  zones={art['n_zones']}  cache_hit={art.get('_cache_hit', False)}",
          flush=True)

    arms: list[dict[str, Any]] = []
    placebo_stats: list[dict[str, Any]] = []

    print("  running canonical ...", flush=True)
    arms.append(_run(bars, art, art["zones"], "canonical"))

    close = bars["close"].to_numpy(dtype=np.float64)

    print("  running recentred (level <- signal-bar close) ...", flush=True)
    zrec, st_rec = _recentre_zone_prices(art["zones"], close)
    a_rec = _run(bars, art, zrec, "recentred")
    a_rec.update(st_rec)
    arms.append(a_rec)

    for k in range(args.seeds):
        seed = SEEDS[k % len(SEEDS)]
        print(f"  running shuffled (seed={seed}) ...", flush=True)
        zsh, st = _shuffle_zone_prices(art["zones"], seed, close)
        st["seed"] = seed
        placebo_stats.append(st)
        a = _run(bars, art, zsh, f"shuffled_{seed}")
        a.update(st)
        arms.append(a)

    # ── report ───────────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("ZONE-SHUFFLE PLACEBO - does the FVG signal say WHERE to enter?")
    print("=" * 78)
    hdr = (f"{'arm':<18}{'legs':>7}{'gross':>11}{'drift':>11}{'excess':>10}"
           f"{'g/leg':>9}{'|entry-mkt|':>12}")
    print(hdr)
    print("-" * len(hdr))
    for a in arms:
        print(f"{a['arm']:<18}{a['legs']:>7}{a['gross_usd']:>11,.2f}"
              f"{a['drift_usd']:>11,.2f}{a['excess_usd']:>10,.2f}"
              f"{a['gross_per_leg']:>9.4f}"
              f"{a.get('entry_offset_p50_usd', float('nan')):>12,.2f}")

    canon = arms[0]
    shuf = [a for a in arms if a["arm"].startswith("shuffled")]
    rec = [a for a in arms if a["arm"] == "recentred"]
    if rec:
        r = rec[0]
        print()
        print(f"  RECENTRED arm (level <- the signal bar's own close):")
        print(f"    gross ${r['gross_usd']:,.2f}   drift ${r['drift_usd']:,.2f}"
              f"   excess ${r['excess_usd']:,.2f}   legs {r['legs']}")
        print(f"    vs canonical gross ${canon['gross_usd']:,.2f}   "
              f"drift ${canon['drift_usd']:,.2f}   excess ${canon['excess_usd']:,.2f}")
        keep_r = r["gross_usd"] / canon["gross_usd"] \
            if abs(canon["gross_usd"]) > 1e-9 else 0.0
        print(f"    gross retained by the recentred arm: {100 * keep_r:.1f}%")
    if shuf:
        mean_gross = float(np.mean([a["gross_usd"] for a in shuf]))
        sd_gross = float(np.std([a["gross_usd"] for a in shuf], ddof=1)) \
            if len(shuf) > 1 else 0.0
        keep = mean_gross / canon["gross_usd"] if abs(canon["gross_usd"]) > 1e-9 \
            else 0.0
        print()
        print(f"  canonical gross      ${canon['gross_usd']:,.2f}")
        print(f"  shuffled  gross      ${mean_gross:,.2f}  "
              f"(sd ${sd_gross:,.2f} over {len(shuf)} shuffles)")
        print(f"  gross retained by the placebo   {100 * keep:.1f}%")
        print()

        # ── VALIDITY GATE ───────────────────────────────────────────
        # A placebo whose entries sit far from the market is not a
        # placebo. The canonical arm's |entry - market@fill| is the
        # reference; if the placebo's is much larger, the comparison
        # is invalid and the verdict must not be printed.
        canon_off = canon.get("entry_offset_p50_usd", 0.0)
        worst_off = max((a.get("entry_offset_p50_usd", 0.0) for a in shuf),
                         default=0.0)
        valid = worst_off <= max(2.0, 5.0 * canon_off)
        print(f"  geometry check: canonical |entry-mkt| p50 = "
              f"${canon_off:,.2f}; placebo max = ${worst_off:,.2f}  "
              f"-> {'VALID' if valid else 'INVALID'}")
        if not valid:
            print()
            print("VERDICT: PLACEBO INVALID - the placebo's entries are not at")
            print("        the market, so its P&L measures the price distance,")
            print("        not the signal. No conclusion may be drawn.")
            payload = {"window": [args.start, args.end], "arms": arms,
                       "placebo_valid": False}
            if args.out:
                args.out.parent.mkdir(parents=True, exist_ok=True)
                args.out.write_text(json.dumps(payload, indent=2, default=float),
                                    encoding="utf-8")
                print(f"\n-> {args.out}")
            return

        if keep >= 0.5:
            print("VERDICT: the gross SURVIVES the shuffle - it comes from the")
            print("        SIGNAL TIMING, not from the entry level. The FVG")
            print("        signal is carrying real information.")
        else:
            print("VERDICT: the gross COLLAPSES under the shuffle - it came")
            print("        from the entry LEVEL (where the zone sat), not from")
            print("        the signal's timing. The 'signal' was not a timing")
            print("        edge.")
            print()
            print("  WHY -- the drift/excess split:")
            print(f"    canonical   drift ${canon['drift_usd']:>10,.2f}  "
                  f"excess ${canon['excess_usd']:>10,.2f}")
            for a in shuf:
                print(f"    {a['arm']:<11} drift ${a['drift_usd']:>10,.2f}  "
                      f"excess ${a['excess_usd']:>10,.2f}")
            d_c = canon["drift_per_leg"]
            d_s = float(np.mean([a["drift_per_leg"] for a in shuf]))
            print()
            print(f"    drift/leg   canonical ${d_c:+.4f}  ->  placebo ${d_s:+.4f}")
            print(f"    excess/leg  canonical ${canon['excess_per_leg']:+.4f}"
                  f"  ->  placebo "
                  f"${float(np.mean([a['excess_per_leg'] for a in shuf])):+.4f}")
            if d_s < d_c:
                print()
                print("    -> the shuffle destroyed the DRIFT: the positions no")
                print("       longer sit on the side of the market that pays.")
                print("       The FVG level was doing directional work.")
            else:
                print()
                print("    -> the drift is INTACT but the EXCESS collapsed: the")
                print("       positions still earn the market's move, but the")
                print("       payoff geometry that captured it is gone.")

    payload = {"window": [args.start, args.end], "arms": arms,
               "placebo_valid": True}
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2, default=float),
                            encoding="utf-8")
        print(f"\n-> {args.out}")


if __name__ == "__main__":
    main()
