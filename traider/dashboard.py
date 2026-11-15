"""Страница аналитики: отчёт эксперимента, встроенный в один HTML-файл.

:wk-id: dashboard-module
:wk-tags: dashboard, report, html
:wk-categories: system-architecture

`render` берёт шаблон `dashboard.html` рядом с модулем и ставит отчёт одной строкой JSON на место
`/*DATA*/null`. Тексты отчёта пишут модели, читавшие открытую сеть, поэтому в JSON каждый `<`, `>`, `&`
и разделители строк U+2028, U+2029 записаны escape-последовательностями: ни `</script>`, ни `<!--`
не выходят из скрипта, а `JSON.parse`-совместимый литерал остаётся тем же объектом. Вся логика страницы
живёт в скрипте шаблона и кладёт данные в страницу только через `textContent`.
"""
import json
from collections.abc import Mapping
from pathlib import Path

TEMPLATE = Path(__file__).with_name("dashboard.html")
PLACEHOLDER = "/*DATA*/null"
SAFE = str.maketrans({"<": "\\u003c", ">": "\\u003e", "&": "\\u0026", " ": "\\u2028", " ": "\\u2029"})


def render(report: Mapping, out: Path) -> None:
    data = json.dumps(report, ensure_ascii=False).translate(SAFE)
    Path(out).write_text(TEMPLATE.read_text(encoding="utf-8").replace(PLACEHOLDER, data, 1), encoding="utf-8")
