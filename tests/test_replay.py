import pytest

from traider.market import Bar
from traider.replay import band, monkeys, percentile, replay, shadow, spy_buy_hold, spy_same_exposure

DAYS = ["2026-12-01", "2026-12-02", "2026-12-03", "2026-12-04"]
def bars(opens, lows, closes):
    return [Bar(d, o, max(o, c), l, c, 1e6) for d, o, l, c in zip(DAYS, opens, lows, closes)]
BARS = {
    "AAPL": bars([200, 201, 199, 190], [198, 199, 188, 185], [201, 202, 189, 187]),
    "XLE": bars([100, 100, 101, 102], [99, 99, 100, 101], [100, 101, 102, 103]),
    "SPY": bars([500, 500, 505, 505], [499, 499, 500, 500], [500, 505, 505, 510]),
}
SNAP = {d: {s: BARS[s][i].close for s in BARS} for i, d in enumerate(DAYS)}
BUY = {"kind": "buy", "day": DAYS[0], "symbol": "AAPL", "notional": 200.0, "price": 201.0, "stop_price": 190.95}   # стоп 5% ниже

def test_shadow_marks_to_the_close_and_stops_out_the_next_days():
    curve = shadow([BUY], SNAP, BARS, DAYS, 1000.0)
    qty = 200.0 / (201.0 * 1.0005)
    assert curve[0] == pytest.approx(800.0 + qty * 201.0)
    assert curve[1] == pytest.approx(800.0 + qty * 202.0)
    assert curve[2] == pytest.approx(800.0 + qty * 190.95 * 0.9995)      # 03.12: минимум 188 ниже стопа, открытие выше
    assert curve[3] == curve[2]

def test_a_gap_below_the_stop_fills_at_the_open():
    gap = {**BARS, "AAPL": bars([200, 180, 181, 182], [198, 178, 180, 181], [201, 181, 182, 183])}
    curve = shadow([BUY], {**SNAP, DAYS[1]: {**SNAP[DAYS[1]], "AAPL": 181.0}}, gap, DAYS, 1000.0)
    assert curve[1] == pytest.approx(800.0 + 200.0 / (201.0 * 1.0005) * 180.0 * 0.9995)

def test_close_and_raise_stop_are_replayed():
    events = [{**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0},
              {"kind": "raise_stop", "day": DAYS[1], "symbol": "XLE", "stop_price": 97.0},
              {"kind": "close", "day": DAYS[2], "symbol": "XLE", "fraction": 0.5}]
    curve = shadow(events, SNAP, BARS, DAYS, 1000.0)
    qty = 200.0 / (100.0 * 1.0005)
    assert curve[2] == pytest.approx(800.0 + qty / 2 * 102.0 * 0.9995 + qty / 2 * 102.0)
    assert curve[3] == pytest.approx(800.0 + qty / 2 * 102.0 * 0.9995 + qty / 2 * 103.0)

def test_pick_swaps_the_ticker_and_scales_the_stop():
    curve = replay([BUY], SNAP, BARS, DAYS, 1000.0, pick=lambda e: "XLE")
    assert curve[3] == pytest.approx(800.0 + 200.0 / (100.0 * 1.0005) * 103.0)      # стоп XLE на 95: не сработал
    assert spy_same_exposure([BUY], SNAP, BARS, DAYS, 1000.0) == replay([BUY], SNAP, BARS, DAYS, 1000.0, pick=lambda e: "SPY")

def test_monkeys_are_deterministic_and_differ_from_each_other():
    a = monkeys([BUY], SNAP, BARS, DAYS, 1000.0, ["AAPL", "XLE", "SPY"], n=30, seed=7)
    assert a == monkeys([BUY], SNAP, BARS, DAYS, 1000.0, ["AAPL", "XLE", "SPY"], n=30, seed=7)
    assert len(a) == 30 and len({tuple(c) for c in a}) == 3                          # три бумаги — три разные кривые
    assert a != monkeys([BUY], SNAP, BARS, DAYS, 1000.0, ["AAPL", "XLE", "SPY"], n=30, seed=8)

def test_missing_price_or_bar_does_not_crash():
    snap = {d: {k: v for k, v in SNAP[d].items() if k != "XLE"} for d in DAYS}
    curves = monkeys([BUY], snap, {k: v for k, v in BARS.items() if k != "XLE"}, DAYS, 1000.0, ["XLE", "AAPL"], n=5, seed=1)
    assert all(c == shadow([BUY], SNAP, BARS, DAYS, 1000.0) for c in curves)        # XLE без цены: взята следующая бумага

def test_no_events_means_flat_cash():
    assert shadow([], SNAP, BARS, DAYS, 1000.0) == [1000.0] * 4

def test_spy_buy_hold_percentile_and_band():
    assert spy_buy_hold(BARS, DAYS, 1000.0) == pytest.approx([1000.0, 1010.0, 1010.0, 1020.0])
    assert percentile(5, [1, 2, 3, 4]) == 100.0 and percentile(2.5, [1, 2, 3, 4]) == 50.0 and percentile(0, [1, 2]) == 0.0
    b = band([[1.0, 10.0], [2.0, 20.0], [3.0, 30.0]])
    assert b["p50"] == [2.0, 20.0] and set(b) == {"p5", "p25", "p50", "p75", "p95"}

# --- правила контролёра ---

def test_buy_is_capped_by_own_cash_and_skipped_under_one_dollar():
    big = {**BUY, "notional": 5000.0}
    qty = 1000.0 / (201.0 * 1.0005)
    assert shadow([big], SNAP, BARS, DAYS, 1000.0)[0] == pytest.approx(qty * 201.0)     # на все деньги
    assert shadow([BUY], SNAP, BARS, DAYS, 0.5) == [0.5] * 4                              # меньше $1: пропуск

def test_add_on_buy_goes_to_the_same_ticker_while_held():
    picks = iter(["XLE", "SPY"])
    events = [BUY, {**BUY, "day": DAYS[1], "price": 202.0, "stop_price": 191.9}]
    curve = replay(events, SNAP, BARS, DAYS, 1000.0, pick=lambda e: next(picks))
    q1, q2 = 200.0 / (100.0 * 1.0005), 200.0 / (101.0 * 1.0005)
    assert curve[3] == pytest.approx(600.0 + (q1 + q2) * 103.0)                          # SPY не выбирался

def test_buy_after_the_replay_position_was_stopped_out_picks_afresh():
    events = [BUY, {**BUY, "day": DAYS[3], "price": 187.0, "stop_price": 177.65}]
    picks = iter(["AAPL", "XLE"])
    curve = replay(events, SNAP, BARS, DAYS, 1000.0, pick=lambda e: next(picks))
    # AAPL выбит 03.12; 04.12 новый выбор XLE, а не докупка AAPL
    stopped = 800.0 + 200.0 / (201.0 * 1.0005) * 190.95 * 0.9995
    qty = 200.0 / (103.0 * 1.0005)
    assert curve[3] == pytest.approx(stopped - 200.0 + qty * 103.0)

def test_close_without_fraction_and_liquidate_close_everything():
    two = [{**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0}, {**BUY, "symbol": "SPY", "price": 500.0, "stop_price": 475.0}]
    full = shadow([*two, {"kind": "close", "day": DAYS[1], "symbol": "XLE"}], SNAP, BARS, DAYS, 1000.0)
    qx = 200.0 / (100.0 * 1.0005)
    qs = 200.0 / (500.0 * 1.0005)
    assert full[3] == pytest.approx(600.0 + qx * 101.0 * 0.9995 + qs * 510.0)
    liq = shadow([*two, {"kind": "liquidate", "day": DAYS[1]}], SNAP, BARS, DAYS, 1000.0)
    assert liq[3] == pytest.approx(600.0 + qx * 101.0 * 0.9995 + qs * 505.0 * 0.9995)

def test_raise_stop_without_price_uses_the_snapshot_and_a_lower_stop_is_ignored():
    buy = {**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0}
    up = {"kind": "raise_stop", "day": DAYS[1], "symbol": "XLE", "stop_price": 101.0}       # доля 101/101: стоп 101, минимум 02.12 уже прошёл
    down = {"kind": "raise_stop", "day": DAYS[1], "symbol": "XLE", "stop_price": 50.0}
    qty = 200.0 / (100.0 * 1.0005)
    assert shadow([buy, up], SNAP, BARS, DAYS, 1000.0)[3] == pytest.approx(800.0 + qty * 101.0 * 0.9995)   # 03.12 минимум 100 <= 101: выбит по min(101, открытие 101)
    assert shadow([buy, up, down], SNAP, BARS, DAYS, 1000.0) == shadow([buy, up], SNAP, BARS, DAYS, 1000.0)
    assert shadow([buy, down], SNAP, BARS, DAYS, 1000.0)[3] == pytest.approx(800.0 + qty * 103.0)

def test_close_uses_the_last_known_price_when_the_snapshot_lacks_the_ticker():
    snap = {**SNAP, DAYS[2]: {k: v for k, v in SNAP[DAYS[2]].items() if k != "XLE"}}
    buy = {**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0}
    curve = shadow([buy, {"kind": "close", "day": DAYS[2], "symbol": "XLE"}], snap, {k: v for k, v in BARS.items() if k != "XLE"}, DAYS, 1000.0)
    assert curve[3] == pytest.approx(800.0 + 200.0 / (100.0 * 1.0005) * 101.0 * 0.9995)   # последняя цена 101 (02.12)

def test_reject_and_stop_fill_events_change_nothing():
    noise = [{"kind": "reject", "day": DAYS[0], "symbol": "XLE", "action": "buy"},
             {"kind": "stop_fill", "day": DAYS[1], "symbol": "AAPL", "price": 1.0}]
    assert shadow([BUY, *noise], SNAP, BARS, DAYS, 1000.0) == shadow([BUY], SNAP, BARS, DAYS, 1000.0)


# --- раунд исправлений 1 ---

from traider.replay import spy_buy_hold as _hold  # noqa: E402

AAPL_BUY = BUY
MSFT_BUY = {"kind": "buy", "day": DAYS[0], "symbol": "MSFT", "notional": 200.0, "price": 400.0, "stop_price": 380.0}
to_xle = lambda e: "XLE"  # noqa: E731

def test_two_agent_positions_in_one_replacement_ticker_stay_separate():
    # Оба лота в XLE по 100: q = 200 / (100 * 1.0005) каждый. 02.12 половина лота AAPL продана по 101 * 0.9995.
    # 04.12: деньги 600 + q/2 * 101 * 0.9995 = 700.0..., остаток XLE: q/2 (AAPL) + q (MSFT) = 1.5q по 103.
    q = 200.0 / (100.0 * 1.0005)
    events = [AAPL_BUY, MSFT_BUY, {"kind": "close", "day": DAYS[1], "symbol": "AAPL", "fraction": 0.5}]
    curve = replay(events, SNAP, BARS, DAYS, 1000.0, pick=to_xle)
    assert curve[3] == pytest.approx(600.0 + q / 2 * 101.0 * 0.9995 + 1.5 * q * 103.0)        # 1009.7... (слитая позиция дала бы иное)
    assert curve[3] == pytest.approx(1009.7, abs=0.1)

def test_a_stopped_out_lot_does_not_hand_its_mapping_to_a_later_lot():
    pick = lambda e: {"AAPL": "AAPL", "MSFT": "XLE"}[e["symbol"]]  # noqa: E731
    msft = {**MSFT_BUY, "day": DAYS[3], "notional": 100.0}
    events = [AAPL_BUY, msft, {"kind": "close", "day": DAYS[3], "symbol": "AAPL"}]
    curve = replay(events, SNAP, BARS, DAYS, 1000.0, pick=pick)
    qa = 200.0 / (201.0 * 1.0005)
    assert curve[3] == pytest.approx(800.0 + qa * 190.95 * 0.9995 - 100.0 + 100.0 / 1.0005)     # лот MSFT цел

def test_spy_replay_keeps_two_agent_positions_apart():
    q = 200.0 / (500.0 * 1.0005)
    two = [AAPL_BUY, {**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0},
           {"kind": "close", "day": DAYS[1], "symbol": "AAPL", "fraction": 0.5}]
    curve = spy_same_exposure(two, SNAP, BARS, DAYS, 1000.0)
    assert curve[3] == pytest.approx(600.0 + q / 2 * 505.0 * 0.9995 + 1.5 * q * 510.0)

def test_a_bot_picking_the_agents_own_symbol_equals_the_shadow():
    events = [BUY, {**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0},
              {"kind": "raise_stop", "day": DAYS[1], "symbol": "XLE", "stop_price": 97.0},
              {"kind": "close", "day": DAYS[2], "symbol": "XLE", "fraction": 0.5}]
    assert replay(events, SNAP, BARS, DAYS, 1000.0, pick=lambda e: e["symbol"]) == shadow(events, SNAP, BARS, DAYS, 1000.0)

def test_stops_are_checked_on_bars_of_days_missing_from_days():
    gap = {**BARS, "AAPL": bars([200, 201, 199, 190], [198, 170, 195, 195], [201, 202, 189, 187])}
    days = [DAYS[0], DAYS[2], DAYS[3]]
    curve = shadow([BUY], SNAP, gap, days, 1000.0)
    assert curve[1] == pytest.approx(800.0 + 200.0 / (201.0 * 1.0005) * 190.95 * 0.9995)       # выбит 02.12, которого нет в days

def test_an_event_outside_days_raises():
    with pytest.raises(ValueError, match="buy AAPL 2026-12-09"):
        shadow([{**BUY, "day": "2026-12-09"}], SNAP, BARS, DAYS, 1000.0)

def test_the_bot_chooses_uniformly_among_priced_tickers():
    snaps = {DAYS[0]: {"A": 100.0, "D": 100.0}, DAYS[1]: {"A": 110.0, "D": 100.0}}
    buy = {"kind": "buy", "day": DAYS[0], "symbol": "Z", "notional": 100.0, "price": 100.0, "stop_price": 95.0}
    curves = monkeys([buy], snaps, {}, DAYS[:2], 1000.0, ["A", "B", "C", "D"], n=400, seed=3)
    a = sum(c[1] > 1000.5 for c in curves)
    assert 0.4 * 400 <= a <= 0.6 * 400 and all(c[1] > 1000.5 or c[1] == pytest.approx(1000.0 - 100.0 + 100.0 / 1.0005) for c in curves)

def test_no_priced_ticker_skips_the_bot_buy():
    assert monkeys([BUY], {d: {} for d in DAYS}, {}, DAYS, 1000.0, ["A", "B"], n=3) == [[1000.0] * 4] * 3

def test_percentile_counts_ties_half():
    assert percentile(2, [2, 2, 2]) == 50.0
    assert percentile(2, [1, 2, 3, 4]) == 37.5          # один ниже, один равен: (1 + 0.5) / 4
    assert percentile(1.0, []) is None

def test_spy_buy_hold_starts_from_the_first_available_bar():
    part = {"SPY": BARS["SPY"][1:]}
    assert spy_buy_hold(part, DAYS, 1000.0) == pytest.approx([1000.0, 1000.0, 1000.0, 1000.0 * 510.0 / 505.0])

def test_raise_stop_uses_the_last_known_price_when_the_snapshot_lacks_the_ticker():
    xle = {**BARS, "XLE": bars([100, 100, 101, 102], [99, 99, 96, 101], [100, 101, 102, 103])}
    snap = {**SNAP, DAYS[1]: {k: v for k, v in SNAP[DAYS[1]].items() if k != "XLE"}}
    buy = {**BUY, "symbol": "XLE", "price": 100.0, "stop_price": 93.0}
    up = {"kind": "raise_stop", "day": DAYS[1], "symbol": "XLE", "price": 100.0, "stop_price": 97.0}
    curve = shadow([buy, up], snap, xle, DAYS, 1000.0)
    assert curve[3] == pytest.approx(800.0 + 200.0 / (100.0 * 1.0005) * 97.0 * 0.9995)         # стоп 97 поднят по цене 100 с 01.12
