from __future__ import annotations
from bt._engine.vp_multi_target_engine import run_vp_sniper_backtest, BacktestResult, ClosedTrade, OpenTrade

def run_vp_sniper_backtest_forex(bars, zones, vp_set, lots: float, contract_size: float, *, engine_kwargs: dict, **kwargs):
    qty_oz = float(lots) * float(contract_size)
    return run_vp_sniper_backtest(bars=bars, zones=zones, vp_set=vp_set, qty_oz=qty_oz, **engine_kwargs, **kwargs)
__all__ = ['run_vp_sniper_backtest', 'run_vp_sniper_backtest_forex', 'BacktestResult', 'ClosedTrade', 'OpenTrade']
