"""
The in-game BL4 Direct panel, drawn with the game's own UMG widgets.

The technique (runtime-constructed UserWidget/WidgetTree/CanvasPanel, polling button press state
from a per-frame camera hook) follows MattsBL4ModsMenu by Matthew Buehrer (MIT).

Since the BL4 Mod Menu came along, the open key (F6 unless rebound) normally opens BL4 Direct's page in its HTML screen instead
(page.py, ui/direct.js), which looks like the game's own menus. This panel is the fallback for
when that page can't show (no Mod Menu, or its page doesn't answer), and `bl4ui panel` opens it.

Input: while the panel is open the game is put in UI-only input mode with focus on one of our
widgets, so nothing reaches the game or its own menus. Text goes into real EditableTextBoxes.
Since PlayerInput sees no keys in that mode, the few hotkeys we need while open (the open key, Esc, Enter)
are read from the Windows keyboard state, and only while the game window is in front.

Rules that keep this crash-free:
- Every widget is rebuilt from scratch on any change; we never patch widgets in place.
- If the player controller changes (map load), widget refs are dropped without touching them.
- Host/Join close the panel first and run a few frames later, so travel never races the UI.
"""

from __future__ import annotations

import ctypes
import sys
import threading
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import unrealsdk
from mods_base import get_pc
from unrealsdk import logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import WeakPointer

DEFAULT_KEY = "F6"
VK_ESCAPE, VK_RETURN, VK_TAB = 0x1B, 0x0D, 0x09
# The open key's name (an Unreal key name, as mods_base keybinds use), set by __init__ from its
# "Open key" option. None: no key (the page is still in the Mods screen)
open_key: Callable[[], str | None] = lambda: DEFAULT_KEY  # noqa: E731

_DIGITS = ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine")
# Unreal key names -> Windows virtual keys, for the keys that can open the panel. The Mod Menu's
# rebind screen offers more (mice, modifiers); its table is used for those when it's loaded.
_VIRTUAL_KEYS: dict[str, int] = {
    **{chr(0x41 + i): 0x41 + i for i in range(26)},
    **{name: 0x30 + i for i, name in enumerate(_DIGITS)},
    **{f"NumPad{name}": 0x60 + i for i, name in enumerate(_DIGITS)},
    **{f"F{i + 1}": 0x70 + i for i in range(24)},
    "Insert": 0x2D, "Delete": 0x2E, "Home": 0x24, "End": 0x23, "PageUp": 0x21, "PageDown": 0x22,
    "Pause": 0x13, "ScrollLock": 0x91, "Tilde": 0xC0, "Tab": 0x09, "CapsLock": 0x14,
}  # fmt: skip


def _key_vk(name: str | None) -> int | None:
    if not name:
        return None
    if (vk := _VIRTUAL_KEYS.get(name)) is not None:
        return vk
    keys = sys.modules.get("bl4_mod_menu.keys")
    return getattr(keys, "VIRTUAL_KEYS", {}).get(name)


def key_text() -> str:
    """The open key as players read it ("F6"), or "" when unbound."""
    name = open_key()
    if not name:
        return ""
    keys = sys.modules.get("bl4_mod_menu.keys")
    try:
        return keys.display_name(name) if keys is not None else name
    except Exception:  # noqa: BLE001
        return name
TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
HOOK_ID = "bl4_direct.ui"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012

# Design space; everything is laid out at 1920x1080 and scaled to the real viewport
DESIGN_W, DESIGN_H = 1920.0, 1080.0
PANEL_W, PANEL_H = 1000.0, 860.0
MAX_FRIEND_ROWS = 5

# ctypes.windll, not PyDLL: these calls must let go of the GIL (GetAsyncKeyState can take ~100 ms,
# e.g. while the desktop is streamed). Keys go through BL4 Mod Menu's key thread when it's loaded.
_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
# Own copies for the functions we give argtypes: ctypes.windll's function objects are shared with
# every other mod in the process, and changing their argtypes changes them for everyone
_clip_k32 = ctypes.WinDLL("kernel32")
_clip_u32 = ctypes.WinDLL("user32")
_clip_k32.GlobalAlloc.restype = ctypes.c_void_p
_clip_k32.GlobalAlloc.argtypes = (ctypes.c_uint, ctypes.c_size_t)
_clip_k32.GlobalLock.restype = ctypes.c_void_p
_clip_k32.GlobalLock.argtypes = (ctypes.c_void_p,)
_clip_k32.GlobalUnlock.argtypes = (ctypes.c_void_p,)
_clip_k32.GlobalFree.restype = ctypes.c_void_p
_clip_k32.GlobalFree.argtypes = (ctypes.c_void_p,)
_clip_u32.SetClipboardData.restype = ctypes.c_void_p
_clip_u32.SetClipboardData.argtypes = (ctypes.c_uint, ctypes.c_void_p)


def log(msg: str) -> None:
    logging.info(f"[BL4 Direct] {msg}")


# region Small UE / Win32 helpers


def _cls(path: str) -> Any:
    return unrealsdk.find_object("Class", path)


def _new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(_cls(path), outer)


def _try(obj: Any, name: str, *args: Any) -> Any:
    try:
        return getattr(obj, name)(*args)
    except Exception:  # noqa: BLE001
        return None


def _vec2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))


def _color(r: float, g: float, b: float, a: float = 1.0) -> Any:
    return unrealsdk.make_struct("LinearColor", R=float(r), G=float(g), B=float(b), A=float(a))


def _slate_color(r: float, g: float, b: float, a: float = 1.0) -> Any:
    return unrealsdk.make_struct("SlateColor", SpecifiedColor=_color(r, g, b, a), ColorUseRule=0)


def _wbl() -> Any:
    return _cls("/Script/UMG.WidgetBlueprintLibrary").ClassDefaultObject


def _ue_key_down(pc: Any, name: str) -> bool:
    try:
        return bool(pc.IsInputKeyDown(unrealsdk.make_struct("Key", KeyName=name)))
    except Exception:  # noqa: BLE001
        return False


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


def _win_key_down(vk: int) -> bool:
    return _async_down(vk)


def _alive(obj: Any) -> bool:
    # Objects kept across frames are stored as WeakPointers (see UIState), which already return
    # None once the engine destroys them. Never "probe" a raw pointer by reading from it: if the
    # object was freed, the read itself crashes the game.
    return obj is not None


def _text_of(box: Any) -> str:
    try:
        return str(box.GetText())
    except Exception:  # noqa: BLE001
        return ""


def copy_to_clipboard(text: str) -> bool:
    """Puts text on the Windows clipboard."""
    k32, u32 = _clip_k32, _clip_u32
    data = ctypes.create_unicode_buffer(text)
    size = ctypes.sizeof(data)
    if not u32.OpenClipboard(None):
        return False
    try:
        u32.EmptyClipboard()
        handle = k32.GlobalAlloc(0x0002, size)  # GMEM_MOVEABLE
        if not handle:
            return False
        locked = k32.GlobalLock(handle)
        if not locked:
            k32.GlobalFree(handle)
            return False
        ctypes.memmove(locked, data, size)
        k32.GlobalUnlock(handle)
        if u32.SetClipboardData(13, handle):  # CF_UNICODETEXT; the clipboard owns it now
            return True
        k32.GlobalFree(handle)
        return False
    finally:
        u32.CloseClipboard()


class PublicIp:
    """Looks up this PC's public IP on a background thread (no unreal calls happen there)."""

    ip: str | None = None
    state = "idle"  # idle / loading / ok / failed

    @classmethod
    def lookup(cls) -> None:
        if cls.state in ("loading", "ok", "failed"):
            return  # Retry sets it back to "idle" (a failure retried on every page poll while offline)
        cls.state = "loading"
        threading.Thread(target=cls._worker, name="bl4_direct.public_ip", daemon=True).start()

    @classmethod
    def _worker(cls) -> None:
        for url in ("https://api.ipify.org", "https://ifconfig.me/ip"):
            try:
                with urllib.request.urlopen(url, timeout=6) as resp:  # noqa: S310
                    ip = resp.read().decode().strip()
                if api.is_ipv4(ip):
                    cls.ip, cls.state = ip, "ok"
                    return
            except Exception:  # noqa: BLE001, S112
                continue
        cls.state = "failed"


# endregion
# region State


class _Weak:
    """
    A UObject attribute held as a WeakPointer: reads give the live object, or None once the
    engine has destroyed it (map change, disconnect...). Holding raw references across frames
    and touching them after they're freed crashes the game (verified the hard way).
    """

    def __set_name__(self, owner: type, name: str) -> None:
        self.slot = "_weak_" + name

    def __get__(self, inst: Any, owner: type | None = None) -> Any:
        if inst is None:
            return self
        ptr = inst.__dict__.get(self.slot)
        return ptr() if ptr is not None else None

    def __set__(self, inst: Any, value: Any) -> None:
        inst.__dict__[self.slot] = WeakPointer(value) if value is not None else None


class ButtonRef:
    button = _Weak()

    def __init__(self, button: Any, action: Callable[[], None]) -> None:
        self.button = button
        self.action = action
        self.was_pressed = False


@dataclass
class UIState:
    is_open: bool = False
    buttons: list[ButtonRef] = field(default_factory=list)
    # Typed text survives redraws by being copied out of the old boxes first
    name_text: str = ""
    ip_text: str = ""
    keys_down: dict[int, bool] = field(default_factory=dict)
    toggle_was_down: bool = False
    # The open key last tick: a key just bound (still held from the rebind) needs a fresh press
    key_seen: str | None = None
    message: str = ""
    message_is_error: bool = False
    pending: Callable[[], None] | None = None
    pending_frames: int = 0
    scale_x: float = 1.0
    scale_y: float = 1.0
    ip_state_drawn: str = ""
    session_drawn: tuple[str, int] = ("none", 0)
    firewall_drawn: bool = False
    firewall_checked: float = 0.0
    ticked_at: float = 0.0
    last_call: float = 0.0

    # Engine objects kept between frames: weak, so they read as None once destroyed
    pc = _Weak()
    overlay = _Weak()
    canvas = _Weak()
    name_box = _Weak()
    ip_box = _Weak()


S = UIState()

# Set by __init__ so this module doesn't import it circularly
api: Any = None


# endregion
# region Layout / drawing


def _update_metrics(pc: Any) -> None:
    wll = _cls("/Script/UMG.WidgetLayoutLibrary").ClassDefaultObject
    w, h, dpi = 1920.0, 1080.0, 1.0
    try:
        size = wll.GetViewportSize(pc)
        if size.X > 100 and size.Y > 100:
            w, h = float(size.X), float(size.Y)
    except Exception:  # noqa: BLE001
        pass
    try:
        dpi = float(wll.GetViewportScale(pc)) or 1.0
    except Exception:  # noqa: BLE001
        pass
    # Slot coordinates are in DPI-unscaled units
    S.scale_x = (w / dpi) / DESIGN_W
    S.scale_y = (h / dpi) / DESIGN_H


class Painter:
    """Builds widgets into one CanvasPanel, in 1920x1080 design coordinates."""

    def __init__(self, tree: Any, canvas: Any) -> None:
        self.tree = tree
        self.canvas = canvas

    def _place(self, widget: Any, x: float, y: float, w: float, h: float, z: int) -> None:
        self.canvas.AddChild(widget)
        slot = widget.Slot
        _try(slot, "SetPosition", _vec2(x * S.scale_x, y * S.scale_y))
        _try(slot, "SetSize", _vec2(w * S.scale_x, h * S.scale_y))
        _try(slot, "SetZOrder", z)
        _try(slot, "SetAutoSize", False)

    def box(self, x: float, y: float, w: float, h: float, fill: tuple[float, ...], z: int = 1) -> Any:
        border = _new("/Script/UMG.Border", self.tree)
        _try(border, "SetBrushColor", _color(*fill))
        _try(border, "SetVisibility", 3)  # HitTestInvisible
        self._place(border, x, y, w, h, z)
        return border

    def text(
        self, value: str, x: float, y: float, w: float, h: float,
        size: float = 0.6, z: int = 10, *, center: bool = False,
        color: tuple[float, ...] = (0.92, 0.95, 0.96, 1.0),
    ) -> Any:
        tb = _new("/Script/UMG.TextBlock", self.tree)
        _try(tb, "SetText", value)
        scale = size * min(S.scale_x, S.scale_y)
        _try(tb, "SetRenderScale", _vec2(scale, scale))
        _try(tb, "SetRenderTransformPivot", _vec2(0.0 if not center else 0.5, 0.5))
        _try(tb, "SetColorAndOpacity", unrealsdk.make_struct("SlateColor", SpecifiedColor=_color(*color)))
        if center:
            _try(tb, "SetJustification", 1)
        _try(tb, "SetVisibility", 3)
        # TextBlocks draw from the top of their slot; give them a line-height slot centred in the box
        line_h = 30.0 * size
        self._place(tb, x, y + max(0.0, (h - line_h) / 2), w, line_h, z)
        return tb

    def button(
        self, label: str, x: float, y: float, w: float, h: float, action: Callable[[], None],
        *, fill: tuple[float, ...] = (0.0, 0.33, 0.38, 0.98), size: float = 0.55, enabled: bool = True,
    ) -> None:
        if not enabled:
            fill = (0.12, 0.14, 0.15, 0.95)
        self.box(x, y, w, h, fill, 20)
        self.text(label, x, y, w, h, size, 22, center=True,
                  color=(0.95, 0.97, 0.98, 1.0) if enabled else (0.55, 0.58, 0.6, 1.0))
        if not enabled:
            return
        # A real (nearly invisible) UMG Button gives us hit testing and press state
        btn = _new("/Script/UMG.Button", self.tree)
        _try(btn, "SetRenderOpacity", 0.03)
        self._place(btn, x, y, w, h, 21)
        S.buttons.append(ButtonRef(btn, action))

    def text_box(self, hint: str, value: str, x: float, y: float, w: float, h: float) -> Any:
        box = _new("/Script/UMG.EditableTextBox", self.tree)
        # The default style is light grey on white; match the panel instead
        style = box.WidgetStyle
        style.BackgroundColor = _slate_color(0.07, 0.17, 0.19)
        style.ForegroundColor = _slate_color(0.95, 0.97, 0.98)
        style.FocusedForegroundColor = _slate_color(1.0, 1.0, 1.0)
        _try(box, "SetHintText", hint)
        _try(box, "SetText", value)
        self._place(box, x, y, w, h, 30)
        # Clicking a text box only focuses it when the window is active, so do it ourselves with
        # an invisible button on top; typing still goes to the box underneath once it has focus
        btn = _new("/Script/UMG.Button", self.tree)
        _try(btn, "SetRenderOpacity", 0.0)
        self._place(btn, x, y, w, h, 31)
        S.buttons.append(ButtonRef(btn, lambda: _focus(box)))
        return box


def session() -> tuple[str, int]:
    """('hosting', friends connected) / ('connected', 0) / ('none', 0)."""
    try:
        world = api.current_world()
        nd = world.NetDriver if world else None
        if not api.is_direct_mode() or nd is None or nd.Class.Name != "SteamNetDriver":
            return "none", 0
        if nd.ServerConnection:
            return "connected", 0
        return "hosting", len(nd.ClientConnections)
    except Exception:  # noqa: BLE001
        return "none", 0


def share_address() -> str | None:
    return f"{PublicIp.ip}:{api.host_port()}" if PublicIp.ip else None


def _save_typed_text() -> None:
    if _alive(S.name_box):
        S.name_text = _text_of(S.name_box)
    if _alive(S.ip_box):
        S.ip_text = _text_of(S.ip_box)


def _draw() -> None:
    pc = S.pc
    _update_metrics(pc)
    _save_typed_text()
    focused_ip = _alive(S.ip_box) and bool(_try(S.ip_box, "HasKeyboardFocus"))
    if _alive(S.canvas):
        _try(S.canvas, "RemoveFromParent")
    S.buttons.clear()

    if S.overlay is None:
        # Destroyed under us (world change); nothing left to draw into
        S.is_open = False
        _forget_widgets()
        return
    tree = S.overlay.WidgetTree
    canvas = _new("/Script/UMG.CanvasPanel", tree)
    tree.RootWidget.AddChild(canvas)
    _try(canvas.Slot, "SetPosition", _vec2(0, 0))
    _try(canvas.Slot, "SetSize", _vec2(DESIGN_W * S.scale_x, DESIGN_H * S.scale_y))
    S.canvas = canvas
    p = Painter(tree, canvas)

    x0 = (DESIGN_W - PANEL_W) / 2
    y0 = (DESIGN_H - PANEL_H) / 2
    p.box(0, 0, DESIGN_W, DESIGN_H, (0.0, 0.0, 0.0, 0.55), 0)
    p.box(x0, y0, PANEL_W, PANEL_H, (0.02, 0.05, 0.06, 0.97), 1)
    p.box(x0, y0, PANEL_W, 64, (0.0, 0.42, 0.48, 1.0), 2)
    p.text("BL4 DIRECT", x0 + 24, y0, 400, 64, 0.85, 5)
    p.button(f"Close  ({key_text()})" if key_text() else "Close", x0 + PANEL_W - 190, y0 + 10, 170, 44, close, fill=(0.45, 0.07, 0.05, 0.98), size=0.5)

    mode, connected = session()
    S.session_drawn = (mode, connected)
    S.firewall_drawn = api.firewall_up_to_date()
    S.ip_state_drawn = PublicIp.state
    green = (0.35, 1.0, 0.5, 1.0)
    y = y0 + 80
    if mode == "hosting":
        p.box(x0 + 24, y, PANEL_W - 48, 50, (0.03, 0.30, 0.10, 0.98), 2)
        p.text(f"HOSTING on port {api.host_port()}   -   {connected} friend(s) connected",
               x0 + 40, y, PANEL_W - 80, 50, 0.7, 5, color=green)
    elif mode == "connected":
        p.box(x0 + 24, y, PANEL_W - 48, 50, (0.03, 0.30, 0.10, 0.98), 2)
        p.text("CONNECTED to a friend's game", x0 + 40, y, PANEL_W - 80, 50, 0.7, 5, color=green)
    else:
        p.text("Not in a direct game", x0 + 24, y, PANEL_W - 48, 50, 0.65)
    p.text(api.compat_line(), x0 + 24, y + 52, PANEL_W - 48, 26, 0.42, color=(0.6, 0.7, 0.72, 1.0))

    # Public IP, with a copy button
    y += 92
    if PublicIp.state == "ok":
        shown = (share_address() if mode == "hosting" else PublicIp.ip) or ""
        label = "Friends join you at:" if mode == "hosting" else "Your public IP:"
        p.text(label, x0 + 24, y, 260, 48, 0.55, color=(0.75, 0.8, 0.82, 1.0))
        p.text(shown, x0 + 290, y, 420, 48, 0.75, color=(1.0, 0.86, 0.35, 1.0))
        p.button("COPY", x0 + PANEL_W - 204, y + 4, 180, 40, lambda: _copy(shown), size=0.5)
    elif PublicIp.state == "failed":
        p.text("Your public IP: couldn't look it up (offline?)", x0 + 24, y, 600, 48, 0.55,
               color=(1.0, 0.6, 0.45, 1.0))
        p.button("RETRY", x0 + PANEL_W - 204, y + 4, 180, 40, _retry_ip, size=0.5)
    else:
        p.text("Your public IP: looking it up...", x0 + 24, y, 600, 48, 0.55, color=(0.75, 0.8, 0.82, 1.0))

    # Host / stop hosting
    y += 64
    in_game = api.in_gameplay()
    if mode == "hosting":
        p.button("STOP HOSTING", x0 + 24, y, 300, 56, lambda: _run_after_close(api.stop_hosting),
                 fill=(0.50, 0.10, 0.06, 0.98), size=0.6)
        if S.firewall_drawn:
            host_hint = "Firewall up to date: only friends below can get in"
        else:
            p.button("UPDATE FIREWALL", x0 + PANEL_W - 284, y + 8, 260, 40, _update_firewall_now,
                     fill=(0.55, 0.35, 0.02, 0.98), size=0.48)
            host_hint = "Firewall not updated - friends may be blocked"
    elif mode == "connected":
        p.button("LEAVE GAME", x0 + 24, y, 300, 56, lambda: _run_after_close(api.leave),
                 fill=(0.50, 0.10, 0.06, 0.98), size=0.6)
        host_hint = "Back to the main menu, and normal online play"
    elif api.hosting_intent() is not None:
        # Between a map change and the watchdog re-hosting (or at the main menu)
        p.button("DON'T RESUME", x0 + 24, y, 300, 56, lambda: (api.cancel_hosting_intent(), _draw()),
                 fill=(0.30, 0.10, 0.08, 0.96), size=0.55)
        host_hint = (
            "Hosting restarts in a moment..." if in_game
            else "Hosting resumes automatically when you load in"
        )
    else:
        p.button("HOST A GAME", x0 + 24, y, 300, 56, lambda: _run_after_close(host_now), size=0.6,
                 enabled=in_game and mode == "none")
        if not in_game:
            host_hint = "Load into your game first (Continue), then host"
        elif api.firewall_up_to_date():
            host_hint = f"Friends join you on UDP port {api.host_port()}"
        else:
            host_hint = "Windows will ask once to let your friends through the firewall"
    p.text(host_hint, x0 + 344, y, PANEL_W - 368, 56, 0.5, color=(0.75, 0.8, 0.82, 1.0))

    # Friends
    y += 80
    p.text("FRIENDS", x0 + 24, y, 300, 34, 0.55, color=(0.3, 0.85, 0.95, 1.0))
    y += 40
    friends = api.friends()
    if not friends:
        p.text("No friends yet - add one below.", x0 + 24, y, PANEL_W - 48, 44, 0.5,
               color=(0.6, 0.65, 0.67, 1.0))
        y += 52
    for friend in friends[:MAX_FRIEND_ROWS]:
        p.box(x0 + 24, y, PANEL_W - 48, 48, (0.04, 0.10, 0.12, 0.96), 2)
        p.text(friend.name, x0 + 40, y, 300, 48, 0.55, 5)
        p.text(f"{friend.address}:{friend.port}", x0 + 340, y, 300, 48, 0.5, 5, color=(0.75, 0.8, 0.82, 1.0))
        p.button("JOIN", x0 + PANEL_W - 290, y + 6, 140, 36,
                 lambda f=friend: _run_after_close(lambda: api.join_friend(f)))
        p.button("Remove", x0 + PANEL_W - 140, y + 6, 100, 36, lambda f=friend: _remove(f.name),
                 fill=(0.30, 0.10, 0.08, 0.96), size=0.45)
        y += 54
    if len(friends) > MAX_FRIEND_ROWS:
        p.text(f"+{len(friends) - MAX_FRIEND_ROWS} more (manage in the BL4 Direct app)",
               x0 + 24, y, PANEL_W - 48, 30, 0.45)

    # Add friend
    y = y0 + PANEL_H - 200
    p.text("ADD A FRIEND  (their public IP - they can see it in their BL4 Direct app)",
           x0 + 24, y, PANEL_W - 48, 34, 0.5, color=(0.3, 0.85, 0.95, 1.0))
    y += 44
    S.name_box = p.text_box("Name", S.name_text, x0 + 24, y, 330, 48)
    S.ip_box = p.text_box("IP address, e.g. 203.0.113.5", S.ip_text, x0 + 366, y, 400, 48)
    p.button("ADD", x0 + 778, y, PANEL_W - 802, 48, _add_friend, size=0.55)

    # Message line
    y += 66
    if S.message:
        color = (1.0, 0.45, 0.35, 1.0) if S.message_is_error else (0.55, 0.95, 0.6, 1.0)
        p.text(S.message, x0 + 24, y, PANEL_W - 48, 34, 0.48, color=color)

    _try(S.overlay, "ForceLayoutPrepass")
    _focus(S.ip_box if focused_ip else S.name_box)


def _focus(widget: Any) -> None:
    """UI-only input with focus on one of our widgets: the game and its menus get no keys."""
    if S.pc is None or not _alive(widget):
        return
    _try(widget, "SetUserFocus", S.pc)
    _try(widget, "SetKeyboardFocus")
    _try(_wbl(), "SetInputMode_UIOnlyEx", S.pc, widget, 0, False)


# endregion
# region Actions


def _say(msg: str, *, error: bool = False) -> None:
    S.message = msg
    S.message_is_error = error
    (logging.error if error else logging.info)(f"[BL4 Direct] {msg}")
    # The HTML page shows it too (as a toast), if it's in use
    if (page := sys.modules.get("bl4_direct.page")) is not None:
        page.say(msg, error=error)


def _copy(text: str) -> None:
    if text and copy_to_clipboard(text):
        _say(f"Copied {text} - paste it to your friend.")
    else:
        _say("Couldn't copy to the clipboard.", error=True)
    _draw()


def _retry_ip() -> None:
    PublicIp.state = "idle"
    PublicIp.lookup()
    _draw()


def _add_friend() -> None:
    _save_typed_text()
    name, ip = S.name_text.strip(), S.ip_text.strip()
    if not name:
        _say("Type a name first.", error=True)
    elif not api.is_ipv4(ip):
        _say("That IP doesn't look right - it should look like 203.0.113.5", error=True)
    else:
        api.add_friend(name[:24], ip)
        # Clear the boxes themselves too, or the redraw would copy their old text back
        S.name_text = S.ip_text = ""
        _try(S.name_box, "SetText", "")
        _try(S.ip_box, "SetText", "")
        _after_friends_changed(f"Added {name}.")
    _draw()


def _remove(name: str) -> None:
    api.remove_friend(name)
    _after_friends_changed(f"Removed {name}.")
    _draw()


def _after_friends_changed(done: str) -> None:
    """
    Only a host needs friends in its firewall (joining is an outgoing connection), so the
    firewall is normally updated when you press Host. If you're already hosting, do it now.
    """
    if session()[0] != "hosting":
        _say(f"{done} If you host, Windows will ask once to let your friends through your firewall.")
    elif error := api.update_firewall():
        _say(f"{done} But: {error}", error=True)
    else:
        _say(f"{done} Windows will ask permission to update your firewall.")


def _update_firewall_now() -> None:
    if error := api.update_firewall():
        _say(error, error=True)
    else:
        _say("Windows will ask permission to update your firewall.")
    _draw()


def host_now() -> None:
    if error := api.host():
        log(f"Hosting, but the firewall wasn't updated: {error}")



def _run_after_close(action: Callable[[], None]) -> None:
    close()
    S.pending = action
    S.pending_frames = 5


# endregion
# region Open / close


def open_panel() -> None:
    if S.is_open:
        return
    pc = get_pc(possibly_loading=True)
    if pc is None:
        log("Can't open the panel while loading.")
        return
    S.pc = pc
    overlay = _new("/Script/UMG.UserWidget", pc)
    overlay.WidgetTree = _new("/Script/UMG.WidgetTree", overlay)
    overlay.WidgetTree.RootWidget = _new("/Script/UMG.CanvasPanel", overlay.WidgetTree)
    _update_metrics(pc)
    _try(overlay, "SetAlignmentInViewport", _vec2(0, 0))
    _try(overlay, "SetPositionInViewport", _vec2(0, 0), False)
    _try(overlay, "SetDesiredSizeInViewport", _vec2(DESIGN_W * S.scale_x, DESIGN_H * S.scale_y))
    overlay.AddToViewport(999998)
    S.overlay = overlay
    S.is_open = True
    S.message = ""
    # The key that opened us is still held
    S.keys_down = {vk: True} if (vk := _key_vk(open_key())) is not None else {}
    pc.bShowMouseCursor = True
    api.refresh()
    if PublicIp.state == "failed":
        PublicIp.state = "idle"  # opening the panel tries again
    PublicIp.lookup()
    _draw()


def close() -> None:
    if not S.is_open:
        return
    S.is_open = False
    overlay, pc = S.overlay, S.pc
    _forget_widgets()
    if _alive(overlay):
        _try(overlay, "RemoveFromParent")
    if _alive(pc):
        if api.in_gameplay():
            pc.bShowMouseCursor = False
            _try(_wbl(), "SetInputMode_GameOnly", pc, True)
        else:
            # The title screen's own menu wants UI input back, and its cursor
            _try(_wbl(), "SetInputMode_GameAndUIEx", pc, None, 0, False, False)
        _try(_wbl(), "SetFocusToGameViewport")
    S.toggle_was_down = True  # don't let a held open key reopen us


def _forget_widgets() -> None:
    S.overlay = S.canvas = S.pc = S.name_box = S.ip_box = None
    S.buttons.clear()


def toggle() -> None:
    if S.is_open:
        close()
    else:
        open_panel()


def open_preferred() -> None:
    """BL4 Direct's page in the Mod Menu screen if it can show, else this panel."""
    page = sys.modules.get("bl4_direct.page")
    try:
        if page is not None and page.open_page():
            return
    except Exception as ex:  # noqa: BLE001 - the panel always works
        log(f"Couldn't open the page: {ex!r}")
    open_panel()


def _toggle_key_pressed() -> None:
    """The open key while this panel is closed: the page toggles if it can, else this panel opens."""
    page = sys.modules.get("bl4_direct.page")
    try:
        if page is not None and page.toggle():
            return
    except Exception as ex:  # noqa: BLE001
        log(f"Couldn't open the page: {ex!r}")
    open_panel()


# endregion
# region Per-frame polling


def _poll_buttons() -> None:
    for ref in list(S.buttons):
        try:
            pressed = bool(ref.button.IsPressed())
        except Exception:  # noqa: BLE001
            pressed = False
        if pressed:
            ref.was_pressed = True
        elif ref.was_pressed:
            ref.was_pressed = False
            ref.action()
            return  # the action probably rebuilt the button list


def _win_edge(vk: int) -> bool:
    down = _win_key_down(vk)
    was = S.keys_down.get(vk, False)
    S.keys_down[vk] = down
    return down and not was


def _on_tick(*_: Any) -> None:
    now = time.perf_counter()
    gap, S.last_call = now - S.last_call, now
    if gap < FRAME_GAP_S and now - S.ticked_at < MAX_WAIT_S:
        return
    S.ticked_at = now
    try:
        pc = get_pc(possibly_loading=True)
    except Exception:  # noqa: BLE001
        pc = None
    if pc is None:
        return
    _report_elevated()
    if (page := sys.modules.get("bl4_direct.page")) is not None:
        try:
            page.tick()
        except Exception as ex:  # noqa: BLE001 - never let the page break the panel or the game
            logging.error(f"[BL4 Direct] page: {ex!r}")

    if S.pending is not None:
        S.pending_frames -= 1
        if S.pending_frames <= 0:
            action, S.pending = S.pending, None
            action()
        return

    if not S.is_open:
        # PlayerInput only sees keys in game input modes; the Windows key state covers the rest
        name = open_key()
        if name != S.key_seen:
            S.key_seen = name
            S.toggle_was_down = True
        vk = _key_vk(name)
        down = bool(name) and (_ue_key_down(pc, name) or (vk is not None and _win_key_down(vk)))
        if down and not S.toggle_was_down:
            _toggle_key_pressed()
        S.toggle_was_down = down
        return

    if pc != S.pc:
        # Map changed underneath us; the old widgets went with the old world
        S.is_open = False
        _forget_widgets()
        return

    vk = _key_vk(open_key())
    if (vk is not None and _win_edge(vk)) or _win_edge(VK_ESCAPE):
        close()
        return
    if S.ip_state_drawn != PublicIp.state or S.session_drawn != session():
        _draw()
        return
    if time.monotonic() - S.firewall_checked > 1.0:
        S.firewall_checked = time.monotonic()
        if api.firewall_up_to_date() != S.firewall_drawn:
            _draw()
            return

    in_ip = _alive(S.ip_box) and bool(_try(S.ip_box, "HasKeyboardFocus"))
    if _win_edge(VK_TAB):
        _focus(S.name_box if in_ip else S.ip_box)
        return
    if _win_edge(VK_RETURN):
        if in_ip:
            _add_friend()
        else:
            _focus(S.ip_box)
        return
    _poll_buttons()


def install() -> None:
    S.toggle_was_down = True  # if the open key is held right now, wait for a fresh press
    unrealsdk.hooks.add_hook(TICK_FUNC, Type.POST, HOOK_ID, _on_tick)


def uninstall() -> None:
    close()
    unrealsdk.hooks.remove_hook(TICK_FUNC, Type.POST, HOOK_ID)
    if (page := sys.modules.get("bl4_direct.page")) is not None:
        page.unregister()


# endregion


class Elevated:
    """
    Starts a program with a UAC prompt on a background thread. ShellExecute("runas") doesn't
    return until the prompt is answered, and on the game thread that stops a hosted server from
    ticking, so connected friends time out. The tick reports a refusal (see _report_elevated).
    """

    busy = False
    refused: str | None = None  # set by the worker when the program didn't start; read by the tick

    @classmethod
    def start(cls, exe: str, args: str, refused_msg: str) -> bool:
        """False if a prompt is already up."""
        if cls.busy:
            return False
        cls.busy = True
        threading.Thread(
            target=cls._worker, args=(exe, args, refused_msg), name="bl4_direct.elevate", daemon=True,
        ).start()
        return True

    @classmethod
    def _worker(cls, exe: str, args: str, refused_msg: str) -> None:
        # Win32 only, no unreal calls. ShellExecute wants COM on its thread.
        com = False
        try:
            # Apartment threaded, no OLE1 DDE; S_OK or S_FALSE both need a matching uninit
            com = ctypes.windll.ole32.CoInitializeEx(None, 0x2 | 0x4) in (0, 1)
            result = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, args, None, 0)
            if int(result) <= 32:
                cls.refused = refused_msg
        except Exception:  # noqa: BLE001
            cls.refused = refused_msg
        finally:
            if com:
                ctypes.windll.ole32.CoUninitialize()
            cls.busy = False


def run_elevated(exe: str, args: str, refused_msg: str) -> bool:
    """Starts a program with a UAC prompt without waiting. False if a prompt is already up."""
    return Elevated.start(exe, args, refused_msg)


def _report_elevated() -> None:
    if (msg := Elevated.refused) is None:
        return
    Elevated.refused = None
    _say(msg, error=True)
    if S.is_open:
        _draw()
