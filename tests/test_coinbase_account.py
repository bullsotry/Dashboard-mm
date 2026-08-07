"""Coverage for the Coinbase account adapter: quote-currency parsing and the
balance derivation. The SDK client is replaced with a small fake exposing
just what get_account() reads — this never calls Coinbase for real.

Runs under pytest, or standalone: `venv/bin/python tests/test_coinbase_account.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from adapters.coinbase_account import CoinbaseAccountAdapter, _quote_currency  # noqa: E402


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_quote_currency_splits_on_dash():
    assert _quote_currency("SOL-USDC") == "USDC"


def test_quote_currency_no_dash_falls_back_to_whole_symbol():
    # Deliberately wrong-looking: better to look for a currency that can't
    # match than to silently guess.
    assert _quote_currency("SOLUSDC") == "SOLUSDC"


class _FakeAccount:
    def __init__(self, currency, available, hold):
        self.currency = currency
        self.available_balance = {"value": available}
        self.hold = {"value": hold}


class _FakeClient:
    def __init__(self, accounts, raise_on_call=False):
        self._accounts = accounts
        self._raise = raise_on_call

    def get_accounts(self, limit=250):
        if self._raise:
            raise RuntimeError("network down")
        return SimpleNamespace(accounts=self._accounts)


def _adapter(client):
    a = CoinbaseAccountAdapter(symbol="SOL-USDC", api_key=None, api_secret=None, poll_interval_s=0.0)
    a._client = client
    return a


def test_get_account_finds_matching_quote_currency():
    client = _FakeClient([_FakeAccount("USDC", "100.5", "10.0"), _FakeAccount("SOL", "3.0", "0.0")])
    acc = _adapter(client).get_account()
    assert approx(acc["available"], 100.5)
    assert approx(acc["margin_used"], 10.0)
    assert acc["unrealised_pnl"] == 0.0  # spot: always 0.0, never a real figure


def test_get_account_no_matching_currency_keeps_cache():
    client = _FakeClient([_FakeAccount("ETH", "1.0", "0.0")])
    assert _adapter(client).get_account() is None


def test_get_account_no_client_returns_none():
    a = CoinbaseAccountAdapter(symbol="SOL-USDC")
    a._client = None
    assert a.get_account() is None


def test_get_account_sdk_exception_keeps_cache():
    client = _FakeClient([], raise_on_call=True)
    assert _adapter(client).get_account() is None


def _run_all():
    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
