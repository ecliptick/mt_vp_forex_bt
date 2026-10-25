"""bt._engine — duplicated engine source code (standalone copy).

The files in this directory are verbatim copies of source
files from ``ict_forex_research/src/`` and
``ict_crypto_research/src/``. The cross-file ``from src.X``
references have been rewritten to ``from bt._engine.X`` so
that this repo is fully standalone — no symlinks, no
sys.path shims, no upstream dependencies.

Files copied from ``ict_forex_research/src/``:

* ``ict_signals.py`` (FVG / iFVG detectors)
* ``ict_strategy.py`` (``PendingSignal``)
* ``market_structure.py`` (BoS / CHoCH / order blocks)
* ``run_report.py`` (run-log writer)

Files copied from ``ict_crypto_research/src/``:

* ``vp_multi_target_engine.py`` (NB74/NB75/NB83 multi-target)
* ``vp_multi_target_config.py`` (TP mode constants)
* ``volume_profile.py`` (daily VP rolling aggregator)
* ``vol_state.py`` (NB85 vol-expansion gate)
* ``swing_cap.py`` (NB72 daily-swing TP cap)

Plus the shared plumbing this repo owns:

* ``rolling_p95.py`` (NB91 rolling p95 TP cap)
* ``timebase.py`` (nanosecond timebase — the single source)
REMOVED (2026-10-24 — config consolidation)
==================================================

The following upstream files were DELETED. Each was either dead or a
second source of truth for a knob ``bt/optimal_config.py`` now owns:

* ``ict_backtest.py`` (``run_ict_backtest``) — the bar engine. It had
  **zero production callers**: the only engine used in production and
  research is the VP multi-target engine
  (``run_vp_sniper_backtest``), which never read the recipe object.
* ``precomputed.py`` — the bar engine's precompute cache, likewise
  with zero callers. The VP engine has no precompute cache (the
  sweep driver owns its own artifact cache).
* ``optimal_config.py`` — the ``TrendStrategyParams`` recipe with TWO
  recipes (``SNIPER`` + ``HF_TIGHT``) and an ``optimal_params()``
  override factory. ``HF_TIGHT`` had zero callers anywhere.
  ``TrendStrategyParams`` itself was removed from
  ``ict_strategy.py`` in the same pass; only ``PendingSignal``
  survives there.

For any new feature / bug-fix in either upstream repo, the
corresponding file here is updated by re-copying and
re-rewriting imports. This module is the SINGLE source of
truth for the engine surface; ``bt/`` itself only contains
sweep plumbing.
"""
