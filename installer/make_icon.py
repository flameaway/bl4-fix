"""
Draws the BL4 Direct icon in a Borderlands-inspired style (original artwork, no game logos):
cel-shaded shapes with thick black ink outlines, comic hatching in the shadows, a grungy
cut-corner panel like BL4's menus, and the yellow-to-orange of the BL4 title.

Motif: an ECHO communicator broadcasting signal arcs = connecting with friends.

    installer/.venv/Scripts/python installer/make_icon.py   ->  installer/icon.ico (+ icon.png)
"""

from __future__ import annotations

import random
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter

HERE = Path(__file__).resolve().parent
S = 1024  # draw big, downsample for smooth edges
INK = (12, 10, 8, 255)
YELLOW_HI = (255, 236, 92)
YELLOW = (255, 196, 30)
ORANGE = (236, 112, 10)
RUST = (150, 52, 8)
TEAL_RIM = (40, 210, 222, 255)
PANEL_TOP = (24, 58, 64)
PANEL_BOTTOM = (8, 20, 24)

rng = random.Random(4)  # fixed seed: same grunge every build


def mask_of(draw_fn) -> Image.Image:  # noqa: ANN001
    m = Image.new("L", (S, S), 0)
    draw_fn(ImageDraw.Draw(m))
    return m


def dilate(mask: Image.Image, px: int) -> Image.Image:
    # MaxFilter needs odd sizes; apply in steps to keep it fast at 1024px
    out = mask
    while px > 0:
        step = min(px, 12)
        out = out.filter(ImageFilter.MaxFilter(step * 2 + 1))
        px -= step
    return out


def vertical_gradient(top: tuple[int, ...], mid: tuple[int, ...], bottom: tuple[int, ...],
                      y0: int, y1: int) -> Image.Image:
    img = Image.new("RGBA", (S, S))
    d = ImageDraw.Draw(img)
    for y in range(S):
        t = min(1.0, max(0.0, (y - y0) / max(1, y1 - y0)))
        a, b, u = (top, mid, t * 2) if t < 0.5 else (mid, bottom, (t - 0.5) * 2)
        d.line([(0, y), (S, y)], fill=tuple(int(p + (q - p) * u) for p, q in zip(a, b)) + (255,))
    return img


def hatching(spacing: int, width: int, color: tuple[int, ...]) -> Image.Image:
    """Diagonal comic hatching lines, slightly irregular like hand inking."""
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    for k in range(-S, 2 * S, spacing):
        jitter = rng.randint(-4, 4)
        d.line([(k + jitter, S), (k + S + jitter, 0)], fill=color, width=width + rng.randint(-2, 2))
    return img


def paint(canvas: Image.Image, mask: Image.Image, fill: Image.Image | tuple[int, ...]) -> None:
    layer = fill if isinstance(fill, Image.Image) else Image.new("RGBA", (S, S), fill)
    canvas.paste(layer, (0, 0), mask)


def panel(canvas: Image.Image) -> None:
    """Dark teal cut-corner panel with a black ink border, teal rim and grunge."""
    pad, cut = 30, 150
    pts = [(pad + cut, pad), (S - pad, pad), (S - pad, S - pad - cut),
           (S - pad - cut, S - pad), (pad, S - pad), (pad, pad + cut)]
    outer = mask_of(lambda d: d.polygon(pts, fill=255))
    paint(canvas, outer, INK)
    inner = outer.filter(ImageFilter.MinFilter(31))
    paint(canvas, inner, vertical_gradient(PANEL_TOP, (14, 36, 42), PANEL_BOTTOM, pad, S - pad))

    # teal rim just inside the ink border, like BL4's menu panels
    rim = ImageChops.subtract(inner, inner.filter(ImageFilter.MinFilter(17)))
    paint(canvas, rim, TEAL_RIM)

    # grunge: speckles and scratches, clipped to the panel
    grunge = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    g = ImageDraw.Draw(grunge)
    for _ in range(900):
        x, y, r = rng.randint(0, S), rng.randint(0, S), rng.randint(1, 4)
        shade = rng.choice([(0, 0, 0, 90), (120, 200, 210, 35)])
        g.ellipse((x - r, y - r, x + r, y + r), fill=shade)
    for _ in range(40):
        x, y = rng.randint(0, S), rng.randint(0, S)
        g.line([(x, y), (x + rng.randint(-120, 120), y + rng.randint(-40, 40))],
               fill=(150, 220, 230, 28), width=rng.randint(1, 3))
    clipped = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    clipped.paste(grunge, (0, 0), ImageChops.multiply(grunge.getchannel("A"), inner))
    canvas.alpha_composite(clipped)

    # hatching in the lower-right corner shadow
    corner = mask_of(lambda d: d.polygon([(S, S * 0.45), (S, S), (S * 0.45, S)], fill=255))
    shade = hatching(26, 6, (0, 0, 0, 120))
    clip = ImageChops.multiply(ImageChops.multiply(shade.getchannel("A"), corner), inner)
    canvas.paste(shade, (0, 0), clip)


def cel_shape(canvas: Image.Image, shape: Image.Image, outline: int) -> None:
    """Yellow-orange cel-shaded fill, hatched shadow side, highlight, thick black ink outline."""
    paint(canvas, dilate(shape, outline), INK)
    paint(canvas, shape, vertical_gradient(YELLOW_HI, YELLOW, ORANGE, 180, 860))

    # cel shadow: the part of the shape not covered when shifted up-left, in rust with hatching
    shifted = ImageChops.offset(shape, -34, -34)
    shadow = ImageChops.subtract(shape, shifted)
    paint(canvas, shadow, RUST + (255,))
    hatch = hatching(22, 7, INK[:3] + (200,))
    body_shadow = ImageChops.subtract(shape, ImageChops.offset(shape, -70, -70))
    paint(canvas, ImageChops.multiply(hatch.getchannel("A"), body_shadow), INK)

    # highlight on the lit top-left edge
    lit = ImageChops.subtract(shape, ImageChops.offset(shape, 18, 18))
    paint(canvas, lit, YELLOW_HI + (255,))


SCREEN_DARK = (6, 26, 30, 255)
WAVE = (90, 245, 255, 255)


def echo_device(canvas: Image.Image) -> None:
    """
    A chunky handheld ECHO communicator (the Vault Hunters' comms device) broadcasting signal
    arcs: cel-shaded yellow body, dark screen with a glowing waveform, speaker grille, antenna.
    Drawn upright on its own layer, then tilted for energy.
    """
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    bx0, by0, bx1, by1 = 250, 300, 690, 900  # device body
    ant_x = bx1 - 95

    def body(d: ImageDraw.ImageDraw) -> None:
        d.rounded_rectangle((bx0, by0, bx1, by1), 70, fill=255)
        # antenna stub with a ball tip
        d.rounded_rectangle((ant_x - 26, by0 - 150, ant_x + 26, by0 + 20), 20, fill=255)
        d.ellipse((ant_x - 52, by0 - 210, ant_x + 52, by0 - 106), fill=255)

    shape = mask_of(body)

    # Signal arcs from the antenna tip, upper right; inked like the body
    tip = (ant_x, by0 - 158)

    def arcs(d: ImageDraw.ImageDraw) -> None:
        for radius, width in ((150, 46), (250, 46), (350, 46)):
            box = (tip[0] - radius, tip[1] - radius, tip[0] + radius, tip[1] + radius)
            d.arc(box, -70, 5, fill=255, width=width)

    arc_mask = mask_of(arcs)
    paint(layer, dilate(arc_mask, 22), INK)
    paint(layer, arc_mask, vertical_gradient(YELLOW_HI, YELLOW, ORANGE, 0, 500))

    cel_shape(layer, shape, outline=30)

    d = ImageDraw.Draw(layer)
    # Screen: inked bezel, dark glass, glowing waveform
    sx0, sy0, sx1, sy1 = bx0 + 55, by0 + 70, bx1 - 55, by0 + 360
    d.rounded_rectangle((sx0 - 16, sy0 - 16, sx1 + 16, sy1 + 16), 36, fill=INK)
    d.rounded_rectangle((sx0, sy0, sx1, sy1), 26, fill=SCREEN_DARK)
    screen = mask_of(lambda m: m.rounded_rectangle((sx0, sy0, sx1, sy1), 26, fill=255))

    mid = (sy0 + sy1) // 2
    amps = [0.05, 0.2, 0.55, 0.15, 0.95, 0.35, 0.7, 0.1, 0.45, 0.85, 0.25, 0.6, 0.12, 0.05]
    xs = [sx0 + 18 + i * (sx1 - sx0 - 36) / (len(amps) - 1) for i in range(len(amps))]
    half = (sy1 - sy0) / 2 - 26
    points = []
    for i, (x, a) in enumerate(zip(xs, amps)):
        points.append((x, mid + (-1 if i % 2 else 1) * a * half))
    glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(glow).line(points, fill=WAVE[:3] + (200,), width=34, joint="curve")
    glow = glow.filter(ImageFilter.GaussianBlur(16))
    sharp = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    ImageDraw.Draw(sharp).line(points, fill=WAVE, width=16, joint="curve")
    for wave_layer in (glow, sharp):
        clipped = Image.new("RGBA", (S, S), (0, 0, 0, 0))
        clipped.paste(wave_layer, (0, 0), ImageChops.multiply(wave_layer.getchannel("A"), screen))
        layer.alpha_composite(clipped)
    # glass glint
    d.line([(sx0 + 30, sy0 + 60), (sx0 + 90, sy0 + 20)], fill=(255, 255, 255, 110), width=10)

    # Speaker grille and a button below the screen
    gy = sy1 + 70
    for k in range(4):
        y = gy + k * 40
        d.rounded_rectangle((bx0 + 70, y, bx0 + 260, y + 16), 8, fill=INK)
    cxb, cyb = bx1 - 120, gy + 62
    d.ellipse((cxb - 62, cyb - 62, cxb + 62, cyb + 62), fill=INK)
    d.ellipse((cxb - 44, cyb - 44, cxb + 44, cyb + 44), fill=(220, 60, 20, 255))
    d.arc((cxb - 44, cyb - 44, cxb + 44, cyb + 44), 200, 280, fill=(255, 170, 120, 255), width=8)

    tilted = layer.rotate(-12, resample=Image.BICUBIC, center=(S // 2, S // 2 + 40), translate=(-10, 20))
    canvas.alpha_composite(tilted)


def draw_icon() -> Image.Image:
    canvas = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    panel(canvas)
    echo_device(canvas)
    return canvas


def main() -> None:
    icon = draw_icon()
    big = icon.resize((256, 256), Image.LANCZOS)
    big.save(HERE / "icon.png")
    big.save(HERE / "icon.ico", sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])

    sizes = (256, 128, 64, 48, 32, 24, 16)
    strip = Image.new("RGBA", (sum(sizes) + 20 * len(sizes) + 20, 296), (235, 238, 240, 255))
    x = 20
    for s in sizes:
        strip.alpha_composite(icon.resize((s, s), Image.LANCZOS), (x, 20 + (256 - s) // 2))
        x += s + 20
    (HERE / "build").mkdir(exist_ok=True)
    strip.save(HERE / "build" / "icon_preview.png")
    print("wrote", HERE / "icon.ico")


if __name__ == "__main__":
    main()
