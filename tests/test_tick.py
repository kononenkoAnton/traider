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

def test_tick_log_write_failure_of_any_kind_does_not_stop_the_tick(world, monkeypatch):
    runs, market, mb, mc, _ = world
    real, first = RunStore.append_tick, []
    def broken(self, rec):
        first.append(self.dir) if not first else None
        if self.dir == first[0]: raise TypeError("не сериализуется")
        return real(self, rec)
    monkeypatch.setattr(RunStore, "append_tick", broken)
    got = tick(runs, market, mb, mc)
    assert len(got) == 5 and [r["action"] for r in got].count("session") == 5
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


# --- финальная волна, C1: один счёт Alpaca на один прогон ---

def _second_experiment(example, name="official"):
    toml = example.parent / f"{name}.toml"
    toml.write_text(example.read_text().replace('experiment = "pilot-1"', f'experiment = "{name}"'))
    return toml

def test_start_refuses_a_second_alpaca_run_of_the_same_mode(tmp_path, example):
    runs = tmp_path / "runs"; start_experiment(example, runs)
    second = _second_experiment(example)
    with pytest.raises(ValueError, match="pilot-1/opus-a"):
        start_experiment(second, runs)
    assert not (runs / "official").exists()
    RunStore(runs / "pilot-1" / "opus-a").update_state(status="finished")
    assert start_experiment(second, runs) == runs / "official"

def test_two_alpaca_runs_of_one_mode_both_get_an_error_and_no_broker(world, example):
    runs, market, _, mc, _ = world
    pilot = RunStore(runs / "pilot-1" / "opus-a")
    pilot.update_state(status="finished"); start_experiment(_second_experiment(example), runs)
    pilot.update_state(status="finishing", finish_day="2026-11-30")    # пилот снова не завершён
    built = []
    def broker(cfg, store):
        built.append(f"{cfg.experiment}/{cfg.participant}"); return SimBroker(cfg, store, market)
    got = tick(runs, market, broker, mc)
    errors = [r for r in got if r["action"] == "error"]
    assert [r["participant"] for r in errors] == ["opus-a", "opus-a"]
    assert all("pilot-1/opus-a" in r["error"] and "official/opus-a" in r["error"] and "ничего не отправлено" in r["error"]
               for r in errors)
    assert not any(b.endswith("/opus-a") for b in built)
    assert [r["action"] for r in got].count("session") == 8


# --- финальная волна, I6.3: ticks.jsonl ---

def test_every_run_gets_a_tick_line_including_errors(world):
    runs, market, _, mc, _ = world
    def flaky(cfg, store):
        if cfg.participant == "sonnet": raise RuntimeError("брокер недоступен")
        return SimBroker(cfg, store, market)
    got = tick(runs, market, flaky, mc)
    assert all(r["experiment"] == "pilot-1" and r["ts"] == market.clock.isoformat() for r in got)
    (err,) = RunStore(runs / "pilot-1" / "sonnet").ticks()
    assert (err["action"], err["day"], err["equity"], err["unpriced"]) == ("error", "2026-12-01", None, None)
    assert "брокер недоступен" in err["error"] and err["ts"] == market.clock.isoformat()
    (ses,) = RunStore(runs / "pilot-1" / "haiku").ticks()
    assert ses["action"] == "session" and ses["status"] == "ok" and ses["cost"] == 0.0 and ses["error"] is None
    assert ses["equity"] == 1000.0 and ses["floor"] == 850.0 and ses["hp"] == 150.0
    assert ses["stops_placed"] == [] and ses["stop_fills"] == [] and ses["unpriced"] == []
    market.clock = ny(2026, 12, 1, 10, 20); tick(runs, market, flaky, mc)
    assert [t["action"] for t in RunStore(runs / "pilot-1" / "haiku").ticks()] == ["session", "safety"]

def test_tick_line_writes_non_finite_numbers_as_null(tmp_path):
    store = RunStore(tmp_path)
    store.append_tick({"ts": "t", "equity": float("nan"), "hp": float("inf"), "floor": 850.0})
    assert store.ticks() == [{"ts": "t", "equity": None, "hp": None, "floor": 850.0}]


# --- задача 6: пауза и остановка ---

from traider.control import pause, request_stop, resume  # noqa: E402


def test_paused_experiment_keeps_safety_and_runs_no_session(world):
    runs, market, mb, mc, clients = world
    pause(runs / "pilot-1", "2026-12-01")
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"paused"} and clients == []
    assert RunStore(runs / "pilot-1" / "haiku").session("2026-12-01")["status"] == "paused"

def test_days_inside_a_pause_are_backfilled_as_paused_not_missed(world):
    runs, market, mb, mc, _ = world
    tick(runs, market, mb, mc)                                                # 01.12 прошла
    pause(runs / "pilot-1", "2026-12-02"); resume(runs / "pilot-1", "2026-12-03")
    market.clock = ny(2026, 12, 4, 10, 5)                                     # машина была выключена 02.12 и 03.12
    tick(runs, market, mb, mc)
    store = RunStore(runs / "pilot-1" / "haiku")
    assert [store.session(d)["status"] for d in ("2026-12-02", "2026-12-03", "2026-12-04")] == ["paused", "paused", "ok"]

def test_paused_days_do_not_use_up_the_session_target(world):                 # в примере sessions_target = 3
    runs, market, mb, mc, _ = world
    for day in (1, 2):
        market.clock = ny(2026, 12, day, 10, 5); tick(runs, market, mb, mc)
    pause(runs / "pilot-1", "2026-12-03"); market.clock = ny(2026, 12, 3, 10, 5); tick(runs, market, mb, mc)
    resume(runs / "pilot-1", "2026-12-03"); market.clock = ny(2026, 12, 4, 10, 5); tick(runs, market, mb, mc)
    store = RunStore(runs / "pilot-1" / "haiku")
    assert store.session("2026-12-04")["status"] == "ok" and store.state()["status"] == "finishing"   # третья сессия, а не четвёртая

def test_stop_request_sells_everything_and_ends_the_runs(world):
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 100.0, market.prices["AAPL"] * 0.9, "т", "seed/1")
    request_stop(runs / "pilot-1", "2026-12-01", market.clock)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"finishing"}
    state = RunStore(run).state()
    assert state["status"] == "finishing" and state["stopped_by"] == "owner" and SimBroker(cfg, RunStore(run), market).account().positions == ()


# --- задача 6, сверх задания ---

def test_a_stop_below_the_market_still_fills_while_paused(world):
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 100.0, market.prices["AAPL"] * 0.9, "т", "seed/1")
    pause(runs / "pilot-1", "2026-12-01")
    low = market.prices["AAPL"] * 0.85
    market.prices["AAPL"], market.lows["AAPL"] = low, (low, low)               # свеча ниже стопа
    got = {r["participant"]: r for r in tick(runs, market, mb, mc)}
    assert got["opus-b"]["action"] == "paused" and got["opus-b"]["stop_fills"] == ["AAPL"]
    assert SimBroker(cfg, RunStore(run), market).account().positions == ()

def test_a_run_below_its_floor_still_dies_while_paused(world):
    runs, market, mb, mc, clients = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 1000.0, 1.0, "т", "seed/1")
    market.prices["AAPL"] *= 0.8
    pause(runs / "pilot-1", "2026-12-01")
    got = actions(tick(runs, market, mb, mc))
    assert got["opus-b"] == "died" and RunStore(run).state()["status"] == "dead" and clients == []

def test_a_session_recorded_before_the_pause_is_kept_and_not_doubled(world):
    runs, market, mb, mc, clients = world
    tick(runs, market, mb, mc)
    store = RunStore(runs / "pilot-1" / "haiku"); before = store.session("2026-12-01")
    pause(runs / "pilot-1", "2026-12-01")
    market.clock = ny(2026, 12, 1, 10, 20)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"safety"} and len(clients) == 6
    assert store.session("2026-12-01") == before and store.sessions() == [before]

def test_a_paused_record_is_created_once_per_day(world):
    runs, market, mb, mc, _ = world
    pause(runs / "pilot-1", "2026-12-01"); tick(runs, market, mb, mc)
    market.clock = ny(2026, 12, 1, 15, 45)                                     # и после окна: пауза, не пропуск
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"safety"}
    assert [s["status"] for s in RunStore(runs / "pilot-1" / "haiku").sessions()] == ["paused"]

def test_a_paused_day_after_the_window_is_paused_not_missed(world):
    runs, market, mb, mc, _ = world
    pause(runs / "pilot-1", "2026-12-01"); market.clock = ny(2026, 12, 1, 15, 45)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"paused"}

def test_backfill_outside_the_pause_is_still_missed(world):
    runs, market, mb, mc, _ = world
    tick(runs, market, mb, mc)
    pause(runs / "pilot-1", "2026-12-03")
    market.clock = ny(2026, 12, 4, 10, 5)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"paused"}
    store = RunStore(runs / "pilot-1" / "haiku")
    assert [store.session(d)["status"] for d in ("2026-12-02", "2026-12-03", "2026-12-04")] == ["missed", "paused", "paused"]

def test_a_stop_request_while_paused_ends_the_runs(world):
    runs, market, mb, mc, clients = world
    pause(runs / "pilot-1", "2026-12-01"); tick(runs, market, mb, mc)
    market.clock = ny(2026, 12, 2, 10, 5); request_stop(runs / "pilot-1", "2026-12-02", market.clock)
    assert set(actions(tick(runs, market, mb, mc)).values()) == {"finishing"} and clients == []
    state = RunStore(runs / "pilot-1" / "haiku").state()
    assert (state["status"], state["finish_day"], state["stopped_by"]) == ("finishing", "2026-12-02", "owner")
    assert RunStore(runs / "pilot-1" / "haiku").session("2026-12-02") is None

def test_a_stop_request_leaves_invalid_dead_finished_and_finishing_runs_alone(world):
    runs, market, mb, mc, _ = world
    _edit_config(runs, "haiku", '"seed": 20261114', '"seed": 1')                          # invalid
    RunStore(runs / "pilot-1" / "sonnet").update_state(status="dead", died_on="2026-11-30")
    RunStore(runs / "pilot-1" / "opus-a").update_state(status="finished")
    RunStore(runs / "pilot-1" / "opus-b").update_state(status="finishing", finish_day="2026-11-27")
    request_stop(runs / "pilot-1", "2026-12-01", market.clock)
    got = actions(tick(runs, market, mb, mc))
    assert got == {"haiku": "invalid", "opus-b": "finishing", "opus-analyst": "finishing"}
    states = {p: RunStore(runs / "pilot-1" / p).state() for p in ("haiku", "sonnet", "opus-a", "opus-b")}
    assert [states[p]["status"] for p in states] == ["invalid", "dead", "finished", "finishing"]
    assert not any("stopped_by" in s for s in states.values()) and states["opus-b"]["finish_day"] == "2026-11-27"

from tests.test_control import BAD_DATES  # noqa: E402


@pytest.mark.parametrize("text", BAD_DATES + ["не json", '{"pauses": "x", "stop": null}'])
def test_a_damaged_control_file_means_no_pause_no_stop_and_is_reported(world, text):
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 100.0, market.prices["AAPL"] * 0.9, "т", "seed/1")
    (runs / "pilot-1" / "control.json").write_text(text)
    got = tick(runs, market, mb, mc)
    assert {r["action"] for r in got} == {"session"} and all("control.json" in r["control_error"] for r in got)
    assert all("error" not in r for r in got)                                  # своя ошибка прогона отдельно
    assert "control.json" in RunStore(runs / "pilot-1" / "haiku").ticks()[-1]["control_error"]
    assert all(RunStore(runs / "pilot-1" / p).state()["status"] == "active" for p in actions(got))
    assert [p.symbol for p in SimBroker(cfg, RunStore(run), market).account().positions] == ["AAPL"]   # ничего не продано
    assert not any(e["kind"] == "liquidate" for e in RunStore(run).orders())
    assert (runs / "pilot-1" / "control.json").read_text() == text

def test_a_death_on_a_day_with_a_damaged_control_file_keeps_its_own_text(world):
    from traider.report import build_report
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 1000.0, 1.0, "т", "seed/1")
    market.prices["AAPL"] *= 0.8
    (runs / "pilot-1" / "control.json").write_text("не json")
    assert actions(tick(runs, market, mb, mc))["opus-b"] == "died"
    inc = build_report(runs / "pilot-1", market)["incidents"]
    died = [i for i in inc if i["title"].startswith("гибель")]
    assert died and all("control.json" not in i["text"] for i in died)
    ctl = [i for i in inc if i["title"] == "файл управления не читается" and i["day"] == "2026-12-01"]
    assert len(ctl) == 1 and ctl[0]["participant"] is None and "control.json" in ctl[0]["text"]

def test_the_tick_never_writes_the_control_file(world, monkeypatch):
    runs, market, mb, mc, _ = world
    exp = runs / "pilot-1"
    tick(runs, market, mb, mc)
    assert not (exp / "control.json").exists()
    pause(exp, "2026-12-02"); before = (exp / "control.json").read_bytes()
    market.clock = ny(2026, 12, 2, 10, 5); tick(runs, market, mb, mc)
    assert (exp / "control.json").read_bytes() == before
    request_stop(exp, "2026-12-03", market.clock); before = (exp / "control.json").read_bytes()
    market.clock = ny(2026, 12, 3, 10, 5); tick(runs, market, mb, mc)
    assert (exp / "control.json").read_bytes() == before

def test_the_control_file_is_read_again_before_each_run(world, monkeypatch):
    runs, market, mb, mc, _ = world
    seen = []
    real = tick_module.read_control
    monkeypatch.setattr(tick_module, "read_control", lambda exp: seen.append(exp.name) or real(exp))
    tick(runs, market, mb, mc)
    assert seen == ["pilot-1"] * 5


def _pressing(world, press):
    """Клиент, который в первой сессии тика зовёт `press`: владелец нажал кнопку, пока сессия идёт."""
    runs, market, mb, mc, clients = world
    def make_client():
        if not clients:
            press()
        return mc()
    return make_client


def test_a_stop_pressed_during_a_session_applies_to_the_runs_not_yet_processed(world):
    runs, market, mb, mc, clients = world
    got = tick(runs, market, mb, _pressing(world, lambda: request_stop(runs / "pilot-1", "2026-12-01", market.clock)))
    first, rest = got[0], got[1:]
    assert first["action"] == "session" and {r["action"] for r in rest} == {"finishing"}
    assert RunStore(runs / "pilot-1" / first["participant"]).state()["status"] == "active"   # её продаст следующий тик
    market.clock = ny(2026, 12, 1, 10, 20)
    assert actions(tick(runs, market, mb, mc))[first["participant"]] == "finishing"


def test_a_pause_pressed_during_a_tick_waits_for_the_next_tick(world):
    runs, market, mb, mc, _ = world
    got = tick(runs, market, mb, _pressing(world, lambda: pause(runs / "pilot-1", "2026-12-01")))
    assert {r["action"] for r in got} == {"session"}                      # день один для всех участников


def test_a_control_file_broken_during_a_tick_leaves_the_first_read_and_is_reported(world):
    runs, market, mb, mc, _ = world
    exp = runs / "pilot-1"
    got = tick(runs, market, mb, _pressing(world, lambda: (exp / "control.json").write_text("не json")))
    assert {r["action"] for r in got} == {"session"}
    assert "control_error" not in got[0] and all("control.json" in r["control_error"] for r in got[1:])

def test_a_paused_day_between_sessions_does_not_bring_the_last_session_closer(world):
    runs, market, mb, mc, _ = world
    tick(runs, market, mb, mc)
    pause(runs / "pilot-1", "2026-12-02"); market.clock = ny(2026, 12, 2, 10, 5); tick(runs, market, mb, mc)
    resume(runs / "pilot-1", "2026-12-02"); market.clock = ny(2026, 12, 3, 10, 5); tick(runs, market, mb, mc)
    assert RunStore(runs / "pilot-1" / "haiku").state()["status"] == "active"           # две сессии из трёх


# --- волна после проверки всей ветки ---

def test_a_stop_waits_while_a_holding_has_no_quote_and_applies_on_the_next_priced_tick(world):
    runs, market, mb, mc, clients = world
    run = runs / "pilot-1" / "opus-b"; cfg, _ = load_run(run)
    SimBroker(cfg, RunStore(run), market).buy("2026-11-30", "AAPL", 100.0, market.prices["AAPL"] * 0.9, "т", "seed/1")
    request_stop(runs / "pilot-1", "2026-12-01", ny(2026, 12, 1, 9, 0))
    quote = market.prices.pop("AAPL")                                        # бумага сегодня ещё не торговалась
    market.clock = ny(2026, 12, 1, 9, 31)
    got = {r["participant"]: r for r in tick(runs, market, mb, mc)}
    assert got["opus-b"]["action"] == "unpriced" and got["opus-b"]["unpriced"] == ["AAPL"]
    state = RunStore(run).state()
    assert state["status"] == "active" and "stopped_by" not in state and "finish_day" not in state
    assert not any(e["kind"] == "liquidate" for e in RunStore(run).orders())
    assert {got[p]["action"] for p in got if p != "opus-b"} == {"finishing"} and clients == []
    market.prices["AAPL"] = quote * 0.8
    market.clock = ny(2026, 12, 1, 9, 46)
    assert actions(tick(runs, market, mb, mc))["opus-b"] == "finishing"
    sold = [e for e in RunStore(run).orders() if e["kind"] == "liquidate"]
    assert [(e["reason"], round(e["price"], 6)) for e in sold] == [("liquidate", round(quote * 0.8 * 0.9995, 6))]
    assert RunStore(run).state()["stopped_by"] == "owner" and RunStore(run).state()["finish_day"] == "2026-12-01"


def test_a_sale_at_the_entry_price_is_a_serious_incident(world):
    from traider.report import build_report
    runs, market, mb, mc, _ = world
    run = runs / "pilot-1" / "haiku"; cfg, _ = load_run(run); store = RunStore(run)
    SimBroker(cfg, store, market).buy("2026-11-30", "AAPL", 100.0, 1.0, "т", "seed/1")
    store.update_state(status="finishing", finish_day="2026-11-30")
    del market.prices["AAPL"]
    tick(runs, market, mb, mc)                                                 # остаток продаётся по цене входа
    assert [e["reason"] for e in store.orders() if e["kind"] == "liquidate"] == ["no_quote_entry_price"]
    inc = [i for i in build_report(runs / "pilot-1", market)["incidents"] if i["participant"] == "haiku"
           and i["title"] == "продано по цене входа: котировки не было"]
    assert [(i["kind"], i["day"]) for i in inc] == [("serious", "2026-12-01")] and "AAPL" in inc[0]["text"]
