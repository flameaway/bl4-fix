"""
BL4 Game Keys: whether a key is held, from the game's own input. Every mod's copy shares one reader.

    from . import gamekeys
    gamekeys.down("F7")                    # an Unreal key name, as keybind options hold them
    gamekeys.down("Gamepad_Special_Left")  # pads too (the View button)
    gamekeys.vk_down(0x76)                 # by Windows virtual-key code, for older callers
    gamekeys.mute(0.5)                     # a text box of ours has the keyboard: nothing counts
    gamekeys.ignore("Escape", 0.25)        # a key we posted ourselves isn't a press

The game's PlayerInput is the key state its own binds read. It sees keys in play and with menus
up (2026-10-30: T, G, H, A, B, C read with the cursor shown), mouse and pad buttons too, and the
game drops held keys when its window loses focus. So no Windows key reads and no keyboard hooks:
until 2026-10-30 our mods polled GetAsyncKeyState (slow while the desktop is streamed: 3 ms p90,
~100 ms worst, 2026-10-24) on a worker thread, and XInput for pads.

It's read from the game's key map in memory (UPlayerInput::KeyStateMap, see "The key map"):
~5 us a frame to find it and well under 1 us a key, with no Win32 call, so the GIL stays with the
game thread. The UFunction for the same thing, PlayerController.IsInputKeyDown, costs ~10 us a key
through the SDK (11 keys: 113 us a frame at p50): it's the fallback, and what the map is checked
against once per game build before it's read directly (see "The key map").

Each key is read at most once a frame for every mod together (cached per engine frame; the frame
number comes from the Frame Tick). Only the game thread reads the game; another thread gets the
last frame's answer.

Copies share `sys.modules["_bl4_gamekeys"]` and the newest VERSION loaded does the reading (like
frametick). Shared fields are only ever added. Imports nothing of the SDK until a key is read.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import types
from typing import Any

VERSION = 1
HUB = "_bl4_gamekeys"
PC_CHECK_S = 1.0  # the controller is looked up again this often (get_pc is 10-13 us in the tick)
WANT_FRAMES = 30  # a key not asked about for this many frames isn't read ahead any more
MAX_KEY_ENTRIES = 4096  # a bigger count isn't a key map
# The check's result, per game build (the exe's PE timestamp + image size) and layout: a later
# launch of the same build skips the probation. Anything else (another build, a failed check) and
# it's checked again.
RECORD_FILE = "bl4fix_gamekeys.json"
# Before the key map is read directly it's on probation: keys come from IsInputKeyDown, and the map
# is read through ReadProcessMemory (fails safely on a bad address) and compared. It's trusted after
# this many equal answers, this many of them for a held key, with real keys' names in it.
TRUST_READS = 200
TRUST_HELD = 5
# Keys a player presses all the time (looked for in the map, and compared, while on probation)
PROBE_KEYS = (
    "W", "A", "S", "D", "SpaceBar", "LeftShift", "LeftMouseButton", "RightMouseButton", "MouseX",
    "MouseY", "Gamepad_LeftX", "Gamepad_LeftY", "Gamepad_FaceButton_Bottom", "Gamepad_RightTrigger",
)  # fmt: skip
UNKNOWN_LIMIT = 16  # a map this full with none of PROBE_KEYS in it isn't a key map

_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]
# Windows virtual-key code -> Unreal key name
VK_NAMES: dict[int, str] = {
    **{0x41 + i: chr(0x41 + i) for i in range(26)},
    **{0x30 + i: name for i, name in enumerate(_DIGITS)},
    **{0x70 + i: f"F{i + 1}" for i in range(12)},
    **{0x60 + i: f"NumPad{name}" for i, name in enumerate(_DIGITS)},
    0x6A: "Multiply", 0x6B: "Add", 0x6D: "Subtract", 0x6E: "Decimal", 0x6F: "Divide",
    0x08: "BackSpace", 0x09: "Tab", 0x0D: "Enter", 0x13: "Pause", 0x14: "CapsLock", 0x1B: "Escape",
    0x20: "SpaceBar", 0x21: "PageUp", 0x22: "PageDown", 0x23: "End", 0x24: "Home",
    0x25: "Left", 0x26: "Up", 0x27: "Right", 0x28: "Down", 0x2D: "Insert", 0x2E: "Delete",
    0x90: "NumLock", 0x91: "ScrollLock",
    0xA0: "LeftShift", 0xA1: "RightShift", 0xA2: "LeftControl", 0xA3: "RightControl",
    0xA4: "LeftAlt", 0xA5: "RightAlt",
    0xBA: "Semicolon", 0xBB: "Equals", 0xBC: "Comma", 0xBD: "Hyphen", 0xBE: "Period",
    0xBF: "Slash", 0xC0: "Tilde", 0xDB: "LeftBracket", 0xDC: "Backslash", 0xDD: "RightBracket",
    0xDE: "Apostrophe",
    0x01: "LeftMouseButton", 0x02: "RightMouseButton", 0x04: "MiddleMouseButton",
    0x05: "ThumbMouseButton", 0x06: "ThumbMouseButton2",
}  # fmt: skip
_EITHER = {0x10: ("LeftShift", "RightShift"), 0x11: ("LeftControl", "RightControl"), 0x12: ("LeftAlt", "RightAlt")}

_STAMP = object()


# region Shared state


def _hub() -> types.ModuleType:
    """The state every copy shares (created by the first one)."""
    hub = sys.modules.get(HUB)
    if hub is None:
        hub = types.ModuleType(HUB)
        hub.__doc__ = "BL4 Game Keys state shared by every mod's copy of gamekeys (see its docstring)."
        hub.owner = None  # the copy that reads (a module object)
        hub.owner_stamp = None
        hub.owner_version = 0
        hub.frame = -2  # engine frame `cache` belongs to
        hub.cache = {}  # key name -> down, this frame
        hub.last = {}  # the frame before (for other threads)
        hub.wanted = {}  # key name -> frame it was last asked about
        hub.structs = {}  # key name -> FKey struct (kept alive: the key map compares its FName)
        hub.names = {}  # key name -> its FName as an int (the struct's first 8 bytes)
        hub.from_map = False  # this frame's keys came from the key map (else IsInputKeyDown)
        hub.read = None  # BoundFunction IsInputKeyDown on this frame's controller (never kept past it)
        hub.read_frame = -2
        hub.pc = None  # WeakPointer to the controller
        hub.pc_at = -PC_CHECK_S
        hub.fn = None  # the IsInputKeyDown UFunction (a native class's: lives as long as the game)
        hub.game_thread = 0  # ident of the thread frame ticks run on (0 until the first)
        hub.ignored = {}  # key name -> time.monotonic() until which it doesn't count (posted by us)
        hub.muted_until = 0.0  # no key counts until then (see mute)
        # The key map
        hub.layout = None  # (map offset, stride, bDown offset, bDown mask); False: not used
        hub.pi = None  # WeakPointer to the PlayerInput
        hub.pi_addr = 0
        hub.data = 0  # element array and count the slots were found in
        hub.count = -1
        hub.slots = {}  # FName -> entry address
        hub.verify_at = 0.0
        hub.verify_i = 0
        hub.mismatches = 0
        # Probation (see TRUST_READS)
        sys.modules[HUB] = hub
    if not hasattr(hub, "trusted"):  # probation (see TRUST_READS); a copy without it made the hub
        hub.trusted = False  # the map's contents checked: read it directly
        hub.agreed = 0  # equal answers so far
        hub.agreed_held = 0  # ... for a held key
        hub.probe_i = 0
        hub.checked_pi = 0  # PlayerInput whose map header ReadProcessMemory read last
        hub.status = "not checked yet"  # for reading(): why the key map is or isn't used
        hub.build = ""  # _build_id() of the running game
        hub.from_map = False
    return hub


def _me() -> types.ModuleType:
    return sys.modules[__name__]


def _elect(hub: types.ModuleType) -> None:
    """Makes this copy the owner if it's newer (or a hot reload of the owner)."""
    owner = hub.owner
    if owner is _me() and hub.owner_stamp is _STAMP:
        return
    if owner is not None and owner is not _me() and getattr(hub, "owner_version", 0) >= VERSION:
        return
    hub.owner, hub.owner_stamp, hub.owner_version = _me(), _STAMP, VERSION
    try:
        from .. import frametick  # noqa: PLC0415 - every mod that carries gamekeys carries frametick

        frametick.add("bl4_gamekeys", _note_thread, every=1.0, mod=__name__.rpartition(".")[0])
        hub.frametick = frametick
    except Exception:  # noqa: BLE001 - outside the game (tests): no frames, every read is fresh
        hub.frametick = None


def _note_thread() -> None:
    _hub().game_thread = threading.get_ident()


# endregion
# region API (any copy, any thread)


def down(name: str | None) -> bool:
    """
    Whether a key (an Unreal key name: "H", "F7", "LeftMouseButton", "Gamepad_Special_Left") is
    held, from the game's own input. False while the game isn't in front, while muted, and while
    the key is ignored (see ignore). Unknown names are never held.
    """
    if not name:
        return False
    hub = _hub()
    if hub.owner is None:
        _elect(hub)
    return hub.owner._down(hub, name)  # noqa: SLF001


def vk_down(vk: int) -> bool:
    """down() by Windows virtual-key code (VK_NAMES; Shift / Ctrl / Alt without a side: either)."""
    either = _EITHER.get(vk)
    if either is not None:
        return down(either[0]) or down(either[1])
    return down(VK_NAMES.get(vk))


def mute(seconds: float) -> None:
    """No key counts as held for this long (a text box of ours is being typed in: typed letters
    reach the game's input too). Call again to extend; mute(0) ends it."""
    _hub().muted_until = time.monotonic() + seconds if seconds > 0 else 0.0


def ignore(name: str, seconds: float) -> None:
    """The key doesn't count as held for this long: a key we post to the game window ourselves
    (the game's input sees posted keys, Windows' key state never did)."""
    _hub().ignored[name] = time.monotonic() + seconds


def reading() -> str:
    """What the keys come from right now, and why ("key map", or "IsInputKeyDown (...)")."""
    hub = _hub()
    if hub.frame < -1:
        return "nothing yet"
    return "key map" if hub.from_map else f"IsInputKeyDown ({hub.status})"


# endregion
# region Reading (the owner's copy)


def _down(hub: types.ModuleType, name: str) -> bool:
    if threading.get_ident() != hub.game_thread:
        held = hub.last.get(name, False) or hub.cache.get(name, False)
    else:
        held = _cached(hub, name)
    if not held:
        return False
    now = time.monotonic()
    return now >= hub.muted_until and now >= hub.ignored.get(name, 0.0)


def _cached(hub: types.ModuleType, name: str) -> bool:
    ft = getattr(hub, "frametick", None)
    frame = ft.frame() if ft is not None else -1
    if frame != hub.frame or frame < 0:
        _new_frame(hub, frame)
    hub.wanted[name] = frame
    held = hub.cache.get(name)
    if held is None:
        held = hub.cache[name] = _map_down(hub, name) if hub.from_map else _sdk_down(hub, name)
    return held


def _new_frame(hub: types.ModuleType, frame: int) -> None:
    # Every key asked in the last WANT_FRAMES frames, read now (through the SDK back to back: spread
    # over the frame, one read inside each mod's callback, each one handed the GIL around alone)
    hub.last, hub.frame = hub.cache, frame
    cache: dict[str, bool] = {}
    hub.cache = cache
    oldest = frame - WANT_FRAMES
    wanted = hub.wanted
    for name, at in list(wanted.items()):
        if at < oldest:
            del wanted[name]
    try:
        hub.from_map = _map_ready(hub)
    except Exception:  # noqa: BLE001
        hub.from_map = False
    read = _map_down if hub.from_map else _sdk_down
    for name in wanted:
        cache[name] = read(hub, name)


def _controller(hub: types.ModuleType) -> Any:
    now = time.monotonic()
    pc = hub.pc() if hub.pc is not None else None
    if pc is None or now - hub.pc_at >= PC_CHECK_S:
        from mods_base import get_pc  # noqa: PLC0415
        from unrealsdk.unreal import WeakPointer  # noqa: PLC0415

        hub.pc_at = now
        pc = get_pc(possibly_loading=True)
        hub.pc = WeakPointer(pc) if pc is not None else None
        hub.pi = None  # looked up again with the controller
    return pc


def _struct(hub: types.ModuleType, name: str) -> Any:
    s = hub.structs.get(name)
    if s is None:
        import unrealsdk  # noqa: PLC0415

        try:
            s = unrealsdk.make_struct("Key", KeyName=name)
            fname = ctypes.c_uint64.from_address(s._get_address()).value  # FKey starts with its FName
        except Exception:  # noqa: BLE001
            s, fname = False, 0
        hub.structs[name], hub.names[name] = s, fname
    return s


def _sdk_down(hub: types.ModuleType, name: str) -> bool:
    struct = _struct(hub, name)
    if struct is False:
        return False
    try:
        if hub.read_frame != hub.frame or hub.frame < 0:
            import unrealsdk  # noqa: PLC0415
            from unrealsdk.unreal import BoundFunction  # noqa: PLC0415

            hub.read_frame, hub.read = hub.frame, None
            pc = _controller(hub)
            if pc is not None:
                if hub.fn is None:
                    hub.fn = unrealsdk.find_class("PlayerController")._find("IsInputKeyDown")
                hub.read = BoundFunction(hub.fn, pc)
        return bool(hub.read(struct)) if hub.read is not None else False
    except Exception:  # noqa: BLE001 - a dying controller during a map change: not held
        hub.read = None
        return False


# endregion
# region The key map
#
# UPlayerInput::KeyStateMap, a TMap<FKey, FKeyState>. IsInputKeyDown -> APlayerController::
# IsInputKeyDown (PlayerInput = [pc+0x508]) -> UPlayerInput::IsPressed, which finds the key's entry
# (stride 0x110, its FName first) and returns `entry[0x4C] & 1` (bDown). Every offset comes from
# that code at start (IsPressed is rva 0x482EF28 in the 2026-10-22 build), each pattern found
# exactly once inside the exe image, and PlayerInput's offset must equal the reflected property's.
# Verified 2026-10-30 (game 22832): 28,301 reads equal to IsInputKeyDown's, 654 of them held. A
# key never pressed this session has no entry (not held).
#
# The map is checked once per game build (see TRUST_READS, RECORD_FILE), not all along: after that,
# only the header of each new PlayerInput (a new map) is read through ReadProcessMemory before the
# direct reads, and every direct read stays inside the bounds the engine's structures give.
#
# Reads are ctypes from_address (no Win32 call, so no GIL hand-over), only at addresses the
# engine's own structures give: the PlayerInput object (alive this frame: its WeakPointer), the
# map's element array (pointer and count read from it this frame), an entry index below that count.


def _build_id() -> str:
    """The running exe's PE timestamp and image size: changes with every game patch."""
    k32 = ctypes.WinDLL("kernel32")  # our own object: shared windll prototypes stay untouched
    k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    base = k32.GetModuleHandleW(None)
    pe = base + ctypes.c_uint32.from_address(base + 0x3C).value
    stamp = ctypes.c_uint32.from_address(pe + 8).value  # FileHeader.TimeDateStamp
    size = ctypes.c_uint32.from_address(pe + 0x50).value  # OptionalHeader.SizeOfImage
    return f"{stamp:08x}-{size:x}"


def _record_path() -> Any:
    from mods_base import SETTINGS_DIR  # noqa: PLC0415

    return SETTINGS_DIR / RECORD_FILE


def _checked_before(hub: types.ModuleType) -> bool:
    """Whether this game build's key map passed the check on an earlier launch (same layout)."""
    import json  # noqa: PLC0415

    try:
        record = json.loads(_record_path().read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 - no record (or a damaged one): check again
        return False
    return record.get("build") == hub.build and record.get("layout") == list(hub.layout) and record.get("ok") is True


def _save_record(**fields: Any) -> None:
    """Writes the check's result on a short-lived thread (temp file + rename)."""
    import json  # noqa: PLC0415

    path = _record_path()

    def write() -> None:
        try:
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(fields, indent=1), encoding="utf-8")
            tmp.replace(path)
        except OSError:
            pass

    threading.Thread(target=write, name="bl4_gamekeys.record", daemon=True).start()


def _find_layout() -> tuple[int, int, int, int] | None:
    import re  # noqa: PLC0415

    import unrealsdk  # noqa: PLC0415

    k32 = ctypes.WinDLL("kernel32")  # our own object: shared windll prototypes stay untouched
    k32.GetModuleHandleW.argtypes = [ctypes.c_wchar_p]
    k32.GetModuleHandleW.restype = ctypes.c_void_p
    base = k32.GetModuleHandleW(None)
    if not base or ctypes.string_at(base, 2) != b"MZ":
        return None
    pe = base + ctypes.c_uint32.from_address(base + 0x3C).value
    if ctypes.string_at(pe, 4) != b"PE\0\0":
        return None
    end = base + ctypes.c_uint32.from_address(pe + 0x50).value  # SizeOfImage

    def code(address: int, size: int) -> bytes:
        return ctypes.string_at(address, size) if base <= address and address + size <= end else b""

    def once(pattern: bytes, raw: bytes) -> Any:
        found = list(re.finditer(pattern, raw, re.DOTALL))
        return found[0] if len(found) == 1 else None

    def rel32(raw: bytes, at: int, start: int) -> int:
        """Target of the call whose E8 is at raw[at] (raw read from `start`)."""
        return start + at + 5 + int.from_bytes(raw[at + 1 : at + 5], "little", signed=True)

    cls = unrealsdk.find_class("PlayerController")
    execf = ctypes.c_uint64.from_address(cls._find("IsInputKeyDown")._get_address() + 0xD8).value  # UFunction::Func
    raw = code(execf, 0x100)
    # execIsInputKeyDown: call APlayerController::IsInputKeyDown; mov [rsi], al (the result)
    m = once(rb"\xE8....\x88\x06", raw)
    if not m:
        return None
    impl = rel32(raw, m.start(), execf)
    raw = code(impl, 0x60)
    # mov rcx, [rcx+PlayerInput]; test rcx, rcx; ... the first call after it is IsPressed
    m = once(rb"\x48\x8B\x89(....)\x48\x85\xC9", raw)
    if not m or int.from_bytes(m.group(1), "little") != cls._find_prop("PlayerInput").Offset_Internal:
        return None
    at = raw.find(b"\xe8", m.end())
    if at < 0 or at + 5 > len(raw):
        return None
    raw = code(rel32(raw, at, impl), 0x120)
    lea = once(rb"\x4C\x8D\xB1(....)", raw)  # lea r14, [rcx+KeyStateMap]
    num = once(rb"\x8B\x8F(....)", raw)  # mov ecx, [rdi+KeyStateMap+8] (the element count)
    imul = once(rb"\x4C\x69\xC2(....)", raw)  # imul r8, rdx, stride
    cmp = once(rb"\x4A\x39\x04\x01", raw)  # cmp [rcx+r8], rax: the FName at the entry's start
    held = once(rb"\x40\x8A\x7A(.)\x40\x80\xE7(.)", raw)  # mov dil, [rdx+bDown]; and dil, mask
    if not (lea and num and imul and cmp and held):
        return None
    key_map = int.from_bytes(lea.group(1), "little")
    stride = int.from_bytes(imul.group(1), "little")
    if int.from_bytes(num.group(1), "little") != key_map + 8 or not 0x20 <= stride <= 0x400:  # noqa: PLR2004
        return None
    return (key_map, stride, held.group(1)[0], held.group(2)[0])


def _map_ready(hub: types.ModuleType) -> bool:
    """Whether the key map can be read this frame (finds it, follows the PlayerInput)."""
    if hub.layout is False:
        return False
    if hub.layout is None:
        try:
            hub.layout = _find_layout() or False
            if hub.layout is not False:
                hub.build = _build_id()
                if _checked_before(hub):
                    hub.trusted, hub.status = True, "checked on an earlier launch of this game build"
        except Exception:  # noqa: BLE001
            hub.layout = False
        if hub.layout is False:
            _drop(hub, "its code wasn't found: a game update?")
            return False
    pi = hub.pi() if hub.pi is not None else None
    if pi is None:
        pc = _controller(hub)
        pi = pc.PlayerInput if pc is not None else None
        if pi is None:
            return False
        from unrealsdk.unreal import WeakPointer  # noqa: PLC0415

        hub.pi = WeakPointer(pi)
    addr = pi._get_address()
    key_map, stride = hub.layout[0], hub.layout[1]
    if not hub.trusted:
        _probation(hub, pi, addr)
        return False
    if addr != hub.checked_pi:
        # A new PlayerInput (a new map): its header through ReadProcessMemory before a direct read
        if _header(hub, pi, addr) is None:
            return False
        hub.checked_pi = addr
    data = ctypes.c_uint64.from_address(addr + key_map).value
    count = ctypes.c_int32.from_address(addr + key_map + 8).value
    most = ctypes.c_int32.from_address(addr + key_map + 12).value
    if not 0 <= count <= most <= MAX_KEY_ENTRIES or (count and not data):
        return False
    if addr != hub.pi_addr or data != hub.data or count != hub.count:
        # A new PlayerInput, or the map changed (a key pressed for the first time): find the entries
        hub.pi_addr, hub.data, hub.count = addr, data, count
        hub.slots = {ctypes.c_uint64.from_address(data + i * stride).value: data + i * stride for i in range(count)}
    return True


class _Rpm:
    k32: Any = None


def _rpm(address: int, size: int) -> bytes | None:
    """size bytes at address of our own process, or None if any of them can't be read (no crash)."""
    k32 = _Rpm.k32
    if k32 is None:
        k32 = _Rpm.k32 = ctypes.WinDLL("kernel32")  # our own object: shared prototypes stay untouched
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        k32.ReadProcessMemory.argtypes = (
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
        )  # fmt: skip
    if size <= 0:
        return b""
    buf = ctypes.create_string_buffer(size)
    got = ctypes.c_size_t()
    if not k32.ReadProcessMemory(k32.GetCurrentProcess(), ctypes.c_void_p(address), buf, size, ctypes.byref(got)):
        return None
    return buf.raw if got.value == size else None


def _drop(hub: types.ModuleType, why: str) -> None:
    """The key map isn't what we expect: IsInputKeyDown for the rest of the session."""
    if hub.trusted and getattr(hub, "build", None):
        _save_record(build=hub.build, ok=False, why=why)  # the next launch checks again
    hub.layout, hub.trusted, hub.status = False, False, why
    from unrealsdk import logging  # noqa: PLC0415

    logging.warning(f"[Game Keys] not reading the game's key map ({why}): keys go through IsInputKeyDown")


def _header(hub: types.ModuleType, pi: Any, addr: int) -> tuple[int, int] | None:
    """(element array, count) of pi's key map, read safely and checked. None: empty, or dropped."""
    key_map = hub.layout[0]
    if pi.Class._get_struct_size() < key_map + 16:
        _drop(hub, f"{pi.Class.Name} is smaller than the map's offset")
        return None
    head = _rpm(addr + key_map, 16)
    if head is None:
        _drop(hub, "its header can't be read")
        return None
    data = int.from_bytes(head[:8], "little")
    count = int.from_bytes(head[8:12], "little", signed=True)
    most = int.from_bytes(head[12:], "little", signed=True)
    if not 0 <= count <= most <= MAX_KEY_ENTRIES or (count and not data):
        _drop(hub, f"its header doesn't look like a map (count {count}, capacity {most})")
        return None
    return (data, count) if count else None


def _probation(hub: types.ModuleType, pi: Any, addr: int) -> None:
    """Compares the map (read through ReadProcessMemory) with IsInputKeyDown; trusts it once enough
    answers matched (TRUST_READS, TRUST_HELD), drops it at the first difference."""
    head = _header(hub, pi, addr)
    if head is None:
        if hub.layout is not False:
            hub.status = "checking: the key map is empty so far"
        return
    data, count = head
    _, stride, down_at, mask = hub.layout
    raw = _rpm(data, count * stride)
    if raw is None:
        _drop(hub, "its entries can't be read")
        return
    entries = {int.from_bytes(raw[i * stride : i * stride + 8], "little"): i for i in range(count)}
    for name in PROBE_KEYS:
        _struct(hub, name)
    present = [n for n in PROBE_KEYS if hub.names.get(n) in entries]
    if not present and count >= UNKNOWN_LIMIT:
        _drop(hub, f"none of {len(PROBE_KEYS)} common keys among its {count} entries")
        return
    # This frame's keys (already read through IsInputKeyDown) and two common keys in turn
    check = [(n, hub.cache[n]) for n in hub.wanted if n in hub.cache]
    for _ in range(min(2, len(present))):
        hub.probe_i = (hub.probe_i + 1) % len(present)
        name = present[hub.probe_i]
        check.append((name, _sdk_down(hub, name)))
    for name, sdk in check:
        i = entries.get(hub.names.get(name, 0))
        mem = i is not None and bool(raw[i * stride + down_at] & mask)
        if mem != sdk:
            said = "held" if mem else "up"
            real = "held" if sdk else "up"
            _drop(hub, f"it said {name} was {said}, IsInputKeyDown said {real}")
            return
        hub.agreed += 1
        hub.agreed_held += sdk
    hub.status = f"checking: {hub.agreed}/{TRUST_READS} reads agreed, {hub.agreed_held}/{TRUST_HELD} held"
    if present and hub.agreed >= TRUST_READS and hub.agreed_held >= TRUST_HELD:
        hub.trusted, hub.checked_pi, hub.status = True, addr, "checked"
        _save_record(
            build=hub.build, layout=list(hub.layout), ok=True, reads=hub.agreed, held=hub.agreed_held,
            checked=time.strftime("%Y-%m-%d %H:%M:%S"),
        )  # fmt: skip
        from unrealsdk import logging  # noqa: PLC0415

        logging.info(
            f"[Game Keys] reading the game's key map: {hub.agreed} reads agreed with IsInputKeyDown"
            f" ({hub.agreed_held} held)"
        )


def _map_down(hub: types.ModuleType, name: str) -> bool:
    fname = hub.names.get(name)
    if fname is None:
        if _struct(hub, name) is False:
            return False
        fname = hub.names[name]
    entry = hub.slots.get(fname) if fname else None
    if entry is None:
        return False
    if ctypes.c_uint64.from_address(entry).value != fname:  # the entry moved (a key removed): look again
        hub.count = -1
        return False
    return bool(ctypes.c_uint8.from_address(entry + hub.layout[2]).value & hub.layout[3])


# endregion

_elect(_hub())
