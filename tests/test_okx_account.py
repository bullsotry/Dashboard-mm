"""Coverage for the OKX account adapter: signing format and the balance
parse/derivation logic. `_get_ipv4` (the actual network call) is
monkeypatched at module level — this never touches OKX or real credentials.

Runs under pytest, or standalone: `venv/bin/python tests/test_okx_account.py`.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import adapters.okx_account as okx_account  # noqa: E402
from adapters.okx_account import OkxAccountAdapter, _iso_timestamp, _sign  # noqa: E402


def approx(a, b, tol=1e-9):
    return abs(a - b) <= tol


def test_iso_timestamp_shape():
    ts = _iso_timestamp()
    assert ts.endswith("Z")
    assert "T" in ts
    assert len(ts) == 24  # YYYY-MM-DDTHH:MM:SS.mmmZ


def test_sign_is_deterministic_hmac_b64():
    a = _sign("secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/account/balance", "")
    b = _sign("secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/account/balance", "")
    assert a == b
    # A different secret must not collide.
    c = _sign("other-secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/account/balance", "")
    assert a != c


def _adapter():
    return OkxAccountAdapter(api_key="k", secret_key="s", passphrase="p", poll_interval_s=0.0)


def test_get_account_parses_balance_and_derives_margin_used(monkeypatch):
    payload = {"data": [{"details": [{"ccy": "USDC", "eq": "100.0", "availEq": "60.0", "upl": "5.0"}]}]}
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (200, json.dumps(payload).encode()))
    acc = _adapter().get_account()
    assert approx(acc["available"], 60.0)
    assert approx(acc["unrealised_pnl"], 5.0)
    # margin_used = eq - availEq - upl = 100 - 60 - 5 = 35
    assert approx(acc["margin_used"], 35.0)
    # equity is OKX's own "eq" field, reported as-is, not re-derived.
    assert approx(acc["equity"], 100.0)


def test_get_account_falls_back_to_first_detail_when_margin_coin_absent(monkeypatch):
    payload = {"data": [{"details": [{"ccy": "USDT", "eq": "10.0", "availBal": "8.0", "upl": "0.0"}]}]}
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (200, json.dumps(payload).encode()))
    acc = _adapter().get_account()
    assert approx(acc["available"], 8.0)


def test_get_account_non_200_keeps_last_cached(monkeypatch):
    a = _adapter()
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (401, b'{"code":"50119"}'))
    assert a.get_account() is None  # nothing cached yet, still None


def test_get_account_keeps_cache_across_a_later_failure(monkeypatch):
    a = _adapter()
    payload = {"data": [{"details": [{"ccy": "USDC", "eq": "1.0", "availEq": "1.0", "upl": "0.0"}]}]}
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (200, json.dumps(payload).encode()))
    first = a.get_account()
    assert first is not None

    a._last_poll_ts = 0.0  # force a repoll
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (500, b""))
    second = a.get_account()
    assert second is first  # unchanged, not blanked


def test_get_account_malformed_json_keeps_cache(monkeypatch):
    a = _adapter()
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (200, b"not json"))
    assert a.get_account() is None


def test_get_account_empty_data_list_keeps_cache(monkeypatch):
    a = _adapter()
    monkeypatch.setattr(okx_account, "_get_ipv4", lambda *a, **kw: (200, b'{"data": []}'))
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
