from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

NY = ZoneInfo("America/New_York")


def ny(year: int, month: int, day: int, hour: int = 10, minute: int = 0) -> datetime:
    return datetime(year, month, day, hour, minute, tzinfo=NY)


class FakeMarket:
    def __init__(self, prices=None, bars=None, lows=None, news_items=None, open=True, clock=None):
        self.prices = dict(prices or {})
        self.bars = dict(bars or {})
        self.lows = dict(lows or {})
        self.news_items = list(news_items or [])
        self.open = open
        self.clock = clock or ny(2026, 12, 1)

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
        return {s: self.bars[s][-days:] for s in symbols if s in self.bars}

    def lows_since(self, symbols, since):
        # запись lows: (минимум, открытие) или (минимум, открытие, время); без времени считается всегда
        out = {}
        for s in symbols:
            e = self.lows.get(s)
            if e is not None and (len(e) < 3 or e[2] >= since):
                out[s] = (e[0], e[1])
        return out

    def news(self, symbols, limit):
        items = [n for n in self.news_items if symbols is None or set(n["symbols"]) & set(symbols)]
        return items[:limit]

    def fractionable(self, symbols):
        return set(symbols)
