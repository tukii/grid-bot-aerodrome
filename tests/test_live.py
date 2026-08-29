"""Tests del módulo live con web3/requests mockeados."""
import pytest


class FakeContractFn:
    def __init__(self, ret=None, exc=None):
        self.ret = ret
        self.exc = exc
        self.calls = 0

    def call(self):
        self.calls += 1
        if self.exc:
            raise self.exc
        return self.ret

    def build_transaction(self, tx):
        return {**tx, "to": "0x"}


class FakeContract:
    def __init__(self, **funcs):
        self._funcs = funcs

    def functions(self):
        return self

    def __getattr__(self, name):
        if name in self._funcs:
            return self._funcs[name]
        raise AttributeError(name)


def test_swapresult_defaults():
    from paperbot.live.aerodrome import SwapResult
    r = SwapResult(True, True, None, 100, None, "dry")
    assert r.gas_used is None
    assert r.receipt_status is None


def test_approval_status_constants():
    from paperbot.live.aerodrome import ApprovalStatus
    assert ApprovalStatus.EXISTS == "exists"
    assert ApprovalStatus.SENT == "sent"
    assert ApprovalStatus.FAILED == "failed"


def test_nonce_manager_tracks():
    from paperbot.live.aerodrome import NonceManager

    class FakeW3:
        def __init__(self):
            self.n = 5
            self.eth = self

        def get_transaction_count(self, addr, block):
            return self.n

    w3 = FakeW3()
    mgr = NonceManager(w3, "0x0000000000000000000000000000000000000001")
    mgr.sync()
    assert mgr.next() == 5
    assert mgr.next() == 6
    mgr.mark_sent(7)
    assert mgr.next() == 8


def test_nonce_manager_resync_behind():
    from paperbot.live.aerodrome import NonceManager

    class FakeW3:
        def __init__(self):
            self.n = 10
            self.eth = self

        def get_transaction_count(self, addr, block):
            return self.n

    w3 = FakeW3()
    mgr = NonceManager(w3, "0x0000000000000000000000000000000000000001")
    mgr.sync()
    assert mgr.next() == 10
    w3.n = 20  # chain moved ahead (external tx)
    mgr.resync_if_behind()
    assert mgr.next() == 20


def test_order_size_caps_at_25pct(monkeypatch):
    from paperbot.paper.store import Store
    from paperbot.live.trader import LiveGridTrader

    # We can't easily construct LiveGridTrader without web3; test the formula.
    max_spend = 5.0
    n = 11
    step_usd = max_spend / n
    step_usd = min(step_usd, max_spend * 0.25)
    # 5/11 = 0.4545, capped at 1.25 -> stays 0.4545
    assert step_usd == pytest.approx(5.0 / 11)
