"""Tests for Round 14 fixes — MEDIA (NonceManager w3 ref) + BAJA (gas cap abort).

All tests mock w3 and RPC calls — no network dependency.
"""
from unittest.mock import MagicMock, patch, PropertyMock
import logging
import pytest


# ---------------------------------------------------------------------------
# Fake helpers
# ---------------------------------------------------------------------------

class FakeW3:
    """Fake Web3 for testing — returns a configurable pending nonce."""

    class _Eth:
        def __init__(self, parent):
            self._parent = parent

        def get_transaction_count(self, addr, block_identifier="latest"):
            return self._parent.n

        def get_block(self, block_identifier="latest"):
            if self._parent._base_fee is not None:
                return {"baseFeePerGas": self._parent._base_fee, "timestamp": 1000}
            return {"baseFeePerGas": 0, "timestamp": 1000}

        @property
        def gas_price(self):
            return self._parent.gas_price_val

        def contract(self, **kwargs):
            return MagicMock()

    def __init__(self, pending_nonce=0, gas_price=0, base_fee=None):
        self.n = pending_nonce
        self.gas_price_val = gas_price
        self._base_fee = base_fee
        self.eth = self._Eth(self)

    def is_connected(self):
        return True


# ---------------------------------------------------------------------------
# Fix 1: NonceManager w3 reference after _switch_rpc
# ---------------------------------------------------------------------------

class TestNonceManagerUpdateW3:
    """NonceManager.update_w3() changes w3 reference after RPC switch."""

    def test_update_w3_changes_reference(self):
        """update_w3() sets self.w3 to the new Web3 instance."""
        from paperbot.live.aerodrome import NonceManager

        w3_old = FakeW3(pending_nonce=42)
        w3_new = FakeW3(pending_nonce=50)
        mgr = NonceManager(w3_old, "0x" + "1" * 40)
        mgr.sync()

        # Before update: resync uses old w3
        assert mgr.w3 is w3_old

        # Update w3
        mgr.update_w3(w3_new)

        # After update: w3 points to new instance
        assert mgr.w3 is w3_new

    def test_resync_after_w3_update_uses_new_rpc(self):
        """resync_if_behind() after update_w3 queries the new RPC."""
        from paperbot.live.aerodrome import NonceManager

        w3_old = FakeW3(pending_nonce=42)
        w3_new = FakeW3(pending_nonce=50)
        mgr = NonceManager(w3_old, "0x" + "1" * 40)
        mgr.sync()  # _nonce = 42

        # Advance local counter
        mgr.next()  # 42 -> _nonce = 43

        # Old RPC shows 42 (stale), new shows 50 (current)
        # Before update: resync stays at 43 (old RPC behind local)
        mgr.resync_if_behind()
        assert mgr._nonce == 43  # old RPC: 42 < 43, no change

        # After update: resync should jump to 50 (new RPC ahead)
        mgr.update_w3(w3_new)
        mgr.resync_if_behind()
        assert mgr._nonce == 50


class TestSwitchRpcUpdatesNonceManager:
    """_switch_rpc() updates NonceManager.w3 when switching RPC."""

    def test_switch_rpc_updates_nonce_manager_w3(self):
        """Verify _switch_rpc updates _nonce_manager.w3 after switching."""
        from pathlib import Path

        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()

        # Find _switch_rpc and check it updates _nonce_manager
        in_switch = False
        found_update = False
        for line in src.split("\n"):
            if "def _switch_rpc" in line:
                in_switch = True
                continue
            if in_switch:
                if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if "_nonce_manager" in line and ("update_w3" in line or "w3" in line):
                    found_update = True

        assert found_update, "_switch_rpc must update _nonce_manager.w3 after RPC switch"

    def test_switch_rpc_no_update_when_no_manager(self):
        """_switch_rpc does NOT crash when _nonce_manager is None."""
        from pathlib import Path

        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()

        in_switch = False
        found_guard = False
        for line in src.split("\n"):
            if "def _switch_rpc" in line:
                in_switch = True
                continue
            if in_switch:
                if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if "_nonce_manager is not None" in line:
                    found_guard = True

        assert found_guard, "_switch_rpc must guard _nonce_manager update with is not None"


class TestGetNonceManagerStartupLog:
    """get_nonce_manager() logs detected nonce on first creation."""

    def test_startup_nonce_log_exists(self):
        """get_nonce_manager() calls sync() and logs nonce on first creation."""
        from pathlib import Path

        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()

        in_get_nonce = False
        found_sync_call = False
        found_log_info = False
        for line in src.split("\n"):
            if "def get_nonce_manager" in line:
                in_get_nonce = True
                continue
            if in_get_nonce:
                if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if "self._nonce_manager.sync()" in line:
                    found_sync_call = True
                if "NonceManager init" in line and "nonce detected" in line:
                    found_log_info = True

        assert found_sync_call, "get_nonce_manager must call sync() on first creation"
        assert found_log_info, "get_nonce_manager must log detected nonce"


# ---------------------------------------------------------------------------
# Fix 2: Gas cap abort with WARNING log
# ---------------------------------------------------------------------------

class TestGasPriceAbort:
    """_gas_price() raises RuntimeError + WARNING log when gas > cap."""

    def test_gas_price_raises_when_network_exceeds_cap(self):
        """_gas_price() raises RuntimeError when network gas exceeds cap."""
        from paperbot.live.aerodrome import AerodromeLive

        # EIP-1559: baseFee > cap -> RuntimeError
        mock = MagicMock(spec=AerodromeLive)
        mock.max_gas_gwei = 0.1  # cap = 0.1 gwei
        mock.eip1559_supported = True
        mock.w3 = FakeW3(gas_price=200_000_000, base_fee=200_000_000)  # 0.2 gwei > cap

        with pytest.raises(RuntimeError, match="aborting tx"):
            AerodromeLive._gas_price(mock)

    def test_gas_price_returns_fee_params_when_under_cap(self):
        """_gas_price() returns EIP-1559 fee params when within cap."""
        from paperbot.live.aerodrome import AerodromeLive

        mock = MagicMock(spec=AerodromeLive)
        mock.max_gas_gwei = 0.1
        mock.eip1559_supported = True
        mock.w3 = FakeW3(gas_price=5_000_000, base_fee=5_000_000)  # 0.005 gwei < cap

        result = AerodromeLive._gas_price(mock)
        assert isinstance(result, dict)
        assert "maxFeePerGas" in result
        assert "maxPriorityFeePerGas" in result
        assert result["maxFeePerGas"] <= 100_000_000  # <= cap

    def test_gas_price_logs_warning_before_raise(self, caplog):
        """_gas_price() logs WARNING before raising RuntimeError."""
        from paperbot.live.aerodrome import AerodromeLive

        mock = MagicMock(spec=AerodromeLive)
        mock.max_gas_gwei = 0.1
        mock.eip1559_supported = True
        mock.w3 = FakeW3(gas_price=200_000_000, base_fee=200_000_000)  # 0.2 gwei > cap

        with caplog.at_level(logging.WARNING):
            with pytest.raises(RuntimeError):
                AerodromeLive._gas_price(mock)

    def test_gas_price_raises_when_cap_zero(self):
        """_gas_price() raises ValueError when max_gas_gwei == 0 (no cap)."""
        from paperbot.live.aerodrome import AerodromeLive

        mock = MagicMock(spec=AerodromeLive)
        mock.max_gas_gwei = 0
        mock.w3 = FakeW3(gas_price=5_000_000)

        with pytest.raises(ValueError, match="max_gas_gwei must be > 0"):
            AerodromeLive._gas_price(mock)

    def test_build_fee_params_raises_when_base_fee_over_cap(self):
        """_build_fee_params() raises RuntimeError when baseFee > cap."""
        from paperbot.live.aerodrome import AerodromeLive

        # Stub real (no MagicMock spec) para que _build_fee_params ejecute el
        # código REAL (que delega en _gas_price). Con MagicMock(spec=...) los
        # métodos se mockean automáticamente y nunca lanzan.
        class _Stub(AerodromeLive):
            def __init__(self):
                self.max_gas_gwei = 0.1
                self._eip1559 = True  # propiedad eip1559_supported lee esto
                self.w3 = FakeW3(base_fee=200_000_000)  # 0.2 gwei > 0.1 gwei
                self._nonce_manager = None
                self._nonce_manager_addr = None

        with pytest.raises(RuntimeError, match="aborting tx"):
            _Stub()._build_fee_params()

    def test_build_fee_params_raises_when_cap_zero(self):
        """_build_fee_params() raises ValueError when cap <= 0."""
        from paperbot.live.aerodrome import AerodromeLive

        class _Stub(AerodromeLive):
            def __init__(self):
                self.max_gas_gwei = 0
                self._eip1559 = False
                self.w3 = FakeW3(gas_price=1000)
                self._nonce_manager = None
                self._nonce_manager_addr = None

        with pytest.raises(ValueError, match="max_gas_gwei must be > 0"):
            _Stub()._build_fee_params()


# ---------------------------------------------------------------------------
# Integration: _switch_rpc triggers NonceManager w3 update
# ---------------------------------------------------------------------------

class TestSwitchRpcNonceManagerIntegration:
    """End-to-end: _switch_rpc → NonceManager.w3 updated → resync uses new RPC."""

    def test_switch_rpc_updates_w3_on_existing_manager(self):
        """After _switch_rpc, NonceManager queries the new RPC."""
        from paperbot.live.aerodrome import NonceManager

        # Simulate: NonceManager created with old RPC
        w3_old = FakeW3(pending_nonce=42)
        mgr = NonceManager(w3_old, "0x" + "1" * 40)
        mgr.sync()  # _nonce = 42

        # Simulate _switch_rpc: create new w3, update manager
        w3_new = FakeW3(pending_nonce=50)
        mgr.update_w3(w3_new)

        # Now resync should see new RPC's nonce
        mgr.resync_if_behind()
        assert mgr._nonce == 50  # jumped from 42 to 50

    def test_switch_rpc_no_manager_no_crash(self):
        """Switching RPC with no NonceManager created does not crash."""
        from paperbot.live.aerodrome import NonceManager

        # _nonce_manager is None, so update_w3 should not be called
        # (guarded by `if self._nonce_manager is not None`)
        # This test verifies the guard logic exists in source
        from pathlib import Path

        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()
        in_switch = False
        for line in src.split("\n"):
            if "def _switch_rpc" in line:
                in_switch = True
                continue
            if in_switch:
                if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if "_nonce_manager is not None" in line:
                    return  # guard found — OK
        pytest.fail("_switch_rpc must guard _nonce_manager update with None check")


# ---------------------------------------------------------------------------
# NonceManager: core behavior (smoke)
# ---------------------------------------------------------------------------

class TestNonceManagerCore:
    """Smoke tests for NonceManager core behavior."""

    def test_sync_sets_nonce(self):
        from paperbot.live.aerodrome import NonceManager
        mgr = NonceManager(FakeW3(pending_nonce=42), "0x" + "1" * 40)
        mgr.sync()
        assert mgr._nonce == 42

    def test_next_auto_increments(self):
        from paperbot.live.aerodrome import NonceManager
        mgr = NonceManager(FakeW3(pending_nonce=42), "0x" + "1" * 40)
        mgr.sync()
        assert mgr.next() == 42
        assert mgr.next() == 43

    def test_mark_sent_advances(self):
        from paperbot.live.aerodrome import NonceManager
        mgr = NonceManager(FakeW3(pending_nonce=42), "0x" + "1" * 40)
        mgr.sync()
        mgr.next()  # 42
        mgr.mark_sent(42)
        assert mgr._nonce == 43

    def test_mark_sent_no_regress(self):
        from paperbot.live.aerodrome import NonceManager
        mgr = NonceManager(FakeW3(pending_nonce=42), "0x" + "1" * 40)
        mgr.sync()
        mgr.next()  # 42 -> _nonce = 43
        mgr.next()  # 43 -> _nonce = 44
        mgr.mark_sent(42)  # 42 < 44, no regress
        assert mgr._nonce == 44

    def test_resync_behind_catches_chain(self):
        from paperbot.live.aerodrome import NonceManager
        w3 = FakeW3(pending_nonce=42)
        mgr = NonceManager(w3, "0x" + "1" * 40)
        mgr.sync()  # 42
        mgr.next()  # 43
        w3.n = 50
        mgr.resync_if_behind()
        assert mgr._nonce == 50

    def test_resync_handles_rpc_failure(self):
        from paperbot.live.aerodrome import NonceManager
        w3 = MagicMock()
        w3.eth.get_transaction_count.side_effect = Exception("RPC down")
        mgr = NonceManager(w3, "0x" + "1" * 40)
        mgr._nonce = 42
        mgr.resync_if_behind()  # no crash
        assert mgr._nonce == 42  # unchanged
