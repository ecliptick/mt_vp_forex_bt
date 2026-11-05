from __future__ import annotations
from dataclasses import dataclass

@dataclass
class PendingSignal:
    trigger_bar: int
    direction: int
    triggered_by: str = 'fvg'
    stop_usd: float = 0.0
    target_usd: float = 0.0
    conviction: float = 1.0
    fvg_zone: object = None
    is_ifvg: bool = False
    rank_tier: str = ''
    rank_percentile: float = 0.0
    fill_price: float | None = None
