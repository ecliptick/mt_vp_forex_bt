"""Sweep drivers for the XAUUSD SNIPER sweep.

``cached_sweep`` is the ONLY sweep driver in this repo. It replaces
the old ``single_sweep`` (which reported empty metrics — it read
``result.calmar`` / ``result.n_trades``, neither of which exists on
``BacktestResult``) and ``run_dual_sweep`` (which hardcoded
detector knobs that disagreed with the canonical recipe).

Usage::

    python -m runtime.sweeps.cached_sweep --knob _canonical
    python -m runtime.sweeps.cached_sweep \\
        --knob sl_zone_mult --grid 10,20,40,80 \\
        --data C:/coding/ict_tier_v2/data/XAUUSD_S1_1y.parquet \\
        --start 2025-01-01 --end 2025-02-01
"""
