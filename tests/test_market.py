import pathlib
from datetime import datetime, timezone
from types import SimpleNamespace

from tests.fakes import FakeMarket
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
