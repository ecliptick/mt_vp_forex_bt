"""Corpus-level A/B for ``vol_state_min_expand_prev_day``.

ONE continuous window over the full corpus (14-day warmup prefix,
executed and discarded), canonical recipe otherwise. Writes ONE variant
ledger per threshold to its OWN file — never the canonical path.

This is the verdict measurement the one-month sweep cannot give.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from runtime.diagnostics.leg_ledger import build_ledger  # noqa: E402
from runtime.results import registry  # noqa: E402

OUT_DIR = Path("runtime/diagnostics/_reports")

THRESHOLDS = [0.0, 0.7, 0.85, 1.0, 1.1, 1.2, 1.3]

# The corpus window the A/B is measured over.
CORPUS_START = "2024-01-01"
CORPUS_END = "2026-07-16"

def summarise(payload: dict) -> dict:
    legs = payload["legs"]
    st = np.array(legs["signal_time_ns"], dtype=np.int64)
    gross = np.array(legs["gross"], dtype=np.float64)
    fee = np.array(legs["fee"], dtype=np.float64)
    n = len(st)
    days = (pd.Timestamp(payload["score_end"])
            - pd.Timestamp(payload["score_start"])).days
    day_of = pd.to_datetime(st, unit="ns", utc=True).normalize()
    counts = pd.Series(day_of).value_counts()
    return {
        "legs": n,
        "calendar_days": days,
        "legs_per_calendar_day": n / days if days else 0.0,
        "legs_per_signal_day": float(counts.mean()) if len(counts) else 0.0,
        "pct_days_below_30": float(100.0 * (counts < 30).mean()) if len(counts) else 0.0,
        "gross": float(gross.sum()),
        "fees": float(fee.sum()),
        "net": float(gross.sum() - fee.sum()),
        "gross_per_leg": float(gross.mean()) if n else 0.0,
        "fee_per_leg": float(fee.mean()) if n else 0.0,
        "hit_rate": float((gross > 0).mean()) if n else 0.0,
    }


def main() -> None:
    rows = []
    for thr in THRESHOLDS:
        # ⚠️ The baseline must EXPLICITLY pass 0.0, not omit the key.
        # An empty override dict means "run the CANONICAL recipe" --
        # and the canonical recipe now IS ``vol_state_min_expand_prev_day
        # = 1.0`` (promoted 2026-11-04). Omitting the key would make the
        # "gate-off" baseline silently re-measure the promoted gate.
        ov = {"vol_state_min_expand_prev_day": thr}
        out = OUT_DIR / f"leg_ledger_expand_{thr:g}.json"
        print(f"=== expand_prev_day = {thr} -> {out.name} ===")
        # ``build_ledger`` is fingerprint-keyed: an existing ledger whose
        # fingerprint matches the CURRENT code is reused as-is (cache
        # HIT), so a re-run never re-measures a threshold that is
        # already on disk at this source hash.
        payload = build_ledger(engine_overrides=ov, out_path=out)
        s = summarise(payload)
        s["threshold"] = thr
        rows.append(s)
        print(json.dumps(s, indent=1))

    print()
    print("| thr | legs | legs/day | %days<30 | gross | fees | net | gross/leg | fee/leg | hit |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        print(f"| {r['threshold']} | {r['legs']} | {r['legs_per_calendar_day']:.2f} | "
              f"{r['pct_days_below_30']:.1f}% | ${r['gross']:.2f} | ${r['fees']:.2f} | "
              f"${r['net']:.2f} | {r['gross_per_leg']:.4f} | {r['fee_per_leg']:.4f} | "
              f"{r['hit_rate']:.3f} |")

    # ── DURABLE RECORD ───────────────────────────────────────────────
    # § 2.3: every diagnostic that produces a verdict must record it.
    # The PER-THRESHOLD ledgers are already recorded by ``build_ledger``;
    # this records the A/B CURVE itself -- the thing that actually
    # decided the knob (is the EV peak at 1.0, or on a rising slope?).
    # Without this, the seven individual runs are in the registry but
    # the conclusion drawn FROM them is not.
    by_thr = {r["threshold"]: r for r in rows}
    peak_thr = max(by_thr, key=lambda t: by_thr[t]["gross_per_leg"])
    peak = by_thr[peak_thr]
    registry.record_run(
        kind="expand_prev_day_ab",
        params={
            "knob": "vol_state_min_expand_prev_day",
            "thresholds": list(THRESHOLDS),
            "start": CORPUS_START,
            "end": CORPUS_END,
            "criterion": "gross_per_leg",
        },
        summary={
            "peak_threshold": peak_thr,
            "peak_gross_per_leg": round(peak["gross_per_leg"], 6),
            "peak_legs": peak["legs"],
            "peak_net": round(peak["net"], 2),
            "curve": {
                str(r["threshold"]): round(r["gross_per_leg"], 6)
                for r in rows
            },
        },
        payload={"rows": rows},
        src_fp=registry.source_fingerprint([
            "bt/_engine/vol_state.py",
            "bt/_engine/vp_multi_target_engine.py",
            "bt/optimal_config.py",
        ]),
    )
    print()
    print(f"recorded: EV peak at {peak_thr} "
          f"(gross/leg={peak['gross_per_leg']:.4f}, legs={peak['legs']})")


if __name__ == "__main__":
    main()
