---
name: promote
description: Use when a swept value beats the canonical recipe and should become canon.
---

# Promote

1. Evidence required: the candidate beats the canonical cell on **net profit** over the full continuous window (run with `--i-know`, user approved), measured on the current code.
2. Show the user the canonical vs candidate row (trades, trades/day, EV/trade, net, Δ net, max DD) and get a yes.
3. Change the one constant in `bt/optimal_config.py`. Nowhere else.
4. Re-run `python -u -m runtime.sweeps.cached_sweep` and update the baseline line and the recipe table in `AGENTS.md`.
5. Knobs are paired when one changes what the other means (e.g. `vol_state_min_range_norm` with `sl_max_usd`). After promoting one, say which partner should be re-swept.
