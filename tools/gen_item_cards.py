"""
Regenerates the detailed item card files shipped in mods/bl4_item_cards/card_page/ (dev only).

    python tools/gen_item_cards.py --oodle <path to an oo2core_*_win64.dll>

BL4's UI is HTML (Coherent Gameface). The item card's markup is item_card/item_card.html inside the
game's paks. Since 0.2.0 the mod no longer ships an edited copy of it in a pak: item_cards.js adds
what it needs to the card at runtime (it runs through the BL4 UI Link). This writes:

- card_page/vanilla.json     the SHA1 Unreal stores for the game's copy of the item card, which the
                             script was written against. The mod only runs the script while the
                             game still ships exactly that copy, so a game patch that touches the
                             item card pauses the feature ("Needs update") instead of misdrawing.

- card_page/skills.json      every skill tree node's name, max rank and tooltip lines, from the
                             game's NCS data (needs tools/ncs_parse.py). The mod evaluates the
                             numbers in game; vanilla.json also records which data files it came
                             from, and the mod switches the numbers off if they change.
- card_page/firmware.json    every firmware's three bonus tiers (pieces needed, text, the
                             attribute behind each number), evaluated in game the same way.

Pak data is Oodle-compressed and Oodle is linked into the game exe, not shipped as a DLL, so this
needs an oo2core DLL from somewhere else (other games ship one); `extract` also takes no DLL and
then uses pyooz. The mod itself never decompresses anything; it only compares the stored SHA1.

"""

from __future__ import annotations

import argparse
import ctypes
import json
import re
import struct
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MOD_DIR = REPO / "mods" / "bl4_item_cards"
OUT_DIR = MOD_DIR / "card_page"
DEFAULT_GAME = Path(r"C:\Program Files (x86)\Steam\steamapps\common\Borderlands 4")
CARD_PATH = "OakGame/Content/uiresources/item_card/item_card.html"

sys.path.insert(0, str(MOD_DIR))
sys.path.insert(0, str(REPO / "mods" / "bl4_direct"))
import pak  # noqa: E402  # mods/bl4_item_cards/pak.py and bl4_direct/shared.py are plain Python, no SDK imports
import shared  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_drops  # noqa: E402
import ncs_parse  # noqa: E402

# region Extraction


def oodle_decompress(oodle_dll: Path | None, data: bytes, size: int) -> bytes:
    """
    With no DLL: ooz (`pip install pyooz`, GPL-3.0, a build-time tool only), an open-source decoder
    for the Kraken and Leviathan blocks BL4's paks use.
    """
    if oodle_dll is None:
        try:
            import ooz  # noqa: PLC0415
        except ImportError:
            raise SystemExit("no Oodle decompressor: pip install pyooz, or pass --oodle <oo2core_*_win64.dll>") from None
        got = ooz.decompress(data, size)
        if len(got) != size:
            raise SystemExit(f"ooz failed (got {len(got)} of {size} bytes)")
        return got
    oodle = ctypes.WinDLL(str(oodle_dll))
    buffer = ctypes.create_string_buffer(size)
    got = oodle.OodleLZ_Decompress(data, len(data), buffer, size, 0, 0, 0, None, 0, None, None, None, 0, 3)
    if got != size:
        raise SystemExit(f"Oodle failed (got {got} of {size} bytes)")
    return buffer.raw


def extract(entry: pak.PakEntryInfo, oodle_dll: Path | None) -> bytes:
    with entry.pak.open("rb") as file:
        file.seek(entry.offset)
        _offset, size, usize, method = struct.unpack("<QQQI", file.read(28))
        file.read(20)  # SHA1
        blocks: list[tuple[int, int]] = []
        if method:
            (count,) = struct.unpack("<I", file.read(4))
            blocks = [struct.unpack("<QQ", file.read(16)) for _ in range(count)]
        _flags, block_size = struct.unpack("<BI", file.read(5))
        if not method:
            return file.read(size)

        out = bytearray()
        for start, end in blocks:
            # v11 block offsets are relative to the entry's header
            file.seek(entry.offset + start)
            compressed = file.read(end - start)
            out += oodle_decompress(oodle_dll, compressed, min(block_size or usize, usize - len(out)))
        return bytes(out)


def extract_ncs(entry: pak.PakEntryInfo, oodle_dll: Path) -> bytes:
    """
    An NCS ("Nexus Config Store") game data file, decompressed.

    After the pak's own compression there's a second container: a 16-byte header ("\x01NCS", u32,
    u32 decompressed size, u32 size), then a block header whose big-endian fields at 0x1B (log2 of
    the block size) and 0x1C (block count) matter, block sizes from 0x50, then the Oodle blocks.
    """
    data = extract(entry, oodle_dll)
    if data[1:4] != b"NCS":
        raise SystemExit(f"{entry.pak.name}: not an NCS file")
    (size,) = struct.unpack_from("<I", data, 8)
    block = 1 << data[0x1B]
    (count,) = struct.unpack_from(">I", data, 0x1C)
    sizes = [struct.unpack_from(">I", data, 0x50 + 4 * i)[0] for i in range(count)]
    pos = 0x50 + 4 * count
    out = bytearray()
    for stored in sizes:
        want = min(block, size - len(out))
        chunk = data[pos : pos + stored]
        out += chunk if stored == want else oodle_decompress(oodle_dll, chunk, want)
        pos += stored
    return bytes(out)


# endregion
# region Skill data

NCS_PATH = "Engine/Content/_NCS/Nexus-Data-{name}{chunk}.ncs"
_REF = re.compile(r"^\w+'(.*)'$")
# Display flags of a tooltip number that the mod's formatter understands
NUMBER_FLAGS = (
    "bdisplayaspercentage", "bdisplayplussign", "floatprecision", "roundingmode", "signstyle",
    "bdisplayasoneminus",
)  # fmt: skip


def _deref(value: str) -> str:
    """"skill'SkillDef_X'" -> "SkillDef_X"."""
    match = _REF.match(value)
    return match.group(1) if match else value


def _entries(paks: list[Path], name: str, oodle_dll: Path) -> tuple[list[dict], dict[str, str]]:
    """
    Every entry ({"key", "value", "deps"?}) of one NCS data type, across all chunks (base game
    and DLC; path lookups ignore case, and the game isn't consistent about it).

    Also returns {file path: SHA1} for the files used, so the mod can tell when they change.
    """
    entries: list[dict] = []
    fingerprints: dict[str, str] = {}
    for chunk in range(10):
        path = NCS_PATH.format(name=name, chunk=chunk)
        entry = pak.winning_entry(paks, path)
        if entry is None:
            continue
        fingerprints[path] = entry.sha1
        doc = ncs_parse.parse(extract_ncs(entry, oodle_dll))
        for table in doc["tables"].values():
            for record in table["records"]:
                entries.extend(record["entries"])
    return entries, fingerprints


def _records(paks: list[Path], name: str, oodle_dll: Path) -> tuple[dict[str, dict], dict[str, str]]:
    """Every record of one NCS data type, keyed by lowercase name (see _entries)."""
    entries, fingerprints = _entries(paks, name, oodle_dll)
    records = {item["key"].lower(): item["value"] for item in entries if isinstance(item.get("value"), dict)}
    return records, fingerprints


def _graph_nodes(graphs: dict[str, dict], name: str) -> dict[str, dict]:
    """
    A skill tree graph's nodes, in the order the game indexes them.

    Graphs are stored as changes to a parent: an entry with an empty value removes the parent's
    node with the same id, one without a key patches fields of that node, the rest add nodes.
    """
    graph = graphs.get(name.lower())
    if graph is None:
        return {}
    nodes = dict(_graph_nodes(graphs, _deref(graph["parent"]))) if graph.get("parent") else {}
    pairs = (graph.get("nodes") or {}).get("pairs") or {}
    for pair_id, pair in pairs.items():
        if pair == "":
            nodes.pop(pair_id, None)
        elif isinstance(pair, dict) and "key" not in pair and pair_id in nodes:
            base = nodes[pair_id]
            nodes[pair_id] = {"key": base["key"], "value": _merged(base.get("value"), pair.get("value"))}
        elif isinstance(pair, dict):
            nodes[pair_id] = pair
    return nodes


def _merged(base: object, patch: object) -> object:
    if isinstance(base, dict) and isinstance(patch, dict):
        return {**base, **{key: _merged(base.get(key), value) for key, value in patch.items()}}
    return base if patch in (None, "") else patch


def _display_line(stat: dict) -> dict:
    """A tooltip line: its text with {placeholders}, and the attribute behind each number."""
    args = {}
    for pair in ((stat.get("argsmap") or {}).get("pairs") or {}).values():
        if not isinstance(pair, dict):
            continue
        number = pair["value"]
        if "attributedef" not in number:
            continue
        args[pair["key"]] = {
            "attribute": _deref(number["attributedef"]),
            # The game's "next level" preview; the mod shows its own comparison instead
            "preview": number.get("bshowmodifierdelta") == "true",
            **{flag: number[flag] for flag in NUMBER_FLAGS if flag in number},
        }
    return {"text": ncs_parse.loc_text(stat["formattext"]), "args": args}


def _stat_lines(tooltip: dict) -> list[dict]:
    return [
        _display_line(stat)
        for stat in tooltip.get("stats") or []
        if isinstance(stat, dict) and "formattext" in stat
    ]


def build_skills(paks: list[Path], oodle_dll: Path) -> tuple[dict, dict[str, str]]:
    """
    {graph name: [node, ...]} in the game's node order, where a node is {"id", "name", "max",
    "stats"} or null for nodes that aren't skills. Tooltips are matched by name: SkillDef_X has
    ToolTip_X (true for 170 of 178 skills; the rest are action skills and traits).
    """
    graphs, fingerprints = _records(paks, "progress_graph", oodle_dll)
    tooltips, tooltip_prints = _records(paks, "uitooltipdata", oodle_dll)
    fingerprints.update(tooltip_prints)

    skills: dict[str, list] = {}
    for key, graph in graphs.items():
        name = graph.get("progress_graph") or key
        nodes: list[dict | None] = []
        for pair in _graph_nodes(graphs, name).values():
            data = pair["value"] if isinstance(pair.get("value"), dict) else {}
            skill = _deref((data.get("itemdata") or {}).get("skill", ""))
            if not skill:
                nodes.append(None)
                continue
            tooltip = tooltips.get(("tooltip_" + skill.removeprefix("SkillDef_")).lower())
            header = tooltip.get("header") if tooltip else None
            nodes.append({
                "id": pair.get("key", ""),
                "name": ncs_parse.loc_text(header) if header else data.get("alias", skill),
                "max": int(data.get("maxprogresspoints") or 0),
                "stats": _stat_lines(tooltip) if tooltip else [],
            })
        if any(nodes):
            skills[name] = nodes
    return skills, fingerprints


def build_firmware(paks: list[Path], oodle_dll: Path) -> tuple[dict, dict[str, str]]:
    """
    {firmware: {"name", "tiers": [{"count", "lines"}]}}. Each tier is unlocked by having that many
    equipped items with the firmware (firmwarecount, 1 when absent) and described by a ui_stat whose
    numbers are attributes, like skill tooltips.
    """
    firmware, fingerprints = _records(paks, "firmware", oodle_dll)
    ui_stats, stat_prints = _records(paks, "ui_stat", oodle_dll)
    fingerprints.update(stat_prints)

    out: dict[str, dict] = {}
    for key, record in sorted(firmware.items()):
        tiers = []
        for tier in record.get("bonusaugmentdefs") or []:
            stat = ui_stats.get(_deref(tier.get("uistat", "")).lower())
            text = (stat or {}).get("statvalue") or {}
            if "formattext" not in text:
                continue
            line = _display_line(text)
            # "Damage Taken is reduced by {mod}": the words give the direction, so show the size.
            # Most such numbers say so (signstyle Positive); a few don't and would read "-30%".
            for name, arg in line["args"].items():
                if re.search(r"\bby\W+(?:\[\w+\])?\{" + re.escape(name) + r"\}", line["text"]):
                    arg.setdefault("signstyle", "Positive")
            tiers.append({"count": int(tier.get("firmwarecount") or 1), "lines": [line]})
        if tiers and record.get("name"):
            out[record.get("firmware") or key] = {"name": ncs_parse.loc_text(record["name"]), "tiers": tiers}
    return out, fingerprints


# endregion



def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--oodle", type=Path, required=True, help="path to an oo2core_*_win64.dll")
    parser.add_argument("--game", type=Path, default=DEFAULT_GAME, help="Borderlands 4 install folder")
    args = parser.parse_args()

    paks = sorted((args.game / "OakGame" / "Content" / "Paks").glob("*.pak"))
    entry = pak.winning_entry(paks, CARD_PATH)
    if entry is None:
        raise SystemExit(f"{CARD_PATH} not found in {args.game}")

    OUT_DIR.mkdir(exist_ok=True)
    # Only the page's fingerprint is kept (vanilla.json below); an edited copy from before 0.2.0
    # goes
    (OUT_DIR / "item_card.html").unlink(missing_ok=True)
    skills, skill_prints = build_skills(paks, args.oodle)
    (OUT_DIR / "skills.json").write_text(json.dumps(skills, indent=1) + "\n", encoding="utf-8")
    print(f"skills: {sum(1 for nodes in skills.values() for n in nodes if n)} in {len(skills)} graphs")
    firmware, firmware_prints = build_firmware(paks, args.oodle)
    (OUT_DIR / "firmware.json").write_text(json.dumps(firmware, indent=1) + "\n", encoding="utf-8")
    print(f"firmware: {len(firmware)}")
    loaded: dict[str, tuple[list[dict], dict[str, str]]] = {}

    def load(name: str) -> tuple[list[dict], dict[str, str]]:
        if name not in loaded:
            loaded[name] = _entries(paks, name, args.oodle)
        return loaded[name]

    drops, machines, drop_prints = gen_drops.build_drops(load)
    (OUT_DIR / "encore.json").write_text(json.dumps(machines, indent=1) + "\n", encoding="utf-8")
    (OUT_DIR / "drops.json").write_text(json.dumps(drops, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"legendary names: {len(drops)}, with a boss: {sum(1 for v in drops.values() if any(e['sources'] for e in v))}")
    fingerprint = {
        "path": CARD_PATH,
        "sha1": entry.sha1,
        "source_pak": entry.pak.name,
        "game_build": shared.game_build_id(args.game),
        # The game data skills.json came from; skill numbers turn off if any of these change
        "skill_data": skill_prints,
        # The game data drops.json came from; drop info turns off if any of these change
        "drop_data": drop_prints,
        # The game data firmware.json came from; firmware details turn off if any of these change
        "firmware_data": firmware_prints,
    }
    (OUT_DIR / "vanilla.json").write_text(json.dumps(fingerprint, indent=4) + "\n", encoding="utf-8")
    print(f"item card page: {entry.pak.name} ({entry.sha1})")


if __name__ == "__main__":
    main()
