"""Аналитик: дешёвая модель собирает информацию для участника, у которого нет своих новостей и поиска.

:wk-id: analyst-module
:wk-tags: analyst, agent, budget
:wk-categories: system-architecture

Как это работает. Один объект `Analyst` обслуживает одну сессию участника с `research = "analyst"`.
`briefing(day)` пишет сводку до сессии (до 1200 слов), `research(question)` отвечает на вопрос
основного агента в сессии (до 400 слов). Каждый вызов это один `run_agent` с моделью
`cfg.analyst_model`, замороженным промптом `cfg.analyst_prompt` (файл при работе не читается) и
ценами `cfg.analyst_token_prices`. Инструменты: `get_news` (тот же, что у основного агента),
серверный поиск и завершающий `submit(text)`. Бюджет `cfg.analyst_max_usd` и лимит поисков
`cfg.analyst_max_searches` общие на сводку и все вопросы: каждому вызову передаётся остаток, потому
что каждый вызов для API отдельный ход. Срок каждого вызова `CALL_SECONDS` (600 с). Расход копится в
`usage`, `cost`, `searches`, а `calls` хранит по записи на вызов: `kind` (`briefing` или `research`),
`question` (текст задачи), `stop`, `cost`, `transcript`.
Текст берётся из `submit`, даже если ответ с ним упёрся в потолок: цикл тогда не выполнил
инструмент, но текст оплачен. Иначе берётся последнее текстовое сообщение при `end_turn`, иначе
заглушка. Методы не поднимают исключений. Без остатка бюджета вопрос не уходит в сеть.
"""
import re

from traider.brain import Tool, Usage, run_agent
from traider.config import RunConfig
from traider.market import Market
from traider.tools import news_tool

BRIEFING_WORDS = 1200
ANSWER_WORDS = 400
BRIEFING_FAILED = "Сводка аналитика недоступна."
ANSWER_FAILED = "Аналитик недоступен."
CALL_SECONDS = 600   # срок одного вызова аналитика
BUDGET_SPENT = "Бюджет аналитика на сегодня исчерпан."


def _cut(text: str, words: int) -> str:
    """Первые `words` слов; абзацы и переводы строк внутри остаются."""
    ends = [m.end() for m in re.finditer(r"\S+", text)]
    return text[:ends[words - 1]] if len(ends) > words else text.strip()


def _answer(res) -> str | None:
    """Текст из `submit` последнего ответа модели, иначе текст последнего ответа при `end_turn`."""
    last = next((m["content"] for m in reversed(res.transcript) if m["role"] == "assistant"), [])
    for b in last:
        if b.get("type") == "tool_use" and b.get("name") == "submit":
            if isinstance(t := b["input"].get("text"), str) and t.strip():
                return t
    if res.stop == "end_turn":
        return "\n".join(b["text"] for b in last if b.get("type") == "text").strip() or None
    return None


class Analyst:
    def __init__(self, client, cfg: RunConfig, market: Market):
        self.client, self.cfg = client, cfg
        self.usage, self.cost, self.searches = Usage(), 0.0, 0
        self.calls: list[dict] = []   # по записи на вызов: kind, question, stop, cost, transcript
        self.tools = [news_tool(market), Tool(
            "submit", "Сдать готовый текст и закончить задачу.",
            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"],
             "additionalProperties": False}, lambda a: "Принято.", terminal=True)]

    def _ask(self, kind: str, user: str, words: int) -> str | None:
        cfg = self.cfg
        res = run_agent(self.client, model=cfg.analyst_model, system=cfg.analyst_prompt, user=user,
                        tools=self.tools, prices=cfg.analyst_token_prices, max_turns=cfg.limits.max_turns,
                        max_usd=cfg.analyst_max_usd - self.cost,
                        max_searches=max(cfg.analyst_max_searches - self.searches, 0), max_seconds=CALL_SECONDS)
        self.calls.append({"kind": kind, "question": user, "stop": res.stop, "cost": res.cost,
                           "transcript": res.transcript})
        self.cost += res.cost
        self.searches += res.usage.searches
        self.usage = Usage(*(a + b for a, b in zip(vars(self.usage).values(), vars(res.usage).values())))
        text = _answer(res)
        return None if text is None else _cut(text, words)

    def briefing(self, day: str) -> str:
        user = (f"Сегодня {day}. Собери сводку для торговой сессии. Вселенная бумаг: "
                f"{', '.join(self.cfg.universe)}.")
        try:
            return (self._ask("briefing", user, BRIEFING_WORDS) if self.cost < self.cfg.analyst_max_usd else None) \
                or BRIEFING_FAILED
        except Exception:
            return BRIEFING_FAILED

    def research(self, question: str) -> str:
        if self.cost >= self.cfg.analyst_max_usd:
            return BUDGET_SPENT
        try:
            return self._ask("research", question, ANSWER_WORDS) or ANSWER_FAILED
        except Exception:
            return ANSWER_FAILED
