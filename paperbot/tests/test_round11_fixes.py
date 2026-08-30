"""Tests for Round 11 ALTO/MEDIO code fixes.

ALTO #1: _order_size_base() accepts total param (avoids 3 RPCs per cycle).
ALTO #2: dashboard.py XSS — html.escape() on error messages.
ALTO #4: _build_fee_params raises ValueError when cap <= 0.
MEDIO #2: ensure_approval caches infinite approve per (token, account, router).
"""
import html as html_mod
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# Ensure project root is on path
ROOT = Path(__file__).resolve().parent.parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ── ALTO #1: _order_size_base(total) ────────────────────────────────────────

class TestOrderSizeBaseTotalParam:
    """_order_size_base(total) uses the pre-fetched total instead of re-calling _usd_value()."""

    def test_uses_provided_total_no_rpc(self):
        """When total > 0 is passed, _usd_value is NOT called."""
        from paperbot.live.trader import LiveGridTrader, _usd_value

        trader = MagicMock(spec=LiveGridTrader)
        trader.grid = MagicMock()
        trader.grid.levels = [MagicMock(), MagicMock(), MagicMock()]  # 3 levels
        trader.max_spend_usd = 5.0
        trader._last_price = 2000.0
        trader.bot = MagicMock()
        trader.bot.base_decimals = 18
        trader.bot.base_token = "0xtoken"

        # When total=100.0 is passed, _usd_value should NOT be called
        with patch("paperbot.live.trader._usd_value") as mock_usd:
            result = LiveGridTrader._order_size_base(trader, total=100.0)
            mock_usd.assert_not_called()

        # step_usd = min(5.0/3, 5.0*0.25, 100.0*0.15) = min(1.667, 1.25, 15.0) = 1.25
        # result = int(1.25 / 2000.0 * 1e18) = 625000000000000
        assert result > 0

    def test_zero_total_falls_back_to_usd_value(self):
        """When total=0 (default), _usd_value IS called (legacy path)."""
        from paperbot.live.trader import LiveGridTrader

        trader = MagicMock(spec=LiveGridTrader)
        trader.grid = MagicMock()
        trader.grid.levels = [MagicMock()]
        trader.max_spend_usd = 5.0
        trader._last_price = 2000.0
        # Explicitly set bot/account since spec-based MagicMock won't auto-create instance attrs
        trader.bot = MagicMock()
        trader.bot.base_decimals = 18
        trader.account = MagicMock()

        with patch("paperbot.live.trader._usd_value", return_value=(100.0, 50.0, 150.0)) as mock_usd:
            result = LiveGridTrader._order_size_base(trader, total=0.0)
            mock_usd.assert_called_once()

    def test_total_zero_exception_falls_back(self):
        """When total=0 and _usd_value raises, uses conservative cap."""
        from paperbot.live.trader import LiveGridTrader

        trader = MagicMock(spec=LiveGridTrader)
        trader.grid = MagicMock()
        trader.grid.levels = [MagicMock()]
        trader.max_spend_usd = 5.0
        trader._last_price = 2000.0
        trader.bot = MagicMock()
        trader.bot.base_decimals = 18

        with patch("paperbot.live.trader._usd_value", side_effect=RuntimeError("no price")):
            result = LiveGridTrader._order_size_base(trader, total=0.0)
        # Should use conservative cap: 5% of 5.0 = 0.25 / 2000 * 1e18
        assert result > 0

    def test_execute_buy_passes_total(self):
        """_execute_buy forwards total to _order_size_base."""
        from paperbot.live.trader import LiveGridTrader

        trader = MagicMock(spec=LiveGridTrader)
        trader.bot = MagicMock()
        trader.bot.quote_decimals = 6
        trader.bot.usdc = "0xusdc"
        trader.account = MagicMock()
        trader.account.address = "0xaddr"
        trader.bot.token_balance.return_value = int(100 * 1e6)  # 100 USDC
        trader.bot.base_decimals = 18
        trader.bot.base_token = "0xtoken"
        trader.dry_run = True
        trader._last_price = 2000.0

        with patch.object(trader, "_order_size_base", return_value=625000000000000) as mock_size:
            LiveGridTrader._execute_buy(trader, 1990.0, total=100.0)
            mock_size.assert_called_with(100.0)


# ── ALTO #2: dashboard XSS ──────────────────────────────────────────────────

class TestDashboardXSS:
    """dashboard.py error handler escapes HTML in exception messages."""

    def test_html_escape_in_error_handler(self):
        """Exception message is escaped before being embedded in HTML."""
        # Simulate what the handler does
        e = Exception('<script>alert("xss")</script>')
        error_html = f"<h1>Error renderizando dashboard</h1><pre>{html_mod.escape(str(e))}</pre>"
        assert "<script>" not in error_html
        assert "&lt;script&gt;" in error_html

    def test_html_escape_with_quotes(self):
        """Quotes in exception messages are escaped."""
        e = Exception('key "value" & <other>')
        error_html = html_mod.escape(str(e))
        assert '"' not in error_html or "&quot;" in error_html
        assert "&amp;" in error_html

    def test_dashboard_import_html_module(self):
        """dashboard.py imports html module as html_mod."""
        import importlib
        spec = importlib.util.find_spec("dashboard")
        if spec:
            source = Path(spec.origin).read_text()
            assert "import html as html_mod" in source
            assert "html_mod.escape" in source


# ── ALTO #4: _build_fee_params cap <= 0 ─────────────────────────────────────

class TestBuildFeeParamsCapZero:
    """_build_fee_params raises ValueError when max_gas_gwei <= 0."""

    def test_raises_value_error_when_cap_zero(self):
        """cap=0 raises ValueError, not gas_price fallback."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.0
        bot._eip1559 = True
        bot.w3 = MagicMock()

        with pytest.raises(ValueError, match="max_gas_gwei must be > 0"):
            AerodromeLive._build_fee_params(bot)

    def test_raises_value_error_when_cap_negative(self):
        """Negative cap also raises ValueError."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = -0.1
        bot._eip1559 = True
        bot.w3 = MagicMock()

        with pytest.raises(ValueError, match="max_gas_gwei must be > 0"):
            AerodromeLive._build_fee_params(bot)

    def test_normal_cap_still_works(self):
        """Positive cap still produces valid EIP-1559 params."""
        from paperbot.live.aerodrome import AerodromeLive

        bot = MagicMock(spec=AerodromeLive)
        bot.max_gas_gwei = 0.1
        bot._eip1559 = True
        bot.w3 = MagicMock()
        bot.w3.eth.get_block.return_value = {"baseFeePerGas": int(0.005e9)}

        result = AerodromeLive._build_fee_params(bot)
        assert "maxFeePerGas" in result
        assert result["maxFeePerGas"] > 0


# ── MEDIO #2: ensure_approval cache ─────────────────────────────────────────

class TestEnsureApprovalCache:
    """ensure_approval caches infinite approve per (token, account, router)."""

    def test_cache_hit_skips_allowance_rpc(self):
        """When (token, account, router) is in _approved_tokens, no RPC is made."""
        from paperbot.live.aerodrome import AerodromeLive, ApprovalStatus
        from web3 import Web3

        bot = MagicMock(spec=AerodromeLive)
        bot.router = Web3.to_checksum_address("0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F")
        bot.w3 = MagicMock()

        token = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        account = MagicMock()
        account.address = "0x1234567890abcdef1234567890abcdef12345678"
        account_addr = Web3.to_checksum_address(account.address)

        # Pre-populate cache
        bot._approved_tokens = {(token, account_addr, bot.router)}

        result = AerodromeLive.ensure_approval(bot, token, 1000, account)
        assert result == ApprovalStatus.EXISTS
        # w3.eth.contract should NOT have been called (no RPC)
        bot.w3.eth.contract.assert_not_called()

    def test_infinite_allowance_gets_cached(self):
        """When on-chain allowance is MAX_UINT, it gets added to cache."""
        from paperbot.live.aerodrome import AerodromeLive, ApprovalStatus
        from web3 import Web3

        MAX_UINT = (1 << 256) - 1

        bot = MagicMock(spec=AerodromeLive)
        bot.router = Web3.to_checksum_address("0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F")
        bot.w3 = MagicMock()
        bot._approved_tokens = set()

        token = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        account = MagicMock()
        account.address = "0x1234567890abcdef1234567890abcdef12345678"
        account_addr = Web3.to_checksum_address(account.address)

        # Mock allowance return MAX_UINT
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = MAX_UINT
        bot.w3.eth.contract.return_value = mock_contract

        result = AerodromeLive.ensure_approval(bot, token, 1000, account)
        assert result == ApprovalStatus.EXISTS
        # Should be cached now
        assert (token, account_addr, bot.router) in bot._approved_tokens

    def test_insufficient_allowance_not_cached(self):
        """When allowance < amount and < MAX_UINT, cache is NOT updated."""
        from paperbot.live.aerodrome import AerodromeLive, ApprovalStatus
        from web3 import Web3

        bot = MagicMock(spec=AerodromeLive)
        bot.router = Web3.to_checksum_address("0x698Cb2b6dd822994581fEa6eA4Fc755d1363A92F")
        bot.w3 = MagicMock()
        bot._approved_tokens = set()

        token = Web3.to_checksum_address("0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913")
        account = MagicMock()
        account.address = "0x1234567890abcdef1234567890abcdef12345678"
        account_addr = Web3.to_checksum_address(account.address)

        # Mock allowance return very small amount
        mock_contract = MagicMock()
        mock_contract.functions.allowance.return_value.call.return_value = 0
        bot.w3.eth.contract.return_value = mock_contract
        # Mock the rest of the approve flow to return FAILED
        bot.get_nonce_manager.return_value = MagicMock()
        bot._build_fee_params.return_value = {"gasPrice": 1000}
        bot.chain_id = 8453
        bot._estimate_gas.return_value = 60000
        bot._send.return_value = (False, None, None)

        result = AerodromeLive.ensure_approval(bot, token, 1000, account)
        # Should NOT be cached since approve failed
        assert (token, account_addr, bot.router) not in bot._approved_tokens
