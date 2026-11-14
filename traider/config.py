"""Конфиг эксперимента и заморозка прогонов.

:wk-id: config-module
:wk-tags: config, runs
:wk-categories: system-architecture

Как это работает. `start_experiment` читает TOML (`tomllib`) и проверяет его целиком
до первой записи на диск: режим live требует флага `live=True`, модель должна быть в
таблице `[prices]`, брокер из `alpaca|sim`, источник информации из `self|analyst`.
Пути к файлам вселенной и промптов считаются от каталога самого TOML. Для каждого
участника в `runs/<experiment>/<participant>/` пишутся `config.json` (тексты промптов
и вселенная встроены в него) и `config.sha256` (SHA-256 байтов `config.json`).
Каталог участника, который уже есть, не перезаписывается. `load_run` читает
замороженный конфиг и возвращает признак, сошлась ли контрольная сумма: правка файла
не скрывается, а помечается. Лимиты у всех участников общие (`Limits()`), TOML
переопределяет только `max_session_usd`.
"""
import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

BROKERS = ("alpaca", "sim")
RESEARCH = ("self", "analyst")


@dataclass(frozen=True)
class Limits:
    max_position_share: float = 0.20
    min_order_usd: float = 20.0
    stop_min: float = 0.03
    stop_max: float = 0.15
    max_open_risk_share: float = 0.08
    max_buys_per_session: int = 3
    floor_share: float = 0.85
    max_turns: int = 40
    max_searches: int = 8
    max_session_usd: float = 2.0


@dataclass(frozen=True)
class RunConfig:
    experiment: str
    participant: str
    model: str
    broker: str
    research: str
    mode: str
    pilot: bool
    start_equity: float
    sessions_target: int
    window_start: str
    window_end: str
    limits: Limits
    universe: tuple[str, ...]
    panel: tuple[str, ...]
    seed: int
    token_prices: dict
    criteria: dict
    system_prompt: str
    analyst_model: str | None
    analyst_prompt: str | None
    analyst_max_searches: int
    analyst_max_usd: float


def _build(toml_path: Path, live: bool) -> list[RunConfig]:
    t = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    base = toml_path.parent
    if t["mode"] not in ("paper", "live"):
        raise ValueError(f"неизвестный режим: {t['mode']}")
    if t["mode"] == "live" and not live:
        raise ValueError("режим live требует явного флага live=True")
    universe = tuple((base / t["universe_file"]).read_text(encoding="utf-8").split())
    system = (base / t["system_prompt_file"]).read_text(encoding="utf-8")
    analyst_text = (base / t["analyst_prompt_file"]).read_text(encoding="utf-8")
    runs = []
    for p in t["participants"]:
        research = p.get("research", "self")
        if p["model"] not in t["prices"]:
            raise ValueError(f"{p['name']}: нет цен для модели {p['model']}")
        if p["broker"] not in BROKERS or research not in RESEARCH:
            raise ValueError(f"{p['name']}: неизвестный брокер или источник информации")
        analyst = research == "analyst"
        if analyst and p.get("analyst_model") not in t["prices"]:
            raise ValueError(f"{p['name']}: у аналитика нет модели с ценами")
        limits = Limits(max_session_usd=p["max_session_usd"]) if "max_session_usd" in p else Limits()
        runs.append(RunConfig(
            experiment=t["experiment"], participant=p["name"], model=p["model"],
            broker=p["broker"], research=research, mode=t["mode"], pilot=t["pilot"],
            start_equity=t["start_equity"], sessions_target=t["sessions_target"],
            window_start=t["window_start"], window_end=t["window_end"], limits=limits,
            universe=universe, panel=tuple(t["panel"]), seed=t["seed"],
            token_prices=t["prices"][p["model"]], criteria=t["criteria"],
            system_prompt=system,
            analyst_model=p["analyst_model"] if analyst else None,
            analyst_prompt=analyst_text if analyst else None,
            analyst_max_searches=p.get("analyst_max_searches", 0) if analyst else 0,
            analyst_max_usd=p.get("analyst_max_usd", 0.0) if analyst else 0.0,
        ))
    return runs


def start_experiment(toml_path: Path, runs_dir: Path, live: bool = False) -> Path:
    configs = _build(Path(toml_path), live)
    exp_dir = Path(runs_dir) / configs[0].experiment
    for cfg in configs:
        if (exp_dir / cfg.participant).exists():
            raise FileExistsError(exp_dir / cfg.participant)
    for cfg in configs:
        run = exp_dir / cfg.participant
        run.mkdir(parents=True)
        data = json.dumps(asdict(cfg), sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8")
        (run / "config.json").write_bytes(data)
        (run / "config.sha256").write_text(hashlib.sha256(data).hexdigest())
    return exp_dir


def load_run(run_dir: Path) -> tuple[RunConfig, bool]:
    raw = (Path(run_dir) / "config.json").read_bytes()
    valid = hashlib.sha256(raw).hexdigest() == (Path(run_dir) / "config.sha256").read_text().strip()
    d = json.loads(raw)
    d["limits"] = Limits(**d["limits"])
    d["universe"], d["panel"] = tuple(d["universe"]), tuple(d["panel"])
    return RunConfig(**d), valid


def active_runs(runs_dir: Path) -> list[Path]:
    # ponytail: статус прогона появится в задаче 4, пока активны все каталоги с config.json
    return sorted(p.parent for p in Path(runs_dir).glob("*/*/config.json"))
