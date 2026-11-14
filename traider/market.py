"""Протокол рынка: всё, что программа узнаёт о ценах и новостях.

:wk-id: market-protocol
:wk-tags: market, protocol
:wk-categories: system-architecture

Как это работает. `Market` это только контракт, реализации живут отдельно (настоящий
источник котировок и поддельный рынок в тестах). Время `now` с поясом America/New_York.
Бумаги без цены в ответ `last_prices` не попадают. `lows_since` отдаёт для каждой бумаги
пару (минимум с указанного момента, первая цена открытия): по ней считаются стопы
задним числом. `Bar` это дневная свеча.
"""
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class Bar:
    day: str
    open: float
    high: float
    low: float
    close: float
    volume: float


class Market(Protocol):
    def is_open(self) -> bool: ...
    def now(self) -> datetime: ...
    def last_prices(self, symbols: Iterable[str]) -> dict[str, float]: ...
    def trading_days(self, start: str, end: str) -> list[str]: ...
    def daily_bars(self, symbols: Iterable[str], days: int) -> dict[str, list[Bar]]: ...
    def lows_since(self, symbols: Iterable[str], since: datetime) -> dict[str, tuple[float, float]]: ...
    def news(self, symbols: Sequence[str] | None, limit: int) -> list[dict]: ...
    def fractionable(self, symbols: Iterable[str]) -> set[str]: ...
