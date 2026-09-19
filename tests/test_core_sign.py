"""Контракт подписи: агент и сервер бота должны подписывать одинаково.

Сервер (bot/ingest_server.py::_validate_hmac) строит сообщение как
f"{api_key}|{timestamp}|{nonce}".encode() + body — агент обязан совпасть.
"""

import hashlib
import hmac

from core._fallback import sign_request


def _server_side_expected(api_key, api_secret, ts, nonce, body):
    message = f"{api_key}|{ts}|{nonce}".encode() + body
    return hmac.new(api_secret.encode(), message, hashlib.sha256).hexdigest()


def test_sign_request_headers_shape():
    headers = sign_request("k123", "s456", b'{"a":1}')
    assert headers["X-API-Key"] == "k123"
    assert headers["Content-Type"] == "application/json"
    for k in ("X-Timestamp", "X-Nonce", "X-Signature"):
        assert headers[k]


def test_sign_request_matches_server_format():
    body = b'{"group_id": 1}'
    headers = sign_request("keyA", "secretB", body)
    expected = _server_side_expected(
        "keyA", "secretB", headers["X-Timestamp"], headers["X-Nonce"], body
    )
    assert headers["X-Signature"] == expected


def test_signature_depends_on_body():
    h1 = sign_request("k", "s", b"one")["X-Signature"]
    h2 = sign_request("k", "s", b"two")["X-Signature"]
    assert h1 != h2


def test_nonces_unique():
    nonces = {sign_request("k", "s", b"x")["X-Nonce"] for _ in range(20)}
    assert len(nonces) == 20
