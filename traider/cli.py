"""Командная строка: `traider start`, `traider tick`, `traider status`.

:wk-id: cli-module
:wk-tags: cli, schedule, status
:wk-categories: system-architecture

Как это работает. Все пути считаются от текущего каталога: прогоны лежат в `runs/`, ключи в `.env`.
`start <toml> [--live]` замораживает прогоны (`start_experiment`) и печатает их каталоги; конфиг с
`mode = "live"` без `--live` даёт код 2 и одну строку, другая ошибка конфига даёт код 1. Живой
режим включается только здесь, существующий прогон командой живым не станет. `tick` читает `.env`,
собирает `AlpacaMarket`, выбирает брокера по `cfg.broker` (`sim` встроенный, `alpaca` через
`make_alpaca_api`), клиента Anthropic и печатает по строке на прогон; ошибка до обхода это одна
строка и код 1. `status` работает без сети, по файлам прогонов: статус, сессий записано из цели,
последняя сессия, суммарная стоимость; расхождение контрольной суммы помечается.
Планировщик (`ops/com.traider.tick.plist`) вручную ставит владелец.
"""
import argparse
import sys
import tomllib
from collections.abc import Sequence
from pathlib import Path

from anthropic import Anthropic

from traider.alpaca import AlpacaBroker, make_alpaca_api
from traider.brokers import SimBroker
from traider.config import load_run, start_experiment
from traider.env import load_env
from traider.market import AlpacaMarket
from traider.store import RunStore
from traider.tick import tick

RUNS = Path("runs")
KEYS = ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY")


def _start(args) -> int:
    try:
        if tomllib.loads(args.toml.read_text(encoding="utf-8"))["mode"] == "live" and not args.live:
            print("режим live в конфиге требует флага --live", file=sys.stderr)
            return 2
        exp = start_experiment(args.toml, RUNS, live=args.live)
    except (OSError, ValueError, KeyError) as e:   # FileExistsError это OSError
        print(f"не удалось начать: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    for run in sorted(p.parent for p in exp.glob("*/config.json")):
        print(run)
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
        results = tick(RUNS, market, make_broker, lambda: Anthropic(api_key=env["ANTHROPIC_API_KEY"]))
    except Exception as e:   # рынок или замок недоступны до обхода
        print(f"тик не состоялся: {type(e).__name__}: {e}", file=sys.stderr)
        return 1
    for r in results:
        extra = f" {r['status']} ${r['cost']:.2f}" if r["action"] == "session" else f" {r['error']}" if "error" in r else ""
        print(f"{r['participant']}: {r['action']}{extra}")
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
