"""
Easy respec for Borderlands 4: right-click any skill in the Skills menu to take one point back out.

The game only lets you undo points in the order you spent them, or reset whole trees. This lets you
pull a point from any skill, as long as the tree stays one the game would let you build: every row
keeps the points it needs in the rows above it, the branches keep the points they need in the
trunk, and no skill loses the skill it hangs from. Anything else gets the menu's error sound and a
message saying what's in the way.

How it fits together:

- The Skills menu is an HTML page. The BL4 UI Link (uilink/, shared by our mods; no Mod Menu
  needed, no game page replaced) runs respec.js in it, a loader for respec_main.js, which knows
  which skill is hovered and has the whole tree to check the rules against. All are loose files
  (skills_page/), reloaded by the page when they change (no restart needed). Until 0.1.x a pak of
  ours replaced the Skills page to load it; skills_page removes that pak after the game exits.
- BL4 keeps the right mouse button away from its menu pages, so we watch it here, and while the
  page says the Skills menu is showing, each right click bumps a counter in a loose file the page
  polls. The page works out which skill was under the cursor.
- The page talks back through its sound events (see respec_main.js): it asks for a point to come out of
  "<trunk or branch graph>, row, slot", and we spend -1 point on that skill graph node, which the
  game accepts and refunds (verified).
- The game's own "undo the last point" is also on right click, so while we're enabled we take the
  mouse button off that action (controllers keep their undo).
- The page also has an "Unspec All" button (with a confirmation) that refunds every point in all
  three trees, using the game's own per-graph reset.
"""

from __future__ import annotations

import ctypes
import sys
import time
from typing import Any

import unrealsdk
from mods_base import SETTINGS_DIR, build_mod, get_pc, hook
from unrealsdk import logging
from unrealsdk.hooks import Block, Type
from unrealsdk.unreal import BoundFunction, UObject, WeakPointer, WrappedStruct

from . import health, skills_page, uilink

__version__ = "0.2.0"
__version_info__ = (0, 2, 0)

GAME_DIR = SETTINGS_DIR.parent.parent
CHANNEL_FUNC = "/Game/UI/Scripts/ui_script_skills.ui_script_skills_C:RemovePoint"
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
# The page script, run by the BL4 UI Link in the menu page
LINK_KEY = "respec"
LINK_SCRIPT = "coui://uiresources/bl4respec/respec.js"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
_last_call = _ticked_at = 0.0
# Messages from respec_main.js have position.X = -(MARK + op * 0x10000 + arg); real sounds sit on screen
MARK = 1 << 23
OP_ACTIVE, OP_REMOVE, OP_UNSPEC_ALL = 1, 3, 4
# Node names in skill tree graphs (trunks, branches) and in the augment/capstone graphs
TREE_NODE_PREFIXES = ("Trunk - Row ", "Branch - Row ")
MODIFIER_NODE_PREFIXES = ("Augment_", "Capstone_")
# The page repeats "showing" every second; after this long without it, assume it's gone
ACTIVE_TIMEOUT = 3.0

# The Skills menu's input mappings, and the native undo we take right click away from
SKILLS_CONTEXT = "/Game/UI/Data/input/context_ui_menu_skills.context_ui_menu_skills"
UNDO_ACTION = "action_ui_skills_undo"
UNDO_KEY = "RightMouseButton"
NO_KEY = "None"

VK_LBUTTON, VK_RBUTTON = 0x01, 0x02
SM_SWAPBUTTON = 23
# ctypes.windll, not PyDLL: these calls must let go of the GIL (GetAsyncKeyState can take ~100 ms,
# e.g. while the desktop is streamed). Keys go through BL4 Mod Menu's key thread when it's loaded.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32


def log(msg: str) -> None:
    logging.info(f"[Easy Respec] {msg}")


_errors: set[str] = set()


def _report_error(ex: Exception) -> None:
    """The tick runs every frame; report each distinct problem only once."""
    message = repr(ex)
    if message not in _errors:
        _errors.add(message)
        logging.error(f"[Easy Respec] {message}")


# region Right clicks


class Page:
    """What respec_main.js last told us about the Skills page."""

    active = False
    last_heard = 0.0
    # The player the page was opened for
    pc = WeakPointer()

    @classmethod
    def showing(cls) -> bool:
        return cls.active and time.monotonic() - cls.last_heard < ACTIVE_TIMEOUT


class SignalFile:
    """
    The loose file respec_main.js polls for right clicks.

    Each click writes a new value; the page only looks for a change. The file is replaced rather
    than rewritten so the page never reads half a value, and a replace that fails because the page
    has the file open is retried on the next frame.
    """

    path = skills_page.signal_path(GAME_DIR)
    pending = False

    @classmethod
    def bump(cls) -> None:
        cls.pending = True
        cls.flush()

    @classmethod
    def flush(cls) -> None:
        if not cls.pending:
            return
        try:
            cls.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cls.path.with_suffix(".tmp")
            tmp.write_text(str(time.time_ns()), encoding="ascii")
            tmp.replace(cls.path)
            cls.pending = False
        except OSError:
            pass


def _game_window_focused() -> bool:
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
    return _game_window_focused() and bool(_user32.GetAsyncKeyState(vk) & 0x8000)


def _right_button_down() -> bool:
    # GetAsyncKeyState reads the physical buttons, so follow the "swap mouse buttons" setting
    vk = VK_LBUTTON if _user32.GetSystemMetrics(SM_SWAPBUTTON) else VK_RBUTTON
    return _async_down(vk)


_was_down = False
_last_publish = 0.0
# Set when enabled: take right click off the native undo on the next tick. At startup mods are
# enabled on another thread while the game already runs, so the mapping isn't touched from there.
_unmap_undo = False


@hook(TICK_FUNC, Type.POST)
def watch_right_clicks(_obj: UObject, _args: WrappedStruct, _ret: Any, _func: BoundFunction) -> None:
    global _was_down, _last_publish, _unmap_undo, _ticked_at, _last_call
    started = time.perf_counter()
    gap, _last_call = started - _last_call, started
    if gap < FRAME_GAP_S and started - _ticked_at < MAX_WAIT_S:
        return
    _ticked_at = started
    try:
        uilink.tick()  # runs our Skills page script (shared/uilink; does work at most once a frame overall)
        health.tick()  # "needs an update" statuses for all our mods (shared/health; a clock compare most calls)
    except Exception as ex:  # noqa: BLE001 - never let this break the game's tick
        _report_error(ex)
    if _unmap_undo:
        _unmap_undo = False
        set_native_undo_on_right_click(False)
    SignalFile.flush()
    now = time.monotonic()
    if skills_page.FROM_SOURCE and now - _last_publish >= 1.0:
        # Dev: edits to the page's scripts and styles go live within a second (the page reloads them)
        _last_publish = now
        try:
            skills_page.write_loose_files(GAME_DIR)
        except OSError:
            pass

    if not Page.showing():
        _was_down = False
        return

    # A map load replaces the menus without telling the page's script to deactivate
    if Page.pc() is not get_pc(possibly_loading=True):
        Page.active = False
        return

    down = _right_button_down()
    if down and not _was_down:
        SignalFile.bump()
    _was_down = down


# endregion
# region Removing points

# Node names per skill graph, e.g. "Trunk - Row 2 - 3"; the game's names don't change at runtime
_node_names: dict[str, list[str]] = {}


def hash24(text: str) -> int:
    """FNV-1a folded to 24 bits, the same as respec_main.js."""
    value = 0x811C9DC5
    for char in text:
        value ^= ord(char)
        value = (value * 0x01000193) & 0xFFFFFFFF
    return (value >> 24) ^ (value & 0xFFFFFF)


def _graph_name(graph: UObject) -> str:
    # GbxDefPtr only prints as "FGbxDefPtr('<name>', '<type>', {...})"
    return str(graph.ProgressGraphDef).split("'", 2)[1]


def _node_names_of(graph: UObject, name: str) -> list[str]:
    if name not in _node_names:
        store = unrealsdk.find_class("NexusConfigStoreUISkillTree").ClassDefaultObject
        _node_names[name] = [str(n) for n in store.GetNodeNames(None, graph.ProgressGraphDef, [])[1]]
    return _node_names[name]


def _player_graphs() -> list[UObject]:
    pc = get_pc()
    pawn = pc.Pawn if pc else None
    return list(pawn.GbxProgressionManager.ProgressGraphs) if pawn else []


def _find_node(graph_hash: int, row: int, slot: int) -> tuple[UObject, int, str] | None:
    """The local player's skill graph and node index the page means, or None."""
    suffix = f" - Row {row + 1} - {slot + 1}"
    found = []
    for graph in _player_graphs():
        name = _graph_name(graph)
        if hash24(name) != graph_hash:
            continue
        names = _node_names_of(graph, name)
        matches = [i for i, node in enumerate(names) if node.endswith(suffix)]
        if len(matches) == 1:
            found.append((graph, matches[0], f"{name} {names[matches[0]]}"))
    return found[0] if len(found) == 1 else None


def remove_point(graph_hash: int, row: int, slot: int) -> None:
    target = _find_node(graph_hash, row, slot)
    if target is None:
        log(f"couldn't find skill row {row + 1} slot {slot + 1} (graph {graph_hash:06x})")
        return
    graph, index, label = target
    if graph.nodes[index].ProgressPointsSpent <= 0:
        return
    graph.Server_SpendProgressPoints(index, -1)
    logging.dev_warning(f"[Easy Respec] removed a point from {label}")


def unspec_all() -> None:
    """Refunds every point in the skill trees, and unequips augments and capstones."""
    for graph in _player_graphs():
        names = _node_names_of(graph, _graph_name(graph))
        if any(name.startswith(TREE_NODE_PREFIXES) for name in names):
            if any(node.ProgressPointsSpent > 0 for node in graph.nodes):
                graph.Server_ResetSpentPoints()
        elif any(name.startswith(MODIFIER_NODE_PREFIXES) for name in names):
            # Their rows lock again with no points in the tree
            if any(node.bIsActivated for node in graph.nodes):
                graph.Server_ResetActivations()
    log("unspecced all skills")


@hook(CHANNEL_FUNC, Type.PRE)
def from_page(_obj: UObject, args: WrappedStruct, _ret: Any, _func: BoundFunction) -> type[Block] | None:
    x = args.position.X
    if x > -MARK + 0.5:
        return None  # a real sound
    code = round(-x) - MARK
    op, arg, value = code >> 16, code & 0xFFFF, round(args.position.Y)
    try:
        if op == OP_ACTIVE:
            if value and not Page.active:
                # In case the mapping context wasn't loaded when we were enabled
                set_native_undo_on_right_click(False)
            Page.active = bool(value)
            Page.last_heard = time.monotonic()
            Page.pc = WeakPointer(get_pc())
        elif op == OP_REMOVE:
            remove_point(value, arg >> 4, arg & 0xF)
        elif op == OP_UNSPEC_ALL:
            unspec_all()
    except Exception as ex:  # noqa: BLE001 - never let our handler break the menu
        logging.error(f"[Easy Respec] {ex!r}")
    return Block


# endregion


def set_native_undo_on_right_click(enabled: bool) -> None:
    """
    The game spends and refunds skill points natively, without any call we could hook (verified),
    and right mouse in menus never reaches the SDK's key hooks either. Its undo is a plain Enhanced
    Input mapping though, and the menu's mappings are rebuilt each time it opens, so editing the
    mapping context takes effect the next time Skills opens.
    """
    try:
        context = unrealsdk.find_object("InputMappingContext", SKILLS_CONTEXT)
    except ValueError:
        return  # not loaded yet; on_enable runs again from the page (see from_page)
    old, new = (NO_KEY, UNDO_KEY) if enabled else (UNDO_KEY, NO_KEY)
    for mapping in context.Mappings:
        if mapping.Action and mapping.Action.Name == UNDO_ACTION and str(mapping.Key.KeyName) == old:
            mapping.Key = unrealsdk.make_struct("Key", KeyName=new)


def sync_page(enabled: bool) -> None:
    try:
        status = skills_page.sync(GAME_DIR, enabled)
    except Exception as ex:  # noqa: BLE001 - never let a file problem break the mod
        status = f"Easy Respec: couldn't update the Skills page ({ex})."
    log(status)
    if enabled and skills_page.page_current:
        uilink.register(LINK_KEY, LINK_SCRIPT, dict, lambda _action: None, views=(uilink.MENUS,), mod=__name__)
    else:
        uilink.unregister(LINK_KEY)


def _enable() -> None:
    global _unmap_undo
    Page.active = False
    _unmap_undo = True
    sync_page(True)


def _disable() -> None:
    global _unmap_undo
    Page.active = False
    _unmap_undo = False
    set_native_undo_on_right_click(True)
    sync_page(False)


mod = build_mod(
    hooks=[watch_right_clicks, from_page],
    on_enable=_enable,
    on_disable=_disable,
)
