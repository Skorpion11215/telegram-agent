"""
Cython-compiled core functions for TransferStats Agent.

Этот файл компилируется в _core.cpython-*.so через Cython.
Содержит ту же логику что и _fallback.py, но в скомпилированном виде.

ВАЖНО: Алгоритмы ДОЛЖНЫ быть идентичны _fallback.py.
Cython даёт защиту через компиляцию, а не через другие алгоритмы.
"""

import hashlib
import hmac
import os
import platform
import socket
import time
import uuid


def sign_request(str api_key, str api_secret, bytes body) -> dict:
    """HMAC-SHA256 подпись запроса. Идентичен _fallback.py."""
    cdef str timestamp = str(int(time.time()))
    cdef str nonce = str(uuid.uuid4())
    cdef bytes message = f"{api_key}|{timestamp}|{nonce}".encode() + body
    cdef str signature = hmac.new(
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
    """SHA256 текущего .so или .py файла. Идентичен _fallback.py."""
    cdef str dir_path = os.path.dirname(os.path.abspath(__file__))
    # Ищем .so файл (скомпилированная версия)
    for fname in os.listdir(dir_path):
        if fname.startswith("_core") and fname.endswith(".so"):
            so_path = os.path.join(dir_path, fname)
            with open(so_path, "rb") as f:
                return hashlib.sha256(f.read()).hexdigest()
    # Fallback — хешируем _fallback.py
    fb_path = os.path.join(dir_path, "_fallback.py")
    if os.path.exists(fb_path):
        with open(fb_path, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    return hashlib.sha256(b"unknown").hexdigest()


def get_hw_fingerprint() -> str:
    """SHA256 hardware fingerprint. Идентичен _fallback.py."""
    cdef list parts = []
    parts.append(hex(uuid.getnode()))
    parts.append(socket.gethostname())
    try:
        with open("/etc/machine-id") as f:
            parts.append(f.read().strip())
    except FileNotFoundError:
        parts.append(platform.node())
    cdef str combined = "|".join(parts)
    return hashlib.sha256(combined.encode()).hexdigest()


def handle_challenge(str nonce, str api_key) -> str:
    """Ответ на challenge. Идентичен _fallback.py."""
    cdef bytes data = nonce.encode()
    cdef bytes key_bytes = api_key.encode()
    if not key_bytes:
        return hashlib.sha256(data).hexdigest()
    cdef bytearray result = bytearray()
    cdef int i
    cdef int b
    for i in range(len(data)):
        b = data[i] ^ key_bytes[i % len(key_bytes)]
        result.append(b)
    cdef bytes rotated = bytes([(b << 3 | b >> 5) & 0xFF for b in result])
    return hashlib.sha256(rotated).hexdigest()
