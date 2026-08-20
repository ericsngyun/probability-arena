"""Minimal base58 (Bitcoin alphabet) decoder.

Solana instruction `data` arrives base58-encoded under `encoding=jsonParsed`
for programs the RPC has no parser for — ComputeBudget among them, which is
exactly the program whose operands we need to separate a priority fee from a
base fee.

Vendored rather than depended on: the repo has no base58 dependency, this is
20 lines, and adding a package to read 9 bytes is not a trade worth making.
Round-trip tested against pinned real instruction data in the fixture suite.
"""

from __future__ import annotations

_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_INDEX = {c: i for i, c in enumerate(_ALPHABET)}


def b58decode(value: str) -> bytes:
    """Decode base58 to bytes. Raises ValueError on any non-alphabet char —
    silently skipping one would shift every subsequent operand."""
    if value == "":
        return b""
    num = 0
    for char in value:
        digit = _INDEX.get(char)
        if digit is None:
            raise ValueError(f"invalid base58 character {char!r}")
        num = num * 58 + digit
    # leading '1's are leading zero bytes
    leading = 0
    for char in value:
        if char != "1":
            break
        leading += 1
    body = num.to_bytes((num.bit_length() + 7) // 8, "big") if num else b""
    return b"\x00" * leading + body


def b58encode(raw: bytes) -> str:
    if not raw:
        return ""
    num = int.from_bytes(raw, "big")
    out = ""
    while num > 0:
        num, rem = divmod(num, 58)
        out = _ALPHABET[rem] + out
    leading = 0
    for byte in raw:
        if byte != 0:
            break
        leading += 1
    return "1" * leading + out
