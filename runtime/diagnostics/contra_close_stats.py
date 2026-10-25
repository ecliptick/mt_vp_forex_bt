"""CONTRA CLOSE — is the paired delta REAL? (2026-10-25)

THE PROBLEM WITH THE t-STAT
===========================

The pair-level measurement (34 overlapping long/short pairs across
15 months) gives:

    mean paired delta = +$3.97   (se $2.68)   t = +1.48   n = 34

That is **NOT significant** at the 5% level. Before concluding
"the feature does not work", the statistical method has to be
checked, because the paired-delta sample is:

* **small** (n=34) — the whole corpus only produces 34 overlapping
  pairs once the vol gate is on;
* **skewed** — the deltas are not symmetric. A handful of pairs
  carry large gains (the $78.44-spread pair on 2026-01 contributes
  $83.43 of the $135.11 total) while the rest cluster near zero;
* **non-normal** — the distribution has a long right tail.

For a small, skewed paired sample, the **Wilcoxon signed-rank
test** has materially better power than the t-test because it
works on the RANKS of the differences rather than their values. The
sign test is even more robust but throws away magnitude.

This diagnostic runs BOTH and reports the honest picture:

* Wilcoxon signed-rank (the primary test for paired data)
* the sign test (the most conservative)
* the t-test (for comparison)
* the **bootstrap** confidence interval on the mean (distribution-free)

It also answers the **critical follow-up**: is the result carried by
one pair? The bootstrap is re-run with the single best pair
excluded, which is the "what if this is one lucky trade?" test.

WHY THIS MATTERS FOR THE DECISION
==================================

The feature is a **risk-management** feature, not an alpha source.
Its job is to convert a probable stop-out into a certain small
gain. So even a modest, borderline-significant paired delta is
worth having IF it reduces the tail. The risk view
(:mod:`runtime.diagnostics.contra_close_risk`) measured that
separately: maxDD falls by $19-23 across the live threshold band.

The honest framing is: *"does the paired delta survive a test
appropriate to a small, skewed sample?"* — not *"is t > 2?"*.

Usage
-----
    python -m runtime.diagnostics.contra_close_stats
"""
from __future__ import annotations

import json
import math
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

import numpy as np  # noqa: E402

REPORT = Path("runtime/diagnostics/_reports/"
              "contra_close_actual_vs_simul.json")
OUT = Path("runtime/diagnostics/_reports/contra_close_stats.json")


def _wilcoxon_signed_rank(d: np.ndarray) -> dict:
    """Two-sided Wilcoxon signed-rank test with a normal approximation.

    Uses the tie-corrected variance. Exact p-values are not used
    because scipy may not be available and the normal approximation
    is adequate for n=34 with the tie correction applied.
    """
    nz = d[d != 0.0]
    n = int(nz.size)
    if n == 0:
        return {"n": 0, "w_plus": 0.0, "z": 0.0, "p_two_sided": 1.0}

    order = np.argsort(np.abs(nz), kind="stable")
    ranks = np.empty(n, dtype=np.float64)
    abs_vals = np.abs(nz)[order]

    # Average ranks within ties.
    i = 0
    r = 1
    while i < n:
        j = i
        while j + 1 < n and abs_vals[j + 1] == abs_vals[i]:
            j += 1
        avg = (r + (r + (j - i))) / 2.0
        ranks[order[i:j + 1]] = avg
        r += (j - i + 1)
        i = j + 1

    signs = np.sign(nz)
    w_plus = float(ranks[signs > 0].sum())
    w_minus = float(ranks[signs < 0].sum())

    # Tie-corrected variance of W+.
    _, counts = np.unique(abs_vals, return_counts=True)
    tie_term = float((counts ** 3 - counts).sum())
    n_eff = float(n)
    mu = n_eff * (n_eff + 1.0) / 4.0
    var = (n_eff * (n_eff + 1.0) * (2.0 * n_eff + 1.0) / 24.0
           - tie_term / 48.0)
    if var <= 0:
        return {"n": n, "w_plus": w_plus, "z": 0.0, "p_two_sided": 1.0}
    z = (w_plus - mu) / math.sqrt(var)
    # Two-sided normal tail.
    p = math.erfc(abs(z) / math.sqrt(2.0))
    return {
        "n": n, "w_plus": w_plus, "w_minus": w_minus,
        "mu": mu, "var": var, "z": z, "p_two_sided": p,
    }


def _sign_test(d: np.ndarray) -> dict:
    nz = d[d != 0.0]
    n = int(nz.size)
    if n == 0:
        return {"n": 0, "n_pos": 0, "p_two_sided": 1.0}
    n_pos = int((nz > 0).sum())
    # Exact two-sided binomial p under p=0.5.
    k = min(n_pos, n - n_pos)
    total = 0
    for i in range(k + 1):
        total += math.comb(n, i)
    p = min(1.0, 2.0 * total / (2.0 ** n))
    return {"n": n, "n_pos": n_pos, "n_neg": n - n_pos, "p_two_sided": p}


def _bootstrap_ci(d: np.ndarray, n_boot: int = 20000,
                  seed: int = 20261025) -> dict:
    """Distribution-free bootstrap CI on the mean paired delta."""
    n = int(d.size)
    if n == 0:
        return {"n": 0}
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, n, size=(n_boot, n))
    means = d[idx].mean(axis=1)
    lo, hi = np.percentile(means, [2.5, 97.5])
    return {
        "n": n,
        "mean": float(d.mean()),
        "ci95_lo": float(lo),
        "ci95_hi": float(hi),
        "p_mean_le_zero": float((means <= 0.0).mean()),
        "n_boot": n_boot,
    }


def _t_test(d: np.ndarray) -> dict:
    n = int(d.size)
    if n < 2:
        return {"n": n, "t": 0.0, "p_two_sided": 1.0}
    m = float(d.mean())
    se = float(d.std(ddof=1) / math.sqrt(n))
    if se <= 0:
        return {"n": n, "t": 0.0, "p_two_sided": 1.0}
    t = m / se
    # Two-sided t via the incomplete beta, computed numerically.
    df = n - 1
    x = df / (df + t * t)
    p = _betainc_regularised(df / 2.0, 0.5, x)
    return {"n": n, "mean": m, "se": se, "t": t, "p_two_sided": p}


def _betainc_regularised(a: float, b: float, x: float) -> float:
    """Regularised incomplete beta I_x(a, b) via a continued fraction.

    Used so the diagnostic has NO scipy dependency.
    """
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = (math.lgamma(a) + math.lgamma(b) - math.lgamma(a + b))
    front = math.exp(math.log(x) * a + math.log(1.0 - x) * b - lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    else:
        return 1.0 - math.exp(
            math.log(1.0 - x) * b + math.log(x) * a - lbeta
        ) * _betacf(b, a, 1.0 - x) / b


def _betacf(a: float, b: float, x: float, itmax: int = 300,
            eps: float = 3e-16) -> float:
    """Continued fraction for the incomplete beta (Lentz's method)."""
    qab = a + b
    qap = a + 1.0
    qam = a - 1.0
    c = 1.0
    d = 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, itmax + 1):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delta = d * c
        h *= delta
        if abs(delta - 1.0) < eps:
            break
    return h


def _describe(d: np.ndarray, label: str) -> dict:
    if d.size == 0:
        return {"label": label, "n": 0}
    return {
        "label": label,
        "n": int(d.size),
        "mean": float(d.mean()),
        "median": float(np.median(d)),
        "std": float(d.std(ddof=1)) if d.size > 1 else 0.0,
        "min": float(d.min()),
        "max": float(d.max()),
        "q25": float(np.percentile(d, 25)),
        "q75": float(np.percentile(d, 75)),
        "n_positive": int((d > 0).sum()),
        "n_negative": int((d < 0).sum()),
        "n_zero": int((d == 0).sum()),
    }


def main() -> int:
    # The Windows console defaults to cp1252, which cannot encode the
    # box-drawing characters used in the report. Force UTF-8 on both
    # streams so the output is not mangled or lost.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass

    if not REPORT.exists():
        print(f"missing {REPORT} — run "
              f"runtime.diagnostics.contra_close_actual_vs_simul first")
        return 1

    payload = json.loads(REPORT.read_text(encoding="utf-8"))
    rows: list[dict] = []
    for ym, v in payload["per_month"].items():
        for r in v["rows"]:
            r = dict(r)
            r["ym"] = ym
            r["mkt"] = v["market_pct"]
            rows.append(r)

    d = np.asarray([r["delta"] for r in rows], dtype=np.float64)
    mk = np.asarray([r["mkt"] for r in rows], dtype=np.float64)
    sp = np.asarray([r["spread"] for r in rows], dtype=np.float64)

    print("=" * 78)
    print("CONTRA CLOSE — IS THE PAIRED DELTA REAL?")
    print("=" * 78)
    print()
    print(_describe(d, "all pairs"))
    print()

    order = np.argsort(-d)
    d_ex_top1 = np.delete(d, order[0])
    d_ex_top3 = np.delete(d, order[:3])

    tests = {
        "all_pairs": {
            "describe": _describe(d, "all pairs"),
            "t_test": _t_test(d),
            "wilcoxon": _wilcoxon_signed_rank(d),
            "sign_test": _sign_test(d),
            "bootstrap": _bootstrap_ci(d),
        },
        "ex_best_pair": {
            "describe": _describe(d_ex_top1, "excluding the best pair"),
            "t_test": _t_test(d_ex_top1),
            "wilcoxon": _wilcoxon_signed_rank(d_ex_top1),
            "sign_test": _sign_test(d_ex_top1),
            "bootstrap": _bootstrap_ci(d_ex_top1),
        },
        "ex_top3_pairs": {
            "describe": _describe(d_ex_top3, "excluding the best 3 pairs"),
            "t_test": _t_test(d_ex_top3),
            "wilcoxon": _wilcoxon_signed_rank(d_ex_top3),
            "sign_test": _sign_test(d_ex_top3),
            "bootstrap": _bootstrap_ci(d_ex_top3),
        },
    }

    for name, t in tests.items():
        w = t["wilcoxon"]
        s = t["sign_test"]
        b = t["bootstrap"]
        tt = t["t_test"]
        print(f"── {name} " + "─" * (70 - len(name)))
        print(f"   t-test          t = {tt.get('t', 0.0):+.3f}   "
              f"p = {tt.get('p_two_sided', 1.0):.4f}")
        print(f"   Wilcoxon        z = {w.get('z', 0.0):+.3f}   "
              f"p = {w.get('p_two_sided', 1.0):.4f}   "
              f"(W+ = {w.get('w_plus', 0.0):.1f})")
        print(f"   sign test       {s.get('n_pos', 0)}/{s.get('n', 0)} "
              f"positive   p = {s.get('p_two_sided', 1.0):.4f}")
        print(f"   bootstrap 95% CI on the mean: "
              f"[${b.get('ci95_lo', 0.0):+.3f}, ${b.get('ci95_hi', 0.0):+.3f}]   "
              f"P(mean <= 0) = {b.get('p_mean_le_zero', 1.0):.4f}")
        print()

    # ── The threshold question, done properly ────────────────────
    print("=" * 78)
    print("PAIRED DELTA BY LOCKED SPREAD (the trigger variable)")
    print("=" * 78)
    print(f"{'spread band':<16}{'n':>5}{'total':>11}{'mean':>10}"
          f"{'wilcox p':>11}{'sign p':>10}{'boot P(<=0)':>13}")
    bands = [(0.0, 1.0), (1.0, 2.0), (2.0, 5.0), (5.0, 1e9)]
    band_stats: dict[str, dict] = {}
    for lo, hi in bands:
        sel = (sp >= lo) & (sp < hi)
        dd = d[sel]
        if dd.size == 0:
            continue
        w = _wilcoxon_signed_rank(dd)
        s = _sign_test(dd)
        b = _bootstrap_ci(dd)
        lbl = (f"${lo:g}-${hi:g}" if hi < 1e8 else f"${lo:g}+")
        band_stats[lbl] = {
            "n": int(dd.size), "total": float(dd.sum()),
            "mean": float(dd.mean()),
            "wilcoxon_p": w.get("p_two_sided", 1.0),
            "sign_p": s.get("p_two_sided", 1.0),
            "bootstrap_p_mean_le_zero": b.get("p_mean_le_zero", 1.0),
        }
        print(f"{lbl:<16}{dd.size:>5}{dd.sum():>11,.2f}{dd.mean():>10,.3f}"
              f"{w.get('p_two_sided', 1.0):>11.4f}"
              f"{s.get('p_two_sided', 1.0):>10.4f}"
              f"{b.get('p_mean_le_zero', 1.0):>13.4f}")
    print()

    # The decision-relevant cut: pairs the shipped feature can even
    # reach (spread >= threshold) vs the ones it cannot.
    print("=" * 78)
    print("THE SHIPPED FEATURE'S REACH — pairs above each threshold")
    print("=" * 78)
    print(f"{'thr':>6}{'n reachable':>13}{'total':>11}{'mean':>10}"
          f"{'wilcox p':>11}{'boot P(<=0)':>13}")
    thr_stats: dict[str, dict] = {}
    for thr in (0.0, 0.5, 1.0, 2.0, 4.5, 6.5):
        sel = sp >= thr
        dd = d[sel]
        if dd.size == 0:
            continue
        w = _wilcoxon_signed_rank(dd)
        b = _bootstrap_ci(dd)
        thr_stats[f"{thr:g}"] = {
            "n": int(dd.size), "total": float(dd.sum()),
            "mean": float(dd.mean()),
            "wilcoxon_p": w.get("p_two_sided", 1.0),
            "bootstrap_p_mean_le_zero": b.get("p_mean_le_zero", 1.0),
        }
        print(f"{thr:>6g}{dd.size:>13}{dd.sum():>11,.2f}{dd.mean():>10,.3f}"
              f"{w.get('p_two_sided', 1.0):>11.4f}"
              f"{b.get('p_mean_le_zero', 1.0):>13.4f}")
    print()

    OUT.write_text(json.dumps(
        {"tests": tests, "by_spread_band": band_stats,
         "by_threshold_reach": thr_stats,
         "n_pairs": int(d.size)},
        indent=2, default=float), encoding="utf-8")
    print(f"wrote {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
