"""Пауза и остановка эксперимента: единственное место, где владелец меняет его ход.

:wk-id: control-module
:wk-tags: control, pause, stop
:wk-categories: system-architecture

Как это работает. Решения владельца лежат в `runs/<эксперимент>/control.json`:
`{"pauses": [{"from": день, "to": день | null}], "stop": {"day": день, "ts": iso} | null}`. Файл пишут только
`pause`, `resume` и `request_stop`, через временный файл с уникальным именем и `os.replace`; тик его только читает.
Состояние прогонов, журналы и записи сессий здесь не пишутся никогда: остановку применяет сам тик в своей работе
над прогоном, поэтому действие владельца не может столкнуться с записью идущего тика. Брокер не создаётся, `.env`
не читается, сети нет.

`read_control`: нет файла, значит `{"pauses": [], "stop": None}`; нечитаемый файл или файл не той формы даёт
`ValueError` с именем файла; не той формы значит и день не `ГГГГ-ММ-ДД` (или не дата), начало интервала позже конца,
`stop.ts` не время ISO (функции записи тоже: повреждённый файл не перезаписывается, иначе потерялся бы запрос
остановки). `pause` открывает интервал, если открытого нет; `resume` закрывает открытый днём `day`, без открытого
ничего не меняет; оба после запроса остановки дают `ValueError`. `request_stop` необратим, повтор ничего не
меняет. `is_paused`: день внутри любого интервала, концы включены, у открытого конца нет (день снятия паузы ещё
пауза, сессии идут со следующего торгового дня). `summary` читает состояния прогонов и ничего не пишет; `state`:
`finished` (все прогоны `finished` или `dead`), `stopping` (остановка запрошена), `finishing` (ни одного `active`:
естественный конец, пока созревают прогнозы), `paused` (открыт интервал паузы), иначе `running`.
"""
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import date, datetime
from pathlib import Path

from traider.config import TERMINAL_STATUSES
from traider.store import RunStore

FILE = "control.json"


def _day(x) -> bool:
    """Только `ГГГГ-ММ-ДД`: дни сравниваются строками, другая запись дала бы паузу или остановку не в тот день."""
    try:
        return isinstance(x, str) and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}", x)) and bool(date.fromisoformat(x))
    except ValueError:
        return False


def _moment(x) -> bool:
    try:
        return isinstance(x, str) and bool(datetime.fromisoformat(x))
    except ValueError:
        return False


def _valid(c) -> bool:
    return (isinstance(c, dict) and set(c) == {"pauses", "stop"} and isinstance(c["pauses"], list)
            and all(isinstance(p, dict) and _day(p.get("from"))
                    and (p.get("to") is None or _day(p["to"]) and p["from"] <= p["to"]) for p in c["pauses"])
            and (c["stop"] is None or isinstance(c["stop"], dict) and _day(c["stop"].get("day"))
                 and _moment(c["stop"].get("ts"))))


def read_control(exp_dir: Path) -> dict:
    path = Path(exp_dir) / FILE
    if not path.exists():
        return {"pauses": [], "stop": None}
    try:
        c = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise ValueError(f"{path} не читается: {type(e).__name__}: {e}") from e
    if not _valid(c):
        raise ValueError(f"{path} не той формы: нужно {{\"pauses\": [{{\"from\": ГГГГ-ММ-ДД, \"to\": ГГГГ-ММ-ДД или null}}], "
                         f"\"stop\": {{\"day\": ГГГГ-ММ-ДД, \"ts\": время ISO}} или null}}, начало паузы не позже конца")
    return c


def _write(exp_dir: Path, c: dict) -> dict:
    # ponytail: две записи владельца в один миг: последняя побеждает; замок, если появится второй пишущий кроме человека
    path = Path(exp_dir) / FILE
    tmp = path.with_name(f".{FILE}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_text(json.dumps(c, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return c


def _open(c: Mapping) -> dict | None:
    return next((p for p in c["pauses"] if p["to"] is None), None)


def _not_stopped(c: Mapping) -> None:
    if c["stop"]:
        raise ValueError(f"остановка уже запрошена {c['stop']['day']}: пауза больше ничего не меняет")


def pause(exp_dir: Path, day: str) -> dict:
    c = read_control(exp_dir)
    _not_stopped(c)
    if _open(c):
        return c
    c["pauses"].append({"from": day, "to": None})
    return _write(exp_dir, c)


def resume(exp_dir: Path, day: str) -> dict:
    c = read_control(exp_dir)
    _not_stopped(c)
    if not (p := _open(c)):
        return c
    p["to"] = day
    return _write(exp_dir, c)


def request_stop(exp_dir: Path, day: str, now: datetime) -> dict:
    c = read_control(exp_dir)
    if c["stop"]:
        return c
    c["stop"] = {"day": day, "ts": now.isoformat()}
    return _write(exp_dir, c)


def is_paused(control: Mapping, day: str) -> bool:
    return any(p["from"] <= day and (p["to"] is None or day <= p["to"]) for p in control["pauses"])


def _status(run: Path) -> str:
    try:
        return RunStore(run).state()["status"]
    except (OSError, ValueError, TypeError):
        return "не читается"


def summary(exp_dir: Path) -> dict:
    c = read_control(exp_dir)
    runs = [{"participant": r.name, "status": _status(r)} for r in sorted(p.parent for p in Path(exp_dir).glob("*/config.json"))]
    statuses = [r["status"] for r in runs]
    opened = _open(c)
    if all(s in TERMINAL_STATUSES for s in statuses):
        state = "finished"
    elif c["stop"]:
        state = "stopping"
    elif "active" not in statuses:
        state = "finishing"
    elif opened:
        state = "paused"
    else:
        state = "running"
    return {"experiment": Path(exp_dir).name, "state": state, "paused_since": opened and opened["from"],
            "stop": c["stop"], "runs": runs}
