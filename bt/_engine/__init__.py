"""bt._engine — THE canonical engine source (standalone, self-contained).

**This directory IS the engine.** Every file here is the single
definition of that algorithm. There is no upstream repo, no symlink,
no ``sys.path`` shim, and no second copy anywhere in this repository.

The public surface is ``bt._engine.X`` directly. There are no
re-export wrapper modules under ``bt/`` — those were deleted on
2026-11-03 because they had zero production callers and gave a
reader a second name for the same object.

Contents:

* ``vp_multi_target_engine.py`` — the engine (NB74/NB75/NB83 multi-target)
* ``vp_multi_target_config.py`` — mode-name vocabulary (no knob VALUES)
* ``ict_signals.py`` — FVG / iFVG detectors
* ``ict_strategy.py`` — ``PendingSignal``
* ``market_structure.py`` — BoS / CHoCH / order blocks
* ``volume_profile.py`` — daily VP rolling aggregator (NB80)
* ``vol_state.py`` — vol-expansion gate (NB85)
* ``swing_cap.py`` — daily-swing TP cap (NB72)
* ``rolling_p95.py`` — rolling p95 TP cap (NB91)
* ``run_report.py`` — run-log writer
* ``timebase.py`` — nanosecond timebase (the single source)

For any new feature or bug-fix, the change lands HERE, in the file
that owns the algorithm. This directory is the SINGLE source of truth
for the engine surface; the rest of ``bt/`` contains only config and
sweep plumbing.
"""
