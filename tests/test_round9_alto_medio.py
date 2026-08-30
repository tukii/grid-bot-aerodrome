"""
Tests para los 4 fixes ALTO/MEDIO del robustness review round 8c (round 9b).
Cubre: nonce waste approve(0), USD_PER_BASE stale, resync_if_behind logging, YAML parser.
"""
import sqlite3
from pathlib import Path
from unittest.mock import patch, MagicMock, PropertyMock

import pytest


# ---------------------------------------------------------------------------
# Fix 1: Nonce waste — approve(MAX) attempted first, approve(0) only as fallback
# ---------------------------------------------------------------------------
class TestNonceWasteApprove:
    """ensure_approval debe intentar approve(MAX) primero; approve(0) solo como fallback."""

    def _make_bot(self, tmp_path):
        """Create a minimal AerodromeLive-like mock for testing ensure_approval logic."""
        from paperbot.live.aerodrome import AerodromeLive, ERC20_ABI, ApprovalStatus

        bot = MagicMock(spec=AerodromeLive)
        bot.router = "0x" + "a" * 40
        bot.chain_id = 8453
        bot.max_gas_gwei = 0.1

        # Mock contract
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = 0
        bot.w3 = MagicMock()
        bot.w3.eth.contract.return_value = mock_contract

        # Mock nonce manager
        nonce_mgr = MagicMock()
        nonce_mgr.next.return_value = 0
        nonce_mgr.resync_if_behind = MagicMock()
        bot.get_nonce_manager.return_value = nonce_mgr

        # Mock _build_fee_params
        bot._build_fee_params = MagicMock(return_value={"gasPrice": 1000})
        bot._estimate_gas = MagicMock(return_value=100000)

        return bot, mock_contract, nonce_mgr

    def test_approve_max_attempted_first_when_current_zero(self, tmp_path):
        """When current allowance = 0, approve(MAX) should be tried first (no approve(0))."""
        from paperbot.live.aerodrome import AerodromeLive

        bot, mock_contract, nonce_mgr = self._make_bot(tmp_path)
        account = MagicMock()
        account.address = "0x" + "b" * 40

        # approve(MAX) succeeds on first try
        mock_tx = MagicMock()
        mock_contract.functions.approve.return_value.build_transaction.return_value = {
            "from": account.address,
            "nonce": 0,
            "gas": 60000,
        }
        bot._send = MagicMock(return_value=(True, 1, "0xhash"))

        result = AerodromeLive.ensure_approval(bot, "0x" + "c" * 40, 1000, account, nonce_mgr)

        # approve(0) should NOT have been called (current == 0, so the fallback block is skipped)
        assert mock_contract.functions.approve.call_count == 1
        first_call_args = mock_contract.functions.approve.call_args_list[0]
        assert first_call_args[0][1] == (1 << 256) - 1  # MAX_UINT

    def test_approve_max_first_then_fallback(self, tmp_path):
        """When approve(MAX) fails, fallback to approve(0) + approve(MAX)."""
        from paperbot.live.aerodrome import AerodromeLive

        bot, mock_contract, nonce_mgr = self._make_bot(tmp_path)
        account = MagicMock()
        account.address = "0x" + "b" * 40
        # Set current allowance > 0 to trigger the fallback path
        mock_contract.functions.allowance.return_value.call.return_value = 100

        call_count = [0]
        def mock_build_tx(params):
            call_count[0] += 1
            if call_count[0] == 1:
                raise Exception("token require approve(0) first")
            return {"from": account.address, "nonce": call_count[0], "gas": 60000}

        mock_contract.functions.approve.return_value.build_transaction.side_effect = mock_build_tx
        bot._send = MagicMock(return_value=(True, 1, "0xhash"))

        result = AerodromeLive.ensure_approval(bot, "0x" + "c" * 40, 1000, account, nonce_mgr)

        # approve called 3 times: first MAX (failed), then 0, then MAX again
        assert mock_contract.functions.approve.call_count == 3

    def test_approve_max_success_skips_zero_reset(self, tmp_path):
        """When approve(MAX) succeeds directly, approve(0) is NEVER called."""
        from paperbot.live.aerodrome import AerodromeLive

        bot, mock_contract, nonce_mgr = self._make_bot(tmp_path)
        account = MagicMock()
        account.address = "0x" + "b" * 40
        mock_contract.functions.allowance.return_value.call.return_value = 100

        mock_contract.functions.approve.return_value.build_transaction.return_value = {
            "from": account.address,
            "nonce": 0,
            "gas": 60000,
        }
        bot._send = MagicMock(return_value=(True, 1, "0xhash"))

        result = AerodromeLive.ensure_approval(bot, "0x" + "c" * 40, 1000, account, nonce_mgr)

        # Only 1 approve call (the direct MAX), no approve(0)
        assert mock_contract.functions.approve.call_count == 1


# ---------------------------------------------------------------------------
# Fix 2: USD_PER_BASE stale — invalidated BEFORE _update_pool_config
# ---------------------------------------------------------------------------
class TestUSDPERBASEStale:
    """USD_PER_BASE must be set to None BEFORE _update_pool_config in _migrate_asset."""

    def test_usd_per_base_invalidated_before_pool_update(self, tmp_path):
        """Verify the source code order: USD_PER_BASE = None comes before _update_pool_config."""
        import ast
        src = Path("/home/tt/thinking/plan/paperbot/live/trader.py").read_text()
        tree = ast.parse(src)

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name == "_migrate_asset":
                # Find all statements in the method body
                body = node.body
                usd_none_line = None
                update_pool_line = None
                for stmt in body:
                    # Look for: global USD_PER_BASE; USD_PER_BASE = None
                    if isinstance(stmt, ast.Global) and "USD_PER_BASE" in stmt.names:
                        usd_none_line = stmt.lineno
                    if isinstance(stmt, ast.Assign):
                        for target in stmt.targets:
                            if isinstance(target, ast.Name) and target.id == "USD_PER_BASE":
                                usd_none_line = stmt.lineno
                    # Look for: self._update_pool_config(cand)
                    if isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Call):
                        call = stmt.value
                        if (isinstance(call.func, ast.Attribute)
                                and call.func.attr == "_update_pool_config"):
                            update_pool_line = stmt.lineno
                break

        assert usd_none_line is not None, "USD_PER_BASE = None not found in _migrate_asset"
        assert update_pool_line is not None, "_update_pool_config call not found in _migrate_asset"
        assert usd_none_line < update_pool_line, (
            f"USD_PER_BASE invalidated at line {usd_none_line} "
            f"but _update_pool_config at line {update_pool_line}; "
            f"expected invalidation BEFORE pool update"
        )


# ---------------------------------------------------------------------------
# Fix 3: resync_if_behind logs exceptions instead of swallowing
# ---------------------------------------------------------------------------
class TestResyncIfBehindLogging:
    """resync_if_behind must log warnings on exception, not silently return."""

    def test_resync_logs_on_exception(self):
        """Verify the source code contains logger.warning in resync_if_behind."""
        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()
        # Find the resync_if_behind method
        lines = src.split("\n")
        in_resync = False
        found_warning = False
        for i, line in enumerate(lines):
            if "def resync_if_behind" in line:
                in_resync = True
                continue
            if in_resync:
                if line.strip() and not line.startswith(" ") and not line.startswith("\t"):
                    break  # Left the method
                if "logger.warning" in line or "log.warning" in line:
                    found_warning = True
                    break
        assert found_warning, "resync_if_behind does not log warnings on exception"

    def test_resync_does_not_swallow_exceptions(self):
        """Verify no bare 'return' after except without logging."""
        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()
        lines = src.split("\n")
        in_resync = False
        for i, line in enumerate(lines):
            if "def resync_if_behind" in line:
                in_resync = True
                continue
            if in_resync:
                stripped = line.strip()
                if stripped and not line.startswith(" ") and not line.startswith("\t"):
                    break
                if "except Exception" in stripped:
                    # Next non-empty line should NOT be bare 'return'
                    for j in range(i + 1, min(i + 4, len(lines))):
                        next_line = lines[j].strip()
                        if next_line:
                            assert next_line != "return", (
                                f"resync_if_behind: bare 'return' after except at line {j+1} "
                                f"without logging"
                            )
                            break


# ---------------------------------------------------------------------------
# Fix 4: Supervisor YAML parser — uses yaml.safe_load()
# ---------------------------------------------------------------------------
class TestSupervisorYAMLParse:
    """read_config must use yaml.safe_load, not manual line parsing."""

    def test_uses_yaml_safe_load(self):
        """Verify the source code imports and uses yaml.safe_load."""
        src = Path("/home/tt/thinking/plan/supervisor.py").read_text()
        assert "yaml.safe_load" in src, "read_config does not use yaml.safe_load"

    def test_read_config_returns_dict_from_yaml(self, tmp_path, monkeypatch):
        """read_config correctly parses a real YAML file."""
        import yaml
        from supervisor import read_config

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(yaml.dump({
            "network": {"rpc_url": "https://base.llamarpc.com"},
            "pool": {"address": "0xabc"},
            "grid": {"anchor_price": 2000.0, "spacing_pct": 2.0},
            "live": {"dry_run": False, "stop_loss_pct": 10},
            "alerts": {"telegram_bot_token": "", "telegram_chat_id": ""},
        }))

        monkeypatch.setattr("supervisor.CONFIG", config_yaml)
        cfg = read_config()

        assert isinstance(cfg, dict)
        assert cfg["grid"]["anchor_price"] == 2000.0
        assert cfg["live"]["stop_loss_pct"] == 10

    def test_read_config_handles_empty_token(self, tmp_path, monkeypatch):
        """Empty telegram_bot_token should be '' (falsy), not None or 'None'."""
        import yaml
        from supervisor import read_config

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text(yaml.dump({
            "alerts": {"telegram_bot_token": ""},
            "grid": {"anchor_price": 100.0},
        }))

        monkeypatch.setattr("supervisor.CONFIG", config_yaml)
        cfg = read_config()

        token = cfg.get("alerts", {}).get("telegram_bot_token", "")
        assert token == "" or token is None, f"Expected empty/None token, got {token!r}"
        # Must be falsy (the send_telegram check relies on this)
        assert not token

    def test_read_config_returns_empty_on_invalid_yaml(self, tmp_path, monkeypatch):
        """Malformed YAML should return {} without crashing."""
        from supervisor import read_config

        config_yaml = tmp_path / "config.yaml"
        config_yaml.write_text("{{{{invalid yaml")

        monkeypatch.setattr("supervisor.CONFIG", config_yaml)
        cfg = read_config()
        assert cfg == {}
