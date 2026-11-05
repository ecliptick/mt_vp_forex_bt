from __future__ import annotations
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
_ENGINE_BARS_PER_SEC = 705000.0
_ARTIFACT_BARS_PER_SEC = 424000.0
_LOAD_SECS_PER_MBAR = 7.0 / 41.2
DEFAULT_START = '2025-01-01'
DEFAULT_END = '2025-02-01'
APPROVAL_DAYS = 31

@dataclass(frozen=True)
class SweepEstimate:
    start: str
    end: str
    days: int
    n_bars: int
    n_cells: int
    artifacts_cached: bool
    load_secs: float
    artifact_secs: float
    engine_secs_per_cell: float

    @property
    def total_secs(self) -> float:
        return self.load_secs + self.artifact_secs + self.engine_secs_per_cell * self.n_cells

    @property
    def needs_approval(self) -> bool:
        return self.days > APPROVAL_DAYS

    @property
    def is_full_corpus(self) -> bool:
        return self.n_bars >= 40000000

    def render(self) -> str:
        art = 'WARM (cache HIT)' if self.artifacts_cached else 'COLD (cache MISS)'
        lines = [f'  window      : {self.start} .. {self.end}   ({self.days} days)', f'  bars        : ~{self.n_bars:,}', f'  artifacts   : {art}', f'  load        : ~{self.load_secs:.1f} s', f'  artifact    : ~{self.artifact_secs:.1f} s', f'  engine/cell : ~{self.engine_secs_per_cell:.1f} s', f'  cells       : {self.n_cells}', '  ' + '-' * 63, f'  ESTIMATED   : ~{_fmt_duration(self.total_secs)} total']
        if self.needs_approval:
            lines += ['  ' + '-' * 63, f'  NOTE        : this window is longer than {APPROVAL_DAYS} days.', '                The monthly chunking is an ENGINE artefact --', '                the p95 cap (7d), swing cap (24h) and VP profile', '                (7d rolling) are history-dependent, so a cold', '                chunk trades its first ~8 days UNCAPPED and', '                discards part of the trade universe.', '                Verify on the continuous corpus with leg_ledger', '                + leg_buckets before believing a cell.']
        lines.append('  ' + '-' * 63 + ('\n  re-run with --i-know to execute.' if self.needs_approval else ''))
        return '\n'.join(lines)

def _fmt_duration(secs: float) -> str:
    if secs < 90:
        return f'{secs:.0f} s'
    if secs < 5400:
        return f'{secs / 60:.1f} min'
    return f'{secs / 3600:.2f} h'

def _count_bars(data_path: Path, start: str | None, end: str | None) -> int:
    import datetime as _dt
    import pyarrow.compute as pc
    import pyarrow.parquet as pq
    pf = pq.ParquetFile(data_path)
    tbl = pf.read(columns=['time'])
    ts = tbl['time']
    lo = _dt.datetime.fromisoformat(start) if start else None
    hi = _dt.datetime.fromisoformat(end) if end else None
    mask = None
    if lo is not None:
        mask = pc.greater_equal(ts, _match_unit(lo, ts.type))
    if hi is not None:
        upper = pc.less(ts, _match_unit(hi, ts.type))
        mask = upper if mask is None else pc.and_(mask, upper)
    if mask is None:
        return tbl.num_rows
    return int(pc.sum(mask).as_py() or 0)

def _match_unit(bound: Any, arrow_type: Any) -> Any:
    import datetime as _dt
    import pyarrow as pa
    unit = getattr(arrow_type, 'unit', 'us')
    tz = getattr(arrow_type, 'tz', None)
    if tz is not None:
        bound = bound.replace(tzinfo=_dt.timezone.utc)
    return pa.scalar(bound, type=pa.timestamp(unit, tz=tz))

def estimate_sweep(data_path: Path, start: str | None=None, end: str | None=None, n_cells: int=1, artifacts_cached: bool=False) -> SweepEstimate:
    import datetime as _dt
    if start is None:
        start = DEFAULT_START
    if end is None:
        end = DEFAULT_END
    s = _dt.date.fromisoformat(start)
    e = _dt.date.fromisoformat(end)
    days = max(1, (e - s).days)
    n_bars = _count_bars(data_path, start, end)
    load_secs = n_bars / 1000000.0 * _LOAD_SECS_PER_MBAR
    artifact_secs = 0.0 if artifacts_cached else n_bars / _ARTIFACT_BARS_PER_SEC
    engine_secs = n_bars / _ENGINE_BARS_PER_SEC
    return SweepEstimate(start=start, end=end, days=days, n_bars=n_bars, n_cells=max(1, n_cells), artifacts_cached=artifacts_cached, load_secs=load_secs, artifact_secs=artifact_secs, engine_secs_per_cell=engine_secs)
