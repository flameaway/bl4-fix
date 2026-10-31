"""
A small crosshair at the centre of the screen, for aiming in third person.

The HUD hides its own crosshair while you're zoomed (in first person the gun's sights take over),
so while aiming in third person we draw this instead: a dot and four ticks in UMG, anchored to the
screen centre so it doesn't depend on the resolution, and hit-test invisible.
"""

from __future__ import annotations

from typing import Any

import unrealsdk
from unrealsdk.unreal import WeakPointer

Z_ORDER = 10
COLLAPSED, HIT_TEST_INVISIBLE = 1, 3

DOT = 4.0
TICK_LONG, TICK_SHORT = 9.0, 2.0
GAP = 7.0  # from the centre to the near end of each tick
OUTLINE = 1.0
FILL = (1.0, 1.0, 1.0, 0.95)
EDGE = (0.0, 0.0, 0.0, 0.6)


def _new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(unrealsdk.find_object("Class", path), outer)


def _vec2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))


class Crosshair:
    widget = WeakPointer()
    owner = WeakPointer()
    shown = False

    @classmethod
    def _build(cls, pc: Any) -> Any:
        widget = _new("/Script/UMG.UserWidget", pc)
        widget.WidgetTree = _new("/Script/UMG.WidgetTree", widget)
        canvas = _new("/Script/UMG.CanvasPanel", widget.WidgetTree)
        widget.WidgetTree.RootWidget = canvas

        def box(x: float, y: float, w: float, h: float, color: tuple[float, ...]) -> None:
            border = _new("/Script/UMG.Border", widget.WidgetTree)
            border.SetBrushColor(unrealsdk.make_struct("LinearColor", R=color[0], G=color[1], B=color[2], A=color[3]))
            border.SetVisibility(HIT_TEST_INVISIBLE)
            canvas.AddChild(border)
            slot = border.Slot
            centre = _vec2(0.5, 0.5)
            slot.SetAnchors(unrealsdk.make_struct("Anchors", Minimum=centre, Maximum=centre))
            slot.SetAlignment(centre)
            slot.SetPosition(_vec2(x, y))
            slot.SetSize(_vec2(w, h))

        far = GAP + TICK_LONG / 2
        shapes = [
            (0.0, 0.0, DOT, DOT),
            (0.0, -far, TICK_SHORT, TICK_LONG),
            (0.0, far, TICK_SHORT, TICK_LONG),
            (-far, 0.0, TICK_LONG, TICK_SHORT),
            (far, 0.0, TICK_LONG, TICK_SHORT),
        ]
        for x, y, w, h in shapes:
            box(x, y, w + 2 * OUTLINE, h + 2 * OUTLINE, EDGE)
        for x, y, w, h in shapes:
            box(x, y, w, h, FILL)

        widget.SetVisibility(HIT_TEST_INVISIBLE)
        widget.AddToViewport(Z_ORDER)
        return widget

    @classmethod
    def show(cls, pc: Any, visible: bool) -> None:
        if not visible and not cls.shown:
            return  # already hidden (or never built): no WeakPointer reads, every frame in first person
        widget = cls.widget()
        if widget is None or cls.owner() is not pc:
            if not visible:
                return
            cls.remove()
            widget = cls._build(pc)
            cls.widget = WeakPointer(widget)
            cls.owner = WeakPointer(pc)
            cls.shown = True
        if visible != cls.shown:
            widget.SetVisibility(HIT_TEST_INVISIBLE if visible else COLLAPSED)
            cls.shown = visible

    @classmethod
    def remove(cls) -> None:
        widget = cls.widget()
        if widget is not None:
            widget.RemoveFromParent()
        cls.widget = WeakPointer()
        cls.owner = WeakPointer()
        cls.shown = False
