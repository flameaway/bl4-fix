"""
The "mods need an update" notice: a box near the top of the screen for a few seconds, once per game
version and set of problems (health/__init__.py decides when).

Plain UMG built in code, like Build Presets' notice.py (the same look): it doesn't need the game's
HTML pages, which may be what a game update broke. Hit-test invisible (never takes the mouse or
input). Its name has a fixed prefix so a later copy of this module finds one an older copy left.
"""

from __future__ import annotations

import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import WeakPointer

Z_ORDER = 21
WIDGET_NAME = "BL4FixHealth_Notice"
COLLAPSED, HIT_TEST_INVISIBLE = 1, 3
SHOW_S = 12.0
MAX_DETAILS = 3
# UMG colors are linear
BACKGROUND = (0.0012, 0.0052, 0.0098, 0.88)
ACCENT = (1.0, 0.45, 0.02, 1.0)
TITLE = (0.776, 0.693, 0.617, 1.0)
DETAIL = (0.745, 0.366, 0.107, 1.0)


class S:
    widget = WeakPointer()
    hide_at = 0.0


def _new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(unrealsdk.find_object("Class", path), outer)


def _vec2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))


def _color(rgba: tuple[float, ...]) -> Any:
    return unrealsdk.make_struct("LinearColor", R=rgba[0], G=rgba[1], B=rgba[2], A=rgba[3])


def _slate(rgba: tuple[float, ...]) -> Any:
    return unrealsdk.make_struct("SlateColor", SpecifiedColor=_color(rgba))


def show(pc: Any, title: str, details: list[str]) -> None:
    remove()
    # A fresh name each time: reusing one an old (removed, not yet collected) widget has would make
    # Unreal recycle that object in place. The PC is its outer and the viewport holds it.
    name = f"{WIDGET_NAME}_{time.monotonic_ns() % 10**12}"
    widget = unrealsdk.construct_object(unrealsdk.find_object("Class", "/Script/UMG.UserWidget"), pc, name)
    tree = widget.WidgetTree = _new("/Script/UMG.WidgetTree", widget)
    canvas = _new("/Script/UMG.CanvasPanel", tree)
    tree.RootWidget = canvas

    box = _new("/Script/UMG.Border", tree)
    box.SetBrushColor(_color(BACKGROUND))
    box.SetPadding(unrealsdk.make_struct("Margin", Left=22.0, Top=12.0, Right=22.0, Bottom=12.0))
    box.SetVisibility(HIT_TEST_INVISIBLE)
    canvas.AddChild(box)
    slot = box.Slot
    top = _vec2(0.5, 0.14)
    slot.SetAnchors(unrealsdk.make_struct("Anchors", Minimum=top, Maximum=top))
    slot.SetAlignment(_vec2(0.5, 0.0))
    slot.SetAutoSize(True)

    column = _new("/Script/UMG.VerticalBox", tree)
    box.SetContent(column)
    accent = _new("/Script/UMG.Border", tree)
    accent.SetBrushColor(_color(ACCENT))
    column.AddChild(accent)
    accent.Slot.SetPadding(unrealsdk.make_struct("Margin", Bottom=8.0))
    spacer = _new("/Script/UMG.Spacer", tree)
    spacer.SetSize(_vec2(1.0, 3.0))
    accent.SetContent(spacer)

    for i, line in enumerate([title, *details[:MAX_DETAILS]]):
        text = _new("/Script/UMG.TextBlock", tree)
        font = text.Font
        font.Size = 28 if i == 0 else 19
        text.SetFont(font)
        text.SetColorAndOpacity(_slate(TITLE if i == 0 else DETAIL))
        text.SetText(line)
        text.SetVisibility(HIT_TEST_INVISIBLE)
        column.AddChild(text)

    widget.SetVisibility(HIT_TEST_INVISIBLE)
    widget.AddToViewport(Z_ORDER)
    S.widget = WeakPointer(widget)
    S.hide_at = time.monotonic() + SHOW_S


def tick() -> None:
    if S.hide_at and time.monotonic() > S.hide_at:
        remove()


def remove() -> None:
    S.hide_at = 0.0
    if (widget := S.widget()) is not None:
        widget.RemoveFromParent()
    S.widget = WeakPointer()
