---
name: results-registry
description: >-
  Record, find, compare and track backtest measurements in the SNIPER
  repo's commit-stamped results registry under results/. Use when asked
  to save a run result, look up a previous measurement, check whether a
  configuration was already measured, compare a run before and after a
  bug fix, or find results from an older commit.
disable-model-invocation: true
---

# Results registry — durable, commit-stamped measurements

## Why it exists

Measurements used to live only in gitignored scratch directories. Two problems
followed:

1. **A result had no identity.** `sl_zone_mult__cell_3.json` says nothing
   about which commit produced it, which recipe it measured, or whether it was
   re-measured after a bug fix. When the engine changed, old numbers sat on
   disk looking exactly like new ones.
2. **"Have we already measured this?" had no answer.** The only way to know
   was to grep a directory of JSON files by eye.

## The model

Every run carries FOUR identities, and they answer different questions:

| field | identifies | answers |
|---|---|---|
| `config_fp` | the **PARAMETERS**, and nothing else | "have we measured this configuration?" |
| `commit` | the **CODE** | "was this measured on the current code?" |
| `dirty` | whether the tree was modified | "is this even reproducible?" |
| `src_fp` | the artifact-producing **sources** | "do these two runs measure the same thing?" |

**`config_fp` deliberately excludes the commit.** That is the whole point:
when a bug fix lands, the SAME parameters are re-measured and the two records
sit side by side with an identical `config_fp` and different `commit` — so
the effect of the fix is visible as a diff. The commit is metadata, not part
of the key.

## The files

```
results/
  index.json          # one small entry per run — committed, diffable
  runs/<run_id>.json  # the full payload of one run — committed
```

`index.json` holds one entry per run, chronological. Each entry is small (a
config fingerprint, a commit, a few summary metrics) so the file stays
readable as a git diff. The full payload lives in `runs/`.

**Both are COMMITTED. A result that is not in git is not a result.** The old
scratch dirs (`runtime/sweeps/_results/`, `runtime/diagnostics/_reports/`)
stay gitignored and are for bulky intermediates only.

## Reading it — the CLI

```bash
python -m runtime.results.cli list                     # every run
python -m runtime.results.cli list --kind sweep_cell
python -m runtime.results.cli find --knob sl_zone_mult --value 40
python -m runtime.results.cli show <run_id>             # full record
python -m runtime.results.cli compare <run_a> <run_b>   # diff two runs
python -m runtime.results.cli history --kind leg_ledger \
    --params '{"start":"2024-01-01","end":"2026-07-16","warmup_days":14}'
python -m runtime.results.cli stale                     # runs on an older commit
python -m runtime.results.cli state                     # the git state
```

The two queries you will actually reach for:

**"Have we measured this before?"** — `find` by params, then compare
`config_fp` AND `commit`.

**"What did the bug fix change?"** — `history` with the same params. Same
`config_fp`, different `commit`, and the `history` output prints the per-metric
deltas against the first run.

## Writing it — the API

```python
from runtime.results import registry

registry.record_run(
    kind="sweep_cell",
    params={
        "knob": "sl_zone_mult",
        "value": 40.0,
        "overrides": {...},      # every override vs canonical
        "scope": scope,
        "start": start,
        "end": end,
        "data": str(data_path),
        "notebook": notebook,
    },
    summary={
        "n_trades": 51075,
        "gross": 7312.96,
        "net": -12445.50,
        "gross_per_leg": 0.1432,
    },
    payload=full_record,        # the whole thing
    src_fp=registry.source_fingerprint(_ARTIFACT_SOURCES),
)
```

**`params` must contain every input that could change the measurement.** If a
param is missing, two genuinely different runs will share a `config_fp` and
the registry will lie.

**`summary` should hold the headline metrics only** — the few numbers a
reader wants to compare across commits. The full detail goes in `payload`.

### `src_fp` — when to pass it

`src_fp` is a SHA1 over the source files that determine the artifact (the
engine, the detector, the config). It answers "do these two runs measure the
same thing?" — which `commit` cannot, because a dirty tree has the same
commit but different code.

Pass the same file list the diagnostic's own cache uses. The sweep driver
passes `_ARTIFACT_SOURCES`; the ledger passes its `_source_hash()`.

## The rules

- **Never hand-edit `results/index.json`.** It is generated.
- **Every new diagnostic that produces a verdict must call
  `registry.record_run`.** A number nobody can find is not evidence.
- **Before re-measuring, check the registry first.** If the params and the
  commit both match, the number is already known.
- **Record the variant, not just the canonical.** A variant run is a real
  measurement. Give it its own `kind` or a `variant` flag in `params` so it
  never masquerades as the canonical book.
- **Never write a variant to the canonical artifact.** The ledger already
  guards this; the registry is a second line of defence because the registry
  is what people read.

## Reference

- [`AGENTS.md`](../../../AGENTS.md) § 2 — the registry section.
- `runtime/results/registry.py` — the implementation.
- `runtime/results/cli.py` — the CLI.
