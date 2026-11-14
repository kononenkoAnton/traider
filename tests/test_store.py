from datetime import datetime, timedelta, timezone

from traider.cage import Account, Position, SessionFacts
from traider.config import active_runs, start_experiment
from traider.store import RunStore


def test_orders_get_increasing_seq_and_survive_reopen(tmp_path):
    s = RunStore(tmp_path)
    assert s.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "XLE", "client_order_id": "x/1"})["seq"] == 1
    assert RunStore(tmp_path).append_order({"day": "2026-12-01", "kind": "reject", "symbol": "AAPL", "reason": "no_price"})["seq"] == 2
    assert [e["kind"] for e in RunStore(tmp_path).orders("2026-12-01")] == ["buy", "reject"]
    assert RunStore(tmp_path).find_order("x/1")["symbol"] == "XLE"

def test_truncated_last_line_is_skipped(tmp_path):
    s = RunStore(tmp_path)
    s.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "XLE"})
    with open(tmp_path / "orders.jsonl", "a") as f:
        f.write('{"seq": 2, "kind": "bu')
    assert len(RunStore(tmp_path).orders()) == 1
    assert RunStore(tmp_path).append_order({"day": "2026-12-01", "kind": "close", "symbol": "XLE"})["seq"] == 2
    assert [e["kind"] for e in RunStore(tmp_path).orders()] == ["buy", "close"]

def test_session_can_start_only_once_per_day(tmp_path):
    s, now = RunStore(tmp_path), datetime(2026, 12, 1, 15, 0, tzinfo=timezone.utc)
    assert s.start_session("2026-12-01", now) is True
    assert s.start_session("2026-12-01", now) is False
    assert s.session("2026-12-01")["status"] == "started"
    assert s.stale_started(now + timedelta(minutes=29)) == []
    assert s.stale_started(now + timedelta(minutes=31)) == ["2026-12-01"]
    s.finish_session("2026-12-01", "ok", {"cost": 0.9})
    assert (s.session("2026-12-01")["status"], s.session("2026-12-01")["cost"]) == ("ok", 0.9)
    assert s.stale_started(now + timedelta(hours=5)) == []
    assert [x["day"] for x in s.sessions()] == ["2026-12-01"]

def test_facts_come_from_the_ledger(tmp_path):
    s, day, panel = RunStore(tmp_path), "2026-12-01", ["AAPL", "MSFT"]
    assert s.facts(day, panel) == SessionFacts(0, frozenset(), frozenset(), False)
    s.append_forecasts(day, [{"symbol": "AAPL", "p": 0.6}, {"symbol": "XLE", "p": 0.55}])
    assert s.facts(day, panel).panel_done is False
    s.append_forecasts(day, [{"symbol": "MSFT", "p": 0.4}])
    for kind, symbol in [("buy", "XLE"), ("reject", "AAPL"), ("close", "JPM"), ("stop_fill", "COST")]:
        s.append_order({"day": day, "kind": kind, "symbol": symbol})
    s.append_order({"day": "2026-11-30", "kind": "buy", "symbol": "NVDA"})
    f = s.facts(day, panel)
    assert (f.buys_done, f.panel_done) == (1, True)
    assert f.closed_today == frozenset({"JPM", "COST"}) and f.forecast_symbols == frozenset({"AAPL", "XLE", "MSFT"})

def test_state_and_account_roundtrip(tmp_path):
    s = RunStore(tmp_path)
    assert s.state()["status"] == "active" and s.account() is None
    s.update_state(status="dead")
    acct = Account(850.0, (Position("AAPL", 0.75, 200.1, 186.0),), 1000.0)
    s.save_account(acct)
    assert RunStore(tmp_path).state()["status"] == "dead" and RunStore(tmp_path).account() == acct

def test_prices_roundtrip(tmp_path):
    s = RunStore(tmp_path)
    assert s.prices("2026-12-01") is None
    s.save_prices("2026-12-01", {"AAPL": 190.5})
    assert RunStore(tmp_path).prices("2026-12-01") == {"AAPL": 190.5}

def test_active_runs_skips_finished_and_dead(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    RunStore(exp / "haiku").update_state(status="dead")
    RunStore(exp / "sonnet").update_state(status="finishing")
    RunStore(exp / "opus-a").update_state(status="invalid")      # HP и стопы работают и у прогона с плохой суммой
    RunStore(exp / "opus-b").update_state(status="finished")
    assert sorted(p.name for p in active_runs(tmp_path / "runs")) == ["opus-a", "opus-analyst", "sonnet"]

def test_damaged_session_file_is_a_stale_started_day(tmp_path):
    s, day = RunStore(tmp_path), "2026-12-01"
    path = tmp_path / "sessions" / f"{day}.json"
    path.parent.mkdir()
    path.write_text("")
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    assert s.session(day)["status"] == "started"
    assert s.start_session(day, mtime) is False
    assert s.stale_started(mtime + timedelta(minutes=29)) == []
    assert s.stale_started(mtime + timedelta(minutes=31)) == [day]
    s.finish_session(day, "failed", {})
    assert s.session(day)["status"] == "failed" and s.stale_started(mtime + timedelta(hours=5)) == []

def test_start_session_leaves_no_empty_target(tmp_path, monkeypatch):
    import traider.store as store
    def boom(*a, **k):
        raise OSError("crash")
    monkeypatch.setattr(store.os, "link", boom)
    try:
        RunStore(tmp_path).start_session("2026-12-01", datetime(2026, 12, 1, tzinfo=timezone.utc))
    except OSError:
        pass
    assert list((tmp_path / "sessions").iterdir()) == []

def test_unicode_line_separators_do_not_split_records(tmp_path):
    s, day = RunStore(tmp_path), "2026-12-01"
    s.append_order({"day": day, "kind": "buy", "symbol": "XLE", "thesis": "a b c\u0085d"})
    s.append_forecasts(day, [{"symbol": "a b", "p": 0.5}])
    s2 = RunStore(tmp_path)
    assert s2.orders()[0]["thesis"] == "a b c\u0085d"
    assert s2.forecasts()[0]["symbol"] == "a b"
    assert s2.append_order({"day": day, "kind": "close", "symbol": "XLE"})["seq"] == 2

def test_start_session_loses_race_cleanly(tmp_path, monkeypatch):
    import json, traider.store as store
    s, day = RunStore(tmp_path), "2026-12-01"
    real_link = store.os.link
    def racing_link(src, dst):
        (tmp_path / "sessions").mkdir(exist_ok=True)
        open(dst, "w").write(json.dumps({"day": day, "status": "started", "started_at": "winner"}))
        return real_link(src, dst)
    monkeypatch.setattr(store.os, "link", racing_link)
    assert s.start_session(day, datetime(2026, 12, 1, tzinfo=timezone.utc)) is False
    assert s.session(day)["started_at"] == "winner"
    assert [p.name for p in (tmp_path / "sessions").iterdir()] == [f"{day}.json"]

def test_active_runs_returns_run_with_damaged_state(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    (exp / "haiku" / "state.json").write_text("garbage")
    (exp / "sonnet" / "state.json").write_text("[1]")             # JSON, но не объект
    assert {"haiku", "sonnet"} <= {p.name for p in active_runs(tmp_path / "runs")}
    import pytest
    with pytest.raises(ValueError):
        RunStore(exp / "haiku").state()

def test_non_finite_numbers_are_never_written(tmp_path):
    import pytest
    s = RunStore(tmp_path)
    s.append_order({"day": "2026-12-01", "kind": "buy", "symbol": "XLE"})
    before = (tmp_path / "orders.jsonl").read_bytes()
    for bad in (float("nan"), float("inf")):
        with pytest.raises(ValueError):
            s.append_order({"day": "2026-12-01", "kind": "raise_stop", "symbol": "XLE", "stop_price": bad})
        with pytest.raises(ValueError):
            s.update_state(notes=bad)
    assert (tmp_path / "orders.jsonl").read_bytes() == before
    assert s.state()["notes"] == "" and list(tmp_path.glob("*.tmp")) == []

def test_find_order_matches_only_buys_and_closes(tmp_path):
    s = RunStore(tmp_path)
    s.append_order({"day": "2026-12-01", "kind": "reject", "symbol": "AAPL", "client_order_id": "x/9"})
    s.append_order({"day": "2026-12-01", "kind": "stop_fill", "symbol": "XLE", "client_order_id": None})
    assert s.find_order("x/9") is None and s.find_order(None) is None
    s.append_order({"day": "2026-12-01", "kind": "close", "symbol": "XLE", "client_order_id": "x/9"})
    assert s.find_order("x/9")["kind"] == "close"
