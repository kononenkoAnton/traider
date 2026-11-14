"""Собирает data/universe.txt: S&P 500 плюс ETF из дизайна.

Запуск: `uv run python tools/build_universe.py [--alpaca]`. С флагом `--alpaca` оставляет только
дробимые бумаги по `AlpacaMarket.fractionable` (нужны ключи в .env). Без флага фильтра нет.
"""
import csv
import io
import sys
import urllib.request
from pathlib import Path

from traider.env import load_env
from traider.market import AlpacaMarket

SOURCE = "https://raw.githubusercontent.com/datasets/s-and-p-500-companies/main/data/constituents.csv"
ETFS = "SPY QQQ IWM DIA XLK XLF XLE XLV XLI XLY XLP XLU XLB XLRE XLC TLT GLD".split()
OUT = Path(__file__).resolve().parent.parent / "data" / "universe.txt"


def main() -> None:
    with urllib.request.urlopen(SOURCE, timeout=30) as r:
        rows = csv.DictReader(io.StringIO(r.read().decode()))
        symbols = {row["Symbol"].strip().replace("-", ".") for row in rows}
    symbols |= set(ETFS)
    if "--alpaca" in sys.argv:
        env = load_env()
        symbols = AlpacaMarket(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]).fractionable(symbols)
    OUT.write_text("\n".join(sorted(symbols)) + "\n")
    print(f"{len(symbols)} тикеров в {OUT}")


if __name__ == "__main__":
    main()
