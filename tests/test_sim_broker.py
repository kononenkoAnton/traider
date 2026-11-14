import pytest

from tests.fakes import ny
from tests.broker_contract import run_contract
from traider.brokers import NoPrice, SimBroker
from traider.cage import Account


def test_contract(env):
    cfg, store, market = env
    run_contract(lambda: SimBroker(cfg, store, market), market)

def test_every_change_is_in_the_ledger(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    b.close("2026-12-01", "AAPL", 1.0, "причина", "x/2")
    events = store.orders()
    assert [e["kind"] for e in events] == ["buy", "raise_stop", "close"]
    assert events[0]["client_order_id"] == "x/1" and events[0]["thesis"] == "тезис" and events[0]["stop_price"] == 186.0

def test_buy_without_a_price_raises_and_changes_nothing(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    with pytest.raises(NoPrice):
        b.buy("2026-12-01", "MSFT", 100.0, 90.0, "тезис", "x/1")
    assert b.account().cash == 1000.0 and store.orders() == []

def test_crash_between_ledger_and_account_does_not_double_fill(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    store.save_account(Account(1000.0, (), 1000.0))                 # как будто счёт не успел сохраниться
    again = SimBroker(cfg, store, market).buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert again.qty == pytest.approx(150.0 / 200.10)
    assert SimBroker(cfg, store, market).account().cash == pytest.approx(850.0)
    assert len(store.orders()) == 1

def test_stale_account_is_repaired_from_the_ledger_for_any_call(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    store.save_account(Account(1000.0, (), 1000.0))                 # счёт отстал от журнала
    acct = SimBroker(cfg, store, market).account()
    assert acct.cash == pytest.approx(850.0) and acct.positions[0].stop_price == 190.0

def test_sync_fills_stops_retroactively(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert b.sync("2026-12-02") == []
    market.lows = {"AAPL": (178.0, 180.0)}                          # гэп ниже стопа, пока машина спала
    (event,) = b.sync("2026-12-02")
    assert event["kind"] == "stop_fill" and event["price"] == pytest.approx(180.0 * 0.9995)
    assert b.account().positions == () and b.sync("2026-12-02") == []

def test_account_carries_the_peak(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 200.0, 186.0, "тезис", "x/1")
    market.prices["AAPL"] = 300.0
    assert b.account().peak_equity > 1090.0
    market.prices["AAPL"] = 200.0
    assert SimBroker(cfg, store, market).account().peak_equity > 1090.0

def test_ensure_stops_is_a_no_op_for_the_built_in_account(env):
    cfg, store, market = env
    assert SimBroker(cfg, store, market).ensure_stops() == []

def test_liquidate_sells_at_entry_when_there_is_no_price(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    del market.prices["AAPL"]
    fills = b.liquidate("2026-12-01")
    assert sorted(f.symbol for f in fills) == ["AAPL", "XLE"]
    assert SimBroker(cfg, store, market).account().positions == ()
    assert [e["kind"] for e in store.orders()][-2:] == ["liquidate", "liquidate"]

def test_raise_stop_on_unheld_symbol_raises(env):
    cfg, store, market = env
    with pytest.raises(KeyError):
        SimBroker(cfg, store, market).raise_stop("2026-12-01", "AAPL", 190.0)
    assert store.orders() == []

def _at(market, *args):
    market.clock = ny(2026, 12, 1, *args)

def test_low_before_the_buy_gives_no_stop_fill(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    _at(market, 10); b.sync("2026-12-01")
    _at(market, 12); b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.lows = {"AAPL": (185.0, 190.0, ny(2026, 12, 1, 11))}
    _at(market, 13)
    assert b.sync("2026-12-01") == [] and len(b.account().positions) == 1

def test_low_after_the_buy_gives_a_stop_fill(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    _at(market, 10); b.sync("2026-12-01")
    _at(market, 12); b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.lows = {"AAPL": (185.0, 190.0, ny(2026, 12, 1, 12, 30))}
    _at(market, 13)
    assert [e["kind"] for e in b.sync("2026-12-01")] == ["stop_fill"]

def test_low_before_the_stop_was_raised_does_not_count(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    _at(market, 10); b.buy("2026-12-01", "AAPL", 150.0, 180.0, "тезис", "x/1")
    _at(market, 12); b.raise_stop("2026-12-01", "AAPL", 190.0)
    market.lows = {"AAPL": (185.0, 190.0, ny(2026, 12, 1, 11))}
    _at(market, 13)
    assert b.sync("2026-12-01") == []

def test_old_position_is_stopped_by_a_low_after_last_sync(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.clock = ny(2026, 12, 2, 10); b.sync("2026-12-02")
    market.lows = {"AAPL": (180.0, 190.0, ny(2026, 12, 2, 11))}
    market.clock = ny(2026, 12, 2, 12)
    assert [e["kind"] for e in b.sync("2026-12-02")] == ["stop_fill"]

def test_liquidate_treats_a_nan_quote_like_a_missing_one(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    market.prices["AAPL"] = float("nan")
    assert sorted(f.symbol for f in b.liquidate("2026-12-01")) == ["AAPL", "XLE"]
    assert SimBroker(cfg, store, market).account().positions == ()
