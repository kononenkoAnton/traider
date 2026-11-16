import shutil
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
    w = SimpleNamespace(market=SimpleNamespace(now=lambda: datetime(2026, 12, 1, 15, 5, tzinfo=timezone.utc)),
                        calls=[], api=Api(), apis=[], root=tmp_path)

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


def started(w):
    """opus-a «обращался к брокеру»: run_uid и время первого обращения, как их пишет AlpacaBroker."""
    RunStore(w.root / "runs" / "pilot-1" / "opus-a").update_state(run_uid="u1", broker_since="2026-12-01T14:00:00+00:00")


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


def test_broker_ids_are_the_filled_orders_with_this_runs_prefix(world, capsys):
    started(world)
    world.api.orders = [{"id": "mine-1", "client_order_id": "pilot-1/opus-a/u1/buy/1"},
                        {"id": "other-run", "client_order_id": "pilot-1/opus-a/zzzz/buy/1"},
                        {"id": "by-hand", "client_order_id": None}]
    assert main(["report", "pilot-1"]) == 0
    assert world.calls[0][3] == {"opus-a": ["mine-1"]}
    assert world.apis == ["opus-a"] and world.api.asked == [datetime(2026, 11, 30, 14, 0, tzinfo=timezone.utc)]


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


def test_auto_report_is_skipped_when_the_market_was_closed(world, monkeypatch):
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", lambda exp, day: pytest.fail("не должен вызываться"))
    monkeypatch.setattr(cli, "tick", tick_returning([]))
    assert main(["tick"]) == 0 and not world.calls


def test_a_failing_report_due_is_one_stderr_line_too(world, capsys, monkeypatch):
    def boom(exp, day):
        raise ValueError("битый json")
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: None)
    monkeypatch.setattr(cli, "report_due", boom)
    monkeypatch.setattr(cli, "tick", tick_returning([{"experiment": "pilot-1", "participant": "haiku", "action": "safety", "ts": TS}]))
    assert main(["tick"]) == 0 and len(capsys.readouterr().err.strip().splitlines()) == 1
