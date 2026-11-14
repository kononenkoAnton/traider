from dataclasses import replace

import pytest

from traider.cage import (Account, BuyRequest, Position, SessionFacts, check_buy,
                          check_raise_stop, equity, floor, hp, is_dead, open_risk)
from traider.config import Limits

LIM = Limits()
UNIVERSE = frozenset({"AAPL", "XLE", "SPY"})
PRICES = {"AAPL": 200.0, "XLE": 100.0}
EMPTY = Account(cash=1000.0, positions=(), peak_equity=1000.0)
FACTS = SessionFacts(0, frozenset(), frozenset({"AAPL", "XLE"}), True)


def code(symbol="AAPL", notional=150.0, stop=186.0, acct=EMPTY, facts=FACTS, prices=PRICES):
    r = check_buy(BuyRequest(symbol, notional, stop), acct, prices, facts, UNIVERSE, LIM)
    return r.code if r else None


def test_valid_buy_is_accepted():
    assert code() is None


@pytest.mark.parametrize("kwargs, expected", [
    (dict(notional=float("nan")), "bad_request"),
    (dict(notional=-5.0), "bad_request"),
    (dict(stop=0.0), "bad_request"),
    (dict(symbol="TSLA"), "not_in_universe"),
    (dict(symbol="SPY"), "no_price"),
    (dict(facts=replace(FACTS, panel_done=False)), "no_panel_forecasts"),
    (dict(facts=replace(FACTS, forecast_symbols=frozenset())), "no_symbol_forecast"),
    (dict(facts=replace(FACTS, buys_done=3)), "too_many_buys"),
    (dict(facts=replace(FACTS, closed_today=frozenset({"AAPL"}))), "rebuy_same_day"),
    (dict(notional=19.99), "order_too_small"),
    (dict(stop=195.0), "stop_out_of_range"),
    (dict(stop=169.0), "stop_out_of_range"),
    (dict(notional=200.01), "position_too_large"),
])
def test_rejections(kwargs, expected):
    assert code(**kwargs) == expected


@pytest.mark.parametrize("kwargs", [dict(notional=20.0), dict(notional=200.0), dict(stop=194.0), dict(stop=170.0)])
def test_boundaries_are_inclusive(kwargs):
    assert code(**kwargs) is None


def test_not_enough_cash():
    acct = Account(100.0, (Position("XLE", 9.0, 100.0, 90.0),), 1000.0)   # капитал 1000, денег 100
    assert code(notional=150.0, acct=acct) == "not_enough_cash"


def test_open_risk_limit_is_8_percent_of_equity():
    acct = Account(500.0, (Position("XLE", 5.0, 100.0, 88.0),), 1000.0)   # риск 60 при лимите 80
    assert code(notional=200.0, stop=170.0, acct=acct) == "open_risk_too_high"   # +30
    assert code(notional=100.0, stop=170.0, acct=acct) is None                   # +15


def test_add_on_cannot_lower_the_stop():
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, 186.0),), 1000.0)
    assert code(notional=50.0, stop=180.0, acct=acct) == "stop_below_existing"
    assert code(notional=50.0, stop=190.0, acct=acct) is None


def test_floor_hp_and_death():
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, 186.0),), 1040.0)
    assert floor(acct, LIM) == pytest.approx(884.0)
    assert hp(acct, {"AAPL": 200.0}, LIM) == pytest.approx(116.0)
    assert is_dead(acct, {"AAPL": 200.0}, LIM) is False
    assert is_dead(Account(884.0, (), 1040.0), {}, LIM) is False      # ровно на полу: жив
    assert is_dead(Account(883.99, (), 1040.0), {}, LIM) is True      # строго ниже пола: погиб
    assert equity(acct, {}) == pytest.approx(1000.0)                  # нет цены: считаем по цене входа


def test_open_risk_ignores_positions_already_below_stop():
    acct = Account(0.0, (Position("AAPL", 1.0, 200.0, 186.0),), 1000.0)
    assert open_risk(acct, {"AAPL": 180.0}) == 0.0
    assert open_risk(acct, {"AAPL": 200.0}) == pytest.approx(14.0)


@pytest.mark.parametrize("symbol, new_stop, expected", [
    ("XLE", 95.0, "no_position"), ("AAPL", 186.0, "stop_not_higher"),
    ("AAPL", 195.0, "stop_out_of_range"), ("AAPL", float("inf"), "bad_request"), ("AAPL", 190.0, None),
])
def test_raise_stop(symbol, new_stop, expected):
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, 186.0),), 1000.0)
    r = check_raise_stop(symbol, new_stop, acct, PRICES, LIM)
    assert (r.code if r else None) == expected


def test_position_too_large_detail_names_the_limit():
    r = check_buy(BuyRequest("AAPL", 250.0, 186.0), EMPTY, PRICES, FACTS, UNIVERSE, LIM)
    assert "20%" in r.detail


@pytest.mark.parametrize("bad", [float("nan"), float("inf")])
def test_bad_quote_of_other_held_symbol_values_it_at_entry(bad):
    acct = Account(300.0, (Position("XLE", 7.0, 100.0, 90.0),), 1000.0)   # по цене входа капитал 1000
    prices = {"AAPL": 200.0, "XLE": bad}
    assert equity(acct, prices) == pytest.approx(equity(acct, {}))
    assert code(notional=250.0, acct=acct, prices=prices) == "position_too_large"


def test_nan_cash_is_bad_request_and_never_alive_or_dead():
    acct = Account(float("nan"), (), 1000.0)
    assert code(notional=5000.0, acct=acct) == "bad_request"
    for fn in (hp, is_dead):
        with pytest.raises(ValueError):
            fn(acct, {}, LIM)
    with pytest.raises(ValueError):
        equity(acct, {})


def test_nan_stop_on_held_position_is_bad_request():
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, float("nan")),), 1000.0)
    assert code(acct=acct) == "bad_request"
    r = check_raise_stop("AAPL", 190.0, acct, PRICES, LIM)
    assert r.code == "bad_request"
