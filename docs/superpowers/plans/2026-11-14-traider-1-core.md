# Traider, план 1 из 3: ядро без сети

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Собрать и покрыть тестами всё, что решает судьбу денег и не требует сети: конфиг прогона, клетку, хранилище, симулятор счёта, встроенный счёт, проверку HP и оценку прогнозов.

**Architecture:** Чистые функции над неизменяемыми структурами (`Account`, `Position`) в `cage.py` и `sim.py`; запись на диск только через `RunStore`; брокер за протоколом `Broker`, первая реализация — встроенный счёт `SimBroker`. Сеть в этом плане не используется вообще.

**Tech Stack:** Python 3.11+, uv, pytest. Только стандартная библиотека (`tomllib`, `dataclasses`, `json`, `hashlib`).

**Spec:** `docs/superpowers/specs/2026-11-14-llm-trader-experiment-design.md`. Следующие планы: `2026-11-14-traider-2-live.md`, `2026-11-14-traider-3-evaluation.md`.

## Global Constraints

- Python не ниже 3.11. Зависимости проекта: `anthropic`, `alpaca-py`; для разработки: `pytest`, `python-frontmatter`. Другие не добавлять.
- Хранение только в файлах JSON и JSONL под `runs/`. Базы данных нет.
- Тесты не ходят в сеть. Тесты с реальным Alpaca помечены `@pytest.mark.paper` и по умолчанию пропускаются.
- Числа клетки из спецификации, дословно: позиция не больше 20% капитала; заявка не меньше $20; стоп от 3% до 15% ниже текущей цены; суммарный риск не больше 8% капитала; не больше 3 покупок за сессию; пол 85% от максимума капитала; 40 ходов и 8 поисков на сессию; проскальзывание 5 б.п. в каждую сторону.
- Ключи читаются только из `.env`. Файл `.env` не коммитится и в тестах не нужен.
- У каждого модуля в `traider/` docstring с полями `:wk-id:`, `:wk-tags:`, `:wk-categories:` по статье `ai-source-code-annotations` базы `kb-self`.
- Каждая задача заканчивается обновлением вики по `CLAUDE.md` и запуском `uv run --with python-frontmatter python tools/check_wiki.py`.
- Коммиты без строк `Co-Authored-By` и без упоминания ассистента.
- Сообщения агенту и пользователю по-русски; имена в коде по-английски.

## Review Focus

Пять ситуаций, о которых спецификация молчит, но которые встретятся первыми. У каждой есть тест в задаче-владельце.

1. Модель присылает мусор в заявке (отрицательная сумма, `NaN`, стоп 0): клетка отвечает отказом `bad_request`, а не падает. Задача 3.
2. Для бумаги нет цены (торги остановлены, нет котировки): покупка отклоняется с `no_price`, расчёт капитала не падает. Задачи 3 и 6.
3. Процесс упал между записью заявки и сохранением счёта: при повторе с тем же `client_order_id` заявка не исполняется второй раз. Задача 6.
4. Файл журнала заявок оборван на середине строки после сбоя: чтение пропускает оборванную последнюю строку, остальные читаются. Задача 4.
5. Цена гэпом открылась ниже стопа: позиция закрывается по цене открытия, а не по стопу. Задача 5.

---

### Task 1: Скелет проекта и перенос дизайна в вики

**Files:**
- Create: `pyproject.toml`, `traider/__init__.py`, `.env.example`, `tests/test_wiki_coverage.py`
- Modify: `.gitignore`
- Create: категории вики `experiment`, `risk-cage`, `system-architecture`, `operations`, `results` в `wiki/categories/` и статьи из списка ниже в `wiki/articles/`
- Modify: `wiki/categories/traider.md`

**Interfaces:**
- Produces: устанавливаемый пакет `traider`; команда тестов `uv run pytest`; статьи вики с ID из шага 1.

- [ ] **Step 1: Написать падающий тест покрытия вики**

```python
# tests/test_wiki_coverage.py
import pathlib

import frontmatter

WIKI = pathlib.Path(__file__).resolve().parent.parent / "wiki"
REQUIRED = {
    "experiment": ["experiment-question", "participants", "pilot", "verdict-criteria",
                   "forecasts-and-calibration", "random-bots-baseline", "self-learning"],
    "risk-cage": ["cage-rules", "hp-floor", "stops"],
    "system-architecture": ["tick-and-session", "agent-tools", "accounts", "model-and-budget", "analytics-page"],
    "operations": ["setup-and-keys", "schedule-and-hosting"],
    "results": [],
}

def test_design_is_in_the_wiki():
    for category, leaves in REQUIRED.items():
        assert (WIKI / "categories" / f"{category}.md").exists(), category
        for leaf in leaves:
            meta = frontmatter.load(WIKI / "articles" / f"{leaf}.md").metadata
            assert category in meta["categories"], leaf
```

- [ ] **Step 2: Создать `pyproject.toml` и пакет, убедиться, что тест падает**

`pyproject.toml`: имя `traider`, `requires-python = ">=3.11"`, зависимости `anthropic`, `alpaca-py`; группа `dev`: `pytest`, `python-frontmatter`; в `[tool.pytest.ini_options]` задать `markers = ["paper: нужен paper-счёт Alpaca"]` и `addopts = "-m 'not paper'"`. `traider/__init__.py` содержит только docstring с полями `wk-`.

В `.gitignore` добавить строки: `runs/`, `logs/`, `.venv/`, `__pycache__/`, `.pytest_cache/`, `.superpowers/`.

`.env.example`:

```
ALPACA_API_KEY=
ALPACA_SECRET_KEY=
ANTHROPIC_API_KEY=
# Только для живого режима, заполняет владелец:
# ALPACA_LIVE_API_KEY=
# ALPACA_LIVE_SECRET_KEY=
```

Run: `uv sync && uv run pytest tests/test_wiki_coverage.py -q`
Expected: FAIL, нет файла `wiki/categories/experiment.md`.

- [ ] **Step 3: Перенести утверждённый дизайн в статьи вики**

Каждая статья описывает «что и зачем» по своему разделу спецификации, без кода. Источник содержания:

| Статья | Раздел спецификации |
|---|---|
| `experiment-question` | 1, 2, 3 |
| `participants` | 5.11 (состав, три сравнения, бюджет состава) |
| `pilot` | 5.11 (пилот) |
| `verdict-criteria` | 5.10 (конец прогона, таблица вердикта) |
| `forecasts-and-calibration` | 5.8 |
| `random-bots-baseline` | 5.9 |
| `self-learning` | 5.7 |
| `cage-rules` | 5.5 |
| `hp-floor` | 5.6 |
| `stops` | 5.2 шаг 3, 5.5, раздел 6 про гэп и спящую машину |
| `tick-and-session` | 5.2, 5.3 |
| `agent-tools` | 5.4 |
| `accounts` | 5.11 (встроенный счёт), 5.13 |
| `model-and-budget` | 4 (оплата), 5.12 |
| `analytics-page` | 5.10 (страница аналитики), макет `docs/mockups/dashboard-mockup.html` |
| `setup-and-keys` | 5.13 |
| `schedule-and-hosting` | 5.14 |

Правила формата: `wiki/articles/traider-wiki-rules.md` и статьи `markup-conventions`, `ai-interaction-guide` базы `kb-self`. В обзоре каждой категории блок `ai` перечисляет все её статьи. Категория `results` создаётся с пустым блоком `ai`. В `wiki/categories/traider.md` блок `ai` дополняется пятью новыми категориями.

- [ ] **Step 4: Проверить**

Run: `uv run pytest tests/test_wiki_coverage.py -q && uv run --with python-frontmatter python tools/check_wiki.py`
Expected: `1 passed`, затем `вики в порядке: 27 статей`.

- [ ] **Step 5: Коммит**

```bash
git add -A
git commit -m "chore: project skeleton, design moved into the wiki"
```

---

### Task 2: Конфиг и заморозка прогона

**Files:**
- Create: `traider/config.py`, `experiment.example.toml`, `tests/test_config.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Limits:
    max_position_share: float = 0.20
    min_order_usd: float = 20.0
    stop_min: float = 0.03
    stop_max: float = 0.15
    max_open_risk_share: float = 0.08
    max_buys_per_session: int = 3
    floor_share: float = 0.85
    max_turns: int = 40
    max_searches: int = 8
    max_session_usd: float = 2.0

@dataclass(frozen=True)
class RunConfig:
    experiment: str
    participant: str
    model: str            # "claude-opus-5-5" | "claude-sonnet-5-5" | "claude-haiku-5-5"
    broker: str           # "alpaca" | "sim"
    research: str         # "self" | "analyst"
    mode: str             # "paper" | "live"
    pilot: bool
    start_equity: float
    sessions_target: int
    window_start: str     # "10:00", время Нью-Йорка
    window_end: str       # "15:30"
    limits: Limits
    universe: tuple[str, ...]
    panel: tuple[str, ...]
    seed: int
    token_prices: dict    # {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0, "search": 0.01}, $ за млн токенов и за один поиск
    criteria: dict
    system_prompt: str
    analyst_model: str | None
    analyst_prompt: str | None
    analyst_max_searches: int
    analyst_max_usd: float

def start_experiment(toml_path: Path, runs_dir: Path, live: bool = False) -> Path
def load_run(run_dir: Path) -> tuple[RunConfig, bool]   # второй элемент: контрольная сумма сошлась
def active_runs(runs_dir: Path) -> list[Path]           # прогоны со статусом active или finishing
```

`start_experiment` создаёт `runs/<experiment>/<participant>/` для каждого участника с файлами `config.json` и `config.sha256` (SHA-256 от байтов `config.json`). Тексты промптов и список вселенной читаются из файлов, указанных в TOML, и встраиваются в `config.json`.

- [ ] **Step 1: Написать `experiment.example.toml`**

```toml
experiment = "pilot-1"
pilot = true
mode = "paper"
start_equity = 1000.0
sessions_target = 3
window_start = "10:00"
window_end = "15:30"
seed = 20261114
universe_file = "data/universe.txt"
system_prompt_file = "traider/prompts/system.md"
analyst_prompt_file = "traider/prompts/analyst.md"
panel = ["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","BRK.B","JPM","V",
         "UNH","XOM","LLY","AVGO","COST","WMT","HD","PG","JNJ","MA"]

[criteria]
min_sessions = 40
max_missed_share = 0.10
min_closed_trades = 15
red_percentile = 25
green_percentile = 90

[prices."claude-opus-5-5"]
input = 4.0
output = 20.0
cache_read = 0.20
cache_write = 5.0
search = 0.01

[prices."claude-sonnet-5-5"]
input = 2.0
output = 10.0
cache_read = 0.10
cache_write = 2.5
search = 0.01

[prices."claude-haiku-5-5"]
input = 0.10
output = 0.50
cache_read = 0.01
cache_write = 0.125
search = 0.01

[[participants]]
name = "haiku"
model = "claude-haiku-5-5"
broker = "sim"
research = "self"
max_session_usd = 0.25

[[participants]]
name = "sonnet"
model = "claude-sonnet-5-5"
broker = "sim"
research = "self"
max_session_usd = 1.0

[[participants]]
name = "opus-a"
model = "claude-opus-5-5"
broker = "alpaca"
research = "self"

[[participants]]
name = "opus-b"
model = "claude-opus-5-5"
broker = "sim"
research = "self"

[[participants]]
name = "opus-analyst"
model = "claude-opus-5-5"
broker = "sim"
research = "analyst"
analyst_model = "claude-haiku-5-5"
analyst_max_searches = 40
analyst_max_usd = 0.30
```

Цены `cache_read`, `cache_write` и `search` для Haiku и цену поиска для всех моделей исполнитель сверяет с актуальной страницей цен Anthropic (ссылка в `shared/live-sources.md` навыка `claude-api`) и исправляет в этом файле.

- [ ] **Step 2: Написать падающие тесты**

```python
# tests/test_config.py
def test_limits_defaults_match_spec():
    lim = Limits()
    assert (lim.max_position_share, lim.min_order_usd) == (0.20, 20.0)
    assert (lim.stop_min, lim.stop_max) == (0.03, 0.15)
    assert (lim.max_open_risk_share, lim.max_buys_per_session) == (0.08, 3)
    assert lim.floor_share == 0.85
    assert (lim.max_turns, lim.max_searches, lim.max_session_usd) == (40, 8, 2.0)

# Фикстура example лежит в tests/conftest.py: копирует experiment.example.toml во временный каталог вместе с файлами
# промптов и вселенной. Вселенная в тестах: 20 бумаг панели плюс SPY, QQQ, XLE, XLF, XLU (25 тикеров).
def test_start_creates_one_frozen_run_per_participant(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    assert sorted(p.name for p in exp.iterdir()) == ["haiku", "opus-a", "opus-analyst", "opus-b", "sonnet"]
    cfg, valid = load_run(exp / "opus-a")
    assert valid and (cfg.model, cfg.broker, cfg.research) == ("claude-opus-5-5", "alpaca", "self")
    assert cfg.token_prices["input"] == 4.0 and cfg.limits.max_session_usd == 2.0
    analyst, _ = load_run(exp / "opus-analyst")
    assert (analyst.research, analyst.analyst_model, analyst.analyst_max_searches) == ("analyst", "claude-haiku-5-5", 40)
    assert load_run(exp / "haiku")[0].limits.max_session_usd == 0.25

def test_edited_frozen_config_is_reported_invalid(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    path = run / "config.json"
    path.write_text(path.read_text().replace('"max_buys_per_session": 3', '"max_buys_per_session": 30'))
    cfg, valid = load_run(run)
    assert valid is False and cfg.limits.max_buys_per_session == 30

def test_start_refuses_to_overwrite(tmp_path, example):
    start_experiment(example, tmp_path / "runs")
    with pytest.raises(FileExistsError):
        start_experiment(example, tmp_path / "runs")

def test_live_mode_needs_explicit_flag(tmp_path, example_live):   # тот же TOML с mode = "live"
    with pytest.raises(ValueError, match="live"):
        start_experiment(example_live, tmp_path / "runs")
    assert start_experiment(example_live, tmp_path / "runs", live=True).exists()

def test_unknown_model_or_broker_is_rejected(tmp_path, example_bad_broker):
    with pytest.raises(ValueError):
        start_experiment(example_bad_broker, tmp_path / "runs")
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_config.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.config`.

- [ ] **Step 4: Реализовать `traider/config.py`**

TOML читается через `tomllib`. `config.json` пишется через `json.dumps(..., sort_keys=True, ensure_ascii=False, indent=1)`. `active_runs` пока возвращает все каталоги с `config.json`; фильтр по статусу добавляется в задаче 4.

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_config.py -q`
Expected: `6 passed`.

- [ ] **Step 6: Вики и коммит**

Дополнить статью `participants` разделом о том, как состав задаётся в TOML. Создать `wiki/sources.json` с двумя источниками: код (`traider/**/*.py`) и документы (`docs/**/*.md`); схема файла берётся из статей `source-code-plugin` и `markdown-files-plugin` базы `kb-self`. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: experiment config and frozen runs"
```

---

### Task 3: Клетка

**Files:**
- Create: `traider/cage.py`, `tests/test_cage.py`

**Interfaces:**
- Consumes: `Limits` из задачи 2.
- Produces:

```python
@dataclass(frozen=True)
class Position:
    symbol: str
    qty: float
    entry_price: float
    stop_price: float

@dataclass(frozen=True)
class Account:
    cash: float
    positions: tuple[Position, ...]
    peak_equity: float

@dataclass(frozen=True)
class BuyRequest:
    symbol: str
    notional: float
    stop_price: float

@dataclass(frozen=True)
class SessionFacts:
    buys_done: int
    closed_today: frozenset[str]
    forecast_symbols: frozenset[str]
    panel_done: bool

@dataclass(frozen=True)
class Reject:
    code: str
    detail: str   # русская фраза с числами для агента

def equity(acct: Account, prices: Mapping[str, float]) -> float      # бумага без цены считается по entry_price
def floor(acct: Account, limits: Limits) -> float
def hp(acct: Account, prices: Mapping[str, float], limits: Limits) -> float
def is_dead(acct: Account, prices: Mapping[str, float], limits: Limits) -> bool
def open_risk(acct: Account, prices: Mapping[str, float]) -> float
def check_buy(req: BuyRequest, acct: Account, prices: Mapping[str, float], facts: SessionFacts,
              universe: frozenset[str], limits: Limits) -> Reject | None
def check_raise_stop(symbol: str, new_stop: float, acct: Account, prices: Mapping[str, float],
                     limits: Limits) -> Reject | None
```

Порядок проверок в `check_buy` фиксирован, возвращается первая сработавшая: `bad_request`, `not_in_universe`, `no_price`, `no_panel_forecasts`, `no_symbol_forecast`, `too_many_buys`, `rebuy_same_day`, `order_too_small`, `stop_out_of_range`, `stop_below_existing`, `position_too_large`, `not_enough_cash`, `open_risk_too_high`.

Коды `check_raise_stop`: `bad_request`, `no_position`, `no_price`, `stop_not_higher`, `stop_out_of_range`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_cage.py
LIM = Limits()
UNIVERSE = frozenset({"AAPL", "XLE", "SPY"})
PRICES = {"AAPL": 200.0, "XLE": 100.0}
EMPTY = Account(cash=1000.0, positions=(), peak_equity=1000.0)
FACTS = SessionFacts(0, frozenset(), frozenset({"AAPL", "XLE"}), True)

def code(symbol="AAPL", notional=150.0, stop=186.0, acct=EMPTY, facts=FACTS, prices=PRICES):
    r = check_buy(BuyRequest(symbol, notional, stop), acct, prices, facts, UNIVERSE, LIM)
    return r.code if r else None

def test_valid_buy_is_accepted():
    assert code() is None

@pytest.mark.parametrize("kwargs, expected", [
    (dict(notional=float("nan")), "bad_request"),
    (dict(notional=-5.0), "bad_request"),
    (dict(stop=0.0), "bad_request"),
    (dict(symbol="TSLA"), "not_in_universe"),
    (dict(symbol="SPY"), "no_price"),
    (dict(facts=replace(FACTS, panel_done=False)), "no_panel_forecasts"),
    (dict(facts=replace(FACTS, forecast_symbols=frozenset())), "no_symbol_forecast"),
    (dict(facts=replace(FACTS, buys_done=3)), "too_many_buys"),
    (dict(facts=replace(FACTS, closed_today=frozenset({"AAPL"}))), "rebuy_same_day"),
    (dict(notional=19.99), "order_too_small"),
    (dict(stop=195.0), "stop_out_of_range"),
    (dict(stop=169.0), "stop_out_of_range"),
    (dict(notional=200.01), "position_too_large"),
])
def test_rejections(kwargs, expected):
    assert code(**kwargs) == expected

@pytest.mark.parametrize("kwargs", [dict(notional=20.0), dict(notional=200.0), dict(stop=194.0), dict(stop=170.0)])
def test_boundaries_are_inclusive(kwargs):
    assert code(**kwargs) is None

def test_not_enough_cash():
    acct = Account(100.0, (Position("XLE", 9.0, 100.0, 90.0),), 1000.0)   # капитал 1000, денег 100
    assert code(notional=150.0, acct=acct) == "not_enough_cash"

def test_open_risk_limit_is_8_percent_of_equity():
    acct = Account(500.0, (Position("XLE", 5.0, 100.0, 88.0),), 1000.0)   # риск 60 при лимите 80
    assert code(notional=200.0, stop=170.0, acct=acct) == "open_risk_too_high"   # +30
    assert code(notional=100.0, stop=170.0, acct=acct) is None                   # +15

def test_add_on_cannot_lower_the_stop():
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, 186.0),), 1000.0)
    assert code(notional=50.0, stop=180.0, acct=acct) == "stop_below_existing"
    assert code(notional=50.0, stop=190.0, acct=acct) is None

def test_floor_hp_and_death():
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, 186.0),), 1040.0)
    assert floor(acct, LIM) == pytest.approx(884.0)
    assert hp(acct, {"AAPL": 200.0}, LIM) == pytest.approx(116.0)
    assert is_dead(acct, {"AAPL": 200.0}, LIM) is False
    assert is_dead(Account(884.0, (), 1040.0), {}, LIM) is False      # ровно на полу: жив
    assert is_dead(Account(883.99, (), 1040.0), {}, LIM) is True      # строго ниже пола: погиб
    assert equity(acct, {}) == pytest.approx(1000.0)                  # нет цены: считаем по цене входа

def test_open_risk_ignores_positions_already_below_stop():
    acct = Account(0.0, (Position("AAPL", 1.0, 200.0, 186.0),), 1000.0)
    assert open_risk(acct, {"AAPL": 180.0}) == 0.0
    assert open_risk(acct, {"AAPL": 200.0}) == pytest.approx(14.0)

@pytest.mark.parametrize("symbol, new_stop, expected", [
    ("XLE", 95.0, "no_position"), ("AAPL", 186.0, "stop_not_higher"),
    ("AAPL", 195.0, "stop_out_of_range"), ("AAPL", float("inf"), "bad_request"), ("AAPL", 190.0, None),
])
def test_raise_stop(symbol, new_stop, expected):
    acct = Account(900.0, (Position("AAPL", 0.5, 200.0, 186.0),), 1000.0)
    r = check_raise_stop(symbol, new_stop, acct, PRICES, LIM)
    assert (r.code if r else None) == expected
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_cage.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.cage`.

- [ ] **Step 3: Реализовать `traider/cage.py`**

Правила расчёта:
- диапазон стопа: `price * (1 - 0.15) <= stop <= price * (1 - 0.03)`;
- размер позиции: `qty_old * price + notional <= 0.20 * equity`;
- риск после покупки считается со стопом новой заявки на всю позицию: `(qty_old + notional / price) * (price - stop)` вместо прежнего риска этой бумаги;
- `bad_request`: сумма или стоп не являются конечным положительным числом.

Модуль не импортирует ничего, что делает ввод-вывод.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_cage.py -q`
Expected: все тесты проходят.

- [ ] **Step 5: Вики и коммит**

В статью `cage-rules` добавить таблицу кодов отказа с русским описанием каждого. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: risk cage as pure functions"
```

---

### Task 4: Хранилище прогона

**Files:**
- Create: `traider/store.py`, `tests/test_store.py`
- Modify: `traider/config.py` (`active_runs` фильтрует по статусу)

**Interfaces:**
- Consumes: `SessionFacts`, `Account`, `Position` из задачи 3.
- Produces:

```python
class RunStore:
    def __init__(self, run_dir: Path)
    def append_order(self, event: dict) -> dict            # добавляет поля seq и ts, пишет строку и делает fsync
    def orders(self, day: str | None = None) -> list[dict]
    def find_order(self, client_order_id: str) -> dict | None
    def start_session(self, day: str, now: datetime) -> bool          # False, если запись за день уже есть
    def finish_session(self, day: str, status: str, record: dict) -> None
    def session(self, day: str) -> dict | None
    def sessions(self) -> list[dict]
    def stale_started(self, now: datetime, minutes: int = 30) -> list[str]   # дни с зависшим статусом started
    def state(self) -> dict
    def update_state(self, **changes) -> dict
    def save_prices(self, day: str, prices: dict[str, float]) -> None
    def prices(self, day: str) -> dict[str, float] | None
    def append_forecasts(self, day: str, items: list[dict]) -> None   # item: {"symbol", "p"}
    def forecasts(self) -> list[dict]                                  # {"day", "symbol", "p"}
    def facts(self, day: str, panel: Sequence[str]) -> SessionFacts
    def account(self) -> Account | None                                # для встроенного счёта
    def save_account(self, acct: Account) -> None
```

Файлы в каталоге прогона: `orders.jsonl`, `forecasts.jsonl`, `state.json`, `sessions/<day>.json`, `prices/<day>.json`.

Событие в `orders.jsonl`:

```json
{"seq": 1, "ts": "2026-12-01T15:04:11+00:00", "day": "2026-12-01", "kind": "buy",
 "symbol": "XLE", "notional": 150.0, "qty": 1.2339, "price": 121.56, "stop_price": 113.0,
 "thesis": "...", "reason": null, "client_order_id": "pilot-1/opus-b/2026-12-01/1", "broker_order_id": null}
```

Значения `kind`: `buy`, `close`, `raise_stop`, `stop_fill`, `liquidate`, `reject`. Статусы сессии: `started`, `ok`, `cap`, `failed`, `missed`. Статусы прогона в `state.json`: `active`, `finishing`, `finished`, `dead`, `invalid`. Начальное состояние: `{"status": "active", "notes": "", "last_sync": null}`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_store.py
def test_orders_get_increasing_seq_and_survive_reopen(tmp_path):
    s = RunStore(tmp_path)
    assert s.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "XLE", "client_order_id": "x/1"})["seq"] == 1
    assert RunStore(tmp_path).append_order({"day": "2026-12-01", "kind": "reject", "symbol": "AAPL", "reason": "no_price"})["seq"] == 2
    assert [e["kind"] for e in RunStore(tmp_path).orders("2026-12-01")] == ["buy", "reject"]
    assert RunStore(tmp_path).find_order("x/1")["symbol"] == "XLE"

def test_truncated_last_line_is_skipped(tmp_path):
    s = RunStore(tmp_path)
    s.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "XLE"})
    with open(tmp_path / "orders.jsonl", "a") as f:
        f.write('{"seq": 2, "kind": "bu')
    assert len(RunStore(tmp_path).orders()) == 1
    assert RunStore(tmp_path).append_order({"day": "2026-12-01", "kind": "close", "symbol": "XLE"})["seq"] == 2

def test_session_can_start_only_once_per_day(tmp_path):
    s, now = RunStore(tmp_path), datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
    assert s.start_session("2026-12-01", now) is True
    assert s.start_session("2026-12-01", now) is False
    assert s.session("2026-12-01")["status"] == "started"
    assert s.stale_started(now + timedelta(minutes=29)) == []
    assert s.stale_started(now + timedelta(minutes=31)) == ["2026-12-01"]
    s.finish_session("2026-12-01", "ok", {"cost": 0.9})
    assert (s.session("2026-12-01")["status"], s.session("2026-12-01")["cost"]) == ("ok", 0.9)
    assert s.stale_started(now + timedelta(hours=5)) == []

def test_facts_come_from_the_ledger(tmp_path):
    s, day, panel = RunStore(tmp_path), "2026-12-01", ["AAPL", "MSFT"]
    assert s.facts(day, panel) == SessionFacts(0, frozenset(), frozenset(), False)
    s.append_forecasts(day, [{"symbol": "AAPL", "p": 0.6}, {"symbol": "XLE", "p": 0.55}])
    assert s.facts(day, panel).panel_done is False
    s.append_forecasts(day, [{"symbol": "MSFT", "p": 0.4}])
    for kind, symbol in [("buy", "XLE"), ("reject", "AAPL"), ("close", "JPM"), ("stop_fill", "COST")]:
        s.append_order({"day": day, "kind": kind, "symbol": symbol})
    s.append_order({"day": "2026-11-30", "kind": "buy", "symbol": "NVDA"})
    f = s.facts(day, panel)
    assert (f.buys_done, f.panel_done) == (1, True)
    assert f.closed_today == frozenset({"JPM", "COST"}) and f.forecast_symbols == frozenset({"AAPL", "XLE", "MSFT"})

def test_state_and_account_roundtrip(tmp_path):
    s = RunStore(tmp_path)
    assert s.state()["status"] == "active" and s.account() is None
    s.update_state(status="dead")
    acct = Account(850.0, (Position("AAPL", 0.75, 200.1, 186.0),), 1000.0)
    s.save_account(acct)
    assert RunStore(tmp_path).state()["status"] == "dead" and RunStore(tmp_path).account() == acct

def test_active_runs_skips_finished_and_dead(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    RunStore(exp / "haiku").update_state(status="dead")
    RunStore(exp / "sonnet").update_state(status="finishing")
    assert sorted(p.name for p in active_runs(tmp_path / "runs")) == ["opus-a", "opus-analyst", "opus-b", "sonnet"]
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_store.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.store`.

- [ ] **Step 3: Реализовать `traider/store.py` и фильтр в `active_runs`**

`state.json`, записи сессий и снимки цен пишутся через временный файл и `os.replace`, чтобы сбой не оставил половину файла.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 5: Вики и коммит**

Создать статью `run-files` в категории `system-architecture`: какие файлы лежат в каталоге прогона и что в них записано. Обновить блок `ai` категории. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: run storage with append-only order ledger"
```

---

### Task 5: Симулятор счёта

**Files:**
- Create: `traider/sim.py`, `tests/test_sim.py`

**Interfaces:**
- Consumes: `Account`, `Position` из задачи 3.
- Produces:

```python
SLIPPAGE = 0.0005

@dataclass(frozen=True)
class Fill:
    symbol: str
    qty: float
    price: float     # цена исполнения с учётом проскальзывания
    kind: str        # "buy" | "close" | "stop_fill" | "liquidate"

def apply_buy(acct: Account, symbol: str, notional: float, price: float, stop_price: float) -> tuple[Account, Fill]
def apply_close(acct: Account, symbol: str, fraction: float, price: float, kind: str = "close") -> tuple[Account, Fill]
def apply_stops(acct: Account, lows: Mapping[str, float], opens: Mapping[str, float]) -> tuple[Account, list[Fill]]
def mark_peak(acct: Account, prices: Mapping[str, float]) -> Account
```

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_sim.py
START = Account(1000.0, (), 1000.0)

def test_buy_pays_slippage_and_sets_the_stop():
    acct, fill = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    assert acct.cash == pytest.approx(850.0)
    assert fill.price == pytest.approx(200.10) and fill.qty == pytest.approx(150.0 / 200.10)
    assert acct.positions == (Position("AAPL", fill.qty, fill.price, 186.0),)

def test_add_on_merges_and_takes_the_new_stop():
    acct, _ = apply_buy(START, "AAPL", 100.0, 200.0, 186.0)
    acct, _ = apply_buy(acct, "AAPL", 100.0, 210.0, 195.0)
    (p,) = acct.positions
    assert p.stop_price == 195.0 and p.qty == pytest.approx(100 / 200.10 + 100 / 210.105)
    assert p.entry_price == pytest.approx(200.0 / p.qty)          # средняя цена входа

def test_close_sells_a_fraction_with_slippage():
    acct, buy = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    half, fill = apply_close(acct, "AAPL", 0.5, 210.0)
    assert fill.price == pytest.approx(210.0 * 0.9995) and fill.qty == pytest.approx(buy.qty / 2)
    assert half.cash == pytest.approx(850.0 + fill.qty * fill.price)
    gone, _ = apply_close(half, "AAPL", 1.0, 210.0)
    assert gone.positions == ()

def test_close_of_unknown_symbol_or_bad_fraction_raises():
    with pytest.raises(KeyError):
        apply_close(START, "AAPL", 1.0, 200.0)
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    with pytest.raises(ValueError):
        apply_close(acct, "AAPL", 1.5, 200.0)

def test_stop_fills_at_stop_or_at_open_after_a_gap():
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    same, fills = apply_stops(acct, {"AAPL": 187.0}, {"AAPL": 199.0})
    assert fills == [] and same == acct
    hit, (f,) = apply_stops(acct, {"AAPL": 185.0}, {"AAPL": 199.0})
    assert f.kind == "stop_fill" and f.price == pytest.approx(186.0 * 0.9995) and hit.positions == ()
    _, (gap,) = apply_stops(acct, {"AAPL": 178.0}, {"AAPL": 180.0})
    assert gap.price == pytest.approx(180.0 * 0.9995)
    assert apply_stops(acct, {}, {})[1] == []                      # нет данных по бумаге: стоп не трогаем

def test_peak_only_goes_up():
    acct, _ = apply_buy(START, "AAPL", 150.0, 200.0, 186.0)
    assert mark_peak(acct, {"AAPL": 260.0}).peak_equity > 1000.0
    assert mark_peak(acct, {"AAPL": 190.0}).peak_equity == 1000.0
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_sim.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.sim`.

- [ ] **Step 3: Реализовать `traider/sim.py`**

Цена покупки: `price * (1 + SLIPPAGE)`. Цена любой продажи, включая стоп: `price * (1 - SLIPPAGE)`. Цена стопа до проскальзывания: `min(stop_price, open)`.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest tests/test_sim.py -q`
Expected: `6 passed`.

- [ ] **Step 5: Вики и коммит**

В статью `accounts` добавить раздел «Как встроенный счёт исполняет заявки»: проскальзывание, правило стопа при гэпе. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: account simulator with slippage and stop fills"
```

---

### Task 6: Протоколы рынка и брокера, встроенный счёт

**Files:**
- Create: `traider/market.py` (только протокол и `Bar`), `traider/brokers.py`, `tests/fakes.py`, `tests/broker_contract.py`, `tests/test_sim_broker.py`

**Interfaces:**
- Consumes: `RunStore` (задача 4), `apply_*`, `Fill`, `mark_peak` (задача 5), `Account` (задача 3), `RunConfig` (задача 2).
- Produces:

```python
# traider/market.py
@dataclass(frozen=True)
class Bar:
    day: str
    open: float
    high: float
    low: float
    close: float
    volume: float

class Market(Protocol):
    def is_open(self) -> bool
    def now(self) -> datetime                                             # с часовым поясом America/New_York
    def last_prices(self, symbols: Iterable[str]) -> dict[str, float]     # бумаги без цены отсутствуют в ответе
    def trading_days(self, start: str, end: str) -> list[str]            # торговые дни от start до end включительно, "YYYY-MM-DD"
    def daily_bars(self, symbols: Iterable[str], days: int) -> dict[str, list[Bar]]
    def lows_since(self, symbols: Iterable[str], since: datetime) -> dict[str, tuple[float, float]]   # (минимум, первая цена открытия)
    def news(self, symbols: Sequence[str] | None, limit: int) -> list[dict]   # {"ts", "headline", "summary", "symbols"}
    def fractionable(self, symbols: Iterable[str]) -> set[str]

# traider/brokers.py
class BrokerRejected(Exception): ...
class NoPrice(Exception): ...

class Broker(Protocol):
    def account(self) -> Account
    def prices(self, symbols: Iterable[str]) -> dict[str, float]
    def buy(self, day: str, symbol: str, notional: float, stop_price: float, thesis: str, client_order_id: str) -> Fill
    def close(self, day: str, symbol: str, fraction: float, reason: str, client_order_id: str) -> Fill
    def raise_stop(self, day: str, symbol: str, stop_price: float) -> None
    def ensure_stops(self) -> list[str]            # бумаги, которым стоп выставлен сейчас
    def sync(self, day: str) -> list[dict]         # новые события журнала: сработавшие стопы
    def liquidate(self, day: str) -> list[Fill]

class SimBroker:
    def __init__(self, cfg: RunConfig, store: RunStore, market: Market)

# tests/fakes.py
class FakeMarket:   # реализует Market; поля prices, bars, lows, news_items, open, clock задаются в тесте; торговые дни — будни
def ny(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime   # время в поясе America/New_York
```

Каждый метод брокера, меняющий счёт, сам пишет событие в журнал заявок через `store.append_order` и после этого сохраняет счёт. Клетка здесь не вызывается: её вызывает слой инструментов во втором плане.

- [ ] **Step 1: Написать контрактные тесты, общие для всех брокеров**

```python
# tests/broker_contract.py
def run_contract(make_broker, market):
    """make_broker() возвращает новый экземпляр над тем же хранилищем."""
    b = make_broker()
    assert b.account().cash == pytest.approx(1000.0) and b.account().positions == ()
    fill = b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    acct = b.account()
    assert fill.kind == "buy" and acct.cash == pytest.approx(850.0, abs=0.5)
    assert [p.symbol for p in acct.positions] == ["AAPL"] and acct.positions[0].stop_price == 186.0
    again = make_broker().buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")      # повтор той же заявки
    assert again.qty == pytest.approx(fill.qty) and make_broker().account().cash == pytest.approx(acct.cash)
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    assert make_broker().account().positions[0].stop_price == 190.0
    half = b.close("2026-12-01", "AAPL", 0.5, "причина", "x/2")
    assert half.qty == pytest.approx(fill.qty / 2, rel=1e-3)
    assert [f.symbol for f in b.liquidate("2026-12-01")] == ["AAPL"] and b.account().positions == ()
```

- [ ] **Step 2: Написать падающие тесты встроенного счёта**

```python
# tests/test_sim_broker.py
@pytest.fixture
def env(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    cfg, _ = load_run(run)
    market = FakeMarket(prices={"AAPL": 200.0, "XLE": 100.0})
    return cfg, RunStore(run), market

def test_contract(env):
    cfg, store, market = env
    run_contract(lambda: SimBroker(cfg, store, market), market)

def test_every_change_is_in_the_ledger(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    b.raise_stop("2026-12-01", "AAPL", 190.0)
    b.close("2026-12-01", "AAPL", 1.0, "причина", "x/2")
    events = store.orders()
    assert [e["kind"] for e in events] == ["buy", "raise_stop", "close"]
    assert events[0]["client_order_id"] == "x/1" and events[0]["thesis"] == "тезис" and events[0]["stop_price"] == 186.0

def test_buy_without_a_price_raises_and_changes_nothing(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    with pytest.raises(NoPrice):
        b.buy("2026-12-01", "MSFT", 100.0, 90.0, "тезис", "x/1")
    assert b.account().cash == 1000.0 and store.orders() == []

def test_crash_between_ledger_and_account_does_not_double_fill(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    store.save_account(Account(1000.0, (), 1000.0))                 # как будто счёт не успел сохраниться
    again = SimBroker(cfg, store, market).buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert again.qty == pytest.approx(150.0 / 200.10)
    assert SimBroker(cfg, store, market).account().cash == pytest.approx(850.0)
    assert len(store.orders()) == 1

def test_sync_fills_stops_retroactively(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    assert b.sync("2026-12-02") == []
    market.lows = {"AAPL": (178.0, 180.0)}                          # гэп ниже стопа, пока машина спала
    (event,) = b.sync("2026-12-02")
    assert event["kind"] == "stop_fill" and event["price"] == pytest.approx(180.0 * 0.9995)
    assert b.account().positions == () and b.sync("2026-12-02") == []

def test_account_carries_the_peak(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 200.0, 186.0, "тезис", "x/1")
    market.prices["AAPL"] = 300.0
    assert b.account().peak_equity > 1090.0
    market.prices["AAPL"] = 200.0
    assert SimBroker(cfg, store, market).account().peak_equity > 1090.0

def test_ensure_stops_is_a_no_op_for_the_built_in_account(env):
    cfg, store, market = env
    assert SimBroker(cfg, store, market).ensure_stops() == []
```

- [ ] **Step 3: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_sim_broker.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.brokers`.

- [ ] **Step 4: Реализовать `traider/market.py`, `traider/brokers.py`, `tests/fakes.py`**

`SimBroker` при первом обращении создаёт счёт `Account(cfg.start_equity, (), cfg.start_equity)`. Повтор заявки определяется через `store.find_order(client_order_id)`: если событие есть, счёт восстанавливается повторным применением событий журнала, а не новой сделкой. `account()` при каждом вызове обновляет максимум капитала через `mark_peak` и сохраняет его. `sync` вызывает `market.lows_since` с временем прошлой сверки из `state["last_sync"]` и применяет `apply_stops`.

- [ ] **Step 5: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 6: Вики и коммит**

В статью `accounts` добавить раздел о том, что брокер пишет в журнал заявок и как обрабатывается повтор заявки. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: broker protocol and built-in simulated account"
```

---

### Task 7: Проверка HP и гибель прогона

**Files:**
- Create: `traider/safety.py`, `tests/test_safety.py`

**Interfaces:**
- Consumes: `Broker`, `RunStore`, `RunConfig`, `is_dead`, `hp`, `floor`.
- Produces:

```python
@dataclass(frozen=True)
class SafetyReport:
    equity: float
    floor: float
    hp: float
    died: bool
    stops_placed: tuple[str, ...]
    stop_fills: tuple[str, ...]

def run_safety(cfg: RunConfig, store: RunStore, broker: Broker, day: str) -> SafetyReport
```

Порядок внутри `run_safety`: сверка (`broker.sync`), расчёт капитала и пола, при капитале ниже пола — `broker.liquidate` и статус `dead`, иначе `broker.ensure_stops`. Для прогона со статусом `dead` или `finished` функция ничего не делает и возвращает отчёт с `died=False`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_safety.py
def test_healthy_run_reports_hp(env):                      # env как в задаче 6
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 200.0, 186.0, "тезис", "x/1")
    r = run_safety(cfg, store, b, "2026-12-01")
    assert r.died is False and r.floor == pytest.approx(850.0, abs=0.2) and r.hp == pytest.approx(r.equity - r.floor)
    assert store.state()["status"] == "active"

def test_run_below_the_floor_is_liquidated_and_marked_dead(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    store.save_account(Account(0.0, (Position("AAPL", 5.0, 200.0, 1.0),), 1000.0))   # стоп далеко, чтобы сработал именно пол
    market.prices["AAPL"] = 169.0                                                    # капитал 845 при поле 850
    r = run_safety(cfg, store, b, "2026-12-02")
    assert r.died is True and store.state()["status"] == "dead"
    assert b.account().positions == () and store.orders()[-1]["kind"] == "liquidate"
    again = run_safety(cfg, store, b, "2026-12-02")
    assert again.died is False and len(store.orders()) == 1

def test_floor_ratchets_up_with_the_peak(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 200.0, 186.0, "тезис", "x/1")
    market.prices["AAPL"] = 300.0
    high = run_safety(cfg, store, b, "2026-12-02")
    market.prices["AAPL"] = 200.0
    low = run_safety(cfg, store, b, "2026-12-03")
    assert low.floor == pytest.approx(high.floor) and low.floor > 900.0

def test_stop_fills_found_by_sync_are_reported(env):
    cfg, store, market = env
    b = SimBroker(cfg, store, market)
    b.buy("2026-12-01", "AAPL", 150.0, 186.0, "тезис", "x/1")
    market.lows = {"AAPL": (185.0, 199.0)}
    assert run_safety(cfg, store, b, "2026-12-02").stop_fills == ("AAPL",)
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_safety.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.safety`.

- [ ] **Step 3: Реализовать `traider/safety.py`**

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 5: Вики и коммит**

В статью `hp-floor` добавить раздел «Что происходит при гибели прогона». Запустить проверку вики.

```bash
git add -A
git commit -m "feat: HP check, floor ratchet and run death"
```

---

### Task 8: Оценка прогнозов

**Files:**
- Create: `traider/forecasts.py`, `tests/test_forecasts.py`

**Interfaces:**
- Produces:

```python
@dataclass(frozen=True)
class Scored:
    day: str
    symbol: str
    p: float
    outcome: int       # 1, если бумага обогнала SPY от закрытия day до закрытия через 5 торговых дней
    missing: bool      # прогноз не был дан и оценён как 0.5

def score(forecasts: Sequence[dict], closes: Mapping[str, Mapping[str, float]], trading_days: Sequence[str],
          session_days: Sequence[str], panel: Sequence[str], horizon: int = 5, benchmark: str = "SPY") -> list[Scored]
def brier(scored: Sequence[Scored]) -> float
def reference(scored: Sequence[Scored]) -> float                    # оценка прогноза «всегда называть фактическую долю»
def bootstrap_diff(scored: Sequence[Scored], block: int = 5, n: int = 2000, seed: int = 0) -> tuple[float, float]   # 90% интервал разности brier - reference
def calibration(scored: Sequence[Scored], edges: Sequence[float] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)) -> list[dict]   # {"lo","hi","n","mean_p","freq"}
def summary_text(scored: Sequence[Scored]) -> str                   # строки для контекста агента
```

В `score` попадают только те пары «день сессии, бумага», для которых в `trading_days` есть день через `horizon` торговых дней и есть цены закрытия. Прогнозы по бумагам вне панели оцениваются так же, но пропуски дополняются только по панели.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_forecasts.py
DAYS = [f"2026-12-{d:02d}" for d in (1, 2, 3, 4, 7, 8, 9, 10)]
CLOSES = {
    "SPY": dict(zip(DAYS, [100, 100, 100, 100, 100, 101, 101, 101])),     # +1% за 5 дней от 01.12
    "AAPL": dict(zip(DAYS, [200, 200, 200, 200, 200, 206, 206, 206])),    # +3%: обогнала
    "MSFT": dict(zip(DAYS, [300, 300, 300, 300, 300, 300, 300, 300])),    # 0%: отстала
}

def test_outcome_is_relative_to_spy_over_five_trading_days():
    got = score([{"day": DAYS[0], "symbol": "AAPL", "p": 0.7}, {"day": DAYS[0], "symbol": "MSFT", "p": 0.7}],
                CLOSES, DAYS, [DAYS[0]], ["AAPL", "MSFT"])
    assert [(s.symbol, s.outcome, s.missing) for s in got] == [("AAPL", 1, False), ("MSFT", 0, False)]

def test_missing_panel_forecast_is_scored_as_half():
    got = score([{"day": DAYS[0], "symbol": "AAPL", "p": 0.7}], CLOSES, DAYS, [DAYS[0]], ["AAPL", "MSFT"])
    assert [(s.symbol, s.p, s.missing) for s in got] == [("AAPL", 0.7, False), ("MSFT", 0.5, True)]

def test_days_without_a_matured_horizon_are_left_out():
    assert score([{"day": DAYS[5], "symbol": "AAPL", "p": 0.7}], CLOSES, DAYS, [DAYS[5]], ["AAPL"]) == []

def test_brier_and_reference_values():
    perfect = [Scored("d", "A", 1.0, 1, False), Scored("d", "B", 0.0, 0, False)]
    coin = [Scored("d", "A", 0.5, 1, False), Scored("d", "B", 0.5, 0, False)]
    assert brier(perfect) == 0.0 and brier(coin) == 0.25
    skewed = [Scored("d", s, 0.5, o, False) for s, o in zip("ABCDEFGHIJ", [1, 1, 1, 0, 0, 0, 0, 0, 0, 0])]
    assert reference(skewed) == pytest.approx(0.21)                  # 0.3 * 0.7

def test_bootstrap_is_deterministic_and_gives_an_interval():
    scored = [Scored(f"d{i // 4:02d}", f"S{i % 4}", 0.6, int((i * 7) % 3 == 0), False) for i in range(80)]
    lo, hi = bootstrap_diff(scored, seed=1)
    assert (lo, hi) == bootstrap_diff(scored, seed=1) and lo < hi and hi - lo < 0.2

def test_calibration_buckets_and_summary():
    scored = [Scored("d", "A", 0.65, 1, False), Scored("d", "B", 0.70, 0, False), Scored("d", "C", 0.10, 0, False)]
    rows = calibration(scored)
    assert [(r["lo"], r["hi"], r["n"]) for r in rows if r["n"]] == [(0.0, 0.2, 1), (0.6, 0.8, 2)]
    assert next(r for r in rows if r["lo"] == 0.6)["freq"] == 0.5
    assert "60–80%" in summary_text(scored) and "50%" in summary_text(scored)
    assert summary_text([]) == "Созревших прогнозов пока нет."
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_forecasts.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.forecasts`.

- [ ] **Step 3: Реализовать `traider/forecasts.py`**

Бутстреп: дни делятся на подряд идущие блоки длиной `block`, блоки выбираются с возвращением до исходного числа дней, для каждой выборки считается `brier - reference`; границы интервала — 5-й и 95-й процентили. Генератор: `random.Random(seed)`.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 5: Вики и коммит**

В статью `forecasts-and-calibration` добавить раздел «Как читается сводка калибровки, которую видит агент». Запустить проверку вики.

```bash
git add -A
git commit -m "feat: forecast scoring, reference score and calibration"
```
