"""Проверка HP на каждом тике: пол капитала, стопы и гибель прогона.

:wk-id: safety-module
:wk-tags: hp, floor, run-death
:wk-categories: risk-cage

Как это работает. `run_safety` вызывается планировщиком для каждого прогона на каждом
тике и модель не использует. Для прогона со статусом `dead` или `finished` она ничего
не делает и возвращает нулевой отчёт с `died=False`; статусы `active`, `finishing` и
`invalid` проверяются как обычно. Порядок: сверка `broker.sync` (сработавшие стопы),
капитал и пол по счёту брокера и ценам удерживаемых бумаг, затем либо гибель, либо
`broker.ensure_stops`. Капитал ниже пола: `broker.liquidate`, статус `dead` и день
гибели в `died_on`. Нечисловой капитал даёт ValueError из `traider.cage`, он не
перехватывается: молча пропущенная проверка опаснее остановки.
"""
from dataclasses import dataclass

from traider.brokers import Broker
from traider.cage import equity, floor, hp, is_dead
from traider.config import TERMINAL_STATUSES, RunConfig
from traider.store import RunStore


@dataclass(frozen=True)
class SafetyReport:
    equity: float
    floor: float
    hp: float
    died: bool
    stops_placed: tuple[str, ...]
    stop_fills: tuple[str, ...]


def run_safety(cfg: RunConfig, store: RunStore, broker: Broker, day: str) -> SafetyReport:
    if store.state()["status"] in TERMINAL_STATUSES:
        return SafetyReport(0.0, 0.0, 0.0, False, (), ())
    fills = tuple(e["symbol"] for e in broker.sync(day))
    acct = broker.account()
    prices = broker.prices([p.symbol for p in acct.positions])
    eq, fl, h = equity(acct, prices), floor(acct, cfg.limits), hp(acct, prices, cfg.limits)
    if is_dead(acct, prices, cfg.limits):
        broker.liquidate(day)
        store.update_state(status="dead", died_on=day)
        return SafetyReport(eq, fl, h, True, (), fills)
    return SafetyReport(eq, fl, h, False, tuple(broker.ensure_stops()), fills)
