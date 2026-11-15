import pathlib
import re

import pytest

from tests.fakes import FakeMarket
from traider.brokers import SimBroker
from traider.config import load_run, start_experiment
from traider.store import RunStore

ROOT = pathlib.Path(__file__).resolve().parent.parent
PANEL = ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "BRK.B", "JPM", "V",
         "UNH", "XOM", "LLY", "AVGO", "COST", "WMT", "HD", "PG", "JNJ", "MA"]


def _make(tmp_path, edit=lambda text: text):
    """Копия experiment.example.toml с тестовыми файлами по тем же относительным путям."""
    d = tmp_path / "exp"
    (d / "data").mkdir(parents=True)
    (d / "traider" / "prompts").mkdir(parents=True)
    (d / "data" / "universe.txt").write_text("\n".join([*PANEL, "SPY", "QQQ", "XLE", "XLF", "XLU"]) + "\n")
    (d / "traider" / "prompts" / "system.md").write_text("Системный промпт теста.\n")
    (d / "traider" / "prompts" / "analyst.md").write_text("Промпт аналитика теста.\n")
    toml = d / "experiment.toml"
    toml.write_text(edit((ROOT / "experiment.example.toml").read_text()))
    return toml


@pytest.fixture
def example(tmp_path):
    return _make(tmp_path)


@pytest.fixture
def env(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    cfg, _ = load_run(run)
    return cfg, RunStore(run), FakeMarket(prices={"AAPL": 200.0, "XLE": 100.0})


@pytest.fixture
def senv(tmp_path, example):
    """Мир сессии для opus-b: конфиг, хранилище, рынок с историей по всей вселенной, SimBroker."""
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    cfg, _ = load_run(run)
    store = RunStore(run)
    market = FakeMarket.with_history(list(cfg.universe), days=70)
    return cfg, store, market, SimBroker(cfg, store, market)


@pytest.fixture
def example_live(tmp_path):
    return _make(tmp_path, lambda t: t.replace('mode = "paper"', 'mode = "live"'))


@pytest.fixture
def example_bad_broker(tmp_path):
    return _make(tmp_path, lambda t: re.sub(r'broker = "sim"', 'broker = "nope"', t, count=1))
