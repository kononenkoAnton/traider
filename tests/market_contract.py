from datetime import timedelta


def run_market_contract(market):
    prices = market.last_prices(["AAPL", "SPY", "NOPE_XX"])
    assert set(prices) == {"AAPL", "SPY"} and all(v > 0 for v in prices.values())
    bars = market.daily_bars(["AAPL"], 30)["AAPL"]
    assert 15 <= len(bars) <= 30 and [b.day for b in bars] == sorted(b.day for b in bars)
    if market.is_open():  # сегодняшняя свеча ещё формируется: только завершённые дни
        assert all(b.day < market.now().date().isoformat() for b in bars)
    assert all(b.low <= b.open <= b.high and b.low <= b.close <= b.high for b in bars)
    assert str(market.now().tzinfo) == "America/New_York" and isinstance(market.is_open(), bool)
    news = market.news(["AAPL"], 5)
    assert len(news) <= 5 and all({"ts", "headline", "summary", "symbols"} <= set(n) for n in news)
    assert market.fractionable(["AAPL", "NOPE_XX"]) == {"AAPL"}
    bars = market.lows_since(["AAPL"], market.now() - timedelta(days=5))
    assert all(low <= open_ for pairs in bars.values() for low, open_ in pairs)
    week = market.trading_days("2026-11-02", "2026-11-06")
    assert week == ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06"]
