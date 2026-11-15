import json
from datetime import datetime, timedelta

import pytest

from tests.fakes import FakeAnthropic, FakeMarket, ny, use
from traider import tick as tick_module
from traider.brokers import SimBroker
from traider.config import load_run, start_experiment
from traider.store import RunStore
from traider.tick import tick


@pytest.fixture
def world(tmp_path, example):
    runs = tmp_path / "runs"; start_experiment(example, runs)
    market = FakeMarket.with_history(list(load_run(runs / "pilot-1" / "haiku")[0].universe), days=70)
    market.clock = ny(2026, 12, 1, 10, 5); market.open = True
    clients = []
    def make_client():
        clients.append(FakeAnthropic([use("finish_session", journal="ок")] * 20)); return clients[-1]
    return runs, market, (lambda cfg, store: SimBroker(cfg, store, market)), make_client, clients

def actions(result): return {r["participant"]: r["action"] for r in result}

def test_closed_market_does_nothing(world):
    runs, market, mb, mc, _ = world; market.open = False
    assert tick(runs, market, mb, mc) == []

def test_before_the_window_only_safety_runs(world):
    runs, market, mb, mc, clients = world; market.clock = ny(2026, 12, 1, 9, 45)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"safety"} and clients == []

def test_each_participant_gets_one_session_per_day(world):
    runs, market, mb, mc, _ = world
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"session"}
    market.clock = ny(2026, 12, 1, 10, 20)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"safety"}
    assert RunStore(runs / "pilot-1" / "haiku").session("2026-12-01")["status"] == "ok"

def test_window_passed_without_a_session_is_a_missed_day(world):
    runs, market, mb, mc, clients = world; market.clock = ny(2026, 12, 1, 15, 31)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"missed"} and clients == []
    assert RunStore(runs / "pilot-1" / "sonnet").session("2026-12-01")["status"] == "missed"

def test_days_with_no_tick_at_all_are_recorded_as_missed_later(world):
    runs, market, mb, mc, _ = world
    tick(runs, market, mb, mc)                                                    # 01.12 прошла
    market.clock = ny(2026, 12, 4, 10, 5)                                         # машина была выключена 02.12 и 03.12
    tick(runs, market, mb, mc)
    store = RunStore(runs / "pilot-1" / "haiku")
    assert [store.session(d)["status"] for d in ("2026-12-02", "2026-12-03", "2026-12-04")] == ["missed", "missed", "ok"]

def test_short_trading_day_still_gets_its_session(world):
    runs, market, mb, mc, _ = world; market.clock = ny(2026, 11, 27, 12, 40)      # рынок закроется в 13:00
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"session"}

def test_stale_started_session_fails_and_is_not_rerun(world):
    runs, market, mb, mc, clients = world
    RunStore(runs / "pilot-1" / "opus-b").start_session("2026-12-01", market.clock - timedelta(minutes=40))
    got = actions(tick(runs, market, mb, mc))
    assert got["opus-b"] == "stale_failed" and RunStore(runs / "pilot-1" / "opus-b").session("2026-12-01")["status"] == "failed"

def test_one_broken_participant_does_not_stop_the_rest(world):
    runs, market, mb, mc, _ = world
    def flaky(cfg, store):
        if cfg.participant == "sonnet": raise RuntimeError("брокер недоступен")
        return SimBroker(cfg, store, market)
    got = actions(tick(runs, market, flaky, mc))
    assert got["sonnet"] == "error" and got["haiku"] == "session"

def test_tampered_config_keeps_safety_but_stops_sessions(world):
    runs, market, mb, mc, _ = world
    path = runs / "pilot-1" / "haiku" / "config.json"; path.write_text(path.read_text().replace('"seed": 20261114', '"seed": 1'))
    assert actions(tick(runs, market, mb, mc))["haiku"] == "invalid"
    assert RunStore(runs / "pilot-1" / "haiku").state()["status"] == "invalid" and RunStore(runs / "pilot-1" / "haiku").session("2026-12-01") is None

def test_lock_blocks_a_second_tick_and_expires(world):
    runs, market, mb, mc, _ = world
    (runs / ".tick.lock").write_text(json.dumps({"pid": 1, "ts": market.clock.isoformat()}))
    assert tick(runs, market, mb, mc) == []
    (runs / ".tick.lock").write_text(json.dumps({"pid": 1, "ts": (market.clock - timedelta(minutes=61)).isoformat()}))
    assert len(tick(runs, market, mb, mc)) == 5 and not (runs / ".tick.lock").exists()

def test_dead_run_is_reported_once_and_then_skipped(world):
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 1000.0, 1.0, "т", "seed/1")
    market.prices["AAPL"] *= 0.8                                                  # капитал около 800 при поле 850
    assert actions(tick(runs, market, mb, mc))["opus-b"] == "died"
    assert "opus-b" not in actions(tick(runs, market, mb, mc))

def test_invalid_run_keeps_its_safety_check_on_later_ticks(world):
    runs, market, mb, mc, _ = world
    path = runs / "pilot-1" / "haiku" / "config.json"; path.write_text(path.read_text().replace('"seed": 20261114', '"seed": 1'))
    tick(runs, market, mb, mc)
    market.clock = ny(2026, 12, 1, 10, 20)
    assert actions(tick(runs, market, mb, mc))["haiku"] == "invalid"              # прогон по-прежнему посещается

def test_unpriced_position_blocks_the_session(world):
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 100.0, 1.0, "т", "seed/1")
    del market.prices["AAPL"]
    assert actions(tick(runs, market, mb, mc))["opus-b"] == "unpriced"
    assert RunStore(run).session("2026-12-01") is None

def test_finishing_run_becomes_finished_after_five_trading_days(world):
    runs, market, mb, mc, _ = world
    store = RunStore(runs / "pilot-1" / "haiku"); store.update_state(status="finishing", finish_day="2026-12-01")
    market.clock = ny(2026, 12, 7, 10, 5)
    assert actions(tick(runs, market, mb, mc))["haiku"] == "finishing"
    market.clock = ny(2026, 12, 8, 10, 5)
    assert actions(tick(runs, market, mb, mc))["haiku"] == "finished"

def test_order_of_participants_rotates_by_day(world):
    runs, market, mb, mc, _ = world
    first = [r["participant"] for r in tick(runs, market, mb, mc)]
    market.clock = ny(2026, 12, 2, 10, 5)
    assert [r["participant"] for r in tick(runs, market, mb, mc)] == first[1:] + first[:1]


# --- сверх задания ---

def test_lock_timestamp_is_refreshed_before_each_run(world):
    runs, market, _, mc, _ = world
    seen = []
    def broker(cfg, store):
        seen.append(datetime.fromisoformat(json.loads((runs / ".tick.lock").read_text())["ts"]))
        market.clock += timedelta(minutes=1)          # прогон «работает» минуту
        return SimBroker(cfg, store, market)
    tick(runs, market, broker, mc)
    assert len(seen) == 5 and seen == sorted(set(seen))   # каждый прогон видит более свежую метку

def test_unreadable_lock_is_taken_over(world):
    runs, market, mb, mc, _ = world
    (runs / ".tick.lock").write_text("не json")
    assert len(tick(runs, market, mb, mc)) == 5 and not (runs / ".tick.lock").exists()

def test_lock_is_removed_even_when_the_tick_crashes(world):
    runs, market, mb, mc, _ = world
    def boom(*a): raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        tick(runs, market, boom, mc)
    assert not (runs / ".tick.lock").exists()

def test_finishing_run_with_a_position_is_liquidated_and_priced(world):
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "haiku"; cfg, _ = load_run(run); store = RunStore(run)
    SimBroker(cfg, store, market).buy("2026-11-30", "AAPL", 100.0, 1.0, "т", "seed/1")
    store.update_state(status="finishing", finish_day="2026-12-01")
    market.clock = ny(2026, 12, 2, 10, 5)
    assert actions(tick(runs, market, mb, mc))["haiku"] == "finishing"
    assert SimBroker(cfg, store, market).account().positions == ()
    assert "AAPL" in store.prices("2026-12-02")

def test_exception_from_run_session_is_an_error_and_others_still_run(world, monkeypatch):
    runs, market, mb, mc, _ = world
    real = tick_module.run_session
    def flaky(cfg, *a, **kw):
        if cfg.participant == "sonnet": raise OSError("диск полон")
        return real(cfg, *a, **kw)
    monkeypatch.setattr(tick_module, "run_session", flaky)
    got = tick(runs, market, mb, mc)
    by = {r["participant"]: r for r in got}
    assert by["sonnet"]["action"] == "error" and "диск полон" in by["sonnet"]["error"]
    assert [r["action"] for r in got].count("session") == 4

def test_analyst_participant_gets_an_analyst_on_a_second_client(world):
    runs, market, mb, mc, clients = world
    got = {r["participant"]: r for r in tick(runs, market, mb, mc)}
    assert len(clients) == 6                                   # по клиенту на сессию и один на аналитика
    rec = RunStore(runs / "pilot-1" / "opus-analyst").session("2026-12-01")
    assert rec["analyst"] is not None and RunStore(runs / "pilot-1" / "haiku").session("2026-12-01")["analyst"] is None
    assert got["opus-analyst"]["status"] == rec["status"] and "cost" in got["opus-analyst"]

def test_started_record_blocks_a_second_start(world):
    runs, market, mb, mc, clients = world
    RunStore(runs / "pilot-1" / "haiku").start_session("2026-12-01", market.clock)      # свежая, не зависшая
    assert actions(tick(runs, market, mb, mc))["haiku"] == "safety"


# --- раунд исправлений 1 ---

def _edit_config(runs, name, old, new):
    path = runs / "pilot-1" / name / "config.json"
    assert old in path.read_text()
    path.write_text(path.read_text().replace(old, new))

def _guarded_tick(world, name, old, new):
    runs, market, _, mc, _ = world
    _edit_config(runs, name, old, new)
    built = []
    def broker(cfg, store):
        built.append(cfg.participant); return SimBroker(cfg, store, market)
    got = tick(runs, market, broker, mc)
    by = {r["participant"]: r for r in got}
    assert name not in built and by[name]["action"] == "error" and "вручную" in by[name]["error"]
    assert RunStore(runs / "pilot-1" / name).state()["status"] == "invalid"
    assert sorted(r["action"] for r in got).count("session") == 4
    assert RunStore(runs / "pilot-1" / name).session("2026-12-01") is None
    market.clock = ny(2026, 12, 1, 10, 20)                      # следующие тики делают то же
    assert actions(tick(runs, market, broker, mc))[name] == "error" and name not in built

def test_paper_run_edited_to_live_never_gets_a_broker(world):
    _guarded_tick(world, "opus-a", '"mode": "paper"', '"mode": "live"')

def test_sim_run_edited_to_alpaca_never_gets_a_broker(world):
    _guarded_tick(world, "haiku", '"broker": "sim"', '"broker": "alpaca"')

def test_run_without_the_mode_and_broker_record_never_gets_a_broker(world):
    runs = world[0]
    RunStore(runs / "pilot-1" / "sonnet").update_state(started_with=None)
    _guarded_tick(world, "sonnet", '"seed": 20261114', '"seed": 20261114')

def test_start_experiment_records_mode_and_broker(tmp_path, example):
    paper = start_experiment(example, tmp_path / "p")
    assert RunStore(paper / "opus-a").state()["started_with"] == {"mode": "paper", "broker": "alpaca"}
    assert RunStore(paper / "haiku").state()["started_with"] == {"mode": "paper", "broker": "sim"}

def test_live_start_records_live(tmp_path, example_live):
    live = start_experiment(example_live, tmp_path / "l", live=True)
    assert all(RunStore(r).state()["started_with"]["mode"] == "live" for r in live.iterdir())

def test_foreign_lock_stops_the_tick_and_is_left_alone(world):
    runs, market, _, mc, _ = world
    lock, calls = runs / ".tick.lock", []
    foreign = json.dumps({"pid": 7, "ts": market.clock.isoformat(), "token": "other"})
    def broker(cfg, store):
        calls.append(cfg.participant); lock.write_text(foreign)       # другой тик забрал замок
        return SimBroker(cfg, store, market)
    got = tick(runs, market, broker, mc)
    assert len(got) == 1 and len(calls) == 1 and lock.read_text() == foreign

def test_fresh_foreign_lock_survives_a_tick_that_returned_early(world):
    runs, market, mb, mc, _ = world
    (runs / ".tick.lock").write_text(json.dumps({"pid": 1, "ts": market.clock.isoformat(), "token": "other"}))
    assert tick(runs, market, mb, mc) == [] and (runs / ".tick.lock").exists()

def test_takeover_of_a_vanished_lock_loses_quietly(world, monkeypatch):
    runs, market, mb, mc, _ = world
    (runs / ".tick.lock").write_text("мусор")
    real = tick_module.os.rename
    def gone(src, dst): (runs / ".tick.lock").unlink(); return real(src, dst)   # другой тик успел раньше
    monkeypatch.setattr(tick_module.os, "rename", gone)
    assert tick(runs, market, mb, mc) == []

def test_run_with_an_unknown_status_gets_no_session_and_no_bookkeeping(world):
    runs, market, mb, mc, clients = world
    RunStore(runs / "pilot-1" / "haiku").update_state(status="weird")
    market.clock = ny(2026, 12, 3, 10, 5)
    assert actions(tick(runs, market, mb, mc))["haiku"] == "safety"
    assert RunStore(runs / "pilot-1" / "haiku").sessions() == []
