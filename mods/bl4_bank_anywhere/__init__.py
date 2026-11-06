"""
Bank Anywhere for Borderlands 4: open the bank (Withdraw / Deposit) anywhere with a key.

The bank screen is a UI state like every game menu (`UI.State.MENU_BANK`, the `uistate` NCS), so
we add that state to the player the way the UI Link opens the pause menu, and the game shows its
own bank screen: no machine needed. The key again (or the bank's own Esc / Exit) closes it.

The key comes from the game's own input (gamekeys, like our other mods): one dict read a frame,
the rest only on a press.
"""

from __future__ import annotations

import time

import unrealsdk
from mods_base import KeybindOption, build_mod, get_pc
from unrealsdk import logging

from . import frametick, gamekeys

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

BANK_STATE = "UI.State.MENU_BANK"
HUD_STATE = "UI.State.BASE_HUD"  # gameplay (a cinematic or a load removes it)
CHECK_S = 0.5  # the state lands a frame later: past this, it didn't take


def log(msg: str) -> None:
    logging.info(f"[Bank Anywhere] {msg}")


key_option = KeybindOption("Open the bank", "F10", description="Opens the bank anywhere. Press it again to close it.")


class S:
    was_down = True  # a key held when we start doesn't count
    check: tuple[float, bool] | None = None  # (when to check, the state wanted) after a change


def _states() -> tuple[object, object] | None:
    """(the player controller, the local player's UI state subsystem), or None."""
    pc = get_pc(possibly_loading=True)
    if pc is None or pc.Player is None or pc.Pawn is None:
        return None
    lib = unrealsdk.find_class("SubsystemBlueprintLibrary").ClassDefaultObject
    sub = lib.GetLocalPlayerSubSystemFromPlayerController(pc, unrealsdk.find_class("GbxUIUMGPlayerSubsystem"))
    return None if sub is None else (pc, sub)


def _tag(name: str) -> object:
    return unrealsdk.make_struct("GameplayTag", TagName=name)


def _has(sub: object, name: str) -> bool:
    got = sub.HasUIState(_tag(name))
    return bool(got[0] if isinstance(got, tuple) else got)


def toggle() -> None:
    found = _states()
    if found is None:
        return
    pc, sub = found
    if _has(sub, BANK_STATE):
        sub.RemoveUIStateToPlayer(pc, _tag(BANK_STATE), pc.Player)
        S.check = (time.monotonic() + CHECK_S, False)
    elif pc.bShowMouseCursor or not _has(sub, HUD_STATE):
        return  # another menu is up, or a cinematic / load: not over the game's own screens
    else:
        sub.AddUIStateToPlayer(pc, _tag(BANK_STATE), pc.Player)
        S.check = (time.monotonic() + CHECK_S, True)


@frametick.tick("bl4_bank_anywhere")  # once per engine frame (the shared Frame Tick, frametick/)
def tick() -> None:
    try:
        down = gamekeys.down(key_option.value)
        if down and not S.was_down:
            toggle()
        S.was_down = down
        if S.check is not None and time.monotonic() >= S.check[0]:
            want = S.check[1]
            S.check = None
            found = _states()
            if found is not None and _has(found[1], BANK_STATE) != want:
                log(f"the bank didn't {'open' if want else 'close'} (a game update may have changed its menu)")
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Bank Anywhere] {ex!r}")


def _enable() -> None:
    S.was_down = True
    S.check = None


# A mod with no settings file loads switched off (a manual install, issue #5): start on
from .firston import ensure_on  # noqa: E402

ensure_on(__name__)

mod = build_mod(options=[key_option], hooks=[tick], on_enable=_enable)
