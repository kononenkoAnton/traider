from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from traider.market import Bar, completed_days

NY = ZoneInfo("America/New_York")


def ny(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


class FakeMarket:
    def __init__(self, prices=None, bars=None, lows=None, news_items=None, open=True, clock=None,
                 fractionable_symbols=None):
        self.prices = dict(prices or {})
        self.bars = dict(bars or {})
        self.lows = dict(lows or {})
        self.news_items = list(news_items or [])
        self.open = open
        self.clock = clock or ny(2026, 12, 1)
        self.fractionable_symbols = fractionable_symbols

    @classmethod
    def with_history(cls, symbols, days, clock=None):
        """Детерминированные дневные свечи по будням, последняя в день часов; цены = последние закрытия."""
        m = cls(clock=clock, fractionable_symbols=symbols)
        d, stamps = m.clock.date(), []
        while len(stamps) < days:
            if d.weekday() < 5:
                stamps.append(d.isoformat())
            d -= timedelta(days=1)
        for s in symbols:
            base = 50 + sum(map(ord, s)) % 100
            m.bars[s] = [Bar(day, p, p + 2, p - 2, p + 1, 1000.0 + i)
                         for i, (day, p) in enumerate((day, base + i) for i, day in enumerate(reversed(stamps)))]
            m.prices[s] = m.bars[s][-1].close
        return m

    def is_open(self):
        return self.open

    def now(self):
        return self.clock

    def last_prices(self, symbols):
        return {s: self.prices[s] for s in symbols if s in self.prices}

    def trading_days(self, start, end):
        d, last, out = date.fromisoformat(start), date.fromisoformat(end), []
        while d <= last:
            if d.weekday() < 5:
                out.append(d.isoformat())
            d += timedelta(days=1)
        return out

    def daily_bars(self, symbols, days):
        today = self.clock.date().isoformat()
        return {s: completed_days(self.bars[s], today, self.open)[-days:] for s in symbols if s in self.bars}

    def lows_since(self, symbols, since):
        # запись lows: одна свеча или список свечей по времени; свеча это (минимум, открытие)
        # или (минимум, открытие, время), без времени она считается всегда
        out = {}
        for s in symbols:
            e = self.lows.get(s)
            bars = [(b[0], b[1]) for b in (e if isinstance(e, list) else [e] if e else [])
                    if len(b) < 3 or b[2] >= since]
            if bars:
                out[s] = bars
        return out

    def news(self, symbols, limit):
        items = [n for n in self.news_items if symbols is None or set(n["symbols"]) & set(symbols)]
        return items[:limit]

    def fractionable(self, symbols):
        return set(symbols) if self.fractionable_symbols is None else set(symbols) & set(self.fractionable_symbols)
