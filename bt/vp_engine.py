"""Multi-target VP engine wrapper.

The multi-target engine source is duplicated into
``bt/_engine/vp_multi_target_engine.py`` (verbatim copy of
``ict_crypto_research/src/strategies/vp_multi_target/engine.py``
with imports rewritten). This module is the sweep-friendly
wrapper: it takes the canonical XAUUSD config and exposes
``run_vp_sniper_backtest_forex`` for the sweep driver.

This is the **multi-target** path: NB74/NB75 promote
``tp_mode='multi_target'``, with TP1 = nth HVN shelf closing
``partial_close_pct``, rolling SL to breakeven, TP2 = zone
multiplier or wide target.

WHERE THE CONFIG LIVES
======================

``bt/optimal_config.py`` is the SINGLE source of truth for every
canonical value. This module does NOT re-import any of them — it
only:

* applies the forex sizing convention (``qty_btc = lots x
  contract_size``), and
* strips the forex-only fee knobs before the engine call.

The engine kwargs themselves come from
:func:`bt.optimal_config.build_engine_kwargs`.
"""
from __future__ import annotations

# Import the duplicated engine source — no shadow package
# workaround needed because everything lives in this repo.
from bt._engine.vp_multi_target_engine import (  # type: ignore  # noqa: E402
    run_vp_sniper_backtest,
    BacktestResult,
    ClosedTrade,
    OpenTrade,
)

# Engine kwargs that are FOREX-OVERLAY only (used to derive
# taker_fee_bps) and should NOT be forwarded to the engine.
_FOREX_ONLY_KWARGS = frozenset({
    'commission_per_lot_usd',
    'spread_per_oz_usd',
})


def run_vp_sniper_backtest_forex(
    bars,
    zones,
    vp_set,
    lots: float,
    contract_size: float,
    *,
    engine_kwargs: dict,
    **kwargs,
):
    """Run the multi-target VP backtest on XAUUSD (MT4 MT4 convention).

    Wraps the crypto ``run_vp_sniper_backtest`` with the
    forex sizing convention: ``qty_btc = lots x contract_size``
    (= 1.0 at canonical 0.01 lots x 100 contract_size).

    Parameters
    ----------
    bars, zones, vp_set : passed through to ``run_vp_sniper_backtest``.
    lots, contract_size : float
        MT4 sizing. ``qty_btc = lots x contract_size`` is the
        notional unit (1.0 = 1 oz for XAUUSD).
    engine_kwargs : dict
        Built via :func:`bt.optimal_config.build_engine_kwargs`.
        The fee knobs (``commission_per_lot_usd``,
        ``spread_per_oz_usd``) are the LIVE MT4 quote (40c/oz
        spread, $7/lot commission); they're used to derive
        ``taker_fee_bps`` and are NOT forwarded to the engine
        (the engine only reads ``taker_fee_bps``).
    **kwargs
        Forwarded to ``run_vp_sniper_backtest`` (vol_state_*,
        conflict_policy, max_same_dir_open, on_bar_callback,
        etc.).

    Returns
    -------
    tuple[BacktestResult, dict]
        Same as ``run_vp_sniper_backtest``.
    """
    qty_btc = float(lots) * float(contract_size)
    # Drop the FOREX fee knobs (the engine reads taker_fee_bps)
    fwd_kwargs = {
        k: v for k, v in engine_kwargs.items()
        if k not in _FOREX_ONLY_KWARGS
    }
    return run_vp_sniper_backtest(
        bars=bars,
        zones=zones,
        vp_set=vp_set,
        qty_btc=qty_btc,
        # Engine kwargs spread into the call
        **fwd_kwargs,
        # Caller's additional kwargs (vol_state, conflict_policy, ...)
        **kwargs,
    )


__all__ = [
    'run_vp_sniper_backtest',
    'run_vp_sniper_backtest_forex',
    'BacktestResult',
    'ClosedTrade',
    'OpenTrade',
]
