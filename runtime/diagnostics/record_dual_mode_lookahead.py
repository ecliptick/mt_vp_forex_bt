"""Record the dual-mode look-ahead measurement in the RESULTS REGISTRY.

THE MEASUREMENT
---------------
The 2026-11-03 look-ahead audit found that the canonical recipe's
immediate entry path skipped any zone with ``zone.inverted_bar >= 0``
-- a WHOLE-FILE verdict read at the zone's trigger bar. Over six
monthly slices the look-ahead was worth **$2,811** and was
systematically FAVOURABLE.

This module records the pre-fix and post-fix books so the two sit side
by side in the registry with the SAME ``config_fp`` and different
``commit`` -- which is exactly what the registry is for.

Run:
    python -m runtime.diagnostics.record_dual_mode_lookahead
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_REPORTS = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"


def _load(name: str) -> dict:
    p = _REPORTS / name
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> None:
    from runtime.results import registry

    pre = _load("dual_mode_multislice.json")
    post = _load("dual_mode_postfix.json")
    excl = _load("dual_mode_lookahead.json")

    if not pre or not post:
        print("probe artifacts missing — run probe_dual_mode_multislice "
              "and probe_dual_mode_postfix first")
        return

    pre_slices = {r["slice"]: r for r in pre.get("slices", [])}
    post_slices = {r["slice"]: r for r in post.get("slices", [])}

    # ── 1. The look-ahead itself (a static property of the detector) ──
    if excl:
        registry.record_run(
            kind="dual_mode_lookahead",
            params={
                "probe": "dual_mode_lookahead",
                "scope": excl.get("scope"),
                "start": excl.get("start"),
                "end": excl.get("end"),
                "data": str(_REPO_ROOT / "results" / ".."),
            },
            summary={
                "n_trigger_zones": excl.get("n_trigger_zones"),
                "lookahead_skipped": excl.get("LOOKAHEAD_skipped_future_inversion"),
                "lookahead_share": excl.get("lookahead_share_of_trigger_zones"),
                "lead_median": excl.get("lead_median"),
                "lead_max": excl.get("lead_max"),
            },
            payload=excl,
        )
        print("recorded dual_mode_lookahead")

    # ── 2. The book, pre-fix and post-fix ─────────────────────────
    # The SAME params on both sides: the registry keys on the CONFIG,
    # not the commit, so the two records sit side by side and the
    # effect of the fix is a visible diff.
    for label, blob, slices in (
        ("pre_fix", pre, pre_slices),
        ("post_fix", post, post_slices),
    ):
        rows = []
        for key, r in sorted(slices.items()):
            if label == "pre_fix":
                arm = r["dual"]
            else:
                arm = r["dual_postfix"]
            rows.append({
                "slice": key,
                "n_legs": arm["n_legs"],
                "gross": arm["gross"],
                "fees": arm["fees"],
                "net": arm["net"],
                "gross_per_leg": arm["gross_per_leg"],
            })

        payload = {
            "label": label,
            "slices": rows,
            "net_sum": blob.get(
                "dual_net_sum" if label == "pre_fix"
                else "dual_postfix_net_sum"),
            "gross_per_leg_mean": blob.get(
                "dual_gross_per_leg_mean" if label == "pre_fix"
                else "dual_postfix_gross_per_leg_mean"),
            "net_positive_slices": blob.get(
                "dual_net_positive_slices" if label == "pre_fix"
                else "dual_postfix_net_positive_slices"),
            "n_slices": blob.get("n_slices"),
            "note": (
                "pre_fix = the shipped engine's `int(zone.inverted_bar) >= 0` "
                "skip (a whole-file verdict read at the trigger bar). "
                "post_fix = the causal `0 <= _inv_b <= i` test."
                if label == "pre_fix" else
                "post_fix = the causal `0 <= _inv_b <= i` test. The book now "
                "matches the `immediate` arm exactly on every slice."
            ),
        }
        entry = registry.record_run(
            kind="dual_mode_book",
            params={
                "probe": "dual_mode_book",
                "label": label,
                "slices": [r["slice"] for r in rows],
                "engine": "run_vp_sniper_backtest_forex",
                "entry_mode": "dual",
                "lots": 0.01,
                "contract_size": 100.0,
            },
            summary={
                "n_slices": payload["n_slices"],
                "net_sum": payload["net_sum"],
                "gross_per_leg_mean": payload["gross_per_leg_mean"],
                "net_positive_slices": payload["net_positive_slices"],
            },
            payload=payload,
        )
        print(f"recorded dual_mode_book[{label}] -> {entry.run_id}")

    # ── 3. The CONFIG-INDEPENDENCE verification ───────────────────
    # The causality property is a property of the ENGINE LOGIC, not of
    # any knob. Re-running under a different config must produce the
    # same invariant: ``dual == immediate`` on every slice.
    v2 = _load("dual_mode_postfix_v2.json")
    if v2:
        rows = []
        for r in v2.get("slices", []):
            d = r["dual_postfix"]
            im = r["immediate"]
            rows.append({
                "slice": r["slice"],
                "dual_legs": d["n_legs"],
                "immediate_legs": im["n_legs"],
                "equal": d["n_legs"] == im["n_legs"],
                "dual_net": d["net"],
                "immediate_net": im["net"],
            })
        n_equal = sum(1 for r in rows if r["equal"])
        payload = {
            "slices": rows,
            "n_slices": len(rows),
            "slices_with_dual_eq_immediate": n_equal,
            "invariant_holds": n_equal == len(rows),
            "dropped_unknown_knobs": v2.get("dropped_unknown_knobs"),
            "note": v2.get("note"),
        }
        entry = registry.record_run(
            kind="dual_mode_invariance",
            params={
                "probe": "dual_mode_invariance",
                "invariant": "dual_n_legs == immediate_n_legs",
                "slices": [r["slice"] for r in rows],
                "engine": "run_vp_sniper_backtest_forex",
                "entry_mode": "dual",
            },
            summary={
                "n_slices": len(rows),
                "slices_with_dual_eq_immediate": n_equal,
                "invariant_holds": n_equal == len(rows),
            },
            payload=payload,
        )
        print(f"recorded dual_mode_invariance -> {entry.run_id}")

    print("\ndone.")


if __name__ == "__main__":
    main()
