import pytest

from traider.config import Limits, load_run, start_experiment


def test_limits_defaults_match_spec():
    lim = Limits()
    assert (lim.max_position_share, lim.min_order_usd) == (0.20, 20.0)
    assert (lim.stop_min, lim.stop_max) == (0.03, 0.15)
    assert (lim.max_open_risk_share, lim.max_buys_per_session) == (0.08, 3)
    assert lim.floor_share == 0.85
    assert (lim.max_turns, lim.max_searches, lim.max_session_usd) == (40, 8, 2.0)


def test_start_creates_one_frozen_run_per_participant(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    assert sorted(p.name for p in exp.iterdir()) == ["haiku", "opus-a", "opus-analyst", "opus-b", "sonnet"]
    cfg, valid = load_run(exp / "opus-a")
    assert valid and (cfg.model, cfg.broker, cfg.research) == ("claude-opus-5-5", "alpaca", "self")
    assert cfg.token_prices["input"] == 4.0 and cfg.limits.max_session_usd == 2.0
    assert len(cfg.universe) == 25 and len(cfg.panel) == 20
    assert cfg.system_prompt.startswith("Системный") and cfg.analyst_prompt is None
    analyst, _ = load_run(exp / "opus-analyst")
    assert (analyst.research, analyst.analyst_model, analyst.analyst_max_searches) == ("analyst", "claude-haiku-5-5", 40)
    assert analyst.analyst_prompt.startswith("Промпт аналитика")
    assert load_run(exp / "haiku")[0].limits.max_session_usd == 0.25


def test_edited_frozen_config_is_reported_invalid(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    path = run / "config.json"
    path.write_text(path.read_text().replace('"max_buys_per_session": 3', '"max_buys_per_session": 30'))
    cfg, valid = load_run(run)
    assert valid is False and cfg.limits.max_buys_per_session == 30


def test_start_refuses_to_overwrite(tmp_path, example):
    start_experiment(example, tmp_path / "runs")
    with pytest.raises(FileExistsError):
        start_experiment(example, tmp_path / "runs")


def test_live_mode_needs_explicit_flag(tmp_path, example_live):
    with pytest.raises(ValueError, match="live"):
        start_experiment(example_live, tmp_path / "runs")
    assert start_experiment(example_live, tmp_path / "runs", live=True).exists()


def test_unknown_model_or_broker_is_rejected(tmp_path, example_bad_broker):
    with pytest.raises(ValueError):
        start_experiment(example_bad_broker, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()  # ничего не создано до проверки
