"""
Runtime UMG drawing for the menu: a full-screen UserWidget with one CanvasPanel, filled with
Borders, TextBlocks and nearly invisible Buttons that give us hit testing (the same technique as
BL4 Direct's F6 panel; see BL4_MODDING_GUIDE.md section 7).

Everything is laid out in 1920x1080 design units and scaled to the viewport. Text is sized with
its font rather than a render scale, so it stays crisp and wraps at the right width.

Buttons are polled, not bound: each frame `Canvas.poll()` checks IsPressed/IsHovered on every
button, fires the action on release, and recolours the hovered one in place.

Engine objects kept between frames are WeakPointers. If the world goes away they read as None, and
we never touch a freed object (that crashes the game).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import unrealsdk
from unrealsdk.unreal import WeakPointer

DESIGN_W, DESIGN_H = 1920.0, 1080.0

Color = tuple[float, float, float, float]

HITTEST_INVISIBLE = 3
JUSTIFY_CENTER = 1
CLIP_TO_BOUNDS = 1


def cls(path: str) -> Any:
    return unrealsdk.find_object("Class", path)


def new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(cls(path), outer)


def call(obj: Any, name: str, *args: Any) -> Any:
    """Calls a UFunction, ignoring failure (a missing function or a destroyed object)."""
    try:
        return getattr(obj, name)(*args)
    except Exception:  # noqa: BLE001
        return None


def vec2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))


def _to_linear(v: float) -> float:
    return v / 12.92 if v <= 0.04045 else ((v + 0.055) / 1.055) ** 2.4


def linear(c: Color) -> Any:
    """Colours are written as on screen (sRGB); UMG wants linear values."""
    r, g, b = (_to_linear(float(v)) for v in c[:3])
    return unrealsdk.make_struct("LinearColor", R=r, G=g, B=b, A=float(c[3]))


def slate(c: Color) -> Any:
    return unrealsdk.make_struct("SlateColor", SpecifiedColor=linear(c), ColorUseRule=0)


def widget_lib() -> Any:
    return cls("/Script/UMG.WidgetBlueprintLibrary").ClassDefaultObject


def viewport_scale(pc: Any) -> tuple[float, float]:
    """Design units to slot units, for the current viewport size and DPI."""
    layout = cls("/Script/UMG.WidgetLayoutLibrary").ClassDefaultObject
    w, h, dpi = DESIGN_W, DESIGN_H, 1.0
    try:
        size = layout.GetViewportSize(pc)
        if size.X > 100 and size.Y > 100:
            w, h = float(size.X), float(size.Y)
    except Exception:  # noqa: BLE001
        pass
    try:
        dpi = float(layout.GetViewportScale(pc)) or 1.0
    except Exception:  # noqa: BLE001
        pass
    # Slot coordinates are in DPI-unscaled units
    return (w / dpi) / DESIGN_W, (h / dpi) / DESIGN_H


@dataclass
class Hint:
    title: str
    text: str


class ButtonRef:
    """A polled button: its action, the backdrop to recolour on hover, and its hover hint."""

    def __init__(self, button: Any, backdrop: Any, fill: Color, hover: Color,
                 action: Callable[[], None] | None, hint: Hint | None) -> None:  # fmt: skip
        self.button = WeakPointer(button)
        self.backdrop = WeakPointer(backdrop) if backdrop is not None else None
        self.fill = fill
        self.hover = hover
        self.action = action
        self.hint = hint
        self.was_pressed = False
        self.was_hovered = False


class Canvas:
    """One drawing pass: a fresh CanvasPanel in the overlay, and the buttons drawn into it."""

    def __init__(self, overlay: Any, scale: tuple[float, float]) -> None:
        self.tree = overlay.WidgetTree
        self.sx, self.sy = scale
        self.panel = new("/Script/UMG.CanvasPanel", self.tree)
        self.tree.RootWidget.AddChild(self.panel)
        call(self.panel.Slot, "SetPosition", vec2(0, 0))
        call(self.panel.Slot, "SetSize", vec2(DESIGN_W * self.sx, DESIGN_H * self.sy))
        self.buttons: list[ButtonRef] = []
        # `panel` and `tree` are only for drawing this pass; later frames go through this
        self._panel_ref = WeakPointer(self.panel)

    def remove(self) -> None:
        """Takes this pass's widgets off the overlay, unless the world already destroyed them."""
        if (panel := self._panel_ref()) is not None:
            call(panel, "RemoveFromParent")

    # region Placement

    def _place(self, widget: Any, x: float, y: float, w: float, h: float, z: int,
               *, align: tuple[float, float] = (0.0, 0.0), auto_size: bool = False) -> None:  # fmt: skip
        self.panel.AddChild(widget)
        slot = widget.Slot
        call(slot, "SetAlignment", vec2(*align))
        call(slot, "SetPosition", vec2(x * self.sx, y * self.sy))
        if not auto_size:
            call(slot, "SetSize", vec2(w * self.sx, h * self.sy))
        call(slot, "SetAutoSize", auto_size)
        call(slot, "SetZOrder", z)

    def _font_size(self, size: float) -> float:
        return max(6.0, size * min(self.sx, self.sy))

    # endregion
    # region Widgets

    def box(self, x: float, y: float, w: float, h: float, fill: Color, z: int = 1) -> Any:
        border = new("/Script/UMG.Border", self.tree)
        call(border, "SetBrushColor", linear(fill))
        call(border, "SetVisibility", HITTEST_INVISIBLE)
        self._place(border, x, y, w, h, z)
        return border

    def _text_block(self, value: str, size: float, color: Color) -> Any:
        tb = new("/Script/UMG.TextBlock", self.tree)
        call(tb, "SetText", value)
        font = tb.Font
        font.Size = self._font_size(size)
        call(tb, "SetFont", font)
        call(tb, "SetColorAndOpacity", slate(color))
        call(tb, "SetVisibility", HITTEST_INVISIBLE)
        return tb

    def label(self, value: str, x: float, y: float, h: float, size: float, color: Color,
              z: int = 10, *, center_w: float | None = None, right: bool = False) -> Any:  # fmt: skip
        """
        One line of text, vertically centred in a row starting at y with height h. Left aligned at x,
        centred in [x, x + center_w] if center_w is given, or right aligned at x if right.
        """
        tb = self._text_block(value, size, color)
        if center_w is not None:
            self._place(tb, x + center_w / 2, y + h / 2, 0, 0, z, align=(0.5, 0.5), auto_size=True)
        else:
            self._place(tb, x, y + h / 2, 0, 0, z, align=(1.0 if right else 0.0, 0.5), auto_size=True)
        return tb

    def paragraph(self, value: str, x: float, y: float, w: float, h: float, size: float,
                  color: Color, z: int = 10) -> Any:  # fmt: skip
        """Wrapped text in a fixed box, clipped to it."""
        tb = self._text_block(value, size, color)
        call(tb, "SetAutoWrapText", True)
        call(tb, "SetClipping", CLIP_TO_BOUNDS)
        self._place(tb, x, y, w, h, z)
        return tb

    def button(self, text: str, x: float, y: float, w: float, h: float,
               action: Callable[[], None] | None, *, fill: Color, hover: Color | None = None,
               text_color: Color = (0.95, 0.97, 0.98, 1.0), size: float = 17,
               hint: Hint | None = None, z: int = 20) -> None:  # fmt: skip
        """
        A clickable box with centred text. With no action it's drawn but does nothing (still shows
        its hint on hover).
        """
        backdrop = self.box(x, y, w, h, fill, z)
        if text:
            self.label(text, x, y, h, size, text_color, z + 2, center_w=w)
        self.hit_area(x, y, w, h, action, backdrop=backdrop, fill=fill,
                      hover=hover if action is not None else None, hint=hint, z=z + 1)  # fmt: skip

    def hit_area(self, x: float, y: float, w: float, h: float, action: Callable[[], None] | None,
                 *, backdrop: Any = None, fill: Color = (0, 0, 0, 0), hover: Color | None = None,
                 hint: Hint | None = None, z: int = 21) -> None:  # fmt: skip
        """An invisible Button: hit testing and press/hover state for whatever is drawn under it."""
        btn = new("/Script/UMG.Button", self.tree)
        call(btn, "SetRenderOpacity", 0.0)
        self._place(btn, x, y, w, h, z)
        self.buttons.append(ButtonRef(btn, backdrop, fill, hover or fill, action, hint))

    # endregion
    # region Polling

    def poll(self) -> tuple[Callable[[], None] | None, Hint | None, bool]:
        """
        Call each frame. Returns (an action to run now, the hovered hint, whether the hovered button
        changed). Actions fire on release, like a normal button.
        """
        fire: Callable[[], None] | None = None
        hovered: Hint | None = None
        changed = False
        for ref in self.buttons:
            btn = ref.button()
            if btn is None:
                continue
            try:
                pressed = bool(btn.IsPressed())
                is_hovered = bool(btn.IsHovered())
            except Exception:  # noqa: BLE001
                continue
            if pressed:
                ref.was_pressed = True
            elif ref.was_pressed:
                ref.was_pressed = False
                if is_hovered and ref.action is not None and fire is None:
                    fire = ref.action
            if is_hovered != ref.was_hovered:
                ref.was_hovered = is_hovered
                changed = True
                backdrop = ref.backdrop() if ref.backdrop is not None else None
                if backdrop is not None and ref.hover != ref.fill:
                    call(backdrop, "SetBrushColor", linear(ref.hover if is_hovered else ref.fill))
            if is_hovered and ref.hint is not None:
                hovered = ref.hint
        return fire, hovered, changed

    # endregion


def make_overlay(pc: Any, scale: tuple[float, float], z_order: int) -> Any:
    """A full-screen, focusable UserWidget with an empty root CanvasPanel, added to the viewport."""
    overlay = new("/Script/UMG.UserWidget", pc)
    overlay.WidgetTree = new("/Script/UMG.WidgetTree", overlay)
    overlay.WidgetTree.RootWidget = new("/Script/UMG.CanvasPanel", overlay.WidgetTree)
    call(overlay, "SetIsFocusable", True)
    call(overlay, "SetAlignmentInViewport", vec2(0, 0))
    call(overlay, "SetPositionInViewport", vec2(0, 0), False)
    call(overlay, "SetDesiredSizeInViewport", vec2(DESIGN_W * scale[0], DESIGN_H * scale[1]))
    overlay.AddToViewport(z_order)
    return overlay
