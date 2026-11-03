# Repo map — every file, and what it is for

> Relocated from `AGENTS.md` on 2026-11-03.
>
> This is a **map, not an inventory**. The evaluation harness is the top
> block — `leg_ledger` + `leg_buckets` + the placebos. Everything below it is
> per-feature history. A feature's diagnostic staying in the tree does **not**
> mean the feature is live; the canonical config table in `AGENTS.md` is the
> authority on that.

## Files

```
bt/
 __init__.py top-level imports of the engine + local modules
 _engine/ THE canonical engine source (standalone, self-contained)
  vp_multi_target_engine.py NB74/NB75/NB83 multi-target + D2 + D3
  vp_multi_target_config.py mode-name vocabulary (no knob VALUES)
  ict_signals.py FVG / iFVG detectors
  ict_strategy.py PendingSignal
  market_structure.py BoS / CHoCH / order blocks
  volume_profile.py daily VP rolling aggregator (NB80)
  vol_state.py NB85 vol-expansion gate (with look-ahead fix)
          + the 2026-11-03 day-shifted measure:
            vol_expand_prev_day (RV today vs RV yesterday,
            same elapsed fraction) — needs times_ns
  swing_cap.py NB72 daily-swing TP cap
  rolling_p95.py NB91 rolling p95 TP cap
  run_report.py run-log writer
  timebase.py NANOSECONDS conversion (single source)
 optimal_config.py THE canonical config + the 7 builders
 vp_engine.py Multi-target VP engine wrapper (XAUUSD sizing)
 risk.py Margin + cost + pnl math
 kelly.py Fractional Kelly sizing (NB81)
 leverage.py 100x leverage exposure math
 (no re-export wrappers — bt._engine.X is the surface)
results/
 index.json THE committed registry — one entry per run
 runs/<run_id>.json the full payload of one run
runtime/
 results/
  registry.py THE results registry (record / find / compare / history)
  cli.py the registry CLI
 sweeps/
 cached_sweep.py THE sweep driver (single + multi-knob grid)
 cost_estimate.py The PRE-FLIGHT cost estimate + the --i-know gate
diagnostics/
 ── THE EVALUATION HARNESS (use these) ───────────────────────────
 leg_ledger.py THE corpus-wide per-LEG record (continuous run)
 leg_buckets.py THE leg-level evaluation (side x outcome buckets)
 leg_random_entry.py RANDOM-ENTRY control (CONFOUNDED — see § 2b)
 zone_shuffle_placebo.py ZONE-SHUFFLE placebo — the decisive test
                          ⚠️ PROMPT-ONLY — never run unprompted (§ rule 5)
 volgate_trade_count.py THE TRADE-COUNT LADDER (30-70/day target)
 expand_prev_day_corpus_ab.py Corpus A/B for vol_state_min_expand_prev_day
                          (2026-11-04 — the promoting measurement)
 trail_sides_corpus_ab.py  Corpus A/B for the PER-SIDE trailing scope
                          (trail_sides) — the END-TO-END measurement that
                          AGENTS.md § 3.11 demands. Builds the artifacts
                          ONCE and runs the engine per arm.
 trail_mfe_mfa.py          The trail's MFE/MFA excursion analysis — the
                          oracle bound, the ATR cadence, the geometry
                          ladders. -> docs/trail_stop.md
 config_iso_2026_10_25.py Isolation A/B for the 2026-10-25 promotions
 chunk_vs_continuous.py Chunked vs continuous on the same window
 chunked_vs_continuous_months.py Per-month chunked vs continuous
 warmup_prefix_effect.py What the warmup prefix changes, per leg
 payoff_breakdown.py Payoff geometry
 validate_candidate.py Candidate validation harness
 ── THE TA EVALUATION HARNESS (the episode metric) ────────────────
 ta_signal_episodes.py The EPISODE metric (signal-to-signal) + its 2 placebos
 ta_1m_signal_sweep.py The TA scoreboard over all 64 TA cells
 ta_fixed_side_placebo.py THE FIXED-SIDE placebo — the decisive control
 ta_1m_core.py The TA signal builders (all 12 families) + the old traded metric
 ── THE TA GATE (post-hoc gate on the canonical book) ─────────────
 ta_gate.py THE GATE — gate open at the signal bar, close on signal change
  2360| ta_gate_sweep.py The TA gate sweep — 74 cells, 14 families, 2 placebos each
 ta_gate_validate.py PROVES the gate reproduces the canonical ledger BIT-EXACTLY
 ta_gate_summary.py The read-out: decay by family, MR vs trend, placebo, excess
 ta_gate_probe_index.py The probe that fixed the bar-index (1s grid) semantics
 ta_gate_patch_baseline.py Fills the baseline's drift/excess from the ledger
 ── DIAGNOSTICS (per-feature, mostly historical) ─────────────────
 directional_beta.py Is the P&L timing skill or market direction?
 short_side_ablation.py Is the short side a pure drag?
 side_gate_equivalence.py PROVES the engine side gate == post-hoc filter
 per_side_sequence_filter.py Per-side (side, last-event) buckets
 per_side_sequence_paired.py The paired delta (drop-bucket scoring)
 per_side_sequence_pooled.py The pooled control
 per_side_sequence_hypotheses.py Hypothesis table
 seq_signal_vs_fill_bar.py SIGNAL bar vs FILL bar (the trap)
 ── THE 5-EVENT SEQUENCE WHITELIST (promoted 2026-11-04) ──────
 seq5_table.py The per-signature table (post-hoc, a SCREEN)
 seq5_walkforward.py POST-HOC walk-forward (also a screen — no engine re-run)
 seq5_engine_walkforward.py THE ENGINE-LEVEL walk-forward — the verdict
                          (fits on one half, re-runs the ENGINE on the other)
 seq5_beta_regression.py Is the whitelisted book a DIRECTIONAL BET?
                         (DAILY gross ~ market return + per-leg excess)
 daily_direction_dependence.py THE daily direction-dependence test
                          (the sanctioned instrument — n≈577 days, corr t)
 rejected_knob_ledgers.py Builds the ledgers for the § 6 NOT-PROMOTED
                          knobs, so the daily test can re-ground them
 seq5_ksweep.py How many signatures keep the trade count in band?
                          (POST-HOC — a screen for picking K)
 seq5_ksweep_engine_check.py The K=48 band variant, ENGINE-level
                          — the verdict that the band is unreachable
 seq_whitelist_corpus_ab2.py The 7-arm end-to-end corpus A/B
 seq_whitelist_equivalence.py PROVES the engine whitelist == post-hoc filter
 contra_close_ab.py Hedge netting A/B
 contra_close_why.py WHY the netting helps (leg-level decomposition)
 contra_close_broad.py Multi-month broad sweep
 contra_close_firing.py Is the feature FIRING when it should?
 contra_close_actual_vs_simul.py Pair-level counterfactual
 contra_close_stats.py Sign test / Wilcoxon / bootstrap on the pairs
 contra_close_risk.py Risk view (maxDD, gross/leg vs fee/leg)
 contra_close_threshold_fine.py Fine threshold ladder for the netting
 hvn_lvn_proof.py Is the volume profile inert?
 zone_lifecycle.py Zone detection lifecycle
 zone_geometry.py Zone shape geometry
 zone_knob_sweep.py Zone detector knob sweep
 sl_ceiling_sweep.py / sl_ceiling_sweep8.py The SL ceiling
 sl_width_breakdown.py Where the stop width comes from
 sl_anchor_breakdown.py Which anchor set the stop
 capture_scaling.py The capture-scaling grid
 combined_screen.py The joint (gate x ceiling) screen
 vol_gate_broad.py / vol_gate_multislice.py The vol gate
 vol_entry_gate.py The vol gate, entry-side only
 cadence_sweep.py The detector cadence grid
 tp_reachability.py / tp_chain_trace.py / tp_metric_identity.py TP geometry
 probe_*.py One-off probes (knob liveness, VP scales, SL A/B, hold time)
 sequence_whitelist*.py DEAD — the signature whitelist
 sequence_flip.py DEAD — the sequence flip
 structure_sequences.py DEAD — structure sequence scoring
 audit_config_surface.py The config audit (duplicate knobs, dead knobs)
 audit_constant_reachability.py STRICT constant-reachability audit
 verify_diag_scale.py Proves the diagnostics run at canonical size
 _import_scan.py Per-module import scan (subprocess + timeout)
 _import_scan_shared.py Shared-process import scan (the pytest semantics)
 ── THE LOOK-AHEAD AUDIT (2026-11-03) ───────────────────────────────
 probe_fvg_lookahead_exclusion.py Is `fvg_retest_signals`'s
                          skip_inverted exclusion causal?  -> CLEAN
 probe_fvg_supersession_lookahead.py Is detect_fvg's deferred
                          supersession causal?  -> LEAK, but not in the
                          shipped path
 probe_dual_mode_lookahead.py The dual-mode inverted_bar skips
                          (detector-side counts + the lead distribution)
 probe_dual_mode_pnl_impact.py The two-arm P&L on ONE slice
 probe_dual_mode_multislice.py The two-arm book across SIX slices
 probe_dual_mode_postfix.py The same six slices AFTER the fix
 record_dual_mode_lookahead.py Writes both books to the results registry
```

> **The Files list is a MAP, not an inventory.** The evaluation
> harness is the top block — `leg_ledger` + `leg_buckets` + the
> placebos. Everything below it is per-feature history. A feature's
> diagnostic staying in the tree does **not** mean the feature is
> live; the canonical config table is the authority on that.

### Deleted in the 2026-10-22 dead-code sweep

Nothing may import these again; `tests/test_no_dead_imports.py`
enforces that.

| Deleted file | Why |
| --- | --- |
| `bt/params.py` | `FocusedParams` — a phantom "no-defaults dataclass" that was **never constructed in production**. |
| `bt/vp_config.py` | Duplicate of the engine-kwargs surface in `bt/optimal_config.py`. |
| `bt/structural_sl_tp.py` | Duplicate of the engine's own `compute_structural_sl` / `compute_structural_tp`. Its geometry had already **diverged** from the engine's. |
| `runtime/sweeps/single_sweep.py` | Reported empty metrics. |
| `runtime/sweeps/run_dual_sweep.py` | Hardcoded detector knobs that disagreed with the canonical recipe. |
| `runtime/sweeps/diag_swing_cap.py` | One-off diagnostic, superseded by the regression test. |
| `runtime/sweeps/diag_engine_memory.py` | One-off diagnostic, superseded by the memory note above. |

### Deleted in the 2026-11-03 streamline

| Deleted file | Why |
| --- | --- |
| `bt/rolling_p95.py` | Pure re-export wrapper over `bt/_engine/rolling_p95.py`. Zero production callers — the sweep driver and the tests import `bt._engine.*` directly. |
| `bt/swing_cap.py` | Same. |
| `bt/vol_state.py` | Same. |
| `bt/volume_profile.py` | Same. |
| `runtime/diagnostics/_probe_old_*.py` (5 files) | Reached into an external repo for the **deleted** bar engine (`run_ict_backtest`). This repo is self-contained — that dependency made it unrunnable on a fresh clone. |
| `runtime/diagnostics/_analyze_old_*.py` (2 files) | Analysed the output of the probes above. |

> **Rule: `bt/_engine/` is the engine surface.** There are no
> re-export wrappers under `bt/`. A wrapper that nothing imports is a
> second name for the same object, and a second place for a reader to
> look.

---
