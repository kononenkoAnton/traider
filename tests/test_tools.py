import ast
import math
import pathlib

import pytest

from tests.fakes import FakeMarket
from traider import tools
from traider.brokers import BrokerRejected, NoPrice, SimBroker
from traider.cage import Account, Position
from traider.config import load_run, start_experiment
from traider.store import RunStore
from traider.tools import SessionContext, build_tools


@pytest.fixture
def ctx(tmp_path, example):
    run = start_experiment(example, tmp_path / "runs") / "opus-b"
    cfg, _ = load_run(run)
    market = FakeMarket.with_history(list(cfg.universe), days=70)
    store = RunStore(run)
    return SessionContext(cfg, store, SimBroker(cfg, store, market), market, "2026-12-01")


def call(ctx, name, **args):
    return next(t for t in build_tools(ctx) if t.name == name).fn(args)


def panel(ctx):
    return call(ctx, "record_forecasts", forecasts=[{"symbol": s, "p": 0.55} for s in ctx.cfg.panel])


def buy(ctx, symbol="AAPL", notional=150.0, stop=0.93, **kw):
    return call(ctx, "buy", symbol=symbol, notional=notional, stop_price=ctx.market.prices[symbol] * stop,
                thesis="т", **kw)


def last(ctx):
    return ctx.store.orders()[-1]


class Wrapped:
    """Брокер с подменёнными методами: остальное берётся у настоящего."""
    def __init__(self, inner, **over):
        self._inner = inner
        self.__dict__.update(over)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def test_tool_set_depends_on_research_mode(ctx):
    names = {t.name for t in build_tools(ctx)}
    assert {"get_news", "buy", "finish_session"} <= names and "research" not in names
    ctx.research = lambda q: "ответ"
    names = {t.name for t in build_tools(ctx)}
    assert "research" in names and "get_news" not in names
    assert call(ctx, "research", question="что?") == "ответ"


def test_every_schema_is_strict(ctx):
    for t in build_tools(ctx):
        assert t.input_schema["additionalProperties"] is False, t.name
        assert t.description and any("а" <= c <= "я" for c in t.description.lower()), t.name


def test_buy_is_rejected_until_panel_forecasts_exist(ctx):
    price = ctx.market.prices["AAPL"]
    out = call(ctx, "buy", symbol="AAPL", notional=150.0, stop_price=price * 0.93, thesis="т")
    assert out.startswith("Отказ") and ctx.store.orders()[-1]["reason"] == "no_panel_forecasts"
    panel(ctx)
    out = call(ctx, "buy", symbol="AAPL", notional=150.0, stop_price=price * 0.93, thesis="т")
    assert out.startswith("Куплено") and ctx.store.orders()[-1]["kind"] == "buy"
    assert ctx.store.orders()[-1]["client_order_id"] == "pilot-1/opus-b/2026-12-01/2"


def test_rejection_text_carries_the_numbers(ctx):
    panel(ctx)
    out = call(ctx, "buy", symbol="AAPL", notional=500.0, stop_price=ctx.market.prices["AAPL"] * 0.93, thesis="т")
    assert "20%" in out and ctx.store.orders()[-1]["reason"] == "position_too_large"


def test_fourth_buy_in_a_session_is_rejected(ctx):
    panel(ctx)
    for symbol in ["AAPL", "MSFT", "NVDA"]:
        assert call(ctx, "buy", symbol=symbol, notional=50.0, stop_price=ctx.market.prices[symbol] * 0.93, thesis="т").startswith("Куплено")
    out = call(ctx, "buy", symbol="JPM", notional=50.0, stop_price=ctx.market.prices["JPM"] * 0.93, thesis="т")
    assert ctx.store.orders()[-1]["reason"] == "too_many_buys" and out.startswith("Отказ")


def test_buy_is_refused_when_the_run_is_not_active(ctx):
    panel(ctx); ctx.store.update_state(status="dead")
    call(ctx, "buy", symbol="AAPL", notional=50.0, stop_price=ctx.market.prices["AAPL"] * 0.93, thesis="т")
    assert ctx.store.orders()[-1]["reason"] == "run_not_active"


def test_forecasts_are_validated(ctx):
    assert call(ctx, "record_forecasts", forecasts=[{"symbol": "AAPL", "p": 1.2}]).startswith("Ошибка")
    assert call(ctx, "record_forecasts", forecasts=[{"symbol": "ZZZZ", "p": 0.5}]).startswith("Ошибка")
    assert ctx.store.forecasts() == []
    panel(ctx)
    assert len(ctx.store.forecasts()) == 20


def test_one_bad_forecast_rejects_the_whole_call(ctx):
    bad = [{"symbol": "AAPL", "p": 0.5}, {"symbol": "MSFT", "p": math.nan}]
    assert call(ctx, "record_forecasts", forecasts=bad).startswith("Ошибка")
    assert call(ctx, "record_forecasts", forecasts=[{"symbol": "AAPL", "p": 0.5}, {"symbol": "ZZZZ", "p": 0.5}]).startswith("Ошибка")
    assert ctx.store.forecasts() == []


def test_stop_can_only_go_up_and_close_frees_the_position(ctx):
    panel(ctx); price = ctx.market.prices["AAPL"]
    call(ctx, "buy", symbol="AAPL", notional=150.0, stop_price=price * 0.93, thesis="т")
    assert call(ctx, "raise_stop", symbol="AAPL", stop_price=price * 0.90).startswith("Отказ")
    assert call(ctx, "raise_stop", symbol="AAPL", stop_price=price * 0.95).startswith("Стоп")
    assert call(ctx, "close_position", symbol="AAPL", fraction=1.0, reason="р").startswith("Закрыто")
    assert ctx.broker.account().positions == ()
    assert call(ctx, "close_position", symbol="AAPL", fraction=1.0, reason="р").startswith("Ошибка")


def test_notes_and_journal_are_capped(ctx):
    call(ctx, "update_notes", text="я" * 5000)
    assert len(ctx.store.state()["notes"]) == 2000
    call(ctx, "finish_session", journal="слово " * 400)
    assert len(ctx.journal.split()) == 300 and next(t for t in build_tools(ctx) if t.name == "finish_session").terminal


def test_read_tools_return_compact_text(ctx):
    account = call(ctx, "get_account")
    assert "HP" in account and "1000" in account.replace(" ", "").replace(" ", "")
    top = call(ctx, "scan_universe", sort_by="ret_20d", descending=True, top=5)
    assert len([line for line in top.splitlines() if line and line.split()[0] in ctx.cfg.universe]) == 5
    assert call(ctx, "get_bars", symbols=["AAPL"], days=5).count("\n") <= 8
    assert isinstance(call(ctx, "get_news", limit=3), str)


def test_get_account_lists_positions_and_buys_left(ctx):
    panel(ctx); buy(ctx)
    account = call(ctx, "get_account")
    assert account.count("AAPL") == 1 and "стоп" in account and "осталось в этой сессии: 2" in account


# --- отказы клетки: по тесту на код ---

def code(ctx):
    return last(ctx)["reason"]


def test_cage_codes_for_a_buy(ctx):
    price = ctx.market.prices
    assert buy(ctx, notional=math.nan).startswith("Отказ") and code(ctx) == "bad_request"
    assert call(ctx, "buy", symbol="ZZZZ", notional=50.0, stop_price=10.0, thesis="т").startswith("Отказ")
    assert code(ctx) == "not_in_universe"
    gone = price.pop("AAPL")
    assert call(ctx, "buy", symbol="AAPL", notional=50.0, stop_price=gone * 0.93, thesis="т").startswith("Отказ")
    assert code(ctx) == "no_price"
    price["AAPL"] = gone
    buy(ctx); assert code(ctx) == "no_panel_forecasts"
    call(ctx, "record_forecasts", forecasts=[{"symbol": s, "p": 0.5} for s in ctx.cfg.panel])
    buy(ctx, "SPY"); assert code(ctx) == "no_symbol_forecast"
    buy(ctx, notional=5.0); assert code(ctx) == "order_too_small"
    buy(ctx, stop=0.5); assert code(ctx) == "stop_out_of_range"
    buy(ctx, notional=500.0); assert code(ctx) == "position_too_large"
    assert buy(ctx, stop=0.93).startswith("Куплено")
    buy(ctx, stop=0.90); assert code(ctx) == "stop_below_existing"
    assert call(ctx, "close_position", symbol="AAPL", fraction=1.0, reason="р").startswith("Закрыто")
    buy(ctx); assert code(ctx) == "rebuy_same_day"
    for s in ("MSFT", "NVDA"):
        assert buy(ctx, s, 50.0).startswith("Куплено")
    buy(ctx, "JPM", 50.0); assert code(ctx) == "too_many_buys"


def test_open_risk_code(ctx):
    panel(ctx)
    for s in ("AAPL", "MSFT"):
        assert buy(ctx, s, 190.0, 0.851).startswith("Куплено")
    out = buy(ctx, "NVDA", 190.0, 0.851)
    assert out.startswith("Отказ") and code(ctx) == "open_risk_too_high" and "8%" in out


def test_not_enough_cash_code(ctx):
    panel(ctx)
    poor = Wrapped(ctx.broker, account=lambda: Account(10.0, (Position("MSFT", 10.0, 100.0, 90.0),), 1000.0))
    ctx.broker = poor
    out = buy(ctx, notional=50.0)
    assert out.startswith("Отказ") and code(ctx) == "not_enough_cash"


def test_raise_stop_refusals_are_logged(ctx):
    panel(ctx); price = ctx.market.prices["AAPL"]
    buy(ctx)
    assert call(ctx, "raise_stop", symbol="AAPL", stop_price=price * 0.90).startswith("Отказ")
    assert last(ctx)["kind"] == "reject" and last(ctx)["reason"] == "stop_not_higher" and last(ctx)["action"] == "raise_stop"
    assert call(ctx, "raise_stop", symbol="AAPL", stop_price=price * 0.99).startswith("Отказ")
    assert code(ctx) == "stop_out_of_range"
    assert call(ctx, "raise_stop", symbol="AAPL", stop_price=math.nan).startswith("Отказ")
    assert code(ctx) == "bad_request" and last(ctx)["stop_price"] is None


def test_unheld_symbol_is_an_input_error_not_a_ledger_line(ctx):
    n = len(ctx.store.orders())
    assert call(ctx, "raise_stop", symbol="AAPL", stop_price=100.0).startswith("Ошибка")
    assert call(ctx, "close_position", symbol="AAPL", fraction=0.5, reason="р").startswith("Ошибка")
    assert len(ctx.store.orders()) == n


# --- исключения ---

def test_cage_exception_becomes_a_logged_refusal(ctx, monkeypatch):
    panel(ctx)
    def boom(*a, **k):
        raise ValueError("нечисло в счёте")
    monkeypatch.setattr(tools, "check_buy", boom)
    out = buy(ctx)
    assert out.startswith("Отказ") and code(ctx) == "bad_request" and "нечисло" in out


def test_broker_refusal_is_logged(ctx):
    panel(ctx)
    def refuse(*a, **k):
        raise BrokerRejected("нет денег у брокера")
    ctx.broker = Wrapped(ctx.broker, buy=refuse)
    out = buy(ctx)
    assert out.startswith("Отказ") and "нет денег у брокера" in out
    assert code(ctx) == "broker_rejected" and last(ctx)["client_order_id"] == "pilot-1/opus-b/2026-12-01/1"


def test_no_price_from_the_broker_is_logged(ctx):
    panel(ctx)
    def none(*a, **k):
        raise NoPrice("AAPL")
    ctx.broker = Wrapped(ctx.broker, buy=none)
    assert buy(ctx).startswith("Отказ") and code(ctx) == "no_price"


def test_close_refusal_by_the_broker_is_a_refusal_text(ctx):
    panel(ctx); buy(ctx)
    def refuse(*a, **k):
        raise BrokerRejected("нельзя")
    ctx.broker = Wrapped(ctx.broker, close=refuse)
    assert call(ctx, "close_position", symbol="AAPL", fraction=1.0, reason="р").startswith("Отказ")
    assert code(ctx) == "broker_rejected"


def test_raise_stop_exception_from_the_broker_is_logged(ctx):
    panel(ctx); buy(ctx)
    def refuse(*a, **k):
        raise BrokerRejected("биржа не приняла")
    ctx.broker = Wrapped(ctx.broker, raise_stop=refuse)
    out = call(ctx, "raise_stop", symbol="AAPL", stop_price=ctx.market.prices["AAPL"] * 0.95)
    assert out.startswith("Отказ") and code(ctx) == "broker_rejected"


def test_nan_in_the_request_is_refused_and_the_ledger_stays_clean(ctx):
    panel(ctx)
    out = buy(ctx, notional=math.nan)
    assert out.startswith("Отказ")
    raw = (ctx.store.dir / "orders.jsonl").read_text()
    assert "NaN" not in raw and last(ctx)["notional"] is None
    out = call(ctx, "buy", symbol="AAPL", notional=50.0, stop_price=math.inf, thesis="т")
    assert out.startswith("Отказ") and last(ctx)["stop_price"] is None


def test_each_attempt_gets_a_fresh_order_id(ctx):
    panel(ctx)
    buy(ctx)                                    # покупка
    call(ctx, "close_position", symbol="AAPL", fraction=0.5, reason="р")
    call(ctx, "close_position", symbol="AAPL", fraction=0.5, reason="р")
    ids = [e["client_order_id"] for e in ctx.store.orders() if e["kind"] in ("buy", "close")]
    assert ids == [f"pilot-1/opus-b/2026-12-01/{n}" for n in (1, 2, 3)]


# --- главное обещание ---

def test_only_tools_py_places_orders():
    root = pathlib.Path(__file__).resolve().parent.parent / "traider"
    allowed = {"tools.py"}      # слой клетки: единственный, кто вызывает брокера
    hits = []
    for path in sorted(root.glob("*.py")):
        if path.name in allowed:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("buy", "close", "raise_stop"):
                hits.append(f"{path.name}:{node.lineno} .{node.func.attr}(")
    assert hits == []
    used = {n.func.attr for n in ast.walk(ast.parse((root / "tools.py").read_text(encoding="utf-8")))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert {"buy", "close", "raise_stop"} <= used          # проверка видит вызовы, а не пустоту


# --- границы схем, проверяемые циклом ---

def schema_of(ctx, name):
    return next(t for t in build_tools(ctx) if t.name == name).input_schema


def keywords(schema):
    """Все ключевые слова схемы (имена полей в properties не считаются)."""
    found = set()
    for key, value in schema.items():
        found.add(key)
        if key == "properties":
            for sub in value.values():
                found |= keywords(sub)
        elif isinstance(value, dict):
            found |= keywords(value)
    return found


def test_schemas_use_only_keywords_the_loop_enforces(ctx):
    from traider.brain import SCHEMA_KEYWORDS
    for research in (None, lambda q: "ответ"):
        ctx.research = research
        for t in build_tools(ctx):
            assert keywords(t.input_schema) <= SCHEMA_KEYWORDS, (t.name, keywords(t.input_schema) - SCHEMA_KEYWORDS)


def test_symbol_lists_are_bounded_by_the_loop_validator(ctx):
    from traider.brain import _check
    symbols = list(ctx.cfg.universe)
    for name in ("get_bars", "get_news"):
        assert _check(schema_of(ctx, name), {"symbols": symbols[:11]})
        assert _check(schema_of(ctx, name), {"symbols": symbols[:10]}) is None
    assert _check(schema_of(ctx, "get_bars"), {"symbols": []})


# --- цены держимых бумаг и время событий ---

def test_cage_values_held_positions_at_the_current_price(ctx):
    panel(ctx)
    assert buy(ctx, "AAPL", 190.0).startswith("Куплено")
    ctx.market.prices["AAPL"] *= 0.5            # позиция обесценилась: капитал упал, лимит 20% тоже
    out = buy(ctx, "MSFT", 190.0)   # по цене входа капитал 1000 и лимит 200: заявка прошла бы
    assert out.startswith("Отказ") and code(ctx) == "position_too_large"


def test_reject_events_carry_the_market_clock(ctx):
    from tests.fakes import ny
    ctx.market.clock = ny(2031, 3, 4, 11, 22)
    call(ctx, "buy", symbol="AAPL", notional=50.0, stop_price=1.0, thesis="т")
    assert last(ctx)["kind"] == "reject" and last(ctx)["ts"] == ctx.market.now().isoformat()
