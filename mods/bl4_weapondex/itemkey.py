"""
Which item an item serial ("@Ug...") is: its item type and composition (serial indices), which is
enough to find the legendary in dex.json. Reads only up to the first part; the format is the one
bl4_reroll/serial.py decodes in full (base85, mirrored bytes, a token bit stream):
`type, 0, 1, level| ... || {composition} {part} ...|`.

Plain Python (no SDK imports), so it runs on the save-reading worker thread.
"""

# SPDX-License-Identifier: AGPL-3.0
# This file follows bl4_reroll/serial.py, a Python rewrite of the Go serial decoder in
# Nicnl's borderlands4-serials (https://github.com/Nicnl/borderlands4-serials, by Nicnl and its
# contributors), which is licensed AGPL-3.0. So this file is licensed under the GNU Affero General
# Public License v3.0 (LICENSE-AGPL in this mod), not the GPL-3.0 that covers the rest of the mod.
# Modified by flameaway, first in 2026-10 (changes since in git history).

from __future__ import annotations

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{/}~"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
_PAD = 84
_MIRROR = bytes(int(f"{b:08b}"[::-1], 2) for b in range(256))
MAGIC = "0010000"


def _bits(text: str) -> str:
    out = bytearray()
    digits = [_INDEX[c] for c in text if c in _INDEX]
    for start in range(0, len(digits), 5):
        group = digits[start : start + 5]
        value = 0
        for digit in group + [_PAD] * (5 - len(group)):
            value = value * 85 + digit
        out += (value & 0xFFFFFFFF).to_bytes(4, "big")[: 4 if len(group) == 5 else len(group) - 1]
    return "".join(f"{b:08b}" for b in bytes(out).translate(_MIRROR))


class _Reader:
    def __init__(self, bits: str) -> None:
        self.bits, self.pos = bits, 0

    def take(self, n: int) -> str:
        if self.pos + n > len(self.bits):
            raise ValueError("serial ends early")
        self.pos += n
        return self.bits[self.pos - n : self.pos]

    def lsb(self, n: int) -> int:
        return int(self.take(n)[::-1], 2) if n else 0

    def varint(self) -> int:
        value = shift = groups = 0
        while True:
            value |= self.lsb(4) << shift
            shift, groups = shift + 4, groups + 1
            if self.take(1) == "0" or groups == 4:
                return value


def type_and_comp(serial: str) -> tuple[int, int] | None:
    """(item type, composition) of a serial, or None if it isn't one we can read."""
    try:
        serial = serial.strip()
        if not serial.startswith("@U"):
            return None
        r = _Reader(_bits(serial[2:]))
        if r.take(7) != MAGIC:
            return None
        item_type: int | None = None
        while r.pos + 2 <= len(r.bits):
            head = r.take(2)
            if head in ("00", "01"):
                continue
            kind = head + r.take(1)
            if kind == "100":
                value = r.varint()
            elif kind == "110":
                value = r.lsb(r.lsb(5))
            elif kind == "101":
                index = r.varint()
                return (item_type, index) if item_type is not None else None
            else:
                return None
            if item_type is None:
                item_type = value
    except (ValueError, KeyError):
        return None
    return None
