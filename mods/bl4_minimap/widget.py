"""
The minimap on screen: a UMG widget we build in code (no assets of our own).

A round map picture (the scene capture's render target, drawn with a fully rounded Slate brush so
it comes out as a circle with a thin ring) with the player's arrow in the middle, in the top-right
corner. The arrow and markers are the game's own radar art; the map icons are the ECHO map's own.

The scene capture's render target holds a bigger north-up picture than the minimap shows. Each
frame that the view moved by a pixel, the part around the player is drawn, north up, into a small
display render target (one Canvas K2_DrawTexture with a UV window: a single textured quad on the
GPU), and the map image shows that whole, turned as a widget (render transform angle): the round
image itself (a turned circle is the same circle), or for the square minimap a plain image sqrt(2)
bigger turned inside a panel that clips under the frame line. (Slate's rounded-box brushes ignore
the brush's UV region, so windowing the big picture in the brush itself didn't move the map.)

Icons sit in layers, bottom to top: the route's dots, the map's icons, markers (enemies,
teammates, waypoint), then the player's arrow. Each layer keeps a pool of images per texture.
Markers out of range (objectives, waypoint, teammates, loot) go in the "edge" layer instead: centred
on the ring, half outside the map, so a far objective doesn't look like it's near the edge of the
range. That layer is on the root, over the frame (the square frame clips).
"""

from __future__ import annotations

from typing import Any

import unrealsdk
from unrealsdk.unreal import IGNORE_STRUCT, UObject, WeakPointer, WrappedStruct

from . import scene

Z_ORDER = -5  # under the game's menus
COLLAPSED, HIT_TEST_INVISIBLE = 1, 3
DRAW_IMAGE, DRAW_ROUNDED_BOX = 3, 4
ROUND_FIXED, ROUND_HALF_HEIGHT = 0, 1  # half the height: a circle when square
SQUARE_CORNER = 6.0
# The square map is clipped this far inside the frame: under the blue line (RING_WIDTH wide, corner
# radius SQUARE_CORNER), so the clipped map's sharp corners stay hidden under it
SQUARE_CLIP_INSET = 4.0
CLIP_TO_BOUNDS = 1
DISPLAY_SIZE = 512  # px of the display render target
RTF_RGBA8 = 2
BLEND_OPAQUE = 0
OVERSCAN = 2**0.5  # the square map image's size / the frame's (so its turned corners still cover it)
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
# Under the route layer: the route's dots well inside the rim, placed once in a fixed frame and
# moved and turned all together with the layer's render transform (set_turned)
TURNED_LAYER = "route_turned"
# Icon positions are rounded to this part of a screen pixel (at most a quarter pixel off, which
# can't be seen), so slow moves cost a call every few frames instead of every frame
PIXEL_STEP = 0.5

# An item: (texture path, base size px, tint, x, y, vertical), x/y in -1..1 of the minimap's
# half-size; vertical: 1 above you, -1 below you (a chevron), 0 neither
Item = tuple[str, float, tuple[float, float, float, float], float, float, int]


def _new(path: str, outer: Any) -> Any:
    return unrealsdk.construct_object(unrealsdk.find_object("Class", path), outer)


def _script_struct(path: str) -> Any:
    try:
        return unrealsdk.find_object("ScriptStruct", path)
    except ValueError:
        return None


# The engine's own struct types, found once: `make_struct` looks the type up by name on every call
# (12.9 us inside the frame tick, of which `WrappedStruct(type, X=, Y=)` is 5.7; game 25456,
# 2026-10-29). Native script structs are never unloaded, so holding them is safe. (Writing X/Y into
# a reused struct is slower still: 36 us for the two fields.)
_VEC2 = _script_struct("/Script/CoreUObject.Vector2D")
_LINEAR = _script_struct("/Script/CoreUObject.LinearColor")


def _vec2(x: float, y: float) -> Any:
    if _VEC2 is None:
        return unrealsdk.make_struct("Vector2D", X=float(x), Y=float(y))
    return WrappedStruct(_VEC2, X=float(x), Y=float(y))


def _color(rgba: tuple[float, float, float, float]) -> Any:
    if _LINEAR is None:
        return unrealsdk.make_struct("LinearColor", R=rgba[0], G=rgba[1], B=rgba[2], A=rgba[3])
    return WrappedStruct(_LINEAR, R=float(rgba[0]), G=float(rgba[1]), B=float(rgba[2]), A=float(rgba[3]))


def _place(widget: Any, anchor: tuple[float, float], align: tuple[float, float], pos: tuple[float, float],
           size: tuple[float, float]) -> None:  # fmt: skip
    slot = widget.Slot
    a = _vec2(*anchor)
    slot.SetAnchors(unrealsdk.make_struct("Anchors", Minimum=a, Maximum=a))
    slot.SetAlignment(_vec2(*align))
    slot.SetPosition(_vec2(*pos))
    slot.SetSize(_vec2(*size))


def _fill(widget: Any, inset: float = 0.0) -> None:
    widget.Slot.SetAnchors(unrealsdk.make_struct("Anchors", Minimum=_vec2(0, 0), Maximum=_vec2(1, 1)))
    widget.Slot.SetOffsets(unrealsdk.make_struct("Margin", Left=inset, Top=inset, Right=inset, Bottom=inset))


class _Pooled:
    """One image in a layer's pool, with what it last showed (to skip unchanged updates)."""

    __slots__ = ("angle", "base", "image", "pos", "set_pos", "shown", "size", "tint")

    def __init__(self, image: Any, base: float) -> None:
        self.image = image
        # Its slot's SetPosition, looked up once: `image.Slot.SetPosition` is a property read and a
        # function lookup on top of the call (23 us vs 3.4 for the call alone inside the frame tick)
        self.set_pos = image.Slot.SetPosition
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
    north_box: Any = None  # the panel holding the disc and the letter
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
    view: tuple[float, ...] = ()  # (u, v, cover) drawn into the display target
    margin = (0.0, 0.0)
    step = 0.1  # UI units icon positions are rounded to (PIXEL_STEP of a screen pixel, from show)
    turn_layers = True  # False: the route's dots are placed one by one like other icons (A/B tests)
    turned: tuple[float, float, float] = (0.0, 0.0, 0.0)  # the turned layer's (angle, x, y) shown
    turned_items: tuple[Any, ...] = ()  # (icon scale, items) the turned layer was filled with
    image_angle = 0.0  # the round map image's own turn
    builds = 0  # widgets built so far (a new one has empty pools: everything must be placed again)
    north_set: tuple[Any, ...] = ()  # the north badge box's slot SetPosition
    krl: Any = None  # KismetRenderingLibrary's default object
    # set_view's draw arguments that don't change from one frame to the next
    draw_args: dict[str, Any] = {}  # noqa: RUF012
    pools: dict[tuple[str, str], list[_Pooled]] = {}  # noqa: RUF012  # (layer, texture) -> images
    used_by: dict[str, dict[str, int]] = {}  # noqa: RUF012  # layer -> images per texture its last set_layer showed
    # No cache of texture pointers: see _texture
    missing: set[str] = set()  # noqa: RUF012  # textures that didn't load
    loaded: set[str] = set()  # noqa: RUF012  # and those that did once (the art exists)

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

        # The map turns as a widget (set_view): round, the round image itself (a turned circle is the
        # same circle, ring and all); square, a plain image OVERSCAN bigger, turned inside a panel
        # that clips just inside the blue line, under a separate (unturned) frame line
        image = _new("/Script/UMG.Image", tree)
        if square:
            clip = _new("/Script/UMG.CanvasPanel", tree)
            frame.AddChild(clip)
            _fill(clip, SQUARE_CLIP_INSET)
            clip.SetClipping(CLIP_TO_BOUNDS)
            clip.AddChild(image)
            frame.SetClipping(CLIP_TO_BOUNDS)  # icons at the edge
        else:
            frame.AddChild(image)
        krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject
        black = unrealsdk.make_struct("LinearColor", R=0, G=0, B=0, A=1)
        display = krl.CreateRenderTarget2D(pc, DISPLAY_SIZE, DISPLAY_SIZE, RTF_RGBA8, black, False, False)
        brush = unrealsdk.make_struct("SlateBrush")
        brush.ResourceObject = display
        brush.ImageSize.X = brush.ImageSize.Y = DISPLAY_SIZE
        brush.TintColor.SpecifiedColor = _color((1, 1, 1, 1))
        if square:
            brush.DrawAs = DRAW_IMAGE
        else:
            brush.DrawAs = DRAW_ROUNDED_BOX
            brush.OutlineSettings.RoundingType = ROUND_HALF_HEIGHT
            brush.OutlineSettings.Width = RING_WIDTH
            brush.OutlineSettings.Color.SpecifiedColor = _color(RING_COLOR)
        image.SetBrush(brush)  # the image holds the display target from here on
        if square:
            _place(image, (0.5, 0.5), (0.5, 0.5), (0, 0), (DISPLAY_SIZE, DISPLAY_SIZE))  # sized in show
            line = _new("/Script/UMG.Image", tree)
            frame.AddChild(line)
            line_brush = unrealsdk.make_struct("SlateBrush")
            line_brush.TintColor.SpecifiedColor = _color((0, 0, 0, 0))  # no fill: only the line
            line_brush.DrawAs = DRAW_ROUNDED_BOX
            line_brush.OutlineSettings.RoundingType = ROUND_FIXED
            corners = line_brush.OutlineSettings.CornerRadii
            corners.X = corners.Y = corners.Z = corners.W = SQUARE_CORNER
            line_brush.OutlineSettings.Width = RING_WIDTH
            line_brush.OutlineSettings.Color.SpecifiedColor = _color(RING_COLOR)
            line.SetBrush(line_brush)
            _fill(line)
        else:
            _fill(image)
        image.SetRenderTransformPivot(_vec2(0.5, 0.5))  # turns about its middle (set_view)
        cls.display = WeakPointer(display)

        layers = {}
        for name in (TURNED_LAYER, *LAYERS):
            layer = _new("/Script/UMG.CanvasPanel", tree)
            frame.AddChild(layer)
            _fill(layer)
            layer.SetVisibility(HIT_TEST_INVISIBLE)
            layers[name] = layer
        # Turns about the frame's middle (the player)
        layers[TURNED_LAYER].SetRenderTransformPivot(_vec2(0.5, 0.5))

        arrow = _new("/Script/UMG.Image", tree)
        frame.AddChild(arrow)
        texture = cls._texture(ARROW_TEXTURE)
        if texture is not None:
            arrow.SetBrushFromTexture(texture, False)
        _place(arrow, (0.5, 0.5), (0.5, 0.5), (0, 0), (ARROW_SIZE, ARROW_SIZE))
        arrow.SetRenderTransformPivot(_vec2(0.5, 0.5))

        # On the root, not the frame: the square frame clips, and the badge sits across its edge.
        # Disc and letter in one box, so moving the badge is one call
        north_box = _new("/Script/UMG.CanvasPanel", tree)
        root.AddChild(north_box)
        _place(north_box, (1, 0), (0.5, 0.5), (0, 0), (NORTH_SIZE, NORTH_SIZE))
        north = _new("/Script/UMG.Image", tree)
        north_box.AddChild(north)
        north_brush = unrealsdk.make_struct("SlateBrush")
        north_brush.TintColor.SpecifiedColor = _color(NORTH_FILL)
        north_brush.DrawAs = DRAW_ROUNDED_BOX
        north_brush.OutlineSettings.RoundingType = ROUND_HALF_HEIGHT
        north_brush.OutlineSettings.Width = NORTH_OUTLINE
        north_brush.OutlineSettings.Color.SpecifiedColor = _color(RING_COLOR)
        north.SetBrush(north_brush)
        _fill(north)
        letter = _new("/Script/UMG.TextBlock", tree)
        north_box.AddChild(letter)
        letter.SetText("N")
        letter.SetJustification(JUSTIFY_CENTER)
        letter.SetColorAndOpacity(unrealsdk.make_struct("SlateColor", SpecifiedColor=_color(NORTH_TEXT_COLOR)))
        _place(letter, (0.5, 0.5), (0.5, 0.5), (0, 0), (NORTH_SIZE, NORTH_SIZE))
        letter.Slot.SetAutoSize(True)
        north_box.SetVisibility(COLLAPSED)

        # Also on the root: its icons sit across the ring, and the square frame would clip them
        edge = _new("/Script/UMG.CanvasPanel", tree)
        root.AddChild(edge)
        edge.SetVisibility(HIT_TEST_INVISIBLE)
        layers[EDGE_LAYER] = edge

        widget.SetVisibility(HIT_TEST_INVISIBLE)
        widget.AddToViewport(Z_ORDER)
        cls.frame, cls.rim, cls.map_image, cls.arrow, cls.layers = frame, rim, image, arrow, layers
        cls.north, cls.letter = north, letter
        cls.north_box = north_box
        cls.builds += 1
        cls.north_set = (north_box.Slot.SetPosition,)
        cls.square = square
        cls.size = 0.0
        cls.icon_scale = 1.0
        cls.arrow_angle = 0.0
        cls.view = ()
        cls.turned, cls.turned_items = (0.0, 0.0, 0.0), ()
        cls.image_angle = 0.0
        cls.north_pos = None
        cls.north_scale = 0.0
        cls.pools = {}
        return widget

    @classmethod
    def _texture(cls, path: str) -> Any:
        """
        The texture, loaded by path each time: put it in a brush in the same call. A texture kept
        between calls (it was a WeakPointer) could be one the GC had already marked for
        destruction (a texture only checked with has_texture, or held only by a rebuilt widget's
        images): given to a brush, it was destroyed under it and Slate crashed painting the image
        (2026-10-29, SImage::OnPaint, a Texture2D's InternalIndex -1). Loading finds only live
        objects. Only new pool images and the arrow ask (existence checks use `loaded`).
        """
        if path in cls.missing:
            return None
        texture = scene._load(path)  # noqa: SLF001
        if texture is None:
            cls.missing.add(path)  # (a DLC's art that isn't installed): don't look again
            return None
        cls.loaded.add(path)
        return texture

    @classmethod
    def has_texture(cls, path: str) -> bool:
        """Whether this art exists (a DLC's may not). Cheap once it loaded: no load."""
        return path in cls.loaded or cls._texture(path) is not None

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
        cls._update(cls._pooled(layer, path, index, base), size, tint, pos, angle)

    @staticmethod
    def _update(entry: _Pooled, size: tuple[float, float], tint: tuple[float, ...], pos: tuple[float, float],
                angle: float = 0.0) -> None:  # fmt: skip
        if size != entry.size:
            entry.image.Slot.SetSize(_vec2(*size))
            entry.size = size
        if tint != entry.tint:
            entry.image.SetColorAndOpacity(_color(tint))
            entry.tint = tint
        if pos != entry.pos:
            entry.set_pos(_vec2(*pos))
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
        step = cls.step
        used: dict[str, int] = {}
        chevrons: list[tuple[tuple[float, float], int]] = []
        for path, base, tint, x, y, vertical in items:
            if path not in cls.loaded and cls._texture(path) is None:
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
            pos = (round(px / step) * step, round(py / step) * step)
            cls._put(layer, path, i, base, (size, size), tint, pos)
            if vertical:
                # Above: over the icon, pointing up. Below: under it, pointing down.
                gap = size / 2 + CHEVRON_SIZE * CHEVRON_ASPECT * cls.icon_scale / 2
                chevrons.append(((pos[0], round((pos[1] - vertical * gap) / step) * step), vertical))
        for i, (pos, vertical) in enumerate(chevrons):
            size = CHEVRON_SIZE * cls.icon_scale
            angle = 180.0 if vertical > 0 else 0.0
            cls._put(layer, CHEVRON_TEXTURE, i, CHEVRON_SIZE, (size, size * CHEVRON_ASPECT), (1, 1, 1, 1), pos, angle)
        used[CHEVRON_TEXTURE] = len(chevrons)
        # Hide what the last call for this layer showed and this one doesn't. (It walked every pool
        # of every layer: 53 pools, 4 calls a frame, game 22832, 2026-10-30.) Only set_layer shows
        # images in these layers, so the last call's counts say what's shown.
        before = cls.used_by.get(layer)
        cls.used_by[layer] = used
        if before is None:
            stale = [(path, used.get(path, 0)) for pool_layer, path in cls.pools if pool_layer == layer]
        else:
            stale = [(path, used.get(path, 0)) for path, n in before.items() if used.get(path, 0) < n]
        for path, keep in stale:
            for entry in cls.pools.get((layer, path), ())[keep:]:
                if entry.shown:
                    entry.image.SetVisibility(COLLAPSED)
                    entry.shown = False

    @classmethod
    def set_turned(cls, layer: str, items: list[Item], fixed: list[tuple[float, float]], angle: float,
                   offset: tuple[float, float]) -> None:  # fmt: skip
        """
        Like set_layer, for icons that look the same at any angle (the route's round dots), with
        each item's position also given in a fixed frame (`fixed`: -1..1 of the half-size, like the
        items' own). Items well inside the rim go into the turned layer at their fixed position, and
        the layer as a whole is turned by `angle` (degrees clockwise) about the middle and moved by
        `offset` (-1..1 of the half-size): two calls a frame however many dots there are, instead of
        one per dot each frame the view moves or turns. The rest (near the rim, where set_layer pulls
        them in, or the square's corners) go into `layer` one by one.
        """
        if cls.widget() is None:
            return
        if not cls.turn_layers:
            cls._hide_layer(TURNED_LAYER)
            cls.turned_items = ()
            cls.set_layer(layer, items)
            return
        half = cls.size / 2
        inside: list[tuple[str, float, tuple[float, ...], tuple[float, float]]] = []
        rest: list[Item] = []
        for item, (fx, fy) in zip(items, fixed):
            path, base, tint, x, y, vertical = item
            # Not where set_layer would pull it in to fit (the same edge), with no chevron
            edge = half - base * cls.icon_scale / 2 - RING_WIDTH
            px, py = x * half, y * half
            if vertical or edge <= 0 or px * px + py * py > edge * edge:
                rest.append(item)
            elif path in cls.loaded or cls._texture(path) is not None:
                # Fixed positions only change when the frame does: no rounding to pixels needed,
                # just enough that the same dot lands on the same value each time
                inside.append((path, base, tint, (round(fx * half, 2), round(fy * half, 2))))
        cls.set_layer(layer, rest)
        key = (cls.icon_scale, inside)
        if key != cls.turned_items:
            cls._fill_turned(inside)
            cls.turned_items = key
        turned = (round(angle, 3), round(offset[0] * half, 3), round(offset[1] * half, 3))
        if turned != cls.turned:
            layer_widget = cls.layers[TURNED_LAYER]
            if turned[0] != cls.turned[0]:
                layer_widget.SetRenderTransformAngle(turned[0])
            if turned[1:] != cls.turned[1:]:
                layer_widget.SetRenderTranslation(_vec2(turned[1], turned[2]))
            cls.turned = turned

    @classmethod
    def _fill_turned(cls, inside: list[tuple[str, float, tuple[float, ...], tuple[float, float]]]) -> None:
        """Puts these (path, base, tint, fixed position in UI units) in the turned layer."""
        # The same dot keeps its image (dots come and go at the ends of the route as you walk; by
        # index every dot after the first would move)
        by_path: dict[str, list[tuple[str, float, tuple[float, ...], tuple[float, float]]]] = {}
        for entry in inside:
            by_path.setdefault(entry[0], []).append(entry)
        for (pool_layer, path), pool in list(cls.pools.items()):
            if pool_layer == TURNED_LAYER and path not in by_path:
                cls._hide_pool(pool)
        for path, wanted in by_path.items():
            cls._pooled(TURNED_LAYER, path, len(wanted) - 1, wanted[0][1])
            pool = cls.pools[(TURNED_LAYER, path)]
            at: dict[tuple[float, float], _Pooled] = {}
            for entry in pool:
                if entry.shown:
                    at.setdefault(entry.pos, entry)
            kept: set[int] = set()
            todo = []
            for want in wanted:
                entry = at.pop(want[3], None)
                if entry is None:
                    todo.append(want)
                else:
                    kept.add(id(entry))
                    size = want[1] * cls.icon_scale
                    cls._update(entry, (size, size), want[2], want[3])
            # Shown ones first: moving one is a call, showing a hidden one two (and hiding the other)
            free = [e for e in pool if e.shown and id(e) not in kept] + [e for e in pool if not e.shown]
            for entry, (_, base, tint, pos) in zip(free, todo):
                size = base * cls.icon_scale
                cls._update(entry, (size, size), tint, pos)
                if not entry.shown:
                    entry.image.SetVisibility(HIT_TEST_INVISIBLE)
                    entry.shown = True
            cls._hide_pool(free[len(todo):])

    @staticmethod
    def _hide_pool(entries: list[_Pooled]) -> None:
        for entry in entries:
            if entry.shown:
                entry.image.SetVisibility(COLLAPSED)
                entry.shown = False

    @classmethod
    def _hide_layer(cls, layer: str) -> None:
        for (pool_layer, _), pool in cls.pools.items():
            if pool_layer == layer:
                cls._hide_pool(pool)

    @classmethod
    def show(cls, pc: UObject, visible: bool, size: float = 0.0, margin: tuple[float, float] = (0, 0),
             arrow_angle: float = 0.0, icon_scale: float = 1.0, *, square: bool = False,
             pixel: float = 0.0) -> None:  # fmt: skip
        """`size`/`margin` in UI units; `pixel`: UI units per screen pixel (0: unknown)."""
        if pixel > 0:
            cls.step = PIXEL_STEP * pixel
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
            if cls.square:
                cls.map_image.Slot.SetSize(_vec2(size * OVERSCAN, size * OVERSCAN))
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
            step = cls.step
            pos = (round((-cls.margin[0] - half + where[0] * reach) / step) * step,
                   round((cls.margin[1] + half + where[1] * reach) / step) * step)  # fmt: skip
        if pos == cls.north_pos:
            return
        if (pos is None) != (cls.north_pos is None):
            cls.north_box.SetVisibility(COLLAPSED if pos is None else HIT_TEST_INVISIBLE)
        cls.north_pos = pos
        if pos is None:
            return
        if cls.icon_scale != cls.north_scale:
            cls.north_scale = cls.icon_scale
            size = NORTH_SIZE * cls.icon_scale
            cls.north_box.Slot.SetSize(_vec2(size, size))
            font = cls._font()
            if font is not None:
                cls.letter.SetFont(unrealsdk.make_struct(
                    "SlateFontInfo", FontObject=font, TypefaceFontName=NORTH_TYPEFACE,
                    Size=round(NORTH_TEXT_SIZE * cls.icon_scale),
                ))  # fmt: skip
        where = _vec2(*pos)
        for set_pos in cls.north_set:
            set_pos(where)

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
        turned by `angle` degrees clockwise.

        The window is drawn north-up into the display target, and the map image is turned as a
        widget (one cheap call a frame): a Canvas redraw costs ~150-200 us a frame inside the frame
        tick (Begin 70, K2_DrawTexture 50, End 16; game 25456, 2026-10-29), so it now runs only
        when the window moved by a pixel of the display target (or the picture changed: `force`),
        not each frame you turn.
        """
        turn = round(angle, 2)
        # The display target covers the round image, or the square one OVERSCAN times the frame
        cover = half * (OVERSCAN if cls.square else 1.0)
        # One display pixel (~0.6-0.8 screen px) in picture UV: the window moves in whole pixels
        step = 2 * cover / DISPLAY_SIZE
        if step > 0:
            u, v = round(u / step) * step, round(v / step) * step
        view = (round(u, 6), round(v, 6), round(cover, 6))
        if view == cls.view and turn == cls.image_angle and not force:
            return  # (before any pointer check: this is most frames)
        display, pc = cls.display(), cls.owner()
        picture = scene.Scene.target()
        if display is None or pc is None or picture is None or cls.map_image is None:
            return
        if turn != cls.image_angle:
            cls.map_image.SetRenderTransformAngle(turn)
            cls.image_angle = turn
        if view == cls.view and not force:
            return
        cls.view = view
        if cls.krl is None:
            cls.krl = unrealsdk.find_class("KismetRenderingLibrary").ClassDefaultObject  # (never unloaded)
        krl = cls.krl
        _, canvas, _, ctx = krl.BeginDrawCanvasToRenderTarget(pc, display, None, IGNORE_STRUCT, IGNORE_STRUCT)
        # Structs that stay the same are made once (each costs ~6 us inside the frame tick)
        args = cls.draw_args
        if not args:
            args.update(pos=_vec2(0, 0), size=_vec2(DISPLAY_SIZE, DISPLAY_SIZE), tint=_color((1, 1, 1, 1)),
                        pivot=_vec2(0.5, 0.5))  # fmt: skip
        if args.get("cover") != cover:
            args["cover"], args["uv_size"] = cover, _vec2(2 * cover, 2 * cover)
        canvas.K2_DrawTexture(
            picture, args["pos"], args["size"], _vec2(u - cover, v - cover), args["uv_size"], args["tint"],
            BLEND_OPAQUE, 0.0, args["pivot"],
        )  # fmt: skip
        krl.EndDrawCanvasToRenderTarget(pc, ctx)

    @classmethod
    def remove(cls) -> None:
        widget = cls.widget()
        if widget is not None:
            widget.RemoveFromParent()
        cls.widget = WeakPointer()
        cls.owner = WeakPointer()
        cls.frame = cls.rim = cls.map_image = cls.arrow = cls.north = cls.north_box = cls.letter = None
        cls.north_pos = None
        cls.north_set = ()
        cls.display = WeakPointer()
        cls.layers = {}
        cls.pools = {}
        cls.used_by = {}
        cls.view = ()
        cls.turned, cls.turned_items = (0.0, 0.0, 0.0), ()
        cls.image_angle = 0.0
        cls.shown = False
