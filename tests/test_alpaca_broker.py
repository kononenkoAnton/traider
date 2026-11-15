import pytest

from tests.broker_contract import run_contract, run_refusal_contract
from tests.fakes import FakeAlpacaApi, FakeMarket
from traider.config import load_run as _load_run
from traider.alpaca import AlpacaBroker, make_alpaca_api
from traider.brokers import BrokerRejected, NoPrice
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
    monkeypatch.setattr("traider.alpaca.AlpacaSdkApi", lambda key, secret, paper: seen.update(paper=paper))
    make_alpaca_api(cfg, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})
    assert seen == {"paper": True}


def test_live_mode_needs_its_own_keys(example_live, tmp_path):
    cfg, _ = load_run(start_experiment(example_live, tmp_path / "runs", live=True) / "opus-a")
    with pytest.raises(ValueError, match="ALPACA_LIVE_API_KEY"):
        make_alpaca_api(cfg, {"ALPACA_API_KEY": "k", "ALPACA_SECRET_KEY": "s"})


def test_live_mode_uses_live_keys_and_not_paper(example_live, tmp_path, monkeypatch):
    cfg, _ = load_run(start_experiment(example_live, tmp_path / "runs", live=True) / "opus-a")
    seen = {}
    monkeypatch.setattr("traider.alpaca.AlpacaSdkApi", lambda key, secret, paper: seen.update(k=key, s=secret, p=paper))
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
    api.submit_market_buy("AAPL", 150.0, b.client_id("x/1"))                      # процесс умер сразу после отправки
    with pytest.raises(BrokerRejected):                              # повтор нашёл заявку, не отправил вторую
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert api.submitted == 1 and api.pending_orders() == [] and env[1].orders() == []
    assert api.open_stop_orders() == {} and env[1].state()["stops"] == {}


def test_crash_after_fill_before_stop(env):
    b = broker(env); api, store = env[3], env[1]
    store.update_state(stops={"AAPL": 186.0})                        # намерение записывается до заявки
    api.submit_market_buy("AAPL", 150.0, b.client_id("x/1"))                      # заявка исполнилась, стопа нет
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
    api.submit_market_sell("AAPL", fill.qty, b.client_id("x/2"))                  # продажа ушла, события нет
    sold = b.close("2026-12-01", "AAPL", 1.0, "причина", "x/2")      # позиции уже нет, заявка найдена по id
    assert sold.qty == pytest.approx(fill.qty)
    last = store.orders()[-1]
    assert last["kind"] == "close" and last["entry_price"] == pytest.approx(200.0)
    assert api.open_stop_orders() == {} and "AAPL" not in store.state()["stops"]
    assert api.submitted == 3


# --- намерение раньше действия ---

def test_desired_stop_is_on_record_before_the_order_is_sent(env):
    b = broker(env); api, store = env[3], env[1]
    seen = {}
    real = api.submit_market_buy

    def spy(*a):
        seen["stops"] = store.state()["stops"]
        return real(*a)

    api.submit_market_buy = spy
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert seen["stops"] == {"AAPL": 186.0}


def test_process_dies_after_the_fill_and_the_tick_places_the_stop(env):
    b = broker(env); api, store = env[3], env[1]

    def die(oid):
        raise KeyboardInterrupt

    b._finish = die                                                  # умер после исполнения, buy больше не зовут
    with pytest.raises(KeyboardInterrupt):
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert store.state()["stops"] == {"AAPL": 186.0} and api.open_stop_orders() == {}
    assert broker(env).ensure_stops() == ["AAPL"]
    assert api.open_stop_orders()["AAPL"]["stop_price"] == 186.0


def test_failed_buy_keeps_the_stop_of_an_existing_position(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    api.next_order_mode = "reject"
    with pytest.raises(BrokerRejected):
        b.buy("2026-12-01", "AAPL", 100.0, 190.0, "тезис", "x/2")
    assert store.state()["stops"] == {"AAPL": 186.0}


# --- финальная волна, C1: счёт принадлежит одному прогону ---

@pytest.mark.parametrize("call", ["ensure_stops", "account", "sync", "liquidate", "buy", "close", "raise_stop"])
def test_a_position_without_a_stop_on_record_stops_every_operation(env, call):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    api.expire_day_orders()                                          # стоп AAPL сгорел: ensure_stops хотел бы его вернуть
    api.positions["XLE"] = [2.0, 120.0]                              # куплено не этим прогоном
    sent, ledger, state = api.submitted, store.orders(), store.state()
    calls = {"ensure_stops": b.ensure_stops, "account": b.account, "sync": lambda: b.sync("2026-12-01"),
             "liquidate": lambda: b.liquidate("2026-12-01"),
             "buy": lambda: b.buy("2026-12-01", "AAPL", 50.0, 186.0, "тезис", "x/2"),
             "close": lambda: b.close("2026-12-01", "AAPL", 1.0, "причина", "x/3"),
             "raise_stop": lambda: b.raise_stop("2026-12-01", "AAPL", 190.0)}
    with pytest.raises(RuntimeError, match="XLE.*ничего не отправлено"):
        calls[call]()
    assert api.submitted == sent and api.replaced == 0 and store.orders() == ledger
    assert store.state()["stops"] == state["stops"] == {"AAPL": 186.0}
    assert api.positions["XLE"] == [2.0, 120.0]                      # чужое не продано


@pytest.mark.parametrize("dirty", ["default_paper_balance", "position", "open_order"])
def test_first_use_refuses_an_account_that_is_not_this_runs_alone(env, dirty):
    b = broker(env); api, store = env[3], env[1]
    if dirty == "default_paper_balance":
        api.cash = 100_000.0
    elif dirty == "position":
        api.positions["XLE"] = [2.0, 90.0]
    else:
        api.next_order_mode = "never_fills"
        api.submit_market_buy("AAPL", 100.0, "руками/1")
    sent = api.submitted
    for _ in range(2):                                               # проверка повторяется, пока счёт не чист
        with pytest.raises(RuntimeError, match="run_uid не записан"):
            b.sync("2026-12-01")
        assert "run_uid" not in store.state() and api.submitted == sent


def test_first_use_accepts_a_clean_account_within_two_percent(env):
    b = broker(env); api, store = env[3], env[1]
    api.cash = 1019.0
    assert b.sync("2026-12-01") == [] and len(store.state()["run_uid"]) == 8


# --- гэп через стоп ---

def test_quote_at_or_below_the_stop_sells_at_market_instead_of_placing_a_stop(env):
    b = broker(env); api, store, market = env[3], env[1], env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.expire_day_orders()
    market.prices["AAPL"] = 180.0
    assert b.ensure_stops() == []
    assert api.positions == {} and api.open_stop_orders() == {} and "AAPL" not in store.state()["stops"]
    fills = [e for e in store.orders() if e["kind"] == "stop_fill"]
    assert len(fills) == 1
    e = fills[0]
    assert (e["reason"], e["price"], e["stop_price"], e["symbol"]) == ("gap_below_stop", 180.0, 186.0, "AAPL")
    assert e["qty"] == pytest.approx(0.75) and e["entry_price"] == pytest.approx(200.0) and e["broker_order_id"]
    assert b.ensure_stops() == [] and len(store.orders()) == 2


def test_live_stop_is_left_alone_even_if_the_quote_is_below_it(env):
    b = broker(env); api, market = env[3], env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.prices["AAPL"] = 180.0                                    # стоп у брокера стоит и сработает сам
    assert b.ensure_stops() == [] and "AAPL" in api.positions and "AAPL" in api.open_stop_orders()


def test_refused_stop_with_the_quote_above_it_raises_with_the_brokers_message(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.expire_day_orders()
    api.fail_stop_for = "AAPL"
    with pytest.raises(BrokerRejected, match="стоп отклонён"):
        b.ensure_stops()
    assert "AAPL" in api.positions


def test_without_a_quote_the_stop_is_placed_as_before(env):
    b = broker(env); api, market = env[3], env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.expire_day_orders()
    del market.prices["AAPL"]
    assert b.ensure_stops() == ["AAPL"] and "AAPL" in api.positions


# --- сверка с брокером ---

def test_sync_recovers_a_liquidation_whose_event_was_never_written(env):
    b = broker(env); api, store = env[3], env[1]
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.cancel(api.open_stop_orders()["AAPL"]["id"])
    api.submit_market_sell("AAPL", fill.qty, b.client_id("liquidate/2026-12-01/AAPL"))   # продажа ушла, события нет
    (e,) = b.sync("2026-12-01")
    assert (e["kind"], e["recovered"], e["symbol"]) == ("liquidate", True, "AAPL")
    assert e["qty"] == pytest.approx(fill.qty) and e["entry_price"] == pytest.approx(200.0) and e["broker_order_id"]
    assert b.sync("2026-12-01") == [] and [x["kind"] for x in store.orders()] == ["buy", "liquidate"]
    assert "AAPL" not in store.state()["stops"]


def test_sync_recovers_a_plain_sale_as_close_and_a_gap_sale_as_stop_fill(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    for o in list(api.open_stop_orders().values()):
        api.cancel(o["id"])
    api.submit_market_sell("AAPL", 0.25, b.client_id("x/3"))
    api.submit_market_sell("XLE", 1.0, b.client_id("gap/2026-12-01/XLE"))
    events = {e["symbol"]: e for e in b.sync("2026-12-01")}
    assert (events["AAPL"]["kind"], events["AAPL"]["recovered"]) == ("close", True)
    assert events["AAPL"]["fraction"] == pytest.approx(0.5)          # было 0.5 акции, продано 0.25
    assert (events["XLE"]["kind"], events["XLE"]["reason"], events["XLE"]["recovered"]) == ("stop_fill", "gap_below_stop", True)
    assert store.state()["stops"] == {"AAPL": 186.0}                  # XLE продана целиком, у AAPL остаток


def test_sync_recovers_a_filled_buy_and_a_later_repeat_sends_nothing(env):
    b = broker(env); api, store = env[3], env[1]
    store.update_state(stops={"AAPL": 186.0})                        # намерение записано до заявки
    api.submit_market_buy("AAPL", 150.0, b.client_id("x/1"))                      # исполнилась, события нет
    (e,) = b.sync("2026-12-01")
    assert (e["kind"], e["recovered"], e["stop_price"], e["thesis"]) == ("buy", True, 186.0, None)
    assert e["client_order_id"] == "x/1" and e["qty"] == pytest.approx(0.75) and e["notional"] == pytest.approx(150.0)
    assert b.sync("2026-12-01") == []
    again = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert again.qty == pytest.approx(0.75) and api.submitted == 1 and len(store.orders()) == 1


def test_sync_recovers_the_filled_part_of_a_cancelled_order(env):
    b = broker(env); api = env[3]
    env[1].update_state(stops={"AAPL": 186.0})                       # намерение записывается до заявки
    api.next_fill_fraction = 0.4
    oid = api.submit_market_buy("AAPL", 150.0, b.client_id("x/1"))
    api.cancel(oid)
    (e,) = b.sync("2026-12-01")
    assert e["kind"] == "buy" and e["qty"] == pytest.approx(0.3)


def test_sync_ignores_orders_still_in_flight(env):
    b = broker(env); api = env[3]
    env[1].update_state(stops={"AAPL": 186.0})
    api.next_fill_fraction = 0.4
    api.submit_market_buy("AAPL", 150.0, b.client_id("x/1"))                      # partially_filled, ещё идёт
    assert b.sync("2026-12-01") == []


# --- видимость покупки без стопа ---

def test_events_say_whether_the_stop_was_placed(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    assert store.orders()[-1]["stop_placed"] is True
    api.fail_stop_for = "AAPL"
    b.buy("2026-12-01", "AAPL", 100.0, 188.0, "тезис", "x/2")
    assert store.orders()[-1]["stop_placed"] is False
    b.close("2026-12-01", "AAPL", 0.5, "причина", "x/3")
    assert store.orders()[-1]["stop_placed"] is False
    api.fail_stop_for = None
    assert b.ensure_stops() == ["AAPL"]
    b.close("2026-12-01", "AAPL", 0.5, "причина", "x/4")
    assert store.orders()[-1]["stop_placed"] is True


# --- раунд исправлений 1: стоп переносится одним шагом ---

def test_raise_stop_moves_a_working_stop_in_one_step(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    assert api.replaced == 1 and api.open_stop_orders()["AAPL"]["stop_price"] == 190.0
    assert api.open_stop_orders()["AAPL"]["qty"] == pytest.approx(0.75)
    assert store.orders()[-1]["kind"] == "raise_stop"


def test_refused_replacement_keeps_the_old_stop_state_and_ledger(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.fail_replace = True
    with pytest.raises(BrokerRejected):
        b.raise_stop("2026-12-01", "AAPL", 190.0)
    assert api.open_stop_orders()["AAPL"]["stop_price"] == 186.0
    assert store.state()["stops"] == {"AAPL": 186.0} and [e["kind"] for e in store.orders()] == ["buy"]


def test_refused_new_stop_without_a_working_one_keeps_the_old_desired_price(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.expire_day_orders()
    api.fail_stop_for = "AAPL"
    with pytest.raises(BrokerRejected):
        b.raise_stop("2026-12-01", "AAPL", 190.0)
    assert store.state()["stops"] == {"AAPL": 186.0} and [e["kind"] for e in store.orders()] == ["buy"]


def test_ensure_stops_moves_a_wrong_price_by_replacing_not_cancelling(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.open_stop_orders()["AAPL"]["stop_price"] = 150.0
    api.fail_replace = True
    with pytest.raises(BrokerRejected):
        b.ensure_stops()
    assert "AAPL" in api.open_stop_orders()                          # неверный, но стоп остался


# --- раунд 2: продажа не больше того, что есть ---

def test_close_sends_nothing_when_the_stop_filled_during_the_cancel(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.fill_stop_on_cancel = {"AAPL"}
    with pytest.raises(BrokerRejected, match="закрыта стопом"):
        b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    assert api.submitted == 2 and api.positions == {}                # покупка и стоп, продажи нет
    assert api.cash_and_positions()[0] == pytest.approx(1000.0 - 150.0 + 0.75 * 186.0)
    (e,) = b.sync("2026-12-01")[-1:]
    assert e["kind"] == "stop_fill" and "AAPL" not in store.state()["stops"]


def test_liquidate_skips_a_symbol_whose_stop_filled_during_the_cancel(env):
    b = broker(env); api = env[3]
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    b.buy("2026-12-01", "XLE", 100.0, 90.0, "тезис", "x/2")
    api.fill_stop_on_cancel = {"AAPL"}
    sent = api.submitted
    assert [f.symbol for f in b.liquidate("2026-12-01")] == ["XLE"]
    assert api.submitted == sent + 1 and api.positions == {}          # одна продажа, по XLE


def test_gap_sale_sends_nothing_when_the_stop_filled_during_the_cancel(env):
    b = broker(env); api, market = env[3], env[2]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    api.open_stop_orders()["AAPL"]["stop_price"] = 150.0              # стоп не тот: ensure_stops его снимает
    market.prices["AAPL"] = 180.0
    api.fill_stop_on_cancel = {"AAPL"}
    sent = api.submitted
    assert b.ensure_stops() == [] and api.submitted == sent and api.positions == {}


# --- раунд 3: у каждого прогона свои заявки ---

def second_broker(env, tmp_path):
    cfg, store, market, api = env
    run = store.dir.parent / "opus-b"
    cfg2, _ = _load_run(run)
    return AlpacaBroker(cfg2, RunStore(run), market, api, poll_seconds=0.05, poll_interval=0.01)


def test_every_order_carries_the_run_prefix_and_the_ledger_keeps_the_callers_id(env):
    b = broker(env); api, store = env[3], env[1]
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    b.liquidate("2026-12-01")
    st = store.state()
    prefix = f"{env[0].experiment}/{env[0].participant}/{st['run_uid']}/"
    assert len(st["run_uid"]) == 8 and st["broker_since"] == env[2].now().isoformat()
    assert all(o["client_order_id"].startswith(prefix) for o in api.orders.values())
    assert [e["client_order_id"] for e in store.orders() if e["kind"] in ("buy", "close")] == ["x/1", "x/2"]
    stop_ids = [o["client_order_id"] for o in api.orders.values() if o["type"] == "stop"]
    assert len(stop_ids) == len(set(stop_ids)) >= 2


def test_a_second_run_on_a_busy_account_sends_nothing_and_records_nothing(env, tmp_path):
    a, b = broker(env), second_broker(env, tmp_path)
    api = env[3]
    a.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис A", "x/1")
    sent = api.submitted
    for call in (lambda: b.sync("2026-12-01"), lambda: b.buy("2026-12-01", "AAPL", 50.0, 186.0, "тезис B", "x/1"),
                 b.ensure_stops, lambda: b.liquidate("2026-12-01")):
        with pytest.raises(RuntimeError):
            call()
    store_b = RunStore(env[1].dir.parent / "opus-b")
    assert store_b.orders() == [] and "run_uid" not in store_b.state() and api.submitted == sent
    assert a.sync("2026-12-01") == [] and [x["kind"] for x in env[1].orders()] == ["buy"]


def test_liquidation_of_one_run_is_not_mistaken_for_the_other_runs(env, tmp_path):
    a, b = broker(env), second_broker(env, tmp_path)
    store_b = RunStore(env[1].dir.parent / "opus-b")
    a.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/1")
    a.liquidate("2026-12-01")
    b.buy("2026-12-01", "AAPL", 100.0, 186.0, "тезис", "x/2")
    assert [f.symbol for f in b.liquidate("2026-12-01")] == ["AAPL"]
    assert env[3].positions == {} and [e["kind"] for e in store_b.orders()] == ["buy", "liquidate"]
    assert store_b.orders()[-1]["qty"] == pytest.approx(0.5)


# --- раунд 4: неизвестно, дошла ли заявка ---

def test_unknown_whether_the_order_reached_the_broker_keeps_the_desired_stop(env):
    b = broker(env); api, store = env[3], env[1]
    api.next_order_mode = "network_down"
    api.lookup_dies_with_network = True
    with pytest.raises(ConnectionError):
        b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert store.state()["stops"] == {"AAPL": 186.0}


# --- финальная волна, M9: сбой чтения счёта после сделки не отменяет сделку ---

def test_failed_account_read_after_the_fill_still_answers_bought(env):
    from traider.tools import SessionContext, build_tools
    cfg, store, market, api = env
    b = broker(env)
    real = api.submit_market_buy
    def submit(*a):
        oid = real(*a)
        def down():
            raise ConnectionError("нет связи")
        api.cash_and_positions = down                                # после исполнения счёт не читается
        return oid
    api.submit_market_buy = submit
    tools = {t.name: t for t in build_tools(SessionContext(cfg, store, b, market, "2026-12-01"))}
    tools["record_forecasts"].fn({"forecasts": [{"symbol": s, "p": 0.5} for s in cfg.panel]})
    out = tools["buy"].fn({"symbol": "AAPL", "notional": 150.0, "stop_price": 186.0, "thesis": "т"})
    assert out.startswith("Куплено AAPL")
    assert [e["kind"] for e in store.orders()] == ["buy"] and store.orders()[0]["stop_placed"] is False
