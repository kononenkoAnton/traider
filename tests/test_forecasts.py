import pytest

from traider.forecasts import Scored, bootstrap_diff, brier, calibration, reference, score, summary_text

DAYS = [f"2026-12-{d:02d}" for d in (1, 2, 3, 4, 7, 8, 9, 10)]
CLOSES = {
    "SPY": dict(zip(DAYS, [100, 100, 100, 100, 100, 101, 101, 101])),     # +1% за 5 дней от 01.12
    "AAPL": dict(zip(DAYS, [200, 200, 200, 200, 200, 206, 206, 206])),    # +3%: обогнала
    "MSFT": dict(zip(DAYS, [300, 300, 300, 300, 300, 300, 300, 300])),    # 0%: отстала
}

def test_outcome_is_relative_to_spy_over_five_trading_days():
    got = score([{"day": DAYS[0], "symbol": "AAPL", "p": 0.7}, {"day": DAYS[0], "symbol": "MSFT", "p": 0.7}],
                CLOSES, DAYS, [DAYS[0]], ["AAPL", "MSFT"])
    assert [(s.symbol, s.outcome, s.missing) for s in got] == [("AAPL", 1, False), ("MSFT", 0, False)]

def test_missing_panel_forecast_is_scored_as_half():
    got = score([{"day": DAYS[0], "symbol": "AAPL", "p": 0.7}], CLOSES, DAYS, [DAYS[0]], ["AAPL", "MSFT"])
    assert [(s.symbol, s.p, s.missing) for s in got] == [("AAPL", 0.7, False), ("MSFT", 0.5, True)]

def test_days_without_a_matured_horizon_are_left_out():
    assert score([{"day": DAYS[5], "symbol": "AAPL", "p": 0.7}], CLOSES, DAYS, [DAYS[5]], ["AAPL"]) == []

def test_last_duplicate_wins():
    fc = [{"day": DAYS[0], "symbol": "AAPL", "p": 0.2}, {"day": DAYS[0], "symbol": "AAPL", "p": 0.9}]
    assert [s.p for s in score(fc, CLOSES, DAYS, [DAYS[0]], ["AAPL"])] == [0.9]

@pytest.mark.parametrize("bad", [1.5, -0.1, float("nan"), float("inf"), None, "0.7"])
def test_bad_p_is_missing(bad):
    got = score([{"day": DAYS[0], "symbol": "AAPL", "p": bad}], CLOSES, DAYS, [DAYS[0]], ["AAPL"])
    assert [(s.p, s.missing) for s in got] == [(0.5, True)]

def test_order_and_symbols_outside_panel():
    closes = {**CLOSES, "NVDA": dict(zip(DAYS, [10] * 5 + [12] * 3))}
    fc = [{"day": DAYS[1], "symbol": "NVDA", "p": 0.4}, {"day": DAYS[0], "symbol": "MSFT", "p": 0.3},
          {"day": DAYS[1], "symbol": "AAPL", "p": 0.6}, {"day": DAYS[0], "symbol": "NVDA", "p": 0.4}]
    got = score(fc, closes, DAYS, [DAYS[0], DAYS[1]], ["AAPL", "MSFT"])
    assert [(s.day, s.symbol, s.missing) for s in got] == [
        (DAYS[0], "AAPL", True), (DAYS[0], "MSFT", False), (DAYS[0], "NVDA", False),
        (DAYS[1], "AAPL", False), (DAYS[1], "MSFT", True), (DAYS[1], "NVDA", False)]

def test_pair_with_missing_close_is_skipped():
    closes = {**CLOSES, "AAPL": {d: v for d, v in CLOSES["AAPL"].items() if d != DAYS[5]}}
    got = score([], closes, DAYS, [DAYS[0]], ["AAPL", "MSFT"])
    assert [s.symbol for s in got] == ["MSFT"]
    no_bench = {k: v for k, v in CLOSES.items() if k != "SPY"}
    assert score([], no_bench, DAYS, [DAYS[0]], ["AAPL"]) == []

def test_equal_return_is_not_outperformance():
    closes = {"SPY": dict(zip(DAYS, [100] * 8)), "AAPL": dict(zip(DAYS, [50] * 8))}
    assert [s.outcome for s in score([], closes, DAYS, [DAYS[0]], ["AAPL"])] == [0]

def test_brier_and_reference_values():
    perfect = [Scored("d", "A", 1.0, 1, False), Scored("d", "B", 0.0, 0, False)]
    coin = [Scored("d", "A", 0.5, 1, False), Scored("d", "B", 0.5, 0, False)]
    assert brier(perfect) == 0.0 and brier(coin) == 0.25
    skewed = [Scored("d", s, 0.5, o, False) for s, o in zip("ABCDEFGHIJ", [1, 1, 1, 0, 0, 0, 0, 0, 0, 0])]
    assert reference(skewed) == pytest.approx(0.21)                  # 0.3 * 0.7

def test_bootstrap_is_deterministic_and_gives_an_interval():
    scored = [Scored(f"d{i // 4:02d}", f"S{i % 4}", 0.6, int((i * 7) % 3 == 0), False) for i in range(80)]
    lo, hi = bootstrap_diff(scored, seed=1)
    assert (lo, hi) == bootstrap_diff(scored, seed=1) and lo < hi and hi - lo < 0.2

def test_empty_input_raises():
    for fn in (brier, reference, bootstrap_diff):
        with pytest.raises(ValueError):
            fn([])

def test_calibration_buckets_and_summary():
    scored = [Scored("d", "A", 0.65, 1, False), Scored("d", "B", 0.70, 0, False), Scored("d", "C", 0.10, 0, False)]
    rows = calibration(scored)
    assert [(r["lo"], r["hi"], r["n"]) for r in rows if r["n"]] == [(0.0, 0.2, 1), (0.6, 0.8, 2)]
    assert next(r for r in rows if r["lo"] == 0.6)["freq"] == 0.5
    assert "60–80%" in summary_text(scored) and "50%" in summary_text(scored)
    assert summary_text([]) == "Созревших прогнозов пока нет."

def test_empty_buckets_and_last_bucket_includes_one():
    rows = calibration([Scored("d", "A", 1.0, 1, False), Scored("d", "B", 0.2, 0, False)])
    assert len(rows) == 5
    assert [r["n"] for r in rows] == [0, 1, 0, 0, 1]
    assert rows[0]["mean_p"] is None and rows[0]["freq"] is None
    assert rows[4]["mean_p"] == 1.0 and rows[1]["lo"] == 0.2     # 0.2 уходит в корзину [0.2, 0.4)

def test_summary_lines():
    scored = [Scored("d", "A", 0.65, 1, False), Scored("d", "B", 0.70, 0, False), Scored("d", "C", 0.10, 0, False)]
    assert summary_text(scored).split("\n") == ["прогнозы 0–20%: сбылось 0% (n=1)", "прогнозы 60–80%: сбылось 50% (n=2)"]

def test_one_block_of_all_days_gives_a_point_interval():
    scored = [Scored(f"d{i // 4:02d}", f"S{i % 4}", 0.6, int((i * 7) % 3 == 0), False) for i in range(80)]
    lo, hi = bootstrap_diff(scored, block=len({s.day for s in scored}))
    assert lo == hi == brier(scored) - reference(scored)

def test_forecast_for_the_benchmark_itself_is_skipped():
    fc = [{"day": DAYS[0], "symbol": "SPY", "p": 0.9}, {"day": DAYS[0], "symbol": "AAPL", "p": 0.7}]
    assert [s.symbol for s in score(fc, CLOSES, DAYS, [DAYS[0]], ["SPY", "AAPL"])] == ["AAPL"]
