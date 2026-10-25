"""LEG LEDGER — the corpus-wide, per-LEG record of the canonical recipe.

WHY THIS EXISTS (2026-10-25)
===========================

Every promotion verdict in this repo used to be computed on **monthly
slices** classified by the month's own market return:

    UP / DOWN / FLAT  ->  "does the strategy make money when the
                           market chops?"

For a **high-frequency** strategy that is the wrong unit, for three
reasons that this module exists to fix.

1. **A month is not a sample of the strategy's distribution.** After
   the vol gate the recipe produces ~40 legs a month. "8 months" is
   n=8 draws from a high-variance process, and the month's own
   return tells us nothing about the distribution the strategy
   actually experiences.

2. **The month boundary is an artefact, not a market event.** The
   market does not reset on the 1st. But the ENGINE's state does:
   the p95 cap (7-day lookback), the swing cap (24h lookback) and
   the VP profile (7-day rolling) are all **history-dependent**, and
   in backtest mode the p95 cap returns ``inf`` until its window
   fills — so a cold chunk's first ~8 days trade with UNCAPPED
   targets.

   Measured on 2025 Q1 (canonical recipe, 1.00 oz):

   | run | legs in Q1 | vs continuous |
   |---|---|---|
   | **continuous** (one run over the quarter) | **126** | — |
   | monthly chunks (Jan + Feb + Mar) | 113 | **89.7%** |

   So monthly slicing **silently discards ~10% of the trade
   universe**. That is not a measurement preference — it is a
   correctness bug in the evaluation harness.

3. **The regime label summarises a distribution we do not have**, at
   a granularity unrelated to the trade.

THE REPLACEMENT
===============

**Run the strategy continuously, and bucket the LEGS.**

This module produces the raw material: one record per closed leg,
carrying everything needed to answer any question the old monthly
paradigm could answer, plus several it could not.

CONTINUOUS RUN, MONTHS AS A REPORTING DIMENSION
-----------------------------------------------

The strategy is executed over **one continuous window**. Month
boundaries then become a *reporting* dimension computed from the
leg timestamps — never an *execution* boundary. Nothing is lost at
a boundary, and the caps are warm everywhere except the corpus's
first 8 days (which are excluded by a warmup prefix).

This is the single most important property of the ledger: **the
execution window and the reporting window are decoupled.**

THE COUNTERFACTUALS (and the one that is NOT a tautology)
==========================================================

Per leg we record two counterfactuals. One of them is a trap and the
other is the real test.

**Trap — the "flip".** The engine computes::

    gross = (exit_price - entry_price) * direction * qty

So negating ``direction`` negates ``gross`` **exactly**. A
"counterfactual" built by flipping the side while holding the entry
and exit fixed is therefore ``-gross``: a **tautology**. It carries
zero information and any conclusion drawn from it is vacuous. It is
recorded here only so that the mistake is documented and cannot be
re-introduced.

**Real test — the passive drift.** What would a **passive hold of the
same side over the same window** have made? That needs the market::

    drift  = (close[exit_bar] - close[entry_bar]) * direction * qty
    excess = gross - drift

``drift`` is the part of the P&L that any position of that size and
side would have earned by being there — the **directional/beta
component**. ``excess`` is the part attributable to the strategy's
**entry timing** — the component a directional proxy cannot have.

The decomposition is exact (it is an algebraic identity, not an
approximation) and it answers the beta-proxy question **at the leg
level, with no monthly aggregation at all**.

THE LOCAL MARKET CONTEXT
========================

Each leg also carries the market's own drift over the hour before
its SIGNAL bar::

    local_drift_1h = (close[signal_bar] - close[signal_bar - 3600s])

This is the **per-leg replacement for the monthly regime label**. It
lets the decisive chop test be asked at the leg's own granularity:
"do the legs whose local hour was FLAT have a positive edge?" —
instead of "do the months whose close moved <3% have a positive
edge?".

EFFICIENCY
==========

The engine run is the expensive part, so the ledger is **cached to
disk** and keyed by a fingerprint of everything that can change the
legs:

* the data file (size + mtime)
* the execution window
* the warmup prefix length
* the full engine kwargs (canonical recipe + any overrides)
* the artifact-producing source files' hash

The cached payload is a compact **columnar** record (lists of
floats/ints), not a pickle of dataclasses, so it is small and
loads in milliseconds.

Usage
-----
    python -m runtime.diagnostics.leg_ledger                 # build/reuse
    python -m runtime.diagnostics.leg_ledger --refresh       # force rebuild
    python -m runtime.diagnostics.leg_ledger --start 2024-01-01 \
        --end 2026-07-16
"""
from __future__ import annotations

import argparse
import hashlib
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
LEDGER_PATH = _REPO_ROOT / "runtime" / "diagnostics" / "_reports" / "leg_ledger.json"

# The CANONICAL recipe has NO engine overrides. The canonical ledger
# path is reserved for it.
#
# ⚠️ WHY THIS IS A HARD RULE (2026-10-25). A variant run
# (``engine_overrides={...}``) was written to the canonical path and
# silently replaced the canonical ledger. Every consumer --
# ``leg_buckets``, ``leg_random_entry``, ``warmup_prefix_effect``,
# ``chunk_vs_continuous``, and the test suite -- then analysed the
# VARIANT while believing they were reading the canonical recipe.
# ``tests/test_level_vs_timing.py::test_ledger_excess_agrees_with_the_
# timing_diagnostic`` caught it only because the gross totals
# disagreed ($751.77 vs $510.58).
#
# The lesson is the repo's own: **a variant must never be able to
# masquerade as the canonical artifact.** A variant ledger goes to
# its own file, and the guard below makes that structural rather
# than a convention.
CANONICAL_OVERRIDES: dict[str, Any] = {}

# The full canonical corpus.
DEFAULT_START = "2024-01-01"
DEFAULT_END = "2026-07-16"

# The warmup prefix. It must exceed the LONGEST history-dependent
# lookback the recipe uses, or the run's first bars are uncapped.
#
#   p95 cap  : 7 days  (TP_P95_LOOKBACK_SECS = 604_800)  + 1h inner
#   swing cap: 1 day  (SWING_LOOKBACK_SECS  = 86_400)
#   VP set   : 7 days rolling (VP_ROLLING_SECS = 604_800)
#
# 14 days covers all three with margin. The prefix's legs are
# DISCARDED — only the warmup effect is kept.
WARMUP_DAYS = 14

# The trailing window for the per-leg local market context.
LOCAL_CONTEXT_SECS = 3600

# The per-leg record schema. Kept as parallel arrays so the cached
# payload is compact and the analysis is vectorised.
LEG_FIELDS: tuple[str, ...] = (
    "side",             # +1 long, -1 short
    "outcome",          # +1 gross>0, -1 gross<=0
    "gross",            # realised gross P&L (USD, canonical 1.00 oz)
    "net",              # realised net P&L (USD)
    "fee",              # fee charged (USD)
    "move_usd",         # |exit - entry| * qty -- the magnitude of the move
    "qty",              # the leg's own size (a TP1 remainder is half)
    "drift_usd",        # PASSIVE hold of the same side, same window
    "excess_usd",       # gross - drift_usd -- the TIMING component
    "flip_gross",       # TAUTOLOGY (-gross). Recorded so the trap is
                        # documented; DO NOT draw conclusions from it.
    "local_drift_1h",   # market drift over the hour before the signal
    "entry_price",
    "exit_price",
    "sl_dist",          # initial SL distance (USD)
    "tp_dist",          # initial TP distance (USD)
    "hold_secs",
    "signal_time_ns",   # when the SIGNAL fired (the causal bar)
    "entry_time_ns",    # when the order FILLED
    "exit_time_ns",
    "entry_bar",
    "exit_bar",
    "signal_bar",      # the SIGNAL bar (the causal decision point)
    "exit_code",        # encoded exit reason
    "source_code",      # immediate | sniper
)

# Exit reasons are encoded as small ints so the cached payload stays
# compact. The order is FIXED — the ledger file records the list.
EXIT_REASONS: tuple[str, ...] = (
    "tp", "tp1", "tp2", "sl", "inv", "eod", "soft_sl",
    "contra_close", "replaced_by_new_signal", "other",
)
_EXIT_CODE = {r: i for i, r in enumerate(EXIT_REASONS)}

SOURCES: tuple[str, ...] = ("immediate", "sniper", "other")
_SOURCE_CODE = {s: i for i, s in enumerate(SOURCES)}

# Fields stored as int64 / int8 rather than float64.
_INT_FIELDS = frozenset({
    "side", "outcome", "exit_code", "source_code",
    "signal_time_ns", "entry_time_ns", "exit_time_ns",
    "entry_bar", "exit_bar", "signal_bar",
})
_I8_FIELDS = frozenset({"side", "outcome", "exit_code", "source_code"})


# ─────────────────────────────────────────────────────────────────────
# Fingerprint
# ─────────────────────────────────────────────────────────────────────
def _source_hash() -> str:
    """SHA1 over the artifact-producing source files.

    Mirrors ``runtime.sweeps.cached_sweep._source_hash`` so a change
    to the engine or the detector invalidates the ledger exactly when
    it invalidates the artifact cache.
    """
    rels = (
        "bt/_engine/ict_signals.py",
        "bt/_engine/volume_profile.py",
        "bt/_engine/swing_cap.py",
        "bt/_engine/rolling_p95.py",
        "bt/_engine/market_structure.py",
        "bt/_engine/timebase.py",
        "bt/_engine/vp_multi_target_engine.py",
        "bt/optimal_config.py",
    )
    h = hashlib.sha1()
    for rel in rels:
        h.update(rel.encode("utf-8"))
        try:
            h.update((_REPO_ROOT / rel).read_bytes())
        except OSError:
            h.update(b"<missing>")
    return h.hexdigest()[:16]


def ledger_fingerprint(*, data: Path, start: str, end: str,
                        engine_overrides: dict[str, Any],
                        warmup_days: int = WARMUP_DAYS) -> str:
    """Fingerprint of everything that can change the legs."""
    st = data.stat()
    payload = json.dumps(
        {
            "data": str(data.resolve()),
            "size": st.st_size,
            "mtime_ns": st.st_mtime_ns,
            "start": start,
            "end": end,
            "engine_overrides": {k: repr(v) for k, v in
                                 sorted(engine_overrides.items())},
            "warmup_days": warmup_days,
            "local_context_secs": LOCAL_CONTEXT_SECS,
            "lots": FOREX_LOTS,
            "contract_size": FOREX_CONTRACT_SIZE,
            "src": _source_hash(),
        },
        sort_keys=True,
    )
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


def _overrides_are_canonical(engine_overrides: dict[str, Any]) -> bool:
    """True when the overrides describe the CANONICAL recipe."""
    return dict(engine_overrides or {}) == CANONICAL_OVERRIDES


def _guard_canonical_path(*, out_path: Path,
                          engine_overrides: dict[str, Any]) -> None:
    """Refuse to write a VARIANT ledger over the CANONICAL artifact.

    The canonical ledger is the recipe's evidence record. If a variant
    can write to it, every downstream reader (``leg_buckets``,
    ``leg_random_entry``, the placebo harness, the test suite) ends up
    analysing a DIFFERENT STRATEGY while believing it is reading the
    canonical one — and the only symptom is that the numbers quietly
    stop agreeing with each other.

    This is the "a default silently substituted for an intent" bug
    class the repo already documents (see AGENTS.md, ``sl_ceiling_
    sweep``): the branch that SHOULD have been measured was instead
    measured as the canonical value. Here it is the mirror image: a
    variant was measured as the canonical value.

    Two ways out, both explicit:
      * write the variant to its OWN file (any path other than the
        canonical one), or
      * pass ``allow_variant_canonical=True`` to assert that you are
        deliberately replacing the canonical artifact.
    """
    if out_path.resolve() != LEDGER_PATH.resolve():
        return  # a variant writing to its own file is fine
    if _overrides_are_canonical(engine_overrides):
        return  # the canonical recipe writing the canonical artifact
    raise SystemExit(
        "REFUSING to write a VARIANT ledger to the canonical path.\n"
        f"  out_path    : {out_path}\n"
        f"  overrides   : {engine_overrides!r}\n"
        "  canonical   : {} (no overrides)\n"
        "\n"
        "The canonical ledger is the recipe's evidence record. Every\n"
        "consumer (leg_buckets, leg_random_entry, warmup_prefix_effect,\n"
        "chunk_vs_continuous, the test suite) reads it as the CANONICAL\n"
        "recipe, so a variant written here is analysed as if it were\n"
        "the canonical strategy.\n"
        "\n"
        "Fix: give the variant its own output path, e.g.\n"
        "    build_ledger(engine_overrides=ov,\n"
        "                out_path=OUT_DIR / 'leg_ledger_variant.json')\n"
        "or, if you really mean to replace the canonical artifact,\n"
        "    build_ledger(..., allow_variant_canonical=True)"
    )


# ─────────────────────────────────────────────────────────────────────
# The engine run
# ─────────────────────────────────────────────────────────────────────
def _leg_rows(trades: list, bars: pd.DataFrame) -> dict[str, np.ndarray]:
    """Turn the engine's ``ClosedTrade`` list into columnar arrays.

    ``entry_bar`` / ``exit_bar`` / ``signal_bar`` are indices into the
    bars frame the engine walked, so the market context is a direct
    array lookup — no re-bucketing, no interpolation.
    """
    n = len(trades)
    out: dict[str, np.ndarray] = {
        "side": np.zeros(n, dtype=np.int8),
        "outcome": np.zeros(n, dtype=np.int8),
        "gross": np.zeros(n, dtype=np.float64),
        "net": np.zeros(n, dtype=np.float64),
        "fee": np.zeros(n, dtype=np.float64),
        "move_usd": np.zeros(n, dtype=np.float64),
        "drift_usd": np.zeros(n, dtype=np.float64),
        "excess_usd": np.zeros(n, dtype=np.float64),
        "flip_gross": np.zeros(n, dtype=np.float64),
        "local_drift_1h": np.zeros(n, dtype=np.float64),
        "entry_price": np.zeros(n, dtype=np.float64),
        "exit_price": np.zeros(n, dtype=np.float64),
        "sl_dist": np.zeros(n, dtype=np.float64),
        "tp_dist": np.zeros(n, dtype=np.float64),
        "hold_secs": np.zeros(n, dtype=np.float64),
        "signal_time_ns": np.zeros(n, dtype=np.int64),
        "entry_time_ns": np.zeros(n, dtype=np.int64),
        "exit_time_ns": np.zeros(n, dtype=np.int64),
        "entry_bar": np.zeros(n, dtype=np.int64),
        "exit_bar": np.zeros(n, dtype=np.int64),
        "signal_bar": np.zeros(n, dtype=np.int64),
        "exit_code": np.zeros(n, dtype=np.int8),
        "source_code": np.zeros(n, dtype=np.int8),
        "qty": np.zeros(n, dtype=np.float64),
    }
    if n == 0:
        return out

    from bt._engine.timebase import to_ns_arr

    times_ns = to_ns_arr(bars)
    close = bars["close"].to_numpy(dtype=np.float64)
    n_bars = len(close)

    # The local context index: the bar LOCAL_CONTEXT_SECS before the
    # signal bar. ``np.searchsorted`` on the ns timestamps is O(log n)
    # per leg and exact to the bar.
    sig_bar = np.zeros(n, dtype=np.int64)
    entry_bar = np.zeros(n, dtype=np.int64)
    exit_bar = np.zeros(n, dtype=np.int64)

    for i, t in enumerate(trades):
        d = int(t.direction)
        g = float(t.pnl_gross_usd)
        out["side"][i] = 1 if d > 0 else -1
        out["outcome"][i] = 1 if g > 0 else -1
        out["gross"][i] = g
        out["net"][i] = float(t.pnl_net_usd)
        out["fee"][i] = float(getattr(t, "fee_usd", 0.0))

        e_px = float(t.entry_price)
        x_px = float(t.exit_price)
        out["entry_price"][i] = e_px
        out["exit_price"][i] = x_px
        qty = float(getattr(t, "qty_btc", 0.0))
        out["qty"][i] = qty
        out["move_usd"][i] = abs(x_px - e_px) * qty

        # TAUTOLOGY (documented trap): negating direction negates
        # gross exactly, because gross = (exit-entry)*direction*qty.
        out["flip_gross"][i] = -g

        eb = int(getattr(t, "entry_bar", 0))
        xb = int(getattr(t, "exit_bar", 0))
        sb = int(getattr(t, "signal_bar", 0))
        entry_bar[i] = eb
        exit_bar[i] = xb
        sig_bar[i] = sb
        out["signal_bar"][i] = sb

        out["sl_dist"][i] = abs(float(getattr(t, "initial_sl_distance_usd", 0.0)))
        out["tp_dist"][i] = abs(float(getattr(t, "initial_tp_distance_usd", 0.0)))
        out["hold_secs"][i] = float(getattr(t, "hold_secs", 0.0))
        out["entry_time_ns"][i] = int(t.entry_time_ns)
        out["exit_time_ns"][i] = int(t.exit_time_ns)

        er = str(getattr(t, "exit_reason", "other"))
        out["exit_code"][i] = _EXIT_CODE.get(er, len(EXIT_REASONS) - 1)
        sr = str(getattr(t, "source", "other"))
        out["source_code"][i] = _SOURCE_CODE.get(sr, len(SOURCES) - 1)

    out["entry_bar"] = entry_bar
    out["exit_bar"] = exit_bar

    # The SIGNAL time — the causal decision point.
    sig_idx = np.clip(sig_bar, 0, max(n_bars - 1, 0))
    out["signal_time_ns"] = times_ns[sig_idx] if n_bars else np.zeros(n, np.int64)

    # ── The REAL counterfactual: the passive drift ──────────────
    # A passive hold of the SAME side over the SAME window.
    # NOTE: the qty is PER LEG — a TP1 remainder leg is half size,
    # so the last leg's qty must not be reused here.
    e_idx = np.clip(entry_bar, 0, max(n_bars - 1, 0))
    x_idx = np.clip(exit_bar, 0, max(n_bars - 1, 0))
    drift = ((close[x_idx] - close[e_idx])
             * out["side"].astype(np.float64) * out["qty"])
    out["drift_usd"] = drift
    out["excess_usd"] = out["gross"] - drift

    # ── The local market context at the SIGNAL bar ─────────────
    if n_bars:
        target = times_ns[sig_idx] - int(LOCAL_CONTEXT_SECS * 1_000_000_000)
        pos = np.searchsorted(times_ns, target, side="left")
        pos = np.clip(pos, 0, n_bars - 1)
        out["local_drift_1h"] = close[sig_idx] - close[pos]

    return out


def run_window(*, data: Path, scope: str, exec_start: str, exec_end: str,
               score_start: str, score_end: str,
               engine_overrides: dict[str, Any] | None = None
               ) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    """Run one continuous window; keep the legs whose SIGNAL is scored.

    ``exec_start`` may precede ``score_start`` — that is the warmup
    prefix. The prefix's legs are dropped; its caps are kept.
    """
    from runtime.sweeps.cached_sweep import ArtifactCache, load_bars

    bars = load_bars(data, exec_start, exec_end)
    if bars.empty:
        z = np.zeros(0)
        return {k: z.copy() for k in LEG_FIELDS}, {
            "scope": scope, "bars": 0, "legs": 0, "skipped": 0}

    art = ArtifactCache(data, scope, exec_start, exec_end).build_or_load(bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))
    ek = build_engine_kwargs(
        spot=spot,
        swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
        structure=art["structure"], vol_state=art["vol_state"],
        **(engine_overrides or {}),
    )
    t0 = time.perf_counter()
    res, _ = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE,
        engine_kwargs=ek,
        # The equity curve is a per-bar float64 array (330 MB on 1y).
        # The ledger does not use it — the leg records are the point.
        record_equity_curve=False,
    )
    wall = time.perf_counter() - t0

    trades = list(res.trades)
    cols = _leg_rows(trades, bars)

    # Keep only the legs whose SIGNAL time falls in the score window.
    # The SIGNAL bar is the causal decision point, so it is the right
    # key for "which period does this trade belong to".
    s_lo = int(pd.Timestamp(score_start, tz="UTC").value)
    s_hi = int(pd.Timestamp(score_end, tz="UTC").value)
    st = cols["signal_time_ns"]
    keep = (st >= s_lo) & (st < s_hi)

    out = {k: (v[keep] if len(v) else v) for k, v in cols.items()}

    meta = {
        "scope": scope,
        "exec_start": exec_start,
        "exec_end": exec_end,
        "score_start": score_start,
        "score_end": score_end,
        "bars": int(len(bars)),
        "n_zones": int(len(art["zones"])),
        "cache_hit": bool(art.get("_cache_hit", False)),
        "engine_secs": wall,
        "legs_total": len(trades),
        "legs_kept": int(keep.sum()),
        "legs_skipped_warmup": int((~keep).sum()),
        "n_dropped_vol_state": int(getattr(res, "n_dropped_vol_state", 0)),
        "n_signals_emitted": int(getattr(res, "n_signals_emitted", 0)),
    }
    return out, meta


# ─────────────────────────────────────────────────────────────────────
# Assembly
# ─────────────────────────────────────────────────────────────────────
def _encode(cols: dict[str, np.ndarray]) -> dict[str, list]:
    """Columnar payload -> JSON-safe lists."""
    out: dict[str, list] = {}
    for k, v in cols.items():
        if k in _I8_FIELDS:
            out[k] = [int(x) for x in v]
        elif k in _INT_FIELDS:
            out[k] = [int(x) for x in v]
        else:
            out[k] = [float(x) for x in v]
    return out


def build_ledger(*, data: Path = DEFAULT_DATA,
                 start: str = DEFAULT_START, end: str = DEFAULT_END,
                 engine_overrides: dict[str, Any] | None = None,
                 warmup_days: int = WARMUP_DAYS,
                 out_path: Path = LEDGER_PATH,
                 refresh: bool = False,
                 allow_variant_canonical: bool = False,
                 verbose: bool = True) -> dict[str, Any]:
    """Build (or reuse) the corpus-wide leg ledger.

    ``allow_variant_canonical`` is the escape hatch for deliberately
    replacing the canonical artifact with a variant run. It exists so
    that the guard in :func:`_guard_canonical_path` is a DECISION
    rather than an obstacle.
    """
    engine_overrides = dict(engine_overrides or {})
    if not allow_variant_canonical:
        _guard_canonical_path(out_path=out_path,
                              engine_overrides=engine_overrides)
    fp = ledger_fingerprint(data=data, start=start, end=end,
                            engine_overrides=engine_overrides,
                            warmup_days=warmup_days)

    if out_path.exists() and not refresh:
        try:
            cached = json.loads(out_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            cached = None
        if cached and cached.get("fingerprint") == fp:
            if verbose:
                print(f"ledger: cache HIT (fp={fp}) -> {out_path}")
            return cached

    # The execution window carries the warmup prefix; the SCORED
    # window is the corpus proper.
    exec_start = (pd.Timestamp(start, tz="UTC")
                  - pd.Timedelta(days=warmup_days)).strftime("%Y-%m-%d")
    scope = f"{data.stem}__{start.replace('-', '')}_{end.replace('-', '')}"

    legs, meta = run_window(
        data=data, scope=scope,
        exec_start=exec_start, exec_end=end,
        score_start=start, score_end=end,
        engine_overrides=engine_overrides,
    )
    if verbose:
        print(f"  {scope}: legs={meta['legs_kept']} "
              f"(of {meta['legs_total']}) "
              f"zones={meta['n_zones']} "
              f"art={'HIT' if meta['cache_hit'] else 'MISS'} "
              f"eng={meta['engine_secs']:.1f}s", flush=True)

    payload = {
        "fingerprint": fp,
        "built_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "data": str(data),
        "score_start": start,
        "score_end": end,
        "exec_start": exec_start,
        "exec_end": end,
        "warmup_days": warmup_days,
        "local_context_secs": LOCAL_CONTEXT_SECS,
        "engine_overrides": {k: repr(v) for k, v in
                             sorted(engine_overrides.items())},
        "lots": FOREX_LOTS,
        "contract_size": FOREX_CONTRACT_SIZE,
        "exit_reasons": list(EXIT_REASONS),
        "sources": list(SOURCES),
        "fields": list(LEG_FIELDS),
        "n_legs": int(len(legs["gross"])),
        "meta": meta,
        "legs": _encode(legs),
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload), encoding="utf-8")
    tmp.replace(out_path)
    if verbose:
        print(f"ledger: BUILT {payload['n_legs']} legs (fp={fp}) -> {out_path}")
    return payload


def load_ledger(path: Path = LEDGER_PATH,
                *, expect_canonical: bool = False) -> dict[str, Any]:
    """Load the ledger from disk.

    ``expect_canonical=True`` asserts that the ledger on disk was
    built with the CANONICAL recipe (no engine overrides). It exists
    because a ledger file records WHICH recipe produced it, but every
    consumer previously ignored that field -- so a variant ledger
    could be read as the canonical recipe with no complaint.

    The check is on the recorded ``engine_overrides``, not on the
    fingerprint: the fingerprint changes for benign reasons (the data
    file's mtime, a source edit) and would make the check unusable.
    The overrides are the thing that actually changes the STRATEGY.
    """
    if not path.exists():
        raise SystemExit(
            f"no ledger at {path}. Build it first:\n"
            f"    python -m runtime.diagnostics.leg_ledger"
        )
    payload = json.loads(path.read_text(encoding="utf-8"))
    if expect_canonical:
        recorded = payload.get("engine_overrides") or {}
        if not _overrides_are_canonical(recorded):
            raise SystemExit(
                f"the ledger at {path} was built with VARIANT overrides:\n"
                f"  recorded overrides : {recorded!r}\n"
                f"  canonical overrides: {CANONICAL_OVERRIDES!r}\n"
                "\n"
                "This reader expects the CANONICAL recipe. Rebuild it:\n"
                "    python -m runtime.diagnostics.leg_ledger --refresh\n"
                "or point --ledger at the variant file you meant to use."
            )
    return payload


def ledger_arrays(payload: dict[str, Any]) -> dict[str, np.ndarray]:
    """Rehydrate the ledger's columnar payload into numpy arrays."""
    out: dict[str, np.ndarray] = {}
    for k, v in payload["legs"].items():
        if k in _I8_FIELDS:
            out[k] = np.asarray(v, dtype=np.int8)
        elif k in _INT_FIELDS:
            out[k] = np.asarray(v, dtype=np.int64)
        else:
            out[k] = np.asarray(v, dtype=np.float64)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--data", type=Path, default=DEFAULT_DATA)
    ap.add_argument("--start", type=str, default=DEFAULT_START)
    ap.add_argument("--end", type=str, default=DEFAULT_END)
    ap.add_argument("--warmup-days", type=int, default=WARMUP_DAYS)
    ap.add_argument("--refresh", action="store_true",
                    help="Ignore any cached ledger and rebuild.")
    ap.add_argument("--out", type=Path, default=LEDGER_PATH)
    args = ap.parse_args()

    print("== leg ledger ==")
    print(f"data   : {args.data}")
    print(f"window : {args.start} .. {args.end}  "
          f"warmup={args.warmup_days}d (execution starts earlier)")
    print()
    build_ledger(data=args.data, start=args.start, end=args.end,
                 warmup_days=args.warmup_days, out_path=args.out,
                 refresh=args.refresh)


if __name__ == "__main__":
    main()
