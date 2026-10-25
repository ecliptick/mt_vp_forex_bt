"""DECOMPOSITION: where does the OLD repo's +$944.70 actually come from?

Measures, on the OLD repo's own trades:
  1. DRIFT vs EXCESS (the same identity our repo uses) — is the gross
     market drift or selection?
  2. CONCENTRATION — what share of gross comes from the top-N trades?
  3. MONTHLY — is the edge per-month or one month?
  4. HOLD TIME — how long is capital locked up?
  5. LIVE-COST RESTATEMENT — what is the net with the LIVE spread
     ($0.40/oz, not $0.19) and with 1-tick slippage on each side?
"""
import sys, time, json
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(r"C:\coding\ict_forex_research")))

from src.core.optimal_config import optimal_params
from src.backtest.ict_backtest import run_ict_backtest

SRC = Path(r"C:\coding\ict_tier_v2\data\XAUUSD_S1_2025Q1.parquet")
OUT = Path(__file__).parent / "_reports"
OUT.mkdir(parents=True, exist_ok=True)

df = pd.read_parquet(SRC)
ts = pd.to_datetime(df["time"], utc=True).astype("int64").to_numpy()
close = df["close"].to_numpy(float)

p = optimal_params(mode="sniper")

t0 = time.time()
res = run_ict_backtest(df, p, strategy_label="sniper_v22")
print("backtest %.1fs" % (time.time() - t0))
trades = res.trades
gross = sum(t.pnl_usd for t in trades)
fee = sum(t.fee_usd for t in trades)
print("trades=%d gross=$%.2f fee=$%.2f NET=$%.2f" % (len(trades), gross, fee, gross - fee))

recs = []
for t in trades:
    qty = t.lots * p.contract_size          # oz
    drift = (close[t.exit_bar] - close[t.entry_bar]) * t.direction * qty
    recs.append(dict(
        entry_bar=int(t.entry_bar), exit_bar=int(t.exit_bar),
        entry_ts=int(t.entry_time), exit_ts=int(t.exit_time),
        direction=int(t.direction), entry_price=float(t.entry_price),
        exit_price=float(t.exit_price), qty=float(qty),
        pnl=float(t.pnl_usd), fee=float(t.fee_usd), gross=float(t.pnl_usd),
        drift=float(drift), hold_s=float(t.hold_secs),
        exit_reason=str(t.exit_reason),
    ))

n = len(recs)
g = np.array([r["gross"] for r in recs])
d = np.array([r["drift"] for r in recs])
e = g - d

def tstat(x):
    x = np.asarray(x, float)
    if len(x) < 2:
        return float("nan")
    return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x))))

print("\n" + "=" * 70)
print("DRIFT / EXCESS  (the same identity our repo uses)")
print("=" * 70)
print("  gross  = $%9.2f   (mean/leg $%.4f)  t=%+.2f" % (g.sum(), g.mean(), tstat(g)))
print("  drift  = $%9.2f   (mean/leg $%.4f)  t=%+.2f" % (d.sum(), d.mean(), tstat(d)))
print("  excess = $%9.2f   (mean/leg $%.4f)  t=%+.2f" % (e.sum(), e.mean(), tstat(e)))
share = 100.0 * d.sum() / g.sum() if g.sum() else float("nan")
print("  >>> DRIFT IS %.1f%% OF GROSS" % share)
for side, lab in ((1, "LONG"), (-1, "SHORT")):
    m = np.array([r["direction"] == side for r in recs])
    if m.sum() == 0:
        continue
    print("  %-5s n=%3d gross $%8.2f  drift $%8.2f (%5.1f%%)  excess $%8.2f  t=%+.2f"
          % (lab, m.sum(), g[m].sum(), d[m].sum(),
             100.0 * d[m].sum() / g[m].sum() if g[m].sum() else 0,
             e[m].sum(), tstat(e[m])))

print("\n" + "=" * 70)
print("CONCENTRATION")
print("=" * 70)
order = np.argsort(-g)
for k in (1, 3, 5, 10, 20):
    if k <= n:
        print("  top %2d trades = $%8.2f of $%.2f  (%.1f%% of gross, %.1f%% of net)"
              % (k, g[order[:k]].sum(), g.sum(),
                 100.0 * g[order[:k]].sum() / g.sum(),
                 100.0 * g[order[:k]].sum() / (gross - fee)))
print("  wins: %d  losses: %d   avg win $%.2f  avg loss $%.2f"
      % ((g > 0).sum(), (g <= 0).sum(),
         g[g > 0].mean() if (g > 0).any() else 0,
         -g[g <= 0].mean() if (g <= 0).any() else 0))

print("\n" + "=" * 70)
print("MONTHLY (2025Q1 = Jan/Feb/Mar)")
print("=" * 70)
months = {}
for r in recs:
    m = pd.Timestamp(r["entry_ts"], unit="ns", tz="UTC").strftime("%Y-%m")
    months.setdefault(m, []).append(r)
for m in sorted(months):
    rs = months[m]
    gg = sum(x["gross"] for x in rs); ff = sum(x["fee"] for x in rs)
    dd = sum(x["drift"] for x in rs)
    print("  %s  n=%3d  gross $%8.2f  fee $%6.2f  NET $%8.2f   drift $%8.2f"
          % (m, len(rs), gg, ff, gg - ff, dd))

print("\n" + "=" * 70)
print("HOLD TIME")
print("=" * 70)
hold = np.array([r["hold_s"] for r in recs])
for q in (10, 25, 50, 75, 90, 99):
    print("  p%-3d %8.0f s = %6.2f h = %5.2f d" % (q, np.percentile(hold, q),
          np.percentile(hold, q) / 3600, np.percentile(hold, q) / 86400))
wins_hold = hold[g > 0]
if len(wins_hold):
    print("  winners only: p50 %.1f h   p90 %.1f h   max %.1f d"
          % (np.percentile(wins_hold, 50) / 3600,
             np.percentile(wins_hold, 90) / 3600,
             wins_hold.max() / 86400))

print("\n" + "=" * 70)
print("LIVE-COST RESTATEMENT")
print("=" * 70)
fee_model = sum(r["fee"] for r in recs)
for spread in (0.19, 0.40):
    fee_live = sum(r["qty"] * spread + 0.01 * 7.0 for r in recs)
    print("  spread $%.2f/oz : fee $%.2f  NET $%.2f" % (spread, fee_live, gross - fee_live))
for slip in (0.0, 0.05, 0.10, 0.20):
    # 1 tick adverse on entry AND exit, both sides -> 2*slip per oz traded
    drag = sum(2 * slip * r["qty"] for r in recs)
    fee_live = sum(r["qty"] * 0.40 + 0.01 * 7.0 for r in recs)
    print("  live spread + %.2f/oz slippage : fee $%.2f + slip $%.2f  NET $%.2f"
          % (slip, fee_live, drag, gross - fee_live - drag))

with open(OUT / "old_repo_decomposition.json", "w", encoding="utf-8") as f:
    json.dump(
        dict(
            trades=recs,
            summary=dict(
                gross=float(gross), fee=float(fee_model), net=float(gross - fee_model),
                drift=float(d.sum()), excess=float(e.sum()),
            ),
        ),
        f, indent=1,
    )
print("\nsaved ->", OUT / "old_repo_decomposition.json")
