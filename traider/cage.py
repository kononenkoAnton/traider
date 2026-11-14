"""Клетка риска: чистые функции над состоянием счёта.

:wk-id: cage-module
:wk-tags: risk-cage, limits
:wk-categories: risk-cage

Как это работает. Модуль ничего не читает и не пишет: на вход состояние счёта, цены,
факты сессии и `Limits`, на выход `Reject` (код и русская фраза с числами для агента)
или `None`, если заявка разрешена. `check_buy` проверяет заявку в фиксированном
порядке и возвращает первый сработавший отказ; `check_raise_stop` делает то же для
подъёма стопа. Границы включительные, сравнения идут с допуском 1e-9, чтобы ошибка
округления float не отклоняла заявку ровно на пределе. Капитал считается по текущим
ценам, бумага без цены оценивается по цене входа. Пол капитала это `floor_share` от
пика капитала, HP это запас капитала над полом. Риск позиции это
`qty * (цена - стоп)`, позиция уже под стопом риска не несёт. Риск после покупки
считается со стопом новой заявки на всю позицию вместо прежнего риска этой бумаги.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from math import isfinite

from traider.config import Limits
from traider.nums import usable

EPS = 1e-9


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


def _price(p: Position, prices: Mapping[str, float]) -> float:
    # котировка годится только конечная и положительная, иначе оцениваем по цене входа
    q = prices.get(p.symbol)
    return q if usable(q) else p.entry_price


def _finite(x: float) -> float:
    if not isfinite(x):
        raise ValueError(f"нечисловое значение в счёте: {x}")
    return x


def _account_corrupt(acct: Account) -> bool:
    nums = [acct.cash, acct.peak_equity]
    for p in acct.positions:
        nums += [p.qty, p.entry_price, p.stop_price]
    return not all(isinstance(x, (int, float)) and isfinite(x) for x in nums)


def equity(acct: Account, prices: Mapping[str, float]) -> float:
    return _finite(acct.cash + sum(p.qty * _price(p, prices) for p in acct.positions))


def floor(acct: Account, limits: Limits) -> float:
    return limits.floor_share * acct.peak_equity


def hp(acct: Account, prices: Mapping[str, float], limits: Limits) -> float:
    return _finite(equity(acct, prices) - floor(acct, limits))


def is_dead(acct: Account, prices: Mapping[str, float], limits: Limits) -> bool:
    return hp(acct, prices, limits) < -EPS


def _risk(p: Position, price: float, stop: float | None = None) -> float:
    return p.qty * max(price - (p.stop_price if stop is None else stop), 0.0)


def open_risk(acct: Account, prices: Mapping[str, float]) -> float:
    return sum(_risk(p, _price(p, prices)) for p in acct.positions)


def _stop_range(price: float, stop: float, limits: Limits) -> Reject | None:
    lo, hi = price * (1 - limits.stop_max), price * (1 - limits.stop_min)
    if lo - EPS <= stop <= hi + EPS:
        return None
    return Reject("stop_out_of_range",
                  f"Стоп {stop:.2f} вне диапазона: при цене {price:.2f} он должен быть "
                  f"от {lo:.2f} ({limits.stop_max:.0%} ниже) до {hi:.2f} ({limits.stop_min:.0%} ниже).")


def check_buy(req: BuyRequest, acct: Account, prices: Mapping[str, float], facts: SessionFacts,
              universe: frozenset[str], limits: Limits) -> Reject | None:
    sym = req.symbol
    if _account_corrupt(acct):
        return Reject("bad_request", "В данных счёта нечисловое значение, заявка не принимается.")
    if not (usable(req.notional) and usable(req.stop_price)):
        return Reject("bad_request", "Сумма и стоп должны быть конечными положительными числами.")
    if sym not in universe:
        return Reject("not_in_universe", f"{sym} нет во вселенной, купить его нельзя.")
    price = prices.get(sym)
    if not usable(price):
        return Reject("no_price", f"Нет цены для {sym}, покупка невозможна.")
    if not facts.panel_done:
        return Reject("no_panel_forecasts", "Сначала запишите прогнозы по всей панели, потом покупайте.")
    if sym not in facts.forecast_symbols:
        return Reject("no_symbol_forecast", f"Сначала запишите прогноз по {sym}, потом покупайте.")
    if not (facts.buys_done < limits.max_buys_per_session):
        return Reject("too_many_buys", f"Лимит покупок за сессию исчерпан: {limits.max_buys_per_session}.")
    if sym in facts.closed_today:
        return Reject("rebuy_same_day", f"{sym} сегодня уже закрыт, повторно купить его сегодня нельзя.")
    if not (req.notional >= limits.min_order_usd - EPS):
        return Reject("order_too_small",
                      f"Заявка ${req.notional:.2f} меньше минимума ${limits.min_order_usd:.2f}.")
    if bad := _stop_range(price, req.stop_price, limits):
        return bad
    held = [p for p in acct.positions if p.symbol == sym]
    old_stop = max((p.stop_price for p in held), default=None)
    if old_stop is not None and not (req.stop_price >= old_stop - EPS):
        return Reject("stop_below_existing",
                      f"Стоп {req.stop_price:.2f} ниже действующего {old_stop:.2f} по {sym}: стоп можно только поднимать.")
    eq = equity(acct, prices)
    old_qty = sum(p.qty for p in held)
    new_value = old_qty * price + req.notional
    cap = limits.max_position_share * eq
    if not (new_value <= cap + EPS):
        return Reject("position_too_large",
                      f"Позиция по {sym} после покупки ${new_value:.2f} превысит "
                      f"{limits.max_position_share:.0%} капитала (${cap:.2f}).")
    if not (req.notional <= acct.cash + EPS):
        return Reject("not_enough_cash", f"Заявка ${req.notional:.2f} больше свободных денег ${acct.cash:.2f}.")
    other = sum(_risk(p, _price(p, prices)) for p in acct.positions if p.symbol != sym)
    new_risk = other + (old_qty + req.notional / price) * (price - req.stop_price)
    max_risk = limits.max_open_risk_share * eq
    if not (new_risk <= max_risk + EPS):
        return Reject("open_risk_too_high",
                      f"Суммарный риск после покупки ${new_risk:.2f} превысит "
                      f"{limits.max_open_risk_share:.0%} капитала (${max_risk:.2f}).")
    return None


def check_raise_stop(symbol: str, new_stop: float, acct: Account, prices: Mapping[str, float],
                     limits: Limits) -> Reject | None:
    if _account_corrupt(acct):
        return Reject("bad_request", "В данных счёта нечисловое значение, стоп не меняется.")
    if not usable(new_stop):
        return Reject("bad_request", "Стоп должен быть конечным положительным числом.")
    held = [p for p in acct.positions if p.symbol == symbol]
    if not held:
        return Reject("no_position", f"Позиции по {symbol} нет, поднимать нечего.")
    price = prices.get(symbol)
    if not usable(price):
        return Reject("no_price", f"Нет цены для {symbol}, стоп изменить нельзя.")
    old_stop = max(p.stop_price for p in held)
    if not (new_stop > old_stop + EPS):
        return Reject("stop_not_higher", f"Новый стоп {new_stop:.2f} не выше действующего {old_stop:.2f}.")
    return _stop_range(price, new_stop, limits)
