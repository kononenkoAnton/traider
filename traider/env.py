"""Чтение файла .env без сторонних библиотек.

:wk-id: env-module
:wk-tags: config, keys
:wk-categories: system-architecture

Как это работает. `load_env(path)` читает строки `KEY=VALUE`: пустые строки и строки с `#`
пропускаются, ключ и значение обрезаются по краям, делится строка по первому `=`; префикс `export ` и парные кавычки вокруг значения снимаются. Нет файла:
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
            key, _, value = line.removeprefix("export ").partition("=")
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                value = value[1:-1]
            out[key.strip()] = value
    return out
