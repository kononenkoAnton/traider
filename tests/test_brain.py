import json

import pytest

from tests.fakes import FakeAnthropic, text, use
from traider.brain import Tool, Usage, cost_usd, run_agent

PRICES = {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0, "search": 0.01}
calls = []
ECHO = Tool("echo", "повторяет", {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False},
            lambda a: calls.append(a["x"]) or f"ok {a['x']}")
DONE = Tool("finish", "конец", {"type": "object", "properties": {}, "additionalProperties": False}, lambda a: "готово", terminal=True)


def run(script, **kw):
    calls.clear()
    args = dict(model="claude-opus-5-5", system="S", user="U", tools=[ECHO, DONE], prices=PRICES, max_turns=40, max_usd=2.0, max_searches=8)
    client = FakeAnthropic(script)
    return run_agent(client, **{**args, **kw}), client


def test_cost_arithmetic():
    assert cost_usd(Usage(1_000_000, 100_000, 2_000_000, 400_000, 3), PRICES) == pytest.approx(4.0 + 2.0 + 0.4 + 2.0 + 0.03)


def test_long_prompts_use_the_long_tier():
    haiku = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125, "search": 0.01,
             "long_prompt_threshold": 100_000, "input_long": 0.50, "output_long": 2.50,
             "cache_read_long": 0.05, "cache_write_long": 0.625}
    # 150 000 токенов запроса: вход 50 000, чтение кэша 80 000, запись кэша 20 000; выход 10 000; все по длинным ставкам
    assert cost_usd(Usage(50_000, 10_000, 80_000, 20_000), haiku) == pytest.approx(0.025 + 0.004 + 0.0125 + 0.025)
    assert cost_usd(Usage(50_000, 10_000, 80_000, 20_000), haiku) == pytest.approx(0.0665)
    # ровно 100 000 токенов запроса: порог строгий, базовые ставки
    assert cost_usd(Usage(20_000, 10_000, 60_000, 20_000), haiku) == pytest.approx(0.002 + 0.0006 + 0.0025 + 0.005)
    assert cost_usd(Usage(20_000, 10_000, 60_000, 20_000), haiku) == pytest.approx(0.0101)


def test_a_table_without_long_keys_prices_any_length_at_base_rates():
    base = {"input": 0.10, "output": 0.50, "cache_read": 0.01, "cache_write": 0.125, "search": 0.01}
    assert cost_usd(Usage(500_000, 100_000, 400_000, 100_000), base) == pytest.approx(0.05 + 0.05 + 0.004 + 0.0125)


def test_runs_tools_until_the_terminal_one():
    result, client = run([use("echo", x="a"), use("finish")])
    assert (result.stop, result.turns, calls) == ("finished", 2, ["a"])
    assert client.requests[0]["model"] == "claude-opus-5-5" and client.requests[0]["system"] == "S"


def test_parallel_tool_calls_come_back_in_one_user_message():
    result, client = run([use(("echo", dict(x="a")), ("echo", dict(x="b"))), use("finish")])
    results = [b for b in client.requests[1]["messages"][-1]["content"] if b["type"] == "tool_result"]
    assert len(results) == 2 and calls == ["a", "b"]


@pytest.mark.parametrize("bad", [use("nope"), use("echo"), use("echo", x=5)])     # нет такого инструмента, нет поля, не тот тип
def test_bad_tool_calls_become_error_results_and_the_loop_goes_on(bad):
    result, client = run([bad, use("finish")])
    assert result.stop == "finished" and calls == []
    assert client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_tool_exception_becomes_an_error_result():
    boom = Tool("echo", "", ECHO.input_schema, lambda a: 1 / 0)
    result, client = run([use("echo", x="a"), use("finish")], tools=[boom, DONE])
    assert result.stop == "finished" and client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_stops_at_max_turns():
    result, client = run([use("echo", x="a")] * 5, max_turns=3)
    assert (result.stop, result.turns, len(client.requests)) == ("max_turns", 3, 3)


def test_budget_stops_the_loop_before_running_that_turns_tools():
    pricey = use("echo", x="late", usage=dict(input_tokens=600_000))             # 600 тыс. входных токенов = $2.40
    result, client = run([use("echo", x="early"), pricey, use("finish")])
    assert result.stop == "budget" and calls == ["early"] and len(client.requests) == 2
    assert result.cost >= 2.0


def test_model_ending_without_the_terminal_tool():
    assert run([text("я закончил")])[0].stop == "end_turn"


def test_refusal_and_api_error_do_not_raise():
    assert run([text("", stop_reason="refusal")])[0].stop == "refusal"
    result, _ = run([use("echo", x="a"), RuntimeError("кредиты кончились")])
    assert result.stop == "error" and "кредиты" in result.error and calls == ["a"]


def test_web_search_is_offered_only_when_allowed():
    _, with_search = run([use("finish")])
    _, without = run([use("finish")], max_searches=0)
    assert any(t.get("name") == "web_search" and t["max_uses"] == 8 for t in with_search.requests[0]["tools"])
    assert not any(t.get("name") == "web_search" for t in without.requests[0]["tools"])


def test_request_shape_and_fallback_per_model():
    _, opus = run([use("finish")])
    r = opus.requests[0]
    assert r["thinking"] == {"type": "adaptive", "display": "summarized"} and r["output_config"] == {"effort": "high"}
    assert r["cache_control"] == {"type": "ephemeral"} and r["fallbacks"] == "default"
    assert not {"temperature", "top_p", "top_k", "tool_choice"} & set(r)
    _, haiku = run([use("finish")], model="claude-haiku-5-5")
    assert "fallbacks" not in haiku.requests[0]


def test_usage_and_cost_are_summed_and_model_used_is_recorded():
    u = dict(input_tokens=1000, output_tokens=500, cache_read_input_tokens=2000, web_search_requests=2)
    result, _ = run([use("echo", x="a", usage=u), use("finish", usage=u)])
    assert (result.usage.input, result.usage.output, result.usage.cache_read, result.usage.searches) == (2000, 1000, 4000, 4)
    assert result.cost == pytest.approx(2 * cost_usd(Usage(1000, 500, 2000, 0, 2), PRICES))
    assert result.model_used == "claude-opus-5-5"


def test_pause_turn_resends_and_counts_as_a_turn():
    result, client = run([text("", stop_reason="pause_turn"), use("finish")])
    assert (result.stop, result.turns, len(client.requests)) == ("finished", 2, 2)
    assert client.requests[1]["messages"][-1]["role"] == "assistant"


def test_transcript_is_json_serializable_without_nan():
    result, _ = run([use("echo", x="a"), use("finish")])
    json.dumps(result.transcript, allow_nan=False)
    assert result.transcript[0] == {"role": "user", "content": "U"}


# --- завершающий инструмент: после него ничего не выполняется ---
def test_calls_after_the_terminal_tool_are_skipped_with_error_results():
    result, client = run([use(("finish", {}), ("echo", dict(x="late")))])
    assert (result.stop, calls) == ("finished", [])
    sent = result.transcript[-1]["content"]
    assert sent[1]["is_error"] is True and sent[1]["content"] == "не выполнено: сессия уже завершена"
    assert "is_error" not in sent[0]


def test_calls_before_the_terminal_tool_still_run():
    result, _ = run([use(("echo", dict(x="a")), ("finish", {})) ])
    assert (result.stop, calls) == ("finished", ["a"])


def test_failing_terminal_call_does_not_end_the_session():
    boom = Tool("finish", "", DONE.input_schema, lambda a: 1 / 0, terminal=True)
    result, _ = run([use(("finish", {}), ("echo", dict(x="a"))), text("продолжаю")], tools=[boom, ECHO])
    assert (result.stop, result.turns, calls) == ("end_turn", 2, ["a"])      # сессия не закончилась, echo выполнен
    bad = run([use("finish", junk=1), use("finish")])[0]
    assert (bad.stop, bad.turns) == ("finished", 2)


# --- стоимость при подмене модели ---
def test_iterations_are_summed_and_fallback_used_is_set():
    step = use("finish", usage=dict(input_tokens=1, output_tokens=1, iterations=[
        dict(input_tokens=1000, output_tokens=100), dict(input_tokens=2000, output_tokens=200, cache_read_input_tokens=3000)]))
    step["model"] = "claude-opus-5"
    result, _ = run([step])
    want = cost_usd(Usage(1000, 100), PRICES) + cost_usd(Usage(2000, 200, 3000), PRICES)
    assert result.cost == pytest.approx(want)
    assert (result.usage.input, result.usage.output, result.usage.cache_read) == (3000, 300, 3000)
    assert result.fallback_used is True and result.model_used == "claude-opus-5"


def test_server_tool_loop_iterations_of_the_requested_model_are_not_a_fallback():
    # Поиск внутри запроса: несколько записей `message` той же модели (у записи `message` поле model необязательно).
    step = use("finish", usage=dict(input_tokens=3000, output_tokens=300, web_search_requests=1, iterations=[
        dict(model="claude-opus-5-5", input_tokens=1000, output_tokens=100),
        dict(input_tokens=2000, output_tokens=200, cache_read_input_tokens=500)]))
    result, _ = run([step])
    assert result.fallback_used is False and result.model_used == "claude-opus-5-5"
    # итерации суммируются вместо верхнего usage, поиск оплачивается один раз
    assert result.cost == pytest.approx(cost_usd(Usage(1000, 100), PRICES) + cost_usd(Usage(2000, 200, 500), PRICES) + 0.01)
    assert result.usage.searches == 1
    assert run([use("finish")])[0].fallback_used is False


def test_documented_fallback_response_is_a_fallback_and_costs_the_sum_of_its_attempts():
    # Пример из документации «Refusals and fallback»: верхний usage это только ответившая попытка.
    step = use("finish", usage=dict(input_tokens=412, output_tokens=264, iterations=[
        dict(type="message", model="claude-fable-5", input_tokens=535, output_tokens=0),
        dict(type="fallback_message", model="claude-opus-4-8", input_tokens=412, output_tokens=264)]))
    step["model"] = "claude-opus-4-8"
    result, _ = run([step], model="claude-fable-5")
    assert result.fallback_used is True and result.model_used == "claude-opus-4-8"
    assert result.cost == pytest.approx(cost_usd(Usage(535, 0), PRICES) + cost_usd(Usage(412, 264), PRICES))


def test_an_iteration_naming_another_model_is_a_fallback_even_if_the_top_model_matches():
    step = use("finish", usage=dict(iterations=[
        dict(type="fallback_message", model="claude-opus-5", input_tokens=10, output_tokens=1)]))
    assert run([step])[0].fallback_used is True


# --- цикл не поднимает исключений ---
def test_unsupported_schema_type_is_an_error_result():
    weird = Tool("echo", "", {"type": "object", "properties": {"x": {"type": "weird"}}}, lambda a: "ok")
    result, client = run([use("echo", x="a"), use("finish")], tools=[weird, DONE])
    assert result.stop == "finished" and client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_failure_after_the_request_keeps_what_was_gathered():
    bad = {"content": [object()], "stop_reason": "end_turn", "usage": dict(input_tokens=1000)}
    result, _ = run([use("echo", x="a", usage=dict(input_tokens=1000)), bad])
    assert result.stop == "error" and result.error and result.turns == 2
    assert result.cost == pytest.approx(2 * cost_usd(Usage(1000), PRICES)) and calls == ["a"]
    json.dumps(result.transcript, allow_nan=False)


def test_failed_stream_error_says_the_request_may_have_been_billed():
    result, _ = run([RuntimeError("обрыв")])
    assert result.stop == "error" and "обрыв" in result.error
    assert "мог быть оплачен" in result.error and "не входит в cost" in result.error


# --- проверка схемы ---
@pytest.mark.parametrize("args", [dict(n=1, extra=2), dict(n=1, kind="c"), dict(n=0), dict(n=11), dict(n=True)])
def test_schema_rejects_extra_property_enum_and_bounds(args):
    schema = {"type": "object", "properties": {"n": {"type": "number", "minimum": 1, "maximum": 10},
                                               "kind": {"type": "string", "enum": ["a", "b"]}},
              "required": ["n"], "additionalProperties": False}
    t = Tool("echo", "", schema, lambda a: calls.append("ran") or "ok")
    result, client = run([use("echo", **args), use("finish")], tools=[t, DONE])
    assert calls == [] and client.requests[1]["messages"][-1]["content"][0]["is_error"] is True


def test_unknown_stop_reason_and_tool_use_without_calls_are_errors():
    assert run([text("x", stop_reason="model_context_window_exceeded")])[0].stop == "error"
    assert run([text("x", stop_reason="tool_use")])[0].stop == "error"


def test_check_enforces_list_size_bounds():
    from traider.brain import _check
    schema = {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 3}
    assert _check(schema, []) and "не меньше 1" in _check(schema, [])
    assert _check(schema, ["a"] * 4) and "не больше 3" in _check(schema, ["a"] * 4)
    assert _check(schema, ["a"]) is None and _check(schema, ["a"] * 3) is None


# --- поиск: лимит на сессию держит API, потому что вся сессия это один ход ---
def _search_tools(request):
    return [t for t in request["tools"] if t.get("name") == "web_search"]


def test_search_is_direct_with_the_session_limit_and_never_changes():
    script = [use("echo", x="a", usage={"web_search_requests": 3}), use("nope"), use("echo", x="b"), use("finish")]
    result, client = run(script, max_searches=8)
    assert result.stop == "finished" and len(client.requests) == 4
    defs = [_search_tools(r) for r in client.requests]
    assert all(len(d) == 1 for d in defs) and all(d == defs[0] for d in defs)
    assert defs[0][0]["allowed_callers"] == ["direct"] and defs[0][0]["max_uses"] == 8


def test_every_follow_up_message_holds_only_tool_results():
    boom = Tool("finish", "", DONE.input_schema, lambda a: 1 / 0, terminal=True)
    good = Tool("done", "", DONE.input_schema, lambda a: "готово", terminal=True)
    script = [use("echo", x="a"), use("nope"), use(("finish", {}), ("echo", dict(x="b"))),
              use(("done", {}), ("echo", dict(x="late")))]
    result, client = run(script, tools=[ECHO, boom, good])
    assert result.stop == "finished" and len(client.requests) == 4
    for r in client.requests[1:]:
        last = r["messages"][-1]
        assert last["role"] == "user" and all(b["type"] == "tool_result" for b in last["content"])
    final = result.transcript[-1]          # шаг с пропущенными после завершающего вызовами
    assert final["role"] == "user" and all(b["type"] == "tool_result" for b in final["content"])
    assert final["content"][1]["is_error"] is True


def test_no_search_tool_in_any_request_when_searches_are_off():
    _, client = run([use("echo", x="a"), use("finish")], max_searches=0)
    assert len(client.requests) == 2 and not any(_search_tools(r) for r in client.requests)


# --- финальная волна, I5: агент видит расход своих потолков ---
def test_status_line_after_each_tool_turn_shows_turns_dollars_and_searches():
    u = dict(input_tokens=100_000, web_search_requests=1)                   # $0.40 и поиск за $0.01 в каждом ответе
    result, client = run([use("echo", x="a", usage=u), use("echo", x="b", usage=u), use("finish")])
    second = client.requests[2]["messages"][-1]["content"]
    assert all(b["type"] == "tool_result" for b in second)
    assert second[-1]["content"].endswith("[Состояние: ход 2 из 40, потрачено $0.82 из $2.00, поисков 2 из 8.]")
    assert second[-1]["content"].startswith("ok b")
    assert "Состояние" in client.requests[1]["messages"][-1]["content"][-1]["content"]

def test_status_line_goes_into_the_last_result_and_has_no_searches_when_they_are_off():
    _, client = run([use(("echo", dict(x="a")), ("echo", dict(x="b"))), use("finish")], max_searches=0)
    first, last = client.requests[1]["messages"][-1]["content"]
    assert first["content"] == "ok a" and last["content"] == "ok b\n\n[Состояние: ход 1 из 40, потрачено $0.00 из $2.00.]"


# --- финальная волна, I6.6: зашифрованные поля поиска не попадают в запись ---
def test_transcript_drops_encrypted_search_fields_but_the_api_gets_them_back():
    from tests.fakes import Block
    found = Block(type="web_search_tool_result", tool_use_id="s1",
                  content=[{"type": "web_search_result", "url": "u", "title": "t", "encrypted_content": "BLOB"}])
    cited = Block(type="text", text="факт", citations=[{"type": "web_search_result_location", "url": "u",
                                                         "cited_text": "c", "encrypted_index": "IDX"}])
    step = use("echo", x="a"); step["content"] = [found, cited, *step["content"]]
    result, client = run([step, use("finish")])
    stored = json.dumps(result.transcript, ensure_ascii=False)
    assert "BLOB" not in stored and "IDX" not in stored and '"url": "u"' in stored and "факт" in stored
    echoed = client.requests[1]["messages"][1]["content"]
    assert echoed[0] is found and echoed[0].content[0]["encrypted_content"] == "BLOB"
    assert echoed[1].citations[0]["encrypted_index"] == "IDX"


# --- финальная волна, M13: срок сессии ---
def test_deadline_is_checked_before_each_request_with_an_injected_clock():
    now = [0.0]
    def slow(a):
        now[0] += 700.0; return "ok"                                    # инструмент «работает» 700 секунд
    tool = Tool("echo", "", ECHO.input_schema, slow)
    result, client = run([use("echo", x="a"), use("echo", x="b"), use("finish")], tools=[tool, DONE],
                         max_seconds=1200, clock=lambda: now[0])
    assert (result.stop, result.turns, len(client.requests)) == ("deadline", 2, 2)

def test_no_deadline_by_default():
    assert run([use("echo", x="a"), use("finish")], clock=lambda: 1e12)[0].stop == "finished"


# --- финальная волна, M21: allowed_callers только у новой версии поиска ---
def test_allowed_callers_only_with_the_search_version_that_needs_it():
    _, opus = run([use("finish")])
    _, haiku = run([use("finish")], model="claude-haiku-5-5")
    (new,), (basic,) = _search_tools(opus.requests[0]), _search_tools(haiku.requests[0])
    assert new["type"] == "web_search_20260209" and new["allowed_callers"] == ["direct"]
    assert basic["type"] == "web_search_20250305" and "allowed_callers" not in basic and basic["max_uses"] == 8
