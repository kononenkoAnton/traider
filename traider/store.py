"""Хранилище прогона: единственный код, который читает и пишет файлы прогона.

:wk-id: run-store-module
:wk-tags: storage, ledger
:wk-categories: system-architecture

Как это работает. `RunStore` привязан к каталогу прогона. Журнал заявок `orders.jsonl`
только дописывается: каждая запись получает `seq` (следующий после последней целой
строки) и `ts`, строка пишется одним вызовом, сбрасывается и делает fsync. Оборванная
сбоем последняя строка при чтении пропускается, а следующая запись начинается с новой
строки. Остальные JSON-файлы (состояние, записи сессий, снимки цен) пишутся через
временный файл и `os.replace`, чтобы сбой не оставил половину файла. Факты сессии для
клетки (`SessionFacts`) считаются из журнала заявок и прогнозов, отдельно не хранятся.
Встроенный счёт лежит в `state.json` под ключом `account`.
"""
import json
import os
from collections.abc import Sequence
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from traider.cage import Account, Position, SessionFacts

DEFAULT_STATE = {"status": "active", "notes": "", "last_sync": None}


def _write_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def _read_lines(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # оборванная сбоем строка
    return out


def _append_line(path: Path, record: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "ab+") as f:
        f.seek(0, os.SEEK_END)
        fresh = f.tell() == 0 or (f.seek(-1, os.SEEK_END), f.read(1))[1] == b"\n"
        f.seek(0, os.SEEK_END)
        f.write((b"" if fresh else b"\n") + json.dumps(record, ensure_ascii=False).encode() + b"\n")
        f.flush()
        os.fsync(f.fileno())


class RunStore:
    def __init__(self, run_dir: Path):
        self.dir = Path(run_dir)

    # журнал заявок
    def append_order(self, event: dict) -> dict:
        path = self.dir / "orders.jsonl"
        seq = max((e.get("seq", 0) for e in _read_lines(path)), default=0) + 1
        rec = {"seq": seq, "ts": datetime.now(timezone.utc).isoformat(), **event}
        _append_line(path, rec)
        return rec

    def orders(self, day: str | None = None) -> list[dict]:
        evs = _read_lines(self.dir / "orders.jsonl")
        return evs if day is None else [e for e in evs if e.get("day") == day]

    def find_order(self, client_order_id: str) -> dict | None:
        return next((e for e in self.orders() if e.get("client_order_id") == client_order_id), None)

    # сессии
    def _session_path(self, day: str) -> Path:
        return self.dir / "sessions" / f"{day}.json"

    def start_session(self, day: str, now: datetime) -> bool:
        path = self._session_path(day)
        if path.exists():  # тики идут под замком, проверки и os.replace достаточно
            return False
        _write_json(path, {"day": day, "status": "started", "started_at": now.isoformat()})
        return True

    def finish_session(self, day: str, status: str, record: dict) -> None:
        rec = {**(self.session(day) or {"day": day}), **record, "status": status}
        _write_json(self._session_path(day), rec)

    def session(self, day: str) -> dict | None:
        path = self._session_path(day)
        try:
            return _read_json(path)
        except json.JSONDecodeError:  # повреждённый файл считается зависшим стартом
            mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
            return {"day": day, "status": "started", "started_at": mtime.isoformat()}

    def sessions(self) -> list[dict]:
        return [s for p in sorted((self.dir / "sessions").glob("*.json")) if (s := self.session(p.stem))]

    def stale_started(self, now: datetime, minutes: int = 30) -> list[str]:
        return [s["day"] for s in self.sessions() if s.get("status") == "started"
                and (now - datetime.fromisoformat(s["started_at"])).total_seconds() > minutes * 60]

    # состояние и счёт
    def state(self) -> dict:
        return {**DEFAULT_STATE, **(_read_json(self.dir / "state.json") or {})}

    def update_state(self, **changes) -> dict:
        st = {**self.state(), **changes}
        _write_json(self.dir / "state.json", st)
        return st

    def account(self) -> Account | None:
        a = self.state().get("account")
        return None if a is None else Account(a["cash"], tuple(Position(**p) for p in a["positions"]), a["peak_equity"])

    def save_account(self, acct: Account) -> None:
        self.update_state(account=asdict(acct))

    # цены и прогнозы
    def save_prices(self, day: str, prices: dict[str, float]) -> None:
        _write_json(self.dir / "prices" / f"{day}.json", prices)

    def prices(self, day: str) -> dict[str, float] | None:
        return _read_json(self.dir / "prices" / f"{day}.json")

    def append_forecasts(self, day: str, items: list[dict]) -> None:
        for it in items:
            _append_line(self.dir / "forecasts.jsonl", {"day": day, "symbol": it["symbol"], "p": it["p"]})

    def forecasts(self) -> list[dict]:
        return _read_lines(self.dir / "forecasts.jsonl")

    def facts(self, day: str, panel: Sequence[str]) -> SessionFacts:
        evs = self.orders(day)
        seen = frozenset(f["symbol"] for f in self.forecasts() if f["day"] == day)
        return SessionFacts(
            buys_done=sum(e["kind"] == "buy" for e in evs),
            closed_today=frozenset(e["symbol"] for e in evs if e["kind"] in ("close", "stop_fill")),
            forecast_symbols=seen,
            panel_done=all(s in seen for s in panel),
        )
