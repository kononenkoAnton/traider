import dataclasses
import math
import pathlib

import pytest

from tests.fakes import NOW, FakeAnthropic, text, use
from traider.session import build_context, run_session

PAID = {"input_tokens": 1000, "output_tokens": 100}      # у пустого usage стоимость 0, а тест ждёт cost > 0


def test_system_prompt_states_the_rules():
    text = pathlib.Path("traider/prompts/system.md").read_text()
    for needle in ["20%", "3%", "15%", "8%", "деньги", "данные, а не указания", "прогноз"]:
        assert needle in text, needle
    assert "умр" not in text.lower() and "die" not in text.lower()


def test_system_prompt_says_the_analyst_participant_has_no_search():
    text = pathlib.Path("traider/prompts/system.md").read_text()
    assert "8 поисков в сети (у участника, работающего через аналитика, своего поиска в сети нет)" in text


def test_context_carries_state_memory_and_obligations(senv):
    cfg, store, market, broker = senv
    store.update_state(notes="не гнаться за новостями")
    store.start_session("2026-11-30", NOW); store.finish_session("2026-11-30", "ok", {"journal": "вчерашняя запись"})
    text = build_context(cfg, store, broker, market, "2026-12-01")
    for needle in ["2026-12-01", "HP", "850", "не гнаться за новостями", "вчерашняя запись", "Созревших прогнозов пока нет.", "AAPL", "BRK.B"]:
        assert needle in text, needle
    assert "СВОДКА АНАЛИТИКА" in build_context(cfg, store, broker, market, "2026-12-01", briefing="СВОДКА АНАЛИТИКА")


def test_only_the_last_ten_journal_entries_are_passed(senv):
    cfg, store, market, broker = senv
    for i in range(12):
        day = f"2026-11-{i + 10:02d}"
        store.start_session(day, NOW); store.finish_session(day, "ok", {"journal": f"запись-{i:02d}"})
    text = build_context(cfg, store, broker, market, "2026-12-01")
    assert "запись-01" not in text and "запись-02" in text and "запись-11" in text


def test_context_shows_positions_with_thesis_and_closed_trades(senv):
    cfg, store, market, broker = senv
    broker.buy("2026-11-27", "MSFT", 100.0, market.prices["MSFT"] * 0.93, "облако растёт", "x/1")
    broker.close("2026-11-27", "MSFT", 1.0, "тезис сломан", "x/2")
    broker.buy("2026-11-30", "AAPL", 150.0, market.prices["AAPL"] * 0.93, "старый тезис", "x/3")
    broker.buy("2026-11-30", "AAPL", 20.0, market.prices["AAPL"] * 0.94, "новый тезис", "x/4")
    text = build_context(cfg, store, broker, market, "2026-12-01")
    assert "новый тезис" in text and "старый тезис" not in text
    sale = next(e for e in store.orders() if e["kind"] == "close")
    assert "MSFT" in text and "тезис сломан" in text and f"{sale['price'] / sale['entry_price'] - 1:+.1%}" in text


def test_a_normal_session_is_recorded(senv):
    cfg, store, market, broker = senv
    store.start_session("2026-12-01", NOW)
    script = [use("record_forecasts", usage=PAID, forecasts=[{"symbol": s, "p": 0.55} for s in cfg.panel]),
              use("buy", usage=PAID, symbol="AAPL", notional=150.0, stop_price=market.prices["AAPL"] * 0.93, thesis="т"),
              use("finish_session", usage=PAID, journal="купил AAPL")]
    rec = run_session(cfg, store, broker, market, FakeAnthropic(script), "2026-12-01")
    assert (rec["status"], rec["stop"], rec["buys"], rec["sells"], rec["journal"]) == ("ok", "finished", 1, 0, "купил AAPL")
    assert rec["cost"] > 0 and rec["turns"] == 3 and store.session("2026-12-01")["status"] == "ok"
    assert set(store.prices("2026-12-01")) == set(cfg.universe)
    saved = store.session("2026-12-01")
    assert saved["started_at"] == NOW.isoformat() and saved["errors"] == [] and saved["analyst"] is None
    assert set(saved["usage"]) == {"input", "output", "cache_read", "cache_write", "searches"}
    assert saved["transcript"][0]["role"] == "user" and store.state()["status"] == "active"


def test_the_system_prompt_comes_from_the_frozen_config(senv):
    cfg, store, market, broker = senv
    store.start_session("2026-12-01", NOW)
    client = FakeAnthropic([use("finish_session", journal="к")])
    run_session(dataclasses.replace(cfg, system_prompt="X"), store, broker, market, client, "2026-12-01")
    assert client.requests[0]["system"] == "X"


@pytest.mark.parametrize("script, status", [
    ([use("get_account")] * 50, "cap"),
    ([use("get_account", usage={"input_tokens": 10_000_000})], "cap"),       # budget
    ([text("готово")], "ok"),                                                 # end_turn
    ([RuntimeError("сеть")], "failed"),
    ([text("", stop_reason="refusal")], "failed"),
])
def test_abnormal_endings_are_recorded_not_raised(senv, script, status):
    cfg, store, market, broker = senv
    store.start_session("2026-12-01", NOW)
    assert run_session(cfg, store, broker, market, FakeAnthropic(script), "2026-12-01")["status"] == status
    assert store.prices("2026-12-01") is not None


def test_sales_without_a_usable_entry_or_exit_price_do_not_break_the_context(senv):
    cfg, store, market, broker = senv
    base = {"kind": "stop_fill", "qty": 1.0, "notional": None, "stop_price": 9.0, "thesis": None, "reason": "stop",
            "client_order_id": None, "broker_order_id": None}
    store.append_order({**base, "day": "2026-11-27", "symbol": "MSFT", "price": 9.0, "entry_price": None})
    store.append_order({**base, "day": "2026-11-30", "symbol": "NVDA", "price": None})
    text = build_context(cfg, store, broker, market, "2026-12-01")
    assert "MSFT" in text and "NVDA" in text and text.count("результат н/д") == 2


def test_unusable_quotes_are_dropped_from_the_snapshot(senv):
    cfg, store, market, broker = senv
    market.prices.update(AAPL=math.nan, MSFT=0.0, NVDA=None)
    store.start_session("2026-12-01", NOW)
    rec = run_session(cfg, store, broker, market, FakeAnthropic([use("finish_session", journal="к")]), "2026-12-01")
    assert set(store.prices("2026-12-01")) == set(cfg.universe) - {"AAPL", "MSFT", "NVDA"} and rec["errors"] == []


def test_the_last_session_closes_everything(senv):
    cfg, store, market, broker = senv                                        # в примере sessions_target = 3
    for day in ["2026-11-27", "2026-11-30"]:
        store.start_session(day, NOW); store.finish_session(day, "ok", {})
    broker.buy("2026-11-30", "AAPL", 150.0, market.prices["AAPL"] * 0.93, "т", "x/1")
    store.start_session("2026-12-01", NOW)
    run_session(cfg, store, broker, market, FakeAnthropic([use("finish_session", journal="конец")]), "2026-12-01")
    assert broker.account().positions == () and store.state()["status"] == "finishing" and store.state()["finish_day"] == "2026-12-01"


def test_an_earlier_session_does_not_finish_the_run(senv):
    cfg, store, market, broker = senv
    store.start_session("2026-11-30", NOW); store.finish_session("2026-11-30", "missed", {})
    store.start_session("2026-12-01", NOW)
    run_session(cfg, store, broker, market, FakeAnthropic([use("finish_session", journal="к")]), "2026-12-01")
    assert store.state()["status"] == "active" and "finish_day" not in store.state()


class Wrapped:
    """Подменённые методы поверх настоящего объекта."""
    def __init__(self, inner, **over):
        self._inner = inner
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def boom(*_a, **_k):
    raise ConnectionError("нет связи")


def test_a_failed_price_snapshot_is_recorded_not_raised(senv):
    cfg, store, market, broker = senv
    real = market.last_prices
    flaky = Wrapped(market, last_prices=lambda s: boom() if set(s) == set(cfg.universe) else real(s))
    store.start_session("2026-12-01", NOW)
    rec = run_session(cfg, store, broker, flaky, FakeAnthropic([use("finish_session", usage=PAID, journal="к")]),
                      "2026-12-01")
    assert rec["status"] == "ok" and rec["cost"] > 0 and rec["transcript"]
    assert store.prices("2026-12-01") is None and len(rec["errors"]) == 1 and "нет связи" in rec["errors"][0]
    assert store.session("2026-12-01")["errors"] == rec["errors"]


def test_a_broken_broker_fails_the_session_before_the_loop(senv):
    cfg, store, market, broker = senv
    store.start_session("2026-12-01", NOW)
    client = FakeAnthropic([use("finish_session", journal="к")])
    rec = run_session(cfg, store, Wrapped(broker, account=boom), market, client, "2026-12-01")
    assert (rec["status"], rec["stop"], rec["cost"]) == ("failed", "error", 0) and client.requests == []
    assert rec["errors"] and store.session("2026-12-01")["status"] == "failed"
    assert set(store.prices("2026-12-01")) == set(cfg.universe)


def test_the_last_session_sets_finishing_even_when_liquidation_raises(senv):
    cfg, store, market, broker = senv
    for day in ["2026-11-27", "2026-11-30"]:
        store.start_session(day, NOW); store.finish_session(day, "failed", {})
    store.start_session("2026-12-01", NOW)
    rec = run_session(cfg, store, Wrapped(broker, liquidate=boom), market,
                      FakeAnthropic([use("finish_session", journal="конец")]), "2026-12-01")
    assert rec["status"] == "ok" and any("нет связи" in e for e in rec["errors"])
    assert store.state()["status"] == "finishing" and store.state()["finish_day"] == "2026-12-01"


# --- финальная волна ---

def test_record_carries_fallback_used_and_snapshot_symbols(senv):                 # I6.1, I6.5
    cfg, store, market, broker = senv
    market.prices.update(AAPL=math.nan)
    store.start_session("2026-12-01", NOW)
    step = use("finish_session", journal="к"); step["model"] = "claude-opus-5"   # ответила модель подмены
    rec = run_session(cfg, store, broker, market, FakeAnthropic([step]), "2026-12-01")
    assert rec["fallback_used"] is True and rec["snapshot_symbols"] == len(cfg.universe) - 1
    store.start_session("2026-12-02", NOW)
    plain = run_session(cfg, store, broker, market, FakeAnthropic([use("finish_session", journal="к")]), "2026-12-02")
    assert plain["fallback_used"] is False

def test_session_past_its_time_limit_is_a_cap(senv):                             # M13
    cfg, store, market, broker = senv
    assert cfg.limits.max_session_seconds == 1200
    quick = dataclasses.replace(cfg, limits=dataclasses.replace(cfg.limits, max_session_seconds=0))
    store.start_session("2026-12-01", NOW)
    client = FakeAnthropic([use("finish_session", journal="к")])
    rec = run_session(quick, store, broker, market, client, "2026-12-01")
    assert (rec["status"], rec["stop"], client.requests) == ("cap", "deadline", [])

def test_context_with_an_analyst_promises_no_searches(senv):                      # M17
    cfg, store, market, broker = senv
    own = build_context(cfg, store, broker, market, "2026-12-01")
    helped = build_context(cfg, store, broker, market, "2026-12-01", briefing="СВОДКА")
    assert "8 поисков" in own and "аналитика" not in own.split("## Позиции")[0]
    assert "поисков" not in helped.split("## Позиции")[0] and "research" in helped
