"""
A short notice at the top of the screen when a benchmark starts and when it's done (the run
happens in gameplay, where no page script runs). Copied from Build Presets' notice.py.

Plain UMG built in code like the third person crosshair: a translucent box with a line of text
and up to three more, anchored near the top centre, hit-test invisible (it never takes the mouse
or input), hidden again after a few seconds by tick(). Its name has a fixed prefix, so remove()
finds one an older copy of this module left on screen. Unlike Build Presets', the first show
doesn't search for those (find_all, ~20 ms, would land on the benchmark's start): the reload
script and the disable path remove the notice first. Game thread only.
"""

from __future__ import annotations

import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import WeakPointer

Z_ORDER = 20
WIDGET_NAME = "BL4Profiler_Notice"
COLLAPSED, HIT_TEST_INVISIBLE = 1, 3
SHOW_S = 3.0
SHOW_ERROR_S = 5.0
MAX_DETAILS = 3
# UMG colors are linear
BACKGROUND = (0.0012, 0.0052, 0.0098, 0.88)
ACCENT = (0.212, 0.723, 1.0, 1.0)
ACCENT_ERROR = (1.0, 0.147, 0.093, 1.0)
TITLE = (0.776, 0.693, 0.617, 1.0)
DETAIL = (0.745, 0.366, 0.107, 1.0)


def _new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(unrealsdk.find_object("Class", path), outer)


_strays_checked = False


def _remove_strays() -> None:
    """
    Removes notices left by earlier copies of this module (they lost track of them). Only strays
    from before this copy loaded can exist (our own notice is tracked), and find_all walks every
    object (~20 ms), so once per module load.
    """
    global _strays_checked
    if _strays_checked:
        return
    _strays_checked = True
    for widget in unrealsdk.find_all("UserWidget", exact=True):
        if widget.Name.startswith(WIDGET_NAME) and widget.IsInViewport():
            widget.RemoveFromParent()


def _vec2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))


def _color(rgba: tuple[float, ...]) -> Any:
    return unrealsdk.make_struct("LinearColor", R=rgba[0], G=rgba[1], B=rgba[2], A=rgba[3])


def _slate(rgba: tuple[float, ...]) -> Any:
    return unrealsdk.make_struct("SlateColor", SpecifiedColor=_color(rgba))


class Notice:
    widget = WeakPointer()
    owner = WeakPointer()
    lines: list[Any] = []  # noqa: RUF012
    accent = WeakPointer()
    hide_at = 0.0

    @classmethod
    def _build(cls, pc: Any) -> Any:
        # A fresh name each time: reusing one an old (removed, not yet collected) widget has would
        # make Unreal recycle that object in place
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

        cls.lines = []
        for i in range(1 + MAX_DETAILS):
            text = _new("/Script/UMG.TextBlock", tree)
            font = text.Font
            font.Size = 30 if i == 0 else 20
            text.SetFont(font)
            text.SetColorAndOpacity(_slate(TITLE if i == 0 else DETAIL))
            text.SetVisibility(HIT_TEST_INVISIBLE)
            column.AddChild(text)
            cls.lines.append(WeakPointer(text))
        cls.accent = WeakPointer(accent)

        widget.SetVisibility(HIT_TEST_INVISIBLE)
        widget.AddToViewport(Z_ORDER)
        return widget

    @classmethod
    def show(cls, pc: Any, title: str, details: list[str] | None = None, *, error: bool = False,
             hold: float | None = None) -> None:  # fmt: skip
        widget = cls.widget()
        if widget is None or cls.owner() is not pc:
            cls.remove()
            widget = cls._build(pc)
            cls.widget = WeakPointer(widget)
            cls.owner = WeakPointer(pc)
        texts = [title, *(details or [])[:MAX_DETAILS]]
        for i, ref in enumerate(cls.lines):
            line = ref()
            if line is None:
                continue
            if i < len(texts):
                line.SetText(texts[i])
                line.SetVisibility(HIT_TEST_INVISIBLE)
            else:
                line.SetVisibility(COLLAPSED)
        if (accent := cls.accent()) is not None:
            accent.SetBrushColor(_color(ACCENT_ERROR if error else ACCENT))
        widget.SetVisibility(HIT_TEST_INVISIBLE)
        cls.hide_at = time.monotonic() + (hold if hold is not None else SHOW_ERROR_S if error or details else SHOW_S)

    @classmethod
    def tick(cls) -> None:
        if cls.hide_at and time.monotonic() > cls.hide_at:
            cls.hide_at = 0.0
            if (widget := cls.widget()) is not None:
                widget.SetVisibility(COLLAPSED)

    @classmethod
    def remove(cls) -> None:
        if (widget := cls.widget()) is not None:
            widget.RemoveFromParent()
        _remove_strays()
        cls.widget = WeakPointer()
        cls.owner = WeakPointer()
        cls.lines = []
        cls.hide_at = 0.0
