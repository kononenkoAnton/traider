"""Страница аналитики: `render` встраивает отчёт в шаблон, чужой текст не выходит из скрипта.

`tests/data/report_fixture.json` это отчёт фикстуры `moving` из conftest (четыре дня на движущихся ценах),
сохранённый один раз через `json.dumps(report, ensure_ascii=False, indent=1, sort_keys=True)`. Отчёт
детерминирован (часы подделки рынка заданы), поэтому файл пересобирается тем же кодом:

    uv run python -c "import json,pathlib,sys,tempfile; sys.path.insert(0,'.'); import tests.conftest as c; \
from traider.report import build_report; t=pathlib.Path(tempfile.mkdtemp()); \
exp,m=c.moving._get_wrapped_function()(t); \
pathlib.Path('tests/data/report_fixture.json').write_text(json.dumps(build_report(exp,m),ensure_ascii=False,indent=1,sort_keys=True)+chr(10))"
"""
import json
import pathlib
import re

from tests.fakes import FakeMarket
from traider.config import load_run, start_experiment
from traider.dashboard import render
from traider.report import build_report

FIXTURE = json.loads(pathlib.Path("tests/data/report_fixture.json").read_text())


def embedded(html: str) -> dict:
    start = html.index("const DATA = ") + len("const DATA = ")
    return json.loads(html[start:html.index(";\n", start)].replace("<\\/", "</"))


def test_render_embeds_the_report(tmp_path):
    render(FIXTURE, tmp_path / "d.html")
    html = (tmp_path / "d.html").read_text()
    assert embedded(html) == FIXTURE and "/*DATA*/" not in html


def test_hostile_text_cannot_break_out_of_the_script(tmp_path):
    report = json.loads(json.dumps(FIXTURE))
    report["sessions"]["haiku"][0]["journal"] = "</script><script>alert(1)</script>"
    render(report, tmp_path / "d.html")
    html = (tmp_path / "d.html").read_text()
    assert html.count("</script>") == 1 and embedded(html)["sessions"]["haiku"][0]["journal"].startswith("</script>")


def test_comment_script_and_line_separators_round_trip(tmp_path):
    report = json.loads(json.dumps(FIXTURE))
    hostile = "<!-- <script>x</script> &     >"
    report["sessions"]["haiku"][0]["journal"] = hostile
    render(report, tmp_path / "d.html")
    html = (tmp_path / "d.html").read_text()
    line = html[html.index("const DATA = "):]
    line = line[:line.index(";\n")]
    assert html.count("</script>") == 1 and html.count("<script>") == 1
    assert not set("<>&  ") & set(line)
    assert embedded(html)["sessions"]["haiku"][0]["journal"] == hostile


def test_template_is_self_contained_and_safe():
    t = pathlib.Path("traider/dashboard.html").read_text()
    assert "innerHTML" not in t and "http://" not in t.replace("http://www.w3.org", "") and "https://" not in t
    assert "выдуманы" not in t and "Math.imul" not in t                         # генератор выдуманных данных убран
    for section in ["summary", "equity", "money", "tokens", "grid", "detail", "positions", "trades", "forecasts", "compare", "rejects", "incidents"]:
        assert f'id="{section}"' in t, section
    for colour in ["#2a78d6", "#eb6834", "#1baf7a", "#eda100", "#e87ba4", "#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]:
        assert colour in t, colour
    for bad in ["outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function", "javascript:"]:
        assert bad not in t, bad
    assert not re.search(r"\son[a-z]+\s*=", t)                                  # обработчики только через addEventListener
    assert t.count("const DATA = /*DATA*/null;\n") == 1


def test_empty_report_renders(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    render(build_report(exp, FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70)), tmp_path / "e.html")
    assert (tmp_path / "e.html").stat().st_size > 10_000


def test_report_with_a_dead_participant_renders(tmp_path, intraday_death):
    exp, market = intraday_death
    report = build_report(exp, market)
    render(report, tmp_path / "x.html")
    assert embedded((tmp_path / "x.html").read_text())["participants"][3]["status"] == "dead"
