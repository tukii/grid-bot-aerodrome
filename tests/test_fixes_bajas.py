"""Tests de los fixes BAJOS de la auditoría (2026-08-29).

Cubre:
1. fetch_ohlcv normaliza a orden cronológico (geckoterminal.py).
2. one_cycle descarta órdenes fantasma fuera del grid vigente (trader.py).
3. _reset_orders: anchor-buy SOLO si no hay posición base.
4. Gas EIP-1559: _build_fee_params aplica cap y cae a legacy si la red no
   soporta maxFeePerGas.

Todos los tests son deterministas (sin red, sin web3 real).
"""
import json

import pandas as pd
import pytest


# ---------------------------------------------------------------------------
# 1. CSV / fetch_ohlcv: orden cronológico normalizado en la fuente
# ---------------------------------------------------------------------------
def test_fetch_ohlcv_normalizes_descending_order(monkeypatch):
    """GeckoTerminal devuelve velas nuevas->viejas; fetch_ohlcv debe devolver
    orden cronológico (viejo->nuevo)."""
    from paperbot.data import geckoterminal

    rows = [
        [1_700_000_000 + 3600, 102.0, 103.0, 101.0, 102.0, 1000.0],  # t+1h (nueva)
        [1_700_000_000, 100.0, 101.0, 99.0, 100.0, 900.0],          # t (vieja)
    ]
    fake = lambda url, params=None, retries=3: {  # noqa: E731
        "data": {"attributes": {"ohlcv_list": rows}}
    }
    monkeypatch.setattr(geckoterminal, "_get", fake)
    df = geckoterminal.fetch_ohlcv("0xpool", "h1", limit=2)
    assert isinstance(df.index, pd.DatetimeIndex)
    # cronológico: el primer timestamp debe ser el más viejo
    assert df.index[0] < df.index[-1]
    assert df.iloc[0]["close"] == pytest.approx(100.0)
    assert df.iloc[-1]["close"] == pytest.approx(102.0)


def test_fetch_ohlcv_keeps_already_ascending(monkeypatch):
    """Si la fuente ya viene en orden ascendente, el fix es inocuo."""
    from paperbot.data import geckoterminal

    rows = [
        [1_700_000_000, 100.0, 101.0, 99.0, 100.0, 900.0],
        [1_700_000_000 + 3600, 102.0, 103.0, 101.0, 102.0, 1000.0],
    ]
    fake = lambda url, params=None, retries=3: {  # noqa: E731
        "data": {"attributes": {"ohlcv_list": rows}}
    }
    monkeypatch.setattr(geckoterminal, "_get", fake)
    df = geckoterminal.fetch_ohlcv("0xpool", "h1", limit=2)
    assert df.index[0] < df.index[-1]
    assert df.iloc[0]["close"] == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# 2. one_cycle: descarta órdenes fantasma fuera del grid vigente
# ---------------------------------------------------------------------------
class FakeStore:
    def __init__(self):
        self.meta = {}

    def get_meta(self, k):
        return self.meta.get(k)

    def set_meta(self, k, v):
        self.meta[k] = v

    def record_trade(self, **kw):
        pass


def _make_trader(monkeypatch, orders, buy_levels, sell_levels, anchor, price):
    """Construye un LiveGridTrader sin tocar red: solo grid + _orders."""
    from paperbot.live import trader as trader_mod
    from paperbot.live.trader import LiveGridTrader
    from paperbot.strategies.grid import Grid

    class FakeBot:
        base_token = "0xbase"
        usdc = "0xusdc"
        base_decimals = 18
        quote_decimals = 6

        def token_balance(self, token, addr):
            return 0

        def eth_balance(self, addr):
            return 0

        def w3(self):
            return None

    t = LiveGridTrader.__new__(LiveGridTrader)
    t.cfg = {"pool": {"address": "0xpool"}}
    t.store = FakeStore()
    t.bot = FakeBot()
    t.account = None
    t.grid = Grid(anchor=anchor, spacing_pct=6.0, range_pct=25.0)
    t._orders = dict(orders)
    t._last_price = price
    t.peak_equity = 0.0
    t.running = True
    t.dry_run = True
    t.stop_loss_pct = 50.0
    t.rebalance_threshold = 0.15
    t.max_spend_usd = 5.0
    t.max_position_pct = 0.8
    t.rotation_enabled = False
    t._last_scan = 0.0
    # stub: no queremos red/rotación
    monkeypatch.setattr(trader_mod, "_usd_value",
                        lambda bot, acc: (0.0, 5.0, 5.0))
    monkeypatch.setattr(trader_mod, "fetch_price", lambda pool: price)
    monkeypatch.setattr(trader_mod, "send_alert", lambda msg: True)
    return t


def test_one_cycle_discards_phantom_orders(monkeypatch):
    """Órdenes a precios fuera del grid vigente deben descartarse sin ejecutar.

    Simula un grid re-anclado: el estado persistido trae una orden compra a un
    precio que ya no es nivel del grid (fantasma). one_cycle debe eliminarla y
    NO ejecutar ninguna compra en ese nivel.
    """
    from paperbot.live import trader as trader_mod
    from paperbot.live.trader import LiveGridTrader

    anchor = 100.0
    grid = __import__("paperbot.strategies.grid", fromlist=["Grid"]).Grid(
        anchor=anchor, spacing_pct=6.0, range_pct=25.0)
    valid = {round(lv.price, 8) for lv in grid.buy_levels} | \
            {round(lv.price, 8) for lv in grid.sell_levels} | \
            {round(anchor, 8)}

    # Orden fantasma: precio 95.5 que NO es nivel del grid (spacing 6% -> niveles
    # ~94, ~88, ...; 95.5 no es uno de ellos) + una orden de compra válida
    # profundamente ITM (también debería ejecutarse -> la marcamos con stub que
    # no hace nada, y comprobamos que se conserva el flujo).
    phantom = 95.5
    assert round(phantom, 8) not in valid

    executed = []
    t = _make_trader(
        monkeypatch,
        orders={phantom: "buy", 94.0: "buy"},
        buy_levels=grid.buy_levels, sell_levels=grid.sell_levels,
        anchor=anchor, price=99.0,
    )
    # _execute_buy stub: registra nivel, devuelve False (no llena) para que la
    # orden válida se conserve y podamos ver la poda de la fantasma.
    monkeypatch.setattr(LiveGridTrader, "_execute_buy",
                        lambda self, p: (executed.append(p), False)[1])
    monkeypatch.setattr(LiveGridTrader, "_maybe_reanchor", lambda self, p: None)
    monkeypatch.setattr(LiveGridTrader, "_maybe_rebalance", lambda self, t: None)
    monkeypatch.setattr(LiveGridTrader, "_maybe_rotate", lambda self: None)
    monkeypatch.setattr(LiveGridTrader, "_save_state", lambda self: None)

    res = t.one_cycle()
    assert res.get("ok") is True
    # la orden fantasma desapareció
    assert phantom not in t._orders
    # la orden válida sigue (no llenó)
    assert 94.0 in t._orders


def test_one_cycle_phantom_sell_also_discarded(monkeypatch):
    """Las órdenes fantasma de venta también se podan."""
    from paperbot.live.trader import LiveGridTrader
    from paperbot.strategies.grid import Grid

    anchor = 100.0
    grid = Grid(anchor=anchor, spacing_pct=6.0, range_pct=25.0)
    valid = {round(lv.price, 8) for lv in grid.buy_levels} | \
            {round(lv.price, 8) for lv in grid.sell_levels} | \
            {round(anchor, 8)}
    phantom = 103.3  # no es nivel de venta del grid (106, 112.36, ...)
    assert round(phantom, 8) not in valid

    t = _make_trader(
        monkeypatch,
        orders={phantom: "sell"},
        buy_levels=grid.buy_levels, sell_levels=grid.sell_levels,
        anchor=anchor, price=99.0,  # precio por debajo: sell no llenaría de todos modos
    )
    monkeypatch.setattr(LiveGridTrader, "_execute_sell",
                        lambda self, p: (_ for _ in ()).throw(AssertionError("no debe ejecutarse")))
    monkeypatch.setattr(LiveGridTrader, "_maybe_reanchor", lambda self, p: None)
    monkeypatch.setattr(LiveGridTrader, "_maybe_rebalance", lambda self, t: None)
    monkeypatch.setattr(LiveGridTrader, "_maybe_rotate", lambda self: None)
    monkeypatch.setattr(LiveGridTrader, "_save_state", lambda self: None)

    t.one_cycle()
    assert phantom not in t._orders


# ---------------------------------------------------------------------------
# 3. _reset_orders: anchor-buy SOLO si no hay posición base
# ---------------------------------------------------------------------------
class FakeBotBal:
    def __init__(self, base_bal_raw):
        self.base_bal_raw = base_bal_raw
        self.base_token = "0xbase"
        self.usdc = "0xusdc"
        self.base_decimals = 18
        self.quote_decimals = 6

    def token_balance(self, token, addr):
        return self.base_bal_raw

    def eth_balance(self, addr):
        return 0


def test_anchor_buy_only_when_no_position(monkeypatch):
    """Con posición base on-chain (>= $0.05), _reset_orders NO añade anchor-buy."""
    from paperbot.live.trader import LiveGridTrader
    from paperbot.strategies.grid import Grid

    t = LiveGridTrader.__new__(LiveGridTrader)
    t.grid = Grid(anchor=100.0, spacing_pct=6.0, range_pct=25.0)
    # 0.01 base * 100 $ = $1.00 >= $0.05 -> hay posición
    t.bot = FakeBotBal(base_bal_raw=int(0.01 * 10 ** 18))
    t.account = type("A", (), {"address": "0xacct"})()
    t._last_price = 100.0
    t._orders = {}
    t.cfg = {"pool": {"address": "0xpool"}}
    t.grid_cfg = {"spacing_pct": 6.0}
    t.peak_equity = 0.0
    t.store = FakeStore()

    t._reset_orders()
    assert round(100.0, 8) not in t._orders, "anchor-buy NO debe existir con posición"
    assert all(s == "buy" or s == "sell" for s in t._orders.values())


def test_anchor_buy_when_no_position(monkeypatch):
    """Sin posición base on-chain, _reset_orders SÍ añade anchor-buy."""
    from paperbot.live.trader import LiveGridTrader
    from paperbot.strategies.grid import Grid

    t = LiveGridTrader.__new__(LiveGridTrader)
    t.grid = Grid(anchor=100.0, spacing_pct=6.0, range_pct=25.0)
    t.bot = FakeBotBal(base_bal_raw=0)  # sin base
    t.account = type("A", (), {"address": "0xacct"})()
    t._last_price = 100.0
    t._orders = {}
    t.cfg = {"pool": {"address": "0xpool"}}
    t.grid_cfg = {"spacing_pct": 6.0}
    t.peak_equity = 0.0
    t.store = FakeStore()

    t._reset_orders()
    assert round(100.0, 8) in t._orders
    assert t._orders[round(100.0, 8)] == "buy"


def test_anchor_buy_on_balance_read_error(monkeypatch):
    """Si el balance no se puede leer, el comportamiento por defecto es añadir
    el anchor-buy (conservador: no dejar de comprar por un error transitorio)."""
    from paperbot.live.trader import LiveGridTrader
    from paperbot.strategies.grid import Grid

    class FakeBotErr:
        base_token = "0xbase"
        usdc = "0xusdc"
        base_decimals = 18
        quote_decimals = 6

        def token_balance(self, token, addr):
            raise RuntimeError("rpc down")

        def eth_balance(self, addr):
            return 0

    t = LiveGridTrader.__new__(LiveGridTrader)
    t.grid = Grid(anchor=100.0, spacing_pct=6.0, range_pct=25.0)
    t.bot = FakeBotErr()
    t.account = type("A", (), {"address": "0xacct"})()
    t._last_price = 100.0
    t._orders = {}
    t.cfg = {"pool": {"address": "0xpool"}}
    t.grid_cfg = {"spacing_pct": 6.0}
    t.peak_equity = 0.0
    t.store = FakeStore()

    t._reset_orders()
    assert round(100.0, 8) in t._orders


# ---------------------------------------------------------------------------
# 4. Gas EIP-1559: cap aplicado y fallback legacy
# ---------------------------------------------------------------------------
class FakeW3EIP:
    """web3 fake con EIP-1559 (baseFeePerGas en el bloque)."""

    def __init__(self, base_fee=5_000_000, gas_price=6_000_000):
        self._base_fee = base_fee
        self._gas_price = gas_price
        self.eth = self

    def get_block(self, tag):
        return {"baseFeePerGas": self._base_fee}

    @property
    def gas_price(self):
        return self._gas_price


class FakeW3Legacy:
    """web3 fake SIN EIP-1559 (bloque sin baseFeePerGas)."""

    def __init__(self, gas_price=6_000_000):
        self._gas_price = gas_price
        self.eth = self

    def get_block(self, tag):
        return {}

    @property
    def gas_price(self):
        return self._gas_price


def _make_bot(w3, max_gas_gwei):
    from paperbot.live.aerodrome import AerodromeLive
    b = AerodromeLive.__new__(AerodromeLive)
    b.w3 = w3
    b.max_gas_gwei = max_gas_gwei
    b._eip1559 = None
    return b


def test_build_fee_params_eip1559_caps_max_fee():
    """Con red EIP-1559, maxFeePerGas es el precio real de la red (base+tip),
    NUNCA mayor que el cap configurado."""
    b = _make_bot(FakeW3EIP(), max_gas_gwei=0.1)
    params = b._build_fee_params()
    cap = int(0.1 * 1e9)
    assert "maxFeePerGas" in params
    assert params["maxFeePerGas"] == 6_000_000  # base 0.005 + tip 0.001 gwei
    assert params["maxPriorityFeePerGas"] == 1_000_000
    assert params["maxFeePerGas"] <= cap
    assert "gasPrice" not in params


def test_build_fee_params_legacy_fallback_when_no_base_fee():
    """Red sin EIP-1559 -> gasPrice legacy (sin maxFeePerGas)."""
    b = _make_bot(FakeW3Legacy(), max_gas_gwei=0.1)
    params = b._build_fee_params()
    assert "gasPrice" in params
    assert "maxFeePerGas" not in params
    # gasPrice legacy también capeado
    assert params["gasPrice"] == 6_000_000  # por debajo del cap -> precio de red


def test_build_fee_params_raises_when_base_fee_exceeds_cap():
    """Si baseFeePerGas > cap, aborta con RuntimeError (fix BAJA round 10c)."""
    import pytest
    b = _make_bot(FakeW3EIP(base_fee=500_000_000), max_gas_gwei=0.1)
    with pytest.raises(RuntimeError, match="baseFee.*gwei > cap"):
        b._build_fee_params()


def test_eip1559_supported_detection():
    b = _make_bot(FakeW3EIP(), max_gas_gwei=0.1)
    assert b.eip1559_supported is True
    b2 = _make_bot(FakeW3Legacy(), max_gas_gwei=0.1)
    assert b2.eip1559_supported is False


def test_gas_price_raises_when_above_cap():
    """_gas_price (legacy path) aborta cuando gas > cap (fix BAJA round 10c)."""
    import pytest
    b = _make_bot(FakeW3Legacy(gas_price=500_000_000), max_gas_gwei=0.1)
    with pytest.raises(RuntimeError, match="network gas.*gwei > cap"):
        b._gas_price()
    b2 = _make_bot(FakeW3Legacy(gas_price=1_000_000), max_gas_gwei=0.1)
    assert b2._gas_price() == 1_000_000  # por debajo del cap -> precio de red
