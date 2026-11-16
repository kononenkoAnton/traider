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
Файлы вики пишутся через временный файл и переименование, frontmatter пишется здесь же в формате
существующих статей. Имя эксперимента должно быть слагом `[A-Za-z0-9_-]+`, иначе `ValueError` до любой записи.

`report_due(exp_dir, day)` истинно, когда за `day` отчёта ещё нет и: у каждого прогона со статусом `active` есть запись
сессии за `day`; а если активных нет, хотя бы у одного прогона вне `finished` и `dead` есть строка тика за `day`
(прогон ждёт конца: `finishing` или `invalid` идут тиками каждый день, без тика за день отчёту нечего добавить).
Отчёт «собран за `day`», когда в существующем `report.json` день `meta.generated_at` (по Нью-Йорку) равен `day` и
статусы записей сессий за `day` в нём (`sessions[участник][].status`) те же, что в файлах прогонов: отчёт,
собранный до конца сессий дня, день не закрывает. Нечитаемый или неполный `report.json` значит «не собран».
"""
import json
import os
import re
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
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


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
        "| Участник | Капитал | Доход | Место среди случайных, % | Оценка прогнозов (Брайер) | Расход | Вердикт |",
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


def _later(old: str | None, now: datetime) -> str:
    """Новое `modified`: не раньше прежнего (часы бывают отстающие)."""
    try:
        return max(datetime.fromisoformat(old), now).isoformat() if old else now.isoformat()
    except (ValueError, TypeError):
        return now.isoformat()


def _touch(path: Path, now: datetime, entry: tuple[str, str] | None = None) -> None:
    """Категория: `modified` вперёд; со статьёй (id, текст записи) ещё и запись в блок `ai`, если её там нет."""
    text = path.read_text(encoding="utf-8")
    text = re.sub(r"^modified: .*$", lambda _: f"modified: {_later(_field(text, 'modified'), now)}", text, count=1, flags=re.M)
    if entry:
        head, rest = text.split(AI[0], 1)
        block, tail = rest.split(AI[1], 1)
        if f"[[{entry[0]}" not in block:
            body = block.strip()
            body = entry[1] if not body or body == "Статей пока нет." else f"{body}\n\n{entry[1]}"
            text = f"{head}{AI[0]}\n{body}\n{AI[1]}{tail}"
    _write(path, text)


def upsert_results(wiki_dir: Path, report: Mapping, now: datetime) -> Path:
    exp = report["meta"]["experiment"]
    if not isinstance(exp, str) or not SLUG.fullmatch(exp):
        raise ValueError(f"имя эксперимента негодно для статьи вики: {exp!r}")
    wiki, aid = Path(wiki_dir), f"results-{exp}"
    path = wiki / "articles" / f"{aid}.md"
    now = now.astimezone(timezone.utc).replace(microsecond=0)
    old = path.read_text(encoding="utf-8") if path.exists() else ""
    kind = "pilot" if report["meta"]["pilot"] else "official"
    _write(path, "\n".join([
        "---", f"id: {aid}", f'title: "Результаты: {exp}"', "type: leaf", f"tags: ['results', '{kind}']",
        "categories: ['results']", f"created: {_field(old, 'created') or now.isoformat()}", f"modified: {now.isoformat()}",
        "---", "", results_article(report)]))
    _touch(wiki / "categories" / "results.md", now, (aid, f"### [[{aid}|Результаты: {exp}]]\n"
           f"Отчёт {'пилота' if kind == 'pilot' else 'прогона'} {exp}: участники, сравнения, происшествия. Обновляется программой."))
    _touch(wiki / "categories" / "traider.md", now)
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


def report_due(exp_dir: Path, day: str) -> bool:
    exp_dir = Path(exp_dir)
    stores = {c.parent.name: RunStore(c.parent) for c in sorted(exp_dir.glob("*/config.json"))}
    status = {pid: st.state()["status"] for pid, st in stores.items()}
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
