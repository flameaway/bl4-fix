"""
Kairosdex for Borderlands 4: a dex of the game's bosses and the legendaries they drop.

Beat a boss and it's registered in the dex, with its drop table: every legendary and pearl it
has a dedicated drop for, what kind of item each is, and the chance per kill (normal, at your UVH
level, and in its Big Encore). The Items tab lists every legendary and pearl and who drops it,
with the bosses' pictures and buttons to set a waypoint on (or travel to) a boss's Big Encore
machine. Items stay "???" until you've had one ("Obtained items only", on by default, so beating a
boss doesn't spoil its drops); with that off, a boss's drops show once you beat it.

Open it with F8 (in game or in any menu), from the Kairosdex entry in the pause menu, or with the
`dex` console command. Search by boss, area or item name (click the search box and type), filter
by region, item type or manufacturer, and sort by name, number, area, type or chance.

The dex is account-wide: a boss beaten by any character counts (kills are kept per character in
progress.py). A boss counts when you're in the fight as it dies (tracker.py). Bosses beaten
before the mod come from the game's own saves (savegame.py: activities finished on the profile,
every character's finished missions). The dex itself is made from the game's own data (tools/gen_weapondex.py ->
dex.json); if a game patch changes that data the screen says so, and the dex needs rebuilding.

The first time a boss is beaten or a legendary or pearl is obtained, the game's own big HUD
banner celebrates it with its picture (celebrate.py, drawn by celebrate_page/: option
"Celebrations").

The screen is HTML in the game's menu page, through the BL4 Mod Menu (screen.py), which must be
installed. Dev options: "Unlock everything" shows the whole dex; "Forget this character's bosses"
starts it over. `dex beat <boss>` / `dex forget <boss>` mark one by hand.
"""

from __future__ import annotations

import argparse
import time
from typing import Any

from mods_base import (
    SETTINGS_DIR,
    BoolOption,
    ButtonOption,
    KeybindOption,
    NestedOption,
    build_mod,
    command,
    get_pc,
    hook,
)
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import BoundFunction, UObject, WrappedStruct

from . import bake, celebrate, data, progress, savegame, screen, thumbs, tracker

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

GAME_DIR = SETTINGS_DIR.parent.parent
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
BOSS_DIED_FUNC = "/Script/OakGame.OakBossFight:BossDied"


def log(msg: str) -> None:
    logging.info(f"[Kairosdex] {msg}")


def beaten(boss_id: str) -> None:
    first = celebrate.first_kill(boss_id)  # asked before the win is recorded
    if progress.mark(boss_id):
        screen.say(f"{data.boss_name(boss_id)} registered in the Kairosdex!")
        if first:
            celebrate.boss(boss_id)


# region Options


def _changed(*_: Any) -> None:
    screen._changed()  # noqa: SLF001


open_key = KeybindOption(
    "Open key",
    "F8",
    description="Opens and closes the Kairosdex. Works in game and in the game's menus.",
)

obtained_option = BoolOption(
    "Obtained items only",
    True,
    description=(
        "Shows an item in the dex only once you've had one (in a backpack, equipped or in the bank, on any"
        " character); the rest read \"???\", even in a beaten boss's drop list. Off: every drop of a beaten"
        " boss shows."
    ),
    on_change=_changed,
)

list_hover_option = BoolOption(
    "Item card on hover in the item list",
    False,
    description=(
        "Hovering an item in the Items list shows its item card beside it. A boss's drop list always"
        " shows the card on hover."
    ),
    on_change=_changed,
)

celebrate_option = BoolOption(
    "Celebrations",
    True,
    description=(
        "The game's big banner, with the boss's or item's picture, the first time you beat a boss or get a"
        " legendary or pearl (on any character). Turning this on or off applies from the next game start."
    ),
    on_change_while_enabled=lambda _, value: celebrate.sync(bool(value)),
)

unlock_option = BoolOption(
    "Unlock everything",
    False,
    description=(
        "Dev option: shows every boss and every item in the dex, beaten or not. Your progress is"
        " kept and shows again when you turn this off."
    ),
    on_change=_changed,
)


def _forget_all(_: ButtonOption) -> None:
    if progress.character() is None:
        screen.say("Load a character first.")
        return
    progress.forget()
    screen.say("This character's Kairosdex was cleared.")


forget_button = ButtonOption(
    "Forget this character's bosses",
    description="Dev option: clears the bosses the mod saw this character beat (other characters' and the game save's stay).",
    on_press=_forget_all,
)

open_button = ButtonOption(
    "Open the Kairosdex",
    description="Opens the dex.",
    on_press=lambda _: screen.request_open(),
)

bake_button = ButtonOption(
    "Take boss pictures",
    description=(
        "Takes the picture of every boss that doesn't have one yet (about 4 seconds each, done once:"
        " the pictures are saved and every later game uses them). Each boss is spawned on the ground in"
        " front of you, photographed and removed; meanwhile you can't be hurt and AI ignores you. Stand in"
        " an open space, face it, and stay in gameplay with no menus open until it's done."
    ),
    on_press=lambda _: _bake_command(["all"]),
)

items_button = ButtonOption(
    "Take item pictures",
    description=(
        "Takes the inventory picture of every legendary that doesn't have one yet (about 4 seconds each,"
        " done once). The game rolls each item out of sight, renders it the way the inventory does, and the"
        " drop is removed. Host only."
    ),
    on_press=lambda _: _bake_command(["items"]),
)

dev_options = NestedOption(
    "Dev options",
    [unlock_option, forget_button, bake_button, items_button],
    description="Testing tools: unlock the whole dex, clear this character's progress, take the boss and item pictures.",
)

screen.S.unlocked = lambda: bool(unlock_option.value)
screen.S.obtained = lambda: bool(obtained_option.value)
screen.S.list_hover = lambda: bool(list_hover_option.value)


# endregion
# region Console


@command(
    "dex",
    description=(
        "Opens the Kairosdex. `dex beat <boss>` / `dex forget <boss>` mark a boss by hand. `dex bake all`"
        " (or `dex bake <boss>`) takes the bosses' pictures, `dex bake items` the legendaries' pictures,"
        " `dex bake stop` stops."
    ),
)
def dex_command(args: argparse.Namespace) -> None:
    words = list(args.words)
    if not words:
        # The console is still open on this frame; open once it has closed
        S.pending_open = time.monotonic() + 0.3
        return
    verb, name = words[0].lower(), " ".join(words[1:])
    if verb == "celebrate":
        # dex celebrate boss|legendary|pearl [name]: shows a banner now (records nothing)
        kind, _, rest = name.partition(" ")
        if kind not in ("boss", "legendary", "pearl"):
            log("Usage: dex celebrate boss|legendary|pearl [name]")
            return
        log(celebrate.test(kind, rest.strip()))
        return
    if verb == "bake":
        _bake_command(words[1:])
        return
    boss = data.find_boss(name) if name else None
    if verb not in ("beat", "forget") or (name and boss is None):
        log("Usage: dex | dex beat <boss name> | dex forget <boss name>  (part of a name works if it's unique)")
        return
    if boss is None:
        log("Which boss? e.g. dex beat horace")
        return
    if verb == "beat":
        beaten(boss)
    else:
        progress.forget(boss)
        screen.say(f"{data.boss_name(boss)} forgotten.")


dex_command.add_argument("words", nargs="*")


def _bake_done(summary: str) -> None:
    bake.write_index(GAME_DIR)
    screen.say(summary)


def _bake_command(words: list[str]) -> None:
    # dex bake [all] | dex bake redo | dex bake <boss name> | dex bake stop
    # Add "sky" to spawn them high above you instead of on the ground in front. (dev: source=<n>)
    source = bake.CAPTURE_SOURCE_HDR
    ground = True
    rest = []
    for word in words:
        if word.lower().startswith("source="):  # dev: a SceneCapture source (7 = base colour)
            source = int(word.split("=", 1)[1])
        elif word.lower() == "sky":
            ground = False
        else:
            rest.append(word)
    target = " ".join(rest).strip().lower()
    if target == "stop":
        bake.stop("stopped")
        thumbs.stop("stopped")
        return
    if target in ("items", "items redo"):
        every = list(data.dex()["items"])
        have = set() if target == "items redo" else set(thumbs.baked(GAME_DIR))
        keys = [k for k in every if k not in have]
        if not keys:
            log("Every item already has a picture (dex bake items redo takes them all again).")
            return
        log(thumbs.start(GAME_DIR, keys, on_finish=screen.say))
        return
    bake.recover(GAME_DIR)
    every = [b["id"] for b in data.dex()["bosses"]]
    if target in ("", "all"):
        skip = set(bake.baked(GAME_DIR)) | set(bake.crashed(GAME_DIR))
        ids = [i for i in every if i not in skip]  # only the missing ones, so it can pick up again
        if not ids:
            log("Every boss already has a picture (dex bake redo takes them all again).")
            return
    elif target == "redo":
        crashed = set(bake.crashed(GAME_DIR))
        ids = [i for i in every if i not in crashed]
    elif (boss := data.find_boss(target)) is not None:
        ids = [boss]
    else:
        log("Which boss? e.g. dex bake horace, or dex bake all")
        return
    log(bake.start(GAME_DIR, ids, source=source, ground=ground, on_finish=_bake_done))


# endregion
# region Hooks


class S:
    pending_open = 0.0
    key_was_down = True
    registered = False
    ticked_at = last_call = 0.0


@hook(BOSS_DIED_FUNC, Type.PRE)
def boss_died(_obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    try:
        actor = args.DeadActor
        if actor is not None and (boss := tracker.identify(actor)) is not None:
            beaten(boss)
    except Exception as ex:  # noqa: BLE001 - never break the game's own handling
        logging.error(f"[Kairosdex] {ex!r}")


@hook(TICK_FUNC, Type.POST)
def tick(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    now = time.perf_counter()
    gap, S.last_call = now - S.last_call, now
    if gap < FRAME_GAP_S and now - S.ticked_at < MAX_WAIT_S:
        return
    S.ticked_at = now
    try:
        _tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Kairosdex] {ex!r}")
        logging.exception(ex)


def _tick() -> None:
    if not S.registered:
        # The Mod Menu may load (or reload) after us
        S.registered = screen.register(GAME_DIR)
    screen.tick()
    tracker.tick(beaten)
    savegame.tick(screen._changed)  # noqa: SLF001
    celebrate.tick()
    bake.tick()
    if S.pending_open and time.monotonic() > S.pending_open:
        S.pending_open = 0.0
        screen.request_open()
    keys = screen._menu_module("keys")  # noqa: SLF001
    if keys is not None and get_pc(possibly_loading=True) is not None:
        down = keys.key_down(open_key.value)
        if down and not S.key_was_down and not screen.S.typing:
            screen.toggle()
        S.key_was_down = down


def _enable() -> None:
    S.key_was_down = True
    celebrate.setup(GAME_DIR, lambda: bool(celebrate_option.value))
    celebrate.sync(bool(celebrate_option.value))
    S.registered = screen.register(GAME_DIR)
    if not S.registered:
        log("Waiting for BL4 Mod Menu: the Kairosdex screen lives in its page.")


def _disable() -> None:
    if bake.running():
        bake.stop("stopped (mod disabled)")
    thumbs.stop("stopped (mod disabled)")
    screen.unregister()
    tracker.forget()
    celebrate.forget()
    celebrate.sync(False, wait=True)  # small files only; done before a re-enable's sync can start
    S.registered = False


# endregion

mod = build_mod(
    options=[open_button, open_key, obtained_option, list_hover_option, celebrate_option, dev_options],
    keybinds=[],
    hooks=[tick, boss_died],
    commands=[dex_command],
    on_enable=_enable,
    on_disable=_disable,
)
