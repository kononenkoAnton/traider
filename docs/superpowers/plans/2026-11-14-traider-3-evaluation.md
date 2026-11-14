# Traider, план 3 из 3: оценка и страница аналитики

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** По записям прогона посчитать тень, случайных ботов, показатели и вердикт, собрать страницу аналитики и сводку в вики, провести пилот.

**Architecture:** Один расчёт (`build_report`) даёт словарь `report`, из которого делаются три вещи: `report.json`, `dashboard.html` (шаблон со встроенными данными) и статья вики. Повтор сделок использует те же функции `apply_*` из симулятора, что и встроенный счёт.

**Tech Stack:** Python 3.11+, pytest; HTML, CSS и JavaScript без сборки и без сторонних библиотек.

**Spec:** `docs/superpowers/specs/2026-11-14-llm-trader-experiment-design.md`, разделы 5.8–5.11. Макет страницы: `docs/mockups/dashboard-mockup.html`. Предыдущие планы: `2026-11-14-traider-1-core.md`, `2026-11-14-traider-2-live.md`.

## Global Constraints

Действуют ограничения планов 1 и 2. Дополнительно:

- Страница — один файл, открывается с диска без сервера и без сети. Внешние скрипты, шрифты и стили не подключаются.
- Вид страницы — по согласованному макету. Цвет есть только у участников; их пять цветов в светлой теме `#2a78d6 #eb6834 #1baf7a #eda100 #e87ba4`, в тёмной `#3987e5 #d95926 #199e70 #c98500 #d55181`, в порядке `haiku`, `sonnet`, `opus-a`, `opus-b`, `opus-analyst`. Цвет закреплён за участником и не меняется при фильтрации.
- Имена, тезисы и любой текст из данных вставляются в страницу через `textContent`, не через `innerHTML`.
- Перед кодом задачи 3 исполнитель загружает навыки `dataviz` и `frontend-design:frontend-design`.
- Пороги вердикта берутся из `cfg.criteria` и не зашиваются в код.
- Расчёт детерминирован: одинаковые записи прогона дают одинаковый отчёт, кроме поля времени сборки.

## Review Focus

1. У бумаги нет цены в снимке или нет дневной свечи: повтор сделок не падает; случайному боту подбирается следующая бумага, позиция оценивается по последней известной цене. Задача 1.
2. Прогон только начался: одна сессия, ни одной сделки, ни одного созревшего прогноза. Отчёт и страница строятся, в пустых разделах стоит понятная фраза. Задачи 2 и 3.
3. Участник погиб на десятый день: его линия обрывается, остальные продолжаются, вердикт «красный». Задачи 2 и 3.
4. В тексте тезиса или журнала есть `</script>` или HTML: страница не ломается и не исполняет чужой код. Задача 3.
5. Отчёт запускают дважды за день: статья вики обновляется, а не дублируется, дата создания сохраняется. Задача 4.

---

### Task 1: Повтор сделок: тень, случайные боты, SPY

**Files:**
- Create: `traider/replay.py`, `tests/test_replay.py`

**Interfaces:**
- Consumes: `Account`, `apply_buy`, `apply_close`, `apply_stops`, `Bar`.
- Produces:

```python
def replay(events: Sequence[dict], snapshots: Mapping[str, Mapping[str, float]], bars: Mapping[str, Sequence[Bar]],
           days: Sequence[str], start_equity: float, pick: Callable[[dict], str] | None = None) -> list[float]
def shadow(events, snapshots, bars, days, start_equity) -> list[float]
def monkeys(events, snapshots, bars, days, start_equity, universe: Sequence[str], n: int = 100, seed: int = 0) -> list[list[float]]
def spy_same_exposure(events, snapshots, bars, days, start_equity) -> list[float]
def spy_buy_hold(bars: Mapping[str, Sequence[Bar]], days: Sequence[str], start_equity: float) -> list[float]
def percentile(value: float, population: Sequence[float]) -> float       # доля population строго ниже value, от 0 до 100
def band(curves: Sequence[Sequence[float]], qs=(5, 25, 50, 75, 95)) -> dict[str, list[float]]   # ключи "p5" ... "p95"
```

`events` — события журнала заявок с `kind` из `buy`, `close`, `raise_stop`, `liquidate`; события `reject` и `stop_fill` в повторе не участвуют, стопы повтор считает сам. `snapshots[day][symbol]` — цена из снимка после сессии. Результат — капитал на конец каждого дня из `days`.

Правила повтора:
- покупка и закрытие дня `d` исполняются по `snapshots[d]` с проскальзыванием (через `apply_buy` и `apply_close`);
- стоп позиции проверяется по дневным свечам начиная со дня после покупки: срабатывает при `low <= stop`, цена `min(stop, open)`;
- капитал на конец дня: деньги плюс количество на цену закрытия дня; если свечи нет, берётся последняя известная цена;
- `pick(event)` возвращает бумагу, которую бот покупает вместо бумаги агента; для каждой покупки выбор делается один раз и действует на все последующие события этой позиции;
- стоп случайного бота стоит на той же доле от цены, что у агента: `stop_bot = price_bot * (event["stop_price"] / event["price"])`; подъём стопа переносится так же и стоп не опускается;
- если позиция бота уже закрыта стопом, последующие события агента по ней пропускаются;
- если у выбранной бумаги нет цены в снимке дня, берётся следующая по списку вселенной;
- бот номер `i` использует `random.Random(seed * 1000 + i)`.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_replay.py
DAYS = ["2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"]
def bars(opens, lows, closes):
    return [Bar(d, o, max(o, c), l, c, 1e6) for d, o, l, c in zip(DAYS, opens, lows, closes)]
BARS = {
    "AAPL": bars([200, 201, 199, 190], [198, 199, 188, 185], [201, 202, 189, 187]),
    "XLE": bars([100, 100, 101, 102], [99, 99, 100, 101], [100, 101, 102, 103]),
    "SPY": bars([500, 500, 505, 505], [499, 499, 500, 500], [500, 505, 505, 510]),
}
SNAP = {d: {s: BARS[s][i].close for s in BARS} for i, d in enumerate(DAYS)}
BUY = {"kind": "buy", "day": DAYS[0], "symbol": "AAPL", "notional": 200.0, "price": 201.0, "stop_price": 190.95}   # стоп 5% ниже

def test_shadow_marks_to_the_close_and_stops_out_the_next_days():
    curve = shadow([BUY], SNAP, BARS, DAYS, 1000.0)
    qty = 200.0 / (201.0 * 1.0005)
    assert curve[0] == pytest.approx(800.0 + qty * 201.0)
    assert curve[1] == pytest.approx(800.0 + qty * 202.0)
    assert curve[2] == pytest.approx(800.0 + qty * 190.95 * 0.9995)      # 03.12: минимум 188 ниже стопа, открытие выше
    assert curve[3] == curve[2]

def test_a_gap_below_the_stop_fills_at_the_open():
    gap = {**BARS, "AAPL": bars([200, 180, 181, 182], [198, 178, 180, 181], [201, 181, 182, 183])}
    curve = shadow([BUY], {**SNAP, DAYS[1]: {**SNAP[DAYS[1]], "AAPL": 181.0}}, gap, DAYS, 1000.0)
    assert curve[1] == pytest.approx(800.0 + 200.0 / (201.0 * 1.0005) * 180.0 * 0.9995)

def test_close_and_raise_stop_are_replayed():
    events = [{**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0},
              {"kind": "raise_stop", "day": DAYS[1], "symbol": "XLE", "stop_price": 97.0},
              {"kind": "close", "day": DAYS[2], "symbol": "XLE", "fraction": 0.5}]
    curve = shadow(events, SNAP, BARS, DAYS, 1000.0)
    qty = 200.0 / (100.0 * 1.0005)
    assert curve[2] == pytest.approx(800.0 + qty / 2 * 102.0 * 0.9995 + qty / 2 * 102.0)
    assert curve[3] == pytest.approx(800.0 + qty / 2 * 102.0 * 0.9995 + qty / 2 * 103.0)

def test_pick_swaps_the_ticker_and_scales_the_stop():
    curve = replay([BUY], SNAP, BARS, DAYS, 1000.0, pick=lambda e: "XLE")
    assert curve[3] == pytest.approx(800.0 + 200.0 / (100.0 * 1.0005) * 103.0)      # стоп XLE на 95: не сработал
    assert spy_same_exposure([BUY], SNAP, BARS, DAYS, 1000.0) == replay([BUY], SNAP, BARS, DAYS, 1000.0, pick=lambda e: "SPY")

def test_monkeys_are_deterministic_and_differ_from_each_other():
    a = monkeys([BUY], SNAP, BARS, DAYS, 1000.0, ["AAPL", "XLE", "SPY"], n=30, seed=7)
    assert a == monkeys([BUY], SNAP, BARS, DAYS, 1000.0, ["AAPL", "XLE", "SPY"], n=30, seed=7)
    assert len(a) == 30 and len({tuple(c) for c in a}) == 3                          # три бумаги — три разные кривые
    assert a != monkeys([BUY], SNAP, BARS, DAYS, 1000.0, ["AAPL", "XLE", "SPY"], n=30, seed=8)

def test_missing_price_or_bar_does_not_crash():
    snap = {d: {k: v for k, v in SNAP[d].items() if k != "XLE"} for d in DAYS}
    curves = monkeys([BUY], snap, {k: v for k, v in BARS.items() if k != "XLE"}, DAYS, 1000.0, ["XLE", "AAPL"], n=5, seed=1)
    assert all(c == shadow([BUY], SNAP, BARS, DAYS, 1000.0) for c in curves)        # XLE без цены: взята следующая бумага

def test_no_events_means_flat_cash():
    assert shadow([], SNAP, BARS, DAYS, 1000.0) == [1000.0] * 4

def test_spy_buy_hold_percentile_and_band():
    assert spy_buy_hold(BARS, DAYS, 1000.0) == pytest.approx([1000.0, 1010.0, 1010.0, 1020.0])
    assert percentile(5, [1, 2, 3, 4]) == 100.0 and percentile(2.5, [1, 2, 3, 4]) == 50.0 and percentile(0, [1, 2]) == 0.0
    b = band([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]])
    assert b["p50"] == [2.0, 20.0] and set(b) == {"p5", "p25", "p50", "p75", "p95"}
```

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_replay.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.replay`.

- [ ] **Step 3: Реализовать `traider/replay.py`**

`shadow` — это `replay` без `pick`. `spy_buy_hold` масштабирует `start_equity` по цене закрытия SPY относительно первого дня.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 5: Вики и коммит**

Дополнить статью `random-bots-baseline` разделом «Как считается повтор»: правила из этой задачи простыми словами. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: trade replay for shadow, random bots and SPY baselines"
```

---

### Task 2: Показатели и вердикт

**Files:**
- Create: `traider/report.py`, `tests/test_report.py`

**Interfaces:**
- Consumes: `load_run`, `RunStore`, `Market`, `replay.*`, `forecasts.*`, `hp`, `floor`.
- Produces:

```python
def verdict(m: Mapping, criteria: Mapping) -> str            # "red" | "no_data" | "yellow" | "green"
def worst(verdicts: Sequence[str]) -> str                    # от худшего к лучшему: red, no_data, yellow, green
def integrity(store: RunStore, broker_order_ids: Iterable[str] | None) -> list[str]   # заявки брокера, которых нет в журнале
def build_report(exp_dir: Path, market: Market, broker_order_ids: Mapping[str, Iterable[str]] | None = None) -> dict
```

Порядок правил вердикта: гибель или нарушение целостности → `red` при любом числе сессий; затем нехватка данных → `no_data`; затем `red` по процентилю ниже `red_percentile` или по прогнозам (`ci[0] > 0`, то есть достоверно хуже эталона); затем `green` при процентиле не ниже `green_percentile` и `ci[1] < 0`; иначе `yellow`.

Ключи словаря `report`:

| Ключ | Содержимое |
|---|---|
| `meta` | `experiment`, `pilot`, `generated_at`, `sessions_target`, `sessions_done`, `credits_month_usd` (100) |
| `days` | дни сессий по порядку |
| `participants` | список `{id, model, research, broker, status, verdict}` в порядке `haiku`, `sonnet`, `opus-a`, `opus-b`, `opus-analyst` |
| `equity` | по участнику: тень, первая точка — стартовый капитал, далее по дням |
| `account_equity` | по участнику: фактический капитал счёта на конец дня |
| `spy`, `spy_same_exposure` | SPY «купил и держал»; по участнику — SPY с той же загрузкой |
| `band` | по участнику: `p5 p25 p50 p75 p95` его случайных ботов |
| `summary` | по участнику: `equity`, `return_pct`, `vs_spy_pp`, `percentile`, `max_drawdown_pct`, `hp`, `hp_share`, `trades`, `closed_trades`, `brier`, `reference`, `ci`, `brier_picks`, `cost`. `brier`, `reference` и `ci` считаются по панели; `brier_picks` — отдельно по прогнозам для купленных бумаг |
| `costs` | по участнику: расход по дням |
| `tokens` | по участнику: `sessions`, `input`, `cache_read`, `output`, `searches`, `cost_per_session`, `analyst` |
| `sessions` | по участнику: список `{day, status, buys, sells, rejects, turns, cost, usage, journal, looked, decisions, cage, forecasts, analyst, transcript}` |
| `positions`, `cash` | бумага → участник → сумма; участник → свободные деньги |
| `trades` | закрытые сделки `{day, participant, symbol, entry, exit, pnl, how}`; `how` — `stop` или `agent` |
| `calibration` | по участнику: строки `calibration()` |
| `compare` | `twins {diff_pp, common, held, forecast_corr}`, `ladder {haiku, sonnet, opus, beyond_noise}`, `analyst {diff_pp, beyond_noise}` |
| `rejects` | `{reason, label, count}` по убыванию |
| `incidents` | `{kind, day, participant, title, text}`; `kind` из `critical`, `serious`, `warning`, `good` |
| `verdict` | `by_participant` и `opus` — худший из `opus-a` и `opus-b` |

`beyond_noise` истинно, когда разница больше удвоенной разницы между близнецами. Происшествия: пропущенный день (`critical`), сбойная сессия (`serious`), сессия со статусом `cap` и подмена модели (`warning`), итог проверки целостности (`good` или `critical`). `looked` строится из диалога: имя инструмента, число вызовов, пример входа. `decisions` и `cage` — из журнала заявок за день.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_report.py
CRIT = dict(min_sessions=40, max_missed_share=0.10, min_closed_trades=15, red_percentile=25, green_percentile=90)
BASE = dict(sessions_done=40, missed_share=0.05, closed_trades=20, dead=False, integrity_violations=0, percentile=60.0, ci=(-0.02, 0.01))

@pytest.mark.parametrize("change, expected", [
    ({}, "yellow"),
    ({"percentile": 90.0, "ci": (-0.03, -0.001)}, "green"),
    ({"percentile": 95.0, "ci": (-0.03, 0.0)}, "yellow"),
    ({"percentile": 89.9, "ci": (-0.03, -0.001)}, "yellow"),
    ({"percentile": 24.9}, "red"),
    ({"percentile": 25.0}, "yellow"),
    ({"ci": (0.001, 0.03)}, "red"),
    ({"sessions_done": 39}, "no_data"),
    ({"missed_share": 0.11}, "no_data"),
    ({"closed_trades": 14}, "no_data"),
    ({"sessions_done": 12, "dead": True}, "red"),
    ({"sessions_done": 12, "integrity_violations": 1}, "red"),
])
def test_verdict_table(change, expected):
    assert verdict({**BASE, **change}, CRIT) == expected

def test_worst_orders_verdicts():
    assert worst(["green", "yellow"]) == "yellow" and worst(["yellow", "no_data"]) == "no_data" and worst(["no_data", "red"]) == "red"

def test_integrity_lists_broker_orders_missing_from_the_ledger(tmp_path):
    store = RunStore(tmp_path)
    store.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "AAPL", "broker_order_id": "a1"})
    assert integrity(store, ["a1"]) == [] and integrity(store, ["a1", "zz"]) == ["zz"] and integrity(store, None) == []

def test_report_of_a_small_experiment(played):            # фикстура: три дня тиков на подделках, см. ниже
    exp, market = played
    r = build_report(exp, market)
    assert [p["id"] for p in r["participants"]] == ["haiku", "sonnet", "opus-a", "opus-b", "opus-analyst"]
    assert r["days"] == ["2026-12-01", "2026-12-02", "2026-12-03"] and r["meta"]["sessions_done"] == 3
    for pid in ("haiku", "opus-a", "opus-analyst"):
        assert len(r["equity"][pid]) == 4 and r["equity"][pid][0] == 1000.0
        assert sum(r["costs"][pid]) == pytest.approx(r["summary"][pid]["cost"])
        assert r["summary"][pid]["cost"] == pytest.approx(sum(s["cost"] for s in r["sessions"][pid]))
        assert set(r["band"][pid]) == {"p5", "p25", "p50", "p75", "p95"} and 0 <= r["summary"][pid]["percentile"] <= 100
        assert r["verdict"]["by_participant"][pid] == "no_data"
    assert r["verdict"]["opus"] == "no_data" and r["sessions"]["haiku"][0]["looked"]
    assert r["tokens"]["opus-analyst"]["analyst"]["searches"] >= 0 and r["tokens"]["haiku"]["analyst"] is None
    assert any(i["kind"] == "good" for i in r["incidents"])
    again = build_report(exp, market)
    assert {k: v for k, v in again.items() if k != "meta"} == {k: v for k, v in r.items() if k != "meta"}

def test_report_of_a_run_that_has_barely_started(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    r = build_report(exp, FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70))
    assert r["days"] == [] and r["trades"] == [] and r["equity"]["haiku"] == [1000.0]
    assert r["summary"]["haiku"]["brier"] is None and r["summary"]["haiku"]["percentile"] is None

def test_dead_participant_is_red_and_its_curve_ends(played_with_death):      # opus-b погибает на второй день
    exp, market = played_with_death
    r = build_report(exp, market)
    assert r["verdict"]["by_participant"]["opus-b"] == "red" and r["verdict"]["opus"] == "red"
    assert len(r["equity"]["opus-b"]) == 3 and len(r["equity"]["opus-a"]) == 4
    assert any(i["kind"] == "critical" and i["participant"] == "opus-b" for i in r["incidents"])
```

Фикстура `played` в `tests/conftest.py`: запускает `start_experiment`, затем три дня подряд вызывает `tick` на `FakeMarket.with_history`, `SimBroker` и `FakeAnthropic`, чей сценарий каждый день записывает прогнозы по панели, покупает одну бумагу и завершает сессию. `played_with_death` дополнительно обрушивает цену бумаги `opus-b` перед вторым днём.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_report.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.report`.

- [ ] **Step 3: Реализовать `traider/report.py`**

Цены закрытия и свечи берутся одним вызовом `market.daily_bars` на всю вселенную и SPY. Случайные боты считаются с `cfg.seed`. Русские подписи причин отказа лежат в одном словаре рядом с `rejects`.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 5: Вики и коммит**

В статью `verdict-criteria` добавить порядок применения правил вердикта. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: experiment metrics, comparisons and verdict"
```

---

### Task 3: Страница аналитики

**Files:**
- Create: `traider/dashboard.py`, `traider/dashboard.html`, `tests/test_dashboard.py`, `tests/data/report_fixture.json`

**Interfaces:**
- Consumes: словарь `report` из задачи 2.
- Produces:

```python
def render(report: Mapping, out: Path) -> None      # пишет один HTML-файл
```

Шаблон `traider/dashboard.html` делается из `docs/mockups/dashboard-mockup.html`. Отличия от макета:

- блок выдуманных данных удалён; данные приходят из строки `const DATA = /*DATA*/null;`, которую `render` заменяет на JSON;
- плашки «Макет» нет; в пилоте в заголовке стоит слово «Пилот»;
- серая полоса случайных ботов у каждого участника своя, поэтому над графиком капитала есть переключатель «Случайные боты для»: по умолчанию `opus-a`;
- раскрытая сессия показывает настоящие данные: что смотрел, что решил, отказы клетки, прогнозы дня, сводку аналитика, полный диалог под сворачиваемой строкой;
- пустые разделы показывают фразу: «Сделок пока нет», «Созревших прогнозов пока нет», «Происшествий нет»;
- у погибшего участника линия обрывается на дне гибели, в сводке стоит пометка «погиб»;
- в сводке есть столбец «Вердикт» со словом и значком; пока прогон не завершён, рядом стоит пометка «предварительный»;
- фильтр периода: «Весь прогон», «Последние 5 дней», «Последние 10 дней».

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_dashboard.py
FIXTURE = json.loads(pathlib.Path("tests/data/report_fixture.json").read_text())

def embedded(html: str) -> dict:
    start = html.index("const DATA = ") + len("const DATA = ")
    return json.loads(html[start:html.index(";\n", start)].replace("<\\/", "</"))

def test_render_embeds_the_report(tmp_path):
    render(FIXTURE, tmp_path / "d.html")
    html = (tmp_path / "d.html").read_text()
    assert embedded(html) == FIXTURE and "/*DATA*/" not in html

def test_hostile_text_cannot_break_out_of_the_script(tmp_path):
    report = json.loads(json.dumps(FIXTURE))
    report["sessions"]["haiku"][0]["journal"] = "</script><script>alert(1)</script>"
    render(report, tmp_path / "d.html")
    html = (tmp_path / "d.html").read_text()
    assert html.count("</script>") == 1 and embedded(html)["sessions"]["haiku"][0]["journal"].startswith("</script>")

def test_template_is_self_contained_and_safe():
    t = pathlib.Path("traider/dashboard.html").read_text()
    assert "innerHTML" not in t and "http://" not in t.replace("http://www.w3.org", "") and "https://" not in t
    assert "выдуманы" not in t and "Math.imul" not in t                         # генератор выдуманных данных убран
    for section in ["summary", "equity", "money", "tokens", "grid", "detail", "positions", "trades", "forecasts", "compare", "rejects", "incidents"]:
        assert f'id="{section}"' in t, section
    for colour in ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]:
        assert colour in t, colour

def test_empty_report_renders(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    render(build_report(exp, FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70)), tmp_path / "e.html")
    assert (tmp_path / "e.html").stat().st_size > 10_000
```

`tests/data/report_fixture.json` — отчёт фикстуры `played` из задачи 2, сохранённый один раз в файл.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_dashboard.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.dashboard`.

- [ ] **Step 3: Реализовать `render` и шаблон**

`render` сериализует отчёт через `json.dumps(..., ensure_ascii=False)` и заменяет в результате `</` на `<\/`. Правила графиков — по навыку `dataviz`: линии 2 px, столбики не шире 24 px с зазором 2 px, подсказка с перекрестием, таблица под каждым графиком.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q`
Expected: все тесты проходят.

- [ ] **Step 5: Посмотреть страницу глазами**

Run: `uv run python -c "import json,pathlib; from traider.dashboard import render; render(json.loads(pathlib.Path('tests/data/report_fixture.json').read_text()), pathlib.Path('runs/preview.html'))"`

Открыть `runs/preview.html` в панели браузера. Проверить: консоль без ошибок; фильтры перестраивают страницу; щелчок по клетке сессии раскрывает её; переключатель случайных ботов меняет полосу; ширина 1200 и 400 пикселей без горизонтальной прокрутки всей страницы; тёмная тема читается. То же для страницы пустого отчёта.

- [ ] **Step 6: Вики и коммит**

Обновить статью `analytics-page`: разделы страницы, чем она отличается от макета, как её открыть. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: analytics page generated from the report"
```

---

### Task 4: Команда `report`, сводка в вики, автоматический запуск

**Files:**
- Modify: `traider/cli.py` (команда `report`, запуск отчёта после тика)
- Create: `traider/wiki_results.py`, `tests/test_wiki_results.py`

**Interfaces:**
- Consumes: `build_report`, `render`, `tick`.
- Produces:

```python
def write_report(exp_dir: Path, market: Market, wiki_dir: Path, broker_order_ids=None) -> dict   # report.json, dashboard.html, статья вики
def report_due(exp_dir: Path, day: str) -> bool        # у всех активных участников есть запись за day, а отчёт за day ещё не собран
def results_article(report: Mapping) -> str             # тело статьи в markdown, без frontmatter
def upsert_results(wiki_dir: Path, report: Mapping, now: datetime) -> Path
```

Статья: `wiki/articles/results-<experiment>.md`, категория `results`, теги `results` и `pilot` либо `official`. `upsert_results` при повторе сохраняет `created`, обновляет `modified` у статьи, у категории `results` и у корневой категории `traider`, и вписывает статью в блок `ai` категории `results`, если её там нет.

Тело статьи: состояние прогона (день N из M), таблица участников (капитал, доход, место среди случайных, оценка прогнозов, расход, вердикт), три сравнения словами, происшествия, ссылка на `runs/<experiment>/dashboard.html`.

Команда `traider tick` после тика вызывает `write_report` для каждого эксперимента, у которого `report_due` истинно. Команда `traider report [experiment]` собирает отчёт сразу. Для участника со счётом Alpaca команда передаёт в `broker_order_ids` результат `AlpacaApi.order_ids_since` с первого дня прогона.

- [ ] **Step 1: Написать падающие тесты**

```python
# tests/test_wiki_results.py
@pytest.fixture
def wiki(tmp_path):
    shutil.copytree("wiki", tmp_path / "wiki"); return tmp_path / "wiki"

def test_report_files_and_article_are_written(played, wiki):
    exp, market = played
    report = write_report(exp, market, wiki)
    assert json.loads((exp / "report.json").read_text())["days"] == report["days"] and (exp / "dashboard.html").exists()
    article = frontmatter.load(wiki / "articles" / "results-pilot-1.md")
    assert article["categories"] == ["results"] and "pilot" in article["tags"]
    assert "opus-analyst" in article.content and "dashboard.html" in article.content
    assert "[[results-pilot-1" in (wiki / "categories" / "results.md").read_text()
    assert subprocess.run([sys.executable, "tools/check_wiki.py", str(wiki)]).returncode == 0

def test_second_report_updates_instead_of_duplicating(played, wiki):
    exp, market = played
    upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    first = frontmatter.load(wiki / "articles" / "results-pilot-1.md")
    upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 4, 16, tzinfo=timezone.utc))
    second = frontmatter.load(wiki / "articles" / "results-pilot-1.md")
    assert second["created"] == first["created"] and second["modified"] > first["modified"]
    assert (wiki / "categories" / "results.md").read_text().count("[[results-pilot-1") == 1

def test_report_is_due_once_per_day(played, wiki):
    exp, market = played
    assert report_due(exp, "2026-12-03") is True
    write_report(exp, market, wiki)
    assert report_due(exp, "2026-12-03") is False and report_due(exp, "2026-12-04") is False   # за 04.12 записей ещё нет
```

Для этого теста `tools/check_wiki.py` принимает необязательный первый аргумент — путь к вики; без аргумента работает как раньше.

- [ ] **Step 2: Убедиться, что тесты падают**

Run: `uv run pytest tests/test_wiki_results.py -q`
Expected: FAIL, `ModuleNotFoundError: traider.wiki_results`.

- [ ] **Step 3: Реализовать `wiki_results.py`, команду `report`, вызов после тика и аргумент в `check_wiki.py`**

Статья пишется по правилам `wiki/articles/traider-wiki-rules.md`; формат frontmatter — как в существующих статьях.

- [ ] **Step 4: Убедиться, что тесты проходят**

Run: `uv run pytest -q && uv run --with python-frontmatter python tools/check_wiki.py`
Expected: все тесты проходят, вики в порядке.

- [ ] **Step 5: Вики и коммит**

В статью `tick-and-session` добавить, когда собирается отчёт. В `traider-wiki-rules` добавить строку о том, что статьи `results-*` пишет программа. Запустить проверку вики.

```bash
git add -A
git commit -m "feat: report command, wiki results article, daily auto-report"
```

---

### Task 5: Пилот и официальный старт

Задача выполняется вместе с владельцем. Каждый запуск, который тратит кредиты, начинается с его согласия.

**Files:**
- Create: `experiment.official.toml`, статьи вики `pilot-results` и `runbook` в категориях `results` и `operations`

- [ ] **Step 1: Запустить пилот**

Владелец пересоздаёт paper-счёт Alpaca на $1000 и обновляет ключи в `.env`. Затем:

```bash
uv run traider start experiment.example.toml
```

Владелец устанавливает расписание по статье `schedule-and-hosting` и держит Mac включённым в часы торгов три дня.

- [ ] **Step 2: Проверка после первого дня**

Run: `uv run traider status && uv run traider report pilot-1`

Открыть `runs/pilot-1/dashboard.html`. Проверить по списку: у каждого участника запись сессии; у `opus-a` заявки в кабинете Alpaca совпадают с журналом; у каждой позиции стоп; отказы клетки осмысленные; ни одна сессия не упёрлась в потолок без причины. Если что-то сломано: остановить расписание, починить, начать пилот заново с новым именем эксперимента.

- [ ] **Step 3: Итоги пилота**

После третьего дня записать в статью `pilot-results`: стоимость сессии каждого участника по дням; прогноз расхода на месяц для пяти и для четырёх участников (без `sonnet`); сколько сессий упёрлись в потолок; найденные ошибки и что с ними сделано; ответы на два вопроса про Alpaca из статьи `alpaca-findings`.

- [ ] **Step 4: Решения владельца**

Владелец по цифрам пилота решает: остаётся ли `sonnet`; потолки `max_session_usd` для каждой модели так, чтобы худший случай за месяц не превышал $90; где идёт официальный прогон, на Mac или на сервере. Решения записываются в `wiki/articles/decision-log.md`.

- [ ] **Step 5: Официальный старт**

`experiment.official.toml` — копия примера с `experiment = "run-1"`, `pilot = false`, `sessions_target = 40`, согласованными потолками и составом. Владелец пересоздаёт paper-счёт и обновляет ключи.

```bash
uv run traider start experiment.official.toml
uv run traider status
```

Expected: участники в статусе `active`, конфиг каждого заморожен.

- [ ] **Step 6: Памятка и коммит**

Статья `runbook`: как посмотреть состояние, как остановить и снять расписание, что делать при пропуске дня, при сбое сессии и при гибели участника, как собрать отчёт вручную. Запустить проверку вики.

```bash
git add -A
git commit -m "docs: pilot results, runbook and official run config"
```
