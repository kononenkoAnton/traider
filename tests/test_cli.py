from datetime import datetime, timezone
from types import SimpleNamespace

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
