"""Чтение файла .env без сторонних библиотек.

:wk-id: env-module
:wk-tags: config, keys
:wk-categories: system-architecture

Как это работает. `load_env(path)` читает строки `KEY=VALUE`: пустые строки и строки с `#`
пропускаются, ключ и значение обрезаются по краям, делится строка по первому `=`. Нет файла:
пустой словарь. В окружение процесса ничего не пишется, ключи читает тот, кому они нужны.
"""
from pathlib import Path


def load_env(path: Path = Path(".env")) -> dict[str, str]:
    if not path.exists():
        return {}
    out = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            out[key.strip()] = value.strip()
    return out
