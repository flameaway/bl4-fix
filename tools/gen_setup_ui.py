"""
Copies the game UI files the setup app's page uses into installer/ui/game/ (dev only, gitignored).

    python tools/gen_setup_ui.py [--oodle <oo2core_*_win64.dll>] [--dump dev/re/ui_dump]

The page (installer/ui/) is built from the game's own menus: its stylesheets, fonts, component
templates (Options rows, toggle, list button, main menu button) and art (title screen, ECHO
portraits, Claptrap). This takes them from a dump of the game's paks (dev/re/ui_extract.py, run
first when --oodle is given), follows every url() the stylesheets reference, and rewrites the
game's coui://uiresources/ links into relative paths so the page loads them from game/.
installer/build.py bundles the folder into the exe.
"""

from __future__ import annotations

import argparse
import os
import posixpath
import re
import shutil
import subprocess
import sys
from pathlib import Path, PurePosixPath

REPO = Path(__file__).resolve().parents[1]
OUT = REPO / "installer" / "ui" / "game"
DUMP = REPO / "dev" / "re" / "ui_dump"

# Stylesheets the page links (game/<path>), in this order
CSS = [
    "_shared/css/gbx.css",
    "_shared/css/oakgame.css",
    "_shared/css/oak_markup.css",
    "_shared/css/oakscroll.css",
    "_shared/css/color_tint_filters.css",
    "_shared/css/shared_animations.css",
    "components/gbx_button/css/gbx_button.css",
    "components/list_button/css/list_button.css",
    "components/main_menu_button/css/main_menu_button.css",
    "components/pip/css/pip.css",
    "components/toggle/css/toggle.css",
    "components/stepper/css/stepper.css",
    "components/text_input/css/text_input.css",
    "components/category_divider/css/category_divider.css",
    "menu_main/css/menu_main.css",
    "menu_options/css/menu_options.css",
    "title_screen/css/title_screen.css",
    "echo_portrait/css/echo_portrait.css",
    "dialog_popup/css/dialog_popup.css",
    "celebratory_notifications/css/celebratory_notifications.css",
]
# Templates the page clones (fetched as text, links rewritten relative to index.html)
HTML = [
    "components/list_button/list_button.html",
    "components/main_menu_button/main_menu_button.html",
    "components/toggle/toggle.html",
    "components/stepper/stepper.html",
    "components/text_input/text_input.html",
    "components/category_divider/category_divider.html",
    "menu_options/menu_options.html",
    "menu_main/menu_main.html",
    "title_screen/title_screen.html",
    "echo_portrait/echo_portrait.html",
    "dialog_popup/dialog_popup.html",
]
# Files used directly by the page
FILES = [
    "_shared/fonts/Industry-Book.ttf",
    "_shared/fonts/Industry-Medium.ttf",
    "_shared/fonts/Industry-MediumItalic.ttf",
    "_shared/fonts/Industry-Demi.ttf",
    "_shared/fonts/Industry-Bold.ttf",
    "_shared/fonts/IndustryMono-Book.ttf",
    "_shared/fonts/IndustryMono-Medium.ttf",
    "_shared/fonts/IndustryMono-Demi.ttf",
    "echo_portrait/assets/UI_Art_Assets/echo_portrait_art/art_echo_portrait_claptrap.png",
    "menu_news/assets/image_large/BL4_News_Claptrap_Full_1920x1080.png",
    "menu_news/assets/image_large/OAK2_TGASCREEN_1920.png",
    "menu_equip/assets/BG.png",
    "loading_spinner/assets/VaultSpinner.png",  # 8x8 frames of 256 px
    "components/menu_dropdown/assets/claptrap.png",
    "components/menu_dropdown/assets/vault_symbol.png",
    *(f"title_screen/assets/ui_art_assets/ts_elec_{i:02}.png" for i in range(1, 16)),
    *(f"title_screen/assets/ui_art_assets/ts_logo_elec_{i:02}.png" for i in range(1, 8)),
    *(f"title_screen/assets/ui_art_assets/ts_logo_elec_idle_{i:02}.png" for i in range(1, 9)),
]

# Not needed by an English page: localized fonts (~30 MB), DLC (common:// etc.) links
SKIP = re.compile(r"^_shared/fonts/[^/]+/|:/")
# Coherent-only CSS: its colour matrix filter (how the game tints its white textures) becomes an
# SVG feColorMatrix the page puts in its document (game/_setup/filters.svg), its text-stroke the
# -webkit- one
COH_MATRIX = re.compile(r"coh-color-matrix\(([^)]*)\)")
TEXT_STROKE = re.compile(r"(?<![-\w])text-stroke\s*:")
COUI = re.compile(r"coui://uiresources/([^\"')\s]+)", re.I)
URL = re.compile(r"url\(\s*[\"']?([^\"')]+)[\"']?\s*\)", re.I)


def _find(rel: str) -> Path | None:
    """Paths in the game's CSS don't always match the files' case."""
    path = DUMP / rel
    if path.is_file():
        return path
    parent = DUMP
    for part in PurePosixPath(rel).parts:
        try:
            match = next((c for c in parent.iterdir() if c.name.lower() == part.lower()), None)
        except OSError:
            return None
        if match is None:
            return None
        parent = match
    return parent if parent.is_file() else None


def make_claptrap(src: Path, dst: Path) -> None:
    """
    Claptrap cut out of the news art (he's drawn on flat red): the red reached from the picture's
    edges goes, then enclosed pockets of the same pure red (between his wheel struts) unless
    small. His own reds (warning plate, paint) are duller, so they stay.
    """
    from collections import deque  # noqa: PLC0415

    import numpy as np  # noqa: PLC0415
    from PIL import Image, ImageFilter  # noqa: PLC0415

    im = Image.open(src).convert("RGB")
    a = np.asarray(im).astype(int)
    r, g, b = a[..., 0], a[..., 1], a[..., 2]
    red = (r > 140) & (r - g > 70) & (r - b > 90)  # the background and its orange glow
    pure = (r > 200) & (g < 35) & (b < 45)  # the background's own red
    h, w = red.shape
    gone = np.zeros_like(red)
    labels = np.zeros((h, w), dtype=np.int32)

    def fill(y0: int, x0: int, mask: np.ndarray, mark: np.ndarray, value: int = 1) -> list[tuple[int, int]]:
        pts = []
        q = deque([(y0, x0)])
        mark[y0, x0] = value
        while q:
            y, x = q.popleft()
            pts.append((y, x))
            for ny, nx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= ny < h and 0 <= nx < w and mask[ny, nx] and not mark[ny, nx]:
                    mark[ny, nx] = value
                    q.append((ny, nx))
        return pts

    for x in range(w):
        for y in (0, h - 1):
            if red[y, x] and not gone[y, x]:
                fill(y, x, red, gone)
    for y in range(h):
        for x in (0, w - 1):
            if red[y, x] and not gone[y, x]:
                fill(y, x, red, gone)
    pocket = pure & ~gone
    n = 0
    for y, x in zip(*np.nonzero(pocket)):
        if not labels[y, x]:
            n += 1
            pts = fill(y, x, pocket, labels, n)
            if len(pts) > 150:
                ys, xs = zip(*pts)
                gone[list(ys), list(xs)] = True
    alpha = Image.fromarray(np.where(gone, 0, 255).astype("uint8"))
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(1.2))
    out = im.convert("RGBA")
    out.putalpha(alpha)
    out = out.crop(alpha.getbbox())
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, optimize=True)


_matrices: dict[str, str] = {}  # values -> filter id


def _matrix_id(values: str) -> str:
    key = " ".join(v.strip() for v in values.split(","))
    return _matrices.setdefault(key, f"cm{len(_matrices)}")


def _write_filters(path: Path) -> None:
    filters = "".join(
        f'<filter id="{fid}" color-interpolation-filters="sRGB"><feColorMatrix type="matrix" values="{values}"/></filter>'
        for values, fid in _matrices.items()
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="0" height="0" style="position:absolute">{filters}</svg>',
                    encoding="utf-8")  # fmt: skip


def make_icon(src: Path, out_dir: Path) -> None:
    """The app's icon: the lit vault symbol from the title screen's menu panel."""
    from PIL import Image  # noqa: PLC0415

    im = Image.open(src).convert("RGBA")
    im = im.crop(im.getchannel("A").getbbox())
    side = max(im.size)
    square = Image.new("RGBA", (side, side))
    square.alpha_composite(im, ((side - im.width) // 2, (side - im.height) // 2))
    big = square.resize((256, 256), Image.LANCZOS)
    out_dir.mkdir(parents=True, exist_ok=True)
    big.save(out_dir / "icon.png", optimize=True)
    big.save(out_dir / "icon.ico", sizes=[(16, 16), (20, 20), (24, 24), (32, 32), (40, 40), (48, 48), (64, 64), (128, 128), (256, 256)])


def _relative(target: str, from_file: str) -> str:
    return os.path.relpath(target, os.path.dirname(from_file) or ".").replace(os.sep, "/")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--oodle", type=Path, help="refresh the dump first (dev/re/ui_extract.py)")
    parser.add_argument("--dump", type=Path, default=DUMP)
    args = parser.parse_args()
    globals()["DUMP"] = args.dump
    if args.oodle:
        subprocess.run([sys.executable, str(REPO / "dev" / "re" / "ui_extract.py"), "--oodle", str(args.oodle),
                        "--out", str(args.dump)], check=True)  # fmt: skip

    shutil.rmtree(OUT, ignore_errors=True)
    done: set[str] = set()
    missing: list[str] = []
    queue = [*CSS, *HTML, *FILES]
    while queue:
        rel = queue.pop()
        key = rel.lower()
        if key in done or SKIP.search(key):
            continue
        done.add(key)
        src = _find(rel)
        if src is None:
            missing.append(rel)
            continue
        dst = OUT / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.suffix.lower() == ".css":
            text = src.read_text(encoding="utf-8", errors="replace")
            refs = [m.group(1) for m in COUI.finditer(text)]
            # Relative url()s inside the game's CSS, resolved against the stylesheet
            for m in URL.finditer(text):
                ref = m.group(1).strip()
                if not ref.lower().startswith(("coui:", "data:", "http", "#", "texture2d", "/")):
                    refs.append(posixpath.normpath(str(PurePosixPath(rel).parent / ref)))
            queue.extend(r.split("?")[0] for r in refs)
            text = COUI.sub(lambda m: _relative(m.group(1), rel), text)
            text = COH_MATRIX.sub(lambda m: f"url(#{_matrix_id(m.group(1))})", text)
            text = TEXT_STROKE.sub("-webkit-text-stroke:", text)
            dst.write_text(text, encoding="utf-8")
        elif src.suffix.lower() == ".html":
            text = src.read_text(encoding="utf-8", errors="replace")
            queue.extend(r for r in (m.group(1) for m in COUI.finditer(text)) if not r.endswith((".js", ".data", ".css")))
            dst.write_text(COUI.sub(lambda m: "game/" + m.group(1), text), encoding="utf-8")
        else:
            shutil.copyfile(src, dst)
    _write_filters(OUT / "_setup/filters.svg")
    make_icon(DUMP / "menu_main/assets/ui_art_asset/menu_panel_logo.png", OUT / "_setup")
    make_claptrap(DUMP / "menu_news/assets/image_large/BL4_News_Claptrap_Full_1920x1080.png", OUT / "_setup/claptrap.png")
    size = sum(f.stat().st_size for f in OUT.rglob("*") if f.is_file())
    print(f"{len(done) - len(missing)} files, {size / 1e6:.1f} MB -> {OUT}")
    if missing:
        print(f"{len(missing)} referenced files not in the dump (fine for unused states): {', '.join(sorted(missing)[:8])}...")


if __name__ == "__main__":
    main()
