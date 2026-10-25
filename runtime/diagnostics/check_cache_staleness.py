"""Check whether the `seq_*` artifact caches are STALE relative to the
current detector code.

The zone-lifecycle detector fix landed in ``bt/_engine/ict_signals.py``
on 2026-10-24 02:16. The artifact fingerprint covers the DATA file
(size/mtime) and the resolved detector/VP/swing/p95/structure KWARGS
-- but NOT the detector SOURCE CODE. So a cache built before the fix
is still considered a HIT.

This script loads two artifact bundles for the same slice and compares
the zone lifecycle counts:

  * ``seq_<slice>``  -- built 2026-10-23, BEFORE the fix
  * ``c1m_<slice>``  -- built 2026-10-24, AFTER the fix

If the inverted counts differ materially, every diagnostic that used
the ``seq_`` scope measured the pre-fix zone set.
"""
from __future__ import annotations

import pickle
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

CACHE = Path("notebooks/.cache")


def newest_pkl(scope: str) -> Path | None:
    d = CACHE / scope
    if not d.is_dir():
        return None
    pkls = sorted(d.glob("artifacts_*.pkl"), key=lambda p: p.stat().st_mtime)
    return pkls[-1] if pkls else None


def summarise(path: Path) -> dict:
    with open(path, "rb") as fh:
        art = pickle.load(fh)
    zones = art.get("zones") or []
    n = len(zones)
    inverted = sum(1 for z in zones if int(getattr(z, "inverted_bar", -1)) >= 0)
    superseded = sum(1 for z in zones if int(getattr(z, "superseded_bar", -1)) >= 0)
    return {
        "path": str(path),
        "mtime": path.stat().st_mtime,
        "zones": n,
        "inverted": inverted,
        "superseded": superseded,
    }


def main() -> None:
    slices = ["2026-01", "2026-03", "2025-05", "2025-01", "2024-11", "2025-09"]
    print(f"{'scope':16s} {'zones':>7s} {'inverted':>9s} {'superseded':>11s}  built")
    print("-" * 70)
    for sl in slices:
        for scope in (f"seq_{sl}", f"seq1m_{sl}", f"c1m_{sl}"):
            p = newest_pkl(scope)
            if p is None:
                continue
            import datetime as _dt
            s = summarise(p)
            ts = _dt.datetime.fromtimestamp(s["mtime"]).strftime("%Y-%m-%d %H:%M")
            print(f"{scope:16s} {s['zones']:>7d} {s['inverted']:>9d} "
                  f"{s['superseded']:>11d}  {ts}")


if __name__ == "__main__":
    sys.exit(main())
