import copy
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

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
    root = tmp_path / "wiki"
    # В настоящей вики уже есть статья результатов пилота с тем же ID, что создают тесты: в копии она
    # переименована вместе со всеми ссылками на неё, копия остаётся цельной, а тест пишет свою с нуля.
    real = root / "articles" / "results-pilot-1.md"
    if real.exists():
        real.rename(real.with_name("results-real-pilot-1.md"))
        for f in root.rglob("*.md"):
            f.write_text(f.read_text().replace("results-pilot-1", "results-real-pilot-1"))
    return root


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
    assert "None" not in article and "nan" not in article.lower() and "проверка по брокеру не проведена" in article
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


# --- раунд исправлений 1 ---

def test_the_final_report_is_built_once_on_the_first_open_day_after_the_runs_finish(played):
    from traider.brokers import SimBroker
    from traider.tick import tick
    exp, market = played
    make_broker = lambda cfg, store: SimBroker(cfg, store, market)
    for d in (4, 7, 8, 9, 10):                                   # пять торговых дней после finish_day 03.12
        market.clock, market.open = ny(2026, 12, d, 10, 5), True
        tick(exp.parent, market, make_broker, lambda: None)
    assert {RunStore(exp / n).state()["status"] for n in ("haiku", "sonnet", "opus-a", "opus-b", "opus-analyst")} == {"finished"}
    assert report_due(exp, "2026-12-10") is False                # день последнего тика: отчёт ещё не итоговый
    market.clock = ny(2026, 12, 11, 10, 0)
    assert report_due(exp, "2026-12-11") is True
    report = write_report(exp, market, None)
    assert {p["status"] for p in report["participants"]} == {"finished"}
    assert [report_due(exp, d) for d in ("2026-12-11", "2026-12-14", "2026-12-15")] == [False] * 3


def test_a_report_that_already_shows_the_final_statuses_is_never_due_again(played):
    exp, market = played
    for n in ("haiku", "sonnet", "opus-a", "opus-b", "opus-analyst"):
        set_status(exp, n, "finished")
    write_report(exp, market, None)
    assert report_due(exp, "2026-12-20") is False
    set_status(exp, "haiku", "dead")                              # статус сменился: отчёт устарел
    assert report_due(exp, "2026-12-20") is True


def test_a_category_without_ai_markers_refuses_and_nothing_is_written(played, wiki):
    exp, market = played
    path = wiki / "categories" / "results.md"
    path.write_text(path.read_text().replace("<!-- ai:start -->", ""))
    before = tree(wiki)
    with pytest.raises(ValueError):
        upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    assert tree(wiki) == before


def test_a_directory_that_is_not_a_wiki_refuses_and_nothing_is_written(played, tmp_path):
    exp, market = played
    empty = tmp_path / "nowiki"
    empty.mkdir()
    with pytest.raises(OSError):
        upsert_results(empty, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    assert list(empty.rglob("*")) == []


def failing_write(monkeypatch, nth):
    from traider import wiki_results
    real, n = wiki_results._write, []

    def write(path, text):
        n.append(path)
        if len(n) == nth:
            raise OSError("диск")
        real(path, text)
    monkeypatch.setattr(wiki_results, "_write", write)


def check(wiki):
    return subprocess.run([sys.executable, "tools/check_wiki.py", str(wiki)], capture_output=True).returncode == 0


@pytest.mark.parametrize("nth", [1, 2, 3])
def test_an_update_interrupted_at_any_write_leaves_a_wiki_the_checker_accepts(played, wiki, monkeypatch, nth):
    exp, market = played
    upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    failing_write(monkeypatch, nth)
    with pytest.raises(OSError):
        upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 4, 16, tzinfo=timezone.utc))
    assert check(wiki)


def test_a_first_creation_interrupted_after_the_root_category_is_still_accepted(played, wiki, monkeypatch):
    exp, market = played
    failing_write(monkeypatch, 2)
    with pytest.raises(OSError):
        upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    assert check(wiki)


def test_every_write_goes_through_its_own_temporary_file(tmp_path, monkeypatch):
    from traider import wiki_results
    seen = []
    real = os.replace
    monkeypatch.setattr(os, "replace", lambda src, dst: (seen.append(Path(src).name), real(src, dst)))
    wiki_results._write(tmp_path / "a.md", "1")
    wiki_results._write(tmp_path / "a.md", "2")
    assert len(set(seen)) == 2 and all(n.startswith(".a.md.") and n.endswith(".tmp") for n in seen) and not list(tmp_path.glob(".*"))


def test_article_modified_never_goes_back_and_is_never_before_created(played, wiki):
    exp, market = played
    report = build_report(exp, market)
    path = wiki / "articles" / "results-pilot-1.md"
    upsert_results(wiki, report, datetime(2030, 1, 1, tzinfo=timezone.utc))
    upsert_results(wiki, report, datetime(2026, 12, 3, tzinfo=timezone.utc))
    a = frontmatter.load(path)
    assert a["modified"] == datetime(2030, 1, 1, tzinfo=timezone.utc) and a["created"] == a["modified"]
    path.write_text(path.read_text().replace("created: 2030-01-01", "created: 2031-01-01"))
    upsert_results(wiki, report, datetime(2026, 12, 4, tzinfo=timezone.utc))
    a = frontmatter.load(path)
    assert a["modified"] >= a["created"] and a["created"].year == 2031
    assert check(wiki)


def test_the_column_reads_beat_the_random_bots(played):
    exp, market = played
    article = results_article(build_report(exp, market))
    assert "Обогнал случайных, %" in article and "Место среди случайных" not in article


def test_the_article_says_when_the_broker_check_was_not_made(played):
    exp, market = played
    assert "проверка по брокеру не проведена" in results_article(build_report(exp, market))
    assert "проверка по брокеру не проведена" not in results_article(build_report(exp, market, {"opus-a": []}))


def test_a_category_whose_modified_has_no_timezone_refuses_and_nothing_is_written(played, wiki):
    exp, market = played
    path = wiki / "categories" / "traider.md"
    path.write_text(re.sub(r"^(modified: .*)\+00:00$", r"\1", path.read_text(), count=1, flags=re.M))
    before = tree(wiki)
    with pytest.raises(ValueError):
        upsert_results(wiki, build_report(exp, market), datetime(2026, 12, 3, 16, tzinfo=timezone.utc))
    assert tree(wiki) == before


# --- волна после проверки всей ветки ---

def _idle_client():
    from tests.fakes import FakeAnthropic, use
    return FakeAnthropic([use("get_account"), use("finish_session", journal="ок")])


def test_a_run_stuck_in_error_does_not_hold_the_report_back(tmp_path, example):
    from traider.brokers import SimBroker
    from traider.tick import tick
    runs = tmp_path / "runs"
    exp = start_experiment(example, runs)
    market = FakeMarket.with_history(list(load_run(exp / "haiku")[0].universe), days=70, clock=ny(2026, 12, 1, 9, 35))
    market.open = True

    def make_broker(cfg, store):
        if cfg.participant == "opus-a":
            raise RuntimeError("счёт Alpaca не готов к прогону")
        return SimBroker(cfg, store, market)
    tick(runs, market, make_broker, _idle_client)                       # до окна: остальные ещё без сессий
    assert report_due(exp, "2026-12-01") is False
    market.clock = ny(2026, 12, 1, 10, 5)
    tick(runs, market, make_broker, _idle_client)
    assert RunStore(exp / "opus-a").session("2026-12-01") is None
    assert report_due(exp, "2026-12-01") is True
    write_report(exp, market, None)
    market.clock = ny(2026, 12, 1, 10, 20)
    tick(runs, market, make_broker, _idle_client)
    assert report_due(exp, "2026-12-01") is False                      # собран: не на каждом тике


def _tick_at(exp, market, when):
    from traider.brokers import SimBroker
    from traider.tick import tick
    market.clock, market.open = when, True
    return tick(exp.parent, market, lambda cfg, store: SimBroker(cfg, store, market), _idle_client)


def test_a_death_later_in_the_day_reopens_the_days_report(moving):
    exp, market = moving
    market.clock, market.open = ny(2026, 12, 4, 10, 30), True
    write_report(exp, market, None)
    assert report_due(exp, "2026-12-04") is False
    from traider.brokers import SimBroker
    run = exp / "opus-b"
    SimBroker(load_run(run)[0], RunStore(run), market).buy("2026-12-04", "AAPL", 190.0, 1.0, "т", "seed/1")
    market.prices["AAPL"] *= 0.01                                        # к 14:00 бумага обвалилась
    got = {r["participant"]: r["action"] for r in _tick_at(exp, market, ny(2026, 12, 4, 14, 0))}
    assert got["opus-b"] == "died"
    assert report_due(exp, "2026-12-04") is True
    report = write_report(exp, market, None)
    assert {p["id"]: p["status"] for p in report["participants"]}["opus-b"] == "dead"
    _tick_at(exp, market, ny(2026, 12, 4, 14, 15))
    assert report_due(exp, "2026-12-04") is False                      # и не на каждом тике после этого


def test_an_owner_stop_later_in_the_day_reopens_the_days_report(moving):
    from traider.control import request_stop
    exp, market = moving
    market.clock, market.open = ny(2026, 12, 4, 10, 30), True
    write_report(exp, market, None)
    request_stop(exp, "2026-12-04", ny(2026, 12, 4, 12, 50))
    _tick_at(exp, market, ny(2026, 12, 4, 13, 0))
    assert report_due(exp, "2026-12-04") is True
    write_report(exp, market, None)
    _tick_at(exp, market, ny(2026, 12, 4, 13, 15))
    assert report_due(exp, "2026-12-04") is False


def test_a_failed_page_leaves_the_old_report_json(moving, monkeypatch):
    import traider.wiki_results as wr
    exp, market = moving
    (exp / "report.json").write_text("старый")
    monkeypatch.setattr(wr, "render", lambda report, path: (_ for _ in ()).throw(OSError("диск полон")))
    with pytest.raises(OSError):
        write_report(exp, market, None)
    assert (exp / "report.json").read_text() == "старый"              # новый отчёт рядом со старой страницей не лежит


def test_a_verdict_is_marked_preliminary_until_the_run_is_finished_or_dead(played):
    exp, market = played                                               # все прогоны finishing: прогнозы ещё зреют
    rows = [l for l in results_article(build_report(exp, market)).splitlines() if l.startswith("| haiku")]
    assert rows and rows[0].rstrip(" |").endswith("(предварительный)")
    set_status(exp, "haiku", "finished"); set_status(exp, "sonnet", "dead")
    art = results_article(build_report(exp, market))
    for pid in ("haiku", "sonnet"):
        assert "предварительный" not in next(l for l in art.splitlines() if l.startswith(f"| {pid} "))
    assert "предварительный" in next(l for l in art.splitlines() if l.startswith("| opus-b "))


def test_the_article_never_shows_unknown_spend_as_a_number(played):
    from tests.test_report import _drop_cost
    exp, market = played
    _drop_cost(exp, "opus-b", "2026-12-02")
    _drop_cost(exp, "haiku", "2026-12-01", "2026-12-02", "2026-12-03")
    art = results_article(build_report(exp, market))
    run = next(l for l in art.splitlines() if "Расход прогона" in l)
    assert "не всё записано: у 4 сессий стоимость неизвестна" in run
    cells = {l.split("|")[1].strip(): l.split("|")[6].strip() for l in art.splitlines() if l.startswith("| ") and "$" in l}
    assert cells["haiku"] == "—" and "у 1 сессии стоимость неизвестна" in cells["opus-b"] and cells["sonnet"].startswith("$")


def test_a_run_spend_with_no_recorded_cost_is_a_dash(played):
    from tests.test_report import _drop_cost
    exp, market = played
    for pid in ("haiku", "sonnet", "opus-a", "opus-b", "opus-analyst"):
        _drop_cost(exp, pid, "2026-12-01", "2026-12-02", "2026-12-03")
    run = next(l for l in results_article(build_report(exp, market)).splitlines() if "Расход прогона" in l)
    assert run.startswith("- Расход прогона —, не всё записано: у 15 сессий стоимость неизвестна")
