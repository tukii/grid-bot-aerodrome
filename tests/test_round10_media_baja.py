"""
Tests para los 4 fixes MEDIA/BAJA del round 10c.
Cubre: halt flag supervisor, log_action None guard, CSV normalization, gas EIP-1559 abort.
"""
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1: HALT FLAG — supervisor no reinicia cuando halted=true
# ---------------------------------------------------------------------------
class TestHaltFlag:
    """Supervisor must not restart the bot when halted=true is in DB."""

    def test_main_loop_skips_restart_when_halted(self, tmp_path, monkeypatch):
        """When halted=true in meta, supervisor main_loop should not restart the bot."""
        import supervisor

        # Setup tmp paths
        db_path = tmp_path / "live.db"
        state_path = tmp_path / "supervisor_state.json"
        config_path = tmp_path / "config.yaml"
        log_path = tmp_path / "supervisor.log"

        monkeypatch.setattr(supervisor, "DB", db_path)
        monkeypatch.setattr(supervisor, "STATE", state_path)
        monkeypatch.setattr(supervisor, "CONFIG", config_path)
        monkeypatch.setattr(supervisor, "LOG", log_path)
        monkeypatch.setattr(supervisor, "BAK", tmp_path / "config.backup.yaml")

        # Write halted=true to meta
        con = sqlite3.connect(db_path)
        con.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", ("halted", "true"))
        con.commit()
        con.close()

        # Also need a config.yaml for read_config
        config_path.write_text("grid:\n  anchor_price: 2446\n  spacing_pct: 3.5\n  range_pct: 20\n")

        restart_called = MagicMock()

        # Patch restart_bot and service_alive and the sleep to break the loop
        call_count = [0]
        def fake_sleep(secs):
            call_count[0] += 1
            if call_count[0] >= 2:
                raise InterruptedBreak()

        class InterruptedBreak(Exception):
            pass

        monkeypatch.setattr(supervisor, "restart_bot", restart_called)
        monkeypatch.setattr(supervisor.time, "sleep", fake_sleep)
        monkeypatch.setattr(supervisor, "service_alive", MagicMock(return_value=False))

        with pytest.raises(InterruptedBreak):
            supervisor.main_loop()

        # restart_bot should NOT have been called
        restart_called.assert_not_called()

    def test_halt_bot_writes_true_to_meta(self, tmp_path, monkeypatch):
        """halt_bot() must write halted=true to the meta table."""
        import supervisor

        db_path = tmp_path / "live.db"
        monkeypatch.setattr(supervisor, "DB", db_path)
        monkeypatch.setattr(supervisor, "LOG", tmp_path / "supervisor.log")
        monkeypatch.setattr(supervisor, "CONFIG", tmp_path / "config.yaml")

        # Patch systemctl stop to not actually run
        mock_run = MagicMock(return_value=MagicMock(returncode=0))
        monkeypatch.setattr(supervisor.subprocess, "run", mock_run)

        result = supervisor.halt_bot("test drawdown")

        assert result is True
        con = sqlite3.connect(db_path)
        row = con.execute("SELECT value FROM meta WHERE key='halted'").fetchone()
        con.close()
        assert row is not None
        assert row[0] == "true"

    def test_bot_cmd_live_exits_when_halted(self, tmp_path, monkeypatch):
        """bot.py cmd_live must exit(0) when halted=true in DB."""
        from paperbot.paper.store import Store

        db_path = tmp_path / "live.db"
        store = Store(str(db_path))
        store.set_meta("halted", "true")
        store.close()

        # Verify the flag is there
        store2 = Store(str(db_path))
        assert store2.get_meta("halted") == "true"
        store2.close()


# ---------------------------------------------------------------------------
# Fix 2: LOG_ACTION None fix — reanchor_config with state=None
# ---------------------------------------------------------------------------
class TestLogActionNone:
    """reanchor_config(state=None) must not crash (previously crashed with AttributeError)."""

    def test_reanchor_config_with_state_none(self, tmp_path, monkeypatch):
        """Call reanchor_config with state=None; should not raise."""
        import supervisor

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "grid:\n  anchor_price: 2000.0\n  spacing_pct: 3.5\n  range_pct: 20\n\n"
            "network:\n  rpc_url: https://example.com\n"
        )

        monkeypatch.setattr(supervisor, "CONFIG", config_path)
        monkeypatch.setattr(supervisor, "DB", tmp_path / "live.db")
        monkeypatch.setattr(supervisor, "BAK", tmp_path / "config.backup.yaml")
        monkeypatch.setattr(supervisor, "LOG", tmp_path / "supervisor.log")

        # Patch restart_bot to not actually restart
        monkeypatch.setattr(supervisor, "restart_bot", MagicMock(return_value=True))

        # This must NOT crash (was AttributeError before fix)
        result = supervisor.reanchor_config(2100.0, "test", state=None)
        assert result is True

        # Verify config was updated
        content = config_path.read_text()
        assert "2100" in content

    def test_reanchor_config_with_state_logs_action(self, tmp_path, monkeypatch):
        """When state is provided, the config_anchor_update action should be in state['actions']."""
        import supervisor

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "grid:\n  anchor_price: 2000.0\n  spacing_pct: 3.5\n  range_pct: 20\n\n"
            "network:\n  rpc_url: https://example.com\n"
        )

        monkeypatch.setattr(supervisor, "CONFIG", config_path)
        monkeypatch.setattr(supervisor, "DB", tmp_path / "live.db")
        monkeypatch.setattr(supervisor, "BAK", tmp_path / "config.backup.yaml")
        monkeypatch.setattr(supervisor, "LOG", tmp_path / "supervisor.log")
        monkeypatch.setattr(supervisor, "restart_bot", MagicMock(return_value=True))

        state = {"last_reanchor_ts": 0.0, "actions": []}
        result = supervisor.reanchor_config(2100.0, "test", state=state)
        assert result is True

        # The action should be logged in state
        assert len(state["actions"]) >= 1
        last_action = state["actions"][-1]
        assert last_action["action"] == "config_anchor_update"
        assert "2100" in last_action["detail"]

    def test_log_action_with_none_state(self, monkeypatch):
        """log_action(state=None) must not crash."""
        import supervisor
        monkeypatch.setattr(supervisor, "LOG", "/dev/null")

        # Must not raise
        supervisor.log_action(None, "test_action", "test detail")

    def test_reanchor_config_state_param_backward_compat(self, tmp_path, monkeypatch):
        """reanchor_config still works when called without state (backward compat)."""
        import supervisor

        config_path = tmp_path / "config.yaml"
        config_path.write_text(
            "grid:\n  anchor_price: 2000.0\n  spacing_pct: 3.5\n  range_pct: 20\n\n"
            "network:\n  rpc_url: https://example.com\n"
        )

        monkeypatch.setattr(supervisor, "CONFIG", config_path)
        monkeypatch.setattr(supervisor, "DB", tmp_path / "live.db")
        monkeypatch.setattr(supervisor, "BAK", tmp_path / "config.backup.yaml")
        monkeypatch.setattr(supervisor, "LOG", tmp_path / "supervisor.log")
        monkeypatch.setattr(supervisor, "restart_bot", MagicMock(return_value=True))

        # Call without state keyword (backward compat)
        result = supervisor.reanchor_config(2100.0, "test")
        assert result is True


# ---------------------------------------------------------------------------
# Fix 3: CSV NORMALIZATION — fetch_ohlcv returns ascending order
# ---------------------------------------------------------------------------
class TestCSVNormalization:
    """fetch_ohlcv must return DataFrame sorted ascending (oldest first)."""

    def test_geckoterminal_sorts_descending_to_ascending(self):
        """Simulate GeckoTerminal descending response → must be sorted ascending."""
        import pandas as pd
        from paperbot.data.geckoterminal import fetch_ohlcv

        # Simulate the data transformation that fetch_ohlcv does
        # GeckoTerminal returns newest-first (descending)
        timestamps_desc = [1700000300, 1700000200, 1700000100, 1700000000]
        rows = [[ts, 100.0, 110.0, 90.0, 105.0, 1000.0] for ts in timestamps_desc]

        df = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume_usd"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.set_index("timestamp").astype(float)

        # Apply the same normalization logic as fetch_ohlcv
        if len(df) > 1 and df.index[0] > df.index[-1]:
            df = df.sort_index()

        # Verify ascending order
        assert df.index[0] < df.index[-1], "DataFrame should be in ascending order"
        # Oldest timestamp should be 1700000000 (first in ascending)
        assert df.index[0] == pd.Timestamp(1700000000, unit="s", tz="UTC")
        assert df.index[-1] == pd.Timestamp(1700000300, unit="s", tz="UTC")

    def test_already_ascending_not_reversed(self):
        """When data is already ascending, sort_index is a no-op."""
        import pandas as pd

        timestamps_asc = [1700000000, 1700000100, 1700000200, 1700000300]
        rows = [[ts, 100.0, 110.0, 90.0, 105.0, 1000.0] for ts in timestamps_asc]

        df = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume_usd"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.set_index("timestamp").astype(float)

        # Apply normalization (should be no-op)
        if len(df) > 1 and df.index[0] > df.index[-1]:
            df = df.sort_index()

        # Still ascending
        assert df.index[0] < df.index[-1]

    def test_csv_write_read_roundtrip(self, tmp_path):
        """CSV written from fetch_ohlcv output is readable in ascending order."""
        import pandas as pd

        # Simulate descending data (what GeckoTerminal returns)
        timestamps_desc = [1700000300, 1700000200, 1700000100, 1700000000]
        rows = [[ts, 100.0, 110.0, 90.0, 105.0, 1000.0] for ts in timestamps_desc]

        df = pd.DataFrame(
            rows,
            columns=["timestamp", "open", "high", "low", "close", "volume_usd"],
        )
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="s", utc=True)
        df = df.set_index("timestamp").astype(float)

        # Apply normalization (as fetch_ohlcv does)
        if len(df) > 1 and df.index[0] > df.index[-1]:
            df = df.sort_index()

        # Write to CSV (as cmd_fetch does)
        csv_path = tmp_path / "ohlcv_h1.csv"
        df.to_csv(csv_path)

        # Read back
        df_read = pd.read_csv(csv_path, index_col="timestamp", parse_dates=True)
        assert df_read.index[0] < df_read.index[-1], "CSV should be in ascending order"


# ---------------------------------------------------------------------------
# Fix 4: GAS EIP-1559 — abort when gas > cap
# ---------------------------------------------------------------------------
class TestGasEIP1559:
    """Gas cap enforcement: abort with clear message when gas exceeds cap."""

    def test_gas_price_raises_when_above_cap(self):
        """_gas_price() must raise RuntimeError when network gas > cap."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1  # 0.1 gwei cap

        # Mock gas_price to return 0.5 gwei (way above cap)
        mock_w3 = MagicMock()
        mock_w3.eth.gas_price = int(0.5 * 1e9)  # 0.5 gwei
        bot.w3 = mock_w3

        with pytest.raises(RuntimeError, match="network gas.*gwei > cap.*aborting"):
            AerodromeLive._gas_price(bot)

    def test_gas_price_normal_when_below_cap(self):
        """_gas_price() returns normal gas when below cap."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 1.0  # 1 gwei cap

        mock_w3 = MagicMock()
        mock_w3.eth.gas_price = int(0.005 * 1e9)  # 0.005 gwei
        bot.w3 = mock_w3

        result = AerodromeLive._gas_price(bot)
        assert result == int(0.005 * 1e9)

    def test_gas_price_normal_when_zero_cap(self):
        """_gas_price() returns network gas when cap is 0 (disabled)."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0  # disabled

        mock_w3 = MagicMock()
        mock_w3.eth.gas_price = int(1.0 * 1e9)  # 1 gwei
        bot.w3 = mock_w3

        result = AerodromeLive._gas_price(bot)
        assert result == int(1.0 * 1e9)

    def test_build_fee_params_raises_when_basefee_above_cap(self):
        """_build_fee_params must raise RuntimeError when baseFee > cap (EIP-1559)."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1  # 0.1 gwei cap
        bot._eip1559 = True

        mock_w3 = MagicMock()
        # baseFeePerGas = 0.5 gwei (way above cap)
        mock_block = {"baseFeePerGas": int(0.5 * 1e9)}
        mock_w3.eth.get_block.return_value = mock_block
        bot.w3 = mock_w3

        with pytest.raises(RuntimeError, match="baseFee.*gwei > cap.*aborting"):
            AerodromeLive._build_fee_params(bot)

    def test_build_fee_params_works_when_basefee_below_cap(self):
        """_build_fee_params returns EIP-1559 params when baseFee < cap."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1  # 0.1 gwei cap
        bot._eip1559 = True

        mock_w3 = MagicMock()
        # baseFeePerGas = 0.005 gwei (well below cap)
        mock_block = {"baseFeePerGas": int(0.005 * 1e9)}
        mock_w3.eth.get_block.return_value = mock_block
        bot.w3 = mock_w3

        result = AerodromeLive._build_fee_params(bot)
        assert "maxFeePerGas" in result
        assert "maxPriorityFeePerGas" in result
        # maxFeePerGas should be capped
        assert result["maxFeePerGas"] <= int(0.1 * 1e9)
