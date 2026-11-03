"""
A short notice at the top of the screen ("Kairosdex: 118 boss pictures made."), for the boss
pictures made at the title screen (titlebake.py), where no page of ours runs. Copied from Build
Presets' notice.py.

Plain UMG built in code like the third person crosshair: a translucent box with a line of text
and up to three more, anchored near the top centre, hit-test invisible (it never takes the mouse
or input), hidden again after a few seconds by tick(). Its name has a fixed prefix, so a copy of
this module loaded later (hot reload) finds and removes one an older copy left on screen.

Kairosdex's additions (2026-11-03): `hold` keeps it up until the next show / hide (the run's
progress, updated in place), and `progress` (0..1) shows a bar under the text: two Borders in a
HorizontalBox whose Fill sizes split the width, so it matches the notice's own look.
"""

from __future__ import annotations

import time
from typing import Any

import unrealsdk
from unrealsdk.unreal import WeakPointer

Z_ORDER = 20
WIDGET_NAME = "Kairosdex_Notice"
COLLAPSED, HIT_TEST_INVISIBLE = 1, 3
SHOW_S = 3.0
SHOW_ERROR_S = 5.0
MAX_DETAILS = 3
BAR_W, BAR_H = 460.0, 6.0
TRACK = (0.02, 0.04, 0.06, 1.0)
FILL_RULE = 1  # ESlateSizeRule::Fill
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
    bar = WeakPointer()  # the progress bar's row (collapsed without a progress)
    fill = WeakPointer()
    rest = WeakPointer()
    hide_at = 0.0

    @classmethod
    def _build(cls, pc: Any) -> Any:
        _remove_strays()
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

        size = _new("/Script/UMG.SizeBox", tree)
        size.SetWidthOverride(BAR_W)
        size.SetHeightOverride(BAR_H)
        size.SetVisibility(COLLAPSED)
        column.AddChild(size)
        size.Slot.SetPadding(unrealsdk.make_struct("Margin", Top=10.0))
        track = _new("/Script/UMG.Border", tree)
        track.SetBrushColor(_color(TRACK))
        track.SetPadding(unrealsdk.make_struct("Margin"))
        size.SetContent(track)
        row = _new("/Script/UMG.HorizontalBox", tree)
        track.SetContent(row)
        fill = _new("/Script/UMG.Border", tree)
        fill.SetBrushColor(_color(ACCENT))
        row.AddChild(fill)
        rest = _new("/Script/UMG.Spacer", tree)
        row.AddChild(rest)
        cls.bar, cls.fill, cls.rest = WeakPointer(size), WeakPointer(fill), WeakPointer(rest)

        widget.SetVisibility(HIT_TEST_INVISIBLE)
        widget.AddToViewport(Z_ORDER)
        return widget

    @classmethod
    def show(cls, pc: Any, title: str, details: list[str] | None = None, *, error: bool = False, hold: bool = False,
             progress: float | None = None) -> None:  # fmt: skip
        """`hold`: stays up until the next show or hide(). `progress` (0..1): a bar under the text."""
        widget = cls.widget()
        if widget is None or cls.owner() is not pc:
            cls.remove()
            widget = cls._build(pc)
            cls.widget = WeakPointer(widget)
            cls.owner = WeakPointer(pc)
        elif not widget.IsInViewport():
            # Taken off the screen by something else (a menu changing): a held notice comes back
            # at its next update instead of staying gone
            widget.AddToViewport(Z_ORDER)
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
        cls._progress(progress)
        widget.SetVisibility(HIT_TEST_INVISIBLE)
        cls.hide_at = 0.0 if hold else time.monotonic() + (SHOW_ERROR_S if error or details else SHOW_S)

    @classmethod
    def _progress(cls, progress: float | None) -> None:
        bar, fill, rest = cls.bar(), cls.fill(), cls.rest()
        if bar is None or fill is None or rest is None:
            return
        if progress is None:
            bar.SetVisibility(COLLAPSED)
            return
        done = min(max(progress, 0.0), 1.0)
        fill.Slot.SetSize(unrealsdk.make_struct("SlateChildSize", Value=done, SizeRule=FILL_RULE))
        rest.Slot.SetSize(unrealsdk.make_struct("SlateChildSize", Value=1.0 - done, SizeRule=FILL_RULE))
        bar.SetVisibility(HIT_TEST_INVISIBLE)

    @classmethod
    def hide(cls) -> None:
        """Hidden now (a held notice whose run was cut short)."""
        cls.hide_at = 0.0
        if (widget := cls.widget()) is not None:
            widget.SetVisibility(COLLAPSED)

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
        cls.bar = cls.fill = cls.rest = WeakPointer()
        cls.hide_at = 0.0
