"""Протокол рынка: всё, что программа узнаёт о ценах и новостях.

:wk-id: market-protocol
:wk-tags: market, protocol
:wk-categories: system-architecture

Как это работает. `Market` это только контракт, реализации живут отдельно (настоящий
источник котировок и поддельный рынок в тестах). Время `now` с поясом America/New_York.
Бумаги без цены в ответ `last_prices` не попадают. `lows_since` отдаёт для каждой бумаги
список пар (минимум, открытие), по одной на каждую минутную свечу с указанного момента, в порядке
времени; бумага без свечей в ответ не попадает. Свеча помечена временем своего начала, поэтому
`since` округляется вниз до минуты (`floor_minute`): минута, в которой лежит `since`, входит целиком,
предыдущая нет. По ним стопы считаются задним числом:
исполнение идёт по открытию той свечи, что первой задела стоп, поэтому гэп через ночь
исполняется по открытию, а не по стопу. `Bar` это дневная свеча.

`AlpacaMarket` это настоящий рынок: тонкая обёртка над alpaca-py, все вызовы SDK только в
нём. Данные берутся с бесплатной ленты IEX, она указывается явно. Любая ошибка связи или API
поднимает исключение, пустой ответ никогда не прячет сбой. Одно исключение (`without_invalid`): если в запросе цен
или свечей одна бумага записана недопустимо, Alpaca отклоняет весь запрос (`invalid symbol: X`, увидено на paper
16.11.2026); названную бумагу убираем и спрашиваем остальных, как того требует контракт. Цены и свечи отдаются обычными
`float`. Цена годна, только если сделка была сегодня по Нью-Йорку, иначе бумага без цены.
Свечи для стопов берутся только из обычной сессии по календарю Alpaca (с учётом коротких
дней), дневные свечи только завершённые: пока рынок открыт, сегодняшняя отбрасывается. Дневные свечи
скорректированы на сплиты (`adjustment=Adjustment.SPLIT`), минутные для стопов нет.
Преобразования без сети (свеча SDK в `Bar`, пары (минимум, открытие), день по
Нью-Йорку) вынесены в чистые функции. Бесплатный счёт не видит свежих данных SIP, потому IEX.
`bounded` ставит на сессию клиента alpaca-py срок запроса 5 секунд на соединение и 30 на чтение (SDK сам срока не
задаёт, и зависшее чтение держало бы тик вечно) и не повторяет запросов: истёкший срок это обычное исключение, которое
тик пишет как `error`. Через `bounded` создаются все клиенты здесь и в `traider.alpaca`.
"""
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol
from zoneinfo import ZoneInfo

from alpaca.common.exceptions import APIError
from alpaca.data.enums import Adjustment, DataFeed
from alpaca.data.historical.news import NewsClient
from alpaca.data.historical.stock import StockHistoricalDataClient
from alpaca.data.requests import NewsRequest, StockBarsRequest, StockLatestTradeRequest
from alpaca.data.timeframe import TimeFrame, TimeFrameUnit
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import AssetStatus
from alpaca.trading.requests import GetAssetsRequest, GetCalendarRequest
from requests.adapters import HTTPAdapter

from traider.nums import usable

NY = ZoneInfo("America/New_York")


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
    def lows_since(self, symbols: Iterable[str], since: datetime) -> dict[str, list[tuple[float, float]]]: ...
    # ^ минутные свечи с начала минуты, в которой лежит since (floor_minute), минута since входит целиком
    def news(self, symbols: Sequence[str] | None, limit: int) -> list[dict]: ...
    def fractionable(self, symbols: Iterable[str]) -> set[str]: ...


def ny_day(ts: datetime) -> str:
    return ts.astimezone(NY).date().isoformat()


def today() -> str:
    """Сегодняшний день в Нью-Йорке по часам системы: день решений владельца (команды и страница управления)."""
    return ny_day(datetime.now(NY))


def floor_minute(ts: datetime) -> datetime:
    """Начало минуты, в которой лежит `ts`: с него начинается свеча, содержащая `ts`."""
    return ts.replace(second=0, microsecond=0)


def bar_from_sdk(b) -> Bar:
    return Bar(ny_day(b.timestamp), float(b.open), float(b.high), float(b.low), float(b.close), float(b.volume))


def traded_today(ts: datetime, now: datetime) -> bool:
    return ny_day(ts) == ny_day(now)


def completed_days(bars: list[Bar], today: str, market_open: bool) -> list[Bar]:
    return [b for b in bars if not (market_open and b.day == today)]


def regular_session_bars(bars, sessions):
    """Свечи, начало которых лежит в [открытие, закрытие) одной из сессий (пары aware-времён)."""
    return [b for b in bars if any(o <= b.timestamp < c for o, c in sessions)]


def low_open_pairs(bars) -> list[tuple[float, float]]:
    return [(float(b.low), float(b.open)) for b in bars]


TIMEOUT = (5, 30)   # секунд на соединение и на чтение


class _Timeout(HTTPAdapter):
    """Срок запроса, если вызов его не задал (alpaca-py не задаёт): зависший ответ становится ошибкой, а не вечным
    тиком. Повторов нет: заявку, чей ответ не пришёл, найдёт следующая сверка, а не второй запрос."""
    def __init__(self, timeout):
        super().__init__(max_retries=0)
        self.timeout = timeout

    def send(self, request, timeout=None, **kw):
        return super().send(request, timeout=self.timeout if timeout is None else timeout, **kw)


def bounded(client, timeout=TIMEOUT):
    """Клиент alpaca-py с этим сроком на каждом запросе его сессии (`_session`: requests.Session SDK)."""
    for prefix in ("https://", "http://"):
        client._session.mount(prefix, _Timeout(timeout))
    return client


_INVALID = re.compile(r'invalid symbol: ([^"\\]+)')


def without_invalid(call, symbols: Iterable[str]) -> dict:
    """Ответ `call(symbols)` без бумаг, которые API назвал недопустимыми (`invalid symbol: X`): названную убираем и
    спрашиваем остальных. Любой другой отказ поднимается как есть; пустой список в API не уходит."""
    symbols = list(symbols)
    while symbols:
        try:
            return call(symbols)
        except APIError as e:
            bad = _INVALID.search(str(e))
            if not bad or bad[1].strip() not in symbols:
                raise
            symbols = [s for s in symbols if s != bad[1].strip()]
    return {}


class AlpacaMarket:
    def __init__(self, api_key: str, secret_key: str):
        self._data = bounded(StockHistoricalDataClient(api_key, secret_key))
        self._news = bounded(NewsClient(api_key, secret_key))
        self._trading = bounded(TradingClient(api_key, secret_key, paper=True))  # календарь, часы и список бумаг

    def is_open(self) -> bool:
        return bool(self._trading.get_clock().is_open)

    def now(self) -> datetime:
        return datetime.now(NY)

    def last_prices(self, symbols: Iterable[str]) -> dict[str, float]:
        trades = without_invalid(lambda ss: self._data.get_stock_latest_trade(
            StockLatestTradeRequest(symbol_or_symbols=ss, feed=DataFeed.IEX)), symbols)
        now = self.now()
        return {s: float(t.price) for s, t in trades.items()
                if usable(t.price) and traded_today(t.timestamp, now)}

    def trading_days(self, start: str, end: str) -> list[str]:
        cal = self._trading.get_calendar(
            GetCalendarRequest(start=date.fromisoformat(start), end=date.fromisoformat(end)))
        return [c.date.isoformat() for c in cal]

    def daily_bars(self, symbols: Iterable[str], days: int) -> dict[str, list[Bar]]:
        symbols = list(symbols)
        if not symbols:
            return {}
        start = self.now() - timedelta(days=days * 2 + 10)
        data = without_invalid(lambda ss: self._data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ss, timeframe=TimeFrame(1, TimeFrameUnit.Day), start=start, feed=DataFeed.IEX,
            adjustment=Adjustment.SPLIT)).data, symbols)
        today, market_open = ny_day(self.now()), self.is_open()
        return {s: completed_days([bar_from_sdk(b) for b in bars], today, market_open)[-days:]
                for s, bars in data.items()}

    def lows_since(self, symbols: Iterable[str], since: datetime) -> dict[str, list[tuple[float, float]]]:
        symbols = list(symbols)
        if not symbols:
            return {}
        data = without_invalid(lambda ss: self._data.get_stock_bars(StockBarsRequest(
            symbol_or_symbols=ss, timeframe=TimeFrame(1, TimeFrameUnit.Minute), start=floor_minute(since),
            feed=DataFeed.IEX)).data, symbols)
        cal = self._trading.get_calendar(GetCalendarRequest(start=since.astimezone(NY).date(), end=self.now().date()))
        sessions = [(c.open.replace(tzinfo=NY), c.close.replace(tzinfo=NY)) for c in cal]  # время календаря наивное, нью-йоркское
        out = {s: low_open_pairs(regular_session_bars(bars, sessions)) for s, bars in data.items()}
        return {s: p for s, p in out.items() if p}

    def news(self, symbols: Sequence[str] | None, limit: int) -> list[dict]:
        req = NewsRequest(symbols=",".join(symbols) if symbols else None, limit=limit, sort="desc")
        articles = self._news.get_news(req).data["news"][:limit]
        return [{"ts": a.created_at.isoformat(), "headline": a.headline, "summary": a.summary,
                 "symbols": list(a.symbols)} for a in articles]

    def fractionable(self, symbols: Iterable[str]) -> set[str]:
        wanted = set(symbols)
        assets = self._trading.get_all_assets(GetAssetsRequest(status=AssetStatus.ACTIVE))
        return {a.symbol for a in assets if a.symbol in wanted and a.tradable and a.fractionable}
