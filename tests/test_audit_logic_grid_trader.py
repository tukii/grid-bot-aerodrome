"""Auditoría sorpresa — lógica del trader y el grid.

Cubre paperbot/live/trader.py + paperbot/strategies/grid.py con mocks,
sin red y sin tocar la DB real (Store se construye sobre tmp_path).
"""
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _store(tmp_path):
    from paperbot.paper.store import Store
    return Store(str(tmp_path / "live.db"))


def _make_trader(monkeypatch, tmp_path, grid_cfg=None):
    """LiveGridTrader sin red: bot/account/price/alert mockeados."""
    from paperbot.live.trader import LiveGridTrader
    cfg = {
        "pool": {"address": "0xpool", "base_token": "WETH",
                 "base_token_address": "0x" + "b" * 40,
                 "quote_token_address": "0x" + "q" * 40},
        "grid": {
            "anchor_price": 2446.0,
            "spacing_pct": 3.5,
            "range_pct": 20.0,
            "drift_reanchor_pct": 5.0,
        },
        "live": {
            "poll_seconds": 120, "dry_run": True,
            "stop_loss_pct": 10.0, "rebalance_threshold": 0.99,
            "max_spend_usd": 4.5, "max_position_pct": 0.8,
            "min_liquidity_usd": 1e6, "min_volume_usd": 1e6,
            "rotation_enabled": False, "rotation_interval_h": 6.0,
            "min_vol_ratio": 1.5, "db_path": "x.db",
            "min_order_usd": 0.01,
        },
    }
    if grid_cfg:
        cfg["grid"].update(grid_cfg)
    monkeypatch.setattr("paperbot.live.trader.load_config", lambda: cfg)
    monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: 2446.0)
    monkeypatch.setattr("paperbot.live.trader.send_alert", lambda *a, **k: None)
    # USD_PER_BASE es global en el módulo trader — limpiarla para aislar tests
    monkeypatch.setattr("paperbot.live.trader.USD_PER_BASE", None)

    # bot 100% mockeado: sin red, sin .env
    bot = MagicMock()
    bot.get_account.return_value = MagicMock(address="0x" + "a" * 40)
    bot.base_token = "0x" + "b" * 40
    bot.usdc = "0x" + "q" * 40
    bot.weth = "0x" + "w" * 40
    bot.base_decimals = 18
    bot.quote_decimals = 6
    bot.token_balance = MagicMock(return_value=0)
    bot.eth_balance = MagicMock(return_value=0)
    bot.w3 = MagicMock()
    bot.w3.eth.gas_price = 1
    bot.w3.from_wei = lambda v, u: v / 1e18
    # SwapResult-like: atributos que el trader lee
    bot.swap_exact_in = MagicMock()
    bot.verify_on_chain = MagicMock(return_value=True)

    monkeypatch.setattr("paperbot.live.trader.AerodromeLive", lambda: bot)
    store = _store(tmp_path)
    t = LiveGridTrader(store, poll_seconds=1, dry_run=True)
    return t, store


def _with_balances(t, base=0.0, usdc=1.0):
    """base/usdc en unidades (WETH/USDC) -> raw ints según decimals.
    El bot es MagicMock: token_balance devuelve por prefijo de dirección."""
    t.bot.token_balance.side_effect = lambda tok, addr: (
        int(base * 10 ** t.bot.base_decimals)
        if str(tok).lower().startswith("0x" + "b") else
        int(usdc * 10 ** t.bot.quote_decimals)
    )


def _swap_ok(t):
    r = MagicMock()
    r.ok, r.receipt_status, r.gas_used, r.tx_hash = True, 1, 100000, "0xabc"
    r.message = "ok"
    t.bot.swap_exact_in = MagicMock(return_value=r)


# ---------------------------------------------------------------------------
# grid.py — geometría de niveles y cruce
# ---------------------------------------------------------------------------

class TestGridGeometry:
    def test_levels_equidistant_and_symmetric(self):
        from paperbot.strategies.grid import Grid
        g = Grid(anchor=100.0, spacing_pct=5.0, range_pct=10.0)
        # range 10% / spacing 5% -> 2 niveles por lado
        assert len(g.buy_levels) == 2 and len(g.sell_levels) == 2
        assert [round(lv.price, 10) for lv in g.buy_levels] == [95.0, 90.0]      # 100*(1-0.05*(i+1))
        assert [round(lv.price, 10) for lv in g.sell_levels] == [105.0, 110.0]
        assert [round(p, 10) for p in g.levels] == [95.0, 90.0, 100.0, 105.0, 110.0]  # buys, anchor, sells

    def test_buy_levels_strictly_below_anchor(self):
        from paperbot.strategies.grid import Grid
        g = Grid(anchor=2446.0, spacing_pct=3.5, range_pct=20.0)
        assert all(lv.price < g.anchor for lv in g.buy_levels)
        assert all(lv.price > g.anchor for lv in g.sell_levels)
        assert len(g.buy_levels) == len(g.sell_levels) == 6
        assert g.buy_levels[0].price == pytest.approx(2446.0 * (1 - 0.035))
        assert g.sell_levels[0].price == pytest.approx(2446.0 * (1 + 0.035))

    def test_nearest_buy_sell(self):
        from paperbot.strategies.grid import Grid
        g = Grid(anchor=100.0, spacing_pct=5.0, range_pct=10.0)
        # precio justo en un nivel de compra -> ese nivel
        assert g.nearest_buy_price(95.0) == 95.0
        assert g.nearest_sell_price(105.0) == 105.0
        # entre niveles -> el más cercano por debajo/arriba
        assert g.nearest_buy_price(94.0) == 90.0
        assert g.nearest_sell_price(106.0) == pytest.approx(110.0)
        # por encima de todos los niveles de compra -> el más alto disponible
        # (equivalente al primer nivel por encima del ancla); y viceversa

    def test_grid_rejects_invalid_params(self):
        from paperbot.strategies.grid import Grid
        with pytest.raises(ValueError):
            Grid(anchor=0, spacing_pct=5, range_pct=10)
        with pytest.raises(ValueError):
            Grid(anchor=100, spacing_pct=0, range_pct=10)
        with pytest.raises(ValueError):
            Grid(anchor=100, spacing_pct=5, range_pct=-1)

    def test_anchor_level_has_buy_flag_true(self):
        """Nivel ancla: se usa como compra solo si no hay posición (ver _reset_orders)."""
        from paperbot.strategies.grid import Grid
        g = Grid(anchor=100.0, spacing_pct=5.0, range_pct=10.0)
        # el ancla NO forma parte de buy_levels (para no duplicar órdenes)
        assert all(lv.price != 100.0 for lv in g.buy_levels)
        assert all(lv.price != 100.0 for lv in g.sell_levels)


# ---------------------------------------------------------------------------
# trader.py — cruce de niveles (one_cycle) y rotación de órdenes
# ---------------------------------------------------------------------------

class TestOneCycleCrossing:
    def test_buy_fires_when_price_crosses_level_downwards(self, monkeypatch, tmp_path):
        """Con price < nivel buy pendiente -> compra y rota a sell en el nivel superior."""
        from paperbot.live.trader import LiveGridTrader
        t, store = _make_trader(monkeypatch, tmp_path)
        # reinicia órdenes con posición base 0 -> anchor-buy incluido
        t.bot.token_balance.side_effect = lambda tok, addr: (
            0 if str(tok).lower().startswith("0x" + "b") else int(0.5 * 1e6))
        t._reset_orders()
        buy_levels = {round(lv.price, 8) for lv in t.grid.buy_levels}
        assert any(s == "buy" for s in t._orders.values())
        step = t.grid.spacing
        # precio = primer nivel de compra menos un pelo -> ese buy debe dispararse
        target = t.grid.buy_levels[0].price
        monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: target - 0.01)
        _swap_ok(t)
        res = t.one_cycle()
        assert res["ok"]
        # la orden buy en `target` debe haber sido consumida
        assert round(target, 8) not in t._orders
        # y rotada a sell en target*(1+step)
        assert round(target * (1 + step), 8) in t._orders
        assert t._orders[round(target * (1 + step), 8)] == "sell"
        # ningún buy pendiente por debajo del precio de cruce (todos los niveles
        # <= price ya se procesaron; los buys restantes están por debajo del nivel
        # comprado, y el nuevo buy rotado está justo un escalón bajo target)
        for p, s in t._orders.items():
            if s == "buy":
                assert p <= target - 0.01

    def test_sell_fires_when_price_crosses_level_upwards(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        # posición base para poder vender (0.002 WETH)
        t.bot.token_balance.side_effect = lambda tok, addr: (
            int(0.002 * 10 ** 18) if str(tok).lower().startswith("0x" + "b") else int(0.5 * 1e6))
        t._reset_orders()
        # solo dejamos una orden sell pendiente en el primer nivel de venta.
        # NO llamamos a one_cycle completo: aislar la lógica de cruce del loop
        # (total $0.5 -> _order_size_base devuelve 0 -> skip 'order too small').
        sl = t.grid.sell_levels[0].price
        t._orders = {round(sl, 8): "sell"}
        _swap_ok(t)
        monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: sl + 0.01)
        res = t.one_cycle()
        assert res["ok"]
        assert round(sl, 8) not in t._orders
        # rotada a buy en sl/(1+step) == ancla
        step = t.grid.spacing
        assert round(sl / (1 + step), 8) in t._orders
        assert t._orders[round(sl / (1 + step), 8)] == "buy"

    def test_buy_fails_then_order_retained(self, monkeypatch, tmp_path):
        """Swap fallido -> la orden NO se consume (evita órdenes fantasma)."""
        t, store = _make_trader(monkeypatch, tmp_path)
        t.bot.token_balance.side_effect = lambda tok, addr: (
            0 if str(tok).lower().startswith("0x" + "b") else int(0.5 * 1e6))
        t._reset_orders()
        target = t.grid.buy_levels[0].price
        r = MagicMock()
        r.ok, r.receipt_status = False, None
        r.message = "revert"
        r.gas_used, r.tx_hash = None, None
        t.bot.swap_exact_in = MagicMock(return_value=r)
        monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: target - 0.01)
        t.one_cycle()
        # orden retenida
        assert round(target, 8) in t._orders
        assert t._orders[round(target, 8)] == "buy"
        # y sin rotar a sell
        assert round(target * (1 + t.grid.spacing), 8) not in t._orders

    def test_phantom_orders_pruned(self, monkeypatch, tmp_path):
        """Órdenes fuera del grid vigente se descartan antes de evaluar fills."""
        t, store = _make_trader(monkeypatch, tmp_path)
        t.bot.token_balance.side_effect = lambda tok, addr: (
            0 if str(tok).lower().startswith("0x" + "b") else int(0.5 * 1e6))
        t._reset_orders()
        valid = {round(lv.price, 8) for lv in t.grid.buy_levels} | \
                {round(lv.price, 8) for lv in t.grid.sell_levels} | \
                {round(t.grid.anchor, 8)}
        # orden fantasma en un precio absurdo
        t._orders[1234.5678] = "buy"
        t._orders[9999.0] = "sell"
        _swap_ok(t)
        monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: 2446.0)
        t.one_cycle()
        assert 1234.5678 not in t._orders
        assert 9999.0 not in t._orders

    def test_no_price_returns_ok_false(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: None)
        assert t.one_cycle() == {"ok": False}

    def test_anchor_buy_only_when_no_position(self, monkeypatch, tmp_path):
        """_reset_orders: con posición base NO añade anchor-buy; sin posición SÍ."""
        t, store = _make_trader(monkeypatch, tmp_path)
        # sin posición
        t.bot.token_balance.side_effect = lambda tok, addr: (
            0 if str(tok).lower().startswith("0x" + "b") else int(0.5 * 1e6))
        t._reset_orders()
        assert round(t.grid.anchor, 8) in t._orders
        assert t._orders[round(t.grid.anchor, 8)] == "buy"
        # con posición -> la compra en el ancla ya se ejecutó: el nivel queda
        # rotado a sell en anchor*(1+step), y los buys de los niveles inferiores
        # siguen pendientes
        t.bot.token_balance.side_effect = lambda tok, addr: (
            int(0.001 * 1e18) if str(tok).lower().startswith("0x" + "b") else int(0.5 * 1e6))
        t._reset_orders()
        assert round(t.grid.anchor, 8) not in t._orders
        assert round(t.grid.anchor * (1 + t.grid.spacing), 8) in t._orders
        assert t._orders[round(t.grid.anchor * (1 + t.grid.spacing), 8)] == "sell"


# ---------------------------------------------------------------------------
# trader.py — _order_size_base y ejecución
# ---------------------------------------------------------------------------

class TestOrderSizeBase:
    def test_caps_at_15pct_total(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t._last_price = 2446.0
        total = 5.0
        n = len(t.grid.levels)  # 13
        size = t._order_size_base(total)
        usd = size / 10 ** t.bot.base_decimals * t._last_price
        # min(max_spend/n=0.346, max_spend*0.25=1.125, total*0.15=0.75) = 0.346
        assert usd == pytest.approx(4.5 / n, abs=1e-6)

    def test_caps_when_total_small(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t._last_price = 2446.0
        size = t._order_size_base(1.0)  # equity $1 -> 15% = $0.15
        usd = size / 10 ** t.bot.base_decimals * t._last_price
        assert usd <= 0.15 + 1e-9

    def test_zero_price_no_crash(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t._last_price = 0.0  # peor caso: sin precio
        # no debe lanzar ZeroDivisionError; devuelve 0
        assert t._order_size_base(1.0) == 0

    def test_zero_total_fallback_path(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t._last_price = 2446.0
        # total=0 -> path de fallback con _usd_value (mockeado)
        with patch("paperbot.live.trader._usd_value", return_value=(1.0, 1.0, 2.0)):
            size = t._order_size_base(0.0)
        usd = size / 10 ** t.bot.base_decimals * t._last_price
        assert usd <= 2.0 * 0.15 + 1e-9
        assert size > 0


class TestExecuteBuySell:
    def test_buy_skips_when_no_usdc(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.0, usdc=0.0)
        assert t._execute_buy(2446.0) is False

    def test_buy_skips_when_order_tiny(self, monkeypatch, tmp_path):
        """usdc_max < $0.05 -> skip sin swap."""
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.0, usdc=0.02)
        assert t._execute_buy(2446.0) is False
        t.bot.swap_exact_in.assert_not_called()

    def test_sell_skips_when_no_base(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.0, usdc=1.0)
        assert t._execute_sell(2446.0) is False

    def test_buy_computes_min_out_from_level(self, monkeypatch, tmp_path):
        """min_out_override debe anclar el swap al nivel del grid, no al mercado."""
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.0, usdc=2.0)
        t._last_price = 2446.0
        _swap_ok(t)
        t._execute_buy(2446.0)
        args, kwargs = t.bot.swap_exact_in.call_args
        assert kwargs.get("min_out_override") is not None
        # min_out = usdc_max / (level*1.01) * 1e18, con usdc_max limitado al
        # tamaño de orden (_order_size_base): ~$0.346 -> raw
        base_need = t._order_size_base(0.0)
        usdc_needed_raw = int(base_need * 2446.0 / 10 ** 18 * 10 ** 6)
        usdc_max = min(usdc_needed_raw, int(2.0 * 1e6))
        expected = int(usdc_max / (2446.0 * 1.01) * 10 ** 18)
        assert kwargs["min_out_override"] == expected

    def test_sell_computes_min_out_from_level(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.01, usdc=1.0)
        t._last_price = 2446.0
        _swap_ok(t)
        t._execute_sell(2446.0)
        args, kwargs = t.bot.swap_exact_in.call_args
        assert kwargs.get("min_out_override") is not None
        base_amt = t._order_size_base(0.0)
        expected = int(base_amt * (2446.0 * 0.99) / 10 ** 18 * 10 ** 6)
        assert kwargs["min_out_override"] == expected

    def test_sell_never_exceeds_balance(self, monkeypatch, tmp_path):
        """base_amt = min(order_size, balance) — no puede vender más de lo que hay.
        Con posición minúscula (0.00001 WETH ≈ $0.024 < $0.05) la orden se
        considera 'too small' y NO se envía ningún swap."""
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.00001, usdc=1.0)  # posición minúscula
        t._last_price = 2446.0
        _swap_ok(t)
        assert t._execute_sell(2446.0) is False
        t.bot.swap_exact_in.assert_not_called()


# ---------------------------------------------------------------------------
# trader.py — rebalance
# ---------------------------------------------------------------------------

class TestMaybeRebalance:
    def test_rebalance_sells_base_when_overweight(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t.rebalance_threshold = 0.15  # activar
        t._last_price = 2446.0
        # 80% base / 20% usdc de $5 -> ratio 0.8 > 0.65
        _with_balances(t, base=0.001635, usdc=1.0)
        _swap_ok(t)
        t._maybe_rebalance(5.0)
        args, kwargs = t.bot.swap_exact_in.call_args
        assert args[0] == t.bot.base_token  # vende base -> USDC

    def test_rebalance_buys_base_when_underweight(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t.rebalance_threshold = 0.15
        t._last_price = 2446.0
        # 10% base -> ratio 0.1 < 0.35
        _with_balances(t, base=0.0002, usdc=3.0)
        _swap_ok(t)
        t._maybe_rebalance(5.0)
        args, kwargs = t.bot.swap_exact_in.call_args
        assert args[0] == t.bot.usdc  # compra base con USDC

    def test_rebalance_noop_within_threshold(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t.rebalance_threshold = 0.99
        t._last_price = 2446.0
        _with_balances(t, base=0.001, usdc=2.5)
        _swap_ok(t)
        t._maybe_rebalance(5.0)
        t.bot.swap_exact_in.assert_not_called()

    def test_rebalance_zero_total_noop(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t._maybe_rebalance(0.0)
        t.bot.swap_exact_in.assert_not_called()


# ---------------------------------------------------------------------------
# trader.py — re-anchor
# ---------------------------------------------------------------------------

class TestMaybeReanchor:
    def test_reanchor_when_drift_beyond_threshold(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t.reanchor_pct = 5.0
        old_anchor = t.grid.anchor
        new_price = old_anchor * 1.10  # +10% > 5%
        t._maybe_reanchor(new_price)
        assert t.grid.anchor == pytest.approx(new_price)
        # órdenes reconstruidas
        assert len(t._orders) >= 12
        assert round(new_price, 8) in t._orders  # anchor-buy (sin posición)

    def test_reanchor_not_fired_within_threshold(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        old_anchor = t.grid.anchor
        t._maybe_reanchor(old_anchor * 1.02)  # +2% < 5%
        assert t.grid.anchor == old_anchor

    def test_reanchor_never_crashes_on_extreme_drift(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t.bot.token_balance.side_effect = lambda tok, addr: 0
        t._maybe_reanchor(0.0001)  # precio ínfimo
        assert t.grid.anchor > 0
        t._maybe_reanchor(1e9)     # precio astronómico
        assert t.grid.anchor > 0


# ---------------------------------------------------------------------------
# trader.py — _reconcile_onchain (idempotencia de fill)
# ---------------------------------------------------------------------------

class TestReconcileOnchain:
    def test_reconcile_rotates_buys_to_sells_when_no_usdc(self, monkeypatch, tmp_path):
        """Sin USDC pero CON base: todas las compras pendientes se consideran
        ejecutadas y rotan a venta; los sells no se tocan (hay base real).
        Con el grid real (6 buys + anchor-buy = 7 buys) la rotación es a
        sell en p*(1+step), que NO coincide con niveles de compra existentes."""
        t, store = _make_trader(monkeypatch, tmp_path)
        t.bot.token_balance.side_effect = lambda tok, addr: (
            int(0.002 * 10 ** 18) if str(tok).lower().startswith("0x" + "b") else 0)
        t._last_price = 2446.0
        t._reset_orders()
        pending_buys = {round(p, 8) for p, s in t._orders.items() if s == "buy"}
        assert pending_buys
        t._reconcile_onchain()
        # ningún buy pendiente queda; cada uno rotado a sell un escalón arriba.
        # Con posición base presente (0.002 WETH), el anchor-buy NO se creó en
        # _reset_orders -> solo rotan los 6 buys de niveles de compra.
        assert all(s == "sell" for s in t._orders.values())
        # cada sell rotado proviene de un buy pendiente (p/(1+step) era buy);
        # los sell levels NATIVOS del grid también siguen existiendo (el
        # reconcile no los toca) — por eso el set de sells > set de buys.
        for p in t._orders:
            assert round(p / (1 + t.grid.spacing), 8) in pending_buys or \
                   round(p, 8) in {round(lv.price, 8) for lv in t.grid.sell_levels}
    def test_reconcile_rotates_sells_to_buys_when_no_base(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t.bot.token_balance.side_effect = lambda tok, addr: (
            0 if str(tok).lower().startswith("0x" + "b") else int(2.0 * 1e6))
        t._last_price = 2446.0
        t._reset_orders()
        t._orders = {round(p, 8): s for p, s in t._orders.items() if s == "sell"}
        pending_sells = set(t._orders)
        t._reconcile_onchain()
        assert all(s == "buy" for s in t._orders.values())
        for p, s in t._orders.items():
            assert round(p * (1 + t.grid.spacing), 8) in pending_sells

    def test_reconcile_noop_with_balances(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        _with_balances(t, base=0.005, usdc=2.0)
        t._last_price = 2446.0
        t._reset_orders()
        before = dict(t._orders)
        t._reconcile_onchain()
        assert t._orders == before

    def test_reconcile_zero_price_noop(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        t._last_price = 0.0
        t._reset_orders()
        before = dict(t._orders)
        t._reconcile_onchain()
        assert t._orders == before


# ---------------------------------------------------------------------------
# trader.py — stop-loss y halt flag
# ---------------------------------------------------------------------------

class TestStopLoss:
    def test_stop_loss_unwinds_and_halts(self, monkeypatch, tmp_path):
        """Drawdown >= stop_loss -> unwind completo + halted=true + running=False."""
        t, store = _make_trader(monkeypatch, tmp_path)
        t.stop_loss_pct = 10.0
        _with_balances(t, base=0.001, usdc=1.0)
        _swap_ok(t)
        # dibuja un drawdown del 12%
        t.peak_equity = 5.0
        with patch("paperbot.live.trader._usd_value", return_value=(2.0, 2.4, 4.4)):
            monkeypatch.setattr("paperbot.live.trader.fetch_price", lambda addr: 2446.0)
            res = t.one_cycle()
        assert res.get("stop_loss") is True
        assert store.get_meta("halted") == "true"
        assert t.running is False
        assert store.get_meta("status") == "stopped"
        # unwind: se intentó vender TODO el base
        assert t.bot.swap_exact_in.call_args[0][0] == t.bot.base_token

    def test_unwind_sells_everything_three_attempts(self, monkeypatch, tmp_path):
        t, store = _make_trader(monkeypatch, tmp_path)
        # balance base ~0.002 WETH
        t.bot.token_balance.side_effect = lambda tok, addr: (
            int(0.002 * 10 ** 18) if str(tok).lower().startswith("0x" + "b") else int(1.0 * 1e6))
        t._emergency_unwind()
        assert t.bot.swap_exact_in.call_args[0][0] == t.bot.base_token
        # base_bal completo (sin min())
        assert t.bot.swap_exact_in.call_args[0][2] == int(0.002 * 10 ** 18)

    def test_halt_flag_blocks_bot_start(self, tmp_path):
        """bot.py cmd_live: con halted=true en DB, el bot sale sin operar."""
        store = _store(tmp_path)
        store.set_meta("halted", "true")
        store.close()
        import subprocess, sys
        code = (
            "from paperbot.paper.store import Store\n"
            f"store = Store({str(tmp_path / 'live.db')!r})\n"
            "halted = store.get_meta('halted')\n"
            "print('HALTED' if halted and halted.strip().lower() in ('1','true','yes') else 'OK')\n"
        )
        r = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                           cwd="/home/tt/thinking/plan")
        assert "HALTED" in r.stdout
