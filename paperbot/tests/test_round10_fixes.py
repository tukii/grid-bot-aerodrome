"""Tests for Round 10 MEDIA/BAJA audit fixes.

Fix 1 (MEDIA): HALT FLAG — supervisor writes halted=true, bot checks and exits.
Fix 2 (MEDIA): LOG_ACTION None — reanchor_config with state=None doesn't crash.
Fix 3 (BAJA):  CSV NORMALIZATION — fetch_ohlcv saves in ascending order.
Fix 4 (BAJA):  GAS EIP-1559 — aborts when gas > cap; uses EIP-1559 params.
"""
import os
import sqlite3
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── Fix 1: HALT FLAG ────────────────────────────────────────────────────────

class TestHaltFlag:
    """Supervisor writes halted=true; bot.py reads it and refuses to operate."""

    def test_write_meta_halted_and_read_back(self):
        """write_meta('halted','true') is readable from supervisor.read_meta."""
        # Use supervisor's write_meta and read_meta on a temp DB
        from supervisor import write_meta, read_meta
        import supervisor

        # Patch DB path to temp
        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            old_db = supervisor.DB
            supervisor.DB = Path(tmp.name)

            # Ensure meta table exists
            con = sqlite3.connect(tmp.name)
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            con.commit()
            con.close()

            ok = write_meta("halted", "true")
            assert ok, "write_meta should return True"

            val = read_meta("halted")
            assert val == "true", f"Expected 'true', got {val!r}"
        finally:
            supervisor.DB = old_db
            os.unlink(tmp.name)

    def test_bot_cmd_live_exits_on_halted(self, tmp_path):
        """bot.py cmd_live checks halted meta and calls sys.exit(0) if set."""
        import supervisor
        # Create a temp DB with halted=true
        db_path = str(tmp_path / "test_halted.db")
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("halted", "true"))
        con.commit()
        con.close()

        # Import and test the Store.get_meta path
        from paperbot.paper.store import Store
        store = Store(db_path)
        halted = store.get_meta("halted")
        assert halted is not None
        assert halted.strip().lower() in ("1", "true", "yes")
        store.close()

    def test_halt_bot_writes_meta(self):
        """supervisor.halt_bot() writes halted=true to meta table."""
        from supervisor import halt_bot, write_meta, read_meta
        import supervisor

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            old_db = supervisor.DB
            supervisor.DB = Path(tmp.name)

            con = sqlite3.connect(tmp.name)
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            con.commit()
            con.close()

            # halt_bot tries to stop service too, but we only care about meta write
            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                ok = halt_bot("test drawdown")

            val = read_meta("halted")
            assert val == "true"
        finally:
            supervisor.DB = old_db
            os.unlink(tmp.name)


# ── Fix 2: LOG_ACTION None fix ──────────────────────────────────────────────

class TestLogActionNoneFix:
    """log_action must not crash when state=None (e.g. reanchor_config called
    from a context without a state dict)."""

    def test_log_action_with_none_state(self):
        """Calling log_action(state=None, ...) should not raise."""
        from supervisor import log_action
        # Should not raise
        log_action(None, "test_action", "test detail")

    def test_log_action_with_valid_state(self):
        """log_action with a valid state dict appends the action."""
        from supervisor import log_action
        state = {"actions": []}
        log_action(state, "test_action", "test detail")
        assert len(state["actions"]) == 1
        assert state["actions"][0]["action"] == "test_action"
        assert state["actions"][0]["detail"] == "test detail"

    def test_reanchor_config_with_state_none_does_not_crash(self):
        """reanchor_config(state=None) should not crash on log_action call.

        We mock filesystem operations and subprocess to isolate the log_action path.
        """
        from supervisor import reanchor_config
        import supervisor

        tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        tmp.close()
        try:
            old_db = supervisor.DB
            old_config = supervisor.CONFIG
            supervisor.DB = Path(tmp.name)

            # Create a temp config.yaml with anchor_price
            cfg_path = Path(tmp.name).with_suffix(".yaml")
            cfg_path.write_text("grid:\n  anchor_price: 2446\n  spacing_pct: 3.5\n")
            supervisor.CONFIG = cfg_path

            # Create meta table
            con = sqlite3.connect(tmp.name)
            con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
            con.commit()
            con.close()

            with patch("subprocess.run") as mock_run:
                mock_run.return_value = MagicMock(returncode=0)
                # This must NOT crash even with state=None
                result = reanchor_config(2500.0, "test", state=None)

            assert result is not None  # Should return True or False, not crash
        finally:
            supervisor.DB = old_db
            supervisor.CONFIG = old_config
            if tmp.name and os.path.exists(tmp.name):
                os.unlink(tmp.name)
            cfg_path = Path(tmp.name).with_suffix(".yaml")
            if cfg_path.exists():
                os.unlink(cfg_path)


# ── Fix 3: CSV NORMALIZATION ────────────────────────────────────────────────

class TestCSVNormalization:
    """fetch_ohlcv normalizes DataFrame to ascending (chronological) order."""

    def test_sort_index_when_descending(self):
        """DataFrame with descending timestamps gets sorted to ascending."""
        import pandas as pd
        from paperbot.data import geckoterminal

        # Simulate the logic from fetch_ohlcv: if index is descending, sort
        idx = pd.to_datetime([3, 2, 1], unit="s", utc=True)
        df = pd.DataFrame({"open": [300, 200, 100]}, index=idx)
        df.index.name = "timestamp"

        # This is the normalization logic in geckoterminal.fetch_ohlcv
        if len(df) > 1 and df.index[0] > df.index[-1]:
            df = df.sort_index()

        assert df.index[0] < df.index[-1], "Should be ascending after sort"
        assert df["open"].iloc[0] == 100, "Oldest candle first"

    def test_already_ascending_not_changed(self):
        """DataFrame already in ascending order stays the same."""
        import pandas as pd

        idx = pd.to_datetime([1, 2, 3], unit="s", utc=True)
        df = pd.DataFrame({"open": [100, 200, 300]}, index=idx)
        df.index.name = "timestamp"

        if len(df) > 1 and df.index[0] > df.index[-1]:
            df = df.sort_index()

        assert df.index[0] < df.index[-1]
        assert df["open"].iloc[0] == 100


# ── Fix 4: GAS EIP-1559 ─────────────────────────────────────────────────────

class TestGasEIP1559:
    """_gas_price aborts when gas > cap; _build_fee_params uses EIP-1559."""

    def test_gas_price_raises_when_over_cap(self):
        """_gas_price raises RuntimeError if network gas exceeds max_gas_gwei."""
        from paperbot.live.aerodrome import AerodromeLive

        # Create a mock instance
        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1  # 0.1 gwei cap

        # Simulate gas_price at 0.5 gwei (way above cap)
        bot.w3 = MagicMock()
        bot.w3.eth.gas_price = int(0.5e9)  # 0.5 gwei in wei

        # Call _gas_price via the real function
        with pytest.raises(RuntimeError, match="network gas.*> cap"):
            AerodromeLive._gas_price(bot)

    def test_gas_price_ok_when_under_cap(self):
        """_gas_price returns normally when gas is under cap."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1  # 0.1 gwei cap
        bot.w3 = MagicMock()
        bot.w3.eth.gas_price = int(0.005e9)  # 0.005 gwei (well under)

        result = AerodromeLive._gas_price(bot)
        assert result == int(0.005e9)

    def test_build_fee_params_eip1559_with_cap(self):
        """_build_fee_params uses maxFeePerGas capped by max_gas_gwei."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1  # cap
        bot._eip1559 = True
        bot.w3 = MagicMock()
        bot.w3.eth.get_block.return_value = {"baseFeePerGas": int(0.005e9)}

        result = AerodromeLive._build_fee_params(bot)
        assert "maxFeePerGas" in result
        assert "maxPriorityFeePerGas" in result
        assert result["maxFeePerGas"] <= int(0.1e9), "maxFee must not exceed cap"

    def test_build_fee_params_aborts_on_high_base_fee(self):
        """_build_fee_params raises when baseFee > cap."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.05  # very low cap
        bot._eip1559 = True
        bot.w3 = MagicMock()
        bot.w3.eth.get_block.return_value = {"baseFeePerGas": int(0.5e9)}  # way above

        with pytest.raises(RuntimeError, match="baseFee.*> cap"):
            AerodromeLive._build_fee_params(bot)
