---
name: engine-change
description: Use before editing anything under bt/ — engine logic, detectors, fills, or adding a knob.
---

# Engine change

1. Run the baseline first: `python -u -m runtime.sweeps.cached_sweep` (2025-01, ~5 s). Note trades, net, exits.
2. Make the change in `bt/_engine/`. One definition per algorithm; no wrappers, no duplicate copies.
3. New knob, in order: engine parameter → constant in `bt/optimal_config.py` → its builder (`build_engine_kwargs` etc.) → the matching `_*_OVERRIDES_ALLOWED` set. Default the constant to the current behaviour so the canonical result does not move.
4. Check:
   * whole-file detector verdicts are only read as `0 <= verdict_bar <= i`;
   * timestamps stay int64 nanoseconds;
   * nothing runs at import time.
5. Re-run the baseline command. If the change was meant to be behaviour-neutral, trades and net must match exactly. If not, report the before/after numbers.
6. Then sweep the new knob with `/sweep`.
