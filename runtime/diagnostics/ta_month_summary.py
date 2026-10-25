"""Cross-month summary of the signal-to-signal EPISODE scoreboard.

Reads every monthly episode report plus the 6-month continuous one
and prints, per cell label:

* how many months the cell shows SIGN skill
* how many months it shows TIMING skill
* the fixed-clock (causal) verdict in the 6-month continuous run

The point: a cell that "works" in one month and not in the next is
noise. Only a cell that clears the bar in MOST months is a candidate.

Usage:
    python -m runtime.diagnostics.ta_month_summary
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

_REPORTS = _REPO_ROOT / "runtime" / "diagnostics" / "_reports"

MONTHLY = {
    "2025-01": "ta_1m_signal_sweep.json",
    "2025-02": "ta_signal_2025-02.json",
    "2025-03": "ta_signal_2025-03.json",
    "2025-04": "ta_signal_2025-04.json",
    "2025-05": "ta_signal_2025-05.json",
    "2025-06": "ta_signal_2025-06.json",
}
CONTINUOUS = "ta_signal_6mo.json"


def _load(name: str) -> dict | None:
    p = _REPORTS / name
    if not p.exists():
        return None
    raw = p.read_text(encoding="utf-8", errors="replace")
    i = raw.find("{")
    if i < 0:
        return None
    try:
        return json.loads(raw[i:])
    except json.JSONDecodeError:
        return None


def main() -> None:
    months: dict[str, dict] = {}
    for m, f in MONTHLY.items():
        d = _load(f)
        if d is None:
            print(f"  [skip] {m}: {f} missing/unreadable")
            continue
        months[m] = {c["label"]: c for c in d.get("cells", [])}

    cont = _load(CONTINUOUS)
    cont_cells = {}
    if cont:
        cont_cells = {c["label"]: c for c in cont.get("cells", [])}

    labels: list[str] = []
    for m in sorted(months):
        for lab in months[m]:
            if lab not in labels:
                labels.append(lab)

    print("=" * 112)
    print("SIGNAL-TO-SIGNAL EPISODE SCOREBOARD — CROSS-MONTH")
    print("=" * 112)
    print(f"months loaded : {', '.join(sorted(months))}")
    print(f"distinct cells: {len(labels)}")
    if cont:
        print(f"continuous    : {CONTINUOUS} "
              f"({cont['window']['start']}..{cont['window']['end']}, "
              f"{len(cont_cells)} cells)")
    print()

    hdr = f"{'cell':<34} {'sign/mo':>8} {'tim/mo':>8}  {'6mo sign':>9} {'6mo tim':>8}"
    print(hdr)
    print("-" * len(hdr))

    rows = []
    for lab in labels:
        sign_n = tim_n = n = 0
        for m in sorted(months):
            c = months[m].get(lab)
            if c is None:
                continue
            n += 1
            sign_n += bool(c.get("sign_skill"))
            tim_n += bool(c.get("timing_skill"))
        cc = cont_cells.get(lab)
        cs = ("yes" if cc.get("sign_skill") else "no") if cc else "-"
        ct = ("yes" if cc.get("timing_skill") else "no") if cc else "-"
        rows.append((lab, sign_n, tim_n, n, cs, ct))

    rows.sort(key=lambda r: (-r[1], -r[2], r[0]))
    for lab, sn, tn, n, cs, ct in rows:
        print(f"{lab:<34} {sn:>4}/{n:<3} {tn:>4}/{n:<3}  {cs:>9} {ct:>8}")

    # ── consistency summary ────────────────────────────────────────
    print()
    print("=" * 112)
    print("CONSISTENCY")
    print("=" * 112)
    n_mo = len(months)
    consistent_sign = [r for r in rows if r[1] >= max(2, n_mo - 1)]
    any_sign = [r for r in rows if r[1] > 0]
    consistent_tim = [r for r in rows if r[2] >= max(2, n_mo - 1)]
    print(f"cells with sign skill in >= 1 month      : {len(any_sign)} / {len(rows)}")
    print(f"cells with sign skill in >= {max(2, n_mo-1)} of {n_mo} months : "
          f"{len(consistent_sign)}")
    print(f"cells with timing skill in >= {max(2, n_mo-1)} of {n_mo} months: "
          f"{len(consistent_tim)}")
    for r in consistent_sign[:20]:
        print(f"   SIGN-consistent: {r[0]}  ({r[1]}/{r[3]} months)")
    for r in consistent_tim[:20]:
        print(f"   TIMING-consistent: {r[0]}  ({r[2]}/{r[3]} months)")

    # ── the 6-month continuous run is the decider ──────────────────
    if cont_cells:
        print()
        print("=" * 112)
        print("6-MONTH CONTINUOUS RUN — the cells with skill")
        print("=" * 112)
        scored = []
        for lab, c in cont_cells.items():
            fc = c.get("fixed_clock") or {}
            scored.append((
                bool(c.get("sign_skill")),
                bool(c.get("timing_skill")),
                bool(fc.get("sign_skill")),
                bool(fc.get("timing_skill")),
                lab, c, fc,
            ))
        for s, t, fs, ft, lab, c, fc in sorted(scored, reverse=True):
            if not (s or t or fs or ft):
                continue
            print(f"  {lab:<34} sign={s} tim={t} | fixed_clock sign={fs} "
                  f"tim={ft}")
            if fc:
                print(f"      {fc.get('verdict','')}")
        print()


if __name__ == "__main__":
    sys.exit(main())
