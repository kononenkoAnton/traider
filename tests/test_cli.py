import json
import shutil
import signal
import socket
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from traider import cli
from traider.brokers import SimBroker
from traider.cli import main
from traider.config import load_run, start_experiment
from traider.store import RunStore


def test_start_and_status(tmp_path, example, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["start", str(example)]) == 0 and (tmp_path / "runs" / "pilot-1" / "opus-a" / "config.json").exists()
    assert main(["status"]) == 0
    out = capsys.readouterr().out
    assert all(name in out for name in ["haiku", "sonnet", "opus-a", "opus-b", "opus-analyst"]) and "active" in out

def test_live_start_needs_the_flag(tmp_path, example_live, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["start", str(example_live)]) == 2


# --- сверх задания ---

def test_live_start_with_the_flag_creates_live_runs(tmp_path, example_live, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["start", str(example_live), "--live"]) == 0
    assert load_run(tmp_path / "runs" / "pilot-1" / "haiku")[0].mode == "live"

def test_start_prints_the_created_run_dirs_and_refuses_a_repeat(tmp_path, example, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["start", str(example)]) == 0
    assert capsys.readouterr().out.count("runs/pilot-1/") == 5
    assert main(["start", str(example)]) == 1

def test_status_line_shows_progress_last_session_and_cost(tmp_path, example, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["start", str(example)]); capsys.readouterr()
    store = RunStore(tmp_path / "runs" / "pilot-1" / "haiku")
    now = datetime(2026, 12, 1, 15, tzinfo=timezone.utc)
    store.start_session("2026-12-01", now)
    store.finish_session("2026-12-01", "ok", {"cost": 0.25})
    store.start_session("2026-12-02", now); store.finish_session("2026-12-02", "cap", {"cost": 0.5})
    main(["status"])
    line = next(l for l in capsys.readouterr().out.splitlines() if "haiku" in l)
    assert "2/3" in line and "2026-12-02 cap" in line and "$0.75" in line

def test_status_flags_a_tampered_config(tmp_path, example, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    main(["start", str(example)]); capsys.readouterr()
    path = tmp_path / "runs" / "pilot-1" / "haiku" / "config.json"; path.write_text(path.read_text().replace('"seed": 20261114', '"seed": 1'))
    main(["status"])
    assert "контрольная сумма" in next(l for l in capsys.readouterr().out.splitlines() if "haiku" in l)

def test_status_with_no_runs_is_quiet(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["status"]) == 0 and "нет прогонов" in capsys.readouterr().out

def test_tick_wiring_picks_the_broker_by_config_and_prints_a_line_per_run(tmp_path, example, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text("ALPACA_API_KEY=a\nALPACA_SECRET_KEY=b\nANTHROPIC_API_KEY=c\n")
    start_experiment(example, tmp_path / "runs")
    market = object()
    made = {}
    monkeypatch.setattr(cli, "AlpacaMarket", lambda key, secret: market)
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: SimpleNamespace(key=kw["api_key"], kw=kw))
    monkeypatch.setattr(cli, "make_alpaca_api", lambda cfg, env: ("api", cfg.participant))
    monkeypatch.setattr(cli, "AlpacaBroker", lambda cfg, store, mkt, api: ("alpaca", api, mkt))
    def fake_tick(runs_dir, mkt, make_broker, make_client):
        made.update(runs=runs_dir.name, market=mkt, client=make_client())
        for name in ("haiku", "opus-a"):
            cfg, _ = load_run(tmp_path / "runs" / "pilot-1" / name)
            made[name] = make_broker(cfg, RunStore(tmp_path / "runs" / "pilot-1" / name))
        ts = "2026-12-01T15:05:00+00:00"
        return [{"experiment": "pilot-1", "participant": "haiku", "action": "session", "status": "ok", "cost": 0.123,
                 "ts": ts}, {"experiment": "pilot-1", "participant": "opus-a", "action": "safety", "ts": ts}]
    monkeypatch.setattr(cli, "tick", fake_tick)
    assert main(["tick"]) == 0
    assert made["runs"] == "runs" and made["market"] is market and made["client"].key == "c"
    assert isinstance(made["haiku"], SimBroker) and made["opus-a"] == ("alpaca", ("api", "opus-a"), market)
    out = capsys.readouterr().out
    assert out.splitlines() == ["2026-12-01 10:05 pilot-1/haiku: session ok $0.12", "2026-12-01 10:05 pilot-1/opus-a: safety"]
    assert made["client"].kw == {"api_key": "c", "max_retries": 2, "timeout": 300.0}

def test_tick_without_keys_prints_one_line_and_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["tick"]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1


# --- финальная волна, M24 ---

def test_start_prints_the_worst_case_spend(tmp_path, example, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert main(["start", str(example)]) == 0
    out = capsys.readouterr().out
    assert "$7.55 в торговый день" in out and "$158.55 за 21 торговый день" in out and "Console" in out


# --- команда report и отчёт после тика (задача 4) ---

ENV = "ALPACA_API_KEY=a\nALPACA_SECRET_KEY=b\nANTHROPIC_API_KEY=c\n"
TS = "2026-12-01T15:05:00+00:00"


class Api:
    """Подделка AlpacaApi: помнит, о чём её спросили."""
    def __init__(self, orders=(), error=None):
        self.orders, self.error, self.asked = list(orders), error, []

    def filled_orders_since(self, since):
        self.asked.append(since)
        if self.error:
            raise self.error
        return self.orders


@pytest.fixture
def world(tmp_path, example, monkeypatch):
    """Каталог с прогонами и .env; рынок, API и write_report заменены; `calls` копит вызовы write_report."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(ENV)
    start_experiment(example, tmp_path / "runs")
    w = SimpleNamespace(calls=[], api=Api(), apis=[], root=tmp_path, open=True)
    w.market = SimpleNamespace(now=lambda: datetime(2026, 12, 1, 15, 5, tzinfo=timezone.utc), is_open=lambda: w.open)

    def write(exp, market, wiki, ids=None):
        w.calls.append((exp.name, market, wiki, ids))
        if isinstance(getattr(w, "boom", None), Exception):
            raise w.boom
        return {}

    def make_api(cfg, env):
        w.apis.append(cfg.participant)
        return w.api
    monkeypatch.setattr(cli, "AlpacaMarket", lambda key, secret: w.market)
    monkeypatch.setattr(cli, "write_report", write)
    monkeypatch.setattr(cli, "make_alpaca_api", make_api)
    return w


def started(w, last_sync="2026-12-01T15:00:00+00:00"):
    """opus-a «обращался к брокеру»: run_uid и время первого обращения, как их пишет AlpacaBroker, и последняя сверка."""
    RunStore(w.root / "runs" / "pilot-1" / "opus-a").update_state(
        run_uid="u1", broker_since="2026-12-01T14:00:00+00:00", last_sync=last_sync)


def at(h, m):
    return datetime(2026, 12, 1, h, m, tzinfo=timezone.utc)


def test_report_prints_the_paths_and_passes_no_wiki_by_default(world, capsys):
    assert main(["report", "pilot-1"]) == 0
    out = capsys.readouterr().out.splitlines()
    assert out == ["runs/pilot-1/report.json", "runs/pilot-1/dashboard.html"]
    assert [(c[0], c[1], c[2]) for c in world.calls] == [("pilot-1", world.market, None)]


def test_report_passes_the_wiki_directory(world, tmp_path):
    assert main(["report", "pilot-1", "--wiki", str(tmp_path / "w")]) == 0
    assert world.calls[0][2] == tmp_path / "w"


def test_report_without_a_name_covers_every_experiment_in_runs(world, example, tmp_path, capsys):
    other = tmp_path / "other"; other.mkdir()
    shutil.copytree(example.parent, other / "exp")
    toml = other / "exp" / "experiment.toml"
    toml.write_text(toml.read_text().replace('experiment = "pilot-1"', 'experiment = "second"').replace('broker = "alpaca"', 'broker = "sim"'))
    start_experiment(toml, tmp_path / "runs")
    (tmp_path / "runs" / ".tick.lock").write_text("{}")
    assert main(["report"]) == 0
    assert [c[0] for c in world.calls] == ["pilot-1", "second"]
    assert capsys.readouterr().out.count("report.json") == 2


def test_no_experiments_is_one_line_and_code_0(world, capsys):
    shutil.rmtree(world.root / "runs")
    assert main(["report"]) == 0
    assert len(capsys.readouterr().err.strip().splitlines()) == 1 and not world.calls


def test_one_failing_experiment_does_not_stop_the_others_and_the_command_returns_1(world, example, tmp_path, capsys):
    other = tmp_path / "other"; other.mkdir()
    shutil.copytree(example.parent, other / "exp")
    toml = other / "exp" / "experiment.toml"
    toml.write_text(toml.read_text().replace('experiment = "pilot-1"', 'experiment = "second"').replace('broker = "alpaca"', 'broker = "sim"'))
    start_experiment(toml, tmp_path / "runs")
    started(world)
    real = cli.write_report

    def flaky(exp, market, wiki, ids=None):
        if exp.name == "pilot-1":
            raise RuntimeError("упал")
        return real(exp, market, wiki, ids)
    cli.write_report = flaky
    try:
        assert main(["report"]) == 1
    finally:
        cli.write_report = real
    out, err = capsys.readouterr()
    assert [c[0] for c in world.calls] == ["second"] and "second/report.json" in out and "упал" in err


def test_unknown_experiment_is_one_line_and_code_2(world, capsys):
    assert main(["report", "nope"]) == 2
    assert len(capsys.readouterr().err.strip().splitlines()) == 1 and not world.calls


def test_report_without_market_keys_is_one_line_and_code_1(world, capsys):
    (world.root / ".env").write_text("ANTHROPIC_API_KEY=c\n")
    assert main(["report", "pilot-1"]) == 1
    assert len(capsys.readouterr().err.strip().splitlines()) == 1 and not world.calls


def test_a_market_error_is_one_line_and_code_1(world, capsys):
    started(world)
    world.boom = RuntimeError("рынок недоступен")
    assert main(["report", "pilot-1"]) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "рынок недоступен" in err[0]


def test_broker_ids_are_the_filled_orders_with_this_runs_prefix_made_before_the_last_sync(world, capsys):
    started(world)                                                  # last_sync 15:00, граница 14:58
    world.api.orders = [{"id": "mine-1", "client_order_id": "pilot-1/opus-a/u1/buy/1", "filled_at": at(14, 30)},
                        {"id": "edge", "client_order_id": "pilot-1/opus-a/u1/stop/AAPL/1", "filled_at": at(14, 58)},
                        {"id": "late", "client_order_id": "pilot-1/opus-a/u1/buy/2", "filled_at": at(14, 59)},
                        {"id": "other-run", "client_order_id": "pilot-1/opus-a/zzzz/buy/1", "filled_at": at(14, 30)},
                        {"id": "by-hand", "client_order_id": None, "filled_at": at(14, 30)}]
    assert main(["report", "pilot-1"]) == 0
    assert world.calls[0][3] == {"opus-a": ["mine-1", "edge"]}
    assert world.apis == ["opus-a"] and world.api.asked == [datetime(2026, 11, 30, 14, 0, tzinfo=timezone.utc)]


def test_without_last_sync_the_last_tick_row_is_the_sync_time(world):
    started(world, last_sync=None)
    RunStore(world.root / "runs" / "pilot-1" / "opus-a").append_tick({"ts": "2026-12-01T15:00:00+00:00", "day": "2026-12-01", "action": "safety"})
    world.api.orders = [{"id": "a", "client_order_id": "pilot-1/opus-a/u1/x", "filled_at": at(14, 50)},
                        {"id": "b", "client_order_id": "pilot-1/opus-a/u1/y", "filled_at": at(14, 59)}]
    assert main(["report", "pilot-1"]) == 0 and world.calls[0][3] == {"opus-a": ["a"]}


def test_without_last_sync_and_tick_rows_the_check_is_skipped_with_a_line(world, capsys):
    started(world, last_sync=None)
    assert main(["report", "pilot-1"]) == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert world.apis == [] and world.calls[0][3] == {} and len(err) == 1 and "пропущена" in err[0]


def test_a_fill_after_the_last_sync_is_no_violation_but_an_older_missing_one_is(played, tmp_path, monkeypatch):
    """Настоящий write_report на мире `played`: прогон pilot-1 лежит в runs/ рабочего каталога."""
    exp, market = played
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(ENV)
    api = Api()
    monkeypatch.setattr(cli, "AlpacaMarket", lambda k, s: market)
    monkeypatch.setattr(cli, "make_alpaca_api", lambda cfg, env: api)
    RunStore(exp / "opus-a").update_state(run_uid="u1", broker_since="2026-12-01T14:00:00+00:00", last_sync="2026-12-03T20:00:00+00:00")
    pref = "pilot-1/opus-a/u1/"
    api.orders = [{"id": "late", "client_order_id": pref + "stop/XLE/1", "filled_at": datetime(2026, 12, 3, 19, 59, tzinfo=timezone.utc)}]
    assert main(["report", "pilot-1"]) == 0
    rep = json.loads((exp / "report.json").read_text())
    assert rep["verdict"]["by_participant"]["opus-a"] != "red" and not any(i["kind"] == "critical" for i in rep["incidents"])
    api.orders = [{"id": "missing-1", "client_order_id": pref + "stop/XLE/1", "filled_at": datetime(2026, 12, 3, 19, 0, tzinfo=timezone.utc)}]
    assert main(["report", "pilot-1"]) == 0
    rep = json.loads((exp / "report.json").read_text())
    assert rep["verdict"]["by_participant"]["opus-a"] == "red"
    assert any(i["kind"] == "critical" and "missing-1" in i["text"] for i in rep["incidents"])


def test_the_api_is_not_asked_for_a_run_that_fails_the_mode_and_broker_check(world, capsys):
    started(world)
    RunStore(world.root / "runs" / "pilot-1" / "opus-a").update_state(started_with={"mode": "live", "broker": "alpaca"})
    assert main(["report", "pilot-1"]) == 0
    assert world.apis == [] and world.calls[0][3] == {}
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "pilot-1/opus-a" in err[0] and "пропущена" in err[0]


def test_a_run_that_never_touched_the_broker_is_skipped_without_asking(world, capsys):
    assert main(["report", "pilot-1"]) == 0
    assert world.apis == [] and world.calls[0][3] == {} and "пропущена" in capsys.readouterr().err


def test_a_failing_broker_request_skips_the_check_and_still_builds(world, capsys):
    started(world)
    world.api.error = ConnectionError("нет связи")
    assert main(["report", "pilot-1"]) == 0
    assert world.calls[0][3] == {} and len(world.calls) == 1
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and "пропущена" in err[0] and "нет связи" in err[0]


def test_the_broker_is_decided_by_the_recorded_started_with_not_by_the_config(world, capsys):
    RunStore(world.root / "runs" / "pilot-1" / "opus-a").update_state(started_with={"mode": "paper", "broker": "sim"})
    RunStore(world.root / "runs" / "pilot-1" / "haiku").update_state(started_with={"mode": "paper", "broker": "alpaca"})
    assert main(["report", "pilot-1"]) == 0
    err = capsys.readouterr().err.strip().splitlines()
    assert world.apis == [] and len(err) == 1 and "pilot-1/haiku" in err[0]    # haiku: записан alpaca, конфиг правили


def test_sim_participants_are_never_sent_to_the_broker(world):
    assert main(["report", "pilot-1"]) == 0
    assert "haiku" not in world.apis and "opus-b" not in world.apis


def tick_returning(results):
    return lambda runs_dir, mkt, make_broker, make_client: results


def test_a_failing_auto_report_changes_neither_the_tick_lines_nor_the_exit_code(world, capsys, monkeypatch):
    started(world)
    world.boom = RuntimeError("отчёт упал")
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: True)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))
    assert main(["tick"]) == 0
    cap = capsys.readouterr()
    assert cap.out.splitlines() == ["2026-12-01 10:05 pilot-1/haiku: safety"]
    assert len(cap.err.strip().splitlines()) == 1 and "отчёт упал" in cap.err and world.calls


def test_auto_report_is_built_for_each_due_experiment_for_todays_market_day(world, capsys, monkeypatch):
    asked = []
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: asked.append((exp.name, day)) or True)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))
    assert main(["tick", "--wiki", "w"]) == 0
    assert asked == [("pilot-1", "2026-12-01")] and world.calls[0][2] == Path("w")
    assert capsys.readouterr().out.splitlines() == ["2026-12-01 10:05 pilot-1/haiku: safety"]


def test_auto_report_without_the_option_touches_no_wiki(world, monkeypatch):
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: True)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))
    main(["tick"])
    assert world.calls[0][2] is None


def test_auto_report_is_skipped_when_the_market_is_closed(world, monkeypatch):
    world.open = False
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: pytest.fail("не должен вызываться"))
    monkeypatch.setattr(cli, "tick", tick_returning([]))
    assert main(["tick"]) == 0 and not world.calls


def test_auto_report_is_attempted_on_an_open_market_even_when_the_tick_returned_nothing(world, capsys, monkeypatch):
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: True)
    monkeypatch.setattr(cli, "tick", tick_returning([]))
    assert main(["tick"]) == 0 and [c[0] for c in world.calls] == ["pilot-1"] and capsys.readouterr().out == ""


def test_a_blocked_auto_report_is_cut_off_by_the_alarm(world, capsys, monkeypatch):
    import signal
    import time
    old = signal.getsignal(signal.SIGALRM)
    monkeypatch.setattr(cli, "AUTO_REPORT_SECONDS", 1)
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: True)
    monkeypatch.setattr(cli, "write_report", lambda *a, **k: time.sleep(10))
    started(world)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))
    t0 = time.monotonic()
    assert main(["tick"]) == 0
    cap = capsys.readouterr()
    assert time.monotonic() - t0 < 5 and cap.out.splitlines() == ["2026-12-01 10:05 pilot-1/haiku: safety"]
    assert len(cap.err.strip().splitlines()) == 1 and "не уложился" in cap.err
    assert signal.alarm(0) == 0 and signal.getsignal(signal.SIGALRM) is old


def test_a_failing_report_due_is_one_stderr_line_too(world, capsys, monkeypatch):
    def boom(exp, day):
        raise ValueError("битый json")
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", boom)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))
    assert main(["tick"]) == 0 and len(capsys.readouterr().err.strip().splitlines()) == 1


# --- раунд исправлений 2 ---

@pytest.fixture
def silent_server():
    """Локальный сокет, который принимает соединения и никогда не отвечает: настоящее зависание чтения HTTP."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen()
    conns = []

    def accept():
        while True:
            try:
                conns.append(srv.accept()[0])
            except OSError:
                return
    t = threading.Thread(target=accept, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.getsockname()[1]}/"
    srv.close()
    t.join(timeout=5)
    for c in conns:
        c.close()
    assert not t.is_alive()


def second_experiment(example, tmp_path):
    other = tmp_path / "other"; other.mkdir()
    shutil.copytree(example.parent, other / "exp")
    toml = other / "exp" / "experiment.toml"
    toml.write_text(toml.read_text().replace('experiment = "pilot-1"', 'experiment = "second"').replace('broker = "alpaca"', 'broker = "sim"'))
    start_experiment(toml, tmp_path / "runs")


def arm_tick(world, monkeypatch, seconds=1):
    monkeypatch.setattr(cli, "AUTO_REPORT_SECONDS", seconds)
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: True)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))


def assert_section_closed(capsys, t0, old, limit=3.5):
    cap = capsys.readouterr()
    assert time.monotonic() - t0 < limit and cap.out.splitlines() == ["2026-12-01 10:05 pilot-1/haiku: safety"]
    assert len(cap.err.strip().splitlines()) == 1 and "не уложился" in cap.err
    assert signal.alarm(0) == 0 and signal.getsignal(signal.SIGALRM) is old


def test_a_real_socket_hang_in_the_build_ends_the_section_and_the_second_experiment_never_starts(
        world, example, tmp_path, capsys, monkeypatch, silent_server):
    import requests
    second_experiment(example, tmp_path)
    started(world)
    old = signal.getsignal(signal.SIGALRM)
    arm_tick(world, monkeypatch)
    started_builds = []
    monkeypatch.setattr(cli, "write_report", lambda exp, *a, **k: (started_builds.append(exp.name), requests.get(silent_server)))
    t0 = time.monotonic()
    assert main(["tick"]) == 0
    assert started_builds == ["pilot-1"]
    assert_section_closed(capsys, t0, old)


def test_a_hang_in_the_broker_request_does_not_let_the_build_run_on(world, capsys, monkeypatch, silent_server):
    import requests
    old = signal.getsignal(signal.SIGALRM)
    arm_tick(world, monkeypatch)
    started(world)

    class Hanging(Api):
        def filled_orders_since(self, since):
            return requests.get(silent_server)
    monkeypatch.setattr(cli, "make_alpaca_api", lambda cfg, env: Hanging())
    t0 = time.monotonic()
    assert main(["tick"]) == 0
    assert world.calls == []                       # сборка не началась
    assert_section_closed(capsys, t0, old)


def test_a_timeout_swallowed_by_the_broker_check_is_not_swallowed_for_good(world, capsys, monkeypatch):
    """Даже если что-то съест первое срабатывание, будильник взводится снова и срабатывает ещё раз."""
    old = signal.getsignal(signal.SIGALRM)
    arm_tick(world, monkeypatch)
    started(world)

    def swallow(since):
        try:
            time.sleep(10)
        except BaseException:      # самое плохое: съедено всё
            time.sleep(10)
    monkeypatch.setattr(cli, "make_alpaca_api", lambda cfg, env: SimpleNamespace(filled_orders_since=swallow))
    t0 = time.monotonic()
    assert main(["tick"]) == 0
    assert world.calls == []
    assert_section_closed(capsys, t0, old, limit=4.5)


def test_a_previously_pending_alarm_is_restored_with_the_time_left(world, monkeypatch):
    arm_tick(world, monkeypatch)
    monkeypatch.setattr(cli, "write_report", lambda *a, **k: time.sleep(1.2))
    started(world)
    signal.alarm(100)
    try:
        assert main(["tick"]) == 0
        assert 90 <= signal.alarm(0) <= 99
    finally:
        signal.alarm(0)


def make_terminal_world(played, tmp_path, monkeypatch, status):
    exp, market = played
    monkeypatch.chdir(tmp_path)
    (tmp_path / ".env").write_text(ENV)
    api = Api()
    monkeypatch.setattr(cli, "AlpacaMarket", lambda k, s: market)
    monkeypatch.setattr(cli, "make_alpaca_api", lambda cfg, env: api)
    RunStore(exp / "opus-a").update_state(run_uid="u1", broker_since="2026-12-01T14:00:00+00:00",
                                          last_sync="2026-12-03T20:00:00+00:00", status=status)
    api.orders = [{"id": "late", "client_order_id": "pilot-1/opus-a/u1/liquidate/2026-12-03/XLE",
                   "filled_at": datetime(2026, 12, 3, 21, 0, tzinfo=timezone.utc)}]
    return exp, api


def integrity_incidents(exp):
    rep = json.loads((exp / "report.json").read_text())
    return [i for i in rep["incidents"] if i["title"] == "целостность записей нарушена"]


@pytest.mark.parametrize("status", ["dead", "finished"])
def test_a_terminal_run_has_no_cutoff_so_a_late_missing_fill_is_flagged(played, tmp_path, monkeypatch, status):
    exp, api = make_terminal_world(played, tmp_path, monkeypatch, status)
    assert main(["report", "pilot-1"]) == 0
    assert any("late" in i["text"] for i in integrity_incidents(exp))
    RunStore(exp / "opus-a").append_order({"day": "2026-12-03", "kind": "liquidate", "symbol": "XLE", "broker_order_id": "late"})
    assert main(["report", "pilot-1"]) == 0
    assert not integrity_incidents(exp)


def test_an_active_run_with_the_same_late_fill_is_still_not_flagged(played, tmp_path, monkeypatch):
    exp, api = make_terminal_world(played, tmp_path, monkeypatch, "active")
    assert main(["report", "pilot-1"]) == 0
    assert not integrity_incidents(exp)
