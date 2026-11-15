"""Цикл агента: один разговор модели Claude с набором инструментов и жёсткими потолками.

:wk-id: brain-module
:wk-tags: agent, budget, anthropic
:wk-categories: system-architecture

Как это работает. `run_agent` вручную ведёт разговор через Messages API (стриминг, итоговое
сообщение, адаптивное мышление, effort в `output_config`, кэш на уровне запроса). После каждого
ответа считается стоимость: при `cost >= max_usd` цикл обрывается до выполнения инструментов этого
ответа. Ходов не больше `max_turns`. Вход инструмента проверяется по его JSON-схеме до вызова.
Цикл ничего не решает о торговле и никогда не поднимает исключений: сбой клиента это `stop="error"`.
"""
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from math import isfinite

MAX_TOKENS = 32000
# Серверный поиск: динамическая фильтрация есть у Opus и Sonnet 5.5; для остальных базовая версия.
SEARCH_TYPES = {"claude-opus-5-5": "web_search_20260209", "claude-sonnet-5-5": "web_search_20260209"}
# Серверная подмена при отказе доступна для Opus и Sonnet, для Haiku нет.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5"}


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    input_schema: dict
    fn: Callable[[dict], str]
    terminal: bool = False


@dataclass
class Usage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_write: int = 0
    searches: int = 0


@dataclass
class AgentResult:
    stop: str             # "finished" | "end_turn" | "max_turns" | "budget" | "refusal" | "error"
    turns: int
    usage: Usage
    cost: float
    model_used: str
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None


def cost_usd(usage: Usage, prices: Mapping[str, float]) -> float:
    """Цена ОДНОГО запроса; длинный запрос (вход + кэш выше порога) идёт по длинным ставкам."""
    prompt = usage.input + usage.cache_read + usage.cache_write
    long = "long_prompt_threshold" in prices and prompt > prices["long_prompt_threshold"]
    rate_in = prices["input_long"] if long else prices["input"]
    rate_out = prices["output_long"] if long else prices["output"]
    return (usage.input * rate_in + usage.output * rate_out + usage.cache_read * prices["cache_read"]
            + usage.cache_write * prices["cache_write"]) / 1e6 + usage.searches * prices["search"]


_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}


def _is(value, kind) -> bool:
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return isfinite(value) and (kind == "number" or float(value).is_integer())
    return isinstance(value, _TYPES[kind])


def _check(schema: dict, value, path: str = "вход") -> str | None:
    """Первое нарушение схемы или None. Поддержано: type, enum, minimum/maximum, required,
    properties, additionalProperties: false, items."""
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if not any(_is(value, k) for k in kinds):
            return f"{path}: нужен тип {'/'.join(kinds)}"
    if "enum" in schema and value not in schema["enum"]:
        return f"{path}: допустимо только {schema['enum']}"
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: не меньше {schema['minimum']}"
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: не больше {schema['maximum']}"
    if isinstance(value, dict):
        props = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                return f"{path}: нет обязательного поля {key}"
        if schema.get("additionalProperties") is False and (extra := sorted(set(value) - set(props))):
            return f"{path}: лишние поля {extra}"
        for key, sub in props.items():
            if key in value and (err := _check(sub, value[key], f"{path}.{key}")):
                return err
    if isinstance(value, list) and "items" in schema:
        for i, item in enumerate(value):
            if err := _check(schema["items"], item, f"{path}[{i}]"):
                return err
    return None


def _clean(x):
    """Только JSON-данные: ненормальные числа становятся null, чужие типы строками."""
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, float):
        return x if isfinite(x) else None
    if x is None or isinstance(x, (str, int, bool)):
        return x
    return str(x)


def _plain(block) -> dict:
    return _clean(block if isinstance(block, dict) else block.model_dump(mode="json"))


def _run_tool(tools: Mapping[str, Tool], name: str, args) -> tuple[str, bool]:
    tool = tools.get(name)
    if tool is None:
        return f"нет инструмента {name}", True
    if err := _check(tool.input_schema, args):
        return f"неверный вход: {err}", True
    try:
        return str(tool.fn(args)), False
    except Exception as e:
        return f"ошибка инструмента: {type(e).__name__}: {e}", True


def _echo(content: list) -> list:
    """После подмены модели посреди ответа блоки мышления и вызовы инструментов до последнего
    блока fallback не возвращаются; сам блок fallback остаётся на месте."""
    marks = [i for i, b in enumerate(content) if b.type == "fallback"]
    if not marks:
        return list(content)
    return [b for i, b in enumerate(content)
            if i >= marks[-1] or b.type not in ("thinking", "redacted_thinking", "tool_use")]


def run_agent(client, *, model: str, system: str, user: str, tools: Sequence[Tool], prices: Mapping[str, float],
              max_turns: int, max_usd: float, max_searches: int, effort: str = "high") -> AgentResult:
    by_name = {t.name: t for t in tools}
    api_tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema,
                  "eager_input_streaming": True} for t in tools]
    if max_searches > 0:
        api_tools.append({"type": SEARCH_TYPES.get(model, "web_search_20250305"), "name": "web_search",
                          "max_uses": max_searches})
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    messages: list[dict] = [{"role": "user", "content": user}]
    transcript: list[dict] = [{"role": "user", "content": user}]
    usage, cost, turns, used = Usage(), 0.0, 0, model

    def done(stop, error=None):
        return AgentResult(stop, turns, usage, cost, used, transcript, error)

    while turns < max_turns:
        turns += 1
        try:
            with client.beta.messages.stream(
                model=model, max_tokens=MAX_TOKENS, system=system, tools=api_tools, messages=list(messages),
                thinking={"type": "adaptive"}, output_config={"effort": effort},
                cache_control={"type": "ephemeral"}, **extra,
            ) as stream:
                response = stream.get_final_message()
            u = response.usage
            step = Usage(u.input_tokens or 0, u.output_tokens or 0, u.cache_read_input_tokens or 0,
                         u.cache_creation_input_tokens or 0,
                         getattr(getattr(u, "server_tool_use", None), "web_search_requests", 0) or 0)
        except Exception as e:
            return done("error", f"{type(e).__name__}: {e}")
        used = getattr(response, "model", None) or model
        usage = Usage(usage.input + step.input, usage.output + step.output, usage.cache_read + step.cache_read,
                      usage.cache_write + step.cache_write, usage.searches + step.searches)
        cost += cost_usd(step, prices)
        content = _echo(response.content)
        messages.append({"role": "assistant", "content": content})
        transcript.append({"role": "assistant", "content": [_plain(b) for b in content]})
        if cost >= max_usd:
            return done("budget")
        reason = response.stop_reason
        if reason == "refusal":
            return done("refusal")
        if reason == "pause_turn":
            continue
        if reason == "max_tokens":
            return done("error", "ответ оборван по max_tokens")
        uses = [b for b in content if b.type == "tool_use"]
        if not uses:
            return done("end_turn")
        results, finished = [], False
        for b in uses:
            out, failed = _run_tool(by_name, b.name, b.input)
            finished |= by_name[b.name].terminal and not failed if b.name in by_name else False
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out,
                            **({"is_error": True} if failed else {})})
        messages.append({"role": "user", "content": results})
        transcript.append({"role": "user", "content": _clean(results)})
        if finished:
            return done("finished")
    return done("max_turns")
