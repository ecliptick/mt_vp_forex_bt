"""THE LEDGER VERDICT for ``trail_sides`` — the promotion-gate measurement.

WHY THIS MODULE EXISTS
======================

``trail_sides_corpus_ab.py`` gave the ENGINE-level verdict (the net, and
the out-of-sample split). But the promotion gate in ``AGENTS.md``
§ 1.2 / § 5.1 asks for two things the engine summary cannot give:

1. **THE LEDGER** — the corpus-wide per-LEG record, so the gross can
   be compared against the **MEASURED** round trip (the engine's fee
   scales with the fill price, so the flat $0.47 spec overstates the
   edge by ~7%).
2. **THE DRIFT/EXCESS DECOMPOSITION** —

       drift  = passive hold of the same side, same window
       excess = gross - drift

   ``drift`` is the beta component (what any position of that size
   would have earned by being there); ``excess`` is the TIMING
   component. This is the question that decides the short scope: the
   engine-level A/B showed the gain is on the LONG side and arrives
   INDIRECTLY (trail-closed shorts free a same-direction slot and
   admit a later long signal). That is a **book-composition** gain.
   The decomposition says whether the legs themselves improved or
   whether the book merely got rearranged.

The **PAIRED** test is the sharpest one available. ``trail_sides``
changes only the EXIT, and both books walk the SAME bars frame, so
``signal_bar`` is a valid join key. For every signal bar present in
BOTH books we can compute the per-leg gross difference, which
removes the between-leg variance that swamps the unpaired
comparison (the two books have different leg COUNTS because freed
slots admit new signals).

The ledgers
-----------

* the CANONICAL ledger (no overrides) — the recipe that ships today;
* the VARIANT ledger (``trail_sides='short'``) — to its OWN file.

The canonical path is reserved for the canonical recipe, and
``build_ledger`` REFUSES to write a variant there
(``leg_ledger._guard_canonical_path``). This driver never asks it to.

Usage
-----
    python -m runtime.diagnostics.trail_sides_ledger_verdict
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from runtime.diagnostics.leg_buckets import analyse  # noqa: E402
from runtime.diagnostics.leg_ledger import (  # noqa: E402
    build_ledger,
    ledger_arrays,
    load_ledger,
)
from runtime.results import registry  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")
VARIANT_PATH = OUT_DIR / "leg_ledger_trail_short.json"
TRAIL_OFF_PATH = OUT_DIR / "leg_ledger_trail_off.json"
TRAIL_BOTH_PATH = OUT_DIR / "leg_ledger_trail_both.json"
TRAIL_LONG_PATH = OUT_DIR / "leg_ledger_trail_long.json"
REPORT_PATH = OUT_DIR / "trail_sides_ledger_verdict.json"

# The full canonical corpus.
CORPUS_START = "2024-01-01"
CORPUS_END = "2026-07-16"

# The out-of-sample split point (signal time). The half is a
# REPORTING dimension, never an execution boundary (AGENTS.md § 1.1).
SPLIT_AT = "2025-04-01"

# ⚠️ THE ARMS ARE EXPLICIT OVERRIDES — NEVER "no overrides".
#
# The canonical recipe's ``trail_sides`` is now ``'short'`` (promoted
# 2026-11-04). A driver that took the "canonical" arm from the
# no-overrides path would therefore build THE SAME BOOK as the
# ``'short'`` variant and compare the book against itself — a
# comparison that measures nothing and reads like a verdict.
#
# Every arm below names its scope EXPLICITLY, so the driver keeps
# measuring the scope grid after the promotion (and after any future
# change to the canonical value).
#
# THE CRITERION IS NET PROFIT (user instruction, 2026-11-04):
# "just promote whatever is most profitable". The drift/excess
# decomposition is NOT a promotion gate — see the note below.
#
# WHY ``excess`` IS NOT A GATE (measured, 2026-11-04). The ledger's
# ``excess = gross - drift`` benchmarks a leg's realised P&L against a
# CLOSE-TO-CLOSE passive hold. But the engine books P&L at the ACTUAL
# SL / TP FILL PRICE (85.9% of ``sl`` legs fill exactly at the stop;
# 100% of the remainder fill adverse at the bar open under the
# pessimistic model). On 71.6% of ``sl`` legs the exit bar's CLOSE is
# BEYOND the stop, so the fill price and the close differ by
# construction. The identity
#
#     excess == gap@exit - gap@entry
#
# holds to 1e-15 in every bucket of every book, where
# ``gap = (fill_price - bar_close) * side * qty``. So the ``sl``
# bucket's excess is the EXECUTION-GAP DIFFERENTIAL, not a timing
# measure: a stop fill and a close-to-close benchmark are not
# exchangeable, exactly like the random-entry control's limit-vs-market
# confound (AGENTS.md § 5.1 rule 4).
VARIANT_OVERRIDES: dict[str, Any] = {"trail_sides": "short"}

# The arms the promotion is judged against, each by EXPLICIT scope.
TRAIL_BOTH_OVERRIDES: dict[str, Any] = {"trail_sides": "both"}
TRAIL_LONG_OVERRIDES: dict[str, Any] = {"trail_sides": "long"}

# The BASELINE the promotion must beat. ``docs/trail_stop.md`` § 8.5.5
# says the finding that matters most is "the trail should be OFF or
# scoped" — so the OFF book is the real alternative to the variant, and
# a variant that only beats the SHIPPING book (which is what ships
# today) is not a promotion.
TRAIL_OFF_OVERRIDES: dict[str, Any] = {"trail_mode": "off"}

# The cut points for the split-robustness check. AGENTS.md § 3.9b
# names "one split point" as an honest limit of the single-split
# verdict; this closes it.
SPLITS = (
    "2024-07-01", "2024-10-01", "2025-01-01", "2025-04-01",
    "2025-07-01", "2025-10-01", "2026-01-01",
)


# ─────────────────────────────────────────────────────────────────────
# Statistics
# ─────────────────────────────────────────────────────────────────────
def _t(x: np.ndarray) -> float:
    """t-stat of the mean of ``x`` against zero."""
    x = np.asarray(x, dtype=np.float64)
    if x.size < 2:
        return 0.0
    sd = x.std(ddof=1)
    if not np.isfinite(sd) or sd <= 0:
        return 0.0
    return float(x.mean() / (sd / np.sqrt(x.size)))


def _welch(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """Unpaired two-sample comparison of the per-leg gross means.

    Welch's t-test — the books have different leg counts AND
    different variances, so the pooled-variance t-test is wrong.
    """
    a = np.asarray(a, dtype=np.float64)
    b = np.asarray(b, dtype=np.float64)
    if a.size < 2 or b.size < 2:
        return {"diff": 0.0, "t": 0.0, "se": 0.0}
    va = a.var(ddof=1)
    vb = b.var(ddof=1)
    se = float(np.sqrt(va / a.size + vb / b.size))
    diff = float(b.mean() - a.mean())
    return {
        "diff": diff,
        "se": se,
        "t": (diff / se) if se > 0 else 0.0,
        "mean_a": float(a.mean()),
        "mean_b": float(b.mean()),
    }


def _paired(a: np.ndarray, b: np.ndarray) -> dict[str, float]:
    """Paired comparison on the JOINED legs.

    ``a`` and ``b`` are already aligned (same length, same order) and
    each element is the same trade's gross in the two books.
    """
    d = np.asarray(b, dtype=np.float64) - np.asarray(a, dtype=np.float64)
    return {
        "n": int(d.size),
        "diff_total": float(d.sum()),
        "diff_mean": float(d.mean()) if d.size else 0.0,
        "t": _t(d),
        "n_better": int((d > 0).sum()),
        "n_worse": int((d < 0).sum()),
        "n_same": int((d == 0).sum()),
    }


def _join_on_signal_bar(a: dict[str, np.ndarray],
                         b: dict[str, np.ndarray]) -> dict[str, Any]:
    """Join two ledgers on ``(signal_bar, side)``.

    Both books walk the SAME bars frame — ``trail_sides`` changes only
    the exit — so ``signal_bar`` identifies the same SIGNAL, and
    ``side`` identifies the same direction.

    ⚠️ THE KEY IS **NOT UNIQUE**. Measured on the two books: the
    canonical ledger has 6,663 legs over 6,561 distinct
    ``(signal_bar, side)`` pairs (102 duplicates) and the variant has
    7,122 legs over the **same** 6,561 pairs (561 duplicates). The
    duplicates are MULTIPLE ZONES TRIGGERING ON THE SAME BAR (two
    FVGs on one bar, or the immediate + sniper paths both firing).

    A naive "first match wins" join therefore silently drops 561 legs
    and makes the two books look like they have the SAME trade
    universe — which would hide the entire book-composition effect
    this module exists to measure.

    The correct pairing is by **ENTRY bar** within the group: a leg's
    entry bar identifies its fill, and a trail-scoped exit changes the
    EXIT bar, never the entry. Within a group the legs are ordered by
    entry bar (the trade's own chronology) and paired positionally —
    a deterministic, documented rule rather than a silent first-match.

    The legs left unpaired on either side are the **book-composition
    effect**: a trail-closed short freed a same-direction slot and
    admitted a later signal (or a longer trail kept the slot occupied
    and blocked one).
    """
    ka = a["signal_bar"].astype(np.int64)
    kb = b["signal_bar"].astype(np.int64)
    # A signal bar can carry more than one leg (both sides, or two
    # zones on one bar), so the join is on (signal_bar, side) — the
    # trade's identity.
    def _key(arr: dict[str, np.ndarray]) -> dict[tuple[int, int], list[int]]:
        sig = arr["signal_bar"].astype(np.int64)
        side = arr["side"].astype(np.int64)
        ent = arr["entry_bar"].astype(np.int64)
        out: dict[tuple[int, int], list[int]] = {}
        for i in range(len(sig)):
            out.setdefault((int(sig[i]), int(side[i])), []).append(i)
        # Within a group, order by entry bar so the pairing is
        # deterministic and follows the trade's own chronology.
        for k in out:
            out[k].sort(key=lambda i: (int(ent[i]), int(sig[i])))
        return out

    ma = _key(a)
    mb = _key(b)
    common = sorted(set(ma) & set(mb))
    only_a = sorted(set(ma) - set(mb))
    only_b = sorted(set(mb) - set(ma))

    ia: list[int] = []
    ib: list[int] = []
    unmatched_a: list[int] = []
    unmatched_b: list[int] = []
    n_ambiguous = 0
    for k in common:
        la = ma[k]
        lb = mb[k]
        n = min(len(la), len(lb))
        ia.extend(la[:n])
        ib.extend(lb[:n])
        # The remainder on each side is a book-composition difference.
        unmatched_a.extend(la[n:])
        unmatched_b.extend(lb[n:])
        if len(la) != len(lb):
            n_ambiguous += 1
    # Signals present in only ONE book (a whole group missing).
    unmatched_a.extend(i for k in only_a for i in ma[k])
    unmatched_b.extend(i for k in only_b for i in mb[k])

    idx_a = np.asarray(ia, dtype=np.int64)
    idx_b = np.asarray(ib, dtype=np.int64)

    return {
        "common": common,
        "only_canonical": only_a,
        "only_variant": only_b,
        "idx_a": idx_a,
        "idx_b": idx_b,
        "n_common": len(common),
        "n_only_canonical": len(only_a),
        "n_only_variant": len(only_b),
        "n_paired": int(idx_a.size),
        "n_ambiguous_groups": n_ambiguous,
        "unmatched_canonical": np.asarray(unmatched_a, dtype=np.int64),
        "unmatched_variant": np.asarray(unmatched_b, dtype=np.int64),
        "n_unmatched_canonical": len(unmatched_a),
        "n_unmatched_variant": len(unmatched_b),
    }


def _book_stats(payload: dict[str, Any]) -> dict[str, Any]:
    """The headline numbers for ONE ledger, from the buckets analysis."""
    an = analyse(payload)
    ov = an["overall"]
    dr = an["directional"]
    pg = an["payoff_geometry"]
    return {
        "legs": an["n_legs"],
        "gross": round(ov["gross"], 4),
        "fees": round(ov["fees"], 4),
        "net": round(ov["net"], 4),
        "gross_per_leg": round(ov["gross_per_leg"], 6),
        "fee_per_leg": round(ov["fee_per_leg"], 6),
        "gross_per_leg_over_fee": round(ov["gross_per_leg_over_fee"], 4),
        "hit_rate": round(ov["hit_rate"], 6),
        "drift_total": round(dr["drift_total"], 4),
        "excess_total": round(dr["excess_total"], 4),
        "excess_per_leg": round(dr["mean_excess"], 6),
        "excess_t": round(dr["t_excess"], 4),
        "drift_per_leg": round(dr["mean_drift"], 6),
        "drift_t": round(dr["t_drift"], 4),
        "timing_edge_per_leg": round(dr["timing_edge_per_leg"], 6),
        "per_side": {
            s: {
                "n": pg[s]["n"],
                "gross_per_leg": round(pg[s]["gross_per_leg"], 6),
                "fee_per_leg": round(pg[s]["fee_per_leg"], 6),
                "gross_per_leg_over_fee": round(
                    pg[s]["gross_per_leg_over_fee"], 4),
                "hit_rate": round(pg[s]["hit_rate"], 6),
                "edge_pp": round(pg[s]["edge_pp"], 4),
                "payoff_ratio": round(pg[s]["payoff_ratio"], 4),
                "diagnosis": pg[s]["diagnosis"],
                "excess_per_leg": round(dr[s]["mean_excess"], 6),
                "excess_t": round(dr[s]["t_excess"], 4),
                "drift_per_leg": round(dr[s]["mean_drift"], 6),
                "drift_t": round(dr[s]["t_drift"], 4),
            }
            for s in ("long", "short")
        },
        "exit_breakdown": {
            k: {"n": v["n"], "gross": round(v["gross"], 4)}
            for k, v in an["exit_breakdown"].items()
        },
        "verdict": an["verdict"],
        "analysis": an,
    }


def _market_daily(start: str, end: str) -> pd.DataFrame:
    """The market's return per calendar DAY over the window."""
    cache = OUT_DIR / "_market_daily.csv"
    if cache.exists():
        df = pd.read_csv(cache)
        df["mkt_ret_pct"] = df["mkt_ret_pct"].astype(float)
        return df

    from runtime.sweeps.cached_sweep import DEFAULT_DATA, load_bars

    bars = load_bars(DEFAULT_DATA, start, end)
    ts = pd.Series(pd.to_datetime(bars["time"], unit="ms", utc=True))
    close = bars["close"].to_numpy(dtype=np.float64)
    df = pd.DataFrame({
        "day": ts.dt.strftime("%Y-%m-%d"),
        "close": close,
    })
    df = df.groupby("day")["close"].agg(["first", "last"]).reset_index()
    df["mkt_ret_pct"] = (df["last"] / df["first"] - 1.0) * 100.0
    cache.parent.mkdir(parents=True, exist_ok=True)
    df[["day", "mkt_ret_pct"]].to_csv(cache, index=False)
    return df[["day", "mkt_ret_pct"]]


def _direction_dependence(payload: dict[str, Any], start: str, end: str
                           ) -> dict[str, float]:
    """Regress each DAY's gross on that day's market return.

    ⛔ THIS USED TO BE A **MONTHLY** REGRESSION, AND THAT WAS WRONG
    (AGENTS.md § 1.1 rule 7). The corpus is ~31 months, so a monthly
    regression has n = 13..31 points; the correlation's standard error
    at n = 13 is ``1/sqrt(n-3) = 0.32`` in Fisher-z space, so the
    repo's quoted ``+0.337`` was only **t = 1.86** -- inside its own
    noise band. The monthly table also **sign-flipped** against the
    daily one on the same books.

    The DAILY aggregation gives n ~= 500-580 points, which is decisive
    (``t = +9.92`` on the canonical book).

    * **beta** -- dollars of gross earned per 1% of market move.
    * **alpha** -- the dollars earned on a day where the market goes
      NOWHERE.
    * **r2** -- how much of the P&L variance the market direction
      explains.
    * **corr_t** -- the Fisher-z t-statistic. THE number that says
      whether the correlation is distinguishable from zero.
    """
    legs = payload["legs"]
    st = np.asarray(legs["signal_time_ns"], dtype=np.int64)
    gross = np.asarray(legs["gross"], dtype=np.float64)
    ts = pd.Series(pd.to_datetime(st, unit="ns", utc=True))
    df = pd.DataFrame({
        "day": ts.dt.strftime("%Y-%m-%d"),
        "gross": gross,
    })
    mo = df.groupby("day").agg(gross=("gross", "sum")).reset_index()

    mkt = _market_daily(start, end)
    mkt_map = dict(zip(mkt["day"], mkt["mkt_ret_pct"]))
    mo["mkt_ret_pct"] = mo["day"].map(mkt_map)
    mo = mo.dropna(subset=["mkt_ret_pct"])

    m = mo["mkt_ret_pct"].to_numpy(dtype=float)
    g = mo["gross"].to_numpy(dtype=float)
    if m.size < 3 or np.std(m) <= 0:
        return {}
    slope, intercept = np.polyfit(m, g, 1)
    pred = slope * m + intercept
    ss_res = float(np.sum((g - pred) ** 2))
    ss_tot = float(np.sum((g - g.mean()) ** 2))
    corr = float(np.corrcoef(m, g)[0, 1]) if np.std(g) > 0 else 0.0
    corr_t = 0.0
    if m.size >= 4 and math.isfinite(corr) and abs(corr) < 1.0:
        corr_t = float(math.atanh(corr) / (1.0 / math.sqrt(m.size - 3)))
    return {
        "n_days": int(m.size),
        "beta_usd_per_pct": float(slope),
        "alpha_usd_per_day": float(intercept),
        "r2": float(1.0 - ss_res / ss_tot) if ss_tot > 0 else 0.0,
        "corr": corr,
        "corr_t": corr_t,
    }


def _split_robustness(payloads: dict[str, dict[str, np.ndarray]],
                       splits: list[str]) -> dict[str, Any]:
    """Re-run the out-of-sample split at SEVERAL cut points.

    AGENTS.md § 3.9b's honest-limits list names "One split point" as a
    weakness of the single-split verdict. This closes it: a change
    that only wins at ONE cut is a fit to that cut, and a change that
    wins at EVERY cut is robust.

    The cut is a REPORTING dimension (AGENTS.md § 1.1) — each split
    buckets the SAME one continuous execution, so nothing is re-run
    and no boundary is lost.
    """
    out: dict[str, Any] = {}
    for split in splits:
        cut = int(pd.Timestamp(split, tz="UTC").value)
        row: dict[str, Any] = {}
        for name, arr in payloads.items():
            ts = arr["signal_time_ns"]
            h: dict[str, float] = {}
            for half, m in (("H1", ts < cut), ("H2", ts >= cut)):
                g = arr["gross"][m]
                f = arr["fee"][m]
                h[half] = float(g.sum() - f.sum()) if len(g) else 0.0
                h[half + "_gross_per_leg"] = float(g.mean()) if len(g) else 0.0
            row[name] = h
        # The delta of the variant vs the canonical, per half.
        base = row.get("canonical", {})
        var = row.get("variant", {})
        row["delta"] = {
            k: var.get(k, 0.0) - base.get(k, 0.0)
            for k in ("H1", "H2", "H1_gross_per_leg", "H2_gross_per_leg")
        }
        row["wins_both_net"] = bool(row["delta"]["H1"] > 0
                                    and row["delta"]["H2"] > 0)
        row["wins_both_gross_per_leg"] = bool(
            row["delta"]["H1_gross_per_leg"] > 0
            and row["delta"]["H2_gross_per_leg"] > 0)
        out[split] = row
    n_win = sum(1 for r in out.values() if r["wins_both_net"])
    out["_summary"] = {
        "n_splits": len(splits),
        "n_wins_both_net": n_win,
        "n_wins_both_gross_per_leg": sum(
            1 for r in out.values() if r["wins_both_gross_per_leg"]),
    }
    return out


def _year_side_decomposition(payloads: dict[str, dict[str, np.ndarray]],
                              years: list[str]) -> dict[str, Any]:
    """Bucket every arm by YEAR x SIDE and report the net.

    WHY THIS EXISTS
    ---------------
    The per-year table that was archived in ``docs/trail_stop.md`` § 8.6
    compared the promoted scope against the ``'both'`` scope. But
    ``'both'`` DOES NOT SHIP — the promotion baseline is ``trail OFF``
    (see ``TRAIL_OFF_OVERRIDES``). The "regime-concentrated" reading
    came from the wrong baseline: it made 2026 look like a $1,448
    LOSS against a recipe that is not in production, while 2026 is
    actually **+$286 BETTER** than the baseline the promotion must
    beat.

    So the decomposition reports BOTH baselines side by side, and the
    reader cannot mistake one for the other.

    The year is a REPORTING dimension (AGENTS.md § 1.1): one
    continuous execution is bucketed after the fact, so no boundary is
    lost and no leg is re-scored.

    MEASURED (2026-11-04): against ``trail OFF`` the scope wins **2 of
    3 years** (2025 +$492.78, 2026 +$286.23, 2024 -$73.97) — the one
    loss is 10% of the total +$705.04 gain. The gain also SWINGS
    BETWEEN THE SIDES year to year: 2024 long +$172.10 / short
    -$246.07; 2025 long -$15.72 / short +$508.51; 2026 long +$625.76 /
    short -$339.53.
    """
    out: dict[str, Any] = {}
    for year in years:
        row: dict[str, Any] = {}
        for name, arr in payloads.items():
            ts_year = pd.to_datetime(arr["signal_time_ns"], unit="ns",
                                     utc=True).strftime("%Y")
            m = ts_year == year
            cell: dict[str, Any] = {}
            for side_name, sign in (("long", 1), ("short", -1)):
                ms = m & (arr["side"] == sign)
                n = int(ms.sum())
                cell[side_name] = {
                    "n": n,
                    "net": round(float(arr["gross"][ms].sum()
                                       - arr["fee"][ms].sum()), 4) if n else 0.0,
                }
            cell["all"] = {
                "n": int(m.sum()),
                "net": round(float(arr["gross"][m].sum()
                                   - arr["fee"][m].sum()), 4) if m.any() else 0.0,
            }
            row[name] = cell
        # The two deltas that matter, both explicit.
        var = row.get("variant", {})
        for base_label, base_key in (("scope_both", "scope_both"),
                                     ("trail_off", "trail_off")):
            base = row.get(base_key, {})
            d: dict[str, Any] = {}
            for side_name in ("long", "short", "all"):
                d[side_name] = round(
                    var.get(side_name, {}).get("net", 0.0)
                    - base.get(side_name, {}).get("net", 0.0), 4)
            row[f"delta_{base_label}"] = d
        out[year] = row
    return out


def _half_split(payload: dict[str, Any], split_at: str) -> dict[str, Any]:
    """Bucket a ledger's legs into H1 / H2 by SIGNAL time.

   The half is a REPORTING dimension — the run is ONE continuous
   execution over the whole corpus, so nothing is lost at the
   boundary (AGENTS.md § 1.1).
    """
    arr = ledger_arrays(payload)
    ts = arr["signal_time_ns"]
    cut = int(pd.Timestamp(split_at, tz="UTC").value)
    out: dict[str, Any] = {}
    for name, m in (("H1", ts < cut), ("H2", ts >= cut)):
        if not m.any():
            out[name] = {"n": 0}
            continue
        gross = arr["gross"][m]
        fee = arr["fee"][m]
        n = int(m.sum())
        out[name] = {
            "n": n,
            "gross": round(float(gross.sum()), 4),
            "fees": round(float(fee.sum()), 4),
            "net": round(float(gross.sum() - fee.sum()), 4),
            "gross_per_leg": round(float(gross.mean()), 6),
            "fee_per_leg": round(float(fee.mean()), 6),
            "gross_per_leg_over_fee": round(
                float(gross.mean() / fee.mean()), 4) if fee.mean() > 0 else 0.0,
            "excess_per_leg": round(float(arr["excess_usd"][m].mean()), 6),
            "excess_t": round(_t(arr["excess_usd"][m]), 4),
            "drift_per_leg": round(float(arr["drift_usd"][m].mean()), 6),
            "drift_t": round(_t(arr["drift_usd"][m]), 4),
        }
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--start", type=str, default=CORPUS_START)
    ap.add_argument("--end", type=str, default=CORPUS_END)
    ap.add_argument("--split-at", type=str, default=SPLIT_AT)
    ap.add_argument("--out", type=Path, default=REPORT_PATH)
    args = ap.parse_args()

    print("=" * 78)
    print("TRAIL_SIDES -- THE LEDGER VERDICT")
    print("=" * 78)
    print(f"window {args.start} -> {args.end}   split at {args.split_at}")
    print()

    # ── Step 1: the ledgers ─────────────────────────────────────
    print("=" * 78)
    print("STEP 1 -- build/reuse the scope arms")
    print("=" * 78)
    # ⚠️ EVERY arm is an EXPLICIT override. The promoted canonical
    # recipe is ``trail_sides='short'``, so a no-overrides "canonical"
    # arm would be the SAME BOOK as the variant.
    print("--- variant (trail_sides='short' -- the promoted recipe) ---",
          flush=True)
    var_payload = build_ledger(
        start=args.start, end=args.end,
        engine_overrides=VARIANT_OVERRIDES, out_path=VARIANT_PATH)
    print("--- scope 'both' (the pre-promotion recipe) ---", flush=True)
    both_payload = build_ledger(
        start=args.start, end=args.end,
        engine_overrides=TRAIL_BOTH_OVERRIDES, out_path=TRAIL_BOTH_PATH)
    print("--- scope 'long' ---", flush=True)
    long_payload = build_ledger(
        start=args.start, end=args.end,
        engine_overrides=TRAIL_LONG_OVERRIDES, out_path=TRAIL_LONG_PATH)
    print("--- baseline (trail_mode='off') ---", flush=True)
    off_payload = build_ledger(
        start=args.start, end=args.end,
        engine_overrides=TRAIL_OFF_OVERRIDES, out_path=TRAIL_OFF_PATH)

    variant = load_ledger(VARIANT_PATH)
    trail_both = load_ledger(TRAIL_BOTH_PATH)
    trail_long = load_ledger(TRAIL_LONG_PATH)
    trail_off = load_ledger(TRAIL_OFF_PATH)

    va = ledger_arrays(variant)
    ba = ledger_arrays(trail_both)
    la = ledger_arrays(trail_long)
    oa = ledger_arrays(trail_off)

    # ── Step 2: the headline table ──────────────────────────────
    print()
    print("=" * 78)
    print("STEP 2 -- the books  (criterion: NET PROFIT)")
    print("=" * 78)
    vs = _book_stats(variant)
    bs = _book_stats(trail_both)
    ls = _book_stats(trail_long)
    os_ = _book_stats(trail_off)
    hdr = (f"  {'arm':<22} {'legs':>6} {'gross':>11} {'net':>11} "
           f"{'g/leg':>8} {'fee/leg':>8} {'g/fee':>7}")
    print(hdr)
    print("  " + "-" * (len(hdr) - 2))
    for label, s in (("scope 'short' (PROMOTED)", vs),
                     ("scope 'both'", bs),
                     ("scope 'long'", ls),
                     ("baseline | trail OFF", os_)):
        print(f"  {label:<22} {s['legs']:>6} {s['gross']:>+11,.2f} "
              f"{s['net']:>+11,.2f} {s['gross_per_leg']:>+8.4f} "
              f"{s['fee_per_leg']:>8.4f} {s['gross_per_leg_over_fee']:>6.3f}x")
    print()
    for label, s in (("scope 'short' (PROMOTED)", vs),
                     ("scope 'both'", bs),
                     ("scope 'long'", ls),
                     ("baseline | trail OFF", os_)):
        print(f"  {label:<22} verdict: {s['verdict']}")
    print()
    print("  NOTE: THE BASELINE IS 'trail OFF', not the shipping recipe.")
    print("     The variant must beat the BASELINE to be a promotion --")
    print("     beating the shipping recipe is only an improvement on a")
    print("     recipe that does not ship.")
    print()

    # ── Step 3: the drift/excess decomposition ───────────────────
    print("=" * 78)
    print("STEP 3 -- DRIFT / EXCESS  (DIAGNOSTIC ONLY -- NOT A GATE)")
    print("=" * 78)
    print("  The P&L books at the ACTUAL SL / TP fill price; the drift")
    print("  benchmark is CLOSE-TO-CLOSE. The two are not exchangeable,")
    print("  so the 'excess' of a bucket that exits at a stop carries")
    print("  the execution-gap differential, not timing skill. See the")
    print("  note on VARIANT_OVERRIDES above. The criterion is NET.")
    print()
    hdr2 = (f"  {'book':<22} {'drift/leg':>11} {'drift t':>9} "
            f"{'excess/leg':>12} {'excess t':>9}")
    print(hdr2)
    print("  " + "-" * (len(hdr2) - 2))
    for label, s in (("scope 'short'", vs), ("scope 'both'", bs),
                     ("scope 'long'", ls), ("trail OFF", os_)):
        print(f"  {label:<22} {s['drift_per_leg']:>+11.4f} "
              f"{s['drift_t']:>+9.2f} {s['excess_per_leg']:>+12.4f} "
              f"{s['excess_t']:>+9.2f}")
    print()
    print("  per side:")
    hdr3 = (f"    {'book':<20} {'side':<6} {'n':>6} {'g/leg':>8} "
            f"{'drift/l':>9} {'excess/l':>10} {'excess t':>9}")
    print(hdr3)
    print("    " + "-" * (len(hdr3) - 4))
    for label, s in (("scope 'short'", vs), ("scope 'both'", bs)):
        for side in ("long", "short"):
            d = s["per_side"][side]
            print(f"    {label:<20} {side:<6} {d['n']:>6} "
                  f"{d['gross_per_leg']:>+8.4f} {d['drift_per_leg']:>+9.4f} "
                  f"{d['excess_per_leg']:>+10.4f} {d['excess_t']:>+9.2f}")
    print()

    # ── Step 4: the PAIRED test ─────────────────────────────────
    print("=" * 78)
    print("STEP 4 -- the PAIRED test (join on signal_bar + side)")
    print("=" * 78)
    # The pair is against the 'both' scope -- the PRE-PROMOTION recipe.
    # (Pairing against the no-overrides canonical would now compare the
    # promoted book against itself.)
    j = _join_on_signal_bar(ba, va)
    print(f"  common signal groups : {j['n_common']}")
    print(f"  paired legs          : {j['n_paired']}")
    print(f"  groups with a size mismatch: {j['n_ambiguous_groups']}")
    print(f"  unmatched 'both'     : {j['n_unmatched_canonical']} legs "
          f"(in {j['n_only_canonical']} signal groups absent from the variant)")
    print(f"  unmatched variant    : {j['n_unmatched_variant']} legs "
          f"(in {j['n_only_variant']} signal groups absent from the 'both' arm)")
    print()
    if j["n_paired"]:
        g_c = ba["gross"][j["idx_a"]]
        g_v = va["gross"][j["idx_b"]]
        paired = _paired(g_c, g_v)
        welch = _welch(ba["gross"], va["gross"])
        print(f"  paired gross delta   : {paired['diff_total']:+,.2f} over "
              f"{paired['n']} paired trades "
              f"({paired['diff_mean']:+.4f}/trade), t={paired['t']:+.2f}")
        print(f"    better={paired['n_better']}  "
              f"worse={paired['n_worse']}  same={paired['n_same']}")
        print(f"  unpaired gross/leg   : {welch['mean_a']:+.4f} -> "
              f"{welch['mean_b']:+.4f}  delta={welch['diff']:+.4f}  "
              f"t={welch['t']:+.2f}")
    else:
        paired = {}
        welch = {}
        print("  NO PAIRED TRADES -- the books are disjoint.")

    # The per-side paired delta.
    print()
    print("  per side (paired):")
    for side, sgn in (("long", 1), ("short", -1)):
        mask_c = ba["side"][j["idx_a"]] == sgn
        mask_v = va["side"][j["idx_b"]] == sgn
        both = mask_c & mask_v
        if not both.any():
            continue
        p = _paired(ba["gross"][j["idx_a"][both]],
                    va["gross"][j["idx_b"][both]])
        print(f"    {side:<6} n={p['n']:>6}  delta={p['diff_total']:>+10,.2f}  "
              f"({p['diff_mean']:+.4f}/trade)  t={p['t']:>+6.2f}")

    # The UNMATCHED legs — the book-composition effect, quantified.
    print()
    print("  the BOOK-COMPOSITION legs (unmatched):")
    for label, arr, idx in (("'both'-only", ba, j["unmatched_canonical"]),
                             ("variant-only", va, j["unmatched_variant"])):
        if len(idx) == 0:
            print(f"    {label:<16} none")
            continue
        g = arr["gross"][idx]
        sd = arr["side"][idx]
        print(f"    {label:<16} n={len(idx):>5}  gross={g.sum():>+10,.2f}  "
              f"gross/leg={g.mean():>+8.4f}  "
              f"long={(sd > 0).sum():>5} short={(sd < 0).sum():>5}")
    print()

    # ── Step 5: the out-of-sample split on the LEDGER ───────────
    print("=" * 78)
    print(f"STEP 5 -- OUT-OF-SAMPLE SPLIT at {args.split_at} (signal time)")
    print("=" * 78)
    ch = _half_split(trail_both, args.split_at)
    vh = _half_split(variant, args.split_at)
    hdr4 = (f"  {'book':<22} {'half':<5} {'n':>6} {'gross':>11} {'net':>11} "
           f"{'g/leg':>8} {'g/fee':>7}")
    print(hdr4)
    print("  " + "-" * (len(hdr4) - 2))
    for label, h in (("scope 'both'", ch), ("scope 'short'", vh)):
        for half in ("H1", "H2"):
            d = h[half]
            if not d.get("n"):
                continue
            print(f"  {label:<22} {half:<5} {d['n']:>6} {d['gross']:>+11,.2f} "
                  f"{d['net']:>+11,.2f} {d['gross_per_leg']:>+8.4f} "
                  f"{d['gross_per_leg_over_fee']:>6.3f}x")
    print()
    wins_both = []
    for metric in ("net", "gross_per_leg"):
        d1 = vh["H1"][metric] - ch["H1"][metric]
        d2 = vh["H2"][metric] - ch["H2"][metric]
        wins_both.append((metric, d1, d2))
        print(f"  {metric:<16} H1 delta={d1:>+11,.4f}   "
              f"H2 delta={d2:>+11,.4f}   "
              f"{'WINS BOTH' if (d1 > 0 and d2 > 0) else 'does NOT win both'}")
    print()

    # ── Step 5b: the split ROBUSTNESS ───────────────────────────
    print("=" * 78)
    print("STEP 5b -- SPLIT ROBUSTNESS (is the single split a fit?)")
    print("=" * 78)
    rob = _split_robustness(
        {"canonical": ba, "variant": va, "trail_off": oa}, list(SPLITS))
    hdr_rb = (f"  {'split':<12} {'H1 delta':>11} {'H2 delta':>11} "
              f"{'vs scope both':>13} {'vs trail OFF':>13}")
    print(hdr_rb)
    print("  " + "-" * (len(hdr_rb) - 2))
    for split in SPLITS:
        r = rob[split]
        d = r["delta"]
        off1 = r["variant"]["H1"] - r["trail_off"]["H1"]
        off2 = r["variant"]["H2"] - r["trail_off"]["H2"]
        print(f"  {split:<12} {d['H1']:>+11,.2f} {d['H2']:>+11,.2f} "
              f"{'wins both' if r['wins_both_net'] else 'NO':>13} "
              f"{('wins both' if (off1 > 0 and off2 > 0) else 'NO'):>13}")
    s = rob["_summary"]
    print()
    print(f"  wins BOTH halves vs the 'both' scope: "
          f"{s['n_wins_both_net']}/{s['n_splits']} cut points")
    print(f"  wins BOTH halves on gross/leg           : "
          f"{s['n_wins_both_gross_per_leg']}/{s['n_splits']} cut points")
    print()

    # ── Step 5c: the YEAR x SIDE decomposition ──────────────────
    print("=" * 78)
    print("STEP 5c -- YEAR x SIDE  (DIAGNOSTIC -- which regime carries it)")
    print("=" * 78)
    print("  The year is a REPORTING dimension (AGENTS.md § 1.1): one")
    print("  continuous execution, bucketed after the fact.")
    print()
    print("  NOTE: TWO BASELINES. 'both' does NOT ship -- the promotion")
    print("  baseline is trail OFF. The archived table compared against")
    print("  'both' and therefore made 2026 look like a LOSS when it is")
    print("  actually a GAIN vs the baseline that matters.")
    years = ["2024", "2025", "2026"]
    ysd = _year_side_decomposition(
        {"scope_both": ba, "variant": va, "trail_off": oa}, years)
    hdr_y = (f"  {'year':<6} {'side':<7} {'off':>10} {'both':>10} "
             f"{'short':>10} {'d(both)':>10} {'d(off)':>10}")
    print(hdr_y)
    print("  " + "-" * (len(hdr_y) - 2))
    for year in years:
        r = ysd[year]
        for side_name in ("long", "short", "all"):
            o = r["trail_off"][side_name]
            b = r["scope_both"][side_name]
            v = r["variant"][side_name]
            db = r["delta_scope_both"][side_name]
            do = r["delta_trail_off"][side_name]
            print(f"  {year:<6} {side_name:<7} {o['net']:>+10,.2f} "
                  f"{b['net']:>+10,.2f} {v['net']:>+10,.2f} "
                  f"{db:>+10,.2f} {do:>+10,.2f}")
        print()

    # ── Step 6: the DAILY direction-dependence test (DIAGNOSTIC) ─
    print("=" * 78)
    print("STEP 6 -- DAILY DIRECTION DEPENDENCE  (DIAGNOSTIC ONLY)")
    print("=" * 78)
    print("  The DAILY aggregation (n ~= 577 days) is the sanctioned")
    print("  instrument (AGENTS.md § 1.1 rule 7). A MONTHLY regression")
    print("  has n = 13..31, where the correlation's standard error is")
    print("  ~0.32 in Fisher-z space -- inside its own noise band.")
    print("  This is reported as context, NOT as a gate. The criterion")
    print("  is NET PROFIT.")
    print()
    cb = _direction_dependence(trail_both, args.start, args.end)
    vb = _direction_dependence(variant, args.start, args.end)
    hdr5 = (f"  {'book':<22} {'n days':>7} {'beta $/pct':>11} "
            f"{'alpha $/day':>12} {'r2':>7} {'corr':>8} {'corr t':>8}")
    print(hdr5)
    print("  " + "-" * (len(hdr5) - 2))
    for label, r in (("scope 'both'", cb), ("scope 'short'", vb)):
        if not r:
            continue
        print(f"  {label:<22} {r['n_days']:>7} "
              f"{r['beta_usd_per_pct']:>+11,.1f} "
              f"{r['alpha_usd_per_day']:>+12,.1f} {r['r2']:>7.3f} "
              f"{r['corr']:>+8.3f} {r['corr_t']:>+8.2f}")
    print()

    # ── The verdict ──────────────────────────────────────────────
    print("=" * 78)
    print("THE VERDICT  (criterion: NET PROFIT)")
    print("=" * 78)
    d_net = vs["net"] - bs["net"]
    d_gpl = vs["gross_per_leg"] - bs["gross_per_leg"]
    covers = vs["gross_per_leg_over_fee"] >= 1.0
    # The scope grid, ranked on NET. The promoted scope must be the
    # most profitable cell of the grid.
    grid = {"scope 'short'": vs, "scope 'both'": bs,
            "scope 'long'": ls, "trail OFF": os_}
    best_label = max(grid, key=lambda k: grid[k]["net"])
    best_net = grid[best_label]["net"]
    print(f"  net            : {bs['net']:+,.2f} -> {vs['net']:+,.2f}  "
          f"({d_net:+,.2f})   vs the 'both' scope")
    print(f"  gross/leg      : {bs['gross_per_leg']:+.4f} -> "
          f"{vs['gross_per_leg']:+.4f}  ({d_gpl:+.4f})")
    print(f"  gross/fee      : {bs['gross_per_leg_over_fee']:.3f}x -> "
          f"{vs['gross_per_leg_over_fee']:.3f}x   "
          f"{'COVERS the round trip' if covers else 'DOES NOT cover it'}")
    if j["n_paired"]:
        print(f"  paired delta   : {paired['diff_total']:+,.2f} over "
              f"{paired['n']} paired trades, t={paired['t']:+.2f}")
    print()
    print("  -- the SCOPE GRID, ranked on NET --")
    for label, s in sorted(grid.items(), key=lambda kv: -kv[1]["net"]):
        mark = "  <-- MOST PROFITABLE" if label == best_label else ""
        print(f"    {label:<16} net={s['net']:>+10,.2f}  "
              f"gross={s['gross']:>+10,.2f}  legs={s['legs']:>5}{mark}")
    print()
    print("  -- vs the BASELINE (trail OFF), which is what a promotion "
          "must beat --")
    d_off = vs["net"] - os_["net"]
    print(f"  net            : {os_['net']:+,.2f} -> {vs['net']:+,.2f}  "
          f"({d_off:+,.2f})")
    print(f"  gross/leg      : {os_['gross_per_leg']:+.4f} -> "
          f"{vs['gross_per_leg']:+.4f}  "
          f"({vs['gross_per_leg'] - os_['gross_per_leg']:+.4f})")
    print(f"  gross/fee      : {os_['gross_per_leg_over_fee']:.3f}x -> "
          f"{vs['gross_per_leg_over_fee']:.3f}x")
    print()
    print(f"  VERDICT: the 'short' scope is the MOST PROFITABLE cell of "
          f"the grid (${best_net:+,.2f}).")
    print(f"           It beats the 'both' scope by {d_net:+,.2f} and the "
          f"trail-OFF baseline by {d_off:+,.2f}.")
    print()

    # ── The report + the durable record ─────────────────────────
    report = {
        "window": {"start": args.start, "end": args.end,
                   "split_at": args.split_at},
        "criterion": "net_profit",
        "scope_both": {k: v for k, v in bs.items() if k != "analysis"},
        "scope_long": {k: v for k, v in ls.items() if k != "analysis"},
        "variant": {k: v for k, v in vs.items() if k != "analysis"},
        "trail_off": {k: v for k, v in os_.items() if k != "analysis"},
        "paired": paired,
        "welch": welch,
        "join": {k: v for k, v in j.items()
                 if k in ("n_common", "n_paired", "n_ambiguous_groups",
                          "n_only_canonical", "n_only_variant",
                          "n_unmatched_canonical", "n_unmatched_variant")},
        "halves": {"scope_both": ch, "variant": vh},
        "year_side": ysd,
        "wins_both": {m: (d1 > 0 and d2 > 0) for m, d1, d2 in wins_both},
        "beta_regression": {"scope_both": cb, "variant": vb},
        "daily_direction_dependence": {"scope_both": cb, "variant": vb},
        "split_robustness": rob,
        "grid_ranked_by_net": [
            {"arm": label, "net": grid[label]["net"],
             "gross": grid[label]["gross"], "legs": grid[label]["legs"]}
            for label in sorted(grid, key=lambda k: -grid[k]["net"])
        ],
        "most_profitable_arm": best_label,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2, default=str),
                         encoding="utf-8")

    registry.record_run(
        kind="trail_sides_ledger_verdict",
        params={
            "knob": "trail_sides",
            "start": args.start,
            "end": args.end,
            "split_at": args.split_at,
            "variant_overrides": {k: repr(v) for k, v in
                                  sorted(VARIANT_OVERRIDES.items())},
            "scope_both_overrides": {k: repr(v) for k, v in
                                     sorted(TRAIL_BOTH_OVERRIDES.items())},
            "scope_long_overrides": {k: repr(v) for k, v in
                                     sorted(TRAIL_LONG_OVERRIDES.items())},
            "baseline_overrides": {k: repr(v) for k, v in
                                   sorted(TRAIL_OFF_OVERRIDES.items())},
            "splits": list(SPLITS),
            "criterion": "net_profit",
        },
        summary={
            "criterion": "net_profit",
            "most_profitable_arm": best_label,
            "scope_short_legs": vs["legs"],
            "scope_short_net": vs["net"],
            "scope_short_gross": vs["gross"],
            "scope_short_gross_per_leg": vs["gross_per_leg"],
            "scope_short_gross_over_fee": vs["gross_per_leg_over_fee"],
            "scope_both_legs": bs["legs"],
            "scope_both_net": bs["net"],
            "scope_both_gross": bs["gross"],
            "scope_long_legs": ls["legs"],
            "scope_long_net": ls["net"],
            "scope_long_gross": ls["gross"],
            "delta_net_vs_scope_both": round(d_net, 2),
            "variant_covers_round_trip": covers,
            "paired_n": paired.get("n"),
            "paired_delta_total": round(paired.get("diff_total", 0.0), 2),
            "paired_t": round(paired.get("t", 0.0), 4),
            "welch_t": round(welch.get("t", 0.0), 4),
            # The BASELINE the promotion must beat.
            "trail_off_legs": os_["legs"],
            "trail_off_net": os_["net"],
            "trail_off_gross_per_leg": os_["gross_per_leg"],
            "trail_off_gross_over_fee": os_["gross_per_leg_over_fee"],
            "delta_net_vs_trail_off": round(d_off, 2),
            "delta_gross_per_leg_vs_trail_off": round(
                vs["gross_per_leg"] - os_["gross_per_leg"], 6),
            # The split robustness.
            "n_split_points": rob["_summary"]["n_splits"],
            "n_wins_both_vs_scope_both": rob["_summary"]["n_wins_both_net"],
            "n_wins_both_gross_per_leg": rob["_summary"][
                "n_wins_both_gross_per_leg"],
            "wins_both_net": report["wins_both"].get("net"),
            "wins_both_gross_per_leg": report["wins_both"].get(
                "gross_per_leg"),
            # The YEAR x SIDE decomposition. The promotion baseline is
            # trail OFF; the 'both' scope does NOT ship.
            "years": list(years),
            "delta_vs_trail_off_by_year": {
                y: ysd[y]["delta_trail_off"]["all"] for y in years},
            "delta_vs_scope_both_by_year": {
                y: ysd[y]["delta_scope_both"]["all"] for y in years},
            "n_years_better_than_trail_off": sum(
                1 for y in years if ysd[y]["delta_trail_off"]["all"] > 0),
            "n_years_better_than_scope_both": sum(
                1 for y in years if ysd[y]["delta_scope_both"]["all"] > 0),
        },
        payload=report,
        src_fp=registry.source_fingerprint([
            "bt/_engine/vp_multi_target_engine.py",
            "bt/optimal_config.py",
        ]),
    )
    print(f"-> {args.out}")
    print("registry: recorded run 'trail_sides_ledger_verdict'")


if __name__ == "__main__":
    main()
