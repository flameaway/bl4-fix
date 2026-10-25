"""
The patches: two small changes to the game's menu tab bar code.

1. The delay (`DELAY`). When you change tabs (Q/E, a click, a bumper), the tab bar doesn't switch
   pages right away. It highlights the new tab, sets a countdown to its `time_to_nav_action`
   setting (0.4 s for the ECHO menu, 0.1 s by default) and lets its Tick switch the page once the
   countdown runs out, so quick presses can skip pages. Its Tick (verified against the running
   game, 2026-10-23):

       movss   xmm0, [rsi+<countdown>]      F3 0F 10 86 <off32>
       xorps   xmm1, xmm1                   0F 57 C9
       ucomiss xmm0, xmm1                   0F 2E C1
       ja      tick                         77 ??           only while a switch is pending
       ...                                                  (epilogue: return)
     tick:
       subss   xmm0, xmm6                   F3 0F 5C C6     countdown -= delta time
       movss   [rsi+<countdown>], xmm0      F3 0F 11 86 <off32>
       ucomiss xmm1, xmm0                   0F 2E C8
       jb      return                       72 ??           still above 0: wait  <- NOPed
       jmp     [vtable+...]                                 switch the page

   With the `jb` gone, a pending switch happens on the very next frame. Setting the delay itself
   to 0 wouldn't work: the Tick only runs while the countdown is above 0.

2. The switch effect (`EFFECT`). The function that switches the page first plays the hexagon wipe
   (material events MenuSwitchLeft / MenuSwitchRight, which hide the page for 0.35 s and run for
   0.6 s) on the menu views, unless the tab didn't change:

       mov     eax, [rsi+<pending tab>]     8B 86 <off32>
       cmp     eax, -1                      83 F8 FF
       je      switch                       0F 84 <rel32>   nothing pending
       mov     ecx, [rsi+<current tab>]     8B 8E <off32>
       cmp     eax, ecx                     39 C8
       je      switch                       0F 84 <rel32>   same tab  <- made a plain jmp
       ...     lea rax, "MenuSwitchLeft" / lea rdx, "MenuSwitchRight", queue the effect
     switch:

   As a plain jmp it always goes straight to the switch. We check that the skipped code really
   names those two animations before patching.

3. The opening effect (`OPEN`). Opening the menu stores the effect key "Open" in a shared UI
   state, and the page that activates plays whatever key it finds there (MenuOpening, which hides
   the page for 0.49 s, plus the tab bar's SpectrogramOpening). With no key the page takes the
   game's own "no effect" path, which also sends MenuOpened right away. The setter:

       lea     rdx, "Open"                  48 8D 15 <rel32>   checked
       ...     make the FName
       mov     ecx, [rsi+<revision>]        8B 4E ??
       cmp     rax, [rsi+<effect key>]      48 3B 46 ??
       je      unchanged                    74 ??           <- jmp past the stores
       inc ecx / mov ebx, ecx / store the key and its revision
     exit:

   Two things that didn't work: skipping the effect inside the page left the key pending (no
   pause in single player, and the effect played later, when the menu closed); blanking the key
   the ECHO tab bar publishes after a switch missed this setter entirely.

4. The warp (`WARP`). Opening a menu also spawns a screen particle in front of the camera for
   0.5 s (NS_UI_ScreenEffect_Menu_Open, from the UI globals). The spawning code already returns
   early when that particle isn't loaded:

       call    <UI globals>                 E8 <rel32>
       lea     rcx, [rax+<MenuOpenParticleEffect>]   48 8D 88 <off32>
       call    <resolve the soft pointer>   E8 <rel32>
       test    rax, rax                     48 85 C0
       je      return                       0F 84 <rel32>   <- made a plain jmp
       mov     rsi, rax                     48 89 C6

   We check that the function goes on to name the particle MENU_OPENED_PARTICLE.

Two optional extras (off by default) in the page's "no effect" path, the one every page takes on
activation now that there's no effect:

       mov     rax, [rsi]                   48 8B 06
       mov     rcx, rsi                     48 89 F1
       call    [rax+<show character>]       FF 90 <off32>   <- CHARACTER: nop
       mov     ecx, 0x40                    B9 40 00 00 00  <- SEE_THROUGH: jmp exit
       ...     build and send the MenuOpened message (checked: names "MenuOpened")
       jmp     exit                         E9 <rel32>

5. `CHARACTER`: the call shows the 3D character model (the "stand-in") behind the page.
6. `SEE_THROUGH`: MenuOpened puts up the menu background and, in single player, pauses the game.
   Without it the page draws straight over the running game.

The tab bar isn't a reflected object, so we find all of these by their bytes in the game's code.
Each pattern must match exactly once, or we leave that part of the game alone. Four tab bar
classes share the first two (the ECHO menu's, the Options menu's, ...), so they all switch
instantly.

Finding them means copying the game's code (~160 MB, 1.6 s) and running the patterns over it
(~0.6 s): on the game thread when the mod is turned on from the Mod Menu. So where each one
matched is kept in settings/bl4_instant_tabs_sites.json for this build of the game (the exe's PE
timestamp and image size); next time each site is checked by matching its whole pattern again
right there, on a few hundred bytes, and only a miss falls back to the full search.

This file holds no hooks, so it can be reloaded in a running game.
"""

from __future__ import annotations

import ctypes
import json
import re
import struct
import sys
from collections.abc import Callable

from mods_base import SETTINGS_DIR
from unrealsdk import logging

PAGE_EXECUTE_READWRITE = 0x40
CACHE_LINE = 64
CACHE_PATH = SETTINGS_DIR / "bl4_instant_tabs_sites.json"
# Bytes read at a remembered site: more than the longest pattern can match (~130)
WINDOW = 512

_kernel32 = ctypes.windll.kernel32
_kernel32.GetModuleHandleW.restype = ctypes.c_void_p
_kernel32.GetCurrentProcess.restype = ctypes.c_void_p
_kernel32.VirtualProtect.argtypes = (
    ctypes.c_void_p,
    ctypes.c_size_t,
    ctypes.c_uint32,
    ctypes.POINTER(ctypes.c_uint32),
)
_kernel32.FlushInstructionCache.argtypes = (ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t)


class NotFound(Exception):
    pass


def _text_section() -> tuple[int, int]:
    """Address and size of the game exe's .text section."""
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    count, opt_size = struct.unpack("<H12xH", ctypes.string_at(base + pe + 6, 16))
    sections = base + pe + 24 + opt_size
    for i in range(count):
        name, vsize, rva = struct.unpack("<8sII", ctypes.string_at(sections + 40 * i, 16))
        if name.rstrip(b"\0") == b".text":
            return base + rva, vsize
    raise NotFound("the game's code section")


def _image() -> tuple[int, int]:
    """Start and end of the game exe in memory (SizeOfImage), all of it mapped."""
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    (size,) = struct.unpack("<I", ctypes.string_at(base + pe + 24 + 56, 4))
    return base, base + size


def _rip_target(addr: int, length: int) -> int:
    """
    Where a `lea reg, [rip+rel32]` (7 bytes) at addr points, if `length` bytes there are inside
    the exe (so reading them can't fault); 0 otherwise.
    """
    (rel,) = struct.unpack("<i", ctypes.string_at(addr + 3, 4))
    target = addr + 7 + rel
    start, end = _image()
    return target if start <= target and target + length <= end else 0


def _write(addr: int, data: bytes) -> None:
    """
    Writes code the game may be running at that moment (mods can be enabled from another thread
    while the game ticks). A single store of up to 8 bytes that stays inside one cache line is
    atomic on x64, so another thread sees either the old instruction or ours, never half of each.
    """
    # The 8 bytes around the patch, kept inside its cache line
    window = addr if addr % CACHE_LINE + 8 <= CACHE_LINE else addr + len(data) - 8
    single = len(data) <= 8 and window // CACHE_LINE == (addr + len(data) - 1) // CACHE_LINE  # noqa: PLR2004
    span = (window, 8) if single else (addr, len(data))
    old = ctypes.c_uint32()
    if not _kernel32.VirtualProtect(span[0], span[1], PAGE_EXECUTE_READWRITE, ctypes.byref(old)):
        raise ctypes.WinError()
    try:
        if single:
            word = bytearray(ctypes.string_at(window, 8))
            word[addr - window : addr - window + len(data)] = data
            ctypes.c_uint64.from_address(window).value = int.from_bytes(word, "little")
        else:
            ctypes.memmove(addr, data, len(data))  # crosses a cache line: no atomic way to do it
    finally:
        _kernel32.VirtualProtect(span[0], span[1], old.value, ctypes.byref(old))
        _kernel32.FlushInstructionCache(_kernel32.GetCurrentProcess(), span[0], span[1])


def _rip_string(addr: int) -> bytes:
    """The C string a `lea reg, [rip+rel32]` (7 bytes) at addr points to; empty if outside the exe."""
    target = _rip_target(addr, 32)
    return ctypes.string_at(target, 32).split(b"\0", 1)[0] if target else b""


_LEA_RIP = re.compile(rb"[\x48\x4C]\x8D[\x05\x0D\x15\x1D\x25\x2D\x35\x3D]", re.DOTALL)


def _strings_near(addr: int, size: int) -> set[bytes]:
    """Every string (narrow, or wide as UTF-16) a `lea reg, [rip+rel32]` in [addr, addr+size) names."""
    found: set[bytes] = set()
    for match in _LEA_RIP.finditer(ctypes.string_at(addr, size)):
        # Not every match is a real instruction (it can start inside another one)
        target = _rip_target(addr + match.start(), 64)
        if not target:
            continue
        raw = ctypes.string_at(target, 64)
        found.add(raw.split(b"\0", 1)[0])
        found.add(raw.decode("utf-16-le", "ignore").split("\0", 1)[0].encode("ascii", "ignore"))
    return found


class Site:
    """One patch: where it goes (found by a byte pattern), the original bytes and ours."""

    def __init__(
        self,
        name: str,
        pattern: re.Pattern[bytes],
        locate: Callable[[re.Match[bytes], int], tuple[int, bytes]],
    ) -> None:
        self.name = name
        self.pattern = pattern
        # (match, address of the match) -> (address to patch, our bytes)
        self.locate = locate
        self.addr = 0
        self.match_addr = 0  # where the pattern matched (the cache keeps this)
        self.original = b""
        self.replacement = b""

    def find(self, text: bytes | bytearray, text_addr: int, matches: list[re.Match[bytes]]) -> None:
        """
        Takes the site from `matches`, the pattern's matches in `text` (a copy of the game's code
        as it is without our patches).
        """
        if len(matches) != 1:
            raise NotFound(f"the {self.name} code ({len(matches)} matches, expected 1)")
        self.match_addr = text_addr + matches[0].start()
        self.addr, self.replacement = self.locate(matches[0], self.match_addr)
        offset = self.addr - text_addr
        self.original = bytes(text[offset : offset + len(self.replacement)])

    def find_at(self, match_addr: int, text_end: int) -> bool:
        """Takes the site at a remembered match address, if the whole pattern still matches there."""
        window = _unpatched(match_addr, min(WINDOW, text_end - match_addr))
        match = self.pattern.match(window)
        if match is None:
            return False
        self.match_addr = match_addr
        self.addr, self.replacement = self.locate(match, match_addr)
        offset = self.addr - match_addr
        self.original = window[offset : offset + len(self.replacement)]
        return len(self.original) == len(self.replacement)

    @property
    def applied(self) -> bool:
        return ctypes.string_at(self.addr, len(self.replacement)) == self.replacement

    def apply(self) -> None:
        current = ctypes.string_at(self.addr, len(self.replacement))
        if current == self.replacement:
            return
        if current != self.original:
            raise NotFound(f"unexpected bytes {current.hex()} at the {self.name} code")
        _applied[self.name] = (self.addr, self.original, self.replacement)
        _write(self.addr, self.replacement)

    def restore(self) -> None:
        if self.applied:
            _write(self.addr, self.original)
        _applied.pop(self.name, None)


def _locate_delay(match: re.Match[bytes], at: int) -> tuple[int, bytes]:
    return at + match.end() - match.start() - 2, b"\x90\x90"


def _locate_effect(match: re.Match[bytes], at: int) -> tuple[int, bytes]:
    nothing_pending = at + 9 + 6 + struct.unpack("<i", match.group(2))[0]
    same_tab_at = at + 23
    same_tab = same_tab_at + 6 + struct.unpack("<i", match.group(4))[0]
    if same_tab != nothing_pending:
        raise NotFound("the switch effect code (its two branches go to different places)")
    names = (_rip_string(at + match.end() - match.start() - 14), _rip_string(at + match.end() - match.start() - 7))
    if names != (b"MenuSwitchLeft", b"MenuSwitchRight"):
        raise NotFound(f"the switch effect code (it names {names!r})")
    return _jmp_instead_of_je(same_tab_at)


def _jmp_instead_of_je(je_at: int) -> tuple[int, bytes]:
    """je rel32 (6 bytes) at je_at -> jmp rel32 (5 bytes) + nop, to the same place."""
    # The game's own bytes: with our jmp already there (a cached lookup while patched), reading the
    # live code gave a wrong target
    target = je_at + 6 + struct.unpack("<i", _unpatched(je_at, 6)[2:6])[0]
    return je_at, b"\xe9" + struct.pack("<i", target - (je_at + 5)) + b"\x90"


def _locate_open(match: re.Match[bytes], at: int) -> tuple[int, bytes]:
    if _rip_string(at) != b"Open":
        raise NotFound(f"the opening effect code (it names {_rip_string(at)!r})")
    # je rel8 -> jmp rel8 to the end of the match, just past the two stores
    je_at = at + 41
    return je_at, b"\xeb" + bytes([match.end() - match.start() - 43])


def _locate_warp(match: re.Match[bytes], at: int) -> tuple[int, bytes]:
    if b"MENU_OPENED_PARTICLE" not in _strings_near(at, 0x200):
        raise NotFound("the warp code (it doesn't name MENU_OPENED_PARTICLE)")
    return _jmp_instead_of_je(at + 20)


def _check_menu_opened(match: re.Match[bytes], at: int) -> None:
    if _rip_string(at + 0x28) != b"MenuOpened":
        raise NotFound(f"the page activation code (it names {_rip_string(at + 0x28)!r})")


def _locate_character(match: re.Match[bytes], at: int) -> tuple[int, bytes]:
    _check_menu_opened(match, at)
    return at + 6, b"\x66\x0f\x1f\x44\x00\x00"  # 6-byte nop


def _locate_see_through(match: re.Match[bytes], at: int) -> tuple[int, bytes]:
    _check_menu_opened(match, at)
    end = at + match.end() - match.start()
    exit_ = end + struct.unpack("<i", match.group(3))[0]
    return at + 12, b"\xe9" + struct.pack("<i", exit_ - (at + 12 + 5))


# The page's "no effect" activation: show the character, then send MenuOpened
_PAGE_ACTIVATED = re.compile(
    rb"\x48\x8B\x06\x48\x89\xF1\xFF\x90(....)"  # call [vtable+off]: show the character
    rb"\xB9\x40\x00\x00\x00\xBA\x10\x00\x00\x00\xE8....\x48\x89\xC7"  # allocate the message
    rb"\x4C\x8D\x35....\x4C\x89\x30\x48\x8D\x15(....)"  # its vtable; lea "MenuOpened"
    rb".{0,60}?\xE8....\xE9(....)",  # send it; jmp exit
    re.DOTALL,
)

DELAY = Site(
    "tab delay",
    re.compile(
        rb"\xF3\x0F\x10\x86(....)\x0F\x57\xC9\x0F\x2E\xC1\x77."  # load countdown, > 0?
        rb"\x0F\x28\x74\x24.\x48\x83\xC4.\x5E\xC3"  # no: return
        rb"\xF3\x0F\x5C\xC6\xF3\x0F\x11\x86\1\x0F\x2E\xC8"  # countdown -= dt, store, compare
        rb"\x72.",  # jb return
        re.DOTALL,
    ),
    _locate_delay,
)
EFFECT = Site(
    "switch effect",
    re.compile(
        rb"\x8B\x86(....)\x83\xF8\xFF\x0F\x84(....)"  # pending tab, -1? -> switch
        rb"\x8B\x8E(....)\x39\xC8\x0F\x84(....)"  # same as the current tab? -> switch
        rb"\x48\x8D\x9E(....)\x48\x83\xBE\5\x00\x75."  # effect name already chosen?
        rb"\x39\xC8\x48\x8D\x05....\x48\x8D\x15....",  # lea "MenuSwitchLeft" / "MenuSwitchRight"
        re.DOTALL,
    ),
    _locate_effect,
)
OPEN = Site(
    "opening effect",
    re.compile(
        rb"\x48\x8D\x15....\x48\x8D\x7C\x24.\xBB\x01\x00\x00\x00\x48\x89\xF9"  # FName("Open")
        rb"\x41\xB8\x01\x00\x00\x00\xE8....\x48\x8B\x07"
        rb"\x8B\x4E(.)\x48\x3B\x46(.)\x74."  # same key as before?
        rb"\xFF\xC1\x89\xCB\x48\x89\x46\2\x89\x5E\1",  # no: store the key and its revision
        re.DOTALL,
    ),
    _locate_open,
)
WARP = Site(
    "warp",
    re.compile(
        rb"\xE8....\x48\x8D\x88(....)\xE8....\x48\x85\xC0\x0F\x84(....)\x48\x89\xC6",  # particle loaded?
        re.DOTALL,
    ),
    _locate_warp,
)
CHARACTER = Site("character model", _PAGE_ACTIVATED, _locate_character)
SEE_THROUGH = Site("menu background", _PAGE_ACTIVATED, _locate_see_through)
SITES = (DELAY, EFFECT, OPEN, WARP, CHARACTER, SEE_THROUGH)
# What "Skip page transitions" covers; DELAY is always on while the mod is
TRANSITIONS = (EFFECT, OPEN, WARP)

_found: dict[str, Site | None] = {}

# Patches in place right now: name -> (address, original bytes, ours). Kept on `sys` so a reload of
# this file still knows them: every pattern includes the bytes it patches, so searching the patched
# code again would find nothing, and the patches could never be undone.
_applied: dict[str, tuple[int, bytes, bytes]] = sys.__dict__.setdefault("_bl4_instant_tabs_applied", {})


def _unpatched_text() -> tuple[bytes | bytearray, int]:
    """A copy of the game's code (~150 MB, about 0.1 s), with any patches of ours taken back out."""
    start, size = _text_section()
    text: bytes | bytearray = ctypes.string_at(start, size)
    if _applied:
        text = bytearray(text)
        for addr, original, replacement in list(_applied.values()):
            offset = addr - start
            if text[offset : offset + len(replacement)] == replacement:
                text[offset : offset + len(original)] = original
    return text, start


def _unpatched(addr: int, size: int) -> bytes:
    """`size` bytes of the game's code at addr, with any patches of ours inside taken back out."""
    data = bytearray(ctypes.string_at(addr, size))
    for at, original, replacement in list(_applied.values()):
        offset = at - addr
        inside = 0 <= offset and offset + len(replacement) <= size
        if inside and data[offset : offset + len(replacement)] == replacement:
            data[offset : offset + len(original)] = original
    return bytes(data)


def _build() -> str:
    """This build of the game: the exe's PE timestamp and image size."""
    base = _kernel32.GetModuleHandleW(None)
    (pe,) = struct.unpack("<I", ctypes.string_at(base + 0x3C, 4))
    (stamp,) = struct.unpack("<I", ctypes.string_at(base + pe + 8, 4))
    start, end = _image()
    return f"{stamp:08x}-{end - start:x}"


def _from_cache(build: str, start: int, size: int) -> bool:
    """Every site from the remembered match addresses, each checked; False = search instead."""
    try:
        cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if cached.get("build") != build:
            return False
        sites = {each.name: start + int(cached["sites"][each.name]) for each in SITES}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return False
    try:
        for each in SITES:
            at = sites[each.name]
            if not (start <= at < start + size and each.find_at(at, start + size)):
                return False
    except Exception:  # noqa: BLE001 - a check that doesn't pass is a miss
        return False
    _found.update({each.name: each for each in SITES})
    return True


def _save_cache(build: str, start: int) -> None:
    sites = {each.name: each.match_addr - start for each in SITES}
    try:
        tmp = CACHE_PATH.with_name(CACHE_PATH.name + ".tmp")
        tmp.write_text(json.dumps({"build": build, "sites": sites}), encoding="utf-8")
        tmp.replace(CACHE_PATH)
    except OSError as ex:
        logging.dev_warning(f"[Instant Tabs] couldn't remember the code sites: {ex!r}")


def _find_all() -> None:
    try:
        start, size = _text_section()
        build = _build()
    except Exception as ex:  # noqa: BLE001 - leave the game alone
        logging.error(f"[Instant Tabs] couldn't read the game's code ({ex!r}); the mod does nothing")
        _found.update(dict.fromkeys((each.name for each in SITES), None))
        return
    if _from_cache(build, start, size):
        return
    try:
        text, start = _unpatched_text()
    except Exception as ex:  # noqa: BLE001 - leave the game alone
        text, start = b"", 0
        logging.error(f"[Instant Tabs] couldn't read the game's code ({ex!r}); the mod does nothing")
    matches: dict[re.Pattern[bytes], list[re.Match[bytes]]] = {}  # two sites share a pattern
    for each in SITES:
        try:
            if not text:
                raise NotFound("the game's code")
            if each.pattern not in matches:
                matches[each.pattern] = list(each.pattern.finditer(text))
            each.find(text, start, matches[each.pattern])
            _found[each.name] = each
        except Exception as ex:  # noqa: BLE001 - one odd site mustn't stop the others
            _found[each.name] = None
            if text:
                logging.error(f"[Instant Tabs] couldn't find {ex}; that part of the mod does nothing")
    del text, matches
    if all(_found.values()):
        _save_cache(build, start)


def get(site: Site) -> Site | None:
    """The site, found on first use. None (logged once) if this game version doesn't match."""
    if not _found:
        _find_all()
    return _found.get(site.name)
