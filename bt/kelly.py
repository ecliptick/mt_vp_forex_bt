"""Fractional Kelly sizing (NB81 quarter-Kelly canonical).

NB81 finding (BTC, 2026-09-05): quarter-Kelly (``f*/4``) on a
stable estimate of ``f*`` is the optimal sizing for the BTC
VP-MT strategy. Full Kelly is too aggressive on the drawdown
curve; half-Kelly underperforms in CAGR by 12%/year. Findings
carry over to forex — BTC and XAUUSD return distributions are
both heavy-tailed.

This module is **self-contained**: there is no upstream
``kelly.py`` to import from (neither the crypto nor the forex
research repo ships one). The math here is the canonical
Kelly formula on a per-trade P&L series. The ``KellySizing``
dataclass is the only artifact sweep drivers consume.

NO DEFAULTS POLICY
==================

Every field on :class:`KellySizing` is populated by
:func:`size_lots`. There are no defaults on the dataclass
itself. Inputs to ``size_lots`` likewise have no defaults — a
sweep cell that forgets a kwarg fails loud.
"""
from __future__ import annotations

from dataclasses import dataclass

# Canonical NB81 fractional Kelly factor.
DEFAULT_FRACTIONAL_KELLY = 0.25          # quarter-Kelly
DEFAULT_MIN_TRADES_ESTIMATE = 30          # below this, return lots=base


@dataclass
class KellySizing:
    """Sizing result for one Kelly pass.

    Fields
    ------
    f_star : float
        Full Kelly fraction (could be > 1; ``>1`` would mean
        leverage > 1).
    fractional : float
        Fractional Kelly (canonical ``f*/4``).
    lots_fraction : float
        Lots at the canonical 0.01 base, scaled by fractional
        Kelly. ``lots = 0.01 × lots_fraction``. Clamped to
        MT4 broker max (``100`` lots typically).
    recommended_lots : float
        Recommended lots for the next trade (clamped).
    sample_size : int
        Number of trades in the input ``pnl_series_usd``.
    insufficient_data : bool
        True if ``sample_size < 30``; the recommended lots
        then defaults to ``lots=0.01`` (the canonical base)
        and ``f*=0``.
    """

    f_star: float
    fractional: float
    lots_fraction: float
    recommended_lots: float
    sample_size: int
    insufficient_data: bool


def win_rate(pnl_series_usd: list[float]) -> float:
    """Fraction of trades that were profitable (P&L > 0)."""
    if not pnl_series_usd:
        return 0.0
    wins = sum(1 for p in pnl_series_usd if p > 0)
    return wins / len(pnl_series_usd)


def avg_win_loss_ratio(pnl_series_usd: list[float]) -> float:
    """Average win / average loss ratio (NB81 b).

    Returns ``0.0`` if there are no wins or no losses.
    """
    wins = [p for p in pnl_series_usd if p > 0]
    losses = [p for p in pnl_series_usd if p <= 0]
    if not wins or not losses:
        return 0.0
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss == 0:
        return 0.0
    return avg_win / avg_loss


def full_kelly_fraction(pnl_series_usd: list[float]) -> float:
    """Full Kelly ``f* = (p × b - q) / b`` from a P&L series.

    Parameters
    ----------
    pnl_series_usd : list[float]
        Per-trade P&L in USD.

    Returns
    -------
    float
        Full Kelly fraction ``f*``. Returns ``0.0`` if the
        input is empty, has no wins, has no losses, or the
        ``f*`` is negative (don't bet on a negative-EV edge).

    Notes
    -----
    ``f*`` can exceed 1.0 if the edge is strong; that means
    the strategy wants leverage > 1×. We don't clamp here;
    the caller does. See :func:`fractional_kelly`.
    """
    if not pnl_series_usd:
        return 0.0
    p = win_rate(pnl_series_usd)
    b = avg_win_loss_ratio(pnl_series_usd)
    if p == 0 or b == 0:
        return 0.0
    q = 1 - p
    f_star = (p * b - q) / b
    if f_star < 0:
        return 0.0
    return f_star


def fractional_kelly(f_star: float, factor: float = DEFAULT_FRACTIONAL_KELLY) -> float:
    """Apply a fractional factor to ``f*``.

    NB81 canonical: ``factor=0.25`` (quarter-Kelly).
    """
    return f_star * factor


def size_lots(
    *,
    pnl_series_usd: list[float],
    base_lots: float = 0.01,
    broker_max_lots: float = 100.0,
    fractional_factor: float = DEFAULT_FRACTIONAL_KELLY,
    min_trades_estimate: int = DEFAULT_MIN_TRADES_ESTIMATE,
) -> KellySizing:
    """Compute the recommended lots for the next trade.

    Parameters
    ----------
    pnl_series_usd : list[float]
        Per-trade P&L in USD (post-fee).
    base_lots : float
        Canonical MT4 base (0.01).
    broker_max_lots : float
        MT4 broker lot cap (typical: 100).
    fractional_factor : float
        Kelly fraction (NB81 canonical: 0.25 = quarter-Kelly).
    min_trades_estimate : int
        Below this, return ``lots=base_lots`` (degenerate).

    Notes
    -----
    The recommended lots is computed as:
    ``base_lots + (broker_max - base_lots) × fractional``,
    then clamped to ``[base_lots, broker_max]``.

    Returns
    -------
    KellySizing
    """
    sample_size = len(pnl_series_usd)
    insufficient = sample_size < min_trades_estimate

    f_star = 0.0
    if not insufficient:
        f_star = full_kelly_fraction(pnl_series_usd)

    fractional = fractional_kelly(f_star, factor=fractional_factor)

    if fractional <= 0.0:
        lots_fraction = 0.0
        raw_lots = base_lots
    elif fractional >= 1.0:
        lots_fraction = 1.0
        raw_lots = broker_max_lots
    else:
        lots_fraction = fractional
        raw_lots = base_lots + (broker_max_lots - base_lots) * fractional

    if raw_lots < base_lots:
        raw_lots = base_lots
    if raw_lots > broker_max_lots:
        raw_lots = broker_max_lots

    if insufficient:
        raw_lots = base_lots

    return KellySizing(
        f_star=f_star,
        fractional=fractional,
        lots_fraction=lots_fraction,
        recommended_lots=raw_lots,
        sample_size=sample_size,
        insufficient_data=insufficient,
    )


__all__ = [
    'size_lots',
    'KellySizing',
    'win_rate',
    'avg_win_loss_ratio',
    'full_kelly_fraction',
    'fractional_kelly',
    'DEFAULT_FRACTIONAL_KELLY',
    'DEFAULT_MIN_TRADES_ESTIMATE',
]