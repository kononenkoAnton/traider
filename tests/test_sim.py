import math

import pytest

from traider.cage import Account, Position
from traider.sim import apply_buy, apply_close, apply_stops, mark_peak

START = Account(1000.0, (), 1000.0)

def test_buy_pays_slippage_and_sets_the_stop():
    acct, fill = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    assert acct.cash == pytest.approx(850.0)
    assert fill.price == pytest.approx(200.10) and fill.qty == pytest.approx(150.0 / 200.10)
    assert acct.positions == (Position("AAPL", fill.qty, fill.price, 186.0),)

def test_add_on_merges_and_takes_the_new_stop():
    acct, _ = apply_buy(START, "AAPL", 100.0, 200.0, 186.0)
    acct, _ = apply_buy(acct, "AAPL", 100.0, 210.0, 195.0)
    (p,) = acct.positions
    assert p.stop_price == 195.0 and p.qty == pytest.approx(100 / 200.10 + 100 / 210.105)
    assert p.entry_price == pytest.approx(200.0 / p.qty)          # средняя цена входа

def test_close_sells_a_fraction_with_slippage():
    acct, buy = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    half, fill = apply_close(acct, "AAPL", 0.5, 210.0)
    assert fill.price == pytest.approx(210.0 * 0.9995) and fill.qty == pytest.approx(buy.qty / 2)
    assert half.cash == pytest.approx(850.0 + fill.qty * fill.price)
    gone, _ = apply_close(half, "AAPL", 1.0, 210.0)
    assert gone.positions == ()

def test_close_of_unknown_symbol_or_bad_fraction_raises():
    with pytest.raises(KeyError):
        apply_close(START, "AAPL", 1.0, 200.0)
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    for bad in (1.5, 0.0, -0.1, math.nan):
        with pytest.raises(ValueError):
            apply_close(acct, "AAPL", bad, 200.0)

def test_non_finite_money_input_raises():
    for notional, price in ((math.nan, 200.0), (100.0, math.nan), (100.0, math.inf),
                            (0.0, 200.0), (-5.0, 200.0), (100.0, 0.0)):
        with pytest.raises(ValueError):
            apply_buy(START, "AAPL", notional, price, 186.0)
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    for price in (math.nan, math.inf, 0.0, -1.0):
        with pytest.raises(ValueError):
            apply_close(acct, "AAPL", 0.5, price)

def test_stop_fills_at_stop_or_at_open_after_a_gap():
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    same, fills = apply_stops(acct, {"AAPL": 187.0}, {"AAPL": 199.0})
    assert fills == [] and same == acct
    hit, (f,) = apply_stops(acct, {"AAPL": 185.0}, {"AAPL": 199.0})
    assert f.kind == "stop_fill" and f.price == pytest.approx(186.0 * 0.9995) and hit.positions == ()
    _, (gap,) = apply_stops(acct, {"AAPL": 178.0}, {"AAPL": 180.0})
    assert gap.price == pytest.approx(180.0 * 0.9995)
    assert apply_stops(acct, {}, {})[1] == []                      # нет данных по бумаге: стоп не трогаем

def test_stop_with_missing_or_bad_open_fills_at_the_stop():
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    for opens in ({}, {"AAPL": math.nan}, {"AAPL": 0.0}):
        _, (f,) = apply_stops(acct, {"AAPL": 178.0}, opens)
        assert f.price == pytest.approx(186.0 * 0.9995)

def test_stop_with_bad_low_is_left_alone():
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    for low in (math.nan, math.inf, 0.0, -1.0):
        same, fills = apply_stops(acct, {"AAPL": low}, {"AAPL": 180.0})
        assert fills == [] and same == acct

def test_peak_only_goes_up():
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    assert mark_peak(acct, {"AAPL": 260.0}).peak_equity > 1000.0
    assert mark_peak(acct, {"AAPL": 190.0}).peak_equity == 1000.0

def test_buy_with_a_bad_stop_raises():
    for stop in (math.nan, math.inf, 0.0, -1.0, True):
        with pytest.raises(ValueError):
            apply_buy(START, "AAPL", 150.0, 200.0, stop)

def test_buy_cannot_spend_more_than_the_cash():
    with pytest.raises(ValueError):
        apply_buy(START, "AAPL", 5000.0, 200.0, 186.0)
    assert START == Account(1000.0, (), 1000.0)
    all_in, _ = apply_buy(START, "AAPL", 1000.0, 200.0, 186.0)
    assert all_in.cash == 0.0
