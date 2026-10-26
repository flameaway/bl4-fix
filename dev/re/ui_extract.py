"""
Dumps the game's UI files (every path under a `uiresources/` folder) out of its paks, to browse.

    python dev/re/ui_extract.py --oodle <oo2core_*_win64.dll> [--out dev/re/ui_dump] [--filter text]

For each path the pak with the highest patch number wins (like the game), our own ~mods paks are
skipped. Writes `<out>/<path under uiresources>` plus `<out>/_index.json` (path -> pak, size).
Dev only: reads the install, never writes to it.
"""

from __future__ import annotations

import argparse
import json
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "mods" / "bl4_item_cards"))
sys.path.insert(0, str(REPO / "tools"))
import pak  # noqa: E402
from gen_item_cards import extract  # noqa: E402

DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")


def _fstring(data: bytes, pos: int) -> tuple[str, int]:
    (length,) = struct.unpack_from("<i", data, pos)
    pos += 4
    if length == 0:
        return "", pos
    if length < 0:  # UTF-16
        raw = data[pos : pos - length * 2]
        return raw.decode("utf-16-le").rstrip("\0"), pos - length * 2
    return data[pos : pos + length].decode("latin-1").rstrip("\0"), pos + length


def list_entries(pak_path: Path) -> dict[str, int]:
    """Every file in a pak's full directory index: game path -> entry header offset (-1 = deleted)."""
    with pak_path.open("rb") as file:
        size = file.seek(0, 2)
        file.seek(size - pak.FOOTER_SIZE)
        footer = file.read(pak.FOOTER_SIZE)
        magic, version, index_offset, index_size = struct.unpack_from("<IIQQ", footer, 17)
        if magic != pak.PAK_MAGIC or version != pak.PAK_VERSION or footer[16]:
            return {}
        file.seek(index_offset)
        index = file.read(index_size)
        mount, pos = _fstring(index, 0)
        pos += 4 + 8  # entry count, path hash seed
        (has_phi,) = struct.unpack_from("<I", index, pos)
        pos += 4
        if has_phi:
            pos += 8 + 8 + 20
        (has_fdi,) = struct.unpack_from("<I", index, pos)
        pos += 4
        if not has_fdi:
            return {}
        fdi_offset, fdi_size = struct.unpack_from("<QQ", index, pos)
        pos += 8 + 8 + 20
        (encoded_size,) = struct.unpack_from("<I", index, pos)
        pos += 4
        encoded = index[pos : pos + encoded_size]
        pos += encoded_size
        legacy: list[int] = []
        (legacy_count,) = struct.unpack_from("<I", index, pos)
        pos += 4
        for _ in range(legacy_count):
            offset, _size, _usize, method = struct.unpack_from("<QQQI", index, pos)
            pos += 28 + 20
            if method:
                (blocks,) = struct.unpack_from("<I", index, pos)
                pos += 4 + blocks * 16
            pos += 1 + 4
            legacy.append(offset)
        file.seek(fdi_offset)
        fdi = file.read(fdi_size)

    prefix = mount.removeprefix(pak.ENGINE_RELATIVE)
    out: dict[str, int] = {}
    (dirs,) = struct.unpack_from("<I", fdi, 0)
    pos = 4
    for _ in range(dirs):
        dirname, pos = _fstring(fdi, pos)
        (files,) = struct.unpack_from("<I", fdi, pos)
        pos += 4
        for _ in range(files):
            name, pos = _fstring(fdi, pos)
            (location,) = struct.unpack_from("<i", fdi, pos)
            pos += 4
            path = prefix + dirname.lstrip("/") + name
            if location == pak.DELETED_ENTRY:
                out[path] = -1
            elif location < 0:
                out[path] = legacy[-location - 1]
            else:
                out[path] = pak._entry_offset(encoded, location)  # noqa: SLF001
    return out


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oodle", type=Path, required=True)
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME)
    parser.add_argument("--out", type=Path, default=REPO / "dev" / "re" / "ui_dump")
    parser.add_argument("--filter", default="", help="only paths containing this text")
    args = parser.parse_args()

    paks = [p for p in (args.game / "OakGame/Content/Paks").rglob("*.pak") if "~mods" not in p.parts]
    paks.sort(key=pak.patch_number)
    winners: dict[str, tuple[Path, int]] = {}
    for pak_path in paks:  # later (higher patch) paks override earlier ones
        for path, offset in list_entries(pak_path).items():
            low = path.lower()
            if "/uiresources/" in low and args.filter.lower() in low:
                winners[low] = (pak_path, offset, path)  # type: ignore[assignment]

    index = {}
    for low, (pak_path, offset, path) in sorted(winners.items()):  # type: ignore[misc]
        if offset < 0:
            continue
        rel = path[path.lower().index("/uiresources/") + len("/uiresources/") :]
        if "/DLC/" in path:
            rel = "_dlc/" + path.split("/DLC/")[1].split("/")[0] + "/" + rel
        data = extract(pak.PakEntryInfo(pak_path, False, "", offset), args.oodle)
        target = args.out / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        index[rel] = {"pak": pak_path.name, "size": len(data)}
    (args.out / "_index.json").write_text(json.dumps(index, indent=1), encoding="utf-8")
    print(f"{len(index)} files -> {args.out}")


if __name__ == "__main__":
    main()
