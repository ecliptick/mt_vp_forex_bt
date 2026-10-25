"""MECHANISM + BEAR-MARKET TEST for the OLD repo.

1. MECHANISM: the `inv` (inversion soft-stop) exit uses the GLOBAL
   `zone.inverted` flag, computed by a forward walk over the WHOLE file.
   If the zone inverts AFTER the trade exits, the soft-stop was armed
   from future information. Measure it.
2. BEAR MARKET: run the OLD repo on a quarter where gold FELL. A pure
   drift bet must lose there.
"""
import json, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(r"C:\coding\ict_forex_research")))
from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest

HERE = Path(__file__).parent
REP = HERE / "_reports"

# ── 1. MECHANISM from the saved FULL run ───────────────────────────────
print("=" * 78)
print("1. MECHANISM — the `inv` soft-stop and the GLOBAL inverted flag")
print("=" * 78)
tc = json.loads((REP / "old_truncation_probe.json").read_text(encoding="utf-8"))
full = tc["full"]
trunc = tc["trunc"]
print("  full run: %d trades | truncated run: %d trades" % (len(full), len(trunc)))

def inv_exit_rows(recs):
    out = []
    for r in recs:
        if r["exit_reason"] != "inv":
            continue
        ib = r["inverted_bar"]
        if ib < 0:
            continue
        out.append((ib - r["exit_bar"], ib - r["entry_bar"], r["pnl"]))
    return out

rows = inv_exit_rows(full)
print("\n  `inv`-exit trades with a known inverted_bar: n=%d" % len(rows))
if rows:
    a = np.array(rows)
    fut = a[:, 0] > 0
    print("  inverted_bar - exit_bar  : min %d median %d max %d"
          % (a[:, 0].min(), np.median(a[:, 0]), a[:, 0].max()))
    print("  inversion happened AFTER the trade exited (FUTURE INFO): %d / %d = %.1f%%"
          % (fut.sum(), len(a), 100.0 * fut.sum() / len(a)))
    if fut.sum():
        print("    those legs' P&L = $%.2f" % a[fut, 2].sum())
    print("  inverted_bar - entry_bar : min %d median %d max %d"
          % (a[:, 1].min(), np.median(a[:, 1]), a[:, 1].max()))

# ── 2. WHICH zones cause the truncation divergence ────────────────────
print("\n" + "=" * 78)
print("2. TRUNCATION DIVERGENCE — what changes?")
print("=" * 78)
fk = {(r["entry_bar"], r["direction"], round(r["entry_price"], 4)) for r in full}
tk = {(r["entry_bar"], r["direction"], round(r["entry_price"], 4)) for r in trunc}
print("  trades only in TRUNCATED (the full run DROPPED these): %d" % len(tk - fk))
print("  trades only in FULL  (the truncated run DROPPED these): %d" % len(fk - tk))
extra = [r for r in trunc
         if (r["entry_bar"], r["direction"], round(r["entry_price"], 4)) in (tk - fk)]
if extra:
    print("  their gross = $%.2f   (of truncated total $%.2f)"
          % (sum(r["pnl"] for r in extra),
             sum(r["pnl"] for r in trunc)))
    print("  their exit reasons:",
          {k: sum(1 for r in extra if r["exit_reason"] == k)
           for k in {r["exit_reason"] for r in extra}})

# ── 3. BEAR-MARKET TEST ───────────────────────────────────────────────
print("\n" + "=" * 78)
print("3. BEAR-MARKET TEST — run the OLD repo on a falling quarter")
print("=" * 78)
src = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_1y.parquet")
df = pd.read_parquet(src)
ts = pd.to_datetime(df["time"], utc=True)
close = df["close"].to_numpy(float)
yr = ts.dt.year.to_numpy(); mo = ts.dt.month.to_numpy()

qmap = {1: "Q1", 2: "Q1", 3: "Q1", 4: "Q2", 5: "Q2", 6: "Q2",
        7: "Q3", 8: "Q3", 9: "Q3", 10: "Q4", 11: "Q4", 12: "Q4"}
q = np.array([qmap[m] for m in mo]) + "-" + yr.astype(str)
uq = sorted(set(q))
print("  quarters and gold's move:")
bear_q = None
for qq in uq:
    idx = np.where(q == qq)[0]
    if len(idx) < 100000:
        continue
    move = close[idx[-1]] - close[idx[0]]
    tag = "BEAR" if move < 0 else "bull"
    print("    %-8s bars %8d  $%7.2f -> $%7.2f   %+7.2f  %s"
          % (qq, len(idx), close[idx[0]], close[idx[-1]], move, tag))
    if move < 0 and (bear_q is None or move < bear_q[1]):
        bear_q = (qq, move, idx)
if bear_q is None:
    print("  NO BEAR QUARTER FOUND")
    sys.exit(0)

qq, move, idx = bear_q
print("\n  >>> running the OLD repo on the worst quarter: %s (gold %+.2f)" % (qq, move))
sub = df.iloc[idx[0]: idx[-1] + 1].copy()
del df
p = optimal_params(mode="sniper")
t0 = time.time()
res = run_ict_backtest(sub, p, strategy_label="bear_q")
gross = sum(t.pnl_usd for t in res.trades)
fee = sum(t.fee_usd for t in res.trades)
print("  %s: %.1fs  trades=%d gross=$%.2f fee=$%.2f NET=$%.2f"
      % (qq, time.time() - t0, len(res.trades), gross, fee, gross - fee))
print("  buy-and-hold the same quarter (1 oz): $%+.2f" % move)

sub_close = sub["close"].to_numpy(float)
recs = []
for t in res.trades:
    qty = t.lots * p.contract_size
    dr = (sub_close[t.exit_bar] - sub_close[t.entry_bar]) * t.direction * qty
    recs.append(dict(direction=int(t.direction), pnl=float(t.pnl_usd),
                     drift=float(dr), exit_reason=str(t.exit_reason)))
g = np.array([r["pnl"] for r in recs])
dr = np.array([r["drift"] for r in recs])
if len(g):
    print("  drift = $%.2f   excess = $%.2f" % (dr.sum(), (g - dr).sum()))
    for side, lab in ((1, "LONG"), (-1, "SHORT")):
        m = np.array([r["direction"] == side for r in recs])
        if m.sum():
            print("    %-5s n=%3d gross $%8.2f  drift $%8.2f"
                  % (lab, m.sum(), g[m].sum(), dr[m].sum()))
with open(REP / "old_bear_quarter.json", "w", encoding="utf-8") as f:
    json.dump(dict(quarter=qq, b_and_h=float(move), trades=recs,
                   gross=float(gross), fee=float(fee)), f, indent=1)
