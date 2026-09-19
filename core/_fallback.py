"""
Fallback implementation of core functions (без Cython).
Используется когда .so не скомпилирован (для разработки/отладки).
В продакшене заменяется на скомпилированную версию из _core.pyx.
"""

import hashlib
import hmac
import platform
import secrets
import socket
import time
import uuid


def sign_request(api_key: str, api_secret: str, body: bytes) -> dict:
    """HMAC-SHA256 подпись запроса."""
    timestamp = str(int(time.time()))
    nonce = str(uuid.uuid4())
    message = f"{api_key}|{timestamp}|{nonce}".encode() + body
    signature = hmac.new(
        api_secret.encode(), message, hashlib.sha256
    ).hexdigest()
    return {
        "X-API-Key": api_key,
        "X-Timestamp": timestamp,
        "X-Nonce": nonce,
        "X-Signature": signature,
        "Content-Type": "application/json",
    }


def compute_integrity() -> str:
    """SHA256 текущего .py файла (для self-check)."""
    import os
    this_file = os.path.abspath(__file__)
    with open(this_file, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def get_hw_fingerprint() -> str:
    """SHA256 hardware fingerprint (MAC + hostname + machine-id)."""
    parts = []

    # MAC адрес
    mac = hex(uuid.getnode())
    parts.append(mac)

    # Hostname
    parts.append(socket.gethostname())

    # machine-id (Linux)
    try:
        with open("/etc/machine-id") as f:
            parts.append(f.read().strip())
    except FileNotFoundError:
        parts.append(platform.node())

    combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def handle_challenge(nonce: str, api_key: str) -> str:
    """Ответ на challenge (трансформации nonce)."""
    data = nonce.encode()

    # XOR с api_key bytes
    key_bytes = api_key.encode()
    result = bytearray()
    for i, b in enumerate(data):
        result.append(b ^ key_bytes[i % len(key_bytes)])

    # Rotation
    result = bytes([(b << 3 | b >> 5) & 0xFF for b in result])

    # SHA256 результата
    return hashlib.sha256(result).hexdigest()
