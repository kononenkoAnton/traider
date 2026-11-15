"""Командная строка: `traider start`, `traider tick`, `traider status`.

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
Планировщик (`ops/com.traider.tick.plist`) вручную ставит владелец.
"""
import argparse
import sys
import tomllib
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path

from anthropic import Anthropic

from traider.alpaca import AlpacaBroker, make_alpaca_api
from traider.brokers import SimBroker
from traider.config import load_run, start_experiment
from traider.env import load_env
from traider.market import NY, AlpacaMarket
from traider.store import RunStore
from traider.tick import tick

RUNS = Path("runs")
KEYS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY")
MONTH_DAYS = 21               # торговых дней в месяце для оценки худшего расхода
API_TIMEOUT = 300.0           # секунд на каждое ожидание сети в одном запросе (связь, каждое чтение потока)
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


def _tick(_args) -> int:
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
    return 0


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
    sub.add_parser("tick", help="один обход прогонов").set_defaults(run=_tick)
    sub.add_parser("status", help="состояние прогонов, без сети").set_defaults(run=_status)
    args = parser.parse_args(argv)
    return args.run(args)
