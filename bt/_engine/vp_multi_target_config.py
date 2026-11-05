from __future__ import annotations
from typing import Tuple
SL_MODE_FVG_ONLY: str = 'fvg_only'
SL_MODE_HVN_LVN: str = 'hvn_lvn'
SL_MODES: Tuple[str, ...] = (SL_MODE_FVG_ONLY, SL_MODE_HVN_LVN, 'fvg_or_poc')
TP_MODE_FVG_MULT: str = 'fvg_mult'
TP_MODE_MULTI_TARGET: str = 'multi_target'
TP_MODE_HVN_SINGLE: str = 'hvn_single'
TP_MODES: Tuple[str, ...] = (TP_MODE_FVG_MULT, TP_MODE_MULTI_TARGET, TP_MODE_HVN_SINGLE)
LVN_MODE_SKIP: str = 'skip'
ENTRY_FILL_TOUCH: str = 'touch'
ENTRY_FILL_NEXT_OPEN: str = 'next_open'
ENTRY_FILL_TRIGGER: str = 'trigger'
ENTRY_FILL_MODELS: Tuple[str, ...] = (ENTRY_FILL_TOUCH, ENTRY_FILL_NEXT_OPEN, ENTRY_FILL_TRIGGER)
FILL_MODEL_MID: str = 'mid'
FILL_MODEL_PESSIMISTIC: str = 'pessimistic'
FILL_MODEL_NEXT_OPEN: str = 'next_open'
FILL_MODELS: Tuple[str, ...] = (FILL_MODEL_MID, FILL_MODEL_PESSIMISTIC, FILL_MODEL_NEXT_OPEN)
