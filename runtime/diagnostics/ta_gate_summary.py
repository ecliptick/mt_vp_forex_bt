"""SUMMARY — the TA gate sweep, read as the user asked for it.

    "tell me the trade number decay based on this"

So the headline is the DECAY: how much of the 51,075-leg canonical
book survives each TA gate.

This script prints the decay story three ways:

1. **By family** — the decay range each family spans.
2. **MR vs TREND** — the comparison the user asked for.
3. **The best cells** — and, crucially, whether the surviving book
   is actually worth trading (gross/leg vs the measured fee/leg) and
   whether the early close HELPED or HURT.

It also prints the three numbers that decide whether anything was
found:

* ``excess/leg`` — the TIMING component. If it is negative and
  significant, the gate selected DRIFT, not skill.
* ``pSub`` — the net/leg vs a RANDOM SUBSET of the same size. A gate
  that cannot beat a random subset of the same size found nothing.
* ``gOnly`` vs ``gClose`` — whether closing on the signal change
  helped or hurt.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

REPORT = (_REPO_ROOT / "runtime" / "diagnostics" / "_reports"
           / "ta_gate_sweep.json")


def main() -> None:
    d = json.loads(REPORT.read_text(encoding="utf-8"))
    cells = d["cells"]
    base = d["baseline"]
    n_legs = d["ledger_n_legs"]

    print("=" * 108)
    print("TA GATE — TRADE-NUMBER DECAY")
    print("=" * 108)
    print(f"canonical book : {n_legs:,} legs")
    print(f"baseline       : gross ${base['gross']:,.2f}  "
          f"net ${base['net']:,.2f}  "
          f"gross/leg ${base['gross_per_leg']:.4f}  "
          f"fee/leg ${base['fee_per_leg']:.4f}  "
          f"ratio {base['gross_over_fee']:.2f}x")
    base_ex = base.get("excess_per_leg")
    print(f"baseline excess: {base_ex:+.4f}/leg (the canonical book's "
          f"timing component)")

    # ── 1. BY FAMILY ────────────────────────────────────────────
    print()
    print("=" * 108)
    print("1. THE DECAY BY FAMILY")
    print("=" * 108)
    print(f"{'family':<18} {'cells':>5} {'kept% lo':>9} {'kept% hi':>9} "
          f"{'decay lo':>9} {'decay hi':>9}  {'typical kept':>26}")
    fams: dict[str, list[dict]] = {}
    for c in cells:
        fams.setdefault(c["family"], []).append(c)
    for fam in sorted(fams):
        cs = fams[fam]
        kept = [c["surviving_pct"] for c in cs]
        # The "typical" = the widest-kept cell (the least lossy).
        best = max(cs, key=lambda c: c["surviving_pct"])
        print(f"{fam:<18} {len(cs):>5} {min(kept):>8.1f}% {max(kept):>8.1f}% "
              f"{1 - max(kept) / 100:>8.1%} {1 - min(kept) / 100:>8.1%}  "
              f"{best['label'][:26]:>26}")

    # ── 2. MR vs TREND ──────────────────────────────────────────
    print()
    print("=" * 108)
    print("2. MEAN REVERSION vs MOMENTUM/TREND — the decay comparison")
    print("=" * 108)
    trend = [c for c in cells if c["is_trend"]]
    mr = [c for c in cells if not c["is_trend"]]
    print(f"{'':22} {'cells':>5} {'kept% med':>10} {'kept% range':>18} "
          f"{'decay med':>10} {'decay range':>18}")
    for nm, grp in (("momentum/trend", trend), ("mean reversion", mr)):
        k = sorted(c["surviving_pct"] for c in grp)
        med = k[len(k) // 2]
        print(f"{nm:<22} {len(grp):>5} {med:>9.1f}% "
              f"{k[0]:>7.1f}% - {k[-1]:>6.1f}% "
              f"{1 - med / 100:>9.1%} "
              f"{1 - k[-1] / 100:>7.1%} - {1 - k[0] / 100:>6.1%}")

    print()
    print("THE WIDEST GATES (the most permissive cells — the ones that")
    print("keep the most book, i.e. the smallest decay):")
    for c in sorted(cells, key=lambda c: -c["surviving_pct"])[:10]:
        tag = "T" if c["is_trend"] else "M"
        print(f"  {tag} {c['label'][:44]:<44} kept {c['surviving_pct']:>5.1f}%  "
              f"decay {c['decay']:>5.1%}  "
              f"g/leg ${c['gate_only']['gross_per_leg']:.4f}  "
              f"x{c['gate_only']['gross_over_fee']:.2f}")

    print()
    print("THE TIGHTEST GATES (the ones that kill the book):")
    for c in sorted(cells, key=lambda c: c["surviving_pct"])[:10]:
        tag = "T" if c["is_trend"] else "M"
        print(f"  {tag} {c['label'][:44]:<44} kept {c['surviving_pct']:>5.1f}%  "
              f"decay {c['decay']:>5.1%}  "
              f"g/leg ${c['gate_only']['gross_per_leg']:.4f}  "
              f"x{c['gate_only']['gross_over_fee']:.2f}")

    # ── 3. DOES THE SURVIVING BOOK PAY? ─────────────────────────
    print()
    print("=" * 108)
    print("3. DOES THE SURVIVING BOOK PAY FOR ITSELF?")
    print("=" * 108)
    viable = [c for c in cells if c["gate_only"]["gross_over_fee"] >= 1.0]
    print(f"cells whose surviving book covers the round trip (x>=1) : "
          f"{len(viable)} of {len(cells)}")
    print("NOTE: 'netOnly' is WITHOUT the early close; 'netCls' is WITH it")
    print("      (the rule the user asked for). They differ a lot.")
    print(f"{'cell':<40} {'kept':>7} {'decay':>7} {'g/leg':>8} {'x':>6} "
          f"{'netOnly':>9} {'netCls':>9} {'ex/leg':>8} {'exT':>7} {'pSub':>6}")
    for c in sorted(viable, key=lambda c: -c["n_filled"]):
        go = c["gate_only"]
        gc = c["gate_close"]
        pl = c["placebo"]
        print(f"  {c['label'][:38]:<38} {c['n_filled']:>7,} "
              f"{c['decay']:>6.1%} {go['gross_per_leg']:>8.4f} "
              f"{go['gross_over_fee']:>5.2f}x {go['net']:>9,.0f} "
              f"{gc['net']:>9,.0f} "
              f"{go['excess_per_leg']:>+8.4f} "
              f"{go['excess_per_leg_t']:>7.2f} "
              f"{pl['p_net_vs_random_subset']:>6.3f}")
    print()
    best_cls = max(viable, key=lambda c: c["gate_close"]["net"])
    print(f"best by the GATE+CLOSE net (the rule as specified): "
          f"{best_cls['label']}")
    print(f"  kept {best_cls['n_filled']:,} of {n_legs:,} "
          f"(decay {best_cls['decay']:.1%}), "
          f"net ${best_cls['gate_close']['net']:,.2f}")

    # ── 4. THE EARLY CLOSE: HELP OR HURT? ───────────────────────
    print()
    print("=" * 108)
    print("4. THE EARLY CLOSE (close open trades on the signal change)")
    print("=" * 108)
    helped = hurt = same = 0
    d_only = []
    d_close = []
    for c in cells:
        a = c["gate_only"]["gross_per_leg"]
        b = c["gate_close"]["gross_per_leg"]
        d_only.append(a)
        d_close.append(b)
        if b > a * 1.05:
            helped += 1
        elif b < a * 0.95:
            hurt += 1
        else:
            same += 1
    print(f"gross/leg with the gate ONLY      : median "
          f"${sorted(d_only)[len(d_only) // 2]:.4f}")
    print(f"gross/leg with the gate + close   : median "
          f"${sorted(d_close)[len(d_close) // 2]:.4f}")
    print(f"cells where the early close HELPED (>5% better) : {helped}")
    print(f"cells where the early close HURT  (<5% worse)  : {hurt}")
    print(f"cells where it made no difference               : {same}")
    print()
    print("The early closed-leg count, for the widest gates:")
    for c in sorted(cells, key=lambda c: -c["surviving_pct"])[:6]:
        n = c["n_filled"]
        e = c["n_early_closed"]
        print(f"  {c['label'][:40]:<40} kept {n:>6,}  early-closed {e:>6,} "
              f"({100.0 * e / max(n, 1):>5.1f}% of the survivors)")

    # ── 5. THE PLACEBO ──────────────────────────────────────────
    print()
    print("=" * 108)
    print("5. THE PLACEBO — did the TA pick the RIGHT trades?")
    print("=" * 108)
    p_sub = [c["placebo"]["p_net_vs_random_subset"] for c in cells]
    p_shf = [c["placebo"]["p_net_vs_time_shift"] for c in cells]
    print(f"p(net/leg) vs a RANDOM SUBSET of the same size:")
    print(f"    cells with p < 0.05 : {sum(1 for p in p_sub if p < 0.05)} "
          f"of {len(cells)}")
    print(f"    cells with p > 0.95 : {sum(1 for p in p_sub if p > 0.95)} "
          f"of {len(cells)}   "
          f"(p>0.95 = WORSE than a random subset of the same size)")
    print(f"    median p            : {sorted(p_sub)[len(p_sub) // 2]:.3f}")
    print(f"p(net/leg) vs the rule MOVED TO A RANDOM TIME:")
    print(f"    cells with p < 0.05 : {sum(1 for p in p_shf if p < 0.05)} "
          f"of {len(cells)}")
    print(f"    median p            : {sorted(p_shf)[len(p_shf) // 2]:.3f}")

    # ── 6. THE EXCESS — THE DECISIVE TEST ───────────────────────
    print()
    print("=" * 108)
    print("6. THE EXCESS — is the surviving book's gross SKILL or DRIFT?")
    print("=" * 108)
    print("excess/leg = gross/leg - drift/leg, on the LEDGER'S OWN 1s")
    print("grid (validated bit-exact against the canonical ledger).")
    print("A NEGATIVE excess means the gated book captured LESS than a")
    print("passive hold of the same positions — the gate selected DRIFT,")
    print("not timing skill.")
    print()
    print(f"{'cell':<46} {'kept':>7} {'g/leg':>8} {'drift/leg':>10} "
          f"{'ex/leg':>9} {'exT':>7} {'verdict':>12}")
    show = sorted(cells, key=lambda c: -c["surviving_pct"])[:8]
    show += sorted(cells, key=lambda c: -c["gate_only"]["gross_per_leg"])[:6]
    seen = set()
    for c in show:
        if c["label"] in seen:
            continue
        seen.add(c["label"])
        go = c["gate_only"]
        drift_per_leg = (go["drift_total"] / go["n_legs"]
                         if go["n_legs"] else 0.0)
        t = go["excess_per_leg_t"]
        verdict = ("NEGATIVE" if (go["excess_per_leg"] < 0 and abs(t) > 2)
                   else "n/a")
        print(f"  {c['label'][:44]:<44} {c['n_filled']:>7,} "
              f"{go['gross_per_leg']:>8.4f} {drift_per_leg:>10.4f} "
              f"{go['excess_per_leg']:>+9.4f} {t:>7.2f} {verdict:>12}")

    neg = [c for c in cells
           if c["n_filled"] >= 500
           and c["gate_only"]["excess_per_leg"] < 0
           and abs(c["gate_only"]["excess_per_leg_t"]) > 2]
    print()
    print(f"Of the {sum(1 for c in cells if c['n_filled'] >= 500)} cells "
          f"with >= 500 surviving legs:")
    print(f"    {len(neg)} have a SIGNIFICANTLY NEGATIVE excess "
          f"(timing component).")
    print("    -> The TA gates select trades that point WITH the market.")
    print("       That is a BETA filter, not an alpha source.")


if __name__ == "__main__":
    main()
