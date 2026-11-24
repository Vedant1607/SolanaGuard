"""Minimal Bitcoin-alphabet base58, as used for Solana public keys.

Kept dependency-free on purpose: only 32-byte public keys are encoded and
decoded here, so performance is irrelevant and correctness is easy to test.
"""

from __future__ import annotations

ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {char: index for index, char in enumerate(ALPHABET)}


def b58encode(data: bytes) -> str:
    number = int.from_bytes(data, "big")
    encoded = ""
    while number:
        number, remainder = divmod(number, 58)
        encoded = ALPHABET[remainder] + encoded
    leading_zeros = len(data) - len(data.lstrip(b"\x00"))
    return "1" * leading_zeros + encoded


def b58decode(text: str) -> bytes:
    number = 0
    for char in text:
        if char not in _INDEX:
            raise ValueError("invalid base58 character")
        number = number * 58 + _INDEX[char]
    body = number.to_bytes((number.bit_length() + 7) // 8, "big") if number else b""
    leading_ones = len(text) - len(text.lstrip("1"))
    return b"\x00" * leading_ones + body


def is_valid_pubkey(text: str) -> bool:
    """True when ``text`` is canonical base58 for exactly 32 bytes."""
    try:
        raw = b58decode(text)
    except ValueError:
        return False
    return len(raw) == 32 and b58encode(raw) == text
