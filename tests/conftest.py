import pathlib
import re
from dataclasses import replace

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
        if death and day == 2:                    # обвал и в котировке, и в свечах: капитал opus-b около 800 при поле 850
            market.prices["AAPL"] *= 0.8
            market.bars["AAPL"] = [replace(b, open=b.open * .8, high=b.high * .8, low=b.low * .8, close=b.close * .8)
                                   if b.day >= "2026-12-02" else b for b in market.bars["AAPL"]]
        tick(runs, market, make_broker, make_client)
    market.clock, market.open = ny(2026, 12, 3, 16, 30), False
    return exp, market


@pytest.fixture
def played(tmp_path, example):
    return _play(tmp_path, example, death=False)


@pytest.fixture
def played_with_death(tmp_path, example):
    return _play(tmp_path, example, death=True)


WAVE = (1.00, 1.03, 0.98, 1.02, 0.97, 1.01, 0.99, 1.04)     # множитель цены по торговым дням с 01.12, со сдвигом по тикеру
MOVING_DAYS = ("2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04")
MOVING_BUYS = {"haiku": "XLE", "sonnet": "XLF", "opus-a": "XLE", "opus-b": "XLU", "opus-analyst": "QQQ"}


def moving_p(symbol: str) -> float:
    return 0.2 + sum(map(ord, symbol)) % 7 / 10


@pytest.fixture
def moving(tmp_path):
    """Четыре дня сессий на движущихся ценах (котировка дня = открытие свечи дня), отчёт строится 09.12 после
    закрытия, поэтому прогнозы 01.12 и 02.12 созрели. Прогон на 10 сессий: позиции остаются открытыми.
    Каждый участник покупает свою бумагу на $45 со стопом 8% (opus-b первую покупку делает в XLE)."""
    from tests.fakes import FakeAnthropic, ny, use
    from traider.tick import tick

    toml = _make(tmp_path, lambda t: t.replace("sessions_target = 3", "sessions_target = 10"))
    runs = tmp_path / "runs"
    exp = start_experiment(toml, runs)
    universe = list(load_run(exp / "haiku")[0].universe)
    market = FakeMarket.with_history(universe, days=70, clock=ny(2026, 12, 9, 16, 30))
    for s in universe:
        shift, k, bars = sum(map(ord, s)) % len(WAVE), 0, []
        for b in market.bars[s]:
            if b.day >= "2026-12-01":
                c = b.close * WAVE[(k + shift) % len(WAVE)]
                k += 1
                b = replace(b, open=c * .995, high=c * 1.01, low=c * .995 * .99, close=c)
            bars.append(b)
        market.bars[s] = bars
    u, who = {"input_tokens": 1000, "output_tokens": 200}, {}

    def make_broker(cfg, store):
        who["p"] = cfg.participant              # тик создаёт брокера прогона раньше его клиентов
        return SimBroker(cfg, store, market)

    def make_client():
        day = market.clock.date().isoformat()
        sym = "XLE" if who["p"] == "opus-b" and day == MOVING_DAYS[0] else MOVING_BUYS[who["p"]]
        return FakeAnthropic([
            use("get_account", usage=u),
            use("record_forecasts", usage=u, forecasts=[{"symbol": s, "p": moving_p(s)} for s in [*PANEL, sym]]),
            use("buy", usage=u, symbol=sym, notional=45.0, stop_price=round(market.prices[sym] * .92, 2), thesis="т"),
            use("finish_session", usage=u, journal="ок")])

    for d in MOVING_DAYS:
        market.clock, market.open = ny(2026, 12, int(d[-2:]), 10, 5), True
        market.prices = {s: next(b.open for b in market.bars[s] if b.day == d) for s in universe}
        tick(runs, market, make_broker, make_client)
    market.clock, market.open = ny(2026, 12, 9, 16, 30), False
    return exp, market


@pytest.fixture
def example_live(tmp_path):
    return _make(tmp_path, lambda t: t.replace('mode = "paper"', 'mode = "live"'))


@pytest.fixture
def example_bad_broker(tmp_path):
    return _make(tmp_path, lambda t: re.sub(r'broker = "sim"', 'broker = "nope"', t, count=1))
