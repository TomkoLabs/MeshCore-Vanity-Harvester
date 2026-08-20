"""Ed25519 / MeshCore key derivation and independent verification.

MeshCore stores the *expanded* 64-byte private key (the SHA-512 of the seed,
clamped), not the 32-byte seed. Key material produced by an external generator
is re-derived here with an independent implementation before it is ever
written to disk, so a buggy or hostile backend cannot poison the leaderboards.
"""

from __future__ import annotations

import hashlib
from typing import Any, Optional, Tuple

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from .catalog import HEX_DIGITS

RESERVED_FIRST_BYTES = (0x00, 0xFF)


def meshcore_private_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 seed must contain exactly 32 bytes")
    expanded = bytearray(hashlib.sha512(seed).digest())
    expanded[0] &= 0xF8
    expanded[31] &= 0x7F
    expanded[31] |= 0x40
    return bytes(expanded)


def public_key_from_seed(seed: bytes) -> bytes:
    private_key = Ed25519PrivateKey.from_private_bytes(seed)
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


# --- Reference scalar multiplication --------------------------------------
#
# Deliberately independent of the `cryptography` backend: it is the second
# opinion used to verify externally supplied private keys.

_Q = 2**255 - 19
_D = (-121665 * pow(121666, _Q - 2, _Q)) % _Q
_I = pow(2, (_Q - 1) // 4, _Q)


def _xrecover(y: int) -> int:
    xx = ((y * y - 1) * pow(_D * y * y + 1, _Q - 2, _Q)) % _Q
    x = pow(xx, (_Q + 3) // 8, _Q)
    if (x * x - xx) % _Q != 0:
        x = (x * _I) % _Q
    if x & 1:
        x = _Q - x
    return x


_BASE_Y = (4 * pow(5, _Q - 2, _Q)) % _Q
_BASE = (_xrecover(_BASE_Y), _BASE_Y)


def _add(left: Tuple[int, int], right: Tuple[int, int]) -> Tuple[int, int]:
    x1, y1 = left
    x2, y2 = right
    product = (_D * x1 * x2 * y1 * y2) % _Q
    x3 = ((x1 * y2 + x2 * y1) * pow(1 + product, _Q - 2, _Q)) % _Q
    y3 = ((y1 * y2 + x1 * x2) * pow(1 - product, _Q - 2, _Q)) % _Q
    return x3, y3


def _scalar_mult(point: Tuple[int, int], scalar: int) -> Tuple[int, int]:
    result = (0, 1)
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def public_key_from_meshcore_private(private_key: bytes) -> bytes:
    if len(private_key) != 64:
        raise ValueError("MeshCore private key must contain exactly 64 bytes")
    scalar_bytes = private_key[:32]
    if scalar_bytes[0] & 0x07 or scalar_bytes[31] & 0x80 or not scalar_bytes[31] & 0x40:
        raise ValueError("MeshCore Ed25519 scalar is not clamped")
    scalar = int.from_bytes(scalar_bytes, "little")
    x, y = _scalar_mult(_BASE, scalar)
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def normalize_hex(value: Any, expected_length: int) -> Optional[str]:
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if len(normalized) != expected_length or any(c not in HEX_DIGITS for c in normalized):
        return None
    return normalized


def loose_hex(value: Any) -> Optional[str]:
    """Accept the several shapes an external tool might use for hex bytes."""
    if isinstance(value, (bytes, bytearray)):
        return bytes(value).hex().upper()
    if isinstance(value, list) and all(isinstance(item, int) and 0 <= item <= 255 for item in value):
        return bytes(value).hex().upper()
    if not isinstance(value, str):
        return None
    normalized = value.strip().upper()
    if normalized.startswith("0X"):
        normalized = normalized[2:]
    normalized = "".join(c for c in normalized if not c.isspace())
    return normalized if normalized and all(c in HEX_DIGITS for c in normalized) else None


def is_reserved_public_key(public_key_hex: str) -> bool:
    """MeshCore reserves keys beginning 00 or FF."""
    return public_key_hex.startswith(("00", "FF"))


def self_test() -> None:
    """RFC 8032 test vector 1, both derivation paths."""
    seed = bytes.fromhex("9D61B19DEFFD5A60BA844AF492EC2CC44449C5697B326919703BAC031CAE7F60")
    expected = "D75A980182B10AB7D54BFED3C964073A0EE172F3DAA62325AF021A68F707511A"
    if public_key_from_seed(seed).hex().upper() != expected:
        raise RuntimeError("Ed25519 public-key self-test failed")
    expanded = meshcore_private_key_from_seed(seed)
    if public_key_from_meshcore_private(expanded).hex().upper() != expected:
        raise RuntimeError("MeshCore expanded-private-key self-test failed")
