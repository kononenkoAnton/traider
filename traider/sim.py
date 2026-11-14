"""Симулятор счёта: покупка, продажа и стопы как чистые функции.

:wk-id: sim-module
:wk-tags: accounts, slippage, stops
:wk-categories: system-architecture

Как это работает. Каждая функция берёт `Account` и возвращает новый, ничего не читая
и не записывая. Покупка идёт по `price * (1 + SLIPPAGE)`, любая продажа (закрытие,
сработавший стоп, ликвидация) по `price * (1 - SLIPPAGE)`. Докупка той же бумаги
сливается в одну позицию: количества складываются, цена входа это уплаченные деньги
на общее количество, стоп берётся от новой покупки. Стоп срабатывает, если минимум
не выше стопа; без гэпа он исполняется по стопу, при гэпе вниз по цене открытия,
то есть по `min(стоп, открытие)`. Нет данных по бумаге: стоп не трогаем. Нечисловые
или неположительные цена, сумма и доля в `apply_buy` и `apply_close` дают ValueError;
в `apply_stops` такой минимум означает отсутствие данных, а такое открытие считается
выше стопа. `mark_peak` пик капитала только поднимает.
"""
from collections.abc import Mapping
from dataclasses import dataclass, replace
from math import isfinite

from traider.cage import Account, Position, equity

SLIPPAGE = 0.0005

@dataclass(frozen=True)
class Fill:
    symbol: str
    qty: float
    price: float     # цена исполнения с учётом проскальзывания
    kind: str        # "buy" | "close" | "stop_fill" | "liquidate"

def _ok(x: float | None) -> bool:
    return isinstance(x, (int, float)) and isfinite(x) and x > 0

def _positive(name: str, x: float) -> float:
    if not _ok(x):
        raise ValueError(f"{name} должно быть конечным положительным числом: {x}")
    return x

def _sell(acct: Account, pos: Position, qty: float, price: float, kind: str) -> tuple[Account, Fill]:
    fill = Fill(pos.symbol, qty, price * (1 - SLIPPAGE), kind)
    kept = pos.qty - qty
    rest = tuple(p for p in acct.positions if p.symbol != pos.symbol)
    if kept > 0:
        rest = tuple(replace(p, qty=kept) if p.symbol == pos.symbol else p for p in acct.positions)
    return replace(acct, cash=acct.cash + qty * fill.price, positions=rest), fill

def apply_buy(acct: Account, symbol: str, notional: float, price: float,
              stop_price: float) -> tuple[Account, Fill]:
    _positive("сумма", notional)
    _positive("цена", price)
    fill_price = price * (1 + SLIPPAGE)
    qty = notional / fill_price
    old = next((p for p in acct.positions if p.symbol == symbol), None)
    if old is None:
        new = Position(symbol, qty, fill_price, stop_price)
        positions = acct.positions + (new,)
    else:
        total = old.qty + qty
        new = Position(symbol, total, (old.qty * old.entry_price + notional) / total, stop_price)
        positions = tuple(new if p.symbol == symbol else p for p in acct.positions)
    return replace(acct, cash=acct.cash - notional, positions=positions), Fill(symbol, qty, fill_price, "buy")

def apply_close(acct: Account, symbol: str, fraction: float, price: float,
                kind: str = "close") -> tuple[Account, Fill]:
    pos = next((p for p in acct.positions if p.symbol == symbol), None)
    if pos is None:
        raise KeyError(symbol)
    if not (_ok(fraction) and fraction <= 1):
        raise ValueError(f"доля должна быть в (0, 1]: {fraction}")
    _positive("цена", price)
    return _sell(acct, pos, pos.qty * fraction, price, kind)

def apply_stops(acct: Account, lows: Mapping[str, float],
                opens: Mapping[str, float]) -> tuple[Account, list[Fill]]:
    fills: list[Fill] = []
    for pos in acct.positions:
        low = lows.get(pos.symbol)
        if not _ok(low) or low > pos.stop_price:
            continue
        opened = opens.get(pos.symbol)
        base = min(pos.stop_price, opened) if _ok(opened) else pos.stop_price
        acct, fill = _sell(acct, pos, pos.qty, base, "stop_fill")
        fills.append(fill)
    return acct, fills

def mark_peak(acct: Account, prices: Mapping[str, float]) -> Account:
    return replace(acct, peak_equity=max(acct.peak_equity, equity(acct, prices)))
