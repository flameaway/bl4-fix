"""
Dialog Skipper for Borderlands 4: skip voice lines with a key, or have them skipped a moment after
they start. Two more keys speed the game up and put it back.

A port of Yeti's Dialog Skipper (GPL-3.0, see ported/README.md), rewritten to run on the game
thread and cost nothing between lines.

Every line starts through GbxDialogProvider.NetMulticast_StartDialog. We note the provider and the
line's thread id; when the delay is up (checked by a tick hook that only runs while a line is
waiting) we stop it with NetMulticast_StopDialog if that line is still the one playing. Who is
speaking comes from the world's LiveDialogSpeakerHost, found once per world.

Co-op: the host decides what plays for everyone, so a host with this mod skips lines for every
player (NetMulticast_StopDialog from the server reaches all of them). On a client the same call
only stops the line on that client's own screen and speakers, which is still a skip for them. Game
speed is host only: the host's time dilation replicates to everyone.
"""

from __future__ import annotations

import time
from typing import Any

import unrealsdk
from mods_base import ENGINE, BoolOption, SliderOption, build_mod, get_pc, hook, keybind
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

__version__ = "0.1.0"
__version_info__ = (0, 1, 0)

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
START_FUNC = "/Script/GbxGame.GbxDialogProvider:NetMulticast_StartDialog"
FADE_S = 0.1
MAX_PENDING = 32
MAX_PROVIDERS = 64
# A line that hasn't become the provider's current one this long after its delay is dropped
GIVE_UP_S = 5.0
RECHECK_S = 0.1
MAX_SPEED = 32.0
HOST_RETRY_S = 10.0
ATTITUDE_HOSTILE = 2

_statics = unrealsdk.find_class("GameplayStatics").ClassDefaultObject
_system = unrealsdk.find_class("KismetSystemLibrary").ClassDefaultObject
_teams = unrealsdk.find_class("GbxTeamFunctionLibrary").ClassDefaultObject
_host_class = unrealsdk.find_class("LiveDialogSpeakerHost")
_pc_class = unrealsdk.find_class("OakPlayerController")
_character_class = unrealsdk.find_class("OakCharacter")


def log(msg: str) -> None:
    logging.info(f"[Dialog Skipper] {msg}")


auto_option = BoolOption(
    "Auto skip",
    False,
    description="Skip every voice line a moment after it starts.",
)
delay_option = SliderOption(
    "Skip delay",
    0.3,
    0.1,
    5.0,
    0.1,
    is_integer=False,
    description=(
        "Seconds a line plays before auto skip stops it. Raise it if lines slip through (mostly"
        " while you don't control your character)."
    ),
)
keep_enemy_option = BoolOption(
    "Keep enemy taunts",
    False,
    description="Auto skip leaves lines from enemies alone.",
)
keep_player_option = BoolOption(
    "Keep player lines",
    False,
    description=(
        "Auto skip leaves every line your Vault Hunter says alone. Some story lines also come from"
        " the player, so skip those with the key."
    ),
)


class State:
    # (due time, provider, thread id) of lines auto skip is waiting on
    pending: list[tuple[float, WeakPointer, int]] = []
    # Providers that started a line in this world, by address (newest last)
    providers: dict[int, WeakPointer] = {}
    world = 0  # address of the world the caches belong to
    host: WeakPointer | None = None
    host_retry_at = 0.0  # a failed search is retried this late at the soonest
    speed_changed = False
    ticking = False


def _ticking(on: bool) -> None:
    if on != State.ticking:
        State.ticking = on
        if on:
            tick.enable()
        else:
            tick.disable()


def _check_world() -> None:
    world = ENGINE.GameViewport.World
    address = world._get_address() if world is not None else 0
    if address != State.world:
        State.world = address
        State.pending.clear()
        State.providers.clear()
        State.host = None
        State.host_retry_at = 0.0


def _host() -> UObject | None:
    if State.host is not None and (host := State.host()) is not None:
        return host
    now = time.monotonic()
    if now < State.host_retry_at:
        return None
    State.host_retry_at = now + HOST_RETRY_S
    _, found = _statics.GetAllActorsOfClass(ENGINE.GameViewport.World, _host_class, [])
    if not found:
        return None
    State.host = WeakPointer(found[0])
    return found[0]


def _speaker(provider: UObject) -> UObject | None:
    """The actor voicing this provider's lines, if the speaker host knows it."""
    host = _host()
    if host is None:
        return None
    address = provider._get_address()
    for speaker in host.LiveSpeakers:
        if speaker is not None and speaker.GbxDialogProvider is not None and (
            speaker.GbxDialogProvider._get_address() == address
        ):
            return speaker.AttachedAudioImplementer
    return None


def _keep(provider: UObject) -> bool:
    """True if the options say this provider's line should play."""
    if not (keep_enemy_option.value or keep_player_option.value):
        return False
    speaker = _speaker(provider)
    if speaker is None or not speaker.Class._inherits(_character_class):
        return False
    controller = speaker.Controller
    if keep_player_option.value and controller is not None and controller.Class._inherits(_pc_class):
        return True
    if keep_enemy_option.value:
        pc = get_pc()
        pawn = pc.Pawn if pc is not None else None
        if pawn is not None and _teams.GetAttitudeTowards(speaker, pawn) == ATTITUDE_HOSTILE:
            return True
    return False


def _stop(provider: UObject) -> bool:
    thread = provider.CurrentPerformance.DialogThreadID
    if thread == 0:
        return False
    provider.NetMulticast_StopDialog(thread, FADE_S)
    return True


@hook(START_FUNC, Type.POST)
def on_line_start(obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        thread = args.params.DialogThreadID
        if thread <= 0:
            return
        _check_world()
        address = obj._get_address()
        State.providers.pop(address, None)
        State.providers[address] = WeakPointer(obj)
        if len(State.providers) > MAX_PROVIDERS:
            del State.providers[next(iter(State.providers))]
        if auto_option.value:
            State.pending.append((time.monotonic() + delay_option.value, WeakPointer(obj), thread))
            del State.pending[:-MAX_PENDING]
            _ticking(True)
    except Exception as ex:  # noqa: BLE001 - never let our hook break the game's
        logging.error(f"[Dialog Skipper] {ex!r}")


# Not in the mod's hook list: on only while a line is waiting
@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        now = time.monotonic()
        if not State.pending:
            _ticking(False)
            return
        if all(due > now for due, _, _ in State.pending):
            return
        _check_world()
        waiting: list[tuple[float, WeakPointer, int]] = []
        for due, weak, thread in State.pending:
            if due > now:
                waiting.append((due, weak, thread))
                continue
            provider = weak()
            if provider is None:
                continue
            if provider.CurrentPerformance.DialogThreadID != thread:
                # Not started yet: try again shortly; ended or replaced: nothing to do
                if provider.PendingPerformance.DialogThreadID == thread and now - due < GIVE_UP_S:
                    waiting.append((now + RECHECK_S, weak, thread))
                continue
            if not _keep(provider):
                provider.NetMulticast_StopDialog(thread, FADE_S)
        State.pending = waiting
        if not waiting:
            _ticking(False)
    except Exception as ex:  # noqa: BLE001
        State.pending.clear()
        _ticking(False)
        logging.error(f"[Dialog Skipper] {ex!r}")


@keybind("Skip dialog", description="Stops the voice lines playing right now.")
def skip_key() -> None:
    try:
        _check_world()
        stopped = 0
        for weak in list(State.providers.values()):
            if (provider := weak()) is not None and _stop(provider):
                stopped += 1
        if stopped:
            log(f"skipped {stopped} line(s)")
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Dialog Skipper] {ex!r}")


@keybind("Toggle auto skip", description="Turns Auto skip on or off.")
def toggle_auto_key() -> None:
    auto_option.value = not auto_option.value
    mod.save_settings()
    log(f"auto skip {'on' if auto_option.value else 'off'}")


def _set_speed(speed: float) -> None:
    world = ENGINE.GameViewport.World
    if world is None:
        return
    if not _system.IsServer(world):
        # The host's time dilation replicates to everyone; a client's own would only fight it
        if speed != 1.0:
            log("game speed: only the host can change it in co-op")
        return
    _statics.SetGlobalTimeDilation(world, speed)
    State.speed_changed = speed != 1.0
    log(f"game speed x{speed:g}")


@keybind("Speed up game", description="Doubles the game speed (up to x32). Host only.")
def speed_up_key() -> None:
    try:
        world = ENGINE.GameViewport.World
        if world is not None:
            _set_speed(min(MAX_SPEED, _statics.GetGlobalTimeDilation(world) * 2))
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Dialog Skipper] {ex!r}")


@keybind("Normal game speed", description="Puts the game speed back to normal.")
def normal_speed_key() -> None:
    try:
        _set_speed(1.0)
    except Exception as ex:  # noqa: BLE001
        logging.error(f"[Dialog Skipper] {ex!r}")


def _disable() -> None:
    _ticking(False)
    State.pending.clear()
    State.providers.clear()
    if State.speed_changed:
        try:
            _set_speed(1.0)
        except Exception as ex:  # noqa: BLE001
            logging.error(f"[Dialog Skipper] {ex!r}")


mod = build_mod(
    options=[auto_option, delay_option, keep_enemy_option, keep_player_option],
    keybinds=[skip_key, toggle_auto_key, speed_up_key, normal_speed_key],
    hooks=[on_line_start],
    on_disable=_disable,
)
