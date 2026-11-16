"""Сервер страницы управления: `traider serve [--port N]`, кнопки «Пауза», «Продолжить», «Остановить всё».

:wk-id: serve-module
:wk-tags: control, dashboard, server
:wk-categories: system-architecture

Как это работает. `make_server` слушает только `127.0.0.1` (другого адреса задать нельзя), порт 0 берёт свободный.
Маршруты сверяются с сырым путём запроса, строка запроса отбрасывается: `GET /` (один незавершённый эксперимент:
переход на его страницу, иначе список), `GET /e/<имя>/` (файл `dashboard.html` эксперимента или, пока его нет, тот же
шаблон с `meta.not_built`), `GET /e/<имя>/control` (`control.summary` за сегодня по `market.today` плюс токен; повреждённый `control.json` даёт
состояние `damaged` с текстом ошибки), `POST /e/<имя>/pause|resume|stop` (действие модуля control, ответ новый
`summary`). Сегмент имени раскодируется из %-записи (ссылки списка кодируют имя целиком) и годен, только если в
`runs_dir` есть такой каталог (не символическая ссылка) хотя бы с одним `*/config.json`; имя служит лишь ключом
словаря каталогов, к пути на диске не приставляется. Всё прочее 404, другие методы получают ответ стандартной библиотеки.

Защита от чужой веб-страницы в том же браузере: заголовок `Host` только `127.0.0.1:<порт>` или `localhost:<порт>`
(подмена DNS), иначе 403 на любой запрос; `POST` только с `X-Traider-Token`, равным случайному токену этого запуска
(`secrets.token_urlsafe`, сравнение `hmac.compare_digest`), и без `Origin` либо с собственным адресом, иначе 403.
Тело `POST` это объект JSON не длиннее 4096 байт по `Content-Length` (пустое годится), иначе 400; `stop` требует
`confirm`, равное имени эксперимента, иначе 400. Отказ модуля control (`ValueError`) это 409 с `{"error": текст}`.
Каждый ответ несёт `Cache-Control: no-store`, `nosniff`, `X-Frame-Options: DENY` и `frame-ancestors 'none'`
(чужая страница не обернёт кнопку в рамку), заголовков CORS нет. Действия идут по одному под замком сервера, так что
одновременные пауза и остановка не теряют остановку. Потоки демонические, у соединения таймаут.
Сервер пишет только `control.json` через модуль control: брокера не строит, `.env` не читает, наружу не ходит,
отчёт не собирает. День действия: `market.today`, как у команд `pause`, `resume`, `stop`.
"""
import hmac
import html
import json
import re
import secrets
import signal
import sys
import threading
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

from traider.control import pause, request_stop, resume, summary
from traider.dashboard import page
from traider.market import NY, today

HOST = "127.0.0.1"
MAX_BODY = 4096
SAFETY = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY",
          "Content-Security-Policy": "frame-ancestors 'none'"}
PAGE = re.compile(r"/e/([^/]+)/(control)?")
ACTION = re.compile(r"/e/([^/]+)/(pause|resume|stop)")


def _experiments(runs_dir: Path) -> dict[str, Path]:
    return {p.name: p for p in runs_dir.glob("*") if p.is_dir() and not p.is_symlink() and any(p.glob("*/config.json"))}


def _state(exp: Path) -> str:
    try:
        return summary(exp)["state"]
    except ValueError:
        return "damaged"


def make_server(runs_dir: Path, port: int = 8787, token: str | None = None) -> ThreadingHTTPServer:
    runs_dir, token, lock = Path(runs_dir), token or secrets.token_urlsafe(32), threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        timeout = 30   # секунд: зависшее соединение не держит поток

        def end_headers(self):   # и для ответов стандартной библиотеки (501, 400 на кривую строку запроса)
            for k, v in SAFETY.items():
                self.send_header(k, v)
            super().end_headers()

        def reply(self, status: int, body: bytes | str | dict, kind: str = "application/json", extra=()):
            data = json.dumps(body, ensure_ascii=False) if isinstance(body, dict) else body
            data = data.encode() if isinstance(data, str) else data
            self.send_response(status)
            for k, v in (("Content-Type", f"{kind}; charset=utf-8"), ("Content-Length", str(len(data))), *extra):
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def ours(self, value: str | None, scheme: str = "") -> bool:
            port = self.server.server_address[1]
            return value in (f"{scheme}{HOST}:{port}", f"{scheme}localhost:{port}")

        def route(self, pattern: re.Pattern):
            m = pattern.fullmatch(self.path.split("?", 1)[0])
            exp = m and _experiments(runs_dir).get(unquote(m[1]))   # имя только ключ словаря, не путь
            return (exp, m[2]) if exp else (None, None)

        def do_GET(self):
            if not self.ours(self.headers.get("Host")):
                return self.reply(403, {"error": "чужой Host"})
            if self.path.split("?", 1)[0] == "/":
                return self.root()
            exp, part = self.route(PAGE)
            if not exp:
                return self.reply(404, {"error": "нет такой страницы"})
            if not part:
                built = exp / "dashboard.html"
                body = built.read_bytes() if built.exists() else page({"meta": {"experiment": exp.name, "not_built": True}})
                return self.reply(200, body, "text/html")
            try:
                state = summary(exp, today())
            except ValueError as e:
                state = {"experiment": exp.name, "state": "damaged", "error": str(e)}
            self.reply(200, state | {"token": token})

        def root(self):
            exps = _experiments(runs_dir)
            live = [name for name, exp in sorted(exps.items()) if _state(exp) != "finished"]
            if len(live) == 1:
                return self.reply(302, "", "text/plain", [("Location", f"/e/{quote(live[0], safe='')}/")])
            items = "".join(f'<li><a href="/e/{quote(n, safe="")}/">{html.escape(n)}</a></li>' for n in sorted(exps))
            self.reply(200, '<!doctype html><html lang="ru"><meta charset="utf-8"><title>Traider</title>'
                            '<meta name="viewport" content="width=device-width, initial-scale=1">'
                            '<body style="font:15px/1.5 system-ui,-apple-system,sans-serif;max-width:40rem;margin:2rem auto;padding:0 16px">'
                            f'<h1>Эксперименты</h1><ul>{items or "<li>в runs/ нет экспериментов</li>"}</ul>', "text/html")

        def do_POST(self):
            given = self.headers.get("X-Traider-Token", "").encode("utf-8", "replace")
            origin = self.headers.get("Origin")
            if not self.ours(self.headers.get("Host")) or not hmac.compare_digest(given, token.encode()) or (
                    origin is not None and not self.ours(origin, "http://")):
                return self.reply(403, {"error": "запрос не со страницы управления"})
            exp, action = self.route(ACTION)
            if not exp:
                return self.reply(404, {"error": "нет такого действия"})
            body = self.body()
            if body is None:
                return self.reply(400, {"error": "тело запроса: объект JSON до 4096 байт"})
            if action == "stop" and body.get("confirm") != exp.name:
                return self.reply(400, {"error": "для остановки введите имя эксперимента"})
            with lock:
                try:
                    if action == "pause":
                        pause(exp, today())
                    elif action == "resume":
                        resume(exp, today())
                    else:
                        request_stop(exp, today(), datetime.now(NY))
                    state = summary(exp, today())
                except ValueError as e:   # пауза после остановки, повреждённый control.json: файл не перезаписан
                    return self.reply(409, {"error": str(e)})
            self.reply(200, state)

        def body(self) -> dict | None:
            try:
                size = int(self.headers["Content-Length"])
            except (TypeError, ValueError):
                return None
            if not 0 <= size <= MAX_BODY:
                return None
            raw = self.rfile.read(size)
            try:
                data = json.loads(raw) if raw else {}
            except ValueError:
                return None
            return data if isinstance(data, dict) else None

    server = ThreadingHTTPServer((HOST, port), Handler)   # daemon_threads у ThreadingHTTPServer уже True
    server.token = token
    return server


def _interrupt(signum, frame):
    raise KeyboardInterrupt


def main_serve(runs_dir: Path, port: int) -> int:
    try:
        server = make_server(runs_dir, port)
    except OSError as e:
        print(f"порт {port} не занять: {e}", file=sys.stderr)
        return 1
    port = server.server_address[1]
    print(f"Страница управления: http://localhost:{port}/ (с MacBook через туннель "
          f"ssh -N -L {port}:127.0.0.1:{port} <пользователь>@<mac-mini>). Остановить: Ctrl+C.", flush=True)
    prev = signal.signal(signal.SIGTERM, _interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        signal.signal(signal.SIGTERM, prev)
        server.server_close()
    return 0
