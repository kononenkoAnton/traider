"""Конфиг эксперимента и заморозка прогонов.

:wk-id: config-module
:wk-tags: config, runs
:wk-categories: system-architecture

Как это работает. `start_experiment` читает TOML (`tomllib`) и проверяет его целиком
до первой записи на диск: режим live требует флага `live=True`, модель должна быть в
таблице `[prices]`, брокер из `alpaca|sim`, источник информации из `self|analyst`, все
бумаги панели во вселенной, в `[criteria]` и в каждой таблице цен есть все ключи; длинные ставки (`long_prompt_threshold` и четыре `*_long`) заданы
либо все, либо ни одной.
Пути к файлам вселенной и промптов считаются от каталога самого TOML. Для каждого
участника в `runs/<experiment>/<participant>/` пишутся `config.json` (тексты промптов
и вселенная встроены в него) и `config.sha256` (SHA-256 байтов `config.json`).
Каталог участника, который уже есть, не перезаписывается. `load_run` читает
замороженный конфиг и возвращает признак, сошлась ли контрольная сумма: правка файла
не скрывается, а помечается. У участника с аналитиком в конфиг замораживается и таблица цен модели аналитика
(`analyst_token_prices`), у остальных там None. Лимиты у всех участников общие (`Limits()`), TOML
переопределяет только `max_session_usd`. При создании режим и брокер записываются в `state.json` прогона (`started_with`), вне `config.json`. `active_runs` отдаёт планировщику каждый прогон,
чей статус не конечный (`TERMINAL_STATUSES`), в том числе `invalid`: у него отключаются
только сессии агента, HP и стопы работают.
Счёт Alpaca принадлежит одному прогону: `start_experiment` до первой записи отказывает, если в `runs_dir`
уже есть нетерминальный прогон с брокером `alpaca` в том же режиме (`alpaca_runs`, по `started_with`), а
новый эксперимент тоже берёт Alpaca.
"""
import hashlib
import json
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

BROKERS = ("alpaca", "sim")
MODES = ("paper", "live")
TERMINAL_STATUSES = ("dead", "finished")   # прогон в этих статусах больше не проверяется
RESEARCH = ("self", "analyst")
CRITERIA_KEYS = ("min_sessions", "max_missed_share", "min_closed_trades", "red_percentile", "green_percentile")
PRICE_KEYS = ("input", "output", "cache_read", "cache_write", "search")
LONG_PRICE_KEYS = ("long_prompt_threshold", "input_long", "output_long", "cache_read_long", "cache_write_long")


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
    max_session_seconds: int = 1200


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
    analyst_token_prices: dict | None


def _build(toml_path: Path, live: bool) -> list[RunConfig]:
    t = tomllib.loads(toml_path.read_text(encoding="utf-8"))
    base = toml_path.parent
    if t["mode"] not in MODES:
        raise ValueError(f"неизвестный режим: {t['mode']}")
    if t["mode"] == "live" and not live:
        raise ValueError("режим live требует явного флага live=True")
    universe = tuple((base / t["universe_file"]).read_text(encoding="utf-8").split())
    system = (base / t["system_prompt_file"]).read_text(encoding="utf-8")
    analyst_text = (base / t["analyst_prompt_file"]).read_text(encoding="utf-8")
    if missing := [s for s in t["panel"] if s not in universe]:
        raise ValueError(f"бумаг панели нет во вселенной: {', '.join(missing)}")
    if missing := [k for k in CRITERIA_KEYS if k not in t["criteria"]]:
        raise ValueError(f"в [criteria] нет ключей: {', '.join(missing)}")
    for model, table in t["prices"].items():
        if missing := [k for k in PRICE_KEYS if k not in table]:
            raise ValueError(f"в ценах {model} нет ключей: {', '.join(missing)}")
        if (have := [k for k in LONG_PRICE_KEYS if k in table]) and len(have) < len(LONG_PRICE_KEYS):
            raise ValueError(f"в ценах {model} длинные ставки заданы не целиком, нет: "
                             f"{', '.join(k for k in LONG_PRICE_KEYS if k not in table)}")
    runs = []
    names = [p["name"] for p in t["participants"]]
    for name in set(names):
        if names.count(name) > 1:
            raise ValueError(f"имя участника повторяется: {name}")
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
            analyst_token_prices=t["prices"][p["analyst_model"]] if analyst else None,
        ))
    return runs


def start_experiment(toml_path: Path, runs_dir: Path, live: bool = False) -> Path:
    from traider.store import RunStore  # store импортирует cage, а cage импортирует config

    configs = _build(Path(toml_path), live)
    exp_dir = Path(runs_dir) / configs[0].experiment
    for cfg in configs:
        if (exp_dir / cfg.participant).exists():
            raise FileExistsError(exp_dir / cfg.participant)
    if any(c.broker == "alpaca" for c in configs) and (busy := alpaca_runs(runs_dir, configs[0].mode)):
        # брокер Alpaca считает своими все позиции счёта: второй прогон на тех же ключах торговал бы чужим
        raise ValueError(f"счёт Alpaca ({configs[0].mode}) уже занят прогоном "
                         f"{', '.join(f'{r.parent.name}/{r.name}' for r in busy)}: дождитесь у него статуса "
                         f"finished или dead или уберите каталог его эксперимента из {runs_dir}")
    for cfg in configs:
        run = exp_dir / cfg.participant
        run.mkdir(parents=True)
        data = json.dumps(asdict(cfg), sort_keys=True, ensure_ascii=False, indent=1).encode("utf-8")
        (run / "config.json").write_bytes(data)
        (run / "config.sha256").write_text(hashlib.sha256(data).hexdigest())
        # режим и брокер ещё и вне config.json: тик сверяет с ними конфиг, чтобы правка файла не открыла живой счёт
        RunStore(run).update_state(started_with={"mode": cfg.mode, "broker": cfg.broker})
    return exp_dir


def load_run(run_dir: Path) -> tuple[RunConfig, bool]:
    raw = (Path(run_dir) / "config.json").read_bytes()
    sha = Path(run_dir) / "config.sha256"
    # нет файла суммы = прогон помечен как изменённый, но конфиг читается (HP и стопы работают)
    valid = sha.exists() and hashlib.sha256(raw).hexdigest() == sha.read_text().strip()
    d = json.loads(raw)
    d["limits"] = Limits(**d["limits"])
    d["universe"], d["panel"] = tuple(d["universe"]), tuple(d["panel"])
    return RunConfig(**d), valid


def active_runs(runs_dir: Path) -> list[Path]:
    from traider.store import RunStore  # store импортирует cage, а cage импортирует config

    def active(run: Path) -> bool:
        try:
            return RunStore(run).state()["status"] not in TERMINAL_STATUSES
        except (OSError, ValueError, TypeError):
            return True  # нечитаемое состояние не прячет прогон: тик сообщит об ошибке по нему

    return sorted(p.parent for p in Path(runs_dir).glob("*/*/config.json") if active(p.parent))


def alpaca_runs(runs_dir: Path, mode: str) -> list[Path]:
    """Нетерминальные прогоны на Alpaca в режиме `mode` по записи `started_with` (прогон с нечитаемым
    состоянием сюда не входит: тик всё равно ничего по нему не делает)."""
    from traider.store import RunStore

    def match(run: Path) -> bool:
        try:
            return RunStore(run).state().get("started_with") == {"mode": mode, "broker": "alpaca"}
        except (OSError, ValueError, TypeError):
            return False

    return [r for r in active_runs(runs_dir) if match(r)]
