import pytest

from tests.fakes import FakeMarket
from traider.config import load_run, start_experiment
from traider.report import build_report, integrity, verdict, worst
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
