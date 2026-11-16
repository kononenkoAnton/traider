import json
import os
import pathlib
import signal
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import pytest

from tests.conftest import _make
from tests.fakes import FakeAnthropic, FakeMarket, ny, use
from traider import cli
from traider.brokers import SimBroker
from traider.cli import main
from traider.config import load_run, start_experiment
from traider.control import pause
from traider.heartbeat import beat
from traider.store import RunStore

SECRET = "/ping/3f9c-secret-uuid"
ENV = "ALPACA_API_KEY=a\nALPACA_SECRET_KEY=b\nANTHROPIC_API_KEY=c\n"


@pytest.fixture
def watchdog():
    """Сторож в этом процессе: записывает метод, путь и тело каждого запроса и отвечает 200."""
    got = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            got.append((self.command, self.path, self.rfile.read(int(self.headers["Content-Length"] or 0)).decode()))
            self.send_response(404 if self.path.startswith("/nope") else 200)
            self.end_headers()
            self.wfile.write(b"OK")

        def log_message(self, *a):
            pass

    before = set(threading.enumerate())
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield SimpleNamespace(url=f"http://127.0.0.1:{server.server_address[1]}{SECRET}", got=got)
    server.shutdown()
    server.server_close()
    thread.join(5)
    assert not set(threading.enumerate()) - before and server.socket.fileno() == -1


@pytest.fixture
def refused():
    """Адрес, на котором никто не слушает: соединение отклоняется."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return f"http://127.0.0.1:{port}{SECRET}"


# --- beat ---

def test_ok_posts_the_text_to_the_url_as_given(watchdog):
    assert beat(watchdog.url, True, "строка\nвторая") is None
    assert watchdog.got == [("POST", SECRET, "строка\nвторая")]


def test_not_ok_posts_to_the_url_with_fail_appended(watchdog):
    assert beat(watchdog.url + "/", False, "x") is None
    assert watchdog.got == [("POST", SECRET + "/fail", "x")]


def test_the_text_is_cut_to_2000_characters(watchdog):
    beat(watchdog.url, True, "я" * 2500)
    assert watchdog.got[0][2] == "я" * 2000


@pytest.mark.parametrize("url", [None, ""])
def test_no_url_sends_nothing(watchdog, url):
    assert beat(url, False, "x") is None and watchdog.got == []


@pytest.mark.parametrize("url", ["ftp://127.0.0.1/ping/x", "file:///etc/passwd", "127.0.0.1/ping/x"])
def test_another_scheme_is_refused_without_a_request(watchdog, url):
    err = beat(url, True, "x")
    assert err and "ping" not in err and watchdog.got == []


def test_a_server_that_never_answers_ends_within_the_timeout(silent_server):
    start = time.monotonic()
    err = beat(silent_server + SECRET.lstrip("/"), True, "x", timeout=0.5)
    assert time.monotonic() - start < 1.0 and err and "secret" not in err and "ping" not in err


def test_a_refused_connection_is_an_error_text_without_the_url(refused):
    err = beat(refused + "?k=secret", False, "x")
    assert err and "secret" not in err and "ping" not in err and "127.0.0.1" not in err


# --- через команду tick ---

def _world(base, monkeypatch, url=None, alpaca=False, client=None, env=ENV):
    """Настоящий тик на подделках в каталоге base. Без alpaca все участники на sim; с alpaca opus-a на Alpaca,
    и его API не готов (брокер падает каждый тик)."""
    base.mkdir(exist_ok=True)
    monkeypatch.chdir(base)
    (base / ".env").write_text(env + (f"HEALTHCHECK_URL={url}\n" if url else ""))
    toml = _make(base, (lambda t: t) if alpaca else (lambda t: t.replace('broker = "alpaca"', 'broker = "sim"')))
    exp = start_experiment(toml, base / "runs")
    market = FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70, clock=ny(2026, 12, 1, 10, 5))
    market.open = True

    def not_ready(cfg, env):
        raise RuntimeError("счёт Alpaca не на 1000 долларов")
    monkeypatch.setattr(cli, "AlpacaMarket", lambda key, secret: market)
    monkeypatch.setattr(cli, "make_alpaca_api", not_ready)
    monkeypatch.setattr(cli, "Anthropic", lambda **kw: FakeAnthropic(
        client or [use("get_account"), use("finish_session", journal="ок")]))
    return exp, market


def _files(base) -> dict:
    return {str(p.relative_to(base)): p.read_bytes() for p in sorted((base / "runs").rglob("*")) if p.is_file()}


def _no_secret(base, cap):
    assert "secret" not in cap.out + cap.err
    assert not [k for k, v in _files(base).items() if b"secret" in v]


def test_closed_market_sends_one_ok_beat(tmp_path, watchdog, capsys, monkeypatch):
    _, market = _world(tmp_path, monkeypatch, watchdog.url)
    market.open = False
    assert main(["tick"]) == 0
    assert watchdog.got == [("POST", SECRET, "тик пуст: рынок закрыт")]
    _no_secret(tmp_path, capsys.readouterr())


def test_a_session_tick_sends_one_ok_beat_with_a_line_per_run(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url)
    assert main(["tick"]) == 0
    cap = capsys.readouterr()
    [(method, path, body)] = watchdog.got
    lines = cap.out.splitlines()
    assert (method, path) == ("POST", SECRET) and len(lines) == 5 and all(": session ok" in l for l in lines)
    assert body.splitlines() == ["прогонов в тике: 5", *lines] and cap.err == ""
    _no_secret(tmp_path, cap)


def test_a_paused_day_is_an_ok_beat(tmp_path, watchdog, capsys, monkeypatch):
    exp, _ = _world(tmp_path, monkeypatch, watchdog.url)
    pause(exp, "2026-12-01")
    assert main(["tick"]) == 0
    assert [p for _, p, _ in watchdog.got] == [SECRET] and "paused" in watchdog.got[0][2]


def test_a_run_whose_broker_raises_sends_one_fail_beat(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url, alpaca=True)
    assert main(["tick"]) == 0
    cap = capsys.readouterr()
    [(_, path, body)] = watchdog.got
    assert path == SECRET + "/fail" and "pilot-1/opus-a: error" in body and "pilot-1/opus-a: error" in cap.out
    _no_secret(tmp_path, cap)


def test_a_death_in_the_tick_sends_one_fail_beat(tmp_path, watchdog, capsys, monkeypatch):
    exp, market = _world(tmp_path, monkeypatch, watchdog.url)
    run = exp / "opus-b"
    SimBroker(load_run(run)[0], RunStore(run), market).buy("2026-12-01", "AAPL", 900.0, 1.0, "т", "seed/1")
    market.prices["AAPL"] *= 0.7
    assert main(["tick"]) == 0
    [(_, path, body)] = watchdog.got
    assert path == SECRET + "/fail" and "pilot-1/opus-b: died" in body
    assert RunStore(run).state()["status"] == "dead"


def test_an_invalid_run_sends_one_fail_beat_naming_it_and_without_it_the_beat_is_ok(tmp_path, watchdog, capsys, monkeypatch):
    exp, _ = _world(tmp_path / "invalid", monkeypatch, watchdog.url)
    cfg = exp / "haiku" / "config.json"
    cfg.write_text(cfg.read_text().replace('"seed": 20261114', '"seed": 1'))   # контрольная сумма не сходится
    assert main(["tick"]) == 0
    [(_, path, body)] = watchdog.got
    assert path == SECRET + "/fail" and "pilot-1/haiku: invalid" in body
    assert sum(": session ok" in l for l in body.splitlines()) == 4
    watchdog.got.clear()
    _world(tmp_path / "healthy", monkeypatch, watchdog.url)
    assert main(["tick"]) == 0
    assert [p for _, p, _ in watchdog.got] == [SECRET]


def test_a_failed_session_sends_one_fail_beat(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url, client=[RuntimeError("обрыв")])
    assert main(["tick"]) == 0
    [(_, path, body)] = watchdog.got
    assert path == SECRET + "/fail" and ": session failed" in body


def test_a_failed_auto_report_sends_one_fail_beat(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url)
    monkeypatch.setattr(cli, "write_report", lambda *a: (_ for _ in ()).throw(RuntimeError("диск полон")))
    assert main(["tick"]) == 0
    assert [p for _, p, _ in watchdog.got] == [SECRET + "/fail"] and "диск полон" in capsys.readouterr().err


def test_a_tick_that_could_not_start_sends_one_fail_beat(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url)
    monkeypatch.setattr(cli, "tick", lambda *a: (_ for _ in ()).throw(ConnectionError("рынок недоступен")))
    assert main(["tick"]) == 1
    [(_, path, body)] = watchdog.got
    assert path == SECRET + "/fail" and "рынок недоступен" in body


def test_an_exception_out_of_the_tick_sends_a_fail_beat_and_propagates_unchanged(tmp_path, watchdog, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url)
    boom = KeyboardInterrupt()
    monkeypatch.setattr(cli, "tick", lambda *a: (_ for _ in ()).throw(boom))
    with pytest.raises(KeyboardInterrupt) as e:
        main(["tick"])
    assert e.value is boom and [p for _, p, _ in watchdog.got] == [SECRET + "/fail"]


def test_without_the_url_nothing_is_sent_and_the_output_is_unchanged(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path / "with", monkeypatch, watchdog.url)
    assert main(["tick"]) == 0
    with_url = capsys.readouterr()
    watchdog.got.clear()
    _world(tmp_path / "without", monkeypatch)
    assert main(["tick"]) == 0
    without = capsys.readouterr()
    assert watchdog.got == [] and without.out == with_url.out and without.err == with_url.err == ""


def test_an_unreachable_watchdog_changes_nothing_but_one_stderr_line(tmp_path, refused, capsys, monkeypatch):
    _world(tmp_path / "plain", monkeypatch)
    assert main(["tick"]) == 0
    plain = capsys.readouterr()
    _world(tmp_path / "beat", monkeypatch, refused)
    assert main(["tick"]) == 0
    cap = capsys.readouterr()
    assert cap.out == plain.out and _files(tmp_path / "beat") == _files(tmp_path / "plain")
    assert cap.err.splitlines() == [l for l in cap.err.splitlines() if l.startswith("сторож недоступен: ")] != []
    assert len(cap.err.splitlines()) == 1
    _no_secret(tmp_path / "beat", cap)


def test_other_commands_send_nothing(tmp_path, watchdog, capsys, monkeypatch):
    exp, _ = _world(tmp_path, monkeypatch, watchdog.url)
    for argv in (["status"], ["report"], ["pause"], ["resume"], ["stop", "pilot-1"]):
        main(argv)
    assert watchdog.got == []


@pytest.mark.parametrize("row, failed", [
    ({"action": "safety"}, False), ({"action": "unpriced", "unpriced": ["AAPL"]}, False), ({"action": "paused"}, False),
    ({"action": "missed"}, False), ({"action": "finishing"}, False), ({"action": "finished"}, False),
    ({"action": "session", "status": "cap"}, False), ({"action": "session", "status": "ok"}, False),
    ({"action": "error", "error": "x"}, True), ({"action": "died"}, True), ({"action": "invalid"}, True), ({"action": "stale_failed"}, True),
    ({"action": "session", "status": "failed"}, True), ({"action": "safety", "error": "счёт не обновлён"}, True),
    ({"action": "safety", "control_error": "control.json"}, True)])
def test_which_rows_make_the_tick_failed(row, failed):
    assert cli._failed(row) is failed


def test_an_http_error_status_is_an_error_text_without_the_url(watchdog):
    err = beat(watchdog.url.replace(SECRET, "/nope-secret"), True, "x")
    assert err == "HTTPError 404" and len(watchdog.got) == 1


def test_text_cannot_break_the_encoding(watchdog):
    assert beat(watchdog.url, True, "a\ud800b") is None and watchdog.got == [("POST", SECRET, "a?b")]


def test_an_empty_tick_on_an_open_market_says_so(tmp_path, watchdog, capsys, monkeypatch):
    _, market = _world(tmp_path, monkeypatch, watchdog.url)
    (tmp_path / "runs" / ".tick.lock").write_text(json.dumps({"pid": 1, "ts": market.clock.isoformat(), "token": "x"}))
    assert main(["tick"]) == 0 and capsys.readouterr().out == ""
    assert watchdog.got == [("POST", SECRET, "тик пуст: рынок открыт, обходить нечего или другой тик ещё идёт")]


def test_env_values_and_the_home_directory_are_cut_from_the_text(tmp_path, watchdog, capsys, monkeypatch):
    key = "AKTEST1234567890"
    _world(tmp_path, monkeypatch, watchdog.url, alpaca=True, env=f"ALPACA_API_KEY={key}\nALPACA_SECRET_KEY=b\nANTHROPIC_API_KEY=c\n")
    def leaky(cfg, env):
        raise RuntimeError(f"ключ {env['ALPACA_API_KEY']} отклонён, файл {pathlib.Path.home() / 'x.txt'}")
    monkeypatch.setattr(cli, "make_alpaca_api", leaky)
    assert main(["tick"]) == 0
    [(_, _, body)] = watchdog.got
    assert key not in body and "ключ *** отклонён" in body
    assert str(pathlib.Path.home()) not in body and "файл ~/x.txt" in body


def test_a_failure_inside_the_heartbeat_changes_neither_the_exit_code_nor_the_exception(tmp_path, watchdog, capsys, monkeypatch):
    _world(tmp_path, monkeypatch, watchdog.url)
    def no_home():
        raise RuntimeError("нет домашнего каталога")
    monkeypatch.setattr(cli.Path, "home", staticmethod(no_home))
    assert main(["tick"]) == 0
    assert [l for l in capsys.readouterr().err.splitlines() if l.startswith("сторож недоступен")] != []
    boom = KeyboardInterrupt()
    monkeypatch.setattr(cli, "tick", lambda *a: (_ for _ in ()).throw(boom))
    with pytest.raises(KeyboardInterrupt) as e:
        main(["tick"])
    assert e.value is boom


def test_sigterm_during_a_tick_exits_143_releases_the_lock_and_sends_one_fail_beat(tmp_path, watchdog, capsys, monkeypatch):
    """launchd останавливает задание сигналом SIGTERM посреди сессии."""
    _world(tmp_path, monkeypatch, watchdog.url)
    def killed(**kw):
        os.kill(os.getpid(), signal.SIGTERM)
        return FakeAnthropic([use("finish_session", journal="ок")])
    monkeypatch.setattr(cli, "Anthropic", killed)
    caught = []
    def before(signum, frame):
        caught.append(signum)   # прежний обработчик теста: без него SIGTERM убил бы сам pytest
    prev = signal.signal(signal.SIGTERM, before)
    try:
        with pytest.raises(SystemExit) as e:
            main(["tick"])
        assert signal.getsignal(signal.SIGTERM) is before and caught == []
    finally:
        signal.signal(signal.SIGTERM, prev)
    assert e.value.code == 143 and not (tmp_path / "runs" / ".tick.lock").exists()
    assert [p for _, p, _ in watchdog.got] == [SECRET + "/fail"]
