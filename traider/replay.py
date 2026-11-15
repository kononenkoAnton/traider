"""Повтор сделок агента: тень, случайные боты и SPY как чистые функции.

:wk-id: replay-module
:wk-tags: replay, monkeys, baselines
:wk-categories: experiment

Как это работает. `replay` проходит дни по порядку: сначала стопы по дневным свечам
(через `apply_stops`, позиция, купленная в этот день, ещё не проверяется), затем
события дня в записанном порядке (`seq`, если он есть) по ценам снимка, затем капитал
по цене закрытия дня. Участвуют только `buy`, `close`, `raise_stop` и `liquidate`.
Покупка идёт на меньшую из суммы агента и свободных денег, меньше $1 пропускается;
стоп стоит на той же доле от цены, что у агента, и не опускается при подъёме.
Каждая позиция агента это отдельный лот в бумаге-замене: счёт повтора держит лоты под
символом агента, цены берутся по замене. Пока лот жив, покупка докупает ту же бумагу, иначе
бумага выбирается заново (`pick`, без него это бумага агента); бот выбирает равновероятно
среди бумаг вселенной с ценой в снимке дня. Лот, выбитый или закрытый, забывается: его
закрытие и подъём стопа пропускаются. Стопы проверяются по каждой свече между днями повтора;
событие вне дней повтора это ValueError. Равные значения в `percentile` считаются за половину. Нет цены в снимке: покупка пропускается, закрытие
идёт по последней известной цене. Правил клетки повтор не применяет, всё без ввода-вывода
и случайности, кроме `random.Random(seed * 1000 + i)` у бота номер i.
"""
import random
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace

from traider.cage import Account
from traider.market import Bar
from traider.nums import usable
from traider.sim import apply_buy, apply_close, apply_stops

KINDS = ("buy", "close", "raise_stop", "liquidate")


def _ordered(events: Sequence[dict]) -> list[dict]:
    if all(isinstance(e.get("seq"), int) for e in events):
        return sorted(events, key=lambda e: e["seq"])
    return list(events)


def replay(events: Sequence[dict], snapshots: Mapping[str, Mapping[str, float]],
           bars: Mapping[str, Sequence[Bar]], days: Sequence[str], start_equity: float,
           pick: Callable[[dict], str | None] | None = None) -> list[float]:
    by_day: dict[str, list[dict]] = {}
    for e in _ordered(events):
        if e.get("kind") in KINDS:
            if e.get("day") not in days:
                raise ValueError(f"событие {e['kind']} {e.get('symbol')} {e.get('day')} вне дней повтора")
            by_day.setdefault(e["day"], []).append(e)
    bar_of = {s: {b.day: b for b in series} for s, series in bars.items()}
    all_dates = sorted({b.day for series in bars.values() for b in series})
    # Позиции счёта повтора названы символом агента: каждая позиция агента это свой лот
    # в бумаге-замене `ticker_of[символ]`. Цены берутся по замене, в симулятор уходят под символом агента.
    acct = Account(start_equity, (), start_equity)
    last: dict[str, float] = {}
    ticker_of: dict[str, str] = {}
    curve: list[float] = []

    def held(sym):
        return next((p for p in acct.positions if p.symbol == sym), None)

    def stops(date: str) -> None:
        nonlocal acct
        bar = {s: bar_of[t][date] for s, t in ticker_of.items() if date in bar_of.get(t, {})}
        acct, _ = apply_stops(acct, {s: b.low for s, b in bar.items()}, {s: b.open for s, b in bar.items()})
        for s in [s for s in ticker_of if held(s) is None]:
            del ticker_of[s]

    def close_prices(date: str) -> None:
        last.update({s: m[date].close for s, m in bar_of.items() if date in m and usable(m[date].close)})

    for i, d in enumerate(days):
        for gap in (x for x in all_dates if i and days[i - 1] < x < d):    # дни между днями повтора
            stops(gap)
            close_prices(gap)
        snap = snapshots.get(d, {})
        last.update({s: p for s, p in snap.items() if usable(p)})
        stops(d)
        for e in by_day.get(d, ()):
            kind, agent_sym = e["kind"], e.get("symbol")
            if kind == "liquidate":
                # журнал пишет по событию на бумагу: первое закрывает все лоты, остальные ничего не находят
                for p in acct.positions:
                    t = ticker_of[p.symbol]
                    price = snap.get(t) if usable(snap.get(t)) else last.get(t)
                    if usable(price):
                        acct, _ = apply_close(acct, p.symbol, 1.0, price)
                for s in [s for s in ticker_of if held(s) is None]:
                    del ticker_of[s]
                continue
            ref = e.get("price") if usable(e.get("price")) else snap.get(agent_sym)
            share = e["stop_price"] / ref if usable(e.get("stop_price")) and usable(ref) else None
            if kind == "buy":
                t = ticker_of[agent_sym] if held(agent_sym) else (pick(e) if pick else agent_sym)
                price = snap.get(t)
                notional = min(e.get("notional") or 0.0, acct.cash)
                if share is None or not usable(price) or not notional >= 1.0:
                    continue
                acct, _ = apply_buy(acct, agent_sym, notional, price, price * share)
                ticker_of[agent_sym] = t
                continue
            pos = held(agent_sym)
            if pos is None:
                continue
            t = ticker_of[agent_sym]
            price = snap.get(t) if usable(snap.get(t)) else last.get(t)
            if kind == "close":
                fraction = e.get("fraction", 1.0)
                if not usable(fraction) or not usable(price):
                    continue
                acct, _ = apply_close(acct, agent_sym, min(fraction, 1.0), price)
                if fraction >= 1:
                    del ticker_of[agent_sym]
            elif share is not None and usable(price):    # raise_stop
                stop = max(pos.stop_price, price * share)
                acct = replace(acct, positions=tuple(replace(p, stop_price=stop) if p.symbol == agent_sym else p
                                                     for p in acct.positions))
        close_prices(d)
        curve.append(acct.cash + sum(p.qty * last.get(ticker_of[p.symbol], p.entry_price) for p in acct.positions))
    return curve


def shadow(events, snapshots, bars, days, start_equity) -> list[float]:
    return replay(events, snapshots, bars, days, start_equity)


def monkeys(events, snapshots, bars, days, start_equity, universe: Sequence[str],
            n: int = 100, seed: int = 0) -> list[list[float]]:
    def bot(i: int) -> list[float]:
        rng = random.Random(seed * 1000 + i)

        def pick(e: dict) -> str | None:
            priced = [s for s in universe if usable(snapshots.get(e["day"], {}).get(s))]
            return rng.choice(priced) if priced else None
        return replay(events, snapshots, bars, days, start_equity, pick)
    return [bot(i) for i in range(n)]


def spy_same_exposure(events, snapshots, bars, days, start_equity) -> list[float]:
    return replay(events, snapshots, bars, days, start_equity, pick=lambda e: "SPY")


def spy_buy_hold(bars: Mapping[str, Sequence[Bar]], days: Sequence[str], start_equity: float) -> list[float]:
    close = {b.day: b.close for b in bars.get("SPY", ()) if usable(b.close)}
    first = next((close[d] for d in days if d in close), None)    # без свечи в первый день: деньги, пока свечи нет
    curve, seen = [], None
    for d in days:
        seen = close.get(d, seen)
        curve.append(start_equity * seen / first if seen is not None else start_equity)
    return curve


def percentile(value: float, population: Sequence[float]) -> float | None:
    """Доля population ниже value плюс половина равных, от 0 до 100; пустая выборка даёт None."""
    below = sum(x < value for x in population)
    equal = sum(x == value for x in population)
    return 100.0 * (below + equal / 2) / len(population) if population else None


def band(curves: Sequence[Sequence[float]], qs=(5, 25, 50, 75, 95)) -> dict[str, list[float]]:
    def at(col: list[float], q: float) -> float:
        col = sorted(col)
        pos = (len(col) - 1) * q / 100
        lo = int(pos)
        return col[lo] + (col[min(lo + 1, len(col) - 1)] - col[lo]) * (pos - lo)
    return {f"p{q:g}": [at(list(col), q) for col in zip(*curves)] for q in qs}
