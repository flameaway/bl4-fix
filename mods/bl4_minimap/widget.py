"""
The minimap on screen: a UMG widget we build in code (no assets of our own).

A round map picture (the scene capture's render target, drawn with a fully rounded Slate brush so
it comes out as a circle with a thin ring) with the player's arrow in the middle, in the top-right
corner. The arrow and markers are the game's own radar art; the map icons are the ECHO map's own.

The scene capture's render target holds a bigger north-up picture than the minimap shows. Each
frame that the view changed, the part around the player is drawn, turned, into a small display
render target (one Canvas K2_DrawTexture with a UV window and a rotation: a single textured quad
on the GPU), and the round (or square) image shows that whole. (Slate's rounded-box brushes ignore
the brush's UV region, so windowing the big picture in the brush itself didn't move the map.)
The quad is sqrt(2) bigger than the display so its turned corners still cover it.

Icons sit in layers, bottom to top: the route's dots, the map's icons, markers (enemies,
teammates, waypoint), then the player's arrow. Each layer keeps a pool of images per texture.
Markers out of range (objectives, waypoint, teammates, loot) go in the "edge" layer instead: centred
on the ring, half outside the map, so a far objective doesn't look like it's near the edge of the
range. That layer is on the root, over the frame (the square frame clips).
"""

from __future__ import annotations

from typing import Any

import unrealsdk
from unrealsdk.unreal import IGNORE_STRUCT, UObject, WeakPointer

from . import scene

Z_ORDER = -5  # under the game's menus
COLLAPSED, HIT_TEST_INVISIBLE = 1, 3
DRAW_ROUNDED_BOX = 4
ROUND_FIXED, ROUND_HALF_HEIGHT = 0, 1  # half the height: a circle when square
SQUARE_CORNER = 6.0
CLIP_TO_BOUNDS = 1
DISPLAY_SIZE = 512  # px of the display render target
RTF_RGBA8 = 2
BLEND_OPAQUE = 0
OVERSCAN = 2**0.5  # the turned quad's size / the display's
# The frame: a bright blue line (the game's HUD blue) with a dark rim outside it, like the
# Borderlands 3 minimap. UMG colours are linear.
RING_WIDTH = 5.0
RING_COLOR = (0.13, 0.62, 1.0, 1.0)  # sRGB ~(100, 206, 255)
RIM_WIDTH = 5.0
RIM_COLOR = (0.0, 0.006, 0.02, 0.85)
ARROW_SIZE = 44.0
# The north marker: an "N" in the game's font on a small dark disc, sitting on the frame's ring
NORTH_SIZE = 26.0  # UI units at 100% icon size
NORTH_OUTLINE = 2.0
NORTH_FONT = "/Game/Fonts/Industry.Industry"
NORTH_TYPEFACE = "Bold"
NORTH_TEXT_SIZE = 14.0
NORTH_TEXT_COLOR = (1.0, 1.0, 1.0, 1.0)
NORTH_FILL = (0.0, 0.006, 0.02, 0.95)
JUSTIFY_CENTER = 1
_RADAR = "/Game/uiresources/enemy_radar/assets/ui_art_asset/"
ARROW_TEXTURE = _RADAR + "enemy_radar_circle_player_arrow.enemy_radar_circle_player_arrow"
DOT_TEXTURE = _RADAR + "enemy_radar_circle_friendly.enemy_radar_circle_friendly"
# The compass's "above / below you" chevron (points down; turned for above), 40x32
CHEVRON_TEXTURE = "/Game/uiresources/compass/assets/UI_Art_Assets/compass_vertical_indicator.compass_vertical_indicator"
CHEVRON_SIZE = 16.0
CHEVRON_ASPECT = 0.8
_DISCOVERY = "/Game/uiresources/_shared/assets/ico_ui_art_discovery/"
# kind -> (texture, size px, tint)
MARKER_LOOKS = {
    "enemy": (_RADAR + "ui_art_radar_pip_medium.ui_art_radar_pip_medium", 24.0, (1, 1, 1, 1)),
    "boss": (_RADAR + "ui_art_radar_Bosspip.ui_art_radar_Bosspip", 40.0, (1, 1, 1, 1)),
    "friend": (DOT_TEXTURE, 24.0, (0.35, 0.85, 1, 1)),
    # A teammate whose class is known: its texture is their vault hunter's map icon (markers.py)
    "friend_class": (DOT_TEXTURE, 36.0, (1, 1, 1, 1)),
    "waypoint": (_DISCOVERY + "ico_Custom_Waypoint.ico_Custom_Waypoint", 40.0, (1, 1, 1, 1)),
    "objective": (_DISCOVERY + "ico_main_mission.ico_main_mission", 40.0, (1, 1, 1, 1)),
    # A rift / world event from the ECHO map (its texture is the map's icon for it)
    "event": (_DISCOVERY + "ico_world_events.ico_world_events", 36.0, (1, 1, 1, 1)),
    "legendary": (_DISCOVERY + "ico_misc_legendaries.ico_misc_legendaries", 34.0, (1, 1, 1, 1)),
    "pearl": ("/Game/DLC/Common/uiresources/dlc_rarity_pips/assets/ico_misc_pearl.ico_misc_pearl", 34.0, (1, 1, 1, 1)),
}
LAYERS = ("route", "icon", "marker")  # bottom to top, under the arrow
EDGE_LAYER = "edge"  # out-of-range markers, on the ring (over everything, the "N" included)

# An item: (texture path, base size px, tint, x, y, vertical), x/y in -1..1 of the minimap's
# half-size; vertical: 1 above you, -1 below you (a chevron), 0 neither
Item = tuple[str, float, tuple[float, float, float, float], float, float, int]


def _new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(unrealsdk.find_object("Class", path), outer)


def _vec2(x: float, y: float) -> Any:
    return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))


def _color(rgba: tuple[float, float, float, float]) -> Any:
    return unrealsdk.make_struct("LinearColor", R=rgba[0], G=rgba[1], B=rgba[2], A=rgba[3])


def _place(widget: Any, anchor: tuple[float, float], align: tuple[float, float], pos: tuple[float, float],
           size: tuple[float, float]) -> None:  # fmt: skip
    slot = widget.Slot
    a = _vec2(*anchor)
    slot.SetAnchors(unrealsdk.make_struct("Anchors", Minimum=a, Maximum=a))
    slot.SetAlignment(_vec2(*align))
    slot.SetPosition(_vec2(*pos))
    slot.SetSize(_vec2(*size))


def _fill(widget: Any) -> None:
    widget.Slot.SetAnchors(unrealsdk.make_struct("Anchors", Minimum=_vec2(0, 0), Maximum=_vec2(1, 1)))
    widget.Slot.SetOffsets(unrealsdk.make_struct("Margin", Left=0, Top=0, Right=0, Bottom=0))


class _Pooled:
    """One image in a layer's pool, with what it last showed (to skip unchanged updates)."""

    __slots__ = ("angle", "base", "image", "pos", "shown", "size", "tint")

    def __init__(self, image: Any, base: float) -> None:
        self.image = image
        self.base = base
        self.size: tuple[float, float] = (0.0, 0.0)
        self.angle = 0.0
        self.tint: tuple[float, ...] = ()
        self.pos: tuple[float, float] = (1e9, 1e9)
        self.shown = True


class Minimap:
    widget = WeakPointer()
    owner = WeakPointer()
    frame: Any = None
    rim: Any = None
    map_image: Any = None
    arrow: Any = None
    north: Any = None  # the north marker's disc
    letter: Any = None  # and its "N"
    north_pos: tuple[float, float] | None = None  # where it is (UI units), None: hidden
    north_scale = 0.0  # the icon scale it was sized for
    layers: dict[str, Any] = {}  # noqa: RUF012  # layer -> CanvasPanel
    shown = False
    square = False
    display = WeakPointer()  # the display render target (held by the image's brush)
    size = 0.0
    icon_scale = 1.0
    arrow_angle = 0.0
    view: tuple[float, ...] = ()  # (u, v, half, angle) shown
    margin = (0.0, 0.0)
    pools: dict[tuple[str, str], list[_Pooled]] = {}  # noqa: RUF012  # (layer, texture) -> images
    # Weak: Python references don't keep Unreal objects alive (a texture held only here can be
    # garbage collected and leave a dangling pointer)
    textures: dict[str, WeakPointer] = {}  # noqa: RUF012
    missing: set[str] = set()  # noqa: RUF012  # textures that didn't load

    @classmethod
    def _build(cls, pc: UObject, square: bool) -> Any:
        widget = _new("/Script/UMG.UserWidget", pc)
        tree = _new("/Script/UMG.WidgetTree", widget)
        widget.WidgetTree = tree
        root = _new("/Script/UMG.CanvasPanel", tree)
        tree.RootWidget = root

        # A dark rounded box a little bigger than the map, behind it: the rim around the blue line
        rim = _new("/Script/UMG.Image", tree)
        root.AddChild(rim)
        rim_brush = unrealsdk.make_struct("SlateBrush")
        rim_brush.TintColor.SpecifiedColor = _color(RIM_COLOR)
        rim_brush.DrawAs = DRAW_ROUNDED_BOX
        if square:
            rim_brush.OutlineSettings.RoundingType = ROUND_FIXED
            corners = rim_brush.OutlineSettings.CornerRadii
            corners.X = corners.Y = corners.Z = corners.W = SQUARE_CORNER + RIM_WIDTH
        else:
            rim_brush.OutlineSettings.RoundingType = ROUND_HALF_HEIGHT
        rim.SetBrush(rim_brush)

        frame = _new("/Script/UMG.CanvasPanel", tree)
        root.AddChild(frame)

        image = _new("/Script/UMG.Image", tree)
        frame.AddChild(image)
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        black = unrealsdk.make_struct("LinearColor", R=0, G=0, B=0, A=1)
        display = krl.CreateRenderTarget2D(pc, DISPLAY_SIZE, DISPLAY_SIZE, RTF_RGBA8, black, False, False)
        brush = unrealsdk.make_struct("SlateBrush")
        brush.ResourceObject = display
        brush.ImageSize.X = brush.ImageSize.Y = DISPLAY_SIZE
        brush.TintColor.SpecifiedColor = _color((1, 1, 1, 1))
        brush.DrawAs = DRAW_ROUNDED_BOX
        if square:
            brush.OutlineSettings.RoundingType = ROUND_FIXED
            corners = brush.OutlineSettings.CornerRadii
            corners.X = corners.Y = corners.Z = corners.W = SQUARE_CORNER
            frame.SetClipping(CLIP_TO_BOUNDS)  # icons at the edge
        else:
            brush.OutlineSettings.RoundingType = ROUND_HALF_HEIGHT
        brush.OutlineSettings.Width = RING_WIDTH
        brush.OutlineSettings.Color.SpecifiedColor = _color(RING_COLOR)
        image.SetBrush(brush)  # the image holds the display target from here on
        _fill(image)
        cls.display = WeakPointer(display)

        layers = {}
        for name in LAYERS:
            layer = _new("/Script/UMG.CanvasPanel", tree)
            frame.AddChild(layer)
            _fill(layer)
            layer.SetVisibility(HIT_TEST_INVISIBLE)
            layers[name] = layer

        arrow = _new("/Script/UMG.Image", tree)
        frame.AddChild(arrow)
        texture = cls._texture(ARROW_TEXTURE)
        if texture is not None:
            arrow.SetBrushFromTexture(texture, False)
        _place(arrow, (0.5, 0.5), (0.5, 0.5), (0, 0), (ARROW_SIZE, ARROW_SIZE))
        arrow.SetRenderTransformPivot(_vec2(0.5, 0.5))

        # On the root, not the frame: the square frame clips, and the badge sits across its edge
        north = _new("/Script/UMG.Image", tree)
        root.AddChild(north)
        north_brush = unrealsdk.make_struct("SlateBrush")
        north_brush.TintColor.SpecifiedColor = _color(NORTH_FILL)
        north_brush.DrawAs = DRAW_ROUNDED_BOX
        north_brush.OutlineSettings.RoundingType = ROUND_HALF_HEIGHT
        north_brush.OutlineSettings.Width = NORTH_OUTLINE
        north_brush.OutlineSettings.Color.SpecifiedColor = _color(RING_COLOR)
        north.SetBrush(north_brush)
        _place(north, (1, 0), (0.5, 0.5), (0, 0), (NORTH_SIZE, NORTH_SIZE))
        letter = _new("/Script/UMG.TextBlock", tree)
        root.AddChild(letter)
        letter.SetText("N")
        letter.SetJustification(JUSTIFY_CENTER)
        letter.SetColorAndOpacity(unrealsdk.make_struct("SlateColor", SpecifiedColor=_color(NORTH_TEXT_COLOR)))
        _place(letter, (1, 0), (0.5, 0.5), (0, 0), (NORTH_SIZE, NORTH_SIZE))
        letter.Slot.SetAutoSize(True)
        for part in (north, letter):
            part.SetVisibility(COLLAPSED)

        # Also on the root: its icons sit across the ring, and the square frame would clip them
        edge = _new("/Script/UMG.CanvasPanel", tree)
        root.AddChild(edge)
        edge.SetVisibility(HIT_TEST_INVISIBLE)
        layers[EDGE_LAYER] = edge

        widget.SetVisibility(HIT_TEST_INVISIBLE)
        widget.AddToViewport(Z_ORDER)
        cls.frame, cls.rim, cls.map_image, cls.arrow, cls.layers = frame, rim, image, arrow, layers
        cls.north, cls.letter = north, letter
        cls.square = square
        cls.size = 0.0
        cls.icon_scale = 1.0
        cls.arrow_angle = 0.0
        cls.view = ()
        cls.north_pos = None
        cls.north_scale = 0.0
        cls.pools = {}
        return widget

    @classmethod
    def _texture(cls, path: str) -> Any:
        if path in cls.missing:
            return None
        texture = cls.textures[path]() if path in cls.textures else None
        if texture is None:
            texture = scene._load(path)  # noqa: SLF001
            if texture is None:
                cls.missing.add(path)  # (a DLC's art that isn't installed): don't look again
                return None
            cls.textures[path] = WeakPointer(texture)
        return texture

    @classmethod
    def _pooled(cls, layer: str, path: str, index: int, base: float) -> _Pooled:
        pool = cls.pools.setdefault((layer, path), [])
        while len(pool) <= index:
            image = _new("/Script/UMG.Image", cls.widget().WidgetTree)
            cls.layers[layer].AddChild(image)
            texture = cls._texture(path)
            if texture is not None:
                image.SetBrushFromTexture(texture, False)
            _place(image, (0.5, 0.5), (0.5, 0.5), (0, 0), (base, base))
            image.SetRenderTransformPivot(_vec2(0.5, 0.5))
            pool.append(_Pooled(image, base))
        return pool[index]

    @classmethod
    def _put(cls, layer: str, path: str, index: int, base: float, size: tuple[float, float],
             tint: tuple[float, ...], pos: tuple[float, float], angle: float = 0.0) -> None:  # fmt: skip
        entry = cls._pooled(layer, path, index, base)
        if size != entry.size:
            entry.image.Slot.SetSize(_vec2(*size))
            entry.size = size
        if tint != entry.tint:
            entry.image.SetColorAndOpacity(_color(tint))
            entry.tint = tint
        if pos != entry.pos:
            entry.image.Slot.SetPosition(_vec2(*pos))
            entry.pos = pos
        if angle != entry.angle:
            entry.image.SetRenderTransformAngle(angle)
            entry.angle = angle
        if not entry.shown:
            entry.image.SetVisibility(HIT_TEST_INVISIBLE)
            entry.shown = True

    @classmethod
    def set_layer(cls, layer: str, items: list[Item]) -> None:
        """Shows these items in a layer (and hides the rest of its pool)."""
        if cls.widget() is None:
            return
        half = cls.size / 2
        used: dict[str, int] = {}
        chevrons: list[tuple[tuple[float, float], int]] = []
        for path, base, tint, x, y, vertical in items:
            if cls._texture(path) is None:
                continue
            i = used.get(path, 0)
            used[path] = i + 1
            size = base * cls.icon_scale
            # Same scale as the map under it; only icons at the very edge are pulled in to fit
            px, py = x * half, y * half
            edge = max(0.0, half - size / 2 - RING_WIDTH)
            if layer == EDGE_LAYER:
                reach = half - RING_WIDTH / 2  # (x, y) is on the rim: the middle of the blue line
                px, py = x * reach, y * reach
            elif cls.square:
                px, py = max(-edge, min(edge, px)), max(-edge, min(edge, py))
            else:
                d = (px * px + py * py) ** 0.5
                if d > edge:
                    px, py = px * edge / d, py * edge / d
            pos = (round(px, 1), round(py, 1))
            cls._put(layer, path, i, base, (size, size), tint, pos)
            if vertical:
                # Above: over the icon, pointing up. Below: under it, pointing down.
                gap = size / 2 + CHEVRON_SIZE * CHEVRON_ASPECT * cls.icon_scale / 2
                chevrons.append(((pos[0], round(pos[1] - vertical * gap, 1)), vertical))
        for i, (pos, vertical) in enumerate(chevrons):
            size = CHEVRON_SIZE * cls.icon_scale
            angle = 180.0 if vertical > 0 else 0.0
            cls._put(layer, CHEVRON_TEXTURE, i, CHEVRON_SIZE, (size, size * CHEVRON_ASPECT), (1, 1, 1, 1), pos, angle)
        used[CHEVRON_TEXTURE] = len(chevrons)
        for (pool_layer, path), pool in cls.pools.items():
            if pool_layer != layer:
                continue
            for entry in pool[used.get(path, 0):]:
                if entry.shown:
                    entry.image.SetVisibility(COLLAPSED)
                    entry.shown = False

    @classmethod
    def show(cls, pc: UObject, visible: bool, size: float = 0.0, margin: tuple[float, float] = (0, 0),
             arrow_angle: float = 0.0, icon_scale: float = 1.0, *, square: bool = False) -> None:  # fmt: skip
        """`size`/`margin` in UI units."""
        widget = cls.widget()
        if widget is None or cls.owner() is not pc or (visible and square != cls.square):
            if not visible:
                return
            cls.remove()
            widget = cls._build(pc, square)
            cls.widget = WeakPointer(widget)
            cls.owner = WeakPointer(pc)
            cls.shown = True
        if visible != cls.shown:
            widget.SetVisibility(HIT_TEST_INVISIBLE if visible else COLLAPSED)
            cls.shown = visible
        if not visible:
            return
        if size != cls.size or margin != cls.margin:
            _place(cls.frame, (1, 0), (1, 0), (-margin[0], margin[1]), (size, size))
            rim = size + 2 * RIM_WIDTH
            _place(cls.rim, (1, 0), (1, 0), (-margin[0] + RIM_WIDTH, margin[1] - RIM_WIDTH), (rim, rim))
            _place(cls.layers[EDGE_LAYER], (1, 0), (1, 0), (-margin[0], margin[1]), (size, size))
            cls.size, cls.margin = size, margin
        if icon_scale != cls.icon_scale:
            cls.icon_scale = icon_scale
            cls.arrow.Slot.SetSize(_vec2(ARROW_SIZE * icon_scale, ARROW_SIZE * icon_scale))
        if arrow_angle != cls.arrow_angle:
            cls.arrow.SetRenderTransformAngle(arrow_angle)
            cls.arrow_angle = arrow_angle

    @classmethod
    def set_north(cls, where: tuple[float, float] | None) -> None:
        """
        Puts the north marker on the ring at `where` (-1..1 of the half-size, x right, y down: a
        point on the rim), or hides it (None). Call after `show`.
        """
        if cls.widget() is None or cls.north is None:
            return
        pos = None
        if where is not None:
            half = cls.size / 2
            reach = half - RING_WIDTH / 2  # the middle of the blue line
            pos = (round(-cls.margin[0] - half + where[0] * reach, 1), round(cls.margin[1] + half + where[1] * reach, 1))
        if pos == cls.north_pos:
            return
        if (pos is None) != (cls.north_pos is None):
            for part in (cls.north, cls.letter):
                part.SetVisibility(COLLAPSED if pos is None else HIT_TEST_INVISIBLE)
        cls.north_pos = pos
        if pos is None:
            return
        if cls.icon_scale != cls.north_scale:
            cls.north_scale = cls.icon_scale
            size = NORTH_SIZE * cls.icon_scale
            cls.north.Slot.SetSize(_vec2(size, size))
            font = cls._font()
            if font is not None:
                cls.letter.SetFont(unrealsdk.make_struct(
                    "SlateFontInfo", FontObject=font, TypefaceFontName=NORTH_TYPEFACE,
                    Size=round(NORTH_TEXT_SIZE * cls.icon_scale),
                ))  # fmt: skip
        cls.north.Slot.SetPosition(_vec2(*pos))
        cls.letter.Slot.SetPosition(_vec2(*pos))

    @classmethod
    def _font(cls) -> Any:
        """The game's UI font (held by the text block's Font from then on)."""
        if NORTH_FONT in cls.missing:
            return None
        try:
            font = unrealsdk.find_object("Font", NORTH_FONT)
        except ValueError:
            font = scene._load(NORTH_FONT)  # noqa: SLF001
        if font is None:
            cls.missing.add(NORTH_FONT)
        return font

    @classmethod
    def set_view(cls, u: float, v: float, half: float, angle: float, *, force: bool = False) -> None:
        """
        Shows the part of the picture centred on (u, v) (0..1), `half` of the picture wide each way,
        turned by `angle` degrees clockwise: draws it into the display target.
        """
        display, pc = cls.display(), cls.owner()
        picture = scene.Scene.target()
        if display is None or pc is None or picture is None:
            return
        view = (round(u, 5), round(v, 5), round(half, 5), round(angle, 2))
        if view == cls.view and not force:
            return
        cls.view = view
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, display, None, IGNORE_STRUCT, IGNORE_STRUCT)
        side = DISPLAY_SIZE * OVERSCAN
        offset = (DISPLAY_SIZE - side) / 2
        span = half * OVERSCAN
        canvas.K2_DrawTexture(
            picture, _vec2(offset, offset), _vec2(side, side), _vec2(u - span, v - span), _vec2(2 * span, 2 * span),
            _color((1, 1, 1, 1)), BLEND_OPAQUE, angle, _vec2(0.5, 0.5),
        )  # fmt: skip
        krl.EndDrawCanvasToRenderTarget(pc, ctx)

    @classmethod
    def remove(cls) -> None:
        widget = cls.widget()
        if widget is not None:
            widget.RemoveFromParent()
        cls.widget = WeakPointer()
        cls.owner = WeakPointer()
        cls.frame = cls.rim = cls.map_image = cls.arrow = cls.north = cls.letter = None
        cls.north_pos = None
        cls.display = WeakPointer()
        cls.layers = {}
        cls.pools = {}
        cls.view = ()
        cls.shown = False
