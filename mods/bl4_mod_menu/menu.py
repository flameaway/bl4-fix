"""
The mod menu panel: every registered mod on the left, the selected mod's options on the right.

Mods don't register anything special: whatever they pass to build_mod (or declare at module
level) as mods_base options and keybinds shows up here, the same list the console `mods` menu
shows. A change goes straight through `option.value = ...`, which runs the mod's on_change
callback, and is saved to the mod's settings file at once. Enabling or disabling a mod runs its
enable/disable. Nothing waits for a restart.

How each option type is drawn:
- BoolOption: an Off/On pair (or its false_text/true_text).
- DropdownOption / SpinnerOption: < value >; clicking the value steps forward too.
- SliderOption: - [track] +; click anywhere on the track to jump there.
- KeybindOption: the key; click it, then press the new key (Esc cancels).
- ButtonOption: a Run button. NestedOption: opens a sub-page. GroupedOption: a heading.
- Anything with a default shows Reset while it differs from it.
Hovering a row shows its description at the bottom.

Input: while open the game is in UI-only mode with focus on our overlay, so keys reach neither
the game nor its own menus. Esc, the open key and Page Up/Down are read from Windows instead
(see keys.py). The whole panel is rebuilt on every change; only the hover highlight and the
description text are updated in place.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mods_base import (
    BaseOption,
    BoolOption,
    ButtonOption,
    CoopSupport,
    DropdownOption,
    GroupedOption,
    KeybindOption,
    Mod,
    NestedOption,
    SliderOption,
    SpinnerOption,
    ValueOption,
    get_ordered_mod_list,
    get_pc,
    html_to_plain_text,
)
from unrealsdk import hooks, logging
from unrealsdk.hooks import Type
from unrealsdk.unreal import WeakPointer

from . import keys, web
from .widgets import DESIGN_H, DESIGN_W, Canvas, Color, Hint, call, make_overlay, viewport_scale, widget_lib

TICK_FUNC = "/Script/Engine.CameraModifier:BlueprintModifyCamera"
HOOK_ID = "bl4_mod_menu.menu"
# The tick hook fires ~6 times a frame (3 camera modifiers x 2 camera updates), the calls close
# together: act on the first call after a gap (a new frame), and at least every MAX_WAIT_S
FRAME_GAP_S = 0.004
MAX_WAIT_S = 0.012
# Above BL4 Direct's F6 panel (999998)
Z_ORDER = 999999
VK_PRIOR, VK_NEXT = 0x21, 0x22
CHANGE_CHECK_S = 0.5
MESSAGE_S = 8.0
WEB_WAIT_S = 4.0
# How long a posted Esc is held. A time, not a count of tick calls: the tick hook fires several
# times a frame, so 6 calls could be under a frame at ~100 fps and the game missed the press (F8
# opened nothing, traced 2026-10-24: one press held 18 ms worked, the misses didn't)
ESC_HOLD_S = 0.08

# region Look

PANEL_W, PANEL_H = 1500.0, 900.0
X0, Y0 = (DESIGN_W - PANEL_W) / 2, (DESIGN_H - PANEL_H) / 2
HEADER_H = 64.0
LIST_X, LIST_W = X0 + 20, 380.0
RIGHT_X, RIGHT_W = X0 + 424, PANEL_W - 444
ROW_H, ROW_GAP = 48.0, 6.0
OPTIONS_TOP = Y0 + 246
FOOTER_H = 104.0
FOOTER_Y = Y0 + PANEL_H - 20 - FOOTER_H
OPTIONS_BOTTOM = FOOTER_Y - 14
CONTROL_W = 440.0
CONTROL_X = RIGHT_X + RIGHT_W - 16 - CONTROL_W
RESET_W = 84.0
SLIDER_SEGMENTS = 24

WHITE: Color = (0.95, 0.97, 0.98, 1.0)
MUTED: Color = (0.62, 0.68, 0.70, 1.0)
ACCENT: Color = (0.30, 0.85, 0.95, 1.0)
GOLD: Color = (1.0, 0.86, 0.35, 1.0)
GOOD: Color = (0.40, 1.0, 0.55, 1.0)
BAD: Color = (1.0, 0.45, 0.35, 1.0)
WARN: Color = (1.0, 0.85, 0.30, 1.0)

BACKDROP: Color = (0.0, 0.0, 0.0, 0.55)
PANEL: Color = (0.02, 0.05, 0.06, 0.97)
HEADER: Color = (0.0, 0.42, 0.48, 1.0)
ROW: Color = (0.04, 0.10, 0.12, 0.96)
ROW_HOVER: Color = (0.06, 0.16, 0.19, 0.98)
ROW_SELECTED: Color = (0.0, 0.30, 0.35, 1.0)
BUTTON: Color = (0.0, 0.33, 0.38, 0.98)
BUTTON_HOVER: Color = (0.0, 0.45, 0.52, 1.0)
CHOSEN: Color = (0.0, 0.52, 0.58, 1.0)
UNCHOSEN: Color = (0.08, 0.13, 0.15, 0.98)
DISABLED: Color = (0.12, 0.14, 0.15, 0.95)
RED: Color = (0.45, 0.07, 0.05, 0.98)
RED_HOVER: Color = (0.60, 0.10, 0.07, 1.0)
TRACK: Color = (0.06, 0.12, 0.14, 1.0)
TRACK_FILL: Color = (0.0, 0.42, 0.48, 1.0)
CAPTURE: Color = (0.55, 0.35, 0.02, 0.98)

DEFAULT_HINT = Hint(
    "",
    "Hover over a setting to see what it does. Changes take effect right away and are saved.",
)

# endregion
# region State


@dataclass
class Row:
    option: BaseOption | None  # None for a group heading
    title: str
    depth: int


@dataclass
class MenuState:
    is_open: bool = False
    mod_name: str | None = None  # by name, since a hot-reloaded mod is a new object
    nested: list[str] = field(default_factory=list)  # identifiers of the NestedOptions we're in
    mod_page: int = 0
    option_page: int = 0
    capture: KeybindOption | None = None
    capture_held: set[int] = field(default_factory=set)
    message: str = ""
    message_error: bool = False
    message_at: float = 0.0
    keys_down: dict[int, bool] = field(default_factory=dict)
    toggle_was_down: bool = False
    cursor_was_shown: bool = False
    pending: Callable[[], None] | None = None
    # A posted key held down: (frames left, virtual key). Its own slot, so an action queued in
    # `pending` meanwhile can't drop the key up and leave the game seeing it held.
    release: tuple[float, int] | None = None  # (when to let go, virtual key)
    # After asking the HTML menu to open: (give up at, mod name). If it doesn't, we open instead.
    # Waiting for the HTML page to open: (give up at, mod name, fallback)
    web_wait: tuple[float, str | None, Callable[[], None] | None] | None = None
    signature: Any = None
    checked_at: float = 0.0
    ticked_at: float = 0.0
    last_call: float = 0.0
    scale: tuple[float, float] = (1.0, 1.0)
    hint: Hint | None = None
    canvas: Canvas | None = None
    pc: WeakPointer = field(default_factory=WeakPointer)
    overlay: WeakPointer = field(default_factory=WeakPointer)
    hint_title: WeakPointer = field(default_factory=WeakPointer)
    hint_text: WeakPointer = field(default_factory=WeakPointer)


S = MenuState()

# Set by install(): returns the key that opens the menu (an Unreal key name)
_open_key: Callable[[], str | None] = lambda: "F7"  # noqa: E731


# endregion
# region Mods and options


def _mods() -> list[Mod]:
    return list(get_ordered_mod_list())


def _selected_mod() -> Mod | None:
    mods = _mods()
    for mod in mods:
        if mod.name == S.mod_name:
            return mod
    # Default to the first ordinary mod rather than the SDK itself
    mod = next((m for m in mods if m.options or m.keybinds), mods[0] if mods else None)
    S.mod_name = mod.name if mod else None
    return mod


def _current_options(mod: Mod) -> tuple[list[BaseOption], list[str]]:
    """The options on the current page (the mod's, or a nested page's) and the page's path."""
    options: list[BaseOption] = list(mod.iter_display_options())
    path = [mod.name]
    for ident in S.nested:
        nested = next((o for o in _walk(options) if isinstance(o, NestedOption) and o.identifier == ident), None)
        if nested is None:
            # The mod changed under us (hot reload); go back to its top page
            S.nested.clear()
            return list(mod.iter_display_options()), [mod.name]
        options = list(nested.children)
        path.append(nested.display_name)
    return options, path


def _walk(options: Iterable[BaseOption]) -> Iterable[BaseOption]:
    """Options and the children of groups (not of nested pages)."""
    for option in options:
        yield option
        if isinstance(option, GroupedOption):
            yield from _walk(option.children)


def _visible(option: BaseOption) -> bool:
    if option.is_hidden:
        return False
    if isinstance(option, GroupedOption):
        return any(_visible(child) for child in option.children)
    return True


def _rows(options: Sequence[BaseOption], depth: int = 0, stack: tuple[GroupedOption, ...] = ()) -> list[Row]:
    rows: list[Row] = []
    for option in options:
        if not _visible(option):
            continue
        if isinstance(option, GroupedOption):
            if option in stack:
                continue  # a group that contains itself
            rows.append(Row(None, option.display_name, depth))
            rows += _rows(option.children, depth + 1, (*stack, option))
        else:
            rows.append(Row(option, option.display_name, depth))
    return rows


def _plain(html: str) -> str:
    try:
        return html_to_plain_text(html).strip()
    except Exception:  # noqa: BLE001
        return html


def _hint_for(option: BaseOption) -> Hint:
    text = _plain(option.description) or "No description."
    return Hint(option.description_title or option.display_name, text)


def _signature(mod: Mod | None) -> Any:
    """Everything drawn that something else could change while we're open."""
    mods = tuple((m.name, m.is_enabled) for m in _mods())
    if mod is None:
        return mods
    options, _ = _current_options(mod)
    values = tuple(
        (o.identifier, repr(getattr(o, "value", None)), tuple(getattr(o, "choices", ())))
        for o in _walk(options)
    )
    return mods, values


def _fmt_number(option: SliderOption, value: float) -> str:
    if option.is_integer:
        return str(round(value))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _default_text(option: ValueOption[Any]) -> str:
    value = option.default_value
    if isinstance(option, BoolOption):
        return (option.true_text or "On") if value else (option.false_text or "Off")
    if isinstance(option, SliderOption):
        return _fmt_number(option, value)
    if isinstance(option, KeybindOption):
        return keys.display_name(value)
    return str(value)


# endregion
# region Drawing


def _page_count(total: int, per_page: int) -> int:
    return max(1, -(-total // per_page))


def _draw() -> None:
    pc, overlay = S.pc(), S.overlay()
    if S.canvas is not None:
        if overlay is not None:
            S.canvas.remove()
        S.canvas = None
    if pc is None or overlay is None:
        # Destroyed under us (world change); nothing left to draw into
        _forget()
        return
    S.scale = viewport_scale(pc)
    c = S.canvas = Canvas(overlay, S.scale)
    mod = _selected_mod()
    S.signature = _signature(mod)

    c.box(0, 0, DESIGN_W, DESIGN_H, BACKDROP, 0)
    c.box(X0, Y0, PANEL_W, PANEL_H, PANEL, 1)
    c.box(X0, Y0, PANEL_W, HEADER_H, HEADER, 2)
    c.label("MOD MENU", X0 + 24, Y0, HEADER_H, 26, WHITE, 5)
    if S.message and time.monotonic() - S.message_at < MESSAGE_S:
        c.label(S.message[:110], X0 + 250, Y0, HEADER_H, 15, BAD if S.message_error else GOOD, 5)
    close_key = keys.display_name(_open_key())
    c.button(f"Close  ({close_key})", X0 + PANEL_W - 200, Y0 + 10, 180, 44, close,
             fill=RED, hover=RED_HOVER, size=16)  # fmt: skip

    _draw_mod_list(c)
    if mod is not None:
        _draw_mod(c, mod)
    _draw_footer(c)

    call(overlay, "ForceLayoutPrepass")


def _draw_mod_list(c: Canvas) -> None:
    mods = _mods()
    top = Y0 + HEADER_H + 20
    per_page = int((Y0 + PANEL_H - 20 - top + ROW_GAP) // (ROW_H + ROW_GAP))
    pages = _page_count(len(mods), per_page)
    if pages > 1:
        per_page -= 1  # room for the pager
        pages = _page_count(len(mods), per_page)
    S.mod_page = min(S.mod_page, pages - 1)
    y = top
    for mod in mods[S.mod_page * per_page:(S.mod_page + 1) * per_page]:
        selected = mod.name == S.mod_name
        fill = ROW_SELECTED if selected else ROW
        hint = Hint(mod.name, _plain(mod.description) or "No description.")
        c.button("", LIST_X, y, LIST_W, ROW_H, lambda m=mod: _select(m),
                 fill=fill, hover=fill if selected else ROW_HOVER, hint=hint, z=3)  # fmt: skip
        c.label(mod.name[:30], LIST_X + 16, y, ROW_H, 17, WHITE, 6)
        status = _plain(mod.get_status())
        c.label(status[:16], LIST_X + LIST_W - 14, y, ROW_H, 13, _status_color(mod, status), 6, right=True)
        y += ROW_H + ROW_GAP
    if pages > 1:
        y = Y0 + PANEL_H - 20 - ROW_H
        _pager(c, LIST_X, y, LIST_W, S.mod_page, pages, _turn_mod_page)


def _status_color(mod: Mod, status: str) -> Color:
    if "ncompatible" in status or "estart" in status:
        return WARN
    return GOOD if mod.is_enabled else BAD


def _pager(c: Canvas, x: float, y: float, w: float, page: int, pages: int, turn: Callable[[int], None]) -> None:
    c.button("<  Prev", x, y, 110, ROW_H, (lambda: turn(-1)) if page > 0 else None,
             fill=BUTTON if page > 0 else DISABLED, hover=BUTTON_HOVER, size=15)  # fmt: skip
    c.label(f"Page {page + 1} of {pages}", x + 110, y, ROW_H, 15, MUTED, 6, center_w=w - 220)
    c.button("Next  >", x + w - 110, y, 110, ROW_H, (lambda: turn(1)) if page < pages - 1 else None,
             fill=BUTTON if page < pages - 1 else DISABLED, hover=BUTTON_HOVER, size=15)  # fmt: skip


def _draw_mod(c: Canvas, mod: Mod) -> None:
    y = Y0 + HEADER_H + 16
    c.label(mod.name[:48], RIGHT_X, y, 44, 28, WHITE, 5)
    if not mod.enabling_locked:
        if mod.is_enabled:
            c.button("Disable mod", RIGHT_X + RIGHT_W - 180, y + 2, 180, 40, lambda: _set_enabled(mod, False),
                     fill=RED, hover=RED_HOVER, size=15,
                     hint=Hint("Disable mod", "Turns the mod off now (its hooks, keys and features)."))  # fmt: skip
        else:
            c.button("Enable mod", RIGHT_X + RIGHT_W - 180, y + 2, 180, 40, lambda: _set_enabled(mod, True),
                     fill=BUTTON, hover=BUTTON_HOVER, size=15,
                     hint=Hint("Enable mod", "Turns the mod on now."))  # fmt: skip
    status = _plain(mod.get_status())
    c.label(status, RIGHT_X + RIGHT_W - (200 if not mod.enabling_locked else 0), y, 44, 15,
            _status_color(mod, status), 5, right=True)  # fmt: skip

    meta = [f"by {mod.author}" if mod.author else "", mod.version or "", _coop_text(mod.coop_support)]
    c.label("   |   ".join(m for m in meta if m), RIGHT_X, y + 44, 26, 14, MUTED, 5)
    c.paragraph(_plain(mod.description), RIGHT_X, y + 76, RIGHT_W, 72, 14, (0.80, 0.85, 0.87, 1.0), 5)

    options, path = _current_options(mod)
    y = OPTIONS_TOP
    if S.nested:
        c.button("<  Back", RIGHT_X, y, 120, 40, _back, fill=BUTTON, hover=BUTTON_HOVER, size=15)
        c.label("  >  ".join(path)[:90], RIGHT_X + 136, y, 40, 16, ACCENT, 5)
        y += 40 + ROW_GAP + 4
    rows = _rows(options)
    if not rows:
        c.label("This mod has no settings.", RIGHT_X, y, ROW_H, 16, MUTED, 5)
        return

    per_page = int((OPTIONS_BOTTOM - y + ROW_GAP) // (ROW_H + ROW_GAP))
    pages = _page_count(len(rows), per_page)
    if pages > 1:
        per_page -= 1
        pages = _page_count(len(rows), per_page)
    S.option_page = min(S.option_page, pages - 1)
    for row in rows[S.option_page * per_page:(S.option_page + 1) * per_page]:
        _draw_row(c, mod, row, y)
        y += ROW_H + ROW_GAP
    if pages > 1:
        _pager(c, RIGHT_X + RIGHT_W / 2 - 230, OPTIONS_BOTTOM - ROW_H, 460, S.option_page, pages, _turn_option_page)


def _coop_text(support: CoopSupport) -> str:
    return {
        CoopSupport.Incompatible: "Co-op: incompatible",
        CoopSupport.RequiresAllPlayers: "Co-op: every player needs it",
        CoopSupport.ClientSide: "Co-op: works for just you",
        CoopSupport.HostOnly: "Co-op: host only",
    }.get(support, "")


def _draw_row(c: Canvas, mod: Mod, row: Row, y: float) -> None:
    indent = max(0, row.depth - 1) * 28
    x = RIGHT_X + indent
    if row.option is None:
        c.label(row.title.upper(), x + 4, y, ROW_H, 16, ACCENT, 5)
        c.box(x + 4, y + ROW_H - 8, RIGHT_W - indent - 8, 2, (0.0, 0.42, 0.48, 0.6), 3)
        return
    option = row.option
    hint = _hint_for(option)
    # The row itself only shows the hint; the controls on it do the work
    c.button("", x, y, RIGHT_W - indent, ROW_H, None, fill=ROW, hint=hint, z=3)
    c.label(option.display_name[:56], x + 16, y, ROW_H, 17, WHITE, 6)

    if (
        isinstance(option, ValueOption)
        and hasattr(option, "default_value")
        and option.value != option.default_value  # pyright: ignore[reportUnknownMemberType]
    ):
        c.button("Reset", CONTROL_X - RESET_W - 10, y + 6, RESET_W, ROW_H - 12,
                 lambda: _set_value(mod, option, option.default_value),  # pyright: ignore[reportUnknownMemberType, reportUnknownLambdaType]
                 fill=UNCHOSEN, hover=BUTTON_HOVER, size=13, text_color=MUTED,
                 hint=Hint("Reset", f"Back to the default: {_default_text(option)}"))  # fmt: skip

    cx, cy, ch = CONTROL_X, y + 5, ROW_H - 10
    match option:
        case BoolOption():
            _draw_bool(c, mod, option, cx, cy, ch, hint)
        case DropdownOption() | SpinnerOption():
            _draw_choice(c, mod, option, cx, cy, ch, hint)
        case SliderOption():
            _draw_slider(c, mod, option, cx, cy, ch, hint)
        case KeybindOption():
            _draw_keybind(c, mod, option, cx, cy, ch, hint)
        case ButtonOption():
            c.button("Run", cx, cy, CONTROL_W, ch, lambda: _press(option),
                     fill=BUTTON, hover=BUTTON_HOVER, size=16, hint=hint)  # fmt: skip
        case NestedOption():
            c.button("Open  >", cx, cy, CONTROL_W, ch, lambda: _open_nested(option),
                     fill=BUTTON, hover=BUTTON_HOVER, size=16, hint=hint)  # fmt: skip
        case ValueOption():
            # Some other kind of value (HiddenOption is never shown): show it read-only
            c.label(str(option.value)[:40], cx + CONTROL_W, y, ROW_H, 16, MUTED, 6, right=True)  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
        case _:
            pass


def _draw_bool(c: Canvas, mod: Mod, option: BoolOption, x: float, y: float, h: float, hint: Hint) -> None:
    half = (CONTROL_W - 6) / 2
    for i, (text, value) in enumerate(((option.false_text or "Off", False), (option.true_text or "On", True))):
        chosen = option.value == value
        c.button(text, x + i * (half + 6), y, half, h, None if chosen else (lambda v=value: _set_value(mod, option, v)),
                 fill=CHOSEN if chosen else UNCHOSEN, hover=BUTTON_HOVER, size=16,
                 text_color=WHITE if chosen else MUTED, hint=hint)  # fmt: skip


def _draw_choice(c: Canvas, mod: Mod, option: DropdownOption | SpinnerOption, x: float, y: float,
                 h: float, hint: Hint) -> None:  # fmt: skip
    choices = list(option.choices)
    index = choices.index(option.value) if option.value in choices else -1
    wraps = isinstance(option, DropdownOption) or option.wrap_enabled

    def step(delta: int) -> Callable[[], None] | None:
        target = index + delta
        if not choices or (not wraps and not 0 <= target < len(choices)):
            return None
        return lambda: _set_value(mod, option, choices[target % len(choices)])

    back, forward = step(-1), step(1)
    c.button("<", x, y, 48, h, back, fill=BUTTON if back else DISABLED, hover=BUTTON_HOVER, size=18, hint=hint)
    c.button(str(option.value)[:34], x + 54, y, CONTROL_W - 108, h, forward,
             fill=UNCHOSEN, hover=ROW_HOVER, size=16, hint=hint)  # fmt: skip
    c.button(">", x + CONTROL_W - 48, y, 48, h, forward,
             fill=BUTTON if forward else DISABLED, hover=BUTTON_HOVER, size=18, hint=hint)  # fmt: skip


def _slider_value(option: SliderOption, value: float) -> float:
    lo, hi = option.min_value, option.max_value
    if option.step > 0:
        value = lo + round((value - lo) / option.step) * option.step
    value = min(hi, max(lo, value))
    return round(value) if option.is_integer else value


def _draw_slider(c: Canvas, mod: Mod, option: SliderOption, x: float, y: float, h: float, hint: Hint) -> None:
    lo, hi = option.min_value, option.max_value
    value = option.value
    less = _slider_value(option, value - option.step)
    more = _slider_value(option, value + option.step)
    c.button("-", x, y, 48, h, (lambda: _set_value(mod, option, less)) if less != value else None,
             fill=BUTTON if less != value else DISABLED, hover=BUTTON_HOVER, size=20, hint=hint)  # fmt: skip
    c.button("+", x + CONTROL_W - 48, y, 48, h, (lambda: _set_value(mod, option, more)) if more != value else None,
             fill=BUTTON if more != value else DISABLED, hover=BUTTON_HOVER, size=20, hint=hint)  # fmt: skip

    tx, tw = x + 54, CONTROL_W - 108
    frac = 0.0 if hi <= lo else min(1.0, max(0.0, (value - lo) / (hi - lo)))
    c.box(tx, y, tw, h, TRACK, 20)
    if frac > 0:
        c.box(tx, y, tw * frac, h, TRACK_FILL, 21)
    c.label(_fmt_number(option, value), tx, y, h, 16, WHITE, 24, center_w=tw)
    # Clickable slices of the track: the first is the minimum, the last the maximum
    seg = tw / SLIDER_SEGMENTS
    for i in range(SLIDER_SEGMENTS):
        target = _slider_value(option, lo + (hi - lo) * i / (SLIDER_SEGMENTS - 1))
        c.hit_area(tx + i * seg, y, seg, h, lambda t=target: _set_value(mod, option, t), hint=hint, z=25)


def _draw_keybind(c: Canvas, mod: Mod, option: KeybindOption, x: float, y: float, h: float, hint: Hint) -> None:
    if S.capture is not None and S.capture.identifier == option.identifier:
        c.button("Press a key...  (Esc cancels)", x, y, CONTROL_W - 126, h, _cancel_capture,
                 fill=CAPTURE, hover=CAPTURE, size=15, hint=hint)  # fmt: skip
        c.button("Unbind", x + CONTROL_W - 120, y, 120, h, lambda: _finish_capture(mod, option, None),
                 fill=RED, hover=RED_HOVER, size=15, hint=hint)  # fmt: skip
        return
    if not option.is_rebindable:
        c.button(keys.display_name(option.value), x, y, CONTROL_W, h, None, fill=DISABLED, size=16,
                 text_color=MUTED, hint=hint)  # fmt: skip
        return
    c.button(keys.display_name(option.value), x, y, CONTROL_W, h, lambda: _start_capture(option),
             fill=UNCHOSEN, hover=BUTTON_HOVER, size=16, text_color=GOLD, hint=hint)  # fmt: skip


def _draw_footer(c: Canvas) -> None:
    c.box(RIGHT_X - 4, FOOTER_Y, RIGHT_W + 8, FOOTER_H, (0.03, 0.08, 0.09, 0.98), 2)
    hint = S.hint or DEFAULT_HINT
    S.hint_title = WeakPointer(c.label(hint.title, RIGHT_X + 12, FOOTER_Y + 6, 30, 16, ACCENT, 5))
    S.hint_text = WeakPointer(
        c.paragraph(hint.text, RIGHT_X + 12, FOOTER_Y + 38, RIGHT_W - 24, FOOTER_H - 44, 14, WHITE, 5)
    )


def _show_hint(hint: Hint | None) -> None:
    """Updates the description at the bottom in place (cheaper and smoother than a redraw)."""
    S.hint = hint
    shown = hint or DEFAULT_HINT
    title, text = S.hint_title(), S.hint_text()
    if title is not None:
        call(title, "SetText", shown.title)
    if text is not None:
        call(text, "SetText", shown.text)


# endregion
# region Actions


def _say(msg: str, *, error: bool = False) -> None:
    S.message, S.message_error, S.message_at = msg, error, time.monotonic()
    (logging.error if error else logging.info)(f"[Mod Menu] {msg}")


def _select(mod: Mod) -> None:
    if mod.name != S.mod_name:
        S.mod_name = mod.name
        S.nested.clear()
        S.option_page = 0
        S.capture = None
        S.hint = None
    _draw()


def _turn_mod_page(delta: int) -> None:
    S.mod_page = max(0, S.mod_page + delta)
    _draw()


def _turn_option_page(delta: int) -> None:
    S.option_page = max(0, S.option_page + delta)
    S.hint = None
    _draw()


def _open_nested(option: NestedOption) -> None:
    S.nested.append(option.identifier)
    S.option_page = 0
    S.hint = None
    _draw()


def _back() -> None:
    if S.nested:
        S.nested.pop()
        S.option_page = 0
        S.hint = None
    _draw()


def _set_enabled(mod: Mod, enabled: bool) -> None:
    try:
        if enabled:
            mod.enable()
        else:
            mod.disable()
        _say(f"{mod.name} {'enabled' if mod.is_enabled else 'disabled'}.")
    except Exception as ex:  # noqa: BLE001
        logging.exception(ex)
        _say(f"Couldn't {'enable' if enabled else 'disable'} {mod.name}: {ex}", error=True)
    _draw()


def _set_value(mod: Mod, option: ValueOption[Any], value: Any) -> bool:
    """Sets an option (running its on_change) and saves the mod's settings."""
    ok = True
    try:
        option.value = value
        mod.save_settings()
    except Exception as ex:  # noqa: BLE001
        logging.exception(ex)
        _say(f"Couldn't change {option.display_name}: {ex}", error=True)
        ok = False
    _draw()
    return ok


def _press(option: ButtonOption) -> None:
    """Runs a button next frame, outside our button polling (it may open UI or travel)."""

    def run() -> None:
        try:
            if option.on_press is not None:
                option.on_press(option)
        except Exception as ex:  # noqa: BLE001
            logging.exception(ex)
            _say(f"{option.display_name} failed: {ex}", error=True)
        if S.is_open:
            _draw()

    S.pending = run


def _start_capture(option: KeybindOption) -> None:
    S.capture = option
    # Only keys pressed from now on count (whatever is already held, e.g. a modifier, doesn't)
    S.capture_held = keys.held_keys()
    _draw()


def _cancel_capture() -> None:
    S.capture = None
    _draw()


def _finish_capture(mod: Mod, option: KeybindOption, key: str | None) -> None:
    S.capture = None
    # The new key is still held: it mustn't count as a press of our own keys next frame
    S.keys_down.update({vk: True for vk in keys.held_keys()})
    if key is not None:
        S.keys_down[keys.VIRTUAL_KEYS[key]] = True
    S.message = ""
    if _set_value(mod, option, key):
        _say(f"{option.display_name}: {keys.display_name(key)}.")
        _draw()


def _poll_capture(mod: Mod | None) -> bool:
    """While rebinding, watches for the new key. Returns True if it handled this frame."""
    option = S.capture
    if option is None or mod is None:
        S.capture = None
        return False
    if not keys.game_focused():
        return False
    if _edge(keys.VK_ESCAPE):
        _cancel_capture()
        return True
    held = keys.held_keys()
    new = held - S.capture_held
    # A key released and pressed again counts too
    S.capture_held &= held
    if new:
        _finish_capture(mod, option, keys.KEY_NAMES[min(new)])
        return True
    return False


# endregion
# region Open / close


def open_menu(mod_name: str | None = None) -> None:
    """Opens the menu, on the given mod's page if a name is given."""
    if mod_name is not None:
        if mod_name != S.mod_name:
            S.nested.clear()
            S.option_page = 0
        S.mod_name = mod_name
    if S.is_open:
        _draw()
        return
    pc = get_pc(possibly_loading=True)
    if pc is None:
        logging.info("[Mod Menu] Can't open the menu while loading.")
        return
    _close_other_panels()
    S.pc = WeakPointer(pc)
    S.scale = viewport_scale(pc)
    overlay = make_overlay(pc, S.scale, Z_ORDER)
    S.overlay = WeakPointer(overlay)
    S.is_open = True
    S.capture = None
    S.hint = None
    S.cursor_was_shown = bool(pc.bShowMouseCursor)
    # The key that opened us is still held
    S.keys_down = {vk: True for vk in keys.held_keys()}
    pc.bShowMouseCursor = True
    _draw()
    call(overlay, "SetUserFocus", pc)
    call(overlay, "SetKeyboardFocus")
    call(widget_lib(), "SetInputMode_UIOnlyEx", pc, overlay, 0, False)


def close() -> None:
    if not S.is_open:
        return
    pc, overlay = S.pc(), S.overlay()
    _forget()
    if overlay is not None:
        call(overlay, "RemoveFromParent")
    if pc is not None:
        if S.cursor_was_shown or pc.Pawn is None:
            # A game menu (or the title screen) was up under us: give it UI input and its cursor back
            call(widget_lib(), "SetInputMode_GameAndUIEx", pc, None, 0, False, False)
        else:
            pc.bShowMouseCursor = False
            call(widget_lib(), "SetInputMode_GameOnly", pc, True)
        call(widget_lib(), "SetFocusToGameViewport")
    S.toggle_was_down = True  # don't let a held open key reopen us


def _forget() -> None:
    S.is_open = False
    S.canvas = None
    S.capture = None
    S.pc = WeakPointer()
    S.overlay = WeakPointer()
    S.hint_title = WeakPointer()
    S.hint_text = WeakPointer()


def toggle() -> None:
    if S.is_open:
        close()
    else:
        open_menu()


def _release_after(seconds: float, vk: int) -> None:
    """Sends the key up after this long."""
    if S.release is not None and S.release[1] != vk:
        keys.post_key(S.release[1], down=False)
    S.release = (time.perf_counter() + seconds, vk)


def press_escape() -> None:
    """Esc for the game: opens the pause menu in gameplay, closes the open menu otherwise."""
    if keys.post_key(keys.VK_ESCAPE, down=True):
        _release_after(ESC_HOLD_S, keys.VK_ESCAPE)


def open_key_pressed() -> None:
    """The open key, while the UMG panel is closed: toggles the MODS screen (or opens the panel)."""
    pc = get_pc(possibly_loading=True)
    # page_open can be stale (the page sleeps when the game's menus close), so only trust it
    # while a menu is actually up
    if pc is not None and pc.bShowMouseCursor and web.Link.page_open and web.connected():
        web.request_close()
    else:
        open_preferred()


def open_preferred(mod_name: str | None = None, fallback: Callable[[], None] | None = None) -> None:
    """
    Opens the HTML menu (the MODS screen in the game's pause menu) when its page is loaded, or
    this UMG panel where it isn't (e.g. the title screen). fallback: opened instead of this UMG
    panel (a mod whose page draws more than its options has a panel of its own, e.g. BL4 Direct).
    """
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return
    # The page lives in the in-game menus, and its scripts only run while one of them is showing,
    # so in game always try it: open the pause menu, ask for MODS, and fall back to the basic
    # panel if the page doesn't answer (e.g. no MODS entry installed). At the title screen the
    # page is the main menu's (main_page), when it's installed and polling.
    if web.Link.server is None or (pc.Pawn is None and not web.connected()):
        fallback() if fallback is not None else open_menu(mod_name)
        return
    own_menu = not pc.bShowMouseCursor
    if own_menu:
        # In gameplay with no menu up: press Esc for the game, which opens the pause menu (the
        # MODS screen lives in that page, whose scripts only run while a menu is showing). Hold it
        # for a few frames: the game samples keys once a frame and ignores a same-frame tap. The
        # page hides itself as it wakes until it has asked us what to show, so the pause menu
        # itself never shows (menu.js, "Waking up").
        press_escape()
    web.request_open(mod_name, own_menu=own_menu)
    # Counts only once the page reports its screen open after this request
    web.Link.page_open = False
    S.web_wait = (time.monotonic() + WEB_WAIT_S, mod_name, fallback)


def _close_other_panels() -> None:
    """Two full-screen panels fighting over input focus is no good: close BL4 Direct's F6 panel."""
    panel = sys.modules.get("bl4_direct.ui")
    state = getattr(panel, "S", None)
    if state is not None and getattr(state, "is_open", False):
        try:
            panel.close()  # pyright: ignore[reportOptionalMemberAccess]
        except Exception:  # noqa: BLE001
            pass


def _other_panel_open() -> bool:
    state = getattr(sys.modules.get("bl4_direct.ui"), "S", None)
    return bool(getattr(state, "is_open", False))


# endregion
# region Per-frame polling


def _edge(vk: int) -> bool:
    down = keys.vk_down(vk)
    was = S.keys_down.get(vk, False)
    S.keys_down[vk] = down
    return down and not was


def _on_tick(*_: Any) -> None:
    try:
        _tick()
    except Exception as ex:  # noqa: BLE001 - never let our tick break the game's
        logging.error(f"[Mod Menu] {ex!r}")
        logging.exception(ex)


def _tick() -> None:
    if S.release is not None:
        until, vk = S.release
        if time.perf_counter() >= until:
            S.release = None
            keys.post_key(vk, down=False)
        return

    now = time.perf_counter()
    gap, S.last_call = now - S.last_call, now
    if gap < FRAME_GAP_S and now - S.ticked_at < MAX_WAIT_S:
        return
    S.ticked_at = now
    pc = get_pc(possibly_loading=True)
    if pc is None:
        return
    web.tick(bool(pc.bShowMouseCursor) and not S.is_open)

    if S.pending is not None:
        action, S.pending = S.pending, None
        action()
        return

    if S.web_wait is not None:
        if web.Link.page_open:
            S.web_wait = None
        elif time.monotonic() > S.web_wait[0]:
            # The page didn't answer (not loaded in this menu?): use this panel instead
            web.Link.trace.append(f"{time.monotonic():.2f} fallback to the basic panel")
            wait, S.web_wait = S.web_wait, None
            fallback = wait[2] if len(wait) > 2 else None
            fallback() if fallback is not None else open_menu(wait[1])
            return

    key = _open_key()
    if not S.is_open:
        down = keys.key_down(key)
        if down and not S.toggle_was_down:
            open_key_pressed()
        S.toggle_was_down = down
        return

    if pc != S.pc() or S.overlay() is None or S.canvas is None:
        # Map changed underneath us; the old widgets went with the old world
        _forget()
        return
    if _other_panel_open():
        # Something opened its own panel from one of our buttons: let it have the screen
        close()
        return

    mod = _selected_mod()
    if S.capture is not None and _poll_capture(mod):
        return
    focused = keys.game_focused()
    if focused and S.capture is None:
        if _edge(keys.VK_ESCAPE):
            _back() if S.nested else close()
            return
        vk = keys.VIRTUAL_KEYS.get(key or "")
        if vk is not None and _edge(vk):
            close()
            return
        if _edge(VK_PRIOR):
            _turn_option_page(-1)
            return
        if _edge(VK_NEXT):
            _turn_option_page(1)
            return

    now = time.monotonic()
    if now - S.checked_at > CHANGE_CHECK_S:
        S.checked_at = now
        if viewport_scale(pc) != S.scale or _signature(mod) != S.signature:
            _draw()
            return
        if S.message and now - S.message_at > MESSAGE_S:
            S.message = ""
            _draw()
            return

    fire, hint, changed = S.canvas.poll()
    if changed and hint is not S.hint:
        _show_hint(hint)
    if fire is not None:
        fire()


def install(open_key: Callable[[], str | None], game_dir: Path) -> None:
    global _open_key
    _open_key = open_key
    S.toggle_was_down = True  # if the key is held right now, wait for a fresh press
    try:
        web.start(game_dir)
    except OSError as ex:
        logging.error(f"[Mod Menu] couldn't start the link to the HTML menu ({ex}); using the basic menu")
    web.close_game_menu = press_escape
    hooks.add_hook(TICK_FUNC, Type.POST, HOOK_ID, _on_tick)


def uninstall() -> None:
    close()
    if S.release is not None:
        keys.post_key(S.release[1], down=False)
        S.release = None
    hooks.remove_hook(TICK_FUNC, Type.POST, HOOK_ID)
    web.stop()


# endregion
