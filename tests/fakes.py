from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

from traider.brokers import BrokerRejected
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


class FakeAlpacaApi:
    """Счёт Alpaca в памяти. Исполняет заявки сразу, по ценам `market.prices` (или `quotes`, если они заданы).

    Ручки на одну следующую рыночную заявку (после неё сбрасываются):
      next_fill_fraction  доля, которая исполнится; остаток висит, пока его не отменят
      next_order_mode     "reject" (статус rejected), "never_fills" (висит без исполнения),
                          "error" (отправка поднимает BrokerRejected), "network_down" (ConnectionError, заявки нет),
                          "lost_reply" (заявка исполнена, но ответ потерян: ConnectionError)
    Постоянные ручки: quotes (цены брокера, если нашей ленты нет), fail_next_stop, fail_stop_for,
    reject_sales_of. Как у настоящего брокера, акции под открытым стопом недоступны для продажи.
    """
    OPEN = ("pending", "partially_filled")

    def __init__(self, cash, market):
        self.cash, self.market = cash, market
        self.positions: dict[str, list[float]] = {}          # тикер -> [количество, средняя цена входа]
        self.orders: dict[str, dict] = {}
        self.quotes: dict[str, float] = {}
        self.next_fill_fraction = 1.0
        self.next_order_mode = None
        self.fail_next_stop = False
        self.fail_stop_for = None
        self.reject_sales_of: set[str] = set()
        self.fail_replace = False
        self.fill_stop_on_cancel: set[str] = set()   # стоп этой бумаги исполняется в тот момент, когда его отменяют
        self.lookup_dies_with_network = False        # после потерянной связи поиск по client_order_id тоже падает
        self._lookup_down = False
        self.replaced = 0
        self.submitted = 0

    # --- ручки теста ---
    def pending_orders(self):
        return [o for o in self.orders.values() if o["type"] == "market" and o["status"] in self.OPEN]

    def expire_day_orders(self):
        for o in self.orders.values():
            if o["status"] in self.OPEN:
                o["status"] = "canceled"

    def trigger_stop(self, symbol, price):
        o = self.open_stop_orders()[symbol]
        self._fill(o, o["qty"], price)

    # --- внутреннее ---
    def _price(self, symbol):
        return self.quotes.get(symbol, self.market.prices.get(symbol))

    def _available(self, symbol):
        held = self.positions.get(symbol, [0.0])[0]
        return held - sum(o["qty"] for o in self.orders.values()
                          if o["symbol"] == symbol and o["type"] == "stop" and o["status"] in self.OPEN)

    def _fill(self, o, qty, price):
        sign = 1 if o["side"] == "buy" else -1
        pos = self.positions.setdefault(o["symbol"], [0.0, 0.0])
        if sign > 0:
            pos[1] = (pos[0] * pos[1] + qty * price) / (pos[0] + qty)
        pos[0] += sign * qty
        if pos[0] < 1e-12:
            del self.positions[o["symbol"]]
        self.cash -= sign * qty * price
        done = o["filled_qty"] + qty
        o["filled_avg_price"] = (o["filled_qty"] * (o["filled_avg_price"] or 0.0) + qty * price) / done
        o["filled_qty"] = done
        o["status"] = "filled" if o["type"] == "stop" or done >= o["qty"] - 1e-12 else "partially_filled"
        o["filled_at"] = self.market.now()

    def _new(self, symbol, side, type_, qty, cid, **extra):
        self.submitted += 1
        o = {"id": f"id-{self.submitted}", "client_order_id": cid, "symbol": symbol, "side": side, "type": type_,
             "status": "pending", "qty": qty, "filled_qty": 0.0, "filled_avg_price": None,
             "submitted_at": self.market.now(), "filled_at": None, **extra}
        self.orders[o["id"]] = o
        return o

    def _market(self, symbol, side, qty, notional, cid):
        mode, fraction = self.next_order_mode, self.next_fill_fraction
        self.next_order_mode, self.next_fill_fraction = None, 1.0
        if mode == "network_down":
            self._lookup_down = self.lookup_dies_with_network
            raise ConnectionError("нет связи")
        if mode == "error":
            raise BrokerRejected("отклонено брокером")
        if any(o["client_order_id"] == cid for o in self.orders.values()):
            raise BrokerRejected("client_order_id must be unique")
        price = self._price(symbol)
        if price is None:
            raise BrokerRejected(f"нет цены у брокера: {symbol}")
        qty = qty if qty is not None else notional / price
        if side == "buy" and qty * price > self.cash + 1e-9:
            raise BrokerRejected("недостаточно денег")
        if side == "sell" and (qty > self._available(symbol) + 1e-9 or symbol in self.reject_sales_of):
            raise BrokerRejected("недостаточно акций для продажи")
        o = self._new(symbol, side, "market", qty, cid)
        if mode == "reject":
            o["status"] = "rejected"
        elif mode != "never_fills":
            self._fill(o, qty * fraction, price)
        if mode == "lost_reply":
            raise ConnectionError("ответ потерян")
        return o["id"]

    # --- AlpacaApi ---
    def cash_and_positions(self):
        return self.cash, {s: (q, a) for s, (q, a) in self.positions.items()}

    def submit_market_buy(self, symbol, notional, client_order_id):
        return self._market(symbol, "buy", None, notional, client_order_id)

    def submit_market_sell(self, symbol, qty, client_order_id):
        return self._market(symbol, "sell", qty, None, client_order_id)

    def _check_stop(self, stop_price, cid):
        if abs(round(stop_price, 2 if stop_price >= 1 else 4) - stop_price) > 1e-9:
            raise BrokerRejected("sub-penny")
        if any(o["client_order_id"] == cid for o in self.orders.values()):
            raise BrokerRejected("client_order_id must be unique")

    def submit_stop_sell(self, symbol, qty, stop_price, client_order_id):
        if self.fail_next_stop or self.fail_stop_for == symbol:
            self.fail_next_stop = False
            raise BrokerRejected("стоп отклонён")
        if qty > self._available(symbol) + 1e-9:
            raise BrokerRejected("недостаточно акций под стоп")
        self._check_stop(stop_price, client_order_id)
        return self._new(symbol, "sell", "stop", qty, client_order_id, stop_price=stop_price)["id"]

    def replace_stop(self, order_id, stop_price, client_order_id):
        """Как у Alpaca: старая заявка заменена, появляется новая (другой id) на то же количество."""
        o = self.orders[order_id]
        if self.fail_replace:
            raise BrokerRejected("замена отклонена")
        if o["type"] != "stop" or o["status"] not in self.OPEN:
            raise BrokerRejected("заменить нельзя")
        self._check_stop(stop_price, client_order_id)
        o["status"] = "canceled"
        self.replaced += 1
        return self._new(o["symbol"], "sell", "stop", o["qty"], client_order_id, stop_price=stop_price)["id"]

    def order(self, order_id):
        o = self.orders[order_id]
        return {k: o.get(k) for k in ("id", "status", "filled_qty", "filled_avg_price", "symbol", "side", "type",
                                      "qty", "client_order_id", "stop_price", "filled_at")}

    def order_by_client_id(self, client_order_id):
        if self._lookup_down:
            raise ConnectionError("нет связи")
        o = next((o for o in self.orders.values() if o["client_order_id"] == client_order_id), None)
        return None if o is None else self.order(o["id"])

    def open_stop_orders(self):
        return {o["symbol"]: o for o in self.orders.values()
                if o["type"] == "stop" and o["status"] in self.OPEN}

    def cancel(self, order_id):
        o = self.orders[order_id]
        if o["status"] in self.OPEN and o["type"] == "stop" and o["symbol"] in self.fill_stop_on_cancel:
            self.fill_stop_on_cancel.discard(o["symbol"])
            self._fill(o, o["qty"], o["stop_price"])      # стоп успел сработать раньше, чем дошла отмена
        if o["status"] not in self.OPEN:
            raise BrokerRejected("заявку уже нельзя отменить")
        o["status"] = "canceled"

    def filled_orders_since(self, since):
        # исполненные заявки любого вида, и отменённые с частичным исполнением; висящие ещё идут, их не трогаем
        return [self.order(o["id"]) for o in self.orders.values()
                if o["filled_qty"] > 0 and o["status"] in ("filled", "canceled")
                and (since is None or o["filled_at"] >= since)]

    def order_ids_since(self, since):
        return [o["id"] for o in self.orders.values() if since is None or o["submitted_at"] >= since]


class Block(SimpleNamespace):
    """Блок ответа модели: атрибуты как у блоков SDK, плюс model_dump."""

    def model_dump(self, mode=None, **_):
        return {k: v for k, v in vars(self).items()}


def use(*calls, usage=None, **input):
    """Ответ с вызовами инструментов: use("name", x=1) или use(("a", {...}), ("b", {...}))."""
    if calls and isinstance(calls[0], tuple):
        pairs = list(calls)
    else:
        pairs = [(calls[0], input)]
    blocks = [Block(type="tool_use", id=f"tu_{i}", name=n, input=a) for i, (n, a) in enumerate(pairs)]
    return {"content": blocks, "stop_reason": "tool_use", "usage": usage or {}}


def text(content, stop_reason="end_turn", usage=None):
    blocks = [Block(type="text", text=content)] if content else []
    return {"content": blocks, "stop_reason": stop_reason, "usage": usage or {}}


class _FakeStream:
    def __init__(self, message):
        self.message = message

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get_final_message(self):
        return self.message


class FakeMessages:
    def __init__(self, owner):
        self.owner = owner

    def stream(self, **kw):
        self.owner.requests.append(kw)
        if not self.owner.script:
            raise RuntimeError("сценарий FakeAnthropic кончился")
        step = self.owner.script.pop(0)
        if isinstance(step, BaseException):
            raise step
        u = step.get("usage") or {}
        its = u.get("iterations")
        usage = SimpleNamespace(
            iterations=None if its is None else [SimpleNamespace(**{"type": "message", **i}) for i in its],
            input_tokens=u.get("input_tokens", 0), output_tokens=u.get("output_tokens", 0),
            cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
            cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
            server_tool_use=SimpleNamespace(web_search_requests=u.get("web_search_requests", 0)))
        return _FakeStream(SimpleNamespace(content=step["content"], stop_reason=step["stop_reason"],
                                           usage=usage, model=step.get("model", kw["model"])))


class FakeAnthropic:
    """Как клиент SDK в цикле агента: client.beta.messages.stream(**kw) как менеджер контекста,
    у потока get_final_message(). Запросы копятся в .requests; исключение в сценарии это сбой API."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []
        self.beta = SimpleNamespace(messages=FakeMessages(self))
