---
name: sweep
description: Use to measure a knob — one-month screen first, then the full corpus only with the user's go-ahead.
---

# Sweep

One knob:
```bash
python -u -m runtime.sweeps.cached_sweep --knob sl_max_usd --grid 5,10,15,20
```
Several knobs (cartesian product):
```bash
python -u -m runtime.sweeps.cached_sweep --knobs "vol_state_min_range_norm=0.5,1.0;sl_max_usd=8,15"
```

* Default window is 2025-01 (~2 s per cell after the first artifact build).
* Longer windows: `--start YYYY-MM-DD --end YYYY-MM-DD --i-know`. Before launching, tell the user the window, cell count and the driver's cost estimate. One long run at a time.
* The knob must be on an allowlist in `bt/optimal_config.py`; otherwise it raises `KeyError`.
* A knob whose every cell matches the baseline is inert under the current recipe — report it, do not promote it.
* Report every cell as a table: trades, EV/trade, net, Δ net vs canonical, max DD.
* Judge on **net profit**, then EV, trades/day and max drawdown. One month is a screen; a promotion needs the full continuous window.
* Each cell is recorded in `results/`. `python -m runtime.results.cli list` / `compare <a> <b>`.
