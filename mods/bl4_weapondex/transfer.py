"""
Take: a copy another character holds (in their backpack or equipped) moves to the character being
played. The user's ask (2026-11-06), with their rules: never lose the item (it's given to you
first; only once it's in your backpack is it taken out of the other character's save), equipped
gear may be taken too, and a full backup of the saves comes first while it's being tested.

The other character isn't loaded, so their save (<n>.sav) is only a file: we decrypt it, take the
one item out of the YAML text and write it back the way the game writes it (savegame.encrypt; its
layout checked on the save first, savegame.same_form). The rest of the text stays byte for byte as
it was:
- a backpack item is its `slot_N` block in `state.inventory.items.backpack`; the slots after it
  are renumbered (the game's saves always number them 0..n-1 with no gaps);
- an equipped item is also in the backpack (with `flags: 1`) and again under
  `inventory.equipped_inventory.equipped` as `slot_K` (an equip slot): both go. A missing equip
  slot is a form the game writes itself (a low level character's locked slots).
A copy that's in the backpack unequipped is taken before an equipped one with the same serial.

Steps: (worker) read and decrypt the save, find the item, make the new text and check it holds the
same items minus that one; (game thread) give it to you as a one-item reward package (Reroll's way:
mods/bl4_reroll/game.py) and wait until that serial is in your backpack; (worker) check the save
file is still the one read, copy it to settings/kairosdex_transfers/, write the new one (a temp
file, then a rename), read it back and check it decrypts to the new text. A failure before the
item arrives changes nothing; one after it leaves a duplicate, never a loss.

The bank lives in profile.sav, which the running game holds and writes itself: a bank copy is
moved live instead, with the game's own internal add and remove (bank.py).
"""

from __future__ import annotations

import json
import re
import shutil
import threading
import time
from collections import Counter
from pathlib import Path
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, get_pc
from unrealsdk import logging
from unrealsdk.unreal import FGbxDefPtr

from . import bank, collection, owned, progress, savegame

BACKUPS = SETTINGS_DIR / "kairosdex_transfers"
# A reward package holding one random item and nothing else (a side mission's reward): its item's
# serial is swapped for ours before it's opened
CARRIER_REWARD = "Reward_ItemPool_AAAForEffort"
ARRIVE_S = 3.0  # the item should be in the backpack the frame the package opens; this is slack

_BACKPACK = re.compile(r"^      backpack: *$")
_EQUIPPED = re.compile(r"^      equipped: *$")
_SLOT = re.compile(r"^        slot_(\d+): *$")


class TransferError(Exception):
    pass


class S:
    job: dict[str, Any] | None = None  # the transfer under way
    on_done: Any = None  # screen.say: the result for the player


def busy() -> bool:
    return S.job is not None


# region The save's text (pure: tested on copies of the saves)


def _indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _section(lines: list[str], header: re.Pattern[str], after: int = 0) -> tuple[int, int]:
    """The lines under the first line matching header (from `after`): (first, end)."""
    for i in range(after, len(lines)):
        if header.match(lines[i]):
            depth = _indent(lines[i])
            end = i + 1
            while end < len(lines) and (not lines[end].strip() or _indent(lines[end]) > depth):
                end += 1
            return i + 1, end
    raise TransferError("the save's inventory isn't laid out as expected")


def _blocks(lines: list[str], first: int, end: int) -> list[tuple[int, int, int]]:
    """slot_N blocks in a section: (number, first line, end line)."""
    out = []
    i = first
    while i < end:
        m = _SLOT.match(lines[i])
        if not m:
            raise TransferError("the save's inventory isn't laid out as expected")
        j = i + 1
        while j < end and not _SLOT.match(lines[j]):
            j += 1
        out.append((int(m.group(1)), i, j))
        i = j
    return out


def remove_item(text: str, serial: str) -> tuple[str, bool]:
    """The save text without one copy of `serial` (backpack first, else equipped); was it equipped."""
    lines = text.split("\n")
    inv = next((i for i, line in enumerate(lines) if line == "  inventory: " or line == "  inventory:"), None)
    if inv is None:
        raise TransferError("the save has no inventory")
    first, end = _section(lines, _BACKPACK, inv)
    blocks = _blocks(lines, first, end)
    if [b[0] for b in blocks] != list(range(len(blocks))):
        raise TransferError("the save's backpack isn't numbered as expected")
    wanted = f"          serial: '{serial}'"
    mine = [b for b in blocks if lines[b[1] + 1] == wanted]
    if not mine:
        raise TransferError("that character doesn't hold it anymore")
    # Equipped copies are in the equipped list (and in the backpack too). `flags: 1` on a backpack
    # entry isn't the mark: an unequipped one had it (C4SH's slot_3). Copies of one serial are
    # alike, so: while the backpack has more than the equipped list, a backpack one goes (one
    # without flags first); else one of each.
    efirst, eend = _section(lines, _EQUIPPED, end)
    worn = [b for b in _blocks(lines, efirst, eend) if lines[b[1] + 1] == f"        - serial: '{serial}'"]
    if len(worn) > len(mine):
        raise TransferError("the save's equipped gear isn't laid out as expected")
    equipped = len(worn) == len(mine)
    plain = [b for b in mine if "          flags: 1" not in lines[b[1] + 2 : b[2]]]
    pick = (plain or mine)[0] if not equipped else ([b for b in mine if b not in plain] or mine)[0]
    drop = set(range(pick[1], pick[2]))
    if equipped:
        drop |= set(range(worn[0][1], worn[0][2]))
    renumber = {b[1]: f"        slot_{b[0] - 1}: " for b in blocks if b[0] > pick[0]}
    out = []
    for i, line in enumerate(lines):
        if i in drop:
            continue
        if i in renumber:
            # (keep the game's trailing space after the colon, whatever it is)
            line = renumber[i] if line.endswith(": ") else renumber[i].rstrip()
        out.append(line)
    new = "\n".join(out)
    before, after = Counter(owned.from_save_text(text)), Counter(owned.from_save_text(new))
    before[serial] -= 1
    if +before != +after:
        raise TransferError("the edit would have changed other items; nothing was written")
    return new, equipped


# endregion
# region The flow


def take(serial: str, holder: str) -> str | None:
    """
    Moves one copy of `serial` from `holder` (a character's id, or "bank") to you. None: under way
    (tick() reports the end); else a message for the player (why not, or the bank's done at once).
    """
    if S.job is not None:
        return "A transfer is already under way."
    here = progress.character()
    if here is None:
        return "Load a character first."
    if holder == here:
        return "It's already yours."
    # Room first: with a full backpack the package may not open, or the game puts the item in Lost
    # Loot (and the source would keep theirs: two). The count the inventory shows: unequipped items
    # against the backpack's size (its MaxSize: the SDU upgrades and the Backpack Size mod change it)
    used, size = _room()
    if used >= size:
        return f"Your backpack is full ({used} / {size}). Make room first."
    if holder == collection.BANK:
        collection.expect_move(serial, holder, here)  # (the Collection moves the bank's copy)
        try:
            bank.withdraw(get_pc(), serial)
        except bank.BankKept as ex:
            collection.moved(serial, holder, here)
            return f"Took it, but {ex}."
        except bank.BankError as ex:
            collection.cancel_move(serial, holder, here)
            return f"Couldn't take it from the bank: {ex}."
        collection.moved(serial, holder, here)
        savegame.S.checked_at = 0.0
        logging.info(f"[Kairosdex] Take {serial[:24]}... from the bank: moved")
        return "Moved to you from the bank."
    S.job = {"serial": serial, "holder": holder, "char": here, "stage": "reading", "error": None}
    threading.Thread(target=_prepare, args=(S.job,), name="KairosdexTake", daemon=True).start()
    return None


def _prepare(job: dict[str, Any]) -> None:
    """Worker: the holder's save, the item in it, the new text."""
    try:
        folder = savegame._save_folder()  # noqa: SLF001
        if folder is None:
            raise TransferError("couldn't find the saves")
        profile = (folder / "profile.sav").read_bytes()
        key = savegame._key(int(folder.parent.parent.name), profile)  # noqa: SLF001
        if key is None:
            raise TransferError("couldn't find the save key")
        for path in sorted(folder.glob("*.sav")):
            if not path.stem.isdigit():
                continue
            blob = path.read_bytes()
            text = savegame._decrypt(key, blob)  # noqa: SLF001
            guid = savegame._CHAR_GUID.search(text)  # noqa: SLF001
            if guid and guid.group(1).lower() == job["holder"]:
                break
        else:
            raise TransferError("couldn't find that character's save")
        if not savegame.same_form(key, blob):
            raise TransferError("that save isn't in the form we can write back; nothing was changed")
        new, equipped = remove_item(text, job["serial"])
        job.update(path=path, blob=blob, key=key, new=new, equipped=equipped, stage="giving")
    except Exception as ex:  # noqa: BLE001 - reported to the player
        job.update(stage="failed", error=str(ex) if isinstance(ex, TransferError) else repr(ex))


def _commit(job: dict[str, Any]) -> None:
    """Worker, once the item is yours: the holder's save without it (backed up first, checked after)."""
    path: Path = job["path"]
    try:
        if path.read_bytes() != job["blob"]:
            raise TransferError("that character's save changed meanwhile; it wasn't written (you have a copy now)")
        BACKUPS.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        shutil.copyfile(path, BACKUPS / f"{stamp}_{path.stem}.sav")
        blob = savegame.encrypt(job["key"], job["new"])
        tmp = path.with_name(path.name + ".kairosdex.tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)
        if savegame._decrypt(job["key"], path.read_bytes()) != job["new"]:  # noqa: SLF001
            shutil.copyfile(BACKUPS / f"{stamp}_{path.stem}.sav", path)
            raise TransferError("the written save didn't read back right; the old one is back (you have a copy now)")
        with (BACKUPS / "log.jsonl").open("a", encoding="utf-8") as log:
            log.write(json.dumps({"at": stamp, "save": path.name, "from": job["holder"], "to": job["char"],
                                  "serial": job["serial"], "equipped": job["equipped"]}) + "\n")  # fmt: skip
        job.update(stage="done")
    except Exception as ex:  # noqa: BLE001 - reported to the player
        job.update(stage="failed", error=str(ex) if isinstance(ex, TransferError) else repr(ex))


def tick() -> None:
    """Game thread, while busy(): gives the item once its save is ready, reports the end."""
    job = S.job
    if job is None:
        return
    stage = job["stage"]
    if stage == "giving":
        if progress.character() != job["char"]:
            _finish(job, "The character changed; nothing was moved.")
            return
        # The Collection moves the holder's copy when it arrives (not a new pickup)
        collection.expect_move(job["serial"], job["holder"], job["char"])
        try:
            job["handles"] = _handles()
            _open_package_with(job["serial"])
        except Exception as ex:  # noqa: BLE001 - nothing moved yet
            collection.cancel_move(job["serial"], job["holder"], job["char"])
            _finish(job, f"Couldn't give it to you: {ex}. Nothing was moved.")
            return
        job.update(stage="arriving", until=time.monotonic() + ARRIVE_S)
    elif stage == "arriving":
        if _arrived(job["serial"], job["handles"]):
            collection.moved(job["serial"], job["holder"], job["char"])  # (before the save read the write starts)
            job["stage"] = "writing"
            threading.Thread(target=_commit, args=(job,), name="KairosdexTake", daemon=True).start()
        elif time.monotonic() > job["until"]:
            collection.cancel_move(job["serial"], job["holder"], job["char"])
            _finish(job, "It didn't arrive in your backpack (full? look in Lost Loot). The other character still has theirs.")
    elif stage == "done":
        _finish(job, "Moved to you." + (" (It was equipped there.)" if job["equipped"] else ""))
    elif stage == "failed":
        _finish(job, f"Couldn't move it: {job['error']}")


def _finish(job: dict[str, Any], text: str) -> None:
    S.job = None
    savegame.S.checked_at = 0.0  # the Collection reads the saves again at once (the copy left)
    (logging.info if job["stage"] == "done" else logging.error)(f"[Kairosdex] Take {job['serial'][:24]}...: {text}")
    if S.on_done:
        S.on_done(text)


# endregion
# region The game's side (game thread)


def _backpack() -> Any:
    return get_pc().PlayerState.BackpackItems.items


def _room() -> tuple[int, int]:
    """(items in the backpack, its size), as the inventory counts them: equipped gear doesn't count."""
    state = get_pc().PlayerState
    used = sum(1 for e in state.BackpackItems.items if int(e.InventoryItem.EquipSlot) == -1 and int(e.InventoryItem.Handle.Handle) != -1)
    return used, int(state.BackpackContainer.MaxSize.Value)


def _handles() -> set[int]:
    return {int(e.InventoryItem.Handle.Handle) for e in _backpack()}


def _arrived(serial: str, before: set[int]) -> bool:
    for entry in _backpack():
        item = entry.InventoryItem
        if int(item.Handle.Handle) not in before and owned.serial_of(item) == serial:
            return True
    return False


def _open_package_with(serial: str) -> None:
    """A one-item reward package opened with our serial in it (as mods/bl4_reroll/game.py does)."""
    pc = get_pc()
    manager = pc.RewardsManager
    if manager is None:
        raise TransferError("the game's reward mail isn't available")
    lib = unrealsdk.find_class("GbxRewards_BlueprintFunctions").ClassDefaultObject
    func = unrealsdk.find_object("Function", "/Script/GbxGame.GbxRewards_BlueprintFunctions:GiveRewardPackage")
    before = len(manager.packages)
    lib.GiveRewardPackage(FGbxDefPtr(CARRIER_REWARD, func._find_prop("RewardDef").Struct), pc)  # noqa: SLF001
    if len(manager.packages) != before + 1:
        raise TransferError("the game didn't make the package")
    package = manager.packages[before]
    if package.RewardsDef._name != CARRIER_REWARD or len(package.contents) != 1 or len(package.contents[0].SerialNumbers) != 1:  # noqa: SLF001
        raise TransferError("the package didn't look right")  # (not ours: left alone)
    package.contents[0].SerialNumbers[0] = serial
    manager.Server_OpenPackage(before)
    if len(manager.packages) > before:
        manager.Server_DeletePackage(before)
        raise TransferError("the game didn't open the package")


# endregion
