"""Счёт Alpaca: `AlpacaBroker` и тонкая обёртка `AlpacaSdkApi` над alpaca-py.

:wk-id: alpaca-module
:wk-tags: accounts, broker, alpaca, stops
:wk-categories: system-architecture

Как это работает. `AlpacaBroker` реализует `Broker`: деньги и позиции берутся у Alpaca, журнал заявок остаётся нашим,
желаемые стопы (`state["stops"]`) и последние цены входа (`state["entries"]`) лежат в состоянии
прогона. Все вызовы alpaca-py только в `AlpacaSdkApi`, брокер говорит с протоколом `AlpacaApi`.
Покупка это рыночная заявка на сумму со сроком DAY; стоп это отдельная DAY-заявка на продажу,
которую `ensure_stops` возвращает на каждом тике. Повтор покупки или закрытия с тем же
`client_order_id` находит заявку у брокера и доводит её, а не отправляет вторую. Продажа не ждёт
нашей котировки, покупка без неё даёт `NoPrice`.
"""
import logging
import time
from collections.abc import Iterable, Mapping
from datetime import datetime, timedelta
from math import floor
from typing import Protocol

from alpaca.common.enums import Sort
from alpaca.common.exceptions import APIError
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, MarketOrderRequest, StopOrderRequest

from traider.brokers import BrokerRejected, NoPrice
from traider.cage import Account, Position
from traider.config import RunConfig
from traider.market import Market
from traider.nums import usable
from traider.sim import Fill, mark_peak
from traider.store import RunStore

log = logging.getLogger(__name__)

TERMINAL = ("filled", "rejected", "canceled")   # статусы `order()`, после которых заявка не меняется
_STATUS = {"filled": "filled", "partially_filled": "partially_filled", "rejected": "rejected",
           "canceled": "canceled", "expired": "canceled", "replaced": "canceled"}   # прочее: pending


def _tick(price: float) -> float:
    """Минимальный шаг цены у Alpaca: от $1 два знака, ниже $1 четыре. Лишние знаки брокер отклоняет."""
    return round(price, 2 if price >= 1 else 4)


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-7 * max(1.0, abs(b))


def _floor9(qty: float) -> float:
    """Количество с 9 знаками (максимум Alpaca), округлённое вниз: продать больше, чем есть, нельзя."""
    return floor(qty * 1e9 + 1e-6) / 1e9


def _enum(x):
    return getattr(x, "value", x)


def _num(x) -> float | None:
    return None if x is None else float(x)


def _order_dict(o) -> dict:
    return {"id": str(o.id), "status": _STATUS.get(_enum(o.status), "pending"),
            "filled_qty": _num(o.filled_qty) or 0.0, "filled_avg_price": _num(o.filled_avg_price),
            "symbol": o.symbol, "side": _enum(o.side), "type": _enum(o.type), "qty": _num(o.qty),
            "client_order_id": o.client_order_id, "stop_price": _num(o.stop_price), "filled_at": o.filled_at}


class AlpacaApi(Protocol):
    """Единственное место, где вызывается alpaca-py (реализация: `AlpacaSdkApi`)."""
    def cash_and_positions(self) -> tuple[float, dict[str, tuple[float, float]]]: ...   # деньги; тикер -> (количество, средняя цена входа)
    def submit_market_buy(self, symbol: str, notional: float, client_order_id: str) -> str: ...
    def submit_market_sell(self, symbol: str, qty: float, client_order_id: str) -> str: ...
    def submit_stop_sell(self, symbol: str, qty: float, stop_price: float) -> str: ...
    def order(self, order_id: str) -> dict: ...   # id, status, filled_qty, filled_avg_price, symbol, side, type, qty, client_order_id, stop_price, filled_at
    def order_by_client_id(self, client_order_id: str) -> dict | None: ...
    def open_stop_orders(self) -> dict[str, dict]: ...   # тикер -> {"id", "qty", "stop_price"}
    def cancel(self, order_id: str) -> None: ...
    def filled_stop_sells_since(self, since: datetime | None) -> list[dict]: ...   # как `order()`; отбор по времени исполнения
    def order_ids_since(self, since: datetime | None) -> list[str]: ...   # все заявки счёта, отбор по времени подачи


class AlpacaSdkApi:
    def __init__(self, api_key: str, secret_key: str, paper: bool):
        self._trading = TradingClient(api_key, secret_key, paper=paper)

    def cash_and_positions(self):
        cash = float(self._trading.get_account().cash)
        held = {}
        for p in self._trading.get_all_positions():
            if _enum(p.side) != "long":
                raise RuntimeError(f"позиция не длинная: {p.symbol} {_enum(p.side)}")
            held[p.symbol] = (float(p.qty), float(p.avg_entry_price))
        return cash, held

    def _submit(self, request) -> str:
        try:
            return str(self._trading.submit_order(request).id)
        except APIError as e:   # 4xx: брокер заявку не принял; прочее (сеть, 5xx, 429) исход неизвестен, пусть летит как есть
            if e.status_code is not None and 400 <= e.status_code < 500 and e.status_code != 429:
                raise BrokerRejected(str(e)) from e
            raise

    def submit_market_buy(self, symbol, notional, client_order_id):
        return self._submit(MarketOrderRequest(symbol=symbol, notional=round(notional, 2), side=OrderSide.BUY,
                                               time_in_force=TimeInForce.DAY, client_order_id=client_order_id))

    def submit_market_sell(self, symbol, qty, client_order_id):
        return self._submit(MarketOrderRequest(symbol=symbol, qty=_floor9(qty), side=OrderSide.SELL,
                                               time_in_force=TimeInForce.DAY, client_order_id=client_order_id))

    def submit_stop_sell(self, symbol, qty, stop_price):
        return self._submit(StopOrderRequest(symbol=symbol, qty=_floor9(qty), side=OrderSide.SELL,
                                             time_in_force=TimeInForce.DAY, stop_price=_tick(stop_price)))

    def order(self, order_id):
        return _order_dict(self._trading.get_order_by_id(order_id))

    def order_by_client_id(self, client_order_id):
        try:
            return _order_dict(self._trading.get_order_by_client_id(client_order_id))
        except APIError as e:
            if e.status_code == 404:
                return None
            raise

    def _orders(self, status, after, side=None) -> list:
        """Все заявки с пагинацией: по 500, по времени подачи, следующая страница после последней."""
        out = []
        while True:
            page = self._trading.get_orders(GetOrdersRequest(status=status, limit=500, after=after,
                                                             direction=Sort.ASC, side=side))
            out += page
            if len(page) < 500:
                return out
            after = page[-1].submitted_at

    def open_stop_orders(self):
        out: dict[str, dict] = {}
        for o in self._orders(QueryOrderStatus.OPEN, None, OrderSide.SELL):
            if _enum(o.type) != "stop":
                continue
            if o.symbol in out:   # два стопа резервируют акции дважды; выбирать молча нельзя
                raise RuntimeError(f"два открытых стопа по {o.symbol}: отмените лишний вручную")
            out[o.symbol] = {"id": str(o.id), "qty": _num(o.qty), "stop_price": _num(o.stop_price)}
        return out

    def cancel(self, order_id):
        self._trading.cancel_order_by_id(order_id)

    def filled_stop_sells_since(self, since):
        # `after` у Alpaca это время подачи, а DAY-стоп мог быть подан до выходных: берём с запасом, отбираем по исполнению
        after = None if since is None else since - timedelta(days=4)
        return [_order_dict(o) for o in self._orders(QueryOrderStatus.CLOSED, after, OrderSide.SELL)
                if _enum(o.type) == "stop" and _enum(o.status) == "filled"
                and (since is None or o.filled_at >= since)]

    def order_ids_since(self, since):
        return [str(o.id) for o in self._orders(QueryOrderStatus.ALL, since)]


def make_alpaca_api(cfg: RunConfig, env: Mapping[str, str]) -> AlpacaSdkApi:
    live = cfg.mode == "live"   # живой режим только по конфигу прогона и только со своими ключами
    names = ("ALPACA_LIVE_API_KEY", "ALPACA_LIVE_SECRET_KEY") if live else ("ALPACA_API_KEY", "ALPACA_SECRET_KEY")
    for name in names:
        if not env.get(name):
            raise ValueError(f"нет переменной {name}")
    return AlpacaSdkApi(env[names[0]], env[names[1]], not live)


def _reraise(errors: list[Exception], **attrs):
    first = errors[0]
    for other in errors[1:]:
        first.add_note(f"ещё ошибка: {other!r}")
    for k, v in attrs.items():
        setattr(first, k, v)
    raise first


class AlpacaBroker:
    def __init__(self, cfg: RunConfig, store: RunStore, market: Market, api: AlpacaApi,
                 poll_seconds: float = 30.0, poll_interval: float = 1.0):
        self.cfg, self.store, self.market, self.api = cfg, store, market, api
        self.poll_seconds, self.poll_interval = poll_seconds, poll_interval

    # --- чтение ---
    def _held(self) -> tuple[float, dict[str, tuple[float, float]]]:
        """Деньги и позиции у брокера; цену входа помним в состоянии: после продажи брокер её уже не отдаёт."""
        cash, held = self.api.cash_and_positions()
        known = self.store.state()["entries"]
        entries = {**known, **{s: avg for s, (_, avg) in held.items()}}
        if entries != known:
            self.store.update_state(entries=entries)
        return cash, held

    def _price(self, symbol: str) -> float:
        price = self.market.last_prices([symbol]).get(symbol)
        if not usable(price):
            raise NoPrice(symbol)
        return price

    def _log(self, day: str, kind: str, symbol: str, qty, price, **extra) -> dict:
        return self.store.append_order({
            "day": day, "kind": kind, "symbol": symbol, "notional": None, "qty": qty, "price": price,
            "stop_price": None, "thesis": None, "reason": None, "client_order_id": None,
            "broker_order_id": None, **extra}, ts=self.market.now())

    def _repeat(self, client_order_id: str, symbol: str) -> Fill | None:
        e = self.store.find_order(client_order_id)
        if e is None:
            return None
        if e["symbol"] != symbol:
            raise BrokerRejected(f"заявка {client_order_id} уже была по {e['symbol']}, не по {symbol}")
        return Fill(e["symbol"], e["qty"], e["price"], e["kind"])

    def account(self) -> Account:
        cash, held = self._held()
        stops = self.store.state()["stops"]   # бумага без желаемого стопа (куплена не нами) идёт со стопом 0: риск полный
        positions = tuple(Position(s, q, avg, stops.get(s, 0.0)) for s, (q, avg) in sorted(held.items()))
        saved = self.store.account()
        acct = Account(cash, positions, max(saved.peak_equity if saved else 0.0, self.cfg.start_equity))
        prices = self.market.last_prices(held)
        if all(usable(prices.get(s)) for s in held):
            acct = mark_peak(acct, prices)
        if acct != saved:
            self.store.save_account(acct)
        return acct

    def prices(self, symbols: Iterable[str]) -> dict[str, float]:
        return self.market.last_prices(symbols)

    # --- заявки: найти, отправить, дождаться ---
    def _find(self, client_order_id: str, symbol: str, side: str) -> str | None:
        """Заявка с этим id уже у брокера (процесс умер после отправки): не отправляем вторую, продолжаем её."""
        o = self.api.order_by_client_id(client_order_id)
        if o is None:
            return None
        if o["symbol"] != symbol or o["side"] != side:
            raise BrokerRejected(f"заявка {client_order_id} у брокера уже есть: {o['side']} {o['symbol']}")
        return o["id"]

    def _submit(self, client_order_id: str, send) -> str:
        try:
            return send()
        except Exception:
            o = self.api.order_by_client_id(client_order_id)   # ответ мог потеряться, а заявка дойти
            if o is None:
                raise
            return o["id"]

    def _wait(self, order_id: str) -> dict:
        deadline = time.monotonic() + self.poll_seconds
        while True:
            o = self.api.order(order_id)
            if o["status"] in TERMINAL or time.monotonic() >= deadline:
                return o
            time.sleep(self.poll_interval)

    def _cancel(self, order_id: str) -> dict:
        try:
            self.api.cancel(order_id)
        except Exception:
            if self.api.order(order_id)["status"] not in TERMINAL:   # уже исполнилась или отменилась: не беда
                raise
        o = self._wait(order_id)   # отмена у брокера не мгновенна, а акции под стопом заняты, пока он не снят
        if o["status"] not in TERMINAL:
            raise BrokerRejected(f"заявка {order_id} не отменилась за {self.poll_seconds} с")
        return o

    def _finish(self, order_id: str) -> dict:
        """Ждёт исполнения, отменяет остаток, возвращает заявку с исполнением больше нуля или отказывает."""
        o = self._wait(order_id)
        if o["status"] not in TERMINAL:
            o = self._cancel(order_id)
        if not (usable(o["filled_qty"]) and usable(o["filled_avg_price"])):
            raise BrokerRejected(f"заявка по {o['symbol']} ничего не исполнила: {o['status']}")
        return o

    # --- стопы ---
    def _sync_stop(self, symbol: str, held_qty: float, open_stops: dict | None = None) -> bool:
        """Приводит стоп у брокера к желаемому в состоянии. True, если поставил новый."""
        want = self.store.state()["stops"].get(symbol)
        cur = (self.api.open_stop_orders() if open_stops is None else open_stops).get(symbol)
        need = want is not None and held_qty > 0
        if cur is not None and need and _same(cur["qty"], held_qty) and _same(cur["stop_price"], want):
            return False
        if cur is not None:
            self._cancel(cur["id"])
        if not need:
            return False
        self.api.submit_stop_sell(symbol, held_qty, want)
        return True

    def _restore_stop(self, symbol: str, at_least: float = 0.0) -> None:
        _, held = self._held()
        qty = held[symbol][0] if symbol in held else at_least
        stops = self.store.state()["stops"]
        if qty <= 0 and symbol in stops:
            self.store.update_state(stops={k: v for k, v in stops.items() if k != symbol})
        self._sync_stop(symbol, qty)

    def _restore_quietly(self, symbol: str, at_least: float = 0.0) -> None:
        """Сделка уже записана; не вышло поставить стоп, его восстановит ensure_stops на ближайшем тике."""
        try:
            self._restore_stop(symbol, at_least)
        except Exception:
            log.exception("стоп по %s не восстановлен, это сделает ensure_stops", symbol)

    def _restore_after_failure(self, symbol: str, error: BaseException) -> None:
        try:
            self._restore_stop(symbol)
        except Exception as e:
            error.add_note(f"стоп по {symbol} не восстановлен: {e!r}")

    def _record_sale(self, day: str, kind: str, symbol: str, o: dict, entry: float | None, fraction: float,
                     **extra) -> Fill:
        self._log(day, kind, symbol, o["filled_qty"], o["filled_avg_price"], fraction=fraction, entry_price=entry,
                  broker_order_id=o["id"], **extra)
        return Fill(symbol, o["filled_qty"], o["filled_avg_price"], kind)

    # --- Broker ---
    def buy(self, day, symbol, notional, stop_price, thesis, client_order_id) -> Fill:
        if (done := self._repeat(client_order_id, symbol)) is not None:
            return done
        oid = self._find(client_order_id, symbol, "buy")
        if oid is None:
            for name, x in (("сумма", notional), ("стоп", stop_price)):
                if not usable(x):
                    raise ValueError(f"{name} должно быть конечным положительным числом: {x}")
            self._price(symbol)   # без нашей котировки не покупаем; цену исполнения даёт брокер
            if notional > self.api.cash_and_positions()[0] + 1e-9:
                raise ValueError(f"сумма {notional} больше свободных денег")
            oid = self._submit(client_order_id, lambda: self.api.submit_market_buy(symbol, notional, client_order_id))
        o = self._finish(oid)
        stop = _tick(stop_price)
        self.store.update_state(stops={**self.store.state()["stops"], symbol: stop})   # желаемый стоп раньше самого стопа
        self._restore_quietly(symbol, at_least=o["filled_qty"])
        self._log(day, "buy", symbol, o["filled_qty"], o["filled_avg_price"], notional=o["filled_qty"] * o["filled_avg_price"],
                  stop_price=stop, thesis=thesis, client_order_id=client_order_id, broker_order_id=oid)
        self.account()
        return Fill(symbol, o["filled_qty"], o["filled_avg_price"], "buy")

    def close(self, day, symbol, fraction, reason, client_order_id) -> Fill:
        if (done := self._repeat(client_order_id, symbol)) is not None:
            return done
        oid = self._find(client_order_id, symbol, "sell")
        _, held = self._held()
        entry = held[symbol][1] if symbol in held else self.store.state()["entries"].get(symbol)
        if oid is None:   # при повторе после сбоя позиции может уже не быть: тогда заявка у брокера говорит за себя
            if symbol not in held:
                raise BrokerRejected(f"нет позиции по {symbol}")
            if not (usable(fraction) and fraction <= 1):
                raise BrokerRejected(f"доля должна быть в (0, 1]: {fraction}")
            qty = held[symbol][0]
            sell = qty if fraction >= 1 else min(qty, round(qty * fraction, 9))
        try:
            if oid is None:
                if (stop := self.api.open_stop_orders().get(symbol)) is not None:
                    self._cancel(stop["id"])   # акции под стопом для продажи недоступны
                oid = self._submit(client_order_id,
                                   lambda: self.api.submit_market_sell(symbol, sell, client_order_id))
            o = self._finish(oid)
        except BaseException as e:
            self._restore_after_failure(symbol, e)   # стоп снят, а продажи не вышло: вернуть до того, как ошибка уйдёт
            raise
        done_fraction = fraction if o["status"] == "filled" or not usable(o["qty"]) else fraction * o["filled_qty"] / o["qty"]
        fill = self._record_sale(day, "close", symbol, o, entry, done_fraction, reason=reason,
                                 client_order_id=client_order_id)
        self._restore_quietly(symbol)
        self.account()
        return fill

    def raise_stop(self, day: str, symbol: str, stop_price: float) -> None:
        _, held = self._held()
        if symbol not in held:
            raise BrokerRejected(f"нет позиции по {symbol}")
        if not usable(stop_price):
            raise BrokerRejected(f"стоп должен быть конечным положительным числом: {stop_price}")
        stop = _tick(stop_price)
        quote = self.market.last_prices([symbol]).get(symbol)
        self.store.update_state(stops={**self.store.state()["stops"], symbol: stop})
        self._log(day, "raise_stop", symbol, None, quote if usable(quote) else None, stop_price=stop)
        self._restore_stop(symbol)   # не вышло: желаемый стоп записан, ensure_stops дожмёт

    def ensure_stops(self) -> list[str]:
        _, held = self._held()
        desired, open_stops = self.store.state()["stops"], self.api.open_stop_orders()
        placed, errors = [], []
        for symbol, (qty, _) in held.items():
            if symbol not in desired:
                continue
            try:
                if self._sync_stop(symbol, qty, open_stops):
                    placed.append(symbol)
            except Exception as e:   # одна бумага не должна оставлять без стопа остальные
                errors.append(e)
        if errors:
            _reraise(errors, placed=placed)
        return placed

    def sync(self, day: str) -> list[dict]:
        last = self.store.state()["last_sync"]
        since = datetime.fromisoformat(last) - timedelta(days=1) if last else None   # запас на расхождение часов
        known = {e.get("broker_order_id") for e in self.store.orders()}
        _, held = self._held()
        events = []
        for o in sorted(self.api.filled_stop_sells_since(since), key=lambda o: o["filled_at"]):
            symbol = o["symbol"]
            if o["id"] not in known:   # повторный вызов и повторное окно не пишут второй раз
                events.append(self._log(day, "stop_fill", symbol, o["filled_qty"], o["filled_avg_price"],
                                        stop_price=o["stop_price"], reason="stop", broker_order_id=o["id"],
                                        entry_price=self.store.state()["entries"].get(symbol)))
                known.add(o["id"])
            stops = self.store.state()["stops"]
            if symbol not in held and symbol in stops:   # докупленная бумага стопа не теряет
                self.store.update_state(stops={k: v for k, v in stops.items() if k != symbol})
        self.store.update_state(last_sync=self.market.now().isoformat())
        return events

    def _liquidation_id(self, day: str, symbol: str) -> str:
        """Один id на (день, бумагу), чтобы повтор после сбоя нашёл свою заявку; отказанная заявка id сжигает."""
        used = {e.get("broker_order_id") for e in self.store.orders()}
        n = 1
        while True:
            cid = f"liquidate/{day}/{symbol}" + (f"/{n}" if n > 1 else "")
            o = self.api.order_by_client_id(cid)
            dead = o is not None and o["status"] in ("rejected", "canceled") and not o["filled_qty"]
            if o is None or (o["id"] not in used and not dead):
                return cid
            n += 1

    def liquidate(self, day: str) -> list[Fill]:
        _, held = self._held()
        stops = self.api.open_stop_orders()
        fills: list[Fill] = []
        errors: list[Exception] = []
        for symbol, (qty, entry) in held.items():
            try:
                if symbol in stops:
                    self._cancel(stops[symbol]["id"])
                cid = self._liquidation_id(day, symbol)
                o = self._finish(self._submit(cid, lambda: self.api.submit_market_sell(symbol, qty, cid)))
                whole = o["filled_qty"] >= qty * (1 - 1e-9)
                fills.append(self._record_sale(day, "liquidate", symbol, o, entry,
                                               1.0 if whole else o["filled_qty"] / qty, reason="liquidate"))
                self._restore_quietly(symbol)   # продана не вся бумага: стоп на остаток
                if not whole:
                    raise BrokerRejected(f"{symbol}: продано {o['filled_qty']} из {qty}")
            except Exception as e:   # одна бумага не останавливает остальные
                errors.append(e)
                self._restore_after_failure(symbol, e)
        if errors:
            _reraise(errors, fills=fills)
        self.account()
        return fills
