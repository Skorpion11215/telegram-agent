"""Challenge-response должен совпадать с серверной проверкой (bot/db.py::verify_challenge_response)."""

import hashlib

from core._fallback import handle_challenge


def _server_expected(nonce: str, api_key: str) -> str:
    data = nonce.encode()
    key_bytes = api_key.encode()
    result = bytearray()
    for i, b in enumerate(data):
        result.append(b ^ key_bytes[i % len(key_bytes)])
    rotated = bytes([(b << 3 | b >> 5) & 0xFF for b in result])
    return hashlib.sha256(rotated).hexdigest()


def test_challenge_matches_server_algorithm():
    nonce = "0f1e2d3c4b5a6978"
    api_key = "abcdef1234567890"
    assert handle_challenge(nonce, api_key) == _server_expected(nonce, api_key)


def test_challenge_deterministic():
    assert handle_challenge("aabb", "kk") == handle_challenge("aabb", "kk")
    assert handle_challenge("aabb", "kk") != handle_challenge("aabb", "ll")
