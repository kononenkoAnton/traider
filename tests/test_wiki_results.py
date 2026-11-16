import copy
import json
import shutil
import subprocess
import sys
from datetime import datetime, timezone

import frontmatter
import pytest

from tests.fakes import FakeMarket, ny
from traider.config import load_run, start_experiment
from traider.report import build_report
from traider.store import RunStore
from traider.wiki_results import report_due, results_article, upsert_results, write_report

MARK = "МАРКЕР-ЧУЖОГО-ТЕКСТА"


@pytest.fixture
def wiki(tmp_path):
    shutil.copytree("wiki", tmp_path / "wiki")
    return tmp_path / "wiki"


def tree(root):
    return {p.relative_to(root): p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file()}


def test_report_files_and_article_are_written(played, wiki):
    exp, market = played
    report = write_report(exp, market, wiki)
    assert json.loads((exp / "report.json").read_text())["days"] == report["days"] and (exp / "dashboard.html").exists()
    article = frontmatter.load(wiki / "articles" / "results-pilot-1.md")
    assert article["categories"] == ["results"] and "pilot" in article["tags"]
    assert "opus-analyst" in article.content and "dashboard.html" in article.content
    assert "[[results-pilot-1" in (wiki / "categories" / "results.md").read_text()
    assert subprocess.run([sys.executable, "tools/check_wiki.py", str(wiki)]).returncode == 0


def test_second_report_updates_instead_of_duplicating(played, wiki):
    exp, market = played
    upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    first = frontmatter.load(wiki / "articles" / "results-pilot-1.md")
    upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 4, 16, tzinfo=timezone.utc))
    second = frontmatter.load(wiki / "articles" / "results-pilot-1.md")
    assert second["created"] == first["created"] and second["modified"] > first["modified"]
    assert (wiki / "categories" / "results.md").read_text().count("[[results-pilot-1") == 1


def test_report_is_due_once_per_day(played, wiki):
    exp, market = played
    assert report_due(exp, "2026-12-03") is True
    write_report(exp, market, wiki)
    assert report_due(exp, "2026-12-03") is False and report_due(exp, "2026-12-04") is False   # за 04.12 записей ещё нет


# --- сверх задания ---

def test_without_a_wiki_only_the_two_report_files_appear(played, wiki):
    exp, market = played
    before_wiki, before_exp = tree(wiki), set(exp.iterdir())
    write_report(exp, market, None)
    assert {p.name for p in set(exp.iterdir()) - before_exp} == {"report.json", "dashboard.html"}
    assert tree(wiki) == before_wiki and not list(exp.glob(".*.tmp")) and not list(exp.glob("*.tmp"))


def test_upsert_twice_moves_modified_of_article_and_both_categories_and_keeps_created(played, wiki):
    exp, market = played
    cats = [wiki / "categories" / "results.md", wiki / "categories" / "traider.md"]
    upsert_results(wiki, build_report(exp, market), datetime(2027, 1, 3, 16, tzinfo=timezone.utc))
    first = [frontmatter.load(p) for p in cats]
    upsert_results(wiki, build_report(exp, market), datetime(2027, 1, 4, 16, tzinfo=timezone.utc))
    for p, a in zip(cats, first):
        b = frontmatter.load(p)
        assert b["created"] == a["created"] and b["modified"] > a["modified"]
        assert b["modified"] == datetime(2027, 1, 4, 16, tzinfo=timezone.utc)
    assert not list(wiki.rglob("*.tmp")) and not list(wiki.rglob("*.md.*"))


def test_an_old_clock_never_moves_a_category_back(played, wiki):
    exp, market = played
    before = frontmatter.load(wiki / "categories" / "traider.md")["modified"]
    upsert_results(wiki, build_report(exp, market), datetime(2020, 1, 1, tzinfo=timezone.utc))
    assert frontmatter.load(wiki / "categories" / "traider.md")["modified"] == before


def test_frontmatter_of_the_article_follows_the_existing_ones(played, wiki):
    exp, market = played
    report = build_report(exp, market)
    upsert_results(wiki, report, datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    text = (wiki / "articles" / "results-pilot-1.md").read_text()
    meta = frontmatter.loads(text).metadata
    assert meta["id"] == "results-pilot-1" and meta["type"] == "leaf" and meta["tags"] == ["results", "pilot"]
    assert text.startswith("---\nid: results-pilot-1\ntitle: ") and "created: 2026-12-03T16:00:00+00:00" in text
    report["meta"]["pilot"] = False
    upsert_results(wiki, report, datetime(2026, 12, 3, 17, tzinfo=timezone.utc))
    assert frontmatter.load(wiki / "articles" / "results-pilot-1.md")["tags"] == ["results", "official"]


def test_the_article_carries_no_text_written_by_a_model(played, wiki):
    exp, market = played
    report = build_report(exp, market)
    for rows in report["sessions"].values():
        for s in rows:
            s["journal"] = MARK
            s["transcript"] = [{"role": "assistant", "content": MARK}]
            s["decisions"] = [{**d, "thesis": MARK, "reason": MARK} for d in s["decisions"]]
            s["cage"] = [{**c, "detail": MARK} for c in s["cage"]]
            s["analyst"] = {"briefing": MARK, "calls": [{"question": MARK}]}
    report["incidents"] += [{"kind": "serious", "day": "2026-12-02", "participant": "haiku", "title": "ошибка тика", "text": MARK}]
    for i in report["incidents"]:
        i["text"] = MARK
    report["trades"] = [{**t, "symbol": t["symbol"]} for t in report["trades"]]
    upsert_results(wiki, report, datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    text = (wiki / "articles" / "results-pilot-1.md").read_text()
    assert MARK not in text and "ошибка тика" in text and "2026-12-02" in text and "haiku" in text


def test_every_missing_number_shows_a_dash(played, wiki):
    exp, market = played
    report = build_report(exp, market)
    for k in report["summary"]["opus-a"]:
        report["summary"]["opus-a"][k] = None
    report["compare"]["twins"] = {k: None for k in report["compare"]["twins"]}
    report["compare"]["analyst"] = {"diff_pp": None, "beyond_noise": None}
    report["compare"]["ladder"]["beyond_noise"] = None
    article = results_article(report)
    row = next(l for l in article.splitlines() if l.startswith("| opus-a "))
    assert row.count("—") >= 5 and "None" not in article and "nan" not in article.lower()


def test_a_run_that_just_started_gives_an_article_with_plain_empty_sections(tmp_path, example, wiki):
    exp = start_experiment(example, tmp_path / "runs")
    market = FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70, clock=ny(2026, 12, 1, 16, 30))
    write_report(exp, market, wiki)
    article = frontmatter.load(wiki / "articles" / "results-pilot-1.md").content
    assert "None" not in article and "nan" not in article.lower() and "целостность в порядке" in article
    assert "Происшествий нет" in results_article({**build_report(exp, market), "incidents": []})
    assert subprocess.run([sys.executable, "tools/check_wiki.py", str(wiki)]).returncode == 0


def test_an_experiment_name_that_is_not_a_slug_is_refused(played, wiki):
    exp, market = played
    report = build_report(exp, market)
    report["meta"]["experiment"] = "../evil"
    before = tree(wiki)
    with pytest.raises(ValueError):
        upsert_results(wiki, report, datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    assert tree(wiki) == before


# --- report_due ---

def set_status(exp, name, status):
    RunStore(exp / name).update_state(status=status)


def test_due_needs_every_active_run_to_have_a_record_for_the_day(moving):
    exp, market = moving
    assert report_due(exp, "2026-12-04") is True
    (exp / "opus-b" / "sessions" / "2026-12-04.json").unlink()
    assert report_due(exp, "2026-12-04") is False


@pytest.mark.parametrize("status", ["finishing", "invalid", "dead", "finished"])
def test_a_run_that_is_not_active_does_not_hold_the_report_back(moving, status):
    exp, market = moving
    (exp / "opus-b" / "sessions" / "2026-12-04.json").unlink()
    set_status(exp, "opus-b", status)
    assert report_due(exp, "2026-12-04") is True


def test_nothing_is_due_when_every_run_is_finished_or_dead(played):
    exp, market = played
    for name in ("haiku", "sonnet", "opus-a"):
        set_status(exp, name, "finished")
    assert report_due(exp, "2026-12-03") is True       # opus-b и opus-analyst ещё finishing
    set_status(exp, "opus-b", "dead")
    set_status(exp, "opus-analyst", "finished")
    assert report_due(exp, "2026-12-03") is False


def test_a_report_of_the_day_built_after_the_sessions_closes_the_day_but_a_changed_record_reopens_it(moving):
    exp, market = moving
    market.clock = ny(2026, 12, 4, 16, 30)
    write_report(exp, market, None)
    assert report_due(exp, "2026-12-04") is False
    path = exp / "haiku" / "sessions" / "2026-12-04.json"
    rec = json.loads(path.read_text())
    path.write_text(json.dumps({**rec, "status": "cap" if rec["status"] != "cap" else "ok"}))
    assert report_due(exp, "2026-12-04") is True


def test_a_report_built_on_another_day_does_not_count(moving):
    exp, market = moving                                 # часы фикстуры: 09.12
    write_report(exp, market, None)
    assert report_due(exp, "2026-12-04") is True


def test_an_unreadable_report_json_means_due(moving):
    exp, market = moving
    (exp / "report.json").write_text("{не json")
    assert report_due(exp, "2026-12-04") is True


def test_article_is_deterministic(played):
    exp, market = played
    r = build_report(exp, market)
    assert results_article(copy.deepcopy(r)) == results_article(r)
