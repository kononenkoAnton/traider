"""Командная строка: `traider start`, `traider tick`, `traider status`, `traider report`.

:wk-id: cli-module
:wk-tags: cli, schedule, status
:wk-categories: system-architecture

Как это работает. Все пути считаются от текущего каталога: прогоны лежат в `runs/`, ключи в `.env`.
`start <toml> [--live]` замораживает прогоны (`start_experiment`), печатает их каталоги и худший расход
эксперимента: сумма `max_session_usd` и `analyst_max_usd` всех участников за торговый день и за 21 день, со
строкой о том, что жёсткая граница это лимит трат в Anthropic Console; конфиг с
`mode = "live"` без `--live` даёт код 2 и одну строку, другая ошибка конфига даёт код 1. Живой
режим включается только здесь, существующий прогон командой живым не станет. `tick` читает `.env`,
собирает `AlpacaMarket`, выбирает брокера по `cfg.broker` (`sim` встроенный, `alpaca` через
`make_alpaca_api`), клиента Anthropic и печатает по строке на прогон: время рынка, `эксперимент/участник`,
действие; ошибка до обхода это одна строка и код 1. Клиент Anthropic создаётся с `timeout=API_TIMEOUT`
(300 с): это предел каждого ожидания сети в одном запросе (соединение, каждое чтение потока), а не всего
запроса; и `max_retries=API_RETRIES` (2): столько раз SDK повторит запрос после сбоя связи, 408, 409, 429 и
5xx, так что тишина в одном запросе стоит не больше трёх таймаутов. Срок всей сессии держит
`max_session_seconds` в цикле агента. `status` работает без сети, по файлам прогонов: статус, сессий записано из цели,
последняя сессия, суммарная стоимость; расхождение контрольной суммы помечается.
`report [эксперимент] [--wiki DIR]` собирает отчёт сразу (без имени: каждый каталог эксперимента в `runs/`), печатает пути
записанных файлов; ключи рынка из `.env` нужны, как у `tick`; нет ключа или ошибка рынка: одна строка и код 1, неизвестный
эксперимент: одна строка и код 2. Без `--wiki` ни один файл вики не читается и не пишется. Для участника с брокером `alpaca`
команда спрашивает у API (`make_alpaca_api`, больше ничего у брокера) исполненные заявки с первого обращения прогона и передаёт
отчёту id тех, что несут префикс этого прогона; только для прогона, у которого `started_with` совпал с режимом и брокером
конфига, как у тика. Иначе, и при сбое запроса, проверка брокера пропускается одной строкой в stderr, отчёт строится.
`tick [--wiki DIR]` после обхода, когда замок уже снят и рынок открыт (его спрашивает команда у рынка: тик пуст и без активных
прогонов), для каждого эксперимента с `report_due` на сегодняшний день рынка собирает отчёт тем же путём; любое исключение там
это одна строка в stderr, строки тика и код выхода не меняются. Весь раздел ограничен `AUTO_REPORT_SECONDS` (`signal.alarm`, прежние
обработчик и будильник возвращаются, последний с остатком времени), ручной `report` не ограничен. Брокера проверяют по записанному при старте `started_with["broker"]`.
Сверка идёт по заявкам с `filled_at` не позже последней сверки (`last_sync`, без него последняя строка тика) минус две минуты;
у окончённого прогона (`finished`, `dead`) отсечки нет. Срок раздела держит исключение `AutoReportTimeout` (`BaseException`, будильник взводится
заново перед броском) и монотонный срок, проверяемый перед каждым шагом.
Планировщик (`ops/com.traider.tick.plist`) вручную ставит владелец, без `--wiki`.
"""
import argparse
import signal
import sys
import time
import tomllib
from collections.abc import Sequence
from datetime import datetime, timedelta
from pathlib import Path

from anthropic import Anthropic

from traider.alpaca import AlpacaBroker, make_alpaca_api
from traider.brokers import SimBroker
from traider.config import TERMINAL_STATUSES, load_run, start_experiment
from traider.env import load_env
from traider.market import NY, AlpacaMarket, ny_day
from traider.store import RunStore
from traider.tick import tick
from traider.wiki_results import report_due, write_report

RUNS = Path("runs")
KEYS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY")
MONTH_DAYS = 21               # торговых дней в месяце для оценки худшего расхода
API_TIMEOUT = 300.0           # секунд на каждое ожидание сети в одном запросе (связь, каждое чтение потока)
AUTO_REPORT_SECONDS = 120     # потолок всего отчёта после тика (рынок, API брокера, запись файлов)
SYNC_MARGIN = timedelta(minutes=2)   # заявки позже последней сверки минус это в сверку целостности не идут
API_RETRIES = 2               # повторов одного запроса после сбоя связи, 408, 409, 429 и 5xx


def _start(args) -> int:
    try:
        if tomllib.loads(args.toml.read_text(encoding="utf-8"))["mode"] == "live" and not args.live:
            print("режим live в конфиге требует флага --live", file=sys.stderr)
            return 2
        exp = start_experiment(args.toml, RUNS, live=args.live)
    except (OSError, ValueError, KeyError) as e:   # FileExistsError это OSError
        print(f"не удалось начать: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    runs = sorted(p.parent for p in exp.glob("*/config.json"))
    for run in runs:
        print(run)
    day = sum(c.limits.max_session_usd + c.analyst_max_usd for c in (load_run(r)[0] for r in runs))
    print(f"Худший расход: ${day:.2f} в торговый день, ${day * MONTH_DAYS:.2f} за {MONTH_DAYS} торговый день "
          "(потолки сессий участников и аналитика).")
    print("Жёсткая граница расхода это лимит трат в Anthropic Console; потолки программы её не заменяют.")
    return 0


def _tick(args) -> int:
    env = load_env()
    if missing := [k for k in KEYS if not env.get(k)]:
        print(f"в .env нет: {', '.join(missing)}", file=sys.stderr)
        return 1
    market = AlpacaMarket(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"])

    def make_broker(cfg, store):
        return SimBroker(cfg, store, market) if cfg.broker == "sim" else AlpacaBroker(
            cfg, store, market, make_alpaca_api(cfg, env))

    try:
        results = tick(RUNS, market, make_broker, lambda: Anthropic(
            api_key=env["ANTHROPIC_API_KEY"], max_retries=API_RETRIES, timeout=API_TIMEOUT))
    except Exception as e:   # рынок или замок недоступны до обхода
        print(f"тик не состоялся: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    for r in results:
        extra = f" {r['status']} ${r['cost']:.2f}" if r["action"] == "session" else f" {r['error']}" if "error" in r else ""
        when = datetime.fromisoformat(r["ts"]).astimezone(NY).strftime("%Y-%m-%d %H:%M")
        print(f"{when} {r['experiment']}/{r['participant']}: {r['action']}{extra}")
    _auto_report(market, env, args.wiki)   # замок тика уже снят
    return 0


def _experiments() -> list[Path]:
    return sorted(p for p in RUNS.glob("*") if p.is_dir() and any(p.glob("*/config.json")))


def _synced(state: dict, run: Path) -> datetime:
    """Время последней завершённой сверки: `last_sync` из состояния (пишут оба брокера), без него время последней строки
    `ticks.jsonl`; нет ни того ни другого: ValueError."""
    when = state.get("last_sync") or next((t["ts"] for t in reversed(RunStore(run).ticks()) if t.get("ts")), None)
    if when is None:
        raise ValueError("нет ни last_sync, ни строки тика: нечем отделить заявки, которые журнал ещё не успел записать")
    return datetime.fromisoformat(when)


def _broker_ids(exp: Path, env, check=lambda: None) -> dict[str, list[str]]:
    """Исполненные заявки брокера с префиксом прогона, по участникам, чей записанный при старте брокер `alpaca`. Берутся
    заявки с `filled_at` (время исполнения из `filled_orders_since`) не позже последней сверки минус SYNC_MARGIN: позже
    журнал мог ещё не успеть записать, это не нарушение; у прогона в `TERMINAL_STATUSES` отсечки нет (его больше не сверят, берутся все
    заявки с префиксом). Прогон, не прошедший проверку режима и брокера, без времени
    сверки или с неудавшимся запросом даёт строку в stderr и ничего в ответ; к брокеру ходит только API."""
    out = {}
    for run in sorted(c.parent for c in exp.glob("*/config.json")):
        cfg, _ = load_run(run)
        state, who = RunStore(run).state(), f"{cfg.experiment}/{cfg.participant}"
        recorded = (state.get("started_with") or {}).get("broker", cfg.broker)
        if recorded != "alpaca":
            continue
        try:
            if state.get("started_with") != {"mode": cfg.mode, "broker": cfg.broker}:
                raise ValueError("конфиг не совпадает с режимом и брокером, записанными при старте")
            if "run_uid" not in state:
                raise ValueError("прогон ещё не обращался к брокеру")
            check()
            # у окончённого прогона сверки больше не будет: его журнал полон настолько, насколько будет, отсечки нет
            cutoff = None if state["status"] in TERMINAL_STATUSES else _synced(state, run) - SYNC_MARGIN
            prefix = f"{cfg.experiment}/{cfg.participant}/{state['run_uid']}/"
            since = datetime.fromisoformat(state["broker_since"]) - timedelta(days=1)   # запас на часы, как у sync
            out[cfg.participant] = [o["id"] for o in make_alpaca_api(cfg, env).filled_orders_since(since)
                                    if (o["client_order_id"] or "").startswith(prefix)
                                    and (cutoff is None or (o.get("filled_at") is not None and o["filled_at"] <= cutoff))]
        except Exception as e:
            print(f"{who}: проверка брокера пропущена ({type(e).__name__}: {e})", file=sys.stderr)
    return out


def _build(exp: Path, market, env, wiki: Path | None, check=lambda: None) -> None:
    check()
    ids = _broker_ids(exp, env, check)
    check()
    write_report(exp, market, wiki, ids)


class AutoReportTimeout(BaseException):
    """Вышел срок отчёта после тика. Не `Exception`: ни `except Exception` в запросах, ни обёртка HTTP-библиотеки над
    таймаутами сокета не превратят его в другую ошибку, ловит его только `_auto_report`."""


def _on_alarm(signum, frame):
    signal.alarm(1)   # если что-то всё же съест исключение, оно сработает снова; обёртка снимет будильник
    raise AutoReportTimeout(f"не уложился в {AUTO_REPORT_SECONDS} с")


def _auto_report(market, env, wiki: Path | None) -> None:
    """Ничего не печатает в stdout и не бросает: отчёт не должен менять ни строки тика, ни код выхода. Он читает файлы
    прогонов и рынок и может идти, пока другой тик держит замок. Весь раздел, с запросами к рынку и брокеру, ограничен
    будильником и монотонным сроком, который проверяется перед каждым экспериментом и каждым шагом (id брокера, сборка):
    зависший запрос не должен держать задание launchd, а с ним проверку HP и стопы следующих тиков."""
    prev, pending, start = None, 0, time.monotonic()

    def check():
        if time.monotonic() - start > AUTO_REPORT_SECONDS:
            raise AutoReportTimeout(f"не уложился в {AUTO_REPORT_SECONDS} с")
    try:
        prev = signal.signal(signal.SIGALRM, _on_alarm)
        pending = signal.alarm(AUTO_REPORT_SECONDS)
        if not market.is_open():   # тик пуст и при закрытом рынке, и когда все прогоны окончены: открытость спрашиваем у рынка
            return
        day = ny_day(market.now())
        for exp in _experiments():
            check()
            try:
                if report_due(exp, day):
                    _build(exp, market, env, wiki, check)
            except Exception as e:
                check()   # сбой после срока это срок
                print(f"отчёт {exp.name} не собран: {type(e).__name__}: {e}", file=sys.stderr)
    except AutoReportTimeout as e:
        signal.signal(signal.SIGALRM, signal.SIG_IGN)   # повторный сигнал не должен вырваться из обработчика
        signal.alarm(0)
        print(f"отчёты не собраны: {type(e).__name__}: {e}", file=sys.stderr)
    except Exception as e:
        try:
            check()
            print(f"отчёты не собраны: {type(e).__name__}: {e}", file=sys.stderr)
        except AutoReportTimeout as t:
            print(f"отчёты не собраны: {type(t).__name__}: {t}", file=sys.stderr)
    finally:
        if prev is not None:
            signal.signal(signal.SIGALRM, signal.SIG_IGN)   # сначала игнор: сигнал между этими строками ничего не бросит
            signal.alarm(0)
            signal.signal(signal.SIGALRM, prev)
            if pending:
                signal.alarm(max(1, pending - int(time.monotonic() - start)))


def _report(args) -> int:
    exps = _experiments()
    if args.experiment is not None:
        exps = [e for e in exps if e.name == args.experiment]
        if not exps:
            print(f"нет эксперимента {args.experiment} в {RUNS}", file=sys.stderr)
            return 2
    elif not exps:
        print(f"в {RUNS} нет экспериментов", file=sys.stderr)
        return 0
    env = load_env()
    if missing := [k for k in KEYS[:2] if not env.get(k)]:
        print(f"в .env нет: {', '.join(missing)}", file=sys.stderr)
        return 1
    market = AlpacaMarket(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"])
    failed = False
    for exp in exps:
        try:
            _build(exp, market, env, args.wiki)
        except Exception as e:   # рынок, файлы прогона или вики; остальные эксперименты всё равно строятся
            print(f"отчёт {exp.name} не собран: {type(e).__name__}: {e}", file=sys.stderr)
            failed = True
            continue
        print(exp / "report.json")
        print(exp / "dashboard.html")
    return 1 if failed else 0


def _status_line(run: Path) -> str:
    try:
        cfg, valid = load_run(run)
        store = RunStore(run)
        sessions = store.sessions()
        last = f"{sessions[-1]['day']} {sessions[-1]['status']}" if sessions else "нет"
        cost = sum(s.get("cost", 0.0) for s in sessions)
        return (f"{cfg.experiment}/{cfg.participant}: {store.state()['status']}, сессий {len(sessions)}/{cfg.sessions_target}, "
                f"последняя {last}, расход ${cost:.2f}" + ("" if valid else ", контрольная сумма не сходится"))
    except (OSError, ValueError, KeyError, TypeError) as e:
        return f"{run.parent.name}/{run.name}: не читается ({type(e).__name__}: {e})"


def _status(_args) -> int:
    runs = sorted(p.parent for p in RUNS.glob("*/*/config.json"))
    for run in runs:
        print(_status_line(run))
    if not runs:
        print("нет прогонов")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="traider")
    sub = parser.add_subparsers(dest="command", required=True)
    start = sub.add_parser("start", help="заморозить прогоны из TOML")
    start.add_argument("toml", type=Path)
    start.add_argument("--live", action="store_true", help="разрешить конфиг с mode = live")
    start.set_defaults(run=_start)
    tk = sub.add_parser("tick", help="один обход прогонов")
    tk.add_argument("--wiki", type=Path, help="каталог вики: отчёт после тика обновит там статью результатов")
    tk.set_defaults(run=_tick)
    rep = sub.add_parser("report", help="собрать отчёт и страницу сразу")
    rep.add_argument("experiment", nargs="?", help="имя эксперимента; без него все из runs/")
    rep.add_argument("--wiki", type=Path, help="каталог вики: обновить там статью результатов")
    rep.set_defaults(run=_report)
    sub.add_parser("status", help="состояние прогонов, без сети").set_defaults(run=_status)
    args = parser.parse_args(argv)
    return args.run(args)
