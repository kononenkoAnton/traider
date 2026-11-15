import pytest

from traider.brokers import BrokerRejected, NoPrice


def run_contract(make_broker, market):
    """make_broker() возвращает новый экземпляр над тем же хранилищем."""
    b = make_broker()
    assert b.account().cash == pytest.approx(1000.0) and b.account().positions == ()
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    acct = b.account()
    assert fill.kind == "buy" and acct.cash == pytest.approx(850.0, abs=0.5)
    assert [p.symbol for p in acct.positions] == ["AAPL"] and acct.positions[0].stop_price == 186.0
    again = make_broker().buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")      # повтор той же заявки
    assert again.qty == pytest.approx(fill.qty) and make_broker().account().cash == pytest.approx(acct.cash)
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    assert make_broker().account().positions[0].stop_price == 190.0
    half = b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    assert half.qty == pytest.approx(fill.qty / 2, rel=1e-3)
    assert [f.symbol for f in b.liquidate("2026-12-01")] == ["AAPL"] and b.account().positions == ()
    # пик поднимается и у пустого счёта: закрылись с прибылью, пол идёт от новых денег
    b.buy("2026-12-02", "AAPL", 150.0, 186.0, "тезис", "x/3")
    market.prices["AAPL"] = 300.0
    b.close("2026-12-02", "AAPL", 1.0, "фиксирую прибыль", "x/4")
    flat = b.account()
    assert flat.positions == () and flat.cash > 1000.0
    assert flat.peak_equity >= flat.cash - 1e-9
    assert make_broker().account().peak_equity == flat.peak_equity


def run_refusal_contract(make_broker, market, close_needs_quote: bool = True):
    """Отказы: негодная котировка даёт NoPrice, негодная заявка BrokerRejected; счёт не меняется.

    close_needs_quote=False у брокера, который продаёт по рынку и не ждёт нашей котировки: закрытие
    уменьшает риск, поэтому оно не отказывает без котировки (его проверяет отдельный тест брокера)."""
    nan, day = float("nan"), "2026-12-01"
    b = make_broker()
    b.buy(day, "AAPL", 150.0, 186.0, "тезис", "r/1")
    before = b.account()
    market.prices["MSFT"] = nan
    for sym in ("MSFT", "QQQ"):                                     # плохая и отсутствующая котировка
        with pytest.raises(NoPrice):
            b.buy(day, sym, 100.0, 90.0, "тезис", f"r/{sym}")
    rejected = [
        lambda: b.close(day, "XLE", 1.0, "нет позиции, есть котировка", "r/2"),
        lambda: b.close(day, "QQQ", 1.0, "нет позиции, нет котировки", "r/3"),
        *[lambda f=f: b.close(day, "AAPL", f, "плохая доля", "r/4") for f in (0.0, -0.5, 1.5, nan)],
        lambda: b.raise_stop(day, "XLE", 95.0),
        *[lambda s=s: b.raise_stop(day, "AAPL", s) for s in (nan, float("inf"), 0.0, -1.0)],
        lambda: b.buy(day, "XLE", 100.0, 90.0, "тезис", "r/1"),      # тот же id, другая бумага
        lambda: b.close(day, "XLE", 1.0, "тот же id, другая бумага", "r/1"),
    ]
    for call in rejected:
        with pytest.raises(BrokerRejected):
            call()
    for quote in (nan, None) if close_needs_quote else ():
        if quote is None:
            del market.prices["AAPL"]
        else:
            market.prices["AAPL"] = quote
        with pytest.raises(NoPrice):
            b.close(day, "AAPL", 1.0, "нет котировки", "r/5")
    market.prices["AAPL"] = 200.0
    after = make_broker().account()
    assert (after.cash, after.positions) == (before.cash, before.positions)
