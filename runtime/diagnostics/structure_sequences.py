"""Structure-sequence experiment harness (2026-10-23).

Answers two questions that the scalar score could not:

1. **Is the 5-event SEQUENCE predictive, or only the sign-weighted
   mean?** ``bos choch bos choch`` is a mixed history: continuation,
   reversal, continuation, reversal. A scalar score averages that to
   a middling number and loses the shape. This harness enumerates
   the distinct sequences and reports each one's realised P&L.

2. **Is the score ANTI-predictive on longs?** Tested directly by
   inverting the Stage 1 sign test, rather than assuming it.

Every run crosses:

* polarity   : aligned (score agrees) vs opposed (score inverted)
* entry mode : ``dual`` (immediate + sniper) vs ``sniper`` only

Cells are independent -- each rebuilds nothing and reads the artifact
cache, so re-running one cell does not disturb another.

Usage
-----
    python -m runtime.diagnostics.structure_sequences --cells all
    python -m runtime.diagnostics.structure_sequences --cells bull
    python -m runtime.diagnostics.structure_sequences --entry-mode sniper

Results land in ``runtime/diagnostics/_reports/structure_sequences/``
as one JSON per cell, following the AGENTS.md per-element policy.
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
from runtime.sweeps.cached_sweep import ArtifactCache, load_bars  # noqa: E402

DATA = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
OUT_DIR = Path("runtime/diagnostics/_reports/structure_sequences")

# Regimes matter: a directional filter must be judged where the
# market actually moved, not only on flat slices.
SLICES: list[tuple[str, str, str, str]] = [
    ("2026-01 BULL", "2026-01-01", "2026-02-01", "bull"),
    ("2025-09 BULL", "2025-09-01", "2025-10-01", "bull"),
    ("2026-03 BEAR", "2026-03-01", "2026-04-01", "bear"),
    ("2024-11 BEAR", "2024-11-01", "2024-12-01", "bear"),
    ("2025-05 FLAT", "2025-05-01", "2025-06-01", "flat"),
    ("2025-01 UP",   "2025-01-01", "2025-02-01", "bull"),
]


def _artifacts(tag: str, start: str, end: str, bars):
    """Zones / VP / caps / structure / momentum for one slice.

    Cached on disk by ArtifactCache, so repeated cells are cheap.
    """
    cache = ArtifactCache(DATA, f"seq_{tag}", start, end)
    return cache.build_or_load(bars)


def run_cell(
    *, label: str, start: str, end: str, regime: str,
    polarity: str, entry_mode: str, min_abs: float,
) -> dict[str, Any]:
    """One (slice x polarity x entry_mode) cell."""
    bars = load_bars(DATA, start, end)
    art = _artifacts(label.split()[0], start, end, bars)
    spot = float(np.median(bars["close"].to_numpy(dtype=np.float64)))

    # Polarity flips which sign the Stage 1 gate demands.
    # aligned -> score must agree with the trade.
    # opposed -> score must DISAGREE (the anti-predictive hypothesis).
    if polarity == "aligned":
        ek = build_engine_kwargs(
            spot=spot,
            swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            structure_gate=True, structure_score_min_abs=min_abs,
            entry_mode=entry_mode,
        )
    else:
        ek = _inverted_gate_kwargs(
            spot=spot,
            swing_cap=art["swing_cap"], p95_cap=art["p95_cap"],
            structure=art["structure"], vol_state=art["vol_state"],
            min_abs=min_abs, entry_mode=entry_mode,
        )

    t0 = time.perf_counter()
    res, _diag = run_vp_sniper_backtest_forex(
        bars=bars, zones=art["zones"], vp_set=art["vp_set"],
        lots=FOREX_LOTS, contract_size=FOREX_CONTRACT_SIZE, engine_kwargs=ek,
    )
    wall = time.perf_counter() - t0

    trades = list(res.trades)
    mkt_ret = float(
        (bars["close"].iloc[-1] / bars["close"].iloc[0] - 1.0) * 100.0
    )
    out: dict[str, Any] = {
        "ts_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "notebook": "structure_sequences",
        "cell": {
            "slice": label, "start": start, "end": end,
            "regime": regime, "polarity": polarity,
            "entry_mode": entry_mode, "min_abs": min_abs,
        },
        "mkt_ret_pct": mkt_ret,
        "n_legs": len(trades),
        "wall_secs": wall,
    }
    out["metrics"] = _summarise(trades)
    out["sequences"] = _sequence_histogram(bars, art, trades)
    return out


def _inverted_gate_kwargs(*, spot, swing_cap, p95_cap, structure,
                          vol_state, min_abs, entry_mode):
    """Engine kwargs for the ANTI-predictive gate.

    The engine's Stage 1 admits when ``direction * score > 0``. To
    test the hypothesis that this is backwards we flip the score
    before the engine sees it, which is done by wrapping the
    structure object so ``combined_score`` returns ``-score``.

    Inverting via a wrapper (rather than adding a polarity flag to
    the engine) keeps the production code path identical between the
    two arms -- the only difference is the sign the gate is handed.
    """
    class _Inverted:
        """Delegates everything to the real StructureState except
        the event log, which is walked in reverse-sign order."""

        def __init__(self, inner):
            self._inner = inner
            # Mirror the break kinds so the scorer computes -score.
            self.breaks = [_flip_event(b) for b in inner.breaks]

        def __getattr__(self, name):
            return getattr(self._inner, name)

    from bt._engine.market_structure import StructureEvent

    _FLIP = {
        StructureEvent.BOS_BULL: StructureEvent.BOS_BEAR,
        StructureEvent.BOS_BEAR: StructureEvent.BOS_BULL,
        StructureEvent.CHOCH_BULL: StructureEvent.CHOCH_BEAR,
        StructureEvent.CHOCH_BEAR: StructureEvent.CHOCH_BULL,
    }

    from bt._engine.market_structure import BreakEvent

    def _flip_event(b):
        return BreakEvent(
            bar=b.bar, kind=_FLIP.get(b.kind, b.kind),
            broken_swing_bar=b.broken_swing_bar,
            broken_swing_price=b.broken_swing_price,
        )

    return build_engine_kwargs(
        spot=spot,
        swing_cap=swing_cap, p95_cap=p95_cap,
        structure=_Inverted(structure), vol_state=vol_state,
        structure_gate=True, structure_score_min_abs=min_abs,
        entry_mode=entry_mode,
    )


def _summarise(trades: list) -> dict[str, Any]:
    """Per-side and aggregate realised P&L."""
    if not trades:
        return {"legs": 0, "net": 0.0, "gross": 0.0, "long": {}, "short": {}}
    out: dict[str, Any] = {
        "legs": len(trades),
        "gross": float(sum(t.pnl_gross_usd for t in trades)),
        "net": float(sum(t.pnl_net_usd for t in trades)),
    }
    for name, d in (("long", 1), ("short", -1)):
        sel = [t for t in trades if int(t.direction) == d]
        if not sel:
            out[name] = {"n": 0}
            continue
        net = float(sum(t.pnl_net_usd for t in sel))
        out[name] = {
            "n": len(sel),
            "net": net,
            "gross": float(sum(t.pnl_gross_usd for t in sel)),
            "ev": net / len(sel),
            "hit": sum(1 for t in sel if t.pnl_net_usd > 0) / len(sel),
        }
    return out


def _sequence_histogram(bars, art, trades: list) -> dict[str, Any]:
    """P&L by the exact BOS/CHOCH sequence behind each trade.

    This is the whole point: a scalar score collapses
    ``bos choch bos choch`` and ``bos bos bos bos`` onto the same
    number. Grouping by the literal sequence shows whether the SHAPE
    carries information the mean threw away.
    """
    from bt._engine.market_structure import (
        StructureEvent,
        structure_score,
    )

    label = {
        StructureEvent.BOS_BULL: "b",
        StructureEvent.BOS_BEAR: "B",
        StructureEvent.CHOCH_BULL: "c",
        StructureEvent.CHOCH_BEAR: "C",
    }
    if art is None or art.get("structure") is None or not trades:
        return {}

    state = art["structure"]
    bars_arr = np.fromiter(
        (b.bar for b in state.breaks), dtype=np.int64, count=len(state.breaks)
    ) if state.breaks else np.zeros(0, dtype=np.int64)

    # Map each trade's entry bar -> its last-5 signature.
    buckets: dict[str, list] = {}
    for t in trades:
        bar = getattr(t, "entry_bar", None)
        if bar is None or bars_arr.size == 0:
            continue
        upto = int(np.searchsorted(bars_arr, bar, side="right"))
        lo = max(0, upto - 5)
        sig = "".join(label.get(state.breaks[j].kind, "?")
                      for j in range(lo, upto))
        if sig:
            buckets.setdefault(sig, []).append(t)

    out: dict[str, Any] = {}
    for sig, group in sorted(buckets.items(), key=lambda kv: -len(kv[1])):
        if len(group) < 5:
            continue
        net = float(sum(x.pnl_net_usd for x in group))
        out[sig] = {
            "n": len(group),
            "net": net,
            "ev": net / len(group),
            "hit": sum(1 for x in group if x.pnl_net_usd > 0) / len(group),
        }
    return dict(sorted(out.items(), key=lambda kv: kv[1]["ev"], reverse=True))


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--cells", type=str, default="all",
                    help="'all', a regime (bull/bear/flat), or a "
                         "comma-separated list of slice labels.")
    ap.add_argument("--polarity", type=str, default="aligned,opposed")
    ap.add_argument("--entry-mode", type=str, default="dual,sniper")
    ap.add_argument("--min-abs", type=float, default=0.10)
    ap.add_argument("--out", type=Path, default=OUT_DIR)
    args = ap.parse_args()

    sel = SLICES
    if args.cells != "all":
        keys = {k.strip() for k in args.cells.split(",")}
        sel = [s for s in SLICES
               if s[3] in keys or s[0] in keys or any(k in s[0] for k in keys)]

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    print(f"== structure sequence harness ==")
    print(f"slices : {len(sel)}   polarity: {args.polarity}   "
          f"entry_mode: {args.entry_mode}")
    print()

    rows: list[dict[str, Any]] = []
    for label, start, end, regime in sel:
        for polarity in args.polarity.split(","):
            for entry_mode in args.entry_mode.split(","):
                out = run_cell(
                    label=label, start=start, end=end, regime=regime,
                    polarity=polarity.strip(),
                    entry_mode=entry_mode.strip(),
                    min_abs=args.min_abs,
                )
                slug = (f"{label.replace(' ', '_')}__{polarity}"
                        f"__{entry_mode}__m{args.min_abs}.json")
                path = OUT_DIR / slug
                with open(path, "w", encoding="utf-8") as fh:
                    json.dump(out, fh, indent=2, default=str)
                rows.append(out)
                m = out["metrics"]
                print(f"{label:<12} {polarity:<8} {entry_mode:<7} "
                      f"mkt={out['mkt_ret_pct']:+7.2f}%  "
                      f"legs={m['legs']:>5}  net=${m['net']:>9,.0f}  "
                      f"L ${m.get('long',{}).get('net',0):>8,.0f} "
                      f"S ${m.get('short',{}).get('net',0):>8,.0f}",
                      flush=True)
    print()
    print(f"-> {OUT_DIR}  ({len(rows)} cells)")


if __name__ == "__main__":
    main()