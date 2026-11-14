"""Протокол брокера и встроенный счёт `SimBroker`.

:wk-id: brokers-module
:wk-tags: accounts, broker, ledger
:wk-categories: system-architecture

Как это работает. Каждый метод, меняющий счёт, сначала пишет событие в журнал заявок,
потом сохраняет счёт. Журнал это истина: перед каждой операцией счёт заново
собирается проигрыванием всего журнала с начального капитала по записанным
количествам и ценам (проскальзывание заново не считается), от сохранённого счёта берётся
только пик капитала, если он выше. Поэтому сбой между записью и сохранением ничего не
портит, а повтор заявки с тем же `client_order_id` не торгует снова, а возвращает
`Fill` из записанного события; повтор id по другой бумаге даёт `BrokerRejected`. Сверка
считает свечи бумаги только с момента, когда существуют позиция и её нынешний стоп, и
берёт первую свечу, чей минимум задел стоп: исполнение по `min(стоп, открытие этой свечи)`.

Отказы до любой записи: нет годной котировки (её нет или она не конечное положительное
число) в `buy` и `close` даёт `NoPrice`; нет позиции, доля вне (0, 1] или негодный стоп в
`close` и `raise_stop` дают `BrokerRejected`. Ошибки `ValueError` из `traider.sim`
пробрасываются. События продажи (`close`, `stop_fill`, `liquidate`) несут `entry_price`
позиции, `close` и `liquidate` ещё `fraction`, `raise_stop` котировку в `price` (None, если
годной нет). Ликвидация бумаги без годной котировки идёт по цене входа и пишет причину
`no_quote_entry_price`. `account` поднимает пик и у пустого счёта, но не поднимает его, пока у
какой-то удерживаемой бумаги нет годной котировки, и сохраняет счёт, если он отличается
от сохранённого.
"""
from collections.abc import Iterable
from dataclasses import replace
from datetime import datetime
from typing import Protocol

from traider.cage import Account, Position
from traider.config import RunConfig
from traider.market import Market
from traider.nums import usable
from traider.sim import Fill, apply_buy, apply_close, apply_stops, mark_peak
from traider.store import RunStore


class BrokerRejected(Exception): ...
class NoPrice(Exception): ...


class Broker(Protocol):
    def account(self) -> Account: ...
    def prices(self, symbols: Iterable[str]) -> dict[str, float]: ...
    def buy(self, day: str, symbol: str, notional: float, stop_price: float, thesis: str,
            client_order_id: str) -> Fill: ...
    def close(self, day: str, symbol: str, fraction: float, reason: str, client_order_id: str) -> Fill: ...
    def raise_stop(self, day: str, symbol: str, stop_price: float) -> None: ...
    def ensure_stops(self) -> list[str]: ...
    def sync(self, day: str) -> list[dict]: ...
    def liquidate(self, day: str) -> list[Fill]: ...


def _replay(events: list[dict], start: Account) -> Account:
    cash, pos = start.cash, {p.symbol: p for p in start.positions}
    for e in events:
        kind, sym = e.get("kind"), e.get("symbol")
        if kind == "buy":
            old, qty = pos.get(sym), e["qty"]
            total = qty if old is None else old.qty + qty
            entry = e["price"] if old is None else (old.qty * old.entry_price + e["notional"]) / total
            pos[sym] = Position(sym, total, entry, e["stop_price"])
            cash -= e["notional"]
        elif kind in ("close", "stop_fill", "liquidate") and sym in pos:
            cash += e["qty"] * e["price"]
            kept = pos[sym].qty - e["qty"]
            if kept > 0:
                pos[sym] = replace(pos[sym], qty=kept)
            else:
                del pos[sym]
        elif kind == "raise_stop" and sym in pos:
            pos[sym] = replace(pos[sym], stop_price=e["stop_price"])
    return Account(cash, tuple(pos.values()), start.peak_equity)


class SimBroker:
    def __init__(self, cfg: RunConfig, store: RunStore, market: Market):
        self.cfg, self.store, self.market = cfg, store, market

    def _load(self) -> Account:
        saved = self.store.account()
        peak = saved.peak_equity if saved else self.cfg.start_equity
        start = Account(self.cfg.start_equity, (), max(peak, self.cfg.start_equity))
        return _replay(self.store.orders(), start)

    def _log(self, day: str, kind: str, symbol: str, fill: Fill, **extra) -> dict:
        return self.store.append_order({
            "day": day, "kind": kind, "symbol": symbol, "notional": None, "qty": fill.qty,
            "price": fill.price, "stop_price": None, "thesis": None, "reason": None,
            "client_order_id": None, "broker_order_id": None, **extra}, ts=self.market.now())

    def _price(self, symbol: str) -> float:
        price = self.market.last_prices([symbol]).get(symbol)
        if not usable(price):   # нет котировки и негодная котировка это один случай
            raise NoPrice(symbol)
        return price

    def _repeat(self, client_order_id: str, symbol: str) -> Fill | None:
        e = self.store.find_order(client_order_id)
        if e is None:
            return None
        if e["symbol"] != symbol:
            raise BrokerRejected(f"заявка {client_order_id} уже была по {e['symbol']}, не по {symbol}")
        self.store.save_account(self._load())
        return Fill(e["symbol"], e["qty"], e["price"], e["kind"])

    def account(self) -> Account:
        acct = self._load()
        prices = self.market.last_prices([p.symbol for p in acct.positions])
        # пик поднимается только по полным котировкам (у пустого счёта по деньгам): оценка по цене входа
        # могла бы навсегда поднять пол над позицией, которая на деле упала
        priced = all(usable(prices.get(p.symbol)) for p in acct.positions)
        marked = mark_peak(acct, prices) if priced else acct
        if marked != self.store.account():
            self.store.save_account(marked)
        return marked

    def prices(self, symbols: Iterable[str]) -> dict[str, float]:
        return self.market.last_prices(symbols)

    def buy(self, day, symbol, notional, stop_price, thesis, client_order_id) -> Fill:
        if (done := self._repeat(client_order_id, symbol)) is not None:
            return done
        acct = self._load()
        new, fill = apply_buy(acct, symbol, notional, self._price(symbol), stop_price)
        self._log(day, "buy", symbol, fill, notional=notional, stop_price=stop_price, thesis=thesis,
                  client_order_id=client_order_id)
        self.store.save_account(new)
        return fill

    def close(self, day, symbol, fraction, reason, client_order_id) -> Fill:
        if (done := self._repeat(client_order_id, symbol)) is not None:
            return done
        acct = self._load()
        pos = next((p for p in acct.positions if p.symbol == symbol), None)
        if pos is None:
            raise BrokerRejected(f"нет позиции по {symbol}")
        if not (usable(fraction) and fraction <= 1):
            raise BrokerRejected(f"доля должна быть в (0, 1]: {fraction}")
        new, fill = apply_close(acct, symbol, fraction, self._price(symbol))
        self._log(day, "close", symbol, fill, reason=reason, client_order_id=client_order_id,
                  fraction=fraction, entry_price=pos.entry_price)
        self.store.save_account(new)
        return fill

    def raise_stop(self, day: str, symbol: str, stop_price: float) -> None:
        acct = self._load()
        if not any(p.symbol == symbol for p in acct.positions):
            raise BrokerRejected(f"нет позиции по {symbol}")
        if not usable(stop_price):
            raise BrokerRejected(f"стоп должен быть конечным положительным числом: {stop_price}")
        quote = self.market.last_prices([symbol]).get(symbol)
        self.store.append_order({
            "day": day, "kind": "raise_stop", "symbol": symbol, "notional": None, "qty": None,
            "price": quote if usable(quote) else None, "stop_price": stop_price, "thesis": None, "reason": None,
            "client_order_id": None, "broker_order_id": None}, ts=self.market.now())
        self.store.save_account(self._load())

    def ensure_stops(self) -> list[str]:
        return []   # встроенный счёт проверяет стопы сам в sync

    def sync(self, day: str) -> list[dict]:
        acct = self._load()
        events: list[dict] = []
        if acct.positions:
            last = self.store.state()["last_sync"]
            since: dict[str, datetime] = {}
            for e in self.store.orders():   # окно бумаги: не раньше покупки и не раньше нынешнего стопа
                if e["kind"] in ("buy", "raise_stop"):
                    since[e["symbol"]] = datetime.fromisoformat(e["ts"])
            groups: dict[datetime, list[str]] = {}
            for p in acct.positions:
                start = since[p.symbol]
                if last:
                    start = max(start, datetime.fromisoformat(last))
                groups.setdefault(start, []).append(p.symbol)
            bars: dict[str, list[tuple[float, float]]] = {}
            for start, syms in groups.items():
                bars.update(self.market.lows_since(syms, start))
            hit: dict[str, tuple[float, float]] = {}   # первая свеча, задевшая стоп: её открытие ловит гэп
            for p in acct.positions:
                first = next(((lo, op) for lo, op in bars.get(p.symbol, ())
                              if usable(lo) and lo <= p.stop_price), None)
                if first is not None:
                    hit[p.symbol] = first
            new, fills = apply_stops(acct, {s: v[0] for s, v in hit.items()},
                                     {s: v[1] for s, v in hit.items()})
            held = {p.symbol: p for p in acct.positions}
            events = [self._log(day, "stop_fill", f.symbol, f, stop_price=held[f.symbol].stop_price,
                                entry_price=held[f.symbol].entry_price, reason="stop")
                      for f in fills]
            self.store.save_account(new)
        self.store.update_state(last_sync=self.market.now().isoformat())
        return events

    def liquidate(self, day: str) -> list[Fill]:
        acct, fills = self._load(), []
        for p in acct.positions:
            # нет цены или она негодная: продаём по цене входа, чтобы ликвидация не оборвалась на полпути
            price = self.market.last_prices([p.symbol]).get(p.symbol)
            reason = "liquidate" if usable(price) else "no_quote_entry_price"
            if not usable(price):
                price = p.entry_price
            acct, fill = apply_close(acct, p.symbol, 1.0, price, "liquidate")
            self._log(day, "liquidate", p.symbol, fill, reason=reason, fraction=1.0,
                      entry_price=p.entry_price)
            self.store.save_account(acct)
            fills.append(fill)
        return fills
