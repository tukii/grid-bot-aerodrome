import pandas as pd
import pytest

from paperbot.engine import Engine
from paperbot.strategies.grid import Grid


def make_df(prices):
    idx = pd.date_range("2024-01-01", periods=len(prices), freq="h")
    return pd.DataFrame({"open": prices, "high": prices, "low": prices, "close": prices}, index=idx)


def test_grid_levels_symmetric():
    g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
    assert len(g.buy_levels) == 5
    assert len(g.sell_levels) == 5
    # buy levels strictly below anchor, sell strictly above
    assert max(lv.price for lv in g.buy_levels) < 100.0
    assert min(lv.price for lv in g.sell_levels) > 100.0
    # prices get closer as index grows? No: index 0 is nearest to anchor
    assert g.buy_levels[0].price == pytest.approx(99.0)
    assert g.sell_levels[0].price == pytest.approx(101.0)


def test_nearest_buy_sell():
    g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
    assert g.nearest_buy_price(99.5) == pytest.approx(99.0)
    assert g.nearest_buy_price(98.0) == pytest.approx(98.0)
    assert g.nearest_buy_price(50.0) is None  # below range -> no fill
    assert g.nearest_sell_price(101.5) == pytest.approx(102.0)
    assert g.nearest_sell_price(110.0) is None  # above range


def test_engine_flat_price_no_trades():
    g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
    e = Engine(g, capital_usd=5.0, taker_fee_pct=0.05, slippage_pct=0.0, gas_usd=0.0, min_trade_usd=0.0)
    df = make_df([100.0] * 10)
    res = e.run(df)
    # only the anchor buy may trigger at open; otherwise nothing
    assert res.final_total_usd == pytest.approx(5.0, abs=0.01)


def test_engine_roundtrip_profit_before_fees():
    g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
    e = Engine(g, capital_usd=100.0, taker_fee_pct=0.0, slippage_pct=0.0, gas_usd=0.0, min_trade_usd=0.0)
    # down to buy level 99, then up to sell level 101 -> buy low sell high
    df = make_df([100.0, 99.0, 100.0, 101.0])
    res = e.run(df)
    assert res.n_buys >= 1
    assert res.n_sells >= 1
    assert res.final_total_usd > res.initial_total_usd


def test_engine_fees_reduce_pnl():
    g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
    e1 = Engine(g, capital_usd=100.0, taker_fee_pct=0.0, slippage_pct=0.0, gas_usd=0.0, min_trade_usd=0.0)
    e2 = Engine(g, capital_usd=100.0, taker_fee_pct=1.0, slippage_pct=1.0, gas_usd=1.0, min_trade_usd=0.0)
    df = make_df([100.0, 99.0, 100.0, 101.0, 100.0, 99.0, 100.0, 101.0])
    r1 = e1.run(df)
    r2 = e2.run(df)
    assert r2.final_total_usd <= r1.final_total_usd


def test_engine_flat_after_run():
    g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
    e = Engine(g, capital_usd=100.0)
    df = make_df([100.0] * 5)
    res = e.run(df)
    assert len(res.equity) == len(df)
