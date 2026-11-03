"""Results registry -- durable, commit-stamped run records.

See ``registry.py`` for the model and the docstring. This package
exposes the public API only::

    from runtime.results import registry

    registry.record_run(kind=..., params=..., summary=...)
    registry.find_runs(kind=...)
    registry.compare(run_a, run_b)
"""
from __future__ import annotations

__all__ = ["registry"]
