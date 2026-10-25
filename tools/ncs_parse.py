"""
Parser for Borderlands 4 NCS ("Nexus Config Store") payloads (dev only).

    python tools/ncs_parse.py <file.bin> [--json out.json] [--find NAME]

Input is a *decompressed* NCS payload (the outer `[ver]NCS` header and Oodle container already
stripped). Output is plain dicts/lists/strings, so it dumps straight to JSON.

Format reference: monokrome/bl4 `bl4-ncs` crate (BSD-2-Clause), reimplemented here from reading it.

Payload layout (all integers little-endian):

    blob header   u32 entry_count, u32 flags, u32 string_bytes, u32 reserved(=0)
    table names   string_bytes of NUL-terminated strings; index 0 is "" (the terminator id)
    type codes    u8 n, u16 type_count, n ASCII codes (e.g. "abefijm")
    type matrix   type_count rows x n bits, byte-aligned; each row's low 2 bits pick the node
                  kind (0 null, 1 leaf, 2 array, 3 map), bit 7 (code 'h') means "carries a key"
    3 string pools  each u32 count, u32 flags, u64 byte_len, NUL-terminated strings:
                  value strings, value kinds (UE class names for typed leaves), key strings
    bitstream     LSB-first packed bits, see _decode_tables

Index fields in the bitstream are ceil(log2(count)) bits wide (min 1) for their pool. Each table
carries two remap arrays (keys, values): when present, indices are local to the table and are
mapped through the array to pool indices, with the width taken from the array length instead.

The other type-code columns don't change how a node is read. Observed meanings: 'i' struct-type
leaf, 'm' object reference, 'l' localized text ("namespace, key, source"; see loc_text), 'f' an
emptied value. Records are deltas over a `parent` record; map entries named pairNNNN with an
emptied value remove the parent's entry of that id.
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from pathlib import Path
from typing import Any

# Type-code letters map to fixed flag bits; the matrix columns are listed in the payload.
_TYPE_BIT = {c: i for i, c in enumerate("abcdefghijkl")} | {"m": 24}

KIND_NULL, KIND_LEAF, KIND_ARRAY, KIND_MAP = range(4)


class NcsError(ValueError):
    pass


def _bit_width(count: int) -> int:
    return max(1, (count - 1).bit_length()) if count >= 2 else 1


class _Bits:
    """LSB-first bit reader over a byte buffer."""

    def __init__(self, data: bytes, start: int = 0) -> None:
        self.data = data
        self.pos = start * 8
        self.end = len(data) * 8

    def read(self, n: int) -> int:
        if self.pos + n > self.end:
            raise NcsError(f"bitstream overrun at bit {self.pos} (+{n})")
        b, off = self.pos >> 3, self.pos & 7
        v = int.from_bytes(self.data[b : b + ((off + n + 7) >> 3)], "little")
        self.pos += n
        return (v >> off) & ((1 << n) - 1)

    def has(self, n: int) -> bool:
        return self.pos + n <= self.end

    def align(self) -> None:
        self.pos = (self.pos + 7) & ~7


def _cstrings(block: bytes) -> list[str]:
    parts = block.split(b"\0")
    if parts and parts[-1] == b"":
        parts.pop()
    return [p.decode("utf-8", "replace") for p in parts]


def _read_pool(data: bytes, pos: int) -> tuple[list[str], int, int]:
    """String pool: (strings padded/truncated to the declared count, declared count, next pos)."""
    count, _flags, size = struct.unpack_from("<IIQ", data, pos)
    pos += 16
    if pos + size > len(data):
        raise NcsError("string pool runs past end of data")
    strings = _cstrings(data[pos : pos + size])[:count]
    strings += [""] * (count - len(strings))
    return strings, count, pos + size


class _Remap:
    """Per-table index remap: 24-bit count, 8-bit value width, then `count` values."""

    def __init__(self, bits: _Bits) -> None:
        count, width = bits.read(24), bits.read(8)
        self.values: list[int] = []
        if count and width:
            if width > 32:  # never seen; skip rather than misread
                bits.pos += count * width
            else:
                self.values = [bits.read(width) for _ in range(count)]
        self.width = _bit_width(len(self.values))

    def __bool__(self) -> bool:
        return bool(self.values)


class _Decoder:
    def __init__(self, data: bytes) -> None:
        if len(data) < 16:
            raise NcsError("too short for an NCS payload")
        _entries, _flags, str_bytes, reserved = struct.unpack_from("<4I", data)
        if reserved != 0:
            raise NcsError("bad blob header (reserved != 0); is this the decompressed payload?")
        pos = 16
        self.table_names = _cstrings(data[pos : pos + str_bytes])
        pos += str_bytes

        ncodes, ntypes = data[pos], struct.unpack_from("<H", data, pos + 1)[0]
        pos += 3
        self.type_codes = data[pos : pos + ncodes].decode("ascii")
        pos += ncodes
        matrix = _Bits(data, pos)
        self.node_kinds: list[tuple[int, bool]] = []
        for _ in range(ntypes):
            flags = 0
            for col, ch in enumerate(self.type_codes):
                if matrix.read(1):
                    flags |= 1 << _TYPE_BIT.get(ch, col)
            kind = flags & 3
            self.node_kinds.append((kind, kind == KIND_MAP or bool(flags & 0x80)))
        pos += (ncodes * ntypes + 7) // 8

        self.values, nvalues, pos = _read_pool(data, pos)
        self.kinds, nkinds, pos = _read_pool(data, pos)
        self.keys, nkeys, pos = _read_pool(data, pos)

        self.table_bits = _bit_width(len(self.table_names))
        self.value_bits = _bit_width(nvalues)
        self.kind_bits = _bit_width(nkinds)
        self.key_bits = _bit_width(nkeys)
        self.type_bits = _bit_width(ntypes)
        self.bits = _Bits(data, pos)

        # Per-table state, set in decode().
        self.key_remap: _Remap | None = None
        self.value_remap: _Remap | None = None
        self.deps: list[str] = []
        self.dep_bits = 0
        self.rec_end = 0

    # -- primitives ---------------------------------------------------------------------------

    def key(self) -> str:
        r = self.key_remap
        if r:
            raw = self.bits.read(r.width)
            idx = r.values[raw] if raw < len(r.values) else raw
        else:
            idx = self.bits.read(self.key_bits)
        return self.keys[idx] if idx < len(self.keys) else f"<key:{idx}>"

    def leaf(self) -> str:
        r = self.value_remap
        if r:
            raw = self.bits.read(r.width)
            kind = self.bits.read(self.kind_bits)
            idx = r.values[raw] if raw < len(r.values) else raw
        else:
            idx = self.bits.read(self.value_bits)
            kind = self.bits.read(self.kind_bits)
        value = self.values[idx] if idx < len(self.values) else ""
        kind_name = self.kinds[kind] if kind < len(self.kinds) else ""
        # Typed leaves (object/class refs) use Unreal's  Class'Path'  text form.
        return f"{kind_name}'{value}'" if kind_name else value

    def node(self) -> Any:
        """A tree node: type index, optional self key, then a null/leaf/array/map body."""
        t = self.bits.read(self.type_bits)
        kind, carries_key = self.node_kinds[t] if t < len(self.node_kinds) else (KIND_NULL, False)
        self_key = self.key() if carries_key else ""
        value: Any
        if kind == KIND_LEAF:
            value = self.leaf()
        elif kind == KIND_ARRAY:
            value = []
            while self.bits.pos < self.rec_end and self.bits.read(1):
                value.append(self.node())
        elif kind == KIND_MAP:
            value = {}
            while self.bits.pos < self.rec_end and self.bits.read(1):
                k = self.key()
                value[k] = self.node()
        else:
            value = None
        if self_key and self_key.lower() != "none":
            return {self_key: value}
        return value

    def op_value(self, op: int) -> Any:
        # 2-bit opcode: 1 = null, 2 = inline node, 3 = reference by name
        if op == 2:
            return self.node()
        if op == 3:
            return {"$ref": self.key()}
        return None

    def name_list(self) -> list[str]:
        out: list[str] = []
        for _ in range(4096):
            s = self.key()
            if not s or s.lower() == "none":
                break
            out.append(s)
        return out

    # -- records ------------------------------------------------------------------------------

    def tags(self) -> list[dict[str, Any]]:
        """Record header tags, one ASCII byte each, until 'z'."""
        bits, out = self.bits, []
        while bits.pos + 8 <= self.rec_end:
            tag = chr(bits.read(8))
            if tag == "z":
                break
            if tag == "a":
                out.append({"a": self.key()})
            elif tag == "b":
                out.append({"b": bits.read(32)})
            elif tag == "c":
                out.append({"c": struct.unpack("<f", struct.pack("<I", bits.read(32)))[0]})
            elif tag in "def":
                out.append({tag: self.name_list()})
            elif tag == "p":
                out.append({"p": self.node()})
            else:
                out.append({"?": ord(tag)})
                break
        return out

    def entries(self) -> list[dict[str, Any]]:
        bits, out = self.bits, []
        while bits.pos + 2 <= self.rec_end:
            op = bits.read(2)
            if op == 0:
                break
            entry: dict[str, Any] = {"key": self.key()}
            entry["value"] = self.op_value(op)
            deps = self.dep_entries()
            if deps:
                entry["deps"] = deps
            out.append(entry)
        return out

    def dep_entries(self) -> list[dict[str, Any]]:
        """Sub-entries that belong to one of the table's dependency tables (e.g. item parts)."""
        if not self.deps:
            return []
        bits, out = self.bits, []
        while bits.pos + 2 <= self.rec_end:
            op = bits.read(2)
            if op == 0:
                break
            key = self.key()
            di = bits.read(self.dep_bits)
            out.append(
                {
                    "table": self.deps[di] if di < len(self.deps) else f"<dep:{di}>",
                    "key": key,
                    "value": self.op_value(op),
                }
            )
        return out

    def records(self) -> list[dict[str, Any]]:
        """Byte-aligned records, each prefixed by its u32 byte length (0 ends the table)."""
        bits, out = self.bits, []
        while True:
            bits.align()
            if not bits.has(32):
                break
            start = bits.pos
            length = bits.read(32)
            if length == 0:
                if bits.has(8):
                    bits.read(8)
                break
            self.rec_end = start + length * 8
            if self.rec_end > bits.end:
                raise NcsError(f"record at byte {start // 8} runs past end of data")
            try:
                rec: dict[str, Any] = {"tags": self.tags(), "entries": self.entries()}
            except (NcsError, IndexError) as e:
                rec = {"error": str(e)}
            bits.pos = self.rec_end
            out.append(rec)
        return out

    def decode(self) -> dict[str, Any]:
        bits = self.bits
        tables: dict[str, Any] = {}
        while bits.has(self.table_bits):
            tid = bits.read(self.table_bits)
            if tid == 0:
                break
            if tid >= len(self.table_names):
                raise NcsError(f"table id {tid} out of range")
            name = self.table_names[tid]
            deps: list[str] = []
            while bits.has(self.table_bits):
                did = bits.read(self.table_bits)
                if did == 0:
                    break
                deps.append(self.table_names[did] if did < len(self.table_names) else f"<{did}>")
            self.key_remap, self.value_remap = _Remap(bits), _Remap(bits)
            self.deps = deps
            self.dep_bits = _bit_width(len(deps)) if deps else 0
            bits.align()
            tables[name] = {"deps": deps, "records": self.records()}
        return {"type_codes": self.type_codes, "tables": tables}


def parse(data: bytes) -> dict[str, Any]:
    """Parse a decompressed NCS payload into {"type_codes", "tables": {name: {deps, records}}}.

    Each record is {"tags": [...], "entries": [{"key", "value", "deps"?}]}. Values are None,
    str (leaf), list, dict, or {"$ref": name}. Leaf values are kept as text, as the game stores
    them (numbers included), and typed leaves read  Kind'Value'.
    """
    return _Decoder(data).decode()


def find(doc: dict[str, Any], name: str) -> list[dict[str, Any]]:
    """All entries named `name` (case-insensitive) across the document's tables."""
    name = name.lower()
    return [
        {"table": tname, **e}
        for tname, table in doc["tables"].items()
        for rec in table["records"]
        for e in rec.get("entries", ())
        if e["key"].lower() == name
    ]


_LOC_KEY = re.compile(r"^(?:[0-9A-Fa-f]{32}|\w+)$")
_LOC_NAMESPACE = re.compile(r"^\w+$")


def loc_text(s: str) -> str:
    """
    Display text of a localized leaf, "namespace, key, source text"; others unchanged. The key is
    a 32-hex id, or a string table key like Name_TKBoss ("npcs, Name_TKBoss, The Timekeeper").
    """
    parts = s.split(", ", 2)
    if len(parts) == 3 and _LOC_NAMESPACE.match(parts[0]) and _LOC_KEY.match(parts[1]):
        return parts[2]
    return s


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    ap.add_argument("file", type=Path, help="decompressed NCS payload")
    ap.add_argument("--json", type=Path, help="write the full structure here instead of stdout")
    ap.add_argument("--find", metavar="NAME", help="only print entries with this key")
    args = ap.parse_args(argv)

    doc = parse(args.file.read_bytes())
    out: Any = find(doc, args.find) if args.find else doc
    text = json.dumps(out, indent=2, ensure_ascii=False)
    if args.json:
        args.json.write_text(text, encoding="utf-8")
    else:
        sys.stdout.reconfigure(encoding="utf-8")
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
