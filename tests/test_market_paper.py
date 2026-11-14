import pytest

from tests.market_contract import run_market_contract
from traider.env import load_env
from traider.market import AlpacaMarket


@pytest.mark.paper
def test_alpaca_market_satisfies_the_contract():
    env = load_env()
    run_market_contract(AlpacaMarket(env["ALPACA_API_KEY"], env["ALPACA_SECRET_KEY"]))
