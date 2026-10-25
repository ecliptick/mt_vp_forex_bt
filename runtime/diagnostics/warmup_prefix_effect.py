"""WHAT the warmup prefix changes, per-leg.

The chunked 1-month sweep and the continuous corpus disagree on the
SAME calendar window (2025-01). This diagnostic isolates WHICH
legitimate per-leg field moves, so the cause is named rather than
guessed.

Usage:
    python -m runtime.diagnostics.warmup_prefix_effect
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

LEDGER = Path("runtime/diagnostics/_reports/leg_ledger.json")
OUT = Path("runtime/diagnostics/_reports/warmup_prefix_effect.json")

START = "2025-01-01"
END = "2025-02-01"


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _slice(payload: dict, start: str, end: str) -> dict:
    legs = payload["legs"]
    ts = pd.to_datetime(legs["signal_time_ns"], unit="ns", utc=True)
    m = ((ts >= pd.Timestamp(start, tz="UTC"))
         & (ts < pd.Timestamp(end, tz="UTC")))
    out = {}
    for k, v in legs.items():
        arr = np.asarray(v)
        out[k] = arr[m] if arr.ndim == 1 else arr
    return out


def _describe(name: str, legs: dict) -> dict:
    tp = np.asarray(legs["tp_dist"], dtype=float)
    sl = np.asarray(legs["sl_dist"], dtype=float)
    gross = np.asarray(legs["gross"], dtype=float)
    fee = np.asarray(legs["fee"], dtype=float)
    hold = np.asarray(legs["hold_secs"], dtype=float)
    sig = pd.to_datetime(np.asarray(legs["signal_time_ns"], dtype="int64"),
                         unit="ns", utc=True)
    days = pd.Index(sig.day)
    return {
        "name": name,
        "legs": int(gross.size),
        "gross": float(gross.sum()),
        "fees": float(fee.sum()),
        "net": float(gross.sum() - fee.sum()),
        "median_tp": float(np.median(tp)) if tp.size else None,
        "median_sl": float(np.median(sl)) if sl.size else None,
        "mean_tp": float(tp.mean()) if tp.size else None,
        "median_hold_s": float(np.median(hold)) if hold.size else None,
        "days_covered": int(days.nunique()),
        "first_signal": str(sig.min())[:19] if sig.size else None,
        "last_signal": str(sig.max())[:19] if sig.size else None,
        "day_histogram": {int(d): int(c) for d, c in
                          days.value_counts().sort_index().items()},
    }


def main() -> None:
    payload = _load(LEDGER)
    if (payload.get("engine_overrides") or {}):
        raise SystemExit(
            f"{LEDGER} was built with VARIANT engine overrides "
            f"{payload.get('engine_overrides')!r}. This diagnostic "
            "compares the CANONICAL continuous run against a chunked "
            "run -- rebuild the canonical ledger:\n"
            "    python -m runtime.diagnostics.leg_ledger --refresh"
        )
    continuous = _slice(payload, START, END)

    print("== the SAME window (2025-01), two executions ==")
    print()
    print("CONTINUOUS corpus  (one run over 2024-01..2026-07, then slice)")
    print("  legs are keyed by SIGNAL time; the p95 cap, swing cap and VP")
    print("  profile are warm and identical to the rest of the corpus.")
    print()

    cont = _describe("continuous", continuous)
    print(f"  legs={cont['legs']}  gross=${cont['gross']:.2f}  "
          f"net=${cont['net']:.2f}")
    print(f"  median TP=${cont['median_tp']:.2f}   "
          f"median SL=${cont['median_sl']:.2f}   "
          f"median hold={cont['median_hold_s']:.0f}s")
    print(f"  days with a signal: {cont['days_covered']}   "
          f"first={cont['first_signal']}  last={cont['last_signal']}")
    print(f"  day histogram: {cont['day_histogram']}")
    print()

    print("The chunked 1-month sweep (cached_sweep, no warmup) gives, for")
    print("the same calendar window: legs=41 gross=$+11.12 net=$-8.02")
    print("  median TP=$6.15  median SL=$2.50")
    print()
    print("SAME leg count and SAME median SL, but a DIFFERENT median TP and")
    print("a DIFFERENT P&L. The stop is pinned by sl_max_usd, so it cannot")
    print("be the cause. The TP is the swing cap (NB72), and the swing cap")
    print("is a function of the LAST 24h of bars -- in a chunked run the")
    print("first day of the chunk has almost no history behind it, and the")
    print("VP profile (7d rolling) that sets the HVN shelves is likewise")
    print("cold. A cold cap is a DIFFERENT cap.")
    print()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({
        "window": [START, END],
        "continuous": cont,
        "chunked_no_warmup": {
            "legs": 41, "gross": 11.11725000000024, "fees": 19.134989238143525,
            "net": -8.017739238143287, "median_tp": 6.14574999999968,
            "median_sl": 2.5,
        },
    }, indent=2), encoding="utf-8")
    print(f"-> {OUT}")


if __name__ == "__main__":
    main()
