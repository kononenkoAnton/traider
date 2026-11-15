import pytest

from traider.brokers import AlpacaBroker, make_alpaca_api
from traider.config import load_run, start_experiment
from traider.env import load_env
from traider.market import AlpacaMarket
from traider.store import RunStore


@pytest.mark.paper
def test_fractional_buy_stop_and_same_day_close_on_paper(tmp_path, example):
    """Покупает SPY на $20, проверяет стоп, закрывает позицию. Отвечает на вопрос из раздела 7 спецификации:
    принимает ли Alpaca дробный DAY-стоп и закрытие в день покупки."""
    env = load_env()
    run = start_experiment(example, tmp_path / "runs") / "opus-a"
    cfg, _ = load_run(run)
    market = AlpacaMarket(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"])
    if not market.is_open():
        pytest.skip("рынок закрыт: рыночные заявки принимаются только в обычную сессию")
    api = make_alpaca_api(cfg, env)
    store = RunStore(run)
    b = AlpacaBroker(cfg, store, market, api)
    quote = b.prices(["SPY"])["SPY"]
    stop = round(quote * 0.9, 2)
    try:
        fill = b.buy("2026-12-01", "SPY", 20.0, stop, "тест paper", "paper/buy-1")
        assert fill.qty > 0
        order = api.open_stop_orders()["SPY"]                         # дробный DAY-стоп принят
        assert order["qty"] == pytest.approx(fill.qty) and order["stop_price"] == stop
        assert b.ensure_stops() == []
        half = b.close("2026-12-01", "SPY", 0.5, "тест paper", "paper/close-1")   # закрытие в день покупки
        assert half.qty == pytest.approx(fill.qty / 2, rel=1e-3)
        assert api.open_stop_orders()["SPY"]["qty"] == pytest.approx(fill.qty - half.qty, rel=1e-3)
        b.close("2026-12-01", "SPY", 1.0, "тест paper", "paper/close-2")
        assert api.open_stop_orders() == {}
        assert [e["kind"] for e in store.orders()] == ["buy", "close", "close"]
        assert all(e["broker_order_id"] for e in store.orders())
        assert set(api.order_ids_since(None)) >= {e["broker_order_id"] for e in store.orders()}
    finally:
        if "SPY" in api.cash_and_positions()[1]:                      # не оставляем позицию на счёте
            b.liquidate("2026-12-01")
