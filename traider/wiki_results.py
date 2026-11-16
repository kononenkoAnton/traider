"""Запись отчёта: `report.json`, страница, статья вики `results-<эксперимент>` и условие дневного запуска.

:wk-id: wiki-results-module
:wk-tags: report, wiki, results
:wk-categories: system-architecture

Как это работает. `write_report` строит отчёт (`build_report`, только чтение) и пишет в каталог
эксперимента `report.json` и `dashboard.html`, каждый через временный файл и переименование. Вики
пишется, только если `wiki_dir` задан; при `None` не меняется ни один файл вне каталога эксперимента.
`results_article` даёт тело статьи из чисел отчёта и фиксированных формулировок: ни журналов, ни тезисов,
ни текстов отказов и ошибок, ни диалогов в неё не попадает (происшествие это вид, день, участник и
готовое название); число, которого в отчёте нет (None), показано как «—». `upsert_results` создаёт статью или
обновляет её (`created` сохраняется), вписывает её в блок `ai` категории `results`, если её там нет, и ставит `modified`
у статьи, категории `results` и корня `traider` (категория не уходит назад, если часы отстают от записи).
Файлы вики пишутся через временный файл с уникальным именем и переименование, frontmatter пишется здесь же в формате
существующих статей. Перед любой записью читаются обе категории и проверяются (есть файл, frontmatter с `modified`, у
`results` блок `ai`; есть каталог `articles`), иначе исключение без записи; пишутся по порядку корень `traider`, категория `results`,
статья. Обновление существующей статьи, прерванное после любой записи, оставляет вики, которую принимает проверка; при первом создании
между записью категории и статьи остаётся ссылка на ещё не созданную статью (две записи в разные файлы атомарно не сделать). `modified` статьи не идёт назад и не раньше `created`, у категорий не раньше `modified` статьи. Имя эксперимента должно быть слагом `[A-Za-z0-9_-]+`, иначе `ValueError` до любой записи.

`report_due(exp_dir, day)`: когда все прогоны `finished` или `dead`, истинно один раз, в первый день `day` позже последней строки
`ticks.jsonl` любого прогона, если `report.json` нет или статусы его `participants` не равны текущим (дальше они равны, отчёт больше не нужен).
Иначе истинно, когда за `day` отчёта ещё нет и: у каждого прогона со статусом `active` есть запись
сессии за `day`; а если активных нет, хотя бы у одного прогона вне `finished` и `dead` есть строка тика за `day`
(прогон ждёт конца: `finishing` или `invalid` идут тиками каждый день, без тика за день отчёту нечего добавить).
Отчёт «собран за `day`», когда в существующем `report.json` день `meta.generated_at` (по Нью-Йорку) равен `day` и
статусы записей сессий за `day` в нём (`sessions[участник][].status`) те же, что в файлах прогонов: отчёт,
собранный до конца сессий дня, день не закрывает. Нечитаемый или неполный `report.json` значит «не собран».
"""
import json
import os
import re
import uuid
from collections.abc import Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path

from traider.config import TERMINAL_STATUSES
from traider.dashboard import render
from traider.market import Market, ny_day
from traider.report import build_report
from traider.store import RunStore

DASH = "—"
SLUG = re.compile(r"[A-Za-z0-9_-]+")
VERDICTS = {"red": "красный", "yellow": "жёлтый", "green": "зелёный", "no_data": "мало данных"}
KINDS = {"critical": "критично", "serious": "серьёзно", "warning": "предупреждение", "good": "в порядке"}
AI = ("<!-- ai:start -->", "<!-- ai:end -->")


def _write(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")   # у каждой записи свой файл: две сборки не мешают друг другу
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _f(x, spec: str = ".2f", pre: str = "", post: str = "") -> str:
    return DASH if x is None else f"{pre}{format(x, spec)}{post}".replace(",", " ")


def _cell(x) -> str:
    return re.sub(r"[|\[\]\n\r]", " ", str(x))


def _yes(x) -> str:
    return "пока не определить" if x is None else "да" if x else "нет"


def results_article(report: Mapping) -> str:
    """Тело статьи (markdown без frontmatter): числа отчёта и фиксированные фразы, ни одного текста модели."""
    meta, summary, cmp_, money = report["meta"], report["summary"], report["compare"], report["money"]
    exp = meta["experiment"]
    days = report["days"]
    rows = [f"| {_cell(p['id'])} | {_f(s.get('equity'), ',.2f', '$')} | {_f(s.get('return_pct'), '+.2f', '', '%')} | "
            f"{_f(s.get('percentile'), '.0f')} | "
            f"{DASH if s.get('brier') is None else _f(s['brier'], '.3f') + ' (эталон ' + _f(s.get('reference'), '.3f') + ')'} | "
            f"{_f(s.get('cost'), '.2f', '$')} | {VERDICTS.get(p['verdict'], DASH)} |"
            for p in report["participants"] for s in [summary.get(p["id"], {})]]
    twins, ladder, analyst = cmp_["twins"], cmp_["ladder"], cmp_["analyst"]
    pct = lambda x: DASH if x is None else f"{x * 100:.0f}%"   # noqa: E731
    opus = ladder["opus"]["return_pct"]
    incidents = [f"- {KINDS.get(i['kind'], i['kind'])}, {i['day'] or DASH}, {_cell(i['participant'] or 'весь эксперимент')}: "
                 f"{_cell(i['title'])}" for i in report["incidents"]]
    return "\n".join([
        f"# Результаты: {exp}", "",
        "Статью пишет программа (`traider report --wiki`) и перезаписывает при каждой сборке отчёта. В ней только числа и "
        "фиксированные формулировки; журналы, тезисы и тексты ошибок остаются в файлах прогонов и на странице. "
        "Что означает каждое число: [[report-contents]], как читается вердикт: [[verdict-criteria]].", "",
        "## Состояние прогона", "",
        f"- Эксперимент {exp}: {'пилот, в вердикт не входит' if meta['pilot'] else 'официальный прогон'}.",
        f"- День {meta['sessions_done']} из {_f(meta['sessions_target'], '.0f')}.",
        f"- Дни сессий: {days[0] + ' – ' + days[-1] if days else 'сессий ещё не было'}.",
        f"- Расход прогона {_f(money['run_spend_usd'], '.2f', '$')}; за месяц {money['month'] or DASH}: "
        f"{_f(money['month_spend_usd'], '.2f', '$')} из ${meta['credits_month_usd']} кредитов.",
        f"- Отчёт собран {meta['generated_at']}.", "",
        "## Участники", "",
        "| Участник | Капитал | Доход | Обогнал случайных, % | Оценка прогнозов (Брайер) | Расход | Вердикт |",
        "|---|---|---|---|---|---|---|", *(rows or [f"| {DASH} | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} | {DASH} |"]), "",
        "## Три сравнения", "",
        f"- Близнецы. Разница доходностей opus-a и opus-b {_f(twins['diff_pp'], '+.2f', '', ' п.п.')}; общих покупок "
        f"{pct(twins['common'])}; корреляция прогнозов {_f(twins['forecast_corr'])}.",
        f"- Лестница моделей. Доходность: haiku {_f(ladder['haiku']['return_pct'], '+.2f', '', '%')}, "
        f"sonnet {_f(ladder['sonnet']['return_pct'], '+.2f', '', '%')}, opus (среднее близнецов) {_f(opus, '+.2f', '', '%')}. "
        f"Разброс больше шума близнецов: {_yes(ladder['beyond_noise'])}.",
        f"- Аналитик. Разница доходностей opus-analyst и среднего близнецов {_f(analyst['diff_pp'], '+.2f', '', ' п.п.')}; "
        f"больше шума близнецов: {_yes(analyst['beyond_noise'])}.", "",
        "## Происшествия", "", *(incidents or ["Происшествий нет."]), "",
        "## Страница аналитики", "",
        f"Подробности на странице `runs/{exp}/dashboard.html`: файл лежит на машине, где идёт прогон, и открывается с диска.",
        ""])


def _field(text: str, name: str) -> str | None:
    m = re.search(rf"^{name}: (.+)$", text, re.M)
    return m and m.group(1).strip()


def _time(x) -> datetime | None:
    try:
        return datetime.fromisoformat(x)
    except (ValueError, TypeError):
        return None


def _touch(path: Path, text: str, modified: datetime, entry: tuple[str, str] | None = None) -> None:
    """Категория: `modified` не назад (и не раньше статьи); со статьёй (id, текст записи) ещё и запись в блок `ai`."""
    old = _time(_field(text, "modified"))
    when = max(old, modified) if old else modified
    text = re.sub(r"^modified: .*$", lambda _: f"modified: {when.isoformat()}", text, count=1, flags=re.M)
    if entry:
        head, rest = text.split(AI[0], 1)
        block, tail = rest.split(AI[1], 1)
        if f"[[{entry[0]}" not in block:
            body = block.strip()
            body = entry[1] if not body or body == "Статей пока нет." else f"{body}\n\n{entry[1]}"
            text = f"{head}{AI[0]}\n{body}\n{AI[1]}{tail}"
    _write(path, text)


def _category(path: Path, ai: bool) -> str:
    """Текст категории, годный к правке; иначе исключение до любой записи."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n") or _time(_field(text, "modified")) is None:
        raise ValueError(f"{path}: нет frontmatter с полем modified")
    if ai and not (text.count(AI[0]) == 1 and text.count(AI[1]) == 1 and text.index(AI[0]) < text.index(AI[1])):
        raise ValueError(f"{path}: нет блока ai (по одному маркеру начала и конца)")
    return text


def upsert_results(wiki_dir: Path, report: Mapping, now: datetime) -> Path:
    exp = report["meta"]["experiment"]
    if not isinstance(exp, str) or not SLUG.fullmatch(exp):
        raise ValueError(f"имя эксперимента негодно для статьи вики: {exp!r}")
    wiki, aid = Path(wiki_dir), f"results-{exp}"
    path = wiki / "articles" / f"{aid}.md"
    root, results = wiki / "categories" / "traider.md", wiki / "categories" / "results.md"
    root_text, results_text = _category(root, False), _category(results, True)   # до любой записи
    if not path.parent.is_dir():
        raise ValueError(f"{wiki}: нет каталога articles")
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    created = _time(_field(old, "created")) or now
    prev = _time(_field(old, "modified"))
    modified = max(now, created, *([prev] if prev else []))   # не назад и не раньше `created`
    kind = "pilot" if report["meta"]["pilot"] else "official"
    entry = (aid, f"### [[{aid}|Результаты: {exp}]]\n"
             f"Отчёт {'пилота' if kind == 'pilot' else 'прогона'} {exp}: участники, сравнения, происшествия. Обновляется программой.")
    # порядок: корень, категория, статья; оборванное обновление оставляет вики, которую проверка принимает
    _touch(root, root_text, modified)
    _touch(results, results_text, modified, entry)
    _write(path, "\n".join([
        "---", f"id: {aid}", f'title: "Результаты: {exp}"', "type: leaf", f"tags: ['results', '{kind}']",
        "categories: ['results']", f"created: {created.isoformat()}", f"modified: {modified.isoformat()}",
        "---", "", results_article(report)]))
    return path


def _day_status(store: RunStore, day: str) -> str | None:
    rec = store.session(day)
    return rec and rec.get("status")


def _built(exp_dir: Path, day: str, stores: Mapping[str, RunStore]) -> bool:
    try:
        rep = json.loads((exp_dir / "report.json").read_text(encoding="utf-8"))
        theirs = {pid: r.get("status") for pid, rs in rep["sessions"].items() for r in rs if r["day"] == day}
        mine = {pid: s for pid, st in stores.items() if (s := _day_status(st, day))}
        return ny_day(datetime.fromisoformat(rep["meta"]["generated_at"])) == day and theirs == mine
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False


def _final_due(exp_dir: Path, day: str, stores: Mapping[str, RunStore], status: Mapping[str, str]) -> bool:
    """Все прогоны окончены: один итоговый отчёт в первый открытый день после последнего тика, пока отчёт не
    показывает текущие статусы участников (прогнозы последнего дня к тому дню созрели)."""
    try:
        rep = json.loads((exp_dir / "report.json").read_text(encoding="utf-8"))
        if {p["id"]: p["status"] for p in rep["participants"]} == dict(status):
            return False
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return all(day > (t.get("day") or "") for st in stores.values() for t in st.ticks()[-1:])


def report_due(exp_dir: Path, day: str) -> bool:
    exp_dir = Path(exp_dir)
    stores = {c.parent.name: RunStore(c.parent) for c in sorted(exp_dir.glob("*/config.json"))}
    status = {pid: st.state()["status"] for pid, st in stores.items()}
    if stores and all(s in TERMINAL_STATUSES for s in status.values()):
        return _final_due(exp_dir, day, stores, status)
    active = [pid for pid, s in status.items() if s == "active"]
    ready = (all(_day_status(stores[pid], day) for pid in active) if active else
             any(s not in TERMINAL_STATUSES and any(t.get("day") == day for t in stores[pid].ticks())
                 for pid, s in status.items()))
    return bool(stores) and ready and not _built(exp_dir, day, stores)


def write_report(exp_dir: Path, market: Market, wiki_dir: Path | None,
                 broker_order_ids: Mapping[str, Iterable[str]] | None = None) -> dict:
    exp_dir = Path(exp_dir)
    report = build_report(exp_dir, market, broker_order_ids)
    _write(exp_dir / "report.json", json.dumps(report, ensure_ascii=False, indent=1))
    render(report, exp_dir / "dashboard.html")
    if wiki_dir is not None:
        upsert_results(wiki_dir, report, market.now())
    return report
