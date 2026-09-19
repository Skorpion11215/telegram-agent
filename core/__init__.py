"""
Core crypto functions for TransferStats Agent.

Пытается импортировать из скомпилированного _core.so (Cython).
Если .so не найден — использует _fallback.py (для разработки).
"""
try:
    from core._core import sign_request, compute_integrity, get_hw_fingerprint, handle_challenge
except ImportError:
    from core._fallback import sign_request, compute_integrity, get_hw_fingerprint, handle_challenge

__all__ = ["sign_request", "compute_integrity", "get_hw_fingerprint", "handle_challenge"]
