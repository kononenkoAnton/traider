"""Тик: один обход всех прогонов, который планировщик запускает каждые 15 минут.

:wk-id: tick-module
:wk-tags: tick, schedule, lock
:wk-categories: system-architecture

Как это работает. `tick` ничего не делает при закрытом рынке и когда замок `runs/.tick.lock`
(`{"pid", "ts"}`) моложе `lock_minutes`. Замок создаётся атомарно (`os.link` готового файла) и несёт токен этого вызова:
владелец тот, чей токен в файле. Нечитаемый или старый замок уходит атомарным `os.rename`
(не вышло, значит другой тик успел) и создаётся заново. Метка `ts` берётся из `market.now()` и переписывается перед
каждым прогоном, если замок ещё наш (иначе тик останавливается и замок не трогает), поэтому срок считается от последнего признака жизни, а не от старта тика.
Замок снимается в `finally`, только если он наш. Порядок участников сдвигается по дням: первым идёт номер
«день года по модулю числа участников».

В начале тика: если два и больше нетерминальных прогона с брокером `alpaca` в одном режиме (по
`started_with`), каждый из них получает `error` с их именами, брокер для них не создаётся; остальные идут как обычно.
Перед всем: режим и брокер конфига должны совпасть с записью `started_with` в состоянии прогона
(пишет `start_experiment`); иначе брокер не создаётся, статус `invalid`, действие `error`.
Для одного прогона, в этом порядке, каждый шаг может закончить работу над ним:
1. неверная контрольная сумма и статус не `invalid` дают `invalid`; 2. первый тик ставит
`first_day`; 3. `run_safety` (погиб: `died`); 4. статус `invalid`: `invalid`, без сессии;
5. статус `finishing`: продать остаток, сохранить снимок цен дня, через 5 торговых дней
после `finish_day` статус `finished` (иначе `finishing`); 6. для `active`: зависшая `started`
старше 30 минут становится `failed` (`stale_failed`, если она сегодняшняя), каждый торговый
день от `first_day` до вчера без записи пишется `missed`; 7. время позже `window_end` без
записи за сегодня: `missed`; 8. не раньше `window_start`, рынок открыт, записи нет: при
непустом `unpriced` это `unpriced`, иначе запись `started`, аналитик (второй клиент) для
`research = "analyst"` и `run_session` (`session` со статусом и стоимостью); 9. иначе `safety`. Статус не `active` после шагов 4 и 5 (неизвестный) даёт `safety` без учёта дней.
Любое исключение по одному прогону даёт `error` с текстом и не мешает остальным. Каждый элемент
результата несёт `experiment`, `participant`, `action`, `ts` (время рынка) и, если проверка HP прошла,
`equity`, `floor`, `hp`, `stops_placed`, `stop_fills`, `unpriced`; тот же набор (с `day`, `error`, а у сессии
`status` и `cost`) пишется строкой в `ticks.jsonl` прогона через `RunStore.append_tick`, для ошибок тоже, если
каталог прогона пишется. Клиенты
создаются до записи `started`, чтобы сбой создания не оставил зависшую запись; когда
сессии нет, `make_client` не вызывается.
"""
import json
import os
import uuid
from collections.abc import Callable
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from traider.analyst import Analyst
from traider.brokers import Broker
from traider.config import MODES, RunConfig, active_runs, alpaca_runs, load_run
from traider.market import NY, Market, ny_day
from traider.nums import usable
from traider.safety import run_safety
from traider.session import run_session
from traider.store import RunStore

FINISH_AFTER_DAYS = 5


def _write_lock(lock: Path, now: datetime, token: str, exclusive: bool) -> bool:
    tmp = lock.with_name(f"{lock.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps({"pid": os.getpid(), "ts": now.isoformat(), "token": token}))
    try:
        if exclusive:
            os.link(tmp, lock)   # не перезаписывает: из двух одновременных тиков замок получит один
        else:
            os.replace(tmp, lock)
        return True
    except FileExistsError:
        return False
    finally:
        tmp.unlink(missing_ok=True)


def _owned(lock: Path, token: str) -> bool:
    """Замок наш, если в файле наш токен; pid только справка (в тестах два тика в одном процессе)."""
    try:
        return json.loads(lock.read_text())["token"] == token
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _acquire(lock: Path, now: datetime, lock_minutes: int, token: str) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    if _write_lock(lock, now, token, exclusive=True):
        return True
    try:
        young = now - datetime.fromisoformat(json.loads(lock.read_text())["ts"]) < timedelta(minutes=lock_minutes)
    except (OSError, ValueError, KeyError, TypeError):
        young = False   # нечитаемый замок это след упавшего тика
    if young:
        return False
    moved = lock.with_name(f"{lock.name}.{uuid.uuid4().hex}.old")
    try:
        os.rename(lock, moved)   # атомарно: замок ушёл у нас или уже не существует (другой тик успел)
    except FileNotFoundError:
        return False
    moved.unlink(missing_ok=True)
    return _write_lock(lock, now, token, exclusive=True)


def _refresh(lock: Path, now: datetime, token: str) -> bool:
    # ponytail: между проверкой и заменой остаётся окно в доли миллисекунды; закрывает только flock
    return _owned(lock, token) and _write_lock(lock, now, token, exclusive=False)


def _finish_day_passed(market: Market, finish_day: str, day: str) -> bool:
    return len([d for d in market.trading_days(finish_day, day) if d > finish_day]) >= FINISH_AFTER_DAYS


def _finishing(cfg: RunConfig, store: RunStore, broker: Broker, market: Market, day: str) -> str:
    if broker.account().positions:
        broker.liquidate(day)
    if store.prices(day) is None:
        store.save_prices(day, {s: float(p) for s, p in market.last_prices(cfg.universe).items() if usable(p)})
    if _finish_day_passed(market, store.state()["finish_day"], day):
        store.update_state(status="finished")
        return "finished"
    return "finishing"


def _bookkeeping(store: RunStore, market: Market, day: str, now: datetime) -> bool:
    """Зависшие старты становятся сбойными, пропущенные дни записываются. True, если сегодняшний старт завис."""
    stale = store.stale_started(now)
    for d in stale:
        store.finish_session(d, "failed", {"stop": "stale", "errors": ["сессия зависла в статусе started"]})
    yesterday = (date.fromisoformat(day) - timedelta(days=1)).isoformat()
    first = store.state()["first_day"]
    for d in market.trading_days(first, yesterday) if first <= yesterday else []:
        if store.start_session(d, now):
            store.finish_session(d, "missed", {})
    return day in stale


def _one(run: Path, market: Market, make_broker, make_client) -> dict:
    cfg, valid = load_run(run)
    store, now = RunStore(run), market.now()
    day = ny_day(now)
    out: dict[str, Any] = {"participant": cfg.participant}
    if store.state().get("started_with") != {"mode": cfg.mode, "broker": cfg.broker}:
        # конфиг правили: режим или брокер уже не те, что записаны при старте. Брокер не создаётся совсем,
        # иначе правка paper на live вывела бы проверку HP на настоящий счёт
        store.update_state(status="invalid")
        return out | {"action": "error", "error": "конфиг больше не совпадает с режимом и брокером, записанными при "
                      "старте прогона; брокер не создавался, ничего не отправлено, счёт нужно проверить вручную"}
    if not valid and store.state()["status"] != "invalid":
        store.update_state(status="invalid")
    if store.state().get("first_day") is None:
        store.update_state(first_day=day)
    broker = make_broker(cfg, store)
    report = run_safety(cfg, store, broker, day)
    out |= {"equity": report.equity, "floor": report.floor, "hp": report.hp, "stops_placed": list(report.stops_placed),
            "stop_fills": list(report.stop_fills), "unpriced": list(report.unpriced)}
    status = store.state()["status"]
    if report.died:
        return out | {"action": "died"}
    if status == "invalid":
        return out | {"action": "invalid"}
    if status == "finishing":
        return out | {"action": _finishing(cfg, store, broker, market, day)}
    if status != "active":
        return out | {"action": "safety"}   # неизвестный статус: ни учёта дней, ни сессии
    stale_today = _bookkeeping(store, market, day, now)
    clock = now.astimezone(NY).strftime("%H:%M")
    if store.session(day) is None:
        if clock > cfg.window_end:
            if store.start_session(day, now):
                store.finish_session(day, "missed", {})
            return out | {"action": "missed"}
        if clock >= cfg.window_start and market.is_open():
            if report.unpriced:
                return out | {"action": "unpriced"}
            client = make_client()
            analyst = Analyst(make_client(), cfg, market) if cfg.research == "analyst" else None
            if store.start_session(day, now):
                rec = run_session(cfg, store, broker, market, client, day, analyst)
                return out | {"action": "session", "status": rec["status"], "cost": rec.get("cost", 0.0)}
    return out | {"action": "stale_failed" if stale_today else "safety"}


def tick(runs_dir: Path, market: Market, make_broker: Callable[[RunConfig, RunStore], Broker],
         make_client: Callable[[], Any], lock_minutes: int = 60) -> list[dict]:
    lock, token = Path(runs_dir) / ".tick.lock", uuid.uuid4().hex
    if not market.is_open() or not _acquire(lock, market.now(), lock_minutes, token):
        return []
    out: list[dict] = []
    try:
        shared = _shared_alpaca(runs_dir)
        for run in _rotated(active_runs(runs_dir), ny_day(market.now())):
            if not _refresh(lock, market.now(), token):
                break   # замок забрал другой тик: не трогаем ни его, ни прогоны
            try:
                r = ({"participant": run.name, "action": "error", "error": shared[run]} if run in shared
                     else _one(run, market, make_broker, make_client))
            except Exception as e:
                r = {"participant": run.name, "action": "error", "error": f"{type(e).__name__}: {e}"}
            out.append({"experiment": run.parent.name, **r, "ts": market.now().isoformat()})
            _log_tick(run, out[-1])
        return out
    finally:
        if _owned(lock, token):
            lock.unlink(missing_ok=True)


TICK_FIELDS = ("equity", "floor", "hp", "stops_placed", "stop_fills", "unpriced", "error")


def _log_tick(run: Path, r: dict) -> None:
    """Строка в `ticks.jsonl` прогона; поля, до которых тик не дошёл, пишутся как null."""
    rec = {"ts": r["ts"], "day": ny_day(datetime.fromisoformat(r["ts"])), "action": r["action"],
           **{k: r.get(k) for k in TICK_FIELDS}}
    if r["action"] == "session":
        rec |= {"status": r["status"], "cost": r["cost"]}
    try:
        RunStore(run).append_tick(rec)
    except OSError:
        pass   # каталог прогона не пишется: результат тика всё равно вернётся и напечатается


def _shared_alpaca(runs_dir: Path) -> dict[Path, str]:
    """Прогоны, которые делят один счёт Alpaca (два и больше нетерминальных в одном режиме), с текстом ошибки."""
    out = {}
    for mode in MODES:
        found = alpaca_runs(runs_dir, mode)
        if len(found) > 1:
            names = ", ".join(f"{r.parent.name}/{r.name}" for r in found)
            text = (f"на одном счёте Alpaca ({mode}) больше одного незавершённого прогона: {names}; брокер не "
                    "создавался, ничего не отправлено. Оставьте один прогон (finished, dead или каталог вне runs/)")
            out |= dict.fromkeys(found, text)
    return out


def _rotated(runs: list[Path], day: str) -> list[Path]:
    k = date.fromisoformat(day).timetuple().tm_yday % len(runs) if runs else 0
    return runs[k:] + runs[:k]
