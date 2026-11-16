"""Страница управления: сервер на 127.0.0.1 поднимается в потоке этого процесса на свободном порту, запросы идут через
http.client с явным заголовком Host. Фикстура останавливает сервер и проверяет, что ни поток, ни порт не остались."""
import ast
import http.client
import json
import pathlib
import shutil
import signal
import socket
import threading
import time
from types import SimpleNamespace

import pytest

from traider import cli, control, serve
from traider.config import start_experiment
from traider.control import read_control
from traider.serve import main_serve, make_server

TEMPLATE = pathlib.Path("traider/dashboard.html").read_text(encoding="utf-8")


def request(base, method, path, host, token=None, origin=None, body=None, raw=None, headers=()):
    conn = http.client.HTTPConnection(*base, timeout=10)
    try:
        h = {"Host": host, **dict(headers)}
        if token is not None:
            h["X-Traider-Token"] = token
        if origin is not None:
            h["Origin"] = origin
        data = raw if raw is not None else None if body is None else json.dumps(body).encode()
        conn.request(method, path, data, h)
        r = conn.getresponse()
        text = r.read()
        return SimpleNamespace(status=r.status, headers=r.headers, body=text,
                               json=json.loads(text) if r.headers.get("Content-Type", "").startswith("application/json") else None)
    finally:
        conn.close()


def get(base, path, host, **kw):
    return request(base, "GET", path, host, **kw)


def post(base, path, host, **kw):
    return request(base, "POST", path, host, **kw)


@pytest.fixture
def runs(tmp_path, example):
    start_experiment(example, tmp_path / "runs")
    return tmp_path / "runs"


@pytest.fixture
def served(runs, monkeypatch):
    """(адрес, порт, токен, runs); сегодня в Нью-Йорке 2026-12-02."""
    monkeypatch.setattr(serve, "today", lambda: "2026-12-02")
    before = set(threading.enumerate())
    server = make_server(runs, port=0)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    token = get(("127.0.0.1", port), "/e/pilot-1/control", host=f"127.0.0.1:{port}").json["token"]
    yield ("127.0.0.1", port), port, token, runs
    server.shutdown()
    server.server_close()
    thread.join(5)
    for t in set(threading.enumerate()) - before:
        t.join(5)
    assert not set(threading.enumerate()) - before and server.socket.fileno() == -1
    with pytest.raises(ConnectionRefusedError):
        socket.create_connection(("127.0.0.1", port), timeout=1).close()


# --- из задания ---

def test_page_and_state_are_served_on_localhost_only(served):
    base, port, token, runs = served
    assert get(base, "/e/pilot-1/control", host=f"127.0.0.1:{port}").json["state"] == "running"
    assert get(base, "/e/pilot-1/", host=f"localhost:{port}").status == 200


def test_foreign_host_header_is_refused(served):
    base, port, token, _ = served
    assert get(base, "/e/pilot-1/control", host="evil.example").status == 403
    assert get(base, "/e/pilot-1/control", host=f"evil.example:{port}").status == 403
    assert get(base, "/e/pilot-1/control", host=f"127.0.0.1:{port + 1}").status == 403
    assert get(base, "/", host="localhost").status == 403
    assert post(base, "/e/pilot-1/pause", host="evil.example", token=token).status == 403


def test_actions_need_the_token_and_no_foreign_origin(served):
    base, port, token, runs = served
    assert post(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}").status == 403                      # без токена
    assert post(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}", token=token + "x").status == 403
    assert post(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}", token=token, origin="https://evil.example").status == 403
    assert post(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}", token=token, origin="null").status == 403
    assert not (runs / "pilot-1" / "control.json").exists()
    assert post(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}", token=token).json["state"] == "paused"
    assert read_control(runs / "pilot-1")["pauses"][0]["to"] is None


def test_stop_needs_the_experiment_name_typed(served):
    base, port, token, runs = served
    assert post(base, "/e/pilot-1/stop", host=f"127.0.0.1:{port}", token=token, body={}).status == 400
    assert post(base, "/e/pilot-1/stop", host=f"127.0.0.1:{port}", token=token, body={"confirm": "pilot"}).status == 400
    assert read_control(runs / "pilot-1")["stop"] is None
    assert post(base, "/e/pilot-1/stop", host=f"127.0.0.1:{port}", token=token, body={"confirm": "pilot-1"}).json["state"] == "stopping"


def test_unknown_experiment_and_traversal_are_404(served):
    base, port, token, runs = served
    (runs / "logs").mkdir()                                      # каталог без прогонов не эксперимент
    for path in ("/e/nope/", "/e/../../etc/passwd", "/e/pilot-1/../../.env", "/secret", "/e/logs/", "/e/pilot-1",
                 "/e/pilot-1/dashboard.html", "/e/pilot-1/control.json", "/e/%2e%2e/", "/e/pilot-1/opus-a/config.json"):
        assert get(base, path, host=f"127.0.0.1:{port}").status == 404, path
    for path in ("/e/nope/pause", "/e/pilot-1/", "/e/pilot-1/control", "/e/logs/pause"):
        assert post(base, path, host=f"127.0.0.1:{port}", token=token).status == 404, path
    assert get(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}").status == 404


def test_no_cors_headers_and_the_safety_headers_are_on_every_response(served):
    base, port, token, _ = served
    answers = [get(base, "/e/pilot-1/control", host=f"127.0.0.1:{port}"),
               get(base, "/e/pilot-1/", host=f"127.0.0.1:{port}"),
               get(base, "/nope", host=f"127.0.0.1:{port}"),
               get(base, "/", host="evil.example"),
               post(base, "/e/pilot-1/pause", host=f"127.0.0.1:{port}", origin="https://evil.example"),
               post(base, "/e/pilot-1/stop", host=f"127.0.0.1:{port}", token=token, body={}),
               post(base, "/e/pilot-1/resume", host=f"127.0.0.1:{port}", token=token),
               request(base, "OPTIONS", "/e/pilot-1/pause", host=f"127.0.0.1:{port}", headers={"Origin": "https://evil.example"})]
    assert [a.status for a in answers] == [200, 200, 404, 403, 403, 400, 200, 501]
    for a in answers:
        assert not [k for k in a.headers if k.lower().startswith("access-control-")]
        assert a.headers["Cache-Control"] == "no-store" and a.headers["X-Content-Type-Options"] == "nosniff"
        assert a.headers["X-Frame-Options"] == "DENY" and a.headers["Content-Security-Policy"] == "frame-ancestors 'none'"


def test_control_answer_is_not_cached_and_carries_the_token(served):
    base, port, token, runs = served
    r = get(base, "/e/pilot-1/control?x=1", host=f"localhost:{port}")
    assert r.headers["Cache-Control"] == "no-store" and r.headers["Content-Type"].startswith("application/json")
    assert r.json == control.summary(runs / "pilot-1") | {"token": token}


def test_template_has_the_control_bar_and_no_external_address():
    t = TEMPLATE
    for word in ["работает", "на паузе с", "останавливается", "завершается", "завершён", "файл управления повреждён",
                 "нет связи с сервером управления", "Пауза", "Продолжить", "Остановить всё", "Отмена", "Отчёт ещё не собран"]:
        assert word in t, word
    assert 'id="control"' in t and 'aria-live="polite"' in t and "<dialog" in t and "showModal" in t
    assert 'location.protocol !== "file:"' in t and 'ask("control")' in t and "X-Traider-Token" in t
    assert "prompt(" not in t and "confirm(" not in t and "innerHTML" not in t
    assert "https://" not in t and "http://" not in t.replace("http://www.w3.org", "")


# --- сверх задания ---

def test_server_listens_on_loopback_only_and_the_address_cannot_be_set(runs):
    assert list(__import__("inspect").signature(make_server).parameters) == ["runs_dir", "port", "token"]
    server = make_server(runs, port=0)
    try:
        assert server.server_address[0] == "127.0.0.1" and server.daemon_threads
        assert 0 < server.RequestHandlerClass.timeout <= 60                       # зависшее соединение не держит поток
    finally:
        server.server_close()


def test_each_server_start_has_its_own_random_token(runs):
    a, b = make_server(runs, port=0), make_server(runs, port=0)
    try:
        assert a.token != b.token and len(a.token) >= 32
        assert make_server.__defaults__ == (8787, None)
    finally:
        a.server_close(); b.server_close()


def test_root_goes_to_the_only_unfinished_experiment_or_lists_them(served):
    base, port, token, runs = served
    r = get(base, "/", host=f"127.0.0.1:{port}")
    assert r.status == 302 and r.headers["Location"] == "/e/pilot-1/"
    shutil.copytree(runs / "pilot-1", runs / "pilot-2")
    r = get(base, "/", host=f"127.0.0.1:{port}")
    assert r.status == 200 and b'href="/e/pilot-1/"' in r.body and b'href="/e/pilot-2/"' in r.body


def test_unbuilt_report_is_the_same_template_and_a_built_one_is_served_as_is(served):
    base, port, token, runs = served
    r = get(base, "/e/pilot-1/", host=f"127.0.0.1:{port}")
    assert r.status == 200 and r.headers["Content-Type"] == "text/html; charset=utf-8"
    page = r.body.decode()
    assert 'id="control"' in page and '"not_built": true' in page and '"experiment": "pilot-1"' in page
    assert page.replace(page[page.index("const DATA = "):page.index(";\n", page.index("const DATA = "))], "const DATA = /*DATA*/null") == TEMPLATE
    (runs / "pilot-1" / "dashboard.html").write_text("<p>отчёт</p>", encoding="utf-8")
    assert get(base, "/e/pilot-1/", host=f"127.0.0.1:{port}").body == "<p>отчёт</p>".encode()


def test_bad_bodies_are_400(served):
    base, port, token, runs = served
    host = f"127.0.0.1:{port}"
    assert post(base, "/e/pilot-1/stop", host=host, token=token, raw=b"{not json").status == 400
    assert post(base, "/e/pilot-1/stop", host=host, token=token, raw=b'["pilot-1"]').status == 400
    assert post(base, "/e/pilot-1/stop", host=host, token=token, body={"confirm": "pilot-1", "pad": "x" * 5000}).status == 400
    conn = http.client.HTTPConnection(*base, timeout=10)                       # без Content-Length
    try:
        conn.putrequest("POST", "/e/pilot-1/pause", skip_host=True)
        conn.putheader("Host", host); conn.putheader("X-Traider-Token", token); conn.endheaders()
        r = conn.getresponse(); r.read()
        assert r.status == 400
    finally:
        conn.close()
    assert read_control(runs / "pilot-1") == {"pauses": [], "stop": None}
    assert post(base, "/e/pilot-1/resume", host=host, token=token, raw=b"").status == 200


def test_refused_action_is_409_and_the_file_is_kept(served):
    base, port, token, runs = served
    host = f"127.0.0.1:{port}"
    post(base, "/e/pilot-1/stop", host=host, token=token, body={"confirm": "pilot-1"})
    r = post(base, "/e/pilot-1/pause", host=host, token=token)
    assert r.status == 409 and "остановка уже запрошена" in r.json["error"]
    path = runs / "pilot-1" / "control.json"
    path.write_text("{oops", encoding="utf-8")
    for action in ("pause", "resume"):
        assert post(base, f"/e/pilot-1/{action}", host=host, token=token).status == 409
    assert post(base, "/e/pilot-1/stop", host=host, token=token, body={"confirm": "pilot-1"}).status == 409
    assert path.read_text(encoding="utf-8") == "{oops"


def test_damaged_control_file_is_shown_as_damaged(served):
    base, port, token, runs = served
    (runs / "pilot-1" / "control.json").write_text("{oops", encoding="utf-8")
    r = get(base, "/e/pilot-1/control", host=f"127.0.0.1:{port}")
    assert r.status == 200 and r.json["state"] == "damaged" and r.json["experiment"] == "pilot-1"
    assert "control.json" in r.json["error"] and r.json["token"] == token


def test_a_stop_at_the_same_moment_as_a_pause_is_not_lost(served, monkeypatch):
    base, port, token, runs = served
    real = control._write

    def slow(exp_dir, c):                                        # пауза прочитала файл без остановки и пишет медленно
        if not c["stop"]:
            time.sleep(.4)
        return real(exp_dir, c)
    monkeypatch.setattr(control, "_write", slow)
    host = f"127.0.0.1:{port}"
    paused = threading.Thread(target=post, args=(base, "/e/pilot-1/pause", host), kwargs={"token": token})
    paused.start()
    time.sleep(.1)
    assert post(base, "/e/pilot-1/stop", host=host, token=token, body={"confirm": "pilot-1"}).status == 200
    paused.join(5)
    c = read_control(runs / "pilot-1")
    assert c["stop"] is not None and c["stop"]["day"] == "2026-12-02"


def test_the_server_builds_no_broker_reads_no_env_and_opens_no_connection():
    tree = ast.parse(pathlib.Path(serve.__file__).read_text(encoding="utf-8"))
    names = {a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    names |= {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert names <= {"hmac", "html", "json", "re", "secrets", "signal", "sys", "threading", "datetime", "http.server",
                     "pathlib", "traider.control", "traider.dashboard", "traider.market"}, names
    imported = {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module == "traider.market" for a in n.names}
    assert imported <= {"NY", "today"}                                            # из рынка только часы, без клиента


def test_main_serve_says_where_to_open_and_returns_0_on_ctrl_c_and_sigterm(runs, monkeypatch, capsys):
    for stop in (lambda: (_ for _ in ()).throw(KeyboardInterrupt), lambda: signal.raise_signal(signal.SIGTERM)):
        before = signal.getsignal(signal.SIGTERM)
        monkeypatch.setattr(serve.ThreadingHTTPServer, "serve_forever", lambda self, *a, stop=stop: stop())
        assert main_serve(runs, 0) == 0
        assert signal.getsignal(signal.SIGTERM) is before
        out = capsys.readouterr().out
        assert "http://localhost:" in out and "Ctrl+C" in out and "ssh -N -L" in out


def test_main_serve_on_a_busy_port_is_1_with_one_line(runs, capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0)); busy.listen()
        port = busy.getsockname()[1]
        assert main_serve(runs, port) == 1
    err = capsys.readouterr()
    assert err.err.count("\n") == 1 and str(port) in err.err and err.out == ""


def test_cli_serve_passes_the_port(monkeypatch):
    seen = []
    monkeypatch.setattr(cli, "main_serve", lambda runs, port: seen.append((runs, port)) or 0)
    assert cli.main(["serve"]) == 0 and cli.main(["serve", "--port", "9000"]) == 0
    assert seen == [(cli.RUNS, 8787), (cli.RUNS, 9000)]
