"""
The toggle's keys and buttons: the game's own sprint binding (read from its live input mappings,
so a key the player rebound in the game's Options counts) or a key picked in the mod's options.

Keys and controller buttons come from the game's own input (gamekeys), only while the game window
is in front.
"""

from __future__ import annotations

import ctypes
from typing import Any

from . import gamekeys

SPRINT_ACTION = "Action_Sprint"
# The game's defaults, until its mappings have been read once (they're only there on foot)
DEFAULT_SPRINT = ("LeftShift", "Gamepad_LeftThumbstick")
FIRE_ACTION, ZOOM_ACTION = "Action_Fire", "Action_Zoom"
BUSY_ACTIONS = (FIRE_ACTION, ZOOM_ACTION)
ACTIONS = frozenset({SPRINT_ACTION, *BUSY_ACTIONS})
DEFAULT_FIRE = ("LeftMouseButton", "Gamepad_RightTrigger")
DEFAULT_ZOOM = ("RightMouseButton", "Gamepad_LeftTrigger")
REFRESH_S = 10.0  # the game's binding is re-read this often on foot (0.46 ms a read, 2026-10-29)

# ctypes.windll, not PyDLL: these calls must let go of the GIL
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32

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


def refresh(pc: Any, pawn: Any, now: float, address: int | None = None) -> None:
    """
    Reads the keys bound to the game's Sprint, Fire and Zoom actions from the player's applied
    mappings (their own rebinds included) when the pawn changes and every REFRESH_S on foot. The
    on-foot mappings aren't applied in a vehicle, so the last ones read stay in use there.
    """
    if address is None:
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


def any_down(names: tuple[str, ...]) -> bool:
    """
    Whether any of these Unreal keys (keyboard, mouse or gamepad) is held, from the game's own input
    (gamekeys, shared by our mods; False while the game isn't in front). Until 2026-10-30 keys came
    from Windows (GetAsyncKeyState) and pads from XInput; the game's names cover both.
    """
    for name in names:
        if name and name != "None" and gamekeys.down(name):
            return True
    return False


# endregion
