"""Risk, lot sizing, and live MT4 fee math for the XAUUSD setup.

The user constraint is:

* Lots base: ``0.01`` (MT4 minimum).
* Leverage: 100× (× = contract_size × lots ÷ account_equity).
* Margin requirement: ``$30`` for a ~$3,000 gold notional
  (1 oz × $3,000 ≈ $3,000 notional, divided by 100× leverage).
* LIVE MT4 fees:
    - spread_per_oz_usd = 0.40 (round-trip, $/oz)
    - commission_per_lot_usd = 7.0 (round-trip, $/lot)

The math:

* Notional at 0.01 lots: ``0.01 × contract_size × spot = 1 ×
  spot`` = 1 oz notional.
* Margin at 100× leverage: ``1 × spot ÷ 100 = spot / 100``.
  At spot ~$3000, margin ≈ $30.
* Round-trip cost per trade (live MT4):
  ``lots × commission_per_lot_usd + oz × spread_per_oz_usd``
  = ``0.01×7 + 1×0.40 = $0.07 + $0.40 = $0.47``.

P&L per $1 move at 1 oz: ``+ $1`` (or ``- $1`` short). Same
as BTC at 1 BTC notional.

There is NO "overlay" concept in this repo. The historical
$0.19 backtest baseline is gone; ``spread_per_oz_usd`` is the
LIVE quote, full stop.

This module materializes the math, with NO defaults — every
field is required, so a sweep cell that forgets a knob fails
loud.
"""
from __future__ import annotations

from dataclasses import dataclass

# CANONICAL SIZING LIVES IN ``bt/optimal_config.py``.
#
# This file used to define its own ``DEFAULT_LOTS = 0.01`` /
# ``DEFAULT_CONTRACT_SIZE = 100.0`` / ``DEFAULT_LEVERAGE = 100.0`` —
# a second copy of the canonical ``FOREX_LOTS`` /
# ``FOREX_CONTRACT_SIZE`` / ``FOREX_LEVERAGE``. Nothing outside this
# module read them. They are deleted; import the canonical constants
# from :mod:`bt.optimal_config` instead.
#
# Note this module is the RISK MATH (margin, per-move P&L, fee
# breakdown). The canonical VALUES it consumes are passed in as
# required arguments — the functions here have no defaults by
# design.


@dataclass
class MarginAndCost:
    """Margin + cost breakdown for one trade.

    Fields
    ------
    notional_usd : float
        Trade notional in USD = lots × contract_size × spot.
        At canonical 0.01 lots, contract_size=100, spot=3000,
        notional = $3000 (1 oz × $3000).
    margin_required_usd : float
        Margin locked at the broker for this trade, at the
        given leverage. ``notional_usd / leverage``. At 100×
        leverage on $3000 notional, margin ≈ $30.
    round_trip_cost_usd : float
        Per-trade fee (= commission + spread). Includes the
        baseline backtest fee PLUS the live overlay.
    commission_usd : float
        Commission leg in ``$`` (= lots × (baseline + overlay)
        commission per lot).
    spread_usd : float
        Spread leg in ``$`` (= oz × (baseline + overlay)
        spread per oz).
    effective_spot : float
        Spot passed in (USD/oz).
    """

    notional_usd: float
    margin_required_usd: float
    round_trip_cost_usd: float
    commission_usd: float
    spread_usd: float
    effective_spot: float


def margin_and_cost(
    *,
    lots: float,
    contract_size: float,
    leverage: float,
    spot: float,
    commission_per_lot_usd: float,
    spread_per_oz_usd: float,
) -> MarginAndCost:
    """Compute margin + cost for one trade.

    All fields are required (NO defaults). Sweep drivers vary
    the inputs explicitly to A/B sizing scenarios.

    Parameters
    ----------
    lots : float
        MT4 lots (canonical ``0.01``).
    contract_size : float
        Contract size (canonical ``100.0`` = 100 oz / lot).
    leverage : float
        Account leverage (canonical ``100.0`` = 100×).
    spot : float
        Current USD/oz spot at entry.
    commission_per_lot_usd : float
        Round-trip commission per lot, $/lot.
    spread_per_oz_usd : float
        Round-trip spread per oz, $/oz (LIVE MT4 quote).

    Returns
    -------
    MarginAndCost

    Notes
    -----
    At canonical ``lots=0.01, contract_size=100, spot=3000,
    leverage=100`` with LIVE MT4 fees:

    * notional = $3,000.
    * margin = $30.
    * round-trip cost = 0.01×$7 + 1×$0.40 = $0.47.

    There is no "overlay" concept — ``spread_per_oz_usd`` is
    the live quote, full stop. The historical $0.19 baseline
    is gone from this repo.
    """
    notional_usd = lots * contract_size * spot
    margin_required_usd = notional_usd / leverage

    oz = lots * contract_size
    commission_usd = lots * commission_per_lot_usd
    spread_usd = oz * spread_per_oz_usd
    round_trip_cost_usd = commission_usd + spread_usd

    return MarginAndCost(
        notional_usd=notional_usd,
        margin_required_usd=margin_required_usd,
        round_trip_cost_usd=round_trip_cost_usd,
        commission_usd=commission_usd,
        spread_usd=spread_usd,
        effective_spot=spot,
    )


def pnl_per_usd_move(
    *,
    lots: float,
    contract_size: float,
    direction: str,
) -> float:
    """P&L per $1 USD price move in the trade direction.

    Returns the dollar P&L per $1 of favorable move
    (``direction='long'``: $1 up = +$X; ``direction='short'``:
    $1 down = +$X).

    For canonical ``lots=0.01, contract_size=100``: 1 oz
    notional, so $1 move = +$1.00 on long, -$1.00 on short.

    For non-canonical sizing (e.g. 0.05 lots for a
    ``max_same_dir_open`` scenario), the function scales.

    Parameters
    ----------
    lots, contract_size : float
        Sizing knobs.
    direction : str
        'long' | 'short'.

    Returns
    -------
    float
        Dollar P&L per $1 of favorable move. Negative for
        shorts (so callers can multiply by the favorable move
        delta directly).
    """
    if direction not in ('long', 'short'):
        raise ValueError(
            f"direction must be 'long' or 'short', got {direction!r}"
        )
    sign = 1.0 if direction == 'long' else -1.0
    return sign * lots * contract_size


def kelly_fraction_from_pnl(
    *,
    pnl_series_usd: list[float],
    margin_per_trade_usd: float,
) -> float:
    """Kelly fraction from a per-trade P&L series.

    Helper for :mod:`bt.kelly` — the actual Kelly fraction
    calculation lives there. This thin wrapper just materializes
    the math for sizing.

    Parameters
    ----------
    pnl_series_usd : list[float]
        Per-trade P&L in USD. Each entry is post-fee, post-spread.
        Length should be ``>= 30`` for a stable estimate; below
        that, returns 0.0 (insufficient data).
    margin_per_trade_usd : float
        Margin per trade in USD (use ``MarginAndCost.margin_required_usd``
        from this module).

    Returns
    -------
    float
        Kelly fraction ``f*`` clamped to ``[0.0, 1.0]``. The
        canonical fractional-Kelly factor is ``f*/4`` (this
        function returns the FULL Kelly; ``bt.kelly`` clamps
        to quarter-Kelly for the engine path).

    Notes
    -----
    ``f* = (p × b - q) / b`` where ``p = P(win)``,
    ``b = avg_win / avg_loss``, ``q = 1 - p``. Returned
    value is the full Kelly, not quarter-Kelly.
    """
    if len(pnl_series_usd) < 30:
        return 0.0
    if margin_per_trade_usd <= 0:
        return 0.0

    wins = [p for p in pnl_series_usd if p > 0]
    losses = [p for p in pnl_series_usd if p <= 0]
    p = len(wins) / len(pnl_series_usd)
    if not wins or not losses:
        return 0.0
    avg_win = sum(wins) / len(wins)
    avg_loss = abs(sum(losses) / len(losses))
    if avg_loss == 0:
        return 0.0
    b = avg_win / avg_loss
    f_star = (p * b - (1 - p)) / b
    if f_star < 0.0:
        return 0.0
    if f_star > 1.0:
        return 1.0
    return f_star


__all__ = [
    'margin_and_cost',
    'pnl_per_usd_move',
    'kelly_fraction_from_pnl',
    'MarginAndCost',
]