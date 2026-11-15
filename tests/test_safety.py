import pytest

from traider.brokers import SimBroker
from traider.safety import run_safety


def test_healthy_run_reports_hp(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 200.0, 186.0, "тезис", "x/1")
    r = run_safety(cfg, store, b, "2026-12-01")
    assert r.died is False and r.floor == pytest.approx(850.0, abs=0.2) and r.hp == pytest.approx(r.equity - r.floor)
    assert store.state()["status"] == "active"

def test_run_below_the_floor_is_liquidated_and_marked_dead(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 1000.0, 1.0, "тезис", "x/1")   # стоп далеко, чтобы сработал именно пол
    market.prices["AAPL"] = 169.0                              # капитал ниже 850
    r = run_safety(cfg, store, b, "2026-12-02")
    assert r.died is True and store.state()["status"] == "dead" and store.state()["died_on"] == "2026-12-02"
    assert b.account().positions == () and store.orders()[-1]["kind"] == "liquidate"
    assert len(store.orders()) == 2
    again = run_safety(cfg, store, b, "2026-12-02")
    assert again.died is False and len(store.orders()) == 2

def test_floor_ratchets_up_with_the_peak(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 200.0, 186.0, "тезис", "x/1")
    market.prices["AAPL"] = 300.0
    high = run_safety(cfg, store, b, "2026-12-02")
    market.prices["AAPL"] = 200.0
    low = run_safety(cfg, store, b, "2026-12-03")
    assert low.floor == pytest.approx(high.floor) and low.floor > 900.0

def test_stop_fills_found_by_sync_are_reported(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.lows = {"AAPL": (185.0, 199.0)}
    assert run_safety(cfg, store, b, "2026-12-02").stop_fills == ("AAPL",)

@pytest.mark.parametrize("status", ["finishing", "invalid"])
def test_finishing_run_is_still_checked(env, status):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 1000.0, 1.0, "тезис", "x/1")
    market.prices["AAPL"] = 169.0
    store.update_state(status=status)
    assert run_safety(cfg, store, b, "2026-12-02").died is True
    assert store.state()["status"] == "dead"

def test_finished_run_is_a_noop(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 1000.0, 1.0, "тезис", "x/1")
    market.prices["AAPL"] = 169.0
    store.update_state(status="finished")
    r = run_safety(cfg, store, b, "2026-12-02")
    assert (r.died, r.equity, r.floor, r.hp, r.stops_placed, r.stop_fills) == (False, 0.0, 0.0, 0.0, (), ())
    assert len(store.orders()) == 1 and store.state()["status"] == "finished"

def test_held_symbols_without_a_quote_are_reported(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    assert run_safety(cfg, store, b, "2026-12-01").unpriced == ()
    del market.prices["AAPL"]
    market.prices["XLE"] = float("nan")
    r = run_safety(cfg, store, b, "2026-12-01")
    assert sorted(r.unpriced) == ["AAPL", "XLE"] and r.died is False


# --- финальная волна, I3: без котировок гибель не решается ---

def test_no_death_while_a_holding_has_no_quote(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    symbols = ["AAPL", "XLE", "MSFT", "NVDA", "AMZN"]
    for i, s in enumerate(symbols):
        market.prices[s] = 100.0
        b.buy("2026-12-01", s, 200.0, 90.0, "тезис", f"x/{i}")
    for s in symbols:
        market.prices[s] = 122.0                                    # каждая +22%: пик около 1220, пол около 1037
    assert run_safety(cfg, store, b, "2026-12-01").floor > 1030.0
    market.prices.clear()                                           # утро: сделок сегодня ещё не было
    stops = []
    b.ensure_stops = lambda: stops.append(1) or []
    r = run_safety(cfg, store, b, "2026-12-02")
    assert r.died is False and sorted(r.unpriced) == sorted(symbols) and r.equity < r.floor
    assert store.state()["status"] == "active" and len(b.account().positions) == 5 and stops == [1]
    for s in symbols:
        market.prices[s] = 121.0                                    # котировки вернулись, капитал выше пола
    assert run_safety(cfg, store, b, "2026-12-02").died is False and store.state()["status"] == "active"
