"""Цикл агента: один разговор модели Claude с набором инструментов и жёсткими потолками.

:wk-id: brain-module
:wk-tags: agent, budget, anthropic
:wk-categories: system-architecture

Как это работает. `run_agent` вручную ведёт разговор через Messages API (стриминг, итоговое
сообщение, адаптивное мышление с кратким изложением (`display: "summarized"`: по умолчанию у этих моделей
текст мышления пуст; на оплату это не влияет), effort в `output_config`, кэш на уровне запроса). После каждого
ответа считается стоимость: при `cost >= max_usd` цикл обрывается до выполнения инструментов этого
ответа. Ходов не больше `max_turns`. Перед каждым запросом сверяется срок: если с начала прошло
`max_seconds` по `clock` (по умолчанию `time.monotonic`), цикл останавливается с `stop="deadline"`. Вход инструмента проверяется по его JSON-схеме до вызова.
Цикл ничего не решает о торговле и никогда не поднимает исключений: сбой клиента это `stop="error"`.
Стоимость ответа: сумма записей `usage.iterations` (это учёт оплаты по попыткам), без них верхний `usage`; поиски из
`server_tool_use` один раз на ответ. `fallback_used` истинно, только если верхний `model` не запрошенный или в
`usage.iterations` есть запись типа `fallback_message`; модель в записях других типов (`message`, советник) не в счёт.
После каждого хода с вызовами инструментов (кроме завершающего) к тексту последнего `tool_result` добавляется
строка состояния: ход из `max_turns`, доллары из `max_usd` и, если поиск включён, поиски из `max_searches`.
Отдельным блоком её не шлют: сообщение должно состоять только из `tool_result`.

Поиск. Лимит поисков держит API: `max_uses` считает поиски за ход, а ход продолжается, пока
последнее сообщение запроса состоит только из блоков `tool_result` (или после `pause_turn`).
Цикл после первого сообщения шлёт только такие сообщения, поэтому вся сессия это один ход, и
`max_uses = max_searches` ограничивает сессию. Поиск вызывается напрямую: у версии с динамической
фильтрацией (`web_search_20260209`) это задаёт `allowed_callers: ["direct"]`, у базовой `web_search_20250305`
вызов прямой и так, поле не шлётся. Динамическая фильтрация выключена намеренно: поиски из
выполнения кода между запросами не суммируются, а так все модели ищут одинаково. Определение
инструмента одно на всю сессию, кэш промпта им не сбрасывается. В записи сессии (`transcript`) у блоков
результатов поиска и цитат нет `encrypted_content` и `encrypted_index`; модели они возвращаются как получены.
"""
import time
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
    stop: str             # "finished" | "end_turn" | "max_turns" | "budget" | "deadline" | "refusal" | "error"
    turns: int
    usage: Usage
    cost: float
    model_used: str
    transcript: list[dict] = field(default_factory=list)
    error: str | None = None
    fallback_used: bool = False


def cost_usd(usage: Usage, prices: Mapping[str, float]) -> float:
    """Цена ОДНОГО запроса. Если в таблице есть `long_prompt_threshold` и запрос (вход + чтение кэша +
    запись кэша) строго длиннее порога, все четыре вида токенов считаются по длинным ставкам
    (`input_long`, `output_long`, `cache_read_long`, `cache_write_long`; конфиг проверяет, что они все есть)."""
    prompt = usage.input + usage.cache_read + usage.cache_write
    sfx = "_long" if "long_prompt_threshold" in prices and prompt > prices["long_prompt_threshold"] else ""
    return (usage.input * prices["input" + sfx] + usage.output * prices["output" + sfx]
            + usage.cache_read * prices["cache_read" + sfx] + usage.cache_write * prices["cache_write" + sfx]
            ) / 1e6 + usage.searches * prices["search"]


_TYPES = {"string": str, "object": dict, "array": list, "boolean": bool, "null": type(None)}


def _is(value, kind) -> bool:
    if kind in ("number", "integer"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        return isfinite(value) and (kind == "number" or float(value).is_integer())
    return isinstance(value, _TYPES[kind])


# Всё, что _check исполняет, и аннотации, которые он игнорирует по смыслу. Схема инструмента
# с другим словом нарушила бы свой же предел молча: тест схем инструментов сверяется с этим набором.
SCHEMA_KEYWORDS = frozenset({"type", "enum", "minimum", "maximum", "minItems", "maxItems", "required",
                             "properties", "additionalProperties", "items", "description"})


def _check(schema: dict, value, path: str = "вход") -> str | None:
    """Первое нарушение схемы или None. Поддержано: SCHEMA_KEYWORDS (type, enum, minimum/maximum,
    minItems/maxItems, required, properties, additionalProperties: false, items; description
    это аннотация)."""
    kinds = schema.get("type")
    if kinds is not None:
        kinds = [kinds] if isinstance(kinds, str) else kinds
        if unknown := [k for k in kinds if k not in _TYPES and k not in ("number", "integer")]:
            return f"{path}: схема с неподдерживаемым типом {unknown}"
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
    if isinstance(value, list):
        if "minItems" in schema and len(value) < schema["minItems"]:
            return f"{path}: число элементов не меньше {schema['minItems']}"
        if "maxItems" in schema and len(value) > schema["maxItems"]:
            return f"{path}: число элементов не больше {schema['maxItems']}"
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


ENCRYPTED = frozenset({"encrypted_content", "encrypted_index"})   # непрозрачные данные поиска: в запись не идут


def _strip(x):
    if isinstance(x, dict):
        return {k: _strip(v) for k, v in x.items() if k not in ENCRYPTED}
    return [_strip(v) for v in x] if isinstance(x, list) else x


def _plain(block) -> dict:
    """Копия блока для записи сессии: без зашифрованных полей поиска. В API уходит сам блок, как получен."""
    return _strip(_clean(block if isinstance(block, dict) else block.model_dump(mode="json")))


def _run_tool(tools: Mapping[str, Tool], name: str, args) -> tuple[str, bool]:
    tool = tools.get(name)
    if tool is None:
        return f"нет инструмента {name}", True
    try:
        if err := _check(tool.input_schema, args):
            return f"неверный вход: {err}", True
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


def _g(obj, name, default=0):
    v = obj.get(name) if isinstance(obj, dict) else getattr(obj, name, None)
    return default if v is None else v


def _usage_of(entry, searches: int = 0) -> Usage:
    return Usage(_g(entry, "input_tokens"), _g(entry, "output_tokens"), _g(entry, "cache_read_input_tokens"),
                 _g(entry, "cache_creation_input_tokens"), searches)


def _status(res: AgentResult, max_turns: int, max_usd: float, max_searches: int) -> str:
    searches = f", поисков {res.usage.searches} из {max_searches}" if max_searches > 0 else ""
    return f"[Состояние: ход {res.turns} из {max_turns}, потрачено ${res.cost:.2f} из ${max_usd:.2f}{searches}.]"


def run_agent(client, *, model: str, system: str, user: str, tools: Sequence[Tool], prices: Mapping[str, float],
              max_turns: int, max_usd: float, max_searches: int, effort: str = "high",
              max_seconds: float = float("inf"), clock: Callable[[], float] = time.monotonic) -> AgentResult:
    """Никогда не поднимает исключений: любой сбой это stop="error" с накопленными ходами, расходом и записью."""
    res = AgentResult("error", 0, Usage(), 0.0, model, [{"role": "user", "content": user}])
    try:
        deadline = clock() + max_seconds
        _loop(client, res, model, system, user, tools, prices, max_turns, max_usd, max_searches, effort,
              lambda: clock() >= deadline)
    except Exception as e:
        res.stop, res.error = "error", f"{type(e).__name__}: {e}"
    return res


def _loop(client, res, model, system, user, tools, prices, max_turns, max_usd, max_searches, effort, late) -> None:
    by_name = {t.name: t for t in tools}
    api_tools = [{"name": t.name, "description": t.description, "input_schema": t.input_schema,
                  "eager_input_streaming": True} for t in tools]
    if max_searches > 0:
        kind = SEARCH_TYPES.get(model, "web_search_20250305")
        search = {"type": kind, "name": "web_search", "max_uses": max_searches}
        if kind != "web_search_20250305":   # у базовой версии вызов и так прямой, у новой по умолчанию нет
            search["allowed_callers"] = ["direct"]
        api_tools.append(search)
    extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"} if model in FALLBACK_MODELS else {}
    messages: list[dict] = [{"role": "user", "content": user}]
    res.stop = "max_turns"
    while res.turns < max_turns:
        if late():   # срок сессии проверяется перед каждым запросом; начатый запрос не обрывается
            res.stop = "deadline"
            return
        res.turns += 1
        try:
            with client.beta.messages.stream(
                model=model, max_tokens=MAX_TOKENS, system=system, tools=api_tools, messages=list(messages),
                thinking={"type": "adaptive", "display": "summarized"}, output_config={"effort": effort},
                cache_control={"type": "ephemeral"}, **extra,
            ) as stream:
                response = stream.get_final_message()
        except Exception as e:
            res.stop = "error"
            res.error = f"{type(e).__name__}: {e}; запрос мог быть оплачен и не входит в cost"
            return
        u = response.usage
        searches = _g(_g(u, "server_tool_use", None), "web_search_requests")
        iterations = _g(u, "iterations", None)
        # Верхний usage покрывает только ответившую попытку; при подмене истина в iterations.
        # Все попытки оцениваются по таблице цен основной модели (приближение).
        steps = [_usage_of(i) for i in iterations] if iterations else [_usage_of(u)]
        steps[0].searches = searches
        for step in steps:
            res.cost += cost_usd(step, prices)
            res.usage = Usage(*(a + b for a, b in zip(vars(res.usage).values(), vars(step).values())))
        res.model_used = getattr(response, "model", None) or model
        # Подмена по признакам из SDK: ответила другая модель или в iterations есть запись `fallback_message`.
        # Записи других типов (`message` серверного цикла, советник) могут называть свою модель, это не подмена.
        res.fallback_used = (res.fallback_used or res.model_used != model
                             or any(_g(i, "type", None) == "fallback_message" for i in iterations or []))
        content = _echo(response.content)
        messages.append({"role": "assistant", "content": content})
        res.transcript.append({"role": "assistant", "content": [_plain(b) for b in content]})
        if res.cost >= max_usd:
            res.stop = "budget"
            return
        reason = response.stop_reason
        if reason == "refusal":
            res.stop = "refusal"
            return
        if reason == "pause_turn":
            continue
        if reason == "max_tokens":
            res.stop, res.error = "error", "ответ оборван по max_tokens"
            return
        uses = [b for b in content if b.type == "tool_use"]
        if reason in ("end_turn", "stop_sequence") and not uses:
            res.stop = "end_turn"
            return
        if reason != "tool_use" or not uses:
            res.stop, res.error = "error", f"неожиданный stop_reason {reason!r} (вызовов инструментов: {len(uses)})"
            return
        results, finished = [], False
        for b in uses:
            if finished:        # сессия объявлена завершённой: остальные вызовы этого ответа не выполняются
                out, failed = "не выполнено: сессия уже завершена", True
            else:
                out, failed = _run_tool(by_name, b.name, b.input)
                finished = not failed and by_name[b.name].terminal
            results.append({"type": "tool_result", "tool_use_id": b.id, "content": out,
                            **({"is_error": True} if failed else {})})
        if not finished:   # строка состояния в тексте последнего результата, отдельным блоком она начала бы новый ход
            results[-1]["content"] += "\n\n" + _status(res, max_turns, max_usd, max_searches)
        # только tool_result: любой другой блок здесь начал бы новый ход и обнулил счёт поисков API
        messages.append({"role": "user", "content": results})
        res.transcript.append({"role": "user", "content": _clean(results)})
        if finished:
            res.stop = "finished"
            return
