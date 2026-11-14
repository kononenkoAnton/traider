import pytest


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
