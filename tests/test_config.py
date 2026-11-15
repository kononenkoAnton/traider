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


def test_duplicate_participant_names_are_rejected(tmp_path, example):
    example.write_text(example.read_text().replace('name = "opus-b"', 'name = "opus-a"'))
    with pytest.raises(ValueError, match="opus-a"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_missing_checksum_is_reported_invalid(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "haiku"
    (run / "config.sha256").unlink()
    cfg, valid = load_run(run)
    assert valid is False and cfg.participant == "haiku"


def test_model_without_prices_is_rejected(tmp_path, example):
    example.write_text(example.read_text().replace('model = "claude-sonnet-5-5"', 'model = "claude-nope"'))
    with pytest.raises(ValueError, match="claude-nope"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_analyst_without_analyst_model_is_rejected(tmp_path, example):
    example.write_text(example.read_text().replace('analyst_model = "claude-haiku-5-5"\n', ''))
    with pytest.raises(ValueError, match="opus-analyst"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_panel_ticker_outside_the_universe_is_rejected(tmp_path, example):
    example.write_text(example.read_text().replace('"MA"]', '"MA","ZZZZ"]'))
    with pytest.raises(ValueError, match="ZZZZ"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_criteria_missing_a_key_is_rejected(tmp_path, example):
    example.write_text(example.read_text().replace("red_percentile = 25\n", ""))
    with pytest.raises(ValueError, match="red_percentile"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_price_table_missing_a_key_is_rejected(tmp_path, example):
    example.write_text(example.read_text().replace("cache_write = 2.5\n", ""))
    with pytest.raises(ValueError, match="cache_write"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_analyst_prices_are_frozen_for_the_analyst_participant_only(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    analyst, _ = load_run(exp / "opus-analyst")
    assert analyst.analyst_token_prices["input"] == 0.10 and analyst.analyst_token_prices["search"] == 0.01
    assert load_run(exp / "opus-a")[0].analyst_token_prices is None


def test_an_analyst_model_without_prices_is_rejected_before_any_write(tmp_path, example):
    example.write_text(example.read_text().replace('analyst_model = "claude-haiku-5-5"', 'analyst_model = "claude-nope"'))
    with pytest.raises(ValueError, match="аналитик"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_partial_long_rates_are_rejected_before_any_write(tmp_path, example):
    text = example.read_text()
    cut = "cache_read_long = 0.05\ncache_write_long = 0.625\n"
    assert cut in text
    example.write_text(text.replace(cut, "").replace("output_long = 2.50\n", ""))  # остались порог и input_long
    with pytest.raises(ValueError, match="длинные ставки"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()


def test_haiku_long_rates_are_frozen_for_the_participant_and_the_analyst(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    want = {"long_prompt_threshold": 100000, "input_long": 0.50, "output_long": 2.50,
            "cache_read_long": 0.05, "cache_write_long": 0.625}
    for table in (load_run(exp / "haiku")[0].token_prices, load_run(exp / "opus-analyst")[0].analyst_token_prices):
        assert {k: table[k] for k in want} == want
    assert "input_long" not in load_run(exp / "opus-a")[0].token_prices


# --- финальная волна ---

def test_session_time_limit_is_frozen_and_read_back(tmp_path, example):            # M13
    assert Limits().max_session_seconds == 1200
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    assert '"max_session_seconds": 1200' in (run / "config.json").read_text()
    cfg, valid = load_run(run)
    assert valid and cfg.limits == Limits(max_session_seconds=1200)


@pytest.mark.parametrize("old, new", [('window_start = "10:00"', 'window_start = "9:45"'),
                                      ('window_end = "15:30"', 'window_end = "15:3"'),
                                      ('window_start = "10:00"', 'window_start = "15:30"')])
def test_window_must_be_padded_hh_mm_and_start_before_end(tmp_path, example, old, new):  # L179
    assert old in example.read_text()
    example.write_text(example.read_text().replace(old, new))
    with pytest.raises(ValueError, match="window|окно"):
        start_experiment(example, tmp_path / "runs")
    assert not (tmp_path / "runs").exists()
