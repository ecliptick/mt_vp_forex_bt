# sniper_engine

Backtest engine for the SNIPER FVG strategy on XAUUSD (MT4/MT5), 0.01 lots × 100 oz = 1 oz per trade, 100× leverage.

## Workflow

**engine change → param sweep → promote to canonical.** Nothing else. Skills: `/engine-change`, `/sweep`, `/promote`.

## Layout

| path | role |
|---|---|
| `bt/_engine/` | the engine: FVG detector, market structure, volume profile, vol state, caps, bar loop |
| `bt/vp_engine.py` | XAUUSD sizing wrapper (`qty_oz = lots × contract_size`) |
| `bt/optimal_config.py` | **the canonical recipe** — the only place a parameter value lives |
| `runtime/sweeps/cached_sweep.py` | the sweep driver |
| `runtime/results/` | results registry (`results/index.json`, `results/runs/`) |

Data: `C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet` (1s bars).

## Execution model

* Entries are resting **limit orders** filled on touch, at the limit price.
* Exits book at the **SL/TP level exactly**, including when a bar gaps through it. No slippage, no overshoot.
* A bar touching both SL and TP: the stop wins.
* No fees: no commission, no spread. Net P&L = gross P&L.
* Timestamps are int64 epoch **nanoseconds** (`bt._engine.timebase.to_ns_arr`).

## Rules

1. **Canonical = `bt/optimal_config.py`.** Every other number in this repo is stale until re-measured on the current code.
2. **Judge on net profit**, over one continuous window. Never split execution by month. Aggregate at most daily.
3. **No placebos, no market-drift / passive-hold comparisons.**
4. **A new knob lands in this order:** engine parameter → config constant → `build_engine_kwargs` (or the right builder) → its `_*_OVERRIDES_ALLOWED` set. A knob not on an allowlist raises `KeyError`.
5. **No look-ahead.** Detector verdicts (`inverted_bar`, `superseded_bar`, `played_out_bar`, `expired_bar`) cover the whole file; only read one at bar `i` as `0 <= verdict_bar <= i`.
6. **Windows over 31 days** need `--i-know`. State window, cells and cost before launching one. Never run two long runs at once (memory).
7. **Imports must be cheap.** Work goes under `if __name__ == "__main__":`.
8. **Every sweep is recorded** in `results/` by the driver. Check `python -m runtime.results.cli list` before re-measuring.

## Canonical recipe (summary — the file is the truth)

| area | values |
|---|---|
| entry | `entry_mode=dual`, `entry_fill_model=touch`, `fill_model=pessimistic` |
| stop | `sl_mode=hvn_lvn`, `sl_nth_hvn_n=1`, `sl_min_usd=0.94`, `sl_max_usd=15.0`, `sl_zone_mult=40` |
| target | `tp_mode=hvn_single`, `nth_hvn_n=3`, `tp_zone_mult_fallback=200`, `swing_cap_pct=3.0`, rolling p95 cap on, `max_hold_secs=0` (lifespan off; contra-close keeps open trades low) |
| trail | shorts: `trail_mode=combined`, k=2, activation=1×ATR1m, giveback=0.5 · longs: `trail_long_mode=giveback`, on at $10 profit, gives back 25% · `trail_sides=both` |
| gates | `vol_state_min_range_norm=1.0`, `vol_state_min_expand_prev_day=1.0`, 63-key `structure_seq_whitelist` (refit on causal breaks, full corpus, in-sample) |
| book | `trade_sides=both`, `conflict_policy=stack`, `max_same_dir_open=20`, `contra_close=True`, `contra_close_distance_usd=5.0` |
| detector | `fvg_min_zone_usd=0.30`, 60s resample, `structure_pivot_len=9`, `vp_rows=200` |

Baseline, full corpus 2024-01-01 → 2026-07-16 (look-ahead-free engine, 2026-11-06): 20,546 trades, EV $0.783, net $16,091.98, max DD $984, Sharpe 2.97 (daily MTM). Contra-close legs: 3,013. Open-trade counts not re-measured. Holdout (whitelist fit on 2024 only, scored 2025-01 → 2026-07): net $4,513 vs $4,060 with no whitelist and $9,530 for the in-sample list.

## Metrics and ranking

* Each sweep cell records `sharpe_mtm` (daily mark-to-market equity changes, open positions marked at each UTC day's last close), `sharpe_daily` (realised P&L by exit day) and both daily series. Both are annualised ×√252.
* `python -m runtime.sweeps.rank [--since TS] [--sort mtm|daily|dsr]` ranks full-corpus cells and adds the deflated Sharpe (a probability, computed on the MTM series, with trials = cells ranked).
* Report every cell with trades, EV, net, Δ net, max DD and Sharpe.

## Contra-close

Fires when the size-weighted average short entry minus the size-weighted average long entry is at least `contra_close_distance_usd`. It closes min(long size, short size) at the bar close, pro-rata across EVERY open trade: the smaller side fully, each trade on the larger side by the same fraction (the rest stays open). Individual trade P&L is not checked; losers are netted too.

## Known book effect

With a wide swing cap the target is rarely hit and contra-close netting dominates (cap 3.0, trail off: contra-close legs made $18,146 of $15,839 net). Treat net gains that come mostly from `contra_close` as book composition, not entry skill.

## Removed SL/TP knobs

`sl_buffer_usd`, `poc_buffer_usd`, `tp_hvn_proxy_frac`, `tp_p95_mult`, `sl_scale_min/max`, `tp_scale_min/max`, and the config constants `PARTIAL_CLOSE_PCT`, `TP2_*`. They had no effect on the canonical recipe; the canonical result is unchanged without them.

Mostly inactive, still live: `tp_zone_mult_fallback` (only bites well below 200), `sl_min_usd` (only above ~$1).
