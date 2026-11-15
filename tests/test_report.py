from statistics import mean

import pytest

from tests.fakes import FakeMarket
from traider import replay
from traider.config import load_run, start_experiment
from traider.report import build_report, integrity, verdict, worst
from traider.sim import SLIPPAGE
from traider.store import RunStore

CRIT = dict(min_sessions=40, max_missed_share=0.10, min_closed_trades=15, red_percentile=25, green_percentile=90)
BASE = dict(sessions_done=40, missed_share=0.05, closed_trades=20, dead=False, integrity_violations=0, percentile=60.0, ci=(-0.02, 0.01))

@pytest.mark.parametrize("change, expected", [
    ({}, "yellow"),
    ({"percentile": 90.0, "ci": (-0.03, -0.001)}, "green"),
    ({"percentile": 95.0, "ci": (-0.03, 0.0)}, "yellow"),
    ({"percentile": 89.9, "ci": (-0.03, -0.001)}, "yellow"),
    ({"percentile": 24.9}, "red"),
    ({"percentile": 25.0}, "yellow"),
    ({"ci": (0.001, 0.03)}, "red"),
    ({"sessions_done": 39}, "no_data"),
    ({"missed_share": 0.11}, "no_data"),
    ({"closed_trades": 14}, "no_data"),
    ({"sessions_done": 12, "dead": True}, "red"),
    ({"sessions_done": 12, "integrity_violations": 1}, "red"),
])
def test_verdict_table(change, expected):
    assert verdict({**BASE, **change}, CRIT) == expected

def test_worst_orders_verdicts():
    assert worst(["green", "yellow"]) == "yellow" and worst(["yellow", "no_data"]) == "no_data" and worst(["no_data", "red"]) == "red"

def test_integrity_lists_broker_orders_missing_from_the_ledger(tmp_path):
    store = RunStore(tmp_path)
    store.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "AAPL", "broker_order_id": "a1"})
    assert integrity(store, ["a1"]) == [] and integrity(store, ["a1", "zz"]) == ["zz"] and integrity(store, None) == []

def test_report_of_a_small_experiment(played):            # фикстура: три дня тиков на подделках, см. ниже
    exp, market = played
    r = build_report(exp, market)
    assert [p["id"] for p in r["participants"]] == ["haiku", "sonnet", "opus-a", "opus-b", "opus-analyst"]
    assert r["days"] == ["2026-12-01", "2026-12-02", "2026-12-03"] and r["meta"]["sessions_done"] == 3
    for pid in ("haiku", "opus-a", "opus-analyst"):
        assert len(r["equity"][pid]) == 4 and r["equity"][pid][0] == 1000.0
        assert sum(r["costs"][pid]) == pytest.approx(r["summary"][pid]["cost"])
        assert r["summary"][pid]["cost"] == pytest.approx(sum(s["cost"] for s in r["sessions"][pid]))
        assert set(r["band"][pid]) == {"p5", "p25", "p50", "p75", "p95"} and 0 <= r["summary"][pid]["percentile"] <= 100
        assert r["verdict"]["by_participant"][pid] == "no_data"
    assert r["verdict"]["opus"] == "no_data" and r["sessions"]["haiku"][0]["looked"]
    assert r["tokens"]["opus-analyst"]["analyst"]["searches"] >= 0 and r["tokens"]["haiku"]["analyst"] is None
    assert any(i["kind"] == "good" for i in r["incidents"])
    again = build_report(exp, market)
    assert {k: v for k, v in again.items() if k != "meta"} == {k: v for k, v in r.items() if k != "meta"}

def test_report_of_a_run_that_has_barely_started(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    r = build_report(exp, FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70))
    assert r["days"] == [] and r["trades"] == [] and r["equity"]["haiku"] == [1000.0]
    assert r["summary"]["haiku"]["brier"] is None and r["summary"]["haiku"]["percentile"] is None

def test_dead_participant_is_red_and_its_curve_ends(played_with_death):      # opus-b погибает на второй день
    exp, market = played_with_death
    r = build_report(exp, market)
    assert r["verdict"]["by_participant"]["opus-b"] == "red" and r["verdict"]["opus"] == "red"
    assert len(r["equity"]["opus-b"]) == 3 and len(r["equity"]["opus-a"]) == 4
    assert any(i["kind"] == "critical" and i["participant"] == "opus-b" for i in r["incidents"])


# --- сверх задания: решения контролёра ---

@pytest.mark.parametrize("change", [{"percentile": None}, {"ci": None}])
def test_missing_percentile_or_interval_is_no_data(change):
    assert verdict({**BASE, **change}, CRIT) == "no_data"
    assert verdict({**BASE, **change, "dead": True}, CRIT) == "red"        # гибель важнее нехватки данных

def test_account_equity_is_the_last_numbered_tick_of_each_day(played):
    exp, market = played
    store = RunStore(exp / "haiku")
    store.append_tick({"ts": "x", "day": "2026-12-02", "action": "safety", "equity": 1234.5, "floor": 850.0, "hp": 384.5})
    store.append_tick({"ts": "y", "day": "2026-12-02", "action": "error", "equity": None, "floor": None, "hp": None})
    (exp / "sonnet" / "ticks.jsonl").write_text("")                          # у sonnet тиков нет совсем
    r = build_report(exp, market)
    assert r["account_equity"]["haiku"][2] == 1234.5                         # [старт, 01.12, 02.12, 03.12]
    assert r["account_equity"]["sonnet"] == [1000.0, None, None, None]
    assert r["summary"]["sonnet"]["hp"] is None and r["summary"]["sonnet"]["hp_share"] is None

def test_every_sale_is_a_closed_trade_even_without_entry_price(played):
    exp, market = played
    store = RunStore(exp / "sonnet")
    store.append_order({"day": "2026-12-03", "kind": "stop_fill", "symbol": "XLE", "qty": 0.1, "price": 140.0,
                        "entry_price": None, "reason": "stop"})
    store.append_order({"day": "2026-12-03", "kind": "close", "symbol": "XLE", "qty": 0.1, "price": 150.0,
                        "entry_price": 145.0, "fraction": 0.5, "reason": "т"})
    r = build_report(exp, market)
    mine = [t for t in r["trades"] if t["participant"] == "sonnet"]
    assert {t["how"] for t in mine} == {"agent", "stop", "liquidation"}
    assert next(t for t in mine if t["how"] == "stop")["pnl"] is None
    assert next(t for t in mine if t["how"] == "agent")["pnl"] == pytest.approx(0.5)
    assert r["summary"]["sonnet"]["closed_trades"] == len(mine)

def test_integrity_marks_a_gap_in_seq(tmp_path):
    store = RunStore(tmp_path)
    for _ in range(3):
        store.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "AAPL"})
    lines = (tmp_path / "orders.jsonl").read_text().splitlines()
    (tmp_path / "orders.jsonl").write_text("\n".join([lines[0], lines[2]]) + "\n")
    assert integrity(store, None) == ["seq 2"]

def test_ticks_collapse_into_one_incident_per_participant_day_and_kind(played):
    exp, market = played
    store = RunStore(exp / "sonnet")
    for _ in range(4):
        store.append_tick({"ts": "x", "day": "2026-12-02", "action": "error", "error": "сбой"})
    session = store.session("2026-12-03")
    store.finish_session("2026-12-03", session["status"], {"fallback_used": True})
    r = build_report(exp, market)
    mine = [i for i in r["incidents"] if i["participant"] == "sonnet"]
    assert len([i for i in mine if i["day"] == "2026-12-02" and i["kind"] == "serious"]) == 1
    assert any(i["day"] == "2026-12-03" and i["kind"] == "warning" for i in mine)

def test_transcripts_are_cut_and_only_recent_days_carry_one(played):
    exp, market = played
    store = RunStore(exp / "haiku")
    for d in [f"2026-11-{n:02d}" for n in (2, 3, 4, 5, 6, 9, 10, 11, 12, 13)]:   # ещё десять дней сессий раньше
        store.finish_session(d, "ok", {"cost": 0.0, "transcript": [{"role": "user", "content": "контекст"}]})
    s = store.session("2026-12-01")
    long = "я" * 5000
    s["transcript"].append({"role": "assistant", "content": [{"type": "text", "text": long},
                                                             {"type": "tool_use", "name": "buy", "input": {}}]})
    store.finish_session("2026-12-01", s["status"], {"transcript": s["transcript"]})
    r = build_report(exp, market)
    by_day = {x["day"]: x for x in r["sessions"]["haiku"]}
    assert [by_day[d]["transcript"] for d in ("2026-11-02", "2026-11-03", "2026-11-04")] == [None, None, None]
    assert by_day["2026-11-05"]["transcript"] == [{"role": "user", "content": "контекст"}]
    assert by_day["2026-12-03"]["transcript"] is not None
    cut = by_day["2026-12-01"]["transcript"][-1]["content"][0]["text"]
    assert cut.startswith("я" * 2000) and len(cut) < 2100 and "обрезано" in cut
    assert [x["name"] for x in by_day["2026-12-01"]["looked"]] == ["get_account"]   # buy и прогнозы не «смотрел»

def test_report_changes_no_file_of_the_runs(played):
    exp, market = played
    def files():   # время изменения ловит и перезапись тем же содержимым
        return {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in exp.rglob("*") if p.is_file()}
    before = files()
    build_report(exp, market, {"opus-a": ["nope"]})
    assert files() == before

def test_percentile_counts_equity_equal_to_a_micro_dollar_as_a_tie(played):
    exp, market = played                  # все боты и тень продали всё по одним ценам: итог одинаков до копейки
    assert build_report(exp, market)["summary"]["haiku"]["percentile"] == 50.0


# --- исправления после просмотра ---

def test_dead_participant_shadow_takes_the_crash_of_its_death_day(played_with_death):
    exp, market = played_with_death
    r = build_report(exp, market)
    cash = RunStore(exp / "opus-b").account().cash
    # тень и счёт исполнили одни и те же сделки с теми же 5 б.п.: расходиться они могут только округлением float
    assert r["equity"]["opus-b"][-1] == pytest.approx(cash, abs=0.01)
    assert r["summary"]["opus-b"]["return_pct"] < 0


def _closes(market, symbol):
    return {b.day: b.close for b in market.bars[symbol]}


def _hand_equity(run, market):
    """Тень вручную: в движущемся мире стопы не срабатывают и продаж нет, поэтому капитал дня это деньги
    минус покупки плюс количество (сумма на цену снимка с проскальзыванием) на закрытие дня."""
    store = RunStore(run)
    cash, qty, out = 1000.0, {}, [1000.0]
    for d in [s["day"] for s in store.sessions()]:
        for e in store.orders(d):
            if e["kind"] == "buy":
                n = min(e["notional"], cash)
                cash -= n
                qty[e["symbol"]] = qty.get(e["symbol"], 0.0) + n / (store.prices(d)[e["symbol"]] * (1 + SLIPPAGE))
        out.append(cash + sum(q * _closes(market, s)[d] for s, q in qty.items()))
    return out


def test_shadow_spy_return_and_drawdown_follow_moving_prices(moving):
    exp, market = moving
    r = build_report(exp, market)
    hand = _hand_equity(exp / "sonnet", market)
    assert hand[2] != pytest.approx(hand[1])                                   # цены и правда двигались
    assert r["equity"]["sonnet"][2] == pytest.approx(hand[2]) and r["equity"]["sonnet"][-1] == pytest.approx(hand[-1])
    spy, days = _closes(market, "SPY"), r["days"]
    assert r["spy"][-1] == pytest.approx(1000 * spy[days[-1]] / spy[days[0]])
    ret = (hand[-1] / 1000 - 1) * 100
    s = r["summary"]["sonnet"]
    assert s["return_pct"] == pytest.approx(ret)
    assert s["vs_spy_pp"] == pytest.approx(ret - (spy[days[-1]] / spy[days[0]] - 1) * 100)
    dd = min((x / max(hand[:i + 1]) - 1) * 100 for i, x in enumerate(hand))
    assert dd < 0 and s["max_drawdown_pct"] == pytest.approx(dd)


def test_brier_and_reference_from_forecasts_and_bars(moving):
    exp, market = moving
    r = build_report(exp, market)
    cfg, store = load_run(exp / "haiku")[0], RunStore(exp / "haiku")
    tdays, spy = [b.day for b in market.bars["SPY"]], _closes(market, "SPY")
    given = {(f["day"], f["symbol"]): f["p"] for f in store.forecasts()}
    pairs = []
    for d in [s["day"] for s in store.sessions()]:
        i = tdays.index(d)
        if i + 5 < len(tdays):                                                   # созрел: есть закрытие через 5 дней
            end = tdays[i + 5]
            for sym in cfg.panel:
                c = _closes(market, sym)
                pairs.append((given.get((d, sym), 0.5), int(c[end] / c[d] > spy[end] / spy[d])))
    assert len(pairs) == 40                                                      # 01.12 и 02.12 по 20 бумаг
    brier, f = mean((p - o) ** 2 for p, o in pairs), mean(o for _, o in pairs)
    s = r["summary"]["haiku"]
    assert s["brier"] == pytest.approx(brier) and s["reference"] == pytest.approx(f * (1 - f))
    # интервал это «Брайер минус эталон»; при двух созревших днях блок один и интервал сжат в точку, отсюда 1e-9
    assert s["ci"][0] - 1e-9 <= brier - f * (1 - f) <= s["ci"][1] + 1e-9


def test_band_spreads_and_percentile_recounts_from_monkeys(moving):
    exp, market = moving
    r = build_report(exp, market)
    band = r["band"]["sonnet"]
    assert band["p95"][-1] > band["p5"][-1]
    cfg, store, days = load_run(exp / "sonnet")[0], RunStore(exp / "sonnet"), r["days"]
    bars = market.daily_bars(cfg.universe, 30)
    bots = replay.monkeys(store.orders(), {d: store.prices(d) for d in days}, bars, days, 1000.0,
                          list(cfg.universe), seed=cfg.seed)
    me, finals = r["equity"]["sonnet"][-1], [b[-1] for b in bots]
    below, equal = sum(x < me - 1e-6 for x in finals), sum(abs(x - me) <= 1e-6 for x in finals)
    assert r["summary"]["sonnet"]["percentile"] == pytest.approx(100 * (below + equal / 2) / len(finals))


def test_twins_numbers_and_open_position_value(moving):
    exp, market = moving
    r = build_report(exp, market)
    a, b = _hand_equity(exp / "opus-a", market), _hand_equity(exp / "opus-b", market)
    twins = r["compare"]["twins"]
    assert twins["diff_pp"] == pytest.approx((a[-1] - b[-1]) / 10)              # доходности в п.п. при старте 1000
    assert twins["common"] == 0.5                                                # XLE у обоих, XLU только у opus-b
    store, last = RunStore(exp / "haiku"), r["days"][-1]
    qty = sum(e["qty"] for e in store.orders() if e["kind"] == "buy")
    assert r["positions"]["XLE"]["haiku"] == pytest.approx(qty * store.prices(last)["XLE"])


def test_beyond_noise_compares_with_twice_the_twins(moving):
    exp, market = moving
    r = build_report(exp, market)
    ret = {p: (_hand_equity(exp / p, market)[-1] / 1000 - 1) * 100 for p in ("haiku", "sonnet", "opus-a", "opus-b")}
    noise = 2 * abs(ret["opus-a"] - ret["opus-b"])
    steps = [ret["haiku"], ret["sonnet"], (ret["opus-a"] + ret["opus-b"]) / 2]
    assert r["compare"]["ladder"]["beyond_noise"] is (max(steps) - min(steps) > noise)


def test_beyond_noise_is_none_when_twins_tie_or_one_never_bought(played):
    exp, market = played
    assert build_report(exp, market)["compare"]["ladder"]["beyond_noise"] is None        # близнецы равны
    (exp / "opus-b" / "orders.jsonl").write_text("")
    r = build_report(exp, market)
    assert r["compare"]["twins"]["diff_pp"] != 0
    assert r["compare"]["ladder"]["beyond_noise"] is None and r["compare"]["analyst"]["beyond_noise"] is None


def test_without_benchmark_bars_spy_is_not_a_number(played):
    exp, market = played
    del market.bars["SPY"]
    r = build_report(exp, market)
    assert r["spy"] is None and r["summary"]["haiku"]["vs_spy_pp"] is None


def test_stale_session_is_one_incident(played):
    exp, market = played
    store = RunStore(exp / "sonnet")
    store.finish_session("2026-12-03", "failed", {"stop": "stale", "errors": ["сессия зависла в статусе started"]})
    store.append_tick({"ts": "x", "day": "2026-12-03", "action": "stale_failed"})
    r = build_report(exp, market)
    assert len([i for i in r["incidents"] if i["participant"] == "sonnet" and i["day"] == "2026-12-03"]) == 1


def test_trades_carry_quantity_and_cost_is_float(played, tmp_path, example):
    exp, market = played
    r = build_report(exp, market)
    sold = next(e for e in RunStore(exp / "haiku").orders() if e["kind"] == "liquidate")
    assert next(t for t in r["trades"] if t["participant"] == "haiku")["qty"] == sold["qty"]
    fresh = start_experiment(example, tmp_path / "fresh")
    s = build_report(fresh, FakeMarket.with_history(list(load_run(fresh / "haiku")[0].universe), days=70))
    assert isinstance(s["summary"]["haiku"]["cost"], float)


def test_beyond_noise_needs_more_than_twice_the_twins_difference():
    from types import SimpleNamespace
    from traider.report import _compare
    ps = {pid: {"buys": [{"symbol": "XLE"}], "forecasts": [], "cfg": SimpleNamespace(panel=())}
          for pid in ("haiku", "opus-a", "opus-b", "opus-analyst")}
    summary = {pid: {"return_pct": r, "percentile": None, "brier": None}
               for pid, r in (("haiku", 2.0), ("opus-a", 1.0), ("opus-b", 0.0), ("opus-analyst", 3.0))}
    c = _compare(ps, summary, {})              # шум близнецов 1 п.п.: порог 2 п.п.
    assert c["ladder"]["beyond_noise"] is False   # разброс 1.5 п.п.: больше шума, но не вдвое
    assert c["analyst"]["beyond_noise"] is True   # 3.0 против среднего 0.5: 2.5 п.п.


def test_intraday_death_after_the_snapshot_takes_the_crash(intraday_death):
    exp, market = intraday_death
    store = RunStore(exp / "opus-b")
    assert store.state()["died_on"] == "2026-12-02" and store.prices("2026-12-02")      # снимок дня уже был
    r = build_report(exp, market)
    # тень и счёт исполнили одни и те же сделки с теми же 5 б.п.: расходиться они могут только округлением float
    assert r["equity"]["opus-b"][-1] == pytest.approx(store.account().cash, abs=0.01)
    assert r["summary"]["opus-b"]["return_pct"] < -20
    assert r["summary"]["opus-b"]["percentile"] != 50.0


def test_beyond_noise_float_noise_twins_and_exactly_twice():
    from types import SimpleNamespace
    from traider.report import _compare
    ps = {pid: {"buys": [{"symbol": "XLE"}], "forecasts": [], "cfg": SimpleNamespace(panel=())}
          for pid in ("haiku", "opus-a", "opus-b", "opus-analyst")}

    def run(a, b, haiku, analyst):
        summary = {pid: {"return_pct": r, "percentile": None, "brier": None}
                   for pid, r in (("haiku", haiku), ("opus-a", a), ("opus-b", b), ("opus-analyst", analyst))}
        return _compare(ps, summary, {})
    c = run(1.0, 1.0 + 1e-12, 5.0, 5.0)                        # разница близнецов это шум float: мерить нечем
    assert c["ladder"]["beyond_noise"] is None and c["analyst"]["beyond_noise"] is None
    c = run(1.0, 0.0, 2.5, 2.5)                                # шум 1 п.п., разброс и разница ровно 2 п.п.
    assert c["ladder"]["beyond_noise"] is False and c["analyst"]["beyond_noise"] is False


def test_brier_series_is_cumulative_and_ends_at_the_summary(moving):
    exp, market = moving
    r = build_report(exp, market)
    assert r["days"] == ["2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"]
    for pid, series in r["brier_series"].items():
        assert len(series) == len(r["days"])
        # точка дня: прогнозы панели, данные до этого дня включительно и уже созревшие; 01.12 и 02.12 созрели
        assert [x and x["n"] for x in series] == [20, 40, 40, 40]
        last = [x for x in series if x][-1]
        assert last["brier"] == pytest.approx(r["summary"][pid]["brier"])
        assert last["reference"] == pytest.approx(r["summary"][pid]["reference"])


def test_brier_series_is_empty_before_any_forecast_matures(played):
    exp, market = played
    r = build_report(exp, market)                       # прогнозы 01–03.12 при отчёте 03.12: ни один не созрел
    assert all(x is None for s in r["brier_series"].values() for x in s)
    assert all(r["summary"][p]["brier"] is None for p in r["brier_series"])


def test_money_block_from_session_costs_and_the_month_calendar(moving):
    exp, market = moving
    m = build_report(exp, market)["money"]
    per_day = 0.0008 + 0.016 + 0.032 + 0.032 + 0.0328   # haiku, sonnet, opus-a, opus-b, opus-analyst за сессию
    assert m["month"] == "2026-12"
    assert m["month_spend_usd"] == pytest.approx(4 * per_day) and m["run_spend_usd"] == pytest.approx(4 * per_day)
    assert m["month_forecast_usd"] == pytest.approx(per_day * 23)   # будни декабря 2026: 4 + 5 + 5 + 5 + 4


def test_money_block_of_a_run_that_has_not_started(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    m = build_report(exp, FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70))["money"]
    assert m == {"month": None, "month_spend_usd": None, "run_spend_usd": 0.0, "month_forecast_usd": None}


def test_death_incident_does_not_repeat_its_title(played_with_death):
    exp, market = played_with_death
    died = [i for i in build_report(exp, market)["incidents"] if i["title"].startswith("гибель")]
    assert died and all(i["title"] not in i["text"] for i in died)
