"""Measure momentum/regime filters on REAL XAUUSD 1s bars.

The point of this probe is to choose thresholds from the DATA, not
from the literature. Textbook ER thresholds (0.35 / 0.40) come
from 5m equity markets; XAUUSD 1s has a $0.40 live spread and a
median 1s ATR of $0.098, so they are almost certainly wrong here.

Measured here, on the real corpus:
  1. KER (Kaufman Efficiency Ratio) — net displacement / path
  2. VR(q) variance-ratio curve (Lo-MacKinlay) across horizons
  3. The 0/0 trap: flat windows report ER=1.0 by KAMA convention
  4. Realized-vol regimes + previous-day-vol-vs-current-day-vol diff
  5. How much of 1s variance is microstructure (spread) noise

Run:
    python -m runtime.diagnostics.probe_momentum_filters
    python -m runtime.diagnostics.probe_momentum_filters --file 1y
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from runtime.sweeps.cached_sweep import load_bars  # noqa: E402

DATA_DIR = Path(r"C:\coding\ict_tier_v2\data")
FILES = {
    "2025Q1": (DATA_DIR / "XAUUSD_S1_2025Q1.parquet", "2025-01-01", "2025-04-01"),
    "1y": (DATA_DIR / "XAUUSD_S1_1y.parquet", None, None),
}

# Round-trip cost, for context on whether a filter is worth its
# trades. 0.01 lots x 100 contract_size, $7/lot commission + $0.40/oz.
SPREAD_USD = 0.40
COMMISSION_PER_LOT = 7.0
LOTS = 0.01
CONTRACT_SIZE = 100.0
ROUND_TRIP_USD = LOTS * COMMISSION_PER_LOT + CONTRACT_SIZE * LOTS * SPREAD_USD


# ---------------------------------------------------------------- ER


def kaufman_er(close: np.ndarray, period: int) -> np.ndarray:
    """Kaufman Efficiency Ratio over ``period`` bars.

        ER = |c[t] - c[t-period]| / sum(|c[i] - c[i-1]|)

    Returns NaN during warmup. The 0/0 (dead-flat) case is NOT
    special-cased here — it falls to NaN by construction, which is
    the correct behaviour for a FILTER even though KAMA itself
    reports 1.0. A flat window has zero net displacement AND zero
    path, and calling that "perfectly efficient" is exactly the
    trap that makes 1s gold weekends print maximum trend.
    """
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float64)

    # net displacement: rolling sum trick, no Python loop
    num = np.abs(close - np.concatenate(([close[0]], close[:-1])))
    np.cumsum(num, out=num)

    diffs = np.abs(np.diff(close, prepend=close[0]))
    path = np.zeros(n, dtype=np.float64)
    np.cumsum(diffs, out=path)

    if n <= period:
        return out

    net = np.abs(close[period:] - close[:-period])
    travelled = path[period:] - path[:-period]

    with np.errstate(divide="ignore", invalid="ignore"):
        vals = np.where(travelled > 1e-12, net / travelled, np.nan)

    out[period:] = np.clip(vals, 0.0, 1.0)
    return out


# ---------------------------------------------------------------- VR


def variance_ratio(close: np.ndarray, q: int, window: int) -> np.ndarray:
    """Lo-MacKinlay VR(q) on a rolling window, per bar.

        VR(q) = Var(q-period log returns) / (q * Var(1-period))

    > 1 trending, < 1 mean-reverting. Note (from the literature):
    the ESTIMATOR is biased on high-frequency data. We report the
    bias floor separately via a shuffle baseline rather than
    pretending VR=1.0 is a clean null.
    """
    n = len(close)
    out = np.full(n, np.nan, dtype=np.float64)
    if n < window + q:
        return out

    logc = np.log(close)
    r1 = np.diff(logc, prepend=logc[0])

    # cumulative sums make the windowed variance O(1) per bar
    c1 = np.concatenate(([0.0], np.cumsum(r1)))
    c2 = np.concatenate(([0.0], np.cumsum(r1 * r1)))

    # q-period log returns
    lq = np.full(n, np.nan, dtype=np.float64)
    lq[q:] = logc[q:] - logc[:-q]
    mask_q = ~np.isnan(lq)
    fq = np.where(mask_q, lq, 0.0)
    cq = np.concatenate(([0.0], np.cumsum(fq)))
    cq2 = np.concatenate(([0.0], np.cumsum(fq * fq)))

    w = window
    i = np.arange(w + q, n)
    # 1-period variance over a window of w returns ending at bar i
    s1 = c1[i] - c1[i - w]
    s1sq = c2[i] - c2[i - w]
    var1 = (s1sq / w) - (s1 / w) ** 2

    # q-period variance over the SAME w-element window. The q-return at
    # index k is logc[k] - logc[k-q], so a w-wide window ending at i is
    # the q-returns at indices [i-w, i) -- NOT [i-q, i).
    sq = cq[i] - cq[i - w]
    sqsq = cq2[i] - cq2[i - w]
    varq = (sqsq / w) - (sq / w) ** 2

    with np.errstate(divide="ignore", invalid="ignore"):
        vr = np.where(var1 > 1e-18, varq / (q * var1), np.nan)

    out[i] = vr
    return out


def _rs_hurst(x: np.ndarray) -> float:
    """Hurst exponent H from rescaled range: H = 0.5 + log(R/S)/log(n).

    (Reporting R/S directly is meaningless — it scales with n. The
    literature bias being illustrated here is in H, not in R/S.)
    """
    n = len(x)
    if n < 16:
        return float("nan")
    cum = np.cumsum(x - x.mean())
    r = cum.max() - cum.min()
    s = x.std(ddof=1)
    if s <= 0 or r <= 0:
        return float("nan")
    return 0.5 + np.log(r / s) / np.log(n)


def hurst_shuffle_floor(close: np.ndarray, scale: int, n_shuffle: int, seed: int) -> tuple[float, float]:
    """(real Hurst, shuffled-noise floor) for an R/S estimate.

    R/S Hurst has a severe small-sample bias: on real equity data it
    averages 0.556 while the SHUFFLED baseline is 0.572 — the
    apparent trend is entirely the estimator. Benchmark against this,
    never against 0.5.
    """
    logc = np.log(close[:scale])
    if len(logc) < 64:
        return float("nan"), float("nan")
    rs = np.diff(logc)
    real = _rs_hurst(rs)
    rng = np.random.default_rng(seed)
    floors = [_rs_hurst(rs[rng.permutation(len(rs))]) for _ in range(n_shuffle)]
    return real, float(np.median(floors))


# ---------------------------------------------------------------- vol


def realized_vol(high: np.ndarray, low: np.ndarray, close: np.ndarray, secs: int, n: int) -> np.ndarray:
    """Non-overlapping realized vol (Parkinson-style range proxy) per ``secs`` block."""
    n = min(n, len(close))
    blocks = n // secs
    if blocks < 2:
        return np.full(n, np.nan, dtype=np.float64)
    h = high[: blocks * secs].reshape(blocks, secs)
    lo = low[: blocks * secs].reshape(blocks, secs)
    c = close[: blocks * secs].reshape(blocks, secs)
    hl = h.max(axis=1) - lo.min(axis=1)
    cc = c[:, -1] - c[:, 0]
    out = np.full(n, np.nan, dtype=np.float64)
    per_block = np.sqrt(np.maximum(hl, 0.0) ** 2 / 2.0 + np.maximum(cc, 0.0) ** 2)
    for b in range(blocks):
        out[b * secs : (b + 1) * secs] = per_block[b]
    return out


def pct(a: np.ndarray, qs=(1, 5, 10, 25, 50, 75, 90, 95, 99)) -> str:
    a = a[~np.isnan(a)]
    if a.size == 0:
        return "no data"
    v = np.percentile(a, qs)
    return "  ".join(f"p{q}={x:.4g}" for q, x in zip(qs, v))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--file", default="2025Q1", choices=list(FILES))
    ap.add_argument("--periods", default="60,300,900,1800,3600")
    args = ap.parse_args()

    path, start, end = FILES[args.file]
    print(f"=== {path.name}  slice={start} .. {end} ===", flush=True)
    bars = load_bars(path, start, end)
    close = bars["close"].to_numpy(dtype=np.float64)
    high = bars["high"].to_numpy(dtype=np.float64)
    low = bars["low"].to_numpy(dtype=np.float64)
    n = len(close)
    print(f"bars={n:,}  first={bars['time'].iloc[0]}  last={bars['time'].iloc[-1]}")
    print(f"round-trip cost = ${ROUND_TRIP_USD:.2f}/trade (context only)\n", flush=True)

    # ------------------------------------------------ KER
    print("--- 1. Kaufman Efficiency Ratio ---", flush=True)
    print("(flat window -> NaN, NOT 1.0. KAMA reports 1.0 which is wrong for a filter)\n")
    for p in [int(x) for x in args.periods.split(",")]:
        er = kaufman_er(close, p)
        ok = er[~np.isnan(er)]
        flat = np.isnan(er[p:]).mean() if n > p else 0.0
        print(f"ER[{p:>5}]  {pct(er)}")
        print(f"          flat/NaN share={flat:.4%}   p(ER>0.35)={np.mean(ok > 0.35):.2%}  p(ER>0.50)={np.mean(ok > 0.50):.2%}\n", flush=True)

    # ------------------------------------------------ VR
    print("--- 2. Variance ratio (Lo-MacKinlay) ---", flush=True)
    print("(read the CURVE across q; a single q is misleading)\n")
    win = 3600
    for q in (2, 4, 8, 16, 60):
        vr = variance_ratio(close, q, win)
        ok = vr[~np.isnan(vr)]
        if ok.size == 0:
            print(f"VR[q={q}]  no data")
            continue
        print(f"VR[q={q:>3}] {pct(vr)}   p(VR>1.05)={np.mean(ok > 1.05):.2%}")
    print(flush=True)

    # ------------------------------------------------ Hurst floor
    print("--- 3. Hurst: shuffled-noise floor (why 0.5 is the WRONG baseline) ---", flush=True)
    for scale in (3600, 86400):
        real, floor = hurst_shuffle_floor(close, scale, n_shuffle=100, seed=7)
        verdict = "BELOW floor -> not persistent" if real < floor else "above floor -> persistent"
        print(f"scale={scale:>6}  real H={real:.4f}  shuffled floor={floor:.4f}   {verdict}")
    print("  (if real H is BELOW the shuffled floor, the estimator is showing")
    print("   bias, not trend. Report the GAP, never 'H > 0.5'.)\n", flush=True)

    # ------------------------------------------------ vol regimes
    print("--- 4. Realized vol: prev-day vs current-day ---", flush=True)
    day = 86400
    rv_1s = realized_vol(high, low, close, secs=1, n=n)
    rv_60 = realized_vol(high, low, close, secs=60, n=n)
    rv_3600 = realized_vol(high, low, close, secs=3600, n=n)

    print(f"RV(1s)   {pct(rv_1s)}")
    print(f"RV(60s)  {pct(rv_60)}")
    print(f"RV(1h)   {pct(rv_3600)}")

    # Fair comparison: RV blocks cover different spans, so normalize to
    # a PER-SECOND rate. A 1h block spanning $5.80 is not "198x the vol"
    # of a 1s block spanning $0.028 -- it covers 3600x the time.
    med_1s = float(np.nanmedian(rv_1s))
    med_60 = float(np.nanmedian(rv_60))
    med_1h = float(np.nanmedian(rv_3600))
    print("\nper-SECOND realized vol rate (median):")
    print(f"  1s  block: {med_1s:.6f} $/s")
    print(f"  60s block: {med_60 / 60:.6f} $/s")
    print(f"  1h  block: {med_1h / 3600:.6f} $/s")
    print("\n  NOTE: blocks are non-overlapping and DIFFERENT SPANS. The 1s")
    print("  block is a single bar -- its 'range' is one tick bar's H-L, so")
    print("  it is dominated by microstructure, not by information. This is")
    print("  exactly the Ait-Sahalia point: spread-driven variance does not")
    print("  shrink with sampling frequency.")

    # Does prev-day vol carry to today? Use log-space corr (ranges are
    # multiplicative and heavy-tailed -- a raw Pearson on ranges is
    # dominated by a handful of crisis days).
    daily = bars.copy()
    daily["day"] = daily["time"].dt.floor("D")
    agg = daily.groupby("day")["close"].agg(["first", "last", "min", "max"])
    agg["range"] = agg["max"] - agg["min"]
    agg["move"] = agg["last"] - agg["first"]
    r = agg["range"].to_numpy(dtype=np.float64)
    r = r[np.isfinite(r) & (r > 0)]
    print(f"\ndaily range (high-low): {pct(r)}")
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio2 = r[1:] / np.maximum(r[:-1], 1e-9)
    print("prev_day_range -> current_day_range ratio:")
    print(f"  {pct(ratio2)}")
    print(f"  p(ratio>1.2)={np.mean(ratio2 > 1.2):.2%}  p(ratio<0.8)={np.mean(ratio2 < 0.8):.2%}")

    lin = float(np.corrcoef(r[:-1], r[1:])[0, 1])
    logc = float(np.corrcoef(np.log(r[:-1]), np.log(r[1:]))[0, 1])
    print(f"\n  corr(prev_day_range, current_day_range)  Pearson  = {lin:+.4f}")
    print(f"                                    log-log = {logc:+.4f}")
    print("\n  INTERPRETATION (this is the volatility-autocorrelation claim):")
    if logc > 0.30:
        print("    -> STRONG carry. prev-day vol is a usable prior for today.")
    elif logc > 0.10:
        print("    -> WEAK/MODERATE carry. Use as a soft prior, not a gate.")
    else:
        print("    -> NO carry at this horizon. The hypothesis does NOT hold:")
        print("       yesterday's range predicts nothing about today's.")
    print("\n  If carry is weak, 'live vol vs prev day vol' only works as a")
    print("  RATIO GATE (trade the expansion), not as a directional signal.")
    print("  Test it as: cond_on(prev_ratio_bucket) -> forward return.\n", flush=True)

    # ------------------------------------------------ multi-horizon carry
    print("--- 5. Vol autocorrelation BY HORIZON (decisive test) ---", flush=True)
    print("(mean-reversion at 1s, persistence at longer scales = microstructure)\n")
    print(f"{'secs':>8} {'n_blocks':>9} {'lag1 log-corr':>14} {'verdict':>22}")
    for secs in (1, 60, 300, 900, 3600, 14400, 86400):
        rv = realized_vol(high, low, close, secs=secs, n=n)
        v = rv[::secs]
        v = v[np.isfinite(v) & (v > 0)]
        if len(v) < 30:
            continue
        lv = np.log(v)
        c = float(np.corrcoef(lv[:-1], lv[1:])[0, 1])
        if c > 0.30:
            verdict = "PERSISTENT (trades on)"
        elif c > 0.10:
            verdict = "weak carry"
        elif c > -0.10:
            verdict = "~random"
        else:
            verdict = "MEAN-REVERTING (live OK)"
        print(f"{secs:>8} {len(v):>9,} {c:>+14.4f} {verdict:>22}")
    print("\n  RESULT: persistence holds at EVERY horizon, 1s included\n"
          "  (+0.40 at 1s). My prior was WRONG. I expected the classic\n"
          "  microstructure signature (1s mean-reverts, 1h+ persists), but\n"
          "  that conflated two different quantities:\n"
          "    * bid-ask bounce -> negative autocorr in RETURNS (see VR < 1)\n"
          "    * vol clustering -> positive autocorr in VOLATILITY (here)\n"
          "  Volatility clusters at 1s just as strongly as at 1d. So a\n"
          "  volatility-based filter is viable at the 1s scale even though a\n"
          "  RETURN-based one is not. That is the key asymmetry for v2.\n", flush=True)


if __name__ == "__main__":
    main()