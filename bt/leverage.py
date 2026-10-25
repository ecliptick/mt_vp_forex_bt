"""Leverage math + exposure accounting (100× canonical).

The user's constraint:

* Leverage: 100×.
* Lots base: 0.01.
* Notional: 1 oz at canonical sizing.

A trade at 1 oz notional costs ``$30`` margin at 100×
leverage on a $3000/oz spot. A daily swing of $20 against
the position is a 67% drawdown on that single position's
margin — which is why the daily-swing TP cap (NB72) caps
the TP distance at 70% of the daily range, and why the
SL/TP trade is zone-anchored (NB82/NB83), not ATR-anchored.

This module is a thin wrapper around ``bt.risk`` for the
exposure math. It adds:

* :func:`account_exposure` — the open-book exposure in USD.
* :func:`max_lots_for_exposure` — the lot cap that keeps the
  open book within ``target_exposure_usd``.

It does NOT re-implement the per-trade math — that's in
``bt.risk.margin_and_cost``.

CANONICAL LEVERAGE LIVES IN ``bt/optimal_config.py``
====================================================

The single source of truth for the 100x leverage factor is
:data:`bt.optimal_config.FOREX_LEVERAGE`. This file used to define
its own ``DEFAULT_LEVERAGE = 100.0`` — a second copy. It is deleted.

``DEFAULT_MAX_BOOK_EXPOSURE_PCT`` (0.50) is a property of THIS
module's exposure cap, not of the trading recipe, so it stays — but
it is the only constant here.
"""
from __future__ import annotations

from dataclasses import dataclass

# CANONICAL LEVERAGE LIVES IN ``bt/optimal_config.py``.
#
# This file used to define its own ``DEFAULT_LEVERAGE = 100.0`` — a
# second copy of the canonical ``FOREX_LEVERAGE``. It is deleted and
# the canonical constant is used as the default instead.
from bt.optimal_config import FOREX_LEVERAGE  # noqa: E402

# Maximum book exposure as fraction of equity (canonical 50%
# at 100x leverage).
DEFAULT_MAX_BOOK_EXPOSURE_PCT = 0.50


@dataclass
class ExposureSnapshot:
    """Snapshot of the open-book exposure.

    Fields
    ------
    open_lots_total : float
        Total open lots across all open trades.
    open_oz_total : float
        Total open oz notional (``open_lots_total × contract_size``).
    open_notional_usd : float
        Open notional at the current spot (USD).
    open_margin_usd : float
        Open margin at ``leverage`` (USD).
    margin_pct_of_equity : float
        Open margin as a fraction of equity (USD/equity).
    over_cap : bool
        True if ``margin_pct_of_equity > max_book_exposure_pct``.
    """

    open_lots_total: float
    open_oz_total: float
    open_notional_usd: float
    open_margin_usd: float
    margin_pct_of_equity: float
    over_cap: bool


def account_exposure(
    *,
    open_lots_total: float,
    contract_size: float,
    spot: float,
    leverage: float,
    account_equity_usd: float,
    max_book_exposure_pct: float = DEFAULT_MAX_BOOK_EXPOSURE_PCT,
) -> ExposureSnapshot:
    """Compute the open-book exposure.

    All fields are required (NO defaults) except ``leverage`` and
    ``max_book_exposure_pct``, which default to the canonical
    constants.

    Parameters
    ----------
    open_lots_total : float
        Sum of open lots across the book.
    contract_size : float
        Contract size (canonical ``100.0``).
    spot : float
        Current USD/oz spot.
    leverage : float
        Account leverage (canonical ``100.0``).
    account_equity_usd : float
        Account equity in USD.
    max_book_exposure_pct : float
        Cap on margin-as-fraction-of-equity (canonical ``0.5``).

    Returns
    -------
    ExposureSnapshot
    """
    oz = open_lots_total * contract_size
    notional = oz * spot
    margin = notional / leverage
    pct = margin / account_equity_usd if account_equity_usd > 0 else 0.0
    return ExposureSnapshot(
        open_lots_total=open_lots_total,
        open_oz_total=oz,
        open_notional_usd=notional,
        open_margin_usd=margin,
        margin_pct_of_equity=pct,
        over_cap=pct > max_book_exposure_pct,
    )


def max_lots_for_exposure(
    *,
    contract_size: float,
    spot: float,
    leverage: float = FOREX_LEVERAGE,
    account_equity_usd: float,
    max_book_exposure_pct: float = DEFAULT_MAX_BOOK_EXPOSURE_PCT,
    base_lots: float = 0.01,
) -> float:
    """Compute the max lots one trade can take without breaching the cap.

    Parameters
    ----------
    contract_size : float
    spot : float
    leverage : float
    account_equity_usd : float
    max_book_exposure_pct : float
    base_lots : float
        Floor — never go below this.

    Returns
    -------
    float
        Max lots per trade to keep the open book within
        ``max_book_exposure_pct × equity``. Returns
        ``base_lots`` if ``account_equity_usd <= 0``.
    """
    if account_equity_usd <= 0:
        return base_lots
    max_oz = (
        account_equity_usd
        * max_book_exposure_pct
        * leverage
        / spot
    )
    max_lots = max_oz / contract_size
    if max_lots < base_lots:
        return base_lots
    return max_lots


__all__ = [
    'account_exposure',
    'max_lots_for_exposure',
    'ExposureSnapshot',
    'DEFAULT_MAX_BOOK_EXPOSURE_PCT',
]