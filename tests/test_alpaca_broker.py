import pytest

from tests.broker_contract import run_contract, run_refusal_contract
from tests.fakes import FakeAlpacaApi, FakeMarket
from traider.brokers import AlpacaBroker, BrokerRejected, NoPrice, make_alpaca_api
from traider.config import load_run, start_experiment
from traider.store import RunStore


@pytest.fixture
def env(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-a"
    cfg, _ = load_run(run)
    market = FakeMarket(prices={"AAPL": 200.0, "XLE": 100.0})
    api = FakeAlpacaApi(cash=1000.0, market=market)       # исполняет заявки по ценам рынка, если тест не задал иное
    return cfg, RunStore(run), market, api


def broker(env):
    cfg, store, market, api = env
    return AlpacaBroker(cfg, store, market, api, poll_seconds=0.05, poll_interval=0.01)


def test_contract(env):
    run_contract(lambda: broker(env), env[2])


def test_refusal_contract(env):
    run_refusal_contract(lambda: broker(env), env[2], close_needs_quote=False)


def test_buy_places_a_stop_for_the_filled_quantity(env):
    b = broker(env); api = env[3]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(fill.qty)
    assert api.open_stop_orders()["AAPL"]["stop_price"] == 186.0
    assert env[1].orders()[-1]["broker_order_id"] is not None


def test_partial_fill_is_recorded_as_it_happened(env):
    b = broker(env); api = env[3]
    api.next_fill_fraction = 0.4                                     # исполнилось 40%, остаток отменён
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert fill.qty == pytest.approx(0.4 * 150.0 / 200.0)
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(fill.qty)
    assert env[1].orders()[-1]["qty"] == pytest.approx(fill.qty)
    assert api.pending_orders() == []                                # остаток отменён


@pytest.mark.parametrize("mode", ["reject", "never_fills", "error"])
def test_unfilled_buy_raises_and_leaves_nothing_behind(env, mode):
    b = broker(env); api = env[3]
    api.next_order_mode = mode
    with pytest.raises(BrokerRejected):
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert api.open_stop_orders() == {} and env[1].orders() == [] and api.pending_orders() == []
    assert env[1].state()["stops"] == {}


def test_ensure_stops_places_missing_and_replaces_wrong_ones(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert b.ensure_stops() == []
    api.expire_day_orders()                                          # DAY-стопы сгорели на закрытии
    assert b.ensure_stops() == ["AAPL"] and api.open_stop_orders()["AAPL"]["stop_price"] == 186.0
    api.open_stop_orders()["AAPL"]["qty"] = 0.1                      # стоит стоп не на то количество
    assert b.ensure_stops() == ["AAPL"]


def test_sync_records_each_filled_stop_once(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.trigger_stop("AAPL", price=185.4)
    (event,) = b.sync("2026-12-02")
    assert (event["kind"], event["price"]) == ("stop_fill", 185.4)
    assert b.sync("2026-12-02") == [] and b.account().positions == () and "AAPL" not in store.state()["stops"]


def test_close_cancels_the_stop_and_restores_it_for_the_rest(env):
    b = broker(env); api = env[3]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(fill.qty / 2)
    b.close("2026-12-01", "AAPL", 1.0, "причина", "x/3")
    assert api.open_stop_orders() == {}


def test_paper_is_the_default(example, tmp_path, monkeypatch):
    cfg, _ = load_run(start_experiment(example, tmp_path / "runs") / "opus-a")
    seen = {}
    monkeypatch.setattr("traider.brokers.AlpacaSdkApi", lambda key, secret, paper: seen.update(paper=paper))
    make_alpaca_api(cfg, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert seen == {"paper": True}


def test_live_mode_needs_its_own_keys(example_live, tmp_path):
    cfg, _ = load_run(start_experiment(example_live, tmp_path / "runs", live=True) / "opus-a")
    with pytest.raises(ValueError, match="ALPACA_LIVE_API_KEY"):
        make_alpaca_api(cfg, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})


def test_live_mode_uses_live_keys_and_not_paper(example_live, tmp_path, monkeypatch):
    cfg, _ = load_run(start_experiment(example_live, tmp_path / "runs", live=True) / "opus-a")
    seen = {}
    monkeypatch.setattr("traider.brokers.AlpacaSdkApi", lambda key, secret, paper: seen.update(k=key, s=secret, p=paper))
    make_alpaca_api(cfg, {"ALPACA_LIVE_API_KEY": "lk", "ALPACA_LIVE_SECRET_KEY": "ls"})
    assert seen == {"k": "lk", "s": "ls", "p": False}


def test_paper_mode_names_the_missing_key(example, tmp_path):
    cfg, _ = load_run(start_experiment(example, tmp_path / "runs") / "opus-a")
    with pytest.raises(ValueError, match="ALPACA_SECRET_KEY"):
        make_alpaca_api(cfg, {"ALPACA_API_KEY": "k"})


# --- отказы: ничего не отправлено и ничего не записано ---

def test_refusals_send_nothing_and_write_nothing(env):
    b = broker(env); api, store, market = env[3], env[1], env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    sent, ledger = api.submitted, store.orders()
    market.prices["MSFT"] = float("nan")
    for sym in ("MSFT", "QQQ"):
        with pytest.raises(NoPrice):
            b.buy("2026-12-01", sym, 100.0, 90.0, "тезис", f"x/{sym}")
    for call in (lambda: b.close("2026-12-01", "XLE", 1.0, "нет позиции", "x/2"),
                 lambda: b.close("2026-12-01", "AAPL", 0.0, "доля", "x/3"),
                 lambda: b.close("2026-12-01", "AAPL", 1.5, "доля", "x/3"),
                 lambda: b.raise_stop("2026-12-01", "AAPL", float("nan")),
                 lambda: b.raise_stop("2026-12-01", "XLE", 90.0),
                 lambda: b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/1")):
        with pytest.raises(BrokerRejected):
            call()
    assert api.submitted == sent and store.orders() == ledger


def test_buy_with_bad_numbers_or_too_big_sum_raises_value_error_before_sending(env):
    b = broker(env); api = env[3]
    for notional, stop in ((0.0, 90.0), (float("nan"), 90.0), (100.0, -1.0), (5000.0, 90.0)):
        with pytest.raises(ValueError):
            b.buy("2026-12-01", "AAPL", notional, stop, "тезис", "x/1")
    assert api.submitted == 0 and env[1].orders() == []


def test_sale_does_not_need_our_quote(env):
    b = broker(env); api, market = env[3], env[2]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.quotes["AAPL"] = 200.0                                       # у брокера цена есть, у нашей ленты нет
    del market.prices["AAPL"]
    half = b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    assert half.qty == pytest.approx(fill.qty / 2)
    assert [f.symbol for f in b.liquidate("2026-12-01")] == ["AAPL"]
    assert b.account().positions == () and api.open_stop_orders() == {}


# --- события журнала ---

def test_ledger_events_carry_the_same_fields_as_the_built_in_account(env):
    b = broker(env); store, market = env[1], env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    b.liquidate("2026-12-01")
    buy, raised, close, liq = store.orders()
    assert raised["price"] == 200.0 and raised["stop_price"] == 190.0
    assert close["fraction"] == 0.5 and close["entry_price"] == pytest.approx(200.0)
    assert liq["fraction"] == 1.0 and liq["entry_price"] == pytest.approx(200.0)
    for e in (buy, raised, close, liq):
        assert e["ts"].startswith("2026-12-01T10:00") and "broker_order_id" in e
    assert [e["broker_order_id"] is not None for e in (buy, close, liq)] == [True] * 3
    assert raised["broker_order_id"] is None
    assert buy["notional"] == pytest.approx(150.0)


def test_stop_fill_event_has_entry_price_and_broker_order_id(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.trigger_stop("AAPL", price=185.0)
    (e,) = b.sync("2026-12-02")
    assert e["entry_price"] == pytest.approx(200.0) and e["broker_order_id"] and e["reason"] == "stop"
    assert e["stop_price"] == 186.0 and e["qty"] == pytest.approx(0.75)
    assert env[1].state()["last_sync"] is not None


def test_sync_does_not_forget_a_stop_of_a_position_bought_again(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.trigger_stop("AAPL", price=185.0)
    b.buy("2026-12-01", "AAPL", 100.0, 180.0, "тезис", "x/2")        # докупили до сверки
    assert len(b.sync("2026-12-01")) == 1
    assert store.state()["stops"] == {"AAPL": 180.0} and api.open_stop_orders()["AAPL"]["stop_price"] == 180.0


# --- account() ---

def test_account_uses_broker_cash_and_entry_price_and_state_stops(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.positions["AAPL"][1] = 199.0                                 # средняя цена входа у брокера
    acct = b.account()
    assert acct.cash == pytest.approx(850.0) and acct.positions[0].entry_price == 199.0
    assert acct.positions[0].stop_price == 186.0 and acct.peak_equity == pytest.approx(1000.0)


def test_account_raises_the_peak_only_when_every_held_symbol_has_a_quote(env):
    b = broker(env); market = env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.prices["AAPL"] = 400.0
    assert b.account().peak_equity == pytest.approx(850.0 + 0.75 * 400.0)
    peak = b.account().peak_equity
    del market.prices["AAPL"]
    market.prices["AAPL"] = float("nan")
    assert b.account().peak_equity == peak                          # нет котировки: пик не трогаем
    market.prices["AAPL"] = 1000.0
    assert b.account().peak_equity == pytest.approx(850.0 + 0.75 * 1000.0)


def test_unknown_position_is_shown_without_a_stop(env):
    b = broker(env); api = env[3]
    api.positions["XLE"] = [2.0, 90.0]                               # куплено не нами
    (p,) = b.account().positions
    assert (p.symbol, p.stop_price) == ("XLE", 0.0)


# --- стопы ---

def test_stop_price_is_rounded_to_the_tick_the_broker_accepts(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.raise_stop("2026-12-01", "AAPL", 190.4567)
    assert env[1].state()["stops"]["AAPL"] == 190.46
    assert api.open_stop_orders()["AAPL"]["stop_price"] == 190.46
    assert b.ensure_stops() == []                                    # округлённый стоп не считается «не тем»


def test_buying_more_moves_the_stop_to_the_whole_position(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "AAPL", 100.0, 188.0, "тезис", "x/2")
    stop = api.open_stop_orders()["AAPL"]
    assert stop["qty"] == pytest.approx(1.0) and stop["stop_price"] == 188.0


def test_failed_stop_does_not_undo_the_buy_and_the_tick_restores_it(env):
    b = broker(env); api, store = env[3], env[1]
    api.fail_next_stop = True
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert fill.qty == pytest.approx(0.75) and len(store.orders()) == 1
    assert api.open_stop_orders() == {}
    assert b.ensure_stops() == ["AAPL"] and api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(0.75)


def test_ensure_stops_goes_on_after_one_symbol_fails(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    api.expire_day_orders()
    api.fail_stop_for = "AAPL"
    with pytest.raises(BrokerRejected):
        b.ensure_stops()
    assert "XLE" in api.open_stop_orders() and "AAPL" not in api.open_stop_orders()


def test_failed_sale_restores_the_stop_before_the_error_propagates(env):
    b = broker(env); api = env[3]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.next_order_mode = "reject"                                   # продажа отклонена после отмены стопа
    with pytest.raises(BrokerRejected):
        b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(fill.qty)
    assert len(env[1].orders()) == 1                                 # только покупка
    assert b.close("2026-12-01", "AAPL", 0.5, "причина", "x/3").qty == pytest.approx(fill.qty / 2)


def test_partial_sale_is_recorded_as_it_happened_and_stop_covers_the_rest(env):
    b = broker(env); api, store = env[3], env[1]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.next_fill_fraction = 0.5
    sold = b.close("2026-12-01", "AAPL", 1.0, "причина", "x/2")
    assert sold.qty == pytest.approx(fill.qty / 2)
    assert store.orders()[-1]["fraction"] == pytest.approx(0.5)
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(fill.qty / 2)
    assert store.state()["stops"] == {"AAPL": 186.0}


# --- liquidate ---

def test_liquidate_goes_on_after_one_sale_fails_and_raises_afterwards(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    api.reject_sales_of = {"AAPL"}
    with pytest.raises(BrokerRejected) as err:
        b.liquidate("2026-12-01")
    assert [f.symbol for f in err.value.fills] == ["XLE"]
    assert [p.symbol for p in b.account().positions] == ["AAPL"]
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(0.5)   # стоп оставшейся бумаги на месте
    assert [e["kind"] for e in store.orders()].count("liquidate") == 1
    api.reject_sales_of = set()
    assert [f.symbol for f in b.liquidate("2026-12-01")] == ["AAPL"]       # повтор доводит дело до конца
    assert b.account().positions == () and api.open_stop_orders() == {}


# --- сбой процесса: тот же buy с тем же client_order_id ---

def test_crash_after_order_sent_but_not_filled(env):
    b = broker(env); api = env[3]
    api.next_order_mode = "never_fills"
    api.submit_market_buy("AAPL", 150.0, "x/1")                      # процесс умер сразу после отправки
    with pytest.raises(BrokerRejected):                              # повтор нашёл заявку, не отправил вторую
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert api.submitted == 1 and api.pending_orders() == [] and env[1].orders() == []
    assert api.open_stop_orders() == {} and env[1].state()["stops"] == {}


def test_crash_after_fill_before_stop(env):
    b = broker(env); api, store = env[3], env[1]
    api.submit_market_buy("AAPL", 150.0, "x/1")                      # заявка исполнилась, стопа нет
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert api.submitted == 2                                        # покупка и стоп; вторая покупка не отправлялась
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(fill.qty)
    assert len(store.orders()) == 1 and store.orders()[0]["qty"] == pytest.approx(0.75)
    assert api.cash_and_positions()[0] == pytest.approx(850.0)


def test_crash_after_stop_before_ledger_event_then_repeat(env):
    b = broker(env); api, store = env[3], env[1]
    real = store.append_order

    def die(event, ts=None):
        raise KeyboardInterrupt                                      # процесс убит при записи события

    store.append_order = die
    with pytest.raises(KeyboardInterrupt):
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    store.append_order = real
    assert store.orders() == [] and api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(0.75)
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert fill.qty == pytest.approx(0.75) and len(store.orders()) == 1
    assert api.submitted == 2 and len(api.open_stop_orders()) == 1  # ни второй покупки, ни второго стопа
    again = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert again.qty == pytest.approx(fill.qty) and api.submitted == 2 and len(store.orders()) == 1


def test_lost_reply_to_the_order_is_recovered_by_client_order_id(env):
    b = broker(env); api = env[3]
    api.next_order_mode = "lost_reply"                               # заявка принята, ответ потерян
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert fill.qty == pytest.approx(0.75) and api.submitted == 2


def test_network_error_without_an_order_is_not_a_rejection(env):
    b = broker(env); api = env[3]
    api.next_order_mode = "network_down"
    with pytest.raises(ConnectionError):
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert env[1].orders() == [] and env[1].state()["stops"] == {}


def test_crash_after_sale_before_ledger_event_then_repeat(env):
    b = broker(env); api, store = env[3], env[1]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.cancel(api.open_stop_orders()["AAPL"]["id"])
    api.submit_market_sell("AAPL", fill.qty, "x/2")                  # продажа ушла, события нет
    sold = b.close("2026-12-01", "AAPL", 1.0, "причина", "x/2")      # позиции уже нет, заявка найдена по id
    assert sold.qty == pytest.approx(fill.qty)
    last = store.orders()[-1]
    assert last["kind"] == "close" and last["entry_price"] == pytest.approx(200.0)
    assert api.open_stop_orders() == {} and "AAPL" not in store.state()["stops"]
    assert api.submitted == 3
