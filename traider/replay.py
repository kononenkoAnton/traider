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
Если позиция агента ещё есть в повторе, покупка докупает ту же бумагу, иначе
бумага выбирается заново (`pick`, без него это бумага агента). Закрытие и подъём стопа
по уже выбитой позиции пропускаются. Нет цены в снимке: покупка пропускается, закрытие
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
           pick: Callable[[dict], str] | None = None) -> list[float]:
    by_day: dict[str, list[dict]] = {}
    for e in _ordered(events):
        if e.get("kind") in KINDS:
            by_day.setdefault(e.get("day"), []).append(e)
    bar_of = {s: {b.day: b for b in series} for s, series in bars.items()}
    acct = Account(start_equity, (), start_equity)
    last: dict[str, float] = {}
    ticker_of: dict[str, str] = {}          # символ агента -> бумага повтора
    curve: list[float] = []

    def held(sym: str):
        return next((p for p in acct.positions if p.symbol == sym), None)

    for d in days:
        snap = snapshots.get(d, {})
        last.update({s: p for s, p in snap.items() if usable(p)})
        today = {s: b for s, m in bar_of.items() if (b := m.get(d))}
        acct, _ = apply_stops(acct, {s: b.low for s, b in today.items()}, {s: b.open for s, b in today.items()})
        for e in by_day.get(d, ()):
            kind, agent_sym = e["kind"], e.get("symbol")
            if kind == "liquidate":
                for p in acct.positions:
                    if usable(last.get(p.symbol)):
                        acct, _ = apply_close(acct, p.symbol, 1.0, snap.get(p.symbol) if usable(snap.get(p.symbol)) else last[p.symbol])
                ticker_of.clear()
                continue
            ref = e.get("price") if usable(e.get("price")) else snap.get(agent_sym)
            share = e["stop_price"] / ref if usable(e.get("stop_price")) and usable(ref) else None
            if kind == "buy":
                sym = ticker_of.get(agent_sym)
                if held(sym) is None:
                    sym = pick(e) if pick else agent_sym
                price = snap.get(sym)
                notional = min(e.get("notional") or 0.0, acct.cash)
                if share is None or not usable(price) or not notional >= 1.0:
                    continue
                acct, _ = apply_buy(acct, sym, notional, price, price * share)
                ticker_of[agent_sym] = sym
                continue
            sym = ticker_of.get(agent_sym)
            pos = held(sym)
            if pos is None:
                continue
            if kind == "close":
                fraction = e.get("fraction", 1.0)
                price = snap.get(sym) if usable(snap.get(sym)) else last.get(sym)
                if not usable(fraction) or not usable(price):
                    continue
                acct, _ = apply_close(acct, sym, min(fraction, 1.0), price)
                if fraction >= 1:
                    del ticker_of[agent_sym]
            elif share is not None and usable(snap.get(sym)):    # raise_stop
                stop = max(pos.stop_price, snap[sym] * share)
                acct = replace(acct, positions=tuple(replace(p, stop_price=stop) if p.symbol == sym else p
                                                     for p in acct.positions))
        for s, b in today.items():
            if usable(b.close):
                last[s] = b.close
        curve.append(acct.cash + sum(p.qty * last.get(p.symbol, p.entry_price) for p in acct.positions))
    return curve


def shadow(events, snapshots, bars, days, start_equity) -> list[float]:
    return replay(events, snapshots, bars, days, start_equity)


def monkeys(events, snapshots, bars, days, start_equity, universe: Sequence[str],
            n: int = 100, seed: int = 0) -> list[list[float]]:
    def bot(i: int) -> list[float]:
        rng = random.Random(seed * 1000 + i)

        def pick(e: dict) -> str:
            start = rng.randrange(len(universe))
            order = [universe[(start + k) % len(universe)] for k in range(len(universe))]
            return next((s for s in order if usable(snapshots.get(e["day"], {}).get(s))), order[0])
        return replay(events, snapshots, bars, days, start_equity, pick)
    return [bot(i) for i in range(n)]


def spy_same_exposure(events, snapshots, bars, days, start_equity) -> list[float]:
    return replay(events, snapshots, bars, days, start_equity, pick=lambda e: "SPY")


def spy_buy_hold(bars: Mapping[str, Sequence[Bar]], days: Sequence[str], start_equity: float) -> list[float]:
    close = {b.day: b.close for b in bars.get("SPY", ())}
    first = close.get(days[0]) if days else None
    curve, seen = [], first
    for d in days:
        seen = close.get(d, seen)
        curve.append(start_equity * seen / first if usable(first) and usable(seen) else start_equity)
    return curve


def percentile(value: float, population: Sequence[float]) -> float:
    """Доля population строго ниже value, от 0 до 100; пустая выборка даёт 0."""
    return 100.0 * sum(x < value for x in population) / len(population) if population else 0.0


def band(curves: Sequence[Sequence[float]], qs=(5, 25, 50, 75, 95)) -> dict[str, list[float]]:
    def at(col: list[float], q: float) -> float:
        col = sorted(col)
        pos = (len(col) - 1) * q / 100
        lo = int(pos)
        return col[lo] + (col[min(lo + 1, len(col) - 1)] - col[lo]) * (pos - lo)
    return {f"p{q:g}": [at(list(col), q) for col in zip(*curves)] for q in qs}
