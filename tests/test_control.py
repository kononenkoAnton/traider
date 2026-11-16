import json

import pytest

from tests.fakes import NOW
from traider.config import start_experiment
from traider.control import is_paused, pause, read_control, request_stop, resume, summary
from traider.store import RunStore


def test_pause_resume_and_stop_are_recorded_and_idempotent(tmp_path):
    exp = tmp_path / "runs" / "pilot-1"; exp.mkdir(parents=True)
    assert read_control(exp) == {"pauses": [], "stop": None}
    pause(exp, "2026-12-02"); pause(exp, "2026-12-03")                       # повтор не открывает второй интервал
    assert read_control(exp)["pauses"] == [{"from": "2026-12-02", "to": None}]
    assert is_paused(read_control(exp), "2026-12-05") and not is_paused(read_control(exp), "2026-12-01")
    resume(exp, "2026-12-04")
    c = read_control(exp)
    assert c["pauses"] == [{"from": "2026-12-02", "to": "2026-12-04"}] and is_paused(c, "2026-12-04") and not is_paused(c, "2026-12-07")
    first = request_stop(exp, "2026-12-08", NOW); assert request_stop(exp, "2026-12-09", NOW)["stop"] == first["stop"]


# --- сверх задания ---

def test_resume_without_an_open_pause_changes_nothing(tmp_path):
    exp = tmp_path / "pilot-1"; exp.mkdir()
    assert resume(exp, "2026-12-02") == {"pauses": [], "stop": None} and not (exp / "control.json").exists()
    pause(exp, "2026-12-02"); resume(exp, "2026-12-03"); before = (exp / "control.json").read_text()
    resume(exp, "2026-12-04")
    assert (exp / "control.json").read_text() == before


def test_a_second_pause_opens_a_second_interval(tmp_path):
    exp = tmp_path / "pilot-1"; exp.mkdir()
    pause(exp, "2026-12-02"); resume(exp, "2026-12-03"); pause(exp, "2026-12-08")
    c = read_control(exp)
    assert c["pauses"] == [{"from": "2026-12-02", "to": "2026-12-03"}, {"from": "2026-12-08", "to": None}]
    assert not is_paused(c, "2026-12-04") and is_paused(c, "2026-12-08") and is_paused(c, "2027-01-04")


def test_pause_and_resume_after_a_stop_are_refused(tmp_path):
    exp = tmp_path / "pilot-1"; exp.mkdir()
    request_stop(exp, "2026-12-02", NOW); before = (exp / "control.json").read_text()
    with pytest.raises(ValueError, match="остановка"):
        pause(exp, "2026-12-03")
    with pytest.raises(ValueError, match="остановка"):
        resume(exp, "2026-12-03")
    assert (exp / "control.json").read_text() == before
    assert json.loads(before)["stop"] == {"day": "2026-12-02", "ts": NOW.isoformat()}


BAD_DATES = ['{"pauses": [{"from": "2026-12-01", "to": "2026.12.30"}], "stop": null}',
             '{"pauses": [], "stop": {"day": "not a date", "ts": "2026-12-01T10:00:00-05:00"}}',
             '{"pauses": [{"from": "2026/12/01", "to": null}], "stop": null}',
             '{"pauses": [{"from": "2026-12-05", "to": "2026-12-01"}], "stop": null}',
             '{"pauses": [], "stop": {"day": "2026-12-01", "ts": "вчера"}}',
             '{"pauses": [{"from": "2026-12-01"}], "stop": null}',
             '{"pauses": [{"to": "2026-12-01"}], "stop": null}',
             '{"pauses": [], "stop": {"day": "2026-12-01"}}',
             '{"pauses": [], "stop": {"ts": "2026-12-01T10:00:00-05:00"}}']


@pytest.mark.parametrize("text", BAD_DATES + ["не json", "[]", '{"pauses": {}, "stop": null}', '{"pauses": [{"from": 1}], "stop": null}',
                                  '{"pauses": [], "stop": {"ts": "x"}}', '{"stop": null}'])
def test_a_damaged_control_file_is_an_error_and_is_never_overwritten(tmp_path, text):
    exp = tmp_path / "pilot-1"; exp.mkdir(); (exp / "control.json").write_text(text)
    for act in (lambda: read_control(exp), lambda: pause(exp, "2026-12-02"), lambda: request_stop(exp, "2026-12-02", NOW)):
        with pytest.raises(ValueError, match="control.json"):
            act()
    assert (exp / "control.json").read_text() == text


def test_writes_leave_no_temporary_files(tmp_path):
    exp = tmp_path / "pilot-1"; exp.mkdir()
    pause(exp, "2026-12-02"); resume(exp, "2026-12-03"); request_stop(exp, "2026-12-04", NOW)
    assert sorted(p.name for p in exp.iterdir()) == ["control.json"]


def _states(exp, **status):
    for pid, st in status.items():
        RunStore(exp / pid).update_state(status=st)
    return summary(exp)


def test_summary_has_five_states_and_never_writes(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    ids = ["haiku", "sonnet", "opus-a", "opus-b", "opus-analyst"]
    before = {p: p.read_bytes() for p in exp.rglob("*") if p.is_file()}
    s = summary(exp)
    assert s["experiment"] == "pilot-1" and s["state"] == "running" and s["paused_since"] is None and s["stop"] is None
    assert [r["participant"] for r in s["runs"]] == sorted(ids) and {r["status"] for r in s["runs"]} == {"active"}
    assert {p: p.read_bytes() for p in exp.rglob("*") if p.is_file()} == before
    pause(exp, "2026-12-02")
    s = summary(exp)
    assert (s["state"], s["paused_since"]) == ("paused", "2026-12-02")
    assert _states(exp, **dict.fromkeys(ids, "finishing"))["state"] == "finishing"        # пауза без активных: идёт конец
    resume(exp, "2026-12-03")
    assert summary(exp)["state"] == "finishing" and summary(exp)["paused_since"] is None
    assert _states(exp, haiku="active")["state"] == "running"
    request_stop(exp, "2026-12-04", NOW)
    s = summary(exp)
    assert s["state"] == "stopping" and s["stop"] == {"day": "2026-12-04", "ts": NOW.isoformat()}
    assert _states(exp, **{**dict.fromkeys(ids, "finished"), "haiku": "dead"})["state"] == "finished"


def test_finishing_while_an_old_pause_is_closed_is_finishing_not_paused(tmp_path, example):
    exp = start_experiment(example, tmp_path / "runs")
    pause(exp, "2026-12-02")
    assert _states(exp, haiku="invalid", sonnet="dead", **{"opus-a": "finishing", "opus-b": "finished",
                                                          "opus-analyst": "finishing"})["state"] == "finishing"
