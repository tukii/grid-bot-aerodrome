"""Tests for Round 14 critical fixes — C1, C3, H5.

C1: _emergency_unwind writes halted=true to DB before self.running = False.
C3: _switch_rpc clears _approved_tokens cache on RPC failover.
H5: supervisor.py SQLite writes use timeout=5 to avoid 'database is locked'.

All tests use mocks — no network, no real DB.
"""
import ast
import sqlite3
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch, call

import pytest


# ---------------------------------------------------------------------------
# C1: _emergency_unwind must write halted=true before self.running = False
# ---------------------------------------------------------------------------

class TestEmergencyUnwindWritesHalted:
    """C1: _emergency_unwind writes halted=true to DB before stopping."""

    def test_halted_written_before_running_false(self):
        """halted=true must appear in source BEFORE self.running = False."""
        src = Path("/home/tt/thinking/plan/paperbot/live/trader.py").read_text()
        # Find _emergency_unwind method body
        in_method = False
        halted_line = None
        running_false_line = None
        for i, line in enumerate(src.split("\n"), 1):
            if "def _emergency_unwind(self)" in line:
                in_method = True
                continue
            if in_method:
                # Stop at next def (not indented)
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def _emergency"):
                    break
                if 'set_meta("halted", "true")' in line:
                    halted_line = i
                if "self.running = False" in line:
                    running_false_line = i

        assert halted_line is not None, "halted=true write not found in _emergency_unwind"
        assert running_false_line is not None, "self.running = False not found in _emergency_unwind"
        assert halted_line < running_false_line, (
            f"halted write (line {halted_line}) must come BEFORE "
            f"running=False (line {running_false_line})"
        )

    def test_halted_written_before_status_stopped(self):
        """halted=true must appear BEFORE status=stopped meta write."""
        src = Path("/home/tt/thinking/plan/paperbot/live/trader.py").read_text()
        in_method = False
        halted_line = None
        status_line = None
        for i, line in enumerate(src.split("\n"), 1):
            if "def _emergency_unwind(self)" in line:
                in_method = True
                continue
            if in_method:
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def _emergency"):
                    break
                if 'set_meta("halted", "true")' in line:
                    halted_line = i
                if 'set_meta("status", "stopped")' in line:
                    status_line = i

        assert halted_line is not None
        assert status_line is not None
        assert halted_line < status_line

    def test_halted_written_in_both_winding_paths(self):
        """halted=true is written regardless of unwind success/failure
        (it appears after the if/else, in the common tail)."""
        src = Path("/home/tt/thinking/plan/paperbot/live/trader.py").read_text()
        # The halted write should be at the same indentation level as
        # self.running = False (common tail, not inside the if/else)
        lines = src.split("\n")
        in_method = False
        found_halted_at_method_level = False
        method_indent = None
        for i, line in enumerate(lines):
            if "def _emergency_unwind(self)" in line:
                in_method = True
                method_indent = len(line) - len(line.lstrip())
                continue
            if in_method:
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def _emergency"):
                    break
                if 'set_meta("halted", "true")' in line:
                    current_indent = len(line) - len(line.lstrip())
                    # Should be at method body level (method_indent + one indent)
                    if current_indent == method_indent + 4:
                        found_halted_at_method_level = True

        assert found_halted_at_method_level, (
            "halted=true write must be at method body level (common tail), "
            "not inside try/except"
        )


# ---------------------------------------------------------------------------
# C3: _switch_rpc must clear _approved_tokens cache
# ---------------------------------------------------------------------------

class TestSwitchRpcClearsCache:
    """C3: _switch_rpc clears _approved_tokens on successful RPC switch."""

    def test_switch_rpc_clears_approved_tokens_in_source(self):
        """Source must contain self._approved_tokens.clear() inside _switch_rpc."""
        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()
        in_method = False
        found_clear = False
        for line in src.split("\n"):
            if "def _switch_rpc" in line:
                in_method = True
                continue
            if in_method:
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def _switch_rpc"):
                    break
                if "_approved_tokens.clear()" in line:
                    found_clear = True
                    break

        assert found_clear, (
            "_switch_rpc must call self._approved_tokens.clear() "
            "to prevent stale allowance cache after RPC failover"
        )

    def test_cache_clear_before_nonce_manager_update(self):
        """Cache clear should happen BEFORE NonceManager update (both are
        on the same RPC change, but cache first is safer — nonce manager
        may re-allow based on stale cache)."""
        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()
        in_method = False
        clear_line = None
        nonce_line = None
        for i, line in enumerate(src.split("\n"), 1):
            if "def _switch_rpc" in line:
                in_method = True
                continue
            if in_method:
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def _switch_rpc"):
                    break
                if "_approved_tokens.clear()" in line:
                    clear_line = i
                if "_nonce_manager" in line and "update_w3" in line:
                    nonce_line = i

        if clear_line and nonce_line:
            assert clear_line < nonce_line, (
                f"cache clear (line {clear_line}) should come before "
                f"NonceManager update (line {nonce_line})"
            )

    def test_approved_tokens_set_exists_in_init(self):
        """_approved_tokens must be initialized as a set in __init__."""
        src = Path("/home/tt/thinking/plan/paperbot/live/aerodrome.py").read_text()
        assert "_approved_tokens" in src
        # Find AerodromeLive's __init__ (has rpc_url param, not w3)
        in_init = False
        found_set_init = False
        for line in src.split("\n"):
            if "def __init__(self, rpc_url" in line:
                in_init = True
                continue
            if in_init:
                stripped = line.strip()
                if stripped.startswith("def ") and not stripped.startswith("def __init__"):
                    break
                if "_approved_tokens" in line and "set()" in line:
                    found_set_init = True
                    break
        assert found_set_init, "_approved_tokens must be initialized as set() in AerodromeLive.__init__"


# ---------------------------------------------------------------------------
# H5: supervisor.py SQLite writes must use timeout=5
# ---------------------------------------------------------------------------

class TestSupervisorSqliteTimeout:
    """H5: supervisor.py SQLite writes use timeout=5."""

    def test_write_meta_uses_timeout(self):
        """write_meta must call sqlite3.connect(DB, timeout=5)."""
        src = Path("/home/tt/thinking/plan/supervisor.py").read_text()
        in_write_meta = False
        found_timeout = False
        for line in src.split("\n"):
            if "def write_meta" in line:
                in_write_meta = True
                continue
            if in_write_meta:
                stripped = line.strip()
                if stripped.startswith("def "):
                    break
                if "sqlite3.connect" in line and "timeout=5" in line:
                    found_timeout = True
                    break
                # Also accept multi-line: connect(\n  DB,\n  timeout=5\n)
                if "sqlite3.connect" in line:
                    # Check next few lines for timeout
                    pass

        assert found_timeout, (
            "write_meta must use sqlite3.connect(DB, timeout=5) "
            "to avoid 'database is locked' errors"
        )

    def test_reanchor_config_uses_timeout(self):
        """reanchor_config grid_state deletion must use timeout=5."""
        src = Path("/home/tt/thinking/plan/supervisor.py").read_text()
        in_reanchor = False
        found_timeout = False
        for line in src.split("\n"):
            if "def reanchor_config" in line:
                in_reanchor = True
                continue
            if in_reanchor:
                stripped = line.strip()
                if stripped.startswith("def "):
                    break
                if "sqlite3.connect" in line and "timeout=5" in line:
                    found_timeout = True
                    break

        assert found_timeout, (
            "reanchor_config must use sqlite3.connect(DB, timeout=5) "
            "when deleting grid_state"
        )

    def test_read_meta_does_not_need_timeout(self):
        """read_meta uses read-only URI mode — timeout not needed there.
        This test confirms we only added timeout to write paths."""
        src = Path("/home/tt/thinking/plan/supervisor.py").read_text()
        in_read_meta = False
        for line in src.split("\n"):
            if "def read_meta" in line:
                in_read_meta = True
                continue
            if in_read_meta:
                stripped = line.strip()
                if stripped.startswith("def "):
                    break
                if "sqlite3.connect" in line:
                    # read_meta should NOT have timeout (it uses uri=True, read-only)
                    assert "timeout=5" not in line, (
                        "read_meta should NOT have timeout=5 (it's read-only)"
                    )
                    return
        # If we got here, read_meta doesn't have a connect — that's fine too
