"""Страница аналитики: отчёт эксперимента, встроенный в один HTML-файл.

:wk-id: dashboard-module
:wk-tags: dashboard, report, html
:wk-categories: system-architecture

`render` берёт шаблон `dashboard.html` рядом с модулем и ставит отчёт одной строкой JSON на место
`/*DATA*/null`. Тексты отчёта пишут модели, читавшие открытую сеть, поэтому в JSON каждый `<`, `>`, `&`
и разделители строк U+2028, U+2029 записаны escape-последовательностями: ни `</script>`, ни `<!--`
не выходят из скрипта, а `JSON.parse`-совместимый литерал остаётся тем же объектом. Вся логика страницы
живёт в скрипте шаблона и кладёт данные в страницу только через `textContent`. JSON пишется с `ensure_ascii`, поэтому
одиночный суррогат из чужого текста не ломает запись; файл пишется во временный рядом и подменяется атомарно, так что
сбой не стирает прежнюю страницу.
"""
import json
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard.html")
PLACEHOLDER = "/*DATA*/null"
SAFE = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"})


def render(report: Mapping, out: Path) -> None:
    data = json.dumps(report).translate(SAFE)              # ensure_ascii: одиночный суррогат станет \ud800, а не ошибкой записи
    page = TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, data, 1)
    out = Path(out)
    fd, tmp = tempfile.mkstemp(dir=out.parent, prefix=f".{out.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(page)
        os.chmod(tmp, out.stat().st_mode & 0o777 if out.exists() else 0o644)   # mkstemp даёт 0600
        os.replace(tmp, out)
    except BaseException:
        os.unlink(tmp)
        raise
