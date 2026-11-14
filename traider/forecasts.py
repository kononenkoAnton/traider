"""Оценка прогнозов: исходы, оценка Брайера, эталон, блочный бутстреп и калибровка.

:wk-id: forecasts-module
:wk-tags: forecasts, calibration, brier
:wk-categories: experiment

Как это работает. Чистые функции без ввода-вывода. `score` берёт для каждого дня сессии
и каждой бумаги пару цен закрытия: в день прогноза и через `horizon` торговых дней по
списку `trading_days`. Исход равен 1, только если доходность бумаги строго больше доходности
`benchmark`. Порядок результата: дни сессии как заданы; внутри дня бумаги панели в порядке
панели (пропущенный прогноз становится 0.5 с `missing=True`), затем бумаги вне панели в
порядке прогнозов. Если день и бумага повторены, остаётся последний прогноз. Прогноз, где
`p` не конечное число из [0, 1], считается пропущенным. Прогноз по самому `benchmark`
пропускается. Пара выпадает целиком, если нет
положительной цены закрытия бумаги или бенчмарка в один из двух дней, либо дня со сдвигом
`horizon` нет в `trading_days`.

`brier` это средний квадрат ошибки, `reference` это f * (1 - f) при фактической доле
исходов f. Пустой список в `brier`, `reference`, `bootstrap_diff` даёт ValueError.
`bootstrap_diff`: отсортированные дни режутся на подряд идущие блоки по `block` дней
(последний может быть короче), блоки берутся с возвращением, пока не наберётся не меньше
дней, чем в исходных данных; для выборки считается `brier - reference`, так `n` раз с
`random.Random(seed)`. Границы интервала: 5-й и 95-й процентили по правилу ближайшего
ранга, то есть элементы `ceil(0.05 * n)` и `ceil(0.95 * n)` (счёт с 1) отсортированных
разностей. `calibration` отдаёт строку на каждую корзину `lo <= p < hi`, последняя
включает 1.0; у пустой `mean_p` и `freq` равны None.
"""
import random
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from math import ceil, isfinite

from traider.nums import usable


@dataclass(frozen=True)
class Scored:
    day: str
    symbol: str
    p: float
    outcome: int       # 1, если бумага обогнала SPY от закрытия day до закрытия через horizon торговых дней
    missing: bool      # прогноз не был дан и оценён как 0.5


def _p_ok(p: object) -> bool:
    return isinstance(p, (int, float)) and not isinstance(p, bool) and isfinite(p) and 0 <= p <= 1


def _close(closes: Mapping[str, Mapping[str, float]], symbol: str, day: str) -> float | None:
    x = closes.get(symbol, {}).get(day)
    return x if usable(x) else None


def score(forecasts: Sequence[dict], closes: Mapping[str, Mapping[str, float]], trading_days: Sequence[str],
          session_days: Sequence[str], panel: Sequence[str], horizon: int = 5, benchmark: str = "SPY") -> list[Scored]:
    index = {d: i for i, d in enumerate(trading_days)}
    given: dict[str, dict[str, object]] = {}          # день -> бумага -> p; повтор перезаписывает, порядок первого появления
    for f in forecasts:
        given.setdefault(f["day"], {})[f["symbol"]] = f.get("p")
    out = []
    for day in session_days:
        i = index.get(day)
        if i is None or i + horizon >= len(trading_days):
            continue
        end = trading_days[i + horizon]
        b0, b1 = _close(closes, benchmark, day), _close(closes, benchmark, end)
        if b0 is None or b1 is None:
            continue
        today = given.get(day, {})
        for sym in [*panel, *(s for s in today if s not in panel)]:
            if sym == benchmark:   # бумага не может обогнать саму себя
                continue
            s0, s1 = _close(closes, sym, day), _close(closes, sym, end)
            if s0 is None or s1 is None:
                continue
            p = today.get(sym)
            ok = _p_ok(p)
            out.append(Scored(day, sym, float(p) if ok else 0.5, int(s1 / s0 > b1 / b0), not ok))
    return out


def _brier_ref(n: int, sq: float, wins: float) -> tuple[float, float]:
    f = wins / n
    return sq / n, f * (1 - f)


def _totals(scored: Sequence[Scored]) -> tuple[int, float, float]:
    if not scored:
        raise ValueError("нет оценённых прогнозов")
    return len(scored), sum((s.p - s.outcome) ** 2 for s in scored), sum(s.outcome for s in scored)


def brier(scored: Sequence[Scored]) -> float:
    return _brier_ref(*_totals(scored))[0]


def reference(scored: Sequence[Scored]) -> float:
    return _brier_ref(*_totals(scored))[1]


def bootstrap_diff(scored: Sequence[Scored], block: int = 5, n: int = 2000, seed: int = 0) -> tuple[float, float]:
    _totals(scored)
    by_day: dict[str, list[Scored]] = {}
    for s in scored:
        by_day.setdefault(s.day, []).append(s)
    days = sorted(by_day)
    blocks = []                                        # на блок: дней, прогнозов, сумма квадратов, сумма исходов
    for k in range(0, len(days), block):
        items = [s for d in days[k:k + block] for s in by_day[d]]
        blocks.append((len(days[k:k + block]), *_totals(items)))
    rng = random.Random(seed)
    diffs = []
    for _ in range(n):
        d = cnt = 0
        sq = wins = 0.0
        while d < len(days):
            bd, bc, bsq, bw = blocks[rng.randrange(len(blocks))]
            d, cnt, sq, wins = d + bd, cnt + bc, sq + bsq, wins + bw
        b, r = _brier_ref(cnt, sq, wins)
        diffs.append(b - r)
    diffs.sort()
    return diffs[ceil(0.05 * n) - 1], diffs[ceil(0.95 * n) - 1]


def calibration(scored: Sequence[Scored], edges: Sequence[float] = (0.0, 0.2, 0.4, 0.6, 0.8, 1.0)) -> list[dict]:
    last = len(edges) - 2
    rows = []
    for k, (lo, hi) in enumerate(zip(edges, edges[1:])):
        items = [s for s in scored if lo <= s.p < hi or (k == last and s.p == hi)]
        rows.append({"lo": lo, "hi": hi, "n": len(items),
                     "mean_p": sum(s.p for s in items) / len(items) if items else None,
                     "freq": sum(s.outcome for s in items) / len(items) if items else None})
    return rows


def summary_text(scored: Sequence[Scored]) -> str:
    if not scored:
        return "Созревших прогнозов пока нет."
    return "\n".join(f"прогнозы {round(r['lo'] * 100)}–{round(r['hi'] * 100)}%: сбылось {round(r['freq'] * 100)}% (n={r['n']})"
                     for r in calibration(scored) if r["n"])
