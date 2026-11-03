"""
Makes the setup app's raster art, all of it original (no game files):

    installer/.venv/Scripts/python tools/gen_setup_art.py [--bg] [--icon]

- installer/ui/art/bg.jpg: the window's backdrop, a dark title-screen scene (a gold horizon and a
  grid floor), drawn with numpy + Pillow from a fixed seed, so the same picture comes out every time.
- installer/icon.png + installer/icon.ico: the BL4 Fix mark (installer/ui/art/mark_ring.svg +
  mark_core.svg) rendered by headless Microsoft Edge (Windows' own browser), for the exe, its
  windows and the Start menu shortcut.

The outputs are committed, so building the exe (installer/build.py) needs neither this script
nor Edge. Run it again after changing the SVGs or the backdrop's look. With no flag it makes both.
"""

from __future__ import annotations

import argparse
import functools
import http.server
import io
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
UI = REPO / "installer" / "ui"
ART = UI / "art"
EDGE = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]


def _noise(rng, shape: tuple[int, int], cells: int):  # noqa: ANN001, ANN202
    """Smooth value noise: a random grid of `cells` across, upscaled bicubically."""
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    h, w = shape
    grid = rng.random((max(2, cells * h // w), cells)).astype("float32")
    im = Image.fromarray(grid, mode="F").resize((w, h), Image.BICUBIC)
    return np.asarray(im)


def _fbm(rng, shape: tuple[int, int], base: int, octaves: int):  # noqa: ANN001, ANN202
    total = 0
    amp, norm = 1.0, 0.0
    for i in range(octaves):
        total = total + amp * _noise(rng, shape, base * 2**i)
        norm += amp
        amp *= 0.5
    return total / norm


def make_bg(out: Path, w: int = 1920, h: int = 1080) -> None:
    """
    A dark title-screen scene: deep navy with a little haze, a gold glow along a horizon, and a
    perspective grid floor running toward it (fading with distance and toward the menu side).
    """
    import numpy as np  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    rng = np.random.default_rng(4)
    y, x = np.mgrid[0:h, 0:w].astype("float32")
    u, v = x / w, y / h
    horizon, vanish = 0.64, 0.66  # the horizon's height, the vanishing point's x

    img = np.empty((h, w, 3), "float32")
    # Sky: navy at the top, darker toward the horizon; a soft low haze
    sky = np.clip(v / horizon, 0, 1)[..., None]
    img[:] = np.array([4, 20, 32], "float32") * (1 - sky) + np.array([2, 10, 18], "float32") * sky
    haze = _fbm(rng, (h, w), 5, 5)
    img += np.array([10, 38, 52], "float32") * (np.clip((haze - 0.42) * 1.8, 0, 1) * 0.6)[..., None]

    # Gold glow along the horizon, brightest behind the vanishing point
    across = np.exp(-(((u - vanish) / 0.38) ** 2))
    band = np.exp(-(((v - horizon) / 0.012) ** 2)) * (0.15 + 0.85 * across)
    bloom = np.exp(-(((v - horizon) / 0.16) ** 2)) * across
    img += np.array([230, 140, 40], "float32") * (band * 0.45)[..., None]
    img += np.array([120, 60, 14], "float32") * (bloom * 0.28)[..., None]

    # Floor grid below the horizon: depth = 1 / (distance below the horizon)
    below = v - horizon
    floor = below > 0.002
    z = np.where(floor, 0.08 / np.maximum(below, 0.002), 0)
    gx = (u - vanish) * z * 70.0  # lines running toward the vanishing point
    gz = z * 9.0  # lines across
    def lines(t, px):  # noqa: ANN001, ANN202
        d = np.abs(t - np.round(t))
        return np.clip(1 - d / np.maximum(px, 1e-4), 0, 1)
    # Line width in grid units: about one pixel at each depth (finite differences of the coordinate)
    px_x = np.abs(np.gradient(gx, axis=1)) * 1.2 + 1e-4
    px_z = np.abs(np.gradient(gz, axis=0)) * 1.2 + 1e-4
    grid = np.maximum(lines(gx, px_x), lines(gz, px_z))
    fade = np.clip(below / 0.36, 0, 1) ** 0.8 * np.clip(u * 1.6 - 0.15, 0, 1)
    grid = np.where(floor, grid * fade, 0)
    # Too fine to draw near the horizon: fade it out where lines crowd together
    grid *= np.clip(1 - (px_x + px_z) * 1.5, 0, 1)
    img += np.array([30, 140, 175], "float32") * (grid * 0.45)[..., None]
    # The floor catches the horizon glow
    img += np.array([90, 45, 12], "float32") * (np.where(floor, np.exp(-below / 0.1), 0) * across * 0.4)[..., None]

    # Darker toward the left (the menu) and the corners
    img *= (0.5 + 0.5 * np.clip(u * 1.5, 0, 1))[..., None]
    vignette = 1 - 0.35 * (((u - 0.6) / 0.7) ** 2 + ((v - 0.5) / 0.8) ** 2)
    img *= np.clip(vignette, 0.4, 1)[..., None]

    # Fine grain against banding in the dark gradients
    img += rng.normal(0, 1.4, (h, w, 1)).astype("float32")
    out.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.clip(img, 0, 255).astype("uint8")).save(out, quality=88, optimize=True, progressive=True)
    print(f"wrote {out} ({out.stat().st_size // 1024} KB)")


def _edge() -> Path:
    for path in EDGE:
        if path.is_file():
            return path
    raise SystemExit("Microsoft Edge not found: it renders the icon from the SVGs")


def make_icon(png_out: Path, ico_out: Path) -> None:
    """The mark rendered at 512 px with a transparent background, then sized down for the .ico."""
    from PIL import Image  # noqa: PLC0415

    page = (
        '<!DOCTYPE html><html><body style="margin:0;background:transparent">'
        '<div style="position:relative;width:512px;height:512px">'
        '<img src="art/mark_core.svg" style="position:absolute;inset:0;width:100%;height:100%">'
        '<img src="art/mark_ring.svg" style="position:absolute;inset:0;width:100%;height:100%">'
        "</div></body></html>"
    )
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        shutil.copytree(ART, root / "art")
        (root / "icon.html").write_text(page, encoding="utf-8")

        class Quiet(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=str(root)))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        shot = root / "shot.png"
        try:
            subprocess.run(
                [str(_edge()), "--headless=new", "--disable-gpu", "--hide-scrollbars", "--window-size=512,512",
                 "--default-background-color=00000000", "--virtual-time-budget=2000",
                 f"--user-data-dir={root / 'profile'}", f"--screenshot={shot}",
                 f"http://127.0.0.1:{server.server_address[1]}/icon.html"],
                check=False, capture_output=True, timeout=120,
            )  # fmt: skip
        finally:
            server.shutdown()
        if not shot.is_file():
            raise SystemExit("Edge didn't write the icon screenshot")
        im = Image.open(io.BytesIO(shot.read_bytes())).convert("RGBA")
    im = im.crop(im.getchannel("A").getbbox())
    side = max(im.size)
    square = Image.new("RGBA", (side, side))
    square.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    big = square.resize((256, 256), Image.LANCZOS)
    big.save(png_out, optimize=True)
    big.save(ico_out, sizes=[(s, s) for s in (16, 20, 24, 32, 40, 48, 64, 128, 256)])
    print(f"wrote {png_out} + {ico_out}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bg", action="store_true", help="only the backdrop")
    parser.add_argument("--icon", action="store_true", help="only the icon")
    args = parser.parse_args()
    both = not (args.bg or args.icon)
    if args.bg or both:
        make_bg(ART / "bg.jpg")
    if args.icon or both:
        make_icon(REPO / "installer" / "icon.png", REPO / "installer" / "icon.ico")


if __name__ == "__main__":
    main()
