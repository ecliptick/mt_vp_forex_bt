"""CORPUS A/B for the PER-SIDE trailing-stop scope (``trail_sides``).

WHY THIS DRIVER EXISTS
======================

The trail MFE/MFA analysis (``docs/trail_stop.md`` § 8.4) measured the
trail's effect PER SIDE on the 8-month corpus and found that the trail
**helps the losing side and hurts the winning side**:

    side   n      baseline gross   canonical trail delta
    long   1,685  +$1,073.50       -$906.77   (753 TPs cut)
    short    854   -$245.05        +$533.38   (290 SLs rescued)

That is a SCREEN, not a verdict. AGENTS.md § 3.11 is explicit:

    A drop-set or whitelist MUST be measured END TO END by re-running
    the engine. The post-hoc leg filter is a screen for which
    hypotheses are worth a re-run -- never the verdict itself.

The trail changes the BOOK, so it MUST be re-measured through the
engine: a trail-closed leg frees a same-direction slot (a later signal
is admitted that would not have been), and it can no longer be netted by
contra-close.

This driver is that end-to-end measurement.

EFFICIENCY — WHY THIS IS NOT 12 x (rebuild everything)
======================================================

The naive way to run an N-arm A/B is to call the sweep driver N times.
That rebuilds the ARTIFACTS (zones / VP profile / swing cap / p95 cap
/ vol_state) N times, and on the 8-month corpus that is ~10 minutes of
artifact building per arm -- the dominant cost, and it is IDENTICAL work
every time, because the artifacts are functions of the FILE, not of the
knob.

This driver instead:

1. builds the artifacts ONCE (the ``ArtifactCache`` handles the
   on-disk cache), then
2. runs only the ENGINE per arm, and
3. decomposes each arm's legs BY SIDE, so one run answers the
   per-side question AND the whole-book question.

The cost is therefore ``1 x artifact + N x engine`` instead of
``N x (artifact + engine)``.

Also: ``record_equity_curve=False``. The equity curve is a per-bar
float64 array -- 330 MB on the 1y file -- and this driver does not use
it.

OUTPUT
======

A per-side table for every arm, plus the whole-book verdict, plus a
DURABLE registry record (``kind="trail_sides_ab"``). The registry is
the committed evidence; the JSON report is the bulky intermediate.
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
from runtime.results import registry  # noqa: E402

DEFAULT_DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT_DIR = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"

# The corpus window. Matches the trail MFE/MFA analysis (8 months, ONE
# continuous run) so the two documents describe the SAME book.
CORPUS_START = "2024-04-01"
CORPUS_END = "2024-12-01"

# The ARMS. Each is (label, engine_overrides).
#
# The baseline is ``trail_mode='off'`` -- the book with NO trail at all.
# Every other arm is compared to it.
#
# The arms are chosen from the corpus replay's own rankings
# (docs/trail_stop.md § 6.3, § 7.1):
#   * the canonical (worst cell in the grid) -- the thing to beat,
#   * the two geometries the replay liked, at all three scopes,
#   * the 'short'-scoped variants, which are the actual hypothesis.
ARMS: list[tuple[str, dict[str, Any]]] = [
    # ── The control: no trail at all ──
    ("trail OFF (baseline)", {"trail_mode": "off"}),
    # ── The canonical geometry, at all three scopes ──
    ("combined k=2 act=1 | both",
     {"trail_mode": "combined", "trail_atr_mult": 2.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "both"}),
    ("combined k=2 act=1 | long",
     {"trail_mode": "combined", "trail_atr_mult": 2.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "long"}),
    ("combined k=2 act=1 | short",
     {"trail_mode": "combined", "trail_atr_mult": 2.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "short"}),
    # ── The replay's best geometry, at all three scopes ──
    ("chandelier k=32 act=1 | both",
     {"trail_mode": "chandelier", "trail_atr_mult": 32.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "both"}),
    ("chandelier k=32 act=1 | short",
     {"trail_mode": "chandelier", "trail_atr_mult": 32.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "short"}),
    # ── The mid geometry (the one the replay called 'widest') ──
    ("chandelier k=8 act=1 | both",
     {"trail_mode": "chandelier", "trail_atr_mult": 8.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "both"}),
    ("chandelier k=8 act=1 | short",
     {"trail_mode": "chandelier", "trail_atr_mult": 8.0,
      "trail_activation_mult": 1.0, "trail_giveback_frac": 0.5,
      "trail_sides": "short"}),
    # ── The 'short'-scoped arm at a LATE activation (fires rarely) ──
    ("chandelier k=32 act=2 | short",
     {"trail_mode": "chandelier", "trail_atr_mult": 32.0,
      "trail_activation_mult": 2.0, "trail_giveback_frac": 0.5,
      "trail_sides": "short"}),
]


def _decompose(trades: list) -> dict[str, Any]:
    """Per-side gross/fees/legs/exit-reason breakdown for one arm."""
    out: dict[str, Any] = {}
    for label, sel in (("long", lambda d: d > 0), ("short", lambda d: d < 0)):
        legs = [t for t in trades if sel(int(t.direction))]
        gross = sum(float(t.pnl_gross_usd) for t in legs)
        fees = sum(float(t.fee_usd) for t in legs)
        reasons: dict[str, int] = {}
        for t in legs:
            r = str(t.exit_reason)
            reasons[r] = reasons.get(r, 0) + 1
        out[label] = {
            "legs": len(legs),
            "gross": gross,
            "fees": fees,
            "net": gross - fees,
            "gross_per_leg": (gross / len(legs)) if legs else 0.0,
            "n_trail_stop": sum(1 for t in legs
                                if str(t.exit_reason) == "trail_stop"),
            "exit_reasons": reasons,
        }
    gross = sum(float(t.pnl_gross_usd) for t in trades)
    fees = sum(float(t.fee_usd) for t in trades)
    reasons_all: dict[str, int] = {}
    for t in trades:
        r = str(t.exit_reason)
        reasons_all[r] = reasons_all.get(r, 0) + 1
    out["all"] = {
        "legs": len(trades),
        "gross": gross,
        "fees": fees,
        "net": gross - fees,
        "gross_per_leg": (gross / len(trades)) if trades else 0.0,
        "fee_per_leg": (fees / len(trades)) if trades else 0.0,
        "n_trail_stop": sum(1 for t in trades
                            if str(t.exit_reason) == "trail_stop"),
        "exit_reasons": reasons_all,
    }
    return out


def _fmt_money(v: float) -> str:
    return f"${v:,.2f}"


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=CORPUS_START)
    ap.add_argument("--end", type=str, default=CORPUS_END)
    ap.add_argument(
        "--expand-prev-day", type=float, default=None,
        help="vol_state_min_expand_prev_day for the base recipe. "
             "DEFAULT = None = USE THE CANONICAL VALUE (1.0, promoted "
             "2026-11-04). Pass 0.0 to reproduce the older gate-off book.",
    )
    ap.add_argument(
        "--warmup-days", type=int, default=0,
        help="Warmup prefix in days, EXECUTED AND DISCARDED. 0 = none. "
             "The ledger uses 14 (it exceeds the longest history-"
             "dependent lookback: p95 cap 7d, swing cap 24h, VP 7d). "
             "Set it for a measurement that must match a ledger.",
    )
    ap.add_argument(
        "--split-at", type=str, default=None,
        help="Split the SCORED window at this date into H1 / H2 and "
             "report the delta vs baseline PER HALF. This is the "
             "out-of-sample test: ONE continuous execution, bucketed by "
             "signal time (AGENTS.md § 1.1 — the half is a REPORTING "
             "dimension, never an execution boundary).",
    )
    ap.add_argument(
        "--quick", action="store_true",
        help="Run only the 3 ESSENTIAL arms (baseline, canonical|both, "
             "canonical|short). For a per-half out-of-sample check, where "
             "the question is whether the SHORT SCOPE helps in BOTH "
             "halves -- the other geometries are already settled.",
    )
    ap.add_argument("--out", type=Path,
                    default=OUT_DIR / "trail_sides_corpus_ab.json")
    args = ap.parse_args()

    # The RESOLVED gate value is what the measurement actually used.
    # ``None`` means "the canonical constant" — but a registry record
    # must name the number, not a sentinel, or two different books
    # share a fingerprint (AGENTS.md § 2.3: params must contain every
    # input that could change the measurement).
    from bt.optimal_config import VOL_STATE_MIN_EXPAND_PREV_DAY

    resolved_expand = (
        VOL_STATE_MIN_EXPAND_PREV_DAY
        if args.expand_prev_day is None
        else float(args.expand_prev_day)
    )

    arms = ARMS
    if args.quick:
        keep = ("trail OFF (baseline)",
                "combined k=2 act=1 | both",
                "combined k=2 act=1 | short")
        arms = [a for a in ARMS if a[0] in keep]

    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    # ── The WARMUP PREFIX ──────────────────────────────────────────
    #
    # The execution window carries the warmup prefix; the SCORED
    # window is the corpus proper. The prefix's legs are DISCARDED —
    # only its warmup effect is kept. This is the ledger's contract
    # (AGENTS.md § 5.4) and it is what makes a number here
    # COMPARABLE to a ledger number.
    #
    # ⚠️ Without it, the p95 cap returns ``inf`` until its 7-day
    # window fills, so the first ~8 days trade with UNCAPPED targets
    # and the book is not the canonical book.
    warmup_days = int(args.warmup_days)
    score_lo = score_hi = None
    if warmup_days > 0:
        import pandas as pd
        exec_start = (pd.Timestamp(args.start, tz="UTC")
                      - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
        score_lo = int(pd.Timestamp(args.start, tz="UTC").value)
        score_hi = int(pd.Timestamp(args.end, tz="UTC").value)
    else:
        exec_start = args.start

    print("=" * 78)
    print("TRAIL_SIDES CORPUS A/B")
    print("=" * 78)
    print(f"window {args.start} -> {args.end}   data {args.data.name}")
    if warmup_days:
        print(f"warmup: {warmup_days}d prefix from {exec_start} "
              f"(executed and discarded)")
    if args.expand_prev_day is None:
        print("base recipe: CANONICAL (expand_prev_day="
              f"{resolved_expand}, 21-key structure whitelist)")
    else:
        print(f"base recipe: expand_prev_day={resolved_expand}")
    print(f"arms: {len(arms)}   (artifacts built ONCE, engine per arm)")
    print()

    t0 = time.perf_counter()
    bars = load_bars(args.data, exec_start, args.end)
    if bars.empty:
        raise SystemExit("no bars in window")
    scope = (f"{args.data.stem}__{exec_start.replace('-', '')}"
             f"_{args.end.replace('-', '')}")
    art = ArtifactCache(args.data, scope, exec_start, args.end).build_or_load(bars)
    print(f"artifacts: {'HIT' if art.get('_cache_hit') else 'MISS'}  "
          f"zones={len(art['zones']):,}  "
          f"({time.perf_counter() - t0:.1f}s total)", flush=True)

    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    def _run(over: dict[str, Any]):
        ek = build_engine_kwargs(
            spot=spot,
            swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art.get("structure"), vol_state=art.get("vol_state"),
            **over,
        )
        # Always set it explicitly: the RESOLVED number is the input
        # that produced this measurement, and the registry must name
        # it (AGENTS.md § 2.3).
        ek["vol_state_min_expand_prev_day"] = float(resolved_expand)
        t = time.perf_counter()
        res, _ = run_vp_sniper_backtest_forex(
            bars=bars, zones=art["zones"], vp_set=art["vp_set"],
            lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
            engine_kwargs=ek, record_equity_curve=False,
        )
        trades = list(res.trades)
        if warmup_days:
            # Drop the warmup prefix's legs — only its warmup EFFECT
            # is kept.
            keep = []
            for tr in trades:
                sb = int(getattr(tr, "signal_bar", 0))
                if sb < len(bars):
                    tns = int(bars["time"].iloc[sb].value)  # ns
                    if score_lo <= tns < score_hi:
                        keep.append(tr)
            trades = keep
        return trades, time.perf_counter() - t

    # ── The control arm FIRST: every delta is measured against it ──
    base_label, base_over = arms[0]
    base_trades, base_wall = _run(base_over)
    base = _decompose(base_trades)
    print(f"{base_label}: {base['all']['legs']} legs in {base_wall:.1f}s  "
          f"net {_fmt_money(base['all']['net'])}", flush=True)
    print()

    hdr = (f"{'arm':<32}{'legs':>6}{'gross':>11}{'net':>11}{'trail':>7}"
           f"{'vs base':>10}{'long net':>11}{'short net':>11}{'L-trail':>8}")
    print(hdr)
    print("-" * len(hdr))

    rows: list[dict[str, Any]] = []
    arm_trades: list[list] = []
    for idx, (label, over) in enumerate(arms):
        if idx == 0:
            trades, wall = base_trades, base_wall
        else:
            trades, wall = _run(over)
        arm_trades.append(trades)
        d = _decompose(trades)
        delta = d["all"]["net"] - base["all"]["net"]
        d["arm"] = label
        d["overrides"] = {k: repr(v) for k, v in sorted(over.items())}
        d["wall_secs"] = round(wall, 1)
        d["delta_vs_base_net"] = delta
        d["delta_long_net"] = d["long"]["net"] - base["long"]["net"]
        d["delta_short_net"] = d["short"]["net"] - base["short"]["net"]
        rows.append(d)
        print(f"{label:<32}{d['all']['legs']:>6}"
              f"{_fmt_money(d['all']['gross']):>11}"
              f"{_fmt_money(d['all']['net']):>11}"
              f"{d['all']['n_trail_stop']:>7}"
              f"{delta:>+10,.2f}"
              f"{d['long']['net']:>+11,.2f}"
              f"{d['short']['net']:>+11,.2f}"
              f"{d['long']['n_trail_stop']:>8}", flush=True)

    # ── THE OUT-OF-SAMPLE SPLIT ─────────────────────────────────────
    #
    # ONE continuous execution, bucketed by SIGNAL TIME into two
    # halves. AGENTS.md § 1.1: the month (here, the half) is a
    # REPORTING dimension, never an execution boundary — so both
    # halves share ONE run's warm state and one set of caps.
    #
    # This is the test that decides the knob: a whole-corpus fit can
    # be positive while one half carries it. § 3.10.0 showed exactly
    # that for the sequence whitelist's short side (in-sample +0.0700
    # -> OOS -0.1803, a SIGN FLIP).
    split_payload: dict[str, Any] | None = None
    if args.split_at:
        import pandas as pd
        split_ns = int(pd.Timestamp(args.split_at, tz="UTC").value)

        def _half(trades: list) -> tuple[list, list]:
            h1, h2 = [], []
            for tr in trades:
                sb = int(getattr(tr, "signal_bar", 0))
                tns = int(bars["time"].iloc[sb].value) if sb < len(bars) else 0
                (h1 if tns < split_ns else h2).append(tr)
            return h1, h2

        print()
        print("=" * 78)
        print(f"OUT-OF-SAMPLE SPLIT at {args.split_at}")
        print("=" * 78)
        base_h1, base_h2 = _half(base_trades)
        bh1, bh2 = _decompose(base_h1), _decompose(base_h2)
        print(f"{'arm':<32}{'H1 net':>11}{'H1 delta':>11}"
              f"{'H2 net':>11}{'H2 delta':>11}{'both +':>9}")
        print("-" * 86)
        split_rows: list[dict[str, Any]] = []
        for r, tr in zip(rows, arm_trades):
            h1, h2 = _half(tr)
            a1, a2 = _decompose(h1), _decompose(h2)
            d1 = a1["all"]["net"] - bh1["all"]["net"]
            d2 = a2["all"]["net"] - bh2["all"]["net"]
            r["h1"] = {"net": a1["all"]["net"], "delta": d1,
                       "legs": a1["all"]["legs"]}
            r["h2"] = {"net": a2["all"]["net"], "delta": d2,
                       "legs": a2["all"]["legs"]}
            split_rows.append(r)
            both = "YES" if (d1 > 0 and d2 > 0) else "no"
            # ⚠️ Use ``r["arm"]`` — NOT the loop variable ``label``,
            # which is left over from the arm loop above and would
            # print the LAST arm's name for every row.
            print(f"{r['arm']:<32}{a1['all']['net']:>+11,.2f}{d1:>+11,.2f}"
                  f"{a2['all']['net']:>+11,.2f}{d2:>+11,.2f}{both:>9}")
        print()
        print("  'both +' = the arm beats the baseline in BOTH halves.")
        print("  An arm that wins one half and loses the other is a FIT,")
        print("  not an edge (AGENTS.md § 3.10.0).")
        print()
        split_payload = {
            "split_at": args.split_at,
            "rows": [{"arm": r["arm"], "h1": r["h1"], "h2": r["h2"]}
                     for r in split_rows],
        }

    # ── THE VERDICT ────────────────────────────────────────────────
    print()
    print("=" * 78)
    print("THE VERDICT")
    print("=" * 78)

    best = max(rows, key=lambda r: r["all"]["net"])
    print(f"best whole-book arm : {best['arm']}  "
          f"net {_fmt_money(best['all']['net'])}  "
          f"delta {best['delta_vs_base_net']:+.2f}")
    print(f"baseline            : {base_label}  "
          f"net {_fmt_money(base['all']['net'])}")
    print()

    short_scoped = [r for r in rows if r["overrides"].get("trail_sides")
                    == "'short'"]
    if short_scoped:
        best_short = max(short_scoped, key=lambda r: r["all"]["net"])
        print(f"best SHORT-SCOPED arm: {best_short['arm']}  "
              f"net {_fmt_money(best_short['all']['net'])}  "
              f"delta {best_short['delta_vs_base_net']:+.2f}")
        print(f"  long  delta : {best_short['delta_long_net']:+.2f}")
        print(f"  short delta : {best_short['delta_short_net']:+.2f}")
        # ── THE SCOPE-GATE INVARIANT ──────────────────────────────
        #
        # A short-scoped trail must NEVER close a LONG leg on the
        # trail. (The long book's NET may still move — a trail-closed
        # short frees a same-direction slot, so a later long signal is
        # admitted that would not have been. That is the book effect
        # AGENTS.md § 3.11 is about, NOT a scope leak.)
        #
        # The leak test is therefore on the EXIT REASON, not on the net:
        # zero long legs may exit with reason 'trail_stop'.
        n_long_trail = best_short["long"]["n_trail_stop"]
        if n_long_trail:
            print(f"  !! SCOPE LEAK: {n_long_trail} LONG legs exited on the "
                  f"trail under a short-scoped trail")
        else:
            print("  OK: zero long legs exited on the trail under the "
                  "short scope (the scope gate holds)")
            print("  NOTE: the long book's NET can still move — a "
                  "trail-closed short frees a slot, admitting a later "
                  "long signal. That is the book effect, not a leak.")
        print()

    # The per-side verdict for every arm, side by side.
    print("PER-SIDE DELTA vs the trail-OFF baseline")
    print(f"{'arm':<32}{'long delta':>13}{'short delta':>13}")
    print("-" * 45)
    for r in rows:
        print(f"{r['arm']:<32}{r['delta_long_net']:>+13,.2f}"
              f"{r['delta_short_net']:>+13,.2f}")
    print()

    # ── DURABLE RECORD ─────────────────────────────────────────────
    payload = {
        "start": args.start,
        "end": args.end,
        "expand_prev_day": resolved_expand,
        "warmup_days": warmup_days,
        "split_at": args.split_at,
        "split": split_payload,
        "rows": rows,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=1), encoding="utf-8")

    registry.record_run(
        kind="trail_sides_ab",
        params={
            "knob": "trail_sides",
            "arms": [label for label, _ in arms],
            "start": args.start,
            "end": args.end,
            "expand_prev_day": resolved_expand,
            "warmup_days": warmup_days,
            "split_at": args.split_at,
            "criterion": "net",
        },
        summary={
            "baseline_net": round(base["all"]["net"], 2),
            "baseline_legs": base["all"]["legs"],
            "best_arm": best["arm"],
            "best_net": round(best["all"]["net"], 2),
            "best_delta": round(best["delta_vs_base_net"], 2),
            "best_short_scoped_arm": (
                max(short_scoped, key=lambda r: r["all"]["net"])["arm"]
                if short_scoped else None),
            "best_short_scoped_delta": (
                round(max(short_scoped,
                          key=lambda r: r["all"]["net"])["delta_vs_base_net"], 2)
                if short_scoped else None),
            "per_arm": {
                r["arm"]: {
                    "legs": r["all"]["legs"],
                    "gross": round(r["all"]["gross"], 2),
                    "net": round(r["all"]["net"], 2),
                    "delta_vs_base": round(r["delta_vs_base_net"], 2),
                    "delta_long": round(r["delta_long_net"], 2),
                    "delta_short": round(r["delta_short_net"], 2),
                    "n_trail_stop": r["all"]["n_trail_stop"],
                    **({"h1_delta": round(r["h1"]["delta"], 2),
                        "h2_delta": round(r["h2"]["delta"], 2),
                        "wins_both_halves": bool(
                            r["h1"]["delta"] > 0 and r["h2"]["delta"] > 0)}
                       if "h1" in r else {}),
                }
                for r in rows
            },
        },
        payload=payload,
        src_fp=registry.source_fingerprint([
            "bt/_engine/vp_multi_target_engine.py",
            "bt/optimal_config.py",
        ]),
    )
    print(f"report -> {args.out}")
    print("registry: recorded run 'trail_sides_ab'")


if __name__ == "__main__":
    main()
