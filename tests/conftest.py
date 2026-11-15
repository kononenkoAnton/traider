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


def _analyst_world(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-analyst"
    cfg, _ = load_run(run)
    return cfg, RunStore(run), FakeMarket.with_history(list(cfg.universe), days=70)

@pytest.fixture
def env_analyst(tmp_path, example):
    """Конфиг opus-analyst и рынок."""
    cfg, _, market = _analyst_world(tmp_path, example)
    return cfg, market

@pytest.fixture
def env_analyst_session(tmp_path, example):
    """Мир сессии для opus-analyst: конфиг, хранилище, рынок, SimBroker."""
    cfg, store, market = _analyst_world(tmp_path, example)
    return cfg, store, market, SimBroker(cfg, store, market)

def _play(tmp_path, example, death: bool):
    """Три дня настоящих тиков: каждый день агент смотрит счёт, пишет прогнозы по панели и XLE,
    покупает XLE на $60 и заканчивает; третья сессия последняя (sessions_target = 3) и продаёт всё."""
    from tests.fakes import FakeAnthropic, ny, use
    from traider.tick import tick

    runs = tmp_path / "runs"
    exp = start_experiment(example, runs)
    market = FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70, clock=ny(2026, 12, 3, 16, 30))
    market.open = True
    u = {"input_tokens": 1000, "output_tokens": 200}

    def make_client():   # у opus-analyst второй клиент достаётся аналитику: он просто доходит до конца сценария
        return FakeAnthropic([
            use("get_account", usage=u),
            use("record_forecasts", usage=u, forecasts=[{"symbol": s, "p": 0.6} for s in [*PANEL, "XLE"]]),
            use("buy", usage=u, symbol="XLE", notional=60.0, stop_price=140.0, thesis="тест"),
            use("finish_session", usage=u, journal="ок")])

    def make_broker(cfg, store):
        return SimBroker(cfg, store, market)

    for day in (1, 2, 3):
        market.clock = ny(2026, 12, day, 10, 5)
        if death and day == 1:
            run = exp / "opus-b"
            SimBroker(load_run(run)[0], RunStore(run), market).buy("2026-12-01", "AAPL", 1000.0, 1.0, "т", "seed/1")
        if death and day == 2:
            market.prices["AAPL"] *= 0.8          # капитал opus-b около 800 при поле 850
        tick(runs, market, make_broker, make_client)
    market.clock, market.open = ny(2026, 12, 3, 16, 30), False
    return exp, market


@pytest.fixture
def played(tmp_path, example):
    return _play(tmp_path, example, death=False)


@pytest.fixture
def played_with_death(tmp_path, example):
    return _play(tmp_path, example, death=True)


@pytest.fixture
def example_live(tmp_path):
    return _make(tmp_path, lambda t: t.replace('mode = "paper"', 'mode = "live"'))


@pytest.fixture
def example_bad_broker(tmp_path):
    return _make(tmp_path, lambda t: re.sub(r'broker = "sim"', 'broker = "nope"', t, count=1))
