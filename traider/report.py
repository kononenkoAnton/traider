"""Отчёт эксперимента: показатели, сравнения участников и вердикт одним словарём.

:wk-id: report-module
:wk-tags: report, verdict, metrics
:wk-categories: experiment

Как это работает. `build_report` только читает: файлы прогонов через `load_run` и `RunStore` и рынок
(`trading_days`, один вызов `daily_bars` на вселенную и бенчмарк, `now`). Брокер не создаётся, файлы
не меняются, поэтому отчёт можно строить в любой момент, и во время тика. Дни участника это все торговые
дни от первой записи сессии до последней записи или `died_on`, что позже; `days` отчёта это их
объединение. Кривые (`equity`, `account_equity`, `spy`, `spy_same_exposure`, полоса) начинаются точкой
старта, дальше по точке на день участника; у погибшего кривая кончается днём гибели. Тень, боты
(`cfg.seed`, вселенная с дневными свечами), SPY считает `traider.replay` по журналу в порядке `seq`
(события вне дней участника не берутся) и снимкам цен самого участника. День без сохранённого снимка
(день гибели: тик снимка не пишет; сессия, упавшая до снимка) получает снимок отчёта: закрытие дневной свечи
каждой бумаги, а у бумаг, которыми участник торговал в этот день, котировка из исполнения в журнале (цена
продажи / (1 - SLIPPAGE), покупки / (1 + SLIPPAGE)); иначе тень погибшего продала бы по вчерашней цене.
Сохранённый снимок берётся как есть, кроме дня гибели (`died_on`): гибель днём наступает после утреннего снимка,
поэтому в этот день снимок отчёта строится всегда (без свечи у бумаги берётся сохранённая цена; ликвидация
главнее других исполнений бумаги). Ограничение: лот, купленный и ликвидированный в сам день гибели, повтор
покупает по цене ликвидации, и убытка у этого лота нет; лоты, купленные раньше, получают падение целиком.
Участник, у которого записанный при старте брокер `alpaca` (без записи: брокер конфига), а id заявок брокера не переданы, даёт происшествие `warning` «проверка по брокеру не проведена»; общее происшествие о целостности `good` только когда проведены все применимые проверки и они чисты. Нет свечей бенчмарка: `spy` и `vs_spy_pp` None. `beyond_noise` None,
пока разница близнецов по модулю меньше 1e-9 (шум float) или у одного из них нет покупок. Место среди ботов: None, пока нет
покупок. Капитал счёта, HP и доля HP берутся из `ticks.jsonl`: последний тик дня с числом, иначе None.
Прогнозы: `brier`, `reference`, `ci` по панели, `brier_picks` по прогнозам купленных в тот же день бумаг;
нет созревших, значит None. `brier_series` по дням отчёта: те же Брайер и эталон по созревшим прогнозам
панели, данным до дня включительно (последняя точка равна сводке); вне дней участника и без таких прогнозов None.
`money`: месяц последнего дня сессии, записанные траты всех участников в нём (None, если в месяце ни одной записанной
стоимости) и за прогон, число сессий без записанной стоимости, прогноз на месяц (траты месяца плюс средний расход дня
сессии на оставшиеся торговые дни месяца, не больше сессий до цели). У участника `first_day` и `day_offset`: его
первый день сессии и номер этого дня в `days`, без сессий None. Закрытая сделка это каждое событие продажи (`agent`, `stop`, `liquidation`);
без годных входа или выхода у неё `pnl` None. Диалог сессии в отчёте есть только у 10 последних дней
сессий эксперимента, каждая строка режется до 2000 знаков с пометкой. Порядок правил `verdict`: гибель или
нарушение целостности, нехватка данных (в том числе нет места или интервала), красный, зелёный, жёлтый;
пороги только из `cfg.criteria`. Всё детерминировано, кроме `meta.generated_at` (`market.now()`).
"""
import calendar
import inspect
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from math import isfinite
from pathlib import Path
from statistics import StatisticsError, correlation

from traider import forecasts, replay
from traider.config import load_run
from traider.market import Market, ny_day
from traider.nums import usable
from traider.sim import SLIPPAGE
from traider.store import RunStore

ORDER = ("haiku", "sonnet", "opus-a", "opus-b", "opus-analyst")
RANK = ("red", "no_data", "yellow", "green")                  # от худшего к лучшему
BENCHMARK = inspect.signature(forecasts.score).parameters["benchmark"].default
INFO_TOOLS = ("get_account", "scan_universe", "get_bars", "get_news", "web_search", "research")
SALES = {"close": "agent", "stop_fill": "stop", "liquidate": "liquidation"}
DECISION_KEYS = ("kind", "symbol", "notional", "qty", "price", "stop_price", "fraction", "thesis", "reason")
MONKEYS = 100
CUT = 2000
TRANSCRIPT_DAYS = 10
CREDITS_MONTH_USD = 100
REJECT_LABELS = {
    "not_in_universe": "Бумага не из списка", "no_price": "Нет цены", "no_panel_forecasts": "Нет прогнозов по панели",
    "no_symbol_forecast": "Нет прогноза по бумаге", "too_many_buys": "Больше 3 покупок за сессию",
    "rebuy_same_day": "Повторная покупка в день закрытия", "order_too_small": "Заявка меньше $20",
    "stop_out_of_range": "Стоп ближе 3% или дальше 15%", "stop_below_existing": "Стоп ниже действующего",
    "position_too_large": "Позиция больше 20% капитала", "not_enough_cash": "Не хватает денег",
    "open_risk_too_high": "Суммарный риск выше 8%", "no_position": "Нет позиции", "stop_not_higher": "Стоп не выше прежнего",
    "bad_request": "Негодная заявка", "broker_rejected": "Отказ брокера", "run_not_active": "Прогон не активен",
    "market_closed": "Рынок закрыт",
}
TICK_INCIDENTS = {"unpriced": ("serious", "нет котировки у удерживаемой бумаги"), "error": ("serious", "ошибка тика"),
                  "stale_failed": ("serious", "сессия зависла"), "invalid": ("critical", "конфиг изменён, прогон недействителен"),
                  "died": ("critical", "гибель: капитал ниже пола")}
SESSION_INCIDENTS = {"missed": ("critical", "пропущенный день"), "failed": ("serious", "сбой сессии"),
                     "cap": ("warning", "сессия упёрлась в потолок")}
DONE = ("ok", "cap", "failed", "missed")
NOT_CHECKED = "проверка по брокеру не проведена"


def verdict(m: Mapping, criteria: Mapping) -> str:
    if m["dead"] or m["integrity_violations"]:
        return "red"
    pct, ci, missed = m["percentile"], m["ci"], m["missed_share"]
    if (m["sessions_done"] < criteria["min_sessions"] or missed is None or missed > criteria["max_missed_share"]
            or m["closed_trades"] < criteria["min_closed_trades"] or pct is None or ci is None):
        return "no_data"
    if pct < criteria["red_percentile"] or ci[0] > 0:
        return "red"
    if pct >= criteria["green_percentile"] and ci[1] < 0:
        return "green"
    return "yellow"


def worst(verdicts: Sequence[str]) -> str:
    return min(verdicts, key=RANK.index)


def integrity(store: RunStore, broker_order_ids: Iterable[str] | None) -> list[str]:
    """Заявки брокера, которых нет в журнале (по алфавиту), затем по пометке на каждый пропуск в `seq`."""
    evs = store.orders()
    have = {e.get("broker_order_id") for e in evs}
    out = sorted(set(broker_order_ids) - have) if broker_order_ids is not None else []
    prev = 0
    for s in sorted({e["seq"] for e in evs if isinstance(e.get("seq"), int)}):
        if s > prev + 1:
            out.append(f"seq {prev + 1}" if s == prev + 2 else f"seq {prev + 1}–{s - 1}")
        prev = s
    return out


def _num(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and isfinite(x)


def _cut(x):
    if isinstance(x, str):
        return x if len(x) <= CUT else x[:CUT] + f" … [обрезано: было {len(x)} знаков]"
    if isinstance(x, list):
        return [_cut(v) for v in x]
    if isinstance(x, dict):
        return {k: _cut(v) for k, v in x.items()}
    return x


def _looked(transcript) -> list[dict]:
    seen: dict[str, dict] = {}
    for msg in transcript or []:
        if not isinstance(msg, dict) or msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
            continue
        for b in msg["content"]:
            if isinstance(b, dict) and b.get("type") in ("tool_use", "server_tool_use") and b.get("name") in INFO_TOOLS:
                x = seen.setdefault(b["name"], {"name": b["name"], "calls": 0, "example": _cut(b.get("input"))})
                x["calls"] += 1
    return list(seen.values())


def _pct(curve: Sequence[float], start: float) -> float:
    return (curve[-1] / start - 1) * 100


def _load(run: Path, market: Market) -> dict:
    cfg, _ = load_run(run)
    store = RunStore(run)
    state, records = store.state(), store.sessions()
    ends = [r["day"] for r in records] + ([state["died_on"]] if state.get("died_on") else [])
    days = market.trading_days(records[0]["day"] if records else ends[0], max(ends)) if ends else []
    return {"id": run.name, "cfg": cfg, "store": store, "state": state, "records": records, "days": days,
            "orders": store.orders(), "ticks": store.ticks(), "forecasts": store.forecasts()}


def _snapshot(p: dict, day: str, closes: Mapping) -> dict:
    """Снимок дня для повтора: сохранённый как есть, кроме дня гибели; иначе закрытия свечей дня (без свечи цена
    сохранённого снимка), а у бумаг со сделками котировка из исполнения."""
    saved = p["store"].prices(day)
    if saved is not None and day != p["state"].get("died_on"):
        return saved
    snap = {**(saved or {}), **{s: c[day] for s, c in closes.items() if usable(c.get(day))}}
    fills = [e for e in p["orders"] if e.get("day") == day and usable(e.get("price")) and e.get("kind") in ("buy", *SALES)]
    for e in sorted(fills, key=lambda e: e["kind"] == "liquidate"):     # ликвидация главнее, иначе последнее исполнение
        snap[e["symbol"]] = e["price"] / ((1 + SLIPPAGE) if e["kind"] == "buy" else (1 - SLIPPAGE))
    return snap


def _has_benchmark(closes: Mapping) -> bool:
    return any(usable(c) for c in closes.get(BENCHMARK, {}).values())


def _curves(p: dict, bars: Mapping, closes: Mapping, tdays: Sequence[str]) -> None:
    cfg, days, start = p["cfg"], p["days"], p["cfg"].start_equity
    dayset = set(days)
    events = [e for e in p["orders"] if e.get("day") in dayset]
    snaps = {d: _snapshot(p, d, closes) for d in days}
    universe = [s for s in cfg.universe if bars.get(s)]
    shadow = replay.shadow(events, snaps, bars, days, start)
    bots = replay.monkeys(events, snaps, bars, days, start, universe, n=MONKEYS, seed=cfg.seed)
    p["snaps"], p["buys"] = snaps, [e for e in events if e.get("kind") == "buy"]
    p["equity"] = [start, *shadow]
    p["same"] = [start, *replay.spy_same_exposure(events, snaps, bars, days, start)]
    p["spy"] = [start, *replay.spy_buy_hold(bars, days, start)] if _has_benchmark(closes) else None
    p["band"] = {k: [start, *v] for k, v in replay.band(bots).items()}
    # итоги равны с точностью до микродоллара: разница в последних битах float не решает место
    p["percentile"] = (replay.percentile(round(shadow[-1], 6), [round(b[-1], 6) for b in bots])
                       if p["buys"] and days else None)
    sdays = [r["day"] for r in p["records"]]
    scored = forecasts.score(p["forecasts"], closes, tdays, sdays, cfg.panel, benchmark=BENCHMARK)
    panel = [s for s in scored if s.symbol in cfg.panel]
    bought = {(e["day"], e["symbol"]) for e in p["buys"]}
    picks = [s for s in forecasts.score(p["forecasts"], closes, tdays, sdays, (), benchmark=BENCHMARK)
             if (s.day, s.symbol) in bought]
    p["scored"] = scored
    p["brier"] = forecasts.brier(panel) if panel else None
    p["reference"] = forecasts.reference(panel) if panel else None
    p["ci"] = list(forecasts.bootstrap_diff(panel, seed=cfg.seed)) if panel else None
    p["brier_picks"] = forecasts.brier(picks) if picks else None
    p["panel_scored"] = panel


def _brier_series(p: dict, days: Sequence[str]) -> list:
    """По точке на день отчёта: Брайер и эталон по созревшим прогнозам панели, данным до этого дня включительно;
    вне дней участника и пока таких прогнозов нет, None."""
    mine, out = set(p["days"]), []
    for d in days:
        items = [s for s in p["panel_scored"] if s.day <= d]
        out.append({"brier": forecasts.brier(items), "reference": forecasts.reference(items), "n": len(items)}
                   if d in mine and items else None)
    return out


def _money(ps: list[dict], market: Market, target: int | None, done: int) -> dict:
    """Расход всех участников: месяц последнего дня сессии, записанные траты в нём и за прогон, сессии без записанной
    стоимости (пропуск стоит 0 и записан) и прогноз на месяц: траты месяца плюс средний расход дня сессии (по дням,
    где записана хоть одна стоимость) на дни сессий, которые ещё ждут в этом месяце (торговые дни после последнего
    дня сессии, но не больше сессий до цели); пока траты месяца неизвестны, прогноза нет."""
    recs = [(r["day"], _session_cost(r)) for p in ps for r in p["records"]]
    spent = [(d, c) for d, c in recs if c is not None]
    sdays = sorted({d for d, _ in recs})
    out = {"month": None, "month_spend_usd": None, "run_spend_usd": float(sum(c for _, c in spent)),
           "month_forecast_usd": None, "sessions_without_cost": sum(c is None for _, c in recs), "month_sessions_without_cost": 0}
    if not sdays:
        return out
    month = out["month"] = sdays[-1][:7]
    in_month = [c for d, c in recs if d.startswith(month)]
    out["month_sessions_without_cost"] = sum(c is None for c in in_month)
    if any(c is not None for c in in_month):
        out["month_spend_usd"] = float(sum(c for c in in_month if c is not None))
    by_day: dict[str, float] = {}
    for d, c in spent:
        by_day[d] = by_day.get(d, 0.0) + c
    if by_day and out["month_spend_usd"] is not None:          # неизвестные траты месяца не прогнозируются
        y, mo = int(month[:4]), int(month[5:])
        after = [d for d in market.trading_days(f"{month}-01", f"{month}-{calendar.monthrange(y, mo)[1]:02d}") if d > sdays[-1]]
        left = len(after) if target is None else min(len(after), max(0, target - done))
        out["month_forecast_usd"] = out["month_spend_usd"] + sum(by_day.values()) / len(by_day) * left
    return out


def _summary(p: dict) -> dict:
    cfg, days, eq, start = p["cfg"], p["days"], p["equity"], p["cfg"].start_equity
    dayset = set(days)
    ticks = [t for t in p["ticks"] if t.get("day") in dayset]
    by_day = {t["day"]: t["equity"] for t in ticks if _num(t.get("equity"))}
    p["account_equity"] = [start, *(by_day.get(d) for d in days)]
    last = next((t for t in reversed(ticks) if _num(t.get("hp"))), None)
    fs = cfg.limits.floor_share
    hp = last["hp"] if last else None
    share = hp / (last["floor"] * (1 - fs) / fs) if last and usable(last.get("floor")) else None
    peak, dd = start, 0.0
    for x in eq:
        peak = max(peak, x)
        dd = min(dd, (x / peak - 1) * 100)
    costs = [r.get("cost") for r in p["records"] if _num(r.get("cost"))]
    ret = _pct(eq, start) if days else None
    return {"equity": eq[-1], "return_pct": ret,
            "vs_spy_pp": ret - _pct(p["spy"], start) if days and p["spy"] else None,
            "percentile": p["percentile"], "max_drawdown_pct": dd if days else None, "hp": hp, "hp_share": share,
            "trades": len(p["buys"]), "closed_trades": sum(e.get("kind") in SALES for e in p["orders"]),
            "brier": p["brier"], "reference": p["reference"], "ci": p["ci"], "brier_picks": p["brier_picks"],
            "cost": float(sum(costs))}


def _session_cost(r: dict):
    return r["cost"] if _num(r.get("cost")) else 0.0 if r.get("status") == "missed" else None


def _sessions(p: dict, recent: set[str]) -> list[dict]:
    out = []
    for r in p["records"]:
        day = r["day"]
        evs = [e for e in p["orders"] if e.get("day") == day]
        a = r.get("analyst")
        out.append({
            "day": day, "status": r.get("status"), "buys": r.get("buys", 0), "sells": r.get("sells", 0),
            "rejects": r.get("rejects", 0), "turns": r.get("turns"), "cost": _session_cost(r), "usage": r.get("usage"),
            "journal": r.get("journal"), "looked": _looked(r.get("transcript")),
            "decisions": [{k: e.get(k) for k in DECISION_KEYS} for e in evs if e.get("kind") in ("buy", "close", "raise_stop")],
            "cage": [{k: e.get(k) for k in ("action", "symbol", "reason", "detail")} for e in evs if e.get("kind") == "reject"],
            "forecasts": [{"symbol": f.get("symbol"), "p": f.get("p")} for f in p["forecasts"] if f.get("day") == day],
            "analyst": a and {"cost": a.get("cost"), "searches": a.get("searches"), "briefing": _cut(a.get("briefing")),
                              "model": a.get("model"), "calls": [{"kind": c.get("kind"), "question": _cut(c.get("question")),
                                                                  "stop": c.get("stop"), "cost": c.get("cost")}
                                                                 for c in a.get("calls") or []]},
            "transcript": _cut(r.get("transcript")) if day in recent else None})
    return out


def _tokens(p: dict, cost: float) -> dict:
    spent = [r for r in p["records"] if isinstance(r.get("usage"), dict)]

    def total(rows, key):
        return sum(u.get(key, 0) or 0 for u in rows)
    usage = [r["usage"] for r in spent]
    analyst = None
    if p["cfg"].research == "analyst":
        an = [r["analyst"] for r in p["records"] if isinstance(r.get("analyst"), dict)]
        acost = sum(a.get("cost") or 0.0 for a in an)
        au = [a.get("usage") or {} for a in an]
        analyst = {"cost": acost, "searches": sum(a.get("searches") or 0 for a in an),
                   "tokens": {k: total(au, k) for k in ("input", "cache_read", "output")},
                   "share": acost / cost if cost > 0 else None}
    return {"sessions": len(spent), "input": total(usage, "input"), "cache_read": total(usage, "cache_read"),
            "output": total(usage, "output"), "searches": total(usage, "searches"),
            "cost_per_session": cost / len(spent) if spent else None, "analyst": analyst}


def _incidents(ps: list[dict], checked: dict[str, list[str]], with_broker: set[str], on_alpaca: set[str]) -> list[dict]:
    out = []
    for p in ps:
        pid = p["id"]
        for r in p["records"]:
            if r.get("status") in SESSION_INCIDENTS:
                kind, title = SESSION_INCIDENTS[r["status"]]
                out.append({"kind": kind, "day": r["day"], "participant": pid, "title": title,
                            "text": "; ".join(r.get("errors") or []) or f"статус сессии {r['status']}"})
            if r.get("fallback_used"):
                out.append({"kind": "warning", "day": r["day"], "participant": pid, "title": "подмена модели",
                            "text": f"ответила {r.get('model_used')}, стоимость посчитана по основной модели"})
        stale = {r["day"] for r in p["records"] if r.get("status") == "failed" and r.get("stop") == "stale"}
        seen: dict[tuple, dict] = {}
        for t in p["ticks"]:
            if t.get("action") == "stale_failed" and t.get("day") in stale:
                continue   # запись дня уже дала «сбой сессии»
            keys = [t.get("action")] + (["unpriced"] if t.get("unpriced") else [])
            for key in dict.fromkeys(k for k in keys if k in TICK_INCIDENTS):
                kind, title = TICK_INCIDENTS[key]
                x = seen.setdefault((t.get("day"), key), {"kind": kind, "day": t.get("day"), "participant": pid,
                                                          "title": title, "text": "", "n": 0})
                x["n"] += 1
                x["text"] = t.get("error") or ", ".join(t.get("unpriced") or []) or x["text"]
        out += [{**{k: v for k, v in x.items() if k != "n"}, "text": f"{x['text'] + '; ' if x['text'] else ''}тиков: {x['n']}"}
                for x in seen.values()]
        if pid in with_broker:
            bad = checked[pid]
            out.append({"kind": "critical" if bad else "good", "day": None, "participant": pid,
                        "title": "целостность записей нарушена" if bad else "целостность в порядке",
                        "text": ", ".join(bad) or "все исполненные заявки брокера есть в журнале"})
        elif pid in on_alpaca:   # счёт брокера есть, а сверки не было: «в порядке» про него никто не прочтёт
            out.append({"kind": "warning", "day": None, "participant": pid, "title": NOT_CHECKED,
                        "text": "заявки брокера не сверены с журналом"})
    bad = {pid: v for pid, v in checked.items() if v}
    unchecked = on_alpaca - with_broker
    out.append({"kind": "critical" if bad else "warning" if unchecked else "good", "day": None, "participant": None,
                "title": "целостность записей нарушена" if bad else NOT_CHECKED if unchecked else "целостность в порядке",
                "text": "; ".join(f"{pid}: {', '.join(v)}" for pid, v in bad.items())
                        or ("сверка с брокером не проведена у: " + ", ".join(sorted(unchecked)) if unchecked else
                            "журналы без пропусков, заявки брокера на месте")})
    return out


def _compare(ps: dict[str, dict], summary: dict, positions: dict) -> dict:
    def get(pid, key):
        return summary.get(pid, {}).get(key)

    def diff(a, b):
        return None if a is None or b is None else a - b

    def mean(pid_a, pid_b, key):
        a, b = get(pid_a, key), get(pid_b, key)
        return None if a is None or b is None else (a + b) / 2

    twins = diff(get("opus-a", "return_pct"), get("opus-b", "return_pct"))

    quiet = twins is None or abs(twins) < 1e-9 or not (ps.get("opus-a", {}).get("buys") and ps.get("opus-b", {}).get("buys"))

    def beyond(d):   # без шума близнецов мерить нечем: разница 0 или один из них не покупал
        return None if d is None or quiet else abs(d) > 2 * abs(twins)

    def bought(pid):
        return {e["symbol"] for e in ps[pid]["buys"]} if pid in ps else set()

    def given(pid):
        return {(f["day"], f["symbol"]): f["p"] for f in ps[pid]["forecasts"]
                if f.get("symbol") in ps[pid]["cfg"].panel and _num(f.get("p"))} if pid in ps else {}
    a, b, fa, fb = bought("opus-a"), bought("opus-b"), given("opus-a"), given("opus-b")
    keys = sorted(fa.keys() & fb.keys())
    try:
        corr = correlation([fa[k] for k in keys], [fb[k] for k in keys]) if len(keys) >= 2 else None
    except StatisticsError:      # одинаковые прогнозы: корреляции нет
        corr = None
    step = ("return_pct", "percentile", "brier")
    ladder = {pid: {k: get(pid, k) for k in step} for pid in ("haiku", "sonnet")}
    ladder["opus"] = {k: mean("opus-a", "opus-b", k) for k in step}
    rets = [v["return_pct"] for v in ladder.values() if v["return_pct"] is not None]
    analyst = diff(get("opus-analyst", "return_pct"), ladder["opus"]["return_pct"])
    return {"twins": {"diff_pp": twins, "common": len(a & b) / len(a | b) if a | b else None,
                      "held": sorted(s for s, who in positions.items() if {"opus-a", "opus-b"} <= set(who)),
                      "forecast_corr": corr},
            "ladder": {**ladder, "beyond_noise": beyond(max(rets) - min(rets)) if len(rets) >= 2 else None},
            "analyst": {"diff_pp": analyst, "beyond_noise": beyond(analyst)}}


def build_report(exp_dir: Path, market: Market, broker_order_ids: Mapping[str, Iterable[str]] | None = None) -> dict:
    runs = sorted((c.parent for c in Path(exp_dir).glob("*/config.json")),
                  key=lambda r: (ORDER.index(r.name) if r.name in ORDER else len(ORDER), r.name))
    ps = [_load(r, market) for r in runs]
    days = sorted({d for p in ps for d in p["days"]})
    tdays = market.trading_days(days[0], ny_day(market.now())) if days else []
    symbols = sorted({s for p in ps for s in p["cfg"].universe} | {BENCHMARK})
    bars = market.daily_bars(symbols, len(tdays) + 5)
    closes = {s: {b.day: b.close for b in bs} for s, bs in bars.items()}
    recent = set(sorted({r["day"] for p in ps for r in p["records"]})[-TRANSCRIPT_DAYS:])
    ids = broker_order_ids or {}
    summary, sessions, tokens, costs, positions, cash, trades, verdicts, checked = {}, {}, {}, {}, {}, {}, [], {}, {}
    for p in ps:
        pid, cfg = p["id"], p["cfg"]
        _curves(p, bars, closes, tdays)
        summary[pid] = s = _summary(p)
        sessions[pid] = _sessions(p, recent)
        tokens[pid] = _tokens(p, s["cost"])
        by_day = {r["day"]: _session_cost(r) for r in p["records"]}
        costs[pid] = [by_day.get(d, 0.0) for d in p["days"]]
        acct = p["store"].account()
        snap = next((p["snaps"][d] for d in reversed(p["days"]) if p["snaps"][d]), {})
        cash[pid] = acct.cash if acct else None
        for pos in acct.positions if acct else ():
            price = snap.get(pos.symbol) if usable(snap.get(pos.symbol)) else pos.entry_price
            positions.setdefault(pos.symbol, {}).setdefault(pid, 0.0)
            positions[pos.symbol][pid] += pos.qty * price
        for e in p["orders"]:
            if e.get("kind") in SALES:
                entry, exit_, qty = e.get("entry_price"), e.get("price"), e.get("qty")
                trades.append({"day": e.get("day"), "participant": pid, "symbol": e.get("symbol"), "entry": entry,
                               "exit": exit_, "qty": qty, "how": SALES[e["kind"]],
                               "pnl": (exit_ - entry) * qty if usable(entry) and usable(exit_) and usable(qty) else None})
        checked[pid] = integrity(p["store"], ids.get(pid))
        done = [r for r in p["records"] if r.get("status") in DONE]
        verdicts[pid] = verdict({"sessions_done": len(done), "closed_trades": s["closed_trades"],
                                 "missed_share": sum(r["status"] == "missed" for r in done) / len(done) if done else None,
                                 "dead": p["state"]["status"] == "dead", "integrity_violations": len(checked[pid]),
                                 "percentile": p["percentile"], "ci": p["ci"]}, cfg.criteria)
        p["done"] = len(done)
    order = [p["id"] for p in ps]
    trades.sort(key=lambda t: (t["day"] or "", order.index(t["participant"])))
    rejects = Counter(e.get("reason") for p in ps for e in p["orders"] if e.get("kind") == "reject")
    twins = [verdicts[x] for x in ("opus-a", "opus-b") if x in verdicts]
    first = ps[0]["cfg"] if ps else None
    on_alpaca = {p["id"] for p in ps if (p["state"].get("started_with") or {}).get("broker", p["cfg"].broker) == "alpaca"}
    incidents = _incidents(ps, checked, {pid for pid in order if pid in ids}, on_alpaca)
    severity = ("critical", "serious", "warning", "good")
    incidents.sort(key=lambda i: (i["day"] is None, i["day"] or "", severity.index(i["kind"]),
                                  order.index(i["participant"]) if i["participant"] in order else len(order), i["title"]))
    return {
        "meta": {"experiment": first and first.experiment, "pilot": first and first.pilot,
                 "generated_at": market.now().isoformat(), "sessions_target": first and first.sessions_target,
                 "sessions_done": max((p["done"] for p in ps), default=0), "credits_month_usd": CREDITS_MONTH_USD},
        "days": days,
        "participants": [{"id": p["id"], "model": p["cfg"].model, "research": p["cfg"].research,
                          "broker": p["cfg"].broker, "status": p["state"]["status"], "verdict": verdicts[p["id"]],
                          "first_day": first_day, "day_offset": days.index(first_day) if first_day in days else None}
                         for p in ps for first_day in [min((r["day"] for r in p["records"]), default=None)]],
        "equity": {p["id"]: p["equity"] for p in ps},
        "account_equity": {p["id"]: p["account_equity"] for p in ps},
        "spy": [first.start_equity, *replay.spy_buy_hold(bars, days, first.start_equity)]
               if first and _has_benchmark(closes) else None,
        "spy_same_exposure": {p["id"]: p["same"] for p in ps},
        "band": {p["id"]: p["band"] for p in ps},
        "brier_series": {p["id"]: _brier_series(p, days) for p in ps},
        "money": _money(ps, market, first and first.sessions_target, max((p["done"] for p in ps), default=0)),
        "summary": summary, "costs": costs, "tokens": tokens, "sessions": sessions,
        "positions": {s: positions[s] for s in sorted(positions)}, "cash": cash, "trades": trades,
        "calibration": {p["id"]: forecasts.calibration(p["scored"]) for p in ps},
        "compare": _compare({p["id"]: p for p in ps}, summary, positions),
        "rejects": [{"reason": r, "label": REJECT_LABELS.get(r, r), "count": n}
                    for r, n in sorted(rejects.items(), key=lambda x: (-x[1], str(x[0])))],
        "incidents": incidents,
        "verdict": {"by_participant": verdicts, "opus": worst(twins) if twins else None},
    }
