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
             "long_prompt_threshold": 100_000, "input_long": 0.50, "output_long": 2.50}
    assert cost_usd(Usage(input=90_000, output=10_000), haiku) == pytest.approx(0.009 + 0.005)
    assert cost_usd(Usage(input=150_000, output=10_000), haiku) == pytest.approx(0.075 + 0.025)
    assert cost_usd(Usage(input=20_000, cache_read=90_000, output=10_000), haiku) == pytest.approx(0.01 + 0.0009 + 0.025)   # длина запроса считается с кэшем


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
    assert r["thinking"] == {"type": "adaptive"} and r["output_config"] == {"effort": "high"}
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
