"""
The toggle's keys and buttons: the game's own sprint binding (read from its live input mappings,
so a key the player rebound in the game's Options counts) or a key picked in the mod's options.

Keys are read from Windows (GetAsyncKeyState, through BL4 Mod Menu's key thread when it's loaded:
on the game thread it took up to ~100 ms on a streamed desktop) and controllers through XInput,
and only while the game window is in front.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

SPRINT_ACTION = "Action_Sprint"
# The game's defaults, until its mappings have been read once (they're only there on foot)
DEFAULT_SPRINT = ("LeftShift", "Gamepad_LeftThumbstick")
FIRE_ACTION, ZOOM_ACTION = "Action_Fire", "Action_Zoom"
BUSY_ACTIONS = (FIRE_ACTION, ZOOM_ACTION)
ACTIONS = frozenset({SPRINT_ACTION, *BUSY_ACTIONS})
DEFAULT_FIRE = ("LeftMouseButton", "Gamepad_RightTrigger")
DEFAULT_ZOOM = ("RightMouseButton", "Gamepad_LeftTrigger")
REFRESH_S = 10.0  # the game's binding is re-read this often on foot (0.46 ms a read, 2026-10-29)
PAD_PROBE_S = 2.0  # how often empty controller slots are checked for a new pad
TRIGGER_DOWN = 30  # of 255, XInput's own threshold

# ctypes.windll, not PyDLL: these calls must let go of the GIL
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

_VIRTUAL_KEYS = {
    "Tab": 0x09, "Enter": 0x0D, "CapsLock": 0x14, "SpaceBar": 0x20, "LeftShift": 0xA0,
    "RightShift": 0xA1, "LeftControl": 0xA2, "RightControl": 0xA3, "LeftAlt": 0xA4, "RightAlt": 0xA5,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Left": 0x25, "Up": 0x26, "Right": 0x27, "Down": 0x28, "BackSpace": 0x08,
    "Tilde": 0xC0, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF, "Semicolon": 0xBA, "Equals": 0xBB,
    "Hyphen": 0xBD, "LeftBracket": 0xDB, "RightBracket": 0xDD, "Backslash": 0xDC, "Apostrophe": 0xDE,
    "LeftMouseButton": 0x01, "RightMouseButton": 0x02, "MiddleMouseButton": 0x04,
    "ThumbMouseButton": 0x05, "ThumbMouseButton2": 0x06,
}  # fmt: skip
_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]

# Unreal gamepad key -> XInput button bit (triggers are separate)
PAD_BUTTONS = {
    "Gamepad_DPad_Up": 0x0001, "Gamepad_DPad_Down": 0x0002, "Gamepad_DPad_Left": 0x0004,
    "Gamepad_DPad_Right": 0x0008, "Gamepad_Special_Right": 0x0010, "Gamepad_Special_Left": 0x0020,
    "Gamepad_LeftThumbstick": 0x0040, "Gamepad_RightThumbstick": 0x0080,
    "Gamepad_LeftShoulder": 0x0100, "Gamepad_RightShoulder": 0x0200,
    "Gamepad_FaceButton_Bottom": 0x1000, "Gamepad_FaceButton_Right": 0x2000,
    "Gamepad_FaceButton_Left": 0x4000, "Gamepad_FaceButton_Top": 0x8000,
}  # fmt: skip
LEFT_TRIGGER, RIGHT_TRIGGER = "Gamepad_LeftTrigger", "Gamepad_RightTrigger"
# The controller choices in the options, after "Game's sprint button" and before "Off"
PAD_CHOICES = {
    "Left stick click (L3)": "Gamepad_LeftThumbstick",
    "Right stick click (R3)": "Gamepad_RightThumbstick",
    "Left bumper (LB)": "Gamepad_LeftShoulder",
    "Right bumper (RB)": "Gamepad_RightShoulder",
    "D-pad up": "Gamepad_DPad_Up",
    "D-pad down": "Gamepad_DPad_Down",
    "D-pad left": "Gamepad_DPad_Left",
    "D-pad right": "Gamepad_DPad_Right",
}


def virtual_key(name: str | None) -> int | None:
    """Windows key code for an Unreal key name (None for gamepad keys and unknown names)."""
    if not name or name == "None":
        return None
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in _DIGITS:
        return ord("0") + _DIGITS.index(name)
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:  # noqa: PLR2004
        return 0x70 + int(name[1:]) - 1
    if name.startswith("NumPad") and name[6:] in _DIGITS:
        return 0x60 + _DIGITS.index(name[6:])
    return _VIRTUAL_KEYS.get(name)


# region The game's binding


class GameBinding:
    keys: tuple[str, ...] = DEFAULT_SPRINT
    # Fire and aim: held, they win over sprint (the weapon's state lags the press by a few frames)
    busy: tuple[str, ...] = DEFAULT_FIRE + DEFAULT_ZOOM
    actions: dict[str, tuple[str, ...]] = {  # noqa: RUF012
        SPRINT_ACTION: DEFAULT_SPRINT, FIRE_ACTION: DEFAULT_FIRE, ZOOM_ACTION: DEFAULT_ZOOM,
    }
    read_at = -1e9
    pawn = 0  # address of the pawn it was last read for


def refresh(pc: Any, pawn: Any, now: float) -> None:
    """
    Reads the keys bound to the game's Sprint, Fire and Zoom actions from the player's applied
    mappings (their own rebinds included) when the pawn changes and every REFRESH_S on foot. The
    on-foot mappings aren't applied in a vehicle, so the last ones read stay in use there.
    """
    address = pawn._get_address()
    if address == GameBinding.pawn and now - GameBinding.read_at < REFRESH_S:
        return
    GameBinding.pawn, GameBinding.read_at = address, now
    player_input = pc.PlayerInput
    if player_input is None:
        return
    found: dict[str, list[str]] = {}
    for m in player_input.EnhancedActionMappings:
        action = m.Action
        if action is not None and action.Name in ACTIONS:
            found.setdefault(action.Name, []).append(m.Key.KeyName)
    # Each action only while the game has it mapped: Fire comes and goes with the context
    for name, keys in found.items():
        keys = [k for k in keys if k != "None"]
        if keys:
            GameBinding.actions[name] = tuple(keys)
    GameBinding.keys = GameBinding.actions[SPRINT_ACTION]
    GameBinding.busy = tuple(k for name in BUSY_ACTIONS for k in GameBinding.actions[name])


# endregion
# region Reading keys and buttons


def game_focused() -> bool:
    hwnd = _user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _async_down(vk: int) -> bool:
    """
    Whether a key is held while the game is in front. Read by BL4 Mod Menu's key thread when it's
    loaded: GetAsyncKeyState can take ~100 ms on the game thread (a streamed desktop, 2026-10-24).
    """
    keys = sys.modules.get("bl4_mod_menu.keys")
    if keys is not None and hasattr(keys, "_Keys"):
        return keys.vk_down(vk)
    return game_focused() and bool(_user32.GetAsyncKeyState(vk) & 0x8000)


class _XInputGamepad(ctypes.Structure):
    _fields_ = [  # noqa: RUF012
        ("wButtons", ctypes.c_ushort), ("bLeftTrigger", ctypes.c_ubyte),
        ("bRightTrigger", ctypes.c_ubyte), ("sThumbLX", ctypes.c_short),
        ("sThumbLY", ctypes.c_short), ("sThumbRX", ctypes.c_short), ("sThumbRY", ctypes.c_short),
    ]  # fmt: skip


class _XInputState(ctypes.Structure):
    _fields_ = [("dwPacketNumber", ctypes.c_ulong), ("Gamepad", _XInputGamepad)]  # noqa: RUF012


try:
    _xinput = ctypes.windll.xinput1_4
except OSError:
    _xinput = None


class _Pads:
    connected: set[int] = set()  # noqa: RUF012
    probed_at = 0.0
    state = _XInputState()


def _pad_down(names: tuple[str, ...]) -> bool:
    if _xinput is None or not names:
        return False
    mask = 0
    for name in names:
        mask |= PAD_BUTTONS.get(name, 0)
    left, right = LEFT_TRIGGER in names, RIGHT_TRIGGER in names
    if not (mask or left or right):
        return False
    # Microsoft: don't read empty slots every frame (slow on some systems); look for new pads every
    # few seconds instead
    now = time.monotonic()
    pads = range(4) if now - _Pads.probed_at >= PAD_PROBE_S else tuple(_Pads.connected)
    if len(pads) == 4:  # noqa: PLR2004
        _Pads.probed_at = now
    down = False
    for pad in pads:
        if _xinput.XInputGetState(pad, ctypes.byref(_Pads.state)) != 0:
            _Pads.connected.discard(pad)
            continue
        _Pads.connected.add(pad)
        g = _Pads.state.Gamepad
        down = down or bool(g.wButtons & mask) or (left and g.bLeftTrigger > TRIGGER_DOWN) or (
            right and g.bRightTrigger > TRIGGER_DOWN
        )
    return down


def any_down(names: tuple[str, ...]) -> bool:
    """Whether any of these Unreal keys (keyboard, mouse or gamepad) is held."""
    for name in names:
        vk = virtual_key(name)
        if vk is not None and _async_down(vk):
            return True
    return _pad_down(tuple(n for n in names if n.startswith("Gamepad_")))


# endregion
