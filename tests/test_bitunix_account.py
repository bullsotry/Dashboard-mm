"""Coverage for the Bitunix account adapter: signing format and the balance
parse/derivation logic. `requests.get` is monkeypatched at module level —
this never touches Bitunix or real credentials.

Runs under pytest, or standalone: `venv/bin/python tests/test_bitunix_account.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters.bitunix_account as bitunix_account  # noqa: E402
from adapters.bitunix_account import BitunixAccountAdapter, _sign  # noqa: E402


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_sign_is_deterministic_double_sha256():
    a = _sign("key", "secret", "nonce1", "1700000000000", "marginCoinUSDT")
    b = _sign("key", "secret", "nonce1", "1700000000000", "marginCoinUSDT")
    assert a == b
    # A different secret must not collide.
    c = _sign("key", "other-secret", "nonce1", "1700000000000", "marginCoinUSDT")
    assert a != c


def _adapter():
    return BitunixAccountAdapter(api_key="k", secret_key="s", poll_interval_s=0.0)


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        pass

    def json(self):
        return self._payload


def test_get_account_parses_balance_and_derives_equity(monkeypatch):
    payload = {"data": [{"available": "60.0", "margin": "35.0", "crossUnrealizedPNL": "5.0"}]}
    monkeypatch.setattr(bitunix_account.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    acc = _adapter().get_account()
    assert approx(acc["available"], 60.0)
    assert approx(acc["margin_used"], 35.0)
    assert approx(acc["unrealised_pnl"], 5.0)
    # Bitunix has no single equity field (unlike OKX's "eq"); derived as
    # available + frozen + margin + upnl + bonus.
    assert approx(acc["equity"], 100.0)


def test_equity_counts_frozen_and_isolated_upnl(monkeypatch):
    """The live 2026-08-12 response, verbatim. Reading only
    available+margin+crossUnrealizedPNL reported 14.42 on an account
    actually holding 106.95 — 87% of it sitting in `frozen`, which rises
    and falls as the bot places and cancels quotes, so the NAV lurched with
    order state instead of with net assets.

      1.7342580352669438  available
    + 92.4299858104473882 frozen
    + 12.6842845663654454 margin
    +  0.0                crossUnrealizedPNL   (positions are isolated)
    +  0.1051843842029562 isolationUnrealizedPNL
    +  0.0                bonus
    = 106.9537127962827336
    """
    payload = {
        "data": [
            {
                "available": "1.7342580352669438",
                "bonus": "0",
                "crossUnrealizedPNL": "0",
                "frozen": "92.4299858104473882",
                "isolationUnrealizedPNL": "0.1051843842029562",
                "margin": "12.6842845663654454",
                "marginCoin": "USDT",
                "positionMode": "HEDGE",
                # Mirrors `available`; must not be added or it double-counts.
                "transfer": "1.7342580352669438",
            }
        ]
    }
    monkeypatch.setattr(bitunix_account.requests, "get", lambda *a, **kw: _FakeResponse(payload))
    acc = _adapter().get_account()
    assert approx(acc["frozen"], 92.4299858104473882)
    assert approx(acc["unrealised_pnl"], 0.1051843842029562)
    assert approx(acc["equity"], 106.9537127962827336, tol=1e-9)
    assert acc["equity_scope"] == "USDT futures account"


def test_get_account_request_exception_keeps_cache(monkeypatch):
    a = _adapter()

    def _raise(*args, **kwargs):
        raise bitunix_account.requests.RequestException("network down")

    monkeypatch.setattr(bitunix_account.requests, "get", _raise)
    assert a.get_account() is None


def test_get_account_empty_data_keeps_cache(monkeypatch):
    a = _adapter()
    monkeypatch.setattr(bitunix_account.requests, "get", lambda *a, **kw: _FakeResponse({"data": []}))
    assert a.get_account() is None


class _FakeMonkeypatch:
    def setattr(self, obj, name, value):
        setattr(obj, name, value)


def _run_all():
    import inspect

    tests = [v for k, v in globals().items() if k.startswith("test_")]
    for t in tests:
        if "monkeypatch" in inspect.signature(t).parameters:
            t(_FakeMonkeypatch())
        else:
            t()
        print(f"ok  {t.__name__}")
    print(f"\n{len(tests)} passed")


if __name__ == "__main__":
    _run_all()
