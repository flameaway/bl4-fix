"""
Borderlands 4 item serials ("@Ug..."): decode to tokens and encode back, bit for bit.

Plain Python (no SDK imports), so dev tools can use it too.

Format (as documented by the community decoders, e.g. Nicnl's borderlands4-serials; this is our
own implementation):

- "@U" + a base85 text with the alphabet below, read in big-endian groups of 5 characters per 4
  bytes (a short last group gives fewer bytes). Each byte is then bit-reversed, and the result is
  read as a bitstream, most significant bit first.
- The stream starts with the 7 bits 0010000, followed by tokens:
    00   separator "|"         01   separator ","
    100  varint: 4-bit groups (LSB first), each followed by a continuation bit, at most 4 groups
    110  varbit: a 5-bit length (LSB first), then that many value bits (LSB first)
    101  part: varint index, then 1 + varint value + 000      -> {index:value}
                               or 010                         -> {index}
                               or 001, 01, values (100/110 tokens), 00   -> {index:[v ...]}
    111  string: varint length, then 7-bit characters (bit-reversed)
- The stream is zero-padded to a whole byte, which reads as trailing "|" separators.

Items read as `type, 0, 1, level| 2, seed|| {part} {part} ...|`: the first number is the item's
type (which part list the indices point into), and each part token is an index into that list.
"""

from __future__ import annotations

from dataclasses import dataclass, field

ALPHABET = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz!#$%&()*+-;<=>?@^_`{/}~"
_INDEX = {c: i for i, c in enumerate(ALPHABET)}
_PAD = 84  # the last alphabet character, used to pad a short last group
MAGIC = "0010000"
# Header field that holds the item level (`type, 0, 1, level| ...`)
LEVEL_FIELD = 1


def _rev8(b: int) -> int:
    return int(f"{b:08b}"[::-1], 2)


_MIRROR = bytes(_rev8(b) for b in range(256))


# region base85


def _b85_decode(text: str) -> bytes:
    out = bytearray()
    digits = [_INDEX[c] for c in text if c in _INDEX]
    for start in range(0, len(digits), 5):
        group = digits[start : start + 5]
        count = len(group)
        value = 0
        for digit in group + [_PAD] * (5 - count):
            value = value * 85 + digit
        value &= 0xFFFFFFFF
        out += value.to_bytes(4, "big")[: 4 if count == 5 else count - 1]
    return bytes(out)


def _b85_encode(data: bytes) -> str:
    out = []
    for start in range(0, len(data), 4):
        chunk = data[start : start + 4]
        value = int.from_bytes(chunk.ljust(4, b"\0"), "big")
        digits = []
        for _ in range(5):
            digits.append(value % 85)
            value //= 85
        digits.reverse()
        out += [ALPHABET[d] for d in digits[: 5 if len(chunk) == 4 else len(chunk) + 1]]
    return "".join(out)


# endregion
# region Tokens


@dataclass
class Sep:
    hard: bool  # "|" (00) or "," (01)


@dataclass
class Num:
    value: int
    varbit: bool = False
    # Bits the value was stored in (varint: 4-bit groups, varbit: value bits), so unchanged
    # numbers re-encode exactly as they came; None means "smallest"
    width: int | None = None


@dataclass
class Part:
    index: int
    value: int | None = None  # {index:value}
    values: list[Num] | None = None  # {index:[...]}
    index_groups: int | None = None
    value_groups: int | None = None

    def __str__(self) -> str:
        if self.values is not None:
            return f"{{{self.index}:[{' '.join(str(v.value) for v in self.values)}]}}"
        if self.value is not None:
            return f"{{{self.index}:{self.value}}}"
        return f"{{{self.index}}}"


@dataclass
class Text:
    text: str


Token = Sep | Num | Part | Text


@dataclass
class Serial:
    tokens: list[Token] = field(default_factory=list)

    # The header numbers before the first "||" (type, 0, 1, level, 2, seed, ...)
    def numbers(self) -> list[int]:
        return [t.value for t in self.tokens if isinstance(t, Num)]

    @property
    def item_type(self) -> int:
        return self.numbers()[0]

    def header(self) -> dict[int, int]:
        """The numbered fields before the parts: `type, 0, 1, level| 9, 1| 2, seed||` -> {1: level, ...}."""
        nums = []
        for t in self.tokens:
            if isinstance(t, Part):
                break
            if isinstance(t, Num):
                nums.append(t.value)
        return dict(zip(nums[2::2], nums[3::2], strict=False))

    @property
    def level(self) -> int:
        # An item without a level field is level 1 in game
        return self.header().get(LEVEL_FIELD, 1)

    def parts(self) -> list[Part]:
        return [t for t in self.tokens if isinstance(t, Part)]

    def __str__(self) -> str:
        out = []
        for t in self.tokens:
            if isinstance(t, Sep):
                out.append("|" if t.hard else ",")
            elif isinstance(t, Num):
                out.append(f" {t.value}")
            elif isinstance(t, Part):
                out.append(f" {t}")
            else:
                out.append(' "' + t.text.replace("\\", "\\\\").replace('"', '\\"') + '"')
        return "".join(out).strip()


# endregion
# region Bits


class _Reader:
    def __init__(self, bits: str) -> None:
        self.bits = bits
        self.pos = 0

    def take(self, n: int) -> str:
        if self.pos + n > len(self.bits):
            raise ValueError("serial ends early")
        chunk = self.bits[self.pos : self.pos + n]
        self.pos += n
        return chunk

    def left(self) -> int:
        return len(self.bits) - self.pos

    def lsb(self, n: int) -> int:
        return int(self.take(n)[::-1], 2) if n else 0

    def varint(self) -> tuple[int, int]:
        value = shift = groups = 0
        while True:
            value |= self.lsb(4) << shift
            shift += 4
            groups += 1
            if self.take(1) == "0" or groups == 4:
                return value, groups

    def varbit(self) -> tuple[int, int]:
        length = self.lsb(5)
        return self.lsb(length), length

    def token(self) -> str | None:
        if self.left() < 2:
            return None
        head = self.take(2)
        if head in ("00", "01"):
            return head
        if self.left() < 1:
            return None
        return head + self.take(1)


def _lsb(value: int, n: int) -> str:
    return f"{value:0{n}b}"[::-1] if n else ""


def _varint_bits(value: int, groups: int | None) -> str:
    need = max(1, (value.bit_length() + 3) // 4)
    groups = max(need, groups or 0)
    if groups > 4:
        raise ValueError(f"{value} doesn't fit a varint")
    out = []
    for i in range(groups):
        out.append(_lsb((value >> (4 * i)) & 0xF, 4))
        out.append("1" if i < groups - 1 else "0")
    return "".join(out)


def _num_bits(num: Num) -> str:
    if num.varbit:
        width = max(num.value.bit_length(), num.width or 0)
        return "110" + _lsb(width, 5) + _lsb(num.value, width)
    return "100" + _varint_bits(num.value, num.width)


# endregion
# region Decode / encode


def decode(serial: str) -> Serial:
    serial = serial.strip()
    if not serial.startswith("@U"):
        raise ValueError("not an item serial")
    data = _b85_decode(serial[2:]).translate(_MIRROR)
    r = _Reader("".join(f"{b:08b}" for b in data))
    if r.take(7) != MAGIC:
        raise ValueError("unknown serial header")
    tokens: list[Token] = []
    while (kind := r.token()) is not None:
        if kind == "00":
            tokens.append(Sep(hard=True))
        elif kind == "01":
            tokens.append(Sep(hard=False))
        elif kind == "100":
            value, groups = r.varint()
            tokens.append(Num(value, width=groups))
        elif kind == "110":
            value, length = r.varbit()
            tokens.append(Num(value, varbit=True, width=length))
        elif kind == "101":
            index, index_groups = r.varint()
            part = Part(index, index_groups=index_groups)
            if r.take(1) == "1":
                part.value, part.value_groups = r.varint()
                if r.take(3) != "000":
                    raise ValueError("bad part terminator")
            else:
                flag = r.take(2)
                if flag == "01":
                    if r.token() != "01":
                        raise ValueError("bad part list start")
                    part.values = []
                    while (item := r.token()) != "00":
                        if item == "100":
                            value, groups = r.varint()
                            part.values.append(Num(value, width=groups))
                        elif item == "110":
                            value, length = r.varbit()
                            part.values.append(Num(value, varbit=True, width=length))
                        else:
                            raise ValueError(f"bad part list token {item}")
                elif flag != "10":
                    raise ValueError(f"bad part flag {flag}")
            tokens.append(part)
        elif kind == "111":
            length, _ = r.varint()
            chars = [r.take(7)[::-1] for _ in range(length)]
            tokens.append(Text("".join(chr(int(c, 2)) for c in chars)))
        else:
            raise ValueError(f"bad token {kind}")
    # Byte padding reads as extra "|"; keep one
    while len(tokens) >= 2 and isinstance(tokens[-1], Sep) and tokens[-1].hard and isinstance(tokens[-2], Sep) and tokens[-2].hard:
        tokens.pop()
    return Serial(tokens)


def encode(serial: Serial) -> str:
    bits = [MAGIC]
    for t in serial.tokens:
        if isinstance(t, Sep):
            bits.append("00" if t.hard else "01")
        elif isinstance(t, Num):
            bits.append(_num_bits(t))
        elif isinstance(t, Part):
            bits.append("101" + _varint_bits(t.index, t.index_groups))
            if t.values is not None:
                bits.append("001" + "01" + "".join(_num_bits(v) for v in t.values) + "00")
            elif t.value is not None:
                bits.append("1" + _varint_bits(t.value, t.value_groups) + "000")
            else:
                bits.append("010")
        else:
            bits.append("111" + _varint_bits(len(t.text), None) + "".join(_lsb(ord(c), 7) for c in t.text))
    stream = "".join(bits)
    stream += "0" * (-len(stream) % 8)
    data = bytes(int(stream[i : i + 8], 2) for i in range(0, len(stream), 8)).translate(_MIRROR)
    return "@U" + _b85_encode(data)


# endregion
