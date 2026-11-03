"""CLI for the results registry.

::

    python -m runtime.results.cli list
    python -m runtime.results.cli list --kind sweep_cell
    python -m runtime.results.cli find --knob sl_zone_mult --value 40
    python -m runtime.results.cli show <run_id>
    python -m runtime.results.cli compare <run_a> <run_b>
    python -m runtime.results.cli history --kind sweep_cell --params '{"knob":"sl_zone_mult"}'
    python -m runtime.results.cli stale
    python -m runtime.results.cli state
"""
from __future__ import annotations

import argparse
import json
import sys

from runtime.results import registry


def _fmt_entry(e: registry.RunEntry) -> str:
    s = e.summary or {}
    head = ", ".join(
        f"{k}={s[k]}" for k in
        ("legs", "n_trades", "gross", "net", "gross_per_leg")
        if k in s
    )
    dirty = " DIRTY" if e.dirty else ""
    return (f"{e.ts_utc}  {e.commit_short or '????????'}{dirty}  "
            f"{e.kind:<22} fp={e.config_fp}  {head}")


def cmd_list(args: argparse.Namespace) -> int:
    entries = registry.find_runs(kind=args.kind)
    if not entries:
        print("no runs recorded")
        return 0
    for e in entries:
        print(_fmt_entry(e))
    print(f"\n{len(entries)} run(s)")
    return 0


def cmd_find(args: argparse.Namespace) -> int:
    """Find runs by a parameter. ``--knob X --value Y`` is shorthand."""
    contains: dict[str, object] = {}
    if args.knob:
        contains["knob"] = args.knob
    if args.value is not None:
        contains["value"] = _coerce(args.value)
    for pair in args.param or []:
        k, _, v = pair.partition("=")
        contains[k] = _coerce(v)
    entries = registry.find_runs(kind=args.kind, param_contains=contains or None)
    if not entries:
        print("no matching runs")
        return 1
    for e in entries:
        print(_fmt_entry(e))
    print(f"\n{len(entries)} match(es)")
    return 0


def _coerce(v: str):
    for cast in (int, float):
        try:
            return cast(v)
        except ValueError:
            pass
    if v.lower() in ("true", "false"):
        return v.lower() == "true"
    return v


def cmd_show(args: argparse.Namespace) -> int:
    run = registry.load_run(args.run_id)
    print(json.dumps(run, indent=2))
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    out = registry.compare(args.run_a, args.run_b)
    a, b = out["a"], out["b"]
    print(f"A: {a['run_id']}  commit={a['commit']}  {a['ts_utc']}")
    print(f"B: {b['run_id']}  commit={b['commit']}  {b['ts_utc']}")
    print(f"same_config={out['same_config']}  same_commit={out['same_commit']}")
    print()
    print(f"{'metric':<22}{'A':>18}{'B':>18}{'delta':>18}")
    for k, v in out["metrics"].items():
        a_s = _num(v["a"])
        b_s = _num(v["b"])
        d_s = _num(v["delta"])
        print(f"{k:<22}{a_s:>18}{b_s:>18}{d_s:>18}")
    return 0


def _num(v) -> str:
    """Format a number for a fixed-width column."""
    if v is None:
        return "-"
    if isinstance(v, bool):
        return str(v)
    if isinstance(v, (int, float)):
        return f"{v:,.4f}".rstrip("0").rstrip(".") or "0"
    return str(v)[:18]


def cmd_history(args: argparse.Namespace) -> int:
    params = json.loads(args.params)
    entries = registry.history_for(args.kind, params)
    if not entries:
        print("no runs with these params")
        return 1
    print(f"params: {json.dumps(params, sort_keys=True)}")
    print(f"config_fp: {entries[0].config_fp}")
    print()
    for e in entries:
        print(_fmt_entry(e))
    if len(entries) > 1:
        print("\ndeltas vs first:")
        base = entries[0].summary
        for e in entries[1:]:
            for k in base:
                if isinstance(base[k], (int, float)) and \
                        isinstance(e.summary.get(k), (int, float)):
                    print(f"  {e.commit_short} {k}: "
                          f"{base[k]} -> {e.summary[k]} "
                          f"({e.summary[k] - base[k]:+.4f})")
    return 0


def cmd_stale(args: argparse.Namespace) -> int:
    """Runs whose params match a CURRENT config but whose commit is old.

    These are the candidates for a re-measure after a code change.
    """
    entries = registry.load_index()
    state = registry.git_state()
    if not entries:
        print("no runs recorded")
        return 0
    stale = [e for e in entries
             if e.commit and state["commit"] and e.commit != state["commit"]]
    print(f"current commit: {state['commit_short']} "
          f"(dirty={state['dirty']})")
    print(f"{len(stale)} of {len(entries)} run(s) are on an older commit\n")
    for e in stale:
        print(f"  {e.commit_short}  {e.kind:<22} "
              f"fp={e.config_fp}  {e.ts_utc}")
    return 0


def cmd_state(args: argparse.Namespace) -> int:
    print(json.dumps(registry.git_state(), indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="runtime.results.cli")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("list", help="list recorded runs")
    p.add_argument("--kind", default=None)
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("find", help="find runs by parameter")
    p.add_argument("--kind", default=None)
    p.add_argument("--knob", default=None)
    p.add_argument("--value", default=None)
    p.add_argument("--param", action="append", default=[],
                   help="key=value (repeatable)")
    p.set_defaults(func=cmd_find)

    p = sub.add_parser("show", help="print one run record")
    p.add_argument("run_id")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("compare", help="diff two runs' summary metrics")
    p.add_argument("run_a")
    p.add_argument("run_b")
    p.set_defaults(func=cmd_compare)

    p = sub.add_parser("history", help="all runs of the same params")
    p.add_argument("--kind", required=True)
    p.add_argument("--params", required=True,
                   help="JSON object of the params")
    p.set_defaults(func=cmd_history)

    p = sub.add_parser("stale", help="runs on an older commit")
    p.set_defaults(func=cmd_stale)

    p = sub.add_parser("state", help="print the git state")
    p.set_defaults(func=cmd_state)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
