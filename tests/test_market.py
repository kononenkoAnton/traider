import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.fakes import FakeMarket, ny
from tests.market_contract import run_market_contract
from traider.env import load_env
from traider.market import Bar, bar_from_sdk, low_open_pairs, ny_day

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_fake_market_satisfies_the_contract():
    run_market_contract(FakeMarket.with_history(["AAPL", "SPY"], days=30))


def test_load_env_reads_pairs_and_ignores_comments(tmp_path):
    (tmp_path / ".env").write_text("# ключи\nALPACA_API_KEY=abc\nEMPTY=\n\nANTHROPIC_API_KEY = k=1 \n")
    assert load_env(tmp_path / ".env") == {"ALPACA_API_KEY": "abc", "EMPTY": "", "ANTHROPIC_API_KEY": "k=1"}
    assert load_env(tmp_path / "missing") == {}


def test_universe_file_is_clean():
    lines = (ROOT / "data" / "universe.txt").read_text().split()
    assert lines == sorted(set(lines)) and 480 <= len(lines) <= 540
    assert {"SPY", "QQQ", "XLE", "AAPL", "BRK.B", "JPM"} <= set(lines)
    assert not {"TQQQ", "SQQQ", "SPXL", "SPXS", "UPRO", "SH"} & set(lines)


def sdk_bar(ts, o=1, h=3, low=0.5, c=2, v=10):
    return SimpleNamespace(timestamp=ts, open=o, high=h, low=low, close=c, volume=v)


def test_ny_day_uses_new_york_not_utc():
    # 02:00 UTC 3 Nov это ещё 2 Nov в Нью-Йорке
    assert ny_day(datetime(2026, 11, 3, 2, 0, tzinfo=timezone.utc)) == "2026-11-02"
    assert ny_day(datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc)) == "2026-11-03"


def test_bar_from_sdk_gives_plain_floats():
    b = bar_from_sdk(sdk_bar(datetime(2026, 11, 3, 5, 0, tzinfo=timezone.utc), o="1.5", v=7))
    assert b == Bar("2026-11-03", 1.5, 3.0, 0.5, 2.0, 7.0)
    assert all(type(x) is float for x in (b.open, b.high, b.low, b.close, b.volume))


def test_low_open_pairs_keep_order_and_plain_floats():
    t = datetime(2026, 11, 3, 15, 0, tzinfo=timezone.utc)
    pairs = low_open_pairs([sdk_bar(t, o=10, low=9), sdk_bar(t, o="8", low="7.5")])
    assert pairs == [(9.0, 10.0), (7.5, 8.0)] and all(type(x) is float for p in pairs for x in p)
    assert low_open_pairs([]) == []


def utc(*a):
    return datetime(*a, tzinfo=timezone.utc)


def test_trade_is_fresh_only_on_todays_new_york_day():
    from traider.market import traded_today
    now = ny(2026, 11, 3, 11, 0)
    assert traded_today(ny(2026, 11, 3, 9, 31), now)
    assert not traded_today(ny(2026, 11, 2, 15, 59), now)
    # 2026-11-03 04:59 UTC = 23:59 2 Nov NY (EST), 05:00 UTC = 00:00 3 Nov NY
    assert not traded_today(utc(2026, 11, 3, 4, 59), now)
    assert traded_today(utc(2026, 11, 3, 5, 0), now)


def test_regular_session_bars_drop_extended_hours_and_half_day_tail():
    from traider.market import regular_session_bars
    full = (ny(2026, 11, 3, 9, 30), ny(2026, 11, 3, 16, 0))
    half = (ny(2026, 11, 27, 9, 30), ny(2026, 11, 27, 13, 0))
    bars = [sdk_bar(t, low=i) for i, t in enumerate([
        ny(2026, 11, 3, 9, 15), ny(2026, 11, 3, 9, 30), ny(2026, 11, 3, 15, 45),
        ny(2026, 11, 3, 16, 0), ny(2026, 11, 27, 12, 45), ny(2026, 11, 27, 13, 0)])]
    kept = regular_session_bars(bars, [full, half])
    assert [b.low for b in kept] == [1, 2, 4]


def test_completed_days_drops_today_only_while_open():
    from traider.market import completed_days
    bars = [Bar("2026-11-02", 1, 1, 1, 1, 1), Bar("2026-11-03", 1, 1, 1, 1, 1)]
    assert [b.day for b in completed_days(bars, "2026-11-03", True)] == ["2026-11-02"]
    assert len(completed_days(bars, "2026-11-03", False)) == 2


def test_fake_market_daily_bars_are_completed_days_only():
    m = FakeMarket.with_history(["AAPL"], days=5, clock=ny(2026, 12, 2))
    assert m.daily_bars(["AAPL"], 5)["AAPL"][-1].day == "2026-12-01"
    m.open = False
    assert m.daily_bars(["AAPL"], 5)["AAPL"][-1].day == "2026-12-02"


def test_load_env_accepts_export_and_quotes(tmp_path):
    (tmp_path / ".env").write_text(
        "export A=1\nB=\"two words\"\nC='x=y'\nexport D = \"q\" \nE=\"unterminated\nF=a\"b\nG=\"\"\n")
    assert load_env(tmp_path / ".env") == {
        "A": "1", "B": "two words", "C": "x=y", "D": "q", "E": "\"unterminated", "F": "a\"b", "G": ""}


# --- финальная волна, I2: стопы видят каждую минуту ---

def test_floor_minute_drops_seconds_and_keeps_the_zone():
    from traider.market import floor_minute
    t = ny(2026, 12, 1, 10, 15).replace(second=42, microsecond=7)
    assert floor_minute(t) == ny(2026, 12, 1, 10, 15) and floor_minute(t).tzinfo == t.tzinfo
    assert floor_minute(ny(2026, 12, 1, 10, 15)) == ny(2026, 12, 1, 10, 15)

def test_fake_lows_include_the_minute_that_holds_since_and_not_the_one_before():
    m = FakeMarket(lows={"AAPL": [(1.0, 2.0, ny(2026, 12, 1, 10, 14)), (3.0, 4.0, ny(2026, 12, 1, 10, 15))]})
    since = ny(2026, 12, 1, 10, 15).replace(second=30)
    assert m.lows_since(["AAPL"], since) == {"AAPL": [(3.0, 4.0)]}

def test_alpaca_market_asks_for_split_adjusted_days_and_one_minute_lows():   # M16, I2
    from types import SimpleNamespace as NS
    from alpaca.data.enums import Adjustment
    from traider.market import AlpacaMarket
    seen = []
    m = object.__new__(AlpacaMarket)
    m._data = NS(get_stock_bars=lambda req: seen.append(req) or NS(data={}))
    m._trading = NS(get_clock=lambda: NS(is_open=True), get_calendar=lambda req: [])
    m.daily_bars(["AAPL"], 5)
    m.lows_since(["AAPL"], ny(2026, 12, 1, 10, 15).replace(second=30))
    days, lows = seen
    assert days.adjustment == Adjustment.SPLIT
    assert lows.timeframe.amount == 1 and lows.timeframe.unit.value == "Min"
    assert lows.start == datetime(2026, 12, 1, 15, 15)                     # SDK хранит начало в UTC без пояса


# --- волна после проверки всей ветки ---

def test_every_alpaca_client_gets_a_5_and_30_second_timeout_and_no_retry():
    from traider.alpaca import AlpacaSdkApi
    from traider.market import AlpacaMarket
    m, api = AlpacaMarket("k", "s"), AlpacaSdkApi("k", "s", paper=True)
    for client in (m._data, m._news, m._trading, api._trading):           # клиенты создаются без сети
        for url in ("https://api.alpaca.markets/", "https://data.alpaca.markets/", "http://127.0.0.1/"):
            adapter = client._session.get_adapter(url)
            assert adapter.timeout == (5, 30) and adapter.max_retries.total == 0


def test_a_server_that_never_answers_ends_the_call_with_a_timeout(silent_server):
    import time

    import pytest
    import requests
    from alpaca.trading.client import TradingClient
    from traider.market import bounded
    client = bounded(TradingClient("k", "s", url_override=silent_server.rstrip("/")), timeout=(1, 0.5))
    t0 = time.monotonic()
    with pytest.raises(requests.exceptions.Timeout):
        client.get_clock()
    assert time.monotonic() - t0 < 3
    client._session.close()
