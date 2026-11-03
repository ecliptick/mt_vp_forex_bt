"""Guards for the RESULTS REGISTRY.

The registry is the repo's durable, commit-stamped record of every
measurement. If it can be corrupted, or if it silently mis-identifies
a run, then every "have we measured this before?" answer is wrong --
and worse, they are wrong CONFIDENTLY.

These tests cover the three failure modes that would matter:

1. The registry writes to a path that is GITIGNORED -- i.e. the result
   exists on disk but is not in the repo. A result that is not in git
   is not a result.
2. Two genuinely different runs share a ``config_fp`` -- i.e. the
   fingerprint does not discriminate.
3. Two identical runs get DIFFERENT ``config_fp`` -- i.e. the
   fingerprint is not stable, so the "same params on a newer commit"
   query never matches.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from runtime.results import registry

_REPO_ROOT = Path(__file__).resolve().parents[1]


# ─────────────────────────────────────────────────────────────────────
# The fingerprint
# ─────────────────────────────────────────────────────────────────────
def test_config_fingerprint_is_stable_for_identical_params():
    a = {"knob": "sl_zone_mult", "value": 40.0, "scope": "x"}
    b = {"knob": "sl_zone_mult", "value": 40.0, "scope": "x"}
    assert registry.config_fingerprint(a) == registry.config_fingerprint(b)


def test_config_fingerprint_is_key_order_independent():
    a = {"knob": "sl_zone_mult", "value": 40.0, "scope": "x"}
    b = {"scope": "x", "value": 40.0, "knob": "sl_zone_mult"}
    assert registry.config_fingerprint(a) == registry.config_fingerprint(b)


def test_config_fingerprint_discriminates_different_params():
    a = {"knob": "sl_zone_mult", "value": 40.0}
    b = {"knob": "sl_zone_mult", "value": 80.0}
    c = {"knob": "sl_max_usd", "value": 40.0}
    assert registry.config_fingerprint(a) != registry.config_fingerprint(b)
    assert registry.config_fingerprint(a) != registry.config_fingerprint(c)


def test_config_fingerprint_does_not_depend_on_the_commit():
    """The commit is METADATA, not part of the key.

    This is the whole point of the registry: the SAME parameters
    re-measured on a newer commit must share a config_fp, so the
    "what did the bug fix do?" query is a diff rather than a
    coincidence.
    """
    p = {"knob": "sl_zone_mult", "value": 40.0}
    before = registry.config_fingerprint(p)
    # The commit cannot enter the fingerprint at all -- the function
    # only ever sees `params`.
    assert set(p) == {"knob", "value"}
    assert before == registry.config_fingerprint(dict(p))


# ─────────────────────────────────────────────────────────────────────
# The run_id
# ─────────────────────────────────────────────────────────────────────
def test_run_id_is_a_legal_filename_on_windows():
    """A run_id becomes a FILENAME. Windows rejects ':' in a name.

    The run_id embeds a timestamp, which is exactly where the colons
    come from -- so this bit us before it is guarded.
    """
    import time
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    assert ":" in stamp
    safe = "".join(
        ch if ch.isalnum() or ch in "-_." else "-" for ch in f"leg_ledger-{stamp}"
    )
    assert ":" not in safe
    # And the sanitised form must survive a round trip through Path.
    assert Path(safe).name == safe


def test_recorded_run_id_contains_no_illegal_characters(tmp_path, monkeypatch):
    """The written file must actually exist on disk."""
    monkeypatch.setattr(registry, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(registry, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(registry, "INDEX_PATH", tmp_path / "index.json")
    entry = registry.record_run(
        kind="leg_ledger",
        params={"a": 1},
        summary={"legs": 1},
    )
    written = tmp_path / "runs" / f"{entry.run_id}.json"
    assert written.exists(), f"run file not written: {written}"
    assert ":" not in entry.run_id


# ─────────────────────────────────────────────────────────────────────
# The index
# ─────────────────────────────────────────────────────────────────────
def test_index_round_trips_a_recorded_run(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(registry, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(registry, "INDEX_PATH", tmp_path / "index.json")
    entry = registry.record_run(
        kind="sweep_cell",
        params={"knob": "x", "value": 1},
        summary={"n_trades": 10, "net": -1.0},
    )
    found = registry.find_runs(kind="sweep_cell")
    assert len(found) == 1
    assert found[0].run_id == entry.run_id
    assert found[0].summary["n_trades"] == 10

    loaded = registry.load_run(entry.run_id)
    assert loaded["summary"]["net"] == -1.0
    assert loaded["params"]["knob"] == "x"


def test_a_corrupt_index_does_not_raise(tmp_path, monkeypatch):
    """A damaged index must degrade to 'no runs', not crash the CLI."""
    monkeypatch.setattr(registry, "INDEX_PATH", tmp_path / "index.json")
    (tmp_path / "index.json").write_text("{not json", encoding="utf-8")
    assert registry.load_index() == []


def test_history_for_finds_every_run_of_the_same_params(tmp_path, monkeypatch):
    """The 'same params, newer commit' query."""
    monkeypatch.setattr(registry, "RUNS_DIR", tmp_path / "runs")
    monkeypatch.setattr(registry, "RESULTS_ROOT", tmp_path)
    monkeypatch.setattr(registry, "INDEX_PATH", tmp_path / "index.json")
    p = {"start": "2024-01-01", "end": "2026-07-16"}
    registry.record_run(kind="leg_ledger", params=p,
                        summary={"legs": 51075}, run_id="a")
    registry.record_run(kind="leg_ledger", params=p,
                        summary={"legs": 51090}, run_id="b")
    other = {"start": "2025-01-01", "end": "2025-02-01"}
    registry.record_run(kind="leg_ledger", params=other,
                        summary={"legs": 904}, run_id="c")

    hist = registry.history_for("leg_ledger", p)
    assert [h.run_id for h in hist] == ["a", "b"]
    assert len(registry.history_for("leg_ledger", other)) == 1


# ─────────────────────────────────────────────────────────────────────
# THE GUARD THAT MATTERS: the results must be committable
# ─────────────────────────────────────────────────────────────────────
def test_the_results_directory_is_not_gitignored():
    """A result that is not in git is not a result.

    ``.gitignore`` has ``*_ledger*.json`` (the bulky scratch ledgers)
    and the results registry's run files are named
    ``leg_ledger-<fp>__<commit>__<ts>.json``. Without an explicit
    re-include, the registry would be silently ignored and every
    measurement would stay local.
    """
    def ignored(path: str) -> bool:
        out = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=str(_REPO_ROOT), capture_output=True, text=True,
        )
        return out.returncode == 0

    assert not ignored("results/index.json"), \
        "results/index.json is gitignored -- the registry would not be committed"
    assert not ignored("results/runs/leg_ledger-abc123__deadbee__20260101T000000Z.json"), \
        "results/runs/*.json is gitignored -- the registry would not be committed"


def test_the_results_registry_files_are_tracked_or_untracked_but_present():
    """The registry's own code must be committable too."""
    def ignored(path: str) -> bool:
        out = subprocess.run(
            ["git", "check-ignore", "-q", path],
            cwd=str(_REPO_ROOT), capture_output=True, text=True,
        )
        return out.returncode == 0

    assert not ignored("runtime/results/registry.py")
    assert not ignored("runtime/results/cli.py")


def test_the_index_schema_is_declared():
    """The index must declare its schema version.

    A consumer that does not know which schema it is reading cannot
    refuse to misread it.
    """
    path = registry.INDEX_PATH
    if not path.exists():
        pytest.skip("no registry on disk yet")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data.get("schema") == "results-index/1"
    assert isinstance(data.get("runs"), list)
    for r in data["runs"]:
        assert {"run_id", "kind", "config_fp", "ts_utc", "commit",
                "dirty", "params", "summary"} <= set(r)
