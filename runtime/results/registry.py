from __future__ import annotations
import ast
import hashlib
import json
import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
_REPO_ROOT = Path(__file__).resolve().parents[2]
RESULTS_ROOT = _REPO_ROOT / 'results'
RUNS_DIR = RESULTS_ROOT / 'runs'
INDEX_PATH = RESULTS_ROOT / 'index.json'
_TS_FMT = '%Y-%m-%dT%H:%M:%SZ'

def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(('git',) + args, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip()

def git_state() -> dict[str, Any]:
    commit = _git('rev-parse', 'HEAD')
    short = _git('rev-parse', '--short', 'HEAD')
    status = _git('status', '--porcelain')
    branch = _git('rev-parse', '--abbrev-ref', 'HEAD')
    return {'commit': commit, 'commit_short': short or (commit[:7] if commit else None), 'dirty': bool(status) if status is not None else False, 'branch': branch}

def config_fingerprint(params: dict[str, Any]) -> str:
    payload = json.dumps({k: repr(v) for k, v in sorted(params.items())}, sort_keys=True)
    return hashlib.sha1(payload.encode('utf-8')).hexdigest()[:16]

def source_fingerprint(files: list[str] | tuple[str, ...] | None=None) -> str | None:
    if not files:
        return None
    return normalised_source_fingerprint(files)

def normalised_source_fingerprint(files: list[str] | tuple[str, ...] | None=None) -> str | None:
    if not files:
        return None
    h = hashlib.sha1()
    for rel in sorted(files):
        h.update(rel.encode('utf-8'))
        try:
            src = (_REPO_ROOT / rel).read_text(encoding='utf-8')
        except OSError:
            h.update(b'<missing>')
            continue
        try:
            h.update(ast.dump(ast.parse(src), annotate_fields=True, include_attributes=False).encode('utf-8'))
        except SyntaxError:
            h.update(src.encode('utf-8'))
    return h.hexdigest()[:16]

@dataclass
class RunEntry:
    run_id: str
    kind: str
    config_fp: str
    ts_utc: str
    commit: str | None
    commit_short: str | None
    dirty: bool
    branch: str | None
    src_fp: str | None
    params: dict[str, Any]
    summary: dict[str, Any]
    path: str
    parent_fp: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {'run_id': self.run_id, 'kind': self.kind, 'config_fp': self.config_fp, 'ts_utc': self.ts_utc, 'commit': self.commit, 'commit_short': self.commit_short, 'dirty': self.dirty, 'branch': self.branch, 'src_fp': self.src_fp, 'params': self.params, 'summary': self.summary, 'path': self.path, 'parent_fp': self.parent_fp}

    @staticmethod
    def from_dict(d: dict[str, Any]) -> 'RunEntry':
        return RunEntry(run_id=d['run_id'], kind=d['kind'], config_fp=d['config_fp'], ts_utc=d['ts_utc'], commit=d.get('commit'), commit_short=d.get('commit_short'), dirty=bool(d.get('dirty')), branch=d.get('branch'), src_fp=d.get('src_fp'), params=d.get('params', {}), summary=d.get('summary', {}), path=d.get('path', ''), parent_fp=d.get('parent_fp'))

def load_index() -> list[RunEntry]:
    if not INDEX_PATH.exists():
        return []
    try:
        raw = json.loads(INDEX_PATH.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError):
        return []
    entries = raw.get('runs') if isinstance(raw, dict) else raw
    out: list[RunEntry] = []
    for d in entries or []:
        try:
            out.append(RunEntry.from_dict(d))
        except (KeyError, TypeError):
            continue
    return out

def _write_index(entries: list[RunEntry]) -> None:
    RESULTS_ROOT.mkdir(parents=True, exist_ok=True)
    payload = {'schema': 'results-index/1', 'updated_utc': time.strftime(_TS_FMT, time.gmtime()), 'runs': [e.to_dict() for e in entries]}
    tmp = INDEX_PATH.with_suffix('.tmp')
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=False), encoding='utf-8')
    tmp.replace(INDEX_PATH)

def find_runs(*, kind: str | None=None, config_fp: str | None=None, commit: str | None=None, src_fp: str | None=None, param_contains: dict[str, Any] | None=None) -> list[RunEntry]:
    out = []
    for e in load_index():
        if kind is not None and e.kind != kind:
            continue
        if config_fp is not None and e.config_fp != config_fp:
            continue
        if commit is not None and e.commit != commit:
            continue
        if src_fp is not None and e.src_fp != src_fp:
            continue
        if param_contains:
            ok = True
            for k, v in param_contains.items():
                if k not in e.params or e.params[k] != v:
                    ok = False
                    break
            if not ok:
                continue
        out.append(e)
    return out

def latest_run(*, kind: str | None=None, config_fp: str | None=None) -> RunEntry | None:
    hits = find_runs(kind=kind, config_fp=config_fp)
    return hits[-1] if hits else None

def history_for(kind: str, params: dict[str, Any]) -> list[RunEntry]:
    return find_runs(kind=kind, config_fp=config_fingerprint(params))

def record_run(*, kind: str, params: dict[str, Any], summary: dict[str, Any] | None=None, payload: dict[str, Any] | None=None, src_fp: str | None=None, run_id: str | None=None) -> RunEntry:
    summary = dict(summary or {})
    params = dict(params or {})
    fp = config_fingerprint(params)
    state = git_state()
    ts = time.strftime(_TS_FMT, time.gmtime())
    if run_id is None:
        stamp = time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())
        safe = ''.join((ch if ch.isalnum() or ch in '-_.' else '-' for ch in f'{kind}-{fp}'))[:80]
        run_id = f"{safe}__{state['commit_short'] or 'nocommit'}__{stamp}"
    run_id = ''.join((ch if ch.isalnum() or ch in '-_.' else '-' for ch in run_id))[:160]
    entry = RunEntry(run_id=run_id, kind=kind, config_fp=fp, ts_utc=ts, commit=state['commit'], commit_short=state['commit_short'], dirty=state['dirty'], branch=state['branch'], src_fp=src_fp, params=params, summary=summary, path=f'results/runs/{run_id}.json', parent_fp=fp)
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    body = {'schema': 'results-run/1', **entry.to_dict(), 'payload': payload or {}}
    (RUNS_DIR / f'{run_id}.json').write_text(json.dumps(body, indent=2, default=str), encoding='utf-8')
    entries = load_index()
    entries = [e for e in entries if e.run_id != run_id]
    entries.append(entry)
    _write_index(entries)
    return entry

def load_run(run_id: str) -> dict[str, Any]:
    path = RUNS_DIR / f'{run_id}.json'
    if not path.exists():
        raise SystemExit(f'no such run: {run_id}\n(looked in {path})')
    return json.loads(path.read_text(encoding='utf-8'))

def compare(run_a: str, run_b: str, keys: list[str] | None=None) -> dict[str, Any]:
    a = load_run(run_a)
    b = load_run(run_b)
    sa = a.get('summary', {}) or {}
    sb = b.get('summary', {}) or {}
    keys = keys or sorted(set(sa) | set(sb))
    deltas: dict[str, Any] = {}
    for k in keys:
        va, vb = (sa.get(k), sb.get(k))
        d = None
        if isinstance(va, (int, float)) and isinstance(vb, (int, float)):
            d = vb - va
        deltas[k] = {'a': va, 'b': vb, 'delta': d}
    return {'a': {'run_id': a.get('run_id'), 'commit': a.get('commit_short'), 'ts_utc': a.get('ts_utc'), 'params': a.get('params')}, 'b': {'run_id': b.get('run_id'), 'commit': b.get('commit_short'), 'ts_utc': b.get('ts_utc'), 'params': b.get('params')}, 'same_config': a.get('config_fp') == b.get('config_fp'), 'same_commit': a.get('commit') == b.get('commit'), 'metrics': deltas}
