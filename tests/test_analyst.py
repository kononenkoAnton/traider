import dataclasses

from tests.fakes import NOW, FakeAnthropic, text, use
from traider.analyst import Analyst
from traider.session import run_session


PAID = dict(input_tokens=1000, output_tokens=100)      # у пустого usage стоимость 0, а тест ждёт cost > 0


def _search(request):
    return next((t for t in request["tools"] if t.get("name") == "web_search"), None)


def test_briefing_uses_the_cheap_model_with_a_bigger_search_allowance(env_analyst):
    cfg, market = env_analyst
    client = FakeAnthropic([use("get_news", limit=20, usage=PAID), use("submit", text="СВОДКА", usage=PAID)])
    a = Analyst(client, cfg, market)
    assert a.briefing("2026-12-01") == "СВОДКА"
    assert client.requests[0]["model"] == "claude-haiku-5-5"
    assert any(t.get("name") == "web_search" and t["max_uses"] == 40 for t in client.requests[0]["tools"])
    assert a.cost > 0


def test_briefing_is_capped_at_1200_words(env_analyst):
    cfg, market = env_analyst
    a = Analyst(FakeAnthropic([use("submit", text="слово " * 3000)]), cfg, market)
    assert len(a.briefing("2026-12-01").split()) == 1200


def test_an_answer_is_capped_at_400_words_and_keeps_its_paragraphs(env_analyst):
    cfg, market = env_analyst
    a = Analyst(FakeAnthropic([use("submit", text="слово " * 2 + "\n\n" + "слово " * 3000)]), cfg, market)
    out = a.research("вопрос")
    assert len(out.split()) == 400 and "\n\n" in out


def test_research_shares_the_daily_budget(env_analyst):
    cfg, market = env_analyst
    pricey = use("submit", text="ответ", usage=dict(input_tokens=4_000_000))            # $2.00 по длинным ставкам Haiku (запрос длиннее 100 тысяч токенов), потолок $0.30
    client = FakeAnthropic([pricey])
    a = Analyst(client, cfg, market)
    assert a.research("вопрос") == "ответ"
    assert a.research("ещё вопрос") == "Бюджет аналитика на сегодня исчерпан."
    assert len(client.requests) == 1


def test_failed_analyst_does_not_break_the_session(env_analyst):
    cfg, market = env_analyst
    a = Analyst(FakeAnthropic([RuntimeError("сеть")]), cfg, market)
    assert a.briefing("2026-12-01") == "Сводка аналитика недоступна."


def test_a_failed_question_gets_a_stub_answer(env_analyst):
    cfg, market = env_analyst
    assert Analyst(FakeAnthropic([RuntimeError("сеть")]), cfg, market).research("вопрос") == "Аналитик недоступен."


def test_a_plain_final_message_is_the_answer(env_analyst):
    cfg, market = env_analyst
    a = Analyst(FakeAnthropic([text("свободный ответ"), text("")]), cfg, market)
    assert a.research("вопрос") == "свободный ответ"
    assert a.briefing("2026-12-01") == "Сводка аналитика недоступна."


def test_the_system_prompt_is_the_frozen_one_not_the_file(env_analyst):
    cfg, market = env_analyst
    client = FakeAnthropic([use("submit", text="х")])
    Analyst(client, dataclasses.replace(cfg, analyst_prompt="ЗАМОРОЖЕНО"), market).research("в")
    assert client.requests[0]["system"] == "ЗАМОРОЖЕНО"


def test_each_call_gets_only_the_searches_that_are_left(env_analyst):
    cfg, market = env_analyst
    cfg = dataclasses.replace(cfg, analyst_max_usd=100.0)
    client = FakeAnthropic([use("submit", text="а", usage=dict(web_search_requests=15)),
                            use("submit", text="б", usage=dict(web_search_requests=25)),
                            use("submit", text="в")])
    a = Analyst(client, cfg, market)
    for q in ("1", "2", "3"):
        a.research(q)
    assert [(_search(r) or {}).get("max_uses") for r in client.requests] == [40, 25, None]
    assert a.searches == a.usage.searches == 40


def test_usage_and_cost_accumulate_over_all_calls(env_analyst):
    cfg, market = env_analyst
    paid = dict(input_tokens=1000, output_tokens=100)
    a = Analyst(FakeAnthropic([use("submit", text="а", usage=paid), use("submit", text="б", usage=paid)]), cfg, market)
    a.briefing("2026-12-01")
    one = a.cost
    a.research("в")
    assert a.usage.input == 2000 and a.usage.output == 200 and abs(a.cost - 2 * one) < 1e-12


def test_session_with_an_analyst(env_analyst_session):
    cfg, store, market, broker = env_analyst_session
    store.start_session("2026-12-01", NOW)
    main = FakeAnthropic([use("research", question="что с LLY?", usage=PAID), use("finish_session", journal="ок")])
    helper = FakeAnthropic([use("submit", text="СВОДКА", usage=PAID), use("submit", text="ОТВЕТ")])
    rec = run_session(cfg, store, broker, market, main, "2026-12-01", analyst=Analyst(helper, cfg, market))
    assert rec["analyst"]["briefing"] == "СВОДКА" and rec["cost"] > rec["analyst"]["cost"] > 0
    assert "СВОДКА" in main.requests[0]["messages"][0]["content"]
    assert not any(t.get("name") in ("web_search", "get_news") for t in main.requests[0]["tools"])
    assert set(rec["analyst"]) == {"cost", "searches", "briefing", "usage", "model", "calls"}
    assert rec["analyst"]["model"] == "claude-haiku-5-5" and rec["analyst"]["usage"]["input"] == 1000
    assert "ОТВЕТ" in str(main.requests[1]["messages"][-1]["content"])


def test_a_failed_session_still_records_the_analysts_cost(env_analyst_session):
    cfg, store, market, broker = env_analyst_session
    store.start_session("2026-12-01", NOW)
    helper = FakeAnthropic([use("submit", text="СВОДКА", usage=dict(input_tokens=1000, output_tokens=100))])
    rec = run_session(cfg, store, broker, market, FakeAnthropic([RuntimeError("сеть")]), "2026-12-01",
                      analyst=Analyst(helper, cfg, market))
    paid = rec["analyst"]["cost"]
    assert rec["status"] == "failed" and paid > 0 and rec["cost"] == paid
    assert store.session("2026-12-01")["analyst"]["cost"] == paid


def test_a_broken_briefing_leaves_a_stub_in_the_context(env_analyst_session):
    cfg, store, market, broker = env_analyst_session

    class Broken(Analyst):
        def briefing(self, day):
            raise RuntimeError("сломался")

    store.start_session("2026-12-01", NOW)
    main = FakeAnthropic([use("finish_session", journal="ок")])
    rec = run_session(cfg, store, broker, market, main, "2026-12-01",
                      analyst=Broken(FakeAnthropic([]), cfg, market))
    assert "Сводка аналитика недоступна." in main.requests[0]["messages"][0]["content"]
    assert rec["status"] == "ok" and any("сломался" in e for e in rec["errors"])


# --- финальная волна ---

def test_each_analyst_call_is_recorded_and_has_its_time_limit(env_analyst_session, monkeypatch):   # I6.4, M13
    from traider import analyst as analyst_module
    cfg, store, market, broker = env_analyst_session
    seen = []
    real = analyst_module.run_agent
    monkeypatch.setattr(analyst_module, "run_agent", lambda *a, **kw: seen.append(kw["max_seconds"]) or real(*a, **kw))
    store.start_session("2026-12-01", NOW)
    main = FakeAnthropic([use("research", question="что с LLY?"), use("finish_session", journal="ок")])
    helper = FakeAnthropic([use("submit", text="СВОДКА", usage=PAID), use("submit", text="ОТВЕТ", usage=PAID)])
    rec = run_session(cfg, store, broker, market, main, "2026-12-01", analyst=Analyst(helper, cfg, market))
    calls = rec["analyst"]["calls"]
    assert [c["kind"] for c in calls] == ["briefing", "research"] and calls[1]["question"] == "что с LLY?"
    assert all(c["stop"] == "finished" and c["cost"] > 0 and c["transcript"][0]["role"] == "user" for c in calls)
    assert sum(c["cost"] for c in calls) == rec["analyst"]["cost"] and seen == [600, 600]
