"""Сигнал сторожу снаружи (протокол healthchecks.io) после каждого тика.

:wk-id: heartbeat-module
:wk-tags: schedule, monitoring
:wk-categories: system-architecture

Как это работает. `beat(url, ok, text)` без адреса ничего не делает. Иначе один POST текста (UTF-8, первые 2000
символов) на адрес как есть, когда тик в порядке, и на адрес с `/fail` в конце пути, когда нет. Принимаются только
`http://` и `https://`. Одна попытка без повторов; весь запрос, с поиском имени хоста, ждёт не дольше `timeout` секунд.
Функция не бросает: успех это None, сбой это короткий текст с классом ошибки. Адрес секретный (кто его знает, может
отчитываться за бота), поэтому ни адрес, ни его путь, ни запрос в текст ошибки не попадают.
"""
import threading
import urllib.error
import urllib.request
from urllib.parse import urlsplit, urlunsplit

LIMIT = 2000


def _error(e: BaseException) -> str:
    """Класс ошибки и, где есть, код HTTP или класс причины; текст исключения не берётся: в нём мог быть адрес."""
    if isinstance(e, urllib.error.HTTPError):
        return f"HTTPError {e.code}"
    if isinstance(e, urllib.error.URLError):
        return f"URLError ({type(e.reason).__name__})"
    return type(e).__name__


def beat(url: str | None, ok: bool, text: str, timeout: float = 5.0) -> str | None:
    if not url:
        return None
    try:
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            return "адрес сторожа не http(s), ничего не отправлено"
        target = url if ok else urlunsplit(parts._replace(path=parts.path.rstrip("/") + "/fail"))
        req = urllib.request.Request(target, data=text[:LIMIT].encode("utf-8"), method="POST")
    except Exception as e:
        return _error(e)
    box: list[str | None] = []

    def send():
        try:
            with urllib.request.urlopen(req, timeout=timeout):
                box.append(None)
        except urllib.error.HTTPError as e:
            with e:   # ответ с кодом ошибки держит сокет
                box.append(_error(e))
        except Exception as e:
            box.append(_error(e))
    # ponytail: таймаут urllib не покрывает поиск имени хоста и медленную передачу; поток-демон держит общий срок,
    # а сам доживает до своего таймаута сокета или до выхода процесса
    t = threading.Thread(target=send, daemon=True)
    t.start()
    t.join(timeout)
    return box[0] if box else f"TimeoutError (нет ответа за {timeout:g} с)"
