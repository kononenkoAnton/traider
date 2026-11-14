"""Проверка числа, которому можно верить: одна на весь проект.

:wk-id: nums-module
:wk-tags: validation, prices
:wk-categories: system-architecture

Как это работает. `usable(x)` истинно, только если `x` это int или float (не bool),
конечное и больше нуля. Так проверяются котировки, суммы, стопы и цены закрытия в
клетке, симуляторе, брокере и оценке прогнозов.
"""
from math import isfinite


def usable(x: object) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x) and x > 0
