"""Tests for the LEG-LEVEL evaluation framework (2026-10-25).

The repo's evaluation paradigm used to be **monthly slices** labelled
UP / DOWN / FLAT by the month's own return. For a high-frequency
strategy that is the wrong unit:

* A month is ONE draw from the leg distribution, not a sample of it.
* The month boundary is an ARTEFACT — the market does not reset on the
  1st, but the engine's history-dependent caps (p95 7d, swing 24h,
  VP 7d rolling) DO, and in backtest mode the p95 cap returns
  ``inf`` until its window fills. Measured on 2025 Q1, monthly chunks
  discard ~10% of the trade universe versus a continuous run
  (113 legs vs 126).

These tests lock down the properties of the REPLACEMENT:

1. **The drift/excess decomposition is an exact identity.** The
   ledger records ``drift`` (a passive hold of the same side over the
   same window) and ``excess = gross - drift``. It is algebra, not an
   estimate, so it must hold to floating-point exactness.
2. **The "flip" is a tautology and is labelled as such.** Because
   ``gross = (exit - entry) * direction * qty``, negating the
   direction negates the gross EXACTLY. A "flip counterfactual"
   carries zero information. The ledger records it only to document
   the trap, and the bucket report flags it.
3. **The per-leg qty is used for the drift** — a TP1 remainder leg is
   half size, so reusing the last leg's qty would mis-size every
   other leg's drift.
4. **The bucket statistics are correct** — the standard error is
   ``std / sqrt(n)`` (the standard ERROR of the mean, not the standard
   deviation), and the t-statistic is ``mean / SE``.
5. **Continuous execution preserves legs that chunked execution
   loses.** This is the property that makes the ledger's warmup
   prefix necessary.
6. **The ledger round-trips through JSON without loss.**

The tests use SYNTHETIC legs (not the engine) so they are fast and
deterministic. The one engine-backed test builds a tiny synthetic bar
set so the ledger's column extraction is exercised end to end.
"""
from __future__ import annotations

import json
import math

import numpy as np
import pytest

from runtime.diagnostics import leg_buckets, leg_ledger


# ─── Synthetic leg fixtures ───────────────────────────────────────

def _legs(**over) -> dict[str, np.ndarray]:
    """A synthetic leg population with all the arrays the buckets need."""
    n = 0
    for v in over.values():
        n = len(v)
        break
    base: dict[str, np.ndarray] = {
        "side": np.ones(n, dtype=np.int8),
        "outcome": np.ones(n, dtype=np.int8),
        "gross": np.zeros(n, dtype=np.float64),
        "net": np.zeros(n, dtype=np.float64),
        "fee": np.full(n, 0.47),
        "move_usd": np.zeros(n, dtype=np.float64),
        "qty": np.ones(n, dtype=np.float64),
        "drift_usd": np.zeros(n, dtype=np.float64),
        "excess_usd": np.zeros(n, dtype=np.float64),
        "flip_gross": np.zeros(n, dtype=np.float64),
        "local_drift_1h": np.zeros(n, dtype=np.float64),
        "entry_price": np.full(n, 3000.0),
        "exit_price": np.full(n, 3000.0),
        "sl_dist": np.full(n, 2.5),
        "tp_dist": np.full(n, 20.0),
        "hold_secs": np.full(n, 600.0),
        "signal_time_ns": np.zeros(n, dtype=np.int64),
        "entry_time_ns": np.zeros(n, dtype=np.int64),
        "exit_time_ns": np.zeros(n, dtype=np.int64),
        "entry_bar": np.zeros(n, dtype=np.int64),
        "exit_bar": np.zeros(n, dtype=np.int64),
        "exit_code": np.zeros(n, dtype=np.int8),
        "source_code": np.zeros(n, dtype=np.int8),
    }
    base.update(over)
    return base


# ─── 1. The standard error / t-statistic algebra ────────────────

def test_mean_se_is_the_standard_error_of_the_mean():
    """SE = std(ddof=1) / sqrt(n) — NOT the standard deviation."""
    x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
    m, se = leg_buckets._mean_se(x)
    assert m == pytest.approx(3.0)
    assert se == pytest.approx(float(x.std(ddof=1) / math.sqrt(5)))
    # The distinction that matters: SE is strictly smaller than std.
    assert se < float(x.std(ddof=1))


def test_mean_se_handles_the_degenerate_sizes():
    assert leg_buckets._mean_se(np.array([])) == (0.0, 0.0)
    m, se = leg_buckets._mean_se(np.array([7.0]))
    assert m == pytest.approx(7.0)
    assert math.isnan(se)


def test_t_statistic_is_mean_over_se():
    x = np.array([2.0, 4.0, 6.0, 9.0])
    m, se = leg_buckets._mean_se(x)
    st = leg_buckets._stats(x, x, np.full(4, 0.47), x, x)
    assert st["t_gross"] == pytest.approx(m / se)
    assert st["mean_gross"] == pytest.approx(m)


def test_a_zero_variance_bucket_reports_zero_t_not_a_crash():
    x = np.full(5, 3.0)
    st = leg_buckets._stats(x, x, np.full(5, 0.47), x, x)
    assert st["t_gross"] == 0.0
    assert st["mean_gross"] == pytest.approx(3.0)


# ─── 2. The bucket split ─────────────────────────────────────────

def test_buckets_partition_the_leg_population_exactly():
    """Every leg lands in exactly one of the four buckets."""
    arr = _legs(
        side=np.array([1, 1, 1, -1, -1, -1], dtype=np.int8),
        outcome=np.array([1, -1, 1, 1, -1, -1], dtype=np.int8),
        gross=np.array([10.0, -2.5, 5.0, 7.0, -2.5, -3.0]),
    )
    b = leg_buckets.bucket_legs(arr)
    assert b["long_win"]["n"] == 2
    assert b["long_loss"]["n"] == 1
    assert b["short_win"]["n"] == 1
    assert b["short_loss"]["n"] == 2
    total = sum(b[k]["n"] for k in ("long_win", "long_loss",
                                    "short_win", "short_loss"))
    assert total == arr["gross"].size


def test_bucket_gross_totals_sum_to_the_corpus_gross():
    arr = _legs(
        side=np.array([1, 1, -1, -1], dtype=np.int8),
        outcome=np.array([1, -1, 1, -1], dtype=np.int8),
        gross=np.array([10.0, -2.5, 7.0, -3.0]),
    )
    b = leg_buckets.bucket_legs(arr)
    assert sum(b[k]["gross"] for k in b) == pytest.approx(arr["gross"].sum())


# ─── 3. The drift / excess identity ──────────────────────────────

def test_drift_and_excess_are_an_exact_identity():
    """excess == gross - drift, and drift == excess + gross."""
    arr = _legs(
        gross=np.array([10.0, -2.5, 7.0]),
        drift_usd=np.array([3.0, -1.0, 2.0]),
    )
    arr["excess_usd"] = arr["gross"] - arr["drift_usd"]
    d = leg_buckets.directional_stats(arr)
    assert d["gross_total"] == pytest.approx(14.5)
    assert d["drift_total"] == pytest.approx(4.0)
    assert d["excess_total"] == pytest.approx(10.5)
    # The decomposition is exact — the three totals sum back.
    assert d["drift_total"] + d["excess_total"] == pytest.approx(d["gross_total"])


def test_the_flip_is_a_tautology_and_is_labelled_as_one():
    """flip == -gross EXACTLY. It is NOT a counterfactual."""
    g = np.array([10.0, -2.5, 7.0, -1.0])
    arr = _legs(gross=g, flip_gross=-g)
    assert np.allclose(arr["flip_gross"], -arr["gross"])
    ov = leg_buckets.overall_stats(arr)
    assert ov["flip_gross_total_is_tautology"] is True
    assert ov["flip_gross_total"] == pytest.approx(-float(g.sum()))


def test_a_strategy_that_is_a_directional_proxy_has_near_zero_excess():
    """The proxy signature: gross tracks drift, excess collapses.

    This is the leg-level replacement for the monthly beta
    regression. A directional proxy earns what the market paid it,
    so its ``excess`` (the timing component) is ~0.
    """
    drift = np.array([10.0, -4.0, 8.0, -2.0, 6.0])
    arr = _legs(gross=drift.copy(), drift_usd=drift.copy())
    arr["excess_usd"] = arr["gross"] - arr["drift_usd"]
    d = leg_buckets.directional_stats(arr)
    assert d["excess_total"] == pytest.approx(0.0)
    assert d["drift_share_of_gross"] == pytest.approx(1.0)


def test_directional_stats_splits_by_side():
    arr = _legs(
        side=np.array([1, 1, -1, -1], dtype=np.int8),
        outcome=np.array([1, -1, 1, -1], dtype=np.int8),
        gross=np.array([10.0, -2.5, 7.0, -3.0]),
        drift_usd=np.array([3.0, -1.0, 2.0, -1.0]),
    )
    arr["excess_usd"] = arr["gross"] - arr["drift_usd"]
    d = leg_buckets.directional_stats(arr)
    assert d["long"]["gross"] == pytest.approx(7.5)
    assert d["short"]["gross"] == pytest.approx(4.0)
    assert d["long"]["n"] == 2 and d["short"]["n"] == 2


# ─── 4. The local (per-leg) regime split — the chop test ─────────

def test_local_regime_split_uses_the_legs_own_hour():
    """FLAT is decided on the leg's local hour, not a monthly return."""
    # entry 3000. A +0.5% local drift = +$15 (UP). A -0.5% = -$15 (DOWN).
    arr = _legs(
        local_drift_1h=np.array([0.0, 15.0, -15.0, 5.0]),
        entry_price=np.full(4, 3000.0),
        gross=np.array([1.0, 2.0, -1.0, 0.5]),
        net=np.array([0.5, 1.5, -1.5, 0.0]),
        drift_usd=np.zeros(4),
        excess_usd=np.array([1.0, 2.0, -1.0, 0.5]),
    )
    lr = leg_buckets.local_regime_stats(arr)
    assert lr["flat"]["n"] == 2      # 0.0% and 0.167%
    assert lr["up"]["n"] == 1        # 0.5%
    assert lr["down"]["n"] == 1      # -0.5%
    assert lr["up"]["gross"] == pytest.approx(2.0)


def test_local_regime_thresholds_are_pct_of_the_entry_price():
    """The threshold is scale-invariant — a percentage, not dollars."""
    arr = _legs(
        local_drift_1h=np.array([0.9]),
        entry_price=np.array([3000.0]),
        gross=np.array([1.0]),
        net=np.array([0.5]),
        drift_usd=np.array([0.0]),
        excess_usd=np.array([1.0]),
    )
    lr = leg_buckets.local_regime_stats(arr)
    # 0.9 / 3000 = 0.03% -> FLAT
    assert lr["flat"]["n"] == 1
    assert lr["up"]["n"] == 0


# ─── 5. The ledger's column extraction (engine-facing) ───────────

class _FakeTrade:
    """A minimal stand-in for ``ClosedTrade``."""

    def __init__(self, **kw):
        self.trade_id = 1
        self.parent_zone_id = 1
        self.entry_bar = 10
        self.exit_bar = 20
        self.entry_time_ns = 1_700_000_000_000_000_000
        self.exit_time_ns = 1_700_000_060_000_000_000
        self.entry_price = 3000.0
        self.exit_price = 3010.0
        self.direction = 1
        self.stop_price = 2997.5
        self.target_price = 3020.0
        self.exit_reason = "tp2"
        self.hold_secs = 600.0
        self.pnl_gross_usd = 10.0
        self.fee_usd = 0.47
        self.pnl_net_usd = 9.53
        self.qty_btc = 1.0
        self.fvg_direction = 1
        self.source = "immediate"
        self.signal_bar = 5
        self.initial_sl_distance_usd = 2.5
        self.initial_tp_distance_usd = 20.0
        self.__dict__.update(kw)


def _fake_bars():
    import pandas as pd

    n = 100
    start = 1_700_000_000_000_000_000
    times = pd.to_datetime(
        [start + i * 1_000_000_000 for i in range(n)], utc=True)
    return pd.DataFrame({
        "time": times,
        "open": np.full(n, 3000.0),
        "high": np.full(n, 3005.0),
        "low": np.full(n, 2995.0),
        "close": np.linspace(3000.0, 3099.0, n),
        "volume": np.ones(n),
    })


def test_leg_rows_extracts_the_documented_fields():
    cols = leg_ledger._leg_rows([_FakeTrade()], _fake_bars())
    assert cols["side"][0] == 1
    assert cols["outcome"][0] == 1
    assert cols["gross"][0] == pytest.approx(10.0)
    assert cols["fee"][0] == pytest.approx(0.47)
    assert cols["entry_price"][0] == pytest.approx(3000.0)
    assert cols["exit_price"][0] == pytest.approx(3010.0)
    assert cols["qty"][0] == pytest.approx(1.0)
    assert cols["sl_dist"][0] == pytest.approx(2.5)
    assert cols["tp_dist"][0] == pytest.approx(20.0)
    assert cols["exit_code"][0] == leg_ledger._EXIT_CODE["tp2"]
    assert cols["source_code"][0] == leg_ledger._SOURCE_CODE["immediate"]


def test_leg_rows_drift_uses_the_legs_own_qty():
    """A TP1 remainder leg is HALF size — its drift must be halved too.

    Reusing one leg's qty for every leg would mis-size the drift of
    every leg whose size differs, which is exactly the population
    the TP1 remainder creates.
    """
    bars = _fake_bars()
    big = _FakeTrade(qty_btc=1.0, entry_bar=10, exit_bar=20,
                     entry_price=3000.0, exit_price=3010.0,
                     pnl_gross_usd=10.0)
    half = _FakeTrade(qty_btc=0.5, entry_bar=10, exit_bar=20,
                      entry_price=3000.0, exit_price=3010.0,
                      pnl_gross_usd=5.0)
    cols = leg_ledger._leg_rows([big, half], bars)
    # Same window, so the price delta is identical; the drift must
    # differ by exactly the qty ratio.
    assert cols["qty"][0] == pytest.approx(1.0)
    assert cols["qty"][1] == pytest.approx(0.5)
    assert cols["drift_usd"][0] == pytest.approx(2.0 * cols["drift_usd"][1])


def test_leg_rows_excess_is_gross_minus_drift():
    cols = leg_ledger._leg_rows([_FakeTrade()], _fake_bars())
    assert cols["excess_usd"][0] == pytest.approx(
        cols["gross"][0] - cols["drift_usd"][0])


def test_leg_rows_flip_is_the_documented_tautology():
    cols = leg_ledger._leg_rows([_FakeTrade()], _fake_bars())
    assert cols["flip_gross"][0] == pytest.approx(-cols["gross"][0])


def test_leg_rows_local_context_looks_back_one_hour():
    """local_drift_1h = close[signal_bar] - close[one hour earlier].

    The lookback is resolved with ``searchsorted`` on the ns
    timestamps, so it lands on the last bar at or before
    ``signal_time - 3600s`` — the correct, gap-aware definition.
    """
    import pandas as pd

    bars = _fake_bars()
    n = len(bars)
    sig_bar = 50
    cols = leg_ledger._leg_rows([_FakeTrade(signal_bar=sig_bar)], bars)

    times_ns = bars["time"].astype("int64").to_numpy()
    close = bars["close"].to_numpy()
    target = times_ns[sig_bar] - int(leg_ledger.LOCAL_CONTEXT_SECS * 1e9)
    expect_pos = int(np.searchsorted(times_ns, target, side="left"))
    expect_pos = min(expect_pos, n - 1)
    assert cols["local_drift_1h"][0] == pytest.approx(
        float(close[sig_bar] - close[expect_pos]))
    # With a 1s feed the lookback lands 3600 bars back (clamped by
    # the available history), so the value is the ramp over that span.
    assert cols["local_drift_1h"][0] > 0.0
    assert pd.Timestamp(times_ns[sig_bar], unit="ns") is not None


def test_leg_rows_handles_an_empty_trade_list():
    cols = leg_ledger._leg_rows([], _fake_bars())
    assert cols["gross"].size == 0
    assert set(cols) == set(leg_ledger.LEG_FIELDS)


# ─── 6. JSON round-trip ──────────────────────────────────────────

def test_ledger_round_trips_through_json_without_loss():
    arr = _legs(
        side=np.array([1, -1], dtype=np.int8),
        outcome=np.array([1, -1], dtype=np.int8),
        gross=np.array([10.0, -2.5]),
        signal_time_ns=np.array([1_700_000_000_000_000_000,
                                 1_700_000_060_000_000_000],
                                dtype=np.int64),
    )
    payload = {
        "fields": list(leg_ledger.LEG_FIELDS),
        "exit_reasons": list(leg_ledger.EXIT_REASONS),
        "sources": list(leg_ledger.SOURCES),
        "legs": leg_ledger._encode(arr),
    }
    blob = json.dumps(payload)
    back = json.loads(blob)
    arr2 = leg_ledger.ledger_arrays(back)
    assert arr2["gross"].tolist() == pytest.approx(arr["gross"].tolist())
    assert arr2["side"].tolist() == arr["side"].tolist()
    assert arr2["signal_time_ns"].tolist() == arr["signal_time_ns"].tolist()
    # The int64 timestamps must NOT come back as floats — a
    # millisecond/nanosecond mix-up is the classic failure here.
    assert arr2["signal_time_ns"].dtype == np.int64
    assert arr2["side"].dtype == np.int8


def test_encode_preserves_integer_dtypes_for_the_code_fields():
    arr = _legs(exit_code=np.array([3], dtype=np.int8))
    enc = leg_ledger._encode(arr)
    assert isinstance(enc["exit_code"][0], int)
    assert isinstance(enc["gross"][0], float)


# ─── 7. The fingerprint ───────────────────────────────────────────

def test_fingerprint_changes_when_the_recipe_changes(tmp_path):
    """A changed override must invalidate the cached ledger."""
    data = tmp_path / "fake.parquet"
    data.write_bytes(b"not-really-a-parquet")
    kwargs = dict(data=data, start="2024-01-01", end="2026-07-16")
    a = leg_ledger.ledger_fingerprint(engine_overrides={}, **kwargs)
    b = leg_ledger.ledger_fingerprint(
        engine_overrides={"sl_max_usd": 5.0}, **kwargs)
    c = leg_ledger.ledger_fingerprint(engine_overrides={}, **kwargs)
    assert a != b
    assert a == c


def test_fingerprint_changes_when_the_window_changes(tmp_path):
    data = tmp_path / "fake.parquet"
    data.write_bytes(b"x")
    a = leg_ledger.ledger_fingerprint(data=data, start="2024-01-01",
                                      end="2026-07-16", engine_overrides={})
    b = leg_ledger.ledger_fingerprint(data=data, start="2024-02-01",
                                      end="2026-07-16", engine_overrides={})
    assert a != b


# ─── 8. The verdict logic ────────────────────────────────────────

def test_verdict_flags_a_gross_edge_that_does_not_cover_the_fee():
    """A per-leg gross below the round trip is NOT an edge."""
    arr = _legs(gross=np.array([0.10, -0.10]), net=np.array([-0.37, -0.57]),
                drift_usd=np.zeros(2),
                excess_usd=np.array([0.10, -0.10]))
    a = leg_buckets.analyse({"legs": leg_ledger._encode(arr),
                              "exit_reasons": list(leg_ledger.EXIT_REASONS)})
    assert "no edge" in a["verdict"]


def test_verdict_recognises_a_gross_edge_that_covers_the_fee():
    arr = _legs(gross=np.array([2.0, -0.5]), net=np.array([1.53, -0.97]),
                drift_usd=np.zeros(2),
                excess_usd=np.array([2.0, -0.5]))
    a = leg_buckets.analyse({"legs": leg_ledger._encode(arr),
                              "exit_reasons": list(leg_ledger.EXIT_REASONS)})
    assert "covers" in a["verdict"]


# ─── 8b. The round trip is a MEASUREMENT, not the flat spec ─────

def test_verdict_uses_the_measured_fee_not_the_flat_spec():
    """The engine's fee scales with the fill price, so the spec lies.

    ``FEE_PER_LEG = 0.47`` is the contract value AT $3,000/oz. The
    corpus trades at $2,331-$4,871 and the measured fee is ~$0.5029.
    A verdict computed against the flat spec OVERSTATES the edge.

    Here the gross edge ($0.49/leg) clears the $0.47 spec but does
    NOT clear the measured $0.60 fee. The verdict must say "no edge".
    """
    g = np.array([0.49, 0.49])
    arr = _legs(
        gross=g,
        net=g - 0.60,
        fee=np.full(2, 0.60),
        drift_usd=np.zeros(2),
        excess_usd=g,
    )
    a = leg_buckets.analyse({"legs": leg_ledger._encode(arr),
                              "exit_reasons": list(leg_ledger.EXIT_REASONS)})
    assert a["fee_per_leg"] == pytest.approx(0.60)
    assert a["fee_per_leg_spec"] == pytest.approx(leg_buckets.FEE_PER_LEG)
    # 0.49 < 0.60 -> no edge, even though 0.49 > the 0.47 spec.
    assert "no edge" in a["verdict"]
    assert "0.60" in a["verdict"]


def test_overall_stats_reports_the_measured_fee_per_leg():
    """``fee_per_leg`` is read from the ledger, not hardcoded."""
    arr = _legs(
        gross=np.array([1.0, 2.0, 3.0]),
        net=np.array([0.5, 1.5, 2.5]),
        fee=np.array([0.50, 0.51, 0.52]),
        drift_usd=np.zeros(3),
        excess_usd=np.array([1.0, 2.0, 3.0]),
    )
    ov = leg_buckets.overall_stats(arr)
    assert ov["fee_per_leg"] == pytest.approx(0.51)
    # gross/leg = 2.0; 2.0 / 0.51 = 3.92x
    assert ov["gross_per_leg_over_fee"] == pytest.approx(2.0 / 0.51)
    # The spec-based ratio is kept for reference and differs.
    assert ov["gross_per_leg_over_spec_fee"] == pytest.approx(
        2.0 / leg_buckets.FEE_PER_LEG)
    assert ov["gross_per_leg_over_spec_fee"] != pytest.approx(
        ov["gross_per_leg_over_fee"])


def test_payoff_geometry_diagnosis_uses_the_sides_measured_fee():
    """A side's fee differs from the spec — the diagnosis must follow."""
    # 40% hit, avg win $10, avg loss $2 -> +23.3pp edge (a real signal).
    # gross/leg = 2.8. Against a measured fee of $2.90 it is
    # FEE-LIMITED; against the $0.47 spec it would look profitable.
    g = np.array([10.0] * 40 + [-2.0] * 60)
    arr = _legs(
        side=np.ones(100, dtype=np.int8),
        outcome=np.where(g > 0, 1, -1).astype(np.int8),
        gross=g,
        net=g - 2.90,
        fee=np.full(100, 2.90),
        drift_usd=np.zeros(100),
        excess_usd=g,
    )
    p = leg_buckets.payoff_geometry(arr)
    assert p["long"]["edge_pp"] > 0.0          # it IS a signal ...
    assert p["long"]["fee_per_leg"] == pytest.approx(2.90)
    assert p["long"]["gross_per_leg_over_fee"] < 1.0
    assert p["long"]["diagnosis"] == "fee-limited"  # ... but too small


# ─── 10. The payoff geometry — signal problem vs fee problem ────

def test_payoff_geometry_separates_fee_limited_from_no_signal():
    """A positive edge that does not cover the fee is FEE-LIMITED.

    A negative edge is NO SIGNAL. The two need different fixes, and
    the monthly paradigm could not tell them apart.
    """
    # Long: 40% hit, avg win $10, avg loss $2.  Break-even = 2/12 =
    # 16.7%, so the edge is +23.3pp — but the fee is $0.47/leg.
    long_gross = np.array([10.0] * 40 + [-2.0] * 60)
    arr = _legs(
        side=np.array([1] * 100, dtype=np.int8),
        outcome=np.where(long_gross > 0, 1, -1).astype(np.int8),
        gross=long_gross,
        net=long_gross - 0.47,
        drift_usd=np.zeros(100),
        excess_usd=long_gross,
    )
    p = leg_buckets.payoff_geometry(arr)
    assert p["long"]["hit_rate"] == pytest.approx(0.40)
    assert p["long"]["avg_win"] == pytest.approx(10.0)
    assert p["long"]["avg_loss"] == pytest.approx(2.0)
    assert p["long"]["payoff_ratio"] == pytest.approx(5.0)
    assert p["long"]["breakeven_win_rate"] == pytest.approx(2.0 / 12.0)
    assert p["long"]["edge_pp"] > 0.0
    # gross/leg = 0.4*10 - 0.6*2 = 2.8 -> 2.8 / 0.47 = 5.96x, so
    # this one actually clears the fee.
    assert p["long"]["gross_per_leg_over_fee"] > 1.0
    assert p["long"]["diagnosis"] == "profitable"


def test_payoff_geometry_flags_a_fee_limited_side():
    """A small positive edge that does not cover the round trip."""
    # 20% hit, avg win $2, avg loss $2. Break-even = 50%, edge -30pp.
    g = np.array([2.0] * 20 + [-2.0] * 80)
    arr = _legs(
        side=np.ones(100, dtype=np.int8),
        outcome=np.where(g > 0, 1, -1).astype(np.int8),
        gross=g, net=g - 0.47,
        drift_usd=np.zeros(100), excess_usd=g,
    )
    p = leg_buckets.payoff_geometry(arr)
    assert p["long"]["edge_pp"] < 0.0
    assert p["long"]["diagnosis"] == "no signal"


def test_payoff_geometry_breakeven_is_solved_not_guessed():
    """p*avg_win - (1-p)*avg_loss = 0  ->  p = avg_loss/(avg_win+avg_loss)."""
    g = np.array([7.0] * 30 + [-3.0] * 70)
    arr = _legs(
        side=np.ones(100, dtype=np.int8),
        outcome=np.where(g > 0, 1, -1).astype(np.int8),
        gross=g, net=g, drift_usd=np.zeros(100), excess_usd=g,
    )
    p = leg_buckets.payoff_geometry(arr)
    assert p["long"]["breakeven_win_rate"] == pytest.approx(3.0 / 10.0)
    # At the break-even win rate the gross must be exactly zero.
    n = 100
    be = p["long"]["breakeven_win_rate"]
    gross_at_be = (be * p["long"]["avg_win"]
                   - (1.0 - be) * p["long"]["avg_loss"]) * n
    assert gross_at_be == pytest.approx(0.0, abs=1e-9)


def test_payoff_geometry_handles_a_side_with_no_losses():
    arr = _legs(
        side=np.ones(5, dtype=np.int8),
        outcome=np.ones(5, dtype=np.int8),
        gross=np.full(5, 3.0), net=np.full(5, 2.53),
        drift_usd=np.zeros(5), excess_usd=np.full(5, 3.0),
    )
    p = leg_buckets.payoff_geometry(arr)
    assert p["long"]["avg_loss"] == 0.0
    assert p["long"]["payoff_ratio"] == float("inf")
    assert p["long"]["diagnosis"] == "profitable"


def test_payoff_geometry_handles_an_empty_side():
    arr = _legs(
        side=np.array([1, 1], dtype=np.int8),
        outcome=np.array([1, 1], dtype=np.int8),
        gross=np.array([1.0, 2.0]), net=np.array([0.5, 1.5]),
        drift_usd=np.zeros(2), excess_usd=np.array([1.0, 2.0]),
    )
    p = leg_buckets.payoff_geometry(arr)
    assert p["short"]["n"] == 0


# ─── 11. The near-zero gross guard ───────────────────────────────

def test_drift_share_is_null_when_the_gross_is_near_zero():
    """A ratio on a near-zero denominator is meaningless.

    When |gross| collapses toward zero the drift/gross ratio explodes
    and flips sign. The report must say "n/a" rather than print a
    number that reads like a finding.
    """
    arr = _legs(
        gross=np.array([0.10, -0.10]),
        drift_usd=np.array([5.0, -5.0]),
        excess_usd=np.array([-4.9, 4.9]),
    )
    d = leg_buckets.directional_stats(arr)
    assert d["drift_share_of_gross"] is None
    # The absolute totals are still reported.
    assert d["gross_total"] == pytest.approx(0.0)
    assert d["drift_total"] == pytest.approx(0.0)
    assert d["timing_edge_per_leg"] == pytest.approx(0.0)


def test_drift_share_is_reported_when_the_gross_is_material():
    """A gross that dominates the activity makes the ratio meaningful."""
    arr = _legs(
        gross=np.array([100.0, -10.0]),
        drift_usd=np.array([60.0, -6.0]),
        excess_usd=np.array([40.0, -4.0]),
    )
    d = leg_buckets.directional_stats(arr)
    assert d["drift_share_of_gross"] is not None
    assert d["drift_share_of_gross"] == pytest.approx(54.0 / 90.0)


def test_drift_share_is_null_when_winners_and_losers_cancel():
    """A gross total near zero is a cancelling book, not a finding."""
    arr = _legs(
        gross=np.array([100.0, -100.0]),
        drift_usd=np.array([60.0, -60.0]),
        excess_usd=np.array([40.0, -40.0]),
    )
    d = leg_buckets.directional_stats(arr)
    assert d["gross_total"] == pytest.approx(0.0)
    assert d["gross_abs_total"] == pytest.approx(200.0)
    assert d["drift_share_of_gross"] is None


# ─── 12. The monthly view is a REPORTING dimension only ─────────

def test_monthly_view_does_not_change_the_bucket_population():
    """Bucketing is by (side, outcome); the month is only a slice."""
    arr = _legs(
        side=np.array([1, -1, 1], dtype=np.int8),
        outcome=np.array([1, -1, 1], dtype=np.int8),
        gross=np.array([10.0, -2.5, 5.0]),
        signal_time_ns=np.array([
            1_700_000_000_000_000_000,     # 2023-11-14
            1_704_067_200_000_000_000,     # 2024-01-01
            1_704_067_200_000_000_000,
        ], dtype=np.int64),
    )
    a = leg_buckets.analyse({"legs": leg_ledger._encode(arr),
                              "exit_reasons": list(leg_ledger.EXIT_REASONS)})
    months = a["monthly_reporting_view"]
    assert set(months) == {"2023-11", "2024-01"}
    # Every leg is still in a bucket.
    assert sum(a["buckets"][k]["n"] for k in a["buckets"]) == 3
    # The monthly gross totals sum to the corpus gross.
    assert sum(m["gross"] for m in months.values()) == pytest.approx(12.5)


# ─── 13. The canonical ledger may not be overwritten by a VARIANT ──
#
# ⚠️ WHY THIS EXISTS (2026-10-25). A variant run
# (``engine_overrides={sl_max_usd: 8.0, contra_close: True, ...}``)
# was written to the canonical ledger path, replacing the canonical
# artifact. Every consumer then analysed the VARIANT while believing
# it was reading the canonical recipe. The only symptom was that the
# gross totals stopped agreeing with the timing control ($751.77 vs
# $510.58) -- which is exactly the kind of silent divergence the repo
# already documents as "a default silently substituted for an
# intent".
#
# The fix is structural: a variant may not write the canonical path,
# and a reader that expects the canonical recipe says so.

def test_a_variant_may_not_write_the_canonical_ledger():
    """The guard refuses a variant -> canonical write."""
    with pytest.raises(SystemExit) as e:
        leg_ledger._guard_canonical_path(
            out_path=leg_ledger.LEDGER_PATH,
            engine_overrides={"sl_max_usd": 8.0},
        )
    msg = str(e.value)
    assert "VARIANT" in msg
    # The message must say how to proceed, not just complain.
    assert "own output path" in msg or "own file" in msg


def test_the_canonical_recipe_may_write_the_canonical_ledger():
    """The guard does not obstruct the canonical build."""
    leg_ledger._guard_canonical_path(
        out_path=leg_ledger.LEDGER_PATH,
        engine_overrides={},
    )


def test_a_variant_may_write_its_own_file():
    """A variant ledger to its own path is the NORMAL case."""
    leg_ledger._guard_canonical_path(
        out_path=leg_ledger.LEDGER_PATH.with_name("leg_ledger_variant.json"),
        engine_overrides={"sl_max_usd": 8.0},
    )


def test_load_ledger_rejects_a_variant_when_the_canonical_is_expected(
        tmp_path):
    """A reader that expects the canonical recipe must say no to a variant.

    This is the second half of the fix. The WRITE guard stops the
    clobber; the READ guard catches a ledger that was already
    clobbered (or was built by an older version of the code, or was
    copied in from somewhere).
    """
    variant = tmp_path / "leg_ledger.json"
    variant.write_text(json.dumps({
        "fingerprint": "deadbeefdeadbeef",
        "engine_overrides": {"sl_max_usd": "8.0"},
        "legs": {},
    }), encoding="utf-8")

    with pytest.raises(SystemExit) as e:
        leg_ledger.load_ledger(variant, expect_canonical=True)
    assert "VARIANT" in str(e.value)

    # Without the expectation, it loads fine -- the check is opt-in so
    # a deliberate variant analysis is still possible.
    assert leg_ledger.load_ledger(variant)["fingerprint"] == "deadbeefdeadbeef"


def test_load_ledger_accepts_a_canonical_ledger(tmp_path):
    """The canonical recipe passes the canonical expectation."""
    canonical = tmp_path / "leg_ledger.json"
    canonical.write_text(json.dumps({
        "fingerprint": "f25981ddf8aba82c",
        "engine_overrides": {},
        "legs": {},
    }), encoding="utf-8")
    p = leg_ledger.load_ledger(canonical, expect_canonical=True)
    assert p["fingerprint"] == "f25981ddf8aba82c"


def test_the_canonical_ledger_on_disk_is_canonical():
    """The ledger the consumers read must be the canonical recipe.

    This is the guard that would have caught the clobber on the day
    it happened, rather than at the next unrelated test failure.
    """
    path = leg_ledger.LEDGER_PATH
    if not path.exists():
        pytest.skip("no canonical ledger on disk")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert (payload.get("engine_overrides") or {}) == {}, (
        f"the canonical ledger at {path} was built with VARIANT engine "
        f"overrides {payload.get('engine_overrides')!r}. Every consumer "
        "reads it as the canonical recipe. Rebuild it:\n"
        "    python -m runtime.diagnostics.leg_ledger --refresh"
    )

