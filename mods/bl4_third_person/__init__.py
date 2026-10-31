"""
Third Person for Borderlands 4: a hotkey switches to an over-the-shoulder third-person camera.

How it fits together:

- BL4 already has third-person camera modes (action skills, vehicles, cutscenes). We borrow two:
  Exo Soldier's heavy-weapon camera (camMode_exo_heavyarms) as the hip camera, and the unused
  ThirdPerson_OverShoulder as the aim-in camera. Both follow the crosshair and pull in when
  there's a wall behind you. camera_defs.py finds their definitions in memory so we can set the
  shoulder offset, distance and height, and the aim-in zoom.
- Aiming is the game's own: right mouse still zooms the gun, with the accuracy and handling bonuses
  that come with it. Only the camera differs. When the gun starts zooming we blend to the aim-in
  camera (closer, over the shoulder, FOV narrowed by the gun's own zoom), and back when it stops.
- When the game asks for a first-person camera (normal play, slides, ziplines, Fight For Your
  Life) we put the third-person one back. Vehicles, ladders, action skills and cutscenes keep
  their own cameras.
- The HUD hides its crosshair while zoomed, so crosshair.py draws one while aiming in third person.
- Keys are read from the Windows keyboard state like our other mods, and only while the game window
  is in front and the game isn't paused.
"""

from __future__ import annotations

import ctypes
import time
from typing import Any

import unrealsdk
from mods_base import BoolOption, KeybindOption, SliderOption, build_mod, get_pc
from unrealsdk import logging
from unrealsdk.unreal import UObject, WeakPointer

from . import camera_defs, frametick, gamekeys, health
from .crosshair import Crosshair

__version__ = "0.1.2"
__version_info__ = (0, 1, 2)

HIP_MODE = "camMode_exo_heavyarms"
AIM_MODE = "ThirdPerson_OverShoulder"
OUR_MODES = (HIP_MODE, AIM_MODE)
# Modes we replace: the ones with bIsFirstPerson that are just normal movement
FIRST_PERSON_MODES = ("Default", "Slide", "FFYL", "Ziplines", "GroundSlamExit", "GroundSlamExitFrom3P")
ZOOMING_IN, ZOOMED = 1, 2  # EZoomState
ENTER_BLEND = 0.25  # s, from first person or another mode
PREWARM_DELAY_S = 60.0  # after the first pawn, the background camera scan starts (if 3P is off)
SWAP_BLEND = 0.2
DEFAULT_ZOOM_BLEND = 0.2

# ctypes.windll, not PyDLL: these calls must let go of the GIL. Only the focus check uses them; keys
# come from the game's own input (gamekeys).
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_character_class = unrealsdk.find_class("OakCharacter")
_zoom_class = unrealsdk.find_class("WeaponBehavior_Zoom")


def log(msg: str) -> None:
    logging.info(f"[Third Person] {msg}")


# region Options


def _changed(_option: Any, _value: Any) -> None:
    State.dirty = True


toggle_key = KeybindOption(
    "Third person key",
    "H",
    description="Switches between first and third person.",
)
swap_key = KeybindOption(
    "Swap shoulder key",
    "K",
    description="Moves the camera to your other shoulder.",
)
distance_option = SliderOption(
    "Camera distance (cm)",
    300,
    150,
    600,
    10,
    True,
    description="How far behind you the camera sits.",
    on_change_while_enabled=_changed,
)
shoulder_option = SliderOption(
    "Shoulder offset (cm)",
    60,
    0,
    150,
    5,
    True,
    description="How far to the side the camera sits, so you aren't in the way of the crosshair.",
    on_change_while_enabled=_changed,
)
height_option = SliderOption(
    "Camera height (cm)",
    20,
    -50,
    100,
    5,
    True,
    description="How high above your eyes the camera sits.",
    on_change_while_enabled=_changed,
)
aim_distance_option = SliderOption(
    "Aim-in camera distance (cm)",
    140,
    60,
    400,
    10,
    True,
    description="How far behind you the camera sits while you aim.",
    on_change_while_enabled=_changed,
)
aim_zoom_option = SliderOption(
    "Aim-in zoom (%)",
    100,
    0,
    100,
    5,
    True,
    description=(
        "How much aiming zooms in, compared with aiming down sights in first person. 100% zooms as"
        " much as the gun's sights would."
    ),
    on_change_while_enabled=_changed,
)
crosshair_option = BoolOption(
    "Crosshair while aiming",
    True,
    description="Draws a crosshair while you aim in third person (the game hides its own).",
)
start_option = BoolOption(
    "Remember third person",
    True,
    description="Stays in third person between play sessions if you left it on.",
)
saved_on_option = BoolOption("Third person on", False, is_hidden=True)


# endregion
# region Keys

_MOUSE_KEYS = {
    "LeftMouseButton": 0x01, "RightMouseButton": 0x02, "MiddleMouseButton": 0x04,
    "ThumbMouseButton": 0x05, "ThumbMouseButton2": 0x06,
}
_NAMED_KEYS = {
    "Tab": 0x09, "Enter": 0x0D, "CapsLock": 0x14, "SpaceBar": 0x20, "LeftShift": 0xA0,
    "RightShift": 0xA1, "LeftControl": 0xA2, "RightControl": 0xA3, "LeftAlt": 0xA4, "RightAlt": 0xA5,
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Tilde": 0xC0, "Comma": 0xBC, "Period": 0xBE, "Slash": 0xBF, "Semicolon": 0xBA,
    "LeftBracket": 0xDB, "RightBracket": 0xDD, "Backslash": 0xDC, "Apostrophe": 0xDE,
}
_DIGITS = ["Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine"]


def virtual_key(name: str | None) -> int | None:
    """Windows key code for an Unreal key name."""
    if not name:
        return None
    if len(name) == 1 and name.isalnum():
        return ord(name.upper())
    if name in _DIGITS:
        return ord("0") + _DIGITS.index(name)
    if name[0] == "F" and name[1:].isdigit() and 1 <= int(name[1:]) <= 24:  # noqa: PLR2004
        return 0x70 + int(name[1:]) - 1
    if name.startswith("NumPad") and name[6:] in _DIGITS:
        return 0x60 + _DIGITS.index(name[6:])
    return _MOUSE_KEYS.get(name) or _NAMED_KEYS.get(name)


def _game_window_focused() -> bool:
    hwnd = _user32.GetForegroundWindow()
    pid = ctypes.c_ulong()
    _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value == _kernel32.GetCurrentProcessId()


def _async_down(vk: int) -> bool:
    """
    Whether a key is held, from the game's own input (gamekeys, shared by our mods; False while the
    game isn't in front). Until 2026-10-30 this read Windows (GetAsyncKeyState, through BL4 Mod
    Menu's key thread when it was loaded).
    """
    return gamekeys.vk_down(vk)

class Keys:
    was_down: dict[int, bool] = {}  # noqa: RUF012
    codes: dict[str | None, int | None] = {}  # noqa: RUF012 - key name -> virtual_key(name) (string work, ~5 us)

    @classmethod
    def pressed(cls, option: KeybindOption) -> bool:
        """True once per press of the option's key."""
        name = option.value
        vk = cls.codes.get(name, -1)
        if vk == -1:
            vk = cls.codes[name] = virtual_key(name)
        if vk is None:
            return False
        down = _async_down(vk)
        was = cls.was_down.get(vk, True)  # a key held when we start doesn't count
        cls.was_down[vk] = down
        return down and not was


# endregion
# region Camera


class State:
    on = False
    side = 1.0  # +1 right shoulder, -1 left
    dirty = False  # options changed: rewrite the modes and restart ours
    frame_time = -1.0  # camera clock of the last frame we handled
    pawn = WeakPointer()
    waiting_logged = False
    ours = False  # whether we put the camera in the mode it's in (not, say, Exo's action skill)
    restore_at: float | None = None  # camera clock time to put the vanilla definitions back
    prewarm_at = 0.0  # time.monotonic() to start the camera scan in the background (0 = no)
    # Third person off with nothing to put back (see _idle_frame): the next full pass, and a
    # toggle press waiting for it (toggle, swap)
    full_at = 0.0
    keys: tuple[bool, bool] | None = None


def _restore_defs() -> None:
    State.restore_at = None
    defs = camera_defs.Finder.defs
    if defs is not None and defs.intact():
        defs.restore()


def _zoom_behavior(pawn: UObject) -> UObject | None:
    slots = pawn.ActiveWeapons.Slots
    weapon = slots[0].Weapon if len(slots) else None
    if weapon is None:
        return None
    return next((b for b in weapon.behaviors if b and b.Class._inherits(_zoom_class)), None)


def _write_modes(defs: camera_defs.Defs, pawn: UObject) -> bool:
    side = State.side * shoulder_option.value
    height = height_option.value
    zoom = _zoom_behavior(pawn)
    gun_scale = zoom.ZoomFOVScale.Value if zoom is not None else 1.0
    aim_scale = 1.0 - (1.0 - gun_scale) * aim_zoom_option.value / 100
    return defs.hip.set(-distance_option.value, side, height, 1.0) and defs.aim.set(
        -aim_distance_option.value, side, height, max(0.05, aim_scale),
    )


def _zoom_blend(pawn: UObject) -> float:
    zoom = _zoom_behavior(pawn)
    return float(zoom.ZoomDuration.Value) if zoom is not None else DEFAULT_ZOOM_BLEND


def _set_mode(camera: UObject, pawn: UObject, mode: str, blend: float, *, restart: bool = False) -> None:
    camera.SetActorCameraMode(pawn, mode, "None", blend, False, restart)


def _update(pc: UObject, pawn: UObject) -> None:
    if not State.on and State.restore_at is None:
        Crosshair.show(pc, False)
        return  # first person and nothing to put back: leave the camera alone
    camera = pc.PlayerCameraManager
    mode = str(camera.GetActorCameraMode(pawn))
    if mode not in OUR_MODES:
        State.ours = False
    if not State.on:
        Crosshair.show(pc, False)
        # Back to vanilla once the blend out of our mode is over, so Exo Soldier's heavy-arms
        # action skill (whose mode we borrow) gets its own camera again
        # (or at once if the clock went back: a new map restarts it)
        at = State.restore_at
        if at is not None and not at - ENTER_BLEND <= State.frame_time < at:
            _restore_defs()
        return
    State.restore_at = None

    defs = camera_defs.Finder.get()
    if defs is None:
        if not State.waiting_logged:
            log("Getting the third-person camera ready (takes a few seconds once per game)...")
            State.waiting_logged = True
        return

    if mode in OUR_MODES and not State.ours:
        # The game put it there (the action skill was already on when third person was turned on):
        # leave its camera alone until it's done
        Crosshair.show(pc, False)
        return

    aiming = int(pawn.ZoomState.State) in (ZOOMING_IN, ZOOMED)
    want = AIM_MODE if aiming else HIP_MODE
    if mode in FIRST_PERSON_MODES or (mode in OUR_MODES and mode != want):
        if not _write_modes(defs, pawn):
            return  # the definitions moved; Finder rescans
        blend = _zoom_blend(pawn) if mode in OUR_MODES else ENTER_BLEND
        _set_mode(camera, pawn, want, blend)
        mode = want
        State.ours = True
        State.dirty = False
    elif State.dirty and mode in OUR_MODES:
        if _write_modes(defs, pawn):
            _set_mode(camera, pawn, mode, SWAP_BLEND, restart=True)
        State.dirty = False

    Crosshair.show(pc, crosshair_option.value and mode == AIM_MODE)


def set_third_person(on: bool) -> None:
    State.on = on
    saved_on_option.value = on
    mod.save_settings()
    if on:
        Keys.was_down.clear()  # a key held now (H itself) only counts once it's pressed again
        return
    State.restore_at = State.frame_time + ENTER_BLEND
    pc = get_pc(possibly_loading=True)
    pawn = pc.Pawn if pc else None
    if pawn is None or not pawn.Class._inherits(_character_class):
        return
    camera = pc.PlayerCameraManager
    if State.ours and str(camera.GetActorCameraMode(pawn)) in OUR_MODES:
        _set_mode(camera, pawn, "Default", ENTER_BLEND)
    State.ours = False
    Crosshair.show(pc, False)


OFF_FULL_S = 0.5  # third person off: the character (prewarm, crosshair) is looked at this often


def _idle_frame() -> bool:
    """
    Third person off with nothing to put back (the usual state): only the keys every frame, the
    full pass (get_pc, the character, the camera clock: ~45 us a frame, 2026-10-29) every
    OFF_FULL_S. A toggle press reads the camera clock now and leaves the press to the next frame's
    full pass, which acts on it only if the clock moved (not paused in a menu, as before): +1 frame
    for H. True when this frame needs nothing more.
    """
    if State.keys is not None or time.monotonic() >= State.full_at:
        return False
    # The swap key does nothing while off: not read here (set_third_person(True) forgets the keys'
    # last states, so a K held from before doesn't count as a press then)
    if not Keys.pressed(toggle_key):
        return True
    pc = get_pc(possibly_loading=True)
    camera = pc.PlayerCameraManager if pc is not None else None
    if camera is not None:
        State.frame_time = camera.CameraModeInputs.CurrentTime
        State.keys = (True, False)
    return True


@frametick.tick("bl4_third_person")
def on_frame() -> None:
    """
    Once per engine frame (the shared frame tick: the first camera update's modifiers, as the
    BlueprintModifyPostProcess PRE hook this was before). Needs none of the hook's arguments.
    """
    try:
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
        if not State.on and State.restore_at is None and not Crosshair.shown:
            if _idle_frame():
                return
            State.full_at = time.monotonic() + OFF_FULL_S
        keys, State.keys = State.keys, None
        pc = get_pc(possibly_loading=True)
        pawn = pc.Pawn if pc else None
        if pawn is None or not pawn.Class._inherits(_character_class):
            # Vehicles keep their own camera
            if pc is not None:
                Crosshair.show(pc, False)
            return
        now = pc.PlayerCameraManager.CameraModeInputs.CurrentTime
        if now == State.frame_time:
            return  # same frame, or paused in a menu
        State.frame_time = now

        if pawn is not State.pawn():
            State.pawn = WeakPointer(pawn)
            # Have the camera ready before the first key press, but the scan reads ~6 GB: not
            # during the load-in (H before then starts it at once)
            if State.on:
                camera_defs.Finder.start(urgent=True)
            elif not State.prewarm_at:
                State.prewarm_at = time.monotonic() + PREWARM_DELAY_S
        if State.prewarm_at and time.monotonic() >= State.prewarm_at:
            State.prewarm_at = 0.0
            camera_defs.Finder.start()

        # (No focus check here: _async_down is False while the game isn't in front, through the Mod
        # Menu's key snapshot or its own check. This one was 3 Win32 calls a frame, 2026-10-29)
        toggle, swap = keys if keys is not None else (Keys.pressed(toggle_key), Keys.pressed(swap_key))
        if toggle:
            set_third_person(not State.on)
        if swap and State.on:
            State.side = -State.side
            State.dirty = True
        _update(pc, pawn)
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Third Person] {ex!r}")


# endregion


def _enable() -> None:
    State.on = bool(start_option.value and saved_on_option.value)
    State.frame_time = -1.0
    State.pawn = WeakPointer()
    State.ours = False
    State.restore_at = None
    State.prewarm_at = 0.0
    State.full_at = 0.0
    State.keys = None
    Keys.was_down.clear()


def _disable() -> None:
    on = State.on
    set_third_person(False)
    saved_on_option.value = on  # disabling the mod isn't the player turning third person off
    mod.save_settings()
    Crosshair.remove()
    _restore_defs()


mod = build_mod(
    options=[
        toggle_key, swap_key, distance_option, shoulder_option, height_option,
        aim_distance_option, aim_zoom_option, crosshair_option, start_option, saved_on_option,
    ],
    hooks=[on_frame],
    on_enable=_enable,
    on_disable=_disable,
)
