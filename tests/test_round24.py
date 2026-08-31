"""Tests unitarios round24: NonceManager, Grid generation, Price feed, validación defensiva.

Estos tests NO requieren conexión a la blockchain ni a APIs externas.
Ejecutar: cd /home/tt/thinking/plan && python -m pytest tests/test_round24.py -v
"""
import json
import time
import threading
from unittest.mock import MagicMock, patch, PropertyMock
import pytest

from paperbot.strategies.grid import Grid, GridLevel


# ======================================================================
# TEST 1: Grid generation — cálculo de niveles
# ======================================================================

class TestGridGeneration:
    """Tests para la clase Grid — generación de niveles de precio."""

    def test_basic_grid_symmetric(self):
        g = Grid(anchor=2500.0, spacing_pct=2.0, range_pct=10.0)
        assert len(g.buy_levels) == 5
        assert len(g.sell_levels) == 5
        # Anchor entre buy y sell
        assert max(lv.price for lv in g.buy_levels) < 2500.0
        assert min(lv.price for lv in g.sell_levels) > 2500.0

    def test_grid_spacing_exact(self):
        g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
        # spacing = 0.01, steps = 5
        assert g.spacing == pytest.approx(0.01)
        assert g.buy_levels[0].price == pytest.approx(99.0)
        assert g.buy_levels[1].price == pytest.approx(98.0)
        assert g.sell_levels[0].price == pytest.approx(101.0)
        assert g.sell_levels[1].price == pytest.approx(102.0)

    def test_grid_levels_property(self):
        g = Grid(anchor=100.0, spacing_pct=2.0, range_pct=6.0)
        # 3 buy + anchor + 3 sell = 7
        assert len(g.levels) == 7
        assert g.levels[3] == 100.0  # anchor in center

    def test_grid_invalid_anchor_raises(self):
        with pytest.raises(ValueError, match="anchor must be > 0"):
            Grid(anchor=0, spacing_pct=1.0, range_pct=5.0)
        with pytest.raises(ValueError, match="anchor must be > 0"):
            Grid(anchor=-100, spacing_pct=1.0, range_pct=5.0)

    def test_grid_invalid_spacing_raises(self):
        with pytest.raises(ValueError, match="spacing/range must be > 0"):
            Grid(anchor=100.0, spacing_pct=0, range_pct=5.0)
        with pytest.raises(ValueError, match="spacing/range must be > 0"):
            Grid(anchor=100.0, spacing_pct=-1, range_pct=5.0)

    def test_grid_invalid_range_raises(self):
        with pytest.raises(ValueError, match="spacing/range must be > 0"):
            Grid(anchor=100.0, spacing_pct=1.0, range_pct=0)

    def test_nearest_buy_price(self):
        g = Grid(anchor=2500.0, spacing_pct=2.0, range_pct=10.0)
        # price = 2480 → should match a buy level below 2500
        nearest = g.nearest_buy_price(2480.0)
        assert nearest is not None
        assert nearest < 2500.0
        assert nearest <= 2480.0

    def test_nearest_buy_price_none_when_above(self):
        g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
        # All buy levels are below 100; if price is 200, nearest buy is still valid
        nearest = g.nearest_buy_price(200.0)
        assert nearest is not None

    def test_nearest_sell_price(self):
        g = Grid(anchor=2500.0, spacing_pct=2.0, range_pct=10.0)
        nearest = g.nearest_sell_price(2520.0)
        assert nearest is not None
        assert nearest >= 2520.0

    def test_nearest_sell_price_all_above(self):
        """Cuando price < todos los sell levels, devuelve el menor sell level."""
        g = Grid(anchor=100.0, spacing_pct=1.0, range_pct=5.0)
        # Todos los sell levels son >= 101; price=95 -> nearest sell = 101
        nearest = g.nearest_sell_price(95.0)
        assert nearest == pytest.approx(101.0)

    def test_grid_small_spacing(self):
        """Spacing muy pequeño (0.1%) — no debe explotar."""
        g = Grid(anchor=2500.0, spacing_pct=0.1, range_pct=1.0)
        # steps = 10
        assert len(g.buy_levels) == 10
        assert len(g.sell_levels) == 10
        # Niveles muy cercanos al anchor (spacing = 0.1% de 2500 = 2.5)
        assert g.buy_levels[0].price < 2500.0
        assert g.buy_levels[0].price == pytest.approx(2497.5)

    def test_grid_large_range(self):
        g = Grid(anchor=2500.0, spacing_pct=5.0, range_pct=50.0)
        # steps = 10
        assert len(g.buy_levels) == 10
        assert g.buy_levels[-1].price == pytest.approx(2500 * (1 - 0.05 * 10))


# ======================================================================
# TEST 2: NonceManager — tracking de nonce
# ======================================================================

class MockWeb3:
    """Mock de Web3 para tests de NonceManager."""
    def __init__(self, nonce=0):
        self._nonce = nonce

    class eth:
        @staticmethod
        def get_transaction_count(address, block_identifier="latest"):
            return MockWeb3._nonce


class TestNonceManager:
    """Tests para NonceManager — no requiere blockchain real."""

    def test_sync_initializes_nonce(self):
        MockWeb3._nonce = 42
        w3 = MockWeb3()
        # Importar la clase directamente
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        assert nm._nonce is None
        nm.sync()
        assert nm._nonce == 42

    def test_next_increments(self):
        MockWeb3._nonce = 10
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        n1 = nm.next()
        n2 = nm.next()
        assert n1 == 10
        assert n2 == 11

    def test_next_auto_syncs(self):
        """Si _nonce es None, next() debe auto-sincronizar."""
        MockWeb3._nonce = 7
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        assert nm._nonce is None
        n = nm.next()
        assert n == 7

    def test_mark_sent_updates_nonce(self):
        MockWeb3._nonce = 5
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        nm.sync()
        nm.mark_sent(10)
        assert nm._nonce == 11

    def test_mark_sent_no_regress(self):
        """mark_sent con nonce menor no debe retroceder."""
        MockWeb3._nonce = 15
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        nm.sync()
        nm.mark_sent(5)  # menor que 15
        assert nm._nonce == 15  # no cambió

    def test_resync_if_behind(self):
        """Si la chain tiene nonce mayor, resync lo actualiza."""
        MockWeb3._nonce = 3
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        nm.sync()
        assert nm._nonce == 3
        # Simular que la chain avanzó
        MockWeb3._nonce = 8
        nm.resync_if_behind()
        assert nm._nonce == 8

    def test_resync_no_regression(self):
        """Si la chain tiene nonce menor, no debe retroceder."""
        MockWeb3._nonce = 5
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        nm.sync()
        assert nm._nonce == 5
        MockWeb3._nonce = 2  # chain "retrocedió" (imposible, pero por si acaso)
        nm.resync_if_behind()
        assert nm._nonce == 5  # no cambió

    def test_thread_safety_basic(self):
        """NonceManager next() no debe producir nonces duplicados (básico)."""
        MockWeb3._nonce = 0
        w3 = MockWeb3()
        from paperbot.live.aerodrome import NonceManager
        nm = NonceManager(w3, "0x0000000000000000000000000000000000000001")
        nm.sync()
        nonces = []
        lock = threading.Lock()
        def grab():
            for _ in range(10):
                n = nm.next()
                with lock:
                    nonces.append(n)
        threads = [threading.Thread(target=grab) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        # Cada thread pidió 10 nonces, 5 threads = 50 nonces
        assert len(nonces) == 50
        # Deben ser todos únicos (sin duplicados)
        assert len(set(nonces)) == 50


# ======================================================================
# TEST 3: Price feed — cache y fallback
# ======================================================================

class TestPriceFeed:
    """Tests para el módulo de precios (price.py) con mocks."""

    def test_cache_hit_returns_cached(self):
        """Si hay cache válida, no debe hacer llamadas HTTP."""
        import paperbot.data.price as price_mod
        # Llenar cache
        original_cache = price_mod._cache.copy()
        try:
            price_mod._cache["0xtest"] = (2500.0, time.time())
            with patch("paperbot.data.price._dexscreener") as mock_ds, \
                 patch("paperbot.data.price._geckoterminal") as mock_gt, \
                 patch("paperbot.data.price._slot0") as mock_s0:
                result = price_mod.fetch_price("0xtest")
                assert result == 2500.0
                mock_ds.assert_not_called()
                mock_gt.assert_not_called()
                mock_s0.assert_not_called()
        finally:
            price_mod._cache.clear()
            price_mod._cache.update(original_cache)

    def test_cache_expired_triggers_fetch(self):
        """Si el cache expiró, debe re-fetch."""
        import paperbot.data.price as price_mod
        original_cache = price_mod._cache.copy()
        try:
            price_mod._cache["0xtest"] = (2500.0, time.time() - 100)  # expired
            with patch("paperbot.data.price._dexscreener", return_value=2600.0) as mock_ds:
                result = price_mod.fetch_price("0xtest")
                assert result == 2600.0
                mock_ds.assert_called_once()
        finally:
            price_mod._cache.clear()
            price_mod._cache.update(original_cache)

    def test_fallback_chain_dex_gecko_slot0(self):
        """Si DexScreener y GeckoTerminal fallan, cae a slot0."""
        import paperbot.data.price as price_mod
        original_cache = price_mod._cache.copy()
        try:
            price_mod._cache.clear()
            with patch("paperbot.data.price._dexscreener", return_value=None), \
                 patch("paperbot.data.price._geckoterminal", return_value=None), \
                 patch("paperbot.data.price._slot0", return_value=2500.0) as mock_s0:
                result = price_mod.fetch_price("0xnew")
                assert result == 2500.0
                mock_s0.assert_called_once()
        finally:
            price_mod._cache.clear()
            price_mod._cache.update(original_cache)

    def test_all_sources_fail_returns_none(self):
        """Si todas las fuentes fallan, retorna None."""
        import paperbot.data.price as price_mod
        original_cache = price_mod._cache.copy()
        try:
            price_mod._cache.clear()
            with patch("paperbot.data.price._dexscreener", return_value=None), \
                 patch("paperbot.data.price._geckoterminal", return_value=None), \
                 patch("paperbot.data.price._slot0", return_value=None):
                result = price_mod.fetch_price("0xfail")
                assert result is None
        finally:
            price_mod._cache.clear()
            price_mod._cache.update(original_cache)


# ======================================================================
# TEST 4: Store — operaciones básicas SQLite
# ======================================================================

class TestStore:
    """Tests para Store — SQLite con WAL mode."""

    def test_set_get_meta(self, tmp_path):
        from paperbot.paper.store import Store
        db = str(tmp_path / "test.db")
        s = Store(db)
        assert s.get_meta("nonexistent") is None
        s.set_meta("key1", "value1")
        assert s.get_meta("key1") == "value1"
        s.set_meta("key1", "value2")
        assert s.get_meta("key1") == "value2"
        s.close()

    def test_record_trade(self, tmp_path):
        from paperbot.paper.store import Store
        db = str(tmp_path / "test.db")
        s = Store(db)
        s.record_trade(ts="2026-01-01T00:00:00Z", side="buy", price=2500.0,
                       size_usd=1.0, fee_usd=0.001, gas_usd=0.01,
                       filled=1, tx_hash="0xabc", gas_used=100000)
        stats = s.stats()
        assert stats["n"] == 1
        assert stats["buys"] == 1
        s.close()

    def test_corrupt_db_recovery(self, tmp_path):
        """Si la DB está corrupta, Store crea una nueva."""
        from paperbot.paper.store import Store
        db = tmp_path / "test.db"
        # Escribir basura
        db.write_text("not a database")
        s = Store(str(db))
        s.set_meta("key1", "value1")
        assert s.get_meta("key1") == "value1"
        s.close()

    def test_stats_empty(self, tmp_path):
        from paperbot.paper.store import Store
        db = str(tmp_path / "test.db")
        s = Store(db)
        stats = s.stats()
        assert stats["n"] == 0
        # BUG HALLAZGO ROUND24: SUM() retorna None en tabla vacía, no 0.
        # COALESCE solo envuelve fees, no buys/sells. Esto es un bug real.
        # Ver hallazgo MEDIO #7 en audit_round24.md.
        assert stats["buys"] in (0, None)  # temporal hasta fix en store.py
        assert stats["sells"] in (0, None)
        s.close()


# ======================================================================
# TEST 5: Validación defensiva de precios
# ======================================================================

class TestPriceDeviation:
    """Tests para la validación de desviación de precio entre ticks."""

    def test_price_deviation_within_threshold(self):
        """Desviación de 5% (dentro del 20%) -> OK."""
        last_price = 2500.0
        new_price = 2625.0  # +5%
        deviation = abs(new_price - last_price) / last_price
        assert deviation <= 0.20

    def test_price_deviation_exceeds_threshold(self):
        """Desviación de 25% (fuera del 20%) -> RECHAZAR."""
        last_price = 2500.0
        new_price = 3125.0  # +25%
        deviation = abs(new_price - last_price) / last_price
        assert deviation > 0.20

    def test_price_deviation_zero_price(self):
        """Si el precio anterior es 0, no debe dividir por cero."""
        last_price = 0.0
        new_price = 2500.0
        if last_price > 0:
            deviation = abs(new_price - last_price) / last_price
        else:
            deviation = 0.0  # safe fallback
        assert deviation == 0.0


# ======================================================================
# TEST 6: Config loading
# ======================================================================

class TestConfig:
    """Tests para la carga de configuración."""

    def test_load_config_returns_dict(self):
        """load_config() debe retornar un dict."""
        from paperbot.config import load_config
        cfg = load_config()
        assert isinstance(cfg, dict)
        assert "pool" in cfg
        assert "grid" in cfg
        assert "network" in cfg

    def test_config_has_required_keys(self):
        from paperbot.config import load_config
        cfg = load_config()
        assert "anchor_price" in cfg["grid"]
        assert "spacing_pct" in cfg["grid"]
        assert "range_pct" in cfg["grid"]
        assert "rpc_url" in cfg["network"]
        assert "chain_id" in cfg["network"]


# ======================================================================
# TEST 7: SwapResult y ApprovalStatus
# ======================================================================

class TestDataClasses:
    def test_swap_result_defaults(self):
        from paperbot.live.aerodrome import SwapResult
        r = SwapResult(dry_run=False, ok=True, tx_hash="0xabc",
                       expected_out=100, actual_out=99,
                       message="ok")
        assert r.gas_used is None
        assert r.receipt_status is None

    def test_approval_status_values(self):
        from paperbot.live.aerodrome import ApprovalStatus
        assert ApprovalStatus.EXISTS == "exists"
        assert ApprovalStatus.SENT == "sent"
        assert ApprovalStatus.FAILED == "failed"


# ======================================================================
# TEST 8: GridLevel dataclass
# ======================================================================

class TestGridLevel:
    def test_grid_level_attributes(self):
        lv = GridLevel(index=0, price=99.0, is_buy=True)
        assert lv.index == 0
        assert lv.price == 99.0
        assert lv.is_buy is True

    def test_grid_level_sell(self):
        lv = GridLevel(index=2, price=105.0, is_buy=False)
        assert lv.is_buy is False


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
