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

import pytest

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


def test_lone_surrogate_renders_and_round_trips(tmp_path):
    report = json.loads(json.dumps(FIXTURE))
    report["sessions"]["haiku"][0]["journal"] = "x\ud800y"
    render(report, tmp_path / "d.html")
    assert embedded((tmp_path / "d.html").read_text())["sessions"]["haiku"][0]["journal"] == "x\ud800y"


def test_failed_render_keeps_the_previous_page(tmp_path):
    out = tmp_path / "d.html"
    render(FIXTURE, out)
    before = out.read_bytes()
    report = json.loads(json.dumps(FIXTURE))
    report["meta"]["bad"] = object()                                            # не сериализуется
    with pytest.raises(TypeError):
        render(report, out)
    assert out.read_bytes() == before and sorted(p.name for p in tmp_path.iterdir()) == ["d.html"]


def test_failed_write_keeps_the_previous_page_and_leaves_no_temporary_file(tmp_path, monkeypatch):
    out = tmp_path / "d.html"
    render(FIXTURE, out)
    before = out.read_bytes()

    def broken(*args, **kwargs):
        raise OSError("диск отвалился")
    monkeypatch.setattr("traider.dashboard.os.replace", broken)
    report = json.loads(json.dumps(FIXTURE))
    report["meta"]["experiment"] = "другой"
    with pytest.raises(OSError):
        render(report, out)
    assert out.read_bytes() == before and sorted(p.name for p in tmp_path.iterdir()) == ["d.html"]


def test_template_pins_phrases_and_keys():
    t = pathlib.Path("traider/dashboard.html").read_text()
    for phrase in ["Сделок пока нет", "Созревших прогнозов пока нет", "Происшествий нет", "предварительный", "окончательный",
                   "погиб", "Случайные боты для", "Последние 10 дней", "Оценка по времени",
                   "решение агента", "стоп", "ликвидация"]:
        assert phrase in t, phrase
    for key in ["DATA.money", "DATA.brier_series"]:
        assert key in t, key


def test_empty_report_renders(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    report = build_report(exp, FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70))
    render(report, tmp_path / "e.html")
    assert embedded((tmp_path / "e.html").read_text()) == json.loads(json.dumps(report))


def test_report_with_a_dead_participant_renders(tmp_path, intraday_death):
    exp, market = intraday_death
    report = build_report(exp, market)
    render(report, tmp_path / "x.html")
    assert embedded((tmp_path / "x.html").read_text()) == json.loads(json.dumps(report))
    assert report["participants"][3]["status"] == "dead"


def late_starter() -> dict:
    """Отчёт, где первая сессия haiku только во второй день отчёта: его кривые на точку короче."""
    r = json.loads(json.dumps(FIXTURE))
    for key in ("equity", "account_equity", "spy_same_exposure"):
        r[key]["haiku"] = r[key]["haiku"][:1] + r[key]["haiku"][2:]
    r["band"]["haiku"] = {k: v[:1] + v[2:] for k, v in r["band"]["haiku"].items()}
    r["costs"]["haiku"] = r["costs"]["haiku"][1:]
    r["sessions"]["haiku"] = r["sessions"]["haiku"][1:]
    r["brier_series"]["haiku"][0] = None
    return r


def test_report_with_a_late_starter_renders(tmp_path):
    report = late_starter()
    render(report, tmp_path / "l.html")
    assert embedded((tmp_path / "l.html").read_text()) == report
